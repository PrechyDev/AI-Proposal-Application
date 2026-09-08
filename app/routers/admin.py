import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.config import get_settings
from app.db import get_db
from app.models import Proposal, User
from app.models.proposal import PROPOSAL_STATUSES
from app.services.account_tokens import issue_invite_token
from app.services.email import EmailError, send_email
from app.templating import render_email, templates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


def _render_users_page(
    request, db: Session, current_user: User, error: str | None = None, status_code: int = 200
):
    users = db.execute(select(User).order_by(User.id)).scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="admin_users.html",
        context={"users": users, "user": current_user, "error": error},
        status_code=status_code,
    )


@router.get("/users")
def list_users(request: Request, db: Session = Depends(get_db), current_user: User = Depends(require_admin)):
    return _render_users_page(request, db, current_user)


def _send_invite_email(db: Session, user: User, inviter: User) -> None:
    """Issues a fresh invite token and emails it - shared by both creating
    a new user and resending a lost/expired invite, so the two paths can
    never drift (spec-equivalent principle to every other email path in
    this app: log the outcome, never let a send failure corrupt the
    already-committed account state)."""
    settings = get_settings()
    raw_token = issue_invite_token(db, user)
    db.commit()
    accept_url = f"{settings.app_base_url}/accept-invite/{raw_token}"
    try:
        send_email(
            to=user.email,
            subject="You're invited to Koya Talent",
            html=render_email("invite.html", name=user.name, inviter_name=inviter.name, accept_url=accept_url),
        )
    except EmailError as exc:
        logger.warning("Invite email failed for user id=%s: %s", user.id, exc)
        raise


@router.post("/users")
def create_user(
    request: Request,
    # "" not Form(...): see the note in routers/proposals.py - Starlette
    # drops blank urlencoded fields entirely rather than keeping "".
    name: str = Form(""),
    email: str = Form(""),
    can_create: bool = Form(False),
    can_approve: bool = Form(False),
    is_admin: bool = Form(False),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    name = name.strip()
    email = email.strip().lower()
    if not name or not email:
        return _render_users_page(request, db, current_user, error="Name and email are required.", status_code=400)

    # No password here (GitHub-style invite flow, not admin-set passwords):
    # the user sets their own via the emailed invite link
    # (app/routers/account.py), so password_hash starts out None.
    user = User(
        name=name,
        email=email,
        password_hash=None,
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
        return _render_users_page(request, db, current_user, error=f"A user with email {email!r} already exists.", status_code=400)

    logger.info("Admin id=%s created user id=%s email=%s (invite pending)", current_user.id, user.id, user.email)

    try:
        _send_invite_email(db, user, current_user)
    except EmailError:
        return _render_users_page(
            request, db, current_user,
            error=(
                f"User {user.email!r} was created, but the invite email failed to send. "
                f"Use \"Resend invite\" below to try again."
            ),
            status_code=502,
        )

    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/resend-invite")
def resend_invite(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    user = db.get(User, user_id)
    if user is None:
        return _render_users_page(request, db, current_user, error="User not found.", status_code=404)
    if user.password_hash is not None:
        return _render_users_page(
            request, db, current_user,
            error=f"{user.email} has already set a password - nothing to resend.", status_code=400,
        )

    try:
        _send_invite_email(db, user, current_user)
    except EmailError:
        return _render_users_page(
            request, db, current_user,
            error=f"Failed to resend the invite email to {user.email}.", status_code=502,
        )

    logger.info("Admin id=%s resent invite to user id=%s", current_user.id, user.id)
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
        return _render_users_page(request, db, current_user, error="User not found.", status_code=404)

    if user.id == current_user.id and not is_admin:
        return _render_users_page(
            request, db, current_user, error="You can't remove your own admin access.", status_code=400
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
        return _render_users_page(request, db, current_user, error="User not found.", status_code=404)

    if user.id == current_user.id and user.is_active:
        return _render_users_page(
            request, db, current_user, error="You can't deactivate your own account.", status_code=400
        )

    user.is_active = not user.is_active
    db.commit()
    logger.info("Admin id=%s set user id=%s is_active=%s", current_user.id, user.id, user.is_active)
    return RedirectResponse(url="/admin/users", status_code=303)


def _render_admin_proposals(
    request: Request,
    db: Session,
    filters: dict,
    user: User,
    error: str | None = None,
    status_code: int = 200,
):
    query = select(Proposal)
    if filters["status"]:
        query = query.where(Proposal.status == filters["status"])
    if filters["client"].strip():
        like = f"%{filters['client'].strip()}%"
        query = query.where(or_(Proposal.client_name.ilike(like), Proposal.company_name.ilike(like)))
    if filters["salesperson_id"].isdigit():
        query = query.where(Proposal.created_by == int(filters["salesperson_id"]))
    if filters["approver_id"].isdigit():
        query = query.where(Proposal.approver_id == int(filters["approver_id"]))
    if filters["date_from"].strip():
        try:
            query = query.where(Proposal.created_at >= datetime.strptime(filters["date_from"].strip(), "%Y-%m-%d"))
        except ValueError:
            pass
    if filters["date_to"].strip():
        try:
            end = datetime.strptime(filters["date_to"].strip(), "%Y-%m-%d") + timedelta(days=1)
            query = query.where(Proposal.created_at < end)
        except ValueError:
            pass

    proposals = db.execute(query.order_by(Proposal.updated_at.desc())).scalars().all()
    people = {u.id: u.name for u in db.execute(select(User)).scalars().all()}
    salespeople = db.execute(
        select(User).where(User.can_create.is_(True)).order_by(User.name)
    ).scalars().all()
    approvers = db.execute(
        select(User).where(User.can_approve.is_(True), User.is_active.is_(True)).order_by(User.name)
    ).scalars().all()

    return templates.TemplateResponse(
        request=request,
        name="admin_proposals.html",
        context={
            "proposals": proposals,
            "people": people,
            "salespeople": salespeople,
            "approvers": approvers,
            "statuses": PROPOSAL_STATUSES,
            "filters": filters,
            "error": error,
            "user": user,
        },
        status_code=status_code,
    )


@router.get("/proposals")
def list_all_proposals(
    request: Request,
    status: str = "",
    client: str = "",
    salesperson_id: str = "",
    approver_id: str = "",
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    filters = {
        "status": status, "client": client, "salesperson_id": salesperson_id,
        "approver_id": approver_id, "date_from": date_from, "date_to": date_to,
    }
    return _render_admin_proposals(request, db, filters, user)


@router.post("/proposals/{proposal_id}/reassign-approver")
def reassign_approver(
    proposal_id: int,
    request: Request,
    approver_id: str = Form(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Spec section 7: "Approver's can_approve revoked mid-flight -> Admin
    can reassign the pending proposal to a different approver." Deliberately
    narrow to pending_approval, matching "the pending proposal" wording -
    reassigning who approved an already-approved/sent proposal after the
    fact isn't a real operation this app models.
    """
    empty_filters = {"status": "", "client": "", "salesperson_id": "", "approver_id": "", "date_from": "", "date_to": ""}
    proposal = db.get(Proposal, proposal_id)
    if proposal is None:
        return _render_admin_proposals(
            request, db, empty_filters, current_user, error="Proposal not found.", status_code=404
        )
    if proposal.status != "pending_approval":
        return _render_admin_proposals(
            request, db, empty_filters, current_user,
            error=f"Can't reassign approver on a proposal that is '{proposal.status}', not 'pending_approval'.",
            status_code=400,
        )

    new_approver = None
    if approver_id.strip().isdigit():
        new_approver = db.execute(
            select(User).where(
                User.id == int(approver_id), User.can_approve.is_(True), User.is_active.is_(True)
            )
        ).scalar_one_or_none()
    if new_approver is None:
        return _render_admin_proposals(
            request, db, empty_filters, current_user,
            error="Pick a valid, active approver to reassign to.", status_code=400
        )

    proposal.approver_id = new_approver.id
    db.commit()
    logger.info(
        "Admin id=%s reassigned proposal id=%s to approver id=%s",
        current_user.id, proposal.id, new_approver.id,
    )
    return RedirectResponse(url="/admin/proposals", status_code=303)
