"""Schemas for the data query workflow."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator


class EvidenceRead(BaseModel):
    id: int
    filename: str
    content_type: str | None = None
    size_bytes: int = 0
    #: SHA-256, so an evidence file can be shown not to have changed since it
    #: was submitted.
    content_hash: str
    description: str | None = None
    uploaded_by: str | None = None
    uploaded_at: datetime | None = None


class QueryResponseRead(BaseModel):
    id: int
    query_id: int
    responder: str | None = None
    submitted_at: datetime | None = None
    narrative: str
    evidence_summary: str | None = None
    #: NULL means "the figure stands as reported"; a value proposes a correction.
    proposed_value: float | None = None
    restates_period_code: str | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    review_outcome: str | None = None
    review_note: str | None = None
    evidence: list[EvidenceRead] = Field(default_factory=list)


class QueryRead(BaseModel):
    """One flagged figure, as it appears in a worklist."""

    id: int
    reference: str
    submission_id: int | None = None
    state_id: int
    state_code: str
    state_name: str
    cohort_code: str | None = None
    period_id: int
    period_code: str
    period_label: str
    indicator_id: int | None = None
    indicator_code: str | None = None
    indicator_name: str | None = None
    rule_code: str | None = None
    dimension: str | None = None
    severity: str | None = None
    title: str
    detail: str | None = None
    reported_value: float | None = None
    #: The figure as it currently stands, which differs from ``reported_value``
    #: once a restatement has been accepted.
    current_value: float | None = None
    #: True while the figure is held out of every aggregation.
    is_quarantined: bool = False
    status: str
    resolution: str | None = None
    resolution_note: str | None = None
    due_date: date | None = None
    is_open: bool = True
    is_overdue: bool = False
    days_overdue: int = 0
    verification_required: bool = False
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    response_count: int = 0
    #: What the state last proposed, so a reviewer sees it without opening the query.
    latest_proposed_value: float | None = None


class QueryDetail(QueryRead):
    responses: list[QueryResponseRead] = Field(default_factory=list)
    #: Other open queries against the same figure. Two rules can flag one
    #: figure, and settling this query does not settle the others -- so the
    #: figure stays out of the aggregations until every one of them is closed.
    held_by: list[str] = Field(default_factory=list)


class RespondRequest(BaseModel):
    """A state's answer: evidence, and a figure confirmed or corrected."""

    narrative: str = Field(min_length=1, max_length=4000)
    #: Omit to confirm the figure as reported; supply one to propose a correction.
    proposed_value: float | None = None
    evidence_summary: str | None = Field(default=None, max_length=2000)
    #: The period the correction applies to, when it is not the one queried. A
    #: cumulative figure that appears to fall is usually put right by restating
    #: the earlier period.
    restates_period_code: str | None = None

    @field_validator("narrative")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A response must explain the figure and cite its evidence.")
        return value


class ReviewRequest(BaseModel):
    note: str | None = Field(default=None, max_length=2000)


class ReasonRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class QuerySummaryRead(BaseModel):
    period_code: str
    period_label: str
    total: int = 0
    open: int = 0
    overdue: int = 0
    awaiting_review: int = 0
    for_verification: int = 0
    resolved: int = 0
    restated: int = 0
    states_with_open_queries: int = 0


class CorrectionUploadResult(BaseModel):
    applied: int = 0
    skipped: int = 0
    response_ids: list[int] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    message: str
