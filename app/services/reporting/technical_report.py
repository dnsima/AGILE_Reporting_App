"""The Quarterly Technical Performance Report.

Follows the NPCU's own report: executive summary, methodology, performance by
component and sub-component, state and financing-cohort comparison, data
quality, and priority actions, with the annex tables behind each figure.

On voice. The narrative is written to read as the Lead M&E Consultant writes
it, because a report that changes register halfway through invites its reader
to wonder which half to trust. That means British spelling, the institutional
register the Bank expects, a bolded lead-in where a paragraph turns from
description to judgement, roman-numeral priority actions, and a closing
sentence in each section that says what the numbers mean rather than repeating
them. Where the platform cannot responsibly form a judgement -- an inversion
that might be genuine or might be an artefact -- it says what it observes and
marks the passage for the author, rather than inventing a view.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Cohort,
    Indicator,
    IndicatorCategory,
    ReportingPeriod,
    State,
    Subcomponent,
    Submission,
)
from app.services import analytics, exposure, reference
from app.services.reporting.document import ReportDocument, Section, Table
from app.services.reporting.validation_report import collect_issues

#: Marks a passage the author should review before the report is circulated.
REVIEW_MARK = "[for review]"

#: The indicators the NPCU ranks states on. Enrolment and completion are the
#: flagship access and retention outcomes; certification is the highest-order
#: learning outcome; the remaining three are where the equity gaps sit.
PRIORITY_INDICATORS: tuple[tuple[str, str], ...] = (
    ("PDO-04", "Girls enrolled in public schools (JS1-SS3)"),
    ("PDO-07", "Overall girls' completion rate"),
    ("PDO-16", "Girls passing Senior WAEC or NECO"),
    ("C1.2-05", "Public JSS and SSS schools receiving School Improvement Grants"),
    ("C2.2c-01", "Out-of-school girls reached through non-formal education"),
    ("C2.3-06", "Total social safety net beneficiaries"),
)

COMPONENT_TITLES: dict[str, str] = {
    "PDO": "Project Development Objective indicators",
    "C1": "Component 1: creating safe and accessible learning spaces",
    "C2": "Component 2: fostering an enabling environment for girls",
    "C3": "Component 3: project management and system strengthening",
}

COMPONENT_PREAMBLES: dict[str, str] = {
    "PDO": (
        "The PDO indicators are the primary measure of the project's "
        "effectiveness in improving learning conditions and increasing girls' "
        "retention in secondary school. The priority outcome indicators are "
        "girls' enrolment and the completion rates."
    ),
    "C1": (
        "Component 1 provides the physical and safety infrastructure that "
        "underpins girls' access and retention: classroom construction and "
        "rehabilitation, WASH facilities, School Improvement Grants and "
        "school-safety systems. In the theory of change this component is the "
        "supply-side enabler, removing the infrastructure and safety barriers "
        "that deter enrolment and attendance."
    ),
    "C2": (
        "Component 2 addresses the demand-side and empowerment determinants "
        "of girls' schooling: community mobilisation, scholarships and the "
        "social safety net, life-skills programming, digital literacy and "
        "non-formal education for out-of-school girls. In the theory of "
        "change it tackles the economic, social, psychosocial and cultural "
        "barriers that prevent girls from enrolling, attending and completing."
    ),
    "C3": (
        "Component 3 addresses the governance, policy and accountability "
        "systems that sustain the project's gains beyond its financing "
        "period: adoption and implementation of the national gender education "
        "policy, the grievance redress mechanism, and climate-change and "
        "environmental programming. In the theory of change it secures the "
        "institutional sustainability of the access and empowerment gains."
    ),
}


def _fmt(value: float | None, *, decimals: int = 0, suffix: str = "") -> str:
    if value is None:
        return "—"
    return f"{value:,.{decimals}f}{suffix}"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:,.1f}%"


_COUNT_WORDS = (
    "no", "one", "two", "three", "four", "five", "six",
    "seven", "eight", "nine", "ten", "eleven", "twelve",
)


def _count(number: int) -> str:
    """Small counts read better as words in a narrative paragraph."""
    return _COUNT_WORDS[number] if 0 <= number < len(_COUNT_WORDS) else f"{number:,}"


def _achievement_words(pct: float | None) -> str:
    """Describe progress against target the way the NPCU report does."""
    if pct is None:
        return "no target is set"
    if pct >= 100:
        return "exceeding target"
    if pct >= 90:
        return "approaching target"
    if pct >= 60:
        return "progressing"
    return "lagging markedly"


@dataclass
class Line:
    """One indicator's performance, ready for a table row or a sentence."""

    code: str
    name: str
    unit: str
    value: float | None
    target: float | None
    achievement: float | None
    status: str
    note: str | None = None

    @property
    def decimals(self) -> int:
        return 1 if self.unit in ("PERCENT", "RATIO", "SCORE") else 0

    @property
    def suffix(self) -> str:
        return "%" if self.unit == "PERCENT" else ""

    def row(self) -> list[str]:
        return [
            self.code,
            self.name,
            _fmt(self.value, decimals=self.decimals, suffix=self.suffix),
            _fmt(self.target, decimals=self.decimals, suffix=self.suffix),
            _pct(self.achievement),
            self.status,
        ]

    def sentence(self) -> str:
        """'X reached N against a target of M, achieving P% of target.'"""
        value = _fmt(self.value, decimals=self.decimals, suffix=self.suffix)
        if self.target is None:
            return f"{self.name} stood at {value}."
        target = _fmt(self.target, decimals=self.decimals, suffix=self.suffix)
        return (
            f"{self.name} reached {value} against a target of {target}, "
            f"achieving {_pct(self.achievement)} of target."
        )


def _lines(
    db: Session,
    period: ReportingPeriod,
    indicators: list[Indicator],
    notes: dict[str, str],
) -> list[Line]:
    rows: list[Line] = []
    for indicator in indicators:
        analysis = analytics.analyse_indicator(db, indicator, period)
        national = analysis.national
        rows.append(
            Line(
                code=indicator.code,
                name=indicator.name,
                unit=indicator.unit,
                value=national.value,
                target=national.target,
                achievement=national.achievement_pct,
                status=national.status,
                note=notes.get(indicator.code),
            )
        )
    return rows


def _indicators_for(db: Session, component: str) -> list[Indicator]:
    """The reported indicators of one results-framework category."""
    return list(
        db.scalars(
            select(Indicator)
            .join(IndicatorCategory)
            .where(
                IndicatorCategory.code == component,
                Indicator.is_active.is_(True),
                Indicator.is_reported.is_(True),
            )
            .order_by(Indicator.number)
        )
    )


def _quality_notes(db: Session, period: ReportingPeriod) -> dict[str, str]:
    """A one-line caveat per indicator whose national figure is in doubt."""
    notes: dict[str, str] = {}
    for entry in exposure.by_indicator(db, period):
        if entry.is_material:
            notes[entry.indicator_code] = entry.note() or ""
    return notes


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------
def _methodology(
    db: Session, period: ReportingPeriod, states_reporting: int
) -> Section:
    section = Section(heading="Methodology and data sources", level=1)
    section.add_paragraph(
        "This report is built on the returns submitted by state Project "
        f"Implementation Units for {period.label} and consolidated by the "
        "platform into a single national dataset. All quantitative statements "
        "are drawn from that dataset, and every figure is reported as the "
        "state submitted it."
    )
    section.add_paragraph(
        "It should be noted at the outset that figures flagged by the data "
        "quality validation remain in the national totals. The platform "
        "reports what states report and discloses what it doubts; it does not "
        "restate a national result on its own authority. Where a national "
        "figure rests materially on a flagged return, the caveat is carried "
        "inline beside that figure and set out in full in the data quality "
        "section, so that the reader can weigh the number rather than be "
        "handed a quietly adjusted one."
    )

    cohorts = list(db.scalars(select(Cohort).order_by(Cohort.sort_order)))
    if cohorts:
        rows = []
        for cohort in cohorts:
            members = [
                state.name
                for state in db.scalars(
                    select(State).where(State.cohort_id == cohort.id).order_by(State.name)
                )
            ]
            rows.append(
                [cohort.name, str(cohort.start_year or "—"), str(len(members)), ", ".join(members)]
            )
        section.add_table(
            Table(
                caption="Table 1: Financing cohorts and their member states",
                headers=["Cohort", "From", "States", "Member states"],
                rows=rows,
                align=["left", "right", "right", "left"],
            )
        )

    counts = []
    for component, title in COMPONENT_TITLES.items():
        indicators = _indicators_for(db, component)
        if indicators:
            counts.append([component, title, str(len(indicators))])
    section.add_table(
        Table(
            caption="Table 2: Reportable indicators by level",
            headers=["Code", "Indicator level", "Number of indicators"],
            rows=counts,
            align=["left", "left", "right"],
            note=(
                f"{states_reporting} states submitted a return for "
                f"{period.label}."
            ),
        )
    )
    return section


def _component_section(
    db: Session,
    period: ReportingPeriod,
    component: str,
    table_number: int,
    notes: dict[str, str],
) -> Section | None:
    indicators = _indicators_for(db, component)
    if not indicators:
        return None

    lines = _lines(db, period, indicators, notes)
    targeted = [line for line in lines if line.achievement is not None]

    section = Section(heading=COMPONENT_TITLES[component], level=2)
    section.add_paragraph(COMPONENT_PREAMBLES[component])
    section.add_table(
        Table(
            caption=f"Table {table_number}: {COMPONENT_TITLES[component]}, {period.label}",
            headers=["Code", "Indicator", "Achieved", "Target", "% of target", "Status"],
            rows=[line.row() for line in lines],
            align=["left", "left", "right", "right", "right", "left"],
            note=(
                "Figures are as reported by states. Indicators carrying a data "
                "quality caveat are listed in the data quality section."
            ),
        )
    )

    if targeted:
        exceeding = [line for line in targeted if (line.achievement or 0) >= 100]
        below = sorted(
            (line for line in targeted if (line.achievement or 0) < 100),
            key=lambda line: line.achievement or 0,
        )
        if len(exceeding) == len(targeted):
            opening = (
                f"All {_count(len(targeted))} indicators carrying a target are "
                "at or beyond it"
            )
        elif exceeding:
            opening = (
                f"{_count(len(exceeding)).capitalize()} of the "
                f"{_count(len(targeted))} indicators carrying a target are at "
                "or beyond it"
            )
        else:
            opening = (
                f"None of the {_count(len(targeted))} indicators carrying a "
                "target has reached it"
            )
        section.add_paragraph(
            f"**Assessment.** {opening}. "
            + " ".join(line.sentence() for line in targeted[:3])
        )

        if below:
            weakest = below[0]
            section.add_paragraph(
                f"The furthest from target is {weakest.name} at "
                f"{_pct(weakest.achievement)}, {_achievement_words(weakest.achievement)}. "
                + (
                    "Delivery on this indicator should be escalated for review "
                    "against the implementation plan, since the gap is now "
                    "large enough that it will not close at the present rate."
                    if (weakest.achievement or 0) < 60
                    else "The shortfall is within the range that can be "
                    "recovered over the remaining periods, provided the "
                    "present rate of delivery is sustained."
                )
            )
        else:
            section.add_paragraph(
                "With every targeted indicator at or beyond target, the "
                "question for this component is no longer delivery against "
                "the target but whether the targets themselves still "
                "represent the ambition the project was appraised on."
            )

    flagged = [line for line in lines if line.note]
    if flagged:
        section.add_paragraph(
            f"{len(flagged)} of the figures above carry a data quality caveat "
            "material enough to affect how they should be read: "
            + "; ".join(f"{line.code} ({line.name})" for line in flagged)
            + ". These are set out in full in the data quality section, and the "
            "figures themselves are unchanged from what the states reported."
        )
    return section


def _subcomponent_section(
    db: Session, period: ReportingPeriod, table_number: int, notes: dict[str, str]
) -> Section:
    section = Section(heading="Performance by sub-component", level=2)
    section.add_paragraph(
        "Components are delivered through sub-components whose performance "
        "diverges sharply, and reporting a component only in aggregate "
        "conceals that divergence. The table below disaggregates achievement "
        "by sub-component."
    )

    subcomponents = list(
        db.scalars(
            select(Subcomponent).order_by(Subcomponent.sort_order)
        )
    )
    rows = []
    summaries: list[tuple[str, float]] = []
    for subcomponent in subcomponents:
        indicators = [
            indicator
            for indicator in subcomponent.indicators
            if indicator.is_active and indicator.is_reported
        ]
        if not indicators:
            continue
        lines = _lines(db, period, indicators, notes)
        targeted = [line for line in lines if line.achievement is not None]
        average = (
            sum(line.achievement or 0 for line in targeted) / len(targeted)
            if targeted
            else None
        )
        if average is not None:
            summaries.append((subcomponent.name, average))
        rows.append(
            [
                subcomponent.code,
                subcomponent.name,
                str(len(indicators)),
                str(len(targeted)),
                _pct(average),
                _achievement_words(average).capitalize(),
            ]
        )

    section.add_table(
        Table(
            caption=f"Table {table_number}: Performance by sub-component, {period.label}",
            headers=[
                "Sub-comp.",
                "Title",
                "Indicators",
                "With target",
                "Mean % of target",
                "Status",
            ],
            rows=rows,
            align=["left", "left", "right", "right", "right", "left"],
            note=(
                "The mean is taken across the indicators in each sub-component "
                "that carry a target, and expresses progress towards the "
                "cumulative end-of-project figure rather than against a "
                "quarterly milestone."
            ),
        )
    )

    if summaries:
        summaries.sort(key=lambda row: row[1], reverse=True)
        strongest, strongest_pct = summaries[0]
        weakest, weakest_pct = summaries[-1]
        section.add_paragraph(
            f"**Assessment.** {strongest} is the strongest performing "
            f"sub-component at {_pct(strongest_pct)} of target, and {weakest} "
            f"the weakest at {_pct(weakest_pct)}. That spread, rather than any "
            "component average, is what should guide the next period's "
            "prioritisation: an average across sub-components performing this "
            "differently describes none of them."
        )
    return section


def _rankings_section(
    db: Session, period: ReportingPeriod, first_table: int, notes: dict[str, str]
) -> tuple[Section, int]:
    section = Section(heading="Priority-indicator performance by state", level=2)
    section.add_paragraph(
        "This section gives the strongest and weakest reporting states on each "
        "priority indicator. Rankings are on figures as reported; where a "
        "state's figure carries a data quality caveat it is marked, because a "
        "ranking built on a doubted figure is itself doubtful."
    )

    table_number = first_table
    flagged_states = {
        (figure.state_code, figure.indicator_code)
        for entry in exposure.by_indicator(db, period)
        for figure in entry.figures
        if figure.is_blocking
    }

    for code, label in PRIORITY_INDICATORS:
        indicator = reference.get_indicator_by_code(db, code, required=False)
        if indicator is None:
            continue
        analysis = analytics.analyse_indicator(db, indicator, period)
        reporting = [row for row in analysis.states if row.reported and row.value is not None]
        if len(reporting) < 4:
            continue
        ordered = sorted(reporting, key=lambda row: row.value or 0, reverse=True)
        decimals = 1 if indicator.unit == "PERCENT" else 0
        suffix = "%" if indicator.unit == "PERCENT" else ""

        def render(rows, rank_from, *, code=code, decimals=decimals, suffix=suffix):
            return [
                [
                    str(rank_from + offset),
                    row.state_name
                    + (" *" if (row.state_code, code) in flagged_states else ""),
                    _fmt(row.value, decimals=decimals, suffix=suffix),
                    _pct(row.achievement_pct),
                ]
                for offset, row in enumerate(rows)
            ]

        top = render(ordered[:5], 1)
        bottom = render(ordered[-5:], len(ordered) - min(5, len(ordered)) + 1)
        section.add_table(
            Table(
                caption=f"Table {table_number}: {label} ({code}) — strongest and weakest states",
                headers=["Rank", "State", "Reported", "% of target"],
                rows=top + [["…", "…", "…", "…"]] + bottom,
                align=["right", "left", "right", "right"],
                note=(
                    "* the state's figure for this indicator is flagged by the "
                    "data quality validation and should be read with the "
                    "caveat in the data quality section."
                    if any("*" in row[1] for row in top + bottom)
                    else None
                ),
            )
        )
        table_number += 1

    return section, table_number


def _cohort_section(db: Session, period: ReportingPeriod, table_number: int) -> Section:
    section = Section(heading="Financing-cohort comparison", level=2)
    section.add_paragraph(
        "Comparing the financing cohorts sets performance against length of "
        "implementation, which the theory of change expects to matter: states "
        "onboarded earlier have had longer to convert supply-side investment "
        "into retention and completion."
    )

    cohorts = list(db.scalars(select(Cohort).order_by(Cohort.sort_order)))
    rows = []
    for code, label in PRIORITY_INDICATORS[:3]:
        indicator = reference.get_indicator_by_code(db, code, required=False)
        if indicator is None:
            continue
        cells = [label]
        for cohort in cohorts:
            analysis = analytics.analyse_indicator(
                db, indicator, period, cohort_code=cohort.code
            )
            decimals = 1 if indicator.unit == "PERCENT" else 0
            suffix = "%" if indicator.unit == "PERCENT" else ""
            cells.append(_fmt(analysis.national.value, decimals=decimals, suffix=suffix))
        rows.append(cells)

    section.add_table(
        Table(
            caption=f"Table {table_number}: Priority indicators by financing cohort, {period.label}",
            headers=["Indicator"] + [cohort.name for cohort in cohorts],
            rows=rows,
            align=["left"] + ["right"] * len(cohorts),
        )
    )
    section.add_paragraph(
        f"**Assessment.** {REVIEW_MARK} The comparison above is presented "
        "without interpretation, because a cohort average can be moved as "
        "easily by one state's reporting anomaly as by genuine performance, "
        "and the smaller cohorts produce the more volatile averages. The "
        "author should read it alongside the data quality section before "
        "drawing a conclusion about the maturity effect."
    )
    return section


def _quality_section(db: Session, period: ReportingPeriod, table_number: int) -> Section:
    section = Section(heading="Data quality", level=1)
    issues = collect_issues(db, period)
    summary = exposure.national_summary(db, period)
    counts = {
        band: sum(1 for issue in issues if issue.severity == band)
        for band in ("CRITICAL", "HIGH", "MEDIUM")
    }

    section.add_paragraph(
        f"A full validation of the {period.label} dataset was carried out "
        "against the preceding period and against the internal logic of the "
        f"results framework. It raised {len(issues)} issues: "
        f"{counts['CRITICAL']} critical, {counts['HIGH']} high and "
        f"{counts['MEDIUM']} medium. "
        f"{summary['states_with_blocking']} of the reporting states carry at "
        "least one finding serious enough that the return should not be relied "
        "on without reconciliation."
    )
    section.add_paragraph(
        "Every figure in this report is nonetheless the figure the state "
        "reported. Nothing has been withheld from a national total, because "
        "removing a doubted figure would leave the total looking sounder than "
        "the data behind it and would put a published national result at odds "
        "with what the states actually submitted. The national figures most "
        "affected are set out below; each should be read with the stated "
        "caveat rather than at face value."
    )

    affected = summary.get("most_affected") or []
    if affected:
        section.add_table(
            Table(
                caption=(
                    f"Table {table_number}: National figures resting materially "
                    "on flagged returns"
                ),
                headers=["Indicator", "Reported total", "Likely error", "Caveat"],
                rows=[
                    [
                        str(row["indicator"]),
                        _fmt(row["reported_total"]),
                        _fmt(row["distortion"]),
                        str(row["note"] or ""),
                    ]
                    for row in affected
                ],
                align=["left", "right", "right", "left"],
            )
        )

    section.add_paragraph(
        "The full issue register, with the evidence and likely cause for each "
        "finding and the reconciliation actions recommended to the SPIUs, is "
        "published separately as the Data Quality Validation Report for this "
        "period."
    )
    return section


def _executive_summary(
    db: Session,
    period: ReportingPeriod,
    states_reporting: int,
    notes: dict[str, str],
) -> Section:
    section = Section(heading="Executive summary", level=1)
    issues = collect_issues(db, period)
    critical = sum(1 for issue in issues if issue.severity == "CRITICAL")

    section.add_paragraph(
        "This report presents the "
        f"{period.label} technical performance of the Adolescent Girls "
        "Initiative for Learning and Empowerment (AGILE), World Bank-financed "
        "operation P170664, across the "
        f"{states_reporting} reporting intervention states. It is organised by "
        "component, sub-component and indicator, with emphasis on outcome "
        "indicators, and measures performance against the targets set out in "
        "the results framework, the Project Implementation Manual and the "
        "Project Appraisal Document."
    )

    for component in ("PDO", "C1", "C2", "C3"):
        indicators = _indicators_for(db, component)
        if not indicators:
            continue
        lines = [line for line in _lines(db, period, indicators, notes) if line.achievement is not None]
        if not lines:
            continue
        exceeding = [line for line in lines if (line.achievement or 0) >= 100]
        below = sorted(
            (line for line in lines if (line.achievement or 0) < 100),
            key=lambda line: line.achievement or 0,
        )[:2]
        if len(exceeding) == len(lines):
            headline = (
                f"All {_count(len(lines))} targeted indicators are at or "
                "beyond target."
            )
        elif not exceeding:
            headline = (
                f"None of the {_count(len(lines))} targeted indicators has "
                "reached its target."
            )
        else:
            headline = (
                f"{_count(len(exceeding)).capitalize()} of "
                f"{_count(len(lines))} targeted indicators are at or beyond "
                "target."
            )
        trailer = (
            " The furthest short are "
            + " and ".join(
                f"{line.name} at {_pct(line.achievement)}" for line in below
            )
            + "."
            if below
            else ""
        )
        section.add_paragraph(
            f"**{COMPONENT_TITLES[component]}.** {headline} "
            + " ".join(line.sentence() for line in lines[:2])
            + trailer
        )

    priorities = []
    for component in ("PDO", "C1", "C2", "C3"):
        indicators = _indicators_for(db, component)
        lines = [line for line in _lines(db, period, indicators, notes) if line.achievement is not None]
        priorities.extend(lines)
    priorities.sort(key=lambda line: line.achievement or 0)

    actions = [
        f"accelerate delivery on {line.name} ({line.code}), at "
        f"{_pct(line.achievement)} of target"
        for line in priorities[:3]
        if (line.achievement or 0) < 100
    ]
    if critical:
        actions.append(
            f"resolve the {critical} critical data quality issues with the "
            "SPIUs before these figures are relied on for disbursement-linked "
            "reporting"
        )
    if actions:
        numerals = ("i", "ii", "iii", "iv", "v", "vi")
        section.add_paragraph(
            "**Priority actions for the NPCU:** "
            + "; ".join(
                f"({numerals[index]}) {action}" for index, action in enumerate(actions)
            )
            + "."
        )
    return section


def build_technical_report(db: Session, period_code: str) -> ReportDocument:
    """Assemble the Quarterly Technical Performance Report for a period."""
    period = reference.get_period_by_code(db, period_code)
    states_reporting = len(
        list(
            db.scalars(
                select(Submission).where(
                    Submission.period_id == period.id, Submission.is_current.is_(True)
                )
            )
        )
    )
    notes = _quality_notes(db, period)

    document = ReportDocument(
        title=f"AGILE Quarterly Technical Performance Report — {period.label}",
        subtitle=(
            "Federal Republic of Nigeria — Federal Ministry of Education — "
            "Adolescent Girls Initiative for Learning and Empowerment (AGILE), "
            "World Bank Financed Project P170664"
        ),
        generated_at=datetime.now(timezone.utc),
        meta={
            "Reporting period": period.label,
            "Coverage": f"{states_reporting} intervention states, all components",
            "Prepared for": "the National Project Coordination Unit and development partners",
        },
    )

    document.add_section(_executive_summary(db, period, states_reporting, notes))
    document.add_section(_methodology(db, period, states_reporting))

    performance = Section(heading="Project performance by component", level=1)
    performance.add_paragraph(
        "This section reports performance by component and sub-component, with "
        "emphasis on outcome indicators. Each indicator is measured against "
        "its target where one is set, with the percentage achieved and a short "
        "narrative on whether progress is exceeding expectations, on track or "
        "lagging."
    )
    table_number = 3
    for component in ("PDO", "C1", "C2", "C3"):
        section = _component_section(db, period, component, table_number, notes)
        if section is not None:
            performance.subsections.append(section)
            table_number += 1
    performance.subsections.append(
        _subcomponent_section(db, period, table_number, notes)
    )
    table_number += 1
    document.add_section(performance)

    state_section = Section(
        heading="State performance and financing-cohort comparison", level=1
    )
    state_section.add_paragraph(
        "This section disaggregates priority-indicator performance by state "
        "and by financing cohort."
    )
    cohort_section = _cohort_section(db, period, table_number)
    table_number += 1
    state_section.subsections.append(cohort_section)
    rankings, table_number = _rankings_section(db, period, table_number, notes)
    state_section.subsections.append(rankings)
    document.add_section(state_section)

    document.add_section(_quality_section(db, period, table_number))
    document.number_sections()
    return document
