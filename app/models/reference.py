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


class Indicator(Base, TimestampMixin):
    """One of the 70 AGILE KPIs."""

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

    category: Mapped[IndicatorCategory | None] = relationship(back_populates="indicators")

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
