"""Generates the standardised state reporting template.

States download this workbook, fill the ``Value`` (and, where applicable,
``Numerator``/``Denominator``) columns, and upload it back. The header block
carries the state and period so the parser can detect them without the user
having to restate them in the upload form.
"""

from __future__ import annotations

import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from app.models import Indicator, ReportingPeriod, State

HEADERS = [
    "KPI Number",
    "Indicator Code",
    "Indicator Name",
    "Unit",
    "Value",
    "Numerator",
    "Denominator",
    "Sex",
    "School Level",
    "Data Source",
    "Comments",
]

COLUMN_WIDTHS = [12, 16, 62, 16, 14, 14, 14, 12, 16, 26, 34]

HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
SUBHEAD_FILL = PatternFill("solid", fgColor="DCE6F1")
LOCKED_FILL = PatternFill("solid", fgColor="F2F2F2")


def build_template_workbook(
    indicators: list[Indicator],
    state: State | None = None,
    period: ReportingPeriod | None = None,
) -> bytes:
    """Return the ``.xlsx`` bytes for a reporting template."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "AGILE Reporting"

    sheet["A1"] = "AGILE Standardised State Reporting Template"
    sheet["A1"].font = Font(size=14, bold=True)
    sheet["A2"] = "State:"
    sheet["B2"] = state.name if state else ""
    sheet["A3"] = "Cohort:"
    sheet["B3"] = state.cohort.name if state and state.cohort else ""
    sheet["A4"] = "Reporting Period:"
    sheet["B4"] = period.code if period else ""
    sheet["A5"] = "Submission Deadline:"
    sheet["B5"] = period.due_date.isoformat() if period else ""
    for row in range(2, 6):
        sheet[f"A{row}"].font = Font(bold=True)

    header_row = 7
    for index, header in enumerate(HEADERS, start=1):
        cell = sheet.cell(row=header_row, column=index, value=header)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        sheet.column_dimensions[get_column_letter(index)].width = COLUMN_WIDTHS[index - 1]
    sheet.freeze_panes = sheet.cell(row=header_row + 1, column=1)

    current_category: str | None = None
    row_cursor = header_row + 1

    for indicator in indicators:
        category_name = indicator.category.name if indicator.category else "Uncategorised"
        if category_name != current_category:
            current_category = category_name
            banner = sheet.cell(row=row_cursor, column=1, value=category_name)
            banner.font = Font(bold=True)
            banner.fill = SUBHEAD_FILL
            sheet.merge_cells(
                start_row=row_cursor, start_column=1, end_row=row_cursor, end_column=len(HEADERS)
            )
            row_cursor += 1

        sheet.cell(row=row_cursor, column=1, value=indicator.number).fill = LOCKED_FILL
        sheet.cell(row=row_cursor, column=2, value=indicator.code).fill = LOCKED_FILL
        name_cell = sheet.cell(row=row_cursor, column=3, value=indicator.name)
        name_cell.fill = LOCKED_FILL
        name_cell.alignment = Alignment(wrap_text=True, vertical="top")
        sheet.cell(row=row_cursor, column=4, value=indicator.unit).fill = LOCKED_FILL
        if not indicator.requires_numerator_denominator:
            for column in (6, 7):
                sheet.cell(row=row_cursor, column=column).fill = LOCKED_FILL
        row_cursor += 1

    guide = workbook.create_sheet("Guidance")
    guide["A1"] = "How to complete this template"
    guide["A1"].font = Font(size=13, bold=True)
    guidance = [
        "1. Do not edit the KPI Number, Indicator Code, Indicator Name or Unit columns.",
        "2. Enter the period figure in 'Value'. Leave a cell blank if there is nothing to report.",
        "3. For percentage indicators, also supply Numerator and Denominator so the national",
        "   roll-up can be weighted correctly. Enter percentages as 45 or 45%, not 0.45.",
        "4. Use only the approved disaggregation labels: Sex (female/male/total);",
        "   School Level (primary/JSS/SSS/total).",
        "5. Cumulative indicators must never fall below the figure reported in an earlier period.",
        "6. Always record the means of verification in 'Data Source'.",
        "7. Upload the completed file to the platform before the submission deadline above.",
    ]
    for offset, line in enumerate(guidance, start=3):
        guide.cell(row=offset, column=1, value=line)
    guide.column_dimensions["A"].width = 100

    reference = workbook.create_sheet("Indicator Reference")
    reference.append(["KPI Number", "Code", "Name", "Unit", "Direction", "Cumulative", "Definition"])
    for cell in reference[1]:
        cell.font = Font(bold=True)
    for indicator in indicators:
        reference.append(
            [
                indicator.number,
                indicator.code,
                indicator.name,
                indicator.unit,
                indicator.direction,
                "Yes" if indicator.is_cumulative else "No",
                indicator.definition or "",
            ]
        )
    for column, width in zip("ABCDEFG", [12, 14, 60, 16, 14, 12, 90], strict=True):
        reference.column_dimensions[column].width = width

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
