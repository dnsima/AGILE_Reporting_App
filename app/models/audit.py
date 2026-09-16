"""Append-only audit trail for uploads, edits and administrative actions."""

from __future__ import annotations

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_entity", "entity_type", "entity_id"),
        Index("ix_audit_created", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64))

    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    actor_email: Mapped[str | None] = mapped_column(String(255), index=True)
    actor_role: Mapped[str | None] = mapped_column(String(24))
    state_id: Mapped[int | None] = mapped_column(ForeignKey("states.id"), index=True)
    period_id: Mapped[int | None] = mapped_column(ForeignKey("reporting_periods.id"))

    summary: Mapped[str | None] = mapped_column(Text)
    before_state: Mapped[dict | None] = mapped_column(JSON)
    after_state: Mapped[dict | None] = mapped_column(JSON)
    request_id: Mapped[str | None] = mapped_column(String(64), index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    status_code: Mapped[int | None] = mapped_column(Integer)
