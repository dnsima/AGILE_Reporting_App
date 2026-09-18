"""Re-seeding has to be an upgrade, not an accumulation.

A framework revision drops indicators, and the 2026 recode replaced every code
in the catalogue. Left alone, the previous catalogue stays active beside the new
one: templates carry both, completeness counts both, and every national total is
computed over a mixture. Retired rather than deleted, because submissions,
targets and findings already point at them and a report published last quarter
has to stay readable.
"""

from __future__ import annotations

from sqlalchemy import select

from app.models import State
from scripts.seed import _retire


class _Row:
    def __init__(self, is_active=True):
        self.is_active = is_active


class TestRetire:
    def test_a_row_the_seed_files_dropped_is_deactivated(self):
        existing = {"OLD-1": _Row(), "KEPT": _Row()}
        assert _retire(None, existing, {"KEPT"}, "indicator") == ["OLD-1"]
        assert existing["OLD-1"].is_active is False
        assert existing["KEPT"].is_active is True

    def test_nothing_is_deleted(self):
        existing = {"OLD-1": _Row()}
        _retire(None, existing, set(), "indicator")
        assert "OLD-1" in existing  # still on record, just inactive

    def test_an_already_retired_row_is_not_reported_again(self):
        existing = {"OLD-1": _Row(is_active=False)}
        assert _retire(None, existing, set(), "indicator") == []

    def test_retiring_nothing_reports_nothing(self):
        existing = {"KEPT": _Row()}
        assert _retire(None, existing, {"KEPT"}, "indicator") == []


class TestAgainstTheRealSeedFiles:
    """The seeder run against the real seeds/ files, not a stand-in."""

    def test_a_state_removed_from_the_list_is_retired(self, db):
        from scripts.seed import seed_states

        db.add(State(code="ZZ", name="Withdrawn State", geopolitical_zone="North East"))
        db.flush()

        seed_states(db)
        db.flush()

        withdrawn = db.scalar(select(State).where(State.code == "ZZ"))
        assert withdrawn is not None       # kept on record
        assert withdrawn.is_active is False

    def test_the_states_in_the_list_stay_active_and_carry_both_facts(self, db):
        from scripts.seed import seed_states

        seed_states(db)
        db.flush()
        states = [s for s in db.scalars(select(State)) if s.is_active]
        assert len(states) == 21
        # The Limited Financing states participate without yet reporting.
        assert sorted(s.code for s in states if not s.is_reporting) == ["DE", "EN", "TA"]

    def test_the_indicator_catalogue_is_the_recoded_one(self, db):
        """Every code carries a dot in its component, so no old code can match."""
        import csv

        from app.core.config import settings

        with (settings.seed_dir / "indicators.csv").open(encoding="utf-8") as handle:
            codes = [row["code"] for row in csv.DictReader(handle)]
        assert len(codes) == 54
        assert not [code for code in codes if code.startswith("KPI-")]
        assert all("." in code for code in codes if code.startswith("C"))
