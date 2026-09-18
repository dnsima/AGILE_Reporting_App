"""Assembles routine/periodic reports from the analysis and DQA services."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import QueryStatus, ReportFormat, ReportScope
from app.core.errors import ValidationError
from app.core.logging_config import get_logger
from app.models import GeneratedReport, Indicator, ReportingPeriod, User
from app.schemas.reporting import ReportArtifact, ReportRequest, ReportResponse
from app.services import analytics, audit, dqa, queries, reference
from app.services import cohort as cohort_service
from app.services.reporting.document import ReportDocument, Section, Table
from app.services.reporting.renderers import (
    render_html,
    render_markdown,
    render_pdf,
)

logger = get_logger(__name__)

MAX_NARRATIVE_INDICATORS = 25


def _fmt(value: float | None, decimals: int = 1, suffix: str = "") -> str:
    if value is None:
        return "—"
    if abs(value) >= 1000:
        return f"{value:,.0f}{suffix}"
    return f"{value:,.{decimals}f}{suffix}"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:,.1f}%"


def _indicator_count(indicators: list[Indicator]) -> str:
    """Say 53 + 1 derived rather than 54.

    The catalogue carries one row the platform computes rather than collects,
    and a report that calls the framework 54 indicators contradicts every other
    document the NPCU issues.
    """
    derived = sum(1 for indicator in indicators if not indicator.is_reported)
    reported = len(indicators) - derived
    if not derived:
        return str(reported)
    return f"{reported} reported + {derived} derived"


# --------------------------------------------------------------------------
# Narrative helpers
# --------------------------------------------------------------------------
def _indicator_narrative(
    analysis: analytics.IndicatorAnalysis, trend: analytics.TrendSeries | None
) -> str:
    indicator = analysis.indicator
    national = analysis.national
    parts: list[str] = []

    if national.value is None:
        parts.append(
            f"No state reported {indicator.code} for {analysis.period_code}, so no national "
            "figure can be computed for this KPI."
        )
    else:
        headline = f"{indicator.code} ({indicator.name}) stands at {_fmt(national.value)}"
        if indicator.unit == "PERCENT":
            headline = f"{indicator.code} ({indicator.name}) stands at {_pct(national.value)}"
        if national.target is not None:
            headline += (
                f" against a national target of {_fmt(national.target)}, "
                f"{_pct(national.achievement_pct)} of target ({national.status.lower()})"
            )
        headline += (
            f", consolidated from {national.states_reporting} of "
            f"{national.states_expected} states."
        )
        parts.append(headline)

    reporting = [row for row in analysis.states if row.reported and row.achievement_pct is not None]
    if reporting:
        best = max(reporting, key=lambda row: row.achievement_pct or 0)
        worst = min(reporting, key=lambda row: row.achievement_pct or 0)
        parts.append(
            f"{best.state_name} leads at {_pct(best.achievement_pct)} of its state target, while "
            f"{worst.state_name} trails at {_pct(worst.achievement_pct)}."
        )

    contributors = sorted(
        (row for row in analysis.states if row.contribution_pct is not None),
        key=lambda row: -(row.contribution_pct or 0),
    )[:3]
    if contributors:
        share = ", ".join(
            f"{row.state_name} ({_pct(row.contribution_pct)})" for row in contributors
        )
        parts.append(f"The largest contributions to the national result come from {share}.")

    if analysis.cohorts:
        ranked = [row for row in analysis.cohorts if row.achievement_pct is not None]
        if ranked:
            ranked.sort(key=lambda row: -(row.achievement_pct or 0))
            parts.append(
                "By financing cohort, "
                + "; ".join(
                    f"{row.cohort_name} reaches {_pct(row.achievement_pct)} of target"
                    for row in ranked
                )
                + "."
            )

    if trend and trend.change_pct is not None:
        parts.append(
            f"Across the last {len(trend.points)} reporting periods the national value has "
            f"moved {trend.change_pct:+.1f}% and is {trend.direction_of_travel}."
        )

    return " ".join(parts)


# --------------------------------------------------------------------------
# Section builders
# --------------------------------------------------------------------------
def _reporting_status_section(db: Session, period: ReportingPeriod) -> Section:
    summary = dqa.national_summary(db, period)
    section = Section(heading="Reporting status and coverage")
    section.add_paragraph(
        f"{summary.states_reported} of {summary.states_expected} states submitted data for "
        f"{period.code} ({_pct(summary.reporting_rate_pct)} reporting rate). "
        f"{summary.states_approved} submissions passed the data quality gate and feed the "
        f"analysis in this report. {_pct(summary.on_time_rate_pct)} of states submitted on or "
        f"before the {period.due_date.isoformat()} deadline."
    )
    section.add_table(
        Table(
            caption="Reporting status by financing cohort",
            headers=[
                "Cohort", "States", "Reported", "Reporting rate", "On-time rate", "Avg DQA", "Grade",
            ],
            rows=[
                [
                    row.cohort_name,
                    str(row.states_expected),
                    str(row.states_reported),
                    _pct(row.reporting_rate_pct),
                    _pct(row.on_time_rate_pct),
                    _fmt(row.average_score),
                    row.grade,
                ]
                for row in summary.cohort_scores
            ],
        )
    )
    non_reporting = [
        card.state_name for card in summary.scorecards if card.submission_id is None
    ]
    if non_reporting:
        section.add_paragraph(
            "States with no submission on record for this period: " + ", ".join(sorted(non_reporting)) + "."
        )
    return section


def _dqa_section(db: Session, period: ReportingPeriod, scope: ReportScope, scope_ref: str | None) -> Section:
    section = Section(heading="Data quality assessment")
    summary = dqa.national_summary(db, period)

    if scope == ReportScope.STATE and scope_ref:
        state = reference.get_state_by_code(db, scope_ref)
        card = dqa.state_scorecard(db, state, period)
        section.add_paragraph(
            f"{state.name} scored {_fmt(card.overall_score)} out of 100 ({card.grade}) across the "
            f"seven data quality dimensions, with {card.error_count} error(s) and "
            f"{card.warning_count} warning(s) raised by the validation engine."
        )
        section.add_table(
            Table(
                caption=f"{state.name} DQA scorecard - {period.code}",
                headers=["Dimension", "Score", "Grade", "Checks run", "Checks failed", "Weight"],
                rows=[
                    [
                        dimension.dimension.title(),
                        _fmt(dimension.score),
                        dimension.grade or "",
                        str(dimension.checks_run),
                        str(dimension.checks_failed),
                        _fmt(dimension.weight, 1),
                    ]
                    for dimension in card.dimensions
                ],
            )
        )
        if card.top_issues:
            section.add_table(
                Table(
                    caption="Validation findings requiring attention",
                    headers=["Severity", "Dimension", "Rule", "Finding"],
                    rows=[
                        [issue.severity, issue.dimension.title(), issue.rule_code, issue.message]
                        for issue in card.top_issues
                    ],
                    align=["left", "left", "left", "left"],
                )
            )
        return section

    scorecards = summary.scorecards
    if scope == ReportScope.COHORT and scope_ref:
        scorecards = [card for card in scorecards if card.cohort_code == scope_ref.upper()]

    section.add_paragraph(
        f"The consolidated DQA score for {period.code} is {_fmt(summary.national_score)} out of "
        f"100 ({summary.grade}), averaged across the {summary.states_reported} states that "
        "submitted data. The score is the proportion of applicable checks that passed; the "
        "grade additionally accounts for figures held out of the totals, which no per-check "
        "score can see."
    )
    if summary.figures_reported:
        section.add_paragraph(
            f"{summary.figures_counting} of {summary.figures_reported} reported figures "
            f"({_pct(summary.usable_share_pct)}) count towards the consolidated results."
            + (f" {summary.grade_note}" if summary.grade_note else "")
        )
    section.add_table(
        Table(
            caption="National average by data quality dimension",
            headers=["Dimension", "Score", "Grade", "States assessed", "Weight"],
            rows=[
                [
                    dimension.dimension.title(),
                    "not assessed" if dimension.score is None else _fmt(dimension.score),
                    "" if dimension.score is None else (dimension.grade or ""),
                    str(dimension.details.get("states_assessed", "—")),
                    _fmt(dimension.weight, 1),
                ]
                for dimension in summary.dimension_averages
            ],
        )
    )
    section.add_table(
        Table(
            caption="State DQA scorecards",
            headers=[
                "State", "Cohort", "Status", "Score", "Grade", "Figures counting",
                "Errors", "Warnings", "Days late",
            ],
            rows=[
                [
                    card.state_name,
                    card.cohort_code or "—",
                    card.status or "—",
                    _fmt(card.overall_score),
                    card.grade,
                    (
                        f"{card.figures_counting} of {card.figures_reported} "
                        f"({_pct(card.usable_share_pct)})"
                        if card.figures_reported
                        else "—"
                    ),
                    str(card.error_count),
                    str(card.warning_count),
                    "—" if card.days_late is None else str(card.days_late),
                ]
                for card in scorecards
            ],
            align=["left", "left", "left", "right", "left", "right", "right", "right", "right"],
            note=(
                "A grade below the score's own band is capped by figures held out of the "
                "totals or by a weak dimension; the reason is given per state on the platform."
            ),
        )
    )
    if summary.common_issues:
        section.add_table(
            Table(
                caption="Most frequent validation findings",
                headers=["Rule", "Dimension", "States affected", "Share of states"],
                rows=[
                    [
                        issue["rule_code"],
                        issue["dimension"].title(),
                        str(issue["states_affected"]),
                        f"{issue['share_pct']}%",
                    ]
                    for issue in summary.common_issues
                ],
            )
        )
    return section


# --------------------------------------------------------------------------
# Figures under query
# --------------------------------------------------------------------------
#: Individual queries listed before the table is summarised instead.
MAX_LISTED_QUERIES = 40


def _scope_state_ids(
    db: Session, scope: ReportScope, scope_ref: str | None
) -> list[int] | None:
    """State ids this report covers; ``None`` for a national report."""
    if scope == ReportScope.STATE and scope_ref:
        return [reference.get_state_by_code(db, scope_ref).id]
    if scope == ReportScope.COHORT and scope_ref:
        return [state.id for state in reference.active_states(db, scope_ref)]
    return None


def _query_section(
    db: Session,
    period: ReportingPeriod,
    indicators: list[Indicator],
    scope: ReportScope,
    scope_ref: str | None,
) -> Section:
    """What remains unresolved, and what it does to the figures above.

    A figure under query is not missing and not wrong; it is unconfirmed. The
    report publishes anyway -- a reporting cycle cannot wait on a query -- so it
    has to say plainly which figures are still open, which are held out of the
    totals printed above, and which are waiting on a physical check. That last
    list is the supervision and DQA worklist: it is how an unconfirmed figure
    becomes something someone actually goes and looks at.
    """
    section = Section(heading="Figures under query")
    state_ids = _scope_state_ids(db, scope, scope_ref)

    summary = queries.query_summary(db, period.id, state_ids=state_ids)
    open_rows = queries.open_queries(db, period_id=period.id)
    if state_ids is not None:
        allowed = set(state_ids)
        open_rows = [row for row in open_rows if row.state_id in allowed]

    if not summary["total"]:
        section.add_paragraph(
            f"No figure reported for {period.code} was queried. Every figure in this report "
            "is as the reporting state submitted it, and none is held out of the totals above."
        )
        return section

    held = queries.held_figures(db, period.id, state_ids=state_ids)
    in_scope = {indicator.id for indicator in indicators}
    held_in_scope = [value for value in held if value.indicator_id in in_scope]

    section.add_paragraph(
        f"{summary['open']} of the {summary['total']} figures queried for {period.code} remain "
        f"unresolved, across {summary['states_with_open_queries']} state(s). "
        f"{summary['overdue']} are past the date the state was given to respond, and "
        f"{summary['awaiting_review']} carry a response now with the NPCU. "
        f"{summary['resolved']} have been settled, {summary['restated']} of them by a "
        "correction accepted on the evidence the state supplied."
    )
    section.add_paragraph(
        f"{len(held_in_scope)} figure(s) covering indicators in this report are held out of the "
        "consolidated totals above until their query is settled. They are on record and shown "
        "below, but they are not counted: a national total that silently included a figure the "
        "rules could not accept would be neither what the states reported nor what the NPCU "
        "can stand behind."
        if held_in_scope
        else "No figure covering an indicator in this report is held out of the totals above."
    )

    # Which printed figures are provisional, and by how much.
    if held_in_scope:
        by_indicator: dict[int, list] = {}
        for value in held_in_scope:
            by_indicator.setdefault(value.indicator_id, []).append(value)
        catalogue = {indicator.id: indicator for indicator in indicators}
        section.add_table(
            Table(
                caption="Indicators whose national total in this report excludes a held figure",
                headers=["KPI", "Indicator", "States held out", "Excluded figures"],
                rows=[
                    [
                        catalogue[indicator_id].code,
                        catalogue[indicator_id].name,
                        ", ".join(
                            sorted(
                                value.submission.state.code
                                for value in rows
                                if value.submission and value.submission.state
                            )
                        ),
                        str(len(rows)),
                    ]
                    for indicator_id, rows in sorted(
                        by_indicator.items(), key=lambda item: catalogue[item[0]].number
                    )
                ],
                align=["left", "left", "left", "right"],
                note=(
                    "Treat these totals as provisional. Each will move when the query behind it "
                    "is settled, in either direction."
                ),
            )
        )

    # Where the unresolved work sits.
    by_state: dict[str, dict] = {}
    for row in open_rows:
        entry = by_state.setdefault(
            row.state.name,
            {"open": 0, "overdue": 0, "awaiting": 0, "verification": 0, "oldest": None},
        )
        entry["open"] += 1
        if row.is_overdue():
            entry["overdue"] += 1
        if row.status == str(QueryStatus.RESPONDED):
            entry["awaiting"] += 1
        if row.verification_required:
            entry["verification"] += 1
        if row.due_date and (entry["oldest"] is None or row.due_date < entry["oldest"]):
            entry["oldest"] = row.due_date

    if by_state:
        section.add_table(
            Table(
                caption="Unresolved queries by state",
                headers=[
                    "State", "Open", "Overdue", "With the NPCU", "For verification", "Earliest due",
                ],
                rows=[
                    [
                        name,
                        str(entry["open"]),
                        str(entry["overdue"]),
                        str(entry["awaiting"]),
                        str(entry["verification"]),
                        entry["oldest"].isoformat() if entry["oldest"] else "—",
                    ]
                    for name, entry in sorted(
                        by_state.items(), key=lambda item: (-item[1]["open"], item[0])
                    )
                ],
                align=["left", "right", "right", "right", "right", "left"],
            )
        )

    # The figures themselves.
    listed = sorted(
        open_rows,
        key=lambda row: (
            not row.is_overdue(),
            row.state.name,
            row.indicator.code if row.indicator else "",
        ),
    )
    truncated = len(listed) > MAX_LISTED_QUERIES
    section.add_table(
        Table(
            caption="Figures still under query",
            headers=["Ref", "State", "KPI", "As reported", "Raised because", "Due", "Status"],
            rows=[
                [
                    f"Q-{row.id}",
                    row.state.code,
                    row.indicator.code if row.indicator else "—",
                    _fmt(row.reported_value),
                    row.title,
                    row.due_date.isoformat() if row.due_date else "—",
                    row.status.title() + (f" ({row.days_overdue()}d late)" if row.is_overdue() else ""),
                ]
                for row in listed[:MAX_LISTED_QUERIES]
            ],
            align=["left", "left", "left", "right", "left", "left", "left"],
            note=(
                f"The {MAX_LISTED_QUERIES} most pressing of {len(listed)} are listed; the full "
                "list is on the platform."
                if truncated
                else None
            ),
        )
    )

    # The supervision worklist: what to go and look at.
    verification = [row for row in open_rows if row.verification_required]
    if verification:
        section.add_paragraph(
            f"{len(verification)} figure(s) could not be settled on the evidence supplied and "
            "have been referred for physical verification. These are the items to carry into "
            "the next supportive supervision or DQA exercise."
        )
        section.add_table(
            Table(
                caption="Referred for physical verification",
                headers=["Ref", "State", "KPI", "Indicator", "As reported", "Referred because"],
                rows=[
                    [
                        f"Q-{row.id}",
                        row.state.code,
                        row.indicator.code if row.indicator else "—",
                        row.indicator.name if row.indicator else "—",
                        _fmt(row.reported_value),
                        row.resolution_note or row.title,
                    ]
                    for row in verification
                ],
                align=["left", "left", "left", "left", "right", "left"],
            )
        )

    # What changed since this period was last published.
    revisions = queries.restatements(db, period.id, state_ids=state_ids)
    if revisions:
        section.add_paragraph(
            f"{len(revisions)} figure(s) for {period.code} have been restated since they were "
            "first reported. The figure as first reported is kept on record, so a report "
            "published earlier can be reconciled against this one."
        )
        section.add_table(
            Table(
                caption="Figures restated for this period",
                headers=["State", "KPI", "As first reported", "As now stated", "Why"],
                rows=[
                    [
                        revision.indicator_value.submission.state.code,
                        (
                            revision.indicator_value.indicator.code
                            if revision.indicator_value.indicator
                            else "—"
                        ),
                        _fmt(revision.previous_value),
                        _fmt(revision.new_value),
                        revision.reason,
                    ]
                    for revision in revisions
                ],
                align=["left", "left", "right", "right", "left"],
            )
        )

    return section


def _kpi_section(
    db: Session,
    period: ReportingPeriod,
    indicators: list[Indicator],
    scope: ReportScope,
    scope_ref: str | None,
) -> Section:
    section = Section(heading="KPI performance")
    board = analytics.scorecard(
        db,
        period,
        scope=str(scope),
        state_code=scope_ref if scope == ReportScope.STATE else None,
        cohort_code=scope_ref if scope == ReportScope.COHORT else None,
        indicators=indicators,
    )
    section.add_paragraph(
        f"{board.indicators_with_data} of {len(indicators)} indicators carry data for "
        f"{period.code}, and {board.indicators_with_target} have a target to be measured "
        f"against. Average achievement across measurable indicators is "
        f"{_pct(board.average_achievement_pct)}, with {board.indicators_on_track} indicator(s) "
        "at or above 90% of target."
    )
    section.add_table(
        Table(
            caption=f"KPI performance against target - {board.scope_label} - {period.code}",
            headers=["KPI", "Indicator", "Unit", "Value", "Target", "Achievement", "Status"],
            rows=[
                [
                    row.indicator.code,
                    row.indicator.name,
                    row.indicator.unit,
                    _fmt(row.value),
                    _fmt(row.target),
                    _pct(row.achievement_pct),
                    row.status,
                ]
                for row in board.rows
            ],
            align=["left", "left", "left", "right", "right", "right", "left"],
        )
    )
    return section


def _state_contribution_section(
    db: Session, period: ReportingPeriod, indicators: list[Indicator]
) -> Section:
    section = Section(heading="State performance and contribution to national results")
    rankings = []
    for state in reference.active_states(db):
        board = analytics.scorecard(
            db, period, scope="STATE", state_code=state.code, indicators=indicators
        )
        card = dqa.state_scorecard(db, state, period)
        rankings.append(
            {
                "state": state.name,
                "cohort": state.cohort.code if state.cohort else "—",
                "average": board.average_achievement_pct,
                "on_track": board.indicators_on_track,
                "with_data": board.indicators_with_data,
                "dqa": card.overall_score,
            }
        )
    rankings.sort(key=lambda row: (row["average"] is None, -(row["average"] or 0), row["state"]))

    section.add_table(
        Table(
            caption="State performance ranking",
            headers=["Rank", "State", "Cohort", "Avg achievement", "KPIs on track", "KPIs reported", "DQA"],
            rows=[
                [
                    str(rank),
                    row["state"],
                    row["cohort"],
                    _pct(row["average"]),
                    str(row["on_track"]),
                    str(row["with_data"]),
                    _fmt(row["dqa"]),
                ]
                for rank, row in enumerate(rankings, start=1)
            ],
        )
    )

    headline = indicators[0] if indicators else None
    if headline is not None:
        contribution = analytics.contribution_analysis(db, headline, period)
        section.add_table(
            Table(
                caption=(
                    f"Percentage contribution of each state to the national result for "
                    f"{headline.code}"
                ),
                headers=["Rank", "State", "Cohort", "Value", "Contribution"],
                rows=[
                    [
                        str(row.rank),
                        row.state_name,
                        row.cohort_code or "—",
                        _fmt(row.value),
                        _pct(row.contribution_pct),
                    ]
                    for row in contribution.rows
                ],
                note=(
                    "Contribution is each state's share of the consolidated national quantity: "
                    "the reported value for additive indicators, or the numerator for indicators "
                    "aggregated as a weighted average."
                ),
            )
        )
    return section


def _cohort_section(db: Session, period: ReportingPeriod, indicators: list[Indicator]) -> Section:
    section = Section(heading="Cohort analysis")
    summaries = cohort_service.cohort_summaries(db, period, indicators=indicators)
    section.add_paragraph(
        "Performance is disaggregated by AGILE financing cohort so states are compared against "
        "peers at a similar stage of implementation rather than against the whole federation."
    )
    section.add_table(
        Table(
            caption="Cohort comparison",
            headers=[
                "Cohort", "States", "Reporting", "On-time", "Completeness", "Avg DQA",
                "Avg achievement", "On track", "Contribution",
            ],
            rows=[
                [
                    row.cohort_name,
                    str(row.states_expected),
                    _pct(row.reporting_rate_pct),
                    _pct(row.on_time_rate_pct),
                    _pct(row.completeness_pct),
                    _fmt(row.average_dqa_score),
                    _pct(row.average_achievement_pct),
                    f"{row.indicators_on_track}/{row.indicators_assessed}",
                    _pct(row.contribution_pct),
                ]
                for row in summaries
            ],
        )
    )
    for row in summaries:
        bullets = []
        if row.best_state:
            bullets.append(f"Strongest performer: {row.best_state}")
        if row.weakest_state:
            bullets.append(f"Needs support: {row.weakest_state}")
        if bullets:
            section.bullets.append(f"{row.cohort_name} — " + "; ".join(bullets))
    return section


def _trend_section(
    db: Session,
    period: ReportingPeriod,
    indicators: list[Indicator],
    scope: ReportScope,
    scope_ref: str | None,
    trend_periods: int,
) -> Section:
    section = Section(heading="Trend analysis")
    section.add_paragraph(
        f"Longitudinal movement over the last {trend_periods} reporting periods of the same type, "
        "for the indicators covered by this report."
    )
    for indicator in indicators[:MAX_NARRATIVE_INDICATORS]:
        series = analytics.trend(
            db,
            indicator,
            scope=str(scope),
            state_code=scope_ref if scope == ReportScope.STATE else None,
            cohort_code=scope_ref if scope == ReportScope.COHORT else None,
            period_type=period.period_type,
            limit=trend_periods,
        )
        if not any(point.value is not None for point in series.points):
            continue
        section.add_table(
            Table(
                caption=f"{indicator.code}: {indicator.name}",
                headers=["Period", "Value", "Target", "Achievement", "States reporting"],
                rows=[
                    [
                        point.period_label,
                        _fmt(point.value),
                        _fmt(point.target),
                        _pct(point.achievement_pct),
                        str(point.states_reporting),
                    ]
                    for point in series.points
                ],
                note=(
                    f"Movement across the series: {series.change_pct:+.1f}% "
                    f"({series.direction_of_travel})."
                    if series.change_pct is not None
                    else None
                ),
            )
        )
    return section


def _narrative_section(
    db: Session,
    period: ReportingPeriod,
    indicators: list[Indicator],
    include_trends: bool,
    trend_periods: int,
) -> Section:
    section = Section(heading="KPI narratives")
    for indicator in indicators[:MAX_NARRATIVE_INDICATORS]:
        analysis = analytics.analyse_indicator(db, indicator, period)
        series = (
            analytics.trend(db, indicator, period_type=period.period_type, limit=trend_periods)
            if include_trends
            else None
        )
        subsection = Section(heading=f"{indicator.code}: {indicator.name}", level=3)
        subsection.add_paragraph(_indicator_narrative(analysis, series))
        if indicator.definition:
            subsection.add_paragraph(f"Definition: {indicator.definition}")
        section.subsections.append(subsection)
    if len(indicators) > MAX_NARRATIVE_INDICATORS:
        section.add_paragraph(
            f"Narratives are provided for the first {MAX_NARRATIVE_INDICATORS} indicators in "
            f"scope. Request a narrower indicator set to generate narratives for the remaining "
            f"{len(indicators) - MAX_NARRATIVE_INDICATORS}."
        )
    return section


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------
def build_report(
    db: Session, request: ReportRequest
) -> tuple[ReportDocument, list[Indicator], ReportingPeriod]:
    """Assemble the report document without writing any files."""
    period = reference.get_period_by_code(db, request.period_code)

    indicators = reference.active_indicators(db, indicator_codes=request.indicator_codes)
    if request.category_codes:
        wanted = {code.strip().upper() for code in request.category_codes}
        indicators = [
            indicator
            for indicator in indicators
            if indicator.category and indicator.category.code.upper() in wanted
        ]
    if not indicators:
        raise ValidationError("No indicators match the requested filters.")

    scope = ReportScope(request.scope)
    scope_ref = request.scope_ref
    if scope == ReportScope.STATE:
        if not scope_ref:
            raise ValidationError("A state code is required for a state-level report.")
        state = reference.get_state_by_code(db, scope_ref)
        scope_ref = state.code
        scope_label = state.name
    elif scope == ReportScope.COHORT:
        if not scope_ref:
            raise ValidationError("A cohort code is required for a cohort-level report.")
        cohort = reference.get_cohort_by_code(db, scope_ref)
        scope_ref = cohort.code
        scope_label = cohort.name
    else:
        # Counted, not asserted: AGILE covers the participating states, which
        # is not every state in the federation, and a report that says
        # otherwise overstates its own coverage.
        participating = len(reference.participating_states(db))
        scope_label = f"National ({participating} AGILE states)"

    title = request.title or (
        f"AGILE {period.period_type.replace('_', '-').title()} Report - {scope_label} - {period.label}"
    )

    document = ReportDocument(
        title=title,
        subtitle="Adolescent Girls Initiative for Learning and Empowerment (AGILE)",
        generated_at=datetime.now(timezone.utc),
        meta={
            "Reporting period": f"{period.label} ({period.code})",
            "Period type": period.period_type.replace("_", "-").title(),
            "Window": f"{period.start_date.isoformat()} to {period.end_date.isoformat()}",
            "Submission deadline": period.due_date.isoformat(),
            "Cycle": (
                "Open — states may still submit"
                if period.is_open
                else "Closed"
                + (f" on {period.locked_at:%Y-%m-%d}" if period.locked_at else "")
                + ". Figures change only through an accepted correction."
            ),
            "Scope": scope_label,
            "Indicators covered": _indicator_count(indicators),
        },
    )

    board = analytics.scorecard(
        db,
        period,
        scope=str(scope),
        state_code=scope_ref if scope == ReportScope.STATE else None,
        cohort_code=scope_ref if scope == ReportScope.COHORT else None,
        indicators=indicators,
    )
    national_dqa = dqa.national_summary(db, period)
    document.summary = (
        f"For {period.label}, {national_dqa.states_reported} of {national_dqa.states_expected} "
        f"AGILE states submitted reporting data and {national_dqa.states_approved} submissions "
        f"cleared the data quality gate. The consolidated DQA score is "
        f"{_fmt(national_dqa.national_score)}/100 ({national_dqa.grade}). Across the "
        f"{len(indicators)} indicators in scope ({_indicator_count(indicators)}), average "
        f"achievement against target is "
        f"{_pct(board.average_achievement_pct)}, with {board.indicators_on_track} of "
        f"{board.indicators_with_target} measurable indicators at or above 90% of target."
    )

    # A caveat that only appears in its own section has been read too late.
    query_counts = queries.query_summary(
        db, period.id, state_ids=_scope_state_ids(db, scope, scope_ref)
    )
    if query_counts["open"]:
        held = len(
            [
                value
                for value in queries.held_figures(
                    db, period.id, state_ids=_scope_state_ids(db, scope, scope_ref)
                )
                if value.indicator_id in {indicator.id for indicator in indicators}
            ]
        )
        document.summary += (
            f" {query_counts['open']} figure(s) remain under query across "
            f"{query_counts['states_with_open_queries']} state(s)"
            + (
                f", of which {held} are held out of the totals above, so those totals are "
                "provisional"
                if held
                else ""
            )
            + "."
            # The caveat stands whether or not the detail was asked for; only
            # the pointer is conditional, so it never names a missing section.
            + (
                " Section details are given under 'Figures under query'."
                if request.include_queries
                else " The detail section was excluded from this report."
            )
        )

    document.add_section(_reporting_status_section(db, period))
    if request.include_dqa:
        document.add_section(_dqa_section(db, period, scope, scope_ref))
    # Before the numbers, not after: what is unconfirmed qualifies every figure
    # that follows, and a reader who meets it in an appendix has already drawn
    # conclusions from totals they did not know were provisional.
    if request.include_queries:
        document.add_section(_query_section(db, period, indicators, scope, scope_ref))
    document.add_section(_kpi_section(db, period, indicators, scope, scope_ref))
    if request.include_state_tables and scope != ReportScope.STATE:
        document.add_section(_state_contribution_section(db, period, indicators))
    document.add_section(_cohort_section(db, period, indicators))
    if request.include_trends:
        document.add_section(
            _trend_section(db, period, indicators, scope, scope_ref, request.trend_periods)
        )
    if request.include_narratives:
        document.add_section(
            _narrative_section(db, period, indicators, request.include_trends, request.trend_periods)
        )

    document.number_sections()
    return document, indicators, period


def _slugify(text: str) -> str:
    safe = "".join(char if char.isalnum() else "-" for char in text.lower())
    while "--" in safe:
        safe = safe.replace("--", "-")
    return safe.strip("-")[:80]


def generate_report(
    db: Session, request: ReportRequest, actor: User | None = None
) -> ReportResponse:
    """Build the report, render the requested formats and persist the artifacts."""
    document, indicators, period = build_report(db, request)

    formats = list(dict.fromkeys(request.formats)) or [ReportFormat.MARKDOWN]
    markdown = render_markdown(document)

    record = GeneratedReport(
        title=document.title,
        scope=str(request.scope),
        scope_ref=request.scope_ref,
        period_code=period.code,
        period_type=period.period_type,
        formats=[str(fmt) for fmt in formats],
        parameters=request.model_dump(mode="json"),
        indicator_count=len(indicators),
        generated_by_id=getattr(actor, "id", None),
        generated_at=document.generated_at,
        summary=document.summary,
    )
    db.add(record)
    db.flush()

    base_dir = Path(settings.report_dir)
    base_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{record.id:06d}-{_slugify(document.title)}"

    artifacts: list[ReportArtifact] = []
    paths: dict[str, str] = {}
    warnings: list[str] = []

    for fmt in formats:
        fmt = ReportFormat(fmt)
        try:
            if fmt == ReportFormat.MARKDOWN:
                payload: bytes = markdown.encode("utf-8")
                filename = f"{stem}.md"
            elif fmt == ReportFormat.HTML:
                payload = render_html(document).encode("utf-8")
                filename = f"{stem}.html"
            else:
                payload = render_pdf(document)
                filename = f"{stem}.pdf"
        except RuntimeError as exc:
            warnings.append(str(exc))
            logger.warning("report format unavailable", extra={"format": str(fmt), "error": str(exc)})
            continue

        destination = base_dir / filename
        destination.write_bytes(payload)
        paths[str(fmt)] = str(destination)
        artifacts.append(
            ReportArtifact(
                format=str(fmt),
                filename=filename,
                size_bytes=len(payload),
                download_url=f"{settings.api_prefix}/reports/{record.id}/download?format={fmt}",
            )
        )

    record.file_paths = paths
    record.formats = [artifact.format for artifact in artifacts]
    db.flush()

    audit.record(
        db,
        action="report.generate",
        entity_type="report",
        entity_id=record.id,
        actor=actor,
        period_id=period.id,
        summary=f"Generated '{document.title}' in {', '.join(record.formats) or 'no'} format(s).",
        after={"formats": record.formats, "indicators": len(indicators)},
    )

    return ReportResponse(
        report_id=record.id,
        title=document.title,
        scope=str(request.scope),
        scope_ref=request.scope_ref,
        period_code=period.code,
        generated_at=document.generated_at,
        indicator_count=len(indicators),
        artifacts=artifacts,
        summary=(document.summary or "") + (" " + " ".join(warnings) if warnings else ""),
        markdown=markdown,
    )
