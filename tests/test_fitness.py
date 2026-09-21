"""The fitness verdict, and how a state's return is reported without a score.

The platform used to score a return out of 100 and grade it Excellent to
Poor. The score was a pass rate over thousands of automated checks, so it sat
near 100 for any plausible return: Gombe scored 99.13 in a quarter where it
reported 127 schools against the 5,960 it reported the quarter before -- an
error large enough to move the national figure by 89% -- and a reader seeing
"Excellent" had no way to know.

Both are gone. These tests hold what replaced them: a verdict that answers
"can I use this?", decided by whether a finding is large enough to matter.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.enums import (
    FitnessVerdict,
    SubmissionStatus,
    verdict_for_submission,
    verdict_note,
)
from app.models import Indicator, IndicatorValue, Submission
from app.services import reference, returns
from app.services.validation import run_validation


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


@pytest.fixture()
def reported(db):
    submission = _submission(db)
    for indicator in db.query(Indicator).order_by(Indicator.number):
        _value(db, submission, indicator, 100.0)
    return submission


# --------------------------------------------------------------------------
# The verdict
# --------------------------------------------------------------------------
class TestVerdict:
    def test_a_return_with_no_findings_is_fit(self):
        assert (
            verdict_for_submission(
                has_data=True, exposed_share=0.0, material_findings=0, open_findings=0
            )
            is FitnessVerdict.FIT
        )

    def test_findings_that_cannot_move_a_national_figure_leave_it_usable(self):
        """Materiality decides, not the count.

        A blocking finding whose error is a rounding artefact is still a
        finding worth chasing, and still leaves the return usable.
        """
        assert (
            verdict_for_submission(
                has_data=True, exposed_share=1.0, material_findings=0, open_findings=9
            )
            is FitnessVerdict.FIT_WITH_NOTES
        )

    def test_one_material_finding_is_enough(self):
        """Gombe's case: one figure, large enough to move the national total."""
        assert (
            verdict_for_submission(
                has_data=True, exposed_share=0.5, material_findings=1, open_findings=1
            )
            is FitnessVerdict.NOT_FIT
        )

    def test_enough_exposure_is_enough_without_any_one_finding_being_material(self):
        """Many small findings can add up to a return nobody should quote."""
        assert (
            verdict_for_submission(
                has_data=True, exposed_share=18.0, material_findings=0, open_findings=12
            )
            is FitnessVerdict.NOT_FIT
        )

    def test_a_state_that_filed_nothing_reads_as_no_data(self):
        """Not "fit", and not "not fit" either -- there is nothing to judge."""
        assert (
            verdict_for_submission(
                has_data=False, exposed_share=None, material_findings=0, open_findings=0
            )
            is FitnessVerdict.NO_DATA
        )

    def test_the_verdict_no_longer_depends_on_a_score(self):
        """The signature is the point: there is no score left to pass in."""
        import inspect

        parameters = inspect.signature(verdict_for_submission).parameters
        assert "score" not in parameters
        assert "has_data" in parameters

    def test_every_verdict_but_no_data_explains_itself(self):
        """An unexplained verdict is worse than none."""
        for verdict in (
            FitnessVerdict.FIT,
            FitnessVerdict.FIT_WITH_NOTES,
            FitnessVerdict.NOT_FIT,
        ):
            note = verdict_note(
                verdict, exposed_share=12.0, material_findings=2, open_findings=5
            )
            assert note, verdict


# --------------------------------------------------------------------------
# What is gone
# --------------------------------------------------------------------------
class TestScoringIsGone:
    def test_the_summary_carries_no_score_or_grade(self, db, reported):
        summary = run_validation(db, reported)

        for field in ("overall_score", "grade", "grade_note", "dimensions"):
            assert not hasattr(summary, field), f"{field} survived the removal"

    def test_findings_are_counted_by_area_not_scored(self, db, reported):
        """A count says what needs looking at; a score made a claim about the whole."""
        summary = run_validation(db, reported)

        assert isinstance(summary.findings_by_dimension, dict)
        assert all(isinstance(count, int) for count in summary.findings_by_dimension.values())

    def test_the_submission_stores_no_score(self, db, reported):
        run_validation(db, reported)

        assert not hasattr(reported, "dqa_score")
        assert not hasattr(reported, "dqa_grade")

    def test_the_grading_functions_are_gone(self):
        from app.core import enums

        for name in (
            "grade_for_score", "grade_for_submission", "grade_note", "GRADE_BANDS",
        ):
            assert not hasattr(enums, name), f"{name} survived the removal"

    def test_rules_no_longer_carry_a_weight(self):
        """Weights existed only to size a score's denominator."""
        from app.services.validation.rules import RULE_REGISTRY

        assert RULE_REGISTRY
        for definition in RULE_REGISTRY.values():
            assert not hasattr(definition, "weight_fn"), definition.code


# --------------------------------------------------------------------------
# The returns service
# --------------------------------------------------------------------------
class TestStateReturn:
    def test_a_state_that_filed_nothing_says_so(self, db):
        state = reference.get_state_by_code(db, "LA")
        period = reference.get_period_by_code(db, "2026-Q1")
        row = returns.state_return(db, state, period)

        assert row.submission_id is None
        assert row.status == "NOT_SUBMITTED"
        assert row.fitness_verdict == str(FitnessVerdict.NO_DATA)
        assert row.verdict_note

    def test_a_return_reports_its_findings_and_coverage(self, db, reported):
        run_validation(db, reported)
        db.commit()
        state = reference.get_state_by_code(db, "KN")
        period = reference.get_period_by_code(db, "2026-Q1")
        row = returns.state_return(db, state, period)

        assert row.submission_id == reported.id
        assert row.figures_reported == 4
        assert row.usable_share_pct is not None

    def test_the_national_view_counts_verdicts_rather_than_averaging_a_score(
        self, db, reported
    ):
        """Averaging a pass rate across states hid the state that broke.

        The national headline is now how many returns a reader cannot rely on.
        """
        run_validation(db, reported)
        db.commit()
        period = reference.get_period_by_code(db, "2026-Q1")
        national = returns.national_returns(db, period)

        assert not hasattr(national, "national_score")
        assert not hasattr(national, "grade")
        assert (
            national.states_fit + national.states_fit_with_notes + national.states_not_fit
            <= national.states_reported
        )

    def test_the_worst_returns_sort_first(self, db, reported):
        """A reader opens this list to find the returns that need work."""
        run_validation(db, reported)
        db.commit()
        period = reference.get_period_by_code(db, "2026-Q1")
        rows = returns.national_returns(db, period).returns

        order = {"NOT FIT FOR USE": 0, "FIT WITH NOTES": 1, "FIT": 2, "NO DATA": 3}
        ranks = [order.get(row.fitness_verdict or "", 4) for row in rows]
        assert ranks == sorted(ranks)
