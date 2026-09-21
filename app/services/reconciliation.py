"""Reconciles the monthly performance tracker against the quarterly framework.

The AGILE states report the same 53 indicators twice: monthly through the
performance tracker and quarterly through the results framework. The NPCU's
rule is that the two must agree -- and today they do not. Comparing the July
tracker against the Q2 framework return turns up 63 state-indicator pairs where
the tracker is *below* the quarter it is supposed to add up to, one indicator
sitting at zero in twelve states at once, and a rate indicator reported as a
count in another.

Reconciling them is not a subtraction, because a quarter is not built from its
months the same way for every indicator:

* "JSS classrooms constructed" is **cumulative**: each month's tracker figure is
  already a running total, so the quarter equals *June*, not April + May + June.
  Adding the months triple-counts the quarter.
* "Students enrolled" is a **position at the period's end**. It is not
  cumulative since the project began, but it does not add across months either:
  a child enrolled in April is still enrolled in June.
* "% of schools implementing the code of conduct" is a **rate**: the quarter is
  the position at quarter end, and averaging three monthly rates is not it.
* "Is the scholarship programme operational?" is a **status**: the quarter is
  the latest answer.
* A genuine **within-period flow** -- something counted afresh each month --
  would be the one case where the months add up. Nothing in the current 53 is
  one, so summing is opt-in per indicator rather than inferred.

So each indicator is reconciled on its own time basis (see
:class:`~app.core.enums.TimeBasis`), derived from the catalogue unless the
catalogue overrides it, and every line states in words what it expected and
why -- because the state resolving the query is the one who has to act on it.

Both streams are matched on **indicator code**. The tracker currently matches on
indicator name, and only 28 of its 49 rows share exact wording with the
framework's 53, which is why reconciliation had to wait for the recode.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    RATE_UNITS,
    TIME_BASES_NEEDING_EVERY_PART,
    UNRECONCILED_STATUSES,
    AggregationMethod,
    IndicatorUnit,
    PeriodType,
    ReconciliationStatus,
    TimeBasis,
)
from app.core.formatting import fmt
from app.core.logging_config import get_logger
from app.models import (
    Indicator,
    IndicatorValue,
    ReportingPeriod,
    State,
    StateSubcomponent,
    Submission,
)

logger = get_logger(__name__)

#: Families from coarsest to finest. A period is reconciled against the family
#: one step finer than itself unless the caller names another.
PERIOD_GRAIN: list[PeriodType] = [
    PeriodType.ANNUAL,
    PeriodType.SEMI_ANNUAL,
    PeriodType.QUARTERLY,
    PeriodType.MONTHLY,
]

#: Counts must agree exactly; this only absorbs binary floating point.
COUNT_EPSILON = 1e-6
#: Percentage points of slack allowed on a rate, for rounding alone.
DEFAULT_RATE_TOLERANCE = 0.1


# --------------------------------------------------------------------------
# Time basis
# --------------------------------------------------------------------------
def time_basis(indicator: Indicator) -> TimeBasis:
    """How this indicator's coarser figure is built from its finer ones.

    The catalogue's own ``time_basis`` wins where it is set. Otherwise the
    basis is derived, and the derivation defaults to SNAPSHOT: in this
    framework every figure is a position -- either a total since the project
    began or a count as at the period's end -- and none of the 53 is a flow
    counted afresh each month.

    Defaulting the other way was tempting, because ``is_cumulative`` is False
    on fourteen of them. But False there means "not a running total since
    inception", not "a within-period flow", and reading those as sums would
    have told eighteen states that their quarterly enrolment should equal
    April + May + June -- roughly three times the truth, on every one of them.
    A wrong SNAPSHOT misses a discrepancy; a wrong SUM manufactures hundreds.
    So SUM is opt-in, one cell in the catalogue, and reviewable.
    """
    override = (indicator.time_basis or "").strip().upper()
    if override:
        try:
            return TimeBasis(override)
        except ValueError:
            logger.warning(
                "unknown time basis on an indicator; deriving instead",
                extra={"indicator": indicator.code, "time_basis": override},
            )

    try:
        unit = IndicatorUnit(indicator.unit)
    except ValueError:
        unit = IndicatorUnit.NUMBER
    try:
        method = AggregationMethod(indicator.aggregation_method)
    except ValueError:
        method = AggregationMethod.SUM

    # A Yes/No status is whatever the answer is now.
    if unit is IndicatorUnit.BOOLEAN:
        return TimeBasis.LATEST
    # MAX/MIN describe the figure itself, not the way states combine, so they
    # carry across to the time axis unchanged.
    if method is AggregationMethod.MAX:
        return TimeBasis.MAX
    if method is AggregationMethod.MIN:
        return TimeBasis.MIN
    # A running total is answered by its last part; adding the parts would count
    # everything before the final month two or three times over. A rate and a
    # point-in-time count are read at the period's end for the same reason.
    return TimeBasis.SNAPSHOT


def fold(basis: TimeBasis, series: list[float]) -> float | None:
    """Combine an oldest-first series of finer figures into the coarser one."""
    if not series:
        return None
    if basis in {TimeBasis.SNAPSHOT, TimeBasis.LATEST}:
        return series[-1]
    if basis is TimeBasis.MAX:
        return max(series)
    if basis is TimeBasis.MIN:
        return min(series)
    return float(sum(series))


def tolerance_for(indicator: Indicator, rate_tolerance: float = DEFAULT_RATE_TOLERANCE) -> float:
    """How far apart the two streams may be before it is a discrepancy."""
    try:
        unit = IndicatorUnit(indicator.unit)
    except ValueError:
        unit = IndicatorUnit.NUMBER
    return rate_tolerance if unit in RATE_UNITS else COUNT_EPSILON


# --------------------------------------------------------------------------
# Period decomposition
# --------------------------------------------------------------------------
def finer_grain(period_type: PeriodType) -> PeriodType | None:
    """The family one step finer than ``period_type``, if there is one."""
    try:
        index = PERIOD_GRAIN.index(PeriodType(period_type))
    except ValueError:
        return None
    return PERIOD_GRAIN[index + 1] if index + 1 < len(PERIOD_GRAIN) else None


def enclosed_periods(
    db: Session, period: ReportingPeriod, child_type: PeriodType | None = None
) -> list[ReportingPeriod]:
    """The finer periods that fall entirely inside ``period``, oldest first."""
    wanted = child_type or finer_grain(PeriodType(period.period_type))
    if wanted is None:
        return []
    rows = db.scalars(
        select(ReportingPeriod).where(
            ReportingPeriod.period_type == str(wanted),
            ReportingPeriod.start_date >= period.start_date,
            ReportingPeriod.end_date <= period.end_date,
        )
    )
    return sorted(rows, key=lambda row: row.sort_key)


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------
#: Bases a mismatch is re-tested against, to catch one configured the wrong way.
ALTERNATIVE_BASES = [TimeBasis.SNAPSHOT, TimeBasis.SUM, TimeBasis.LATEST]


@dataclass
class ReconciliationLine:
    """One indicator, judged across the two streams."""

    indicator_id: int
    indicator_code: str
    indicator_name: str
    subcomponent_code: str | None
    unit: str
    basis: TimeBasis
    coarse_value: float | None
    fine_value: float | None
    status: ReconciliationStatus
    note: str
    parts_expected: list[str] = field(default_factory=list)
    parts_reported: list[str] = field(default_factory=list)
    part_values: dict[str, float] = field(default_factory=dict)
    #: The figure carries an open finding. It still counts towards every
    #: total -- the label says what to trust, not what was counted.
    is_flagged: bool = False
    #: A basis under which this line would have reconciled exactly. Set only on
    #: a mismatch, and it means the indicator's own basis may be wrong rather
    #: than the figure.
    reconciles_as: TimeBasis | None = None

    @property
    def variance(self) -> float | None:
        if self.coarse_value is None or self.fine_value is None:
            return None
        return self.coarse_value - self.fine_value

    @property
    def variance_pct(self) -> float | None:
        variance = self.variance
        if variance is None or not self.fine_value:
            return None
        return round(100.0 * variance / abs(self.fine_value), 2)

    @property
    def needs_attention(self) -> bool:
        return self.status in UNRECONCILED_STATUSES


@dataclass
class StateReconciliation:
    """Every line for one state and one coarser period."""

    state_id: int
    state_code: str
    state_name: str
    cohort_code: str | None
    period_code: str
    period_label: str
    parts_expected: list[str]
    parts_reported: list[str]
    coarse_submission_id: int | None
    fine_submission_ids: list[int]
    lines: list[ReconciliationLine]

    def count(self, status: ReconciliationStatus) -> int:
        return sum(1 for line in self.lines if line.status is status)

    @property
    def judged(self) -> int:
        """Lines a verdict could be reached on."""
        return sum(
            1 for line in self.lines if line.status is not ReconciliationStatus.INCOMPLETE
        )

    @property
    def unreconciled(self) -> list[ReconciliationLine]:
        return [line for line in self.lines if line.needs_attention]

    @property
    def agreement(self) -> float | None:
        """Share of judged lines where the two streams agree."""
        if not self.judged:
            return None
        return round(100.0 * self.count(ReconciliationStatus.MATCHED) / self.judged, 1)

    @property
    def is_complete(self) -> bool:
        return bool(self.parts_expected) and set(self.parts_reported) == set(self.parts_expected)


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------
def _current_submission(db: Session, state_id: int, period_id: int) -> Submission | None:
    return db.scalar(
        select(Submission).where(
            Submission.state_id == state_id,
            Submission.period_id == period_id,
            Submission.is_current.is_(True),
        )
    )


def _figures(db: Session, submission: Submission | None) -> dict[int, IndicatorValue]:
    if submission is None:
        return {}
    return {
        value.indicator_id: value
        for value in db.scalars(
            select(IndicatorValue).where(IndicatorValue.submission_id == submission.id)
        )
    }


def _applicable_indicator_ids(db: Session, state_id: int, indicators: list[Indicator]) -> set[int]:
    """Indicators in the sub-components this state implements; empty means all."""
    implemented = set(
        db.scalars(
            select(StateSubcomponent.subcomponent_id).where(
                StateSubcomponent.state_id == state_id,
                StateSubcomponent.implements.is_(True),
            )
        )
    )
    if not implemented:
        return set()
    return {
        indicator.id
        for indicator in indicators
        if indicator.subcomponent_id in implemented
    }


def _describe(
    line_basis: TimeBasis,
    indicator: Indicator,
    parts_reported: list[str],
    part_values: dict[str, float],
    fine_value: float | None,
    coarse_value: float | None,
    status: ReconciliationStatus,
    alternative: TimeBasis | None = None,
) -> str:
    """Say in words what was expected and why, for whoever resolves the query."""
    code = indicator.code
    if status is ReconciliationStatus.INCOMPLETE:
        missing = "no month has been reported yet" if not parts_reported else (
            f"only {', '.join(parts_reported)} {'has' if len(parts_reported) == 1 else 'have'} "
            "been reported"
        )
        return (
            f"{code} is reconciled by adding its months together, so a verdict needs all of "
            f"them; {missing}."
        )
    if status is ReconciliationStatus.TRACKER_MISSING:
        return (
            f"{code} was reported for the quarter but never appears in the tracker, so there "
            "is nothing to reconcile it against."
        )
    if status is ReconciliationStatus.FRAMEWORK_MISSING:
        return (
            f"{code} was reported in the tracker but left out of the quarterly return."
        )

    if line_basis is TimeBasis.SNAPSHOT:
        last = parts_reported[-1] if parts_reported else "the last month"
        if indicator.is_cumulative:
            expectation = (
                f"{code} is a running total, so the quarter should equal {last}'s figure of "
                f"{fmt(fine_value)}"
            )
        elif indicator.unit in {str(unit) for unit in RATE_UNITS}:
            expectation = (
                f"{code} is a rate, so the quarter should be the position at {last}: "
                f"{fmt(fine_value)}"
            )
        else:
            expectation = (
                f"{code} is reported as at the period's end, so the quarter should equal "
                f"{last}'s figure of {fmt(fine_value)}"
            )
    elif line_basis is TimeBasis.LATEST:
        last = parts_reported[-1] if parts_reported else "the last month"
        expectation = f"{code} is a status, so the quarter should be {last}'s answer"
    elif line_basis is TimeBasis.SUM:
        arithmetic = " + ".join(f"{fmt(part_values[part])}" for part in parts_reported)
        expectation = (
            f"{code} counts what happened within the period, so the quarter should equal "
            f"{arithmetic} = {fmt(fine_value)}"
        )
    else:
        expectation = (
            f"{code} is reconciled as the {line_basis.value.lower()} of its months: "
            f"{fmt(fine_value)}"
        )

    if status is ReconciliationStatus.MATCHED:
        return f"{expectation}. The quarterly return agrees."

    note = f"{expectation}. The quarterly return says {fmt(coarse_value)}."
    if alternative is not None:
        reading = {
            TimeBasis.SUM: "its months added together",
            TimeBasis.SNAPSHOT: "the last month reported",
            TimeBasis.LATEST: "the most recent answer",
            TimeBasis.MAX: "the highest month",
            TimeBasis.MIN: "the lowest month",
        }[alternative]
        note += (
            f" It reconciles exactly if {code} is read as {reading} instead, so the "
            f"figure may be right and its time basis wrong."
        )
    return note


def _basis_that_would_agree(
    basis: TimeBasis,
    series: list[float],
    coarse_value: float | None,
    tolerance: float,
) -> TimeBasis | None:
    """A different basis that reconciles this line exactly, if one does.

    A figure can disagree because it is wrong, or because the platform is
    reading it the wrong way -- adding months that are already running totals,
    or taking the last month of something counted afresh each month. The two
    look identical in a mismatch report, and only one of them is the state's
    problem. Where the arithmetic works out exactly under another basis, that
    is worth saying: it points at the catalogue rather than at the return.
    """
    if coarse_value is None or len(series) < 2:
        return None
    for candidate in ALTERNATIVE_BASES:
        if candidate is basis:
            continue
        folded = fold(candidate, series)
        if folded is not None and abs(coarse_value - folded) <= tolerance:
            return candidate
    return None


def reconcile_state(
    db: Session,
    state: State,
    period: ReportingPeriod,
    *,
    indicators: list[Indicator] | None = None,
    child_type: PeriodType | None = None,
    rate_tolerance: float = DEFAULT_RATE_TOLERANCE,
    include_unreported: bool = False,
) -> StateReconciliation:
    """Reconcile one state's coarser return against its finer returns."""
    catalogue = indicators if indicators is not None else list(
        db.scalars(select(Indicator).where(Indicator.is_active.is_(True)).order_by(Indicator.number))
    )
    reportable = [indicator for indicator in catalogue if indicator.is_reported]
    applicable = _applicable_indicator_ids(db, state.id, reportable)
    if applicable:
        reportable = [indicator for indicator in reportable if indicator.id in applicable]

    parts = enclosed_periods(db, period, child_type)
    coarse_submission = _current_submission(db, state.id, period.id)
    coarse_figures = _figures(db, coarse_submission)

    fine_submissions: list[Submission] = []
    fine_figures: dict[str, dict[int, IndicatorValue]] = {}
    for part in parts:
        submission = _current_submission(db, state.id, part.id)
        if submission is None:
            continue
        fine_submissions.append(submission)
        fine_figures[part.code] = _figures(db, submission)

    parts_expected = [part.code for part in parts]
    parts_reported = [code for code in parts_expected if code in fine_figures]
    final_part = parts_expected[-1] if parts_expected else None

    lines: list[ReconciliationLine] = []
    # A state that has filed no tracker return at all has nothing to reconcile.
    # Reporting it as 53 indicators "missing from the tracker" would be true and
    # useless: it would drown the handful of real disagreements and drag the
    # national agreement figure down to a number about who has started, not
    # about whether the figures agree. Who has started is `parts_reported`.
    if not fine_submissions:
        return StateReconciliation(
            state_id=state.id,
            state_code=state.code,
            state_name=state.name,
            cohort_code=state.cohort.code if state.cohort else None,
            period_code=period.code,
            period_label=period.label,
            parts_expected=parts_expected,
            parts_reported=[],
            coarse_submission_id=coarse_submission.id if coarse_submission else None,
            fine_submission_ids=[],
            lines=[],
        )

    for indicator in reportable:
        coarse_row = coarse_figures.get(indicator.id)
        coarse_value = coarse_row.effective_value if coarse_row else None

        basis = time_basis(indicator)
        contributing: list[str] = []
        part_values: dict[str, float] = {}
        for code in parts_reported:
            row = fine_figures[code].get(indicator.id)
            value = row.effective_value if row else None
            if value is None:
                continue
            contributing.append(code)
            part_values[code] = value

        fine_value = fold(basis, [part_values[code] for code in contributing])

        if coarse_value is None and fine_value is None:
            if not include_unreported:
                continue
            status = ReconciliationStatus.INCOMPLETE
        elif fine_value is None:
            status = ReconciliationStatus.TRACKER_MISSING
        elif basis in TIME_BASES_NEEDING_EVERY_PART and contributing != parts_expected:
            # A partial sum is always short of the whole, so calling it a
            # discrepancy would invent one out of a late tracker return.
            status = ReconciliationStatus.INCOMPLETE
        elif basis not in TIME_BASES_NEEDING_EVERY_PART and final_part not in part_values:
            # A snapshot is answered by the final period alone -- but only if
            # that period is actually in.
            status = ReconciliationStatus.INCOMPLETE
        elif coarse_value is None:
            status = ReconciliationStatus.FRAMEWORK_MISSING
        elif abs(coarse_value - fine_value) <= tolerance_for(indicator, rate_tolerance):
            status = ReconciliationStatus.MATCHED
        else:
            status = ReconciliationStatus.MISMATCH

        alternative = (
            _basis_that_would_agree(
                basis,
                [part_values[code] for code in contributing],
                coarse_value,
                tolerance_for(indicator, rate_tolerance),
            )
            if status is ReconciliationStatus.MISMATCH
            else None
        )

        lines.append(
            ReconciliationLine(
                indicator_id=indicator.id,
                indicator_code=indicator.code,
                indicator_name=indicator.name,
                subcomponent_code=(
                    indicator.subcomponent.code if indicator.subcomponent else None
                ),
                unit=indicator.unit,
                basis=basis,
                coarse_value=coarse_value,
                fine_value=fine_value,
                status=status,
                note=_describe(
                    basis,
                    indicator,
                    contributing,
                    part_values,
                    fine_value,
                    coarse_value,
                    status,
                    alternative,
                ),
                parts_expected=parts_expected,
                parts_reported=contributing,
                part_values=part_values,
                is_flagged=bool(coarse_row and not coarse_row.is_valid),
                reconciles_as=alternative,
            )
        )

    return StateReconciliation(
        state_id=state.id,
        state_code=state.code,
        state_name=state.name,
        cohort_code=state.cohort.code if state.cohort else None,
        period_code=period.code,
        period_label=period.label,
        parts_expected=parts_expected,
        parts_reported=parts_reported,
        coarse_submission_id=coarse_submission.id if coarse_submission else None,
        fine_submission_ids=[row.id for row in fine_submissions],
        lines=lines,
    )


def reconcile_period(
    db: Session,
    period: ReportingPeriod,
    *,
    state_ids: list[int] | None = None,
    child_type: PeriodType | None = None,
    rate_tolerance: float = DEFAULT_RATE_TOLERANCE,
) -> list[StateReconciliation]:
    """Reconcile every reporting state for one coarser period."""
    stmt = select(State).where(State.is_active.is_(True)).order_by(State.name)
    if state_ids is not None:
        stmt = stmt.where(State.id.in_(state_ids or [0]))
    states = list(db.scalars(stmt))

    indicators = list(
        db.scalars(select(Indicator).where(Indicator.is_active.is_(True)).order_by(Indicator.number))
    )
    return [
        reconcile_state(
            db,
            state,
            period,
            indicators=indicators,
            child_type=child_type,
            rate_tolerance=rate_tolerance,
        )
        for state in states
    ]


def national_summary(results: list[StateReconciliation]) -> dict:
    """Headline counts across states, for the dashboard and the report."""
    lines = [line for result in results for line in result.lines]
    judged = [line for line in lines if line.status is not ReconciliationStatus.INCOMPLETE]
    matched = sum(1 for line in judged if line.status is ReconciliationStatus.MATCHED)
    by_status: dict[str, int] = {}
    for line in lines:
        by_status[str(line.status)] = by_status.get(str(line.status), 0) + 1

    return {
        "states": len(results),
        "states_reporting": sum(1 for result in results if result.coarse_submission_id),
        # Agreement below is computed only over states that have filed a tracker
        # return, so this is the denominator that makes it meaningful.
        "states_tracking": sum(1 for result in results if result.parts_reported),
        "states_with_complete_tracker": sum(1 for result in results if result.is_complete),
        "lines": len(lines),
        "judged": len(judged),
        "matched": matched,
        "agreement": round(100.0 * matched / len(judged), 1) if judged else None,
        "by_status": by_status,
        "states_unreconciled": sorted(
            result.state_code for result in results if result.unreconciled
        ),
    }
