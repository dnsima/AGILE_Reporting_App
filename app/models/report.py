"""Generated routine/periodic reports."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import ReportScope
from app.db.base import Base, TimestampMixin


class GeneratedReport(Base, TimestampMixin):
    __tablename__ = "generated_reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    scope: Mapped[str] = mapped_column(String(16), default=ReportScope.NATIONAL, nullable=False)
    scope_ref: Mapped[str | None] = mapped_column(String(64))
    period_code: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    period_type: Mapped[str | None] = mapped_column(String(24))
    formats: Mapped[list | None] = mapped_column(JSON, default=list)
    file_paths: Mapped[dict | None] = mapped_column(JSON, default=dict)
    parameters: Mapped[dict | None] = mapped_column(JSON, default=dict)
    indicator_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    generated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    summary: Mapped[str | None] = mapped_column(Text)
