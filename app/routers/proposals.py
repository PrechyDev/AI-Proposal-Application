import logging
import re
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth import require_can_create, require_user
from app.config import get_settings
from app.db import SessionLocal, get_db
from app.pagination import paginate
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
from app.models.proposal import PROPOSAL_STATUSES
from app.services.email import EmailError, send_email
from app.services.proposal_generation import (
    MAX_REGENERATIONS_PER_SECTION,
    SECTION_KEYS,
    SECTION_TITLES,
    GenerationError,
    generate_proposal_sections,
    regenerate_section,
)
from app.services.reference_files import (
    ReferenceFileError,
    parse_tags,
    purge_orphaned_reference_files,
    upload_reference_file,
)
from app.templating import render_email, templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/proposals")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Blank is a form-validation error; filler/placeholder *content* is
# Claude's job to catch at generation time (spec section 7), not this
# form's. Split in two: the narrative fields are only required when
# there's no reference material to draw from instead - see
# _validate_intake_fields's has_references parameter.
ALWAYS_REQUIRED_TEXT_FIELDS = [
    ("client_name", "Client name"),
    ("company_name", "Company name"),
]
NARRATIVE_TEXT_FIELDS = [
    ("client_needs_summary", "Summary of client's needs"),
    ("project_scope", "Project scope"),
    ("goals_and_objectives", "Goals and objectives"),
    ("recommended_services", "Recommended services or deliverables"),
    ("proposed_timeline", "Proposed timeline"),
    ("estimated_pricing", "Estimated pricing"),
]


@router.get("")
def list_my_proposals(
    request: Request,
    tab: str = "all",
    status: str = "",
    page: int = 1,
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    """Spec section 4/5: "/proposals ... dashboard, filtered by
    created_by = me OR approver_id = me" - every status, not just what's
    actionable right now (that's what /dashboard's narrower widgets are
    for); this is the full history a salesperson or approver would want to
    browse. `tab` narrows that same base scope for an approver (who sees
    both their own created proposals and the ones they're reviewing mixed
    together) into "All" / "Created" / "Awaiting My Approval" - a
    salesperson with no approver relationships never has rows the latter
    two tabs would exclude, so the tabs only render when they'd do
    anything (`user.can_approve`). `status`, separately, is what the
    dashboard's clickable stat tiles link into - an optional further
    narrowing by one specific status, independent of which tab is active.
    """
    base_scope = or_(Proposal.created_by == user.id, Proposal.approver_id == user.id)
    query = select(Proposal).where(base_scope)
    if tab == "created":
        query = query.where(Proposal.created_by == user.id)
    elif tab == "awaiting":
        query = query.where(Proposal.approver_id == user.id, Proposal.status == "pending_approval")
    else:
        tab = "all"

    # A garbage/tampered status value is silently ignored (treated as no
    # filter) rather than erroring - same defensive posture already used
    # for reference_file_ids elsewhere in this file.
    if status not in PROPOSAL_STATUSES:
        status = ""
    if status:
        query = query.where(Proposal.status == status)

    proposals, pagination = paginate(db, query, Proposal.updated_at.desc(), page)

    person_ids = {p.created_by for p in proposals} | {p.approver_id for p in proposals if p.approver_id}
    people = {u.id: u.name for u in db.execute(select(User).where(User.id.in_(person_ids))).scalars().all()}

    extra_params = {"tab": tab}
    if status:
        extra_params["status"] = status

    context = {
        "proposals": proposals, "people": people, "user": user, "active_tab": tab,
        "pagination": pagination, "base_url": "/proposals",
        "extra_params": extra_params,
        "active_status": status,
        "pagination_hx_target": "#proposals-list-body",
    }
    # Tab/status switches on this page are HTMX partial swaps (see
    # _proposals_list_body.html) - a plain request (direct navigation,
    # curl, JS disabled) still gets the full page. A *boosted* request
    # (the dashboard tiles' hx-boost, a real page-to-page navigation) also
    # sends HX-Request, but is distinguished by HX-Boosted - it must get
    # the full page too, since boost swaps document.body wholesale and a
    # bare partial here would wipe out the navbar.
    is_boosted = request.headers.get("hx-boosted") == "true"
    template_name = "_proposals_list_body.html" if (_is_htmx(request) and not is_boosted) else "proposals_list.html"
    return templates.TemplateResponse(request=request, name=template_name, context=context)


MAX_NEW_PROPOSAL_REFERENCES = 5


def _render_new_proposal(
    request: Request,
    db: Session,
    user: User,
    values: dict | None = None,
    library_file_ids: list[int] | None = None,
    error: str | None = None,
    status_code: int = 200,
):
    """Reference selection is now deferred to the final submit entirely -
    there's no "already attached" state to carry across a re-render, so
    the only thing that needs restoring on a validation-failure re-render
    is which library checkboxes were checked (`library_file_ids`). Staged
    device files can't be restored - browsers never let a server response
    repopulate a file input - so that part is lost on a failed submit.
    """
    values = values or {}
    library_file_ids = library_file_ids or []

    attachable_library_files = db.execute(
        select(ReferenceFile)
        .where(ReferenceFile.is_library.is_(True), ReferenceFile.deleted_at.is_(None))
        .order_by(ReferenceFile.name)
    ).scalars().all()

    return templates.TemplateResponse(
        request=request,
        name="proposal_new.html",
        context={
            "user": user,
            "error": error,
            "values": values,
            "attachable_library_files": attachable_library_files,
            "library_file_ids": library_file_ids,
            "max_references": MAX_NEW_PROPOSAL_REFERENCES,
            "today": datetime.now(timezone.utc).date().isoformat(),
        },
        status_code=status_code,
    )


@router.get("/new")
def new_proposal_form(request: Request, db: Session = Depends(get_db), user: User = Depends(require_can_create)):
    purge_orphaned_reference_files(db)
    return _render_new_proposal(request, db, user)


def _validate_intake_fields(
    values: dict, date_of_call: str, has_references: bool = False
) -> tuple[list[str], datetime | None]:
    """has_references defaults to False (the strict/original behavior) so a
    caller that forgets to pass it fails safe, not permissive - this is the
    real validation gate, not just a courtesy the client-side JS mirrors."""
    errors = []
    for field, label in ALWAYS_REQUIRED_TEXT_FIELDS:
        if not values[field].strip():
            errors.append(f"{label} is required.")

    if not has_references:
        for field, label in NARRATIVE_TEXT_FIELDS:
            if not values[field].strip():
                errors.append(f"{label} is required.")

    if not EMAIL_RE.match(values["client_email"].strip()):
        errors.append("Client email must be a valid email address.")

    parsed_date = None
    try:
        parsed_date = datetime.strptime(date_of_call.strip(), "%Y-%m-%d")
    except ValueError:
        errors.append("Date of call must be a valid date.")
    else:
        # A discovery call can't have happened in the future - the `max`
        # attribute on the date input is just UX, this is the real gate.
        if parsed_date.date() > datetime.now(timezone.utc).date():
            errors.append("Date of call cannot be in the future.")
    return errors, parsed_date


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
    library_file_ids: list[int] = Form(default=[]),
    files: list[UploadFile] = File(default=[]),
    names: list[str] = Form(default=[]),
    descriptions: list[str] = Form(default=[]),
    tags: list[str] = Form(default=[]),
    add_to_library_indices: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    """Reference handling - both a library pick and a device upload - is
    deferred entirely to this one submit. No ReferenceFile or Proposal row
    is created until every cheap, no-I/O check has passed; only once
    validation clears does the function touch storage. If a device file
    then fails to upload, the whole request aborts (no Proposal row is
    created) and any file that *did* upload in that same failed attempt is
    left as a harmless orphan - the existing 7-day orphaned-reference-file
    purge (purge_orphaned_reference_files) already cleans those up, so no
    new rollback logic is needed here.
    """
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

    # Defensive re-check - the submitted ids could in principle be tampered
    # with client-side. Done before validation (not after) since
    # has_references below must reflect real, verified rows - never the raw
    # submitted ids, which a "Fill In From Reference Documents" submission
    # could otherwise spoof to skip narrative-field validation with
    # references that don't actually exist.
    valid_library_ids = list(
        db.execute(
            select(ReferenceFile.id).where(
                ReferenceFile.id.in_(library_file_ids),
                ReferenceFile.is_library.is_(True),
                ReferenceFile.deleted_at.is_(None),
            )
        ).scalars().all()
    )
    device_files = [f for f in files if f.filename]

    has_references = bool(valid_library_ids) or bool(device_files)
    errors, parsed_date = _validate_intake_fields(values, date_of_call, has_references=has_references)

    total_references = len(valid_library_ids) + len(device_files)
    if total_references > MAX_NEW_PROPOSAL_REFERENCES:
        errors.append(
            f"Maximum {MAX_NEW_PROPOSAL_REFERENCES} reference files per proposal - "
            "remove some before submitting."
        )

    if errors:
        return _render_new_proposal(
            request, db, user, values=values, library_file_ids=library_file_ids,
            error=" ".join(errors), status_code=400,
        )

    # Only now, with validation clear, does anything touch storage.
    add_to_library_set = set(add_to_library_indices)
    uploaded_ids = []
    upload_errors = []
    for index, (file, name, description, row_tags) in enumerate(zip(device_files, names, descriptions, tags)):
        content = file.file.read()
        try:
            storage_path = upload_reference_file(file.filename or "", content)
        except ReferenceFileError as exc:
            upload_errors.append(f"'{file.filename}': {exc}")
            continue

        reference_file = ReferenceFile(
            name=name.strip() or file.filename,
            description=description.strip() or None,
            storage_path=storage_path,
            tags=parse_tags(row_tags),
            is_library=(index in add_to_library_set),
            uploaded_by=user.id,
        )
        db.add(reference_file)
        db.flush()
        uploaded_ids.append(reference_file.id)

    if upload_errors:
        # Abort - no Proposal row is created. Anything that did upload above
        # is committed as an orphan; the 7-day purge picks it up, same
        # trade-off as abandoning this page mid-flow.
        db.commit()
        return _render_new_proposal(
            request, db, user, values=values, library_file_ids=library_file_ids,
            error=" ".join(upload_errors), status_code=400,
        )

    all_reference_ids = valid_library_ids + uploaded_ids

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

    for reference_file_id in all_reference_ids:
        db.add(ProposalReference(proposal_id=proposal.id, reference_file_id=reference_file_id))
    db.commit()
    logger.info(
        "User id=%s created proposal id=%s with %d reference file(s)",
        user.id, proposal.id, len(all_reference_ids),
    )

    # Whether or not Claude succeeds, intake + references are already saved -
    # the workspace page's own empty-state "Generate Proposal" button covers
    # a retry, so a generation failure here never loses the submission.
    _generate_sections_for_proposal(proposal, db)
    return RedirectResponse(url=f"/proposals/{proposal.id}", status_code=303)


def _get_viewable_proposal(proposal_id: int, db: Session, user: User) -> Proposal:
    proposal = db.get(Proposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposal not found")
    if proposal.created_by != user.id and not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted to view this proposal")
    return proposal


def get_role_flags(proposal: Proposal, user: User) -> tuple[bool, bool]:
    """(can_edit, can_review) - independent, not mutually exclusive: a
    self-approving user or an admin can have both at once on the same
    proposal, and the unified workspace page renders controls for each
    flag separately rather than picking one "mode." Admin bypasses both,
    matching the bypass this app has always given admins on both sides
    (_get_viewable_proposal here, _get_proposal_for_approver in
    approvals.py) - not a new decision, just named in one shared place.
    """
    can_edit = proposal.created_by == user.id or user.is_admin
    can_review = proposal.approver_id == user.id or user.is_admin
    return can_edit, can_review


def get_workspace_proposal(proposal_id: int, db: Session, user: User) -> tuple[Proposal, bool, bool]:
    proposal = db.get(Proposal, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposal not found")
    can_edit, can_review = get_role_flags(proposal, user)
    if not can_edit and not can_review:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not permitted to view this proposal")
    return proposal, can_edit, can_review


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


def _get_delivery_logs(proposal: Proposal, db: Session) -> list[DeliveryLog]:
    return db.execute(
        select(DeliveryLog)
        .where(DeliveryLog.proposal_id == proposal.id)
        .order_by(DeliveryLog.attempted_at.desc())
    ).scalars().all()


def _get_whole_doc_comments(proposal: Proposal, db: Session) -> list[ApprovalComment]:
    return db.execute(
        select(ApprovalComment)
        .where(ApprovalComment.proposal_id == proposal.id, ApprovalComment.section_key.is_(None))
        .order_by(ApprovalComment.created_at.desc())
    ).scalars().all()


def _compute_unresolved_flags(
    section_views: list[dict], whole_doc_comments: list[ApprovalComment]
) -> tuple[bool, bool]:
    """Shared by _render_workspace and _render_section_fragment so the two
    can never drift - both feed the same Review Decision card (directly, or
    via the out-of-band swap in _section_fragment.html)."""
    has_unresolved_gap = any(v["section"].has_gap_marker for v in section_views)
    has_unresolved_comments = bool(
        [c for c in whole_doc_comments if not c.resolved]
    ) or any(v["unresolved_comments"] for v in section_views)
    return has_unresolved_gap, has_unresolved_comments


def _regen_guard(
    request: Request, db: Session, proposal: Proposal, user: User, section_key: str | None = None
):
    """Blocks any write while a full-document regenerate is in flight, so
    nothing races the background task that's rewriting every section at
    once - returns a response to short-circuit the caller, or None to let
    it proceed normally. In practice the workspace page's own wait-state
    (in `_render_workspace`) already keeps a fresh page load from showing
    any actionable controls at all; this only matters for a stale page (one
    that loaded before the regenerate started) still being submitted.
    """
    if not proposal.is_regenerating:
        return None
    msg = "This proposal is being regenerated in full - please wait for it to finish."
    if section_key and _is_htmx(request):
        return _render_section_fragment(request, db, proposal, section_key, user, section_error=msg)
    return _render_workspace(request, db, proposal, user, error=msg, status_code=409)


def _render_workspace(
    request: Request,
    db: Session,
    proposal: Proposal,
    user: User,
    error: str | None = None,
    status_code: int = 200,
    pending_section_key: str | None = None,
    pending_comment: str = "",
    pending_is_overwrite: bool = False,
    pending_full_regenerate_confirm: bool = False,
    pending_intake_values: dict | None = None,
    just_approved: bool = False,
):
    """The one page everyone looks at (Google-Docs style) - replaces the
    old three-way split across proposal_detail.html (creator overview),
    proposal_edit.html (creator's section editor), and proposal_approve.html
    (a separate approver-only review page). `can_edit`/`can_review` gate
    which controls render; both can be true at once (self-approval, or an
    admin), in which case the viewer just sees both sets of controls -
    there's no single "mode" to pick between.
    """
    can_edit, can_review = get_role_flags(proposal, user)

    # A full-document regenerate is in flight (a background task is
    # rewriting every section at once) - every viewer, not just whoever
    # triggered it, sees this instead of the real page until it clears.
    # Self-polls via htmx rather than a dedicated status endpoint: fetching
    # this same URL again naturally stops needing to poll once
    # is_regenerating flips back to false, since that response then
    # contains the real page instead of another poll trigger.
    if proposal.is_regenerating:
        return templates.TemplateResponse(
            request=request,
            name="proposal_regenerating.html",
            context={"proposal": proposal, "user": user},
            status_code=status_code,
        )

    section_views = _build_section_views(db, proposal)
    delivery_logs = _get_delivery_logs(proposal, db)
    whole_doc_comments = _get_whole_doc_comments(proposal, db)
    has_unresolved_gap, has_unresolved_comments = _compute_unresolved_flags(section_views, whole_doc_comments)
    # Once approved/sent, the proposal is finalized - the review UI (inline
    # comments, per-section edit/regenerate, Raw Inputs) disappears in favor
    # of a clean read-only view with just Send to Client / Reopen for
    # Editing, matching a "this document is done" mental model rather than
    # "still being worked on."
    is_finalized = proposal.status in ("approved", "sent")

    return templates.TemplateResponse(
        request=request,
        name="proposal_workspace.html",
        context={
            "proposal": proposal,
            "user": user,
            "can_edit": can_edit,
            "can_review": can_review,
            "is_finalized": is_finalized,
            "just_approved": just_approved,
            "client_view_url": (
                f"{get_settings().app_base_url}/view/{proposal.client_token}" if proposal.client_token else None
            ),
            "section_views": section_views,
            "whole_doc_comments": whole_doc_comments,
            "has_unresolved_gap": has_unresolved_gap,
            "has_unresolved_comments": has_unresolved_comments,
            "error": error,
            "pending_section_key": pending_section_key,
            "pending_comment": pending_comment,
            "pending_is_overwrite": pending_is_overwrite,
            "pending_full_regenerate_confirm": pending_full_regenerate_confirm,
            "pending_intake_values": pending_intake_values,
            "today": datetime.now(timezone.utc).date().isoformat(),
            "attached_references": _get_attached_references(proposal, db),
            "attachable_library_files": _get_attachable_library_files(proposal, db),
            "approvers": _get_approvers(db),
            "delivery_logs": delivery_logs,
            # Whether a client-delivery send has actually been attempted -
            # distinct from delivery_logs being non-empty, since that list
            # also holds approver_notification/changes_requested_notification/
            # pdf_export rows that can exist with no send attempt yet, which
            # would otherwise mislabel a first send as a "retry".
            "has_send_attempt": any(log.channel == "client_delivery" for log in delivery_logs),
        },
        status_code=status_code,
    )


@router.get("/{proposal_id}")
def view_proposal(
    proposal_id: int,
    request: Request,
    just_approved: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require_user),
):
    proposal, _can_edit, _can_review = get_workspace_proposal(proposal_id, db, user)
    return _render_workspace(request, db, proposal, user, just_approved=bool(just_approved))


@router.post("/{proposal_id}/submit")
def submit_proposal(
    proposal_id: int,
    request: Request,
    approver_id: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    proposal = _get_viewable_proposal(proposal_id, db, user)
    if (guard := _regen_guard(request, db, proposal, user)) is not None:
        return guard

    if proposal.status not in ("draft", "changes_requested"):
        return _render_workspace(
            request, db, proposal, user,
            error=f"Can't submit a proposal that is already '{proposal.status}'.",
            status_code=400,
        )
    if not db.execute(select(Section).where(Section.proposal_id == proposal.id)).scalars().first():
        return _render_workspace(
            request, db, proposal, user, error="Generate the proposal before submitting it.", status_code=400
        )
    if db.execute(
        select(ApprovalComment).where(
            ApprovalComment.proposal_id == proposal.id, ApprovalComment.resolved.is_(False)
        )
    ).scalars().first():
        return _render_workspace(
            request, db, proposal, user,
            error="Resolve every comment from the last review round before resubmitting for approval.",
            status_code=400,
        )

    approver = None
    if approver_id.strip().isdigit():
        approver = db.execute(
            select(User).where(
                User.id == int(approver_id), User.can_approve.is_(True), User.is_active.is_(True)
            )
        ).scalar_one_or_none()
    if approver is None:
        return _render_workspace(
            request, db, proposal, user, error="Pick a valid approver to submit this proposal.", status_code=400
        )

    proposal.approver_id = approver.id
    proposal.status = "pending_approval"
    db.commit()
    logger.info("User id=%s submitted proposal id=%s to approver id=%s", user.id, proposal.id, approver.id)

    settings = get_settings()
    review_url = f"{settings.app_base_url}/proposals/{proposal.id}"
    try:
        send_email(
            to=approver.email,
            subject=f"Proposal ready for your review: {proposal.client_name} ({proposal.company_name})",
            html=render_email(
                "approver_notification.html",
                approver_name=approver.name,
                client_name=proposal.client_name,
                company_name=proposal.company_name,
                review_url=review_url,
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
    if (guard := _regen_guard(request, db, proposal, user)) is not None:
        return guard

    if proposal.status not in ("approved", "sent"):
        return _render_workspace(
            request, db, proposal, user,
            error=f"Can't reopen a proposal that is '{proposal.status}' - only an approved or sent one.",
            status_code=400,
        )

    # Invalidates the old client link immediately (spec section 7: "no live
    # client link ever points at stale content") - a new token is issued the
    # next time this proposal is approved (step 10), not here, since an
    # unapproved/reopened proposal shouldn't have a live client-facing link
    # at all. The outgoing token is kept as `retired_client_token` (only the
    # single most recent one - each reopen overwrites it) so a client who
    # still has that exact link gets "this is being updated" instead of the
    # generic "link doesn't exist" redirect every other bad token gets.
    proposal.status = "draft"
    proposal.retired_client_token = proposal.client_token
    proposal.client_token = None
    proposal.token_expires_at = None
    db.commit()
    logger.info("User id=%s reopened proposal id=%s", user.id, proposal.id)
    return RedirectResponse(url=f"/proposals/{proposal.id}", status_code=303)


@router.post("/{proposal_id}/send")
def send_to_client(
    proposal_id: int,
    request: Request,
    client_name: str = Form(""),
    client_email: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    proposal = _get_viewable_proposal(proposal_id, db, user)
    if (guard := _regen_guard(request, db, proposal, user)) is not None:
        return guard

    # Idempotency check (spec section 7: "Duplicate send - only send if
    # status isn't already sent"). A prior failed attempt leaves status at
    # "approved", not "sent", so a retry after a failure is still allowed -
    # only a proposal that has actually succeeded once is locked out.
    if proposal.status != "approved":
        return _render_workspace(
            request, db, proposal, user,
            error=f"Can't send a proposal that is '{proposal.status}' - it must be approved (and not already sent).",
            status_code=400,
        )

    # The Send to Client confirm dialog lets the representative's name/
    # email be corrected right here - the one moment a wrong delivery
    # address actually matters - without the heavier Reopen for Editing
    # flow (which invalidates tokens and forces re-approval) for a typo
    # that has nothing to do with the approved proposal content itself.
    client_name = client_name.strip()
    client_email = client_email.strip()
    if not client_name or not EMAIL_RE.match(client_email):
        return _render_workspace(
            request, db, proposal, user,
            error="Enter a representative name and a valid email before sending.",
            status_code=400,
        )
    proposal.client_name = client_name
    proposal.client_email = client_email
    db.commit()

    settings = get_settings()
    creator = db.get(User, proposal.created_by)
    view_url = f"{settings.app_base_url}/view/{proposal.client_token}"
    try:
        send_email(
            to=proposal.client_email,
            # CC'd so the salesperson sees exactly what the client received;
            # replies go straight to them, not the shared inbox, since
            # they're the one with the actual client relationship.
            cc=creator.email if creator else None,
            reply_to=creator.email if creator else None,
            subject=f"Proposal for {proposal.company_name}",
            html=render_email(
                "client_delivery.html",
                client_name=proposal.client_name,
                company_name=proposal.company_name,
                view_url=view_url,
                salesperson_name=creator.name if creator else "Koya Talent",
            ),
        )
        db.add(DeliveryLog(proposal_id=proposal.id, channel="client_delivery", status="success"))
        proposal.status = "sent"
        db.commit()
        logger.info("User id=%s sent proposal id=%s to client", user.id, proposal.id)
    except EmailError as exc:
        logger.warning("Client delivery email failed for proposal id=%s: %s", proposal.id, exc)
        db.add(
            DeliveryLog(
                proposal_id=proposal.id, channel="client_delivery", status="failed",
                error_message=str(exc),
            )
        )
        db.commit()
        # Proposal stays "approved", never silently marked "sent" (spec
        # section 7: "Email API is down... proposal shows a clear
        # 'delivery failed, retry' state, never silently marked sent").

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
    if (guard := _regen_guard(request, db, proposal, user)) is not None:
        return guard

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
    if (guard := _regen_guard(request, db, proposal, user)) is not None:
        return guard
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
    files: list[UploadFile] = File(default=[]),
    names: list[str] = Form(default=[]),
    descriptions: list[str] = Form(default=[]),
    tags: list[str] = Form(default=[]),
    add_to_library_indices: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    """Same parallel names/descriptions/tags + per-file add_to_library_indices
    shape as the library page's and New Proposal page's upload routes (see
    those for the row-index-based checkbox rationale) - one row per staged
    file, built client-side by the same window.setupAccumulatingFileInput()
    helper.
    """
    proposal = _get_viewable_proposal(proposal_id, db, user)
    if (guard := _regen_guard(request, db, proposal, user)) is not None:
        return guard

    files = [f for f in files if f.filename]
    if not files:
        return _render_workspace(
            request, db, proposal, user, error="Choose at least one file to upload.", status_code=400
        )

    add_to_library_set = set(add_to_library_indices)
    errors = []
    uploaded = 0
    for index, (file, name, description, row_tags) in enumerate(zip(files, names, descriptions, tags)):
        content = file.file.read()
        try:
            storage_path = upload_reference_file(file.filename or "", content)
        except ReferenceFileError as exc:
            errors.append(f"'{file.filename}': {exc}")
            continue

        reference_file = ReferenceFile(
            name=name.strip() or file.filename,
            description=description.strip() or None,
            storage_path=storage_path,
            tags=parse_tags(row_tags),
            is_library=(index in add_to_library_set),
            uploaded_by=user.id,
        )
        db.add(reference_file)
        db.flush()
        db.add(ProposalReference(proposal_id=proposal.id, reference_file_id=reference_file.id))
        uploaded += 1
    db.commit()
    logger.info(
        "User id=%s uploaded %d reference file(s) for proposal id=%s (%d failed)",
        user.id, uploaded, proposal.id, len(errors),
    )
    if errors:
        return _render_workspace(request, db, proposal, user, error=" ".join(errors), status_code=400)
    return RedirectResponse(url=f"/proposals/{proposal.id}", status_code=303)


def _generate_sections_for_proposal(proposal: Proposal, db: Session) -> bool:
    """Shared by the standalone "Generate Proposal" route below and the new
    intake flow's immediate-generate-on-create path. Returns True on success
    (sections created and committed); False on a Claude failure - the
    caller's job to decide how to surface that (both callers currently just
    show/redirect to the workspace page's own empty-state retry button
    rather than losing already-saved intake/reference data).
    """
    try:
        generated = generate_proposal_sections(proposal, db)
    except GenerationError as exc:
        logger.warning("Generation failed for proposal id=%s: %s", proposal.id, exc)
        return False

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
    return True


@router.post("/{proposal_id}/generate")
def generate_sections(
    proposal_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    proposal = _get_viewable_proposal(proposal_id, db, user)
    if (guard := _regen_guard(request, db, proposal, user)) is not None:
        return guard

    existing_count = db.execute(
        select(Section).where(Section.proposal_id == proposal.id)
    ).scalars().first()
    if existing_count is not None:
        return _render_workspace(
            request, db, proposal, user,
            error="This proposal already has generated sections. Use per-section regenerate to update them.",
            status_code=400,
        )

    if not _generate_sections_for_proposal(proposal, db):
        return _render_workspace(
            request, db, proposal, user,
            error="Generation failed - the AI service did not return a usable proposal. Please try again.",
            status_code=502,
        )
    return RedirectResponse(url=f"/proposals/{proposal.id}", status_code=303)


def _run_full_regenerate(proposal_id: int, user_id: int) -> None:
    """Runs in a FastAPI BackgroundTask, after the triggering request has
    already returned - needs its own DB session (the request's `db` is
    closed by the time this executes). Regenerates every section from the
    (possibly just-edited) intake fields and always clears
    `is_regenerating` in `finally`, even on failure, so the proposal never
    gets stuck showing the "please wait" placeholder forever - a failed
    attempt just leaves the existing sections untouched.
    """
    db = SessionLocal()
    try:
        proposal = db.get(Proposal, proposal_id)
        if proposal is None:
            return
        try:
            generated = generate_proposal_sections(proposal, db)
        except GenerationError as exc:
            logger.warning("Full regenerate failed for proposal id=%s: %s", proposal_id, exc)
            return

        existing = {
            s.section_key: s
            for s in db.execute(select(Section).where(Section.proposal_id == proposal.id)).scalars().all()
        }
        for sort_order, key in enumerate(SECTION_KEYS):
            section_output = getattr(generated, key)
            section = existing.get(key)
            if section is None:
                db.add(
                    Section(
                        proposal_id=proposal.id, section_key=key, content=section_output.content,
                        sort_order=sort_order, has_gap_marker=section_output.has_gap,
                    )
                )
                continue
            old_content = section.content
            section.content = section_output.content
            section.has_gap_marker = section_output.has_gap
            section.sort_order = sort_order
            db.add(
                SectionHistory(
                    section_id=section.id, old_content=old_content, new_content=section_output.content,
                    change_type="full_regenerate", triggering_comment=None, changed_by=user_id,
                )
            )
        db.commit()
        logger.info("Full regenerate completed for proposal id=%s (%d sections)", proposal.id, len(SECTION_KEYS))
    except Exception:
        logger.exception("Full regenerate crashed for proposal id=%s", proposal_id)
    finally:
        try:
            proposal = db.get(Proposal, proposal_id)
            if proposal is not None:
                proposal.is_regenerating = False
                db.commit()
        finally:
            db.close()


@router.post("/{proposal_id}/regenerate-full")
def regenerate_full(
    proposal_id: int,
    request: Request,
    background_tasks: BackgroundTasks,
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
    confirm_overwrite: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    """Regenerates every section from scratch (the "Raw Inputs" tab's
    action) - unlike per-section regenerate, this always needs the real
    shared lock (`is_regenerating`) since it touches every section at once
    and takes several sequential Claude calls, long enough that every
    viewer (not just whoever clicked the button) needs to see a "please
    wait" state rather than a stale or half-updated page.
    """
    proposal = _get_viewable_proposal(proposal_id, db, user)
    if (guard := _regen_guard(request, db, proposal, user)) is not None:
        return guard

    values = {
        "client_name": client_name, "client_email": client_email, "company_name": company_name,
        "client_needs_summary": client_needs_summary, "project_scope": project_scope,
        "goals_and_objectives": goals_and_objectives, "recommended_services": recommended_services,
        "proposed_timeline": proposed_timeline, "estimated_pricing": estimated_pricing,
    }
    # No reference_file_ids form field on this route (references are
    # managed via their own immediately-committed attach/upload/remove
    # POSTs, not hidden fields riding along with this form) - so whether
    # narrative fields can be left blank is answered from the DB directly,
    # same pattern as the has_manual_edits check just below.
    has_reference = db.execute(
        select(ProposalReference.reference_file_id).where(ProposalReference.proposal_id == proposal.id).limit(1)
    ).scalar_one_or_none() is not None
    errors, parsed_date = _validate_intake_fields(values, date_of_call, has_references=has_reference)
    if errors:
        return _render_workspace(request, db, proposal, user, error=" ".join(errors), status_code=400)

    has_manual_edits = db.execute(
        select(SectionHistory.id)
        .join(Section, Section.id == SectionHistory.section_id)
        .where(Section.proposal_id == proposal.id, SectionHistory.change_type == "manual_edit")
        .limit(1)
    ).scalar_one_or_none()
    if has_manual_edits and confirm_overwrite != "yes":
        return _render_workspace(
            request, db, proposal, user,
            error="One or more sections have manual edits that a full regenerate would overwrite. "
            "Confirm below to continue anyway.",
            status_code=409,
            pending_full_regenerate_confirm=True,
            pending_intake_values={**values, "date_of_call": date_of_call},
        )

    proposal.client_name = client_name.strip()
    proposal.client_email = client_email.strip().lower()
    proposal.company_name = company_name.strip()
    proposal.date_of_call = parsed_date
    proposal.client_needs_summary = client_needs_summary.strip()
    proposal.project_scope = project_scope.strip()
    proposal.goals_and_objectives = goals_and_objectives.strip()
    proposal.recommended_services = recommended_services.strip()
    proposal.proposed_timeline = proposed_timeline.strip()
    proposal.estimated_pricing = estimated_pricing.strip()
    proposal.is_regenerating = True
    db.commit()
    logger.info("User id=%s started a full regenerate for proposal id=%s", user.id, proposal.id)

    background_tasks.add_task(_run_full_regenerate, proposal.id, user.id)
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


def _is_htmx(request: Request) -> bool:
    return request.headers.get("hx-request") == "true"


def _render_section_fragment(
    request: Request,
    db: Session,
    proposal: Proposal,
    section_key: str,
    user: User,
    section_error: str | None = None,
    pending_overwrite: bool = False,
    pending_comment: str = "",
):
    """Renders just one section's block (app/templates/_section_fragment.html)
    for an HTMX swap - the spec's own reason for choosing HTMX (section 2:
    "edit a section, see it update, without disturbing the rest of the
    page"). Plain (non-HTMX) form submits never hit this - they keep the
    original full-page redirect via `_render_workspace`, so the app still
    works with JS disabled. `can_edit`/`can_review` are recomputed from
    `user` here (not assumed from which router called this) since either
    a creator-side action or an approver-side comment can trigger this
    same swap, and a self-approving/admin user can have both at once.
    """
    can_edit, can_review = get_role_flags(proposal, user)
    views = _build_section_views(db, proposal)
    view = next(v for v in views if v["section"].section_key == section_key)
    whole_doc_comments = _get_whole_doc_comments(proposal, db)
    has_unresolved_gap, has_unresolved_comments = _compute_unresolved_flags(views, whole_doc_comments)
    return templates.TemplateResponse(
        request=request,
        name="_section_fragment.html",
        context={
            "proposal": proposal,
            "view": view,
            "can_edit": can_edit,
            "can_review": can_review,
            "is_finalized": proposal.status in ("approved", "sent"),
            "section_error": section_error,
            "pending_overwrite": pending_overwrite,
            "pending_comment": pending_comment,
            # Feeds the out-of-band Review Decision swap at the bottom of
            # _section_fragment.html - this action may have changed either.
            "has_unresolved_gap": has_unresolved_gap,
            "has_unresolved_comments": has_unresolved_comments,
            # True only here - this is the standalone HTMX-swap render of
            # this template. proposal_workspace.html includes the same
            # template once per section for a full page load, where this
            # must stay unset (see _section_fragment.html's own comment).
            "oob_review_decision": True,
        },
    )


@router.get("/{proposal_id}/edit")
def edit_proposal_redirect(proposal_id: int):
    """The old separate editing page - now folded into the one unified
    workspace page. Kept as a redirect (not removed outright) since real
    emails already sent (changes-requested notifications) link here."""
    return RedirectResponse(url=f"/proposals/{proposal_id}", status_code=303)


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
    if (guard := _regen_guard(request, db, proposal, user, section_key=section_key)) is not None:
        return guard
    section = _get_section(proposal, section_key, db)

    new_content = content.strip()
    if not new_content:
        if _is_htmx(request):
            return _render_section_fragment(
                request, db, proposal, section_key, user, section_error="Section content can't be empty."
            )
        return _render_workspace(request, db, proposal, user, error="Section content can't be empty.", status_code=400)

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
    if _is_htmx(request):
        return _render_section_fragment(request, db, proposal, section_key, user)
    return RedirectResponse(url=f"/proposals/{proposal.id}", status_code=303)


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
    if (guard := _regen_guard(request, db, proposal, user, section_key=section_key)) is not None:
        return guard
    section = _get_section(proposal, section_key, db)
    title = SECTION_TITLES.get(section_key, section_key)

    history = db.execute(
        select(SectionHistory)
        .where(SectionHistory.section_id == section.id)
        .order_by(SectionHistory.created_at.desc())
    ).scalars().all()

    regenerate_count = sum(1 for h in history if h.change_type == "regenerate")
    if regenerate_count >= MAX_REGENERATIONS_PER_SECTION:
        limit_error = (
            f"'{title}' has reached its regeneration limit ({MAX_REGENERATIONS_PER_SECTION}). "
            f"Edit it manually instead."
        )
        if _is_htmx(request):
            return _render_section_fragment(request, db, proposal, section_key, user, section_error=limit_error)
        return _render_workspace(
            request, db, proposal, user, error=limit_error, status_code=400, pending_section_key=section_key
        )

    pending_manual_edit = bool(history) and history[0].change_type == "manual_edit"
    if pending_manual_edit and confirm_overwrite != "yes":
        overwrite_error = f"'{title}' has a manual edit that regenerating would overwrite."
        if _is_htmx(request):
            return _render_section_fragment(
                request, db, proposal, section_key, user,
                section_error=overwrite_error, pending_overwrite=True, pending_comment=comment,
            )
        return _render_workspace(
            request, db, proposal, user,
            error=overwrite_error,
            status_code=409,
            pending_section_key=section_key,
            pending_comment=comment,
            pending_is_overwrite=True,
        )

    try:
        result = regenerate_section(proposal, section_key, section.content, comment.strip() or None, db)
    except GenerationError as exc:
        logger.warning(
            "Regeneration failed for section id=%s (proposal id=%s): %s", section.id, proposal.id, exc
        )
        generation_error = "Regeneration failed - the AI service did not return a usable section. Please try again."
        if _is_htmx(request):
            return _render_section_fragment(request, db, proposal, section_key, user, section_error=generation_error)
        return _render_workspace(
            request, db, proposal, user, error=generation_error, status_code=502, pending_section_key=section_key
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
    if _is_htmx(request):
        return _render_section_fragment(request, db, proposal, section_key, user)
    return RedirectResponse(url=f"/proposals/{proposal.id}", status_code=303)


@router.post("/{proposal_id}/comments/{comment_id}/resolve")
def resolve_comment(
    proposal_id: int,
    comment_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    proposal = _get_viewable_proposal(proposal_id, db, user)
    if (guard := _regen_guard(request, db, proposal, user)) is not None:
        return guard
    comment = db.execute(
        select(ApprovalComment).where(
            ApprovalComment.id == comment_id, ApprovalComment.proposal_id == proposal.id
        )
    ).scalar_one_or_none()
    if comment is not None:
        comment.resolved = True
        db.commit()
        logger.info("User id=%s resolved approval comment id=%s (proposal id=%s)", user.id, comment_id, proposal.id)
    return RedirectResponse(url=f"/proposals/{proposal.id}", status_code=303)


@router.post("/{proposal_id}/comments/resolve-all")
def resolve_all_comments(
    proposal_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    proposal = _get_viewable_proposal(proposal_id, db, user)
    if (guard := _regen_guard(request, db, proposal, user)) is not None:
        return guard
    unresolved = db.execute(
        select(ApprovalComment).where(
            ApprovalComment.proposal_id == proposal.id, ApprovalComment.resolved.is_(False)
        )
    ).scalars().all()
    for comment in unresolved:
        comment.resolved = True
    db.commit()
    logger.info(
        "User id=%s resolved all %d unresolved comment(s) on proposal id=%s", user.id, len(unresolved), proposal.id
    )
    return RedirectResponse(url=f"/proposals/{proposal.id}", status_code=303)
