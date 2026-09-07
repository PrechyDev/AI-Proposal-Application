import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.auth import require_user
from app.config import get_settings
from app.db import engine, get_db
from app.models import User
from app.routers.admin import router as admin_router
from app.routers.auth import router as auth_router
from app.routers.proposals import router as proposals_router

logger = logging.getLogger(__name__)
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
    yield


app = FastAPI(title="AI Proposal Application", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.session_secret_key, same_site="lax")
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(proposals_router)

templates = Jinja2Templates(directory="app/templates")


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
    return templates.TemplateResponse(
        request=request, name="index.html", context={"title": "AI Proposal Application"}
    )


@app.get("/dashboard")
def dashboard(request: Request, user: User = Depends(require_user)):
    return templates.TemplateResponse(request=request, name="dashboard.html", context={"user": user})
