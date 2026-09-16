"""The rule engine and the seven-dimension DQA."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.enums import DQADimension, Severity, SubmissionStatus
from app.models import IndicatorValue, Submission
from app.services import reference
from app.services.validation import RULE_REGISTRY, run_validation


def _submission(db, *, state="KN", period="2026-Q1", uploaded_at=None, **kwargs) -> Submission:
    state_row = reference.get_state_by_code(db, state)
    period_row = reference.get_period_by_code(db, period)
    submission = Submission(
        state_id=state_row.id,
        period_id=period_row.id,
        version=1,
        status=str(SubmissionStatus.UPLOADED),
        is_current=True,
        uploaded_at=uploaded_at or datetime.now(timezone.utc),
        row_count=kwargs.pop("row_count", 4),
        mapped_count=kwargs.pop("mapped_count", 4),
        **kwargs,
    )
    db.add(submission)
    db.flush()
    return submission


def _value(db, submission, code, **kwargs) -> IndicatorValue:
    indicator = reference.get_indicator_by_code(db, code)
    value = IndicatorValue(
        submission_id=submission.id,
        indicator_id=indicator.id,
        disaggregation=kwargs.pop("disaggregation", {}),
        **kwargs,
    )
    db.add(value)
    db.flush()
    return value


def _issues(summary, rule_code):
    return [issue for issue in summary.issues if issue.rule_code == rule_code]


def _score(summary, dimension):
    return next(d.score for d in summary.dimensions if d.dimension == str(dimension))


class TestRuleCatalogue:
    def test_every_dimension_is_covered(self):
        covered = {rule.dimension for rule in RULE_REGISTRY.values()}
        assert covered == set(DQADimension)

    def test_catalogue_is_mirrored_into_the_database(self, db):
        from app.models import ValidationRule

        stored = {row.code for row in db.query(ValidationRule).all()}
        assert stored == set(RULE_REGISTRY)


class TestValidity:
    def test_percentage_above_one_hundred_is_an_error(self, db):
        submission = _submission(db)
        _value(db, submission, "KPI-002", value=137.0, numerator=1370, denominator=1000)
        summary = run_validation(db, submission)

        issue = _issues(summary, "VAL-002")
        assert issue and issue[0].severity == Severity.ERROR
        assert issue[0].is_blocking
        assert summary.blocking

    def test_unparseable_value_is_an_error(self, db):
        submission = _submission(db)
        _value(db, submission, "KPI-001", value=None, raw_value="see attached")
        summary = run_validation(db, submission)

        assert _issues(summary, "VAL-001")
        assert summary.blocking

    def test_unknown_disaggregation_label_is_a_warning(self, db):
        submission = _submission(db)
        _value(db, submission, "KPI-001", value=100.0, disaggregation={"sex": "unspecified"})
        summary = run_validation(db, submission)

        issue = _issues(summary, "VAL-004")
        assert issue and issue[0].severity == Severity.WARNING

    def test_clean_values_score_full_validity(self, db):
        submission = _submission(db)
        _value(db, submission, "KPI-001", value=900.0, disaggregation={"sex": "total"})
        summary = run_validation(db, submission)
        assert _score(summary, DQADimension.VALIDITY) == 100.0


class TestIntegrity:
    def test_numerator_above_denominator_is_blocking(self, db):
        submission = _submission(db)
        _value(db, submission, "KPI-002", value=50.0, numerator=900, denominator=800)
        summary = run_validation(db, submission)

        assert _issues(summary, "INT-001")
        assert summary.blocking

    def test_percentage_inconsistent_with_its_components_is_a_warning(self, db):
        submission = _submission(db)
        _value(db, submission, "KPI-002", value=90.0, numerator=500, denominator=1000)
        summary = run_validation(db, submission)

        issue = _issues(summary, "INT-002")
        assert issue and issue[0].severity == Severity.WARNING
        assert "50.00%" in issue[0].message

    def test_unmapped_rows_are_flagged(self, db):
        submission = _submission(db, row_count=10, mapped_count=6, unmapped_count=4)
        _value(db, submission, "KPI-001", value=100.0)
        summary = run_validation(db, submission)

        assert _issues(summary, "INT-004")


class TestUniqueness:
    def test_duplicate_indicator_rows_are_blocking(self, db):
        submission = _submission(db)
        _value(db, submission, "KPI-001", value=100.0)
        _value(db, submission, "KPI-001", value=100.0)
        summary = run_validation(db, submission)

        issue = _issues(summary, "UNQ-001")
        assert issue and issue[0].is_blocking
        assert summary.blocking

    def test_same_indicator_with_different_disaggregation_is_allowed(self, db):
        submission = _submission(db)
        _value(db, submission, "KPI-001", value=60.0, disaggregation={"sex": "female"})
        _value(db, submission, "KPI-001", value=40.0, disaggregation={"sex": "male"})
        summary = run_validation(db, submission)

        assert not _issues(summary, "UNQ-001")


class TestTimeliness:
    def test_on_time_submission_scores_full_marks(self, db):
        period = reference.get_period_by_code(db, "2026-Q1")
        on_time = datetime.combine(period.due_date, datetime.min.time(), tzinfo=timezone.utc)
        submission = _submission(db, uploaded_at=on_time)
        _value(db, submission, "KPI-001", value=100.0)
        summary = run_validation(db, submission)

        assert _score(summary, DQADimension.TIMELINESS) == 100.0
        assert not _issues(summary, "TIM-001")

    def test_late_submission_loses_points_per_day(self, db):
        period = reference.get_period_by_code(db, "2026-Q1")
        late = datetime.combine(
            period.due_date + timedelta(days=10), datetime.min.time(), tzinfo=timezone.utc
        )
        submission = _submission(db, uploaded_at=late)
        _value(db, submission, "KPI-001", value=100.0)
        summary = run_validation(db, submission)

        assert _score(summary, DQADimension.TIMELINESS) == 70.0  # 100 - 3 points x 10 days
        issue = _issues(summary, "TIM-001")
        assert issue and "10 day(s)" in issue[0].message


class TestCompleteness:
    def test_score_is_the_share_of_expected_indicators_reported(self, db):
        submission = _submission(db)
        # The fixture sets targets for all four indicators, so all four are expected.
        _value(db, submission, "KPI-001", value=100.0)
        _value(db, submission, "KPI-004", value=50.0)
        summary = run_validation(db, submission)

        assert _score(summary, DQADimension.COMPLETENESS) == 50.0
        missing = {issue.indicator_code for issue in _issues(summary, "COM-001")}
        assert missing == {"KPI-002", "KPI-003"}

    def test_full_reporting_scores_full_completeness(self, db):
        submission = _submission(db)
        _value(db, submission, "KPI-001", value=100.0)
        _value(db, submission, "KPI-002", value=75.0, numerator=750, denominator=1000)
        _value(db, submission, "KPI-003", value=42.0)
        _value(db, submission, "KPI-004", value=50.0)
        summary = run_validation(db, submission)

        assert _score(summary, DQADimension.COMPLETENESS) == 100.0


class TestAccuracy:
    def test_achievement_far_above_target_is_flagged(self, db):
        submission = _submission(db)
        _value(db, submission, "KPI-001", value=50_000.0)  # target is 1,000
        summary = run_validation(db, submission)

        issue = _issues(summary, "ACC-001")
        assert issue and issue[0].severity == Severity.WARNING

    def test_numerator_without_denominator_is_blocking(self, db):
        submission = _submission(db)
        _value(db, submission, "KPI-002", value=None, numerator=500, denominator=0)
        summary = run_validation(db, submission)

        assert _issues(summary, "ACC-003")
        assert summary.blocking


class TestGating:
    def test_blocking_error_rejects_the_submission(self, db):
        submission = _submission(db)
        _value(db, submission, "KPI-001", value=100.0)
        _value(db, submission, "KPI-001", value=100.0)  # duplicate
        run_validation(db, submission)

        assert submission.status == SubmissionStatus.REJECTED
        assert "blocking validation error" in submission.rejection_reason

    def test_clean_submission_is_validated(self, db):
        submission = _submission(db)
        _value(db, submission, "KPI-001", value=900.0, disaggregation={"sex": "total"})
        _value(db, submission, "KPI-002", value=75.0, numerator=750, denominator=1000)
        _value(db, submission, "KPI-003", value=42.0)
        _value(db, submission, "KPI-004", value=850.0)
        run_validation(db, submission)

        assert submission.status == SubmissionStatus.VALIDATED
        assert submission.rejection_reason is None
        assert submission.dqa_score > 90

    def test_weak_dimension_caps_the_headline_grade(self, db):
        """A high weighted mean must not hide one badly failing dimension."""
        submission = _submission(db)
        _value(db, submission, "KPI-001", value=900.0)  # 1 of 4 expected indicators
        summary = run_validation(db, submission)

        assert _score(summary, DQADimension.COMPLETENESS) < 60
        assert summary.overall_score > 80
        assert summary.grade in {"Fair", "Weak", "Poor"}

    def test_scores_are_persisted_for_every_dimension(self, db):
        submission = _submission(db)
        _value(db, submission, "KPI-001", value=100.0)
        run_validation(db, submission)

        from app.models import DQAScore

        stored = db.query(DQAScore).filter_by(submission_id=submission.id).all()
        assert {row.dimension for row in stored} == {str(d) for d in DQADimension}
