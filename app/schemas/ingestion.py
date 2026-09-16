"""Ingestion and submission schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel


class ColumnMapping(BaseModel):
    """How one incoming header was resolved against the unified schema."""

    source_header: str
    mapped_field: str | None = None
    confidence: float = 0.0
    strategy: str = "unmapped"


class IngestionDiagnostics(BaseModel):
    template_profile: str
    detected_state: str | None = None
    detected_period: str | None = None
    sheet_name: str | None = None
    header_row: int | None = None
    total_rows: int = 0
    mapped_rows: int = 0
    unmapped_rows: int = 0
    column_mappings: list[ColumnMapping] = Field(default_factory=list)
    unmatched_indicators: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class IndicatorValueRead(ORMModel):
    id: int
    indicator_id: int
    indicator_code: str | None = None
    indicator_number: int | None = None
    indicator_name: str | None = None
    value: float | None = None
    numerator: float | None = None
    denominator: float | None = None
    raw_value: str | None = None
    disaggregation: dict | None = None
    data_source: str | None = None
    comment: str | None = None
    source_row: int | None = None
    is_valid: bool


class SubmissionRead(ORMModel):
    id: int
    state_id: int
    state_code: str | None = None
    state_name: str | None = None
    cohort_code: str | None = None
    period_id: int
    period_code: str | None = None
    period_label: str | None = None
    version: int
    status: str
    is_current: bool
    source_file_name: str | None = None
    file_hash: str | None = None
    row_count: int
    mapped_count: int
    unmapped_count: int
    dqa_score: float | None = None
    dqa_grade: str | None = None
    error_count: int
    warning_count: int
    uploaded_at: datetime | None = None
    approved_at: datetime | None = None
    notes: str | None = None
    rejection_reason: str | None = None


class SubmissionDetail(SubmissionRead):
    ingestion_report: dict | None = None
    values: list[IndicatorValueRead] = Field(default_factory=list)


class IngestionResult(BaseModel):
    submission: SubmissionRead
    diagnostics: IngestionDiagnostics
    validation: ValidationSummary
    accepted: bool
    message: str


class ManualValueEntry(BaseModel):
    indicator_code: str
    value: float | None = None
    numerator: float | None = None
    denominator: float | None = None
    disaggregation: dict[str, Any] | None = None
    data_source: str | None = None
    comment: str | None = None


class ManualSubmission(BaseModel):
    """Direct API submission, used by state systems that integrate with us."""

    state_code: str
    period_code: str
    values: list[ManualValueEntry] = Field(min_length=1, max_length=5000)
    notes: str | None = None
    auto_submit: bool = True


class SubmissionDecision(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


from app.schemas.validation import ValidationSummary  # noqa: E402

IngestionResult.model_rebuild()
