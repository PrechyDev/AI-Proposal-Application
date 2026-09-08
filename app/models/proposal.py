import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

PROPOSAL_STATUSES = ("draft", "pending_approval", "changes_requested", "approved", "sent")


class Proposal(Base):
    __tablename__ = "proposals"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Intake fields (assets/intake-form-fields.md)
    client_name: Mapped[str] = mapped_column(String, nullable=False)
    client_email: Mapped[str] = mapped_column(String, nullable=False)
    company_name: Mapped[str] = mapped_column(String, nullable=False)
    date_of_call: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    client_needs_summary: Mapped[str] = mapped_column(Text, nullable=False)
    project_scope: Mapped[str] = mapped_column(Text, nullable=False)
    goals_and_objectives: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_services: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_timeline: Mapped[str] = mapped_column(Text, nullable=False)
    estimated_pricing: Mapped[str] = mapped_column(Text, nullable=False)

    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    approver_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)

    status: Mapped[str] = mapped_column(
        Enum(*PROPOSAL_STATUSES, name="proposal_status", native_enum=False, validate_strings=True),
        nullable=False,
        default="draft",
    )

    client_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, unique=True
    )
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Set to the outgoing client_token's value at the moment a proposal is
    # reopened (app/routers/proposals.py's reopen_proposal), instead of
    # just discarding it - lets /view/{old_token} recognize "this exact
    # link really was valid a moment ago" and show a specific message,
    # rather than the generic silent redirect used for a token that was
    # never real (see client_view.py). Only the single most recently
    # retired token is kept, not a full history.
    retired_client_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    # True while a full-document regenerate is running in the background
    # (app/routers/proposals.py's regenerate_full route) - every viewer of
    # this proposal, not just the person who triggered it, sees a "please
    # wait" state while this is set, since the content is mid-rewrite.
    is_regenerating: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
