"""Users, API keys and refresh-free session identity."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import Role
from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.reference import State


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(24), default=Role.VIEWER, nullable=False, index=True)
    #: Required for STATE_PIU users; scopes everything they can see or upload.
    state_id: Mapped[int | None] = mapped_column(ForeignKey("states.id"), index=True)
    phone: Mapped[str | None] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    state: Mapped["State | None"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.email} ({self.role})>"


class ApiKey(Base, TimestampMixin):
    """Read-only credential issued to an external dashboard or data consumer."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(24), default=Role.VIEWER, nullable=False)
    state_id: Mapped[int | None] = mapped_column(ForeignKey("states.id"))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    state: Mapped["State | None"] = relationship()
