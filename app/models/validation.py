"""Validation rules, issues raised against submissions and DQA scorecards."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import Severity
from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.submission import Submission


class ValidationRule(Base, TimestampMixin):
    """Configuration row for a rule executed by the validation engine.

    Rules are registered in code (see ``app.services.validation``) and mirrored
    here so administrators can toggle them, retune thresholds and document the
    quality dimension each one serves without a redeploy.
    """

    __tablename__ = "validation_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    dimension: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), default=Severity.ERROR, nullable=False)
    #: Rule-specific tuning, e.g. {"threshold_pct": 200}.
    config: Mapped[dict | None] = mapped_column(JSON, default=dict)
    #: When set the rule only applies to one indicator.
    indicator_id: Mapped[int | None] = mapped_column(ForeignKey("indicators.id"))
    #: Blocking rules stop a submission from being approved.
    is_blocking: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ValidationIssue(Base, TimestampMixin):
    """A single finding raised while validating a submission."""

    __tablename__ = "validation_issues"
    __table_args__ = (
        Index("ix_issues_submission_dimension", "submission_id", "dimension"),
        Index("ix_issues_severity", "severity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rule_code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    dimension: Mapped[str] = mapped_column(String(24), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    indicator_id: Mapped[int | None] = mapped_column(ForeignKey("indicators.id"))
    field: Mapped[str | None] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    observed: Mapped[str | None] = mapped_column(String(255))
    expected: Mapped[str | None] = mapped_column(String(255))
    source_row: Mapped[int | None] = mapped_column(Integer)
    is_blocking: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    context: Mapped[dict | None] = mapped_column(JSON, default=dict)

    submission: Mapped["Submission"] = relationship(back_populates="issues")


class DQAScore(Base, TimestampMixin):
    """Per-dimension data quality score for one submission."""

    __tablename__ = "dqa_scores"
    __table_args__ = (
        UniqueConstraint("submission_id", "dimension", name="dqa_dimension_identity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dimension: Mapped[str] = mapped_column(String(24), nullable=False)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    checks_run: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    checks_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    details: Mapped[dict | None] = mapped_column(JSON, default=dict)

    submission: Mapped["Submission"] = relationship(back_populates="dqa_scores")
