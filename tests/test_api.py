"""End-to-end API behaviour: auth, RBAC, ingestion, analysis and reporting."""

from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

TEMPLATE_HEADERS = [
    "KPI Number", "Indicator Code", "Indicator Name", "Unit", "Value",
    "Numerator", "Denominator", "Sex", "School Level", "Data Source", "Comments",
]

CLEAN_ROWS = [
    [1, "KPI-001", "Number of girls benefiting from the project", "NUMBER", 900, None, None, "total", "total", "EMIS", ""],
    [2, "KPI-002", "Transition rate", "PERCENT", 75.0, 750, 1000, "total", "total", "EMIS", ""],
    [3, "KPI-003", "Pupil-classroom ratio", "RATIO", 45.0, None, None, "total", "total", "EMIS", ""],
    [4, "KPI-004", "Teachers recruited", "NUMBER", 850, None, None, "total", "total", "EMIS", ""],
]


def _template(rows, state="Kano", period="2026-Q1") -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["AGILE Standardised State Reporting Template"])
    sheet.append(["State:", state])
    sheet.append(["Reporting Period:", period])
    sheet.append([])
    sheet.append(TEMPLATE_HEADERS)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _upload(client, headers, rows=None, content=None, **form):
    """Upload a template. Pass ``content`` to send exactly the same bytes twice.

    openpyxl stamps a creation time into the workbook, so rebuilding the same
    rows produces different bytes and a different hash.
    """
    payload = {
        "state_code": "KN",
        "period_code": "2026-Q1",
        "auto_approve": "true",
        "allow_duplicate": "true",
    }
    payload.update({key: str(value) for key, value in form.items()})
    return client.post(
        "/api/v1/ingestion/upload",
        headers=headers,
        files={
            "file": (
                "upload.xlsx",
                content if content is not None else _template(rows or CLEAN_ROWS),
                "application/vnd.ms-excel",
            )
        },
        data=payload,
    )


# --------------------------------------------------------------------------
# Health and authentication
# --------------------------------------------------------------------------
class TestHealth:
    def test_health_is_public(self, client):
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] in {"ok", "degraded"}
        assert body["checks"]["indicators"] == 4


class TestAuthentication:
    def test_login_returns_a_token_and_the_user(self, client):
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@test.gov.ng", "password": "TestPass123!"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "bearer"
        assert body["user"]["role"] == "ADMIN"
        assert "data:upload" in body["user"]["permissions"]

    def test_login_sets_the_dashboard_session_cookie(self, client):
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "admin@test.gov.ng", "password": "TestPass123!"},
        )
        assert "agile_session" in response.cookies

    @pytest.mark.parametrize(
        "payload",
        [
            {"email": "admin@test.gov.ng", "password": "wrong"},
            {"email": "nobody@test.gov.ng", "password": "TestPass123!"},
        ],
    )
    def test_bad_credentials_give_the_same_answer(self, client, payload):
        response = client.post("/api/v1/auth/login", json=payload)
        assert response.status_code == 401
        assert response.json()["error"]["message"] == "Incorrect email or password"

    def test_protected_endpoint_requires_a_token(self, client):
        response = client.get("/api/v1/reference/indicators")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "authentication_error"

    def test_errors_carry_a_request_id(self, client):
        response = client.get("/api/v1/reference/indicators")
        assert response.json()["request_id"]
        assert response.headers["X-Request-ID"]

    def test_me_describes_the_caller(self, client, state_headers):
        body = client.get("/api/v1/auth/me", headers=state_headers).json()
        assert body["role"] == "STATE_PIU"
        assert body["state_code"] == "KN"


# --------------------------------------------------------------------------
# Role-based access control
# --------------------------------------------------------------------------
class TestRoleBasedAccess:
    def test_viewer_cannot_upload(self, client, viewer_headers):
        assert _upload(client, viewer_headers).status_code == 403

    def test_viewer_can_read_analysis(self, client, viewer_headers):
        response = client.get(
            "/api/v1/analytics/scorecard", headers=viewer_headers, params={"period": "2026-Q1"}
        )
        assert response.status_code == 200

    def test_viewer_cannot_manage_users(self, client, viewer_headers):
        response = client.get("/api/v1/admin/users", headers=viewer_headers)
        assert response.status_code == 403
        assert "permission" in response.json()["error"]["message"].lower()

    def test_state_piu_cannot_upload_for_another_state(self, client, state_headers):
        response = _upload(client, state_headers, state_code="LA")
        assert response.status_code == 403
        assert "only access data for Kano" in response.json()["error"]["message"]

    def test_state_piu_can_upload_for_its_own_state(self, client, state_headers):
        assert _upload(client, state_headers, state_code="KN").status_code == 201

    def test_state_piu_only_sees_its_own_submissions(self, client, state_headers, npcu_headers):
        _upload(client, npcu_headers, state_code="LA")
        _upload(client, npcu_headers, state_code="KN")

        rows = client.get("/api/v1/ingestion/submissions", headers=state_headers).json()
        assert rows
        assert {row["state_code"] for row in rows} == {"KN"}

    def test_state_piu_upload_is_not_self_approved(self, client, state_headers):
        body = _upload(client, state_headers, auto_approve="true").json()
        assert body["submission"]["status"] == "VALIDATED"  # awaiting NPCU review

    def test_api_key_cannot_be_issued_with_admin_rights(self, client, admin_headers):
        response = client.post(
            "/api/v1/admin/api-keys",
            headers=admin_headers,
            json={"name": "External dashboard", "role": "ADMIN"},
        )
        assert response.status_code == 422


class TestApiKeys:
    def test_key_authenticates_read_only_access(self, client, admin_headers):
        created = client.post(
            "/api/v1/admin/api-keys",
            headers=admin_headers,
            json={"name": "External dashboard", "role": "VIEWER"},
        ).json()
        key_headers = {"X-API-Key": created["api_key"]}

        assert client.get("/api/v1/reference/indicators", headers=key_headers).status_code == 200
        assert _upload(client, key_headers).status_code == 403

    def test_revoked_key_is_rejected(self, client, admin_headers):
        created = client.post(
            "/api/v1/admin/api-keys",
            headers=admin_headers,
            json={"name": "Temporary", "role": "VIEWER"},
        ).json()
        client.delete(f"/api/v1/admin/api-keys/{created['id']}", headers=admin_headers)

        response = client.get(
            "/api/v1/reference/indicators", headers={"X-API-Key": created["api_key"]}
        )
        assert response.status_code == 401

    def test_unknown_key_is_rejected(self, client):
        response = client.get(
            "/api/v1/reference/indicators", headers={"X-API-Key": "agile_dead_beef"}
        )
        assert response.status_code == 401


# --------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------
class TestReferenceData:
    def test_states_carry_their_cohort(self, client, npcu_headers):
        rows = client.get("/api/v1/reference/states", headers=npcu_headers).json()
        kano = next(row for row in rows if row["code"] == "KN")
        assert kano["cohort_code"] == "ORIGINAL"

    def test_states_can_be_filtered_by_cohort(self, client, npcu_headers):
        rows = client.get(
            "/api/v1/reference/states", headers=npcu_headers, params={"cohort": "ORIGINAL"}
        ).json()
        assert {row["code"] for row in rows} == {"KN", "KD"}

    def test_template_download_is_an_xlsx_workbook(self, client, npcu_headers):
        response = client.get(
            "/api/v1/ingestion/template",
            headers=npcu_headers,
            params={"state": "KN", "period": "2026-Q1"},
        )
        assert response.status_code == 200
        assert response.content[:2] == b"PK"  # a zip container, i.e. xlsx
        assert "AGILE_reporting_template_KN_2026-Q1.xlsx" in response.headers["content-disposition"]

    def test_period_generation_creates_a_full_calendar(self, client, admin_headers):
        response = client.post(
            "/api/v1/reference/periods/generate",
            headers=admin_headers,
            json={"fiscal_year": 2027, "period_types": ["QUARTERLY", "ANNUAL"]},
        )
        assert response.status_code == 200
        codes = {row["code"] for row in response.json()}
        assert codes == {"2027-Q1", "2027-Q2", "2027-Q3", "2027-Q4", "2027-A"}

    def test_reference_edits_are_denied_to_state_users(self, client, state_headers):
        response = client.patch(
            "/api/v1/reference/states/LA", headers=state_headers, json={"cohort_code": "ORIGINAL"}
        )
        assert response.status_code == 403


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------
class TestIngestion:
    def test_clean_upload_is_accepted_and_analysed(self, client, npcu_headers):
        body = _upload(client, npcu_headers).json()

        assert body["accepted"] is True
        assert body["submission"]["status"] == "APPROVED"
        assert body["submission"]["mapped_count"] == 4
        assert body["validation"]["error_count"] == 0
        assert body["submission"]["dqa_score"] > 90

    def test_diagnostics_explain_the_mapping(self, client, npcu_headers):
        diagnostics = _upload(client, npcu_headers).json()["diagnostics"]

        assert diagnostics["detected_state"] == "KN"
        assert diagnostics["detected_period"] == "2026-Q1"
        mapped = {
            row["source_header"]: row["mapped_field"]
            for row in diagnostics["column_mappings"]
            if row["mapped_field"]
        }
        assert mapped["Value"] == "value"
        assert mapped["Indicator Code"] == "indicator_code"

    def test_state_and_period_are_detected_from_the_file(self, client, npcu_headers):
        response = client.post(
            "/api/v1/ingestion/upload",
            headers=npcu_headers,
            files={"file": ("kano.xlsx", _template(CLEAN_ROWS), "application/vnd.ms-excel")},
            data={"auto_approve": "true"},
        )
        assert response.status_code == 201
        assert response.json()["submission"]["state_code"] == "KN"

    def test_an_unusable_figure_is_quarantined_not_rejected(self, client, npcu_headers):
        """The bad figure leaves the analysis; the rest of the return still counts."""
        bad = [row[:] for row in CLEAN_ROWS]
        bad[1][4] = 137.0  # a transition rate above 100%

        body = _upload(client, npcu_headers, rows=bad).json()
        assert body["accepted"] is True
        assert body["submission"]["status"] == "APPROVED"
        assert body["submission"]["is_current"] is True
        assert body["submission"]["quarantined_count"] >= 1
        assert body["submission"]["open_query_count"] >= 1
        assert any(issue["rule_code"] == "VAL-002" for issue in body["validation"]["issues"])

        # The out-of-range figure is excluded from the national roll-up...
        analysis = client.get(
            "/api/v1/analytics/indicators/KPI-002",
            headers=npcu_headers,
            params={"period": "2026-Q1"},
        ).json()
        assert analysis["national"]["value"] is None

        # ...while the sound figures in the same file still report.
        analysis = client.get(
            "/api/v1/analytics/indicators/KPI-001",
            headers=npcu_headers,
            params={"period": "2026-Q1"},
        ).json()
        assert analysis["national"]["value"] == 900

    def test_the_same_file_uploaded_twice_is_refused(self, client, npcu_headers):
        content = _template(CLEAN_ROWS)
        assert _upload(
            client, npcu_headers, content=content, allow_duplicate="false"
        ).status_code == 201

        response = _upload(client, npcu_headers, content=content, allow_duplicate="false")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"

    def test_reupload_supersedes_the_previous_version(self, client, npcu_headers):
        first = _upload(client, npcu_headers).json()["submission"]
        revised = [row[:] for row in CLEAN_ROWS]
        revised[0][4] = 950
        second = _upload(client, npcu_headers, rows=revised).json()["submission"]

        assert second["version"] == first["version"] + 1
        rows = client.get(
            "/api/v1/ingestion/submissions",
            headers=npcu_headers,
            params={"state": "KN", "current_only": "true"},
        ).json()
        assert [row["id"] for row in rows] == [second["id"]]

    def test_unreadable_file_is_reported_clearly(self, client, npcu_headers):
        response = client.post(
            "/api/v1/ingestion/upload",
            headers=npcu_headers,
            files={"file": ("notes.docx", b"not a spreadsheet", "application/octet-stream")},
            data={"state_code": "KN", "period_code": "2026-Q1"},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "ingestion_error"

    def test_file_with_no_recognised_indicators_is_refused(self, client, npcu_headers):
        rows = [[99, "XXX-999", "Something else", "NUMBER", 5, None, None, "", "", "", ""]]
        response = _upload(client, npcu_headers, rows=rows)
        assert response.status_code == 422
        assert "could be matched" in response.json()["error"]["message"]

    def test_json_submission_from_a_state_system(self, client, npcu_headers):
        response = client.post(
            "/api/v1/ingestion/submissions",
            headers=npcu_headers,
            params={"auto_approve": "true"},
            json={
                "state_code": "GO",
                "period_code": "2026-Q1",
                "values": [
                    {"indicator_code": "KPI-001", "value": 700},
                    {"indicator_code": "KPI-002", "numerator": 600, "denominator": 1000},
                ],
            },
        )
        assert response.status_code == 201
        assert response.json()["submission"]["mapped_count"] == 2

    def test_approval_workflow(self, client, npcu_headers):
        submission = _upload(client, npcu_headers, auto_approve="false").json()["submission"]
        assert submission["status"] == "VALIDATED"

        approved = client.post(
            f"/api/v1/ingestion/submissions/{submission['id']}/approve",
            headers=npcu_headers,
            json={"reason": "Verified against source records"},
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "APPROVED"

    def test_a_manually_rejected_submission_cannot_be_approved(self, client, npcu_headers):
        """Validation no longer rejects, but a reviewer still can."""
        submission = _upload(client, npcu_headers, auto_approve="false").json()["submission"]
        client.post(
            f"/api/v1/ingestion/submissions/{submission['id']}/reject",
            headers=npcu_headers,
            json={"reason": "Wrong reporting period."},
        )

        response = client.post(
            f"/api/v1/ingestion/submissions/{submission['id']}/approve",
            headers=npcu_headers,
            json={},
        )
        assert response.status_code == 409
        assert "Upload a new version" in response.json()["error"]["message"]


# --------------------------------------------------------------------------
# Analysis, cohorts and quality
# --------------------------------------------------------------------------
class TestAnalysisEndpoints:
    @pytest.fixture()
    def ingested(self, client, npcu_headers):
        _upload(client, npcu_headers, state_code="KN")
        rows = [row[:] for row in CLEAN_ROWS]
        rows[0][4] = 400
        _upload(client, npcu_headers, rows=rows, state_code="GO")
        return npcu_headers

    def test_three_layer_indicator_analysis(self, client, ingested):
        body = client.get(
            "/api/v1/analytics/indicators/KPI-001", headers=ingested, params={"period": "2026-Q1"}
        ).json()

        assert body["national"]["value"] == 1300
        assert body["national"]["states_reporting"] == 2
        assert {row["cohort_code"] for row in body["cohorts"]} == {
            "ORIGINAL", "ADDITIONAL", "LIMITED",
        }
        reported = [row for row in body["states"] if row["reported"]]
        assert sum(row["contribution_pct"] for row in reported) == pytest.approx(100, abs=0.1)

    def test_contribution_endpoint_ranks_states(self, client, ingested):
        rows = client.get(
            "/api/v1/analytics/contribution/KPI-001", headers=ingested, params={"period": "2026-Q1"}
        ).json()["rows"]
        assert [row["state_code"] for row in rows] == ["KN", "GO"]
        assert rows[0]["rank"] == 1

    def test_trend_endpoint(self, client, ingested):
        body = client.get(
            "/api/v1/analytics/trend/KPI-001",
            headers=ingested,
            params={"scope": "NATIONAL", "period_type": "QUARTERLY"},
        ).json()
        assert [point["period_code"] for point in body["points"]] == ["2025-Q4", "2026-Q1"]

    def test_heatmap_endpoint(self, client, ingested):
        body = client.get(
            "/api/v1/analytics/heatmap", headers=ingested, params={"period": "2026-Q1"}
        ).json()
        assert set(body["rows"]) == {"KN", "KD", "GO", "LA"}
        assert len(body["cells"]) == len(body["rows"]) * len(body["columns"])

    def test_cohort_summary(self, client, ingested):
        rows = client.get(
            "/api/v1/cohorts/summary", headers=ingested, params={"period": "2026-Q1"}
        ).json()
        by_code = {row["cohort_code"]: row for row in rows}
        assert by_code["ORIGINAL"]["states_expected"] == 2
        assert by_code["ORIGINAL"]["states_reporting"] == 1
        assert by_code["ORIGINAL"]["reporting_rate_pct"] == 50.0

    def test_dqa_scorecard_and_national_summary(self, client, ingested):
        card = client.get(
            "/api/v1/quality/scorecards/KN", headers=ingested, params={"period": "2026-Q1"}
        ).json()
        assert card["state_name"] == "Kano"
        assert {dim["dimension"] for dim in card["dimensions"]} == {
            "INTEGRITY", "TIMELINESS", "ACCURACY", "COMPLETENESS",
            "CONSISTENCY", "VALIDITY", "UNIQUENESS",
        }

        national = client.get(
            "/api/v1/quality/national", headers=ingested, params={"period": "2026-Q1"}
        ).json()
        assert national["states_expected"] == 4
        assert national["states_reported"] == 2
        assert len(national["scorecards"]) == 4

    def test_dashboard_overview(self, client, ingested):
        body = client.get(
            "/api/v1/dashboard/overview", headers=ingested, params={"period": "2026-Q1"}
        ).json()
        assert body["period_code"] == "2026-Q1"
        assert {tile["key"] for tile in body["tiles"]} >= {
            "reporting_rate", "approved_rate", "dqa_score", "average_achievement",
        }
        assert body["reporting_status"]["states_expected"] == 4

    def test_unknown_indicator_is_a_clean_404(self, client, npcu_headers):
        response = client.get("/api/v1/analytics/indicators/KPI-999", headers=npcu_headers)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
class TestReporting:
    @pytest.fixture()
    def ingested(self, client, npcu_headers):
        _upload(client, npcu_headers, state_code="KN")
        _upload(client, npcu_headers, state_code="GO")
        return npcu_headers

    def test_national_report_in_three_formats(self, client, ingested):
        response = client.post(
            "/api/v1/reports",
            headers=ingested,
            json={
                "period_code": "2026-Q1",
                "scope": "NATIONAL",
                "formats": ["markdown", "html", "pdf"],
            },
        )
        assert response.status_code == 201
        body = response.json()
        assert {artifact["format"] for artifact in body["artifacts"]} == {
            "markdown", "html", "pdf",
        }
        assert body["indicator_count"] == 4
        assert "Q1 2026" in body["title"]
        assert "## 1. Reporting status and coverage" in body["markdown"]
        assert "## 5. Cohort analysis" in body["markdown"]

    def test_report_is_downloadable(self, client, ingested):
        report = client.post(
            "/api/v1/reports",
            headers=ingested,
            json={"period_code": "2026-Q1", "formats": ["markdown", "pdf"]},
        ).json()

        markdown = client.get(
            f"/api/v1/reports/{report['report_id']}/download",
            headers=ingested,
            params={"format": "markdown"},
        )
        assert markdown.status_code == 200
        assert markdown.text.startswith("# AGILE")

        pdf = client.get(
            f"/api/v1/reports/{report['report_id']}/download",
            headers=ingested,
            params={"format": "pdf"},
        )
        assert pdf.status_code == 200
        assert pdf.content[:5] == b"%PDF-"

    def test_state_report_scope(self, client, ingested):
        body = client.post(
            "/api/v1/reports",
            headers=ingested,
            json={"period_code": "2026-Q1", "scope": "STATE", "scope_ref": "KN"},
        ).json()
        assert "Kano" in body["title"]

    def test_cohort_report_scope(self, client, ingested):
        body = client.post(
            "/api/v1/reports",
            headers=ingested,
            json={"period_code": "2026-Q1", "scope": "COHORT", "scope_ref": "ORIGINAL"},
        ).json()
        assert "Original Financing States" in body["title"]

    def test_state_scope_reference_is_required(self, client, ingested):
        response = client.post(
            "/api/v1/reports", headers=ingested, json={"period_code": "2026-Q1", "scope": "STATE"}
        )
        assert response.status_code == 422
        assert "state code is required" in response.json()["error"]["message"]

    def test_state_piu_cannot_report_on_another_state(self, client, state_headers):
        response = client.post(
            "/api/v1/reports",
            headers=state_headers,
            json={"period_code": "2026-Q1", "scope": "STATE", "scope_ref": "LA"},
        )
        assert response.status_code == 403

    def test_unavailable_format_is_a_clean_404(self, client, ingested):
        report = client.post(
            "/api/v1/reports", headers=ingested, json={"period_code": "2026-Q1", "formats": ["markdown"]}
        ).json()
        response = client.get(
            f"/api/v1/reports/{report['report_id']}/download",
            headers=ingested,
            params={"format": "pdf"},
        )
        assert response.status_code == 404


# --------------------------------------------------------------------------
# Audit trail
# --------------------------------------------------------------------------
class TestAuditTrail:
    def test_uploads_and_approvals_are_recorded(self, client, npcu_headers):
        _upload(client, npcu_headers)
        rows = client.get("/api/v1/admin/audit", headers=npcu_headers).json()
        actions = {row["action"] for row in rows}

        assert "submission.ingest" in actions
        assert "auth.login" in actions
        entry = next(row for row in rows if row["action"] == "submission.ingest")
        assert entry["actor_email"] == "npcu@test.gov.ng"
        assert entry["request_id"]

    def test_audit_can_be_filtered(self, client, npcu_headers):
        _upload(client, npcu_headers)
        rows = client.get(
            "/api/v1/admin/audit", headers=npcu_headers, params={"action": "submission.ingest"}
        ).json()
        assert rows and all(row["action"] == "submission.ingest" for row in rows)

    def test_state_users_cannot_read_the_audit_trail(self, client, state_headers):
        assert client.get("/api/v1/admin/audit", headers=state_headers).status_code == 403


# --------------------------------------------------------------------------
# Dashboard pages
# --------------------------------------------------------------------------
class TestDashboardPages:
    def test_root_redirects_to_the_dashboard(self, client):
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 307
        assert response.headers["location"] == "/dashboard"

    def test_pages_render(self, client):
        assert "Sign in" in client.get("/login").text
        assert "National M&amp;E dashboard" in client.get("/dashboard").text

    def test_static_assets_are_served(self, client):
        for path in ["/static/css/app.css", "/static/js/charts.js", "/static/js/dashboard.js"]:
            assert client.get(path).status_code == 200, path

    def test_openapi_schema_is_published(self, client):
        schema = client.get("/api/openapi.json").json()
        assert schema["info"]["title"]
        assert "/api/v1/analytics/scorecard" in schema["paths"]
