"""Schemas for reconciling the monthly tracker against the quarterly framework."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ReconciliationLineRead(BaseModel):
    """One indicator, judged across both reporting streams."""

    indicator_id: int
    indicator_code: str
    indicator_name: str
    subcomponent_code: str | None = None
    unit: str
    #: How the coarser figure is built from the finer ones for this indicator.
    basis: str
    #: The figure from the coarser stream, e.g. the quarterly framework return.
    coarse_value: float | None = None
    #: What the finer stream implies it should be, folded on the basis above.
    fine_value: float | None = None
    variance: float | None = None
    variance_pct: float | None = None
    status: str
    #: What was expected and why, in words the reporting state can act on.
    note: str
    parts_expected: list[str] = Field(default_factory=list)
    parts_reported: list[str] = Field(default_factory=list)
    part_values: dict[str, float] = Field(default_factory=dict)
    #: The figure carries an open finding; it still counts.
    is_flagged: bool = False
    #: Set on a mismatch that would reconcile exactly under another basis: the
    #: figure may be right and the indicator's time basis wrong.
    reconciles_as: str | None = None


class StateReconciliationRead(BaseModel):
    """Every line for one state and one period, with its headline counts."""

    state_id: int
    state_code: str
    state_name: str
    cohort_code: str | None = None
    period_code: str
    period_label: str
    parts_expected: list[str] = Field(default_factory=list)
    parts_reported: list[str] = Field(default_factory=list)
    tracker_is_complete: bool = False
    coarse_submission_id: int | None = None
    fine_submission_ids: list[int] = Field(default_factory=list)
    judged: int = 0
    matched: int = 0
    mismatched: int = 0
    tracker_missing: int = 0
    framework_missing: int = 0
    incomplete: int = 0
    #: Share of judged lines where the two streams agree.
    agreement: float | None = None
    lines: list[ReconciliationLineRead] = Field(default_factory=list)


class ReconciliationSummary(BaseModel):
    """Headline reconciliation counts across states."""

    period_code: str
    period_label: str
    child_period_type: str | None = None
    states: int = 0
    states_reporting: int = 0
    #: States that have filed at least one of the finer returns. Agreement is
    #: computed over these alone.
    states_tracking: int = 0
    states_with_complete_tracker: int = 0
    lines: int = 0
    judged: int = 0
    matched: int = 0
    agreement: float | None = None
    by_status: dict[str, int] = Field(default_factory=dict)
    states_unreconciled: list[str] = Field(default_factory=list)


class ReconciliationReport(BaseModel):
    summary: ReconciliationSummary
    states: list[StateReconciliationRead] = Field(default_factory=list)
