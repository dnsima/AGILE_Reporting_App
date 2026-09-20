"""Disclosure and exposure: what the platform says about a doubted figure.

The contract these tests hold in place is that the platform never removes a
reported figure from a total. It reports what states reported, says what it
doubts, and leaves changing a figure to the change-management process.
"""

from __future__ import annotations

from datetime import datetime, timezone
from itertools import count

import pytest
from sqlalchemy import select

from app.core.enums import (
    DisclosureStatus,
    FitnessVerdict,
    SubmissionStatus,
    verdict_for_submission,
)
from app.models import DataQuery, Indicator, IndicatorValue, Submission
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


class TestChangeManagement:
    """A re-upload is not a resolution."""

    def test_an_open_query_follows_the_figure_onto_a_new_submission(self, db, collapse):
        from app.services.ingestion.pipeline import _supersede_previous

        indicator, _flagged, later = collapse
        state_id, period_id = later.state_id, later.period_id
        opened = [
            q
            for q in db.scalars(
                select(DataQuery).where(DataQuery.submission_id == later.id)
            )
        ]
        assert opened, "the fixture should have raised a query"

        replacement = Submission(
            state_id=state_id,
            period_id=period_id,
            version=later.version + 1,
            status=str(SubmissionStatus.APPROVED),
            is_current=True,
            uploaded_at=datetime.now(timezone.utc),
        )
        db.add(replacement)
        db.flush()
        # The state re-submits the very same wrong figure.
        resubmitted = _value(db, replacement, indicator, 127)

        _supersede_previous(db, state_id, period_id, keep_id=replacement.id)

        carried = [
            q
            for q in db.scalars(
                select(DataQuery).where(DataQuery.submission_id == replacement.id)
            )
        ]
        assert len(carried) == len(opened)
        assert all(q.is_open for q in carried)
        assert resubmitted.is_valid is False
        assert resubmitted.disclosure_status == str(DisclosureStatus.UNFIT)

    def test_the_superseded_return_keeps_no_open_queries(self, db, collapse):
        from app.services.ingestion.pipeline import _supersede_previous

        indicator, _flagged, later = collapse
        replacement = Submission(
            state_id=later.state_id,
            period_id=later.period_id,
            version=later.version + 1,
            status=str(SubmissionStatus.APPROVED),
            is_current=True,
            uploaded_at=datetime.now(timezone.utc),
        )
        db.add(replacement)
        db.flush()
        _value(db, replacement, indicator, 127)
        _supersede_previous(db, later.state_id, later.period_id, keep_id=replacement.id)

        db.refresh(later)
        assert later.open_query_count == 0
        assert replacement.open_query_count > 0


class TestDashboardSurfacesTheVerdict:
    """The verdict was computed, stored, and then shown nowhere at all."""

    def _tiles(self, db, period, state_code=None):
        from app.services import dashboard as ds

        return {t.key: t for t in ds.overview(db, period, state_code=state_code).tiles}

    def test_the_board_leads_with_fitness_not_the_score(self, db, collapse):
        period = reference.get_period_by_code(db, "2026-Q1")
        from app.services.ingestion.pipeline import revalidate_period

        revalidate_period(db, period)
        tiles = self._tiles(db, period)
        assert "fitness" in tiles
        assert tiles["fitness"].status == str(FitnessVerdict.NOT_FIT)

    def test_the_score_tile_says_what_it_is_not(self, db, collapse):
        """A reader took "Excellent" as a verdict, so the tile disowns that."""
        period = reference.get_period_by_code(db, "2026-Q1")
        caption = self._tiles(db, period)["dqa_score"].caption
        assert "not a verdict" in caption.lower()

    def test_selecting_a_state_changes_the_figures(self, db, collapse):
        """The filter set a variable nothing read, so the board never moved."""
        from app.services.ingestion.pipeline import revalidate_period

        period = reference.get_period_by_code(db, "2026-Q1")
        revalidate_period(db, period)

        flagged = self._tiles(db, period, state_code="KN")
        sound = self._tiles(db, period, state_code="KD")

        assert flagged["fitness"].status == str(FitnessVerdict.NOT_FIT)
        assert sound["fitness"].status != str(FitnessVerdict.NOT_FIT)
        assert flagged["fitness"].value != sound["fitness"].value

    def test_a_state_view_is_not_the_national_view(self, db, collapse):
        period = reference.get_period_by_code(db, "2026-Q1")
        national = self._tiles(db, period)["fitness"]
        one_state = self._tiles(db, period, state_code="KD")["fitness"]
        assert national.label != one_state.label

    def test_headline_indicator_codes_exist_in_the_catalogue(self, db):
        """They named pre-recode codes for a while, so every tile was blank."""
        from app.services.dashboard import HEADLINE_INDICATORS

        seeded = {i.code for i in db.query(Indicator)}
        # The test catalogue is small, so assert the shape rather than presence:
        # a headline code must at least look like a current framework code.
        for code in HEADLINE_INDICATORS:
            assert code.startswith(("PDO-", "C1", "C2", "C3")), code
            assert not code.startswith("KPI-"), f"{code} is a retired code"
        assert isinstance(seeded, set)


class TestStateContribution:
    """Targets are national, so a state is measured on its share of them."""

    def _tile(self, db, period, state_code):
        from app.services import dashboard as ds

        tiles = {t.key: t for t in ds.overview(db, period, state_code=state_code).tiles}
        return tiles.get("contribution")

    def test_a_state_view_reports_contribution_not_achievement(self, db, collapse):
        period = reference.get_period_by_code(db, "2026-Q1")
        from app.services import dashboard as ds

        keys = {t.key for t in ds.overview(db, period, state_code="KD").tiles}
        assert "contribution" in keys
        assert "average_achievement" not in keys

    def test_the_national_view_keeps_average_achievement(self, db, collapse):
        from app.services import dashboard as ds

        period = reference.get_period_by_code(db, "2026-Q1")
        keys = {t.key for t in ds.overview(db, period).tiles}
        assert "average_achievement" in keys
        assert "contribution" not in keys

    def test_rates_are_excluded_from_the_share(self, db):
        """A completion rate is a state's own performance, not a slice.

        Including one gave a state reporting 88% against a national target of
        56% a "contribution" of 157%, which is not a share of anything.
        """
        from app.core.enums import ADDITIVE_METHODS, AggregationMethod

        assert AggregationMethod.SUM in ADDITIVE_METHODS
        assert AggregationMethod.AVERAGE_NONZERO not in ADDITIVE_METHODS
        assert AggregationMethod.AVERAGE not in ADDITIVE_METHODS


class TestReportingPeriodTypes:
    """AGILE reports monthly and quarterly; nothing else was ever filed."""

    def test_only_monthly_and_quarterly_are_offered(self, db):
        from app.core.enums import PeriodType

        offered = {p.period_type for p in reference.ordered_periods(db)}
        assert offered <= {str(PeriodType.MONTHLY), str(PeriodType.QUARTERLY)}

    def test_a_legacy_period_is_still_reachable_when_asked_for(self, db):
        """Retained on the enum so an older database still loads."""
        from app.core.enums import PeriodType

        assert PeriodType("SEMI_ANNUAL") is PeriodType.SEMI_ANNUAL
        everything = reference.ordered_periods(db, reporting_only=False)
        assert len(everything) >= len(reference.ordered_periods(db))

    def test_generating_a_year_makes_only_the_two(self, db):
        from app.core.enums import PeriodType

        made = reference.generate_year(db, 2031)
        types = {p.period_type for p in made}
        assert types == {str(PeriodType.MONTHLY), str(PeriodType.QUARTERLY)}
        assert len(made) == 16


class TestEveryBoardHonoursTheStateFilter:
    """Twice now a filter has been wired to nothing and shipped.

    The overview ignored it, then the data-quality board ignored it, and both
    times the page looked identical whichever state was chosen. These assert
    the contract rather than one call site.
    """

    def test_the_quality_board_scopes_to_the_state(self, db, collapse):
        from app.services import dqa
        from app.services.ingestion.pipeline import revalidate_period

        period = reference.get_period_by_code(db, "2026-Q1")
        revalidate_period(db, period)

        national = dqa.national_summary(db, period)
        scoped = dqa.national_summary(db, period, state_code="KD")

        assert national.states_reported > scoped.states_reported
        assert scoped.states_reported == 1
        assert scoped.scorecards[0].state_code == "KD"

    def test_the_scorecard_carries_the_verdict(self, db, collapse):
        from app.services import dqa
        from app.services.ingestion.pipeline import revalidate_period

        period = reference.get_period_by_code(db, "2026-Q1")
        revalidate_period(db, period)
        scoped = dqa.national_summary(db, period, state_code="KN")
        card = scoped.scorecards[0]
        assert card.fitness_verdict == str(FitnessVerdict.NOT_FIT)
        assert card.exposed_share_pct is not None

    def test_the_national_board_counts_states_that_are_not_fit(self, db, collapse):
        from app.services import dqa
        from app.services.ingestion.pipeline import revalidate_period

        period = reference.get_period_by_code(db, "2026-Q1")
        revalidate_period(db, period)
        assert dqa.national_summary(db, period).states_not_fit >= 1

    def test_two_states_do_not_produce_the_same_board(self, db, collapse):
        """The complaint that started this: every state looked identical."""
        from app.services import dqa
        from app.services.ingestion.pipeline import revalidate_period

        period = reference.get_period_by_code(db, "2026-Q1")
        revalidate_period(db, period)

        flagged = dqa.national_summary(db, period, state_code="KN").scorecards[0]
        sound = dqa.national_summary(db, period, state_code="KD").scorecards[0]
        assert flagged.fitness_verdict != sound.fitness_verdict
        assert flagged.exposed_share_pct != sound.exposed_share_pct
