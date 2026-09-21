"""Runs the rule catalogue over a submission and records what it finds.

There is no score and no grade. There was: seven weighted dimensions combined
into a 0-100 number and mapped onto Excellent/Good/Fair/Weak/Poor. It was a
pass rate over thousands of automated checks, so it sat near 100 for any
plausible return -- Gombe scored 99.13 in a quarter where it reported 127
schools against 5,960 the quarter before -- and readers took "Excellent" as an
answer to "can I use this?". Capping the grade by the weakest dimension and by
the share of figures under query helped, but the number underneath still
answered the wrong question.

So the engine now reports findings and nothing else. What a reader needs is
the finding, the state it belongs to, and what it does to the national figure;
those are the query, the fitness verdict and the exposure calculation. Scoring
a return is work for a data quality assessment with a field visit behind it,
and belongs in a subsystem of its own.

``DQADimension`` survives as a category -- which kind of problem a finding is
-- because that is useful on a query. Nothing weights it any more.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import (
    DQADimension,
    Severity,
    SubmissionStatus,
)
from app.core.logging_config import get_logger
from app.models import (
    DQAScore,
    Indicator,
    IndicatorValue,
    ReportingPeriod,
    StateSubcomponent,
    Submission,
    Target,
    ValidationIssue,
    ValidationRule,
)
from app.schemas.validation import (
    ValidationIssueRead,
    ValidationSummary,
)
from app.services import reconciliation, reference
from app.services.validation.rules import (
    RULE_REGISTRY,
    Finding,
    RuleContext,
    RuleDefinition,
)

logger = get_logger(__name__)

#: How many earlier periods a rule may look back over.
HISTORY_DEPTH = 8


# --------------------------------------------------------------------------
# Rule catalogue synchronisation
# --------------------------------------------------------------------------
def sync_rule_catalog(db: Session) -> int:
    """Mirror code-defined rules into the database so they can be tuned."""
    existing = {row.code: row for row in db.scalars(select(ValidationRule))}
    created = 0
    for definition in RULE_REGISTRY.values():
        row = existing.get(definition.code)
        if row is None:
            db.add(
                ValidationRule(
                    code=definition.code,
                    name=definition.name,
                    description=definition.description,
                    dimension=str(definition.dimension),
                    severity=str(definition.severity),
                    config=dict(definition.default_config),
                    is_blocking=definition.is_blocking,
                    is_active=True,
                )
            )
            created += 1
        else:
            # Keep descriptive metadata in step with the code; leave operator
            # overrides (severity, config, is_blocking, is_active) alone.
            row.name = definition.name
            row.description = definition.description
            row.dimension = str(definition.dimension)
    db.flush()
    return created


def _load_overrides(db: Session) -> dict[str, ValidationRule]:
    return {row.code: row for row in db.scalars(select(ValidationRule))}


# --------------------------------------------------------------------------
# Context assembly
# --------------------------------------------------------------------------
def _applicable_indicator_ids(
    db: Session, submission: Submission, indicators: list[Indicator]
) -> set[int]:
    """Indicators in the sub-components this state actually implements.

    Returns an empty set when no applicability matrix is configured, which
    callers treat as "no restriction".
    """
    implemented = set(
        db.scalars(
            select(StateSubcomponent.subcomponent_id).where(
                StateSubcomponent.state_id == submission.state_id,
                StateSubcomponent.implements.is_(True),
            )
        )
    )
    if not implemented:
        return set()
    return {
        indicator.id
        for indicator in indicators
        if indicator.is_active
        and indicator.is_reported
        and indicator.subcomponent_id in implemented
    }


def _expected_indicator_ids(
    db: Session, submission: Submission, indicators: list[Indicator], applicable: set[int]
) -> set[int]:
    """Indicators this state must report this period.

    Preference order: the sub-component applicability matrix, which is the real
    obligation; then state targets where they exist; then every active core
    indicator.
    """
    if applicable:
        return applicable

    targeted = set(
        db.scalars(
            select(Target.indicator_id).where(
                Target.period_id == submission.period_id,
                Target.state_id == submission.state_id,
            )
        )
    )
    if targeted:
        return targeted
    return {indicator.id for indicator in indicators if indicator.is_core and indicator.is_active}


def _national_context(
    db: Session, submission: Submission
) -> tuple[dict[int, float], dict[int, int]]:
    """Per-indicator national totals from the other states already approved.

    Used by the concentration check to ask whether one state accounts for an
    implausible share of the national figure.
    """
    peers = list(
        db.scalars(
            select(Submission.id).where(
                Submission.period_id == submission.period_id,
                Submission.is_current.is_(True),
                Submission.status == SubmissionStatus.APPROVED,
                Submission.id != submission.id,
            )
        )
    )
    totals: dict[int, float] = {}
    counts: dict[int, int] = {}
    if not peers:
        return totals, counts

    for value in db.scalars(
        select(IndicatorValue).where(IndicatorValue.submission_id.in_(peers))
    ):
        effective = value.effective_value
        if effective is None:
            continue
        totals[value.indicator_id] = totals.get(value.indicator_id, 0.0) + effective
        counts[value.indicator_id] = counts.get(value.indicator_id, 0) + 1
    return totals, counts


def _tracker_reconciliation(
    db: Session, submission: Submission, indicators: list[Indicator]
) -> tuple[list, bool]:
    """Reconcile this submission against the finer returns inside its period.

    Returns nothing at all unless the state has actually filed at least one of
    those finer returns. States are not yet reporting the monthly tracker, and
    a rule that fires 53 times the moment a quarterly return arrives -- because
    a stream nobody has started is empty -- would bury the real findings. The
    checks switch themselves on, state by state, as tracker reporting starts.
    """
    period = submission.period
    if period is None or reconciliation.finer_grain(period.period_type) is None:
        return [], False
    result = reconciliation.reconcile_state(
        db, submission.state, period, indicators=indicators
    )
    if not result.fine_submission_ids:
        return [], False
    return result.lines, result.is_complete


def _approved_history(
    db: Session, submission: Submission, depth: int = HISTORY_DEPTH
) -> list[Submission]:
    """Previously approved submissions for this state, oldest first."""
    rows = list(
        db.scalars(
            select(Submission).where(
                Submission.state_id == submission.state_id,
                Submission.id != submission.id,
                Submission.status == SubmissionStatus.APPROVED,
            )
        )
    )
    periods = {
        period.id: period
        for period in db.scalars(
            select(ReportingPeriod).where(
                ReportingPeriod.id.in_({row.period_id for row in rows} or {0})
            )
        )
    }
    rows.sort(key=lambda row: periods[row.period_id].sort_key if row.period_id in periods else (0, 0, 0))
    return rows[-depth:]


def build_context(db: Session, submission: Submission) -> RuleContext:
    state = submission.state
    period = submission.period
    values = list(
        db.scalars(select(IndicatorValue).where(IndicatorValue.submission_id == submission.id))
    )
    indicators = list(db.scalars(select(Indicator)))
    indicators_by_id = {indicator.id: indicator for indicator in indicators}

    targets = {
        row.indicator_id: row.target_value
        for row in db.scalars(
            select(Target).where(
                Target.period_id == submission.period_id,
                Target.state_id == submission.state_id,
            )
        )
    }

    previous_period = reference.preceding_period(db, period)
    previous_values: dict[int, float] = {}
    previous_is_provisional = False
    if previous_period is not None:
        # Prefer the approved figure, but fall back to whatever the state last
        # submitted. The quality gate governs what reaches *analysis*; a
        # comparison is a different question, and a state whose previous
        # quarter was rejected is precisely the one worth comparing. Findings
        # built on an unapproved baseline say so.
        previous_submission = db.scalar(
            select(Submission).where(
                Submission.state_id == submission.state_id,
                Submission.period_id == previous_period.id,
                Submission.is_current.is_(True),
                Submission.status == SubmissionStatus.APPROVED,
            )
        )
        if previous_submission is None:
            candidates = list(
                db.scalars(
                    select(Submission).where(
                        Submission.state_id == submission.state_id,
                        Submission.period_id == previous_period.id,
                    )
                )
            )
            if candidates:
                previous_submission = max(candidates, key=lambda row: row.version)
                previous_is_provisional = True

        if previous_submission is not None:
            for value in db.scalars(
                select(IndicatorValue).where(
                    IndicatorValue.submission_id == previous_submission.id
                )
            ):
                effective = value.effective_value
                if effective is not None:
                    previous_values[value.indicator_id] = effective

    history: dict[int, list[float]] = {}
    historical_ids = [row.id for row in _approved_history(db, submission)]
    if historical_ids:
        for value in db.scalars(
            select(IndicatorValue).where(IndicatorValue.submission_id.in_(historical_ids))
        ):
            effective = value.effective_value
            if effective is not None:
                history.setdefault(value.indicator_id, []).append(effective)

    duplicates: list[int] = []
    if submission.file_hash:
        duplicates = list(
            db.scalars(
                select(Submission.id).where(
                    Submission.file_hash == submission.file_hash,
                    Submission.id != submission.id,
                )
            )
        )

    conflicting = list(
        db.scalars(
            select(Submission.id).where(
                Submission.state_id == submission.state_id,
                Submission.period_id == submission.period_id,
                Submission.is_current.is_(True),
                Submission.id != submission.id,
            )
        )
    )
    submission.__dict__["_conflicting_current_ids"] = conflicting

    applicable = _applicable_indicator_ids(db, submission, indicators)
    national_totals, national_counts = _national_context(db, submission)
    reconciled, tracker_complete = _tracker_reconciliation(db, submission, indicators)

    return RuleContext(
        submission=submission,
        state=state,
        period=period,
        values=values,
        indicators_by_id=indicators_by_id,
        expected_indicator_ids=_expected_indicator_ids(db, submission, indicators, applicable),
        previous_values=previous_values,
        history=history,
        targets=targets,
        duplicate_submission_ids=duplicates,
        settings=settings,
        applicable_indicator_ids=applicable,
        national_totals=national_totals,
        national_state_counts=national_counts,
        previous_is_provisional=previous_is_provisional,
        reconciliation=reconciled,
        reconciliation_is_complete=tracker_complete,
    )


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------
def _severity_for(definition: RuleDefinition, override: ValidationRule | None, finding: Finding) -> Severity:
    if finding.severity is not None:
        return finding.severity
    if override is not None and override.severity:
        try:
            return Severity(override.severity)
        except ValueError:
            pass
    return definition.severity


def _usable_share(ctx: RuleContext) -> float | None:
    """Percentage of this state's reported figures that are fit for use.

    Every reported figure counts towards the national totals regardless --
    holding the doubtful ones out made this platform's totals disagree with
    the NPCU's own published report. This says how much of what the state
    filed a reader can lean on, not how much of it was counted.
    """
    reported = [value for value in ctx.values if value.effective_value is not None]
    if not reported:
        return None
    fit = sum(1 for value in reported if value.is_valid)
    return 100.0 * fit / len(reported)


def run_validation(db: Session, submission: Submission, *, persist: bool = True) -> ValidationSummary:
    """Execute every active rule against ``submission`` and score its data quality."""
    ctx = build_context(db, submission)
    overrides = _load_overrides(db)
    ctx.overrides = {
        code: (row.config or {}) for code, row in overrides.items() if row.config
    }

    issues: list[ValidationIssue] = []
    issue_reads: list[ValidationIssueRead] = []
    #: Findings per dimension. A count, not a score: it says how many things
    #: need looking at in each area, and makes no claim about how good the
    #: return is overall.
    failed: dict[DQADimension, int] = {dimension: 0 for dimension in DQADimension}
    counts = {Severity.ERROR: 0, Severity.WARNING: 0, Severity.INFO: 0}
    blocking = False

    for definition in RULE_REGISTRY.values():
        override = overrides.get(definition.code)
        if override is not None and not override.is_active:
            continue

        try:
            findings = list(definition.fn(ctx))
        except Exception:  # noqa: BLE001 - a broken rule must not fail the upload
            logger.exception(
                "validation rule raised", extra={"rule_code": definition.code}
            )
            continue

        is_blocking_rule = (
            override.is_blocking if override is not None else definition.is_blocking
        )

        for finding in findings:
            severity = _severity_for(definition, override, finding)
            counts[severity] += 1
            failed[definition.dimension] += 1
            finding_blocks = is_blocking_rule and severity == Severity.ERROR
            blocking = blocking or finding_blocks

            issue = ValidationIssue(
                submission_id=submission.id,
                rule_code=definition.code,
                dimension=str(definition.dimension),
                severity=str(severity),
                indicator_id=finding.indicator_id,
                field=finding.field,
                message=finding.message,
                observed=finding.observed,
                expected=finding.expected,
                source_row=finding.source_row,
                is_blocking=finding_blocks,
                context=finding.context or {},
            )
            issues.append(issue)
            issue_reads.append(
                ValidationIssueRead(
                    rule_code=definition.code,
                    dimension=str(definition.dimension),
                    severity=str(severity),
                    indicator_id=finding.indicator_id,
                    indicator_code=finding.indicator_code,
                    field=finding.field,
                    message=finding.message,
                    observed=finding.observed,
                    expected=finding.expected,
                    source_row=finding.source_row,
                    is_blocking=finding_blocks,
                )
            )

    # No score and no grade. The platform judges a return by whether it can
    # be used, not by what share of automated checks it passed -- that pass
    # rate sat near 100 for any plausible return, and a state reporting 127
    # schools where it had reported 5,960 scored 99.13 and read "Excellent".
    # Scoring a return is work for a data quality assessment with a field
    # visit behind it; this counts findings and says what they mean.
    usable_share = _usable_share(ctx)
    findings_by_dimension = {
        str(dimension): count for dimension, count in failed.items() if count
    }

    summary = ValidationSummary(
        submission_id=submission.id,
        passed=counts[Severity.ERROR] == 0,
        blocking=blocking,
        error_count=counts[Severity.ERROR],
        warning_count=counts[Severity.WARNING],
        info_count=counts[Severity.INFO],
        usable_share_pct=None if usable_share is None else round(usable_share, 1),
        findings_by_dimension=findings_by_dimension,
        issues=issue_reads,
    )

    if persist:
        _persist(db, submission, issues, summary)

    logger.info(
        "validation complete",
        extra={
            "submission_id": submission.id,
            "state": ctx.state.code,
            "period": ctx.period.code,
            "errors": summary.error_count,
            "warnings": summary.warning_count,
            "blocking": blocking,
        },
    )
    return summary


def _persist(
    db: Session,
    submission: Submission,
    issues: list[ValidationIssue],
    summary: ValidationSummary,
) -> None:
    """Replace this submission's findings with the ones just raised.

    Any dimension scores left over from before scoring was removed go with
    them, so a re-validation clears the last trace of a grade nobody trusted.
    """
    for stale in db.scalars(
        select(ValidationIssue).where(ValidationIssue.submission_id == submission.id)
    ):
        db.delete(stale)
    for stale_score in db.scalars(
        select(DQAScore).where(DQAScore.submission_id == submission.id)
    ):
        db.delete(stale_score)
    db.flush()
    db.add_all(issues)

    submission.error_count = summary.error_count
    submission.warning_count = summary.warning_count

    # A finding no longer takes the whole return down. Figures the rules cannot
    # use are quarantined individually and queried with the state that reported
    # them; everything else still counts. A cumulative figure that falls may be
    # a genuine downward restatement once evidence is produced, and blocking it
    # would penalise exactly the correction the process exists to capture.
    #
    # Re-validation must not quietly withdraw an approval: rules are re-run
    # whenever a threshold changes or a figure is restated, and an approved
    # return stays approved unless someone decides otherwise.
    if submission.status != SubmissionStatus.APPROVED:
        submission.status = str(SubmissionStatus.VALIDATED)
    submission.rejection_reason = None

    db.flush()


def _count_by_dimension(issues: list[ValidationIssue]) -> dict[str, int]:
    """How many findings fall in each area -- a count, never a score."""
    counts: dict[str, int] = {}
    for issue in issues:
        if Severity(issue.severity) is Severity.INFO or not issue.dimension:
            continue
        counts[issue.dimension] = counts.get(issue.dimension, 0) + 1
    return counts


def summarise_submission(db: Session, submission: Submission) -> ValidationSummary:
    """Rebuild a summary from stored findings, without re-running the rules."""
    issues = list(
        db.scalars(select(ValidationIssue).where(ValidationIssue.submission_id == submission.id))
    )
    indicator_codes = {
        indicator.id: indicator.code
        for indicator in db.scalars(
            select(Indicator).where(
                Indicator.id.in_({i.indicator_id for i in issues if i.indicator_id} or {0})
            )
        )
    }

    return ValidationSummary(
        submission_id=submission.id,
        passed=submission.error_count == 0,
        blocking=any(issue.is_blocking for issue in issues),
        error_count=sum(1 for i in issues if i.severity == Severity.ERROR),
        warning_count=sum(1 for i in issues if i.severity == Severity.WARNING),
        info_count=sum(1 for i in issues if i.severity == Severity.INFO),
        findings_by_dimension=_count_by_dimension(issues),
        issues=[
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
            for issue in issues
        ],
    )
