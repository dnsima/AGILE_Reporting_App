"""Markdown, HTML and PDF renderers for :class:`ReportDocument`."""

from __future__ import annotations

import html as html_lib
from datetime import datetime, timezone

from app.services.reporting.document import ReportDocument, Section, Table

try:  # PDF export is optional; Markdown and HTML always work.
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        TableStyle,
    )
    from reportlab.platypus import (
        Table as PdfTable,
    )

    PDF_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only where reportlab is absent
    PDF_AVAILABLE = False


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------
def _markdown_table(table: Table) -> str:
    lines: list[str] = []
    if table.caption:
        lines.append(f"**{table.caption}**")
        lines.append("")
    lines.append("| " + " | ".join(table.headers) + " |")
    separators = [
        "---:" if alignment == "right" else ":---" for alignment in table.alignment()
    ]
    lines.append("| " + " | ".join(separators) + " |")
    for row in table.rows:
        cells = [str(cell).replace("|", "\\|") for cell in row]
        lines.append("| " + " | ".join(cells) + " |")
    if table.note:
        lines.append("")
        lines.append(f"_{table.note}_")
    return "\n".join(lines)


def _markdown_section(section: Section) -> str:
    parts = [f"{'#' * min(section.level, 6)} {section.heading}", ""]
    for paragraph in section.paragraphs:
        parts.extend([paragraph, ""])
    if section.bullets:
        parts.extend([f"- {bullet}" for bullet in section.bullets])
        parts.append("")
    for table in section.tables:
        parts.extend([_markdown_table(table), ""])
    for subsection in section.subsections:
        parts.append(_markdown_section(subsection))
    return "\n".join(parts)


def render_markdown(document: ReportDocument) -> str:
    generated = document.generated_at or datetime.now(timezone.utc)
    parts = [f"# {document.title}", ""]
    if document.subtitle:
        parts.extend([f"_{document.subtitle}_", ""])

    meta_lines = [f"**Generated:** {generated.strftime('%Y-%m-%d %H:%M UTC')}"]
    meta_lines.extend(f"**{key}:** {value}" for key, value in document.meta.items())
    parts.extend(["  \n".join(meta_lines), ""])

    if document.summary:
        parts.extend(["## Executive summary", "", document.summary, ""])

    parts.append("---")
    parts.append("")
    for section in document.sections:
        parts.append(_markdown_section(section))

    return "\n".join(parts).rstrip() + "\n"


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------
HTML_STYLE = """
:root { color-scheme: light dark; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       margin: 0 auto; max-width: 1100px; padding: 32px 20px 64px; line-height: 1.55;
       color: #16202c; background: #ffffff; }
h1 { font-size: 1.9rem; margin: 0 0 4px; color: #0f3d61; }
h2 { font-size: 1.3rem; margin: 40px 0 10px; padding-bottom: 6px; border-bottom: 2px solid #e3e9f0;
     color: #0f3d61; }
h3 { font-size: 1.05rem; margin: 26px 0 8px; color: #24486b; }
.subtitle { color: #5b6b7d; font-size: 1rem; margin: 0 0 18px; }
.meta { background: #f4f7fa; border: 1px solid #e0e7ee; border-radius: 8px; padding: 12px 16px;
        font-size: .88rem; display: flex; flex-wrap: wrap; gap: 8px 28px; margin-bottom: 24px; }
.meta span strong { color: #0f3d61; }
.summary { background: #eef5fb; border-left: 4px solid #1f6fb2; padding: 14px 18px; border-radius: 4px; }
table { border-collapse: collapse; width: 100%; margin: 12px 0 8px; font-size: .86rem; }
th, td { border: 1px solid #dde4ec; padding: 7px 10px; text-align: left; vertical-align: top; }
th { background: #0f3d61; color: #fff; font-weight: 600; position: sticky; top: 0; }
tbody tr:nth-child(even) { background: #f7fafc; }
td.right, th.right { text-align: right; font-variant-numeric: tabular-nums; }
caption { caption-side: top; text-align: left; font-weight: 600; padding-bottom: 6px; color: #24486b; }
.note { color: #667788; font-size: .8rem; font-style: italic; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: .78rem; font-weight: 600; }
.badge.on-track { background: #d6f0dd; color: #11633a; }
.badge.progressing { background: #dbeafe; color: #1a4f87; }
.badge.lagging { background: #fdf0d2; color: #8a5a06; }
.badge.off-track { background: #fbdcdc; color: #97231f; }
ul { padding-left: 22px; }
@media (prefers-color-scheme: dark) {
  body { background: #11161d; color: #dde5ee; }
  h1, h2, h3 { color: #8ec4ee; }
  .meta { background: #18212c; border-color: #263341; }
  .summary { background: #16232f; border-left-color: #4c9bd8; }
  th, td { border-color: #27333f; }
  tbody tr:nth-child(even) { background: #151d26; }
  th { background: #1b3854; }
}
@media print { body { max-width: none; } h2 { page-break-after: avoid; } table { page-break-inside: avoid; } }
"""

STATUS_CLASSES = {
    "On track": "on-track",
    "Progressing": "progressing",
    "Lagging": "lagging",
    "Off track": "off-track",
}


def _html_cell(value: str) -> str:
    escaped = html_lib.escape(str(value))
    if value in STATUS_CLASSES:
        return f'<span class="badge {STATUS_CLASSES[value]}">{escaped}</span>'
    return escaped


def _html_table(table: Table) -> str:
    alignment = table.alignment()
    head = "".join(
        f'<th class="{alignment[i] if i < len(alignment) else "left"}">{html_lib.escape(header)}</th>'
        for i, header in enumerate(table.headers)
    )
    body_rows = []
    for row in table.rows:
        cells = "".join(
            f'<td class="{alignment[i] if i < len(alignment) else "left"}">{_html_cell(cell)}</td>'
            for i, cell in enumerate(row)
        )
        body_rows.append(f"<tr>{cells}</tr>")

    caption = f"<caption>{html_lib.escape(table.caption)}</caption>" if table.caption else ""
    note = f'<p class="note">{html_lib.escape(table.note)}</p>' if table.note else ""
    return (
        f"<table>{caption}<thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody></table>{note}"
    )


def _html_section(section: Section) -> str:
    level = min(section.level, 6)
    parts = [f"<h{level}>{html_lib.escape(section.heading)}</h{level}>"]
    parts.extend(f"<p>{html_lib.escape(p)}</p>" for p in section.paragraphs)
    if section.bullets:
        items = "".join(f"<li>{html_lib.escape(b)}</li>" for b in section.bullets)
        parts.append(f"<ul>{items}</ul>")
    parts.extend(_html_table(table) for table in section.tables)
    parts.extend(_html_section(sub) for sub in section.subsections)
    return "\n".join(parts)


def render_html(document: ReportDocument) -> str:
    generated = document.generated_at or datetime.now(timezone.utc)
    meta_items = [f"<span><strong>Generated</strong> {generated.strftime('%Y-%m-%d %H:%M UTC')}</span>"]
    meta_items.extend(
        f"<span><strong>{html_lib.escape(key)}</strong> {html_lib.escape(str(value))}</span>"
        for key, value in document.meta.items()
    )
    summary = (
        f'<div class="summary"><h2 style="margin-top:0">Executive summary</h2>'
        f"<p>{html_lib.escape(document.summary)}</p></div>"
        if document.summary
        else ""
    )
    subtitle = (
        f'<p class="subtitle">{html_lib.escape(document.subtitle)}</p>' if document.subtitle else ""
    )
    body = "\n".join(_html_section(section) for section in document.sections)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html_lib.escape(document.title)}</title>
<style>{HTML_STYLE}</style>
</head>
<body>
<h1>{html_lib.escape(document.title)}</h1>
{subtitle}
<div class="meta">{''.join(meta_items)}</div>
{summary}
{body}
</body>
</html>
"""


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------
def render_pdf(document: ReportDocument) -> bytes:
    """Render to PDF. Raises ``RuntimeError`` when reportlab is unavailable."""
    if not PDF_AVAILABLE:
        raise RuntimeError(
            "PDF export requires reportlab. Install it with `pip install reportlab`, "
            "or request the HTML or Markdown format instead."
        )

    import io

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=document.title,
        author="AGILE Reporting Platform",
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "AgileTitle", parent=styles["Title"], fontSize=18, textColor=colors.HexColor("#0F3D61")
    )
    heading_style = ParagraphStyle(
        "AgileHeading",
        parent=styles["Heading2"],
        fontSize=13,
        spaceBefore=14,
        textColor=colors.HexColor("#0F3D61"),
    )
    sub_heading_style = ParagraphStyle(
        "AgileSubHeading", parent=styles["Heading3"], fontSize=11, textColor=colors.HexColor("#24486B")
    )
    body_style = ParagraphStyle(
        "AgileBody", parent=styles["BodyText"], fontSize=9, leading=13, alignment=TA_LEFT
    )
    cell_style = ParagraphStyle("AgileCell", parent=body_style, fontSize=7.5, leading=9.5)
    header_cell_style = ParagraphStyle(
        "AgileHeaderCell", parent=cell_style, textColor=colors.white, fontName="Helvetica-Bold"
    )

    story: list = [Paragraph(html_lib.escape(document.title), title_style)]
    if document.subtitle:
        story.append(Paragraph(html_lib.escape(document.subtitle), body_style))
    generated = document.generated_at or datetime.now(timezone.utc)
    meta_line = " &nbsp;|&nbsp; ".join(
        [f"<b>Generated</b> {generated.strftime('%Y-%m-%d %H:%M UTC')}"]
        + [f"<b>{html_lib.escape(k)}</b> {html_lib.escape(str(v))}" for k, v in document.meta.items()]
    )
    story.extend([Spacer(1, 4 * mm), Paragraph(meta_line, body_style)])

    if document.summary:
        story.extend(
            [
                Spacer(1, 4 * mm),
                Paragraph("Executive summary", heading_style),
                Paragraph(html_lib.escape(document.summary), body_style),
            ]
        )

    def render_section(section: Section, depth: int = 0) -> None:
        story.append(
            Paragraph(
                html_lib.escape(section.heading),
                heading_style if depth == 0 else sub_heading_style,
            )
        )
        for paragraph in section.paragraphs:
            story.append(Paragraph(html_lib.escape(paragraph), body_style))
        for bullet in section.bullets:
            story.append(Paragraph(f"&bull; {html_lib.escape(bullet)}", body_style))
        for table in section.tables:
            if table.caption:
                story.append(Paragraph(f"<b>{html_lib.escape(table.caption)}</b>", body_style))
            data = [[Paragraph(html_lib.escape(h), header_cell_style) for h in table.headers]]
            data.extend(
                [Paragraph(html_lib.escape(str(cell)), cell_style) for cell in row]
                for row in table.rows
            )
            available = doc.width
            widths = [available / max(len(table.headers), 1)] * len(table.headers)
            if len(table.headers) > 2:
                widths[0] = available * 0.22
                remainder = (available - widths[0]) / (len(table.headers) - 1)
                widths[1:] = [remainder] * (len(table.headers) - 1)

            pdf_table = PdfTable(data, colWidths=widths, repeatRows=1)
            pdf_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0F3D61")),
                        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#C9D4DF")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F5F8FB")]),
                        ("TOPPADDING", (0, 0), (-1, -1), 3),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ]
                )
            )
            story.extend([Spacer(1, 2 * mm), pdf_table, Spacer(1, 3 * mm)])
            if table.note:
                story.append(Paragraph(f"<i>{html_lib.escape(table.note)}</i>", cell_style))
        for subsection in section.subsections:
            render_section(subsection, depth + 1)

    for index, section in enumerate(document.sections):
        if index and section.level == 2:
            story.append(PageBreak())
        render_section(section)

    doc.build(story)
    return buffer.getvalue()
