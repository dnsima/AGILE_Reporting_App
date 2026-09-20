"""Word rendering for :class:`ReportDocument`.

Kept apart from ``renderers`` because python-docx is optional and the module
carries a fair amount of styling; the other three formats should not pay for
importing it.

The output is meant to be edited. NPCU sends these to the Bank and to state
PIUs after adding narrative of its own, so the styling uses Word's built-in
heading styles rather than direct formatting: an editor's navigation pane,
table of contents and restyling all keep working.
"""

from __future__ import annotations

import io
from datetime import datetime, timezone

from app.services.reporting.document import ReportDocument, Section, Table

try:
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    DOCX_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only where python-docx is absent
    DOCX_AVAILABLE = False


#: Federal Ministry of Education house blue, as used in the NPCU reports.
HEADING_COLOUR = (0x0F, 0x3D, 0x61)
RULE_COLOUR = "BFBFBF"


def _shade(cell, colour: str) -> None:
    """Fill a table cell. python-docx has no API for this, so set the XML."""
    shading = OxmlElement("w:shd")
    shading.set(qn("w:val"), "clear")
    shading.set(qn("w:color"), "auto")
    shading.set(qn("w:fill"), colour)
    cell._tc.get_or_add_tcPr().append(shading)


def _repeat_header(row) -> None:
    """Mark a header row so Word repeats it when a table breaks across pages."""
    properties = row._tr.get_or_add_trPr()
    header = OxmlElement("w:tblHeader")
    header.set(qn("w:val"), "true")
    properties.append(header)


def _add_table(document, table: Table) -> None:
    if table.caption:
        caption = document.add_paragraph(table.caption)
        caption.style = document.styles["Caption"]

    word_table = document.add_table(rows=1, cols=len(table.headers))
    word_table.style = "Table Grid"
    word_table.alignment = WD_TABLE_ALIGNMENT.CENTER
    alignment = table.alignment()

    header_cells = word_table.rows[0].cells
    for index, heading in enumerate(table.headers):
        cell = header_cells[index]
        cell.text = ""
        run = cell.paragraphs[0].add_run(str(heading))
        run.bold = True
        run.font.size = Pt(9)
        _shade(cell, "E7EDF3")
    _repeat_header(word_table.rows[0])

    for row in table.rows:
        cells = word_table.add_row().cells
        for index, value in enumerate(row):
            if index >= len(cells):
                break
            cell = cells[index]
            cell.text = ""
            paragraph = cell.paragraphs[0]
            run = paragraph.add_run("" if value is None else str(value))
            run.font.size = Pt(9)
            if index < len(alignment) and alignment[index] == "right":
                paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT

    if table.note:
        note = document.add_paragraph(table.note)
        note.runs[0].italic = True
        note.runs[0].font.size = Pt(8)

    document.add_paragraph()


def _add_section(document, section: Section) -> None:
    level = min(max(section.level, 1), 4)
    heading = document.add_heading(section.heading, level=level)
    for run in heading.runs:
        run.font.color.rgb = RGBColor(*HEADING_COLOUR)

    for paragraph in section.paragraphs:
        document.add_paragraph(paragraph)

    for bullet in section.bullets:
        document.add_paragraph(bullet, style="List Bullet")

    for table in section.tables:
        _add_table(document, table)

    for subsection in section.subsections:
        _add_section(document, subsection)


def render_docx(document: ReportDocument) -> bytes:
    """Render to Word. Raises ``RuntimeError`` when python-docx is unavailable."""
    if not DOCX_AVAILABLE:
        raise RuntimeError(
            "Word export requires python-docx. Install it with "
            "`pip install python-docx`, or request another format instead."
        )

    word = Document()

    normal = word.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(10.5)
    normal.paragraph_format.space_after = Pt(8)

    title = word.add_heading(document.title, level=0)
    for run in title.runs:
        run.font.color.rgb = RGBColor(*HEADING_COLOUR)

    if document.subtitle:
        subtitle = word.add_paragraph(document.subtitle)
        subtitle.runs[0].italic = True
        subtitle.runs[0].font.size = Pt(11)

    generated = document.generated_at or datetime.now(timezone.utc)
    meta_parts = [f"{key}: {value}" for key, value in document.meta.items()]
    meta_parts.append(f"Generated: {generated:%d %B %Y %H:%M UTC}")
    meta = word.add_paragraph("  |  ".join(meta_parts))
    meta.runs[0].font.size = Pt(8.5)
    meta.runs[0].font.color.rgb = RGBColor(0x55, 0x55, 0x55)

    if document.summary:
        summary = word.add_paragraph(document.summary)
        summary.runs[0].bold = True

    for section in document.sections:
        _add_section(word, section)

    buffer = io.BytesIO()
    word.save(buffer)
    return buffer.getvalue()
