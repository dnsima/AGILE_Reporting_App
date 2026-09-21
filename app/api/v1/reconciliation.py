"""Reconciliation between the monthly tracker and the quarterly framework.

States report the same 53 indicators twice. These endpoints say, per state and
per indicator, whether the two streams agree -- and where they do not, what the
tracker implies the quarterly figure should have been, and why.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import (
    CurrentPrincipal,
    DbSession,
    enforce_state_scope,
    require,
    visible_state_codes,
)
from app.core.enums import PeriodType, Permission, ReconciliationStatus
from app.core.errors import NotFoundError
from app.schemas.reconciliation import (
    ReconciliationLineRead,
    ReconciliationReport,
    ReconciliationSummary,
    StateReconciliationRead,
)
from app.services import reconciliation, reference

router = APIRouter(
    prefix="/reconciliation",
    tags=["Reconciliation"],
    dependencies=[Depends(require(Permission.DATA_READ))],
)


def _resolve_period(db, period_code: str | None):
    if period_code:
        return reference.get_period_by_code(db, period_code)
    period = reference.latest_period(db, str(PeriodType.QUARTERLY))
    if period is None:
        raise NotFoundError("No quarterly reporting period has been configured yet")
    return period


def _line_read(line: reconciliation.ReconciliationLine) -> ReconciliationLineRead:
    return ReconciliationLineRead(
        indicator_id=line.indicator_id,
        indicator_code=line.indicator_code,
        indicator_name=line.indicator_name,
        subcomponent_code=line.subcomponent_code,
        unit=line.unit,
        basis=str(line.basis),
        coarse_value=line.coarse_value,
        fine_value=line.fine_value,
        variance=line.variance,
        variance_pct=line.variance_pct,
        status=str(line.status),
        note=line.note,
        parts_expected=line.parts_expected,
        parts_reported=line.parts_reported,
        part_values=line.part_values,
        is_flagged=line.is_flagged,
        reconciles_as=None if line.reconciles_as is None else str(line.reconciles_as),
    )


def _state_read(
    result: reconciliation.StateReconciliation,
    lines: list[reconciliation.ReconciliationLine],
) -> StateReconciliationRead:
    return StateReconciliationRead(
        state_id=result.state_id,
        state_code=result.state_code,
        state_name=result.state_name,
        cohort_code=result.cohort_code,
        period_code=result.period_code,
        period_label=result.period_label,
        parts_expected=result.parts_expected,
        parts_reported=result.parts_reported,
        tracker_is_complete=result.is_complete,
        coarse_submission_id=result.coarse_submission_id,
        fine_submission_ids=result.fine_submission_ids,
        judged=result.judged,
        matched=result.count(ReconciliationStatus.MATCHED),
        mismatched=result.count(ReconciliationStatus.MISMATCH),
        tracker_missing=result.count(ReconciliationStatus.TRACKER_MISSING),
        framework_missing=result.count(ReconciliationStatus.FRAMEWORK_MISSING),
        incomplete=result.count(ReconciliationStatus.INCOMPLETE),
        agreement=result.agreement,
        # Counts describe the whole reconciliation; the line list honours the
        # caller's filter, so a summary never changes because of a filter.
        lines=[_line_read(line) for line in lines],
    )


@router.get(
    "",
    response_model=ReconciliationReport,
    summary="Reconcile the tracker against the framework for a period",
)
def report(
    db: DbSession,
    principal: CurrentPrincipal,
    period: str | None = Query(default=None, description="Coarser period, e.g. 2026-Q2"),
    state: str | None = Query(default=None, description="Limit to one state"),
    status: ReconciliationStatus | None = Query(
        default=None, description="Keep only lines with this verdict"
    ),
    unreconciled_only: bool = Query(
        default=False, description="Keep only lines that are somebody's work"
    ),
) -> ReconciliationReport:
    period_row = _resolve_period(db, period)

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

    results = reconciliation.reconcile_period(db, period_row, state_ids=state_ids)

    def keep(line: reconciliation.ReconciliationLine) -> bool:
        if status is not None and line.status is not status:
            return False
        return line.needs_attention if unreconciled_only else True

    summary = reconciliation.national_summary(results)
    child = reconciliation.finer_grain(PeriodType(period_row.period_type))
    return ReconciliationReport(
        summary=ReconciliationSummary(
            period_code=period_row.code,
            period_label=period_row.label,
            child_period_type=str(child) if child else None,
            **summary,
        ),
        states=[
            _state_read(result, [line for line in result.lines if keep(line)])
            for result in results
        ],
    )


@router.get(
    "/{state_code}",
    response_model=StateReconciliationRead,
    summary="Reconcile one state's tracker against its framework return",
)
def state_report(
    state_code: str,
    db: DbSession,
    principal: CurrentPrincipal,
    period: str | None = None,
    unreconciled_only: bool = False,
) -> StateReconciliationRead:
    enforce_state_scope(db, principal, state_code)
    state = reference.get_state_by_code(db, state_code)
    result = reconciliation.reconcile_state(db, state, _resolve_period(db, period))
    lines = result.unreconciled if unreconciled_only else result.lines
    return _state_read(result, lines)
