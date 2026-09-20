"""Re-run validation over periods already in the database.

Needed after an upgrade that adds or changes validation rules. Figures already
loaded were checked against the rules in force when they arrived; this puts
them through the current set, settles each state's fitness verdict once the
national picture is whole, and leaves the figures themselves untouched.

Nothing is changed except findings, labels and verdicts. A figure's value is
only ever changed through the query workflow.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.core.logging_config import configure_logging  # noqa: E402
from app.db.session import init_db, session_scope  # noqa: E402
from app.models import ReportingPeriod, Submission  # noqa: E402
from app.services import reference  # noqa: E402
from app.services.ingestion.pipeline import revalidate_period  # noqa: E402


def _periods_with_data(db) -> list[ReportingPeriod]:
    """Every period that actually carries a current submission, oldest first."""
    period_ids = {
        row
        for row in db.scalars(
            select(Submission.period_id).where(Submission.is_current.is_(True))
        )
    }
    if not period_ids:
        return []
    return list(
        db.scalars(
            select(ReportingPeriod)
            .where(ReportingPeriod.id.in_(period_ids))
            .order_by(ReportingPeriod.fiscal_year, ReportingPeriod.sequence)
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-run validation over periods already loaded",
        epilog=(
            "Examples:\n"
            "  python -m scripts.revalidate                 # every period with data\n"
            "  python -m scripts.revalidate 2026-Q1 2026-Q2 # just these two"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "periods",
        nargs="*",
        help="Period codes to re-validate. Omit for every period with data.",
    )
    args = parser.parse_args()

    configure_logging("WARNING", as_json=False)
    init_db()

    with session_scope() as db:
        if args.periods:
            periods = [
                reference.get_period_by_code(db, code) for code in args.periods
            ]
        else:
            periods = _periods_with_data(db)

        if not periods:
            raise SystemExit(
                "No periods carry a current submission, so there is nothing to "
                "re-validate. Load a period first."
            )

        for period in periods:
            result = revalidate_period(db, period)
            verdicts = result.get("verdicts") or {}
            summary = ", ".join(
                f"{count} {verdict.lower()}" for verdict, count in sorted(verdicts.items())
            )
            print(
                f"{period.code}: {result['submissions']} submissions, "
                f"{result['findings']} findings"
                + (f" -> {summary}" if summary else "")
            )

    print("\nRe-validation complete. No reported figure was changed.")


if __name__ == "__main__":
    main()
