import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.models import AccessLog, DeliveryLog, Proposal, Snapshot
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


def _get_valid_snapshot(token: str) -> tuple[Proposal, Snapshot] | str | None:
    """Looks up the proposal + latest snapshot for a client token. Returns
    None for anything invalid - malformed token, no match, expired, wrong
    status, or an unexpected error - so the caller can redirect to the
    homepage uniformly, never distinguishing *why* a link doesn't work.

    One narrow, deliberate exception: a token that no longer matches
    `client_token` but does match `retired_client_token` (i.e. it was valid
    until this proposal was just reopened for editing, a moment ago)
    returns the literal string "retired" instead of None, so the route can
    tell that specific client "this is being updated" rather than the
    generic silent redirect. This doesn't weaken spec section 7's "a bad
    link reveals nothing" rule for genuinely unrecognized tokens - those
    still return None exactly as before; it only helps someone whose link
    really was live a moment ago.
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
                retired = db.execute(
                    select(Proposal).where(Proposal.retired_client_token == token_uuid)
                ).scalar_one_or_none()
                return "retired" if retired is not None else None
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


def _log_access(proposal_id: int, request: Request) -> None:
    """Records a client view (spec sections 5/6: access_logs proves whether/
    when a proposal was opened, and is what the 7-day nudge check reads).
    Best-effort - a logging failure must never break the client's actual
    view of the page, so any error here is swallowed, not raised.

    Note: a "Download as PDF" click causes Playwright to navigate to this
    same /view/{token} route internally (spec section 2's "one template,
    not two parallel renderers"), so it logs one extra access_logs row here
    too - distinguishable by IP/user-agent (the server's own, not the
    client's), and harmless: first_opened_at is only ever set once, so it
    still reflects the real first visit whichever request happens to be it.
    """
    try:
        db = SessionLocal()
        try:
            db.add(
                AccessLog(
                    proposal_id=proposal_id,
                    ip_address=request.client.host if request.client else "unknown",
                    user_agent=request.headers.get("user-agent"),
                )
            )
            proposal = db.get(Proposal, proposal_id)
            if proposal is not None and proposal.first_opened_at is None:
                proposal.first_opened_at = datetime.now(timezone.utc)
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("Failed to log access for proposal id=%s", proposal_id)


@router.get("/view/{token}")
def view_proposal(token: str, request: Request):
    result = _get_valid_snapshot(token)
    if result is None:
        return RedirectResponse(url="/", status_code=303)
    if result == "retired":
        return templates.TemplateResponse(request=request, name="proposal_being_updated.html", context={})
    proposal, snapshot = result
    _log_access(proposal.id, request)
    return templates.TemplateResponse(
        request=request,
        name="proposal_view.html",
        # `prepared_date` is the snapshot's own created_at (the moment this
        # proposal was approved) - deliberately not proposal.date_of_call
        # (the original discovery-call date, frozen inside content itself),
        # since the client-facing "Date" should read as "when we prepared
        # this for you," not the older internal-process date.
        context={"token": token, "content": snapshot.full_content_json, "prepared_date": snapshot.created_at},
    )


def _log_pdf_export(proposal_id: int, status_value: str, error_message: str | None = None) -> None:
    """Persists a PDF export attempt to `delivery_logs` (spec section 7: a
    failed export must surface on the proposal record, not just as a
    one-off error response to whoever clicked download) - same
    log-both-outcomes, never-block pattern as every email send in this app.
    Best-effort like `_log_access`: a logging failure here must never turn
    a successful download into an error, or hide a real export failure
    behind a second one.
    """
    try:
        db = SessionLocal()
        try:
            db.add(
                DeliveryLog(
                    proposal_id=proposal_id, channel="pdf_export", status=status_value, error_message=error_message
                )
            )
            db.commit()
        finally:
            db.close()
    except Exception:
        logger.exception("Failed to log PDF export attempt for proposal id=%s", proposal_id)


@router.get("/view/{token}/pdf")
def download_pdf(token: str):
    result = _get_valid_snapshot(token)
    # A PDF response has no good way to show the "being updated" message, so
    # the "retired" sentinel (see /view/{token}) is treated the same as None
    # here - only the HTML view route has that specific messaging.
    if result is None or result == "retired":
        return RedirectResponse(url="/", status_code=303)
    proposal, _snapshot = result

    settings = get_settings()
    view_url = f"{settings.app_base_url}/view/{token}"
    try:
        pdf_bytes = render_pdf(view_url)
    except PdfRenderError as exc:
        logger.error("PDF export failed for proposal id=%s", proposal.id)
        _log_pdf_export(proposal.id, "failed", str(exc))
        return Response(
            content="Sorry, the PDF could not be generated right now. Please try again shortly.",
            media_type="text/plain",
            status_code=502,
        )
    _log_pdf_export(proposal.id, "success")

    safe_name = "".join(c if c.isalnum() else "_" for c in proposal.company_name).strip("_") or "proposal"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}-proposal.pdf"'},
    )
