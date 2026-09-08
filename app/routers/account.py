import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import SESSION_USER_KEY
from app.config import get_settings
from app.db import get_db
from app.models import User
from app.security import hash_password
from app.services.account_tokens import check_reset_code, consume_token, find_valid_invite, issue_reset_code
from app.services.email import EmailError, send_email
from app.templating import render_email, templates

logger = logging.getLogger(__name__)

router = APIRouter()

MIN_PASSWORD_LENGTH = 8


@router.get("/accept-invite/{token}")
def accept_invite_form(token: str, request: Request, db: Session = Depends(get_db)):
    account_token = find_valid_invite(db, token)
    if account_token is None:
        return templates.TemplateResponse(request=request, name="invite_invalid.html", status_code=400)
    user = db.get(User, account_token.user_id)
    return templates.TemplateResponse(
        request=request, name="accept_invite.html", context={"token": token, "email": user.email, "error": None}
    )


@router.post("/accept-invite/{token}")
def accept_invite_submit(
    token: str,
    request: Request,
    password: str = Form(""),
    confirm_password: str = Form(""),
    db: Session = Depends(get_db),
):
    account_token = find_valid_invite(db, token)
    if account_token is None:
        return templates.TemplateResponse(request=request, name="invite_invalid.html", status_code=400)
    user = db.get(User, account_token.user_id)

    if len(password) < MIN_PASSWORD_LENGTH:
        return templates.TemplateResponse(
            request=request,
            name="accept_invite.html",
            context={
                "token": token, "email": user.email,
                "error": f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
            },
            status_code=400,
        )
    if password != confirm_password:
        return templates.TemplateResponse(
            request=request,
            name="accept_invite.html",
            context={"token": token, "email": user.email, "error": "Passwords don't match."},
            status_code=400,
        )

    user.password_hash = hash_password(password)
    consume_token(account_token)
    db.commit()
    logger.info("User id=%s completed invite setup", user.id)

    request.session[SESSION_USER_KEY] = user.id
    return RedirectResponse(url="/dashboard", status_code=303)


@router.get("/forgot-password")
def forgot_password_form(request: Request, email: str = ""):
    return templates.TemplateResponse(request=request, name="forgot_password.html", context={"email": email})


@router.post("/forgot-password")
def forgot_password_submit(request: Request, email: str = Form(""), db: Session = Depends(get_db)):
    email = email.strip().lower()

    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    # An invited-but-not-yet-activated user (password_hash is None) has no
    # password to reset - they need their invite link, not this flow.
    if user is not None and user.is_active and user.password_hash is not None:
        settings = get_settings()
        code = issue_reset_code(db, user)
        db.commit()
        reset_url = f"{settings.app_base_url}/reset-password?email={quote(user.email)}"
        try:
            send_email(
                to=user.email,
                subject="Your Koya Talent password reset code",
                html=render_email("password_reset.html", name=user.name, code=code, reset_url=reset_url),
            )
        except EmailError as exc:
            logger.warning("Password reset email failed for user id=%s: %s", user.id, exc)
    else:
        logger.info("Password reset requested for an email with no active/activated account: %s", email)

    # Always land on the same page regardless of whether the account
    # existed - spec-equivalent to §7's "no email-confirmation gate"
    # reasoning elsewhere in this app, applied here to avoid letting this
    # form double as an "is this email registered" oracle. reset-password
    # itself never distinguishes "no such user" from "wrong code" either,
    # so redirecting straight there (with the email carried along, instead
    # of making the person retype it) loses no anti-enumeration protection.
    return RedirectResponse(url=f"/reset-password?email={quote(email)}&sent=1", status_code=303)


@router.get("/reset-password")
def reset_password_form(request: Request, email: str = "", sent: str = ""):
    return templates.TemplateResponse(
        request=request, name="reset_password.html", context={"email": email, "error": None, "sent": bool(sent)}
    )


@router.post("/reset-password")
def reset_password_submit(
    request: Request,
    email: str = Form(""),
    code: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    db: Session = Depends(get_db),
):
    email = email.strip().lower()
    # One deliberately generic message for "no such user," "wrong code,"
    # and "code expired" - distinguishing them would tell an attacker
    # which part of their guess was right.
    generic_error = "That code is invalid or has expired. Request a new one and try again."

    user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if user is None:
        return templates.TemplateResponse(
            request=request, name="reset_password.html",
            context={"email": email, "error": generic_error}, status_code=400,
        )

    account_token = check_reset_code(db, user, code.strip())
    if account_token is None:
        db.commit()  # persists the failed-attempt counter from check_reset_code
        return templates.TemplateResponse(
            request=request, name="reset_password.html",
            context={"email": email, "error": generic_error}, status_code=400,
        )

    if len(password) < MIN_PASSWORD_LENGTH:
        return templates.TemplateResponse(
            request=request, name="reset_password.html",
            context={"email": email, "error": f"Password must be at least {MIN_PASSWORD_LENGTH} characters."},
            status_code=400,
        )
    if password != confirm_password:
        return templates.TemplateResponse(
            request=request, name="reset_password.html",
            context={"email": email, "error": "Passwords don't match."}, status_code=400,
        )

    user.password_hash = hash_password(password)
    consume_token(account_token)
    db.commit()
    logger.info("User id=%s completed password reset", user.id)

    request.session[SESSION_USER_KEY] = user.id
    return RedirectResponse(url="/dashboard", status_code=303)
