"""Correction sheets: the state-facing half of the change process.

After a validation meeting the NPCU issues each state a sheet containing only
the figures flagged against it -- never the whole return. Each row carries the
query reference, what was reported, what was flagged, and blank columns for the
corrected figure and the evidence behind it. The state fills it in and uploads
it; every row becomes a response on the query it names.

Restricting the sheet to flagged rows is the control: a figure nobody queried
cannot be changed by this route, so a correction sheet can never quietly alter
something that was never in question.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill, Protection
from openpyxl.utils import get_column_letter
from sqlalchemy.orm import Session

from app.core.errors import IngestionError, ValidationError
from app.core.logging_config import get_logger
from app.models import DataQuery, ReportingPeriod, State, User
from app.services import queries as query_service
from app.services.ingestion.mapper import to_number

logger = get_logger(__name__)

HEADERS = [
    "Query ref",
    "Indicator code",
    "Indicator",
    "Period of the figure",
    "Figure as reported",
    "What was flagged",
    "Your response",
    "Corrected figure",
    "Evidence provided",
    "Explanation",
]
WIDTHS = [11, 14, 46, 17, 16, 52, 15, 15, 34, 48]

LOCKED_FILL = PatternFill("solid", fgColor="F2F2F2")
ENTRY_FILL = PatternFill("solid", fgColor="FFF9E6")
HEADER_FILL = PatternFill("solid", fgColor="0F3D61")

#: What a state may put in "Your response".
CONFIRM_TOKENS = {"confirm", "confirmed", "as reported", "no change", "stands"}
CORRECT_TOKENS = {"correct", "corrected", "restate", "restated", "change", "amend"}

QUERY_REF = re.compile(r"Q-(\d+)")


def _ref(query: DataQuery) -> str:
    return f"Q-{query.id}"


# --------------------------------------------------------------------------
# Building the sheet
# --------------------------------------------------------------------------
def build_correction_sheet(
    db: Session,
    state: State,
    period: ReportingPeriod,
    *,
    queries: list[DataQuery] | None = None,
) -> bytes:
    """Produce the state's correction sheet for one reporting period."""
    rows = queries if queries is not None else query_service.open_queries(
        db, state_id=state.id, period_id=period.id
    )
    if not rows:
        raise ValidationError(
            f"{state.name} has no open queries for {period.code}; there is nothing to correct."
        )

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Corrections"

    sheet["A1"] = f"AGILE — data correction sheet — {state.name} — {period.label}"
    sheet["A1"].font = Font(size=14, bold=True)
    sheet["A2"] = (
        f"{len(rows)} figure(s) flagged during validation. For each row, either confirm the "
        "figure as reported or enter a corrected figure, and state the evidence that supports "
        "it. Rows cannot be added: only figures already flagged can be changed through this sheet."
    )
    sheet["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    sheet.merge_cells("A2:J2")
    sheet.row_dimensions[2].height = 44

    due = min((row.due_date for row in rows if row.due_date), default=None)
    sheet["A3"] = f"Response due: {due.isoformat() if due else 'not set'}"
    sheet["A3"].font = Font(bold=True, color="A52824" if due and due < date.today() else "16202C")

    header_row = 5
    for index, header in enumerate(HEADERS, start=1):
        cell = sheet.cell(row=header_row, column=index, value=header)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        sheet.column_dimensions[get_column_letter(index)].width = WIDTHS[index - 1]
    sheet.freeze_panes = sheet.cell(row=header_row + 1, column=1)

    for offset, query in enumerate(rows):
        row_number = header_row + 1 + offset
        indicator = query.indicator
        values = [
            _ref(query),
            indicator.code if indicator else "—",
            indicator.name if indicator else query.title[:80],
            query.period.code if query.period else period.code,
            query.reported_value,
            query.title,
            None,   # Your response
            None,   # Corrected figure
            None,   # Evidence provided
            None,   # Explanation
        ]
        for column, value in enumerate(values, start=1):
            cell = sheet.cell(row=row_number, column=column, value=value)
            cell.alignment = Alignment(
                wrap_text=column in {3, 6}, vertical="top",
                horizontal="right" if column in {5, 8} else "left",
            )
            if column <= 6:
                cell.fill = LOCKED_FILL
                cell.protection = Protection(locked=True)
            else:
                cell.fill = ENTRY_FILL
                cell.protection = Protection(locked=False)
        sheet.row_dimensions[row_number].height = 30

    guide = workbook.create_sheet("How to complete")
    guide["A1"] = "Completing the correction sheet"
    guide["A1"].font = Font(size=13, bold=True)
    for offset, line in enumerate(
        [
            "1. Do not add, delete or reorder rows. Only the flagged figures listed can be "
            "changed through this sheet.",
            "2. 'Your response' must say either CONFIRMED (the figure stands as reported) or "
            "CORRECTED (you are changing it).",
            "3. If CORRECTED, enter the new figure in 'Corrected figure'. Leave it blank when "
            "confirming.",
            "4. 'Evidence provided' names the document or verification that supports your "
            "answer, for example 'handover certificates, 12 LGAs'.",
            "5. 'Explanation' is for the reasoning a reviewer needs. A correction without an "
            "explanation will be returned.",
            "6. Where a figure was reported in an earlier period and is being restated, the "
            "'Period of the figure' column shows which period your correction applies to.",
            "7. Upload the completed sheet to the platform before the response deadline. "
            "Attach supporting documents with the upload.",
            "",
            "Nothing changes on the record until the NPCU reviews and accepts your response.",
        ],
        start=3,
    ):
        guide.cell(row=offset, column=1, value=line).alignment = Alignment(wrap_text=True)
    guide.column_dimensions["A"].width = 104

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------
# Reading it back
# --------------------------------------------------------------------------
@dataclass
class CorrectionRow:
    query_id: int
    response: str
    corrected_value: float | None
    evidence: str | None
    explanation: str | None
    source_row: int


@dataclass
class CorrectionResult:
    applied: int = 0
    skipped: int = 0
    responses: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_correction_sheet(content: bytes) -> list[CorrectionRow]:
    """Read a completed correction sheet into rows, ignoring untouched ones."""
    try:
        workbook = load_workbook(io.BytesIO(content), data_only=True)
    except Exception as exc:  # noqa: BLE001 - any reader failure is a bad file
        raise IngestionError(f"Could not read the correction sheet: {exc}") from exc

    if "Corrections" not in workbook.sheetnames:
        raise IngestionError(
            "This does not look like a correction sheet: no 'Corrections' tab was found."
        )

    sheet = workbook["Corrections"]
    rows: list[CorrectionRow] = []
    for index, values in enumerate(sheet.iter_rows(min_row=6, values_only=True), start=6):
        if not values or values[0] is None:
            continue
        match = QUERY_REF.match(str(values[0]).strip())
        if not match:
            continue

        response = str(values[6] or "").strip()
        corrected = to_number(values[7]) if len(values) > 7 else None
        evidence = str(values[8] or "").strip() or None if len(values) > 8 else None
        explanation = str(values[9] or "").strip() or None if len(values) > 9 else None

        if not response and corrected is None and not explanation:
            continue  # row left untouched

        rows.append(
            CorrectionRow(
                query_id=int(match.group(1)),
                response=response,
                corrected_value=corrected,
                evidence=evidence,
                explanation=explanation,
                source_row=index,
            )
        )
    return rows


def _intent(row: CorrectionRow) -> str:
    """Whether the row confirms the figure or corrects it."""
    text = row.response.lower()
    if any(token in text for token in CORRECT_TOKENS):
        return "CORRECTED"
    if any(token in text for token in CONFIRM_TOKENS):
        return "CONFIRMED"
    # Fall back on what was actually filled in.
    return "CORRECTED" if row.corrected_value is not None else "CONFIRMED"


def ingest_correction_sheet(
    db: Session,
    content: bytes,
    *,
    state: State,
    period: ReportingPeriod,
    actor: User | None = None,
) -> CorrectionResult:
    """Turn a completed correction sheet into responses on the queries it names."""
    rows = parse_correction_sheet(content)
    if not rows:
        raise IngestionError(
            "No completed rows were found. Enter a response against at least one flagged figure."
        )

    result = CorrectionResult()
    for row in rows:
        query = db.get(DataQuery, row.query_id)
        if query is None:
            result.warnings.append(f"Row {row.source_row}: query Q-{row.query_id} does not exist.")
            result.skipped += 1
            continue
        # A sheet issued to one state can only answer that state's queries.
        if query.state_id != state.id or query.period_id != period.id:
            result.warnings.append(
                f"Row {row.source_row}: Q-{row.query_id} does not belong to "
                f"{state.name} for {period.code}."
            )
            result.skipped += 1
            continue
        if not query.is_open:
            result.warnings.append(
                f"Row {row.source_row}: Q-{row.query_id} is already {query.status}."
            )
            result.skipped += 1
            continue

        intent = _intent(row)
        if intent == "CORRECTED" and row.corrected_value is None:
            result.warnings.append(
                f"Row {row.source_row}: marked as corrected but no corrected figure was entered."
            )
            result.skipped += 1
            continue
        if not row.explanation:
            result.warnings.append(
                f"Row {row.source_row}: an explanation is required before a response is accepted."
            )
            result.skipped += 1
            continue

        response = query_service.respond(
            db,
            query,
            narrative=row.explanation,
            evidence_summary=row.evidence,
            proposed_value=row.corrected_value if intent == "CORRECTED" else None,
            restates_period_code=query.period.code if query.period else None,
            actor=actor,
        )
        result.responses.append(response.id)
        result.applied += 1

    logger.info(
        "correction sheet ingested",
        extra={
            "state": state.code,
            "period": period.code,
            "applied": result.applied,
            "skipped": result.skipped,
        },
    )
    return result
