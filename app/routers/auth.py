import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import SESSION_USER_KEY, get_current_user
from app.db import get_db
from app.models import User
from app.rate_limit import limiter
from app.security import verify_password
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/login")
def login_form(request: Request, user: User | None = Depends(get_current_user)):
    if user is not None:
        return RedirectResponse(url="/dashboard", status_code=303)
    return templates.TemplateResponse(request=request, name="login.html", context={"error": None})


@router.post("/login")
@limiter.limit("5/minute")
def login_submit(
    request: Request,
    # "" not Form(...): Starlette's urlencoded-form parser drops blank
    # fields entirely, so a required Form(...) field left empty would 422
    # instead of showing this page's normal "invalid credentials" message.
    email: str = Form(""),
    password: str = Form(""),
    db: Session = Depends(get_db),
):
    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    # password_hash is None for an invited user who hasn't completed setup
    # yet (app/routers/account.py) - checked before verify_password, which
    # can't accept None as a hash.
    if (
        user is None
        or not user.is_active
        or user.password_hash is None
        or not verify_password(password, user.password_hash)
    ):
        logger.warning("Failed login attempt for email=%s", email)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"error": "Invalid email or password."},
            status_code=401,
        )
    request.session[SESSION_USER_KEY] = user.id
    logger.info("User id=%s logged in", user.id)
    return RedirectResponse(url="/dashboard", status_code=303)


@router.post("/logout")
def logout(request: Request):
    user_id = request.session.get(SESSION_USER_KEY)
    request.session.clear()
    logger.info("User id=%s logged out", user_id)
    return RedirectResponse(url="/login", status_code=303)
