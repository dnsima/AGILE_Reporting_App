"""Reconciling the monthly performance tracker against the quarterly framework.

The rule the NPCU states is simple -- the two streams must agree -- but the
arithmetic is not, because a quarter is not built from its months the same way
for every indicator. These tests pin that arithmetic down, and pin down the
cases where no verdict is possible yet, because a reconciliation that invents
discrepancies out of a late tracker return is worse than none at all.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.enums import (
    AggregationMethod,
    IndicatorUnit,
    PeriodType,
    ReconciliationStatus,
    Severity,
    SubmissionStatus,
    TimeBasis,
)
from app.models import (
    DataQuery,
    Indicator,
    IndicatorValue,
    StateSubcomponent,
    Subcomponent,
    Submission,
)
from app.services import queries, reconciliation, reference
from app.services.validation import run_validation

QUARTER = "2026-Q1"
MONTHS = ("2026-M01", "2026-M02", "2026-M03")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
@pytest.fixture()
def months(db):
    """The three months of 2026-Q1, which the fixture does not create."""
    rows = [reference.ensure_period(db, PeriodType.MONTHLY, 2026, n) for n in (1, 2, 3)]
    # Committed, not just flushed: the API tests read through their own session.
    db.commit()
    return rows


def _indicators(db) -> dict[str, Indicator]:
    return {row.code: row for row in db.query(Indicator).order_by(Indicator.number)}


def _submit(db, period_code: str, figures: dict[str, float], state: str = "KN") -> Submission:
    """Record a current submission carrying ``{indicator code: value}``."""
    state_row = reference.get_state_by_code(db, state)
    period_row = reference.get_period_by_code(db, period_code)
    version = 1
    for existing in db.query(Submission).filter_by(
        state_id=state_row.id, period_id=period_row.id
    ):
        existing.is_current = False
        version = max(version, existing.version + 1)

    submission = Submission(
        state_id=state_row.id,
        period_id=period_row.id,
        version=version,
        status=str(SubmissionStatus.APPROVED),
        is_current=True,
        uploaded_at=datetime.now(timezone.utc),
    )
    db.add(submission)
    db.flush()

    catalogue = _indicators(db)
    for code, value in figures.items():
        db.add(
            IndicatorValue(
                submission_id=submission.id,
                indicator_id=catalogue[code].id,
                value=value,
                original_value=value,
                disaggregation={},
            )
        )
    db.commit()
    return submission


def _line(result, code: str):
    for line in result.lines:
        if line.indicator_code == code:
            return line
    raise AssertionError(f"{code} is not in the reconciliation")


def _reconcile(db, state: str = "KN", period: str = QUARTER):
    return reconciliation.reconcile_state(
        db,
        reference.get_state_by_code(db, state),
        reference.get_period_by_code(db, period),
    )


# --------------------------------------------------------------------------
# Time basis -- the part that is easy to get silently wrong
# --------------------------------------------------------------------------
def test_a_cumulative_indicator_is_its_last_month_not_the_sum(db):
    """Adding the months of a running total triple-counts the quarter."""
    indicator = _indicators(db)["KPI-001"]
    assert indicator.is_cumulative is True
    assert indicator.aggregation_method == str(AggregationMethod.SUM)
    # SUM is how states combine into a national figure. Across time the same
    # indicator is a snapshot, and conflating the two axes is the error.
    assert reconciliation.time_basis(indicator) is TimeBasis.SNAPSHOT


def test_a_non_cumulative_count_is_still_a_position_not_a_flow(db):
    """`is_cumulative=False` means "not a running total", not "adds up".

    Fourteen of the real 53 are marked this way. Reading them as sums would
    tell every state its quarterly enrolment should equal April + May + June.
    """
    indicator = _indicators(db)["KPI-004"]
    indicator.is_cumulative = False
    assert reconciliation.time_basis(indicator) is TimeBasis.SNAPSHOT


def test_a_genuine_within_period_flow_is_marked_in_the_catalogue(db):
    """Summing months is opt-in, one cell, because getting it wrong is loud."""
    indicator = _indicators(db)["KPI-004"]
    indicator.is_cumulative = False
    indicator.time_basis = str(TimeBasis.SUM)
    assert reconciliation.time_basis(indicator) is TimeBasis.SUM


def test_an_unreadable_time_basis_falls_back_to_the_derivation(db):
    indicator = _indicators(db)["KPI-004"]
    indicator.time_basis = "MONTHLY-ISH"
    assert reconciliation.time_basis(indicator) is TimeBasis.SNAPSHOT


def test_a_rate_is_the_position_at_the_periods_end(db):
    indicator = _indicators(db)["KPI-002"]
    assert indicator.unit == str(IndicatorUnit.PERCENT)
    assert reconciliation.time_basis(indicator) is TimeBasis.SNAPSHOT


def test_a_yes_no_status_is_the_latest_answer(db):
    indicator = _indicators(db)["KPI-004"]
    indicator.unit = str(IndicatorUnit.BOOLEAN)
    indicator.is_cumulative = True  # must not win over the unit
    assert reconciliation.time_basis(indicator) is TimeBasis.LATEST


def test_max_and_min_carry_across_to_the_time_axis(db):
    indicator = _indicators(db)["KPI-004"]
    indicator.aggregation_method = str(AggregationMethod.MAX)
    assert reconciliation.time_basis(indicator) is TimeBasis.MAX
    indicator.aggregation_method = str(AggregationMethod.MIN)
    assert reconciliation.time_basis(indicator) is TimeBasis.MIN


@pytest.mark.parametrize(
    ("basis", "series", "expected"),
    [
        (TimeBasis.SNAPSHOT, [100, 250, 400], 400),
        (TimeBasis.LATEST, [1, 0, 1], 1),
        (TimeBasis.SUM, [12, 9, 14], 35),
        (TimeBasis.MAX, [12, 30, 14], 30),
        (TimeBasis.MIN, [12, 30, 14], 12),
        (TimeBasis.SUM, [], None),
    ],
)
def test_fold(basis, series, expected):
    assert reconciliation.fold(basis, [float(v) for v in series]) == expected


# --------------------------------------------------------------------------
# Period decomposition
# --------------------------------------------------------------------------
def test_a_quarter_decomposes_into_its_own_months(db, months):
    reference.ensure_period(db, PeriodType.MONTHLY, 2026, 4)  # outside Q1
    db.flush()
    quarter = reference.get_period_by_code(db, QUARTER)
    assert [p.code for p in reconciliation.enclosed_periods(db, quarter)] == list(MONTHS)


def test_grain_runs_from_annual_down_to_monthly():
    assert reconciliation.finer_grain(PeriodType.ANNUAL) is PeriodType.SEMI_ANNUAL
    assert reconciliation.finer_grain(PeriodType.QUARTERLY) is PeriodType.MONTHLY
    assert reconciliation.finer_grain(PeriodType.MONTHLY) is None


# --------------------------------------------------------------------------
# Verdicts
# --------------------------------------------------------------------------
def test_a_cumulative_figure_matching_its_final_month(db, months):
    for code, value in zip(MONTHS, [100.0, 250.0, 400.0], strict=True):
        _submit(db, code, {"KPI-001": value})
    _submit(db, QUARTER, {"KPI-001": 400.0})

    line = _line(_reconcile(db), "KPI-001")
    assert line.status is ReconciliationStatus.MATCHED
    assert line.fine_value == 400.0
    assert line.variance == 0.0


def test_a_cumulative_figure_that_disagrees_says_what_it_expected(db, months):
    for code, value in zip(MONTHS, [100.0, 250.0, 400.0], strict=True):
        _submit(db, code, {"KPI-001": value})
    _submit(db, QUARTER, {"KPI-001": 994.0})

    line = _line(_reconcile(db), "KPI-001")
    assert line.status is ReconciliationStatus.MISMATCH
    assert line.variance == 594.0
    assert "running total" in line.note
    assert "2026-M03's figure of 400" in line.note
    assert "The quarterly return says 994." in line.note


def test_a_within_period_count_is_reconciled_by_adding_the_months(db, months):
    counter = _indicators(db)["KPI-004"]
    counter.is_cumulative = False
    counter.time_basis = str(TimeBasis.SUM)
    db.flush()
    for code, value in zip(MONTHS, [12.0, 9.0, 14.0], strict=True):
        _submit(db, code, {"KPI-004": value})
    _submit(db, QUARTER, {"KPI-004": 31.0})

    line = _line(_reconcile(db), "KPI-004")
    assert line.basis is TimeBasis.SUM
    assert line.fine_value == 35.0
    assert line.status is ReconciliationStatus.MISMATCH
    assert "12 + 9 + 14 = 35" in line.note


def test_a_partial_tracker_never_manufactures_a_shortfall(db, months):
    """Two months of a three-month sum are always short of the quarter."""
    counter = _indicators(db)["KPI-004"]
    counter.is_cumulative = False
    counter.time_basis = str(TimeBasis.SUM)
    db.flush()
    for code, value in zip(MONTHS[:2], [12.0, 9.0], strict=True):
        _submit(db, code, {"KPI-004": value})
    _submit(db, QUARTER, {"KPI-004": 35.0})

    line = _line(_reconcile(db), "KPI-004")
    assert line.status is ReconciliationStatus.INCOMPLETE
    assert "needs all of them" in line.note
    assert line.parts_reported == list(MONTHS[:2])


def test_a_snapshot_is_answered_by_its_final_month_alone(db, months):
    """A missing January does not stop March's running total settling the quarter."""
    for code in MONTHS[1:]:
        _submit(db, code, {"KPI-001": 400.0})
    _submit(db, QUARTER, {"KPI-001": 400.0})

    line = _line(_reconcile(db), "KPI-001")
    assert line.status is ReconciliationStatus.MATCHED
    assert line.parts_reported == list(MONTHS[1:])


def test_a_snapshot_without_its_final_month_reaches_no_verdict(db, months):
    for code in MONTHS[:2]:
        _submit(db, code, {"KPI-001": 250.0})
    _submit(db, QUARTER, {"KPI-001": 400.0})

    assert _line(_reconcile(db), "KPI-001").status is ReconciliationStatus.INCOMPLETE


def test_reported_monthly_but_left_out_of_the_quarterly_return(db, months):
    for code in MONTHS:
        _submit(db, code, {"KPI-001": 400.0})
    _submit(db, QUARTER, {"KPI-002": 70.0})

    line = _line(_reconcile(db), "KPI-001")
    assert line.status is ReconciliationStatus.FRAMEWORK_MISSING
    assert "left out of the quarterly return" in line.note


def test_reported_quarterly_but_absent_from_the_tracker(db, months):
    for code in MONTHS:
        _submit(db, code, {"KPI-002": 70.0})
    _submit(db, QUARTER, {"KPI-001": 400.0, "KPI-002": 70.0})

    line = _line(_reconcile(db), "KPI-001")
    assert line.status is ReconciliationStatus.TRACKER_MISSING
    assert "never appears in the tracker" in line.note


def test_an_indicator_neither_stream_reported_is_not_a_line(db, months):
    for code in MONTHS:
        _submit(db, code, {"KPI-001": 400.0})
    _submit(db, QUARTER, {"KPI-001": 400.0})

    assert [line.indicator_code for line in _reconcile(db).lines] == ["KPI-001"]


# --------------------------------------------------------------------------
# Tolerance
# --------------------------------------------------------------------------
def test_a_rate_tolerates_rounding_but_not_a_real_difference(db, months):
    for code in MONTHS:
        _submit(db, code, {"KPI-002": 64.5})
    _submit(db, QUARTER, {"KPI-002": 64.55})
    assert _line(_reconcile(db), "KPI-002").status is ReconciliationStatus.MATCHED

    _submit(db, QUARTER, {"KPI-002": 65.0})
    assert _line(_reconcile(db), "KPI-002").status is ReconciliationStatus.MISMATCH


def test_a_count_must_agree_exactly(db, months):
    for code in MONTHS:
        _submit(db, code, {"KPI-001": 400.0})
    _submit(db, QUARTER, {"KPI-001": 401.0})
    assert _line(_reconcile(db), "KPI-001").status is ReconciliationStatus.MISMATCH


# --------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------
def test_a_sub_component_the_state_does_not_implement_is_not_reconciled(db, months):
    subcomponent = Subcomponent(code="C1.1", name="New builds", sort_order=1)
    other = Subcomponent(code="C1.2", name="Existing schools", sort_order=2)
    db.add_all([subcomponent, other])
    db.flush()
    catalogue = _indicators(db)
    catalogue["KPI-001"].subcomponent_id = subcomponent.id
    catalogue["KPI-002"].subcomponent_id = other.id
    state = reference.get_state_by_code(db, "KN")
    db.add_all([
        StateSubcomponent(state_id=state.id, subcomponent_id=subcomponent.id, implements=False),
        StateSubcomponent(state_id=state.id, subcomponent_id=other.id, implements=True),
    ])
    db.flush()

    for code in MONTHS:
        _submit(db, code, {"KPI-001": 400.0, "KPI-002": 70.0})
    _submit(db, QUARTER, {"KPI-001": 1.0, "KPI-002": 70.0})

    codes = [line.indicator_code for line in _reconcile(db).lines]
    assert codes == ["KPI-002"]


# --------------------------------------------------------------------------
# Roll-up
# --------------------------------------------------------------------------
def test_state_and_national_roll_up(db, months):
    for code, value in zip(MONTHS, [100.0, 250.0, 400.0], strict=True):
        _submit(db, code, {"KPI-001": value, "KPI-002": 70.0})
    _submit(db, QUARTER, {"KPI-001": 400.0, "KPI-002": 65.0})

    result = _reconcile(db)
    assert result.is_complete is True
    assert result.count(ReconciliationStatus.MATCHED) == 1
    assert result.count(ReconciliationStatus.MISMATCH) == 1
    assert result.agreement == 50.0
    assert [line.indicator_code for line in result.unreconciled] == ["KPI-002"]

    quarter = reference.get_period_by_code(db, QUARTER)
    summary = reconciliation.national_summary(
        reconciliation.reconcile_period(db, quarter)
    )
    assert summary["states"] == 4
    assert summary["states_reporting"] == 1
    assert summary["states_with_complete_tracker"] == 1
    assert summary["matched"] == 1
    assert summary["agreement"] == 50.0
    assert summary["states_unreconciled"] == ["KN"]


# --------------------------------------------------------------------------
# Validation rules
# --------------------------------------------------------------------------
def test_a_disagreement_becomes_a_finding_and_a_query(db, months):
    for code, value in zip(MONTHS, [100.0, 250.0, 400.0], strict=True):
        _submit(db, code, {"KPI-001": value})
    submission = _submit(db, QUARTER, {"KPI-001": 994.0})

    summary = run_validation(db, submission)
    findings = [issue for issue in summary.issues if issue.rule_code == "REC-001"]
    assert len(findings) == 1
    assert findings[0].severity == str(Severity.WARNING)
    assert "2026-M03's figure of 400" in findings[0].message

    opened = queries.raise_queries(db, submission)
    assert [query.rule_code for query in opened if query.rule_code == "REC-001"] == ["REC-001"]


def test_the_tracker_rules_stay_silent_until_the_tracker_starts(db, months):
    """Nobody is filing the tracker yet; 53 findings a quarter would be noise."""
    submission = _submit(db, QUARTER, {"KPI-001": 994.0, "KPI-004": 31.0})
    summary = run_validation(db, submission)
    assert [i for i in summary.issues if i.rule_code.startswith("REC-")] == []


def test_a_disagreement_does_not_block_or_quarantine_the_figure(db, months):
    """Figures change through the query process, not by being thrown out."""
    for code in MONTHS:
        _submit(db, code, {"KPI-001": 400.0})
    submission = _submit(db, QUARTER, {"KPI-001": 994.0})
    run_validation(db, submission)
    queries.raise_queries(db, submission)

    assert submission.status != str(SubmissionStatus.REJECTED)
    assert all(value.is_valid for value in submission.values)
    assert submission.open_query_count >= 1


def test_an_absent_indicator_is_only_a_finding_once_the_tracker_is_complete(db, months):
    for code in MONTHS[:2]:
        _submit(db, code, {"KPI-002": 70.0})
    submission = _submit(db, QUARTER, {"KPI-001": 400.0, "KPI-002": 70.0})
    summary = run_validation(db, submission)
    assert [i for i in summary.issues if i.rule_code == "REC-003"] == []

    _submit(db, MONTHS[2], {"KPI-002": 70.0})
    summary = run_validation(db, submission)
    absent = [i for i in summary.issues if i.rule_code == "REC-003"]
    assert [i.indicator_code for i in absent] == ["KPI-001"]


def test_a_monthly_figure_left_out_of_the_quarter_is_a_finding(db, months):
    for code in MONTHS:
        _submit(db, code, {"KPI-002": 70.0})
    submission = _submit(db, QUARTER, {"KPI-001": 400.0})
    summary = run_validation(db, submission)
    missing = [i for i in summary.issues if i.rule_code == "REC-002"]
    assert [i.indicator_code for i in missing] == ["KPI-002"]


# --------------------------------------------------------------------------
# The cascade
# --------------------------------------------------------------------------
def test_the_last_month_landing_re_checks_the_quarter(db, client, npcu_headers, months):
    """The quarter is filed before its third month, so the verdict arrives late."""
    for code in MONTHS[:2]:
        _submit(db, code, {"KPI-001": 250.0})
    quarterly = _submit(db, QUARTER, {"KPI-001": 994.0})
    run_validation(db, quarterly)
    assert quarterly.open_query_count == 0
    db.commit()  # release the write lock before the request opens its own session

    response = client.post(
        "/api/v1/ingestion/submissions",
        headers=npcu_headers,
        json={
            "state_code": "KN",
            "period_code": MONTHS[2],
            "values": [{"indicator_code": "KPI-001", "value": 400}],
        },
    )
    assert response.status_code == 201, response.text

    db.expire_all()
    refreshed = db.get(Submission, quarterly.id)
    assert refreshed.open_query_count >= 1
    raised = db.query(DataQuery).filter_by(submission_id=quarterly.id).all()
    assert "REC-001" in {query.rule_code for query in raised}


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
def test_endpoint_reports_the_period(db, client, npcu_headers, months):
    for code, value in zip(MONTHS, [100.0, 250.0, 400.0], strict=True):
        _submit(db, code, {"KPI-001": value, "KPI-002": 70.0})
    _submit(db, QUARTER, {"KPI-001": 400.0, "KPI-002": 65.0})

    response = client.get(f"/api/v1/reconciliation?period={QUARTER}", headers=npcu_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["summary"]["period_code"] == QUARTER
    assert body["summary"]["child_period_type"] == str(PeriodType.MONTHLY)
    assert body["summary"]["agreement"] == 50.0
    kano = next(row for row in body["states"] if row["state_code"] == "KN")
    assert kano["tracker_is_complete"] is True
    assert kano["mismatched"] == 1
    assert {line["indicator_code"] for line in kano["lines"]} == {"KPI-001", "KPI-002"}


def test_filtering_does_not_move_the_counts(db, client, npcu_headers, months):
    for code, value in zip(MONTHS, [100.0, 250.0, 400.0], strict=True):
        _submit(db, code, {"KPI-001": value, "KPI-002": 70.0})
    _submit(db, QUARTER, {"KPI-001": 400.0, "KPI-002": 65.0})

    response = client.get(
        f"/api/v1/reconciliation?period={QUARTER}&unreconciled_only=true", headers=npcu_headers
    )
    kano = next(row for row in response.json()["states"] if row["state_code"] == "KN")
    assert [line["indicator_code"] for line in kano["lines"]] == ["KPI-002"]
    # The headline still describes the whole reconciliation, not the filter.
    assert kano["matched"] == 1
    assert kano["judged"] == 2


def test_a_state_sees_only_its_own_reconciliation(db, client, state_headers, months):
    for code in MONTHS:
        _submit(db, code, {"KPI-001": 400.0})
        _submit(db, code, {"KPI-001": 400.0}, state="KD")

    response = client.get(f"/api/v1/reconciliation?period={QUARTER}", headers=state_headers)
    assert response.status_code == 200, response.text
    assert [row["state_code"] for row in response.json()["states"]] == ["KN"]

    denied = client.get(f"/api/v1/reconciliation/KD?period={QUARTER}", headers=state_headers)
    assert denied.status_code == 403


def test_state_endpoint_returns_one_states_lines(db, client, npcu_headers, months):
    for code, value in zip(MONTHS, [100.0, 250.0, 400.0], strict=True):
        _submit(db, code, {"KPI-001": value})
    _submit(db, QUARTER, {"KPI-001": 994.0})

    response = client.get(f"/api/v1/reconciliation/KN?period={QUARTER}", headers=npcu_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state_code"] == "KN"
    assert body["parts_expected"] == list(MONTHS)
    line = body["lines"][0]
    assert line["basis"] == str(TimeBasis.SNAPSHOT)
    assert line["status"] == str(ReconciliationStatus.MISMATCH)
    assert line["variance"] == 594.0
    assert line["part_values"] == {"2026-M01": 100.0, "2026-M02": 250.0, "2026-M03": 400.0}


def test_a_state_with_no_tracker_at_all_produces_no_lines(db, months):
    """53 "missing from tracker" rows would drown the handful that matter."""
    _submit(db, QUARTER, {"KPI-001": 400.0, "KPI-002": 70.0})

    result = _reconcile(db)
    assert result.lines == []
    assert result.parts_reported == []
    assert result.parts_expected == list(MONTHS)
    assert result.coarse_submission_id is not None
    assert result.agreement is None


def test_agreement_is_measured_over_states_that_actually_track(db, months):
    for code, value in zip(MONTHS, [100.0, 250.0, 400.0], strict=True):
        _submit(db, code, {"KPI-001": value})
    _submit(db, QUARTER, {"KPI-001": 400.0})
    # Three other states filed quarterly returns and no tracker at all.
    for other in ("KD", "GO", "LA"):
        _submit(db, QUARTER, {"KPI-001": 400.0}, state=other)

    quarter = reference.get_period_by_code(db, QUARTER)
    summary = reconciliation.national_summary(reconciliation.reconcile_period(db, quarter))
    assert summary["states_reporting"] == 4
    assert summary["states_tracking"] == 1
    assert summary["judged"] == 1
    assert summary["agreement"] == 100.0
