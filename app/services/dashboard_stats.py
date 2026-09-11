from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Proposal, User
from app.models.proposal import PROPOSAL_STATUSES

STATUS_LABELS = {
    "draft": "Draft",
    "pending_approval": "Pending Approval",
    "changes_requested": "Changes Requested",
    "approved": "Approved",
    "sent": "Sent",
}


def my_proposals_filter(user: User):
    """A person's own proposals are whatever they created or are the
    assigned approver for - shared by the dashboard's stats/recent-proposals
    view and the full /proposals list so the two never disagree about what
    counts as "mine"."""
    return or_(Proposal.created_by == user.id, Proposal.approver_id == user.id)


def get_dashboard_stats(user: User, db: Session) -> dict:
    rows = db.execute(
        select(Proposal.status, func.count(Proposal.id))
        .where(my_proposals_filter(user))
        .group_by(Proposal.status)
    ).all()
    by_status = {status_value: 0 for status_value in PROPOSAL_STATUSES}
    total = 0
    for status_value, count in rows:
        by_status[status_value] = count
        total += count
    return {"total": total, "by_status": by_status}


def visible_status_tiles(user: User) -> dict:
    """Draft is structurally meaningless for a pure approver - a draft
    never has an approver_id set (only gets one once submitted for
    review), so my_proposals_filter can never return a nonzero Draft
    count for them. Everyone with can_create (salesperson or admin) keeps
    the full set."""
    if user.can_create:
        return STATUS_LABELS
    return {key: label for key, label in STATUS_LABELS.items() if key != "draft"}
