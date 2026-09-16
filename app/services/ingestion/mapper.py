"""Auto-maps incoming template columns onto the unified reporting schema.

States submit templates whose headers drift ("Value", "Actual", "Achievement
this quarter"...). The mapper resolves each header to a canonical field using,
in order: an exact synonym table, token containment, then fuzzy matching. It
resolves indicators the same way against code, number, name and configured
aliases, so a template does not have to be byte-identical to be accepted.

Two layouts are supported:

* **long**  - one row per indicator (the standard AGILE template);
* **wide**  - one column per indicator, rows carrying disaggregations.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.models import Indicator
from app.schemas.ingestion import ColumnMapping

# --------------------------------------------------------------------------
# Canonical field vocabulary
# --------------------------------------------------------------------------
FIELD_SYNONYMS: dict[str, tuple[str, ...]] = {
    "indicator_code": (
        "indicator code", "kpi code", "code", "indicator id", "kpi id", "indicator reference",
    ),
    "indicator_number": (
        "kpi number", "kpi no", "kpi", "indicator number", "indicator no", "s/n", "sn",
        "serial", "serial number", "no", "number", "ref no",
    ),
    "indicator_name": (
        "indicator", "indicator name", "kpi name", "indicator description", "description",
        "kpi description", "indicator title", "result indicator",
    ),
    "value": (
        "value", "actual", "actual value", "achievement", "achieved", "result", "reported value",
        "current value", "performance", "cumulative achievement", "period achievement", "total",
        "actual achievement", "reported figure",
    ),
    "numerator": ("numerator", "num", "numerator value", "n"),
    "denominator": ("denominator", "denom", "den", "denominator value", "d"),
    "target": ("target", "target value", "planned", "plan", "annual target", "period target"),
    "sex": ("sex", "gender", "male female", "disaggregation sex"),
    "school_level": (
        "school level", "level", "education level", "class level", "school type", "grade",
    ),
    "location": ("location", "urban rural", "settlement", "area type"),
    "lga": ("lga", "local government", "local government area", "council"),
    "data_source": (
        "data source", "source", "source of data", "means of verification", "verification",
    ),
    "comment": ("comment", "comments", "remark", "remarks", "note", "notes", "explanation"),
    "state": ("state", "state name", "reporting state"),
    "period": (
        "period", "reporting period", "quarter", "month", "reporting month", "reporting quarter",
        "cycle",
    ),
}

#: Fields that identify which indicator a row is about.
INDICATOR_FIELDS = {"indicator_code", "indicator_number", "indicator_name"}
#: Fields that become part of the stored disaggregation payload.
DISAGGREGATION_FIELDS = {"sex", "school_level", "location", "lga"}

FUZZY_CUTOFF = 0.82
INDICATOR_NAME_CUTOFF = 0.86

_CODE_PATTERN = re.compile(r"^(?:kpi|ind|agile)?[\s_\-]*0*(\d{1,3})$", re.I)
_NUMERIC_CLEAN = re.compile(r"[,\s ₦]|(?:ngn)", re.I)
_MISSING_TOKENS = {
    "", "na", "n/a", "n.a", "nil", "none", "null", "-", "--", "nan", "not applicable",
    "not available", "no data", "tbd", "tbc", "#n/a", "x",
}


def normalize(text: Any) -> str:
    """Lowercase, strip punctuation and collapse whitespace."""
    if text is None:
        return ""
    cleaned = re.sub(r"[^a-z0-9]+", " ", str(text).lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def to_number(raw: Any) -> float | None:
    """Parse a spreadsheet cell into a float, tolerating human formatting."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return float(raw)
    if isinstance(raw, (int, float)):
        value = float(raw)
        return None if value != value else value  # drop NaN

    text = str(raw).strip()
    if text.lower() in _MISSING_TOKENS:
        return None

    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]

    percent = text.endswith("%")
    text = text.rstrip("%").strip()
    text = _NUMERIC_CLEAN.sub("", text)
    if not text:
        return None

    try:
        value = float(text)
    except ValueError:
        return None
    if negative:
        value = -value
    _ = percent  # "45%" and "45" both mean 45 for a PERCENT indicator
    return value


def is_missing(raw: Any) -> bool:
    if raw is None:
        return True
    if isinstance(raw, float) and raw != raw:
        return True
    return str(raw).strip().lower() in _MISSING_TOKENS


# --------------------------------------------------------------------------
# Mapper
# --------------------------------------------------------------------------
@dataclass
class MappedValue:
    """One resolved indicator reading, ready to be persisted."""

    indicator: Indicator
    value: float | None = None
    numerator: float | None = None
    denominator: float | None = None
    raw_value: str | None = None
    disaggregation: dict[str, str] = field(default_factory=dict)
    data_source: str | None = None
    comment: str | None = None
    source_row: int | None = None


@dataclass
class MappingResult:
    values: list[MappedValue] = field(default_factory=list)
    column_mappings: list[ColumnMapping] = field(default_factory=list)
    unmatched_indicators: list[str] = field(default_factory=list)
    layout: str = "long"
    mapped_rows: int = 0
    unmapped_rows: int = 0
    warnings: list[str] = field(default_factory=list)
    detected_state: str | None = None
    detected_period: str | None = None


class SchemaMapper:
    """Resolves headers to canonical fields and labels to indicators."""

    def __init__(self, indicators: Iterable[Indicator]) -> None:
        self.indicators = list(indicators)
        self._by_code: dict[str, Indicator] = {}
        self._by_number: dict[int, Indicator] = {}
        self._by_name: dict[str, Indicator] = {}

        for indicator in self.indicators:
            self._by_code[normalize(indicator.code)] = indicator
            self._by_code[normalize(indicator.code).replace(" ", "")] = indicator
            self._by_number[indicator.number] = indicator
            self._by_name[normalize(indicator.name)] = indicator
            for alias in indicator.aliases or []:
                self._by_name[normalize(alias)] = indicator

        self._name_keys = list(self._by_name.keys())
        self._field_lookup: dict[str, str] = {}
        for canonical, synonyms in FIELD_SYNONYMS.items():
            for synonym in synonyms:
                self._field_lookup[normalize(synonym)] = canonical

    # -- indicator resolution --------------------------------------------
    def resolve_indicator(self, *candidates: Any) -> tuple[Indicator | None, str]:
        """Resolve an indicator from any mix of code, number and name cells."""
        for candidate in candidates:
            if is_missing(candidate):
                continue
            text = str(candidate).strip()
            key = normalize(text)

            if key in self._by_code:
                return self._by_code[key], "code"
            compact = key.replace(" ", "")
            if compact in self._by_code:
                return self._by_code[compact], "code"

            match = _CODE_PATTERN.match(text.replace("_", "-").strip())
            if match:
                number = int(match.group(1))
                if number in self._by_number:
                    return self._by_number[number], "number"

            if key in self._by_name:
                return self._by_name[key], "name"

        for candidate in candidates:
            if is_missing(candidate):
                continue
            key = normalize(candidate)
            if len(key) < 8:
                continue
            close = difflib.get_close_matches(key, self._name_keys, n=1, cutoff=INDICATOR_NAME_CUTOFF)
            if close:
                return self._by_name[close[0]], "fuzzy-name"

        return None, "unmatched"

    # -- header resolution ------------------------------------------------
    def resolve_header(self, header: str) -> ColumnMapping:
        key = normalize(header)
        if not key or key.startswith("column "):
            return ColumnMapping(source_header=header, mapped_field=None, strategy="unmapped")

        if key in self._field_lookup:
            return ColumnMapping(
                source_header=header,
                mapped_field=self._field_lookup[key],
                confidence=1.0,
                strategy="exact",
            )

        # Header is itself an indicator (wide templates).
        indicator, strategy = self.resolve_indicator(header)
        if indicator is not None and strategy in {"code", "number", "name"}:
            return ColumnMapping(
                source_header=header,
                mapped_field=f"indicator:{indicator.code}",
                confidence=0.95,
                strategy=f"indicator-{strategy}",
            )

        tokens = key.split()
        for synonym_key, canonical in self._field_lookup.items():
            synonym_tokens = synonym_key.split()
            if synonym_tokens and all(token in tokens for token in synonym_tokens):
                return ColumnMapping(
                    source_header=header,
                    mapped_field=canonical,
                    confidence=0.9,
                    strategy="token",
                )

        close = difflib.get_close_matches(key, list(self._field_lookup), n=1, cutoff=FUZZY_CUTOFF)
        if close:
            return ColumnMapping(
                source_header=header,
                mapped_field=self._field_lookup[close[0]],
                confidence=round(difflib.SequenceMatcher(None, key, close[0]).ratio(), 3),
                strategy="fuzzy",
            )

        if indicator is not None:
            return ColumnMapping(
                source_header=header,
                mapped_field=f"indicator:{indicator.code}",
                confidence=0.75,
                strategy="indicator-fuzzy",
            )

        return ColumnMapping(source_header=header, mapped_field=None, strategy="unmapped")


def _first_present(row: dict[str, Any], headers: list[str]) -> Any:
    for header in headers:
        value = row.get(header)
        if not is_missing(value):
            return value
    return None


def _text(raw: Any, limit: int = 255) -> str | None:
    if is_missing(raw):
        return None
    return str(raw).strip()[:limit]


def map_rows(
    headers: list[str],
    rows: list[dict[str, Any]],
    indicators: Iterable[Indicator],
) -> MappingResult:
    """Map parsed rows onto indicator readings."""
    mapper = SchemaMapper(indicators)
    result = MappingResult()

    field_columns: dict[str, list[str]] = {}
    indicator_columns: dict[str, Any] = {}

    for header in headers:
        mapping = mapper.resolve_header(header)
        result.column_mappings.append(mapping)
        if not mapping.mapped_field:
            continue
        if mapping.mapped_field.startswith("indicator:"):
            code = mapping.mapped_field.split(":", 1)[1]
            indicator = next((i for i in mapper.indicators if i.code == code), None)
            if indicator is not None:
                indicator_columns[header] = indicator
        else:
            field_columns.setdefault(mapping.mapped_field, []).append(header)

    has_indicator_key = bool(INDICATOR_FIELDS & set(field_columns))
    result.layout = "long" if has_indicator_key else ("wide" if indicator_columns else "long")

    if result.layout == "wide" and not indicator_columns:
        result.warnings.append(
            "No indicator column and no indicator-named columns were recognised."
        )
        return result

    def disaggregation_for(row: dict[str, Any]) -> dict[str, str]:
        payload: dict[str, str] = {}
        for axis in DISAGGREGATION_FIELDS:
            value = _first_present(row, field_columns.get(axis, []))
            text = _text(value, 64)
            if text:
                payload[axis] = text
        return payload

    state_hint = period_hint = None

    for row in rows:
        source_row = row.get("__row__")
        if state_hint is None:
            state_hint = _text(_first_present(row, field_columns.get("state", [])), 64)
        if period_hint is None:
            period_hint = _text(_first_present(row, field_columns.get("period", [])), 64)

        shared_disaggregation = disaggregation_for(row)
        data_source = _text(_first_present(row, field_columns.get("data_source", [])))
        comment = _text(_first_present(row, field_columns.get("comment", [])), 2000)

        if result.layout == "long":
            indicator, strategy = mapper.resolve_indicator(
                _first_present(row, field_columns.get("indicator_code", [])),
                _first_present(row, field_columns.get("indicator_number", [])),
                _first_present(row, field_columns.get("indicator_name", [])),
            )
            if indicator is None:
                label = _text(
                    _first_present(
                        row,
                        field_columns.get("indicator_name", [])
                        + field_columns.get("indicator_code", [])
                        + field_columns.get("indicator_number", []),
                    ),
                    200,
                )
                if label:
                    result.unmatched_indicators.append(label)
                result.unmapped_rows += 1
                continue
            if strategy == "fuzzy-name":
                result.warnings.append(
                    f"Row {source_row}: matched '{indicator.code}' by approximate name match."
                )

            raw_value = _first_present(row, field_columns.get("value", []))
            result.values.append(
                MappedValue(
                    indicator=indicator,
                    value=to_number(raw_value),
                    numerator=to_number(_first_present(row, field_columns.get("numerator", []))),
                    denominator=to_number(
                        _first_present(row, field_columns.get("denominator", []))
                    ),
                    raw_value=_text(raw_value, 255),
                    disaggregation=shared_disaggregation,
                    data_source=data_source,
                    comment=comment,
                    source_row=source_row,
                )
            )
            result.mapped_rows += 1
            continue

        # Wide layout: every indicator column on this row is one reading.
        produced = 0
        for header, indicator in indicator_columns.items():
            raw_value = row.get(header)
            if is_missing(raw_value):
                continue
            result.values.append(
                MappedValue(
                    indicator=indicator,
                    value=to_number(raw_value),
                    raw_value=_text(raw_value, 255),
                    disaggregation=shared_disaggregation,
                    data_source=data_source,
                    comment=comment,
                    source_row=source_row,
                )
            )
            produced += 1
        if produced:
            result.mapped_rows += 1
        else:
            result.unmapped_rows += 1

    result.detected_state = state_hint
    result.detected_period = period_hint
    return result
