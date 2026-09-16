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

from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402
from app.core.enums import PeriodType, Role  # noqa: E402
from app.core.logging_config import configure_logging, get_logger  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import engine, init_db, session_scope  # noqa: E402
from app.models import Cohort, Indicator, IndicatorCategory, State, User  # noqa: E402
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


def seed_states(db) -> int:
    cohorts = {row.code: row for row in db.scalars(select(Cohort))}
    existing = {row.code: row for row in db.scalars(select(State))}
    created = 0
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


def seed_indicators(db) -> int:
    categories = {row.code: row for row in db.scalars(select(IndicatorCategory))}
    existing = {row.code: row for row in db.scalars(select(Indicator))}
    created = 0
    for row in _rows("indicators.csv"):
        indicator = existing.get(row["code"])
        if indicator is None:
            indicator = Indicator(code=row["code"])
            db.add(indicator)
            created += 1
        indicator.number = int(row["number"])
        indicator.name = row["name"]
        indicator.definition = row["definition"]
        category = categories.get(row["category_code"])
        indicator.category_id = category.id if category else None
        indicator.unit = row["unit"]
        indicator.aggregation_method = row["aggregation_method"]
        indicator.direction = row["direction"]
        indicator.is_cumulative = _as_bool(row["is_cumulative"])
        indicator.requires_numerator_denominator = _as_bool(row["requires_numerator_denominator"])
        indicator.baseline_value = _as_float(row["baseline_value"])
        indicator.min_value = _as_float(row["min_value"])
        indicator.max_value = _as_float(row["max_value"])
        indicator.decimal_places = int(row["decimal_places"] or 0)
        indicator.disaggregations = _as_list(row["disaggregations"])
        indicator.aliases = _as_list(row["aliases"])
        indicator.is_core = _as_bool(row["is_core"])
        indicator.is_active = True
    db.flush()
    return created


def seed_periods(db, years: list[int]) -> int:
    created = 0
    for year in years:
        created += len(reference.generate_year(db, year, list(PeriodType)))
    db.flush()
    return created


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
            "categories": seed_categories(db),
            "indicators": seed_indicators(db),
            "periods": seed_periods(db, args.years),
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
