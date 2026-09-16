"""Reads uploaded state templates into a plain tabular structure.

Handles ``.xlsx``/``.xlsm``/``.xls`` and ``.csv``/``.tsv``. State templates in
practice carry a cover block ("State: Kano", "Reporting period: Q2 2025") above
the real header row, so the parser scans the first rows to find the header and
harvests any state/period hints it passes on the way.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app.core.errors import IngestionError
from app.core.logging_config import get_logger

logger = get_logger(__name__)

EXCEL_SUFFIXES = {".xlsx", ".xlsm", ".xltx", ".xls"}
CSV_SUFFIXES = {".csv", ".txt"}
TSV_SUFFIXES = {".tsv"}

MAX_HEADER_SCAN_ROWS = 15

#: Header tokens that signal "this row is the real header row".
HEADER_SIGNALS = {
    "indicator",
    "kpi",
    "value",
    "achievement",
    "actual",
    "numerator",
    "denominator",
    "target",
    "code",
    "number",
    "result",
    "reported",
}

#: ``|`` is the separator used when a row's cells are joined for scanning, so
#: both patterns are allowed to step over it between the label and the value.
STATE_HINT = re.compile(
    r"\b(state|sub[- ]?national)\b\s*[:\-]\s*\|?\s*(?P<value>[A-Za-z][A-Za-z .'-]{1,39})",
    re.I,
)
PERIOD_HINT = re.compile(
    r"\b(reporting\s+period|period|quarter|reporting\s+cycle)\b\s*[:\-]\s*\|?\s*"
    r"(?P<value>[A-Za-z0-9][A-Za-z0-9 /\-]{1,39})",
    re.I,
)


@dataclass
class ParsedSheet:
    """A template reduced to headers plus rows of raw cell values."""

    headers: list[str]
    rows: list[dict[str, Any]]
    sheet_name: str | None = None
    header_row: int | None = None
    detected_state: str | None = None
    detected_period: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.rows)


def _suffix(filename: str) -> str:
    return ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""


def _clean_header(value: Any, index: int) -> str:
    text = "" if value is None else str(value).strip()
    if not text or text.lower().startswith("unnamed:") or text.lower() == "nan":
        return f"column_{index + 1}"
    return re.sub(r"\s+", " ", text)


def _row_header_score(values: list[Any]) -> int:
    score = 0
    for cell in values:
        if cell is None:
            continue
        text = str(cell).strip().lower()
        if not text or text == "nan":
            continue
        if any(signal in text for signal in HEADER_SIGNALS):
            score += 2
        elif len(text) <= 40 and not _looks_numeric(text):
            score += 1
    return score


def _looks_numeric(text: str) -> bool:
    try:
        float(text.replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return False
    return True


def _scan_hints(frame: pd.DataFrame, upto: int) -> tuple[str | None, str | None, list[str]]:
    state_hint: str | None = None
    period_hint: str | None = None
    notes: list[str] = []
    for row_index in range(min(upto, len(frame))):
        joined = " | ".join(
            str(cell) for cell in frame.iloc[row_index].tolist() if cell is not None and str(cell) != "nan"
        )
        if not joined.strip():
            continue
        if state_hint is None:
            match = STATE_HINT.search(joined)
            if match:
                state_hint = match.group("value").strip(" .|")
        if period_hint is None:
            match = PERIOD_HINT.search(joined)
            if match:
                period_hint = match.group("value").strip(" .|")
    if state_hint:
        notes.append(f"State detected from the file header block: '{state_hint}'")
    if period_hint:
        notes.append(f"Reporting period detected from the file header block: '{period_hint}'")
    return state_hint, period_hint, notes


def _frame_to_sheet(raw: pd.DataFrame, sheet_name: str | None) -> ParsedSheet:
    raw = raw.dropna(axis=0, how="all")
    if raw.empty:
        raise IngestionError("The uploaded file contains no data rows")

    # Dropping blank rows renumbers the frame, so keep the original positions to
    # report row numbers a user can find in their own spreadsheet.
    original_positions = list(raw.index)
    raw = raw.reset_index(drop=True)

    scan_limit = min(MAX_HEADER_SCAN_ROWS, len(raw))
    scores = [(_row_header_score(raw.iloc[i].tolist()), i) for i in range(scan_limit)]
    best_score, header_row = max(scores, key=lambda pair: (pair[0], -pair[1]))
    if best_score == 0:
        header_row = 0

    detected_state, detected_period, warnings = _scan_hints(raw, header_row)

    header_values = raw.iloc[header_row].tolist()
    headers = [_clean_header(value, index) for index, value in enumerate(header_values)]
    if len(set(headers)) != len(headers):
        seen: dict[str, int] = {}
        deduped = []
        for header in headers:
            seen[header] = seen.get(header, 0) + 1
            deduped.append(header if seen[header] == 1 else f"{header} ({seen[header]})")
        headers = deduped

    body = raw.iloc[header_row + 1 :].reset_index(drop=True)
    rows: list[dict[str, Any]] = []
    for offset, (_, series) in enumerate(body.iterrows()):
        values = series.tolist()
        record = {
            headers[i]: (values[i] if i < len(values) else None) for i in range(len(headers))
        }
        if all(value is None or str(value).strip() in {"", "nan"} for value in record.values()):
            continue
        position = header_row + 1 + offset
        record["__row__"] = (
            original_positions[position] + 1  # 1-based, as spreadsheets number rows
            if position < len(original_positions)
            else position + 1
        )
        rows.append(record)

    if not rows:
        raise IngestionError("No data rows were found beneath the header row")

    return ParsedSheet(
        headers=headers,
        rows=rows,
        sheet_name=sheet_name,
        header_row=(
            original_positions[header_row] + 1
            if header_row < len(original_positions)
            else header_row + 1
        ),
        detected_state=detected_state,
        detected_period=detected_period,
        warnings=warnings,
    )


def parse_upload(content: bytes, filename: str) -> ParsedSheet:
    """Parse an uploaded template into a :class:`ParsedSheet`."""
    suffix = _suffix(filename)
    if not content:
        raise IngestionError("The uploaded file is empty")

    try:
        if suffix in EXCEL_SUFFIXES:
            book = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None, dtype=object)
            if not book:
                raise IngestionError("The workbook contains no sheets")
            # Prefer the sheet that looks most like a data sheet.
            best_name, best_sheet, best_score = None, None, -1
            for name, frame in book.items():
                frame = frame.dropna(axis=0, how="all")
                if frame.empty:
                    continue
                score = max(
                    (_row_header_score(frame.iloc[i].tolist()) for i in range(min(10, len(frame)))),
                    default=0,
                )
                score += min(len(frame), 200) / 200
                if score > best_score:
                    best_name, best_sheet, best_score = name, frame, score
            if best_sheet is None:
                raise IngestionError("Every sheet in the workbook is empty")
            return _frame_to_sheet(best_sheet, best_name)

        if suffix in CSV_SUFFIXES or suffix in TSV_SUFFIXES or suffix == "":
            separator = "\t" if suffix in TSV_SUFFIXES else None
            frame = pd.read_csv(
                io.BytesIO(content),
                header=None,
                dtype=object,
                sep=separator,
                engine="python",
                skip_blank_lines=True,
            )
            return _frame_to_sheet(frame, None)

    except IngestionError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface any parser failure cleanly
        logger.warning("template parse failed", extra={"source_file": filename, "error": str(exc)})
        raise IngestionError(f"Could not read '{filename}': {exc}") from exc

    raise IngestionError(
        f"Unsupported file type '{suffix or 'unknown'}'. Upload .xlsx, .xls, .csv or .tsv."
    )
