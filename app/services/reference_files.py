import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import storage
from app.models import ProposalReference, ReferenceFile

logger = logging.getLogger(__name__)

# Deliberately limited to formats Claude's Messages API reads natively as a
# single content block, with no local parsing and no extra Anthropic API
# surface: .txt/.md as plain text, .pdf as a native "document" block, images
# as a native "image" block. Office formats (.docx/.xlsx/.pptx) are NOT
# supported - Claude can only read those via Anthropic's Skills + code
# execution container feature, which needs a separate Files API upload, a
# preliminary auto-tool-choice call to run the skill, and conflicts with
# this app's forced-tool_choice structured-output design (see PROGRESS.md
# step 8 for the full reasoning) - deliberately rejected as disproportionate
# to this app's scope. A user uploading one of those gets a friendly
# "convert to PDF first" message instead, per the decision recorded there.
ALLOWED_EXTENSIONS = {".txt", ".md", ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp"}

# Formats we explicitly recognize as "convert this to PDF and retry" rather
# than a bare "unsupported file type" - the common real-world formats a
# salesperson is actually likely to try attaching.
OFFICE_EXTENSIONS = {".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt"}

CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}

# Generous for text/PDF reference material, cheap guard against abuse -
# not a spec requirement, just a sane ceiling.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024


class ReferenceFileError(Exception):
    """Raised when an uploaded file fails validation or storage upload fails."""


def extension(filename: str) -> str:
    return "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def validate_upload(filename: str, content: bytes) -> None:
    if not filename:
        raise ReferenceFileError("A file is required.")
    ext = extension(filename)
    if ext in OFFICE_EXTENSIONS:
        raise ReferenceFileError(
            f"'{ext}' files aren't supported. Please convert it to PDF and upload that instead."
        )
    if ext not in ALLOWED_EXTENSIONS:
        raise ReferenceFileError(
            f"Unsupported file type '{ext or filename}'. Allowed types: "
            + ", ".join(sorted(ALLOWED_EXTENSIONS)) + "."
        )
    if not content:
        raise ReferenceFileError("The uploaded file is empty.")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ReferenceFileError(f"File is too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB).")


def build_storage_path(filename: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", filename)
    return f"{uuid.uuid4().hex}_{safe_name}"


def upload_reference_file(filename: str, content: bytes) -> str:
    """Validates and uploads to Supabase Storage; returns the storage path."""
    validate_upload(filename, content)
    storage_path = build_storage_path(filename)
    content_type = CONTENT_TYPES[extension(filename)]
    try:
        storage.upload_file(storage_path, content, content_type)
    except storage.StorageError as exc:
        raise ReferenceFileError(f"Could not upload file to storage: {exc}") from None
    return storage_path


ORPHAN_RETENTION_DAYS = 7


def purge_orphaned_reference_files(db: Session) -> None:
    """A device upload that was never added to the shared library (see the
    "Also add to library" checkbox on the upload panels) has no path in the
    UI to ever be found again once it's detached from every proposal - it
    isn't on the Library page (that's filtered to is_library=True), can't
    be re-attached anywhere, and has no trash/delete control reaching it.
    Same lazy-check pattern as _purge_expired_trash in app/routers/library.py
    (spec's "no background jobs" constraint) - run opportunistically
    whenever someone's already loading a relevant page, not on a schedule.

    Deliberately excludes anything still attached to any proposal
    (regardless of that proposal's status) and anything already in the
    Trash flow (deleted_at set - that path has its own 30-day purge). A
    library file (is_library=True) is never touched here even if nothing
    currently attaches to it - it stays manageable via the Library page on
    its own terms.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=ORPHAN_RETENTION_DAYS)
    attached_ids = select(ProposalReference.reference_file_id)
    orphaned = db.execute(
        select(ReferenceFile).where(
            ReferenceFile.is_library.is_(False),
            ReferenceFile.deleted_at.is_(None),
            ReferenceFile.created_at < cutoff,
            ReferenceFile.id.notin_(attached_ids),
        )
    ).scalars().all()

    purged = 0
    for f in orphaned:
        try:
            storage.delete_file(f.storage_path)
        except storage.StorageError:
            logger.exception("Failed to purge orphaned reference file id=%s from storage", f.id)
            continue
        db.delete(f)
        purged += 1
    if purged:
        db.commit()
        logger.info("Purged %d orphaned reference file(s) (never added to library, unattached to any proposal)", purged)


def parse_tags(raw: str) -> list[str]:
    tags: list[str] = []
    for tag in raw.split(","):
        tag = tag.strip()
        if tag and tag not in tags:
            tags.append(tag)
    return tags
