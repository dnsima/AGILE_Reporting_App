"""Validation schemas: findings, fitness verdicts and reporting status.

No score and no grade. Both were removed: the score was a pass rate over
thousands of automated checks, so it sat near 100 for any plausible return,
and readers took the grade it produced as an answer to "can I use this?".
The fitness verdict answers that question directly, and scoring a return is
work for a data quality assessment with a field visit behind it.
"""

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


class DimensionFindings(BaseModel):
    """How many findings fall in one area. A count, never a score."""

    dimension: str
    findings: int = 0


class ValidationSummary(BaseModel):
    submission_id: int | None = None
    passed: bool
    blocking: bool
    error_count: int = 0
    warning_count: int = 0
    info_count: int = 0
    #: Figures fit for use, as a percentage of those reported. Every figure
    #: counts towards the national totals whatever its label; this says how
    #: much of the return a reader can lean on.
    usable_share_pct: float | None = None
    #: Findings per area -- integrity, accuracy, completeness and so on. A
    #: count of what needs looking at, making no claim about the whole.
    findings_by_dimension: dict[str, int] = Field(default_factory=dict)
    issues: list[ValidationIssueRead] = Field(default_factory=list)


class StateReturn(BaseModel):
    """How one state's return for one period stands.

    What replaced the DQA scorecard. The headline is the fitness verdict, not
    a number: a state that scored "Excellent" while reporting 127 schools
    against the 5,960 it reported the quarter before is the reason.
    """

    state_code: str
    state_name: str
    cohort_code: str | None = None
    period_code: str
    submission_id: int | None = None
    status: str | None = None
    submitted_at: datetime | None = None
    days_late: int | None = None
    figures_reported: int = 0
    #: Figures with no open finding against them. Every figure counts towards
    #: the national totals regardless; this says how many are unencumbered.
    figures_counting: int = 0
    usable_share_pct: float | None = None
    #: Whether the return can be relied on: FIT, FIT WITH NOTES, NOT FIT FOR
    #: USE, NO DATA. This is the headline a reader should act on.
    fitness_verdict: str | None = None
    #: Why the verdict is what it is. An unexplained verdict is worthless.
    verdict_note: str | None = None
    #: Share of this state's contribution to the national totals that rests on
    #: figures the validation could not vouch for.
    exposed_share_pct: float | None = None
    findings_by_dimension: dict[str, int] = Field(default_factory=dict)
    error_count: int = 0
    warning_count: int = 0
    top_issues: list[ValidationIssueRead] = Field(default_factory=list)


class NationalReturns(BaseModel):
    """How the period's returns stand nationally."""

    period_code: str
    states_expected: int
    states_reported: int
    states_approved: int
    reporting_rate_pct: float
    on_time_rate_pct: float
    figures_reported: int = 0
    figures_counting: int = 0
    usable_share_pct: float | None = None
    #: How many of the assessed states cannot be relied on for this period.
    #: This is the national headline, in place of an average score.
    states_not_fit: int = 0
    states_fit_with_notes: int = 0
    states_fit: int = 0
    findings_by_dimension: dict[str, int] = Field(default_factory=dict)
    cohort_returns: list[CohortReturns] = Field(default_factory=list)
    returns: list[StateReturn] = Field(default_factory=list)
    common_issues: list[dict] = Field(default_factory=list)


class CohortReturns(BaseModel):
    cohort_code: str
    cohort_name: str
    states_expected: int
    states_reported: int
    reporting_rate_pct: float
    on_time_rate_pct: float
    states_not_fit: int = 0
    usable_share_pct: float | None = None


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


NationalReturns.model_rebuild()
