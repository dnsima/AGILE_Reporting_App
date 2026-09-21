"""The Kobo backend export: the platform's front door.

States file on Kobo Toolbox, not into this platform, so the backend export is
where a reporting cycle actually starts. These tests hold the two properties
that make that safe: every question column resolves to exactly one indicator,
and the file is refused outright when it cannot.
"""

from __future__ import annotations

import io
from datetime import date

import pandas as pd
import pytest
from sqlalchemy import select

from app.core.enums import PeriodType
from app.core.errors import IngestionError
from app.models import DataQuery, Indicator, IndicatorCategory, ReportingPeriod
from app.services.ingestion import kobo
from app.services.ingestion.pipeline import ingest_kobo_export

#: The pair that matters. These two questions differ by one character, and the
#: platform's general-purpose header matcher -- which strips punctuation --
#: collapses them. One is a count of schools, the other the share of them.
CLIMATE_COUNT = "Schools implementing awareness programs on climate change"
CLIMATE_RATE = "% schools implementing awareness programs on climate change"


@pytest.fixture()
def framework(db):
    """A miniature results framework shaped like the real one."""
    category = db.scalar(select(IndicatorCategory))
    existing = db.scalar(select(Indicator).order_by(Indicator.number.desc()))
    start = (existing.number if existing else 0) + 1

    indicators = [
        Indicator(
            number=start, code="C3.0-03", name=CLIMATE_COUNT,
            category_id=category.id, unit="NUMBER", aggregation_method="SUM",
            direction="INCREASE", is_cumulative=True, min_value=0, decimal_places=0,
        ),
        Indicator(
            number=start + 1, code="C3.0-04", name=CLIMATE_RATE,
            category_id=category.id, unit="PERCENT", aggregation_method="AVERAGE",
            direction="INCREASE", min_value=0, max_value=100, decimal_places=1,
        ),
        Indicator(
            number=start + 2, code="C2.3-01",
            name="Scholarship program operational in the state?",
            category_id=category.id, unit="BOOLEAN", aggregation_method="COUNT_YES",
            direction="INCREASE", decimal_places=0,
        ),
        Indicator(
            number=start + 3, code="C2.1-01",
            name="Community members reached at local level on girls’ education "
                 "(through direct activity)",
            category_id=category.id, unit="NUMBER", aggregation_method="SUM",
            direction="INCREASE", is_cumulative=True, min_value=0, decimal_places=0,
        ),
    ]
    db.add_all(indicators)
    db.flush()
    return indicators


@pytest.fixture()
def period(db):
    existing = db.scalar(select(ReportingPeriod).where(ReportingPeriod.code == "2026-Q2"))
    if existing:
        return existing
    period = ReportingPeriod(
        code="2026-Q2", label="Q2 2026 (Apr-Jun)", period_type=str(PeriodType.QUARTERLY),
        fiscal_year=2026, sequence=2, start_date=date(2026, 4, 1),
        end_date=date(2026, 6, 30), due_date=date(2026, 7, 15),
    )
    db.add(period)
    db.flush()
    return period


def export_bytes(rows: list[dict], columns: list[str] | None = None) -> bytes:
    frame = pd.DataFrame(rows, columns=columns or list(rows[0]))
    buffer = io.BytesIO()
    frame.to_excel(buffer, index=False)
    return buffer.getvalue()


def _row(state: str, section: str, **answers) -> dict:
    base = {
        "Reporting Date": "2026-06-30",
        "State": state,
        "Select Reporting data": section,
        CLIMATE_COUNT: None,
        CLIMATE_RATE: None,
        "Scholarship program operational in the state?": None,
        "Community members reached at local level on girls’ education_"
        "(through direct activity)": None,
        "_id": f"{state}-{section}",
        "_uuid": "uuid",
        "_submission_time": "2026-07-09 13:43:21",
        "_index": 1,
    }
    base.update(answers)
    return base


# --------------------------------------------------------------------------
# Column resolution
# --------------------------------------------------------------------------
def test_count_and_rate_questions_stay_apart(db, framework):
    """The whole reason this module does its own matching.

    If these two ever resolve to the same indicator, a percentage lands in a
    count field and no downstream check would question it.
    """
    resolution = kobo.resolve_columns([CLIMATE_COUNT, CLIMATE_RATE], framework)

    assert resolution.matched[CLIMATE_COUNT].code == "C3.0-03"
    assert resolution.matched[CLIMATE_RATE].code == "C3.0-04"
    assert not resolution.collisions
    assert not resolution.unmatched


def test_percent_survives_the_forgiving_pass():
    """Relaxing punctuation must never relax the percent sign."""
    assert kobo.relax(CLIMATE_COUNT.lower()) != kobo.relax(CLIMATE_RATE.lower())


def test_underscores_and_curly_quotes_are_noise(db, framework):
    """Kobo writes ``education_(through...)``; the framework writes a space."""
    header = (
        "community members reached at local level on girls’ education_"
        "(through direct activity)"
    )
    resolution = kobo.resolve_columns([header], framework)
    assert resolution.matched[header].code == "C2.1-01"
    assert not resolution.relaxed  # matched exactly, not by forgiveness


def test_kobo_system_columns_are_not_questions(db, framework):
    """``_id`` normalises to ``id``, so the underscore must be read raw."""
    headers = ["_id", "_uuid", "_submission_time", "meta/rootUuid", "_index", CLIMATE_COUNT]
    resolution = kobo.resolve_columns(headers, framework)

    assert list(resolution.matched) == [CLIMATE_COUNT]
    assert not resolution.unmatched


def test_two_columns_claiming_one_indicator_is_a_collision(db, framework):
    resolution = kobo.resolve_columns([CLIMATE_COUNT, "Schools implementing awareness "
                                       "programs on climate change!"], framework)
    assert resolution.collisions
    assert not resolution.is_usable


# --------------------------------------------------------------------------
# Reading a return
# --------------------------------------------------------------------------
def test_sections_fold_into_one_return_per_state(db, framework):
    content = export_bytes(
        [
            _row("Kano", "Component 2 Data", **{
                "Scholarship program operational in the state?": "Yes",
                "Community members reached at local level on girls’ education_"
                "(through direct activity)": 1200,
            }),
            _row("Kano", "Component 3 Data", **{CLIMATE_COUNT: 41, CLIMATE_RATE: 12.5}),
        ]
    )
    export = kobo.read_export(content, "export.xlsx", framework)

    assert len(export.returns) == 1
    ret = export.returns[0]
    assert ret.values == {"C2.3-01": 1.0, "C2.1-01": 1200.0, "C3.0-03": 41.0, "C3.0-04": 12.5}
    assert ret.sections == {"C2", "C3"}
    assert ret.missing_sections == ["PDO", "C1"]


def test_unanswered_questions_never_become_zero(db, framework):
    """Kobo writes 'NaN' for a question nobody answered.

    A zero here would read as "no schools", which is a finding against the
    state; unreported reads as unreported, which is a finding against the
    return. They are different conversations.
    """
    content = export_bytes(
        [_row("Kano", "Component 3 Data", **{CLIMATE_COUNT: 41, CLIMATE_RATE: "NaN"})]
    )
    export = kobo.read_export(content, "export.xlsx", framework)

    values = export.returns[0].values
    assert values["C3.0-03"] == 41.0
    assert "C3.0-04" not in values
    assert not export.returns[0].unreadable  # 'NaN' is expected, not a surprise


def test_yes_no_answers_become_one_and_zero(db, framework):
    content = export_bytes(
        [
            _row("Kano", "Component 2 Data",
                 **{"Scholarship program operational in the state?": "Yes"}),
            _row("Kaduna", "Component 2 Data",
                 **{"Scholarship program operational in the state?": "No"}),
        ]
    )
    export = kobo.read_export(content, "export.xlsx", framework)
    by_state = {ret.state_name: ret.values for ret in export.returns}

    assert by_state["Kano"]["C2.3-01"] == 1.0
    assert by_state["Kaduna"]["C2.3-01"] == 0.0


def test_an_unreadable_answer_is_reported_not_swallowed(db, framework):
    content = export_bytes([_row("Kano", "Component 3 Data", **{CLIMATE_COUNT: "about forty"})])
    export = kobo.read_export(content, "export.xlsx", framework)

    assert "C3.0-03" not in export.returns[0].values
    assert export.returns[0].unreadable == ["C3.0-03='about forty'"]


# --------------------------------------------------------------------------
# Refusals
# --------------------------------------------------------------------------
def test_an_unknown_question_column_refuses_the_file(db, framework):
    """Better a refused export than a partial one loaded without anyone noticing."""
    content = export_bytes(
        [_row("Kano", "Component 3 Data", **{CLIMATE_COUNT: 41, "Something new": 7})]
    )
    with pytest.raises(IngestionError, match="match no indicator"):
        kobo.read_export(content, "export.xlsx", framework)


def test_a_file_that_is_not_an_export_is_refused(db, framework):
    content = export_bytes([{"Region": "North", "Total": 12}])
    with pytest.raises(IngestionError, match="does not look"):
        kobo.read_export(content, "other.xlsx", framework)


# --------------------------------------------------------------------------
# Through the pipeline
# --------------------------------------------------------------------------
def test_each_state_becomes_its_own_submission(db, framework, period):
    content = export_bytes(
        [
            _row("Kano", "Component 3 Data", **{CLIMATE_COUNT: 41, CLIMATE_RATE: 12.5}),
            _row("Kaduna", "Component 3 Data", **{CLIMATE_COUNT: 30, CLIMATE_RATE: 9.0}),
        ]
    )
    result = ingest_kobo_export(
        db, content=content, filename="export.xlsx", period_code="2026-Q2"
    )

    assert len(result.loaded) == 2
    assert {row.state_code for row in result.loaded} == {"KN", "KD"}
    assert all(row.values == 2 for row in result.loaded)


def test_a_state_outside_the_project_is_skipped_not_guessed(db, framework, period):
    content = export_bytes(
        [
            _row("Kano", "Component 3 Data", **{CLIMATE_COUNT: 41}),
            _row("Rivers", "Component 3 Data", **{CLIMATE_COUNT: 5}),
        ]
    )
    result = ingest_kobo_export(
        db, content=content, filename="export.xlsx", period_code="2026-Q2"
    )

    assert [row.state_code for row in result.loaded] == ["KN"]
    skipped = result.skipped[0]
    assert skipped.state_name == "Rivers"
    assert "not a participating state" in (skipped.skipped_reason or "").lower()


def test_one_bad_return_does_not_stop_the_others(db, framework, period):
    """Seventeen states that filed properly should not wait on the one that did not."""
    content = export_bytes(
        [
            _row("Kano", "Component 3 Data", **{CLIMATE_COUNT: 41}),
            _row("Kaduna", "Component 3 Data"),  # answered nothing
            _row("Gombe", "Component 3 Data", **{CLIMATE_COUNT: 12}),
        ]
    )
    result = ingest_kobo_export(
        db, content=content, filename="export.xlsx", period_code="2026-Q2"
    )

    assert {row.state_code for row in result.loaded} == {"KN", "GO"}
    assert result.skipped[0].state_code == "KD"
    assert "no readable answer" in (result.skipped[0].skipped_reason or "")


def test_only_states_narrows_the_load(db, framework, period):
    content = export_bytes(
        [
            _row("Kano", "Component 3 Data", **{CLIMATE_COUNT: 41}),
            _row("Kaduna", "Component 3 Data", **{CLIMATE_COUNT: 30}),
        ]
    )
    result = ingest_kobo_export(
        db, content=content, filename="export.xlsx", period_code="2026-Q2",
        only_states={"KN"},
    )

    assert [row.state_code for row in result.loaded] == ["KN"]


def test_findings_are_raised_as_queries_against_the_state(db, framework, period):
    """A flagged figure is a conversation with the state, not a silent note."""
    content = export_bytes(
        [_row("Kano", "Component 3 Data", **{CLIMATE_COUNT: 41, CLIMATE_RATE: 480.0})]
    )
    result = ingest_kobo_export(
        db, content=content, filename="export.xlsx", period_code="2026-Q2"
    )

    loaded = result.loaded[0]
    assert loaded.findings >= 1
    raised = db.scalars(
        select(DataQuery).where(DataQuery.submission_id == loaded.submission_id)
    ).all()
    assert raised, "a finding must leave a query the state can answer"


def test_the_submission_records_where_it_came_from(db, framework, period):
    content = export_bytes([_row("Kano", "Component 3 Data", **{CLIMATE_COUNT: 41})])
    result = ingest_kobo_export(
        db, content=content, filename="Q2_2026_Backend.xlsx", period_code="2026-Q2"
    )

    from app.models import Submission

    submission = db.get(Submission, result.loaded[0].submission_id)
    assert submission.template_profile == "kobo"
    assert submission.source_file_name == "Q2_2026_Backend.xlsx"
    assert "Kobo backend export" in (submission.notes or "")
