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

from app.core.enums import SubmissionStatus, grade_for_score
from app.core.events import event_bus
from app.models import ReportingPeriod, Submission
from app.schemas.analytics import DashboardKpiTile, DashboardOverview
from app.services import analytics, cohort, dqa, reference

#: Headline indicators surfaced as tiles at the top of the dashboard.
HEADLINE_INDICATORS = ("KPI-001", "KPI-007", "KPI-009", "KPI-029")


def overview(
    db: Session,
    period: ReportingPeriod,
    *,
    cohort_code: str | None = None,
) -> DashboardOverview:
    states = reference.active_states(db, cohort_code)
    indicators = reference.active_indicators(db)

    board = (
        analytics.scorecard(db, period, scope="COHORT", cohort_code=cohort_code, indicators=indicators)
        if cohort_code
        else analytics.scorecard(db, period, scope="NATIONAL", indicators=indicators)
    )
    national_dqa = dqa.national_summary(db, period)
    cohort_rows = cohort.cohort_summaries(db, period, indicators=indicators)

    submitted = db.scalar(
        select(func.count(func.distinct(Submission.state_id))).where(
            Submission.period_id == period.id
        )
    ) or 0
    approved = db.scalar(
        select(func.count(func.distinct(Submission.state_id))).where(
            Submission.period_id == period.id,
            Submission.status == SubmissionStatus.APPROVED,
            Submission.is_current.is_(True),
        )
    ) or 0

    tiles: list[DashboardKpiTile] = [
        DashboardKpiTile(
            key="reporting_rate",
            label="States reporting",
            value=round(submitted / len(states) * 100, 1) if states else 0.0,
            unit="%",
            caption=f"{submitted} of {len(states)} states submitted for {period.code}",
            status="On track" if states and submitted / len(states) >= 0.9 else "Lagging",
        ),
        DashboardKpiTile(
            key="approved_rate",
            label="Data cleared for analysis",
            value=round(approved / len(states) * 100, 1) if states else 0.0,
            unit="%",
            caption=f"{approved} state submissions passed the quality gate",
            status="On track" if states and approved / len(states) >= 0.8 else "Lagging",
        ),
        DashboardKpiTile(
            key="dqa_score",
            label="National DQA score",
            value=national_dqa.national_score,
            unit="/100",
            caption=f"Grade: {national_dqa.grade}",
            status=national_dqa.grade,
        ),
        DashboardKpiTile(
            key="average_achievement",
            label="Average KPI achievement",
            value=board.average_achievement_pct,
            unit="%",
            caption=f"{board.indicators_on_track} of {board.indicators_with_target} KPIs on track",
            status=analytics.status_for(board.average_achievement_pct),
        ),
    ]

    for code in HEADLINE_INDICATORS:
        indicator = reference.get_indicator_by_code(db, code, required=False)
        if indicator is None:
            continue
        row = next((r for r in board.rows if r.indicator.code == code), None)
        if row is None:
            continue
        tiles.append(
            DashboardKpiTile(
                key=code,
                label=indicator.name,
                value=row.value,
                unit=indicator.unit,
                caption=(
                    f"{row.achievement_pct:.0f}% of target"
                    if row.achievement_pct is not None
                    else "No target set"
                ),
                status=row.status,
            )
        )

    top_states = sorted(
        (row for row in _state_rankings(db, period, cohort_code) if row.get("average_achievement_pct")),
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
