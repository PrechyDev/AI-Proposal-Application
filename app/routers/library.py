import logging

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_can_create
from app.db import get_db
from app.models import ReferenceFile, User
from app.services.reference_files import ReferenceFileError, parse_tags, upload_reference_file
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/library")


def _render_library(request: Request, db: Session, error: str | None = None, status_code: int = 200):
    files = db.execute(
        select(ReferenceFile)
        .where(ReferenceFile.is_library.is_(True))
        .order_by(ReferenceFile.created_at.desc())
    ).scalars().all()
    uploaders = {
        u.id: u.name
        for u in db.execute(select(User).where(User.id.in_({f.uploaded_by for f in files}))).scalars().all()
    }
    return templates.TemplateResponse(
        request=request,
        name="library.html",
        context={"files": files, "uploaders": uploaders, "error": error},
        status_code=status_code,
    )


@router.get("")
def library_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_can_create)):
    return _render_library(request, db)


@router.post("/upload")
def upload_library_file(
    request: Request,
    file: UploadFile = File(...),
    tags: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    content = file.file.read()
    try:
        storage_path = upload_reference_file(file.filename or "", content)
    except ReferenceFileError as exc:
        return _render_library(request, db, error=str(exc), status_code=400)

    reference_file = ReferenceFile(
        name=file.filename,
        storage_path=storage_path,
        tags=parse_tags(tags),
        is_library=True,
        uploaded_by=user.id,
    )
    db.add(reference_file)
    db.commit()
    logger.info("User id=%s added library reference file id=%s", user.id, reference_file.id)
    return _render_library(request, db)


@router.post("/{reference_file_id}/retire")
def retire_library_file(
    reference_file_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(require_can_create),
):
    reference_file = db.get(ReferenceFile, reference_file_id)
    if reference_file is not None and reference_file.is_library:
        # Soft "retire", not a delete: the file stays in storage and any
        # proposal_references row pointing at it keeps working (a proposal
        # already citing it shouldn't lose that reference) - it just stops
        # being offered for new proposals. Matches this app's no-hard-delete
        # philosophy for anything with history (spec section 7).
        reference_file.is_library = False
        db.commit()
        logger.info("User id=%s retired library reference file id=%s", user.id, reference_file_id)
    return _render_library(request, db)
