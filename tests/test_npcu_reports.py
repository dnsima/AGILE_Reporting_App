"""The two NPCU quarterly reports, and Word output.

These are the documents the NPCU circulates to the Bank and to state PIUs, so
what matters is that they are generated from the same figures the dashboard
shows, that they disclose rather than quietly adjust, and that they open in
Word.
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone
from itertools import count

import pytest

from app.core.enums import ReportFormat, ReportKind, SubmissionStatus
from app.models import Indicator, IndicatorValue, Submission
from app.schemas.reporting import ReportRequest
from app.services import analytics, queries, reference
from app.services.ingestion.pipeline import revalidate_period
from app.services.reporting.docx_renderer import render_docx
from app.services.reporting.renderers import render_markdown
from app.services.reporting.technical_report import build_technical_report
from app.services.reporting.validation_report import build_validation_report
from app.services.validation import run_validation

_numbers = count(1400)


def _indicator(db, code, **kwargs):
    indicator = Indicator(
        code=code,
        number=next(_numbers),
        name=kwargs.pop("name", code),
        unit=kwargs.pop("unit", "NUMBER"),
        aggregation_method="SUM",
        direction="INCREASE",
        is_cumulative=kwargs.pop("is_cumulative", True),
        **kwargs,
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
def quarter(db):
    """A period with one collapsed cumulative figure and one sound state."""
    indicator = _indicator(db, "NPR-01", name="Schools with safeguarding mechanisms")
    before = _submission(db, "KN", "2025-Q4")
    _value(db, before, indicator, 5_960)

    flagged_state = _submission(db, "KN", "2026-Q1")
    _value(db, flagged_state, indicator, 127)
    run_validation(db, flagged_state)
    queries.raise_queries(db, flagged_state)

    sound = _submission(db, "KD", "2026-Q1")
    _value(db, sound, indicator, 1_000)
    run_validation(db, sound)

    period = reference.get_period_by_code(db, "2026-Q1")
    revalidate_period(db, period)
    analytics.clear_analysis_cache(db)
    # The API tests reach the database on their own connection, so the fixture
    # has to release its write lock before the TestClient can see any of this.
    db.commit()
    return period


class TestValidationReport:
    def test_it_names_the_issue_with_its_evidence_and_likely_cause(self, db, quarter):
        text = render_markdown(build_validation_report(db, quarter.code))
        assert "Data Quality Validation Report" in text
        assert "NPR-01" in text
        assert "5,960 to 127" in text
        assert "cumulative" in text.lower()

    def test_it_states_that_nothing_was_withheld(self, db, quarter):
        text = render_markdown(build_validation_report(db, quarter.code))
        assert "does not restate a national result on its own authority" in text

    def test_it_recommends_resolving_through_the_query_workflow(self, db, quarter):
        text = render_markdown(build_validation_report(db, quarter.code))
        assert "A re-upload does not clear an open query." in text


class TestTechnicalReport:
    def test_the_national_figure_is_the_one_states_reported(self, db, quarter):
        """1,127 -- the flagged 127 included, not the 1,000 left after it."""
        text = render_markdown(build_technical_report(db, quarter.code))
        assert "1,127" in text

    def test_it_carries_the_data_quality_caveat(self, db, quarter):
        text = render_markdown(build_technical_report(db, quarter.code))
        assert "Data quality" in text
        assert "understated" in text

    def test_it_does_not_invent_a_judgement_it_cannot_support(self, db, quarter):
        """The cohort comparison is marked for the author, not asserted."""
        text = render_markdown(build_technical_report(db, quarter.code))
        assert "[for review]" in text


class TestWordOutput:
    def test_both_reports_open_as_word_documents(self, db, quarter):
        for build in (build_validation_report, build_technical_report):
            payload = render_docx(build(db, quarter.code))
            archive = zipfile.ZipFile(io.BytesIO(payload))
            names = archive.namelist()
            assert "word/document.xml" in names
            assert "[Content_Types].xml" in names
            assert len(payload) > 10_000

    def test_the_word_file_carries_the_report_text(self, db, quarter):
        payload = render_docx(build_validation_report(db, quarter.code))
        archive = zipfile.ZipFile(io.BytesIO(payload))
        document = archive.read("word/document.xml").decode("utf-8")
        assert "NPR-01" in document
        assert "Data Quality Validation Report" in document


class TestThroughTheApi:
    def test_a_validation_report_can_be_generated_and_downloaded(
        self, client, npcu_headers, db, quarter
    ):
        response = client.post(
            "/api/v1/reports",
            headers=npcu_headers,
            json=ReportRequest(
                period_code=quarter.code,
                kind=ReportKind.VALIDATION,
                formats=[ReportFormat.DOCX, ReportFormat.MARKDOWN],
            ).model_dump(mode="json"),
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert {a["format"] for a in body["artifacts"]} == {"docx", "markdown"}

        download = client.get(
            f"/api/v1/reports/{body['report_id']}/download",
            headers=npcu_headers,
            params={"format": "docx"},
        )
        assert download.status_code == 200
        assert download.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument"
        )
        assert zipfile.ZipFile(io.BytesIO(download.content)).namelist()

    def test_a_technical_report_can_be_generated(
        self, client, npcu_headers, db, quarter
    ):
        response = client.post(
            "/api/v1/reports",
            headers=npcu_headers,
            json=ReportRequest(
                period_code=quarter.code,
                kind=ReportKind.TECHNICAL,
                formats=[ReportFormat.DOCX],
            ).model_dump(mode="json"),
        )
        assert response.status_code == 201, response.text
        assert "Technical Performance Report" in response.json()["title"]
