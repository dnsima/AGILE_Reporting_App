"""Generate demonstration data.

Builds targets for every state and indicator, then produces a filled reporting
template per state per period and pushes it through the real ingestion
pipeline, so parsing, auto-mapping, validation, DQA scoring and approval are all
exercised exactly as they would be in production.

A deterministic seed makes runs reproducible, and a handful of states are given
deliberate data quality problems (missing indicators, out-of-range percentages,
duplicate rows, an implausible swing, late submission) so the DQA scorecards and
the validation engine have something real to show.

Usage::

    python -m scripts.demo --periods 2025-Q3 2025-Q4 2026-Q1 2026-Q2
    python -m scripts.demo --year 2026 --period-type QUARTERLY --format csv
"""

from __future__ import annotations

import argparse
import csv
import io
import random
import sys
from datetime import timedelta
from pathlib import Path

from openpyxl import Workbook
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings  # noqa: E402
from app.core.enums import IndicatorUnit, SubmissionStatus, TargetLevel  # noqa: E402
from app.core.logging_config import configure_logging, get_logger  # noqa: E402
from app.db.session import init_db, session_scope  # noqa: E402
from app.models import Indicator, ReportingPeriod, State, Target  # noqa: E402
from app.services import reference  # noqa: E402
from app.services.ingestion import ingest_upload  # noqa: E402
from app.services.ingestion.pipeline import approve_submission  # noqa: E402
from app.services.validation import run_validation  # noqa: E402
from app.services.validation.rules import SUBSET_RELATIONSHIPS  # noqa: E402

logger = get_logger("demo")

HEADERS = [
    "KPI Number", "Indicator Code", "Indicator Name", "Unit", "Value",
    "Numerator", "Denominator", "Sex", "School Level", "Data Source", "Comments",
]

#: Mean achievement against target, by financing cohort. Original-financing
#: states have been implementing longest and perform best.
COHORT_PERFORMANCE = {"ORIGINAL": 0.96, "ADDITIONAL": 0.84, "LIMITED": 0.70}

#: States given deliberate quality problems so the DQA scorecards, the
#: validation findings and the quality gate all have something real to show.
QUALITY_ISSUES = {
    "BY": "missing",        # omits a block of indicators
    "EB": "missing",
    "IM": "missing",
    "TA": "out_of_range",   # reports a percentage above 100
    "KO": "out_of_range",
    "NA": "out_of_range",
    "AN": "duplicate",      # the same indicator reported twice
    "ZA": "swing",          # implausible period-over-period change
    "DE": "swing",
    "CR": "late",           # submitted well after the deadline
    "OS": "late",
    "BE": "late",
    "AB": "sparse",         # reports only part of the template
    "AK": "sparse",
    "EN": "sparse",
    "OG": "sparse",
}


def base_target(indicator: Indicator, rng: random.Random) -> float:
    """A plausible national-scale target for one indicator."""
    unit = IndicatorUnit(indicator.unit)
    if unit == IndicatorUnit.PERCENT:
        return round(rng.uniform(55, 92), 1)
    if unit == IndicatorUnit.SCORE:
        return 85.0
    if unit == IndicatorUnit.RATIO:
        return round(rng.uniform(35, 45), 1)
    if unit == IndicatorUnit.CURRENCY_NGN_M:
        return round(rng.uniform(60, 900), 2)
    # Counts: beneficiary indicators are orders of magnitude larger than
    # infrastructure or training counts.
    if indicator.number in {1, 7, 29, 36, 41, 42, 46}:
        return float(rng.randint(8000, 45000))
    if indicator.number in {9, 10, 11, 12, 13, 14, 15, 16, 18, 22}:
        return float(rng.randint(40, 320))
    return float(rng.randint(120, 2600))


def enforce_subsets(values: dict[str, float], rng: random.Random) -> None:
    """Keep child indicators inside their parent totals.

    The validation engine enforces this as a blocking integrity rule, so demo
    data has to respect it or every submission would be rejected for a defect
    in the generator rather than in the data story being told.
    """
    for child_code, parent_code in SUBSET_RELATIONSHIPS.items():
        child = values.get(child_code)
        parent = values.get(parent_code)
        if child is None or parent is None:
            continue
        if child > parent:
            values[child_code] = round(parent * rng.uniform(0.35, 0.92), 2)


def state_scale(state: State, rng: random.Random) -> float:
    """Relative size multiplier so bigger states carry bigger targets."""
    large = {"KN", "KD", "KT", "LA", "OY", "BO", "BA", "RI"}
    if state.code in large:
        return rng.uniform(1.3, 1.9)
    return rng.uniform(0.55, 1.15)


def build_targets(
    db,
    periods: list[ReportingPeriod],
    indicators: list[Indicator],
    states: list[State],
) -> int:
    existing = {
        (row.indicator_id, row.period_id, row.level, row.state_id)
        for row in db.scalars(select(Target))
    }
    created = 0
    for period in periods:
        national_totals: dict[int, float] = {}
        for state in states:
            rng = random.Random(f"{state.code}-{period.code}-targets")
            scale = state_scale(state, rng)

            by_code: dict[str, float] = {}
            for indicator in indicators:
                value = base_target(indicator, rng)
                if IndicatorUnit(indicator.unit) in {
                    IndicatorUnit.NUMBER, IndicatorUnit.CURRENCY_NGN_M
                }:
                    value = round(value * scale, 2)
                by_code[indicator.code] = value
            enforce_subsets(by_code, rng)

            for indicator in indicators:
                value = by_code[indicator.code]
                if IndicatorUnit(indicator.unit) in {
                    IndicatorUnit.NUMBER, IndicatorUnit.CURRENCY_NGN_M
                }:
                    national_totals[indicator.id] = national_totals.get(indicator.id, 0) + value
                else:
                    national_totals.setdefault(indicator.id, value)

                key = (indicator.id, period.id, str(TargetLevel.STATE), state.id)
                if key in existing:
                    continue
                db.add(
                    Target(
                        indicator_id=indicator.id,
                        period_id=period.id,
                        level=str(TargetLevel.STATE),
                        state_id=state.id,
                        target_value=value,
                        source="Demo data generator",
                    )
                )
                existing.add(key)
                created += 1

        for indicator in indicators:
            key = (indicator.id, period.id, str(TargetLevel.NATIONAL), None)
            if key in existing:
                continue
            db.add(
                Target(
                    indicator_id=indicator.id,
                    period_id=period.id,
                    level=str(TargetLevel.NATIONAL),
                    state_id=None,
                    target_value=round(national_totals.get(indicator.id, 0), 2),
                    source="Demo data generator",
                )
            )
            existing.add(key)
            created += 1
    db.flush()
    return created


def generate_rows(
    state: State,
    period: ReportingPeriod,
    indicators: list[Indicator],
    targets: dict[int, float],
    carried: dict[int, float],
) -> list[list]:
    """Build the data rows for one state's template."""
    rng = random.Random(f"{state.code}-{period.code}-values")
    cohort_code = state.cohort.code if state.cohort else "LIMITED"
    mean = COHORT_PERFORMANCE.get(cohort_code, 0.75)
    issue = QUALITY_ISSUES.get(state.code)

    reported = [
        (position, indicator)
        for position, indicator in enumerate(indicators)
        if targets.get(indicator.id) is not None
        # A "missing" state leaves a whole block of the template blank.
        and not (issue == "missing" and 12 <= position < 30)
        # A "sparse" state skips a scattering of indicators across the template.
        and not (issue == "sparse" and rng.random() < 0.18)
    ]

    # --- pass 1: draw a raw value for every indicator -----------------------
    values: dict[str, float] = {}
    forced_invalid: dict[str, float] = {}
    for position, indicator in reported:
        target = targets[indicator.id]
        achievement = max(0.15, min(1.45, rng.gauss(mean, 0.17)))
        value = target * achievement

        if indicator.is_cumulative:
            value = max(value, carried.get(indicator.id, 0.0) * rng.uniform(1.0, 1.12))

        if issue == "swing" and position % 23 == 5:
            value = value * rng.uniform(6, 12)  # implausible period-over-period jump

        values[indicator.code] = value
        if (
            issue == "out_of_range"
            and IndicatorUnit(indicator.unit) == IndicatorUnit.PERCENT
            and position % 17 == 3
        ):
            # Applied after the numerator/denominator rebuild in pass 3, which
            # is what a state mistyping a percentage actually looks like.
            forced_invalid[indicator.code] = round(rng.uniform(104, 138), 1)

    # --- pass 2: keep subtotals inside their parents ------------------------
    enforce_subsets(values, rng)

    # --- pass 3: format each row the way a state would fill the template ----
    rows: list[list] = []
    for position, indicator in reported:
        value = values[indicator.code]
        unit = IndicatorUnit(indicator.unit)
        if indicator.is_cumulative:
            carried[indicator.id] = value

        numerator = denominator = None
        if indicator.requires_numerator_denominator:
            denominator = float(rng.randint(400, 9000))
            numerator = round(denominator * min(value, 100) / 100.0, 0)
            value = round(numerator / denominator * 100.0, 1)
        elif unit == IndicatorUnit.PERCENT:
            value = round(value, 1)
        elif unit in {IndicatorUnit.CURRENCY_NGN_M, IndicatorUnit.RATIO, IndicatorUnit.SCORE}:
            value = round(value, 2)
        else:
            value = float(round(value))

        if indicator.code in forced_invalid:
            value = forced_invalid[indicator.code]

        rows.append(
            [
                indicator.number,
                indicator.code,
                indicator.name,
                indicator.unit,
                value,
                numerator,
                denominator,
                "total",
                "total",
                "State EMIS / project MIS",
                "",
            ]
        )

        if issue == "duplicate" and position == 9:
            rows.append(rows[-1][:])  # same indicator reported twice


    return rows


def to_xlsx(state: State, period: ReportingPeriod, rows: list[list]) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "AGILE Reporting"
    sheet.append(["AGILE Standardised State Reporting Template"])
    sheet.append(["State:", state.name])
    sheet.append(["Cohort:", state.cohort.name if state.cohort else ""])
    sheet.append(["Reporting Period:", period.code])
    sheet.append([])
    sheet.append(HEADERS)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def to_csv(state: State, period: ReportingPeriod, rows: list[list]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["AGILE Standardised State Reporting Template"])
    writer.writerow(["State:", state.name])
    writer.writerow(["Reporting Period:", period.code])
    writer.writerow([])
    writer.writerow(HEADERS)
    for row in rows:
        writer.writerow(["" if cell is None else cell for cell in row])
    return buffer.getvalue().encode("utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate AGILE demonstration data")
    parser.add_argument("--periods", nargs="*", help="Explicit period codes, e.g. 2026-Q1")
    parser.add_argument("--year", type=int, help="Generate for every period of this fiscal year")
    parser.add_argument("--period-type", default="QUARTERLY")
    parser.add_argument("--format", choices=["xlsx", "csv"], default="xlsx")
    parser.add_argument("--states", nargs="*", help="Limit to these state codes")
    parser.add_argument(
        "--skip-approval",
        action="store_true",
        help="Leave submissions in VALIDATED status instead of approving them",
    )
    args = parser.parse_args()

    configure_logging(settings.log_level, as_json=False)
    init_db()

    with session_scope() as db:
        indicators = reference.active_indicators(db)
        states = reference.active_states(db)
        if args.states:
            wanted = {code.upper() for code in args.states}
            states = [state for state in states if state.code in wanted]
        if not indicators or not states:
            raise SystemExit("Reference data is missing. Run `python -m scripts.seed` first.")

        if args.periods:
            periods = [reference.get_period_by_code(db, code) for code in args.periods]
        elif args.year:
            periods = [
                period
                for period in reference.ordered_periods(db, args.period_type)
                if period.fiscal_year == args.year
            ]
        else:
            periods = reference.ordered_periods(db, args.period_type, limit=4)
        if not periods:
            raise SystemExit("No matching reporting periods. Run `python -m scripts.seed` first.")

        periods = sorted(periods, key=lambda p: p.sort_key)
        created_targets = build_targets(db, periods, indicators, states)
        logger.info("targets ready", extra={"created_count": created_targets})

        target_lookup: dict[tuple[int, int], float] = {}
        for row in db.scalars(
            select(Target).where(
                Target.period_id.in_([p.id for p in periods]),
                Target.level == str(TargetLevel.STATE),
            )
        ):
            target_lookup[(row.state_id, row.indicator_id)] = row.target_value

        carried: dict[str, dict[int, float]] = {state.code: {} for state in states}
        ingested = approved = rejected = 0

        for period in periods:
            for state in states:
                targets = {
                    indicator.id: target_lookup.get((state.id, indicator.id))
                    for indicator in indicators
                }
                targets = {k: v for k, v in targets.items() if v is not None}
                rows = generate_rows(state, period, indicators, targets, carried[state.code])
                payload = (
                    to_xlsx(state, period, rows)
                    if args.format == "xlsx"
                    else to_csv(state, period, rows)
                )
                filename = f"{state.code}_{period.code}.{args.format}"

                try:
                    submission, _, _ = ingest_upload(
                        db,
                        content=payload,
                        filename=filename,
                        state_code=state.code,
                        period_code=period.code,
                        auto_approve=False,
                        allow_duplicate=True,
                        notes="Generated by scripts.demo",
                    )
                except Exception as exc:  # noqa: BLE001 - keep generating the rest
                    logger.warning(
                        "ingest failed",
                        extra={"state": state.code, "period": period.code, "error": str(exc)},
                    )
                    continue

                # Simulate realistic submission timing relative to the deadline.
                rng = random.Random(f"{state.code}-{period.code}-timing")
                if QUALITY_ISSUES.get(state.code) == "late":
                    offset = rng.randint(6, 21)
                else:
                    offset = rng.choice([-9, -6, -4, -3, -2, -1, 0, 0, 1, 3])
                submission.uploaded_at = submission.uploaded_at.replace(
                    year=period.due_date.year, month=period.due_date.month, day=period.due_date.day
                ) + timedelta(days=offset)
                db.flush()
                run_validation(db, submission)
                ingested += 1

                if submission.status == SubmissionStatus.REJECTED:
                    rejected += 1
                elif not args.skip_approval:
                    approve_submission(db, submission, None, "Approved by the demo generator")
                    approved += 1

            logger.info(
                "period complete",
                extra={"period": period.code, "ingested": ingested, "approved": approved},
            )

    print(
        f"\nDemo data ready: {ingested} submission(s) ingested, {approved} approved, "
        f"{rejected} rejected by the quality gate across {len(periods)} period(s)."
    )


if __name__ == "__main__":
    main()
