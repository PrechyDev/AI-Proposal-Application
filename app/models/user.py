from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    # Nullable: an invited (not-yet-activated) user has no password until
    # they complete the invite-email flow (app/services/account_tokens.py) -
    # None here means "hasn't set one up yet," not "no login," which login
    # itself (app/routers/auth.py) treats as a distinct, clearly-messaged case.
    password_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    can_create: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    can_approve: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
