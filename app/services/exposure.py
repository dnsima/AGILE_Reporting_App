"""How much of a result rests on figures the platform doubts.

Nothing here changes a number. Every reported figure counts towards every
total; this module says how much of that total is standing on ground the
validation rules could not vouch for, and how far off it is likely to be.

The measure that matters is the size of the *error*, not the size of the
figure. Q2 2026 makes the distinction plainly:

* Gombe reported 127 schools operating both a Code of Conduct and a GBV/SEA
  mechanism, against 5,960 in Q1, on a cumulative indicator. The figure is
  small; the error is 5,833 and it drives most of the national movement.
* Borno reported 1,024,306 on direct community reach against 1,024,314 in Q1.
  The figure is enormous; the error is eight, and is almost certainly rounding.

Weighting by the figure would rank Borno far above Gombe. Weighting by the
error ranks them correctly, which is what ``distortion`` does below.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import DisclosureStatus, Severity, verdict_for_submission
from app.models import (
    Indicator,
    IndicatorValue,
    ReportingPeriod,
    State,
    Submission,
    ValidationIssue,
)
from app.services import disclosure

#: Below this share of the national total, a distortion is noise rather than a
#: finding worth putting in front of the NPCU. Borno's eight lands here.
IMMATERIAL_SHARE = 0.5

#: At or above this share of an indicator's national total, one state's error
#: is large enough that the national figure should not be read without it.
MATERIAL_SHARE = 5.0


@dataclass
class FigureExposure:
    """One doubted figure, and what it does to the national result."""

    state_code: str
    state_name: str
    indicator_code: str
    indicator_name: str
    reported: float | None
    expected: float | None
    status: DisclosureStatus
    rule_code: str
    severity: str
    is_blocking: bool
    reason: str

    @property
    def distortion(self) -> float | None:
        """How far the figure is likely to be out, in the indicator's own unit."""
        if self.reported is None or self.expected is None:
            return None
        return self.expected - self.reported

    @property
    def direction(self) -> str:
        """Which way the national figure is pulled, in plain words."""
        gap = self.distortion
        if gap is None:
            return "of unknown size and direction"
        if gap > 0:
            return "understated"
        if gap < 0:
            return "overstated"
        return "unaffected"


@dataclass
class IndicatorExposure:
    """An indicator's national figure, and how much of it is in doubt."""

    indicator_code: str
    indicator_name: str
    reported_total: float
    figures: list[FigureExposure] = field(default_factory=list)

    @property
    def exposed_value(self) -> float:
        """The part of the national total contributed by doubted figures."""
        return sum(f.reported or 0.0 for f in self.figures)

    @property
    def exposed_share(self) -> float:
        """That part as a percentage of the national total."""
        if not self.reported_total:
            return 0.0
        return 100.0 * self.exposed_value / self.reported_total

    @property
    def net_distortion(self) -> float | None:
        """Net amount the national total is likely out by, where known.

        One figure flagged by two rules is one error, not two, so the largest
        gap claimed against a figure is the one that counts. Summing them would
        charge the same wrong number twice over.
        """
        worst: dict[tuple[str, str], float] = {}
        for figure in self.figures:
            gap = figure.distortion
            if gap is None:
                continue
            key = (figure.state_code, figure.indicator_code)
            if key not in worst or abs(gap) > abs(worst[key]):
                worst[key] = gap
        return sum(worst.values()) if worst else None

    @property
    def distortion_share(self) -> float | None:
        """The likely error as a percentage of the national total."""
        gap = self.net_distortion
        if gap is None or not self.reported_total:
            return None
        return 100.0 * abs(gap) / self.reported_total

    @property
    def is_material(self) -> bool:
        """Whether the national figure should not be read without a caveat."""
        share = self.distortion_share
        return share is not None and share >= MATERIAL_SHARE

    @property
    def figure_count(self) -> int:
        """Distinct figures in doubt, not findings: two rules can flag one."""
        return len({(f.state_code, f.indicator_code) for f in self.figures})

    def note(self) -> str | None:
        """The disclosure line to carry wherever this national figure appears."""
        if not self.figures:
            return None
        codes = sorted({f.state_code for f in self.figures})
        count = self.figure_count
        noun = "figure" if count == 1 else "figures"
        states = ", ".join(codes)
        gap = self.net_distortion
        if gap is None or abs(gap) < 1:
            return (
                f"{count} {noun} under query ({states}), "
                f"{self.exposed_share:.1f}% of the national total."
            )
        direction = "understated" if gap > 0 else "overstated"
        return (
            f"{count} {noun} under query ({states}). The national total is "
            f"{direction} by up to {abs(gap):,.0f} "
            f"({abs(self.distortion_share or 0):.1f}%)."
        )


@dataclass
class StateExposure:
    """A state's contribution to the national picture, and the doubt in it."""

    state_code: str
    state_name: str
    figures_reported: int
    figures_unfit: int
    figures_queried: int
    #: Share of this state's weight in the national totals that is in doubt,
    #: each figure weighted by the size of its likely error.
    exposed_share: float
    #: The indicators where this state's error is material nationally.
    material_findings: list[FigureExposure] = field(default_factory=list)
    #: The verdict recorded on the return, once the period has been settled.
    fitness: str | None = None

    @property
    def has_blocking(self) -> bool:
        return self.figures_unfit > 0


def _expected_value(issue: ValidationIssue) -> float | None:
    """The value the rule says the figure should have carried, if it said."""
    context = issue.context or {}
    raw = context.get("expected_value")
    if isinstance(raw, (int, float)):
        return float(raw)
    return None


def _national_totals(
    db: Session, period: ReportingPeriod, indicator_ids: list[int]
) -> dict[int, float]:
    """As-reported national total per indicator. Nothing is held out."""
    totals: dict[int, float] = defaultdict(float)
    submission_ids = [
        row
        for row in db.scalars(
            select(Submission.id).where(
                Submission.period_id == period.id, Submission.is_current.is_(True)
            )
        )
    ]
    if not submission_ids:
        return {}
    for value in db.scalars(
        select(IndicatorValue).where(
            IndicatorValue.submission_id.in_(submission_ids),
            IndicatorValue.indicator_id.in_(indicator_ids),
        )
    ):
        if value.value is not None:
            totals[value.indicator_id] += value.value
    return dict(totals)


def _collect(
    db: Session, period: ReportingPeriod
) -> tuple[list[FigureExposure], dict[int, float], dict[int, Indicator]]:
    """Every doubted figure in a period, with the national totals behind them."""
    submissions = list(
        db.scalars(
            select(Submission).where(
                Submission.period_id == period.id, Submission.is_current.is_(True)
            )
        )
    )
    if not submissions:
        return [], {}, {}

    states = {s.id: s for s in db.scalars(select(State))}
    indicators = {i.id: i for i in db.scalars(select(Indicator))}
    sub_ids = [s.id for s in submissions]

    values: dict[tuple[int, int], IndicatorValue] = {}
    for value in db.scalars(
        select(IndicatorValue).where(IndicatorValue.submission_id.in_(sub_ids))
    ):
        values[(value.submission_id, value.indicator_id)] = value

    state_of = {s.id: s.state_id for s in submissions}
    found: list[FigureExposure] = []
    seen: set[tuple[int, int, str]] = set()

    for issue in db.scalars(
        select(ValidationIssue).where(ValidationIssue.submission_id.in_(sub_ids))
    ):
        if issue.indicator_id is None:
            continue
        if Severity(issue.severity) not in (Severity.ERROR, Severity.WARNING):
            continue
        key = (issue.submission_id, issue.indicator_id, issue.rule_code)
        if key in seen:
            continue
        seen.add(key)

        value = values.get((issue.submission_id, issue.indicator_id))
        indicator = indicators.get(issue.indicator_id)
        state = states.get(state_of.get(issue.submission_id, -1))
        if value is None or indicator is None or state is None:
            continue

        found.append(
            FigureExposure(
                state_code=state.code,
                state_name=state.name,
                indicator_code=indicator.code,
                indicator_name=indicator.name,
                reported=value.value,
                expected=_expected_value(issue),
                status=disclosure.status_of(value),
                rule_code=issue.rule_code,
                severity=issue.severity,
                is_blocking=bool(issue.is_blocking),
                reason=issue.message,
            )
        )

    totals = _national_totals(db, period, list(indicators))
    return found, totals, indicators


def by_indicator(db: Session, period: ReportingPeriod) -> list[IndicatorExposure]:
    """Per-indicator exposure for a period, heaviest likely distortion first."""
    found, totals, indicators = _collect(db, period)
    by_code: dict[str, IndicatorExposure] = {}
    code_to_id = {i.code: i.id for i in indicators.values()}

    for figure in found:
        entry = by_code.get(figure.indicator_code)
        if entry is None:
            indicator_id = code_to_id.get(figure.indicator_code)
            entry = IndicatorExposure(
                indicator_code=figure.indicator_code,
                indicator_name=figure.indicator_name,
                reported_total=totals.get(indicator_id, 0.0) if indicator_id else 0.0,
            )
            by_code[figure.indicator_code] = entry
        entry.figures.append(figure)

    return sorted(
        by_code.values(),
        key=lambda e: (e.distortion_share or 0.0, e.exposed_share),
        reverse=True,
    )


def by_state(db: Session, period: ReportingPeriod) -> list[StateExposure]:
    """Per-state exposure, weighted by how much each error moves the nation."""
    found, totals, indicators = _collect(db, period)
    code_to_id = {i.code: i.id for i in indicators.values()}

    submissions = list(
        db.scalars(
            select(Submission).where(
                Submission.period_id == period.id, Submission.is_current.is_(True)
            )
        )
    )
    states = {s.id: s for s in db.scalars(select(State))}

    per_state: dict[str, list[FigureExposure]] = defaultdict(list)
    for figure in found:
        per_state[figure.state_code].append(figure)

    results: list[StateExposure] = []
    for submission in submissions:
        state = states.get(submission.state_id)
        if state is None:
            continue
        figures = list(
            db.scalars(
                select(IndicatorValue).where(
                    IndicatorValue.submission_id == submission.id
                )
            )
        )
        flagged = per_state.get(state.code, [])

        # A state's weight in the national picture is the share of each
        # indicator's total it supplies. The doubted part of that weight is
        # driven by the size of each error, so a large figure with a trivial
        # error contributes almost nothing and a small figure with a large one
        # contributes a great deal.
        weight = 0.0
        exposed = 0.0
        for value in figures:
            total = totals.get(value.indicator_id) or 0.0
            if not total or value.value is None:
                continue
            weight += abs(value.value) / total
        for figure in flagged:
            indicator_id = code_to_id.get(figure.indicator_code)
            total = totals.get(indicator_id) if indicator_id else None
            if not total:
                continue
            gap = figure.distortion
            magnitude = abs(gap) if gap is not None else abs(figure.reported or 0.0)
            exposed += magnitude / total

        share = 100.0 * exposed / weight if weight else 0.0
        results.append(
            StateExposure(
                state_code=state.code,
                state_name=state.name,
                figures_reported=len(figures),
                figures_unfit=sum(1 for v in figures if not v.is_valid),
                figures_queried=sum(
                    1
                    for v in figures
                    if disclosure.status_of(v) is DisclosureStatus.QUERIED
                ),
                exposed_share=min(share, 100.0),
                fitness=submission.fitness_verdict,
                material_findings=[
                    f
                    for f in flagged
                    if f.is_blocking
                    and (
                        (
                            abs(f.distortion or 0.0)
                            / (totals.get(code_to_id.get(f.indicator_code, -1)) or 1)
                        )
                        * 100.0
                    )
                    >= IMMATERIAL_SHARE
                ],
            )
        )

    return sorted(results, key=lambda s: s.exposed_share, reverse=True)


def national_summary(db: Session, period: ReportingPeriod) -> dict[str, object]:
    """The standing disclosure line for a period, for dashboards and reports."""
    indicator_view = by_indicator(db, period)
    state_view = by_state(db, period)
    material = [entry for entry in indicator_view if entry.is_material]
    return {
        "period": period.code,
        "indicators_affected": len(indicator_view),
        "indicators_materially_affected": len(material),
        "states_with_blocking": sum(1 for s in state_view if s.has_blocking),
        "figures_unfit": sum(s.figures_unfit for s in state_view),
        "figures_queried": sum(s.figures_queried for s in state_view),
        "most_affected": [
            {
                "indicator": entry.indicator_code,
                "name": entry.indicator_name,
                "reported_total": entry.reported_total,
                "distortion": entry.net_distortion,
                "distortion_share": entry.distortion_share,
                "note": entry.note(),
            }
            for entry in material[:10]
        ],
    }


def apply_verdicts(db: Session, period: ReportingPeriod) -> dict[str, int]:
    """Set each submission's fitness verdict, now the national picture is whole.

    Materiality cannot be judged one state at a time: whether Gombe's missing
    5,833 schools matters depends on the national total it is missing from.
    So the verdict is settled here, after every state is in, exactly as the
    cross-state checks are.
    """
    state_view = {entry.state_code: entry for entry in by_state(db, period)}
    submissions = list(
        db.scalars(
            select(Submission).where(
                Submission.period_id == period.id, Submission.is_current.is_(True)
            )
        )
    )
    states = {s.id: s for s in db.scalars(select(State))}

    tally: dict[str, int] = defaultdict(int)
    for submission in submissions:
        state = states.get(submission.state_id)
        entry = state_view.get(state.code) if state else None
        if entry is None:
            continue
        open_findings = entry.figures_unfit + entry.figures_queried
        verdict = verdict_for_submission(
            score=submission.dqa_score,
            exposed_share=entry.exposed_share,
            material_findings=len(entry.material_findings),
            open_findings=open_findings,
        )
        submission.fitness_verdict = str(verdict)
        submission.exposed_share = round(entry.exposed_share, 2)
        tally[str(verdict)] += 1

    db.flush()
    return dict(tally)
