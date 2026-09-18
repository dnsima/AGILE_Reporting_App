"""Data queries, evidence and restatements.

A validation finding is not a wall the submission hits: it becomes a query
assigned to the state that owns the figure. The state responds with evidence
and either confirms the figure or proposes a correction; the NPCU accepts or
rejects. Only an accepted correction changes the stored figure, and the
original is never overwritten -- ``IndicatorValue.original_value`` keeps what
was first reported so a published report and the live dashboard can always be
reconciled.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import OPEN_QUERY_STATUSES, QueryStatus
from app.db.base import Base, TimestampMixin


class DataQuery(Base, TimestampMixin):
    """One flagged figure, raised with the state that reported it."""

    __tablename__ = "data_queries"
    __table_args__ = (
        Index("ix_queries_state_period_status", "state_id", "period_id", "status"),
        Index("ix_queries_due", "due_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    submission_id: Mapped[int | None] = mapped_column(
        ForeignKey("submissions.id", ondelete="SET NULL"), index=True
    )
    state_id: Mapped[int] = mapped_column(ForeignKey("states.id"), nullable=False, index=True)
    period_id: Mapped[int] = mapped_column(
        ForeignKey("reporting_periods.id"), nullable=False, index=True
    )
    indicator_id: Mapped[int | None] = mapped_column(ForeignKey("indicators.id"), index=True)

    #: Which rule raised it, and what it said. Kept verbatim so the state sees
    #: the same wording the NPCU saw.
    rule_code: Mapped[str | None] = mapped_column(String(64), index=True)
    dimension: Mapped[str | None] = mapped_column(String(24))
    severity: Mapped[str | None] = mapped_column(String(16), index=True)
    title: Mapped[str] = mapped_column(String(400), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    #: The figure as it stood when the query was raised.
    reported_value: Mapped[float | None] = mapped_column(Float)

    status: Mapped[str] = mapped_column(
        String(16), default=QueryStatus.OPEN, nullable=False, index=True
    )
    due_date: Mapped[date | None] = mapped_column(Date)
    opened_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(String(24))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    #: Set when the query is parked for checking on a supervision or DQA visit.
    verification_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    state: Mapped["State"] = relationship()  # noqa: F821
    period: Mapped["ReportingPeriod"] = relationship()  # noqa: F821
    indicator: Mapped["Indicator | None"] = relationship()  # noqa: F821
    responses: Mapped[list["QueryResponse"]] = relationship(
        back_populates="query", cascade="all, delete-orphan", order_by="QueryResponse.id"
    )

    @property
    def is_open(self) -> bool:
        return QueryStatus(self.status) in OPEN_QUERY_STATUSES

    def is_overdue(self, today: date | None = None) -> bool:
        if not self.is_open or self.due_date is None:
            return False
        return (today or date.today()) > self.due_date

    def days_overdue(self, today: date | None = None) -> int:
        if not self.is_overdue(today):
            return 0
        return ((today or date.today()) - self.due_date).days


class QueryResponse(Base, TimestampMixin):
    """A state's answer to a query: evidence, and a figure confirmed or corrected."""

    __tablename__ = "query_responses"

    id: Mapped[int] = mapped_column(primary_key=True)
    query_id: Mapped[int] = mapped_column(
        ForeignKey("data_queries.id", ondelete="CASCADE"), nullable=False, index=True
    )
    responder_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The state's explanation, and what evidence backs it.
    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_summary: Mapped[str | None] = mapped_column(Text)
    #: NULL means "the figure stands as reported"; a value proposes a correction.
    proposed_value: Mapped[float | None] = mapped_column(Float)
    #: Which period the correction applies to. A cumulative figure that appears
    #: to fall is most often resolved by restating the *earlier* period, once
    #: evidence shows the original was overstated -- so a response may correct a
    #: period other than the one the query was raised against. NULL means the
    #: query's own period.
    restates_period_id: Mapped[int | None] = mapped_column(
        ForeignKey("reporting_periods.id")
    )

    #: Set when the NPCU reviews this response.
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_outcome: Mapped[str | None] = mapped_column(String(16))
    review_note: Mapped[str | None] = mapped_column(Text)

    query: Mapped[DataQuery] = relationship(back_populates="responses")
    evidence: Mapped[list["EvidenceFile"]] = relationship(
        back_populates="response", cascade="all, delete-orphan"
    )


class EvidenceFile(Base, TimestampMixin):
    """A document attached to a query response."""

    __tablename__ = "evidence_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    response_id: Mapped[int] = mapped_column(
        ForeignKey("query_responses.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(128))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: SHA-256, so an evidence file can be shown not to have changed since it
    #: was submitted.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    uploaded_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    description: Mapped[str | None] = mapped_column(Text)

    response: Mapped[QueryResponse] = relationship(back_populates="evidence")


class ValueRevision(Base, TimestampMixin):
    """An approved change to a reported figure.

    Every restatement is recorded here with who proposed it, who approved it and
    why. The figure's original remains on the value row, so "as first reported"
    and "as currently stated" are both always available.
    """

    __tablename__ = "value_revisions"
    __table_args__ = (Index("ix_revisions_value", "indicator_value_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    indicator_value_id: Mapped[int] = mapped_column(
        ForeignKey("indicator_values.id", ondelete="CASCADE"), nullable=False
    )
    query_id: Mapped[int | None] = mapped_column(ForeignKey("data_queries.id"), index=True)
    response_id: Mapped[int | None] = mapped_column(ForeignKey("query_responses.id"))

    previous_value: Mapped[float | None] = mapped_column(Float)
    new_value: Mapped[float | None] = mapped_column(Float)
    reason: Mapped[str] = mapped_column(Text, nullable=False)

    proposed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    indicator_value: Mapped["IndicatorValue"] = relationship()  # noqa: F821
