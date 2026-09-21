"""Reads the Kobo backend export the NPCU downloads each quarter.

States do not upload a workbook to this platform. They fill the AGILE Results
Framework form on Kobo Toolbox, and the NPCU downloads one backend dataset
covering every state that filed. That export is the real entry point, so this
module reads it directly rather than asking anyone to re-key it into a
template.

The export's shape follows the form, not the results framework:

* one row per *form section*, not per state -- a state that files completely
  appears four times, once for ``PDO Data`` and once for each component, with
  only that section's columns carrying values;
* one *column per indicator*, headed with the question text as the form asks
  it, which is the indicator's name in the catalogue give or take an
  underscore;
* a tail of Kobo's own fields (``_id``, ``_uuid``, ``_submission_time``, GPS)
  that are provenance, not data.

So the work here is to resolve each question column to exactly one indicator,
then fold a state's sections back into the single return the rest of the
platform expects.

**Why the column resolution is strict.** Two questions on this form differ
only by a leading percent sign: "schools implementing awareness programs on
climate change" is a count, and "% schools implementing awareness programs on
climate change" is a rate. The platform's general-purpose header matcher
normalises punctuation away and collapses the pair -- a test of mine did
exactly that -- and the cost of getting it wrong is a rate landing in a count
field, where nothing downstream would ever question 87 schools. So the
matching here keeps the percent sign, demands that each column claim one
indicator and each indicator at most one column, and refuses the whole file
when it cannot. A refused export is a morning's work; a silently mis-mapped
one is a wrong national figure in a Bank report.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from app.core.errors import IngestionError
from app.core.logging_config import get_logger
from app.models import Indicator
from app.services.ingestion.mapper import to_boolean, to_number

logger = get_logger(__name__)

#: The form's section names, and the component each one reports.
SECTIONS: dict[str, str] = {
    "pdo data": "PDO",
    "component 1 data": "C1",
    "component 2 data": "C2",
    "component 3 data": "C3",
}

SECTION_ORDER = ("PDO", "C1", "C2", "C3")

#: Columns the form carries that identify the return rather than report on it.
STATE_COLUMN = "state"
SECTION_COLUMN = "select reporting data"
DATE_COLUMN = "reporting date"

#: Contact fields the form collects, kept as provenance on the submission note.
FOCAL_NAME_COLUMN = "m&e's person name"
FOCAL_PHONE_COLUMN = "m&e person phone number"

#: Kobo's own bookkeeping. Anything starting with one of these is never an
#: indicator, whatever it is called.
SYSTEM_PREFIXES = ("_", "meta/")

#: Question columns that are instructions to the enumerator, not questions.
#: The form opens each section with a note; Kobo exports notes as columns.
_NOTE_MIN_LENGTH = 120

_MISSING_HEADERS = {"", "nan", "none"}

_DASHES = str.maketrans({"‐": "-", "‑": "-", "‒": "-", "–": "-",
                         "—": "-", "―": "-", "−": "-"})
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})


def normalise_question(text: Any) -> str:
    """Fold a question or indicator name to its comparable form.

    Case, underscores, dash flavours, curly quotes and runs of whitespace all
    vary between the Kobo form and the catalogue and mean nothing. The percent
    sign is kept, because on this form it is the whole difference between a
    count of schools and the share of them.
    """
    if text is None:
        return ""
    value = str(text).strip().translate(_DASHES).translate(_QUOTES).lower()
    value = value.replace("_", " ")
    return re.sub(r"\s+", " ", value).strip()


def relax(text: str) -> str:
    """A second pass that forgives punctuation -- but never the percent sign.

    A form edit that adds a comma or drops a bracket should not fail an
    export. Collapsing ``%`` would merge C3.0-03 into C3.0-04, so it survives
    as the word ``percent``.
    """
    value = text.replace("%", " percent ")
    value = re.sub(r"[^a-z0-9%]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _is_system(header: Any) -> bool:
    """Kobo bookkeeping, recognised on the raw header.

    Checked before normalising, because normalising turns ``_id`` into ``id``
    and the leading underscore is the whole signal.
    """
    return str(header).strip().lower().startswith(SYSTEM_PREFIXES)


def _is_note(header: str) -> bool:
    """A section's standing instruction, exported as though it were a question."""
    return len(header) >= _NOTE_MIN_LENGTH


# --------------------------------------------------------------------------
# Column resolution
# --------------------------------------------------------------------------
@dataclass
class ColumnResolution:
    """Which question column reports which indicator."""

    #: Header text -> indicator.
    matched: dict[str, Indicator] = field(default_factory=dict)
    #: Question columns that matched no indicator.
    unmatched: list[str] = field(default_factory=list)
    #: Indicators the export never asks about.
    absent: list[str] = field(default_factory=list)
    #: Headers that matched an indicator another header had already claimed.
    collisions: list[tuple[str, str, str]] = field(default_factory=list)
    #: Headers matched only after punctuation was forgiven.
    relaxed: list[str] = field(default_factory=list)

    @property
    def is_usable(self) -> bool:
        return bool(self.matched) and not self.unmatched and not self.collisions


def resolve_columns(headers: list[str], indicators: list[Indicator]) -> ColumnResolution:
    """Map the export's question columns onto the indicator catalogue.

    Exact first, then punctuation-forgiving, and never onto an indicator that
    another column already claimed.
    """
    reported = [i for i in indicators if i.is_active and i.is_reported]
    by_exact: dict[str, list[Indicator]] = {}
    by_relaxed: dict[str, list[Indicator]] = {}
    for indicator in reported:
        exact = normalise_question(indicator.name)
        by_exact.setdefault(exact, []).append(indicator)
        by_relaxed.setdefault(relax(exact), []).append(indicator)

    resolution = ColumnResolution()
    claimed: dict[int, str] = {}

    questions = [
        header
        for header in headers
        if not _is_system(header)
        and (norm := normalise_question(header)) not in _MISSING_HEADERS
        and norm not in {STATE_COLUMN, SECTION_COLUMN, DATE_COLUMN,
                         FOCAL_NAME_COLUMN, FOCAL_PHONE_COLUMN}
        and not _is_note(norm)
        and not norm.startswith("please collect gps")
    ]

    def claim(header: str, candidates: list[Indicator], *, was_relaxed: bool) -> bool:
        if len(candidates) != 1:
            return False
        indicator = candidates[0]
        if indicator.id in claimed:
            resolution.collisions.append((header, claimed[indicator.id], indicator.code))
            return True  # handled: recorded as a collision, not left unmatched
        claimed[indicator.id] = header
        resolution.matched[header] = indicator
        if was_relaxed:
            resolution.relaxed.append(header)
        return True

    pending: list[str] = []
    for header in questions:
        exact = normalise_question(header)
        if not claim(header, by_exact.get(exact, []), was_relaxed=False):
            pending.append(header)

    for header in pending:
        loose = relax(normalise_question(header))
        # Deliberately not filtered by what is already claimed: a column whose
        # only candidate is taken is a duplicated question, and saying so is
        # more use than reporting it as matching nothing.
        if not claim(header, by_relaxed.get(loose, []), was_relaxed=True):
            resolution.unmatched.append(header)

    resolution.absent = sorted(
        indicator.code for indicator in reported if indicator.id not in claimed
    )
    return resolution


# --------------------------------------------------------------------------
# Reading the export
# --------------------------------------------------------------------------
@dataclass
class StateReturn:
    """One state's quarterly return, folded back from its form sections."""

    state_name: str
    #: Indicator code -> reported figure, missing answers left out entirely.
    values: dict[str, float] = field(default_factory=dict)
    #: Components the state actually filed a section for.
    sections: set[str] = field(default_factory=set)
    reported_on: datetime | None = None
    submitted_at: datetime | None = None
    kobo_ids: list[str] = field(default_factory=list)
    focal_person: str | None = None
    focal_phone: str | None = None
    #: Answers that came back as text the parsers could not read.
    unreadable: list[str] = field(default_factory=list)

    @property
    def missing_sections(self) -> list[str]:
        return [code for code in SECTION_ORDER if code not in self.sections]

    @property
    def note(self) -> str:
        filed = ", ".join(code for code in SECTION_ORDER if code in self.sections)
        note = f"Kobo backend export; sections filed: {filed or 'none'}"
        if self.focal_person:
            note += f"; M&E focal person: {self.focal_person}"
        return note


@dataclass
class KoboExport:
    """An export read, resolved and folded into one return per state."""

    returns: list[StateReturn]
    resolution: ColumnResolution
    row_count: int
    #: Section labels the form used that this module does not recognise.
    unknown_sections: list[str] = field(default_factory=list)

    @property
    def state_names(self) -> list[str]:
        return [ret.state_name for ret in self.returns]


def _read_frame(content: bytes, filename: str) -> pd.DataFrame:
    suffix = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    try:
        if suffix in {".csv", ".txt"}:
            return pd.read_csv(io.BytesIO(content), dtype=object, keep_default_na=False)
        if suffix == ".tsv":
            return pd.read_csv(
                io.BytesIO(content), sep="\t", dtype=object, keep_default_na=False
            )
        return pd.read_excel(io.BytesIO(content), dtype=object, keep_default_na=False)
    except Exception as exc:  # pragma: no cover - pandas raises many shapes
        raise IngestionError(f"Could not read the Kobo export: {exc}") from exc


def _as_datetime(raw: Any) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw
    try:
        parsed = pd.to_datetime(raw, errors="coerce")
    except Exception:
        return None
    if parsed is None or pd.isna(parsed):
        return None
    return parsed.to_pydatetime()


def _text(raw: Any) -> str | None:
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def read_export(content: bytes, filename: str, indicators: list[Indicator]) -> KoboExport:
    """Read a Kobo backend export into one return per state.

    Raises rather than guessing when the columns cannot be resolved: a
    mis-mapped column is a wrong figure nobody downstream can see.
    """
    frame = _read_frame(content, filename)
    headers = [str(column) for column in frame.columns]
    resolution = resolve_columns(headers, indicators)

    if not resolution.matched:
        raise IngestionError(
            "No column in this file matches an indicator, so it does not look "
            "like an AGILE Kobo export.",
            details={"headers": headers[:20]},
        )
    if resolution.collisions:
        detail = "; ".join(
            f"'{header}' and '{other}' both resolve to {code}"
            for header, other, code in resolution.collisions
        )
        raise IngestionError(
            "Two columns in this export resolve to the same indicator, so at "
            f"least one figure would land in the wrong field: {detail}. "
            "Check the form for a renamed or duplicated question.",
            details={"collisions": resolution.collisions},
        )
    if resolution.unmatched:
        raise IngestionError(
            f"{len(resolution.unmatched)} question column(s) in this export "
            "match no indicator in the catalogue: "
            + "; ".join(f"'{header}'" for header in resolution.unmatched[:5])
            + ". The form and the results framework have drifted apart; "
            "reconcile them before loading, rather than loading a partial return.",
            details={"unmatched": resolution.unmatched},
        )

    lookup = {normalise_question(header): header for header in headers}
    state_header = lookup.get(STATE_COLUMN)
    section_header = lookup.get(SECTION_COLUMN)
    if state_header is None:
        raise IngestionError(
            "This export has no 'State' column, so there is no way to tell "
            "whose figures these are."
        )

    returns: dict[str, StateReturn] = {}
    unknown_sections: set[str] = set()

    for _, row in frame.iterrows():
        state_name = _text(row.get(state_header))
        if not state_name:
            continue

        entry = returns.setdefault(state_name, StateReturn(state_name=state_name))

        section_label = (
            normalise_question(row.get(section_header)) if section_header else ""
        )
        component = SECTIONS.get(section_label)
        if component:
            entry.sections.add(component)
        elif section_label:
            unknown_sections.add(section_label)

        for header, indicator in resolution.matched.items():
            raw = row.get(header)
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                continue
            parse = to_boolean if indicator.unit == "BOOLEAN" else to_number
            value = parse(raw)
            if value is None:
                # 'NaN' and 'N/A' are how the form records "not answered", and
                # they must stay unreported rather than become a zero.
                if normalise_question(raw) not in {"nan", "n/a", "na", "-", "none", "null"}:
                    entry.unreadable.append(f"{indicator.code}='{raw}'")
                continue
            # Sections do not overlap, so a second value for the same
            # indicator means the state filed a section twice. The later row
            # wins, matching Kobo's own "latest submission" behaviour.
            entry.values[indicator.code] = value

        reported_on = _as_datetime(row.get(lookup.get(DATE_COLUMN, "")))
        if reported_on and (entry.reported_on is None or reported_on > entry.reported_on):
            entry.reported_on = reported_on
        submitted = _as_datetime(row.get("_submission_time"))
        if submitted and (entry.submitted_at is None or submitted > entry.submitted_at):
            entry.submitted_at = submitted

        kobo_id = _text(row.get("_id"))
        if kobo_id:
            entry.kobo_ids.append(kobo_id)
        entry.focal_person = entry.focal_person or _text(row.get(lookup.get(FOCAL_NAME_COLUMN, "")))
        entry.focal_phone = entry.focal_phone or _text(row.get(lookup.get(FOCAL_PHONE_COLUMN, "")))

    ordered = sorted(returns.values(), key=lambda ret: ret.state_name)
    logger.info(
        "read kobo export",
        extra={
            "rows": len(frame),
            "states": len(ordered),
            "columns_matched": len(resolution.matched),
            "columns_relaxed": len(resolution.relaxed),
        },
    )
    return KoboExport(
        returns=ordered,
        resolution=resolution,
        row_count=len(frame),
        unknown_sections=sorted(unknown_sections),
    )
