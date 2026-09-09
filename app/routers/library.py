import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_can_create
from app.db import get_db
from app.models import Proposal, ProposalReference, ReferenceFile, User
from app.pagination import paginate
from app.services.reference_files import ReferenceFileError, parse_tags, upload_reference_file
from app.storage import StorageError, create_signed_url, delete_file
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/library")

TRASH_RETENTION_DAYS = 30

# A file attached to a proposal that hasn't reached a final state yet blocks
# trashing it - an approved/sent proposal's reference list already lives in
# its frozen snapshot (static JSON, not a live link), so it's never a blocker.
ACTIVE_STATUSES = ("draft", "pending_approval", "changes_requested")


def _purge_expired_trash(db: Session) -> None:
    """Same pattern as the 7-day unopened-proposal nudge (spec section 9's
    "no background jobs" constraint) - no scheduled job, just a lazy check
    run whenever someone's already loading a library-related page. A file
    whose storage delete fails is left in trash to retry next time, rather
    than dropping the DB row and silently orphaning the storage object.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=TRASH_RETENTION_DAYS)
    expired = db.execute(
        select(ReferenceFile).where(ReferenceFile.deleted_at.is_not(None), ReferenceFile.deleted_at < cutoff)
    ).scalars().all()
    for f in expired:
        try:
            delete_file(f.storage_path)
        except StorageError:
            logger.exception("Failed to purge expired trashed file id=%s from storage", f.id)
            continue
        db.delete(f)
    if expired:
        db.commit()


def _can_manage(reference_file: ReferenceFile, user: User) -> bool:
    return reference_file.uploaded_by == user.id or user.is_admin


def _blocking_proposals(reference_file_id: int, db: Session) -> list[Proposal]:
    return db.execute(
        select(Proposal)
        .join(ProposalReference, ProposalReference.proposal_id == Proposal.id)
        .where(
            ProposalReference.reference_file_id == reference_file_id,
            Proposal.status.in_(ACTIVE_STATUSES),
        )
    ).scalars().all()


def _render_library(
    request: Request,
    db: Session,
    user: User,
    view: str = "all",
    error: str | None = None,
    status_code: int = 200,
    page: int = 1,
):
    if view == "trash":
        query = select(ReferenceFile).where(ReferenceFile.deleted_at.is_not(None))
        if not user.is_admin:
            query = query.where(ReferenceFile.uploaded_by == user.id)
        files, pagination = paginate(db, query, ReferenceFile.deleted_at.desc(), page)
    else:
        view = "all"
        query = select(ReferenceFile).where(
            ReferenceFile.is_library.is_(True), ReferenceFile.deleted_at.is_(None)
        )
        files, pagination = paginate(db, query, ReferenceFile.created_at.desc(), page)

    uploaders = {
        u.id: u.name
        for u in db.execute(select(User).where(User.id.in_({f.uploaded_by for f in files}))).scalars().all()
    }
    now = datetime.now(timezone.utc)
    days_left = {
        f.id: max(0, TRASH_RETENTION_DAYS - (now - f.deleted_at).days)
        for f in files
        if f.deleted_at is not None
    }
    return templates.TemplateResponse(
        request=request,
        name="library.html",
        context={
            "files": files,
            "uploaders": uploaders,
            "days_left": days_left,
            "view": view,
            "user": user,
            "error": error,
            "pagination": pagination,
            "base_url": "/library",
            "extra_params": {"view": view},
        },
        status_code=status_code,
    )


@router.get("")
def library_page(
    request: Request,
    view: str = "all",
    page: int = 1,
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    _purge_expired_trash(db)
    return _render_library(request, db, user, view=view, page=page)


@router.post("/upload")
def upload_library_files(
    request: Request,
    files: list[UploadFile] = File(...),
    names: list[str] = Form(...),
    descriptions: list[str] = Form(...),
    tags: list[str] = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    """Takes several files in one submission - each with its own name/
    description/tags, entered as parallel form fields (one input row per
    file, built client-side via window.setupAccumulatingFileInput() in
    app.js as files are chosen; see library.html). A file that fails
    validation (bad format, too large) is reported by name without
    blocking the others - a batch of 5 shouldn't all fail because one was
    a .docx.
    """
    _purge_expired_trash(db)
    errors = []
    created = 0
    for file, name, description, row_tags in zip(files, names, descriptions, tags):
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
            is_library=True,
            uploaded_by=user.id,
        )
        db.add(reference_file)
        created += 1
    db.commit()
    logger.info("User id=%s added %d library reference file(s) (%d failed)", user.id, created, len(errors))
    error = " ".join(errors) if errors else None
    return _render_library(request, db, user, error=error, status_code=400 if errors else 200)


@router.get("/{reference_file_id}/preview")
def preview_file(
    reference_file_id: int, db: Session = Depends(get_db), user: User = Depends(require_can_create)
):
    """Redirects to a short-lived signed Supabase Storage URL rather than
    proxying the file's bytes through this server - the file's real
    content-type was already set at upload time (see
    services/reference_files.py), so Supabase serves it back the same
    way regardless of which path fetches it. This is what actually makes
    the preview modal's iframe load faster: one hop straight to storage
    instead of browser -> us -> Supabase -> us -> browser for every open.
    """
    reference_file = db.get(ReferenceFile, reference_file_id)
    if reference_file is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")
    try:
        signed_url = create_signed_url(reference_file.storage_path)
    except StorageError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from None
    return RedirectResponse(url=signed_url, status_code=status.HTTP_302_FOUND)


@router.post("/{reference_file_id}/trash")
def trash_file(
    reference_file_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    _purge_expired_trash(db)
    reference_file = db.get(ReferenceFile, reference_file_id)
    if reference_file is None or reference_file.deleted_at is not None:
        return _render_library(request, db, user, error="File not found.", status_code=404)
    if not _can_manage(reference_file, user):
        return _render_library(
            request, db, user, error="You can only trash files you uploaded.", status_code=403
        )
    blockers = _blocking_proposals(reference_file_id, db)
    if blockers:
        names = ", ".join(f"{p.client_name} ({p.company_name})" for p in blockers)
        return _render_library(
            request, db, user,
            error=f"Can't trash '{reference_file.name}' - still attached to: {names}. "
            "Remove it from those proposals first.",
            status_code=400,
        )
    reference_file.deleted_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("User id=%s moved reference file id=%s to trash", user.id, reference_file_id)
    return RedirectResponse(url="/library", status_code=303)


@router.post("/trash-selected")
def trash_selected_files(
    request: Request,
    reference_file_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    _purge_expired_trash(db)
    trashed = 0
    skipped = []
    for reference_file_id in reference_file_ids:
        reference_file = db.get(ReferenceFile, reference_file_id)
        if reference_file is None or reference_file.deleted_at is not None:
            continue
        if not _can_manage(reference_file, user):
            skipped.append(f"'{reference_file.name}' (not yours)")
            continue
        if _blocking_proposals(reference_file_id, db):
            skipped.append(f"'{reference_file.name}' (attached to an active proposal)")
            continue
        reference_file.deleted_at = datetime.now(timezone.utc)
        trashed += 1
    db.commit()
    logger.info("User id=%s bulk-trashed %d reference file(s) (%d skipped)", user.id, trashed, len(skipped))
    error = f"Skipped: {', '.join(skipped)}." if skipped else None
    return _render_library(request, db, user, error=error, status_code=400 if skipped else 200)


@router.post("/{reference_file_id}/restore")
def restore_file(
    reference_file_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    reference_file = db.get(ReferenceFile, reference_file_id)
    if reference_file is None or reference_file.deleted_at is None:
        return _render_library(request, db, user, view="trash", error="File not found.", status_code=404)
    if not _can_manage(reference_file, user):
        return _render_library(
            request, db, user, view="trash", error="You can only restore files you uploaded.", status_code=403
        )
    reference_file.deleted_at = None
    db.commit()
    logger.info("User id=%s restored reference file id=%s from trash", user.id, reference_file_id)
    return RedirectResponse(url="/library?view=trash", status_code=303)


@router.post("/{reference_file_id}/delete-permanent")
def delete_file_permanent(
    reference_file_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    reference_file = db.get(ReferenceFile, reference_file_id)
    if reference_file is None or reference_file.deleted_at is None:
        return _render_library(request, db, user, view="trash", error="File not found.", status_code=404)
    if not _can_manage(reference_file, user):
        return _render_library(
            request, db, user, view="trash",
            error="You can only permanently delete files you uploaded.", status_code=403,
        )
    try:
        delete_file(reference_file.storage_path)
    except StorageError as exc:
        return _render_library(request, db, user, view="trash", error=str(exc), status_code=502)
    db.delete(reference_file)
    db.commit()
    logger.info("User id=%s permanently deleted reference file id=%s", user.id, reference_file_id)
    return RedirectResponse(url="/library?view=trash", status_code=303)


@router.post("/empty-trash")
def empty_trash(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    query = select(ReferenceFile).where(ReferenceFile.deleted_at.is_not(None))
    if not user.is_admin:
        query = query.where(ReferenceFile.uploaded_by == user.id)
    files = db.execute(query).scalars().all()
    deleted = 0
    for reference_file in files:
        try:
            delete_file(reference_file.storage_path)
        except StorageError:
            logger.exception("Failed to delete storage object for reference file id=%s", reference_file.id)
            continue
        db.delete(reference_file)
        deleted += 1
    db.commit()
    logger.info("User id=%s emptied trash (%d file(s) permanently deleted)", user.id, deleted)
    return RedirectResponse(url="/library?view=trash", status_code=303)
