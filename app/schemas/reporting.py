"""Report generation schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.core.enums import ReportFormat, ReportKind, ReportScope
from app.schemas.common import ORMModel


class ReportRequest(BaseModel):
    period_code: str = Field(description="Reporting period, e.g. 2025-Q2.")
    kind: ReportKind = Field(
        default=ReportKind.PERFORMANCE,
        description=(
            "PERFORMANCE for the configurable platform report; VALIDATION for "
            "the quarterly Data Quality Validation Report; TECHNICAL for the "
            "Quarterly Technical Performance Report. The two NPCU quarterlies "
            "take the period and ignore the scope and section switches."
        ),
    )
    scope: ReportScope = ReportScope.NATIONAL
    scope_ref: str | None = Field(
        default=None,
        description="State code for STATE scope, cohort code for COHORT scope.",
    )
    indicator_codes: list[str] | None = Field(
        default=None, description="Restrict the report to specific KPIs."
    )
    category_codes: list[str] | None = None
    formats: list[ReportFormat] = Field(default_factory=lambda: [ReportFormat.MARKDOWN])
    #: The section on what the validation found and which returns can be
    #: relied on. Named for the DQA section it replaced; it no longer carries
    #: a score or a grade.
    include_data_quality: bool = True
    #: The section listing what remains unconfirmed. On by default: a report
    #: that prints a provisional total without saying so is the failure this
    #: section exists to prevent.
    include_queries: bool = True
    include_trends: bool = True
    include_narratives: bool = True
    include_state_tables: bool = True
    trend_periods: int = Field(default=6, ge=2, le=24)
    title: str | None = None


class ReportArtifact(BaseModel):
    format: str
    filename: str
    size_bytes: int
    download_url: str


class ReportResponse(BaseModel):
    report_id: int
    title: str
    scope: str
    scope_ref: str | None = None
    period_code: str
    generated_at: datetime
    indicator_count: int
    artifacts: list[ReportArtifact] = Field(default_factory=list)
    summary: str | None = None
    markdown: str | None = None


class GeneratedReportRead(ORMModel):
    id: int
    title: str
    scope: str
    scope_ref: str | None = None
    period_code: str
    period_type: str | None = None
    formats: list | None = None
    indicator_count: int
    generated_at: datetime | None = None
    summary: str | None = None
