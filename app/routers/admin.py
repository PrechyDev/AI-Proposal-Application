import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import require_admin
from app.db import get_db
from app.models import Proposal, User
from app.models.proposal import PROPOSAL_STATUSES
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


def _render_admin_proposals(
    request: Request,
    db: Session,
    filters: dict,
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
):
    filters = {
        "status": status, "client": client, "salesperson_id": salesperson_id,
        "approver_id": approver_id, "date_from": date_from, "date_to": date_to,
    }
    return _render_admin_proposals(request, db, filters)


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
        return _render_admin_proposals(request, db, empty_filters, error="Proposal not found.", status_code=404)
    if proposal.status != "pending_approval":
        return _render_admin_proposals(
            request, db, empty_filters,
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
            request, db, empty_filters, error="Pick a valid, active approver to reassign to.", status_code=400
        )

    proposal.approver_id = new_approver.id
    db.commit()
    logger.info(
        "Admin id=%s reassigned proposal id=%s to approver id=%s",
        current_user.id, proposal.id, new_approver.id,
    )
    return RedirectResponse(url="/admin/proposals", status_code=303)
