"""What the DQA score counts, and what the grade refuses to hide.

The score is a per-check pass rate over thousands of checks, so it sits near
100 for any plausible return. Left alone it said "Excellent" for a state with
sixteen of its fifty-three figures held out of the national totals. Two things
fix that: a denominator that counts only checks a rule could actually run, and
a grade capped by how much of the data survives into the results.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.enums import (
    CRITICAL_USABLE_FIGURES_THRESHOLD,
    USABLE_FIGURES_THRESHOLD,
    SubmissionStatus,
    grade_for_score,
    grade_for_submission,
    grade_note,
)
from app.models import DQAScore, Indicator, IndicatorValue, Submission
from app.services import reference
from app.services.validation import run_validation
from app.services.validation.engine import build_context
from app.services.validation.rules import RULE_REGISTRY


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _submission(db, state="KN", period="2026-Q1"):
    state_row = reference.get_state_by_code(db, state)
    period_row = reference.get_period_by_code(db, period)
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
    return submission


def _value(db, submission, indicator, value, *, valid=True):
    row = IndicatorValue(
        submission_id=submission.id,
        indicator_id=indicator.id,
        value=value,
        original_value=value,
        is_valid=valid,
        disaggregation={},
    )
    db.add(row)
    db.flush()
    return row


def _weight(db, submission, code: str) -> int:
    ctx = build_context(db, submission)
    return RULE_REGISTRY[code].weight_fn(ctx)


@pytest.fixture()
def reported(db):
    """All four fixture indicators reported, none carrying numerator/denominator."""
    submission = _submission(db)
    for indicator in db.query(Indicator).order_by(Indicator.number):
        _value(db, submission, indicator, 100.0)
    return submission


# --------------------------------------------------------------------------
# The denominator
# --------------------------------------------------------------------------
class TestChecksActuallyRun:
    def test_a_rule_with_nothing_to_examine_declares_no_checks(self, db, reported):
        """INT-001 needs a numerator and a denominator; no figure has either."""
        assert _weight(db, reported, "INT-001") == 0
        assert _weight(db, reported, "INT-002") == 0
        assert _weight(db, reported, "VAL-004") == 0

    def test_a_rule_declares_exactly_what_it_can_examine(self, db, reported):
        indicators = db.query(Indicator).order_by(Indicator.number).all()
        indicators[0].requires_numerator_denominator = True
        indicators[1].requires_numerator_denominator = True
        db.flush()
        # Two indicators require the pair, so COM-002 has two checks, not four.
        assert _weight(db, reported, "COM-002") == 2

    def test_the_target_rule_counts_only_figures_with_a_target(self, db, reported):
        """Q1 has targets for all four; a period without them declares none."""
        assert _weight(db, reported, "ACC-001") == 4

        no_targets = _submission(db, period="2025-Q4")
        for indicator in db.query(Indicator).order_by(Indicator.number):
            _value(db, no_targets, indicator, 10.0)
        assert _weight(db, no_targets, "ACC-001") == 0

    def test_the_cumulative_rule_counts_only_cumulative_figures_with_a_baseline(
        self, db, reported
    ):
        assert _weight(db, reported, "CON-002") == 0  # no previous period yet

        earlier = _submission(db, period="2025-Q4")
        indicators = db.query(Indicator).order_by(Indicator.number).all()
        for indicator in indicators:
            _value(db, earlier, indicator, 50.0)
        later = _submission(db, period="2026-Q1")
        for indicator in indicators:
            _value(db, later, indicator, 60.0)
        db.flush()

        # Only KPI-001 is cumulative in the fixture.
        assert _weight(db, later, "CON-001") == 4
        assert _weight(db, later, "CON-002") == 1

    def test_checks_run_is_reported_honestly(self, db, reported):
        summary = run_validation(db, reported)
        by_dimension = {d.dimension: d for d in summary.dimensions}
        # Validity: VAL-001 and VAL-002 see all four; VAL-003 sees the whole
        # numbers; VAL-004 sees nothing; APP-001 sees nothing without a matrix.
        assert by_dimension["VALIDITY"].checks_run < 4 * 5


# --------------------------------------------------------------------------
# Dimensions that could not be assessed
# --------------------------------------------------------------------------
class TestUnassessedDimensions:
    def test_a_dimension_with_no_applicable_checks_scores_nothing(self, db, reported):
        """Not a perfect score. An unanswered question."""
        summary = run_validation(db, reported)
        consistency = next(d for d in summary.dimensions if d.dimension == "CONSISTENCY")
        assert consistency.checks_run == 0  # no previous period, no tracker
        assert consistency.score is None

    def test_an_unassessed_dimension_is_left_out_of_the_mean(self, db, reported):
        summary = run_validation(db, reported)
        assessed = [d for d in summary.dimensions if d.score is not None]
        assert all(d.score is not None for d in assessed)
        # The mean is over assessed dimensions only, so a dimension nobody
        # could check cannot lift the score.
        assert summary.overall_score <= max(d.score for d in assessed)

    def test_no_row_is_persisted_for_an_unassessed_dimension(self, db, reported):
        run_validation(db, reported)
        db.flush()
        stored = {
            row.dimension
            for row in db.query(DQAScore).filter_by(submission_id=reported.id)
        }
        assert "CONSISTENCY" not in stored
        assert "VALIDITY" in stored


# --------------------------------------------------------------------------
# The grade cap
# --------------------------------------------------------------------------
class TestGradeCap:
    def test_held_figures_cap_the_grade(self):
        """Kebbi's real Q2: a high pass rate over data a third of which is held."""
        assert grade_for_score(94.9) == "Excellent"
        assert grade_for_submission(94.9, [95.0], usable_share=69.8) == "Fair"
        assert grade_for_submission(94.9, [95.0], usable_share=84.9) == "Good"
        assert grade_for_submission(94.9, [95.0], usable_share=94.2) == "Excellent"

    def test_the_thresholds_are_the_ones_agreed(self):
        assert USABLE_FIGURES_THRESHOLD == 90.0
        assert CRITICAL_USABLE_FIGURES_THRESHOLD == 75.0

    def test_the_two_caps_are_not_charged_twice(self):
        """Held figures usually are the findings driving a weak dimension."""
        both = grade_for_submission(99.0, [50.0], usable_share=50.0)
        one = grade_for_submission(99.0, [50.0], usable_share=100.0)
        assert both == one == "Fair"  # two bands, not four

    def test_a_clean_submission_keeps_its_grade(self):
        assert grade_for_submission(99.0, [98.0], usable_share=100.0) == "Excellent"

    def test_no_usable_share_falls_back_to_the_dimension_cap_alone(self):
        assert grade_for_submission(99.0, [70.0], usable_share=None) == "Good"

    def test_the_note_says_why_the_grade_was_capped(self):
        note = grade_note(94.9, [95.0], usable_share=69.8)
        assert "Scored 94.9 (Excellent), graded Fair" in note
        assert "only 70% of its figures count towards the national totals" in note

    def test_an_uncapped_grade_carries_no_note(self):
        assert grade_note(99.0, [98.0], usable_share=100.0) is None


class TestEndToEnd:
    def test_a_held_figure_moves_the_grade_but_not_the_score(self, db, reported):
        clean = run_validation(db, reported)
        assert clean.usable_share_pct == 100.0
        assert clean.grade_note is None

        # Hold three of the four figures, as an open query would.
        for value in list(reported.values)[:3]:
            value.is_valid = False
        db.flush()
        held = run_validation(db, reported)

        assert held.overall_score == clean.overall_score  # the score cannot see it
        assert held.usable_share_pct == 25.0
        assert held.grade != clean.grade
        assert "25% of its figures" in held.grade_note

    def test_the_scorecard_shows_the_figures_behind_the_grade(self, db, reported):
        for value in list(reported.values)[:2]:
            value.is_valid = False
        run_validation(db, reported)
        db.flush()

        from app.services import dqa

        card = dqa.state_scorecard(
            db,
            reference.get_state_by_code(db, "KN"),
            reference.get_period_by_code(db, "2026-Q1"),
        )
        assert card.figures_reported == 4
        assert card.figures_counting == 2
        assert card.usable_share_pct == 50.0
        assert card.grade_note is not None
