"""The analysis model: one payload, every consumer.

This is the shape the NPCU's own Q1 dashboard uses -- every reported indicator
with its component, its type, its national target and achievement, and a value
per state in a fixed order. The dashboard tabs, the state analysis, the full
data table and the Excel workbook are all views of this one structure.

That is deliberate. The dashboard used to assemble each panel from its own
query with its own filters, and the panels disagreed: a reporting rate read
"18 of 11 states" because the denominator honoured a cohort filter and the
numerator did not, and the data-quality board ignored both filters entirely.
Three fixes went in at three call sites and a fourth panel was always waiting.

Here there is exactly one scope -- the reporting period -- and every consumer
reads the same arrays. A panel cannot disagree with the workbook, because
neither one computes anything of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import Severity
from app.models import (
    Cohort,
    Indicator,
    IndicatorCategory,
    ReportingPeriod,
    State,
    Submission,
    ValidationIssue,
)
from app.services import analytics, reference

#: Component order, as the results framework and every NPCU report present it.
COMPONENT_ORDER = ("PDO", "C1", "C2", "C3")

COMPONENT_NAMES: dict[str, str] = {
    "PDO": "PDO Indicators",
    "C1": "Component 1 — Creating Safe and Accessible Learning Spaces",
    "C2": "Component 2 — Fostering an Enabling Environment for Girls",
    "C3": "Component 3 — Project Management and System Strengthening",
}

COMPONENT_SHORT: dict[str, str] = {
    "PDO": "PDO",
    "C1": "Component 1",
    "C2": "Component 2",
    "C3": "Component 3",
}


def _indicator_type(indicator: Indicator) -> str:
    """How the figure behaves over time, in the words the NPCU workbook uses.

    The workbook's Type column is what tells a reader whether a figure is a
    running total they may compare against last quarter, a snapshot that
    replaces it, or a yes/no answer.
    """
    if indicator.unit == "BOOLEAN":
        return "Yes/No"
    if indicator.unit == "PERCENT":
        return "Rate (%)"
    return "Cumulative (No.)" if indicator.is_cumulative else "Current (No.)"


@dataclass
class IndicatorRow:
    """One indicator across every state, with its national position."""

    code: str
    name: str
    component: str
    component_name: str
    type_label: str
    unit: str
    target: float | None
    achieved: float | None
    achievement_pct: float | None
    status: str
    #: One entry per state, in the order of ``AnalysisModel.states``. None
    #: means the state did not report the indicator, which is not the same as
    #: reporting zero and must not be shown as one.
    states: list[float | None] = field(default_factory=list)
    #: Codes of the states carrying an open finding against this indicator.
    flagged_states: list[str] = field(default_factory=list)
    #: State name -> flag severity, so a table can shade the offending cell
    #: rather than the whole row. C critical, H high, M medium.
    flag_severity: dict[str, str] = field(default_factory=dict)
    #: A flag against the national figure itself rather than any one state --
    #: most often that too few states reported for the total to mean much.
    national_flag: str | None = None
    #: What a reader hovering the row should be told.
    flag_note: str = ""
    #: How many states reported a figure, out of how many were expected.
    reporting: int = 0
    expected: int = 0

    @property
    def decimals(self) -> int:
        return 1 if self.unit in ("PERCENT", "RATIO", "SCORE") else 0

    @property
    def is_rate(self) -> bool:
        return self.unit in ("PERCENT", "RATIO", "SCORE")

    @property
    def is_boolean(self) -> bool:
        return self.unit == "BOOLEAN"

    @property
    def coverage_pct(self) -> float | None:
        return None if not self.expected else round(self.reporting / self.expected * 100, 1)

    def state_value(self, index: int) -> float | None:
        return self.states[index] if 0 <= index < len(self.states) else None

    def display(self, value: float | None) -> str:
        """One figure as the workbook prints it.

        A boolean reads Yes or No, never 1 or 0; a rate keeps one decimal; a
        count is grouped. An unreported figure is a dash, never a zero -- the
        two mean different things and the dash is the one that starts a
        conversation with the state.
        """
        if value is None:
            return "\u2014"
        if self.is_boolean:
            return "Yes" if value >= 1 else "No"
        if self.is_rate:
            return f"{value:,.1f}"
        return f"{value:,.0f}"

    @property
    def national_display(self) -> str:
        if self.achieved is None:
            return "\u2014"
        if self.is_boolean:
            return f"{self.achieved:,.0f} / {self.expected}"
        if self.is_rate:
            return f"{self.achieved:,.1f}%"
        return f"{self.achieved:,.0f}"


@dataclass
class Flag:
    """One open finding, as the workbook's Data Quality Flags sheet lists it."""

    severity: str
    code: str
    indicator: str
    #: State names, not codes -- the same labels the tables are headed with.
    states: list[str]
    #: What the finding says. When several states carry it and their wording
    #: differs, this is the part they share, and ``details`` holds the rest.
    issue: str
    #: One line per state, where the states' findings differ. Empty when they
    #: all say the same thing.
    details: list[tuple[str, str]] = field(default_factory=list)

    @property
    def states_label(self) -> str:
        return ", ".join(self.states)


@dataclass
class AnalysisModel:
    """Everything a consumer needs about one reporting period."""

    period_code: str
    period_label: str
    states: list[str]
    state_codes: list[str]
    indicators: list[IndicatorRow]
    flags: list[Flag]
    states_reporting: int
    #: Cohort code -> the names of its states, in the order of ``states``.
    #: The cohort is a dimension the charts cut by, not a filter over the
    #: whole board: a cohort filter is what made a reporting rate read
    #: "18 of 11 states".
    cohorts: dict[str, list[str]] = field(default_factory=dict)
    cohort_names: dict[str, str] = field(default_factory=dict)

    def by_component(self, component: str) -> list[IndicatorRow]:
        return [row for row in self.indicators if row.component == component]

    def find(self, code: str) -> IndicatorRow | None:
        return next((row for row in self.indicators if row.code == code), None)

    @property
    def components(self) -> list[str]:
        present = {row.component for row in self.indicators}
        return [c for c in COMPONENT_ORDER if c in present]


#: Words a sentence leads into rather than ends on.
_DANGLING = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into", "is",
    "of", "on", "or", "than", "that", "the", "to", "was", "were", "with",
}


def _shared_wording(messages: list[str]) -> str:
    """The headline for a finding several states carry.

    When they all say the same thing, that is the headline. When they do not
    -- because each message quotes its own state's figures -- the headline is
    only the part they agree on, cut at a word boundary, and each state's own
    wording is kept alongside. Printing one state's numbers above a list of
    four states' names is how a register stops being trustworthy.
    """
    if not messages:
        return ""
    first = messages[0]
    if all(message == first for message in messages):
        return first

    prefix = first
    for message in messages[1:]:
        limit = min(len(prefix), len(message))
        cut = 0
        while cut < limit and prefix[cut] == message[cut]:
            cut += 1
        prefix = prefix[:cut]

    # The prefix ends wherever the states' figures start diverging, which is
    # mid-word and usually mid-phrase: "...is cumulative but fell from". Drop
    # the partial word, then any dangling connective it was leading into, so
    # the headline ends on something that reads as a finished thought.
    words = prefix.split(" ")[:-1]
    while words and words[-1].lower().strip(",;:") in _DANGLING:
        words.pop()
    prefix = " ".join(words).strip(" ,;:-\u2014")

    count = len(messages)
    if len(prefix) < 20:
        return f"{count} states carry this finding, each with its own figures."
    return f"{prefix} \u2014 in {count} states, each with its own figures."


def _severity_of(issue: ValidationIssue) -> str:
    """The finding's weight, in the three bands the NPCU's own flags use.

    Critical is a figure that cannot be true -- a cumulative total that fell,
    parts that do not sum to their whole. High is a movement large enough to
    change a national reading. Medium is a gap in coverage or an unexplained
    change. The bands matter because a table that shades everything the same
    colour tells a reader nothing about where to look first.
    """
    if issue.is_blocking:
        return "C"
    return "H" if Severity(issue.severity) is Severity.ERROR else "M"


#: Worst first, so a cell carrying two findings shows the one that matters.
_SEVERITY_RANK = {"C": 0, "H": 1, "M": 2}


def _flags(db: Session, period: ReportingPeriod, submissions: dict[int, str]) -> list[Flag]:
    """Open findings grouped the way the workbook lists them.

    One finding kind against one indicator is one row, with the affected
    states named alongside -- not one row per state, which is how a register
    of thirty issues becomes an unreadable list of a hundred and forty.
    """
    if not submissions:
        return []
    indicators = {i.id: i for i in db.scalars(select(Indicator))}
    grouped: dict[tuple[str, int | None], dict] = {}

    for issue in db.scalars(
        select(ValidationIssue).where(
            ValidationIssue.submission_id.in_(list(submissions))
        )
    ):
        if Severity(issue.severity) is Severity.INFO:
            continue
        key = (issue.rule_code, issue.indicator_id)
        entry = grouped.setdefault(
            key,
            {
                "severity": "CRITICAL" if issue.is_blocking else "REVIEW",
                "code": (
                    indicators[issue.indicator_id].code
                    if issue.indicator_id in indicators
                    else "—"
                ),
                "indicator": (
                    indicators[issue.indicator_id].name
                    if issue.indicator_id in indicators
                    else issue.rule_code
                ),
                "messages": {},
            },
        )
        # Keyed by state, because the same rule against the same indicator
        # says something different in each one -- it quotes that state's own
        # figures. Collapsing them onto the first message it happened to read
        # would print Borno's numbers under Kaduna's name.
        entry["messages"].setdefault(submissions[issue.submission_id], issue.message)

    rows = [
        Flag(
            severity=entry["severity"],
            code=entry["code"],
            indicator=entry["indicator"],
            states=sorted(entry["messages"]),
            issue=_shared_wording(list(entry["messages"].values())),
            details=(
                sorted(entry["messages"].items())
                if len(set(entry["messages"].values())) > 1
                else []
            ),
        )
        for entry in grouped.values()
    ]
    rows.sort(key=lambda f: (f.severity != "CRITICAL", -len(f.states), f.code))
    return rows


def build(db: Session, period_code: str) -> AnalysisModel:
    """Assemble the analysis model for one reporting period."""
    period = reference.get_period_by_code(db, period_code)
    states = reference.active_states(db)
    state_index = {state.code: position for position, state in enumerate(states)}

    # Cohort membership, as a dimension the charts cut by rather than a filter
    # over everything. Held by state name so a chart can pick its group
    # straight out of the one ``states`` array every panel shares.
    cohorts: dict[str, list[str]] = {}
    cohort_names: dict[str, str] = {}
    for cohort in db.scalars(select(Cohort).order_by(Cohort.id)):
        members = [state.name for state in states if state.cohort_id == cohort.id]
        if members:
            cohorts[cohort.code] = members
            cohort_names[cohort.code] = cohort.name

    submissions = {
        row.id: db.get(State, row.state_id).code
        for row in db.scalars(
            select(Submission).where(
                Submission.period_id == period.id, Submission.is_current.is_(True)
            )
        )
    }

    # Which indicators carry an open finding, where, and how badly. Keyed by
    # state *name*, because that is what a table column is headed with.
    state_name = {state.code: state.name for state in states}
    flagged: dict[str, dict[str, str]] = {}
    notes: dict[str, list[str]] = {}
    if submissions:
        indicators_by_id = {i.id: i for i in db.scalars(select(Indicator))}
        for issue in db.scalars(
            select(ValidationIssue).where(
                ValidationIssue.submission_id.in_(list(submissions))
            )
        ):
            indicator = indicators_by_id.get(issue.indicator_id)
            if indicator is None or Severity(issue.severity) is Severity.INFO:
                continue
            code = submissions[issue.submission_id]
            name = state_name.get(code, code)
            severity = _severity_of(issue)
            cell = flagged.setdefault(indicator.code, {})
            if _SEVERITY_RANK[severity] < _SEVERITY_RANK.get(cell.get(name, "M"), 3):
                cell[name] = severity
            elif name not in cell:
                cell[name] = severity
            bucket = notes.setdefault(indicator.code, [])
            if issue.message not in bucket:
                bucket.append(issue.message)

    catalogue = list(
        db.scalars(
            select(Indicator)
            .join(IndicatorCategory)
            .where(
                Indicator.is_active.is_(True),
                Indicator.is_reported.is_(True),
            )
            .order_by(IndicatorCategory.sort_order, Indicator.number)
        )
    )

    rows: list[IndicatorRow] = []
    for indicator in catalogue:
        analysis = analytics.analyse_indicator(db, indicator, period)
        national = analysis.national

        values: list[float | None] = [None] * len(states)
        for row in analysis.states:
            position = state_index.get(row.state_code)
            if position is not None and row.reported:
                values[position] = row.value

        component = indicator.category.code if indicator.category else "PDO"
        severities = flagged.get(indicator.code, {})
        reporting = sum(1 for value in values if value is not None)

        # A flag against the national figure itself. Not a judgement anyone
        # entered by hand: a total assembled from half the states is a partial
        # total whatever its findings say, and a reader deserves to be told so
        # before quoting it.
        national_flag = None
        note_parts = list(notes.get(indicator.code, []))
        if reporting and reporting < len(states):
            missing = len(states) - reporting
            band = "H" if reporting * 2 < len(states) else "M"
            national_flag = band
            note_parts.insert(
                0,
                f"Only {reporting} of {len(states)} reporting states supplied this "
                f"figure; the national total is short by {missing} state"
                f"{'s' if missing != 1 else ''}.",
            )

        rows.append(
            IndicatorRow(
                code=indicator.code,
                name=indicator.name,
                component=component,
                component_name=COMPONENT_NAMES.get(component, component),
                type_label=_indicator_type(indicator),
                unit=indicator.unit,
                target=national.target,
                achieved=national.value,
                achievement_pct=national.achievement_pct,
                status=national.status,
                states=values,
                flagged_states=sorted(severities),
                flag_severity=severities,
                national_flag=national_flag,
                flag_note=" ".join(note_parts),
                reporting=reporting,
                expected=len(states),
            )
        )

    return AnalysisModel(
        period_code=period.code,
        period_label=period.label,
        states=[state.name for state in states],
        state_codes=[state.code for state in states],
        indicators=rows,
        flags=_flags(db, period, {sid: state_name.get(code, code)
                                  for sid, code in submissions.items()}),
        states_reporting=len(submissions),
        cohorts=cohorts,
        cohort_names=cohort_names,
    )


def to_payload(model: AnalysisModel) -> dict:
    """The JSON the browser renders every tab from."""
    return {
        "period_code": model.period_code,
        "period_label": model.period_label,
        "states": model.states,
        "state_codes": model.state_codes,
        "states_reporting": model.states_reporting,
        "cohorts": [
            {
                "code": code,
                "name": model.cohort_names.get(code, code),
                "states": members,
            }
            for code, members in model.cohorts.items()
        ],
        "components": [
            {
                "code": code,
                "name": COMPONENT_NAMES[code],
                "short": COMPONENT_SHORT[code],
            }
            for code in model.components
        ],
        "indicators": [
            {
                "code": row.code,
                "name": row.name,
                "component": row.component,
                "type": row.type_label,
                "unit": row.unit,
                "target": row.target,
                "achieved": row.achieved,
                "achievement_pct": row.achievement_pct,
                "status": row.status,
                "states": row.states,
                "display": [row.display(value) for value in row.states],
                "national_display": row.national_display,
                "rate": row.is_rate,
                "boolean": row.is_boolean,
                "flagged_states": row.flagged_states,
                "flag_severity": row.flag_severity,
                "national_flag": row.national_flag,
                "flag_note": row.flag_note,
                "reporting": row.reporting,
                "expected": row.expected,
            }
            for row in model.indicators
        ],
        "flags": [
            {
                "severity": flag.severity,
                "code": flag.code,
                "indicator": flag.indicator,
                "states": flag.states,
                "issue": flag.issue,
                "details": [
                    {"state": state, "issue": message} for state, message in flag.details
                ],
            }
            for flag in model.flags
        ],
    }
