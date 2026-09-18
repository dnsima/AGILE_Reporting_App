"""The rule catalogue.

Each rule is a small function that inspects a :class:`RuleContext` and yields
:class:`Finding` objects. Rules declare the DQA dimension they serve, a default
severity and whether a failure blocks the submission from entering the analysis
pipeline. Defaults are mirrored into the ``validation_rules`` table at startup
so an administrator can retune thresholds or disable a rule without a redeploy.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator

# ``field`` is aliased because ``Finding`` declares an attribute of that name,
# which would otherwise shadow the dataclasses helper inside the class body.
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import date
from typing import Any

from app.core.enums import DQADimension, IndicatorUnit, ReconciliationStatus, Severity
from app.models import Indicator, IndicatorValue, ReportingPeriod, State, Submission
from app.services.reconciliation import ReconciliationLine

#: Parent -> child indicator codes whose totals must not be exceeded.
SUBSET_RELATIONSHIPS: dict[str, str] = {
    "KPI-025": "KPI-024",  # female teachers recruited <= teachers recruited
    "KPI-033": "KPI-032",  # functional safe spaces <= safe spaces established
    "KPI-007": "KPI-001",  # girls receiving CCT <= girls benefiting
}

ALLOWED_DISAGGREGATIONS: dict[str, set[str]] = {
    "sex": {"female", "male", "girls", "boys", "women", "men", "total", "all"},
    "location": {"urban", "rural", "total", "all"},
    "school_level": {
        "primary", "jss", "sss", "junior secondary", "senior secondary",
        "secondary", "ecd", "non formal", "non-formal", "total", "all",
    },
}

#: Weight applied to a failed check when scoring its dimension.
SEVERITY_PENALTY = {Severity.ERROR: 1.0, Severity.WARNING: 0.45, Severity.INFO: 0.1}


@dataclass
class Finding:
    """One rule failure."""

    message: str
    indicator_id: int | None = None
    indicator_code: str | None = None
    field: str | None = None
    observed: str | None = None
    expected: str | None = None
    source_row: int | None = None
    severity: Severity | None = None
    context: dict[str, Any] = dc_field(default_factory=dict)


@dataclass
class RuleContext:
    """Everything a rule needs, assembled once per submission."""

    submission: Submission
    state: State
    period: ReportingPeriod
    values: list[IndicatorValue]
    indicators_by_id: dict[int, Indicator]
    #: Indicators this state is obliged to report this period.
    expected_indicator_ids: set[int]
    #: Latest approved value per indicator from the preceding period.
    previous_values: dict[int, float]
    #: Historical series per indicator (oldest first) for outlier detection.
    history: dict[int, list[float]]
    #: State-level targets for this period, keyed by indicator id.
    targets: dict[int, float]
    #: Ids of other submissions sharing this file hash.
    duplicate_submission_ids: list[int]
    settings: Any
    #: Rule configuration overrides loaded from the database.
    overrides: dict[str, dict[str, Any]] = dc_field(default_factory=dict)
    #: Indicators this state is expected to report, from the sub-component
    #: applicability matrix. Empty means no matrix is configured.
    applicable_indicator_ids: set[int] = dc_field(default_factory=set)
    #: National total per indicator from the other states already approved for
    #: this period, used for the concentration check.
    national_totals: dict[int, float] = dc_field(default_factory=dict)
    #: How many states contributed to each of those totals.
    national_state_counts: dict[int, int] = dc_field(default_factory=dict)
    #: True when the previous period's figures come from a submission that did
    #: not pass validation, so comparisons against them are indicative.
    previous_is_provisional: bool = False
    #: Performance-tracker reconciliation for this state and period. Empty
    #: unless the state has filed at least one of the finer returns, so the
    #: tracker rules stay silent until tracker reporting actually starts.
    reconciliation: list[ReconciliationLine] = dc_field(default_factory=list)
    #: True when every finer period inside this one has been reported, which is
    #: what makes an indicator missing from the tracker a real omission rather
    #: than one that may still arrive.
    reconciliation_is_complete: bool = False

    def reconciled(self, status: ReconciliationStatus) -> list[ReconciliationLine]:
        return [line for line in self.reconciliation if line.status is status]

    @property
    def baseline_note(self) -> str:
        return (
            " The previous figure is from a submission that did not pass validation."
            if self.previous_is_provisional
            else ""
        )

    def config(self, rule_code: str, key: str, default: Any) -> Any:
        return (self.overrides.get(rule_code) or {}).get(key, default)

    def indicator(self, value: IndicatorValue) -> Indicator | None:
        return self.indicators_by_id.get(value.indicator_id)

    @property
    def reported_indicator_ids(self) -> set[int]:
        return {
            value.indicator_id
            for value in self.values
            if value.value is not None or value.numerator is not None
        }


RuleFn = Callable[[RuleContext], Iterator[Finding]]


@dataclass
class RuleDefinition:
    code: str
    name: str
    description: str
    dimension: DQADimension
    severity: Severity
    is_blocking: bool
    default_config: dict[str, Any]
    fn: RuleFn
    #: How many checks this rule contributes to its dimension's denominator.
    weight_fn: Callable[[RuleContext], int]


RULE_REGISTRY: dict[str, RuleDefinition] = {}


def rule(
    code: str,
    name: str,
    dimension: DQADimension,
    *,
    severity: Severity = Severity.ERROR,
    blocking: bool = False,
    description: str = "",
    config: dict[str, Any] | None = None,
    weight: Callable[[RuleContext], int] | None = None,
) -> Callable[[RuleFn], RuleFn]:
    def decorator(fn: RuleFn) -> RuleFn:
        RULE_REGISTRY[code] = RuleDefinition(
            code=code,
            name=name,
            description=description or (fn.__doc__ or "").strip(),
            dimension=dimension,
            severity=severity,
            is_blocking=blocking,
            default_config=config or {},
            fn=fn,
            weight_fn=weight or (lambda ctx: max(len(ctx.values), 1)),
        )
        return fn

    return decorator


def _one(_: RuleContext) -> int:
    return 1


def _numeric_values(ctx: RuleContext) -> Iterator[tuple[IndicatorValue, Indicator, float]]:
    for value in ctx.values:
        indicator = ctx.indicator(value)
        if indicator is None:
            continue
        effective = value.effective_value
        if effective is None:
            continue
        yield value, indicator, effective


# ==========================================================================
# INTEGRITY
# ==========================================================================
@rule(
    "INT-001",
    "Numerator does not exceed denominator",
    DQADimension.INTEGRITY,
    severity=Severity.ERROR,
    blocking=True,
    description="For ratio-style indicators the numerator must be a subset of the denominator.",
)
def numerator_within_denominator(ctx: RuleContext) -> Iterator[Finding]:
    for value in ctx.values:
        indicator = ctx.indicator(value)
        if indicator is None or value.numerator is None or value.denominator is None:
            continue
        if value.denominator and value.numerator > value.denominator:
            yield Finding(
                message=(
                    f"{indicator.code}: numerator ({value.numerator:g}) exceeds the "
                    f"denominator ({value.denominator:g})."
                ),
                indicator_id=indicator.id,
                indicator_code=indicator.code,
                field="numerator",
                observed=f"{value.numerator:g}",
                expected=f"<= {value.denominator:g}",
                source_row=value.source_row,
            )


@rule(
    "INT-002",
    "Reported percentage matches numerator over denominator",
    DQADimension.INTEGRITY,
    severity=Severity.WARNING,
    description="A reported percentage should agree with its own numerator and denominator.",
    config={"tolerance_pct_points": 0.5},
)
def percentage_arithmetic(ctx: RuleContext) -> Iterator[Finding]:
    tolerance = float(ctx.config("INT-002", "tolerance_pct_points", 0.5))
    for value in ctx.values:
        indicator = ctx.indicator(value)
        if indicator is None or indicator.unit != IndicatorUnit.PERCENT:
            continue
        if value.value is None or value.numerator is None or not value.denominator:
            continue
        derived = value.numerator / value.denominator * 100.0
        if abs(derived - value.value) > tolerance:
            yield Finding(
                message=(
                    f"{indicator.code}: reported {value.value:g}% but numerator/denominator "
                    f"gives {derived:.2f}%."
                ),
                indicator_id=indicator.id,
                indicator_code=indicator.code,
                field="value",
                observed=f"{value.value:g}",
                expected=f"{derived:.2f}",
                source_row=value.source_row,
            )


@rule(
    "INT-003",
    "Subset indicators do not exceed their parent",
    DQADimension.INTEGRITY,
    severity=Severity.ERROR,
    blocking=True,
    description="Disaggregated counts must not exceed the total they are drawn from.",
    weight=lambda ctx: len(SUBSET_RELATIONSHIPS),
)
def subset_within_parent(ctx: RuleContext) -> Iterator[Finding]:
    totals: dict[str, float] = defaultdict(float)
    indicator_by_code: dict[str, Indicator] = {}
    for _value, indicator, effective in _numeric_values(ctx):
        totals[indicator.code] += effective
        indicator_by_code[indicator.code] = indicator

    for child_code, parent_code in SUBSET_RELATIONSHIPS.items():
        if child_code not in totals or parent_code not in totals:
            continue
        if totals[child_code] > totals[parent_code]:
            child = indicator_by_code[child_code]
            yield Finding(
                message=(
                    f"{child_code} ({totals[child_code]:g}) exceeds {parent_code} "
                    f"({totals[parent_code]:g}), which it must be a subset of."
                ),
                indicator_id=child.id,
                indicator_code=child_code,
                field="value",
                observed=f"{totals[child_code]:g}",
                expected=f"<= {totals[parent_code]:g}",
            )


@rule(
    "INT-004",
    "All submitted rows were mapped to the unified schema",
    DQADimension.INTEGRITY,
    severity=Severity.WARNING,
    description="Rows the mapper could not resolve are dropped and never reach analysis.",
    weight=_one,
)
def rows_fully_mapped(ctx: RuleContext) -> Iterator[Finding]:
    if ctx.submission.unmapped_count > 0:
        yield Finding(
            message=(
                f"{ctx.submission.unmapped_count} of {ctx.submission.row_count} rows could not "
                "be matched to a known indicator and were not ingested."
            ),
            field="file",
            observed=str(ctx.submission.unmapped_count),
            expected="0",
        )


@rule(
    "INT-005",
    "Composite indicators equal the sum of their parts",
    DQADimension.INTEGRITY,
    severity=Severity.ERROR,
    blocking=True,
    description=(
        "A composite such as C1.0-01 must equal its sub-component parts. "
        "Non-implementation is reported as zero, so the identity holds for "
        "every state regardless of what it implements."
    ),
    config={"tolerance": 0.5},
    weight=lambda ctx: max(
        sum(1 for i in ctx.indicators_by_id.values() if i.composite_of), 1
    ),
)
def composite_equals_parts(ctx: RuleContext) -> Iterator[Finding]:
    tolerance = float(ctx.config("INT-005", "tolerance", 0.5))
    totals: dict[str, float] = {}
    for _value, indicator, effective in _numeric_values(ctx):
        totals[indicator.code] = totals.get(indicator.code, 0.0) + effective

    for indicator in ctx.indicators_by_id.values():
        parts = indicator.composite_of or []
        if not parts or indicator.code not in totals:
            continue
        if any(part not in totals for part in parts):
            continue  # a part is unreported; completeness covers that
        expected = sum(totals[part] for part in parts)
        reported = totals[indicator.code]
        if abs(reported - expected) > tolerance:
            yield Finding(
                message=(
                    f"{indicator.code} reports {reported:g} but its parts "
                    f"({' + '.join(parts)}) sum to {expected:g}, a gap of "
                    f"{reported - expected:+g}."
                ),
                indicator_id=indicator.id,
                indicator_code=indicator.code,
                field="value",
                observed=f"{reported:g}",
                expected=f"{expected:g}",
                context={"parts": parts, "gap": round(reported - expected, 4)},
            )


@rule(
    "APP-001",
    "Figures are only reported where the state implements",
    DQADimension.VALIDITY,
    severity=Severity.WARNING,
    description=(
        "A value against a sub-component the state does not implement is "
        "usually a mis-keyed row or the wrong state's file."
    ),
)
def within_applicable_scope(ctx: RuleContext) -> Iterator[Finding]:
    if not ctx.applicable_indicator_ids:
        return  # no applicability matrix configured
    for value, indicator, effective in _numeric_values(ctx):
        if indicator.id in ctx.applicable_indicator_ids or not effective:
            continue
        subcomponent = getattr(indicator.subcomponent, "code", "this sub-component")
        yield Finding(
            message=(
                f"{indicator.code}: {effective:g} reported, but {ctx.state.name} "
                f"does not implement {subcomponent}."
            ),
            indicator_id=indicator.id,
            indicator_code=indicator.code,
            field="value",
            observed=f"{effective:g}",
            expected="blank or zero",
            source_row=value.source_row,
        )


# ==========================================================================
# TIMELINESS
# ==========================================================================
@rule(
    "TIM-001",
    "Submission received by the reporting deadline",
    DQADimension.TIMELINESS,
    severity=Severity.WARNING,
    description="Submissions are due within the window configured on the reporting period.",
    weight=_one,
)
def submitted_on_time(ctx: RuleContext) -> Iterator[Finding]:
    uploaded = ctx.submission.uploaded_at
    if uploaded is None:
        return
    submitted_on = uploaded.date()
    if submitted_on > ctx.period.due_date:
        days_late = (submitted_on - ctx.period.due_date).days
        yield Finding(
            message=(
                f"Submitted {days_late} day(s) after the {ctx.period.due_date.isoformat()} "
                f"deadline for {ctx.period.code}."
            ),
            field="uploaded_at",
            observed=submitted_on.isoformat(),
            expected=ctx.period.due_date.isoformat(),
            context={"days_late": days_late},
        )


@rule(
    "TIM-002",
    "Reporting period is open for submission",
    DQADimension.TIMELINESS,
    severity=Severity.WARNING,
    description="Flags data arriving against a period the NPCU has already closed.",
    weight=_one,
)
def period_still_open(ctx: RuleContext) -> Iterator[Finding]:
    if not ctx.period.is_open:
        yield Finding(
            message=f"Reporting period {ctx.period.code} is closed for new submissions.",
            field="period",
            observed="closed",
            expected="open",
        )


# ==========================================================================
# ACCURACY
# ==========================================================================
@rule(
    "ACC-001",
    "Achievement against target is plausible",
    DQADimension.ACCURACY,
    severity=Severity.WARNING,
    description="Achievement far above target usually signals a unit or period error.",
    config={"max_ratio_pct": 300.0},
)
def plausible_versus_target(ctx: RuleContext) -> Iterator[Finding]:
    ceiling = float(
        ctx.config("ACC-001", "max_ratio_pct", ctx.settings.accuracy_target_ratio_pct)
    )
    for value, indicator, effective in _numeric_values(ctx):
        target = ctx.targets.get(indicator.id)
        if not target:
            continue
        ratio = effective / target * 100.0
        if ratio > ceiling:
            yield Finding(
                message=(
                    f"{indicator.code}: reported {effective:g} is {ratio:.0f}% of the "
                    f"{target:g} target."
                ),
                indicator_id=indicator.id,
                indicator_code=indicator.code,
                field="value",
                observed=f"{effective:g}",
                expected=f"<= {target * ceiling / 100:.0f}",
                source_row=value.source_row,
                context={"achievement_pct": round(ratio, 1)},
            )


@rule(
    "ACC-002",
    "Value is not a statistical outlier against the state's own history",
    DQADimension.ACCURACY,
    severity=Severity.WARNING,
    description="Compares the reading with this state's previous readings for the indicator.",
    config={"zscore_threshold": 3.0, "min_history": 3},
)
def outlier_against_history(ctx: RuleContext) -> Iterator[Finding]:
    threshold = float(
        ctx.config("ACC-002", "zscore_threshold", ctx.settings.outlier_zscore_threshold)
    )
    min_history = int(ctx.config("ACC-002", "min_history", 3))
    for value, indicator, effective in _numeric_values(ctx):
        series = ctx.history.get(indicator.id, [])
        if len(series) < min_history:
            continue
        mean = statistics.fmean(series)
        try:
            deviation = statistics.stdev(series)
        except statistics.StatisticsError:
            continue
        if deviation <= 0:
            continue
        zscore = abs(effective - mean) / deviation
        if zscore > threshold:
            yield Finding(
                message=(
                    f"{indicator.code}: {effective:g} is {zscore:.1f} standard deviations from "
                    f"this state's historical mean of {mean:.1f}."
                ),
                indicator_id=indicator.id,
                indicator_code=indicator.code,
                field="value",
                observed=f"{effective:g}",
                expected=f"~{mean:.1f}",
                source_row=value.source_row,
                context={"zscore": round(zscore, 2)},
            )


@rule(
    "ACC-003",
    "Ratio indicators carry a usable denominator",
    DQADimension.ACCURACY,
    severity=Severity.ERROR,
    blocking=True,
    description="A non-zero numerator with a zero or missing denominator cannot be computed.",
)
def denominator_usable(ctx: RuleContext) -> Iterator[Finding]:
    for value in ctx.values:
        indicator = ctx.indicator(value)
        if indicator is None or not indicator.requires_numerator_denominator:
            continue
        if value.numerator is None:
            continue
        if value.denominator in (None, 0):
            yield Finding(
                message=(
                    f"{indicator.code}: numerator {value.numerator:g} supplied with a "
                    "missing or zero denominator."
                ),
                indicator_id=indicator.id,
                indicator_code=indicator.code,
                field="denominator",
                observed=str(value.denominator),
                expected="> 0",
                source_row=value.source_row,
            )


@rule(
    "ACC-004",
    "No single state dominates the national result",
    DQADimension.ACCURACY,
    severity=Severity.WARNING,
    description=(
        "One state accounting for most of a national total usually signals a "
        "unit error or a double count rather than genuine concentration."
    ),
    config={"max_share_pct": 60.0, "min_states": 5},
)
def state_concentration(ctx: RuleContext) -> Iterator[Finding]:
    ceiling = float(ctx.config("ACC-004", "max_share_pct", 60.0))
    min_states = int(ctx.config("ACC-004", "min_states", 5))

    for value, indicator, effective in _numeric_values(ctx):
        if not _is_additive(indicator):
            continue
        # The peer total excludes this state, so add its own figure back to get
        # the national total it is a share *of*. Dividing by the peers alone
        # would let a dominant state exceed 100%.
        peers_total = ctx.national_totals.get(indicator.id, 0.0)
        contributors = ctx.national_state_counts.get(indicator.id, 0) + 1
        national = peers_total + effective
        if not national or contributors < min_states or effective <= 0:
            continue
        share = effective / national * 100.0
        if share > ceiling:
            yield Finding(
                message=(
                    f"{indicator.code}: {ctx.state.name} alone is {share:.0f}% of the "
                    f"national total across {contributors} reporting states."
                ),
                indicator_id=indicator.id,
                indicator_code=indicator.code,
                field="value",
                observed=f"{effective:g}",
                expected=f"< {ceiling:.0f}% of {national:g}",
                source_row=value.source_row,
                context={"share_pct": round(share, 1), "national_total": national},
            )


def _is_additive(indicator: Indicator) -> bool:
    """Shares only mean something where the national figure is a total."""
    return str(indicator.aggregation_method) in {"SUM", "WEIGHTED_AVERAGE"}


# ==========================================================================
# COMPLETENESS
# ==========================================================================
@rule(
    "COM-001",
    "All expected indicators were reported",
    DQADimension.COMPLETENESS,
    severity=Severity.WARNING,
    description="Every indicator the state is obliged to report this period carries a value.",
    weight=lambda ctx: max(len(ctx.expected_indicator_ids), 1),
)
def expected_indicators_present(ctx: RuleContext) -> Iterator[Finding]:
    missing = ctx.expected_indicator_ids - ctx.reported_indicator_ids
    for indicator_id in sorted(missing):
        indicator = ctx.indicators_by_id.get(indicator_id)
        if indicator is None:
            continue
        yield Finding(
            message=f"{indicator.code} ({indicator.name}) was not reported for {ctx.period.code}.",
            indicator_id=indicator.id,
            indicator_code=indicator.code,
            field="value",
            observed="missing",
            expected="a reported value",
        )


@rule(
    "COM-002",
    "Numerator and denominator supplied where required",
    DQADimension.COMPLETENESS,
    severity=Severity.WARNING,
    description="Weighted national aggregation needs the components, not just the percentage.",
)
def components_supplied(ctx: RuleContext) -> Iterator[Finding]:
    for value in ctx.values:
        indicator = ctx.indicator(value)
        if indicator is None or not indicator.requires_numerator_denominator:
            continue
        if value.value is None and value.numerator is None:
            continue
        if value.numerator is None or value.denominator is None:
            yield Finding(
                message=(
                    f"{indicator.code}: numerator/denominator missing; the national roll-up "
                    "will fall back to an unweighted average."
                ),
                indicator_id=indicator.id,
                indicator_code=indicator.code,
                field="numerator",
                observed="missing",
                expected="numerator and denominator",
                source_row=value.source_row,
            )


# ==========================================================================
# CONSISTENCY
# ==========================================================================
@rule(
    "CON-001",
    "Period-over-period movement is within the expected band",
    DQADimension.CONSISTENCY,
    severity=Severity.WARNING,
    description="Very large swings between consecutive periods are flagged for verification.",
    config={"threshold_pct": 200.0},
)
def period_over_period_change(ctx: RuleContext) -> Iterator[Finding]:
    threshold = float(
        ctx.config("CON-001", "threshold_pct", ctx.settings.consistency_change_threshold_pct)
    )
    for value, indicator, effective in _numeric_values(ctx):
        previous = ctx.previous_values.get(indicator.id)
        if previous is None or previous == 0:
            continue
        change = abs(effective - previous) / abs(previous) * 100.0
        if change > threshold:
            yield Finding(
                message=(
                    f"{indicator.code}: changed {change:.0f}% from the previous period "
                    f"({previous:g} to {effective:g}).{ctx.baseline_note}"
                ),
                indicator_id=indicator.id,
                indicator_code=indicator.code,
                field="value",
                observed=f"{effective:g}",
                expected=f"within {threshold:.0f}% of {previous:g}",
                source_row=value.source_row,
                context={"change_pct": round(change, 1)},
            )


@rule(
    "CON-002",
    "Cumulative indicators never decrease",
    DQADimension.CONSISTENCY,
    severity=Severity.ERROR,
    blocking=True,
    description="A cumulative total cannot fall below the value reported in an earlier period.",
)
def cumulative_monotonic(ctx: RuleContext) -> Iterator[Finding]:
    for value, indicator, effective in _numeric_values(ctx):
        if not indicator.is_cumulative:
            continue
        previous = ctx.previous_values.get(indicator.id)
        if previous is None:
            continue
        if effective < previous:
            yield Finding(
                message=(
                    f"{indicator.code} is cumulative but fell from {previous:g} to "
                    f"{effective:g}.{ctx.baseline_note}"
                ),
                indicator_id=indicator.id,
                indicator_code=indicator.code,
                field="value",
                observed=f"{effective:g}",
                expected=f">= {previous:g}",
                source_row=value.source_row,
            )


# ==========================================================================
# VALIDITY
# ==========================================================================
@rule(
    "VAL-001",
    "Value is numeric",
    DQADimension.VALIDITY,
    severity=Severity.ERROR,
    blocking=True,
    description="A populated cell that could not be parsed into a number is unusable.",
)
def value_is_numeric(ctx: RuleContext) -> Iterator[Finding]:
    for value in ctx.values:
        indicator = ctx.indicator(value)
        if indicator is None:
            continue
        if value.value is None and value.numerator is None and value.raw_value:
            yield Finding(
                message=f"{indicator.code}: '{value.raw_value}' is not a number.",
                indicator_id=indicator.id,
                indicator_code=indicator.code,
                field="value",
                observed=value.raw_value,
                expected="a numeric value",
                source_row=value.source_row,
            )


@rule(
    "VAL-002",
    "Value lies within the indicator's permitted range",
    DQADimension.VALIDITY,
    severity=Severity.ERROR,
    blocking=True,
    description="Enforces the min/max configured on each indicator, including 0-100 percentages.",
)
def value_in_range(ctx: RuleContext) -> Iterator[Finding]:
    for value, indicator, effective in _numeric_values(ctx):
        minimum = indicator.min_value
        maximum = indicator.max_value
        if indicator.unit == IndicatorUnit.PERCENT:
            minimum = 0.0 if minimum is None else minimum
            maximum = 100.0 if maximum is None else maximum
        if minimum is not None and effective < minimum:
            yield Finding(
                message=f"{indicator.code}: {effective:g} is below the minimum of {minimum:g}.",
                indicator_id=indicator.id,
                indicator_code=indicator.code,
                field="value",
                observed=f"{effective:g}",
                expected=f">= {minimum:g}",
                source_row=value.source_row,
            )
        elif maximum is not None and effective > maximum:
            yield Finding(
                message=f"{indicator.code}: {effective:g} is above the maximum of {maximum:g}.",
                indicator_id=indicator.id,
                indicator_code=indicator.code,
                field="value",
                observed=f"{effective:g}",
                expected=f"<= {maximum:g}",
                source_row=value.source_row,
            )


@rule(
    "VAL-003",
    "Counts are whole numbers",
    DQADimension.VALIDITY,
    severity=Severity.INFO,
    description="Headcount indicators reported with decimals are rounded on ingestion.",
)
def counts_are_integers(ctx: RuleContext) -> Iterator[Finding]:
    for value, indicator, effective in _numeric_values(ctx):
        if indicator.unit != IndicatorUnit.NUMBER or indicator.decimal_places:
            continue
        if abs(effective - round(effective)) > 1e-9:
            yield Finding(
                message=f"{indicator.code}: headcount reported as {effective:g}.",
                indicator_id=indicator.id,
                indicator_code=indicator.code,
                field="value",
                observed=f"{effective:g}",
                expected="a whole number",
                source_row=value.source_row,
            )


@rule(
    "VAL-004",
    "Disaggregation labels use the approved vocabulary",
    DQADimension.VALIDITY,
    severity=Severity.WARNING,
    description="Keeps sex, location and school-level labels comparable across states.",
)
def disaggregation_vocabulary(ctx: RuleContext) -> Iterator[Finding]:
    for value in ctx.values:
        indicator = ctx.indicator(value)
        for axis, raw in (value.disaggregation or {}).items():
            allowed = ALLOWED_DISAGGREGATIONS.get(axis)
            if not allowed:
                continue
            if str(raw).strip().lower() not in allowed:
                yield Finding(
                    message=(
                        f"{indicator.code if indicator else 'row'}: unrecognised {axis} "
                        f"label '{raw}'."
                    ),
                    indicator_id=getattr(indicator, "id", None),
                    indicator_code=getattr(indicator, "code", None),
                    field=axis,
                    observed=str(raw),
                    expected="/".join(sorted(allowed)),
                    source_row=value.source_row,
                )


# ==========================================================================
# UNIQUENESS
# ==========================================================================
@rule(
    "UNQ-001",
    "No duplicate indicator rows within the submission",
    DQADimension.UNIQUENESS,
    severity=Severity.ERROR,
    blocking=True,
    description="The same indicator and disaggregation must appear at most once per submission.",
)
def no_duplicate_rows(ctx: RuleContext) -> Iterator[Finding]:
    seen: Counter[tuple] = Counter()
    for value in ctx.values:
        key = (
            value.indicator_id,
            tuple(sorted((value.disaggregation or {}).items())),
        )
        seen[key] += 1

    for (indicator_id, disaggregation), count in seen.items():
        if count <= 1:
            continue
        indicator = ctx.indicators_by_id.get(indicator_id)
        label = dict(disaggregation) or "no disaggregation"
        yield Finding(
            message=(
                f"{indicator.code if indicator else indicator_id} appears {count} times for "
                f"{label}."
            ),
            indicator_id=indicator_id,
            indicator_code=getattr(indicator, "code", None),
            field="indicator",
            observed=f"{count} rows",
            expected="1 row",
        )


@rule(
    "UNQ-002",
    "File has not already been submitted",
    DQADimension.UNIQUENESS,
    severity=Severity.WARNING,
    description="An identical file already on record usually means an accidental re-upload.",
    weight=_one,
)
def file_not_duplicated(ctx: RuleContext) -> Iterator[Finding]:
    if ctx.duplicate_submission_ids:
        ids = ", ".join(f"#{sid}" for sid in ctx.duplicate_submission_ids)
        yield Finding(
            message=f"An identical file was already submitted as {ids}.",
            field="file_hash",
            observed=ctx.submission.file_hash or "",
            expected="a new file",
            context={"duplicate_submission_ids": ctx.duplicate_submission_ids},
        )


@rule(
    "UNQ-003",
    "Only one current submission per state and period",
    DQADimension.UNIQUENESS,
    severity=Severity.ERROR,
    blocking=True,
    description="Superseded versions must be retired so analysis reads a single source of truth.",
    weight=_one,
)
def single_current_submission(ctx: RuleContext) -> Iterator[Finding]:
    # The pipeline retires prior versions before validating; this rule catches a
    # failure of that invariant rather than normal re-submission.
    conflicting = ctx.submission.__dict__.get("_conflicting_current_ids") or []
    if conflicting:
        yield Finding(
            message=(
                f"{len(conflicting)} other submission(s) are still marked current for "
                f"{ctx.state.code}/{ctx.period.code}."
            ),
            field="is_current",
            observed=str(len(conflicting) + 1),
            expected="1",
        )


# ==========================================================================
# CONSISTENCY -- the monthly tracker against the quarterly framework
# ==========================================================================
def _judged_lines(ctx: RuleContext) -> int:
    """Checks the reconciliation actually performed, for the score denominator."""
    return max(
        sum(
            1
            for line in ctx.reconciliation
            if line.status is not ReconciliationStatus.INCOMPLETE
        ),
        1,
    )


@rule(
    "REC-001",
    "Quarterly figure agrees with the performance tracker",
    DQADimension.CONSISTENCY,
    severity=Severity.WARNING,
    description=(
        "The same indicator reported twice -- monthly through the tracker and quarterly "
        "through the results framework -- must give the same answer. Each indicator is "
        "reconciled on its own time basis: a running total equals its last month, a count "
        "within the period is the months added together, a rate is the position at the "
        "period's end."
    ),
    weight=_judged_lines,
)
def tracker_agrees_with_framework(ctx: RuleContext) -> Iterator[Finding]:
    for line in ctx.reconciled(ReconciliationStatus.MISMATCH):
        yield Finding(
            message=line.note,
            indicator_id=line.indicator_id,
            indicator_code=line.indicator_code,
            field="value",
            observed=f"{line.coarse_value:g}",
            expected=f"{line.fine_value:g}",
            context={
                "basis": str(line.basis),
                "variance": line.variance,
                "variance_pct": line.variance_pct,
                "months": line.part_values,
            },
        )


@rule(
    "REC-002",
    "Nothing reported monthly is missing from the quarterly return",
    DQADimension.CONSISTENCY,
    severity=Severity.WARNING,
    description=(
        "A figure the state reported in the tracker but left out of the quarterly return "
        "is a gap in the framework submission, not an absence of data."
    ),
    weight=_one,
)
def framework_covers_the_tracker(ctx: RuleContext) -> Iterator[Finding]:
    for line in ctx.reconciled(ReconciliationStatus.FRAMEWORK_MISSING):
        yield Finding(
            message=line.note,
            indicator_id=line.indicator_id,
            indicator_code=line.indicator_code,
            field="value",
            observed="not reported",
            expected=f"{line.fine_value:g}",
            context={"basis": str(line.basis), "months": line.part_values},
        )


@rule(
    "REC-003",
    "Nothing reported quarterly is absent from the tracker",
    DQADimension.CONSISTENCY,
    severity=Severity.WARNING,
    description=(
        "An indicator reported for the quarter but absent from every month of a complete "
        "tracker has no monthly evidence behind it."
    ),
    weight=_one,
)
def tracker_covers_the_framework(ctx: RuleContext) -> Iterator[Finding]:
    # Only once every month is in: until then the figure may still arrive.
    if not ctx.reconciliation_is_complete:
        return
    for line in ctx.reconciled(ReconciliationStatus.TRACKER_MISSING):
        yield Finding(
            message=line.note,
            indicator_id=line.indicator_id,
            indicator_code=line.indicator_code,
            field="value",
            observed=f"{line.coarse_value:g}",
            expected="a monthly figure in the tracker",
            context={"basis": str(line.basis), "months_reported": line.parts_reported},
        )


def days_late(period: ReportingPeriod, submitted_on: date | None) -> int | None:
    if submitted_on is None:
        return None
    return max(0, (submitted_on - period.due_date).days)
