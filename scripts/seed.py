"""Seed reference data: cohorts, states, indicator catalogue and periods.

Usage::

    python -m scripts.seed                       # seed everything
    python -m scripts.seed --years 2024 2025 2026
    python -m scripts.seed --reset               # drop and recreate all tables
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402
from app.core.enums import REPORTING_PERIOD_TYPES, PeriodType, Role  # noqa: E402
from app.core.logging_config import configure_logging, get_logger  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import engine, init_db, session_scope  # noqa: E402
from app.models import (  # noqa: E402
    Cohort,
    Indicator,
    IndicatorCategory,
    ReportingPeriod,
    State,
    StateSubcomponent,
    Subcomponent,
    Submission,
    User,
)
from app.services import reference  # noqa: E402
from app.services.validation import sync_rule_catalog  # noqa: E402

logger = get_logger("seed")


def _rows(filename: str) -> list[dict]:
    path = Path(settings.seed_dir) / filename
    if not path.exists():
        raise SystemExit(f"Missing seed file: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _as_bool(value: str | None) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def _as_float(value: str | None) -> float | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _as_list(value: str | None) -> list[str]:
    text = (value or "").strip()
    return [part.strip() for part in text.split("|") if part.strip()] if text else []


def seed_cohorts(db) -> int:
    existing = {row.code: row for row in db.scalars(select(Cohort))}
    created = 0
    for row in _rows("cohorts.csv"):
        cohort = existing.get(row["code"])
        if cohort is None:
            cohort = Cohort(code=row["code"])
            db.add(cohort)
            created += 1
        cohort.name = row["name"]
        cohort.description = row["description"]
        cohort.financing_window = row["financing_window"]
        cohort.start_year = int(row["start_year"]) if row["start_year"] else None
        cohort.sort_order = int(row["sort_order"])
    db.flush()
    return created


def _retire(db, existing: dict, seen: set[str], label: str) -> list[str]:
    """Deactivate rows the seed files no longer carry.

    Never deletes: submissions, targets and findings already point at them, and
    a report published last quarter has to stay readable.
    """
    retired = []
    for code, row in existing.items():
        if code not in seen and row.is_active:
            row.is_active = False
            retired.append(code)
    return retired


def seed_states(db) -> int:
    cohorts = {row.code: row for row in db.scalars(select(Cohort))}
    existing = {row.code: row for row in db.scalars(select(State))}
    created = 0
    seen: set[str] = set()
    for row in _rows("states.csv"):
        state = existing.get(row["code"])
        if state is None:
            state = State(code=row["code"])
            db.add(state)
            created += 1
        state.name = row["name"]
        state.geopolitical_zone = row["geopolitical_zone"]
        cohort = cohorts.get(row["cohort_code"])
        state.cohort_id = cohort.id if cohort else None
        # Participating from the day it is named; reporting once it starts.
        state.is_active = True
        state.is_reporting = _as_bool(row.get("is_reporting", "1"))
        seen.add(row["code"])

    retired = _retire(db, existing, seen, "state")
    db.flush()
    if retired:
        logger.info(
            "retired states no longer in the seed list",
            extra={"count": len(retired), "codes": sorted(retired)},
        )
    return created


def seed_subcomponents(db) -> int:
    existing = {row.code: row for row in db.scalars(select(Subcomponent))}
    created = 0
    for row in _rows("subcomponents.csv"):
        subcomponent = existing.get(row["code"])
        if subcomponent is None:
            subcomponent = Subcomponent(code=row["code"])
            db.add(subcomponent)
            created += 1
        subcomponent.name = row["name"]
        subcomponent.sort_order = int(row["sort_order"])
    db.flush()
    return created


def seed_applicability(db) -> int:
    """Which sub-components each state implements, and so must report."""
    states = {row.code: row for row in db.scalars(select(State))}
    subcomponents = {row.code: row for row in db.scalars(select(Subcomponent))}
    existing = {
        (row.state_id, row.subcomponent_id): row
        for row in db.scalars(select(StateSubcomponent))
    }
    created = 0
    for row in _rows("state_subcomponents.csv"):
        state = states.get(row["state_code"])
        subcomponent = subcomponents.get(row["subcomponent_code"])
        if state is None or subcomponent is None:
            continue
        link = existing.get((state.id, subcomponent.id))
        if link is None:
            link = StateSubcomponent(state_id=state.id, subcomponent_id=subcomponent.id)
            db.add(link)
            created += 1
        link.implements = _as_bool(row["implements"])
    db.flush()
    return created


def seed_categories(db) -> int:
    existing = {row.code: row for row in db.scalars(select(IndicatorCategory))}
    created = 0
    for row in _rows("indicator_categories.csv"):
        category = existing.get(row["code"])
        if category is None:
            category = IndicatorCategory(code=row["code"])
            db.add(category)
            created += 1
        category.name = row["name"]
        category.description = row["description"]
        category.sort_order = int(row["sort_order"])
    db.flush()
    return created


#: Percentages are bounded 0-100 and booleans 0-1; the rest are unbounded
#: counts that simply may not go negative.
UNIT_BOUNDS = {"PERCENT": (0.0, 100.0), "BOOLEAN": (0.0, 1.0)}


def seed_indicators(db) -> int:
    categories = {row.code: row for row in db.scalars(select(IndicatorCategory))}
    subcomponents = {row.code: row for row in db.scalars(select(Subcomponent))}
    existing = {row.code: row for row in db.scalars(select(Indicator))}
    created = 0
    seen: set[str] = set()
    for row in _rows("indicators.csv"):
        indicator = existing.get(row["code"])
        if indicator is None:
            indicator = Indicator(code=row["code"])
            db.add(indicator)
            created += 1

        indicator.number = int(row["number"])
        indicator.name = row["name"]
        indicator.legacy_code = row.get("legacy_code") or None
        category = categories.get(row["component"])
        indicator.category_id = category.id if category else None
        subcomponent = subcomponents.get(row["subcomponent"])
        indicator.subcomponent_id = subcomponent.id if subcomponent else None

        indicator.unit = row["unit"]
        indicator.aggregation_method = row["aggregation_method"]
        indicator.direction = row["direction"]
        indicator.is_cumulative = _as_bool(row["is_cumulative"])
        # Blank means "derive it", which reads a figure as a position at the
        # period's end. Set it only for a genuine within-period flow.
        indicator.time_basis = (row.get("time_basis") or "").strip().upper() or None
        indicator.is_reported = _as_bool(row["is_reported"])
        indicator.composite_of = _as_list(row["composite_of"])

        minimum, maximum = UNIT_BOUNDS.get(row["unit"], (0.0, None))
        indicator.min_value = minimum
        indicator.max_value = maximum
        indicator.decimal_places = 2 if row["unit"] in {"PERCENT", "RATIO"} else 0

        # The old code stays a recognised alias so files still using the
        # pre-recode template keep ingesting.
        indicator.aliases = _as_list(row.get("aliases", ""))
        indicator.is_core = True
        indicator.is_active = True
        seen.add(row["code"])

    # Re-seeding has to be an upgrade, not an accumulation. A framework
    # revision drops indicators, and the 2026 recode replaced every code: left
    # alone, the old catalogue stays active beside the new one and pollutes
    # every template, completeness check and national total. Retired rather
    # than deleted, because submissions already reference them.
    retired = _retire(db, existing, seen, "indicator")
    db.flush()
    if retired:
        logger.info(
            "retired indicators no longer in the catalogue",
            extra={"count": len(retired), "codes": sorted(retired)[:20]},
        )
    return created


def seed_periods(db, years: list[int]) -> int:
    created = 0
    for year in years:
        created += len(reference.generate_year(db, year))
    db.flush()
    return created


def retire_unused_period_types(db) -> int:
    """Remove half-year and annual periods nothing was ever filed against.

    Earlier seeds generated all four calendars, so an existing install carries
    H1/H2/A periods that clutter every picker. One carrying a submission is
    left exactly where it is -- deleting a period would orphan findings, targets
    and any report published against it.
    """
    unwanted = [str(t) for t in PeriodType if t not in REPORTING_PERIOD_TYPES]
    removed = 0
    for period in db.scalars(
        select(ReportingPeriod).where(ReportingPeriod.period_type.in_(unwanted))
    ):
        has_data = db.scalar(
            select(func.count(Submission.id)).where(Submission.period_id == period.id)
        )
        if has_data:
            logger.info(
                "keeping an unused-type period that carries data",
                extra={"period": period.code, "submissions": has_data},
            )
            continue
        db.delete(period)
        removed += 1
    db.flush()
    return removed


def seed_admin(db) -> bool:
    email = settings.bootstrap_admin_email.lower()
    if db.scalar(select(User).where(User.email == email)) is not None:
        return False
    db.add(
        User(
            email=email,
            full_name="Platform Administrator",
            hashed_password=hash_password(settings.bootstrap_admin_password),
            role=str(Role.ADMIN),
        )
    )
    db.flush()
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed AGILE reference data")
    parser.add_argument(
        "--years",
        nargs="*",
        type=int,
        default=[2024, 2025, 2026],
        help="Fiscal years to generate reporting calendars for",
    )
    parser.add_argument(
        "--reset", action="store_true", help="Drop every table before seeding (destructive)"
    )
    args = parser.parse_args()

    configure_logging(settings.log_level, as_json=False)

    if args.reset:
        confirm = input("This deletes ALL data in the database. Type 'yes' to continue: ")
        if confirm.strip().lower() != "yes":
            raise SystemExit("Aborted.")
        Base.metadata.drop_all(bind=engine)
        logger.info("dropped all tables")

    init_db()

    with session_scope() as db:
        counts = {
            "cohorts": seed_cohorts(db),
            "states": seed_states(db),
            "subcomponents": seed_subcomponents(db),
            "applicability": seed_applicability(db),
            "categories": seed_categories(db),
            "indicators": seed_indicators(db),
            "periods": seed_periods(db, args.years),
            "unused_periods_removed": retire_unused_period_types(db),
            "validation_rules": sync_rule_catalog(db),
        }
        admin_created = seed_admin(db)

    for entity, count in counts.items():
        logger.info("seeded", extra={"entity": entity, "created_count": count})
    if admin_created:
        logger.info(
            "bootstrap admin created",
            extra={"email": settings.bootstrap_admin_email},
        )
        print(
            f"\nAdmin account: {settings.bootstrap_admin_email} / "
            f"{settings.bootstrap_admin_password}\nChange this password after first sign-in.\n"
        )
    print("Seeding complete.")


if __name__ == "__main__":
    main()
