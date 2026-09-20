"""DQA scorecards: state-level, cohort-level and the consolidated national view."""

from __future__ import annotations

from collections import Counter
from statistics import fmean

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import (
    DQADimension,
    FitnessVerdict,
    Severity,
    SubmissionStatus,
    grade_for_score,
    grade_for_submission,
    grade_note,
)
from app.models import (
    Cohort,
    DQAScore,
    Indicator,
    IndicatorValue,
    ReportingPeriod,
    State,
    Submission,
    ValidationIssue,
)
from app.schemas.validation import (
    CohortDQASummary,
    DimensionScore,
    DQAScorecard,
    NationalDQASummary,
    ValidationIssueRead,
)
from app.services import reference

TOP_ISSUE_LIMIT = 10


def _current_submission(db: Session, state_id: int, period_id: int) -> Submission | None:
    """The submission that represents this state for this period.

    Prefers the approved current version; falls back to the latest attempt so a
    state that only ever submitted rejected data still appears on the scorecard.
    """
    approved = db.scalar(
        select(Submission).where(
            Submission.state_id == state_id,
            Submission.period_id == period_id,
            Submission.is_current.is_(True),
        )
    )
    if approved is not None:
        return approved
    candidates = list(
        db.scalars(
            select(Submission).where(
                Submission.state_id == state_id, Submission.period_id == period_id
            )
        )
    )
    return max(candidates, key=lambda row: row.version) if candidates else None


def _dimension_scores(db: Session, submission_id: int) -> list[DimensionScore]:
    rows = list(db.scalars(select(DQAScore).where(DQAScore.submission_id == submission_id)))
    order = {str(dimension): index for index, dimension in enumerate(DQADimension)}
    rows.sort(key=lambda row: order.get(row.dimension, 99))
    return [
        DimensionScore(
            dimension=row.dimension,
            score=row.score,
            weight=row.weight,
            checks_run=row.checks_run,
            checks_failed=row.checks_failed,
            grade=grade_for_score(row.score),
            details=row.details or {},
        )
        for row in rows
    ]


def state_scorecard(db: Session, state: State, period: ReportingPeriod) -> DQAScorecard:
    """The DQA scorecard for one state in one reporting period."""
    submission = _current_submission(db, state.id, period.id)
    cohort_code = state.cohort.code if state.cohort else None

    if submission is None:
        return DQAScorecard(
            state_code=state.code,
            state_name=state.name,
            cohort_code=cohort_code,
            period_code=period.code,
            status="NOT_SUBMITTED",
            grade="No data",
        )

    issues = list(
        db.scalars(
            select(ValidationIssue)
            .where(ValidationIssue.submission_id == submission.id)
            .order_by(ValidationIssue.severity, ValidationIssue.id)
        )
    )
    indicator_codes = {
        indicator.id: indicator.code
        for indicator in db.scalars(
            select(Indicator).where(
                Indicator.id.in_({i.indicator_id for i in issues if i.indicator_id} or {0})
            )
        )
    }
    severity_rank = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
    issues.sort(key=lambda issue: severity_rank.get(Severity(issue.severity), 3))

    days_late = None
    if submission.uploaded_at is not None:
        days_late = max(0, (submission.uploaded_at.date() - period.due_date).days)

    # What the score cannot see: figures held out of the totals under query.
    values = [
        value
        for value in db.scalars(
            select(IndicatorValue).where(IndicatorValue.submission_id == submission.id)
        )
        if value.effective_value is not None
    ]
    counting = sum(1 for value in values if value.is_valid)
    usable_share = 100.0 * counting / len(values) if values else None
    dimensions = _dimension_scores(db, submission.id)

    return DQAScorecard(
        state_code=state.code,
        state_name=state.name,
        cohort_code=cohort_code,
        period_code=period.code,
        submission_id=submission.id,
        status=submission.status,
        submitted_at=submission.uploaded_at,
        days_late=days_late,
        overall_score=submission.dqa_score,
        grade=submission.dqa_grade or grade_for_score(submission.dqa_score),
        figures_reported=len(values),
        figures_counting=counting,
        usable_share_pct=None if usable_share is None else round(usable_share, 1),
        fitness_verdict=submission.fitness_verdict,
        exposed_share_pct=submission.exposed_share,
        grade_note=grade_note(
            submission.dqa_score,
            [row.score for row in dimensions if row.score is not None],
            usable_share=usable_share,
        ),
        dimensions=dimensions,
        error_count=submission.error_count,
        warning_count=submission.warning_count,
        top_issues=[
            ValidationIssueRead(
                id=issue.id,
                rule_code=issue.rule_code,
                dimension=issue.dimension,
                severity=issue.severity,
                indicator_id=issue.indicator_id,
                indicator_code=indicator_codes.get(issue.indicator_id),
                field=issue.field,
                message=issue.message,
                observed=issue.observed,
                expected=issue.expected,
                source_row=issue.source_row,
                is_blocking=issue.is_blocking,
            )
            for issue in issues[:TOP_ISSUE_LIMIT]
        ],
    )


def _average_dimensions(scorecards: list[DQAScorecard]) -> list[DimensionScore]:
    buckets: dict[str, list[DimensionScore]] = {}
    for card in scorecards:
        for dimension in card.dimensions:
            buckets.setdefault(dimension.dimension, []).append(dimension)

    order = {str(dimension): index for index, dimension in enumerate(DQADimension)}
    averages: list[DimensionScore] = []
    for name, rows in sorted(buckets.items(), key=lambda pair: order.get(pair[0], 99)):
        mean_score = round(fmean(row.score for row in rows), 2)
        averages.append(
            DimensionScore(
                dimension=name,
                score=mean_score,
                weight=rows[0].weight,
                checks_run=sum(row.checks_run for row in rows),
                checks_failed=sum(row.checks_failed for row in rows),
                grade=grade_for_score(mean_score),
                details={"states_assessed": len(rows)},
            )
        )
    return averages


def cohort_summary(
    db: Session, cohort: Cohort, period: ReportingPeriod, scorecards: list[DQAScorecard] | None = None
) -> CohortDQASummary:
    members = [state for state in reference.active_states(db) if state.cohort_id == cohort.id]
    cards = [card for card in (scorecards or []) if card.cohort_code == cohort.code]
    if not cards:
        cards = [state_scorecard(db, state, period) for state in members]

    submitted = [card for card in cards if card.submission_id is not None]
    scored = [card for card in submitted if card.overall_score is not None]
    on_time = [card for card in submitted if (card.days_late or 0) == 0]
    average = round(fmean(card.overall_score for card in scored), 2) if scored else None

    return CohortDQASummary(
        cohort_code=cohort.code,
        cohort_name=cohort.name,
        states_expected=len(members),
        states_reported=len(submitted),
        reporting_rate_pct=round(len(submitted) / len(members) * 100, 2) if members else 0.0,
        on_time_rate_pct=round(len(on_time) / len(members) * 100, 2) if members else 0.0,
        average_score=average,
        grade=grade_for_score(average),
        dimension_averages=_average_dimensions(scored),
    )


def national_summary(
    db: Session, period: ReportingPeriod, *, state_code: str | None = None
) -> NationalDQASummary:
    """Consolidated DQA summary for a period, or for one state within it.

    The state filter reaches here because the data-quality board ignored it
    entirely: choosing Bauchi still showed "18 of 18 states assessed" and the
    national score, which is the opposite of what the filter promised.
    """
    if state_code:
        states = [reference.get_state_by_code(db, state_code)]
    else:
        states = reference.active_states(db)
    scorecards = [state_scorecard(db, state, period) for state in states]

    submitted = [card for card in scorecards if card.submission_id is not None]
    approved = [card for card in submitted if card.status == SubmissionStatus.APPROVED]
    scored = [card for card in submitted if card.overall_score is not None]
    on_time = [card for card in submitted if (card.days_late or 0) == 0]
    national_score = round(fmean(card.overall_score for card in scored), 2) if scored else None

    # Graded the same way a state is: a national figure that leaves 1 in 10
    # reported figures out of its own totals is not "Excellent" either.
    figures_reported = sum(card.figures_reported for card in submitted)
    figures_counting = sum(card.figures_counting for card in submitted)
    national_usable = (
        100.0 * figures_counting / figures_reported if figures_reported else None
    )
    dimension_averages = _average_dimensions(scored)

    # Count the states each rule affects, not the raw number of findings, and
    # read every issue rather than the truncated per-card top-10.
    submission_ids = {
        card.submission_id: card.state_code for card in submitted if card.submission_id
    }
    affected: dict[tuple[str, str], set[str]] = {}
    findings: Counter[tuple[str, str]] = Counter()
    if submission_ids:
        for issue in db.scalars(
            select(ValidationIssue).where(
                ValidationIssue.submission_id.in_(list(submission_ids))
            )
        ):
            key = (issue.rule_code, issue.dimension)
            affected.setdefault(key, set()).add(submission_ids[issue.submission_id])
            findings[key] += 1

    common_issues = [
        {
            "rule_code": rule_code,
            "dimension": dimension,
            "states_affected": len(affected[(rule_code, dimension)]),
            "findings": findings[(rule_code, dimension)],
            "share_pct": (
                round(len(affected[(rule_code, dimension)]) / len(states) * 100, 1)
                if states
                else 0.0
            ),
        }
        for (rule_code, dimension) in sorted(
            affected, key=lambda key: (-len(affected[key]), key[0])
        )[:10]
    ]

    cohorts = sorted(db.scalars(select(Cohort)), key=lambda c: c.sort_order)

    return NationalDQASummary(
        period_code=period.code,
        states_expected=len(states),
        states_reported=len(submitted),
        states_approved=len(approved),
        reporting_rate_pct=round(len(submitted) / len(states) * 100, 2) if states else 0.0,
        on_time_rate_pct=round(len(on_time) / len(states) * 100, 2) if states else 0.0,
        national_score=national_score,
        grade=grade_for_submission(
            national_score,
            [row.score for row in dimension_averages if row.score is not None],
            usable_share=national_usable,
        ),
        figures_reported=figures_reported,
        figures_counting=figures_counting,
        usable_share_pct=None if national_usable is None else round(national_usable, 1),
        states_not_fit=sum(
            1
            for card in submitted
            if card.fitness_verdict == str(FitnessVerdict.NOT_FIT)
        ),
        grade_note=grade_note(
            national_score,
            [row.score for row in dimension_averages if row.score is not None],
            usable_share=national_usable,
        ),
        dimension_averages=dimension_averages,
        cohort_scores=[cohort_summary(db, cohort, period, scorecards) for cohort in cohorts],
        scorecards=sorted(
            scorecards,
            key=lambda card: (card.overall_score is None, -(card.overall_score or 0), card.state_name),
        ),
        common_issues=common_issues,
    )
