"""The data query workflow: the screen a state resolves its flagged figures on.

A validation finding is not a wall the submission hits. It becomes a query
owned by the state that reported the figure, answered with evidence, and
settled by the NPCU. These endpoints are the two halves of that: a state's
worklist and its responses, and the NPCU's review queue.

Who may do what follows the roles, not a flag on the request: a state responds
to its own queries and can do nothing else, and nobody accepts their own
response into the national figures.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response
from sqlalchemy import select

from app.api.deps import (
    CurrentPrincipal,
    DbSession,
    enforce_state_scope,
    require,
    visible_state_codes,
)
from app.core.config import settings
from app.core.enums import Permission, QueryStatus
from app.core.errors import NotFoundError, PermissionDeniedError, ValidationError
from app.core.security import file_digest
from app.models import DataQuery, IndicatorValue, QueryResponse, ReportingPeriod, User
from app.schemas.query import (
    CorrectionUploadResult,
    EvidenceRead,
    QueryDetail,
    QueryRead,
    QueryResponseRead,
    QuerySummaryRead,
    ReasonRequest,
    RespondRequest,
    ReviewRequest,
)
from app.services import corrections, disclosure, reference
from app.services import queries as query_service

router = APIRouter(prefix="/queries", tags=["Data queries"])

#: Evidence is stored beside the returns it supports.
EVIDENCE_SUBDIR = "evidence"


# --------------------------------------------------------------------------
# Serialisation
# --------------------------------------------------------------------------
def _actor_name(db: DbSession, user_id: int | None) -> str | None:
    if user_id is None:
        return None
    user = db.get(User, user_id)
    return user.full_name or user.email if user else None


def _evidence_read(db, row) -> EvidenceRead:
    return EvidenceRead(
        id=row.id,
        filename=row.filename,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        content_hash=row.content_hash,
        description=row.description,
        uploaded_by=_actor_name(db, row.uploaded_by_id),
        uploaded_at=row.created_at,
    )


def _response_read(db, response: QueryResponse) -> QueryResponseRead:
    period = (
        db.get(ReportingPeriod, response.restates_period_id)
        if response.restates_period_id
        else None
    )
    return QueryResponseRead(
        id=response.id,
        query_id=response.query_id,
        responder=_actor_name(db, response.responder_id),
        submitted_at=response.submitted_at,
        narrative=response.narrative,
        evidence_summary=response.evidence_summary,
        proposed_value=response.proposed_value,
        restates_period_code=period.code if period else None,
        reviewed_by=_actor_name(db, response.reviewed_by_id),
        reviewed_at=response.reviewed_at,
        review_outcome=response.review_outcome,
        review_note=response.review_note,
        evidence=[_evidence_read(db, row) for row in response.evidence],
    )


def _current_figure(db, query: DataQuery) -> IndicatorValue | None:
    if query.submission_id is None or query.indicator_id is None:
        return None
    return db.scalar(
        select(IndicatorValue).where(
            IndicatorValue.submission_id == query.submission_id,
            IndicatorValue.indicator_id == query.indicator_id,
        )
    )


def _query_read(db, query: DataQuery) -> QueryRead:
    figure = _current_figure(db, query)
    responses = query.responses
    latest = responses[-1] if responses else None
    state = query.state
    return QueryRead(
        id=query.id,
        reference=f"Q-{query.id}",
        submission_id=query.submission_id,
        state_id=query.state_id,
        state_code=state.code,
        state_name=state.name,
        cohort_code=state.cohort.code if state.cohort else None,
        period_id=query.period_id,
        period_code=query.period.code,
        period_label=query.period.label,
        indicator_id=query.indicator_id,
        indicator_code=query.indicator.code if query.indicator else None,
        indicator_name=query.indicator.name if query.indicator else None,
        rule_code=query.rule_code,
        dimension=query.dimension,
        severity=query.severity,
        title=query.title,
        detail=query.detail,
        reported_value=query.reported_value,
        current_value=figure.effective_value if figure else None,
        disclosure=str(disclosure.status_of(figure)) if figure else "CLEAN",
        status=query.status,
        resolution=query.resolution,
        resolution_note=query.resolution_note,
        due_date=query.due_date,
        is_open=query.is_open,
        is_overdue=query.is_overdue(),
        days_overdue=query.days_overdue(),
        verification_required=query.verification_required,
        opened_at=query.opened_at,
        closed_at=query.closed_at,
        response_count=len(responses),
        latest_proposed_value=latest.proposed_value if latest else None,
    )


def _other_open_queries(db, query: DataQuery) -> list[str]:
    """Other open queries holding the same figure out of the aggregations."""
    if query.submission_id is None or query.indicator_id is None:
        return []
    rows = db.scalars(
        select(DataQuery).where(
            DataQuery.submission_id == query.submission_id,
            DataQuery.indicator_id == query.indicator_id,
            DataQuery.id != query.id,
        )
    )
    return [f"Q-{row.id}" for row in rows if row.is_open]


def _detail(db, query: DataQuery) -> QueryDetail:
    return QueryDetail(
        **_query_read(db, query).model_dump(),
        responses=[_response_read(db, row) for row in query.responses],
        held_by=_other_open_queries(db, query),
    )


# --------------------------------------------------------------------------
# Access
# --------------------------------------------------------------------------
def _load(db, query_id: int, principal) -> DataQuery:
    query = db.get(DataQuery, query_id)
    if query is None:
        raise NotFoundError(f"Query #{query_id} does not exist")
    if principal.is_state_scoped and query.state_id != principal.state_id:
        raise PermissionDeniedError("You may only work on queries raised against your own state.")
    return query


def _own_state(db, query: DataQuery, principal) -> None:
    """A state answers for its own figures and nobody else's."""
    if principal.is_state_scoped and query.state_id != principal.state_id:
        raise PermissionDeniedError("You may only respond to queries raised against your own state.")


# --------------------------------------------------------------------------
# Worklists
# --------------------------------------------------------------------------
@router.get(
    "",
    response_model=list[QueryRead],
    summary="Queries, filtered for a worklist",
    dependencies=[Depends(require(Permission.DATA_READ))],
)
def list_queries(
    db: DbSession,
    principal: CurrentPrincipal,
    state: str | None = None,
    period: str | None = None,
    status: QueryStatus | None = None,
    open_only: bool = Query(default=True, description="Only queries that are somebody's work"),
    overdue_only: bool = False,
    awaiting_review: bool = Query(default=False, description="Responded, not yet reviewed"),
    verification_only: bool = False,
    indicator: str | None = None,
    limit: int = Query(default=200, le=1000),
    offset: int = 0,
) -> list[QueryRead]:
    stmt = select(DataQuery)
    if state:
        enforce_state_scope(db, principal, state)
        stmt = stmt.where(DataQuery.state_id == reference.get_state_by_code(db, state).id)
    else:
        allowed = visible_state_codes(db, principal)
        if allowed is not None:
            ids = [reference.get_state_by_code(db, code).id for code in allowed]
            stmt = stmt.where(DataQuery.state_id.in_(ids or [0]))
    if period:
        stmt = stmt.where(DataQuery.period_id == reference.get_period_by_code(db, period).id)
    if indicator:
        stmt = stmt.where(
            DataQuery.indicator_id == reference.get_indicator_by_code(db, indicator).id
        )
    if status is not None:
        stmt = stmt.where(DataQuery.status == str(status))
    elif awaiting_review:
        stmt = stmt.where(DataQuery.status == str(QueryStatus.RESPONDED))
    if verification_only:
        stmt = stmt.where(DataQuery.verification_required.is_(True))

    rows = list(db.scalars(stmt.order_by(DataQuery.due_date, DataQuery.id)))
    if status is None and open_only and not awaiting_review:
        rows = [row for row in rows if row.is_open]
    if overdue_only:
        rows = [row for row in rows if row.is_overdue()]
    return [_query_read(db, row) for row in rows[offset : offset + limit]]


@router.get(
    "/summary",
    response_model=QuerySummaryRead,
    summary="Query counts for one reporting period",
    dependencies=[Depends(require(Permission.DATA_READ))],
)
def summary(
    db: DbSession,
    principal: CurrentPrincipal,
    period: str | None = None,
    state: str | None = None,
) -> QuerySummaryRead:
    period_row = reference.get_period_by_code(db, period) if period else reference.latest_period(db)
    if period_row is None:
        raise NotFoundError("No reporting periods have been configured yet")

    if state:
        enforce_state_scope(db, principal, state)
        state_ids = [reference.get_state_by_code(db, state).id]
    else:
        allowed = visible_state_codes(db, principal)
        state_ids = (
            [reference.get_state_by_code(db, code).id for code in allowed]
            if allowed is not None
            else None
        )

    return QuerySummaryRead(
        period_code=period_row.code,
        period_label=period_row.label,
        **query_service.query_summary(db, period_row.id, state_ids=state_ids),
    )


@router.get(
    "/{query_id}",
    response_model=QueryDetail,
    summary="One query and every response on it",
    dependencies=[Depends(require(Permission.DATA_READ))],
)
def get_query(query_id: int, db: DbSession, principal: CurrentPrincipal) -> QueryDetail:
    return _detail(db, _load(db, query_id, principal))


# --------------------------------------------------------------------------
# The state's half
# --------------------------------------------------------------------------
@router.post(
    "/{query_id}/responses",
    response_model=QueryDetail,
    status_code=201,
    summary="Respond to a query: confirm the figure or propose a correction",
)
def respond(
    query_id: int,
    payload: RespondRequest,
    db: DbSession,
    principal=Depends(require(Permission.DATA_UPLOAD)),
) -> QueryDetail:
    query = _load(db, query_id, principal)
    _own_state(db, query, principal)
    query_service.respond(
        db,
        query,
        narrative=payload.narrative,
        proposed_value=payload.proposed_value,
        evidence_summary=payload.evidence_summary,
        restates_period_code=payload.restates_period_code,
        actor=principal.user,
    )
    db.commit()
    db.refresh(query)
    return _detail(db, query)


@router.post(
    "/{query_id}/responses/{response_id}/evidence",
    response_model=QueryDetail,
    status_code=201,
    summary="Attach a document to a response",
)
async def attach_evidence(
    query_id: int,
    response_id: int,
    db: DbSession,
    file: UploadFile = File(..., description="The document backing the response"),
    description: str | None = Form(default=None),
    principal=Depends(require(Permission.DATA_UPLOAD)),
) -> QueryDetail:
    query = _load(db, query_id, principal)
    _own_state(db, query, principal)
    response = db.get(QueryResponse, response_id)
    if response is None or response.query_id != query.id:
        raise NotFoundError(f"Response #{response_id} is not on query #{query_id}")

    content = await file.read()
    if not content:
        raise ValidationError("The uploaded file is empty.")
    if len(content) > settings.max_upload_bytes:
        raise ValidationError(
            f"Evidence files are limited to {settings.max_upload_mb} MB."
        )

    digest = file_digest(content)
    suffix = Path(file.filename or "evidence.bin").suffix or ".bin"
    target = Path(settings.upload_dir) / EVIDENCE_SUBDIR / query.state.code / f"Q-{query.id}"
    target.mkdir(parents=True, exist_ok=True)
    destination = target / f"{digest[:12]}{suffix}"
    destination.write_bytes(content)

    query_service.attach_evidence(
        db,
        response,
        filename=file.filename or destination.name,
        stored_path=str(destination),
        content_hash=digest,
        size_bytes=len(content),
        content_type=file.content_type,
        description=description,
        actor=principal.user,
    )
    db.commit()
    db.refresh(query)
    return _detail(db, query)


# --------------------------------------------------------------------------
# The NPCU's half
# --------------------------------------------------------------------------
@router.post(
    "/{query_id}/accept",
    response_model=QueryDetail,
    summary="Accept the response, restating the figure if one was proposed",
)
def accept(
    query_id: int,
    payload: ReviewRequest,
    db: DbSession,
    principal=Depends(require(Permission.DATA_APPROVE)),
) -> QueryDetail:
    query = _load(db, query_id, principal)
    query_service.accept(db, query, actor=principal.user, note=payload.note)
    db.commit()
    db.refresh(query)
    return _detail(db, query)


@router.post(
    "/{query_id}/reject",
    response_model=QueryDetail,
    summary="Send the query back to the state for a better response",
)
def reject(
    query_id: int,
    payload: ReasonRequest,
    db: DbSession,
    principal=Depends(require(Permission.DATA_APPROVE)),
) -> QueryDetail:
    query = _load(db, query_id, principal)
    query_service.reject(db, query, reason=payload.reason, actor=principal.user)
    db.commit()
    db.refresh(query)
    return _detail(db, query)


@router.post(
    "/{query_id}/verification",
    response_model=QueryDetail,
    summary="Refer the figure for physical checking on a supervision visit",
)
def escalate(
    query_id: int,
    payload: ReasonRequest,
    db: DbSession,
    principal=Depends(require(Permission.DATA_APPROVE)),
) -> QueryDetail:
    query = _load(db, query_id, principal)
    query_service.escalate_to_verification(
        db, query, reason=payload.reason, actor=principal.user
    )
    db.commit()
    db.refresh(query)
    return _detail(db, query)


@router.post(
    "/{query_id}/withdraw",
    response_model=QueryDetail,
    summary="Close a query raised in error",
)
def withdraw(
    query_id: int,
    payload: ReasonRequest,
    db: DbSession,
    principal=Depends(require(Permission.DATA_APPROVE)),
) -> QueryDetail:
    query = _load(db, query_id, principal)
    query_service.withdraw(db, query, reason=payload.reason, actor=principal.user)
    db.commit()
    db.refresh(query)
    return _detail(db, query)


# --------------------------------------------------------------------------
# Correction sheets
# --------------------------------------------------------------------------
@router.get(
    "/sheets/{state_code}",
    response_class=Response,
    summary="Download the correction sheet: only this state's flagged figures",
    dependencies=[Depends(require(Permission.DATA_READ))],
)
def correction_sheet(
    state_code: str,
    db: DbSession,
    principal: CurrentPrincipal,
    period: str | None = None,
) -> Response:
    enforce_state_scope(db, principal, state_code)
    state = reference.get_state_by_code(db, state_code)
    period_row = reference.get_period_by_code(db, period) if period else reference.latest_period(db)
    if period_row is None:
        raise NotFoundError("No reporting periods have been configured yet")

    payload = corrections.build_correction_sheet(db, state, period_row)
    filename = f"AGILE_corrections_{state.code}_{period_row.code}.xlsx"
    return Response(
        content=payload,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/sheets/{state_code}",
    response_model=CorrectionUploadResult,
    status_code=201,
    summary="Upload a completed correction sheet",
)
async def upload_correction_sheet(
    state_code: str,
    db: DbSession,
    file: UploadFile = File(..., description="The completed correction sheet"),
    period_code: str | None = Form(default=None),
    principal=Depends(require(Permission.DATA_UPLOAD)),
) -> CorrectionUploadResult:
    enforce_state_scope(db, principal, state_code)
    state = reference.get_state_by_code(db, state_code)
    period_row = (
        reference.get_period_by_code(db, period_code)
        if period_code
        else reference.latest_period(db)
    )
    if period_row is None:
        raise NotFoundError("No reporting periods have been configured yet")

    content = await file.read()
    result = corrections.ingest_correction_sheet(
        db, content, state=state, period=period_row, actor=principal.user
    )
    db.commit()

    return CorrectionUploadResult(
        applied=result.applied,
        skipped=result.skipped,
        response_ids=result.responses,
        warnings=result.warnings,
        message=(
            f"Recorded {result.applied} response(s) for {state.name}/{period_row.code}."
            + (f" {result.skipped} row(s) were skipped." if result.skipped else "")
        ),
    )
