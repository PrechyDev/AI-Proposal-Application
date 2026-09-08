from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ReferenceFile(Base):
    __tablename__ = "reference_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_path: Mapped[str] = mapped_column(String, nullable=False)
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), nullable=False, default=list)
    is_library: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    # Trash marker: NULL = live, set = in trash and eligible for the lazy
    # 30-day auto-purge (see _purge_expired_trash in app/routers/library.py) -
    # replaces the old is_library-flip "retire" mechanism with a real,
    # recoverable delete.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ProposalReference(Base):
    __tablename__ = "proposal_references"

    proposal_id: Mapped[int] = mapped_column(ForeignKey("proposals.id"), primary_key=True)
    reference_file_id: Mapped[int] = mapped_column(
        ForeignKey("reference_files.id"), primary_key=True
    )
