"""A format-neutral report model.

The builder produces one of these; the renderers turn it into Markdown, HTML or
PDF. Keeping the model separate means a new output format is a new renderer,
not a second copy of the report logic.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Table:
    caption: str | None
    headers: list[str]
    rows: list[list[str]]
    #: "left" or "right" per column; defaults to left for the first column.
    align: list[str] = field(default_factory=list)
    note: str | None = None

    def alignment(self) -> list[str]:
        if self.align:
            return self.align
        return ["left"] + ["right"] * (len(self.headers) - 1)


@dataclass
class Figure:
    """A rendered chart, with the data behind it.

    ``data`` is not optional garnish. It is the accessibility relief for a
    palette slot below 3:1 contrast, the fallback for any renderer that cannot
    embed an image, and what a reader checks a number against -- which is why
    the NPCU reports carry an annex table for every figure they print.
    """

    caption: str
    png: bytes
    alt_text: str
    width_inches: float = 6.3
    data: Table | None = None


@dataclass
class Section:
    heading: str
    level: int = 2
    paragraphs: list[str] = field(default_factory=list)
    bullets: list[str] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    figures: list[Figure] = field(default_factory=list)
    subsections: list[Section] = field(default_factory=list)

    def add_paragraph(self, text: str) -> Section:
        self.paragraphs.append(text)
        return self

    def add_table(self, table: Table) -> Section:
        self.tables.append(table)
        return self

    def add_figure(self, figure: Figure | None) -> Section:
        """Append a figure, ignoring ``None`` so a builder can opt out."""
        if figure is not None:
            self.figures.append(figure)
        return self


@dataclass
class ReportDocument:
    title: str
    subtitle: str | None = None
    generated_at: datetime | None = None
    meta: dict[str, str] = field(default_factory=dict)
    summary: str | None = None
    sections: list[Section] = field(default_factory=list)

    def add_section(self, section: Section | None) -> Section | None:
        """Append a section, ignoring ``None`` so a builder can opt out."""
        if section is not None:
            self.sections.append(section)
        return section

    def number_sections(self) -> None:
        """Number the top-level headings in the order they were added.

        Numbering is computed rather than written into each heading, because a
        report whose sections are conditional cannot hard-code them: inserting
        one section would silently leave two sections numbered 4.
        """
        for index, section in enumerate(self.sections, start=1):
            section.heading = f"{index}. {section.heading.lstrip('0123456789. ')}"
