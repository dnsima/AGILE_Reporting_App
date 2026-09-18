"""Rules and aggregations added for the real AGILE results framework."""

from __future__ import annotations

from datetime import datetime, timezone
from itertools import count

import pytest

from app.core.enums import SubmissionStatus
from app.models import (
    Indicator,
    IndicatorValue,
    StateSubcomponent,
    Subcomponent,
    Submission,
)
from app.services import analytics, reference
from app.services.analytics import Reading
from app.services.ingestion.mapper import to_boolean
from app.services.validation import run_validation

_next_number = count(900)


def _indicator(db, code, **kwargs):
    indicator = Indicator(
        code=code,
        number=kwargs.pop("number", next(_next_number)),
        name=kwargs.pop("name", code),
        unit=kwargs.pop("unit", "NUMBER"),
        aggregation_method=kwargs.pop("aggregation_method", "SUM"),
        direction="INCREASE",
        **kwargs,
    )
    db.add(indicator)
    db.flush()
    return indicator


def _submission(db, state="KN", period="2026-Q1"):
    state_row = reference.get_state_by_code(db, state)
    period_row = reference.get_period_by_code(db, period)
    previous = (
        db.query(Submission)
        .filter_by(state_id=state_row.id, period_id=period_row.id)
        .order_by(Submission.version.desc())
        .first()
    )
    submission = Submission(
        state_id=state_row.id,
        period_id=period_row.id,
        version=(previous.version + 1) if previous else 1,
        status=str(SubmissionStatus.UPLOADED),
        is_current=True,
        uploaded_at=datetime.now(timezone.utc),
    )
    db.add(submission)
    db.flush()
    return submission


def _value(db, submission, indicator, value):
    db.add(
        IndicatorValue(
            submission_id=submission.id,
            indicator_id=indicator.id,
            value=value,
            disaggregation={},
        )
    )
    db.flush()


def _issues(summary, rule_code):
    return [issue for issue in summary.issues if issue.rule_code == rule_code]


class TestBooleanIndicators:
    """States answer Yes/No questions in at least five different ways."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Yes", 1.0), ("YES", 1.0), ("y", 1.0), ("TRUE", 1.0), (True, 1.0),
            ("No", 0.0), ("n", 0.0), ("false", 0.0), (False, 0.0),
            (1, 1.0), (0, 0.0), ("1", 1.0), ("0", 0.0),
            ("", None), ("N/A", None), (None, None), ("perhaps", None),
        ],
    )
    def test_parsing(self, raw, expected):
        assert to_boolean(raw) == expected

    def test_national_figure_counts_yes_states(self, db):
        indicator = _indicator(db, "C2.1-03", unit="BOOLEAN", aggregation_method="COUNT_YES")
        readings = [Reading(value=v) for v in (1.0, 0.0, 1.0, 1.0, 0.0)]
        assert analytics.aggregate_national(readings, indicator).value == 3.0


class TestRateAggregation:
    def test_zero_reporting_states_are_excluded_from_a_rate(self, db):
        """A state reporting 0% is a non-entry, not a completion rate of zero."""
        indicator = _indicator(
            db, "PDO-07", unit="PERCENT", aggregation_method="AVERAGE_NONZERO"
        )
        readings = [Reading(value=v) for v in (64.0, 0.0, 56.0, 0.0, 72.0)]
        assert analytics.aggregate_national(readings, indicator).value == 64.0

    def test_plain_average_would_have_been_dragged_down(self, db):
        indicator = _indicator(db, "PDO-99", unit="PERCENT", aggregation_method="AVERAGE")
        readings = [Reading(value=v) for v in (64.0, 0.0, 56.0, 0.0, 72.0)]
        assert analytics.aggregate_national(readings, indicator).value == pytest.approx(38.4)

    def test_a_rate_is_never_summed(self, db):
        indicator = _indicator(
            db, "PDO-98", unit="PERCENT", aggregation_method="AVERAGE_NONZERO"
        )
        readings = [Reading(value=v) for v in (64.09, 63.26, 54.78)]
        result = analytics.aggregate_national(readings, indicator).value
        assert result < 100  # the 1,089% summed-rates error cannot occur
        assert result == pytest.approx(60.71, abs=0.01)

    def test_rates_offer_no_contribution_share(self, db):
        """A share of an averaged rate is arithmetic without meaning."""
        indicator = _indicator(
            db, "PDO-97", unit="PERCENT", aggregation_method="AVERAGE_NONZERO"
        )
        assert analytics._contribution_basis(Reading(value=64.0), indicator) is None


class TestCompositeIndicators:
    def test_composite_must_equal_its_parts(self, db):
        """Reproduces the NPCU's Plateau finding: 994 reported, parts sum to 714."""
        total = _indicator(db, "C1.0-01", composite_of=["C1.1-01", "C1.2-01", "C1.2-02"])
        new_build = _indicator(db, "C1.1-01")
        existing = _indicator(db, "C1.2-01")
        rehab = _indicator(db, "C1.2-02")

        submission = _submission(db)
        _value(db, submission, total, 994.0)
        _value(db, submission, new_build, 0.0)
        _value(db, submission, existing, 644.0)
        _value(db, submission, rehab, 70.0)

        summary = run_validation(db, submission)
        finding = _issues(summary, "INT-005")
        assert finding, "composite mismatch not detected"
        assert "994" in finding[0].message and "714" in finding[0].message
        assert finding[0].is_blocking
        assert summary.blocking

    def test_composite_holds_when_a_part_is_not_implemented(self, db):
        """Ekiti does not implement 1.1 and reports zero; the identity still holds."""
        total = _indicator(db, "C1.0-02", composite_of=["C1.1-02", "C1.2-03", "C1.2-04"])
        new_build = _indicator(db, "C1.1-02")
        existing = _indicator(db, "C1.2-03")
        rehab = _indicator(db, "C1.2-04")

        submission = _submission(db, state="KD")
        _value(db, submission, total, 829.0)
        _value(db, submission, new_build, 0.0)
        _value(db, submission, existing, 61.0)
        _value(db, submission, rehab, 768.0)

        assert not _issues(run_validation(db, submission), "INT-005")

    def test_unreported_part_is_left_to_the_completeness_check(self, db):
        total = _indicator(db, "C1.0-03", composite_of=["C1.1-03", "C1.2-06"])
        part = _indicator(db, "C1.1-03")
        _indicator(db, "C1.2-06")

        submission = _submission(db)
        _value(db, submission, total, 500.0)
        _value(db, submission, part, 100.0)

        assert not _issues(run_validation(db, submission), "INT-005")


class TestApplicability:
    def _limit_state_to(self, db, state_code, subcomponent_code):
        subcomponent = Subcomponent(code=subcomponent_code, name=subcomponent_code)
        db.add(subcomponent)
        db.flush()
        state = reference.get_state_by_code(db, state_code)
        db.add(
            StateSubcomponent(
                state_id=state.id, subcomponent_id=subcomponent.id, implements=True
            )
        )
        db.flush()
        return subcomponent

    def test_value_outside_the_implemented_scope_is_flagged(self, db):
        implemented = self._limit_state_to(db, "KN", "C1.2")
        excluded = Subcomponent(code="C1.1", name="C1.1")
        db.add(excluded)
        db.flush()

        inside = _indicator(db, "C1.2-09", subcomponent_id=implemented.id)
        outside = _indicator(db, "C1.1-09", subcomponent_id=excluded.id)

        submission = _submission(db)
        _value(db, submission, inside, 50.0)
        _value(db, submission, outside, 120.0)

        finding = _issues(run_validation(db, submission), "APP-001")
        assert finding
        assert "C1.1-09" in finding[0].message
        assert "does not implement" in finding[0].message

    def test_a_zero_outside_scope_is_not_flagged(self, db):
        """Non-implementation is reported as zero; that is correct, not an error."""
        implemented = self._limit_state_to(db, "KN", "C1.2")
        excluded = Subcomponent(code="C1.1", name="C1.1")
        db.add(excluded)
        db.flush()
        _indicator(db, "C1.2-08", subcomponent_id=implemented.id)
        outside = _indicator(db, "C1.1-08", subcomponent_id=excluded.id)

        submission = _submission(db)
        _value(db, submission, outside, 0.0)

        assert not _issues(run_validation(db, submission), "APP-001")

    def test_expectations_follow_the_matrix(self, db):
        """Only indicators in implemented sub-components count for completeness."""
        implemented = self._limit_state_to(db, "KN", "C2.1")
        excluded = Subcomponent(code="C2.2b", name="C2.2b")
        db.add(excluded)
        db.flush()
        expected = _indicator(db, "C2.1-07", subcomponent_id=implemented.id)
        _indicator(db, "C2.2b-07", subcomponent_id=excluded.id)

        submission = _submission(db)
        _value(db, submission, expected, 10.0)

        summary = run_validation(db, submission)
        completeness = next(
            d for d in summary.dimensions if d.dimension == "COMPLETENESS"
        )
        assert completeness.score == 100.0
        assert not _issues(summary, "COM-001")


class TestProvisionalBaseline:
    def test_comparison_still_runs_when_the_prior_quarter_was_rejected(self, db):
        """A state whose previous quarter failed is the one most worth comparing."""
        indicator = _indicator(db, "C1.0-09", is_cumulative=True)

        earlier = _submission(db, period="2025-Q4")
        _value(db, earlier, indicator, 1577.0)
        earlier.status = str(SubmissionStatus.REJECTED)
        db.flush()

        later = _submission(db, period="2026-Q1")
        _value(db, later, indicator, 342.0)
        summary = run_validation(db, later)

        finding = _issues(summary, "CON-002")
        assert finding, "cumulative fall missed because the baseline was rejected"
        assert "1,577" in finding[0].message and "342" in finding[0].message
        assert "did not pass validation" in finding[0].message
