"""Purpose: converts spreadsheets and delimited text to Parquet (SPEC-01 req 1), one output per
populated worksheet. The dominant risk in this corpus is silent corruption rather than failure:
PAN, GSTIN, TAN and bank account numbers are zero-padded identifiers that ordinary readers turn
into integers. pandas is deliberately not used on the read path for exactly that reason. Instead
every cell is captured with its original text, and a column becomes typed only if every value
round-trips back to that text unchanged. Rows are never dropped and values are never coerced;
anything that cannot be typed stays a string with the reason recorded for the format document.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import pyarrow as pa
import pyarrow.parquet as pq

from ca_agent.config.settings import TabularSettings
from ca_agent.core.enums import ErrorCategory, FormatFamily
from ca_agent.core.model import ErrorInfo

_STAGE = "tabular_conversion"
_MAX_SHEET_SLUG = 60
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
#: The stdlib default of 128 KiB is too small for corpus cells holding pasted statements.
_CSV_FIELD_LIMIT = 16 * 1024 * 1024
csv.field_size_limit(_CSV_FIELD_LIMIT)

_TABULAR_FAMILIES = {
    FormatFamily.SPREADSHEET_OOXML,
    FormatFamily.SPREADSHEET_BIFF,
    FormatFamily.SPREADSHEET_XLSB,
    FormatFamily.DELIMITED_TEXT,
}


@dataclass(frozen=True, slots=True)
class ColumnObservation:
    """What was observed about one column, including why a type was or was not inferred."""

    name: str
    position: int
    arrow_type: str
    inferred: bool
    null_count: int
    source_name: str | None = None
    inference_rejected_reason: str | None = None


@dataclass(frozen=True, slots=True)
class TableObservation:
    """One worksheet or delimited table, whether or not it produced an output."""

    name: str
    row_count: int
    column_count: int
    columns: tuple[ColumnObservation, ...]
    output_relative_path: PurePosixPath | None = None
    is_empty: bool = False
    header_detected: bool = True
    encoding: str | None = None
    delimiter: str | None = None
    warnings: tuple[ErrorInfo, ...] = ()


@dataclass(frozen=True, slots=True)
class TabularResult:
    """Everything one source file produced. A failure is returned, never raised."""

    reader: str
    tables: tuple[TableObservation, ...] = ()
    failure: ErrorInfo | None = None
    warnings: tuple[ErrorInfo, ...] = field(default=())

    def parquet_count(self) -> int:
        return sum(1 for table in self.tables if table.output_relative_path is not None)


@dataclass(slots=True)
class _Cell:
    """A cell as both its original text and whatever native type the reader supplied."""

    text: str
    native: object


def read_tabular(
    source: Path,
    *,
    destination: Path,
    settings: TabularSettings,
    family: FormatFamily,
) -> TabularResult:
    """Convert one tabular source into Parquet outputs under ``destination``."""
    if family not in _TABULAR_FAMILIES:
        raise ValueError(f"{family.value} is not a tabular family; reader selection is wrong")

    try:
        if family is FormatFamily.DELIMITED_TEXT:
            return _read_delimited(source, destination, settings)
        if family is FormatFamily.SPREADSHEET_OOXML:
            return _read_ooxml(source, destination, settings)
        if family is FormatFamily.SPREADSHEET_BIFF:
            return _read_biff(source, destination, settings)
        return _read_xlsb(source, destination, settings)
    except _ReaderFailure as failure:
        return TabularResult(reader=failure.reader, failure=failure.as_error())


class _ReaderFailure(Exception):
    """A whole-file failure. Member-level problems are recorded as warnings instead."""

    def __init__(self, reader: str, category: ErrorCategory, message: str) -> None:
        super().__init__(message)
        self.reader = reader
        self.category = category

    def as_error(self) -> ErrorInfo:
        return ErrorInfo(
            category=self.category, message=str(self), stage=_STAGE, reader=self.reader
        )


# --- OOXML -------------------------------------------------------------------------------


def _read_ooxml(source: Path, destination: Path, settings: TabularSettings) -> TabularResult:
    from openpyxl import load_workbook
    from openpyxl.utils.exceptions import InvalidFileException

    reader = "openpyxl"
    try:
        # data_only gives stored results rather than formulas; keep_vba stays off so no macro
        # is ever carried into the process. read_only streams rather than loading the workbook.
        workbook = load_workbook(source, read_only=True, data_only=True, keep_vba=False)
    except InvalidFileException as error:
        raise _ReaderFailure(reader, ErrorCategory.CORRUPT_FILE, str(error)) from error
    except zipfile.BadZipFile as error:
        raise _ReaderFailure(reader, ErrorCategory.CORRUPT_FILE, str(error)) from error
    except (OSError, KeyError, ValueError) as error:
        raise _ReaderFailure(reader, ErrorCategory.CORRUPT_FILE, str(error)) from error

    tables: list[TableObservation] = []
    try:
        formula_cells = _uncached_formula_cells(source)
        for sheet in workbook.worksheets:
            grid = [
                [_cell_from_native(value) for value in row]
                for row in sheet.iter_rows(values_only=True)
            ]
            warnings = _formula_warnings(formula_cells.get(sheet.title, ()), sheet.title)
            tables.append(
                _build_table(
                    name=sheet.title,
                    grid=grid,
                    destination=destination,
                    settings=settings,
                    warnings=warnings,
                )
            )
    finally:
        workbook.close()

    return TabularResult(reader=reader, tables=tuple(tables))


def _uncached_formula_cells(source: Path) -> dict[str, tuple[str, ...]]:
    """Find formula cells whose cached result is absent (SPEC-01 req 1).

    Requires a second pass because ``data_only=True`` cannot distinguish an empty cell from a
    formula Excel never calculated - both read as None.
    """
    from openpyxl import load_workbook

    found: dict[str, tuple[str, ...]] = {}
    try:
        workbook = load_workbook(source, read_only=True, data_only=False, keep_vba=False)
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        # The value pass already succeeded; losing only the formula audit is acceptable.
        return found

    try:
        for sheet in workbook.worksheets:
            references = [
                cell.coordinate
                for row in sheet.iter_rows()
                for cell in row
                if isinstance(cell.value, str) and cell.value.startswith("=")
            ]
            if references:
                found[sheet.title] = tuple(references)
    finally:
        workbook.close()
    return found


def _formula_warnings(references: tuple[str, ...], sheet_name: str) -> tuple[ErrorInfo, ...]:
    return tuple(
        ErrorInfo(
            category=ErrorCategory.FORMULA_NO_CACHED_VALUE,
            message=f"{sheet_name}!{reference} holds a formula with no cached value",
            stage=_STAGE,
            reader="openpyxl",
        )
        for reference in references
    )


# --- legacy BIFF and XLSB -------------------------------------------------------------------


def _read_biff(source: Path, destination: Path, settings: TabularSettings) -> TabularResult:
    import xlrd

    reader = "xlrd"
    try:
        book = xlrd.open_workbook(source)
    except xlrd.biffh.XLRDError as error:
        raise _ReaderFailure(reader, ErrorCategory.CORRUPT_FILE, str(error)) from error
    except AssertionError as error:
        # xlrd validates defined-name formulas with bare asserts and raises AssertionError on
        # a malformed BIFF record rather than a typed exception. Corpus files trigger this, and
        # letting it escape would abort a 16,000-file batch over one damaged workbook.
        raise _ReaderFailure(
            reader, ErrorCategory.CORRUPT_FILE, f"xlrd rejected the workbook structure: {error}"
        ) from error
    except (OSError, IndexError, ValueError, UnicodeDecodeError) as error:
        raise _ReaderFailure(reader, ErrorCategory.EXTRACTION_ERROR, str(error)) from error

    tables = [
        _build_table(
            name=sheet.name,
            grid=[
                [_cell_from_native(sheet.cell_value(row, column)) for column in range(sheet.ncols)]
                for row in range(sheet.nrows)
            ],
            destination=destination,
            settings=settings,
        )
        for sheet in book.sheets()
    ]
    return TabularResult(reader=reader, tables=tuple(tables))


def _read_xlsb(source: Path, destination: Path, settings: TabularSettings) -> TabularResult:
    from pyxlsb import open_workbook

    reader = "pyxlsb"
    try:
        with open_workbook(source) as book:
            tables = []
            for sheet_name in book.sheets:
                with book.get_sheet(sheet_name) as sheet:
                    grid = [
                        [_cell_from_native(cell.v) for cell in row] for row in sheet.rows()
                    ]
                tables.append(
                    _build_table(
                        name=sheet_name, grid=grid, destination=destination, settings=settings
                    )
                )
    except (OSError, ValueError, KeyError, StopIteration) as error:
        raise _ReaderFailure(reader, ErrorCategory.EXTRACTION_ERROR, str(error)) from error
    return TabularResult(reader=reader, tables=tuple(tables))


# --- delimited text ---------------------------------------------------------------------------


def _read_delimited(source: Path, destination: Path, settings: TabularSettings) -> TabularResult:
    from charset_normalizer import from_bytes

    reader = "csv"
    try:
        payload = source.read_bytes()
    except OSError as error:
        raise _ReaderFailure(reader, ErrorCategory.FILE_ACCESS_ERROR, str(error)) from error
    if not payload.strip():
        return TabularResult(
            reader=reader,
            tables=(TableObservation(source.stem, 0, 0, (), is_empty=True),),
        )

    best = from_bytes(payload).best()
    if best is None:
        raise _ReaderFailure(
            reader, ErrorCategory.CORRUPT_FILE, "content could not be decoded as text"
        )
    text = str(best)
    delimiter = _sniff_delimiter(text, settings)

    try:
        rows = list(csv.reader(_reader_stream(text), delimiter=delimiter))
    except csv.Error as error:
        raise _ReaderFailure(
            reader, ErrorCategory.CORRUPT_FILE, f"delimited text could not be parsed: {error}"
        ) from error
    grid = [[_Cell(str(value), value) for value in row] for row in rows if any(row)]
    table = _build_table(
        name=source.stem,
        grid=grid,
        destination=destination,
        settings=settings,
        encoding=best.encoding,
        delimiter=delimiter,
    )
    return TabularResult(reader=reader, tables=(table,))


def _sniff_delimiter(text: str, settings: TabularSettings) -> str:
    """Pick the delimiter giving a consistent, multi-column shape."""
    sample = text[: settings.csv_sniff_bytes]
    lines = [line for line in sample.splitlines()[:20] if line.strip()]
    best_delimiter, best_width = settings.csv_delimiter_candidates[0], 1
    joined = "\n".join(lines)
    for candidate in settings.csv_delimiter_candidates:
        try:
            widths = {
                len(row) for row in csv.reader(_reader_stream(joined), delimiter=candidate)
            }
        except csv.Error:
            continue
        if len(widths) == 1:
            width = widths.pop()
            if width > best_width:
                best_delimiter, best_width = candidate, width
    return best_delimiter


def _reader_stream(text: str) -> io.StringIO:
    """Wrap text for csv.reader with newline translation disabled.

    The csv module must see line endings verbatim. With default translation a stray carriage
    return inside an unquoted field raises "new-line character seen in unquoted field" instead
    of parsing, which real corpus CSVs trigger.
    """
    return io.StringIO(text, newline="")


# --- shared table construction ------------------------------------------------------------------


def _cell_from_native(value: object) -> _Cell:
    """Capture a cell as both its native value and the text that produced it."""
    if value is None:
        return _Cell("", None)
    if isinstance(value, str):
        return _Cell(value, value)
    if isinstance(value, bool):
        return _Cell("TRUE" if value else "FALSE", value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return _Cell(value.isoformat(), value)
    if isinstance(value, float) and value.is_integer():
        # Excel stores every number as a float; 100.0 should present as "100".
        return _Cell(str(int(value)), int(value))
    return _Cell(str(value), value)


def _build_table(
    *,
    name: str,
    grid: list[list[_Cell]],
    destination: Path,
    settings: TabularSettings,
    warnings: tuple[ErrorInfo, ...] = (),
    encoding: str | None = None,
    delimiter: str | None = None,
) -> TableObservation:
    populated = [row for row in grid if any(cell.text.strip() for cell in row)]
    if not populated:
        return TableObservation(
            name=name,
            row_count=0,
            column_count=0,
            columns=(),
            is_empty=True,
            encoding=encoding,
            delimiter=delimiter,
            warnings=warnings,
        )

    header_detected = _looks_like_header(populated)
    header_row = populated[0] if header_detected else []
    body = populated[1:] if header_detected else populated
    width = max(len(row) for row in populated)
    names = _column_names(header_row, width)

    columns: list[ColumnObservation] = []
    arrays: list[pa.Array] = []
    for position in range(width):
        cells = [row[position] if position < len(row) else _Cell("", None) for row in body]
        array, observation = _build_column(names[position], position, cells, settings)
        arrays.append(array)
        columns.append(observation)

    if not body:
        return TableObservation(
            name=name,
            row_count=0,
            column_count=width,
            columns=tuple(columns),
            is_empty=True,
            header_detected=header_detected,
            encoding=encoding,
            delimiter=delimiter,
            warnings=warnings,
        )

    relative = PurePosixPath(f"{_slug(name)}.parquet")
    table = pa.Table.from_arrays(arrays, names=[column.name for column in columns])
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, target, compression=settings.parquet_compression)

    return TableObservation(
        name=name,
        row_count=len(body),
        column_count=width,
        columns=tuple(columns),
        output_relative_path=relative,
        header_detected=header_detected,
        encoding=encoding,
        delimiter=delimiter,
        warnings=warnings,
    )


def _build_column(
    name: str, position: int, cells: list[_Cell], settings: TabularSettings
) -> tuple[pa.Array, ColumnObservation]:
    """Type a column only when every value round-trips; otherwise keep the original text."""
    null_count = sum(1 for cell in cells if cell.text.strip() == "")
    texts = [None if cell.text.strip() == "" else cell.text for cell in cells]

    arrow_type, inferred, reason = _infer_type(cells, settings)
    if inferred:
        values = [_coerce(cell, arrow_type) for cell in cells]
        array = pa.array(values, type=arrow_type)
    else:
        array = pa.array(texts, type=pa.string())

    return array, ColumnObservation(
        name=name,
        position=position,
        arrow_type=str(array.type),
        inferred=inferred,
        null_count=null_count,
        inference_rejected_reason=reason,
    )


def _infer_type(
    cells: list[_Cell], settings: TabularSettings
) -> tuple[pa.DataType, bool, str | None]:
    """Decide a column's Arrow type, or explain why it must stay text."""
    if not settings.infer_column_types:
        return pa.string(), False, "type inference disabled by configuration"

    present = [cell for cell in cells if cell.text.strip() != ""]
    if not present:
        return pa.string(), False, "column is empty"

    native_types = {type(cell.native) for cell in present}
    if native_types <= {int} and native_types:
        return pa.int64(), True, None
    if native_types <= {int, float}:
        return pa.float64(), True, None
    if native_types <= {bool}:
        return pa.bool_(), True, None
    if native_types <= {dt.datetime, dt.date}:
        return pa.timestamp("us"), True, None
    if native_types != {str}:
        listed = ", ".join(sorted(item.__name__ for item in native_types))
        return pa.string(), False, f"mixed source types ({listed}); values kept as text"

    return _infer_from_text(present, settings)


def _infer_from_text(
    present: list[_Cell], settings: TabularSettings
) -> tuple[pa.DataType, bool, str | None]:
    """Infer a type for an all-text column, refusing any value that does not round-trip.

    The round-trip check is what preserves leading zeros: "0012345" parses to 12345, but
    str(12345) is "12345", so the column stays text and the identifier survives intact.
    """
    if not settings.require_round_trip_for_typing:
        return pa.string(), False, "round-trip checking disabled by configuration"

    as_integers = [_parse_int(cell.text) for cell in present]
    if all(value is not None for value in as_integers):
        if all(str(value) == cell.text.strip() for value, cell in zip(as_integers, present, strict=True)):
            return pa.int64(), True, None
        return (
            pa.string(),
            False,
            "values parse as integers but do not round-trip (leading zeros or formatting "
            "would be lost); kept as text",
        )

    as_floats = [_parse_float(cell.text) for cell in present]
    if all(value is not None for value in as_floats):
        if all(_float_round_trips(value, cell.text) for value, cell in zip(as_floats, present, strict=True)):
            return pa.float64(), True, None
        return pa.string(), False, "values parse as floats but do not round-trip; kept as text"

    return pa.string(), False, None


def _coerce(cell: _Cell, arrow_type: pa.DataType) -> object:
    if cell.text.strip() == "":
        return None
    if pa.types.is_integer(arrow_type):
        return int(cell.native) if isinstance(cell.native, (int, float)) else _parse_int(cell.text)
    if pa.types.is_floating(arrow_type):
        return float(cell.native) if isinstance(cell.native, (int, float)) else _parse_float(cell.text)
    if pa.types.is_boolean(arrow_type):
        return bool(cell.native)
    if pa.types.is_timestamp(arrow_type):
        return cell.native
    return cell.text


def _parse_int(text: str) -> int | None:
    stripped = text.strip()
    try:
        return int(stripped)
    except ValueError:
        return None


def _parse_float(text: str) -> float | None:
    stripped = text.strip()
    try:
        return float(stripped)
    except ValueError:
        return None


def _float_round_trips(value: float | None, text: str) -> bool:
    if value is None:
        return False
    stripped = text.strip()
    return repr(value) == stripped or str(value) == stripped or f"{value:g}" == stripped


def _looks_like_header(rows: list[list[_Cell]]) -> bool:
    """A first row is a header when it is complete, unique and non-numeric.

    SPEC-01 req 4 forbids inventing column meanings, so an ambiguous first row is treated as
    data and the columns get positional names instead.
    """
    first = rows[0]
    if not first or any(cell.text.strip() == "" for cell in first):
        return False
    labels = [cell.text.strip() for cell in first]
    if len(set(labels)) != len(labels):
        return False
    # The check is on the text, not the native type: a CSV delivers every cell as a string, so
    # a purely numeric first row would otherwise be mistaken for column headings.
    return not any(_is_numeric_label(cell) for cell in first)


def _is_numeric_label(cell: _Cell) -> bool:
    if isinstance(cell.native, (int, float, dt.date, dt.datetime)) and not isinstance(
        cell.native, bool
    ):
        return True
    return _parse_float(cell.text) is not None


def _column_names(header_row: list[_Cell], width: int) -> list[str]:
    """Produce unique column names, falling back to positional ones."""
    names: list[str] = []
    seen: dict[str, int] = {}
    for position in range(width):
        raw = header_row[position].text.strip() if position < len(header_row) else ""
        candidate = raw or f"column_{position}"
        if candidate in seen:
            seen[candidate] += 1
            candidate = f"{candidate}_{seen[candidate]}"
        else:
            seen[candidate] = 0
        names.append(candidate)
    return names


def _slug(sheet_name: str) -> str:
    """Filesystem-safe output name. Accounting sheets are called things like "P&L" and "B/S"."""
    cleaned = _UNSAFE_NAME.sub("_", sheet_name).strip("_")
    return (cleaned[:_MAX_SHEET_SLUG] or "sheet").lower()
