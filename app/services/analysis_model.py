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

    @property
    def decimals(self) -> int:
        return 1 if self.unit in ("PERCENT", "RATIO", "SCORE") else 0

    @property
    def is_rate(self) -> bool:
        return self.unit in ("PERCENT", "RATIO", "SCORE")

    def state_value(self, index: int) -> float | None:
        return self.states[index] if 0 <= index < len(self.states) else None


@dataclass
class Flag:
    """One open finding, as the workbook's Data Quality Flags sheet lists it."""

    severity: str
    code: str
    indicator: str
    states: list[str]
    issue: str

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

    def by_component(self, component: str) -> list[IndicatorRow]:
        return [row for row in self.indicators if row.component == component]

    def find(self, code: str) -> IndicatorRow | None:
        return next((row for row in self.indicators if row.code == code), None)

    @property
    def components(self) -> list[str]:
        present = {row.component for row in self.indicators}
        return [c for c in COMPONENT_ORDER if c in present]


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
                "states": set(),
                "issue": issue.message,
            },
        )
        entry["states"].add(submissions[issue.submission_id])

    rows = [
        Flag(
            severity=entry["severity"],
            code=entry["code"],
            indicator=entry["indicator"],
            states=sorted(entry["states"]),
            issue=entry["issue"],
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

    submissions = {
        row.id: db.get(State, row.state_id).code
        for row in db.scalars(
            select(Submission).where(
                Submission.period_id == period.id, Submission.is_current.is_(True)
            )
        )
    }

    # Which indicators carry an open finding, and where.
    flagged: dict[str, set[str]] = {}
    if submissions:
        indicators_by_id = {i.id: i for i in db.scalars(select(Indicator))}
        for issue in db.scalars(
            select(ValidationIssue).where(
                ValidationIssue.submission_id.in_(list(submissions))
            )
        ):
            indicator = indicators_by_id.get(issue.indicator_id)
            if indicator is None:
                continue
            flagged.setdefault(indicator.code, set()).add(
                submissions[issue.submission_id]
            )

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
                flagged_states=sorted(flagged.get(indicator.code, set())),
            )
        )

    return AnalysisModel(
        period_code=period.code,
        period_label=period.label,
        states=[state.name for state in states],
        state_codes=[state.code for state in states],
        indicators=rows,
        flags=_flags(db, period, submissions),
        states_reporting=len(submissions),
    )


def to_payload(model: AnalysisModel) -> dict:
    """The JSON the browser renders every tab from."""
    return {
        "period_code": model.period_code,
        "period_label": model.period_label,
        "states": model.states,
        "state_codes": model.state_codes,
        "states_reporting": model.states_reporting,
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
                "flagged_states": row.flagged_states,
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
            }
            for flag in model.flags
        ],
    }
