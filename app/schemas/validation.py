"""Validation and DQA schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class ValidationIssueRead(ORMModel):
    id: int | None = None
    rule_code: str
    dimension: str
    severity: str
    indicator_id: int | None = None
    indicator_code: str | None = None
    field: str | None = None
    message: str
    observed: str | None = None
    expected: str | None = None
    source_row: int | None = None
    is_blocking: bool = False


class DimensionScore(BaseModel):
    dimension: str
    score: float
    weight: float
    checks_run: int = 0
    checks_failed: int = 0
    grade: str | None = None
    details: dict = Field(default_factory=dict)


class ValidationSummary(BaseModel):
    submission_id: int | None = None
    passed: bool
    blocking: bool
    error_count: int = 0
    warning_count: int = 0
    info_count: int = 0
    overall_score: float = 0.0
    grade: str = "No data"
    dimensions: list[DimensionScore] = Field(default_factory=list)
    issues: list[ValidationIssueRead] = Field(default_factory=list)


class DQAScorecard(BaseModel):
    """State-level DQA scorecard for one reporting period."""

    state_code: str
    state_name: str
    cohort_code: str | None = None
    period_code: str
    submission_id: int | None = None
    status: str | None = None
    submitted_at: datetime | None = None
    days_late: int | None = None
    overall_score: float | None = None
    grade: str = "No data"
    dimensions: list[DimensionScore] = Field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    top_issues: list[ValidationIssueRead] = Field(default_factory=list)


class NationalDQASummary(BaseModel):
    period_code: str
    states_expected: int
    states_reported: int
    states_approved: int
    reporting_rate_pct: float
    on_time_rate_pct: float
    national_score: float | None = None
    grade: str = "No data"
    dimension_averages: list[DimensionScore] = Field(default_factory=list)
    cohort_scores: list[CohortDQASummary] = Field(default_factory=list)
    scorecards: list[DQAScorecard] = Field(default_factory=list)
    common_issues: list[dict] = Field(default_factory=list)


class CohortDQASummary(BaseModel):
    cohort_code: str
    cohort_name: str
    states_expected: int
    states_reported: int
    reporting_rate_pct: float
    on_time_rate_pct: float
    average_score: float | None = None
    grade: str = "No data"
    dimension_averages: list[DimensionScore] = Field(default_factory=list)


class ValidationRuleRead(ORMModel):
    id: int
    code: str
    name: str
    description: str | None = None
    dimension: str
    severity: str
    config: dict | None = None
    is_blocking: bool
    is_active: bool


class ValidationRuleUpdate(BaseModel):
    severity: str | None = None
    config: dict | None = None
    is_blocking: bool | None = None
    is_active: bool | None = None


NationalDQASummary.model_rebuild()
