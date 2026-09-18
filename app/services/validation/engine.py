"""Runs the rule catalogue over a submission and scores the seven DQA dimensions.

Scoring
-------
Five dimensions (integrity, accuracy, consistency, validity, uniqueness) use a
penalty model: every rule contributes a number of *checks* to its dimension and
each finding costs a fraction of one check according to its severity, so a
dimension's score is ``100 * (1 - penalty / checks)``.

Two dimensions are measured directly because a penalty model would understate
them:

* **completeness** is the share of expected indicators actually reported;
* **timeliness** decays from 100 by a fixed number of points per day late.

The seven scores are combined into one weighted mean. Because a mean can hide a
single badly failing dimension, the headline *grade* is additionally capped by
the weakest dimension (see ``grade_for_submission``): the score says how much
passed, the grade says whether anything needs attention.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import (
    DQA_WEIGHTS,
    DQADimension,
    Severity,
    SubmissionStatus,
    grade_for_score,
    grade_for_submission,
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
    DimensionScore,
    ValidationIssueRead,
    ValidationSummary,
)
from app.services import reference
from app.services.validation.rules import (
    RULE_REGISTRY,
    SEVERITY_PENALTY,
    Finding,
    RuleContext,
    RuleDefinition,
)

logger = get_logger(__name__)

#: Points deducted per day a submission arrives after the deadline.
TIMELINESS_DECAY_PER_DAY = 3.0
#: Flat deduction when data arrives against a closed reporting period.
CLOSED_PERIOD_PENALTY = 10.0
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


def _timeliness_score(ctx: RuleContext) -> tuple[float, dict]:
    uploaded = ctx.submission.uploaded_at
    if uploaded is None:
        return 0.0, {"submitted": False, "days_late": None}
    late = max(0, (uploaded.date() - ctx.period.due_date).days)
    score = max(0.0, 100.0 - TIMELINESS_DECAY_PER_DAY * late)
    if not ctx.period.is_open:
        score = max(0.0, score - CLOSED_PERIOD_PENALTY)
    return score, {
        "submitted": True,
        "days_late": late,
        "due_date": ctx.period.due_date.isoformat(),
        "submitted_on": uploaded.date().isoformat(),
        "on_time": late == 0,
    }


def _completeness_score(ctx: RuleContext) -> tuple[float, dict]:
    expected = ctx.expected_indicator_ids
    if not expected:
        return 100.0, {"expected": 0, "reported": 0, "note": "No reporting obligation configured"}
    reported = len(expected & ctx.reported_indicator_ids)
    score = reported / len(expected) * 100.0
    return score, {
        "expected": len(expected),
        "reported": reported,
        "missing": len(expected) - reported,
    }


def run_validation(db: Session, submission: Submission, *, persist: bool = True) -> ValidationSummary:
    """Execute every active rule against ``submission`` and score its data quality."""
    ctx = build_context(db, submission)
    overrides = _load_overrides(db)
    ctx.overrides = {
        code: (row.config or {}) for code, row in overrides.items() if row.config
    }

    issues: list[ValidationIssue] = []
    issue_reads: list[ValidationIssueRead] = []
    checks: dict[DQADimension, float] = {dimension: 0.0 for dimension in DQADimension}
    penalties: dict[DQADimension, float] = {dimension: 0.0 for dimension in DQADimension}
    failed: dict[DQADimension, int] = {dimension: 0 for dimension in DQADimension}
    rule_penalties: dict[str, float] = {}
    counts = {Severity.ERROR: 0, Severity.WARNING: 0, Severity.INFO: 0}
    blocking = False

    for definition in RULE_REGISTRY.values():
        override = overrides.get(definition.code)
        if override is not None and not override.is_active:
            continue

        weight = max(definition.weight_fn(ctx), 1)
        checks[definition.dimension] += weight

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
            penalties[definition.dimension] += SEVERITY_PENALTY[severity]
            rule_penalties[definition.code] = (
                rule_penalties.get(definition.code, 0.0) + SEVERITY_PENALTY[severity]
            )
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

    # --- score each dimension -------------------------------------------
    dimension_scores: list[DimensionScore] = []
    weighted_total = 0.0
    weight_total = 0.0

    for dimension in DQADimension:
        details: dict = {}
        if dimension == DQADimension.TIMELINESS:
            score, details = _timeliness_score(ctx)
        elif dimension == DQADimension.COMPLETENESS:
            score, details = _completeness_score(ctx)
            # The score already reflects the indicators COM-001 found missing;
            # only the missing-components rule deducts on top of it.
            component_penalty = min(rule_penalties.get("COM-002", 0.0) * 0.5, 10.0)
            score = max(0.0, score - component_penalty)
            details["component_penalty"] = round(component_penalty, 2)
        else:
            denominator = max(checks[dimension], 1.0)
            score = max(0.0, 100.0 * (1.0 - penalties[dimension] / denominator))
            details = {
                "checks": int(checks[dimension]),
                "penalty": round(penalties[dimension], 2),
            }

        score = round(min(100.0, max(0.0, score)), 2)
        weight = DQA_WEIGHTS[dimension]
        weighted_total += score * weight
        weight_total += weight

        dimension_scores.append(
            DimensionScore(
                dimension=str(dimension),
                score=score,
                weight=weight,
                checks_run=int(checks[dimension]),
                checks_failed=failed[dimension],
                grade=grade_for_score(score),
                details=details,
            )
        )

    overall = round(weighted_total / weight_total, 2) if weight_total else 0.0
    grade = grade_for_submission(overall, [d.score for d in dimension_scores])

    summary = ValidationSummary(
        submission_id=submission.id,
        passed=counts[Severity.ERROR] == 0,
        blocking=blocking,
        error_count=counts[Severity.ERROR],
        warning_count=counts[Severity.WARNING],
        info_count=counts[Severity.INFO],
        overall_score=overall,
        grade=grade,
        dimensions=dimension_scores,
        issues=issue_reads,
    )

    if persist:
        _persist(db, submission, issues, dimension_scores, summary)

    logger.info(
        "validation complete",
        extra={
            "submission_id": submission.id,
            "state": ctx.state.code,
            "period": ctx.period.code,
            "errors": summary.error_count,
            "warnings": summary.warning_count,
            "dqa_score": overall,
            "blocking": blocking,
        },
    )
    return summary


def _persist(
    db: Session,
    submission: Submission,
    issues: list[ValidationIssue],
    dimension_scores: list[DimensionScore],
    summary: ValidationSummary,
) -> None:
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
    for dimension in dimension_scores:
        db.add(
            DQAScore(
                submission_id=submission.id,
                dimension=dimension.dimension,
                score=dimension.score,
                weight=dimension.weight,
                checks_run=dimension.checks_run,
                checks_failed=dimension.checks_failed,
                details=dimension.details,
            )
        )

    submission.dqa_score = summary.overall_score
    submission.dqa_grade = summary.grade
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


def summarise_submission(db: Session, submission: Submission) -> ValidationSummary:
    """Rebuild a summary from stored issues and scores, without re-running rules."""
    issues = list(
        db.scalars(select(ValidationIssue).where(ValidationIssue.submission_id == submission.id))
    )
    scores = list(
        db.scalars(select(DQAScore).where(DQAScore.submission_id == submission.id))
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
        overall_score=submission.dqa_score or 0.0,
        grade=submission.dqa_grade or grade_for_score(submission.dqa_score),
        dimensions=[
            DimensionScore(
                dimension=score.dimension,
                score=score.score,
                weight=score.weight,
                checks_run=score.checks_run,
                checks_failed=score.checks_failed,
                grade=grade_for_score(score.score),
                details=score.details or {},
            )
            for score in sorted(scores, key=lambda s: s.dimension)
        ],
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
