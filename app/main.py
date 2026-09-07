import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.auth import require_user
from app.config import get_settings
from app.db import engine, get_db
from app.models import DeliveryLog, Proposal, User
from app.routers.admin import router as admin_router
from app.routers.approvals import router as approvals_router
from app.routers.auth import router as auth_router
from app.routers.client_view import router as client_view_router
from app.routers.library import router as library_router
from app.routers.proposals import router as proposals_router
from app.storage import ensure_bucket_exists
from app.templating import templates

logger = logging.getLogger(__name__)

# Spec section 7: "Proposal sent but never opened -> 7-day check (via
# access_logs) triggers a notification back to the salesperson." No job
# scheduler exists yet (and the hosting platform for step 16 isn't chosen),
# so this is computed live on dashboard load rather than via a persistent
# background job - functionally the same signal, no new infrastructure.
NUDGE_THRESHOLD = timedelta(days=7)
settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("Database connection verified at startup.")
    except Exception:
        logger.critical("Database connection failed at startup.", exc_info=True)
        raise
    # Non-fatal unlike the DB check above: a storage outage shouldn't take
    # down routes that don't touch reference files.
    ensure_bucket_exists()
    yield


app = FastAPI(title="AI Proposal Application", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key, same_site="lax")
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(proposals_router)
app.include_router(approvals_router)
app.include_router(library_router)
app.include_router(client_view_router)


@app.exception_handler(HTTPException)
async def redirect_unauthenticated_to_login(request: Request, exc: HTTPException):
    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        return RedirectResponse(url="/login", status_code=303)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers)


@app.exception_handler(Exception)
async def log_unhandled_exception(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    db.execute(text("SELECT 1"))
    return {"status": "ok", "database": "connected"}


@app.get("/")
def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html", context={})


def _get_nudge_candidates(user: User, db: Session) -> list[dict]:
    """Proposals this salesperson sent that the client still hasn't opened,
    7+ days after the successful send. "Sent at" comes from the successful
    client_delivery DeliveryLog row, not a proposals column (there isn't
    one) - that log is the one unambiguous record of when delivery actually
    succeeded.
    """
    sent_unopened = db.execute(
        select(Proposal).where(
            Proposal.created_by == user.id,
            Proposal.status == "sent",
            Proposal.first_opened_at.is_(None),
        )
    ).scalars().all()

    now = datetime.now(timezone.utc)
    candidates = []
    for proposal in sent_unopened:
        sent_log = db.execute(
            select(DeliveryLog)
            .where(
                DeliveryLog.proposal_id == proposal.id,
                DeliveryLog.channel == "client_delivery",
                DeliveryLog.status == "success",
            )
            .order_by(DeliveryLog.attempted_at.desc())
        ).scalars().first()
        if sent_log is None:
            continue
        age = now - sent_log.attempted_at
        if age >= NUDGE_THRESHOLD:
            candidates.append({"proposal": proposal, "days_unopened": age.days})
    return candidates


@app.get("/dashboard")
def dashboard(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    pending_approvals = []
    if user.can_approve:
        pending_approvals = db.execute(
            select(Proposal)
            .where(Proposal.approver_id == user.id, Proposal.status == "pending_approval")
            .order_by(Proposal.updated_at)
        ).scalars().all()

    nudge_candidates = []
    if user.can_create:
        nudge_candidates = _get_nudge_candidates(user, db)

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={"user": user, "pending_approvals": pending_approvals, "nudge_candidates": nudge_candidates},
    )
