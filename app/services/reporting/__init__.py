"""Routine/periodic report generation."""

from app.services.reporting.builder import build_report, generate_report
from app.services.reporting.document import ReportDocument, Section, Table
from app.services.reporting.renderers import (
    PDF_AVAILABLE,
    render_html,
    render_markdown,
    render_pdf,
)

__all__ = [
    "PDF_AVAILABLE",
    "ReportDocument",
    "Section",
    "Table",
    "build_report",
    "generate_report",
    "render_html",
    "render_markdown",
    "render_pdf",
]
