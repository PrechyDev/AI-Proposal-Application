import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_can_approve
from app.config import get_settings
from app.db import get_db
from app.models import (
    ApprovalComment,
    DeliveryLog,
    Proposal,
    ProposalReference,
    ReferenceFile,
    Section,
    Snapshot,
    User,
)
from app.services.email import EmailError, send_email
from app.services.proposal_generation import SECTION_TITLES
from app.templating import templates

logger = logging.getLogger(__name__)

# Client link expiry, per spec section 7/10.
CLIENT_TOKEN_EXPIRY_DAYS = 30

router = APIRouter(prefix="/proposals")


def _get_proposal_for_approver(proposal_id: int, db: Session, user: User) -> Proposal:
    proposal = db.get(Proposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposal not found")
    if proposal.approver_id != user.id and not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not the assigned approver for this proposal"
        )
    return proposal


def _get_references(proposal: Proposal, db: Session) -> list[ReferenceFile]:
    return db.execute(
        select(ReferenceFile)
        .join(ProposalReference, ProposalReference.reference_file_id == ReferenceFile.id)
        .where(ProposalReference.proposal_id == proposal.id)
        .order_by(ReferenceFile.created_at)
    ).scalars().all()


def _build_snapshot_content(proposal: Proposal, sections: list[Section], references: list[ReferenceFile]) -> dict:
    """Frozen "what the client saw" record (spec section 3/5) - immutable
    once written, never edited even if the proposal is later reopened."""
    return {
        "proposal": {
            "client_name": proposal.client_name,
            "client_email": proposal.client_email,
            "company_name": proposal.company_name,
            "date_of_call": proposal.date_of_call.isoformat(),
            "client_needs_summary": proposal.client_needs_summary,
            "project_scope": proposal.project_scope,
            "goals_and_objectives": proposal.goals_and_objectives,
            "recommended_services": proposal.recommended_services,
            "proposed_timeline": proposal.proposed_timeline,
            "estimated_pricing": proposal.estimated_pricing,
        },
        "sections": [
            {
                "section_key": s.section_key,
                "title": SECTION_TITLES.get(s.section_key, s.section_key),
                "content": s.content,
            }
            for s in sections
        ],
        "references": [{"name": r.name, "tags": r.tags} for r in references],
    }


def _render_approve(
    request: Request, db: Session, proposal: Proposal, error: str | None = None, status_code: int = 200
):
    sections = db.execute(
        select(Section).where(Section.proposal_id == proposal.id).order_by(Section.sort_order)
    ).scalars().all()
    comments = db.execute(
        select(ApprovalComment)
        .where(ApprovalComment.proposal_id == proposal.id)
        .order_by(ApprovalComment.created_at.desc())
    ).scalars().all()

    section_views = [
        {
            "section": s,
            "title": SECTION_TITLES.get(s.section_key, s.section_key),
            "comments": [c for c in comments if c.section_key == s.section_key],
        }
        for s in sections
    ]

    return templates.TemplateResponse(
        request=request,
        name="proposal_approve.html",
        context={
            "proposal": proposal,
            "section_views": section_views,
            "attached_references": _get_references(proposal, db),
            "has_unresolved_gap": any(s.has_gap_marker for s in sections),
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/{proposal_id}/approve")
def approve_page(
    proposal_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_can_approve)
):
    proposal = _get_proposal_for_approver(proposal_id, db, user)
    return _render_approve(request, db, proposal)


@router.post("/{proposal_id}/approve")
def approve_proposal(
    proposal_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_can_approve)
):
    proposal = _get_proposal_for_approver(proposal_id, db, user)

    if proposal.status != "pending_approval":
        return _render_approve(
            request, db, proposal,
            error=f"This proposal is '{proposal.status}', not pending approval.",
            status_code=400,
        )

    sections = db.execute(
        select(Section).where(Section.proposal_id == proposal.id).order_by(Section.sort_order)
    ).scalars().all()
    if any(s.has_gap_marker for s in sections):
        return _render_approve(
            request, db, proposal,
            error="Every section must have its gap marker resolved before this proposal can be approved.",
            status_code=400,
        )

    references = _get_references(proposal, db)
    next_version = (
        db.execute(
            select(func.max(Snapshot.version_number)).where(Snapshot.proposal_id == proposal.id)
        ).scalar() or 0
    ) + 1
    db.add(
        Snapshot(
            proposal_id=proposal.id,
            version_number=next_version,
            full_content_json=_build_snapshot_content(proposal, sections, references),
        )
    )

    proposal.status = "approved"
    proposal.client_token = uuid.uuid4()
    proposal.token_expires_at = datetime.now(timezone.utc) + timedelta(days=CLIENT_TOKEN_EXPIRY_DAYS)
    db.commit()
    logger.info(
        "User id=%s approved proposal id=%s (snapshot v%d, new client token issued)",
        user.id, proposal.id, next_version,
    )
    return RedirectResponse(url=f"/proposals/{proposal.id}/approve", status_code=303)


@router.post("/{proposal_id}/request-changes")
def request_changes(
    proposal_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_can_approve),
    comment_introduction: str = Form(""),
    comment_proposed_solution: str = Form(""),
    comment_deliverables: str = Form(""),
    comment_timeline: str = Form(""),
    comment_pricing: str = Form(""),
    comment_next_steps: str = Form(""),
):
    proposal = _get_proposal_for_approver(proposal_id, db, user)

    if proposal.status != "pending_approval":
        return _render_approve(
            request, db, proposal,
            error=f"This proposal is '{proposal.status}', not pending approval.",
            status_code=400,
        )

    comments_by_key = {
        "introduction": comment_introduction,
        "proposed_solution": comment_proposed_solution,
        "deliverables": comment_deliverables,
        "timeline": comment_timeline,
        "pricing": comment_pricing,
        "next_steps": comment_next_steps,
    }
    provided = {key: text.strip() for key, text in comments_by_key.items() if text.strip()}
    if not provided:
        return _render_approve(
            request, db, proposal,
            error="Add at least one comment explaining what needs to change.",
            status_code=400,
        )

    for section_key, comment_text in provided.items():
        db.add(
            ApprovalComment(
                proposal_id=proposal.id, section_key=section_key, comment_text=comment_text, created_by=user.id
            )
        )
    proposal.status = "changes_requested"
    db.commit()
    logger.info(
        "User id=%s requested changes on proposal id=%s (%d section comment(s))",
        user.id, proposal.id, len(provided),
    )

    # created_by is a required FK, so `creator` is never actually None in
    # practice - the guard just avoids emailing the client if that ever
    # somehow weren't true, rather than falling back to some other address.
    creator = db.get(User, proposal.created_by)
    if creator is not None:
        settings = get_settings()
        edit_url = f"{settings.app_base_url}/proposals/{proposal.id}/edit"
        comment_list = "".join(
            f"<li><strong>{SECTION_TITLES.get(key, key)}:</strong> {text}</li>" for key, text in provided.items()
        )
        try:
            send_email(
                to=creator.email,
                subject=f"Changes requested on your proposal: {proposal.client_name} ({proposal.company_name})",
                html=(
                    f"<p>{user.name} requested changes on this proposal:</p>"
                    f"<ul>{comment_list}</ul>"
                    f'<p><a href="{edit_url}">Review and update it</a>.</p>'
                ),
            )
            db.add(DeliveryLog(proposal_id=proposal.id, channel="changes_requested_notification", status="success"))
        except EmailError as exc:
            logger.warning("Changes-requested notification email failed for proposal id=%s: %s", proposal.id, exc)
            db.add(
                DeliveryLog(
                    proposal_id=proposal.id, channel="changes_requested_notification", status="failed",
                    error_message=str(exc),
                )
            )
        db.commit()

    return RedirectResponse(url=f"/proposals/{proposal.id}/approve", status_code=303)
