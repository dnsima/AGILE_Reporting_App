"""The report section listing what remains under query.

A reporting cycle cannot wait on a query, so the report publishes anyway. What
it must not do is print a provisional total as though it were settled. These
tests hold the report to saying plainly what is unconfirmed, which printed
figures are affected, and what someone should go and physically check.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from itertools import count

import pytest

from app.core.enums import QueryStatus, SubmissionStatus
from app.core.formatting import fmt
from app.models import DataQuery, Indicator, IndicatorValue, Submission
from app.schemas.reporting import ReportRequest
from app.services import queries as query_service
from app.services import reference
from app.services.reporting import build_report, render_markdown

_numbers = count(600)


# --------------------------------------------------------------------------
# Fixtures
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
        quarantine_reason=None if valid else "CON-002: cumulative figure fell",
        disaggregation={},
    )
    db.add(row)
    db.flush()
    return row


def _query(db, submission, indicator, **kwargs):
    query = DataQuery(
        submission_id=submission.id,
        state_id=submission.state_id,
        period_id=submission.period_id,
        indicator_id=indicator.id,
        rule_code=kwargs.pop("rule_code", "CON-002"),
        dimension="CONSISTENCY",
        severity="ERROR",
        title=kwargs.pop("title", f"{indicator.code} is cumulative but fell from 1,577 to 342."),
        reported_value=kwargs.pop("reported_value", 342.0),
        status=str(kwargs.pop("status", QueryStatus.OPEN)),
        due_date=kwargs.pop("due_date", date.today() + timedelta(days=14)),
        **kwargs,
    )
    db.add(query)
    db.flush()
    return query


def _report(db, **overrides) -> str:
    request = ReportRequest(
        period_code="2026-Q1",
        scope="NATIONAL",
        formats=["markdown"],
        include_trends=False,
        include_narratives=False,
        **overrides,
    )
    document, _indicators, _period = build_report(db, request)
    return render_markdown(document)


def _section(markdown: str, heading: str) -> str:
    lines = markdown.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("## ") and heading in line)
    following = [
        i for i, line in enumerate(lines) if i > start and line.startswith("## ")
    ]
    end = following[0] if following else len(lines)
    return "\n".join(lines[start:end])


@pytest.fixture()
def under_query(db):
    indicator = db.query(Indicator).order_by(Indicator.number).first()
    submission = _submission(db)
    value = _value(db, submission, indicator, 342.0, valid=False)
    query = _query(db, submission, indicator)
    db.flush()
    return {"indicator": indicator, "submission": submission, "value": value, "query": query}


# --------------------------------------------------------------------------
# Numbers a person can read
# --------------------------------------------------------------------------
class TestFormatting:
    def test_a_large_figure_keeps_its_digits(self):
        """`:g` gave "1.02431e+06" for both sides of a comparison that differed."""
        assert fmt(1024314) == "1,024,314"
        assert fmt(1024306) == "1,024,306"
        assert fmt(1024314) != fmt(1024306)

    def test_a_whole_number_shows_no_decimals(self):
        assert fmt(342.0) == "342"
        assert fmt(0) == "0"

    def test_a_fraction_keeps_the_part_that_matters(self):
        assert fmt(64.55) == "64.55"
        assert fmt(64.5) == "64.5"

    def test_nothing_is_an_em_dash(self):
        assert fmt(None) == "—"
        assert fmt(float("nan")) == "—"

    def test_the_finding_a_state_reads_is_formatted(self, db):
        from app.services.validation.rules import RuleContext, cumulative_monotonic

        indicator = db.query(Indicator).order_by(Indicator.number).first()
        indicator.is_cumulative = True
        submission = _submission(db)
        value = _value(db, submission, indicator, 1024306.0)
        ctx = RuleContext(
            submission=submission,
            state=submission.state,
            period=submission.period,
            values=[value],
            indicators_by_id={indicator.id: indicator},
            expected_indicator_ids={indicator.id},
            previous_values={indicator.id: 1024314.0},
            history={},
            targets={},
            duplicate_submission_ids=[],
            settings=None,
        )
        finding = next(iter(cumulative_monotonic(ctx)))
        assert "fell from 1,024,314 to 1,024,306" in finding.message


# --------------------------------------------------------------------------
# The section
# --------------------------------------------------------------------------
class TestQuerySection:
    def test_it_comes_before_the_numbers_it_qualifies(self, db, under_query):
        markdown = _report(db)
        headings = [
            line
            for line in markdown.splitlines()
            if line.startswith("## ") and line != "## Executive summary"
        ]
        names = [heading.split(". ", 1)[1] for heading in headings]
        assert names.index("Figures under query") < names.index("KPI performance")

    def test_sections_are_numbered_in_order(self, db, under_query):
        headings = [
            line
            for line in _report(db).splitlines()
            if line.startswith("## ") and line != "## Executive summary"
        ]
        numbers = [int(heading[3:].split(".", 1)[0]) for heading in headings]
        assert numbers == list(range(1, len(numbers) + 1))

    def test_the_executive_summary_carries_the_caveat(self, db, under_query):
        """A caveat that only appears in its own section has been read too late."""
        document, _indicators, _period = build_report(
            db, ReportRequest(period_code="2026-Q1", formats=["markdown"])
        )
        assert "1 figure(s) remain under query" in document.summary
        assert "held out of the totals above, so those totals are provisional" in document.summary

    def test_it_names_the_indicators_whose_totals_are_provisional(self, db, under_query):
        section = _section(_report(db), "Figures under query")
        assert "Indicators whose national total in this report excludes a held figure" in section
        assert under_query["indicator"].code in section
        assert "Kano" in section or "| KN " in section
        assert "Treat these totals as provisional" in section

    def test_a_figure_that_counts_is_not_listed_as_held(self, db, under_query):
        under_query["value"].is_valid = True
        db.flush()
        section = _section(_report(db), "Figures under query")
        assert "No figure covering an indicator in this report is held out" in section

    def test_it_lists_the_figures_with_the_finding_verbatim(self, db, under_query):
        section = _section(_report(db), "Figures under query")
        assert f"Q-{under_query['query'].id}" in section
        assert "is cumulative but fell from 1,577 to 342" in section

    def test_overdue_queries_are_marked(self, db, under_query):
        under_query["query"].due_date = date.today() - timedelta(days=12)
        db.flush()
        section = _section(_report(db), "Figures under query")
        assert "12d late" in section
        assert "1 are past the date the state was given to respond" in section

    def test_the_verification_list_is_the_supervision_worklist(self, db, under_query):
        under_query["query"].verification_required = True
        under_query["query"].status = str(QueryStatus.VERIFICATION)
        under_query["query"].resolution_note = "Count the classrooms on site."
        db.flush()
        section = _section(_report(db), "Figures under query")
        assert "Referred for physical verification" in section
        assert "next supportive supervision or DQA exercise" in section
        assert "Count the classrooms on site." in section

    def test_restatements_are_reported_so_an_earlier_report_reconciles(
        self, db, under_query
    ):
        query = under_query["query"]
        query_service.respond(
            db, query, narrative="The Q4 figure was overstated.", proposed_value=300.0
        )
        query_service.accept(db, query, note="Certificates checked.")
        db.flush()

        section = _section(_report(db), "Figures under query")
        assert "Figures restated for this period" in section
        assert "As first reported" in section
        assert "342" in section and "300" in section

    def test_a_period_with_nothing_queried_says_so(self, db):
        _submission(db)
        section = _section(_report(db), "Figures under query")
        assert "No figure reported for 2026-Q1 was queried" in section
        assert "none is held out of the totals above" in section

    def test_the_section_can_be_switched_off(self, db, under_query):
        markdown = _report(db, include_queries=False)
        assert "Figures under query" not in markdown
        # And the remaining sections are still numbered without a gap.
        headings = [
            line
            for line in markdown.splitlines()
            if line.startswith("## ") and line != "## Executive summary"
        ]
        numbers = [int(heading[3:].split(".", 1)[0]) for heading in headings]
        assert numbers == list(range(1, len(numbers) + 1))


# --------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------
class TestScope:
    def test_a_state_report_covers_only_that_states_queries(self, db, under_query):
        other = _submission(db, state="KD")
        indicator = under_query["indicator"]
        _value(db, other, indicator, 99.0, valid=False)
        kaduna_query = _query(db, other, indicator, title="Kaduna's own flagged figure.")
        db.flush()

        document, _indicators, _period = build_report(
            db,
            ReportRequest(
                period_code="2026-Q1",
                scope="STATE",
                scope_ref="KD",
                formats=["markdown"],
                include_trends=False,
                include_narratives=False,
            ),
        )
        section = _section(render_markdown(document), "Figures under query")
        assert f"Q-{kaduna_query.id}" in section
        assert f"Q-{under_query['query'].id}" not in section
        assert "Kaduna" in section
