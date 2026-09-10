"""Tests for JSON and XML routed by observed structure (TP-01 Group J, SPEC-01 requirement 6).

Requirement 6 says structure decides the route, not the extension: an array of records becomes
Parquet, everything else becomes field-path text for the chunker. These tests pin the boundary
between those two outcomes, because both mistakes are costly - flattening a document into a
table invents columns that were never there, and chunking a 5,000-row register instead of
tabulating it makes it unqueryable. ITR JSON is also full of zero-padded PANs and TANs, so the
round-trip typing rule matters here exactly as much as it does for spreadsheets.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ca_agent.config.settings import StructuredSettings, TabularSettings  # noqa: E402
from ca_agent.core.enums import ErrorCategory, FormatFamily, UnitType  # noqa: E402
from ca_agent.readers.structured import read_structured  # noqa: E402
from testdata.builders import files  # noqa: E402

_SETTINGS = StructuredSettings()
_TABULAR = TabularSettings()


def _read(tmp_path: Path, name: str, payload: bytes, family=FormatFamily.JSON):
    source = files.write_bytes(tmp_path / "src" / name, payload)
    return read_structured(
        source,
        destination=tmp_path / "out",
        settings=_SETTINGS,
        tabular_settings=_TABULAR,
        family=family,
    )


def _json(tmp_path: Path, document: object, name: str = "doc.json"):
    return _read(tmp_path, name, json.dumps(document).encode("utf-8"))


def _all_text(result) -> str:
    return "\n".join(unit.text for unit in result.units)


# --- collections become Parquet ------------------------------------------------------------


def test_tabular_json_collection_routes_to_parquet(tmp_path):
    # Arrange - the ITR shape: a homogeneous array of records inside a nested document
    result = _read(tmp_path, "itr.json", files.itr_json_bytes(record_count=5))

    # Assert - one table, and its field path is recorded as lineage
    assert len(result.tables) == 1
    table = result.tables[0]
    assert "ScheduleBP" in table.name
    assert table.row_count == 5


def test_json_leading_zero_identifier_survives_flattening(tmp_path):
    # Arrange - ITR JSON is full of zero-padded PANs, TANs and codes
    result = _read(tmp_path, "itr.json", files.itr_json_bytes(record_count=4))
    table = result.tables[0]

    # Act
    parquet = pq.read_table(tmp_path / "out" / table.output_relative_path)
    values = parquet.column("Code").to_pylist()

    # Assert - the same round-trip rule the tabular reader uses
    assert values == ["0001", "0002", "0003", "0004"]
    assert "string" in str(parquet.schema.field("Code").type)


def test_repeated_xml_elements_become_a_collection(tmp_path):
    # Arrange - XML is routed by observed structure exactly as JSON is
    rows = "".join(
        f"<Entry><SrNo>{index}</SrNo><Amount>{index * 100}</Amount></Entry>"
        for index in range(1, 7)
    )
    payload = f'<?xml version="1.0"?><Register>{rows}</Register>'.encode()

    # Act
    result = _read(tmp_path, "register.xml", payload, FormatFamily.XML)

    # Assert
    assert len(result.tables) == 1
    assert result.tables[0].row_count == 6


# --- everything else becomes field-path text --------------------------------------------------


def test_document_like_json_routes_to_chunking(tmp_path):
    # Arrange / Act
    result = _read(tmp_path, "note.json", files.nested_json_bytes())

    # Assert
    assert result.tables == ()
    assert result.units
    assert all(unit.unit_type is UnitType.FIELD_PATH for unit in result.units)
    assert "deeply nested prose value" in _all_text(result)


def test_a_short_array_is_not_treated_as_a_collection(tmp_path):
    # Arrange - two options are not a register
    document = {"Options": [{"Code": "A", "Value": 1}, {"Code": "B", "Value": 2}]}

    # Act
    result = _json(tmp_path, document)

    # Assert
    assert result.tables == ()
    assert "Options.0.Code" in _all_text(result)


def test_an_array_of_dissimilar_objects_is_not_treated_as_a_collection(tmp_path):
    # Arrange - key-set overlap is what separates a record array from a list of structures
    document = {
        "Items": [
            {"alpha": 1, "beta": 2},
            {"gamma": 3, "delta": 4},
            {"epsilon": 5, "zeta": 6},
            {"eta": 7, "theta": 8},
        ]
    }

    # Act
    result = _json(tmp_path, document)

    # Assert
    assert result.tables == ()


def test_an_array_of_scalars_is_not_treated_as_a_collection(tmp_path):
    # Arrange - no object elements at all
    document = {"Names": ["one", "two", "three", "four", "five", "six"]}

    # Act
    result = _json(tmp_path, document)

    # Assert
    assert result.tables == ()
    assert "Names.0" in _all_text(result)


def test_a_collection_of_deeply_nested_objects_is_not_flattened_to_parquet(tmp_path):
    # Arrange - flattening this would silently discard structure
    document = {
        "Records": [
            {"id": index, "detail": {"inner": {"deeper": {"value": index}}}}
            for index in range(5)
        ]
    }

    # Act
    result = _json(tmp_path, document)

    # Assert
    assert result.tables == ()


# --- field paths ----------------------------------------------------------------------------


def test_field_paths_use_dotted_notation_with_list_indices(tmp_path):
    # Arrange - SPEC-01 req 6: every chunk must trace back to its field path
    document = {"Schedule": {"Items": [10, 20, 30]}}

    # Act
    result = _json(tmp_path, document)
    text = _all_text(result)

    # Assert
    assert "Schedule.Items.0: 10" in text
    assert "Schedule.Items.2: 30" in text


def test_document_order_is_preserved_in_the_rendering(tmp_path):
    # Arrange
    document = {"First": "alpha", "Second": "beta", "Third": "gamma"}

    # Act
    text = _all_text(_json(tmp_path, document))

    # Assert
    assert text.index("alpha") < text.index("beta") < text.index("gamma")


def test_xml_elements_attributes_and_text_are_all_rendered(tmp_path):
    # Arrange - XML carries data in three places and dropping any one loses client data
    payload = b'<?xml version="1.0"?><Return period="Q1"><PAN>0001234A</PAN></Return>'

    # Act
    text = _all_text(_read(tmp_path, "r.xml", payload, FormatFamily.XML))

    # Assert
    assert "Q1" in text
    assert "0001234A" in text


def test_extracted_collections_are_not_duplicated_in_the_text_rendering(tmp_path):
    # Arrange - content written to Parquet must not also be embedded, or every row is stored twice
    document = {
        "Note": "prose that should be chunked",
        "Register": [{"SrNo": index, "Amount": index * 10} for index in range(5)],
    }

    # Act
    result = _json(tmp_path, document)
    text = _all_text(result)

    # Assert
    assert len(result.tables) == 1
    assert "prose that should be chunked" in text
    assert "Register.0.Amount" not in text, "rows belong in the Parquet output, not in chunks"
    assert "Register" in text, "the text must still reference where the table went"


# --- regressions found by running the real corpus ---------------------------------------------


def test_a_huge_document_is_split_into_bounded_units(tmp_path):
    """Corpus regression: units were grouped only by top-level field path.

    A Tally register has a single root child holding the whole document, so one corpus export
    rendered to a single TextUnit of 56.6 million characters - a memory and chunk-addressing
    hazard no matter what else is true.
    """
    # Arrange
    document = {"ENVELOPE": {f"Field{index}": f"value {index}" for index in range(4000)}}
    settings = StructuredSettings(max_unit_characters=2000)
    source = files.write_bytes(tmp_path / "src" / "big.json", json.dumps(document).encode())

    # Act
    result = read_structured(
        source,
        destination=tmp_path / "out",
        settings=settings,
        tabular_settings=_TABULAR,
        family=FormatFamily.JSON,
    )

    # Assert - many bounded units rather than one enormous one, each individually citable
    assert len(result.units) > 10
    assert all(len(unit.text) <= 2000 + 200 for unit in result.units)
    assert len({unit.unit_ref for unit in result.units}) == len(result.units)


def test_split_units_together_hold_every_line(tmp_path):
    # Arrange - splitting must not drop content
    document = {"Root": {f"Field{index}": index for index in range(500)}}
    settings = StructuredSettings(max_unit_characters=500)
    source = files.write_bytes(tmp_path / "src" / "big.json", json.dumps(document).encode())

    # Act
    result = read_structured(
        source,
        destination=tmp_path / "out",
        settings=settings,
        tabular_settings=_TABULAR,
        family=FormatFamily.JSON,
    )
    combined = "\n".join(unit.text for unit in result.units)

    # Assert
    assert combined.count("Root.Field") == 500


def test_malformed_xml_control_character_is_recovered_and_recorded(tmp_path):
    """Corpus regression: Tally writes raw control characters such as &#4; into its exports.

    Four corpus files carrying real ledger and item master data failed to parse at all. Recovery
    mode salvages them, and the fallback is recorded so it is never mistaken for a clean parse.
    """
    # Arrange
    payload = b'<?xml version="1.0"?><Root><Ledger>ACME&#4; TRADERS</Ledger></Root>'

    # Act
    result = _read(tmp_path, "tally.xml", payload, FormatFamily.XML)

    # Assert
    assert result.failure is None, "the file must be salvaged, not discarded"
    assert "ACME" in _all_text(result)
    assert any("recovery mode" in warning.message for warning in result.warnings)


def test_recovery_can_be_disabled(tmp_path):
    # Arrange - a run that wants only strictly valid XML must be able to say so
    payload = b'<?xml version="1.0"?><Root><Ledger>ACME&#4; TRADERS</Ledger></Root>'
    source = files.write_bytes(tmp_path / "src" / "tally.xml", payload)

    # Act
    result = read_structured(
        source,
        destination=tmp_path / "out",
        settings=StructuredSettings(recover_malformed_xml=False),
        tabular_settings=_TABULAR,
        family=FormatFamily.XML,
    )

    # Assert
    assert result.failure is not None
    assert result.failure.category is ErrorCategory.CORRUPT_FILE


# --- failures and safety ------------------------------------------------------------------------


def test_malformed_json_is_reported_not_raised(tmp_path):
    # Arrange / Act
    result = _read(tmp_path, "broken.json", b'{"a": [1, 2')

    # Assert
    assert result.failure is not None
    assert result.failure.category is ErrorCategory.CORRUPT_FILE
    assert result.tables == ()


def test_truncated_xml_is_recovered_with_a_warning(tmp_path):
    # Arrange - a truncated file still holding readable client data is worth salvaging, as long
    # as the recovery is recorded rather than passed off as a clean parse
    result = _read(tmp_path, "broken.xml", b"<Return><PAN>0001234A", FormatFamily.XML)

    # Assert
    assert result.failure is None
    assert "0001234A" in _all_text(result)
    assert any("recovery mode" in warning.message for warning in result.warnings)


def test_xml_with_no_recoverable_element_is_reported_not_raised(tmp_path):
    # Arrange / Act - nothing to salvage, so this is a genuine failure
    result = _read(tmp_path, "junk.xml", b"\x00\x01 not xml at all", FormatFamily.XML)

    # Assert
    assert result.failure is not None
    assert result.failure.category is ErrorCategory.CORRUPT_FILE


def test_xml_external_entity_is_not_resolved(tmp_path):
    # Arrange - an XXE payload in a client file must never read a local file or reach the network
    secret = files.write_bytes(tmp_path / "secret.txt", b"CONFIDENTIAL")
    payload = (
        '<?xml version="1.0"?>'
        f'<!DOCTYPE r [<!ENTITY xxe SYSTEM "file:///{secret.as_posix()}">]>'
        "<r><v>&xxe;</v></r>"
    ).encode()

    # Act
    result = _read(tmp_path, "xxe.xml", payload, FormatFamily.XML)

    # Assert - either the parse is refused or the entity is left unexpanded; never resolved
    assert "CONFIDENTIAL" not in _all_text(result)


def test_empty_json_document_is_recorded_without_failure(tmp_path):
    # Arrange / Act - an empty object is a recorded outcome, not an error
    result = _json(tmp_path, {})

    # Assert
    assert result.failure is None
    assert result.tables == ()


@pytest.mark.parametrize("family", [FormatFamily.PDF, FormatFamily.IMAGE])
def test_non_structured_family_is_rejected(tmp_path, family):
    # Arrange / Act / Assert - fail fast rather than producing a meaningless empty result
    with pytest.raises(ValueError):
        _read(tmp_path, "wrong.bin", b"data", family)
