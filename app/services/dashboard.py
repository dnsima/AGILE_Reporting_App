"""Assembles the dashboard overview payload.

Everything here reads from the analytics and DQA services, so the dashboard and
the API always agree. The ``data_version`` returned with each payload lets the
browser detect that new data has landed without diffing the whole response.
"""

from __future__ import annotations

from datetime import datetime, timezone
from statistics import fmean

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import ADDITIVE_METHODS, FitnessVerdict, SubmissionStatus, grade_for_score
from app.core.events import event_bus
from app.models import ReportingPeriod, Submission
from app.schemas.analytics import DashboardKpiTile, DashboardOverview
from app.services import analytics, cohort, dqa, exposure, reference

#: What a unit looks like on a tile. The raw enum was going straight to the
#: page, so a completion rate rendered as "64.0882PERCENT".
_UNIT_SYMBOLS = {"PERCENT": "%", "RATIO": "", "SCORE": "/100", "NUMBER": "", "BOOLEAN": ""}

#: Headline indicators surfaced as tiles at the top of the dashboard.
#:
#: These were KPI-001/007/009/029 until the 70-to-53 recode retired those
#: codes, after which every tile silently resolved to nothing and the dashboard
#: led with four empty cards. They now name the flagship access, retention,
#: supply-side and equity indicators the NPCU's own reporting leads with.
HEADLINE_INDICATORS = ("PDO-04", "PDO-07", "C1.2-05", "C2.2c-01")


def _contribution_tile(db, period, states, indicators, state_board) -> DashboardKpiTile:
    """What a state or cohort contributes towards the national targets.

    "Average KPI achievement" is meaningless below the national level, because
    targets are set nationally: the tile read "No target" for every state in
    the federation and "0 of 0 KPIs on track" for every cohort. What a slice of
    the programme can be measured on is the share of the national target it is
    delivering against.

    Each indicator is normalised against its own national target before the
    average is taken. Summing classrooms and girls and grievances would be
    arithmetic on incompatible units, and the resulting number would mean
    nothing at all.

    Rates are excluded, and the caption does not stop to say so, because a
    tile caption is read at a glance: a completion rate is a state's own
    performance rather than a slice of a national rate, and counting one gave
    Kogi 40% against the 10.6% of the national result it actually supplies.
    """
    national = analytics.scorecard(db, period, scope="NATIONAL", indicators=indicators)
    national_by_code = {row.indicator.code: row for row in national.rows}
    state_by_code = {row.indicator.code: row for row in state_board.rows}

    additive = {str(method) for method in ADDITIVE_METHODS}
    of_target: list[float] = []
    of_delivery: list[float] = []
    for code, national_row in national_by_code.items():
        state_row = state_by_code.get(code)
        if state_row is None or state_row.value is None:
            continue
        # Only where the national figure is the sum of what states supplied.
        indicator = next(
            (i for i in indicators if i.code == code and i.aggregation_method in additive),
            None,
        )
        if indicator is None:
            continue
        if national_row.target:
            of_target.append(100.0 * state_row.value / national_row.target)
        if national_row.value:
            of_delivery.append(100.0 * state_row.value / national_row.value)

    single = len(states) == 1
    subject = "this state" if single else f"these {len(states)} states"
    if not of_target:
        return DashboardKpiTile(
            key="contribution",
            label="Contribution to national targets",
            value=None,
            caption=(
                f"No national target is set on the indicators {subject} report."
            ),
            status="No target",
        )

    share = sum(of_target) / len(of_target)
    delivered = sum(of_delivery) / len(of_delivery) if of_delivery else None
    reporting = len(reference.active_states(db)) or 1
    # An even share is proportional to how many states are in the selection,
    # so a cohort of eleven is judged against eleven-eighteenths, not one.
    even = 100.0 * len(states) / reporting

    caption = (
        f"Mean over {len(of_target)} countable indicators. An even share for "
        f"{len(states)} of {reporting} states would be {even:.1f}%."
    )
    if delivered is not None:
        caption += f" Supplies {delivered:.1f}% of the national result."

    return DashboardKpiTile(
        key="contribution",
        label="Share of national targets delivered",
        value=round(share, 1),
        unit="%",
        caption=caption,
        status=analytics.status_for(100.0 * share / even) if even else None,
    )


def _fitness_tile(db: Session, period, state, scope_codes) -> DashboardKpiTile:
    """Whether the data can be used, which is what the DQA score never said.

    The verdict was being computed and stored and then shown nowhere, so the
    dashboard still led with a grade of "Excellent" for a state reporting a
    figure that moves a national total by 89%. It is the first thing on the
    board now, ahead of the score it is so often mistaken for.
    """
    entries = [
        row for row in exposure.by_state(db, period) if row.state_code in scope_codes
    ]

    if not entries:
        return DashboardKpiTile(
            key="fitness",
            label="Fit for use",
            value=None,
            caption="No return has been validated for this period yet.",
            status=str(FitnessVerdict.NO_DATA),
        )

    not_fit = [row for row in entries if row.fitness == str(FitnessVerdict.NOT_FIT)]
    worst = max(entries, key=lambda row: row.exposed_share)

    if state is not None:
        row = entries[0]
        verdict = row.fitness or str(FitnessVerdict.NO_DATA)
        return DashboardKpiTile(
            key="fitness",
            label="Fit for use",
            value=round(row.exposed_share, 1),
            unit="% in doubt",
            caption=(
                f"{row.figures_unfit} figure(s) unfit, {row.figures_queried} "
                "under query. Every figure is still counted in the national "
                "totals."
            ),
            status=verdict,
        )

    return DashboardKpiTile(
        key="fitness",
        label="States not fit for use",
        value=float(len(not_fit)),
        unit=f" of {len(entries)}",
        caption=(
            f"Worst: {worst.state_name}, {worst.exposed_share:.0f}% of its "
            "contribution in doubt. Nothing is withheld from a total."
        ),
        status=(
            str(FitnessVerdict.NOT_FIT)
            if not_fit
            else str(FitnessVerdict.FIT)
        ),
    )


def overview(
    db: Session,
    period: ReportingPeriod,
    *,
    cohort_code: str | None = None,
    state_code: str | None = None,
) -> DashboardOverview:
    """Headline tiles for a period, optionally narrowed to a cohort or state.

    The state filter existed in the page for some time without reaching here:
    choosing a state set a variable nothing read, so every figure on screen
    stayed national and the filter looked broken because it was.
    """
    state = reference.get_state_by_code(db, state_code) if state_code else None
    # One resolved scope drives every figure on the board. Deriving the
    # denominator from the filter and the numerator from an unfiltered count
    # is how the reporting rate came to read "18 of 11 states", 164%.
    states = [state] if state else reference.active_states(db, cohort_code)
    scope_ids = [s.id for s in states]
    indicators = reference.active_indicators(db)

    if state is not None:
        board = analytics.scorecard(
            db, period, scope="STATE", state_code=state.code, indicators=indicators
        )
    elif cohort_code:
        board = analytics.scorecard(
            db, period, scope="COHORT", cohort_code=cohort_code, indicators=indicators
        )
    else:
        board = analytics.scorecard(db, period, scope="NATIONAL", indicators=indicators)

    national_dqa = dqa.national_summary(
        db, period, state_code=state_code, cohort_code=cohort_code
    )
    cohort_rows = [] if state else cohort.cohort_summaries(db, period, indicators=indicators)

    scoped = [Submission.period_id == period.id, Submission.state_id.in_(scope_ids)]
    submitted = db.scalar(
        select(func.count(func.distinct(Submission.state_id))).where(*scoped)
    ) or 0
    approved = db.scalar(
        select(func.count(func.distinct(Submission.state_id))).where(
            *scoped,
            Submission.status == SubmissionStatus.APPROVED,
            Submission.is_current.is_(True),
        )
    ) or 0

    scoped_view = state is not None or bool(cohort_code)
    fitness = _fitness_tile(db, period, state, {s.code for s in states})
    scope_word = state.name if state else ("cohort" if cohort_code else "National")

    tiles: list[DashboardKpiTile] = [
        DashboardKpiTile(
            key="reporting_rate",
            label="Reporting" if state else "States reporting",
            value=round(submitted / len(states) * 100, 1) if states else 0.0,
            unit="%",
            caption=(
                f"{state.name} "
                + ("submitted" if submitted else "has not submitted")
                + f" for {period.code}"
                if state
                else f"{submitted} of {len(states)} states submitted for {period.code}"
            ),
            status="On track" if states and submitted / len(states) >= 0.9 else "Lagging",
        ),
        DashboardKpiTile(
            key="approved_rate",
            label="Data cleared for analysis",
            value=round(approved / len(states) * 100, 1) if states else 0.0,
            unit="%",
            caption=(
                f"{approved} state submission(s) passed the quality gate"
            ),
            status="On track" if states and approved / len(states) >= 0.8 else "Lagging",
        ),
        fitness,
        DashboardKpiTile(
            key="dqa_score",
            label=f"{scope_word} DQA score" if not state else "DQA score",
            # national_dqa is already scoped to the selection, so this needs
            # no branch of its own -- and cannot drift from the rest of the
            # board the way a separately resolved figure would.
            value=national_dqa.national_score,
            unit="/100",
            caption=(
                "How many checks passed. It is not a verdict on whether the "
                "data can be used -- see above."
            ),
            status=national_dqa.grade,
        ),
        (
            _contribution_tile(db, period, states, indicators, board)
            if (state is not None or cohort_code)
            else DashboardKpiTile(
                key="average_achievement",
                label="Average KPI achievement",
                value=board.average_achievement_pct,
                unit="%",
                caption=(
                    f"{board.indicators_on_track} of "
                    f"{board.indicators_with_target} KPIs on track"
                ),
                status=analytics.status_for(board.average_achievement_pct),
            )
        ),
    ]

    # A scoped board has no target of its own, so its tiles all read "No target
    # set" -- true but useless. The national target is the one that exists, so
    # a scoped tile says what share of it this selection has delivered.
    national_rows = (
        {}
        if scoped_view
        else {row.indicator.code: row for row in board.rows}
    )
    if scoped_view:
        national_board = analytics.scorecard(
            db, period, scope="NATIONAL", indicators=indicators
        )
        national_rows = {row.indicator.code: row for row in national_board.rows}

    additive = {str(method) for method in ADDITIVE_METHODS}
    for code in HEADLINE_INDICATORS:
        indicator = reference.get_indicator_by_code(db, code, required=False)
        if indicator is None:
            continue
        row = next((r for r in board.rows if r.indicator.code == code), None)
        if row is None:
            continue

        if row.achievement_pct is not None:
            caption = f"{row.achievement_pct:.0f}% of target"
        elif scoped_view and indicator.aggregation_method in additive:
            national_row = national_rows.get(code)
            if national_row is not None and national_row.target and row.value is not None:
                share = 100.0 * row.value / national_row.target
                caption = (
                    f"{share:.1f}% of the national target "
                    f"({national_row.target:,.0f})"
                )
            else:
                caption = "No national target set"
        else:
            caption = "No target set for this selection"

        tiles.append(
            DashboardKpiTile(
                key=code,
                label=indicator.name,
                value=row.value,
                unit=_UNIT_SYMBOLS.get(indicator.unit, ""),
                caption=caption,
                status=row.status,
            )
        )

    rankings = [
        row
        for row in _state_rankings(db, period, cohort_code)
        if row["state_code"] in {s.code for s in states}
    ]
    top_states = sorted(
        (row for row in rankings if row.get("average_achievement_pct")),
        key=lambda row: -row["average_achievement_pct"],
    )[:10]

    return DashboardOverview(
        period_code=period.code,
        period_label=period.label,
        generated_at=datetime.now(timezone.utc).isoformat(),
        data_version=event_bus.data_version,
        tiles=tiles,
        cohorts=cohort_rows,
        top_states=[],
        reporting_status={
            "states_expected": len(states),
            "states_submitted": submitted,
            "states_approved": approved,
            "on_time_rate_pct": national_dqa.on_time_rate_pct,
            "due_date": period.due_date.isoformat(),
            "top_states": top_states,
        },
        dqa_summary={
            "national_score": national_dqa.national_score,
            "grade": national_dqa.grade,
            "dimensions": [d.model_dump() for d in national_dqa.dimension_averages],
            "common_issues": national_dqa.common_issues,
            "states_reported": national_dqa.states_reported,
            "states_approved": national_dqa.states_approved,
        },
    )


def _state_rankings(
    db: Session, period: ReportingPeriod, cohort_code: str | None
) -> list[dict]:
    rows: list[dict] = []
    indicators = reference.active_indicators(db)
    for state in reference.active_states(db, cohort_code):
        board = analytics.scorecard(
            db, period, scope="STATE", state_code=state.code, indicators=indicators
        )
        card = dqa.state_scorecard(db, state, period)
        rows.append(
            {
                "state_code": state.code,
                "state_name": state.name,
                "cohort_code": state.cohort.code if state.cohort else None,
                "average_achievement_pct": board.average_achievement_pct,
                "indicators_on_track": board.indicators_on_track,
                "indicators_with_data": board.indicators_with_data,
                "dqa_score": card.overall_score,
                "dqa_grade": card.grade,
                "reported": card.submission_id is not None,
                "days_late": card.days_late,
            }
        )
    return rows


def state_rankings(
    db: Session, period: ReportingPeriod, cohort_code: str | None = None
) -> list[dict]:
    rows = _state_rankings(db, period, cohort_code)
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


def dqa_heatmap(
    db: Session,
    period_codes: list[str] | None = None,
    limit: int = 8,
    period_type: str | None = None,
) -> dict:
    """States x periods DQA score heatmap for the dashboard."""
    periods = (
        [reference.get_period_by_code(db, code) for code in period_codes]
        if period_codes
        else analytics.recent_periods(db, period_type, limit=limit)
    )
    states = reference.active_states(db)
    cells = []
    for state in states:
        for period in periods:
            card = dqa.state_scorecard(db, state, period)
            cells.append(
                {
                    "row_key": state.code,
                    "column_key": period.code,
                    "value": card.overall_score,
                    "label": card.grade,
                }
            )
    scores = [cell["value"] for cell in cells if cell["value"] is not None]
    # DQA scores cluster near the top, so the colour scale spans the observed
    # range rather than a flat 0-100 where every cell would look identical. The
    # legend carries both ends so the scale stays self-describing.
    lowest = min(scores) if scores else 0.0
    return {
        "metric": "dqa_score",
        "rows": [state.code for state in states],
        "row_labels": {state.code: state.name for state in states},
        "columns": [period.code for period in periods],
        "column_labels": {period.code: period.label for period in periods},
        "cells": cells,
        "scale_min": round(max(0.0, lowest - 2), 1),
        "scale_max": 100.0,
        "average": round(fmean(scores), 2) if scores else None,
        "grade": grade_for_score(round(fmean(scores), 2) if scores else None),
    }
