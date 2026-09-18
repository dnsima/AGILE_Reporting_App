"""Build the AGILE seed files from the NPCU workbooks.

Reads the Q2 2026 results-framework model, the Q1 2026 model and the Q1-vs-Q2
crosswalk, and writes the reference data the platform runs on: the recoded
53-indicator catalogue, the state list with cohorts, the subcomponent
applicability matrix and the Q1 to Q2 indicator lineage.

The recoding rule: every Component code carries its subcomponent as a dotted
segment (C1.2-05), which no code in the old scheme contains. That makes a
collision between an old code and a new one structurally impossible -- the
failure mode that turned Q1 PDO-01 (616 school buildings) into Q2 PDO-01
(6.7 million students).
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from openpyxl import load_workbook

UPLOADS = Path("/root/.claude/uploads/2e5b36ef-43ca-515f-bb8f-925304562963")
Q2_MODEL = UPLOADS / "d0b593e1-AGILE_Q2_2026_Analysis_Model_Flagged.xlsx"
SEEDS = Path("seeds")

# --------------------------------------------------------------------------
# Old code -> (new code, subcomponent)
# Subcomponents are taken from the NPCU performance tracker, which tags every
# indicator explicitly; the handful the tracker words differently are assigned
# from the results framework's own component sections.
# --------------------------------------------------------------------------
RECODE: dict[str, tuple[str, str]] = {
    **{f"PDO-{n:02d}": (f"PDO-{n:02d}", "PDO") for n in range(1, 17)},
    # Component 1 -- general (composites, WASH, safeguarding, whole-school)
    "C1-01": ("C1.0-01", "C1"),
    "C1-05": ("C1.0-02", "C1"),
    "C1-10": ("C1.0-03", "C1"),
    "C1-11": ("C1.0-04", "C1"),
    "C1-12": ("C1.0-05", "C1"),
    "C1-13": ("C1.0-06", "C1"),
    # Component 1.1 -- new build only
    "C1-02": ("C1.1-01", "C1.1"),
    "C1-06": ("C1.1-02", "C1.1"),
    # Component 1.2 -- existing schools and school improvement grants
    "C1-03": ("C1.2-01", "C1.2"),
    "C1-04": ("C1.2-02", "C1.2"),
    "C1-07": ("C1.2-03", "C1.2"),
    "C1-08": ("C1.2-04", "C1.2"),
    "C1-09": ("C1.2-05", "C1.2"),
    # Component 2.1 -- community mobilisation and media
    "C2-01+02": ("C2.0-01", "C2.1"),
    "C2-01": ("C2.1-01", "C2.1"),
    "C2-02": ("C2.1-02", "C2.1"),
    "C2-03": ("C2.1-03", "C2.1"),
    # Component 2.2 -- life skills, digital, second chance
    "C2-04": ("C2.2a-01", "C2.2a"),
    "C2-05": ("C2.2a-02", "C2.2a"),
    "C2-06": ("C2.2b-01", "C2.2b"),
    "C2-07": ("C2.2b-02", "C2.2b"),
    "C2-08": ("C2.2b-03", "C2.2b"),
    "C2-09": ("C2.2b-04", "C2.2b"),
    "C2-10": ("C2.2b-05", "C2.2b"),
    "C2-11": ("C2.2c-01", "C2.2c"),
    # Component 2.3 -- scholarships and social safety net
    "C2-12": ("C2.3-01", "C2.3"),
    "C2-13": ("C2.3-02", "C2.3"),
    "C2-14": ("C2.3-03", "C2.3"),
    "C2-15": ("C2.3-04", "C2.3"),
    "C2-16": ("C2.3-05", "C2.3"),
    "C2-17": ("C2.3-06", "C2.3"),
    # Component 3 -- cross-cutting
    **{f"C3-{n:02d}": (f"C3.0-{n:02d}", "C3") for n in range(1, 8)},
}

#: Composite indicators and the parts they must equal.
COMPOSITES: dict[str, list[str]] = {
    "C1.0-01": ["C1.1-01", "C1.2-01", "C1.2-02"],
    "C1.0-02": ["C1.1-02", "C1.2-03", "C1.2-04"],
    "C2.0-01": ["C2.1-01", "C2.1-02"],
}

#: The results framework's `Type` column drives every downstream calculation.
TYPE_MAP = {
    "Current (No.)": ("NUMBER", "SUM", 0),
    "Cumulative (No.)": ("NUMBER", "SUM", 1),
    "Current (%)": ("PERCENT", "AVERAGE_NONZERO", 0),
    "Yes/No": ("BOOLEAN", "COUNT_YES", 0),
}

STATES = [
    # code, name, zone, cohort, reporting
    ("BO", "Borno", "North East", "ORIGINAL", 1),
    ("EK", "Ekiti", "South West", "ORIGINAL", 1),
    ("KD", "Kaduna", "North West", "ORIGINAL", 1),
    ("KN", "Kano", "North West", "ORIGINAL", 1),
    ("KT", "Katsina", "North West", "ORIGINAL", 1),
    ("KE", "Kebbi", "North West", "ORIGINAL", 1),
    ("PL", "Plateau", "North Central", "ORIGINAL", 1),
    ("AD", "Adamawa", "North East", "ADDITIONAL", 1),
    ("BA", "Bauchi", "North East", "ADDITIONAL", 1),
    ("GO", "Gombe", "North East", "ADDITIONAL", 1),
    ("JI", "Jigawa", "North West", "ADDITIONAL", 1),
    ("KO", "Kogi", "North Central", "ADDITIONAL", 1),
    ("KW", "Kwara", "North Central", "ADDITIONAL", 1),
    ("NA", "Nasarawa", "North Central", "ADDITIONAL", 1),
    ("NI", "Niger", "North Central", "ADDITIONAL", 1),
    ("SO", "Sokoto", "North West", "ADDITIONAL", 1),
    ("YO", "Yobe", "North East", "ADDITIONAL", 1),
    ("ZA", "Zamfara", "North West", "ADDITIONAL", 1),
    ("DE", "Delta", "South South", "LIMITED", 0),
    ("EN", "Enugu", "South East", "LIMITED", 0),
    ("TA", "Taraba", "North East", "LIMITED", 0),
]

SUBCOMPONENTS = [
    ("PDO", "Project Development Objective", 1),
    ("C1", "Component 1 - general (composites, WASH, safeguarding, whole-school)", 2),
    ("C1.1", "Sub-component 1.1 - new school construction", 3),
    ("C1.2", "Sub-component 1.2 - existing schools and school improvement grants", 4),
    ("C2.1", "Sub-component 2.1 - community mobilisation and media outreach", 5),
    ("C2.2a", "Sub-component 2.2a - life skills and safe spaces", 6),
    ("C2.2b", "Sub-component 2.2b - digital literacy", 7),
    ("C2.2c", "Sub-component 2.2c - second-chance non-formal education", 8),
    ("C2.3", "Sub-component 2.3 - scholarships and social safety net", 9),
    ("C3", "Component 3 - cross-cutting, management and system strengthening", 10),
]

#: Everyone reports these; the rest depend on what a state implements.
UNIVERSAL = {"PDO", "C3", "C1", "C1.2", "C2.1"}
LIMITED_SET = UNIVERSAL                      # LF states: 1.2, 2.1, PDO, cross-cutting
FULL_SET = {code for code, _, _ in SUBCOMPONENTS}
EKITI_SET = FULL_SET - {"C1.1"}              # Ekiti does not implement 1.1


def read_q2_catalogue() -> list[tuple[str, str, str, str]]:
    """(component, old_code, name, type) in results-framework order."""
    workbook = load_workbook(Q2_MODEL, data_only=True)
    rows: list[tuple[str, str, str, str]] = []
    for sheet, component in [
        ("PDO", "PDO"), ("Component 1", "C1"), ("Component 2", "C2"), ("Component 3", "C3")
    ]:
        seen: set[str] = set()
        for row in workbook[sheet].iter_rows(min_row=5, values_only=True):
            code = str(row[0] or "").strip()
            if not re.match(r"^(PDO|C\d)-", code) or code in seen:
                continue
            seen.add(code)
            rows.append((component, code, str(row[1]).strip(), str(row[2] or "").strip()))
    return rows


def main() -> None:
    catalogue = read_q2_catalogue()
    missing = [code for _, code, _, _ in catalogue if code not in RECODE]
    if missing:
        raise SystemExit(f"No recode mapping for: {missing}")

    SEEDS.mkdir(exist_ok=True)

    with (SEEDS / "indicators.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "code", "legacy_code", "number", "name", "component", "subcomponent",
            "unit", "aggregation_method", "direction", "is_cumulative",
            "is_composite", "composite_of", "is_reported", "aliases",
        ])
        for number, (component, old, name, type_label) in enumerate(catalogue, start=1):
            new, subcomponent = RECODE[old]
            unit, aggregation, cumulative = TYPE_MAP[type_label]
            parts = COMPOSITES.get(new, [])
            writer.writerow([
                new, old, number, name, component, subcomponent,
                unit, aggregation, "INCREASE", cumulative,
                1 if parts else 0, "|".join(parts),
                0 if new == "C2.0-01" else 1,   # the derived row is computed, not collected
                old,                             # old code stays an alias for old files
            ])

    with (SEEDS / "states.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["code", "name", "geopolitical_zone", "cohort_code", "is_reporting"])
        writer.writerows(STATES)

    with (SEEDS / "subcomponents.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["code", "name", "sort_order"])
        writer.writerows(SUBCOMPONENTS)

    with (SEEDS / "state_subcomponents.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["state_code", "subcomponent_code", "implements"])
        for code, _name, _zone, cohort, _reporting in STATES:
            if cohort == "LIMITED":
                applicable = LIMITED_SET
            elif code == "EK":
                applicable = EKITI_SET
            else:
                applicable = FULL_SET
            for subcomponent in sorted(FULL_SET):
                writer.writerow([code, subcomponent, 1 if subcomponent in applicable else 0])

    print(f"indicators.csv          {len(catalogue)} rows")
    print(f"states.csv              {len(STATES)} rows")
    print(f"subcomponents.csv       {len(SUBCOMPONENTS)} rows")
    print(f"state_subcomponents.csv {len(STATES) * len(FULL_SET)} rows")


if __name__ == "__main__":
    main()
