"""Load one quarter's Kobo backend export.

This is how a reporting cycle starts. States file the AGILE Results Framework
form on Kobo; the NPCU downloads the backend dataset for the quarter; this
puts it through the platform -- one submission per state, validated on its
own, with every finding raised as a query against the state that reported it.

    python -m scripts.load_kobo --period 2026-Q2 --file Q2_2026_Backend.xlsx

Nothing is overwritten quietly. A state that already has a current return for
the period gets a new version, the old one is retired, and any query still
open against it follows the figure onto the replacement -- a state cannot
clear a finding by re-filing over it.

Start with ``--dry-run`` to see what the file contains before loading it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.core.errors import IngestionError  # noqa: E402
from app.core.logging_config import configure_logging  # noqa: E402
from app.db.session import init_db, session_scope  # noqa: E402
from app.models import Indicator, Submission  # noqa: E402
from app.services import reference  # noqa: E402
from app.services.ingestion import kobo  # noqa: E402
from app.services.ingestion.pipeline import approve_submission, ingest_kobo_export  # noqa: E402


def _resolve(path_text: str) -> Path:
    path = Path(path_text).expanduser()
    if path.is_file():
        return path
    for folder in (Path("data"), Path("data/uploads"), Path("storage/uploads")):
        candidate = folder / path.name
        if candidate.is_file():
            return candidate
    raise SystemExit(
        f"Could not find '{path_text}'.\n"
        "Pass the full path to the file, for example:\n"
        "  python -m scripts.load_kobo --period 2026-Q2 "
        "--file C:\\AGILE\\Q2_2026_AGILE_PF_Backend_Dataset.xlsx"
    )


def _dry_run(path: Path, period_code: str) -> None:
    init_db()
    with session_scope() as db:
        reference.get_period_by_code(db, period_code)
        indicators = list(db.scalars(select(Indicator).where(Indicator.is_active.is_(True))))
        export = kobo.read_export(path.read_bytes(), path.name, indicators)

        print(f"{path.name}: {export.row_count} rows, {len(export.returns)} states")
        print(f"  {len(export.resolution.matched)} question columns resolved to indicators")
        if export.resolution.relaxed:
            print(f"  {len(export.resolution.relaxed)} matched only after ignoring punctuation:")
            for header in export.resolution.relaxed:
                print(f"    - {header}")
        if export.resolution.absent:
            print(
                f"  {len(export.resolution.absent)} reported indicators the form "
                f"never asks about: {', '.join(export.resolution.absent)}"
            )
        if export.unknown_sections:
            print(f"  unrecognised form sections: {', '.join(export.unknown_sections)}")
        print()

        for ret in export.returns:
            state = reference.get_state_by_code(db, ret.state_name, required=False)
            marker = " " if state else "?"
            notes = []
            if state is None:
                notes.append("NOT A PARTICIPATING STATE")
            if ret.missing_sections:
                notes.append("no " + "/".join(ret.missing_sections) + " section")
            if ret.unreadable:
                notes.append(f"{len(ret.unreadable)} unreadable answer(s)")
            suffix = ("  <- " + "; ".join(notes)) if notes else ""
            print(f" {marker} {ret.state_name:<12} {len(ret.values):>3} answers{suffix}")

        print("\nNothing was loaded. Drop --dry-run to load it.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load a quarter's Kobo backend export into the platform",
        epilog=(
            "Examples:\n"
            "  python -m scripts.load_kobo --period 2026-Q2 --file Q2_2026_Backend.xlsx --dry-run\n"
            "  python -m scripts.load_kobo --period 2026-Q2 --file Q2_2026_Backend.xlsx\n"
            "  python -m scripts.load_kobo --period 2026-Q2 --file Q2.xlsx --states KN,KD"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--file", required=True, help="The Kobo backend export (.xlsx or .csv)")
    parser.add_argument(
        "--period",
        required=True,
        help=(
            "The reporting period these returns belong to, e.g. 2026-Q2. Required: "
            "states file in the fortnight after a quarter ends, so the dates in the "
            "file would put half the returns in the wrong quarter."
        ),
    )
    parser.add_argument(
        "--states",
        help="Comma-separated state codes to load. Omit to load every state in the file.",
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help=(
            "Approve each loaded return. Findings are still raised as queries; "
            "approval means the figures enter the national totals now rather than "
            "waiting on review."
        ),
    )
    parser.add_argument(
        "--no-queries",
        action="store_true",
        help="Validate without raising queries. For a trial load you intend to discard.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and report on the file without loading anything.",
    )
    args = parser.parse_args()

    configure_logging("WARNING", as_json=False)
    path = _resolve(args.file)

    if args.dry_run:
        _dry_run(path, args.period)
        return

    only = {code.strip() for code in args.states.split(",")} if args.states else None

    init_db()
    with session_scope() as db:
        try:
            result = ingest_kobo_export(
                db,
                content=path.read_bytes(),
                filename=path.name,
                period_code=args.period,
                only_states=only,
                raise_queries=not args.no_queries,
            )
        except IngestionError as exc:
            raise SystemExit(f"\nThe export could not be loaded.\n\n{exc}\n") from exc

        print(f"{path.name} -> {result.period_code}")
        print(f"  {result.rows} rows, {result.columns_matched} indicator columns\n")

        for row in result.results:
            if not row.loaded:
                print(f"  -  {row.state_name:<12} skipped: {row.skipped_reason}")
                continue
            notes = []
            if row.missing_sections:
                notes.append("no " + "/".join(row.missing_sections))
            if row.unreadable:
                notes.append(f"{len(row.unreadable)} unreadable")
            suffix = ("  (" + "; ".join(notes) + ")") if notes else ""
            print(
                f"  ok {row.state_code:<4} {row.state_name:<12} "
                f"{row.values:>3} figures, {row.findings:>3} findings, "
                f"{row.open_queries:>3} open queries{suffix}"
            )

        if args.approve:
            approved = 0
            for row in result.loaded:
                submission = db.get(Submission, row.submission_id)
                if submission is not None and submission.status != "REJECTED":
                    approve_submission(db, submission, None, "Loaded from the Kobo backend export")
                    approved += 1
            print(f"\n  {approved} returns approved.")

        print(
            f"\nLoaded {len(result.loaded)} of {len(result.results)} states: "
            f"{result.findings} findings raised, "
            f"{result.open_queries} queries now open (new and carried forward)."
        )
        if result.absent_indicators:
            print(
                f"\nThe form does not ask about: {', '.join(result.absent_indicators)}. "
                "Those indicators will read as unreported for every state."
            )
        if result.relaxed_columns:
            print(
                "\nSome columns matched only after ignoring punctuation. The form and "
                "the results framework are drifting apart; worth reconciling:"
            )
            for header in result.relaxed_columns:
                print(f"  - {header}")
        print("\nOpen the Queries page to work the findings with the states.")


if __name__ == "__main__":
    main()
