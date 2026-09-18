"""KPI consolidation and analysis.

Produces the three layers the platform is specified around:

1. **State layer**    - each state's value against its own state-level target;
2. **National layer** - the consolidated national value against the national target;
3. **Contribution**   - each state's share of the national achievement.

Every result can be disaggregated by financing cohort, and the same primitives
serve both cross-sectional (one period, many states) and longitudinal (one
scope, many periods) analysis.

Only submissions that are ``APPROVED`` **and** ``is_current`` are read here, so
data that failed the quality gate can never reach an analysis result.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    AggregationMethod,
    Direction,
    SubmissionStatus,
    TargetLevel,
)
from app.core.events import event_bus
from app.models import (
    Cohort,
    Indicator,
    IndicatorValue,
    ReportingPeriod,
    Submission,
    Target,
)
from app.schemas.analytics import (
    CohortPerformance,
    ContributionAnalysis,
    ContributionRow,
    Heatmap,
    HeatmapCell,
    IndicatorAnalysis,
    IndicatorRef,
    NationalPerformance,
    PerformanceScorecard,
    ScorecardRow,
    StatePerformance,
    TrendPoint,
    TrendSeries,
)
from app.services import reference

#: Labels applied to an achievement percentage.
STATUS_BANDS = (
    (90.0, "On track"),
    (70.0, "Progressing"),
    (50.0, "Lagging"),
    (0.0, "Off track"),
)

#: Disaggregation labels that mark a row as an already-totalled figure.
TOTAL_LABELS = {"total", "all", "both", "overall"}


def _row_status(achievement_pct: float | None, target: float | None, has_data: bool) -> str:
    """Status label for a row that may have no target, or no data at all."""
    if target is not None:
        return status_for(achievement_pct)
    return "Reported" if has_data else "No data"


def _nonzero_mean(values: list[float]) -> float | None:
    """Mean over the values that are not zero.

    A state reporting 0% for a completion rate is almost always a non-entry
    rather than a genuine zero, and averaging it in drags the national rate
    down. This mirrors the NPCU's own rule: "unweighted average of non-zero
    states".
    """
    populated = [value for value in values if value]
    return round(fmean(populated), 4) if populated else None


def _count_yes(values: list[float]) -> float:
    """How many states answered Yes. Any value at or above 1 counts as Yes."""
    return float(sum(1 for value in values if value is not None and value >= 1))


def status_for(achievement_pct: float | None) -> str:
    if achievement_pct is None:
        return "No target"
    for threshold, label in STATUS_BANDS:
        if achievement_pct >= threshold:
            return label
    return "Off track"


def indicator_ref(indicator: Indicator) -> IndicatorRef:
    return IndicatorRef(
        id=indicator.id,
        number=indicator.number,
        code=indicator.code,
        name=indicator.name,
        unit=indicator.unit,
        direction=indicator.direction,
        category_code=indicator.category.code if indicator.category else None,
        category_name=indicator.category.name if indicator.category else None,
    )


@dataclass
class Reading:
    """One state's consolidated figure for one indicator in one period."""

    value: float | None = None
    numerator: float | None = None
    denominator: float | None = None

    @property
    def has_data(self) -> bool:
        return self.value is not None or self.numerator is not None


# --------------------------------------------------------------------------
# Request-scoped memoisation
# --------------------------------------------------------------------------
# A 70-indicator national report asks for the same readings and targets dozens
# of times. Caching them on the Session keeps the cache alive exactly as long as
# the request that owns it, and folding the event bus's data version into the
# key means any ingestion invalidates it automatically.
def _cache(db: Session) -> dict:
    store = db.info.setdefault("_analytics_cache", {})
    version = event_bus.data_version
    if store.get("_version") != version:
        store.clear()
        store["_version"] = version
    return store


def clear_analysis_cache(db: Session) -> None:
    db.info.pop("_analytics_cache", None)


# --------------------------------------------------------------------------
# Data access
# --------------------------------------------------------------------------
def analysable_submissions(
    db: Session, period_id: int, state_ids: list[int] | None = None
) -> dict[int, int]:
    """Map ``state_id -> submission_id`` for data cleared to enter analysis."""
    cache = _cache(db)
    key = ("submissions", period_id, tuple(sorted(state_ids)) if state_ids else None)
    if key in cache:
        return cache[key]

    stmt = select(Submission).where(
        Submission.period_id == period_id,
        Submission.is_current.is_(True),
        Submission.status == SubmissionStatus.APPROVED,
    )
    if state_ids:
        stmt = stmt.where(Submission.state_id.in_(state_ids))
    result = {row.state_id: row.id for row in db.scalars(stmt)}
    cache[key] = result
    return result


def _collapse(rows: list[IndicatorValue], indicator: Indicator) -> Reading:
    """Reduce a state's rows for one indicator to a single figure.

    A row explicitly marked as a total wins; otherwise disaggregated rows are
    combined according to the indicator's aggregation method.
    """
    if not rows:
        return Reading()

    totals = [
        row
        for row in rows
        if not row.disaggregation
        or all(str(v).strip().lower() in TOTAL_LABELS for v in row.disaggregation.values())
    ]
    pool = totals or rows

    numerators = [row.numerator for row in pool if row.numerator is not None]
    denominators = [row.denominator for row in pool if row.denominator is not None]
    values = [row.value for row in pool if row.value is not None]

    if len(pool) == 1:
        single = pool[0]
        return Reading(
            value=single.effective_value,
            numerator=single.numerator,
            denominator=single.denominator,
        )

    method = AggregationMethod(indicator.aggregation_method)
    numerator = sum(numerators) if numerators else None
    denominator = sum(denominators) if denominators else None

    if method == AggregationMethod.WEIGHTED_AVERAGE:
        if numerator is not None and denominator:
            return Reading(
                value=numerator / denominator * 100.0,
                numerator=numerator,
                denominator=denominator,
            )
        return Reading(value=fmean(values) if values else None, numerator=numerator, denominator=denominator)

    if method == AggregationMethod.AVERAGE:
        return Reading(value=fmean(values) if values else None, numerator=numerator, denominator=denominator)
    if method == AggregationMethod.AVERAGE_NONZERO:
        return Reading(value=_nonzero_mean(values), numerator=numerator, denominator=denominator)
    if method == AggregationMethod.COUNT_YES:
        # Within one state a Yes/No indicator has a single answer; if rows were
        # split by disaggregation, any Yes makes the state a Yes.
        return Reading(value=max(values) if values else None, numerator=numerator, denominator=denominator)
    if method == AggregationMethod.MAX:
        return Reading(value=max(values) if values else None, numerator=numerator, denominator=denominator)
    if method == AggregationMethod.MIN:
        return Reading(value=min(values) if values else None, numerator=numerator, denominator=denominator)
    if method == AggregationMethod.LATEST:
        return Reading(value=values[-1] if values else None, numerator=numerator, denominator=denominator)

    return Reading(
        value=sum(values) if values else None, numerator=numerator, denominator=denominator
    )


def readings_for_period(
    db: Session,
    period_id: int,
    indicators: list[Indicator],
    state_ids: list[int] | None = None,
) -> dict[int, dict[int, Reading]]:
    """``{state_id: {indicator_id: Reading}}`` for one period."""
    cache = _cache(db)
    key = (
        "readings",
        period_id,
        tuple(sorted(indicator.id for indicator in indicators)),
        tuple(sorted(state_ids)) if state_ids else None,
    )
    if key in cache:
        return cache[key]

    submissions = analysable_submissions(db, period_id, state_ids)
    if not submissions:
        cache[key] = {}
        return {}

    submission_to_state = {sid: state_id for state_id, sid in submissions.items()}
    indicator_ids = [indicator.id for indicator in indicators]
    indicators_by_id = {indicator.id: indicator for indicator in indicators}

    grouped: dict[int, dict[int, list[IndicatorValue]]] = {}
    for row in db.scalars(
        select(IndicatorValue).where(
            IndicatorValue.submission_id.in_(list(submission_to_state)),
            IndicatorValue.indicator_id.in_(indicator_ids),
            IndicatorValue.is_valid.is_(True),
        )
    ):
        state_id = submission_to_state[row.submission_id]
        grouped.setdefault(state_id, {}).setdefault(row.indicator_id, []).append(row)

    result = {
        state_id: {
            indicator_id: _collapse(rows, indicators_by_id[indicator_id])
            for indicator_id, rows in per_indicator.items()
        }
        for state_id, per_indicator in grouped.items()
    }
    cache[key] = result
    return result


def targets_for_period(
    db: Session, period_id: int, level: TargetLevel | None = None
) -> tuple[dict[tuple[int, int], float], dict[int, float]]:
    """Return ``({(state_id, indicator_id): target}, {indicator_id: national_target})``."""
    cache = _cache(db)
    key = ("targets", period_id, str(level) if level else None)
    if key in cache:
        return cache[key]

    state_targets: dict[tuple[int, int], float] = {}
    national_targets: dict[int, float] = {}
    stmt = select(Target).where(Target.period_id == period_id)
    if level is not None:
        stmt = stmt.where(Target.level == str(level))
    for row in db.scalars(stmt):
        if row.level == TargetLevel.NATIONAL or row.state_id is None:
            national_targets[row.indicator_id] = row.target_value
        else:
            state_targets[(row.state_id, row.indicator_id)] = row.target_value
    cache[key] = (state_targets, national_targets)
    return state_targets, national_targets


# --------------------------------------------------------------------------
# Achievement maths
# --------------------------------------------------------------------------
def achievement(
    value: float | None, target: float | None, indicator: Indicator
) -> float | None:
    """Achievement against target as a percentage, respecting indicator direction."""
    if value is None or target is None:
        return None

    if Direction(indicator.direction) == Direction.DECREASE:
        baseline = indicator.baseline_value
        if baseline is not None and baseline != target:
            # Progress along the baseline -> target journey.
            return round((baseline - value) / (baseline - target) * 100.0, 2)
        if value <= 0:
            return 100.0 if target <= 0 else None
        return round(target / value * 100.0, 2)

    if target == 0:
        return 100.0 if value == 0 else None
    return round(value / target * 100.0, 2)


def aggregate_national(
    readings: list[Reading], indicator: Indicator
) -> Reading:
    """Consolidate state readings into one national figure."""
    populated = [reading for reading in readings if reading.has_data]
    if not populated:
        return Reading()

    method = AggregationMethod(indicator.aggregation_method)
    values = [r.value for r in populated if r.value is not None]
    numerators = [r.numerator for r in populated if r.numerator is not None]
    denominators = [r.denominator for r in populated if r.denominator is not None]
    numerator = sum(numerators) if numerators else None
    denominator = sum(denominators) if denominators else None

    if method == AggregationMethod.WEIGHTED_AVERAGE:
        if numerator is not None and denominator:
            return Reading(numerator / denominator * 100.0, numerator, denominator)
        return Reading(round(fmean(values), 4) if values else None, numerator, denominator)
    if method == AggregationMethod.AVERAGE:
        return Reading(round(fmean(values), 4) if values else None, numerator, denominator)
    if method == AggregationMethod.AVERAGE_NONZERO:
        return Reading(_nonzero_mean(values), numerator, denominator)
    if method == AggregationMethod.COUNT_YES:
        return Reading(_count_yes(values), numerator, denominator)
    if method == AggregationMethod.MAX:
        return Reading(max(values) if values else None, numerator, denominator)
    if method == AggregationMethod.MIN:
        return Reading(min(values) if values else None, numerator, denominator)
    if method == AggregationMethod.LATEST:
        return Reading(values[-1] if values else None, numerator, denominator)

    return Reading(sum(values) if values else None, numerator, denominator)


def _contribution_basis(reading: Reading, indicator: Indicator) -> float | None:
    """The quantity a state contributes to the national figure.

    Contribution is only meaningful where the national figure is a total a
    state adds to. For an averaged rate or a count of Yes states, a share of
    the national value would be arithmetic without meaning, so none is offered.
    """
    method = AggregationMethod(indicator.aggregation_method)
    if method == AggregationMethod.WEIGHTED_AVERAGE:
        return reading.numerator if reading.numerator is not None else reading.value
    if method in {
        AggregationMethod.AVERAGE,
        AggregationMethod.AVERAGE_NONZERO,
        AggregationMethod.COUNT_YES,
        AggregationMethod.MAX,
        AggregationMethod.MIN,
        AggregationMethod.LATEST,
    }:
        return None
    return reading.value


# --------------------------------------------------------------------------
# Layer 1-3: full analysis for one indicator
# --------------------------------------------------------------------------
def analyse_indicator(
    db: Session,
    indicator: Indicator,
    period: ReportingPeriod,
    *,
    cohort_code: str | None = None,
) -> IndicatorAnalysis:
    states = reference.active_states(db, cohort_code)
    state_ids = [state.id for state in states]
    readings = readings_for_period(db, period.id, [indicator], state_ids)
    state_targets, national_targets = targets_for_period(db, period.id)

    cohorts = {cohort.id: cohort for cohort in db.scalars(select(Cohort))}

    # --- layer 1: state performance -------------------------------------
    state_rows: list[StatePerformance] = []
    contribution_total = 0.0
    bases: dict[int, float] = {}

    for state in states:
        reading = readings.get(state.id, {}).get(indicator.id, Reading())
        target = state_targets.get((state.id, indicator.id))
        pct = achievement(reading.value, target, indicator)
        basis = _contribution_basis(reading, indicator)
        if basis is not None:
            bases[state.id] = basis
            contribution_total += basis

        state_rows.append(
            StatePerformance(
                state_code=state.code,
                state_name=state.name,
                cohort_code=cohorts[state.cohort_id].code if state.cohort_id in cohorts else None,
                value=reading.value,
                numerator=reading.numerator,
                denominator=reading.denominator,
                target=target,
                achievement_pct=pct,
                variance=(
                    round(reading.value - target, 4)
                    if reading.value is not None and target is not None
                    else None
                ),
                status=_row_status(pct, target, reading.has_data),
                reported=reading.has_data,
            )
        )

    for row in state_rows:
        state = next(s for s in states if s.code == row.state_code)
        basis = bases.get(state.id)
        if basis is not None and contribution_total:
            row.contribution_pct = round(basis / contribution_total * 100.0, 2)

    # --- layer 2: national performance ----------------------------------
    national_reading = aggregate_national(
        [readings.get(state.id, {}).get(indicator.id, Reading()) for state in states], indicator
    )
    national_target = national_targets.get(indicator.id)
    if national_target is None:
        # Fall back to the sum/mean of state targets where no explicit national
        # target has been loaded.
        applicable = [
            value for (state_id, indicator_id), value in state_targets.items()
            if indicator_id == indicator.id and state_id in set(state_ids)
        ]
        if applicable:
            method = AggregationMethod(indicator.aggregation_method)
            national_target = (
                sum(applicable) if method == AggregationMethod.SUM else round(fmean(applicable), 4)
            )

    national_pct = achievement(national_reading.value, national_target, indicator)
    national = NationalPerformance(
        value=national_reading.value,
        numerator=national_reading.numerator,
        denominator=national_reading.denominator,
        target=national_target,
        achievement_pct=national_pct,
        variance=(
            round(national_reading.value - national_target, 4)
            if national_reading.value is not None and national_target is not None
            else None
        ),
        status=status_for(national_pct) if national_target is not None else "No target",
        aggregation_method=indicator.aggregation_method,
        states_reporting=sum(1 for row in state_rows if row.reported),
        states_expected=len(states),
    )

    # --- cohort disaggregation ------------------------------------------
    cohort_rows: list[CohortPerformance] = []
    for cohort in sorted(cohorts.values(), key=lambda c: c.sort_order):
        members = [state for state in states if state.cohort_id == cohort.id]
        if not members:
            continue
        member_readings = [
            readings.get(state.id, {}).get(indicator.id, Reading()) for state in members
        ]
        cohort_reading = aggregate_national(member_readings, indicator)
        member_targets = [
            state_targets.get((state.id, indicator.id))
            for state in members
            if state_targets.get((state.id, indicator.id)) is not None
        ]
        method = AggregationMethod(indicator.aggregation_method)
        cohort_target = (
            (sum(member_targets) if method == AggregationMethod.SUM else round(fmean(member_targets), 4))
            if member_targets
            else None
        )
        cohort_pct = achievement(cohort_reading.value, cohort_target, indicator)
        cohort_basis = sum(
            bases.get(state.id, 0.0) for state in members if state.id in bases
        )
        member_achievements = [
            row.achievement_pct
            for row in state_rows
            if row.cohort_code == cohort.code and row.achievement_pct is not None
        ]

        cohort_rows.append(
            CohortPerformance(
                cohort_code=cohort.code,
                cohort_name=cohort.name,
                value=cohort_reading.value,
                target=cohort_target,
                achievement_pct=cohort_pct,
                status=status_for(cohort_pct) if cohort_target is not None else "No target",
                contribution_pct=(
                    round(cohort_basis / contribution_total * 100.0, 2)
                    if contribution_total
                    else None
                ),
                states_reporting=sum(
                    1 for row in state_rows if row.cohort_code == cohort.code and row.reported
                ),
                states_expected=len(members),
                average_state_achievement_pct=(
                    round(fmean(member_achievements), 2) if member_achievements else None
                ),
            )
        )

    state_rows.sort(
        key=lambda row: (row.achievement_pct is None, -(row.achievement_pct or 0), row.state_name)
    )

    return IndicatorAnalysis(
        indicator=indicator_ref(indicator),
        period_code=period.code,
        national=national,
        cohorts=cohort_rows,
        states=state_rows,
    )


# --------------------------------------------------------------------------
# Cross-sectional scorecards
# --------------------------------------------------------------------------
def scorecard(
    db: Session,
    period: ReportingPeriod,
    *,
    scope: str = "NATIONAL",
    state_code: str | None = None,
    cohort_code: str | None = None,
    indicators: list[Indicator] | None = None,
) -> PerformanceScorecard:
    """Every indicator for one scope in one period."""
    indicators = indicators or reference.active_indicators(db)
    scope = scope.upper()

    if scope == "STATE":
        state = reference.get_state_by_code(db, state_code or "")
        states = [state]
        scope_ref, scope_label = state.code, state.name
    elif scope == "COHORT":
        cohort = reference.get_cohort_by_code(db, cohort_code or "")
        states = reference.active_states(db, cohort.code)
        scope_ref, scope_label = cohort.code, cohort.name
    else:
        states = reference.active_states(db)
        scope_ref, scope_label = None, "National"

    state_ids = [state.id for state in states]
    readings = readings_for_period(db, period.id, indicators, state_ids)
    state_targets, national_targets = targets_for_period(db, period.id)

    rows: list[ScorecardRow] = []
    achievements: list[float] = []
    with_data = with_target = on_track = 0

    for indicator in indicators:
        if scope == "STATE":
            state = states[0]
            reading = readings.get(state.id, {}).get(indicator.id, Reading())
            target = state_targets.get((state.id, indicator.id))
        else:
            reading = aggregate_national(
                [readings.get(s.id, {}).get(indicator.id, Reading()) for s in states], indicator
            )
            target = national_targets.get(indicator.id) if scope == "NATIONAL" else None
            if target is None:
                applicable = [
                    value
                    for (sid, iid), value in state_targets.items()
                    if iid == indicator.id and sid in set(state_ids)
                ]
                if applicable:
                    method = AggregationMethod(indicator.aggregation_method)
                    target = (
                        sum(applicable)
                        if method == AggregationMethod.SUM
                        else round(fmean(applicable), 4)
                    )

        pct = achievement(reading.value, target, indicator)
        if reading.has_data:
            with_data += 1
        if target is not None:
            with_target += 1
        if pct is not None:
            achievements.append(pct)
            if pct >= 90:
                on_track += 1

        rows.append(
            ScorecardRow(
                indicator=indicator_ref(indicator),
                value=reading.value,
                target=target,
                achievement_pct=pct,
                status=_row_status(pct, target, reading.has_data),
            )
        )

    return PerformanceScorecard(
        scope=scope,
        scope_ref=scope_ref,
        scope_label=scope_label,
        period_code=period.code,
        rows=rows,
        indicators_with_data=with_data,
        indicators_with_target=with_target,
        indicators_on_track=on_track,
        average_achievement_pct=round(fmean(achievements), 2) if achievements else None,
    )


# --------------------------------------------------------------------------
# Layer 3 as a standalone view
# --------------------------------------------------------------------------
def contribution_analysis(
    db: Session, indicator: Indicator, period: ReportingPeriod
) -> ContributionAnalysis:
    analysis = analyse_indicator(db, indicator, period)
    rows = [
        ContributionRow(
            state_code=row.state_code,
            state_name=row.state_name,
            cohort_code=row.cohort_code,
            value=row.value,
            contribution_pct=row.contribution_pct,
        )
        for row in analysis.states
        if row.contribution_pct is not None
    ]
    rows.sort(key=lambda row: -(row.contribution_pct or 0))
    for rank, row in enumerate(rows, start=1):
        row.rank = rank

    return ContributionAnalysis(
        indicator=analysis.indicator,
        period_code=period.code,
        national_value=analysis.national.value,
        rows=rows,
    )


# --------------------------------------------------------------------------
# Longitudinal analysis
# --------------------------------------------------------------------------
def recent_periods(
    db: Session,
    period_type: str | None = None,
    state_ids: list[int] | None = None,
    limit: int = 8,
) -> list[ReportingPeriod]:
    """The last ``limit`` periods of one family that are worth plotting.

    A series must stay inside one period family: comparing a quarter with an
    annual figure would be meaningless. Reporting calendars are also generated a
    year or more ahead, so the tail of the list is usually periods that have not
    happened yet; trailing periods with neither approved data nor a target are
    dropped rather than drawn as empty future columns.
    """
    if not period_type:
        latest = reference.latest_period(db)
        period_type = latest.period_type if latest else None

    candidates = reference.ordered_periods(db, period_type)
    trimmed = list(candidates)
    while trimmed:
        last = trimmed[-1]
        has_data = bool(analysable_submissions(db, last.id, state_ids))
        has_target = db.scalar(
            select(Target.id).where(Target.period_id == last.id).limit(1)
        ) is not None
        if has_data or has_target:
            break
        trimmed.pop()

    # Nothing in this family has data yet: show the recent calendar rather than
    # an empty series, so the caller can see the periods exist.
    return (trimmed or candidates)[-limit:]


def trend(
    db: Session,
    indicator: Indicator,
    *,
    scope: str = "NATIONAL",
    state_code: str | None = None,
    cohort_code: str | None = None,
    period_type: str | None = None,
    limit: int = 8,
) -> TrendSeries:
    """Longitudinal series for one indicator at one scope."""
    scope = scope.upper()
    if scope == "STATE":
        state = reference.get_state_by_code(db, state_code or "")
        states = [state]
        scope_ref, scope_label = state.code, state.name
    elif scope == "COHORT":
        cohort = reference.get_cohort_by_code(db, cohort_code or "")
        states = reference.active_states(db, cohort.code)
        scope_ref, scope_label = cohort.code, cohort.name
    else:
        states = reference.active_states(db)
        scope_ref, scope_label = None, "National"

    state_ids = [state.id for state in states]
    periods = recent_periods(db, period_type, state_ids, limit)

    points: list[TrendPoint] = []
    for period in periods:
        readings = readings_for_period(db, period.id, [indicator], state_ids)
        per_state = [readings.get(sid, {}).get(indicator.id, Reading()) for sid in state_ids]
        consolidated = aggregate_national(per_state, indicator)

        state_targets, national_targets = targets_for_period(db, period.id)
        if scope == "STATE":
            target = state_targets.get((states[0].id, indicator.id))
        else:
            target = national_targets.get(indicator.id)
            if target is None:
                applicable = [
                    value
                    for (sid, iid), value in state_targets.items()
                    if iid == indicator.id and sid in set(state_ids)
                ]
                if applicable:
                    method = AggregationMethod(indicator.aggregation_method)
                    target = (
                        sum(applicable)
                        if method == AggregationMethod.SUM
                        else round(fmean(applicable), 4)
                    )

        points.append(
            TrendPoint(
                period_code=period.code,
                period_label=period.label,
                fiscal_year=period.fiscal_year,
                sequence=period.sequence,
                value=consolidated.value,
                target=target,
                achievement_pct=achievement(consolidated.value, target, indicator),
                states_reporting=sum(1 for reading in per_state if reading.has_data),
            )
        )

    populated = [point for point in points if point.value is not None]
    change_pct = None
    direction_of_travel = "flat"
    if len(populated) >= 2:
        first, last = populated[0].value, populated[-1].value
        if first:
            change_pct = round((last - first) / abs(first) * 100.0, 2)
        improving = last > first
        if Direction(indicator.direction) == Direction.DECREASE:
            improving = last < first
        if first != last:
            direction_of_travel = "improving" if improving else "deteriorating"

    return TrendSeries(
        indicator=indicator_ref(indicator),
        scope=scope,
        scope_ref=scope_ref,
        scope_label=scope_label,
        points=points,
        change_pct=change_pct,
        direction_of_travel=direction_of_travel,
    )


# --------------------------------------------------------------------------
# Heatmaps
# --------------------------------------------------------------------------
def heatmap(
    db: Session,
    period: ReportingPeriod,
    *,
    metric: str = "achievement",
    indicators: list[Indicator] | None = None,
    cohort_code: str | None = None,
    limit_indicators: int = 20,
) -> Heatmap:
    """States (rows) x indicators (columns) achievement heatmap."""
    states = reference.active_states(db, cohort_code)
    indicators = (indicators or reference.active_indicators(db))[:limit_indicators]
    state_ids = [state.id for state in states]
    readings = readings_for_period(db, period.id, indicators, state_ids)
    state_targets, _ = targets_for_period(db, period.id)

    cells: list[HeatmapCell] = []
    for state in states:
        for indicator in indicators:
            reading = readings.get(state.id, {}).get(indicator.id, Reading())
            if metric == "value":
                cell_value = reading.value
            else:
                cell_value = achievement(
                    reading.value, state_targets.get((state.id, indicator.id)), indicator
                )
            cells.append(
                HeatmapCell(
                    row_key=state.code,
                    column_key=indicator.code,
                    value=None if cell_value is None else round(cell_value, 2),
                    label=None if cell_value is None else f"{cell_value:,.1f}",
                )
            )

    return Heatmap(
        metric=metric,
        rows=[state.code for state in states],
        row_labels={state.code: state.name for state in states},
        columns=[indicator.code for indicator in indicators],
        column_labels={indicator.code: f"{indicator.code}: {indicator.name}" for indicator in indicators},
        cells=cells,
    )
