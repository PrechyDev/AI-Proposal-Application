from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

# "invite": admin creates a user with no password (GitHub-style) and the
# user sets their own via a one-time link. "reset": forgot-password, a
# short numeric code rather than a link - see app/services/account_tokens.py
# for why the two use different secret shapes.
TOKEN_PURPOSES = ("invite", "reset")


class AccountToken(Base):
    __tablename__ = "account_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    purpose: Mapped[str] = mapped_column(
        Enum(*TOKEN_PURPOSES, name="account_token_purpose", native_enum=False, validate_strings=True),
        nullable=False,
    )
    # Only a hash of the secret is ever stored - same principle as password
    # storage - so a DB leak alone can't be used to complete an invite or
    # reset a password. The raw secret exists only in the email that was sent.
    token_hash: Mapped[str] = mapped_column(String, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
