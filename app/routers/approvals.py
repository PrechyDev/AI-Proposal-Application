import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_can_approve, require_can_create
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
from app.routers.proposals import _is_htmx, _regen_guard, _render_section_fragment, _render_workspace, get_role_flags
from app.services.email import EmailError, send_email
from app.services.proposal_generation import SECTION_TITLES
from app.templating import render_email

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


def _build_snapshot_content(
    proposal: Proposal, sections: list[Section], references: list[ReferenceFile], salesperson_name: str
) -> dict:
    """Frozen "what the client saw" record (spec section 3/5) - immutable
    once written, never edited even if the proposal is later reopened.

    `salesperson_name` is captured here (not read live from `created_by` at
    view time) so a frozen snapshot's attribution can never change out from
    under a client link - e.g. if the creator's account is later renamed or
    deactivated, matching this record's own "immutable once written" rule.
    """
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
            "salesperson_name": salesperson_name,
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


def _unresolved_comments(proposal: Proposal, db: Session) -> list[ApprovalComment]:
    return db.execute(
        select(ApprovalComment).where(
            ApprovalComment.proposal_id == proposal.id, ApprovalComment.resolved.is_(False)
        )
    ).scalars().all()


@router.get("/{proposal_id}/approve")
def approve_page_redirect(proposal_id: int):
    """The old separate approver-review page - now folded into the one
    unified workspace page. Kept as a redirect (not removed outright)
    since real emails already sent (approver notifications) link here."""
    return RedirectResponse(url=f"/proposals/{proposal_id}", status_code=303)


@router.post("/{proposal_id}/comments")
def add_comment(
    proposal_id: int,
    request: Request,
    section_key: str = Form(""),
    comment_text: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_can_approve),
):
    """Google-Docs-style: one comment added as its own small action while
    actively reviewing, not bundled into a single request-changes submit
    with a fixed per-section textarea. `section_key` empty means a
    whole-document comment (spec: ApprovalComment.section_key is nullable
    for exactly this case)."""
    proposal = _get_proposal_for_approver(proposal_id, db, user)
    section_key = section_key.strip() or None
    if (guard := _regen_guard(request, db, proposal, user, section_key=section_key)) is not None:
        return guard

    if proposal.status != "pending_approval":
        error = f"Can't comment on a proposal that is '{proposal.status}', not pending approval."
        if section_key and _is_htmx(request):
            return _render_section_fragment(request, db, proposal, section_key, user, section_error=error)
        return _render_workspace(request, db, proposal, user, error=error, status_code=400)

    comment_text = comment_text.strip()
    if not comment_text:
        error = "Comment text can't be empty."
        if section_key and _is_htmx(request):
            return _render_section_fragment(request, db, proposal, section_key, user, section_error=error)
        return _render_workspace(request, db, proposal, user, error=error, status_code=400)

    db.add(
        ApprovalComment(
            proposal_id=proposal.id, section_key=section_key, comment_text=comment_text, created_by=user.id
        )
    )
    db.commit()
    logger.info(
        "User id=%s added a comment to proposal id=%s (section=%s)",
        user.id, proposal.id, section_key or "whole-document",
    )
    # Whole-document comments have no single section to swap in place, so
    # they always take the full-page path even under HTMX.
    if section_key and _is_htmx(request):
        return _render_section_fragment(request, db, proposal, section_key, user)
    return RedirectResponse(url=f"/proposals/{proposal.id}", status_code=303)


def _freeze_and_approve(proposal: Proposal, sections: list[Section], db: Session, approver: User) -> int:
    """Shared by a normal approve and a self-approve - both end up at the
    exact same "approved" state through the exact same snapshot-freeze/
    token-issue path, so a self-approved proposal is indistinguishable
    from a normally-approved one except for who's recorded as approver_id.
    """
    references = _get_references(proposal, db)
    creator = db.get(User, proposal.created_by)
    next_version = (
        db.execute(
            select(func.max(Snapshot.version_number)).where(Snapshot.proposal_id == proposal.id)
        ).scalar() or 0
    ) + 1
    db.add(
        Snapshot(
            proposal_id=proposal.id,
            version_number=next_version,
            full_content_json=_build_snapshot_content(
                proposal, sections, references, creator.name if creator else "Koya Talent"
            ),
        )
    )
    proposal.approver_id = approver.id
    proposal.status = "approved"
    proposal.client_token = uuid.uuid4()
    proposal.token_expires_at = datetime.now(timezone.utc) + timedelta(days=CLIENT_TOKEN_EXPIRY_DAYS)
    db.commit()
    return next_version


@router.post("/{proposal_id}/approve")
def approve_proposal(
    proposal_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_can_approve)
):
    proposal = _get_proposal_for_approver(proposal_id, db, user)
    if (guard := _regen_guard(request, db, proposal, user)) is not None:
        return guard

    if proposal.status != "pending_approval":
        return _render_workspace(
            request, db, proposal, user,
            error=f"This proposal is '{proposal.status}', not pending approval.",
            status_code=400,
        )

    sections = db.execute(
        select(Section).where(Section.proposal_id == proposal.id).order_by(Section.sort_order)
    ).scalars().all()
    if any(s.has_gap_marker for s in sections):
        return _render_workspace(
            request, db, proposal, user,
            error="Every section must have its gap marker resolved before this proposal can be approved.",
            status_code=400,
        )
    if _unresolved_comments(proposal, db):
        return _render_workspace(
            request, db, proposal, user,
            error="This proposal has unresolved comments - request changes instead, or resolve them first.",
            status_code=400,
        )

    next_version = _freeze_and_approve(proposal, sections, db, approver=user)
    logger.info(
        "User id=%s approved proposal id=%s (snapshot v%d, new client token issued)",
        user.id, proposal.id, next_version,
    )
    # just_approved triggers a one-time "Approved! Send to client now?" modal
    # on the workspace page - stripped from the URL client-side after it
    # shows, so a refresh/back-nav doesn't keep re-triggering it.
    return RedirectResponse(url=f"/proposals/{proposal.id}?just_approved=1", status_code=303)


@router.post("/{proposal_id}/self-approve")
def self_approve_proposal(
    proposal_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_can_create)
):
    """One click, for someone who's going to approve their own proposal
    anyway - skips the submit -> wait -> come back and approve dance
    entirely, going straight from draft/changes_requested to approved
    through the same freeze/token path as a normal approval, and still
    surfaces the same post-approve "send to client now?" prompt. Only
    offered alongside the normal Submit for Approval button (never
    replacing it, so submitting to a different approver is still fully
    available) and only to whoever could actually approve something -
    an admin, or a user with can_approve - not to a plain salesperson
    self-approving their own work.
    """
    if not (user.is_admin or user.can_approve):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted to self-approve")

    proposal = db.get(Proposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposal not found")
    can_edit, _can_review = get_role_flags(proposal, user)
    if not can_edit:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted to edit this proposal")

    if (guard := _regen_guard(request, db, proposal, user)) is not None:
        return guard

    if proposal.status not in ("draft", "changes_requested"):
        return _render_workspace(
            request, db, proposal, user,
            error=f"Can't approve a proposal that is already '{proposal.status}'.",
            status_code=400,
        )

    sections = db.execute(
        select(Section).where(Section.proposal_id == proposal.id).order_by(Section.sort_order)
    ).scalars().all()
    if not sections:
        return _render_workspace(
            request, db, proposal, user, error="Generate the proposal before approving it.", status_code=400
        )
    if any(s.has_gap_marker for s in sections):
        return _render_workspace(
            request, db, proposal, user,
            error="Every section must have its gap marker resolved before this proposal can be approved.",
            status_code=400,
        )
    if _unresolved_comments(proposal, db):
        return _render_workspace(
            request, db, proposal, user,
            error="Resolve every comment from the last review round before approving.",
            status_code=400,
        )

    next_version = _freeze_and_approve(proposal, sections, db, approver=user)
    logger.info(
        "User id=%s self-approved proposal id=%s (snapshot v%d, new client token issued)",
        user.id, proposal.id, next_version,
    )
    return RedirectResponse(url=f"/proposals/{proposal.id}?just_approved=1", status_code=303)


@router.post("/{proposal_id}/request-changes")
def request_changes(
    proposal_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_can_approve),
):
    """A pure status transition now - comments are added one at a time via
    POST /comments while reviewing (Google-Docs style), not typed into a
    fixed per-section form in the same request as this action. Gated on
    at least one unresolved comment already existing - the mirror image
    of approve_proposal's "blocked while any unresolved comment exists."
    """
    proposal = _get_proposal_for_approver(proposal_id, db, user)
    if (guard := _regen_guard(request, db, proposal, user)) is not None:
        return guard

    if proposal.status != "pending_approval":
        return _render_workspace(
            request, db, proposal, user,
            error=f"This proposal is '{proposal.status}', not pending approval.",
            status_code=400,
        )

    unresolved = _unresolved_comments(proposal, db)
    if not unresolved:
        return _render_workspace(
            request, db, proposal, user,
            error="Add at least one comment explaining what needs to change before requesting changes.",
            status_code=400,
        )

    proposal.status = "changes_requested"
    db.commit()
    logger.info(
        "User id=%s requested changes on proposal id=%s (%d unresolved comment(s))",
        user.id, proposal.id, len(unresolved),
    )

    # created_by is a required FK, so `creator` is never actually None in
    # practice - the guard just avoids emailing the client if that ever
    # somehow weren't true, rather than falling back to some other address.
    creator = db.get(User, proposal.created_by)
    if creator is not None:
        settings = get_settings()
        edit_url = f"{settings.app_base_url}/proposals/{proposal.id}"
        comments_for_email = [
            {
                "title": SECTION_TITLES.get(c.section_key, c.section_key) if c.section_key else "Whole document",
                "text": c.comment_text,
            }
            for c in unresolved
        ]
        try:
            send_email(
                to=creator.email,
                subject=f"Changes requested on your proposal: {proposal.client_name} ({proposal.company_name})",
                html=render_email(
                    "changes_requested.html",
                    salesperson_name=creator.name,
                    approver_name=user.name,
                    comments=comments_for_email,
                    edit_url=edit_url,
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

    return RedirectResponse(url=f"/proposals/{proposal.id}", status_code=303)
