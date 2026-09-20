"""The data query workflow: raise, respond, review, restate."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from itertools import count

import pytest

from app.core.enums import DisclosureStatus, QueryResolution, QueryStatus, SubmissionStatus
from app.core.errors import ConflictError, ValidationError
from app.models import Indicator, IndicatorValue, Submission, ValueRevision
from app.services import analytics, queries, reference
from app.services.validation import run_validation

_numbers = count(800)


def _indicator(db, code, **kwargs):
    indicator = Indicator(
        code=code,
        number=next(_numbers),
        name=kwargs.pop("name", code),
        unit=kwargs.pop("unit", "NUMBER"),
        aggregation_method="SUM",
        direction="INCREASE",
        **kwargs,
    )
    db.add(indicator)
    db.flush()
    return indicator


def _submission(db, state="KN", period="2026-Q1", approved=True):
    state_row = reference.get_state_by_code(db, state)
    period_row = reference.get_period_by_code(db, period)
    previous = (
        db.query(Submission)
        .filter_by(state_id=state_row.id, period_id=period_row.id)
        .order_by(Submission.version.desc())
        .first()
    )
    if previous is not None:
        previous.is_current = False
    submission = Submission(
        state_id=state_row.id,
        period_id=period_row.id,
        version=(previous.version + 1) if previous else 1,
        status=str(SubmissionStatus.APPROVED if approved else SubmissionStatus.VALIDATED),
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
        disaggregation={},
    )
    db.add(row)
    db.flush()
    return row


def _cumulative_fall(db):
    """Kebbi's real case: a cumulative figure that falls between quarters."""
    indicator = _indicator(db, "C1.0-02", is_cumulative=True)
    earlier = _submission(db, period="2025-Q4")
    _value(db, earlier, indicator, 1577.0)
    later = _submission(db, period="2026-Q1")
    value = _value(db, later, indicator, 342.0)
    summary = run_validation(db, later)
    raised = queries.raise_queries(db, later)
    return indicator, earlier, later, value, summary, raised


class TestRaising:
    def test_a_finding_becomes_a_query_owned_by_the_state(self, db):
        indicator, _earlier, later, _value, _summary, raised = _cumulative_fall(db)
        query = next(q for q in raised if q.rule_code == "CON-002")

        assert query.state_id == later.state_id
        assert query.indicator_id == indicator.id
        assert query.status == QueryStatus.OPEN
        assert query.reported_value == 342.0
        assert "fell from 1,577 to 342" in query.title
        assert query.due_date > date.today()

    def test_validation_no_longer_rejects_the_submission(self, db):
        """A flagged figure is queried, not a reason to refuse the whole return."""
        _indicator_, _earlier, later, _value, _summary, _raised = _cumulative_fall(db)
        assert later.status != SubmissionStatus.REJECTED
        assert later.rejection_reason is None

    def test_an_unusable_figure_is_flagged_not_rejected(self, db):
        _indicator_, _earlier, _later, value, _summary, _raised = _cumulative_fall(db)
        assert value.is_valid is False
        assert value.disclosure_status == str(DisclosureStatus.UNFIT)
        assert "CON-002" in value.quarantine_reason

    def test_a_flagged_figure_still_counts_towards_the_total(self, db):
        """Disclose the doubt; never restate the national result to hide it.

        Dropping the figure here made the platform's own totals disagree with
        the NPCU's published ones -- on Q2 2026, life-skills completion read
        82,820 against the 373,529 states actually reported.
        """
        indicator, _earlier, _later, value, _summary, _raised = _cumulative_fall(db)
        period = reference.get_period_by_code(db, "2026-Q1")

        analytics.clear_analysis_cache(db)
        assert analytics.analyse_indicator(db, indicator, period).national.value == 342.0
        assert value.is_valid is False  # counted, and still marked unfit

    def test_counts_are_recorded_on_the_submission(self, db):
        _i, _e, later, _v, _s, raised = _cumulative_fall(db)
        assert later.open_query_count == len(raised)
        assert later.quarantined_count == 1

    def test_revalidating_does_not_duplicate_queries(self, db):
        _i, _e, later, _v, _s, raised = _cumulative_fall(db)
        run_validation(db, later)
        again = queries.raise_queries(db, later)
        assert again == []
        assert later.open_query_count == len(raised)

    def test_info_findings_do_not_raise_queries(self, db):
        indicator = _indicator(db, "C9.9-01")
        submission = _submission(db)
        _value(db, submission, indicator, 10.5)  # whole-number INFO finding
        run_validation(db, submission)
        raised = queries.raise_queries(db, submission)
        assert all(q.severity != "INFO" for q in raised)


class TestResponding:
    def test_confirming_the_figure_as_reported(self, db):
        _i, _e, later, value, _s, raised = _cumulative_fall(db)
        query = next(q for q in raised if q.rule_code == "CON-002")

        queries.respond(db, query, narrative="Figure verified against the works register.")
        assert query.status == QueryStatus.RESPONDED

        queries.accept(db, query, note="Evidence accepted.")
        assert query.status == QueryStatus.ACCEPTED
        assert query.resolution == QueryResolution.CONFIRMED
        assert value.value == 342.0
        assert value.is_restated is False
        assert value.is_valid is True  # released back into the aggregation

    def test_a_response_requires_an_explanation(self, db):
        _i, _e, _l, _v, _s, raised = _cumulative_fall(db)
        with pytest.raises(ValidationError, match="evidence"):
            queries.respond(db, raised[0], narrative="   ")

    def test_a_settled_query_takes_no_further_response(self, db):
        _i, _e, _l, _v, _s, raised = _cumulative_fall(db)
        query = raised[0]
        queries.respond(db, query, narrative="Checked.")
        queries.accept(db, query)
        with pytest.raises(ConflictError, match="no longer accepts"):
            queries.respond(db, query, narrative="Again.")

    def test_rejection_returns_the_query_to_the_state(self, db):
        _i, _e, _l, _v, _s, raised = _cumulative_fall(db)
        query = raised[0]
        queries.respond(db, query, narrative="No evidence attached.")
        queries.reject(db, query, reason="Please attach the handover certificates.")

        assert query.status == QueryStatus.REJECTED
        assert query.is_open
        assert "handover certificates" in query.resolution_note


class TestRestatement:
    def test_an_accepted_correction_changes_the_figure(self, db):
        _i, _e, _l, value, _s, raised = _cumulative_fall(db)
        query = next(q for q in raised if q.rule_code == "CON-002")

        queries.respond(db, query, narrative="Overstated.", proposed_value=300.0)
        queries.accept(db, query, note="Accepted.")

        assert value.value == 300.0
        assert query.resolution == QueryResolution.RESTATED

    def test_the_original_figure_survives_the_restatement(self, db):
        """A published report and the live dashboard must stay reconcilable."""
        _i, _e, _l, value, _s, raised = _cumulative_fall(db)
        query = next(q for q in raised if q.rule_code == "CON-002")

        queries.respond(db, query, narrative="Overstated.", proposed_value=300.0)
        queries.accept(db, query)

        assert value.value == 300.0
        assert value.original_value == 342.0
        assert value.is_restated is True

    def test_a_correction_may_restate_an_earlier_period(self, db):
        """The usual fix for an apparent fall is to correct the earlier quarter."""
        indicator, earlier, _later, _value, _s, raised = _cumulative_fall(db)
        query = next(q for q in raised if q.rule_code == "CON-002")

        queries.respond(
            db, query,
            narrative="The Q4 figure was overstated; evidence supports 342.",
            proposed_value=342.0,
            restates_period_code="2025-Q4",
        )
        queries.accept(db, query, note="Accepted at the validation meeting.")

        restated = db.query(IndicatorValue).filter_by(
            submission_id=earlier.id, indicator_id=indicator.id
        ).one()
        assert restated.value == 342.0
        assert restated.original_value == 1577.0

    def test_the_restatement_is_fully_attributed(self, db):
        _i, _e, _l, _v, _s, raised = _cumulative_fall(db)
        query = next(q for q in raised if q.rule_code == "CON-002")
        queries.respond(db, query, narrative="Recount.", proposed_value=300.0)
        queries.accept(db, query, note="Approved by NPCU M&E.")

        revision = db.query(ValueRevision).filter_by(query_id=query.id).one()
        assert revision.previous_value == 342.0
        assert revision.new_value == 300.0
        assert revision.approved_at is not None
        assert "Approved by NPCU" in revision.reason

    def test_restating_clears_the_original_finding(self, db):
        indicator, earlier, later, _v, _s, raised = _cumulative_fall(db)
        query = next(q for q in raised if q.rule_code == "CON-002")
        queries.respond(
            db, query, narrative="Q4 overstated.", proposed_value=342.0,
            restates_period_code="2025-Q4",
        )
        queries.accept(db, query)

        summary = run_validation(db, later)
        assert not [i for i in summary.issues if i.rule_code == "CON-002"]


class TestQuarantineRelease:
    def test_a_figure_held_by_two_queries_waits_for_both(self, db):
        total = _indicator(db, "C1.0-01", composite_of=["C1.1-01", "C1.2-01"], is_cumulative=True)
        part_a = _indicator(db, "C1.1-01")
        part_b = _indicator(db, "C1.2-01")

        earlier = _submission(db, period="2025-Q4")
        _value(db, earlier, total, 900.0)

        later = _submission(db, period="2026-Q1")
        value = _value(db, later, total, 500.0)   # falls, and does not equal its parts
        _value(db, later, part_a, 100.0)
        _value(db, later, part_b, 100.0)

        run_validation(db, later)
        raised = queries.raise_queries(db, later)
        holding = [q for q in raised if q.indicator_id == total.id and q.rule_code in {"CON-002", "INT-005"}]
        assert len(holding) == 2
        assert value.is_valid is False

        queries.respond(db, holding[0], narrative="Explained.")
        queries.accept(db, holding[0])
        assert value.is_valid is False, "released while another query is still open"

        queries.respond(db, holding[1], narrative="Explained.")
        queries.accept(db, holding[1])
        assert value.is_valid is True


class TestOverdueAndViews:
    def test_an_unanswered_query_becomes_overdue(self, db):
        _i, _e, _l, _v, _s, raised = _cumulative_fall(db)
        query = raised[0]
        query.due_date = date.today() - timedelta(days=5)
        db.flush()

        assert query.is_overdue()
        assert query.days_overdue() == 5
        assert query in queries.open_queries(db, overdue_only=True)

    def test_a_settled_query_is_never_overdue(self, db):
        _i, _e, _l, _v, _s, raised = _cumulative_fall(db)
        query = raised[0]
        query.due_date = date.today() - timedelta(days=5)
        queries.respond(db, query, narrative="Done.")
        queries.accept(db, query)
        assert not query.is_overdue()

    def test_escalation_puts_the_figure_on_the_verification_worklist(self, db):
        _i, _e, _l, _v, _s, raised = _cumulative_fall(db)
        query = raised[0]
        queries.escalate_to_verification(
            db, query, reason="To be checked during the next supportive supervision visit."
        )
        assert query.status == QueryStatus.VERIFICATION
        assert query.is_open
        assert query in queries.verification_worklist(db)

    def test_summary_counts_for_the_dashboard(self, db):
        _i, _e, later, _v, _s, raised = _cumulative_fall(db)
        queries.respond(db, raised[0], narrative="Explained.")

        summary = queries.query_summary(db, later.period_id)
        assert summary["total"] == len(raised)
        assert summary["open"] == len(raised)
        assert summary["awaiting_review"] == 1
        assert summary["states_with_open_queries"] == 1
