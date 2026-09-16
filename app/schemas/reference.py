"""Reference data schemas: cohorts, states, indicators, periods, targets."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field

from app.core.enums import (
    AggregationMethod,
    CohortCode,
    Direction,
    IndicatorUnit,
    PeriodType,
    TargetLevel,
)
from app.schemas.common import ORMModel


class CohortRead(ORMModel):
    id: int
    code: str
    name: str
    description: str | None = None
    financing_window: str | None = None
    start_year: int | None = None
    sort_order: int
    is_active: bool
    state_count: int = 0


class StateRead(ORMModel):
    id: int
    code: str
    name: str
    geopolitical_zone: str | None = None
    cohort_id: int | None = None
    cohort_code: str | None = None
    cohort_name: str | None = None
    piu_contact_email: str | None = None
    is_active: bool


class StateUpdate(BaseModel):
    cohort_code: CohortCode | None = None
    geopolitical_zone: str | None = None
    piu_contact_email: str | None = None
    is_active: bool | None = None


class IndicatorCategoryRead(ORMModel):
    id: int
    code: str
    name: str
    description: str | None = None
    sort_order: int
    indicator_count: int = 0


class IndicatorRead(ORMModel):
    id: int
    number: int
    code: str
    name: str
    definition: str | None = None
    category_id: int | None = None
    category_code: str | None = None
    category_name: str | None = None
    unit: str
    aggregation_method: str
    direction: str
    is_cumulative: bool
    requires_numerator_denominator: bool
    baseline_value: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    decimal_places: int
    disaggregations: list | None = None
    aliases: list | None = None
    is_core: bool
    is_active: bool


class IndicatorUpdate(BaseModel):
    name: str | None = None
    definition: str | None = None
    unit: IndicatorUnit | None = None
    aggregation_method: AggregationMethod | None = None
    direction: Direction | None = None
    is_cumulative: bool | None = None
    requires_numerator_denominator: bool | None = None
    baseline_value: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    decimal_places: int | None = Field(default=None, ge=0, le=6)
    aliases: list[str] | None = None
    is_core: bool | None = None
    is_active: bool | None = None


class PeriodRead(ORMModel):
    id: int
    code: str
    label: str
    period_type: str
    fiscal_year: int
    sequence: int
    start_date: date
    end_date: date
    due_date: date
    is_open: bool


class PeriodCreate(BaseModel):
    period_type: PeriodType
    fiscal_year: int = Field(ge=2000, le=2100)
    sequence: int = Field(ge=1, le=12)
    due_date: date | None = None
    is_open: bool = True


class PeriodGenerate(BaseModel):
    """Generate a full calendar of periods for a fiscal year."""

    fiscal_year: int = Field(ge=2000, le=2100)
    period_types: list[PeriodType] = Field(
        default_factory=lambda: list(PeriodType),
        description="Which period families to generate.",
    )
    due_days_after_close: int = Field(default=15, ge=0, le=120)


class TargetUpsert(BaseModel):
    indicator_code: str
    period_code: str
    level: TargetLevel = TargetLevel.STATE
    state_code: str | None = None
    target_value: float
    source: str | None = None
    notes: str | None = None


class TargetRead(ORMModel):
    id: int
    indicator_id: int
    indicator_code: str | None = None
    period_id: int
    period_code: str | None = None
    level: str
    state_id: int | None = None
    state_code: str | None = None
    target_value: float
    source: str | None = None


class TargetBulkUpsert(BaseModel):
    targets: list[TargetUpsert] = Field(min_length=1, max_length=20000)
