"""The quarterly Data Quality Validation Report, built from the findings.

Modelled on the NPCU's own Q2 2026 report, which was assembled by hand: the
same shape, the same severity definitions, the same grouping of individual
figures into issues, and the same closing list of reconciliation actions.

One finding is not one issue. The NPCU report lists "Cumulative classrooms fell
for SSS" once and names Kebbi and Borno underneath it, rather than twice. So
findings are grouped by rule and indicator, which reproduces that structure and
keeps a report of 144 findings down to the 70 issues a reader can act on.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import Severity
from app.models import (
    Indicator,
    ReportingPeriod,
    State,
    Submission,
    ValidationIssue,
)
from app.services import exposure, reference
from app.services.reporting.document import ReportDocument, Section, Table

#: The eight families of checks the NPCU validation applies, by the rule that
#: implements each. A report grouped by family reads the way the reviewer
#: worked, rather than the way the rules happen to be numbered.
CHECK_FAMILIES: dict[str, str] = {
    "CON-001": "Period-over-period movement",
    "CON-002": "Direction checks on cumulative indicators",
    "INT-005": "Disaggregated components against parent totals",
    "INT-006": "Disaggregated components against parent totals",
    "INT-001": "Logical constraints from the PIM and PAD definitions",
    "INT-003": "Logical constraints from the PIM and PAD definitions",
    "INT-007": "Logical constraints from the PIM and PAD definitions",
    "VAL-002": "Logical constraints from the PIM and PAD definitions",
    "INT-002": "Rate range and aggregation validity",
    "ACC-003": "Rate range and aggregation validity",
    "COM-001": "Missing values and zeros where activity was reported",
    "COM-002": "Missing values and zeros where activity was reported",
    "COM-003": "Missing values and zeros where activity was reported",
    "ACC-002": "State-level spikes and drops",
    "ACC-004": "State-level spikes and drops",
    "ACC-001": "Achievement against target",
    "REC-001": "Reconciliation against the performance tracker",
    "REC-002": "Reconciliation against the performance tracker",
    "REC-003": "Reconciliation against the performance tracker",
}

#: What a finding of each kind usually turns out to be. The NPCU report gives a
#: likely cause for every issue, because a reconciliation request that says
#: only "this is wrong" wastes a round trip with the SPIU.
LIKELY_CAUSES: dict[str, str] = {
    "CON-002": (
        "Q2 entered as a quarterly increment rather than a cumulative running "
        "total, or a transcription error. A cumulative achievement cannot "
        "decrease between quarters."
    ),
    "CON-001": (
        "A movement of this size in a single quarter usually indicates a "
        "change of counting basis, a units error, or a correction of an "
        "earlier under-count rather than genuine delivery."
    ),
    "INT-005": (
        "Disaggregated rows under-reported relative to the headline, or the "
        "headline inflated. The total must equal the sum of its parts."
    ),
    "INT-006": (
        "The sex or level disaggregation does not reconcile with the total. "
        "Either the total includes a population outside the definition, or "
        "the split is under-reported."
    ),
    "INT-003": (
        "Either the subset count is overstated, for example counting grant "
        "tranches rather than schools, or the parent denominator is "
        "understated."
    ),
    "INT-007": (
        "Two indicators with different definitions and different targets are "
        "carrying the same figure, which means one field is being copied into "
        "the other rather than measured."
    ),
    "INT-001": "A numerator larger than its denominator cannot be a rate.",
    "INT-002": (
        "The reported percentage does not agree with the numerator and "
        "denominator supplied alongside it."
    ),
    "COM-001": (
        "The figure was not entered, or the state does not implement this "
        "activity. The two need distinguishing, because the interpretation "
        "differs sharply."
    ),
    "COM-002": "A rate was reported without the counts it is derived from.",
    "COM-003": (
        "A zero where substantial activity was reported in the previous "
        "period is a classic missing-data signal, not a genuine cessation."
    ),
    "ACC-002": (
        "The figure sits well outside this state's own reporting history."
    ),
    "ACC-004": (
        "One state dominating the national total usually indicates a units "
        "error, or beneficiaries conflated with the population they are drawn "
        "from."
    ),
    "ACC-003": "A ratio indicator was reported without a usable denominator.",
    "VAL-002": "The figure lies outside the permitted range for this indicator.",
    "ACC-001": (
        "Achievement far from target may be genuine, but warrants "
        "confirmation against the delivery plan."
    ),
}

SEVERITY_DEFINITIONS: dict[str, str] = {
    "CRITICAL": (
        "Logical impossibilities or errors that invalidate the indicator as "
        "reported. Must be corrected before the figure is relied on in "
        "analysis, dashboards or Bank reporting."
    ),
    "HIGH": (
        "Movements or gaps large enough to distort national aggregates and "
        "priority-indicator rankings. Require SPIU confirmation or correction "
        "before sign-off."
    ),
    "MEDIUM": (
        "Plausible but unexplained changes or minor inconsistencies. Should be "
        "annotated with a data-source note; not necessarily errors."
    ),
}

SEVERITY_ORDER = ("CRITICAL", "HIGH", "MEDIUM")


@dataclass
class Issue:
    """One finding kind against one indicator, across every state showing it."""

    ref: str
    rule_code: str
    indicator_code: str
    indicator_name: str
    family: str
    severity: str
    states: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    net_distortion: float | None = None
    distortion_share: float | None = None
    national_total: float | None = None

    @property
    def likely_cause(self) -> str:
        return LIKELY_CAUSES.get(self.rule_code, "Requires SPIU confirmation.")

    def evidence_text(self, limit: int = 3) -> str:
        shown = self.evidence[:limit]
        text = " ".join(shown)
        if len(self.evidence) > limit:
            text += f" ({len(self.evidence) - limit} further state(s) affected.)"
        if self.net_distortion is not None and abs(self.net_distortion) >= 1:
            direction = "understated" if self.net_distortion > 0 else "overstated"
            text += (
                f" The national total for {self.indicator_code} is {direction} "
                f"by up to {abs(self.net_distortion):,.0f}"
            )
            if self.distortion_share is not None:
                text += f" ({self.distortion_share:.1f}% of it)"
            text += "."
        return text


def _classify(is_blocking: bool, distortion_share: float | None) -> str:
    """Place an issue in the report's own severity bands.

    Blocking findings are the logical impossibilities the NPCU calls Critical.
    Among the rest, what separates High from Medium is whether the movement is
    big enough to distort a national aggregate, which is what the exposure
    service measures.
    """
    if is_blocking:
        return "CRITICAL"
    if distortion_share is not None and distortion_share >= exposure.MATERIAL_SHARE:
        return "HIGH"
    return "MEDIUM"


def collect_issues(db: Session, period: ReportingPeriod) -> list[Issue]:
    """Group a period's findings into the issues a reader can act on."""
    submissions = list(
        db.scalars(
            select(Submission).where(
                Submission.period_id == period.id, Submission.is_current.is_(True)
            )
        )
    )
    if not submissions:
        return []

    states = {s.id: s for s in db.scalars(select(State))}
    indicators = {i.id: i for i in db.scalars(select(Indicator))}
    state_of = {s.id: states.get(s.state_id) for s in submissions}

    distortion = {
        entry.indicator_code: entry for entry in exposure.by_indicator(db, period)
    }

    grouped: dict[tuple[str, str], list[tuple[str, ValidationIssue]]] = defaultdict(list)
    for issue in db.scalars(
        select(ValidationIssue).where(
            ValidationIssue.submission_id.in_([s.id for s in submissions])
        )
    ):
        if Severity(issue.severity) is Severity.INFO:
            continue
        indicator = indicators.get(issue.indicator_id) if issue.indicator_id else None
        code = indicator.code if indicator else "—"
        state = state_of.get(issue.submission_id)
        grouped[(issue.rule_code, code)].append((state.code if state else "—", issue))

    issues: list[Issue] = []
    for (rule_code, indicator_code), rows in grouped.items():
        first = rows[0][1]
        entry = distortion.get(indicator_code)
        share = entry.distortion_share if entry else None
        indicator = indicators.get(first.indicator_id) if first.indicator_id else None
        issues.append(
            Issue(
                ref="",  # assigned once sorted, so refs read C-1, C-2, ...
                rule_code=rule_code,
                indicator_code=indicator_code,
                indicator_name=indicator.name if indicator else indicator_code,
                family=CHECK_FAMILIES.get(rule_code, "Other checks"),
                severity=_classify(bool(first.is_blocking), share),
                states=sorted({state for state, _ in rows}),
                evidence=[f"{state}: {issue.message}" for state, issue in rows],
                net_distortion=entry.net_distortion if entry else None,
                distortion_share=share,
                national_total=entry.reported_total if entry else None,
            )
        )

    def sort_key(issue: Issue) -> tuple:
        return (
            SEVERITY_ORDER.index(issue.severity),
            -(issue.distortion_share or 0.0),
            -len(issue.states),
            issue.indicator_code,
        )

    issues.sort(key=sort_key)
    prefixes = {"CRITICAL": "C", "HIGH": "H", "MEDIUM": "M"}
    counters: dict[str, int] = defaultdict(int)
    for issue in issues:
        counters[issue.severity] += 1
        issue.ref = f"{prefixes[issue.severity]}-{counters[issue.severity]}"
    return issues


def _severity_section(issues: list[Issue]) -> Section:
    counts = {band: sum(1 for i in issues if i.severity == band) for band in SEVERITY_ORDER}
    section = Section(heading="Summary of findings by severity", level=2)
    section.add_table(
        Table(
            caption="Table 1: Findings by severity",
            headers=["Severity", "Issues", "Definition and reporting implication"],
            rows=[
                [band, f"{counts[band]} issues", SEVERITY_DEFINITIONS[band]]
                for band in SEVERITY_ORDER
            ],
            align=["left", "right", "left"],
        )
    )
    return section


def _band_section(issues: list[Issue], band: str, heading: str, lead: str) -> Section | None:
    selected = [issue for issue in issues if issue.severity == band]
    if not selected:
        return None
    section = Section(heading=heading, level=1)
    section.add_paragraph(lead)

    by_family: dict[str, list[Issue]] = defaultdict(list)
    for issue in selected:
        by_family[issue.family].append(issue)

    for family, family_issues in sorted(by_family.items()):
        subsection = Section(heading=family, level=2)
        subsection.add_table(
            Table(
                caption=None,
                headers=[
                    "Ref",
                    "Indicator",
                    "States",
                    "Evidence",
                    "Likely cause",
                ],
                rows=[
                    [
                        issue.ref,
                        issue.indicator_code,
                        ", ".join(issue.states),
                        issue.evidence_text(),
                        issue.likely_cause,
                    ]
                    for issue in family_issues
                ],
                align=["left", "left", "left", "left", "left"],
            )
        )
        section.subsections.append(subsection)
    return section


def _state_section(db: Session, period: ReportingPeriod, issues: list[Issue]) -> Section:
    section = Section(heading="Consolidated state-level view", level=1)
    section.add_paragraph(
        "The table below consolidates the flagged issues by state, so that "
        "reconciliation requests can be directed to the right SPIUs. States "
        "are ordered by how much of their contribution to the national totals "
        "rests on figures the validation could not vouch for, rather than by "
        "the number of findings against them: one error that moves a national "
        "figure matters more than several that do not."
    )

    per_state: dict[str, list[Issue]] = defaultdict(list)
    for issue in issues:
        for state in issue.states:
            per_state[state].append(issue)

    rows = []
    for entry in exposure.by_state(db, period):
        attached = per_state.get(entry.state_code, [])
        if not attached:
            continue
        counts = {band: sum(1 for i in attached if i.severity == band) for band in SEVERITY_ORDER}
        detail = "; ".join(
            f"{issue.ref} {issue.indicator_code}" for issue in attached[:6]
        )
        if len(attached) > 6:
            detail += f"; and {len(attached) - 6} more"
        rows.append(
            [
                entry.state_name,
                f"{len(attached)} ({counts['CRITICAL']}C, {counts['HIGH']}H, {counts['MEDIUM']}M)",
                f"{entry.exposed_share:.1f}%",
                str(entry.fitness or "—"),
                detail,
            ]
        )

    section.add_table(
        Table(
            caption="Table 2: Issues by state",
            headers=[
                "State",
                "Issues (severity)",
                "Contribution in doubt",
                "Verdict",
                "References",
            ],
            rows=rows,
            align=["left", "left", "right", "left", "left"],
            note=(
                "Every figure in this table is counted in the national totals "
                "exactly as the state reported it; the verdict describes "
                "whether the return can be relied on, not whether it was "
                "included. The two middle columns answer different questions "
                "and will not always agree: severity classifies the issue "
                "wherever it appears, while the verdict weighs this state's "
                "own instance of it. A state can therefore carry a critical "
                "issue and still be usable, where its own part in that issue "
                "is too small to move a national figure — Nasarawa's "
                "beneficiary split is short by 100 against Yobe's 163,917 on "
                "the same finding."
            ),
        )
    )
    return section


def _actions_section(db: Session, period: ReportingPeriod, issues: list[Issue]) -> Section:
    section = Section(heading="Recommended reconciliation actions", level=1)
    critical = [issue for issue in issues if issue.severity == "CRITICAL"]
    high = [issue for issue in issues if issue.severity == "HIGH"]

    section.add_paragraph(
        f"The dataset carries {len(critical)} critical and {len(high)} "
        "high-severity issues. Every figure remains in the national totals as "
        "the states reported it; what follows is what must be reconciled "
        "before those totals are relied on for disbursement-linked reporting."
    )

    priority_states = [
        entry
        for entry in exposure.by_state(db, period)
        if entry.material_findings
    ][:5]
    if priority_states:
        section.bullets.append(
            "Issue targeted reconciliation requests to "
            + ", ".join(entry.state_name for entry in priority_states)
            + " before any other step. These states carry the findings large "
            "enough to move a national figure."
        )

    by_rule: dict[str, int] = defaultdict(int)
    for issue in critical:
        by_rule[issue.rule_code] += len(issue.states)
    if by_rule.get("CON-002"):
        section.bullets.append(
            "Re-confirm the cumulative-versus-increment convention with all "
            f"SPIUs. {by_rule['CON-002']} cumulative figures fell between "
            "periods, which indicates some states entered the quarter's "
            "increment where others entered a running total. A single "
            "convention must be enforced and the affected returns re-submitted."
        )
    if by_rule.get("INT-006") or by_rule.get("INT-005"):
        section.bullets.append(
            "Resolve the disaggregation identities: require that the parts "
            "sum to the headline, and that girls plus boys equals total "
            "students benefiting, for every state."
        )
    if by_rule.get("INT-003"):
        section.bullets.append(
            "Confirm the denominators behind the subset checks, in particular "
            "the count of public schools in the state, before any coverage "
            "ratio is published."
        )
    if any(issue.rule_code == "INT-007" for issue in issues):
        section.bullets.append(
            "Clarify with all SPIUs the distinction between the social safety "
            "net and the scholarship programme. Identical figures in both "
            "fields across every state indicate one is being copied from the "
            "other."
        )
    if any(issue.rule_code == "COM-003" for issue in issues):
        section.bullets.append(
            "Recover the figures reported as zero after substantial activity "
            "in the previous period, confirming in each case whether the "
            "activity ceased or the cell was not filled."
        )

    section.bullets.append(
        "Resolve each finding through the query workflow so the correction is "
        "recorded against the figure it changes. A re-upload does not clear an "
        "open query."
    )
    return section


def build_validation_report(db: Session, period_code: str) -> ReportDocument:
    """Assemble the Data Quality Validation Report for a reporting period."""
    period = reference.get_period_by_code(db, period_code)
    issues = collect_issues(db, period)
    summary = exposure.national_summary(db, period)
    counts = {band: sum(1 for i in issues if i.severity == band) for band in SEVERITY_ORDER}

    states_reporting = len(
        list(
            db.scalars(
                select(Submission).where(
                    Submission.period_id == period.id, Submission.is_current.is_(True)
                )
            )
        )
    )

    document = ReportDocument(
        title=f"AGILE {period.label} Data Quality Validation Report",
        subtitle=(
            "Adolescent Girls Initiative for Learning and Empowerment (AGILE) "
            "— World Bank Financed Project P170664 — Federal Ministry of "
            "Education, Nigeria"
        ),
        generated_at=datetime.now(timezone.utc),
        meta={
            "Reporting period": period.label,
            "Coverage": f"{states_reporting} reporting states, all components",
            "Outcome": (
                f"{len(issues)} issues flagged — {counts['CRITICAL']} Critical, "
                f"{counts['HIGH']} High, {counts['MEDIUM']} Medium"
            ),
        },
        summary=(
            f"{len(issues)} issues flagged across {states_reporting} states: "
            f"{counts['CRITICAL']} Critical, {counts['HIGH']} High, "
            f"{counts['MEDIUM']} Medium. "
            f"{summary['indicators_materially_affected']} national figures "
            "should not be read without the caveats set out below."
        ),
    )

    approach = Section(heading="Validation approach and summary", level=1)
    approach.add_paragraph(
        "This report validates the "
        f"{period.label} dataset against the preceding period, which serves as "
        "the quality-assured baseline, and against the internal logic of the "
        "results framework itself. The validation applies eight families of "
        "checks: period-over-period movement on every comparable indicator; "
        "reconciliation of disaggregated components against their parent "
        "totals; logical constraints derived from the PIM and PAD indicator "
        "definitions; rate range and aggregation validity; missing values and "
        "zeros where activity was previously reported; state-level spikes and "
        "drops beyond plausible quarterly movement; direction checks on "
        "cumulative indicators, which by definition cannot fall between "
        "periods; and reconciliation against the monthly performance tracker."
    )
    approach.add_paragraph(
        "It should be noted at the outset that every figure discussed below "
        "remains in the national totals exactly as the state reported it. The "
        "platform reports what states report and discloses what it doubts; it "
        "does not restate a national result on its own authority. Corrections "
        "are made through the query workflow, with the change recorded against "
        "the figure it alters, so that a published total and the live "
        "dashboard can always be reconciled."
    )
    approach.subsections.append(_severity_section(issues))
    document.add_section(approach)

    document.add_section(
        _band_section(
            issues,
            "CRITICAL",
            "Critical issues: logical impossibilities and invalidating errors",
            "The following issues are classified Critical because the figure "
            "as reported is either logically impossible or is constructed in a "
            "way that makes the national value unsafe to interpret. None "
            "should be relied on without correction.",
        )
    )
    document.add_section(
        _band_section(
            issues,
            "HIGH",
            "High-severity issues: large unexplained movements",
            "These movements exceed the plausible range for a single period "
            "and are large enough to distort national aggregates or priority "
            "indicator rankings. Each requires SPIU confirmation.",
        )
    )
    document.add_section(
        _band_section(
            issues,
            "MEDIUM",
            "Medium-severity issues: coverage gaps, zeros and magnitude checks",
            "These issues are plausible but unexplained, or are minor "
            "inconsistencies that should be annotated rather than necessarily "
            "corrected. They do not on their own invalidate an indicator, but "
            "they weaken confidence and should carry a data-source note.",
        )
    )
    document.add_section(_state_section(db, period, issues))
    document.add_section(_actions_section(db, period, issues))
    document.number_sections()
    return document
