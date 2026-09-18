"""Participating in the programme, and being expected to file, are two facts.

They were one column. A Limited Financing state is named in the results
framework, is issued a template, has an applicability matrix — and has not
started reporting. Marking that as "inactive" let it file a return the platform
accepted and analysed while leaving it out of every national denominator it
should have been counted in.
"""

from __future__ import annotations

import pytest

from app.models import AuditLog, State
from app.schemas.ingestion import ManualSubmission
from app.services import reference
from app.services.ingestion import ingest_manual


@pytest.fixture()
def not_yet_reporting(db):
    """Lagos is in the programme but has not started filing."""
    state = reference.get_state_by_code(db, "LA")
    state.is_reporting = False
    db.flush()
    return state


class TestTheTwoFacts:
    def test_a_state_not_yet_reporting_is_still_participating(self, db, not_yet_reporting):
        assert not_yet_reporting.is_active is True
        codes = {state.code for state in reference.participating_states(db)}
        assert "LA" in codes

    def test_it_is_not_counted_as_a_non_reporter(self, db, not_yet_reporting):
        """It was never asked, so it must not depress the reporting rate."""
        codes = {state.code for state in reference.active_states(db)}
        assert "LA" not in codes
        assert len(reference.active_states(db)) == len(reference.participating_states(db)) - 1

    def test_an_inactive_state_is_in_neither_set(self, db, not_yet_reporting):
        not_yet_reporting.is_active = False
        db.flush()
        assert "LA" not in {s.code for s in reference.participating_states(db)}
        assert "LA" not in {s.code for s in reference.active_states(db)}

    def test_the_cohort_filter_still_applies(self, db, not_yet_reporting):
        assert [s.code for s in reference.participating_states(db, "LIMITED")] == ["LA"]
        assert reference.active_states(db, "LIMITED") == []


class TestFilingStartsReporting:
    def test_filing_a_return_makes_a_state_a_reporting_state(self, db, not_yet_reporting):
        submission, _diagnostics, _summary = ingest_manual(
            db,
            ManualSubmission(
                state_code="LA",
                period_code="2026-Q1",
                values=[{"indicator_code": "KPI-001", "value": 42}],
            ),
            raise_queries=False,
        )
        db.flush()

        assert submission.id is not None
        assert db.get(State, not_yet_reporting.id).is_reporting is True
        assert "LA" in {state.code for state in reference.active_states(db)}

    def test_it_is_recorded_rather_than_happening_quietly(self, db, not_yet_reporting):
        ingest_manual(
            db,
            ManualSubmission(
                state_code="LA",
                period_code="2026-Q1",
                values=[{"indicator_code": "KPI-001", "value": 42}],
            ),
            raise_queries=False,
        )
        db.flush()
        entry = db.query(AuditLog).filter_by(action="state.reporting_started").one()
        assert entry.state_id == not_yet_reporting.id
        assert "filed its first return" in entry.summary

    def test_a_state_already_reporting_is_not_re_recorded(self, db):
        ingest_manual(
            db,
            ManualSubmission(
                state_code="KN",
                period_code="2026-Q1",
                values=[{"indicator_code": "KPI-001", "value": 42}],
            ),
            raise_queries=False,
        )
        db.flush()
        assert db.query(AuditLog).filter_by(action="state.reporting_started").count() == 0


class TestTheReportSaysWhatItCovers:
    def test_the_scope_claims_the_programme_not_the_reporters(self, db, not_yet_reporting):
        from app.schemas.reporting import ReportRequest
        from app.services.reporting import build_report

        document, _indicators, _period = build_report(
            db, ReportRequest(period_code="2026-Q1", formats=["markdown"])
        )
        # Four states are in the programme; three are expected to report.
        assert document.meta["Scope"] == "National (4 AGILE states)"

    def test_a_derived_indicator_is_not_counted_as_a_reported_one(self, db):
        from app.models import Indicator
        from app.schemas.reporting import ReportRequest
        from app.services.reporting import build_report

        db.query(Indicator).filter_by(code="KPI-004").update({"is_reported": False})
        db.flush()

        document, _indicators, _period = build_report(
            db, ReportRequest(period_code="2026-Q1", formats=["markdown"])
        )
        assert document.meta["Indicators covered"] == "3 reported + 1 derived"

    def test_a_catalogue_with_nothing_derived_just_counts(self, db):
        from app.schemas.reporting import ReportRequest
        from app.services.reporting import build_report

        document, _indicators, _period = build_report(
            db, ReportRequest(period_code="2026-Q1", formats=["markdown"])
        )
        assert document.meta["Indicators covered"] == "4"
