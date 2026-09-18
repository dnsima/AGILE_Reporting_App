"""Closing a reporting cycle, and what it takes to write into a closed one.

Closing is not a freeze. A correction still lands, because the whole change
process is built on corrections landing -- with evidence, and with approval.
What closing stops is a state uploading a new file over figures that have
already been published, which is the silent overwrite everything else exists to
prevent.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.core.enums import QueryStatus
from app.core.errors import ConflictError, ValidationError
from app.models import AuditLog, DataQuery, User
from app.schemas.ingestion import ManualSubmission
from app.services import periods, reference
from app.services import queries as query_service
from app.services.ingestion import ingest_manual


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _submit(db, state="KN", period="2026-Q1", value=100.0, **kwargs):
    payload = ManualSubmission(
        state_code=state,
        period_code=period,
        values=[{"indicator_code": "KPI-001", "value": value}],
        **kwargs,
    )
    submission, _diagnostics, _summary = ingest_manual(db, payload, raise_queries=False)
    db.flush()
    return submission


def _close(db, code="2026-Q1", **kwargs):
    period = reference.get_period_by_code(db, code)
    return periods.close_period(db, period, **kwargs)


@pytest.fixture()
def closed(db):
    """Q1 reported, then closed -- the state the NPCU is in after consolidating."""
    submission = _submit(db)
    period = _close(db, note="Q1 2026 quarterly report published to the Bank.")
    db.flush()
    return {"period": period, "submission": submission}


# --------------------------------------------------------------------------
# Closing
# --------------------------------------------------------------------------
class TestClosing:
    def test_closing_records_who_and_why(self, db):
        user = db.query(User).first()
        period = _close(db, actor=user, note="Report published.")
        assert period.is_open is False
        assert period.locked_at is not None
        assert period.locked_by_id == user.id
        assert period.lock_note == "Report published."

        entry = (
            db.query(AuditLog)
            .filter_by(action="period.close", entity_id=period.id)
            .one()
        )
        assert "Report published." in entry.summary

    def test_closing_twice_is_refused(self, db, closed):
        with pytest.raises(ConflictError, match="already closed"):
            _close(db)

    def test_a_closed_period_takes_no_new_return(self, db, closed):
        with pytest.raises(ConflictError) as error:
            _submit(db, value=999.0)
        message = str(error.value)
        assert "2026-Q1 is closed" in message
        # The refusal has to say what to do instead, or it is just a wall.
        assert "respond to its query with evidence" in message
        assert "ask the NPCU to grant a reopening" in message

    def test_other_periods_are_unaffected(self, db, closed):
        assert _submit(db, period="2025-Q4").id is not None

    def test_other_states_are_unaffected_by_one_states_reopening(self, db, closed):
        kaduna = reference.get_state_by_code(db, "KD")
        periods.grant_reopening(
            db, closed["period"], kaduna, reason="Wrong file uploaded."
        )
        db.flush()
        # Kaduna may file; Kano still may not.
        assert _submit(db, state="KD", value=5.0).id is not None
        with pytest.raises(ConflictError):
            _submit(db, state="KN", value=5.0)


# --------------------------------------------------------------------------
# Corrections still land
# --------------------------------------------------------------------------
class TestCorrectionsOnAClosedPeriod:
    def test_an_accepted_correction_changes_a_closed_periods_figure(self, db, closed):
        """Closing must not break the route the change process depends on."""
        value = closed["submission"].values[0]
        query = DataQuery(
            submission_id=closed["submission"].id,
            state_id=closed["submission"].state_id,
            period_id=closed["period"].id,
            indicator_id=value.indicator_id,
            rule_code="CON-002",
            title="KPI-001 is cumulative but fell.",
            reported_value=value.value,
            status=str(QueryStatus.OPEN),
            due_date=date.today() + timedelta(days=14),
        )
        db.add(query)
        db.flush()

        query_service.respond(
            db, query, narrative="Evidence supports 80.", proposed_value=80.0
        )
        query_service.accept(db, query, note="Certificates checked.")
        db.flush()

        db.refresh(value)
        assert value.value == 80.0
        assert value.original_value == 100.0
        assert closed["period"].is_open is False  # still closed throughout


# --------------------------------------------------------------------------
# Reopenings
# --------------------------------------------------------------------------
class TestReopenings:
    def test_a_grant_admits_exactly_one_return(self, db, closed):
        state = reference.get_state_by_code(db, "KN")
        grant = periods.grant_reopening(
            db, closed["period"], state, reason="The June file was uploaded by mistake."
        )
        db.flush()
        assert grant.status == "ACTIVE"

        submission = _submit(db, value=222.0)
        db.flush()
        assert grant.status == "USED"
        assert grant.consumed_submission_id == submission.id

        with pytest.raises(ConflictError) as error:
            _submit(db, value=333.0)
        assert "was used" in str(error.value).lower() or "used" in str(error.value).lower()

    def test_an_expired_grant_does_not_admit_a_return(self, db, closed):
        state = reference.get_state_by_code(db, "KN")
        grant = periods.grant_reopening(db, closed["period"], state, reason="Late return.")
        grant.expires_on = date.today() - timedelta(days=1)
        db.flush()

        assert grant.status == "EXPIRED"
        with pytest.raises(ConflictError):
            _submit(db, value=1.0)

    def test_a_withdrawn_grant_does_not_admit_a_return(self, db, closed):
        state = reference.get_state_by_code(db, "KN")
        grant = periods.grant_reopening(db, closed["period"], state, reason="Late return.")
        periods.revoke_reopening(db, grant, reason="Resolved through a correction instead.")
        db.flush()

        assert grant.status == "REVOKED"
        with pytest.raises(ConflictError):
            _submit(db, value=1.0)

    def test_a_used_grant_cannot_be_withdrawn(self, db, closed):
        state = reference.get_state_by_code(db, "KN")
        grant = periods.grant_reopening(db, closed["period"], state, reason="Wrong file.")
        _submit(db, value=1.0)
        db.flush()
        with pytest.raises(ConflictError, match="already been used"):
            periods.revoke_reopening(db, grant, reason="Changed my mind.")

    def test_two_active_grants_for_one_state_are_refused(self, db, closed):
        state = reference.get_state_by_code(db, "KN")
        periods.grant_reopening(db, closed["period"], state, reason="Wrong file.")
        db.flush()
        with pytest.raises(ConflictError, match="already holds an unused reopening"):
            periods.grant_reopening(db, closed["period"], state, reason="Again.")

    def test_a_grant_needs_a_reason(self, db, closed):
        state = reference.get_state_by_code(db, "KN")
        with pytest.raises(ValidationError):
            periods.grant_reopening(db, closed["period"], state, reason="   ")

    def test_an_open_period_needs_no_grant(self, db):
        state = reference.get_state_by_code(db, "KN")
        period = reference.get_period_by_code(db, "2026-Q1")
        with pytest.raises(ConflictError, match="is open"):
            periods.grant_reopening(db, period, state, reason="Unnecessary.")

    def test_granting_is_recorded_against_the_state(self, db, closed):
        state = reference.get_state_by_code(db, "KN")
        grant = periods.grant_reopening(
            db, closed["period"], state, reason="The June file was uploaded by mistake."
        )
        db.flush()
        entry = (
            db.query(AuditLog).filter_by(action="period.reopening_grant").one()
        )
        assert entry.state_id == state.id
        assert "uploaded by mistake" in entry.summary
        assert str(grant.expires_on) in entry.summary


# --------------------------------------------------------------------------
# Reopening the whole period
# --------------------------------------------------------------------------
class TestReopeningThePeriod:
    def test_reopening_admits_every_state_again(self, db, closed):
        periods.reopen_period(db, closed["period"], reason="Closed a cycle early in error.")
        db.flush()
        assert closed["period"].is_open is True
        assert closed["period"].locked_at is None
        assert _submit(db, value=7.0).id is not None

    def test_reopening_needs_a_reason(self, db, closed):
        with pytest.raises(ValidationError):
            periods.reopen_period(db, closed["period"], reason="  ")

    def test_reopening_an_open_period_is_refused(self, db):
        period = reference.get_period_by_code(db, "2026-Q1")
        with pytest.raises(ConflictError, match="already open"):
            periods.reopen_period(db, period, reason="Nothing to reopen.")


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------
class TestEndpoints:
    def _close(self, client, headers, code="2026-Q1", note="Report published."):
        response = client.post(
            f"/api/v1/reference/periods/{code}/close", headers=headers, json={"note": note}
        )
        assert response.status_code == 200, response.text
        return response.json()

    def test_closing_through_the_api_shows_the_lock(self, client, npcu_headers):
        body = self._close(client, npcu_headers)
        assert body["is_open"] is False
        assert body["locked_at"] is not None
        assert body["lock_note"] == "Report published."
        assert body["reopened_for"] == []

    def test_a_state_cannot_close_a_period(self, client, state_headers):
        response = client.post(
            "/api/v1/reference/periods/2026-Q1/close",
            headers=state_headers,
            json={"note": "Not my call."},
        )
        assert response.status_code == 403

    def test_a_closed_period_refuses_an_upload_with_a_useful_message(
        self, client, npcu_headers, state_headers
    ):
        self._close(client, npcu_headers)
        response = client.post(
            "/api/v1/ingestion/submissions",
            headers=state_headers,
            json={
                "state_code": "KN",
                "period_code": "2026-Q1",
                "values": [{"indicator_code": "KPI-001", "value": 10}],
            },
        )
        assert response.status_code == 409
        message = response.json()["error"]["message"]
        assert "2026-Q1 is closed" in message
        assert "grant a reopening" in message

    def test_granting_a_reopening_lets_that_state_back_in(
        self, client, npcu_headers, state_headers
    ):
        self._close(client, npcu_headers)
        granted = client.post(
            "/api/v1/reference/periods/2026-Q1/reopenings",
            headers=npcu_headers,
            json={"state_code": "KN", "reason": "The June file was uploaded by mistake."},
        )
        assert granted.status_code == 201, granted.text
        grant = granted.json()
        assert grant["status"] == "ACTIVE"
        assert grant["state_code"] == "KN"
        assert grant["expires_on"] is not None

        accepted = client.post(
            "/api/v1/ingestion/submissions",
            headers=state_headers,
            json={
                "state_code": "KN",
                "period_code": "2026-Q1",
                "values": [{"indicator_code": "KPI-001", "value": 10}],
            },
        )
        assert accepted.status_code == 201, accepted.text

        # Used, and the period lists nobody as reopened any more.
        listed = client.get(
            "/api/v1/reference/periods/2026-Q1/reopenings", headers=npcu_headers
        ).json()
        assert [row["status"] for row in listed] == ["USED"]
        period = client.get(
            "/api/v1/reference/periods?fiscal_year=2026", headers=npcu_headers
        ).json()
        assert next(p for p in period if p["code"] == "2026-Q1")["reopened_for"] == []

    def test_an_active_grant_is_visible_on_the_period(self, client, npcu_headers):
        self._close(client, npcu_headers)
        client.post(
            "/api/v1/reference/periods/2026-Q1/reopenings",
            headers=npcu_headers,
            json={"state_code": "KD", "reason": "Return never filed."},
        )
        periods_list = client.get(
            "/api/v1/reference/periods?fiscal_year=2026", headers=npcu_headers
        ).json()
        assert next(p for p in periods_list if p["code"] == "2026-Q1")["reopened_for"] == ["KD"]

    def test_a_state_sees_only_its_own_reopenings(self, client, npcu_headers, state_headers):
        self._close(client, npcu_headers)
        for code in ("KN", "KD"):
            client.post(
                "/api/v1/reference/periods/2026-Q1/reopenings",
                headers=npcu_headers,
                json={"state_code": code, "reason": "Re-file."},
            )
        mine = client.get(
            "/api/v1/reference/periods/2026-Q1/reopenings", headers=state_headers
        ).json()
        assert {row["state_code"] for row in mine} == {"KN"}

    def test_a_state_cannot_grant_itself_a_reopening(self, client, npcu_headers, state_headers):
        self._close(client, npcu_headers)
        response = client.post(
            "/api/v1/reference/periods/2026-Q1/reopenings",
            headers=state_headers,
            json={"state_code": "KN", "reason": "Letting myself back in."},
        )
        assert response.status_code == 403

    def test_revoking_through_the_api(self, client, npcu_headers, state_headers):
        self._close(client, npcu_headers)
        grant = client.post(
            "/api/v1/reference/periods/2026-Q1/reopenings",
            headers=npcu_headers,
            json={"state_code": "KN", "reason": "Re-file."},
        ).json()

        revoked = client.post(
            f"/api/v1/reference/reopenings/{grant['id']}/revoke",
            headers=npcu_headers,
            json={"reason": "Settled through a correction instead."},
        )
        assert revoked.status_code == 200, revoked.text
        assert revoked.json()["status"] == "REVOKED"

        blocked = client.post(
            "/api/v1/ingestion/submissions",
            headers=state_headers,
            json={
                "state_code": "KN",
                "period_code": "2026-Q1",
                "values": [{"indicator_code": "KPI-001", "value": 10}],
            },
        )
        assert blocked.status_code == 409

    def test_reopening_the_whole_period_through_the_api(self, client, npcu_headers):
        self._close(client, npcu_headers)
        response = client.post(
            "/api/v1/reference/periods/2026-Q1/reopen",
            headers=npcu_headers,
            json={"reason": "Cycle closed a week early in error."},
        )
        assert response.status_code == 200, response.text
        assert response.json()["is_open"] is True
        assert response.json()["locked_at"] is None


class TestReport:
    def test_the_report_says_whether_the_cycle_is_closed(self, client, npcu_headers, db):
        from app.schemas.reporting import ReportRequest
        from app.services.reporting import build_report

        document, _i, _p = build_report(
            db, ReportRequest(period_code="2026-Q1", formats=["markdown"])
        )
        assert document.meta["Cycle"].startswith("Open")

        _close(db)
        db.flush()
        document, _i, _p = build_report(
            db, ReportRequest(period_code="2026-Q1", formats=["markdown"])
        )
        assert document.meta["Cycle"].startswith("Closed")
        assert "only through an accepted correction" in document.meta["Cycle"]
