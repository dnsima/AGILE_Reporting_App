"""Auto-mapping of incoming template columns onto the unified schema."""

from __future__ import annotations

from app.services.ingestion.mapper import SchemaMapper, map_rows, to_number
from app.services.reference import active_indicators


class TestNumberParsing:
    def test_plain_and_formatted_numbers(self):
        assert to_number("1234") == 1234
        assert to_number("1,234.5") == 1234.5
        assert to_number("45%") == 45
        assert to_number(" 12 ") == 12
        assert to_number("₦ 1,500") == 1500

    def test_parenthesised_values_are_negative(self):
        assert to_number("(250)") == -250

    def test_missing_markers_become_none(self):
        for token in ["", "N/A", "n/a", "-", "nil", "TBD", "no data", None]:
            assert to_number(token) is None

    def test_unparseable_text_is_none(self):
        assert to_number("see attached note") is None


class TestHeaderResolution:
    def test_exact_synonyms(self, db):
        mapper = SchemaMapper(active_indicators(db))
        assert mapper.resolve_header("Indicator Code").mapped_field == "indicator_code"
        assert mapper.resolve_header("Value").mapped_field == "value"
        assert mapper.resolve_header("Numerator").mapped_field == "numerator"

    def test_alternative_wording_still_resolves(self, db):
        mapper = SchemaMapper(active_indicators(db))
        for header in ["Actual Achievement", "Reported Value", "Achievement"]:
            assert mapper.resolve_header(header).mapped_field == "value", header

    def test_near_miss_header_resolves_by_fuzzy_match(self, db):
        mapper = SchemaMapper(active_indicators(db))
        mapping = mapper.resolve_header("Denomenator")  # misspelled in the template
        assert mapping.mapped_field == "denominator"
        assert mapping.strategy == "fuzzy"

    def test_unknown_header_is_left_unmapped(self, db):
        mapper = SchemaMapper(active_indicators(db))
        assert mapper.resolve_header("Officer signature").mapped_field is None


class TestIndicatorResolution:
    def test_by_code_number_and_name(self, db):
        mapper = SchemaMapper(active_indicators(db))
        assert mapper.resolve_indicator("KPI-001")[0].code == "KPI-001"
        assert mapper.resolve_indicator("kpi 1")[0].code == "KPI-001"
        assert mapper.resolve_indicator("1")[0].code == "KPI-001"
        assert (
            mapper.resolve_indicator("Number of girls benefiting from the project")[0].code
            == "KPI-001"
        )

    def test_by_configured_alias(self, db):
        mapper = SchemaMapper(active_indicators(db))
        indicator, strategy = mapper.resolve_indicator("girls benefiting")
        assert indicator.code == "KPI-001"
        assert strategy == "name"

    def test_unknown_label_is_unmatched(self, db):
        mapper = SchemaMapper(active_indicators(db))
        indicator, strategy = mapper.resolve_indicator("Something entirely unrelated")
        assert indicator is None
        assert strategy == "unmatched"


class TestRowMapping:
    def test_long_layout(self, db):
        headers = ["Indicator Code", "Value", "Numerator", "Denominator", "Sex"]
        rows = [
            {"Indicator Code": "KPI-001", "Value": "1,200", "Numerator": None,
             "Denominator": None, "Sex": "female", "__row__": 2},
            {"Indicator Code": "KPI-002", "Value": "75.5", "Numerator": "755",
             "Denominator": "1000", "Sex": "total", "__row__": 3},
        ]
        result = map_rows(headers, rows, active_indicators(db))

        assert result.layout == "long"
        assert result.mapped_rows == 2
        assert [v.indicator.code for v in result.values] == ["KPI-001", "KPI-002"]
        assert result.values[0].value == 1200
        assert result.values[0].disaggregation == {"sex": "female"}
        assert result.values[1].numerator == 755
        assert result.values[1].denominator == 1000

    def test_wide_layout_one_column_per_indicator(self, db):
        headers = ["State", "KPI-001", "KPI-004"]
        rows = [{"State": "Kano", "KPI-001": "900", "KPI-004": "45", "__row__": 2}]
        result = map_rows(headers, rows, active_indicators(db))

        assert result.layout == "wide"
        assert result.mapped_rows == 1
        assert {v.indicator.code: v.value for v in result.values} == {
            "KPI-001": 900, "KPI-004": 45,
        }
        assert result.detected_state == "Kano"

    def test_unrecognised_indicator_rows_are_reported_not_silently_dropped(self, db):
        headers = ["Indicator", "Value"]
        rows = [
            {"Indicator": "KPI-001", "Value": "10", "__row__": 2},
            {"Indicator": "Some unknown indicator name", "Value": "20", "__row__": 3},
        ]
        result = map_rows(headers, rows, active_indicators(db))

        assert result.mapped_rows == 1
        assert result.unmapped_rows == 1
        assert result.unmatched_indicators == ["Some unknown indicator name"]
