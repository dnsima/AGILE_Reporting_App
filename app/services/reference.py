"""Reference-data lookups and reporting-period calendar generation."""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import PeriodType
from app.core.errors import NotFoundError, ValidationError
from app.models import Cohort, Indicator, IndicatorCategory, ReportingPeriod, State

MONTH_NAMES = list(calendar.month_name)


# --------------------------------------------------------------------------
# Lookups
# --------------------------------------------------------------------------
def get_state_by_code(db: Session, code: str, *, required: bool = True) -> State | None:
    if not code:
        if required:
            raise ValidationError("A state code is required")
        return None
    normalized = code.strip().upper()
    state = db.scalar(select(State).where(func.upper(State.code) == normalized))
    if state is None:
        state = db.scalar(select(State).where(func.upper(State.name) == normalized))
    if state is None and required:
        raise NotFoundError(f"Unknown state '{code}'")
    return state


def get_period_by_code(db: Session, code: str, *, required: bool = True) -> ReportingPeriod | None:
    if not code:
        if required:
            raise ValidationError("A reporting period code is required")
        return None
    period = db.scalar(
        select(ReportingPeriod).where(func.upper(ReportingPeriod.code) == code.strip().upper())
    )
    if period is None and required:
        raise NotFoundError(f"Unknown reporting period '{code}'")
    return period


def get_indicator_by_code(db: Session, code: str, *, required: bool = True) -> Indicator | None:
    normalized = (code or "").strip().upper()
    if not normalized:
        if required:
            raise ValidationError("An indicator code is required")
        return None
    indicator = db.scalar(select(Indicator).where(func.upper(Indicator.code) == normalized))
    if indicator is None and normalized.isdigit():
        indicator = db.scalar(select(Indicator).where(Indicator.number == int(normalized)))
    if indicator is None and required:
        raise NotFoundError(f"Unknown indicator '{code}'")
    return indicator


def get_cohort_by_code(db: Session, code: str, *, required: bool = True) -> Cohort | None:
    normalized = (code or "").strip().upper()
    if not normalized:
        if required:
            raise ValidationError("A cohort code is required")
        return None
    cohort = db.scalar(select(Cohort).where(func.upper(Cohort.code) == normalized))
    if cohort is None and required:
        raise NotFoundError(f"Unknown cohort '{code}'")
    return cohort


def _states(db: Session, cohort_code: str | None, *, reporting_only: bool):
    stmt = select(State).where(State.is_active.is_(True)).order_by(State.name)
    if reporting_only:
        stmt = stmt.where(State.is_reporting.is_(True))
    if cohort_code:
        stmt = stmt.join(Cohort).where(func.upper(Cohort.code) == cohort_code.strip().upper())
    return list(db.scalars(stmt))


def active_states(db: Session, cohort_code: str | None = None) -> list[State]:
    """States expected to file returns -- the denominator for reporting rates.

    A participating state that has not started reporting is not counted as a
    non-reporter, because it was never asked.
    """
    return _states(db, cohort_code, reporting_only=True)


def participating_states(db: Session, cohort_code: str | None = None) -> list[State]:
    """Every state in the programme, including those not yet reporting.

    This is the project's real footprint, and what a report's scope should
    claim.
    """
    return _states(db, cohort_code, reporting_only=False)


def active_indicators(
    db: Session,
    *,
    category_code: str | None = None,
    indicator_codes: list[str] | None = None,
) -> list[Indicator]:
    stmt = select(Indicator).where(Indicator.is_active.is_(True)).order_by(Indicator.number)
    if category_code:
        stmt = stmt.join(IndicatorCategory).where(
            func.upper(IndicatorCategory.code) == category_code.strip().upper()
        )
    if indicator_codes:
        wanted = {code.strip().upper() for code in indicator_codes}
        stmt = stmt.where(func.upper(Indicator.code).in_(wanted))
    return list(db.scalars(stmt))


def latest_period(db: Session, period_type: str | None = None) -> ReportingPeriod | None:
    """Most recent period that has closed, falling back to the newest defined."""
    stmt = select(ReportingPeriod)
    if period_type:
        stmt = stmt.where(ReportingPeriod.period_type == period_type)
    periods = list(db.scalars(stmt))
    if not periods:
        return None
    today = date.today()
    closed = [p for p in periods if p.end_date <= today]
    pool = closed or periods
    return max(pool, key=lambda p: (p.end_date, p.sort_key))


def ordered_periods(
    db: Session, period_type: str | None = None, limit: int | None = None
) -> list[ReportingPeriod]:
    stmt = select(ReportingPeriod)
    if period_type:
        stmt = stmt.where(ReportingPeriod.period_type == period_type)
    periods = sorted(db.scalars(stmt), key=lambda p: p.sort_key)
    return periods[-limit:] if limit else periods


def preceding_period(db: Session, period: ReportingPeriod) -> ReportingPeriod | None:
    """The period of the same family that immediately precedes ``period``."""
    same_family = ordered_periods(db, period.period_type)
    previous: ReportingPeriod | None = None
    for candidate in same_family:
        if candidate.id == period.id:
            return previous
        previous = candidate
    return None


# --------------------------------------------------------------------------
# Period calendar generation
# --------------------------------------------------------------------------
def _period_bounds(period_type: PeriodType, year: int, sequence: int) -> tuple[date, date, str, str]:
    if period_type == PeriodType.MONTHLY:
        if not 1 <= sequence <= 12:
            raise ValidationError("Monthly periods need a sequence between 1 and 12")
        start = date(year, sequence, 1)
        end = date(year, sequence, calendar.monthrange(year, sequence)[1])
        return start, end, f"{year}-M{sequence:02d}", f"{MONTH_NAMES[sequence]} {year}"

    if period_type == PeriodType.QUARTERLY:
        if not 1 <= sequence <= 4:
            raise ValidationError("Quarterly periods need a sequence between 1 and 4")
        first_month = (sequence - 1) * 3 + 1
        last_month = first_month + 2
        start = date(year, first_month, 1)
        end = date(year, last_month, calendar.monthrange(year, last_month)[1])
        window = f"{MONTH_NAMES[first_month][:3]}-{MONTH_NAMES[last_month][:3]}"
        return start, end, f"{year}-Q{sequence}", f"Q{sequence} {year} ({window})"

    if period_type == PeriodType.SEMI_ANNUAL:
        if not 1 <= sequence <= 2:
            raise ValidationError("Semi-annual periods need a sequence of 1 or 2")
        first_month = 1 if sequence == 1 else 7
        last_month = first_month + 5
        start = date(year, first_month, 1)
        end = date(year, last_month, calendar.monthrange(year, last_month)[1])
        window = f"{MONTH_NAMES[first_month][:3]}-{MONTH_NAMES[last_month][:3]}"
        return start, end, f"{year}-H{sequence}", f"H{sequence} {year} ({window})"

    if period_type == PeriodType.ANNUAL:
        return date(year, 1, 1), date(year, 12, 31), f"{year}-A", f"FY{year}"

    raise ValidationError(f"Unsupported period type '{period_type}'")


def ensure_period(
    db: Session,
    period_type: PeriodType,
    fiscal_year: int,
    sequence: int,
    *,
    due_days_after_close: int = 15,
    is_open: bool = True,
) -> ReportingPeriod:
    """Create the period if it does not exist; return it either way."""
    start, end, code, label = _period_bounds(period_type, fiscal_year, sequence)
    existing = db.scalar(select(ReportingPeriod).where(ReportingPeriod.code == code))
    if existing:
        return existing

    period = ReportingPeriod(
        code=code,
        label=label,
        period_type=str(period_type),
        fiscal_year=fiscal_year,
        sequence=sequence,
        start_date=start,
        end_date=end,
        due_date=end + timedelta(days=due_days_after_close),
        is_open=is_open,
    )
    db.add(period)
    db.flush()
    return period


def generate_year(
    db: Session,
    fiscal_year: int,
    period_types: list[PeriodType] | None = None,
    due_days_after_close: int = 15,
) -> list[ReportingPeriod]:
    """Generate the full reporting calendar for a fiscal year."""
    types = period_types or list(PeriodType)
    counts = {
        PeriodType.MONTHLY: 12,
        PeriodType.QUARTERLY: 4,
        PeriodType.SEMI_ANNUAL: 2,
        PeriodType.ANNUAL: 1,
    }
    created: list[ReportingPeriod] = []
    for period_type in types:
        for sequence in range(1, counts[PeriodType(period_type)] + 1):
            created.append(
                ensure_period(
                    db,
                    PeriodType(period_type),
                    fiscal_year,
                    sequence,
                    due_days_after_close=due_days_after_close,
                )
            )
    return created
