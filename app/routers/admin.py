import logging

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db
from app.models import User
from app.security import hash_password
from app.templating import templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


def _render_users_page(request, db: Session, error: str | None = None, status_code: int = 200):
    users = db.execute(select(User).order_by(User.id)).scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="admin_users.html",
        context={"users": users, "error": error},
        status_code=status_code,
    )


@router.get("/users")
def list_users(request: Request, db: Session = Depends(get_db)):
    return _render_users_page(request, db)


@router.post("/users")
def create_user(
    request: Request,
    # "" not Form(...): see the note in routers/proposals.py - Starlette
    # drops blank urlencoded fields entirely rather than keeping "".
    name: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    can_create: bool = Form(False),
    can_approve: bool = Form(False),
    is_admin: bool = Form(False),
    db: Session = Depends(get_db),
):
    name = name.strip()
    email = email.strip().lower()
    if not name or not email or len(password) < 8:
        return _render_users_page(
            request, db, error="Name and email are required, and password must be at least 8 characters.",
            status_code=400,
        )

    user = User(
        name=name,
        email=email,
        password_hash=hash_password(password),
        can_create=can_create,
        can_approve=can_approve,
        is_admin=is_admin,
        is_active=True,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        logger.warning("Attempted to create duplicate user email=%s", email)
        return _render_users_page(request, db, error=f"A user with email {email!r} already exists.", status_code=400)

    logger.info("Admin created user id=%s email=%s", user.id, user.email)
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/permissions")
def update_permissions(
    request: Request,
    user_id: int,
    can_create: bool = Form(False),
    can_approve: bool = Form(False),
    is_admin: bool = Form(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    user = db.get(User, user_id)
    if user is None:
        return _render_users_page(request, db, error="User not found.", status_code=404)

    if user.id == current_user.id and not is_admin:
        return _render_users_page(
            request, db, error="You can't remove your own admin access.", status_code=400
        )

    user.can_create = can_create
    user.can_approve = can_approve
    user.is_admin = is_admin
    db.commit()
    logger.info(
        "Admin id=%s updated permissions for user id=%s: can_create=%s can_approve=%s is_admin=%s",
        current_user.id, user.id, can_create, can_approve, is_admin,
    )
    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/toggle-active")
def toggle_active(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    user = db.get(User, user_id)
    if user is None:
        return _render_users_page(request, db, error="User not found.", status_code=404)

    if user.id == current_user.id and user.is_active:
        return _render_users_page(
            request, db, error="You can't deactivate your own account.", status_code=400
        )

    user.is_active = not user.is_active
    db.commit()
    logger.info("Admin id=%s set user id=%s is_active=%s", current_user.id, user.id, user.is_active)
    return RedirectResponse(url="/admin/users", status_code=303)
