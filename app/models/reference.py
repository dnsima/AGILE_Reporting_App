"""Reference (master) data: cohorts, states, indicators and reporting periods."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    AggregationMethod,
    Direction,
    IndicatorUnit,
    PeriodType,
)
from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.submission import Submission, Target


class Cohort(Base, TimestampMixin):
    """An AGILE financing cohort / cycle."""

    __tablename__ = "cohorts"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    financing_window: Mapped[str | None] = mapped_column(String(128))
    start_year: Mapped[int | None] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    states: Mapped[list["State"]] = relationship(back_populates="cohort")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Cohort {self.code}>"


class State(Base, TimestampMixin):
    """A Nigerian state (or the FCT) participating in AGILE."""

    __tablename__ = "states"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    geopolitical_zone: Mapped[str | None] = mapped_column(String(64), index=True)
    cohort_id: Mapped[int | None] = mapped_column(ForeignKey("cohorts.id"), index=True)
    piu_contact_email: Mapped[str | None] = mapped_column(String(255))
    joined_on: Mapped[date | None] = mapped_column(Date)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    cohort: Mapped[Cohort | None] = relationship(back_populates="states")
    submissions: Mapped[list["Submission"]] = relationship(
        back_populates="state", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<State {self.code} {self.name}>"


class IndicatorCategory(Base, TimestampMixin):
    """Results-framework grouping, e.g. PDO or a project component."""

    __tablename__ = "indicator_categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    indicators: Mapped[list["Indicator"]] = relationship(back_populates="category")


class Subcomponent(Base, TimestampMixin):
    """A results-framework sub-component, e.g. 1.1 or 2.2b.

    Reporting obligations are defined at this level: a state reports the
    indicators belonging to the sub-components it actually implements.
    """

    __tablename__ = "subcomponents"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    indicators: Mapped[list["Indicator"]] = relationship(back_populates="subcomponent")


class StateSubcomponent(Base, TimestampMixin):
    """Whether one state implements one sub-component.

    This is what stops a legitimate blank being reported as a data gap: Ekiti
    does not implement 1.1, and the Limited Financing states implement only 1.2
    and 2.1, so neither is expected to report against the rest.
    """

    __tablename__ = "state_subcomponents"
    __table_args__ = (
        UniqueConstraint("state_id", "subcomponent_id", name="state_subcomponent_identity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    state_id: Mapped[int] = mapped_column(ForeignKey("states.id"), nullable=False, index=True)
    subcomponent_id: Mapped[int] = mapped_column(
        ForeignKey("subcomponents.id"), nullable=False, index=True
    )
    implements: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)

    state: Mapped["State"] = relationship()
    subcomponent: Mapped[Subcomponent] = relationship()


class IndicatorLink(Base, TimestampMixin):
    """Lineage between an indicator in one framework revision and the next.

    Codes were reused across the Q1-to-Q2 revision -- Q1 PDO-01 counted school
    buildings, Q2 PDO-01 counts students -- so period-over-period comparison
    has to follow this lineage rather than the code.
    """

    __tablename__ = "indicator_links"
    __table_args__ = (
        Index("ix_indicator_links_predecessor", "predecessor_code"),
        Index("ix_indicator_links_successor", "successor_code"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Codes rather than ids: a predecessor may no longer exist as an indicator.
    predecessor_code: Mapped[str] = mapped_column(String(32), nullable=False)
    successor_code: Mapped[str | None] = mapped_column(String(32))
    disposition: Mapped[str] = mapped_column(String(16), nullable=False)
    #: False for TRANSFORMED, DROPPED and NEW: no like-for-like basis exists.
    is_comparable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: The revision at which the change took effect.
    effective_from_period: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)


class Indicator(Base, TimestampMixin):
    """One indicator of the AGILE results framework."""

    __tablename__ = "indicators"
    __table_args__ = (Index("ix_indicators_category_number", "category_id", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[int] = mapped_column(Integer, unique=True, nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(400), nullable=False)
    definition: Mapped[str | None] = mapped_column(Text)
    category_id: Mapped[int | None] = mapped_column(
        ForeignKey("indicator_categories.id"), index=True
    )
    subcomponent_id: Mapped[int | None] = mapped_column(
        ForeignKey("subcomponents.id"), index=True
    )
    #: The code this indicator carried before the sub-component recode.
    legacy_code: Mapped[str | None] = mapped_column(String(32), index=True)

    unit: Mapped[str] = mapped_column(String(32), default=IndicatorUnit.NUMBER, nullable=False)
    aggregation_method: Mapped[str] = mapped_column(
        String(32), default=AggregationMethod.SUM, nullable=False
    )
    direction: Mapped[str] = mapped_column(String(16), default=Direction.INCREASE, nullable=False)
    is_cumulative: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    requires_numerator_denominator: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    baseline_value: Mapped[float | None] = mapped_column(Float)
    min_value: Mapped[float | None] = mapped_column(Float)
    max_value: Mapped[float | None] = mapped_column(Float)
    decimal_places: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Disaggregation axes the template may carry, e.g. ["sex", "school_level"].
    disaggregations: Mapped[list | None] = mapped_column(JSON, default=list)
    #: Alternative header spellings used by state templates, for auto-mapping.
    aliases: Mapped[list | None] = mapped_column(JSON, default=list)

    is_core: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: False for derived rows the platform computes rather than collects.
    is_reported: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Codes this indicator must equal the sum of, e.g. C1.0-01 = its 1.1 and
    #: 1.2 parts. Empty for ordinary indicators.
    composite_of: Mapped[list | None] = mapped_column(JSON, default=list)

    #: Framework revision this indicator belongs to, as period sort keys. NULL
    #: means "applies to every period".
    valid_from_period: Mapped[str | None] = mapped_column(String(32))
    valid_to_period: Mapped[str | None] = mapped_column(String(32))

    category: Mapped[IndicatorCategory | None] = relationship(back_populates="indicators")
    subcomponent: Mapped[Subcomponent | None] = relationship(back_populates="indicators")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Indicator {self.code}>"


class ReportingPeriod(Base, TimestampMixin):
    """A reporting window; monthly, quarterly, semi-annual or annual."""

    __tablename__ = "reporting_periods"
    __table_args__ = (
        UniqueConstraint("period_type", "fiscal_year", "sequence", name="period_identity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    period_type: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    fiscal_year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    #: Month (1-12), quarter (1-4), half (1-2) or 1 for annual periods.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    #: Deadline used by the timeliness dimension of the DQA.
    due_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_open: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    submissions: Mapped[list["Submission"]] = relationship(back_populates="period")
    targets: Mapped[list["Target"]] = relationship(back_populates="period")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ReportingPeriod {self.code}>"

    @property
    def sort_key(self) -> tuple[int, int, int]:
        order = {
            PeriodType.MONTHLY: 0,
            PeriodType.QUARTERLY: 1,
            PeriodType.SEMI_ANNUAL: 2,
            PeriodType.ANNUAL: 3,
        }
        return (self.fiscal_year, order.get(PeriodType(self.period_type), 9), self.sequence)
