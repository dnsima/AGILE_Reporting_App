"""Routine/periodic report generation and download."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.api.deps import DbSession, enforce_state_scope, require
from app.core.config import settings
from app.core.enums import Permission, ReportFormat, ReportScope
from app.core.errors import NotFoundError, ValidationError
from app.models import GeneratedReport
from app.schemas.reporting import (
    GeneratedReportRead,
    ReportRequest,
    ReportResponse,
)
from app.services import analysis_model, reference, reporting
from app.services.reporting import workbook

router = APIRouter(prefix="/reports", tags=["Reporting"])

MEDIA_TYPES = {
    "markdown": "text/markdown; charset=utf-8",
    "html": "text/html; charset=utf-8",
    "pdf": "application/pdf",
    "docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
}


@router.post(
    "",
    response_model=ReportResponse,
    status_code=201,
    summary="Generate a routine report for a period and scope",
    description=(
        "Generates a monthly, quarterly, semi-annual or annual report at national, state or "
        "cohort scope, in any combination of Markdown, HTML and PDF."
    ),
)
def generate(
    payload: ReportRequest,
    db: DbSession,
    principal=Depends(require(Permission.REPORTS_GENERATE)),
) -> ReportResponse:
    if payload.scope == ReportScope.STATE:
        enforce_state_scope(db, principal, payload.scope_ref)
    elif principal.is_state_scoped:
        raise ValidationError(
            "State PIU accounts may only generate state-scoped reports for their own state."
        )

    response = reporting.generate_report(db, payload, principal.user)
    db.commit()
    return response


@router.get(
    "/analysis-workbook",
    summary="Download the quarterly analysis model as a workbook",
    dependencies=[Depends(require(Permission.ANALYTICS_READ))],
    response_class=FileResponse,
)
def analysis_workbook(
    db: DbSession,
    period: str | None = None,
) -> FileResponse:
    """The analysis model as the workbook the NPCU already works in.

    Built from the same payload the dashboard draws, so the sheet a reader
    downloads cannot disagree with the screen they downloaded it from. This
    is the artefact that used to be assembled by hand each quarter.
    """
    resolved = (
        reference.get_period_by_code(db, period) if period else reference.latest_period(db)
    )
    if resolved is None:
        raise NotFoundError("No reporting periods have been configured yet")

    model = analysis_model.build(db, resolved.code)
    path = workbook.write(model, Path(settings.report_dir) / "workbooks")
    return FileResponse(
        path,
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        filename=path.name,
    )


@router.get(
    "",
    response_model=list[GeneratedReportRead],
    summary="List previously generated reports",
    dependencies=[Depends(require(Permission.REPORTS_GENERATE))],
)
def list_reports(
    db: DbSession,
    period: str | None = None,
    scope: ReportScope | None = None,
    limit: int = Query(default=50, le=500),
) -> list[GeneratedReportRead]:
    stmt = select(GeneratedReport).order_by(GeneratedReport.id.desc())
    if period:
        stmt = stmt.where(GeneratedReport.period_code == period.upper())
    if scope:
        stmt = stmt.where(GeneratedReport.scope == str(scope))
    return [GeneratedReportRead.model_validate(row) for row in db.scalars(stmt.limit(limit))]


@router.get(
    "/{report_id}/download",
    summary="Download a generated report artifact",
    dependencies=[Depends(require(Permission.REPORTS_GENERATE))],
    response_class=FileResponse,
)
def download(
    report_id: int,
    db: DbSession,
    format: ReportFormat = Query(default=ReportFormat.MARKDOWN),
) -> FileResponse:
    report = db.get(GeneratedReport, report_id)
    if report is None:
        raise NotFoundError(f"Report #{report_id} does not exist")

    path_str = (report.file_paths or {}).get(str(format))
    if not path_str:
        raise NotFoundError(
            f"Report #{report_id} was not rendered as {format}. "
            f"Available: {', '.join(report.file_paths or {}) or 'none'}."
        )

    path = Path(path_str)
    if not path.exists():
        raise NotFoundError(f"The stored file for report #{report_id} is no longer on disk.")

    return FileResponse(
        path,
        media_type=MEDIA_TYPES.get(str(format), "application/octet-stream"),
        filename=path.name,
    )
