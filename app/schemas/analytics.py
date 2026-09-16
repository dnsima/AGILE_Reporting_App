"""KPI analysis, cohort analytics and dashboard schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field


class IndicatorRef(BaseModel):
    id: int
    number: int
    code: str
    name: str
    unit: str
    direction: str
    category_code: str | None = None
    category_name: str | None = None


class StatePerformance(BaseModel):
    """Layer 1: state-level performance against state-level targets."""

    state_code: str
    state_name: str
    cohort_code: str | None = None
    value: float | None = None
    numerator: float | None = None
    denominator: float | None = None
    target: float | None = None
    achievement_pct: float | None = None
    variance: float | None = None
    status: str = "No data"
    contribution_pct: float | None = None
    reported: bool = False


class NationalPerformance(BaseModel):
    """Layer 2: national roll-up against the national target."""

    value: float | None = None
    numerator: float | None = None
    denominator: float | None = None
    target: float | None = None
    achievement_pct: float | None = None
    variance: float | None = None
    status: str = "No data"
    aggregation_method: str
    states_reporting: int = 0
    states_expected: int = 0


class CohortPerformance(BaseModel):
    cohort_code: str
    cohort_name: str
    value: float | None = None
    target: float | None = None
    achievement_pct: float | None = None
    status: str = "No data"
    contribution_pct: float | None = Field(
        default=None, description="Layer 3: share of the national achievement."
    )
    states_reporting: int = 0
    states_expected: int = 0
    average_state_achievement_pct: float | None = None


class IndicatorAnalysis(BaseModel):
    """The full three-layer analysis for one indicator in one period."""

    indicator: IndicatorRef
    period_code: str
    national: NationalPerformance
    cohorts: list[CohortPerformance] = Field(default_factory=list)
    states: list[StatePerformance] = Field(default_factory=list)


class TrendPoint(BaseModel):
    period_code: str
    period_label: str
    fiscal_year: int
    sequence: int
    value: float | None = None
    target: float | None = None
    achievement_pct: float | None = None
    states_reporting: int = 0


class TrendSeries(BaseModel):
    """Longitudinal series for one indicator at one scope."""

    indicator: IndicatorRef
    scope: str
    scope_ref: str | None = None
    scope_label: str | None = None
    points: list[TrendPoint] = Field(default_factory=list)
    change_pct: float | None = None
    direction_of_travel: str = "flat"


class ScorecardRow(BaseModel):
    indicator: IndicatorRef
    value: float | None = None
    target: float | None = None
    achievement_pct: float | None = None
    status: str = "No data"


class PerformanceScorecard(BaseModel):
    """Cross-sectional view: every indicator for one scope and period."""

    scope: str
    scope_ref: str | None = None
    scope_label: str | None = None
    period_code: str
    rows: list[ScorecardRow] = Field(default_factory=list)
    indicators_with_data: int = 0
    indicators_with_target: int = 0
    indicators_on_track: int = 0
    average_achievement_pct: float | None = None


class ContributionRow(BaseModel):
    state_code: str
    state_name: str
    cohort_code: str | None = None
    value: float | None = None
    contribution_pct: float | None = None
    rank: int | None = None


class ContributionAnalysis(BaseModel):
    indicator: IndicatorRef
    period_code: str
    national_value: float | None = None
    rows: list[ContributionRow] = Field(default_factory=list)


class CohortComparison(BaseModel):
    """Within- and across-cohort comparison for one indicator/period."""

    period_code: str
    indicator: IndicatorRef | None = None
    cohorts: list[CohortSummary] = Field(default_factory=list)


class CohortSummary(BaseModel):
    cohort_code: str
    cohort_name: str
    states_expected: int
    states_reporting: int
    reporting_rate_pct: float
    on_time_rate_pct: float
    completeness_pct: float
    average_dqa_score: float | None = None
    dqa_grade: str = "No data"
    average_achievement_pct: float | None = None
    indicators_on_track: int = 0
    indicators_assessed: int = 0
    contribution_pct: float | None = None
    best_state: str | None = None
    weakest_state: str | None = None


class HeatmapCell(BaseModel):
    row_key: str
    column_key: str
    value: float | None = None
    label: str | None = None


class Heatmap(BaseModel):
    metric: str
    rows: list[str]
    row_labels: dict[str, str] = Field(default_factory=dict)
    columns: list[str]
    column_labels: dict[str, str] = Field(default_factory=dict)
    cells: list[HeatmapCell] = Field(default_factory=list)


class DashboardKpiTile(BaseModel):
    key: str
    label: str
    value: float | None = None
    unit: str | None = None
    delta_pct: float | None = None
    caption: str | None = None
    status: str | None = None


class DashboardOverview(BaseModel):
    period_code: str
    period_label: str
    generated_at: str
    data_version: int
    tiles: list[DashboardKpiTile] = Field(default_factory=list)
    cohorts: list[CohortSummary] = Field(default_factory=list)
    top_states: list[ScorecardRow | ContributionRow] = Field(default_factory=list)
    reporting_status: dict = Field(default_factory=dict)
    dqa_summary: dict = Field(default_factory=dict)


CohortComparison.model_rebuild()
