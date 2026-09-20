"""Disclosure and exposure: what the platform says about a doubted figure.

The contract these tests hold in place is that the platform never removes a
reported figure from a total. It reports what states reported, says what it
doubts, and leaves changing a figure to the change-management process.
"""

from __future__ import annotations

from datetime import datetime, timezone
from itertools import count

import pytest

from app.core.enums import (
    DisclosureStatus,
    FitnessVerdict,
    SubmissionStatus,
    verdict_for_submission,
)
from app.models import Indicator, IndicatorValue, Submission
from app.services import analytics, exposure, queries, reference
from app.services.validation import run_validation

_numbers = count(1200)


def _indicator(db, code, *, cumulative=True):
    indicator = Indicator(
        code=code,
        number=next(_numbers),
        name=code,
        unit="NUMBER",
        aggregation_method="SUM",
        direction="INCREASE",
        is_cumulative=cumulative,
    )
    db.add(indicator)
    db.flush()
    return indicator


def _submission(db, state, period):
    state_row = reference.get_state_by_code(db, state)
    period_row = reference.get_period_by_code(db, period)
    for previous in db.query(Submission).filter_by(
        state_id=state_row.id, period_id=period_row.id
    ):
        previous.is_current = False
    submission = Submission(
        state_id=state_row.id,
        period_id=period_row.id,
        version=1,
        status=str(SubmissionStatus.APPROVED),
        is_current=True,
        uploaded_at=datetime.now(timezone.utc),
    )
    db.add(submission)
    db.flush()
    return submission


def _value(db, submission, indicator, value):
    row = IndicatorValue(
        submission_id=submission.id,
        indicator_id=indicator.id,
        value=value,
        original_value=value,
    )
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def collapse(db):
    """One state's cumulative figure collapses; another state reports soundly.

    Modelled on Gombe's Q2 2026 return, where 5,960 schools became 127.
    """
    indicator = _indicator(db, "EXP-01")
    earlier = _submission(db, "KN", "2025-Q4")
    _value(db, earlier, indicator, 5_960)

    later = _submission(db, "KN", "2026-Q1")
    flagged = _value(db, later, indicator, 127)
    run_validation(db, later)
    queries.raise_queries(db, later)

    sound = _submission(db, "KD", "2026-Q1")
    _value(db, sound, indicator, 1_000)
    run_validation(db, sound)

    analytics.clear_analysis_cache(db)
    return indicator, flagged, later


class TestTotalsStayAsReported:
    def test_the_flagged_figure_is_still_in_the_national_total(self, db, collapse):
        indicator, _flagged, _later = collapse
        period = reference.get_period_by_code(db, "2026-Q1")
        national = analytics.analyse_indicator(db, indicator, period).national
        assert national.value == 1_127  # 127 flagged + 1,000 sound

    def test_the_figure_is_marked_unfit_all_the_same(self, db, collapse):
        _indicator, flagged, _later = collapse
        assert flagged.is_valid is False
        assert flagged.disclosure_status == str(DisclosureStatus.UNFIT)
        assert "CON-002" in flagged.quarantine_reason


class TestExposure:
    def test_the_damage_is_measured_against_the_error_not_the_figure(self, db, collapse):
        _indicator, _flagged, _later = collapse
        period = reference.get_period_by_code(db, "2026-Q1")
        entry = exposure.by_indicator(db, period)[0]

        assert entry.indicator_code == "EXP-01"
        assert entry.reported_total == 1_127
        # The figure is 127; the error is 5,960 - 127.
        assert entry.net_distortion == pytest.approx(5_833)
        assert entry.is_material

    def test_the_disclosure_line_names_the_state_and_the_direction(self, db, collapse):
        _indicator, _flagged, _later = collapse
        period = reference.get_period_by_code(db, "2026-Q1")
        note = exposure.by_indicator(db, period)[0].note()
        assert "KN" in note
        assert "understated" in note
        assert "5,833" in note

    def test_a_state_reporting_soundly_carries_no_exposure(self, db, collapse):
        period = reference.get_period_by_code(db, "2026-Q1")
        by_state = {s.state_code: s for s in exposure.by_state(db, period)}
        assert by_state["KD"].exposed_share == 0.0
        assert by_state["KN"].exposed_share > 0


class TestVerdict:
    """Materiality decides, not the count of findings."""

    def test_a_material_finding_makes_a_return_unfit(self):
        assert (
            verdict_for_submission(
                score=99.13,
                exposed_share=58.6,
                material_findings=1,
                open_findings=3,
            )
            is FitnessVerdict.NOT_FIT
        )

    def test_a_rounding_artefact_does_not(self):
        """Borno's Q2 direct-reach figure fell by eight in a million."""
        assert (
            verdict_for_submission(
                score=98.57,
                exposed_share=0.1,
                material_findings=0,
                open_findings=1,
            )
            is FitnessVerdict.FIT_WITH_NOTES
        )

    def test_a_clean_return_is_fit(self):
        assert (
            verdict_for_submission(
                score=100.0,
                exposed_share=0.0,
                material_findings=0,
                open_findings=0,
            )
            is FitnessVerdict.FIT
        )

    def test_exposure_alone_can_condemn_a_return(self):
        """Many small findings still add up to a return that cannot be used."""
        assert (
            verdict_for_submission(
                score=97.0,
                exposed_share=42.0,
                material_findings=0,
                open_findings=9,
            )
            is FitnessVerdict.NOT_FIT
        )

    def test_the_verdict_is_recorded_on_the_submission(self, db, collapse):
        from app.services.ingestion.pipeline import revalidate_period

        _indicator, _flagged, later = collapse
        period = reference.get_period_by_code(db, "2026-Q1")
        revalidate_period(db, period)
        assert later.fitness_verdict == str(FitnessVerdict.NOT_FIT)
        assert later.exposed_share > 0
