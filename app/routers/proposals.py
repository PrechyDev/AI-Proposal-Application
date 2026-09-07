import logging
import re
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_can_create, require_user
from app.config import get_settings
from app.db import get_db
from app.models import (
    ApprovalComment,
    DeliveryLog,
    Proposal,
    ProposalReference,
    ReferenceFile,
    Section,
    SectionHistory,
    User,
)
from app.services.email import EmailError, send_email
from app.services.proposal_generation import (
    MAX_REGENERATIONS_PER_SECTION,
    SECTION_KEYS,
    SECTION_TITLES,
    GenerationError,
    generate_proposal_sections,
    regenerate_section,
)
from app.services.reference_files import ReferenceFileError, parse_tags, upload_reference_file
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/proposals")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Required per assets/intake-form-fields.md - blank is a form-validation
# error; filler/placeholder *content* is Claude's job to catch at
# generation time (spec section 7), not this form's.
REQUIRED_TEXT_FIELDS = [
    ("client_name", "Client name"),
    ("company_name", "Company name"),
    ("client_needs_summary", "Summary of client's needs"),
    ("project_scope", "Project scope"),
    ("goals_and_objectives", "Goals and objectives"),
    ("recommended_services", "Recommended services or deliverables"),
    ("proposed_timeline", "Proposed timeline"),
    ("estimated_pricing", "Estimated pricing"),
]


@router.get("/new")
def new_proposal_form(request: Request, user: User = Depends(require_can_create)):
    return templates.TemplateResponse(
        request=request, name="proposal_new.html", context={"error": None, "values": {}}
    )


@router.post("/new")
def create_proposal(
    request: Request,
    # Defaults ("" rather than Form(...)) matter here: Starlette's
    # urlencoded-form parser drops blank-valued fields entirely rather than
    # keeping them as "", so a required Form(...) field left empty in the
    # browser would 422 before ever reaching the validation below.
    client_name: str = Form(""),
    client_email: str = Form(""),
    company_name: str = Form(""),
    date_of_call: str = Form(""),
    client_needs_summary: str = Form(""),
    project_scope: str = Form(""),
    goals_and_objectives: str = Form(""),
    recommended_services: str = Form(""),
    proposed_timeline: str = Form(""),
    estimated_pricing: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    values = {
        "client_name": client_name,
        "client_email": client_email,
        "company_name": company_name,
        "date_of_call": date_of_call,
        "client_needs_summary": client_needs_summary,
        "project_scope": project_scope,
        "goals_and_objectives": goals_and_objectives,
        "recommended_services": recommended_services,
        "proposed_timeline": proposed_timeline,
        "estimated_pricing": estimated_pricing,
    }

    errors = []
    for field, label in REQUIRED_TEXT_FIELDS:
        if not values[field].strip():
            errors.append(f"{label} is required.")

    if not EMAIL_RE.match(client_email.strip()):
        errors.append("Client email must be a valid email address.")

    parsed_date = None
    try:
        parsed_date = datetime.strptime(date_of_call.strip(), "%Y-%m-%d")
    except ValueError:
        errors.append("Date of call must be a valid date.")

    if errors:
        return templates.TemplateResponse(
            request=request,
            name="proposal_new.html",
            context={"error": " ".join(errors), "values": values},
            status_code=400,
        )

    proposal = Proposal(
        client_name=client_name.strip(),
        client_email=client_email.strip().lower(),
        company_name=company_name.strip(),
        date_of_call=parsed_date,
        client_needs_summary=client_needs_summary.strip(),
        project_scope=project_scope.strip(),
        goals_and_objectives=goals_and_objectives.strip(),
        recommended_services=recommended_services.strip(),
        proposed_timeline=proposed_timeline.strip(),
        estimated_pricing=estimated_pricing.strip(),
        created_by=user.id,
        status="draft",
    )
    db.add(proposal)
    db.commit()
    db.refresh(proposal)
    logger.info("User id=%s created proposal id=%s", user.id, proposal.id)
    return RedirectResponse(url=f"/proposals/{proposal.id}", status_code=303)


def _get_viewable_proposal(proposal_id: int, db: Session, user: User) -> Proposal:
    proposal = db.get(Proposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposal not found")
    if proposal.created_by != user.id and not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted to view this proposal")
    return proposal


def _get_attached_references(proposal: Proposal, db: Session) -> list[ReferenceFile]:
    return db.execute(
        select(ReferenceFile)
        .join(ProposalReference, ProposalReference.reference_file_id == ReferenceFile.id)
        .where(ProposalReference.proposal_id == proposal.id)
        .order_by(ReferenceFile.created_at)
    ).scalars().all()


def _get_attachable_library_files(proposal: Proposal, db: Session) -> list[ReferenceFile]:
    attached_ids = select(ProposalReference.reference_file_id).where(ProposalReference.proposal_id == proposal.id)
    return db.execute(
        select(ReferenceFile)
        .where(ReferenceFile.is_library.is_(True), ReferenceFile.id.notin_(attached_ids))
        .order_by(ReferenceFile.name)
    ).scalars().all()


def _get_approvers(db: Session) -> list[User]:
    return db.execute(
        select(User).where(User.can_approve.is_(True), User.is_active.is_(True)).order_by(User.name)
    ).scalars().all()


def _render_detail(request: Request, db: Session, proposal: Proposal, error: str | None = None, status_code: int = 200):
    sections = db.execute(
        select(Section).where(Section.proposal_id == proposal.id).order_by(Section.sort_order)
    ).scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="proposal_detail.html",
        context={
            "proposal": proposal,
            "sections": sections,
            "error": error,
            "attached_references": _get_attached_references(proposal, db),
            "attachable_library_files": _get_attachable_library_files(proposal, db),
            "approvers": _get_approvers(db),
        },
        status_code=status_code,
    )


@router.get("/{proposal_id}")
def view_proposal(
    proposal_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    proposal = _get_viewable_proposal(proposal_id, db, user)
    return _render_detail(request, db, proposal)


@router.post("/{proposal_id}/submit")
def submit_proposal(
    proposal_id: int,
    request: Request,
    approver_id: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    proposal = _get_viewable_proposal(proposal_id, db, user)

    if proposal.status not in ("draft", "changes_requested"):
        return _render_detail(
            request, db, proposal,
            error=f"Can't submit a proposal that is already '{proposal.status}'.",
            status_code=400,
        )
    if not db.execute(select(Section).where(Section.proposal_id == proposal.id)).scalars().first():
        return _render_detail(
            request, db, proposal, error="Generate the proposal before submitting it.", status_code=400
        )

    approver = None
    if approver_id.strip().isdigit():
        approver = db.execute(
            select(User).where(
                User.id == int(approver_id), User.can_approve.is_(True), User.is_active.is_(True)
            )
        ).scalar_one_or_none()
    if approver is None:
        return _render_detail(
            request, db, proposal, error="Pick a valid approver to submit this proposal.", status_code=400
        )

    proposal.approver_id = approver.id
    proposal.status = "pending_approval"
    db.commit()
    logger.info("User id=%s submitted proposal id=%s to approver id=%s", user.id, proposal.id, approver.id)

    settings = get_settings()
    review_url = f"{settings.app_base_url}/proposals/{proposal.id}/approve"
    try:
        send_email(
            to=approver.email,
            subject=f"Proposal ready for your review: {proposal.client_name} ({proposal.company_name})",
            html=(
                f"<p>A proposal for <strong>{proposal.client_name}</strong> "
                f"({proposal.company_name}) is ready for your review.</p>"
                f'<p><a href="{review_url}">Log in and review it</a>.</p>'
            ),
        )
        db.add(DeliveryLog(proposal_id=proposal.id, channel="approver_notification", status="success"))
    except EmailError as exc:
        logger.warning("Approver notification email failed for proposal id=%s: %s", proposal.id, exc)
        db.add(
            DeliveryLog(
                proposal_id=proposal.id, channel="approver_notification", status="failed",
                error_message=str(exc),
            )
        )
    db.commit()

    return RedirectResponse(url=f"/proposals/{proposal.id}", status_code=303)


@router.post("/{proposal_id}/reopen")
def reopen_proposal(
    proposal_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    proposal = _get_viewable_proposal(proposal_id, db, user)

    if proposal.status not in ("approved", "sent"):
        return _render_detail(
            request, db, proposal,
            error=f"Can't reopen a proposal that is '{proposal.status}' - only an approved or sent one.",
            status_code=400,
        )

    # Invalidates the old client link immediately (spec section 7: "no live
    # client link ever points at stale content") - a new token is issued the
    # next time this proposal is approved (step 10), not here, since an
    # unapproved/reopened proposal shouldn't have a live client-facing link
    # at all.
    proposal.status = "draft"
    proposal.client_token = None
    proposal.token_expires_at = None
    db.commit()
    logger.info("User id=%s reopened proposal id=%s", user.id, proposal.id)
    return RedirectResponse(url=f"/proposals/{proposal.id}", status_code=303)


@router.post("/{proposal_id}/references/attach")
def attach_references(
    proposal_id: int,
    request: Request,
    reference_file_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    proposal = _get_viewable_proposal(proposal_id, db, user)

    already_attached = {
        row for row in db.execute(
            select(ProposalReference.reference_file_id).where(ProposalReference.proposal_id == proposal.id)
        ).scalars().all()
    }
    # Only attach files that actually exist in the library - a stale/tampered
    # checkbox value pointing at a retired or nonexistent id is silently
    # ignored rather than erroring the whole request.
    valid_library_ids = {
        row for row in db.execute(
            select(ReferenceFile.id).where(ReferenceFile.is_library.is_(True))
        ).scalars().all()
    }
    for reference_file_id in reference_file_ids:
        if reference_file_id in already_attached or reference_file_id not in valid_library_ids:
            continue
        db.add(ProposalReference(proposal_id=proposal.id, reference_file_id=reference_file_id))
    db.commit()
    logger.info("User id=%s attached reference files %s to proposal id=%s", user.id, reference_file_ids, proposal.id)
    return RedirectResponse(url=f"/proposals/{proposal.id}", status_code=303)


@router.post("/{proposal_id}/references/{reference_file_id}/remove")
def remove_reference(
    proposal_id: int,
    reference_file_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    proposal = _get_viewable_proposal(proposal_id, db, user)
    # Detaches from this proposal only - never deletes the ReferenceFile row
    # or its storage object, so it stays available (in the library, or still
    # attached to any other proposal that also cites it).
    link = db.execute(
        select(ProposalReference).where(
            ProposalReference.proposal_id == proposal.id,
            ProposalReference.reference_file_id == reference_file_id,
        )
    ).scalar_one_or_none()
    if link is not None:
        db.delete(link)
        db.commit()
        logger.info("User id=%s removed reference file id=%s from proposal id=%s", user.id, reference_file_id, proposal.id)
    return RedirectResponse(url=f"/proposals/{proposal.id}", status_code=303)


@router.post("/{proposal_id}/references/upload")
def upload_reference_for_proposal(
    proposal_id: int,
    request: Request,
    file: UploadFile = File(...),
    tags: str = Form(""),
    add_to_library: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    proposal = _get_viewable_proposal(proposal_id, db, user)
    content = file.file.read()
    try:
        storage_path = upload_reference_file(file.filename or "", content)
    except ReferenceFileError as exc:
        return _render_detail(request, db, proposal, error=str(exc), status_code=400)

    reference_file = ReferenceFile(
        name=file.filename,
        storage_path=storage_path,
        tags=parse_tags(tags),
        is_library=(add_to_library == "yes"),
        uploaded_by=user.id,
    )
    db.add(reference_file)
    db.flush()
    db.add(ProposalReference(proposal_id=proposal.id, reference_file_id=reference_file.id))
    db.commit()
    logger.info(
        "User id=%s uploaded reference file id=%s for proposal id=%s (added to library: %s)",
        user.id, reference_file.id, proposal.id, reference_file.is_library,
    )
    return RedirectResponse(url=f"/proposals/{proposal.id}", status_code=303)


@router.post("/{proposal_id}/generate")
def generate_sections(
    proposal_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    proposal = _get_viewable_proposal(proposal_id, db, user)

    existing_count = db.execute(
        select(Section).where(Section.proposal_id == proposal.id)
    ).scalars().first()
    if existing_count is not None:
        return _render_detail(
            request, db, proposal,
            error="This proposal already has generated sections. Use per-section regenerate to update them.",
            status_code=400,
        )

    try:
        generated = generate_proposal_sections(proposal, db)
    except GenerationError:
        return _render_detail(
            request, db, proposal,
            error="Generation failed - the AI service did not return a usable proposal. Please try again.",
            status_code=502,
        )

    for sort_order, key in enumerate(SECTION_KEYS):
        section_output = getattr(generated, key)
        db.add(
            Section(
                proposal_id=proposal.id,
                section_key=key,
                content=section_output.content,
                sort_order=sort_order,
                has_gap_marker=section_output.has_gap,
            )
        )
    db.commit()
    logger.info("Generated %d sections for proposal id=%s", len(SECTION_KEYS), proposal.id)
    return RedirectResponse(url=f"/proposals/{proposal.id}", status_code=303)


def _get_section(proposal: Proposal, section_key: str, db: Session) -> Section:
    section = db.execute(
        select(Section).where(Section.proposal_id == proposal.id, Section.section_key == section_key)
    ).scalar_one_or_none()
    if section is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Section not found")
    return section


def _build_section_views(db: Session, proposal: Proposal) -> list[dict]:
    sections = db.execute(
        select(Section).where(Section.proposal_id == proposal.id).order_by(Section.sort_order)
    ).scalars().all()

    all_history = db.execute(
        select(SectionHistory)
        .where(SectionHistory.section_id.in_([s.id for s in sections]))
        .order_by(SectionHistory.created_at.desc())
    ).scalars().all()

    all_comments = db.execute(
        select(ApprovalComment)
        .where(ApprovalComment.proposal_id == proposal.id)
        .order_by(ApprovalComment.created_at.desc())
    ).scalars().all()

    author_ids = {h.changed_by for h in all_history} | {c.created_by for c in all_comments}
    authors = {u.id: u.name for u in db.execute(select(User).where(User.id.in_(author_ids))).scalars().all()}

    views = []
    for section in sections:
        history = [h for h in all_history if h.section_id == section.id]
        regenerate_count = sum(1 for h in history if h.change_type == "regenerate")
        comments = [c for c in all_comments if c.section_key == section.section_key]
        views.append({
            "section": section,
            "title": SECTION_TITLES.get(section.section_key, section.section_key),
            "history": history,
            "authors": authors,
            "regenerate_count": regenerate_count,
            "regenerate_limit": MAX_REGENERATIONS_PER_SECTION,
            "pending_manual_edit": bool(history) and history[0].change_type == "manual_edit",
            "comments": comments,
            "unresolved_comments": [c for c in comments if not c.resolved],
        })
    return views


def _render_edit(
    request: Request,
    db: Session,
    proposal: Proposal,
    error: str | None = None,
    status_code: int = 200,
    pending_section_key: str | None = None,
    pending_comment: str = "",
):
    return templates.TemplateResponse(
        request=request,
        name="proposal_edit.html",
        context={
            "proposal": proposal,
            "section_views": _build_section_views(db, proposal),
            "error": error,
            "pending_section_key": pending_section_key,
            "pending_comment": pending_comment,
            "attached_references": _get_attached_references(proposal, db),
        },
        status_code=status_code,
    )


@router.get("/{proposal_id}/edit")
def edit_proposal(
    proposal_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    proposal = _get_viewable_proposal(proposal_id, db, user)
    if not db.execute(select(Section).where(Section.proposal_id == proposal.id)).scalars().first():
        return RedirectResponse(url=f"/proposals/{proposal.id}", status_code=303)
    return _render_edit(request, db, proposal)


@router.post("/{proposal_id}/sections/{section_key}/edit")
def edit_section(
    proposal_id: int,
    section_key: str,
    request: Request,
    content: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    proposal = _get_viewable_proposal(proposal_id, db, user)
    section = _get_section(proposal, section_key, db)

    new_content = content.strip()
    if not new_content:
        return _render_edit(request, db, proposal, error="Section content can't be empty.", status_code=400)

    old_content = section.content
    section.content = new_content
    # A manual edit is how a salesperson resolves a flagged gap - trust
    # their judgment that real content now exists.
    section.has_gap_marker = False
    db.add(
        SectionHistory(
            section_id=section.id,
            old_content=old_content,
            new_content=new_content,
            change_type="manual_edit",
            triggering_comment=None,
            changed_by=user.id,
        )
    )
    db.commit()
    logger.info("User id=%s manually edited section id=%s (proposal id=%s)", user.id, section.id, proposal.id)
    return RedirectResponse(url=f"/proposals/{proposal.id}/edit", status_code=303)


@router.post("/{proposal_id}/sections/{section_key}/regenerate")
def regenerate_section_route(
    proposal_id: int,
    section_key: str,
    request: Request,
    comment: str = Form(""),
    confirm_overwrite: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    proposal = _get_viewable_proposal(proposal_id, db, user)
    section = _get_section(proposal, section_key, db)
    title = SECTION_TITLES.get(section_key, section_key)

    history = db.execute(
        select(SectionHistory)
        .where(SectionHistory.section_id == section.id)
        .order_by(SectionHistory.created_at.desc())
    ).scalars().all()

    regenerate_count = sum(1 for h in history if h.change_type == "regenerate")
    if regenerate_count >= MAX_REGENERATIONS_PER_SECTION:
        return _render_edit(
            request, db, proposal,
            error=f"'{title}' has reached its regeneration limit ({MAX_REGENERATIONS_PER_SECTION}). "
            f"Edit it manually instead.",
            status_code=400,
        )

    pending_manual_edit = bool(history) and history[0].change_type == "manual_edit"
    if pending_manual_edit and confirm_overwrite != "yes":
        return _render_edit(
            request, db, proposal,
            error=f"'{title}' has a manual edit that regenerating would overwrite.",
            status_code=409,
            pending_section_key=section_key,
            pending_comment=comment,
        )

    try:
        result = regenerate_section(proposal, section_key, section.content, comment.strip() or None, db)
    except GenerationError:
        return _render_edit(
            request, db, proposal,
            error="Regeneration failed - the AI service did not return a usable section. Please try again.",
            status_code=502,
        )

    old_content = section.content
    section.content = result.content
    section.has_gap_marker = result.has_gap
    db.add(
        SectionHistory(
            section_id=section.id,
            old_content=old_content,
            new_content=result.content,
            change_type="regenerate",
            triggering_comment=comment.strip() or None,
            changed_by=user.id,
        )
    )
    db.commit()
    logger.info("User id=%s regenerated section id=%s (proposal id=%s)", user.id, section.id, proposal.id)
    return RedirectResponse(url=f"/proposals/{proposal.id}/edit", status_code=303)


@router.post("/{proposal_id}/comments/{comment_id}/resolve")
def resolve_comment(
    proposal_id: int,
    comment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    proposal = _get_viewable_proposal(proposal_id, db, user)
    comment = db.execute(
        select(ApprovalComment).where(
            ApprovalComment.id == comment_id, ApprovalComment.proposal_id == proposal.id
        )
    ).scalar_one_or_none()
    if comment is not None:
        comment.resolved = True
        db.commit()
        logger.info("User id=%s resolved approval comment id=%s (proposal id=%s)", user.id, comment_id, proposal.id)
    return RedirectResponse(url=f"/proposals/{proposal.id}/edit", status_code=303)
