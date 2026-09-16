"""The ingestion pipeline.

    upload -> parse -> auto-map -> version -> persist -> validate -> gate

The gate is the important part: a submission that fails a blocking rule, or
scores below the configured DQA minimum, is left in ``REJECTED`` status with
``is_current`` cleared, so the analysis engine never reads it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import SubmissionStatus
from app.core.errors import ConflictError, IngestionError, ValidationError
from app.core.events import event_bus
from app.core.logging_config import get_logger
from app.core.security import file_digest
from app.models import Indicator, IndicatorValue, ReportingPeriod, State, Submission, User
from app.schemas.ingestion import (
    ColumnMapping,
    IngestionDiagnostics,
    ManualSubmission,
)
from app.schemas.validation import ValidationSummary
from app.services import audit, reference
from app.services.ingestion.mapper import MappedValue, map_rows
from app.services.ingestion.parser import parse_upload
from app.services.validation import run_validation

logger = get_logger(__name__)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _next_version(db: Session, state_id: int, period_id: int) -> int:
    highest = db.scalar(
        select(func.max(Submission.version)).where(
            Submission.state_id == state_id, Submission.period_id == period_id
        )
    )
    return (highest or 0) + 1


def _supersede_previous(db: Session, state_id: int, period_id: int, keep_id: int | None) -> int:
    """Retire earlier current submissions so exactly one stays authoritative."""
    stmt = select(Submission).where(
        Submission.state_id == state_id,
        Submission.period_id == period_id,
        Submission.is_current.is_(True),
    )
    if keep_id is not None:
        stmt = stmt.where(Submission.id != keep_id)
    retired = 0
    for previous in db.scalars(stmt):
        previous.is_current = False
        if previous.status == SubmissionStatus.APPROVED:
            previous.status = str(SubmissionStatus.SUPERSEDED)
        retired += 1
    db.flush()
    return retired


def _store_file(content: bytes, state: State, period: ReportingPeriod, version: int, filename: str) -> str:
    suffix = Path(filename).suffix or ".dat"
    target_dir = Path(settings.upload_dir) / state.code / period.code
    target_dir.mkdir(parents=True, exist_ok=True)
    destination = target_dir / f"v{version}_{file_digest(content)[:12]}{suffix}"
    destination.write_bytes(content)
    return str(destination)


def _persist_values(
    db: Session, submission: Submission, values: list[MappedValue]
) -> None:
    for mapped in values:
        db.add(
            IndicatorValue(
                submission_id=submission.id,
                indicator_id=mapped.indicator.id,
                value=mapped.value,
                numerator=mapped.numerator,
                denominator=mapped.denominator,
                raw_value=mapped.raw_value,
                disaggregation=mapped.disaggregation or {},
                data_source=mapped.data_source,
                comment=mapped.comment,
                source_row=mapped.source_row,
            )
        )
    db.flush()


def _finalise(
    db: Session,
    submission: Submission,
    actor: User | None,
    auto_approve: bool,
) -> ValidationSummary:
    """Validate, then either admit the submission or keep it out of the pipeline."""
    summary = run_validation(db, submission)

    if submission.status == SubmissionStatus.REJECTED:
        submission.is_current = False
    elif auto_approve:
        submission.status = str(SubmissionStatus.APPROVED)
        submission.approved_by_id = getattr(actor, "id", None)
        submission.approved_at = datetime.now(timezone.utc)
    db.flush()
    return summary


def _diagnostics_dict(diagnostics: IngestionDiagnostics) -> dict:
    return diagnostics.model_dump(mode="json")


# --------------------------------------------------------------------------
# File upload
# --------------------------------------------------------------------------
def ingest_upload(
    db: Session,
    *,
    content: bytes,
    filename: str,
    state_code: str | None = None,
    period_code: str | None = None,
    actor: User | None = None,
    auto_approve: bool = False,
    allow_duplicate: bool = False,
    notes: str | None = None,
) -> tuple[Submission, IngestionDiagnostics, ValidationSummary]:
    """Ingest an uploaded state reporting template."""
    if len(content) > settings.max_upload_bytes:
        raise IngestionError(
            f"File is larger than the {settings.max_upload_mb} MB upload limit."
        )

    sheet = parse_upload(content, filename)
    indicators = list(db.scalars(select(Indicator).where(Indicator.is_active.is_(True))))
    if not indicators:
        raise IngestionError(
            "No indicators are configured. Seed the indicator catalogue before ingesting data."
        )

    mapping = map_rows(sheet.headers, sheet.rows, indicators)

    # Explicit parameters win; fall back to whatever the file told us.
    state = reference.get_state_by_code(
        db, state_code or mapping.detected_state or sheet.detected_state or "", required=False
    )
    if state is None:
        raise ValidationError(
            "Could not determine which state this file belongs to. "
            "Pass state_code with the upload."
        )

    resolved_period_code = period_code or mapping.detected_period or sheet.detected_period or ""
    period = reference.get_period_by_code(db, resolved_period_code, required=False)
    if period is None:
        raise ValidationError(
            "Could not determine the reporting period for this file. "
            "Pass period_code with the upload."
        )

    digest = file_digest(content)
    if not allow_duplicate:
        duplicate = db.scalar(
            select(Submission).where(
                Submission.file_hash == digest,
                Submission.state_id == state.id,
                Submission.period_id == period.id,
            )
        )
        if duplicate is not None:
            raise ConflictError(
                f"This exact file was already submitted for {state.code}/{period.code} "
                f"as submission #{duplicate.id}.",
                details={"submission_id": duplicate.id},
            )

    if not mapping.values:
        raise IngestionError(
            "No rows in the file could be matched to a known indicator.",
            details={
                "unmatched_examples": mapping.unmatched_indicators[:10],
                "headers": sheet.headers,
            },
        )

    version = _next_version(db, state.id, period.id)
    stored_path = _store_file(content, state, period, version, filename)

    submission = Submission(
        state_id=state.id,
        period_id=period.id,
        version=version,
        status=str(SubmissionStatus.UPLOADED),
        is_current=True,
        source_file_name=filename,
        stored_file_path=stored_path,
        file_hash=digest,
        template_profile=mapping.layout,
        row_count=sheet.row_count,
        mapped_count=mapping.mapped_rows,
        unmapped_count=mapping.unmapped_rows,
        uploaded_by_id=getattr(actor, "id", None),
        uploaded_at=datetime.now(timezone.utc),
        notes=notes,
    )
    db.add(submission)
    db.flush()

    _supersede_previous(db, state.id, period.id, keep_id=submission.id)
    _persist_values(db, submission, mapping.values)

    diagnostics = IngestionDiagnostics(
        template_profile=mapping.layout,
        detected_state=state.code,
        detected_period=period.code,
        sheet_name=sheet.sheet_name,
        header_row=sheet.header_row,
        total_rows=sheet.row_count,
        mapped_rows=mapping.mapped_rows,
        unmapped_rows=mapping.unmapped_rows,
        column_mappings=mapping.column_mappings,
        unmatched_indicators=sorted(set(mapping.unmatched_indicators))[:50],
        warnings=sheet.warnings + mapping.warnings[:50],
    )
    submission.ingestion_report = _diagnostics_dict(diagnostics)
    db.flush()

    summary = _finalise(db, submission, actor, auto_approve)

    audit.record(
        db,
        action="submission.ingest",
        entity_type="submission",
        entity_id=submission.id,
        actor=actor,
        state_id=state.id,
        period_id=period.id,
        summary=(
            f"Ingested {filename} for {state.code}/{period.code} (v{version}): "
            f"{mapping.mapped_rows} rows mapped, DQA {submission.dqa_score}, "
            f"status {submission.status}."
        ),
        after={
            "status": submission.status,
            "dqa_score": submission.dqa_score,
            "errors": summary.error_count,
            "warnings": summary.warning_count,
            "file_hash": digest,
        },
    )

    event_bus.publish(
        "submission.ingested",
        {
            "submission_id": submission.id,
            "state": state.code,
            "cohort": state.cohort.code if state.cohort else None,
            "period": period.code,
            "status": submission.status,
            "dqa_score": submission.dqa_score,
        },
    )
    return submission, diagnostics, summary


# --------------------------------------------------------------------------
# Direct API submission
# --------------------------------------------------------------------------
def ingest_manual(
    db: Session,
    payload: ManualSubmission,
    actor: User | None = None,
    *,
    auto_approve: bool = False,
) -> tuple[Submission, IngestionDiagnostics, ValidationSummary]:
    """Ingest a JSON submission from a state system integrating over the API."""
    state = reference.get_state_by_code(db, payload.state_code)
    period = reference.get_period_by_code(db, payload.period_code)

    indicators = {
        indicator.code.upper(): indicator
        for indicator in db.scalars(select(Indicator).where(Indicator.is_active.is_(True)))
    }

    mapped: list[MappedValue] = []
    unmatched: list[str] = []
    for index, entry in enumerate(payload.values, start=1):
        indicator = indicators.get(entry.indicator_code.strip().upper())
        if indicator is None:
            indicator = reference.get_indicator_by_code(db, entry.indicator_code, required=False)
        if indicator is None:
            unmatched.append(entry.indicator_code)
            continue
        mapped.append(
            MappedValue(
                indicator=indicator,
                value=entry.value,
                numerator=entry.numerator,
                denominator=entry.denominator,
                raw_value=None if entry.value is None else f"{entry.value:g}",
                disaggregation={
                    key: str(value) for key, value in (entry.disaggregation or {}).items()
                },
                data_source=entry.data_source,
                comment=entry.comment,
                source_row=index,
            )
        )

    if not mapped:
        raise IngestionError(
            "None of the supplied indicator codes are recognised.",
            details={"unmatched": unmatched[:20]},
        )

    version = _next_version(db, state.id, period.id)
    submission = Submission(
        state_id=state.id,
        period_id=period.id,
        version=version,
        status=str(SubmissionStatus.UPLOADED if payload.auto_submit else SubmissionStatus.DRAFT),
        is_current=True,
        source_file_name="api-submission.json",
        template_profile="api",
        row_count=len(payload.values),
        mapped_count=len(mapped),
        unmapped_count=len(unmatched),
        uploaded_by_id=getattr(actor, "id", None),
        uploaded_at=datetime.now(timezone.utc),
        notes=payload.notes,
    )
    db.add(submission)
    db.flush()

    _supersede_previous(db, state.id, period.id, keep_id=submission.id)
    _persist_values(db, submission, mapped)

    diagnostics = IngestionDiagnostics(
        template_profile="api",
        detected_state=state.code,
        detected_period=period.code,
        total_rows=len(payload.values),
        mapped_rows=len(mapped),
        unmapped_rows=len(unmatched),
        column_mappings=[
            ColumnMapping(
                source_header="indicator_code", mapped_field="indicator_code",
                confidence=1.0, strategy="api",
            )
        ],
        unmatched_indicators=unmatched[:50],
    )
    submission.ingestion_report = _diagnostics_dict(diagnostics)
    db.flush()

    summary = _finalise(db, submission, actor, auto_approve)

    audit.record(
        db,
        action="submission.ingest_api",
        entity_type="submission",
        entity_id=submission.id,
        actor=actor,
        state_id=state.id,
        period_id=period.id,
        summary=(
            f"API submission for {state.code}/{period.code} (v{version}): "
            f"{len(mapped)} values, status {submission.status}."
        ),
        after={"status": submission.status, "dqa_score": submission.dqa_score},
    )
    event_bus.publish(
        "submission.ingested",
        {
            "submission_id": submission.id,
            "state": state.code,
            "period": period.code,
            "status": submission.status,
            "dqa_score": submission.dqa_score,
        },
    )
    return submission, diagnostics, summary


# --------------------------------------------------------------------------
# Review decisions
# --------------------------------------------------------------------------
def approve_submission(
    db: Session, submission: Submission, actor: User | None, reason: str | None = None
) -> Submission:
    if submission.status == SubmissionStatus.REJECTED:
        raise ConflictError(
            "This submission failed validation and cannot be approved. "
            "Correct the data and upload a new version.",
            details={"rejection_reason": submission.rejection_reason},
        )
    if (submission.dqa_score or 0) < settings.dqa_minimum_score:
        raise ConflictError(
            f"DQA score {submission.dqa_score} is below the "
            f"{settings.dqa_minimum_score:.0f} minimum required for approval."
        )

    before = {"status": submission.status, "is_current": submission.is_current}
    _supersede_previous(db, submission.state_id, submission.period_id, keep_id=submission.id)
    submission.status = str(SubmissionStatus.APPROVED)
    submission.is_current = True
    submission.approved_by_id = getattr(actor, "id", None)
    submission.approved_at = datetime.now(timezone.utc)
    submission.rejection_reason = None
    if reason:
        submission.notes = f"{submission.notes}\n{reason}" if submission.notes else reason
    db.flush()

    audit.record(
        db,
        action="submission.approve",
        entity_type="submission",
        entity_id=submission.id,
        actor=actor,
        state_id=submission.state_id,
        period_id=submission.period_id,
        summary=f"Approved submission #{submission.id}.",
        before=before,
        after={"status": submission.status, "is_current": True},
    )
    event_bus.publish(
        "submission.approved",
        {"submission_id": submission.id, "state_id": submission.state_id},
    )
    return submission


def reject_submission(
    db: Session, submission: Submission, actor: User | None, reason: str
) -> Submission:
    before = {"status": submission.status, "is_current": submission.is_current}
    submission.status = str(SubmissionStatus.REJECTED)
    submission.is_current = False
    submission.rejection_reason = reason
    db.flush()

    audit.record(
        db,
        action="submission.reject",
        entity_type="submission",
        entity_id=submission.id,
        actor=actor,
        state_id=submission.state_id,
        period_id=submission.period_id,
        summary=f"Rejected submission #{submission.id}: {reason}",
        before=before,
        after={"status": submission.status, "is_current": False},
    )
    event_bus.publish(
        "submission.rejected",
        {"submission_id": submission.id, "state_id": submission.state_id},
    )
    return submission


def revalidate_submission(db: Session, submission: Submission, actor: User | None) -> ValidationSummary:
    """Re-run validation, e.g. after a rule threshold changed."""
    summary = run_validation(db, submission)
    audit.record(
        db,
        action="submission.revalidate",
        entity_type="submission",
        entity_id=submission.id,
        actor=actor,
        state_id=submission.state_id,
        period_id=submission.period_id,
        summary=f"Re-validated submission #{submission.id}: DQA {summary.overall_score}.",
        after={"dqa_score": summary.overall_score, "status": submission.status},
    )
    event_bus.publish("submission.revalidated", {"submission_id": submission.id})
    return summary
