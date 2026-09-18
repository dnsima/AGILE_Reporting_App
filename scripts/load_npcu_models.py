"""Load the NPCU quarterly analysis models into the platform.

Reads the Q1 and Q2 2026 results-framework workbooks and pushes each state's
figures through the real ingestion pipeline, so parsing, validation, DQA
scoring and approval all run exactly as they would on an upload.

Q1 is translated onto the Q2 codes using the NPCU's own crosswalk before it is
loaded. Retained and moved indicators carry across directly; merged indicators
have their Q1 parts summed, which is what the crosswalk's Merge Validation
sheet does by hand. Transformed, dropped and new indicators have no
like-for-like basis and are not translated, so no false period-over-period
movement is ever reported against them.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import load_workbook
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.enums import COMPARABLE_DISPOSITIONS, IndicatorDisposition, TargetLevel  # noqa: E402
from app.core.logging_config import configure_logging, get_logger  # noqa: E402
from app.db.session import init_db, session_scope  # noqa: E402
from app.models import Indicator, IndicatorLink, Target  # noqa: E402
from app.schemas.ingestion import ManualSubmission, ManualValueEntry  # noqa: E402
from app.services import reference  # noqa: E402
from app.services.ingestion.mapper import to_boolean, to_number  # noqa: E402
from app.services.ingestion.pipeline import approve_submission, ingest_manual  # noqa: E402

logger = get_logger("load")

UPLOADS = Path("/root/.claude/uploads/2e5b36ef-43ca-515f-bb8f-925304562963")
Q2_MODEL = UPLOADS / "d0b593e1-AGILE_Q2_2026_Analysis_Model_Flagged.xlsx"
Q1_MODEL = UPLOADS / "d693b3b0-AGILE_Q1_2026_Analysis_Model_v3_5.xlsx"
CROSSWALK = UPLOADS / "d2398a92-AGILE_Q1_vs_Q2_2026_Model_Comparison.xlsx"

SHEETS = ["PDO", "Component 1", "Component 2", "Component 3"]
CODE_PATTERN = re.compile(r"^(PDO|C\d)-")


def read_model(path: Path) -> tuple[dict[str, dict[str, object]], list[str]]:
    """``{old_code: {state_name: raw_value}}`` plus the state column order."""
    workbook = load_workbook(path, data_only=True)
    values: dict[str, dict[str, object]] = {}
    states: list[str] = []

    for sheet in SHEETS:
        if sheet not in workbook.sheetnames:
            continue
        worksheet = workbook[sheet]
        header = list(next(worksheet.iter_rows(min_row=4, max_row=4, values_only=True)))
        columns = {
            index: name
            for index, name in enumerate(header)
            if name and 3 <= index and str(name).upper() not in {"NATIONAL\nTOTAL", "TARGET", "% OF TARGET"}
            and not str(name).upper().startswith(("NATIONAL", "TARGET", "%"))
        }
        states = states or [str(name) for name in columns.values()]

        seen: set[str] = set()
        for row in worksheet.iter_rows(min_row=5, values_only=True):
            code = str(row[0] or "").strip()
            if not CODE_PATTERN.match(code) or code in seen:
                continue
            seen.add(code)
            values[code] = {str(name): row[index] for index, name in columns.items()}
    return values, states


def read_crosswalk() -> dict[str, list[str]]:
    """``{q2_old_code: [q1_codes that feed it]}`` for comparable dispositions."""
    workbook = load_workbook(CROSSWALK, data_only=True)
    mapping: dict[str, list[str]] = defaultdict(list)
    for row in workbook["Crosswalk"].iter_rows(min_row=3, values_only=True):
        q1_code = str(row[0] or "").strip()
        disposition = str(row[2] or "").strip().upper()
        q2_code = str(row[3] or "").strip()
        if not CODE_PATTERN.match(q1_code) or not disposition:
            continue
        if disposition not in {d.value for d in IndicatorDisposition}:
            continue
        if IndicatorDisposition(disposition) not in COMPARABLE_DISPOSITIONS:
            continue
        if not CODE_PATTERN.match(q2_code):
            continue
        mapping[q2_code].append(q1_code)
    return dict(mapping)


def record_lineage(db) -> int:
    """Persist the full crosswalk, including the non-comparable dispositions."""
    workbook = load_workbook(CROSSWALK, data_only=True)
    existing = {
        (row.predecessor_code, row.successor_code)
        for row in db.scalars(select(IndicatorLink))
    }
    created = 0
    for row in workbook["Crosswalk"].iter_rows(min_row=3, values_only=True):
        q1_code = str(row[0] or "").strip()
        disposition = str(row[2] or "").strip().upper()
        q2_code = str(row[3] or "").strip()
        if not CODE_PATTERN.match(q1_code) or disposition not in {d.value for d in IndicatorDisposition}:
            continue
        successor = q2_code if CODE_PATTERN.match(q2_code) else None
        if (q1_code, successor) in existing:
            continue
        db.add(
            IndicatorLink(
                predecessor_code=q1_code,
                successor_code=successor,
                disposition=disposition,
                is_comparable=IndicatorDisposition(disposition) in COMPARABLE_DISPOSITIONS,
                effective_from_period="2026-Q2",
                note=str(row[5] or "")[:2000] or None,
            )
        )
        existing.add((q1_code, successor))
        created += 1
    db.flush()
    return created


def load_targets(db, period_code: str) -> int:
    """State-level targets are not published per state, so load national ones."""
    workbook = load_workbook(Q2_MODEL, data_only=True)
    worksheet = workbook["National Summary"]
    period = reference.get_period_by_code(db, period_code)
    by_legacy = {i.legacy_code: i for i in db.scalars(select(Indicator)) if i.legacy_code}

    created = 0
    for row in worksheet.iter_rows(min_row=4, values_only=True):
        code = str(row[0] or "").strip()
        indicator = by_legacy.get(code)
        target = row[22] if len(row) > 22 else None
        if indicator is None or not isinstance(target, (int, float)):
            continue
        existing = db.scalar(
            select(Target).where(
                Target.indicator_id == indicator.id,
                Target.period_id == period.id,
                Target.level == str(TargetLevel.NATIONAL),
                Target.state_id.is_(None),
            )
        )
        if existing is None:
            db.add(
                Target(
                    indicator_id=indicator.id,
                    period_id=period.id,
                    level=str(TargetLevel.NATIONAL),
                    target_value=float(target),
                    source="AGILE Q2 2026 analysis model",
                )
            )
            created += 1
        else:
            existing.target_value = float(target)
    db.flush()
    return created


def build_entries(
    db, old_code_values: dict[str, object], indicator: Indicator
) -> ManualValueEntry | None:
    raw = old_code_values
    parse = to_boolean if indicator.unit == "BOOLEAN" else to_number
    value = parse(raw)
    if value is None:
        return None
    return ManualValueEntry(indicator_code=indicator.code, value=value)


def load_period(
    db,
    model: Path,
    period_code: str,
    submitted_on: datetime,
    translate: dict[str, list[str]] | None,
) -> tuple[int, int, int]:
    values, state_names = read_model(model)
    indicators = list(db.scalars(select(Indicator)))
    by_legacy = {i.legacy_code: i for i in indicators if i.legacy_code}

    ingested = approved = rejected = 0
    for state_name in state_names:
        state = reference.get_state_by_code(db, state_name, required=False)
        if state is None:
            logger.warning("unknown state column", extra={"column": state_name})
            continue

        entries: list[ManualValueEntry] = []
        for old_code, indicator in by_legacy.items():
            if not indicator.is_reported:
                continue  # derived rows are computed, never collected

            if translate is None:
                cell = values.get(old_code, {}).get(state_name)
            else:
                # Q1: gather whichever Q1 indicators feed this Q2 code.
                parts = translate.get(old_code, [])
                numbers = [
                    to_number(values.get(part, {}).get(state_name)) for part in parts
                ]
                numbers = [n for n in numbers if n is not None]
                if not numbers:
                    continue
                cell = sum(numbers) if len(numbers) > 1 else numbers[0]

            entry = build_entries(db, cell, indicator)
            if entry is not None:
                entries.append(entry)

        if not entries:
            continue

        submission, _diagnostics, _summary = ingest_manual(
            db,
            ManualSubmission(
                state_code=state.code,
                period_code=period_code,
                values=entries,
                notes=f"Loaded from {model.name}",
            ),
        )
        submission.uploaded_at = submitted_on
        submission.source_file_name = model.name
        db.flush()

        from app.services.validation import run_validation

        run_validation(db, submission)
        ingested += 1
        if submission.status == "REJECTED":
            rejected += 1
        else:
            approve_submission(db, submission, None, "Loaded from the NPCU analysis model")
            approved += 1

    return ingested, approved, rejected


def main() -> None:
    parser = argparse.ArgumentParser(description="Load the NPCU quarterly models")
    parser.add_argument("--skip-q1", action="store_true")
    args = parser.parse_args()

    configure_logging("WARNING", as_json=False)
    init_db()

    with session_scope() as db:
        links = record_lineage(db)
        targets = load_targets(db, "2026-Q2")
        print(f"lineage links recorded: {links}")
        print(f"national targets loaded: {targets}")

        if not args.skip_q1:
            crosswalk = read_crosswalk()
            print(f"crosswalk: {len(crosswalk)} Q2 codes have a comparable Q1 basis")
            i, a, r = load_period(
                db, Q1_MODEL, "2026-Q1",
                datetime(2026, 4, 14, tzinfo=timezone.utc), translate=crosswalk,
            )
            print(f"Q1 2026: {i} ingested, {a} approved, {r} rejected")

        i, a, r = load_period(
            db, Q2_MODEL, "2026-Q2",
            datetime(2026, 7, 14, tzinfo=timezone.utc), translate=None,
        )
        print(f"Q2 2026: {i} ingested, {a} approved, {r} rejected")


if __name__ == "__main__":
    main()
