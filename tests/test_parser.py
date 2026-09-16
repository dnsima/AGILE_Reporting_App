"""Template parsing: header detection, cover-block hints and file formats."""

from __future__ import annotations

import io

import pytest
from openpyxl import Workbook

from app.core.errors import IngestionError
from app.services.ingestion.parser import parse_upload

HEADERS = ["KPI Number", "Indicator Code", "Indicator Name", "Value", "Numerator", "Denominator"]


def _workbook(cover_rows, data_rows):
    workbook = Workbook()
    sheet = workbook.active
    for row in cover_rows:
        sheet.append(row)
    sheet.append(HEADERS)
    for row in data_rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_reads_headers_and_rows_from_xlsx():
    payload = _workbook([], [[1, "KPI-001", "Girls benefiting", 1200, None, None]])
    sheet = parse_upload(payload, "upload.xlsx")

    assert sheet.headers[:3] == ["KPI Number", "Indicator Code", "Indicator Name"]
    assert sheet.row_count == 1
    assert sheet.rows[0]["Indicator Code"] == "KPI-001"


def test_finds_the_header_row_beneath_a_cover_block():
    payload = _workbook(
        [
            ["AGILE Standardised State Reporting Template"],
            ["State:", "Kano"],
            ["Reporting Period:", "2026-Q1"],
            [],
        ],
        [[1, "KPI-001", "Girls benefiting", 1200, None, None]],
    )
    sheet = parse_upload(payload, "upload.xlsx")

    assert sheet.headers[1] == "Indicator Code"
    assert sheet.detected_state == "Kano"
    assert sheet.detected_period == "2026-Q1"
    assert sheet.rows[0]["Value"] == 1200


def test_source_row_numbers_point_back_at_the_spreadsheet():
    payload = _workbook(
        [["Cover"], ["State:", "Kano"], []],
        [[1, "KPI-001", "A", 1, None, None], [2, "KPI-002", "B", 2, None, None]],
    )
    sheet = parse_upload(payload, "upload.xlsx")
    assert [row["__row__"] for row in sheet.rows] == [5, 6]


def test_reads_csv():
    csv_payload = (
        b"KPI Number,Indicator Code,Value\n1,KPI-001,1200\n2,KPI-002,75\n"
    )
    sheet = parse_upload(csv_payload, "upload.csv")

    assert sheet.row_count == 2
    assert sheet.headers == ["KPI Number", "Indicator Code", "Value"]


def test_blank_rows_are_skipped():
    payload = _workbook([], [[1, "KPI-001", "A", 5, None, None], [None] * 6,
                             [2, "KPI-002", "B", 6, None, None]])
    assert parse_upload(payload, "upload.xlsx").row_count == 2


def test_duplicate_headers_are_disambiguated():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Indicator Code", "Value", "Value"])
    sheet.append(["KPI-001", 1, 2])
    buffer = io.BytesIO()
    workbook.save(buffer)

    parsed = parse_upload(buffer.getvalue(), "dupes.xlsx")
    assert parsed.headers == ["Indicator Code", "Value", "Value (2)"]


def test_empty_file_is_rejected():
    with pytest.raises(IngestionError, match="empty"):
        parse_upload(b"", "upload.xlsx")


def test_unsupported_extension_is_rejected():
    with pytest.raises(IngestionError, match="Unsupported file type"):
        parse_upload(b"some bytes", "report.docx")


def test_file_with_no_data_rows_is_rejected():
    with pytest.raises(IngestionError):
        parse_upload(_workbook([], []), "upload.xlsx")
