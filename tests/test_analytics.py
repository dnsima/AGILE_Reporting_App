"""KPI consolidation: the three analysis layers, aggregation and trends."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.enums import SubmissionStatus
from app.models import IndicatorValue, Submission
from app.services import analytics, reference


def _approved(db, state_code, period_code, values):
    """Create an approved submission carrying ``{indicator_code: kwargs}``."""
    state = reference.get_state_by_code(db, state_code)
    period = reference.get_period_by_code(db, period_code)
    previous = (
        db.query(Submission)
        .filter_by(state_id=state.id, period_id=period.id)
        .order_by(Submission.version.desc())
        .first()
    )
    submission = Submission(
        state_id=state.id,
        period_id=period.id,
        version=(previous.version + 1) if previous else 1,
        status=str(SubmissionStatus.APPROVED),
        is_current=True,
        uploaded_at=datetime.now(timezone.utc),
        approved_at=datetime.now(timezone.utc),
    )
    db.add(submission)
    db.flush()
    for code, payload in values.items():
        indicator = reference.get_indicator_by_code(db, code)
        db.add(
            IndicatorValue(
                submission_id=submission.id,
                indicator_id=indicator.id,
                disaggregation=payload.pop("disaggregation", {}),
                **payload,
            )
        )
    db.flush()
    analytics.clear_analysis_cache(db)
    return submission


class TestAchievement:
    def test_increase_indicator(self, db):
        indicator = reference.get_indicator_by_code(db, "KPI-001")
        assert analytics.achievement(800, 1000, indicator) == 80.0
        assert analytics.achievement(1200, 1000, indicator) == 120.0

    def test_decrease_indicator_uses_the_baseline_journey(self, db):
        """KPI-003 has baseline 80 and a lower-is-better direction."""
        indicator = reference.get_indicator_by_code(db, "KPI-003")
        # Halfway from the 80 baseline to a target of 40.
        assert analytics.achievement(60, 40, indicator) == 50.0
        assert analytics.achievement(40, 40, indicator) == 100.0
        assert analytics.achievement(80, 40, indicator) == 0.0

    def test_missing_value_or_target_yields_none(self, db):
        indicator = reference.get_indicator_by_code(db, "KPI-001")
        assert analytics.achievement(None, 1000, indicator) is None
        assert analytics.achievement(500, None, indicator) is None

    def test_status_bands(self):
        assert analytics.status_for(95) == "On track"
        assert analytics.status_for(75) == "Progressing"
        assert analytics.status_for(55) == "Lagging"
        assert analytics.status_for(10) == "Off track"
        assert analytics.status_for(None) == "No target"


class TestAggregation:
    def test_counts_are_summed(self, db):
        indicator = reference.get_indicator_by_code(db, "KPI-001")
        readings = [analytics.Reading(value=v) for v in (100, 200, 300)]
        assert analytics.aggregate_national(readings, indicator).value == 600

    def test_percentages_are_weighted_by_their_components(self, db):
        """A small state at 100% must not outweigh a large state at 50%."""
        indicator = reference.get_indicator_by_code(db, "KPI-002")
        readings = [
            analytics.Reading(value=50.0, numerator=500, denominator=1000),
            analytics.Reading(value=100.0, numerator=10, denominator=10),
        ]
        result = analytics.aggregate_national(readings, indicator)
        assert result.value == pytest.approx(510 / 1010 * 100)
        assert result.value < 55  # not the 75% an unweighted mean would give

    def test_ratios_are_averaged(self, db):
        indicator = reference.get_indicator_by_code(db, "KPI-003")
        readings = [analytics.Reading(value=v) for v in (30.0, 50.0)]
        assert analytics.aggregate_national(readings, indicator).value == 40.0

    def test_states_without_data_are_ignored(self, db):
        indicator = reference.get_indicator_by_code(db, "KPI-001")
        readings = [analytics.Reading(value=100), analytics.Reading(), analytics.Reading(value=50)]
        result = analytics.aggregate_national(readings, indicator)
        assert result.value == 150


class TestThreeLayerAnalysis:
    @pytest.fixture()
    def populated(self, db):
        _approved(db, "KN", "2026-Q1", {"KPI-001": {"value": 900.0}})
        _approved(db, "KD", "2026-Q1", {"KPI-001": {"value": 600.0}})
        _approved(db, "GO", "2026-Q1", {"KPI-001": {"value": 400.0}})
        _approved(db, "LA", "2026-Q1", {"KPI-001": {"value": 100.0}})
        return db

    def test_state_layer_measures_against_state_targets(self, populated):
        db = populated
        analysis = analytics.analyse_indicator(
            db,
            reference.get_indicator_by_code(db, "KPI-001"),
            reference.get_period_by_code(db, "2026-Q1"),
        )
        rows = {row.state_code: row for row in analysis.states}
        assert rows["KN"].value == 900
        assert rows["KN"].target == 1000
        assert rows["KN"].achievement_pct == 90.0
        assert rows["KN"].status == "On track"
        assert rows["LA"].achievement_pct == 10.0
        assert rows["LA"].status == "Off track"

    def test_national_layer_consolidates_every_state(self, populated):
        db = populated
        analysis = analytics.analyse_indicator(
            db,
            reference.get_indicator_by_code(db, "KPI-001"),
            reference.get_period_by_code(db, "2026-Q1"),
        )
        assert analysis.national.value == 2000
        assert analysis.national.target == 4000  # four state targets of 1,000
        assert analysis.national.achievement_pct == 50.0
        assert analysis.national.states_reporting == 4
        assert analysis.national.states_expected == 4

    def test_contribution_layer_sums_to_one_hundred(self, populated):
        db = populated
        analysis = analytics.analyse_indicator(
            db,
            reference.get_indicator_by_code(db, "KPI-001"),
            reference.get_period_by_code(db, "2026-Q1"),
        )
        contributions = {row.state_code: row.contribution_pct for row in analysis.states}
        assert contributions["KN"] == 45.0
        assert contributions["KD"] == 30.0
        assert sum(contributions.values()) == pytest.approx(100.0, abs=0.05)

    def test_results_are_disaggregated_by_cohort(self, populated):
        db = populated
        analysis = analytics.analyse_indicator(
            db,
            reference.get_indicator_by_code(db, "KPI-001"),
            reference.get_period_by_code(db, "2026-Q1"),
        )
        cohorts = {row.cohort_code: row for row in analysis.cohorts}
        assert cohorts["ORIGINAL"].value == 1500  # Kano + Kaduna
        assert cohorts["ORIGINAL"].contribution_pct == 75.0
        assert cohorts["ADDITIONAL"].value == 400
        assert cohorts["LIMITED"].states_expected == 1


class TestPipelineGate:
    def test_only_approved_current_data_is_analysed(self, db):
        submission = _approved(db, "KN", "2026-Q1", {"KPI-001": {"value": 900.0}})
        period = reference.get_period_by_code(db, "2026-Q1")
        indicator = reference.get_indicator_by_code(db, "KPI-001")

        assert analytics.analyse_indicator(db, indicator, period).national.value == 900

        submission.status = str(SubmissionStatus.REJECTED)
        db.flush()
        analytics.clear_analysis_cache(db)
        assert analytics.analyse_indicator(db, indicator, period).national.value is None

    def test_superseded_versions_are_excluded(self, db):
        first = _approved(db, "KN", "2026-Q1", {"KPI-001": {"value": 500.0}})
        first.is_current = False
        first.status = str(SubmissionStatus.SUPERSEDED)
        db.flush()
        _approved(db, "KN", "2026-Q1", {"KPI-001": {"value": 800.0}})

        period = reference.get_period_by_code(db, "2026-Q1")
        indicator = reference.get_indicator_by_code(db, "KPI-001")
        assert analytics.analyse_indicator(db, indicator, period).national.value == 800


class TestDisaggregatedRows:
    def test_an_explicit_total_row_wins_over_its_parts(self, db):
        _approved(
            db,
            "KN",
            "2026-Q1",
            {"KPI-001": {"value": 900.0, "disaggregation": {"sex": "total"}}},
        )
        period = reference.get_period_by_code(db, "2026-Q1")
        indicator = reference.get_indicator_by_code(db, "KPI-001")
        assert analytics.analyse_indicator(db, indicator, period).national.value == 900

    def test_parts_are_combined_when_no_total_is_given(self, db):
        state = reference.get_state_by_code(db, "KN")
        period = reference.get_period_by_code(db, "2026-Q1")
        indicator = reference.get_indicator_by_code(db, "KPI-001")
        submission = Submission(
            state_id=state.id, period_id=period.id, version=1,
            status=str(SubmissionStatus.APPROVED), is_current=True,
            uploaded_at=datetime.now(timezone.utc),
        )
        db.add(submission)
        db.flush()
        for sex, value in (("female", 600.0), ("male", 300.0)):
            db.add(
                IndicatorValue(
                    submission_id=submission.id, indicator_id=indicator.id,
                    value=value, disaggregation={"sex": sex},
                )
            )
        db.flush()
        analytics.clear_analysis_cache(db)

        assert analytics.analyse_indicator(db, indicator, period).national.value == 900


class TestTrends:
    def test_series_runs_across_periods_and_reports_direction(self, db):
        _approved(db, "KN", "2025-Q4", {"KPI-001": {"value": 400.0}})
        _approved(db, "KN", "2026-Q1", {"KPI-001": {"value": 800.0}})

        series = analytics.trend(
            db,
            reference.get_indicator_by_code(db, "KPI-001"),
            scope="STATE",
            state_code="KN",
            period_type="QUARTERLY",
        )
        assert [point.value for point in series.points] == [400.0, 800.0]
        assert series.change_pct == 100.0
        assert series.direction_of_travel == "improving"

    def test_falling_value_is_improving_for_a_decrease_indicator(self, db):
        _approved(db, "KN", "2025-Q4", {"KPI-003": {"value": 70.0}})
        _approved(db, "KN", "2026-Q1", {"KPI-003": {"value": 45.0}})

        series = analytics.trend(
            db,
            reference.get_indicator_by_code(db, "KPI-003"),
            scope="STATE",
            state_code="KN",
            period_type="QUARTERLY",
        )
        assert series.direction_of_travel == "improving"


class TestScorecard:
    def test_counts_data_targets_and_on_track_indicators(self, db):
        _approved(
            db,
            "KN",
            "2026-Q1",
            {
                "KPI-001": {"value": 950.0},
                "KPI-004": {"value": 300.0},
            },
        )
        board = analytics.scorecard(
            db, reference.get_period_by_code(db, "2026-Q1"), scope="STATE", state_code="KN"
        )
        assert board.scope_label == "Kano"
        assert board.indicators_with_data == 2
        assert board.indicators_with_target == 4
        assert board.indicators_on_track == 1  # KPI-001 at 95%
        assert board.average_achievement_pct == pytest.approx((95 + 30) / 2)
