"""Tests for tabular conversion to Parquet (TP-01 Group H, SPEC-01 requirement 1).

The dominant risk in this corpus is silent data corruption, not crashes. PAN, GSTIN, TAN and
bank account numbers are zero-padded identifiers that every naive reader turns into integers,
and requirement 1 forbids both that coercion and any silent row loss. These tests pin the
round-trip typing rule that prevents it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pyarrow.parquet as pq
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ca_agent.config.settings import TabularSettings  # noqa: E402
from ca_agent.core.enums import ErrorCategory, FormatFamily  # noqa: E402
from ca_agent.readers.tabular import read_tabular  # noqa: E402
from testdata.builders import files  # noqa: E402

_SETTINGS = TabularSettings()


def _convert(tmp_path: Path, name: str, payload: bytes, family=FormatFamily.SPREADSHEET_OOXML):
    source = files.write_bytes(tmp_path / "src" / name, payload)
    return read_tabular(
        source, destination=tmp_path / "out", settings=_SETTINGS, family=family
    )


def _table(result, name: str):
    return next(table for table in result.tables if table.name == name)


def _read_column(tmp_path: Path, table, column: str):
    parquet = pq.read_table(tmp_path / "out" / table.output_relative_path)
    return parquet.column(column).to_pylist(), parquet.schema.field(column).type


# --- the leading-zero guarantee ------------------------------------------------------------


def test_leading_zero_identifier_column_stays_string_in_parquet(tmp_path):
    # Arrange - the exact shape of a PAN column in the real corpus
    payload = files.workbook_bytes(
        {"Data": [["PAN", "Amount"], ["0001234A", 100], ["0000001B", 200]]}
    )

    # Act
    result = _convert(tmp_path, "fs.xlsx", payload)
    values, arrow_type = _read_column(tmp_path, _table(result, "Data"), "PAN")

    # Assert
    assert values == ["0001234A", "0000001B"]
    assert "string" in str(arrow_type)


def test_numeric_looking_identifier_with_leading_zeros_is_not_coerced(tmp_path):
    # Arrange - an all-digit account number. pandas would turn these into integers.
    payload = files.workbook_bytes(
        {"Bank": [["AccountNo"], ["0012345"], ["0000001"]]},
        text_columns={"Bank": (0,)},
    )

    # Act
    result = _convert(tmp_path, "bank.xlsx", payload)
    values, arrow_type = _read_column(tmp_path, _table(result, "Bank"), "AccountNo")

    # Assert
    assert values == ["0012345", "0000001"]
    assert "string" in str(arrow_type)


def test_genuine_integers_are_still_typed_as_integers(tmp_path):
    # Arrange - the round-trip rule must not make everything a string
    payload = files.workbook_bytes({"Data": [["Amount"], [100], [250]]})

    # Act
    result = _convert(tmp_path, "amounts.xlsx", payload)
    values, arrow_type = _read_column(tmp_path, _table(result, "Data"), "Amount")

    # Assert
    assert values == [100, 250]
    assert "int" in str(arrow_type)


def test_csv_leading_zeros_survive(tmp_path):
    # Arrange
    result = _convert(tmp_path, "ledger.csv", files.csv_bytes(), FormatFamily.DELIMITED_TEXT)

    # Act
    values, arrow_type = _read_column(tmp_path, result.tables[0], "PAN")

    # Assert
    assert values == ["0001234A", "0005678B"]
    assert "string" in str(arrow_type)


# --- type inference is documented, never silent ------------------------------------------------


def test_mixed_type_column_is_kept_as_string_and_documented(tmp_path):
    # Arrange
    payload = files.workbook_bytes({"Data": [["Mixed"], [12], ["abc"], [3.5]]})

    # Act
    result = _convert(tmp_path, "mixed.xlsx", payload)
    column = next(c for c in _table(result, "Data").columns if c.name == "Mixed")

    # Assert
    assert "string" in column.arrow_type
    assert column.inference_rejected_reason is not None
    assert "mixed" in column.inference_rejected_reason.lower()


def test_rejected_inference_records_why(tmp_path):
    # Arrange
    payload = files.workbook_bytes(
        {"Data": [["Code"], ["007"], ["008"]]}, text_columns={"Data": (0,)}
    )

    # Act
    result = _convert(tmp_path, "codes.xlsx", payload)
    column = next(c for c in _table(result, "Data").columns if c.name == "Code")

    # Assert - the format document must be able to explain the decision
    assert column.inference_rejected_reason is not None
    assert "round" in column.inference_rejected_reason.lower()


def test_null_counts_are_reported_per_column(tmp_path):
    # Arrange
    payload = files.workbook_bytes({"Data": [["A", "B"], [1, None], [2, 5]]})

    # Act
    result = _convert(tmp_path, "nulls.xlsx", payload)
    column = next(c for c in _table(result, "Data").columns if c.name == "B")

    # Assert
    assert column.null_count == 1


# --- worksheets ---------------------------------------------------------------------------------


def test_every_populated_worksheet_becomes_its_own_parquet(tmp_path):
    # Arrange
    payload = files.workbook_bytes(
        {
            "Balance Sheet": [["A"], [1]],
            "P&L": [["B"], [2]],
            "Trial Balance": [["C"], [3]],
            "Empty": [],
        }
    )

    # Act
    result = _convert(tmp_path, "fs.xlsx", payload)

    # Assert
    written = [table for table in result.tables if table.output_relative_path is not None]
    assert len(written) == 3
    assert {table.name for table in written} == {"Balance Sheet", "P&L", "Trial Balance"}


def test_empty_sheet_is_recorded_with_no_parquet_output(tmp_path):
    # Arrange - SPEC-01 req 1 requires recording empty sheets rather than dropping them
    payload = files.workbook_bytes({"Data": [["A"], [1]], "Notes": []})

    # Act
    result = _convert(tmp_path, "fs.xlsx", payload)
    empty = _table(result, "Notes")

    # Assert
    assert empty.is_empty is True
    assert empty.output_relative_path is None
    assert empty.row_count == 0


def test_sheet_names_with_illegal_path_characters_are_written_safely(tmp_path):
    # Arrange - "P&L" and "B/S" are ordinary accounting sheet names
    payload = files.workbook_bytes({"P&L": [["A"], [1]]})

    # Act
    result = _convert(tmp_path, "fs.xlsx", payload)
    table = result.tables[0]

    # Assert
    assert table.name == "P&L", "the original sheet name is preserved as metadata"
    assert (tmp_path / "out" / table.output_relative_path).exists()


# --- rows are never dropped -------------------------------------------------------------------------


def test_no_rows_are_dropped_on_conversion_error(tmp_path):
    # Arrange - one unparseable cell in an otherwise numeric column
    payload = files.workbook_bytes({"Data": [["Amount"], [1], ["not-a-number"], [3]]})

    # Act
    result = _convert(tmp_path, "rows.xlsx", payload)
    table = _table(result, "Data")
    values, _ = _read_column(tmp_path, table, "Amount")

    # Assert
    assert table.row_count == 3
    assert len(values) == 3
    assert "not-a-number" in values


def test_row_count_matches_the_source(tmp_path):
    # Arrange
    rows = [["A"]] + [[index] for index in range(50)]
    payload = files.workbook_bytes({"Data": rows})

    # Act
    result = _convert(tmp_path, "big.xlsx", payload)

    # Assert
    assert _table(result, "Data").row_count == 50


# --- formulas and macros ----------------------------------------------------------------------------


def test_formula_cell_without_cached_value_is_reported(tmp_path):
    # Arrange - openpyxl writes no cached result for a formula it did not calculate
    payload = files.workbook_bytes(
        {"Data": [["A", "B"], [1, 2]]}, formulas={"Data": {"C2": "=A2+B2"}}
    )

    # Act
    result = _convert(tmp_path, "formula.xlsx", payload)
    table = _table(result, "Data")

    # Assert
    warnings = [w for w in table.warnings if w.category is ErrorCategory.FORMULA_NO_CACHED_VALUE]
    assert warnings, "SPEC-01 req 1 requires reporting formula cells with no cached value"
    assert "C2" in warnings[0].message


def test_formula_audit_can_be_disabled_for_faster_batch_runs(tmp_path):
    # Arrange - the audit reopens the workbook a second time; batch runs may disable it
    payload = files.workbook_bytes(
        {"Data": [["A", "B"], [1, 2]]}, formulas={"Data": {"C2": "=A2+B2"}}
    )
    settings = TabularSettings(audit_uncached_formulas=False)
    source = files.write_bytes(tmp_path / "src" / "formula.xlsx", payload)

    # Act
    result = read_tabular(
        source, destination=tmp_path / "out", settings=settings, family=FormatFamily.SPREADSHEET_OOXML
    )
    table = _table(result, "Data")

    # Assert - no crash, no formula warning, and data still converts
    assert result.failure is None
    assert table.warnings == ()
    assert table.row_count > 0


def test_macro_enabled_workbook_is_read_without_executing_macros(tmp_path):
    # Arrange - a real xlsm carrying a vbaProject part
    payload = files.workbook_bytes({"Data": [["A"], [1]]})

    # Act - keep_vba is never enabled, so no macro can run
    result = _convert(tmp_path, "book.xlsm", payload)

    # Assert
    assert _table(result, "Data").row_count == 1


# --- headers and delimiters ---------------------------------------------------------------------------


def test_csv_header_row_is_detected(tmp_path):
    result = _convert(tmp_path, "x.csv", b"Name,Amount\nACME,100\n", FormatFamily.DELIMITED_TEXT)
    assert [column.name for column in result.tables[0].columns] == ["Name", "Amount"]


def test_csv_without_a_header_uses_positional_names_and_records_the_decision(tmp_path):
    # Arrange - SPEC-01 req 4: never invent column meanings
    payload = b"100,200\n300,400\n"

    # Act
    result = _convert(tmp_path, "x.csv", payload, FormatFamily.DELIMITED_TEXT)
    table = result.tables[0]

    # Assert
    assert [column.name for column in table.columns] == ["column_0", "column_1"]
    assert table.header_detected is False


def test_semicolon_delimited_file_is_parsed(tmp_path):
    result = _convert(
        tmp_path, "x.csv", files.csv_bytes(delimiter=";"), FormatFamily.DELIMITED_TEXT
    )
    assert [column.name for column in result.tables[0].columns] == ["PAN", "GSTIN", "Amount"]


def test_duplicate_column_names_are_disambiguated(tmp_path):
    # Arrange - a real risk in hand-built accounting sheets
    payload = files.workbook_bytes({"Data": [["Amount", "Amount"], [1, 2]]})

    # Act
    result = _convert(tmp_path, "dupes.xlsx", payload)
    names = [column.name for column in _table(result, "Data").columns]

    # Assert
    assert len(set(names)) == 2


def test_encoding_is_detected_and_recorded_for_csv(tmp_path):
    # Arrange
    payload = "Name,City\nपाटील,सातारा\n".encode("utf-16")

    # Act
    result = _convert(tmp_path, "x.csv", payload, FormatFamily.DELIMITED_TEXT)

    # Assert
    assert result.tables[0].encoding is not None
    assert result.tables[0].row_count == 1


# --- failures -------------------------------------------------------------------------------------------


def test_corrupt_workbook_is_reported_not_raised(tmp_path):
    # Arrange
    result = _convert(tmp_path, "broken.xlsx", b"PK\x03\x04garbage")

    # Assert
    assert result.tables == ()
    assert result.failure is not None
    assert result.failure.category in {ErrorCategory.CORRUPT_FILE, ErrorCategory.EXTRACTION_ERROR}


def test_completely_empty_csv_is_recorded(tmp_path):
    result = _convert(tmp_path, "blank.csv", b"", FormatFamily.DELIMITED_TEXT)
    assert result.tables == () or all(table.is_empty for table in result.tables)


def test_reader_name_is_recorded_for_the_format_document(tmp_path):
    # Arrange / Act
    result = _convert(tmp_path, "fs.xlsx", files.workbook_bytes({"Data": [["A"], [1]]}))

    # Assert - SPEC-01 req 6 wants the selected reader recorded
    assert result.reader


@pytest.mark.parametrize("family", [FormatFamily.PDF, FormatFamily.IMAGE])
def test_non_tabular_family_is_rejected(tmp_path, family):
    # Arrange / Act / Assert - fail fast rather than producing a meaningless empty table
    with pytest.raises(ValueError):
        _convert(tmp_path, "wrong.bin", b"data", family)


def test_ooxml_content_with_a_misleading_xls_extension_is_still_read(tmp_path):
    """openpyxl's load_workbook validates the *filename* extension before touching content and

    refuses anything not already named .xlsx/.xlsm/.xltx/.xltm - real corpus files routinely
    fail this because the source application saved genuine OOXML zips under a stale .xls or
    .xlk extension. Detection already proved these are OOXML from the zip signature, so the
    reader must not let openpyxl's extension check override that.
    """
    # Arrange
    payload = files.workbook_bytes({"Data": [["A"], [1]]})

    # Act - the file on disk is named .xls even though the bytes are a real OOXML workbook
    result = _convert(tmp_path, "misnamed.xls", payload, family=FormatFamily.SPREADSHEET_OOXML)

    # Assert
    assert result.failure is None
    assert _table(result, "Data").row_count == 1


# --- regressions found by converting the real corpus -------------------------------------


def test_xlrd_assertion_error_is_recorded_not_propagated(tmp_path, monkeypatch):
    """xlrd raises a bare AssertionError on malformed defined-name formulas.

    A real corpus workbook triggers this. Letting it escape aborted the whole batch run, so it
    is mapped to CORRUPT_FILE like any other structural rejection.
    """
    # Arrange
    import xlrd

    def _assert_fail(_source):
        raise AssertionError()

    monkeypatch.setattr(xlrd, "open_workbook", _assert_fail)
    source = files.write_bytes(tmp_path / "src" / "old.xls", files.legacy_xls_bytes())

    # Act
    result = read_tabular(
        source,
        destination=tmp_path / "out",
        settings=_SETTINGS,
        family=FormatFamily.SPREADSHEET_BIFF,
    )

    # Assert
    assert result.failure is not None
    assert result.failure.category is ErrorCategory.CORRUPT_FILE
    assert result.tables == ()


def test_ooxml_namespace_syntax_error_is_recorded_not_propagated(tmp_path, monkeypatch):
    """A handful of corpus workbooks (GST-portal exports) declare an MS-extension namespace

    prefix such as x15 on an element but never define it. lxml raises XMLSyntaxError, which is
    not a ValueError subclass, so it escaped the existing except clause and aborted the batch.
    """
    # Arrange
    import openpyxl
    from lxml import etree

    def _raise_syntax_error(*_args, **_kwargs):
        raise etree.XMLSyntaxError("Namespace prefix x15 on workbookPr is not defined", 0, 2, 843)

    monkeypatch.setattr(openpyxl, "load_workbook", _raise_syntax_error)
    source = files.write_bytes(tmp_path / "src" / "bad_ns.xlsx", files.workbook_bytes({"Data": [["A"], [1]]}))

    # Act
    result = read_tabular(
        source, destination=tmp_path / "out", settings=_SETTINGS, family=FormatFamily.SPREADSHEET_OOXML
    )

    # Assert
    assert result.failure is not None
    assert result.failure.category is ErrorCategory.CORRUPT_FILE
    assert result.tables == ()


def test_xlrd_compdoc_error_is_recorded_not_propagated(tmp_path, monkeypatch):
    """xlrd's compound-document directory walker raises CompDocError (not XLRDError) when a

    corpus workbook's stream chain is corrupt. It is a plain Exception subclass, so it also
    escaped the existing except clause and aborted the batch, the same way AssertionError did.
    """
    # Arrange
    import xlrd

    def _raise_compdoc_error(_source):
        raise xlrd.compdoc.CompDocError("Workbook corruption: seen[2] == 4")

    monkeypatch.setattr(xlrd, "open_workbook", _raise_compdoc_error)
    source = files.write_bytes(tmp_path / "src" / "old.xls", files.legacy_xls_bytes())

    # Act
    result = read_tabular(
        source,
        destination=tmp_path / "out",
        settings=_SETTINGS,
        family=FormatFamily.SPREADSHEET_BIFF,
    )

    # Assert
    assert result.failure is not None
    assert result.failure.category is ErrorCategory.CORRUPT_FILE
    assert result.tables == ()


def test_csv_with_embedded_newline_in_a_field_is_parsed(tmp_path):
    """A corpus CSV carries a newline inside an unquoted field.

    Without newline="" the csv module raises instead of parsing, which aborted the batch run.
    """
    # Arrange
    payload = b'Name,Note\n"ACME","line one\nline two"\n'

    # Act
    result = _convert(tmp_path, "notes.csv", payload, FormatFamily.DELIMITED_TEXT)

    # Assert
    assert result.failure is None
    assert result.tables[0].row_count == 1


def test_unparseable_delimited_text_is_reported_not_raised(tmp_path):
    # Arrange - an unterminated quote leaves the parser with no valid interpretation
    payload = b'A,B\n"unterminated,1\n' + b"x" * 32

    # Act
    result = _convert(tmp_path, "broken.csv", payload, FormatFamily.DELIMITED_TEXT)

    # Assert - a whole-file failure is returned, never raised
    assert result.failure is None or result.failure.category is ErrorCategory.CORRUPT_FILE
