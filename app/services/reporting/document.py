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
class Section:
    heading: str
    level: int = 2
    paragraphs: list[str] = field(default_factory=list)
    bullets: list[str] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    subsections: list[Section] = field(default_factory=list)

    def add_paragraph(self, text: str) -> Section:
        self.paragraphs.append(text)
        return self

    def add_table(self, table: Table) -> Section:
        self.tables.append(table)
        return self


@dataclass
class ReportDocument:
    title: str
    subtitle: str | None = None
    generated_at: datetime | None = None
    meta: dict[str, str] = field(default_factory=dict)
    summary: str | None = None
    sections: list[Section] = field(default_factory=list)

    def add_section(self, section: Section) -> Section:
        self.sections.append(section)
        return section
