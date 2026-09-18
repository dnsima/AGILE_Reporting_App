"""Upload, review and approval endpoints for state reporting data."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import Response
from sqlalchemy import select

from app.api.deps import (
    CurrentPrincipal,
    DbSession,
    client_ip,
    enforce_state_scope,
    require,
    visible_state_codes,
)
from app.core.enums import PeriodType, Permission, Role, SubmissionStatus
from app.core.errors import NotFoundError, PermissionDeniedError, ValidationError
from app.models import Indicator, IndicatorValue, State, Submission
from app.schemas.common import Message
from app.schemas.ingestion import (
    IndicatorValueRead,
    IngestionResult,
    ManualSubmission,
    SubmissionDecision,
    SubmissionDetail,
    SubmissionRead,
)
from app.schemas.validation import ValidationSummary
from app.services import audit, reference
from app.services.ingestion import build_reporting_template, ingest_manual, ingest_upload
from app.services.ingestion.pipeline import (
    approve_submission,
    reject_submission,
    revalidate_submission,
)
from app.services.validation import summarise_submission

router = APIRouter(prefix="/ingestion", tags=["Data ingestion"])

#: Roles allowed to clear their own upload straight into the analysis pipeline.
SELF_APPROVING_ROLES = {Role.ADMIN, Role.NPCU, Role.ME_OFFICER}


def _submission_read(submission: Submission) -> SubmissionRead:
    state = submission.state
    return SubmissionRead(
        id=submission.id,
        state_id=submission.state_id,
        state_code=state.code if state else None,
        state_name=state.name if state else None,
        cohort_code=state.cohort.code if state and state.cohort else None,
        period_id=submission.period_id,
        period_code=submission.period.code if submission.period else None,
        period_label=submission.period.label if submission.period else None,
        version=submission.version,
        status=submission.status,
        is_current=submission.is_current,
        source_file_name=submission.source_file_name,
        file_hash=submission.file_hash,
        row_count=submission.row_count,
        mapped_count=submission.mapped_count,
        unmapped_count=submission.unmapped_count,
        dqa_score=submission.dqa_score,
        dqa_grade=submission.dqa_grade,
        error_count=submission.error_count,
        warning_count=submission.warning_count,
        open_query_count=submission.open_query_count,
        quarantined_count=submission.quarantined_count,
        uploaded_at=submission.uploaded_at,
        approved_at=submission.approved_at,
        notes=submission.notes,
        rejection_reason=submission.rejection_reason,
    )


def _indicator_attr(indicators: dict[int, Indicator], indicator_id: int | None, attribute: str):
    indicator = indicators.get(indicator_id)
    return getattr(indicator, attribute) if indicator else None


def _load_submission(db, submission_id: int, principal) -> Submission:
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise NotFoundError(f"Submission #{submission_id} does not exist")
    if principal.is_state_scoped and submission.state_id != principal.state_id:
        raise PermissionDeniedError("You may only access submissions for your own state.")
    return submission


# --------------------------------------------------------------------------
# Template
# --------------------------------------------------------------------------
@router.get(
    "/template",
    summary="Download the reporting template for one state and period",
    response_class=Response,
)
def download_template(
    db: DbSession,
    principal: CurrentPrincipal,
    state: str | None = Query(default=None, description="State the template is issued to"),
    period: str | None = Query(default=None, description="Reporting period to report against"),
) -> Response:
    """Issue the template a state fills in.

    The template is built per state and per period rather than handed out as one
    generic workbook: it carries the state's approved targets, locks the rows
    for sub-components that state does not implement, and shows what it already
    reported in the preceding periods. The period type picks the layout --
    monthly issues the performance tracker, anything else the results framework.
    """
    state_row = reference.get_state_by_code(db, state, required=False) if state else None
    if state_row is None and principal.is_state_scoped:
        state_row = db.get(State, principal.state_id)
    if state_row is None:
        raise ValidationError(
            "Name the state the template is for. Templates carry that state's targets and "
            "lock the sub-components it does not implement, so there is no generic one."
        )
    enforce_state_scope(db, principal, state_row.code)

    period_row = reference.get_period_by_code(db, period, required=False) if period else None
    if period_row is None:
        period_row = reference.latest_period(db, str(PeriodType.QUARTERLY))
    if period_row is None:
        raise ValidationError("No reporting period has been defined yet.")

    payload = build_reporting_template(db, state_row, period_row)
    stream = (
        "performance_tracker"
        if period_row.period_type == PeriodType.MONTHLY
        else "results_framework"
    )
    filename = f"AGILE_{stream}_{state_row.code}_{period_row.code}.xlsx"

    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------------
@router.post(
    "/upload",
    response_model=IngestionResult,
    status_code=201,
    summary="Upload a completed state reporting template",
)
async def upload(
    request: Request,
    db: DbSession,
    file: UploadFile = File(..., description="The completed .xlsx or .csv template"),
    state_code: str | None = Form(default=None),
    period_code: str | None = Form(default=None),
    notes: str | None = Form(default=None),
    auto_approve: bool = Form(default=False),
    allow_duplicate: bool = Form(default=False),
    principal=Depends(require(Permission.DATA_UPLOAD)),
) -> IngestionResult:
    if state_code:
        enforce_state_scope(db, principal, state_code)
    elif principal.is_state_scoped:
        own = db.get(State, principal.state_id)
        state_code = own.code if own else None

    content = await file.read()
    may_auto_approve = auto_approve and principal.role in SELF_APPROVING_ROLES

    submission, diagnostics, summary = ingest_upload(
        db,
        content=content,
        filename=file.filename or "upload.xlsx",
        state_code=state_code,
        period_code=period_code,
        actor=principal.user,
        auto_approve=may_auto_approve,
        allow_duplicate=allow_duplicate,
        notes=notes,
    )
    db.commit()
    db.refresh(submission)

    accepted = submission.status != SubmissionStatus.REJECTED
    if accepted:
        message = (
            f"Ingested {submission.mapped_count} value(s) for {submission.state.code}/"
            f"{submission.period.code}. DQA score {submission.dqa_score} ({submission.dqa_grade})."
        )
    else:
        message = (
            "Data was received but did not pass the quality gate, so it has not entered the "
            f"analysis pipeline. {submission.rejection_reason}"
        )

    return IngestionResult(
        submission=_submission_read(submission),
        diagnostics=diagnostics,
        validation=summary,
        accepted=accepted,
        message=message,
    )


@router.post(
    "/submissions",
    response_model=IngestionResult,
    status_code=201,
    summary="Submit reporting data as JSON (for state system integrations)",
)
def submit_json(
    payload: ManualSubmission,
    db: DbSession,
    auto_approve: bool = Query(default=False),
    principal=Depends(require(Permission.DATA_UPLOAD)),
) -> IngestionResult:
    enforce_state_scope(db, principal, payload.state_code) if principal.is_state_scoped else None

    submission, diagnostics, summary = ingest_manual(
        db,
        payload,
        actor=principal.user,
        auto_approve=auto_approve and principal.role in SELF_APPROVING_ROLES,
    )
    db.commit()
    db.refresh(submission)

    accepted = submission.status != SubmissionStatus.REJECTED
    return IngestionResult(
        submission=_submission_read(submission),
        diagnostics=diagnostics,
        validation=summary,
        accepted=accepted,
        message=(
            f"Accepted {submission.mapped_count} value(s)."
            if accepted
            else f"Rejected: {submission.rejection_reason}"
        ),
    )


# --------------------------------------------------------------------------
# Submission browsing
# --------------------------------------------------------------------------
@router.get("/submissions", response_model=list[SubmissionRead], summary="List submissions")
def list_submissions(
    db: DbSession,
    principal: CurrentPrincipal,
    state: str | None = None,
    period: str | None = None,
    status: SubmissionStatus | None = None,
    cohort: str | None = None,
    current_only: bool = False,
    limit: int = Query(default=100, le=1000),
    offset: int = 0,
) -> list[SubmissionRead]:
    stmt = select(Submission)
    if state:
        stmt = stmt.where(Submission.state_id == reference.get_state_by_code(db, state).id)
    if period:
        stmt = stmt.where(Submission.period_id == reference.get_period_by_code(db, period).id)
    if status:
        stmt = stmt.where(Submission.status == str(status))
    if current_only:
        stmt = stmt.where(Submission.is_current.is_(True))

    allowed = visible_state_codes(db, principal)
    if allowed is not None:
        allowed_ids = [reference.get_state_by_code(db, code).id for code in allowed]
        stmt = stmt.where(Submission.state_id.in_(allowed_ids or [0]))

    rows = list(db.scalars(stmt.order_by(Submission.uploaded_at.desc(), Submission.id.desc())))
    if cohort:
        wanted = cohort.strip().upper()
        rows = [
            row for row in rows if row.state and row.state.cohort and row.state.cohort.code.upper() == wanted
        ]
    return [_submission_read(row) for row in rows[offset : offset + limit]]


@router.get(
    "/submissions/{submission_id}",
    response_model=SubmissionDetail,
    summary="Inspect one submission and its values",
)
def get_submission(
    submission_id: int, db: DbSession, principal: CurrentPrincipal
) -> SubmissionDetail:
    submission = _load_submission(db, submission_id, principal)
    values = list(
        db.scalars(select(IndicatorValue).where(IndicatorValue.submission_id == submission.id))
    )
    indicators = {
        indicator.id: indicator
        for indicator in db.scalars(
            select(Indicator).where(Indicator.id.in_({v.indicator_id for v in values} or {0}))
        )
    }

    base = _submission_read(submission).model_dump()
    return SubmissionDetail(
        **base,
        ingestion_report=submission.ingestion_report,
        values=[
            IndicatorValueRead(
                id=value.id,
                indicator_id=value.indicator_id,
                indicator_code=_indicator_attr(indicators, value.indicator_id, "code"),
                indicator_number=_indicator_attr(indicators, value.indicator_id, "number"),
                indicator_name=_indicator_attr(indicators, value.indicator_id, "name"),
                value=value.value,
                numerator=value.numerator,
                denominator=value.denominator,
                raw_value=value.raw_value,
                disaggregation=value.disaggregation,
                data_source=value.data_source,
                comment=value.comment,
                source_row=value.source_row,
                is_valid=value.is_valid,
            )
            for value in sorted(
                values,
                key=lambda v: indicators[v.indicator_id].number if v.indicator_id in indicators else 0,
            )
        ],
    )


@router.get(
    "/submissions/{submission_id}/validation",
    response_model=ValidationSummary,
    summary="Validation findings and DQA scores for a submission",
)
def submission_validation(
    submission_id: int, db: DbSession, principal: CurrentPrincipal
) -> ValidationSummary:
    submission = _load_submission(db, submission_id, principal)
    return summarise_submission(db, submission)


# --------------------------------------------------------------------------
# Review decisions
# --------------------------------------------------------------------------
@router.post(
    "/submissions/{submission_id}/approve",
    response_model=SubmissionRead,
    summary="Clear a submission into the analysis pipeline",
)
def approve(
    submission_id: int,
    payload: SubmissionDecision,
    request: Request,
    db: DbSession,
    principal=Depends(require(Permission.DATA_APPROVE)),
) -> SubmissionRead:
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise NotFoundError(f"Submission #{submission_id} does not exist")
    approve_submission(db, submission, principal.user, payload.reason)
    db.commit()
    db.refresh(submission)
    return _submission_read(submission)


@router.post(
    "/submissions/{submission_id}/reject",
    response_model=SubmissionRead,
    summary="Reject a submission and keep it out of analysis",
)
def reject(
    submission_id: int,
    payload: SubmissionDecision,
    db: DbSession,
    principal=Depends(require(Permission.DATA_APPROVE)),
) -> SubmissionRead:
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise NotFoundError(f"Submission #{submission_id} does not exist")
    reject_submission(db, submission, principal.user, payload.reason or "No reason recorded")
    db.commit()
    db.refresh(submission)
    return _submission_read(submission)


@router.post(
    "/submissions/{submission_id}/revalidate",
    response_model=ValidationSummary,
    summary="Re-run validation, e.g. after a rule change",
)
def revalidate(
    submission_id: int,
    db: DbSession,
    principal=Depends(require(Permission.DATA_APPROVE)),
) -> ValidationSummary:
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise NotFoundError(f"Submission #{submission_id} does not exist")
    summary = revalidate_submission(db, submission, principal.user)
    db.commit()
    return summary


@router.delete(
    "/submissions/{submission_id}",
    response_model=Message,
    summary="Delete a submission (administrators only)",
)
def delete_submission(
    submission_id: int,
    request: Request,
    db: DbSession,
    principal=Depends(require(Permission.DATA_DELETE)),
) -> Message:
    submission = db.get(Submission, submission_id)
    if submission is None:
        raise NotFoundError(f"Submission #{submission_id} does not exist")

    audit.record(
        db,
        action="submission.delete",
        entity_type="submission",
        entity_id=submission.id,
        actor=principal.user,
        state_id=submission.state_id,
        period_id=submission.period_id,
        summary=f"Deleted submission #{submission.id} ({submission.state.code}/{submission.period.code}).",
        before={"status": submission.status, "dqa_score": submission.dqa_score},
        ip_address=client_ip(request),
    )
    db.delete(submission)
    db.commit()
    return Message(message=f"Submission #{submission_id} deleted.")
