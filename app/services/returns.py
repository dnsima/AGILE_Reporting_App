"""How each state's return stands: findings, fitness, timeliness and coverage.

This replaced the DQA scorecard. The scorecard carried a 0-100 score and a
letter grade built from seven weighted dimensions; both are gone. The score
was a pass rate over thousands of automated checks, so it sat near 100 for
any plausible return -- Gombe scored 99.13 in a quarter where it reported 127
schools against 5,960 the quarter before -- and a reader seeing "Excellent"
had no way to know. Capping the grade helped and still left a number
underneath answering the wrong question.

What survives is what a reader actually needs, and none of it is a score:

* the **fitness verdict** -- FIT, FIT WITH NOTES, NOT FIT FOR USE, NO DATA;
* the **findings** themselves, each already raised as a query with the state;
* **exposure** -- how much of this state's contribution rests on figures the
  validation could not vouch for;
* **timeliness and coverage** -- when the return arrived and how much of the
  framework it answered.

Scoring a return is work for a data quality assessment with a field visit
behind it. That belongs in a subsystem of its own, not in a pass rate.
"""

from __future__ import annotations

from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import FitnessVerdict, Severity, SubmissionStatus, verdict_note
from app.models import (
    Cohort,
    Indicator,
    IndicatorValue,
    ReportingPeriod,
    State,
    Submission,
    ValidationIssue,
)
from app.schemas.validation import (
    CohortReturns,
    NationalReturns,
    StateReturn,
    ValidationIssueRead,
)
from app.services import reference

TOP_ISSUE_LIMIT = 10


def _current_submission(db: Session, state_id: int, period_id: int) -> Submission | None:
    """The submission that represents this state for this period.

    Prefers the approved current version; falls back to the latest attempt so a
    state that filed something is never shown as having filed nothing.
    """
    approved = db.scalar(
        select(Submission)
        .where(
            Submission.state_id == state_id,
            Submission.period_id == period_id,
            Submission.is_current.is_(True),
        )
        .order_by(Submission.version.desc())
    )
    if approved is not None:
        return approved
    return db.scalar(
        select(Submission)
        .where(Submission.state_id == state_id, Submission.period_id == period_id)
        .order_by(Submission.version.desc())
    )


def _findings_by_dimension(issues: list[ValidationIssue]) -> dict[str, int]:
    """How many findings fall in each area. A count, never a score."""
    counts: Counter[str] = Counter()
    for issue in issues:
        if Severity(issue.severity) is Severity.INFO or not issue.dimension:
            continue
        counts[issue.dimension] += 1
    return dict(counts)


def state_return(db: Session, state: State, period: ReportingPeriod) -> StateReturn:
    """How one state's return for one period stands."""
    submission = _current_submission(db, state.id, period.id)
    cohort_code = state.cohort.code if state.cohort else None

    if submission is None:
        return StateReturn(
            state_code=state.code,
            state_name=state.name,
            cohort_code=cohort_code,
            period_code=period.code,
            status="NOT_SUBMITTED",
            fitness_verdict=str(FitnessVerdict.NO_DATA),
            verdict_note="This state has not filed a return for this period.",
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

    values = [
        value
        for value in db.scalars(
            select(IndicatorValue).where(IndicatorValue.submission_id == submission.id)
        )
        if value.effective_value is not None
    ]
    # Every one of these counts towards the national totals. This says how many
    # a reader can lean on, which is a different question.
    fit = sum(1 for value in values if value.is_valid)
    usable_share = 100.0 * fit / len(values) if values else None

    verdict = submission.fitness_verdict
    return StateReturn(
        state_code=state.code,
        state_name=state.name,
        cohort_code=cohort_code,
        period_code=period.code,
        submission_id=submission.id,
        status=submission.status,
        submitted_at=submission.uploaded_at,
        days_late=days_late,
        figures_reported=len(values),
        figures_counting=fit,
        usable_share_pct=None if usable_share is None else round(usable_share, 1),
        fitness_verdict=verdict,
        verdict_note=(
            verdict_note(
                FitnessVerdict(verdict),
                exposed_share=submission.exposed_share,
                material_findings=submission.material_findings or 0,
                open_findings=submission.error_count + submission.warning_count,
            )
            if verdict
            else None
        ),
        exposed_share_pct=submission.exposed_share,
        findings_by_dimension=_findings_by_dimension(issues),
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


def cohort_returns(
    db: Session,
    cohort: Cohort,
    period: ReportingPeriod,
    returns: list[StateReturn] | None = None,
) -> CohortReturns:
    members = [state for state in reference.active_states(db) if state.cohort_id == cohort.id]
    rows = [row for row in (returns or []) if row.cohort_code == cohort.code]
    if not rows:
        rows = [state_return(db, state, period) for state in members]

    submitted = [row for row in rows if row.submission_id is not None]
    on_time = [row for row in submitted if (row.days_late or 0) == 0]
    reported = sum(row.figures_reported for row in submitted)
    counting = sum(row.figures_counting for row in submitted)

    return CohortReturns(
        cohort_code=cohort.code,
        cohort_name=cohort.name,
        states_expected=len(members),
        states_reported=len(submitted),
        reporting_rate_pct=round(len(submitted) / len(members) * 100, 2) if members else 0.0,
        on_time_rate_pct=round(len(on_time) / len(members) * 100, 2) if members else 0.0,
        states_not_fit=sum(
            1 for row in submitted if row.fitness_verdict == str(FitnessVerdict.NOT_FIT)
        ),
        usable_share_pct=round(100.0 * counting / reported, 1) if reported else None,
    )


#: The order a verdict sorts in: worst first, because the returns that need
#: work are the ones a reader opened this list to find.
_VERDICT_ORDER = {
    str(FitnessVerdict.NOT_FIT): 0,
    str(FitnessVerdict.FIT_WITH_NOTES): 1,
    str(FitnessVerdict.FIT): 2,
    str(FitnessVerdict.NO_DATA): 3,
}


def national_returns(
    db: Session,
    period: ReportingPeriod,
    *,
    state_code: str | None = None,
    cohort_code: str | None = None,
) -> NationalReturns:
    """How the period's returns stand nationally, or across a slice of them.

    Both filters reach here because this board once honoured neither: choosing
    Bauchi still showed "18 of 18 states assessed". A state wins over a cohort
    when both are set, because the narrower selection is the one the reader
    last asked for.
    """
    if state_code:
        states = [reference.get_state_by_code(db, state_code)]
    else:
        states = reference.active_states(db, cohort_code)
    rows = [state_return(db, state, period) for state in states]

    submitted = [row for row in rows if row.submission_id is not None]
    approved = [row for row in submitted if row.status == SubmissionStatus.APPROVED]
    on_time = [row for row in submitted if (row.days_late or 0) == 0]

    figures_reported = sum(row.figures_reported for row in submitted)
    figures_counting = sum(row.figures_counting for row in submitted)
    national_usable = (
        100.0 * figures_counting / figures_reported if figures_reported else None
    )

    dimension_totals: Counter[str] = Counter()
    for row in submitted:
        dimension_totals.update(row.findings_by_dimension)

    # Count the states each rule affects, not the raw number of findings, and
    # read every issue rather than the truncated per-return top-10.
    submission_ids = {
        row.submission_id: row.state_code for row in submitted if row.submission_id
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

    def _verdicts(verdict: FitnessVerdict) -> int:
        return sum(1 for row in submitted if row.fitness_verdict == str(verdict))

    return NationalReturns(
        period_code=period.code,
        states_expected=len(states),
        states_reported=len(submitted),
        states_approved=len(approved),
        reporting_rate_pct=round(len(submitted) / len(states) * 100, 2) if states else 0.0,
        on_time_rate_pct=round(len(on_time) / len(states) * 100, 2) if states else 0.0,
        figures_reported=figures_reported,
        figures_counting=figures_counting,
        usable_share_pct=None if national_usable is None else round(national_usable, 1),
        # The national headline, in place of an average score: how many states
        # a reader cannot rely on this quarter.
        states_not_fit=_verdicts(FitnessVerdict.NOT_FIT),
        states_fit_with_notes=_verdicts(FitnessVerdict.FIT_WITH_NOTES),
        states_fit=_verdicts(FitnessVerdict.FIT),
        findings_by_dimension=dict(dimension_totals),
        cohort_returns=[cohort_returns(db, cohort, period, rows) for cohort in cohorts],
        returns=sorted(
            rows,
            key=lambda row: (
                _VERDICT_ORDER.get(row.fitness_verdict or "", 4),
                -(row.error_count + row.warning_count),
                row.state_name,
            ),
        ),
        common_issues=common_issues,
    )
