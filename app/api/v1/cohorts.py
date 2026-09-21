"""Cohort-based analytics endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import CurrentPrincipal, DbSession, require
from app.core.enums import Permission
from app.core.errors import NotFoundError
from app.schemas.analytics import CohortComparison, CohortSummary
from app.schemas.validation import CohortReturns
from app.services import cohort as cohort_service
from app.services import reference, returns

router = APIRouter(
    prefix="/cohorts",
    tags=["Cohort analytics"],
    dependencies=[Depends(require(Permission.ANALYTICS_READ))],
)


def _resolve_period(db, period_code: str | None):
    if period_code:
        return reference.get_period_by_code(db, period_code)
    period = reference.latest_period(db)
    if period is None:
        raise NotFoundError("No reporting periods have been configured yet")
    return period


@router.get(
    "/summary",
    response_model=list[CohortSummary],
    summary="Cohort summaries: KPI, DQA, contribution, timeliness and completeness",
)
def cohort_summary(
    db: DbSession,
    principal: CurrentPrincipal,
    period: str | None = None,
    category: str | None = Query(default=None, description="Restrict to an indicator category"),
) -> list[CohortSummary]:
    indicators = reference.active_indicators(db, category_code=category) if category else None
    return cohort_service.cohort_summaries(db, _resolve_period(db, period), indicators=indicators)


@router.get(
    "/compare",
    response_model=CohortComparison,
    summary="Compare performance across financing cohorts",
)
def compare(
    db: DbSession,
    principal: CurrentPrincipal,
    period: str | None = None,
    indicator: str | None = Query(default=None, description="Narrow to a single KPI"),
) -> CohortComparison:
    return cohort_service.compare_cohorts(db, _resolve_period(db, period), indicator)


@router.get(
    "/{cohort_code}/states",
    response_model=list[dict],
    summary="Rank the states inside one cohort",
)
def within_cohort(
    cohort_code: str,
    db: DbSession,
    principal: CurrentPrincipal,
    period: str | None = None,
    indicator: str | None = None,
) -> list[dict]:
    return cohort_service.within_cohort_ranking(
        db, cohort_code, _resolve_period(db, period), indicator_code=indicator
    )


@router.get(
    "/{cohort_code}/returns",
    response_model=CohortReturns,
    summary="How a cohort's returns for a period stand",
)
def cohort_return_status(
    cohort_code: str,
    db: DbSession,
    principal: CurrentPrincipal,
    period: str | None = None,
) -> CohortReturns:
    cohort = reference.get_cohort_by_code(db, cohort_code)
    return returns.cohort_returns(db, cohort, _resolve_period(db, period))
