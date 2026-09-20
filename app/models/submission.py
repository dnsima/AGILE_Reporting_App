"""Submission, indicator values and targets."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import DisclosureStatus, SubmissionStatus, TargetLevel
from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.reference import Indicator, ReportingPeriod, State
    from app.models.user import User
    from app.models.validation import DQAScore, ValidationIssue


class Submission(Base, TimestampMixin):
    """One upload of a state reporting template for one reporting period.

    Re-uploads create a new ``version``; the previously current submission is
    marked ``SUPERSEDED``. Only ``is_current`` + ``APPROVED`` submissions feed
    the analysis pipeline.
    """

    __tablename__ = "submissions"
    __table_args__ = (
        UniqueConstraint("state_id", "period_id", "version", name="submission_version"),
        Index("ix_submissions_state_period_current", "state_id", "period_id", "is_current"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    state_id: Mapped[int] = mapped_column(ForeignKey("states.id"), nullable=False, index=True)
    period_id: Mapped[int] = mapped_column(
        ForeignKey("reporting_periods.id"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), default=SubmissionStatus.UPLOADED, nullable=False, index=True
    )
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    source_file_name: Mapped[str | None] = mapped_column(String(512))
    stored_file_path: Mapped[str | None] = mapped_column(String(1024))
    file_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    template_profile: Mapped[str | None] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    mapped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unmapped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    #: How many figures in this submission are under query, and how many are
    #: marked unfit for use. Neither count removes anything from an aggregate.
    open_query_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    quarantined_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    dqa_score: Mapped[float | None] = mapped_column(Float, index=True)
    dqa_grade: Mapped[str | None] = mapped_column(String(24))
    #: The headline a reader acts on. The DQA score measures how many checks
    #: passed; this says whether the return can be used.
    fitness_verdict: Mapped[str | None] = mapped_column(String(24), index=True)
    #: Share of this state's reported volume, weighted by each figure's place
    #: in the national total, that sits behind an unfit or queried figure.
    exposed_share: Mapped[float | None] = mapped_column(Float)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    warning_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Free-form parsing/mapping diagnostics kept for the audit trail.
    ingestion_report: Mapped[dict | None] = mapped_column(JSON)
    notes: Mapped[str | None] = mapped_column(Text)
    rejection_reason: Mapped[str | None] = mapped_column(Text)

    state: Mapped["State"] = relationship(back_populates="submissions")
    period: Mapped["ReportingPeriod"] = relationship(back_populates="submissions")
    uploaded_by: Mapped["User | None"] = relationship(foreign_keys=[uploaded_by_id])
    approved_by: Mapped["User | None"] = relationship(foreign_keys=[approved_by_id])
    values: Mapped[list["IndicatorValue"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )
    issues: Mapped[list["ValidationIssue"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )
    dqa_scores: Mapped[list["DQAScore"]] = relationship(
        back_populates="submission", cascade="all, delete-orphan"
    )

    @property
    def unfit_count(self) -> int:
        """Figures marked unfit for use. They still count towards the totals."""
        return self.quarantined_count

    @property
    def is_analysable(self) -> bool:
        """Only approved, current submissions may enter the analysis pipeline."""
        return self.is_current and self.status == SubmissionStatus.APPROVED

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Submission state={self.state_id} period={self.period_id} v{self.version}>"


class IndicatorValue(Base, TimestampMixin):
    """A single reported KPI figure inside a submission."""

    __tablename__ = "indicator_values"
    __table_args__ = (
        Index("ix_values_submission_indicator", "submission_id", "indicator_id"),
        Index("ix_values_indicator", "indicator_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int] = mapped_column(
        ForeignKey("submissions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    indicator_id: Mapped[int] = mapped_column(ForeignKey("indicators.id"), nullable=False)

    value: Mapped[float | None] = mapped_column(Float)
    #: The figure as first reported. Set once at ingestion and never changed,
    #: so a published report and the live dashboard can always be reconciled
    #: after a restatement.
    original_value: Mapped[float | None] = mapped_column(Float)
    is_restated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    numerator: Mapped[float | None] = mapped_column(Float)
    denominator: Mapped[float | None] = mapped_column(Float)
    #: Verbatim cell content, retained so parsing decisions stay auditable.
    raw_value: Mapped[str | None] = mapped_column(String(255))
    #: Disaggregation key/values, e.g. {"sex": "female", "school_level": "JSS"}.
    disaggregation: Mapped[dict | None] = mapped_column(JSON, default=dict)
    data_source: Mapped[str | None] = mapped_column(String(255))
    comment: Mapped[str | None] = mapped_column(Text)
    source_row: Mapped[int | None] = mapped_column(Integer)
    #: False marks the figure unfit for use. It is NOT an exclusion: the figure
    #: still counts towards every aggregate, because the NPCU reports what
    #: states reported and discloses what it doubts. Removing a figure from a
    #: total silently restates the national result, which is a decision for the
    #: change-management process and not for a validation rule.
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: The fuller label behind ``is_valid``: CLEAN, QUERIED, UNFIT or CORRECTED.
    #: Kept in step with it through ``services.disclosure.set_status``.
    disclosure_status: Mapped[str] = mapped_column(
        String(16), default=str(DisclosureStatus.CLEAN), nullable=False
    )
    #: Why the figure is flagged, shown wherever the figure is shown.
    quarantine_reason: Mapped[str | None] = mapped_column(String(255))

    submission: Mapped[Submission] = relationship(back_populates="values")
    indicator: Mapped["Indicator"] = relationship()

    @property
    def effective_value(self) -> float | None:
        """Value as reported, deriving percentages from numerator/denominator."""
        if self.value is not None:
            return self.value
        if self.numerator is not None and self.denominator:
            return self.numerator / self.denominator * 100.0
        return None


class Target(Base, TimestampMixin):
    """A state-level or national-level target for one indicator and period."""

    __tablename__ = "targets"
    __table_args__ = (
        UniqueConstraint("indicator_id", "period_id", "level", "state_id", name="target_identity"),
        Index("ix_targets_lookup", "period_id", "level", "state_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    indicator_id: Mapped[int] = mapped_column(ForeignKey("indicators.id"), nullable=False)
    period_id: Mapped[int] = mapped_column(ForeignKey("reporting_periods.id"), nullable=False)
    level: Mapped[str] = mapped_column(String(16), default=TargetLevel.STATE, nullable=False)
    #: NULL for national targets.
    state_id: Mapped[int | None] = mapped_column(ForeignKey("states.id"))
    target_value: Mapped[float] = mapped_column(Float, nullable=False)
    #: Targets only count once the project's governance body has cleared them.
    status: Mapped[str] = mapped_column(String(16), default="APPROVED", nullable=False)
    approved_by_body: Mapped[str | None] = mapped_column(String(255))
    approved_on: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str | None] = mapped_column(String(255))
    notes: Mapped[str | None] = mapped_column(Text)

    indicator: Mapped["Indicator"] = relationship()
    period: Mapped["ReportingPeriod"] = relationship(back_populates="targets")
    state: Mapped["State | None"] = relationship()
