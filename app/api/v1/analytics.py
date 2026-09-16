"""KPI analysis endpoints: the three analysis layers, trends and heatmaps."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app.api.deps import CurrentPrincipal, DbSession, enforce_state_scope, require
from app.core.enums import PeriodType, Permission
from app.core.errors import NotFoundError
from app.schemas.analytics import (
    ContributionAnalysis,
    Heatmap,
    IndicatorAnalysis,
    PerformanceScorecard,
    TrendSeries,
)
from app.services import analytics, reference

router = APIRouter(
    prefix="/analytics",
    tags=["KPI analysis"],
    dependencies=[Depends(require(Permission.ANALYTICS_READ))],
)


def _resolve_period(db, period_code: str | None, period_type: PeriodType | None = None):
    if period_code:
        return reference.get_period_by_code(db, period_code)
    period = reference.latest_period(db, str(period_type) if period_type else None)
    if period is None:
        raise NotFoundError("No reporting periods have been configured yet")
    return period


@router.get(
    "/indicators/{indicator_code}",
    response_model=IndicatorAnalysis,
    summary="Three-layer analysis for one indicator",
    description=(
        "Returns state performance against state targets (layer 1), the national roll-up "
        "against the national target (layer 2) and each state's percentage contribution to "
        "the national achievement (layer 3), disaggregated by financing cohort."
    ),
)
def indicator_analysis(
    indicator_code: str,
    db: DbSession,
    principal: CurrentPrincipal,
    period: str | None = Query(default=None, description="Defaults to the latest closed period"),
    cohort: str | None = Query(default=None, description="Restrict to one financing cohort"),
) -> IndicatorAnalysis:
    indicator = reference.get_indicator_by_code(db, indicator_code)
    return analytics.analyse_indicator(
        db, indicator, _resolve_period(db, period), cohort_code=cohort
    )


@router.get(
    "/scorecard",
    response_model=PerformanceScorecard,
    summary="Cross-sectional scorecard across all indicators",
)
def scorecard(
    db: DbSession,
    principal: CurrentPrincipal,
    period: str | None = None,
    scope: str = Query(default="NATIONAL", pattern="^(?i)(NATIONAL|STATE|COHORT)$"),
    state: str | None = None,
    cohort: str | None = None,
    category: str | None = Query(default=None, description="Restrict to an indicator category"),
    indicator_codes: list[str] | None = Query(default=None),
) -> PerformanceScorecard:
    if scope.upper() == "STATE" and state:
        enforce_state_scope(db, principal, state)
    indicators = reference.active_indicators(
        db, category_code=category, indicator_codes=indicator_codes
    )
    return analytics.scorecard(
        db,
        _resolve_period(db, period),
        scope=scope.upper(),
        state_code=state,
        cohort_code=cohort,
        indicators=indicators,
    )


@router.get(
    "/contribution/{indicator_code}",
    response_model=ContributionAnalysis,
    summary="Each state's percentage contribution to the national result",
)
def contribution(
    indicator_code: str,
    db: DbSession,
    principal: CurrentPrincipal,
    period: str | None = None,
) -> ContributionAnalysis:
    indicator = reference.get_indicator_by_code(db, indicator_code)
    return analytics.contribution_analysis(db, indicator, _resolve_period(db, period))


@router.get(
    "/trend/{indicator_code}",
    response_model=TrendSeries,
    summary="Longitudinal series for one indicator",
)
def trend(
    indicator_code: str,
    db: DbSession,
    principal: CurrentPrincipal,
    scope: str = Query(default="NATIONAL", pattern="^(?i)(NATIONAL|STATE|COHORT)$"),
    state: str | None = None,
    cohort: str | None = None,
    period_type: PeriodType | None = None,
    periods: int = Query(default=8, ge=2, le=36),
) -> TrendSeries:
    if scope.upper() == "STATE" and state:
        enforce_state_scope(db, principal, state)
    indicator = reference.get_indicator_by_code(db, indicator_code)
    return analytics.trend(
        db,
        indicator,
        scope=scope.upper(),
        state_code=state,
        cohort_code=cohort,
        period_type=str(period_type) if period_type else None,
        limit=periods,
    )


@router.get("/heatmap", response_model=Heatmap, summary="States by indicators heatmap")
def heatmap(
    db: DbSession,
    principal: CurrentPrincipal,
    period: str | None = None,
    metric: str = Query(default="achievement", pattern="^(achievement|value)$"),
    cohort: str | None = None,
    category: str | None = None,
    indicator_codes: list[str] | None = Query(default=None),
    limit_indicators: int = Query(default=20, ge=1, le=70),
) -> Heatmap:
    indicators = reference.active_indicators(
        db, category_code=category, indicator_codes=indicator_codes
    )
    return analytics.heatmap(
        db,
        _resolve_period(db, period),
        metric=metric,
        indicators=indicators,
        cohort_code=cohort,
        limit_indicators=limit_indicators,
    )
