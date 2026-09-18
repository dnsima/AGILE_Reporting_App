"""Data ingestion: parsing, schema auto-mapping and the upload pipeline."""

from app.services.ingestion.mapper import SchemaMapper, map_rows
from app.services.ingestion.parser import ParsedSheet, parse_upload
from app.services.ingestion.pipeline import ingest_manual, ingest_upload
from app.services.ingestion.template import build_reporting_template, reporting_basis

__all__ = [
    "ParsedSheet",
    "SchemaMapper",
    "build_reporting_template",
    "ingest_manual",
    "ingest_upload",
    "map_rows",
    "parse_upload",
    "reporting_basis",
]
