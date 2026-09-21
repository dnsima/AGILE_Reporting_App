"""The analysis model: one payload, every panel.

The board used to assemble each panel from its own query under its own subset
of five global filters, and the panels disagreed. A reporting rate read "18 of
11 states" because the denominator honoured a cohort filter the numerator did
not; the data-quality board ignored both. These tests hold the property that
replaced all three fixes: there is one scope, and every consumer reads the
same arrays.
"""

from __future__ import annotations

from datetime import datetime, timezone
from itertools import count

import pytest
from sqlalchemy import select

from app.core.enums import SubmissionStatus
from app.models import (
    Indicator,
    IndicatorCategory,
    IndicatorValue,
    Submission,
    Target,
)
from app.services import analysis_model, reference
from app.services.validation import run_validation

_numbers = count(900)


def _category(db, code, sort_order):
    existing = db.scalar(select(IndicatorCategory).where(IndicatorCategory.code == code))
    if existing:
        return existing
    row = IndicatorCategory(code=code, name=f"{code} indicators", sort_order=sort_order)
    db.add(row)
    db.flush()
    return row


def _indicator(db, code, *, component="PDO", unit="NUMBER", method="SUM", **kwargs):
    row = Indicator(
        code=code,
        number=next(_numbers),
        name=kwargs.pop("name", f"Indicator {code}"),
        category_id=_category(db, component, 1).id,
        unit=unit,
        aggregation_method=method,
        direction="INCREASE",
        **kwargs,
    )
    db.add(row)
    db.flush()
    return row


def _submission(db, state, period="2026-Q1"):
    state_row = reference.get_state_by_code(db, state)
    period_row = reference.get_period_by_code(db, period)
    for existing in db.query(Submission).filter_by(
        state_id=state_row.id, period_id=period_row.id
    ):
        existing.is_current = False
    row = Submission(
        state_id=state_row.id,
        period_id=period_row.id,
        version=1,
        status=str(SubmissionStatus.APPROVED),
        is_current=True,
        uploaded_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.flush()
    return row


def _report(db, submission, indicator, value):
    db.add(
        IndicatorValue(
            submission_id=submission.id,
            indicator_id=indicator.id,
            value=value,
            original_value=value,
            disaggregation={},
        )
    )
    db.flush()


@pytest.fixture()
def quarter(db):
    """Three of the four states report one count and one rate."""
    count_ind = _indicator(db, "PDO-90", name="Girls enrolled", is_cumulative=False)
    rate_ind = _indicator(db, "PDO-91", name="Girls completion rate", unit="PERCENT",
                          method="AVERAGE", max_value=100)
    flag_ind = _indicator(db, "C1-90", component="C1", name="Classrooms built",
                          is_cumulative=True)

    period = reference.get_period_by_code(db, "2026-Q1")
    db.add(
        Target(
            indicator_id=count_ind.id, period_id=period.id, level="NATIONAL",
            target_value=1000.0, source="test",
        )
    )

    # A cumulative figure that falls between quarters -- the finding the NPCU's
    # own validation review raised most often.
    before = _submission(db, "KN", "2025-Q4")
    _report(db, before, flag_ind, 500.0)

    for state, enrolled, rate in (("KN", 400.0, 80.0), ("KD", 300.0, 60.0), ("GO", 200.0, 40.0)):
        submission = _submission(db, state)
        _report(db, submission, count_ind, enrolled)
        _report(db, submission, rate_ind, rate)
        if state == "KN":
            _report(db, submission, flag_ind, 120.0)
            run_validation(db, submission)
    db.commit()
    return {"count": count_ind, "rate": rate_ind, "flag": flag_ind, "period": period}


# --------------------------------------------------------------------------
# One scope
# --------------------------------------------------------------------------
def test_every_row_carries_one_value_per_state_in_one_order(db, quarter):
    """The property the whole design rests on.

    Every panel indexes into the same ``states`` array. If a row's values
    could be a different length, or in a different order, two panels reading
    the same indicator would disagree -- which is exactly what happened.
    """
    model = analysis_model.build(db, "2026-Q1")

    assert len(model.states) == len(model.state_codes)
    for row in model.indicators:
        assert len(row.states) == len(model.states), row.code


def test_a_state_that_did_not_report_is_none_not_zero(db, quarter):
    """A dash and a zero are different conversations with a state."""
    model = analysis_model.build(db, "2026-Q1")
    row = model.find("PDO-90")
    lagos = model.states.index("Lagos")

    assert row.states[lagos] is None
    assert row.display(row.states[lagos]) == "—"
    assert row.reporting == 3
    assert row.expected == len(model.states)


def test_a_rate_is_averaged_and_a_count_is_summed(db, quarter):
    model = analysis_model.build(db, "2026-Q1")

    assert model.find("PDO-90").achieved == 900.0
    assert model.find("PDO-91").achieved == pytest.approx(60.0)


def test_the_national_total_counts_the_flagged_figure(db, quarter):
    """Holding doubtful figures out is what broke reconciliation with the NPCU.

    Excluding them made this platform's own totals disagree with the numbers
    in the NPCU's published technical report by nearly 200,000 on one
    indicator. A flagged figure counts, and is labelled.
    """
    model = analysis_model.build(db, "2026-Q1")
    row = model.find("C1-90")

    assert row.achieved == 120.0
    assert row.flag_severity, "the fall should have been flagged"


# --------------------------------------------------------------------------
# Flags
# --------------------------------------------------------------------------
def test_a_flag_names_the_state_the_tables_are_headed_with(db, quarter):
    """Names, not codes. The register and the table have to agree."""
    model = analysis_model.build(db, "2026-Q1")
    row = model.find("C1-90")

    assert set(row.flag_severity) <= set(model.states)
    assert "Kano" in row.flag_severity


def test_a_partial_total_says_so_on_the_national_figure(db, quarter):
    """Three states out of four is a partial total, whatever its findings say."""
    model = analysis_model.build(db, "2026-Q1")
    row = model.find("PDO-90")

    assert row.national_flag is not None
    assert "3 of 4" in row.flag_note


def test_states_findings_are_not_collapsed_onto_one_message(db):
    """Each state's message quotes its own figures.

    Printing the first state's numbers above a list of four states' names is
    how a register stops being trustworthy.
    """
    shared = analysis_model._shared_wording(
        [
            "C2.3-03 is cumulative but fell from 46,572 to 0.",
            "C2.3-03 is cumulative but fell from 145,840 to 72,920.",
        ]
    )
    assert "46,572" not in shared
    assert "145,840" not in shared
    assert shared.startswith("C2.3-03 is cumulative but fell")
    # ...and never ends on a dangling preposition.
    assert not shared.split("—")[0].strip().endswith("from")


def test_identical_messages_are_used_as_they_stand(db):
    same = "Reported as unfit for use."
    assert analysis_model._shared_wording([same, same]) == same


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------
def test_a_boolean_reads_yes_or_no_never_one_or_zero(db, quarter):
    flag = _indicator(db, "C3-90", component="C3", unit="BOOLEAN", method="COUNT_YES",
                      name="Policy adopted?")
    period = reference.get_period_by_code(db, "2026-Q1")
    submission = db.scalar(
        select(Submission).where(
            Submission.is_current.is_(True), Submission.period_id == period.id
        )
    )
    _report(db, submission, flag, 1.0)
    db.commit()

    model = analysis_model.build(db, "2026-Q1")
    row = model.find("C3-90")
    assert row.display(1.0) == "Yes"
    assert row.display(0.0) == "No"
    assert "/" in row.national_display  # "1 / 4", not "1"


def test_cohorts_are_a_dimension_not_a_filter(db, quarter):
    """Cohorts are carried as membership lists, so a chart can cut by them
    without any panel narrowing its own scope."""
    model = analysis_model.build(db, "2026-Q1")

    assert model.cohorts
    named = {name for members in model.cohorts.values() for name in members}
    assert named <= set(model.states)


def test_the_payload_carries_what_the_browser_draws(db, quarter):
    payload = analysis_model.to_payload(analysis_model.build(db, "2026-Q1"))

    assert set(payload) >= {
        "period_code", "states", "state_codes", "cohorts", "components",
        "indicators", "flags", "states_reporting",
    }
    row = next(r for r in payload["indicators"] if r["code"] == "PDO-90")
    assert len(row["display"]) == len(payload["states"])
    assert row["national_display"]


# --------------------------------------------------------------------------
# Over the API
# --------------------------------------------------------------------------
def test_the_endpoint_takes_a_period_and_nothing_else(client, npcu_headers, quarter):
    """One scope. A cohort or state parameter here is how the panels came to
    disagree in the first place, so there is nowhere to pass one."""
    response = client.get(
        "/api/v1/dashboard/analysis", headers=npcu_headers, params={"period": "2026-Q1"}
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["period_code"] == "2026-Q1"
    assert payload["states_reporting"] == 3


def test_a_state_piu_reads_its_own_column_and_the_national_picture(
    client, state_headers, quarter
):
    """A state may see where it sits nationally, not its neighbours' figures.

    The national totals stay whole -- they are every state's -- while the
    columns this reader may not see read as unreported.
    """
    payload = client.get(
        "/api/v1/dashboard/analysis", headers=state_headers, params={"period": "2026-Q1"}
    ).json()

    row = next(r for r in payload["indicators"] if r["code"] == "PDO-90")
    kano = payload["states"].index("Kano")
    kaduna = payload["states"].index("Kaduna")

    assert row["states"][kano] == 400.0
    assert row["states"][kaduna] is None, "another state's figure must not be readable"
    assert row["display"][kaduna] == "—"
    assert row["achieved"] == 900.0, "the national total is still every state's"
