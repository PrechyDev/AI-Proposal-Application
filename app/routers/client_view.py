import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import Proposal, Snapshot
from app.services.pdf import PdfRenderError, render_pdf
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()

# Uses its own SessionLocal() rather than the shared Depends(get_db) - that
# dependency re-raises SQLAlchemyError (see app/db.py), which would surface
# as this app's generic JSON 500 to an anonymous client instead of the
# homepage redirect spec sections 6/7 require for *any* bad/expired/broken
# link. A broad except below treats an unexpected DB error the same as an
# invalid token: redirect, never an error page.


def _get_valid_snapshot(token: str) -> tuple[Proposal, Snapshot] | None:
    """Looks up the proposal + latest snapshot for a client token. Returns
    None for anything invalid - malformed token, no match, expired, wrong
    status, or an unexpected error - so the caller can redirect to the
    homepage uniformly, never distinguishing *why* a link doesn't work.
    """
    try:
        token_uuid = uuid.UUID(token)
    except ValueError:
        return None

    try:
        db: Session = SessionLocal()
        try:
            proposal = db.execute(
                select(Proposal).where(Proposal.client_token == token_uuid)
            ).scalar_one_or_none()
            if proposal is None:
                return None
            if proposal.status not in ("approved", "sent"):
                return None
            if proposal.token_expires_at is None or proposal.token_expires_at < datetime.now(timezone.utc):
                return None

            snapshot = db.execute(
                select(Snapshot)
                .where(Snapshot.proposal_id == proposal.id)
                .order_by(Snapshot.version_number.desc())
            ).scalars().first()
            if snapshot is None:
                return None

            # Detach from the session before it's closed - the caller only
            # needs plain data (proposal.client_name etc., the snapshot's
            # JSON), not a live ORM object.
            db.expunge(proposal)
            db.expunge(snapshot)
            return proposal, snapshot
        finally:
            db.close()
    except Exception:
        logger.exception("Unexpected error resolving client token")
        return None


@router.get("/view/{token}")
def view_proposal(token: str, request: Request):
    result = _get_valid_snapshot(token)
    if result is None:
        return RedirectResponse(url="/", status_code=303)
    proposal, snapshot = result
    return templates.TemplateResponse(
        request=request,
        name="proposal_view.html",
        context={"token": token, "content": snapshot.full_content_json},
    )


@router.get("/view/{token}/pdf")
def download_pdf(token: str):
    result = _get_valid_snapshot(token)
    if result is None:
        return RedirectResponse(url="/", status_code=303)
    proposal, _snapshot = result

    settings = get_settings()
    view_url = f"{settings.app_base_url}/view/{token}"
    try:
        pdf_bytes = render_pdf(view_url)
    except PdfRenderError:
        logger.error("PDF export failed for proposal id=%s", proposal.id)
        return Response(
            content="Sorry, the PDF could not be generated right now. Please try again shortly.",
            media_type="text/plain",
            status_code=502,
        )

    safe_name = "".join(c if c.isalnum() else "_" for c in proposal.client_name).strip("_") or "proposal"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}-proposal.pdf"'},
    )
