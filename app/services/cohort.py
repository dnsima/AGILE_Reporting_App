"""Cohort-based analytics.

Groups states by financing cohort and compares performance both *within* a
cohort (state against state) and *across* cohorts, covering KPI achievement,
DQA performance, contribution to national results, reporting timeliness and
data completeness.
"""

from __future__ import annotations

from statistics import fmean

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import FitnessVerdict
from app.models import Cohort, Indicator, ReportingPeriod
from app.schemas.analytics import CohortComparison, CohortSummary
from app.schemas.validation import StateReturn
from app.services import analytics, reference, returns


def _completeness_pct(rows: list[StateReturn]) -> float:
    """Share of the framework the cohort's states actually answered.

    Counted from the figures themselves rather than read off a completeness
    score, because the scores went with the grade.
    """
    reported = sum(row.figures_reported for row in rows)
    expected = len(rows) * max((row.figures_reported for row in rows), default=0)
    return round(100.0 * reported / expected, 2) if expected else 0.0


def cohort_summaries(
    db: Session,
    period: ReportingPeriod,
    *,
    indicators: list[Indicator] | None = None,
) -> list[CohortSummary]:
    """One summary row per financing cohort for a reporting period."""
    indicators = indicators or reference.active_indicators(db)
    states = reference.active_states(db)
    state_returns = {state.code: returns.state_return(db, state, period) for state in states}
    cohorts = sorted(db.scalars(select(Cohort)), key=lambda c: c.sort_order)

    # Per-cohort contribution to national results, averaged over indicators.
    contribution_totals: dict[str, list[float]] = {cohort.code: [] for cohort in cohorts}
    for indicator in indicators:
        analysis = analytics.analyse_indicator(db, indicator, period)
        for row in analysis.cohorts:
            if row.contribution_pct is not None:
                contribution_totals[row.cohort_code].append(row.contribution_pct)

    summaries: list[CohortSummary] = []
    for cohort in cohorts:
        members = [state for state in states if state.cohort_id == cohort.id]
        if not members:
            continue
        cards = [state_returns[state.code] for state in members]
        submitted = [card for card in cards if card.submission_id is not None]
        on_time = [card for card in submitted if (card.days_late or 0) == 0]

        board = analytics.scorecard(
            db, period, scope="COHORT", cohort_code=cohort.code, indicators=indicators
        )
        state_averages: dict[str, float] = {}
        for state in members:
            state_board = analytics.scorecard(
                db, period, scope="STATE", state_code=state.code, indicators=indicators
            )
            if state_board.average_achievement_pct is not None:
                state_averages[state.name] = state_board.average_achievement_pct

        reported = sum(card.figures_reported for card in submitted)
        counting = sum(card.figures_counting for card in submitted)
        contributions = contribution_totals.get(cohort.code, [])

        summaries.append(
            CohortSummary(
                cohort_code=cohort.code,
                cohort_name=cohort.name,
                states_expected=len(members),
                states_reporting=len(submitted),
                reporting_rate_pct=round(len(submitted) / len(members) * 100, 2),
                on_time_rate_pct=round(len(on_time) / len(members) * 100, 2),
                completeness_pct=_completeness_pct(submitted),
                states_not_fit=sum(
                    1 for card in submitted
                    if card.fitness_verdict == str(FitnessVerdict.NOT_FIT)
                ),
                usable_share_pct=(
                    round(100.0 * counting / reported, 1) if reported else None
                ),
                average_achievement_pct=board.average_achievement_pct,
                indicators_on_track=board.indicators_on_track,
                indicators_assessed=board.indicators_with_target,
                contribution_pct=round(fmean(contributions), 2) if contributions else None,
                best_state=max(state_averages, key=state_averages.get) if state_averages else None,
                weakest_state=min(state_averages, key=state_averages.get) if state_averages else None,
            )
        )

    return summaries


def compare_cohorts(
    db: Session, period: ReportingPeriod, indicator_code: str | None = None
) -> CohortComparison:
    """Across-cohort comparison, optionally narrowed to a single indicator."""
    indicator = (
        reference.get_indicator_by_code(db, indicator_code) if indicator_code else None
    )
    indicators = [indicator] if indicator else None
    summaries = cohort_summaries(db, period, indicators=indicators)

    return CohortComparison(
        period_code=period.code,
        indicator=analytics.indicator_ref(indicator) if indicator else None,
        cohorts=summaries,
    )


def within_cohort_ranking(
    db: Session,
    cohort_code: str,
    period: ReportingPeriod,
    *,
    indicator_code: str | None = None,
) -> list[dict]:
    """Rank the states inside one cohort, for within-cohort comparison."""
    cohort = reference.get_cohort_by_code(db, cohort_code)
    states = reference.active_states(db, cohort.code)
    indicators = (
        [reference.get_indicator_by_code(db, indicator_code)]
        if indicator_code
        else reference.active_indicators(db)
    )

    rows: list[dict] = []
    for state in states:
        board = analytics.scorecard(
            db, period, scope="STATE", state_code=state.code, indicators=indicators
        )
        card = returns.state_return(db, state, period)
        rows.append(
            {
                "state_code": state.code,
                "state_name": state.name,
                "cohort_code": cohort.code,
                "average_achievement_pct": board.average_achievement_pct,
                "indicators_on_track": board.indicators_on_track,
                "indicators_with_data": board.indicators_with_data,
                "fitness_verdict": card.fitness_verdict,
                "open_findings": card.error_count + card.warning_count,
                "days_late": card.days_late,
                "reported": card.submission_id is not None,
            }
        )

    rows.sort(
        key=lambda row: (
            row["average_achievement_pct"] is None,
            -(row["average_achievement_pct"] or 0),
            row["state_name"],
        )
    )
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows
