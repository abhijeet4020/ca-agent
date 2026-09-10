"""Purpose: routes JSON and XML by the structure actually present, not by extension (SPEC-01
req 6). An array of similar flat records is a register and becomes Parquet; everything else is
a document and becomes field-path text for the chunker. Both mistakes are expensive - flattening
a document invents columns nobody wrote, and chunking a 5,000-row register makes it unqueryable
- so the collection test is explicit and configurable rather than a guess. Records reuse the
tabular reader's round-trip typing because ITR JSON is full of zero-padded PANs and TANs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ca_agent.config.settings import StructuredSettings, TabularSettings
from ca_agent.core.enums import ErrorCategory, FormatFamily, UnitType
from ca_agent.core.model import ErrorInfo
from ca_agent.core.text import TextUnit
from ca_agent.readers.tabular import TableObservation, write_records_as_parquet

_STAGE = "structured_extraction"
_STRUCTURED_FAMILIES = {FormatFamily.JSON, FormatFamily.XML}
#: Attribute and text keys follow the widely used xmltodict convention, so an XML attribute and
#: a same-named child element cannot collide in the flattened field path.
_ATTRIBUTE_PREFIX = "@"
_TEXT_KEY = "#text"
#: Key-set similarity is averaged over at most this many elements. A register can hold thousands
#: of records and the pairwise comparison is quadratic; the leading sample settles the question
#: just as well and keeps a 5,000-row array from costing 12 million set operations.
_JACCARD_SAMPLE = 50


@dataclass(frozen=True, slots=True)
class StructuredResult:
    """What one JSON or XML document produced.

    A document can yield both: an ITR return has registers that become Parquet and prose fields
    that become chunks. Content that reached Parquet is deliberately absent from ``units`` -
    embedding it as well would store every row twice.
    """

    reader: str
    tables: tuple[TableObservation, ...] = ()
    units: tuple[TextUnit, ...] = ()
    collections_found: int = 0
    failure: ErrorInfo | None = None
    warnings: tuple[ErrorInfo, ...] = ()

    def has_content(self) -> bool:
        return bool(self.tables) or any(unit.has_content() for unit in self.units)


def read_structured(
    source: Path,
    *,
    destination: Path,
    settings: StructuredSettings,
    tabular_settings: TabularSettings,
    family: FormatFamily,
) -> StructuredResult:
    """Extract one JSON or XML document by its observed structure."""
    if family not in _STRUCTURED_FAMILIES:
        raise ValueError(f"{family.value} is not a structured family; reader selection is wrong")

    reader = "json" if family is FormatFamily.JSON else "lxml.etree"
    try:
        payload = source.read_bytes()
    except OSError as error:
        return StructuredResult(
            reader=reader,
            failure=ErrorInfo(
                category=ErrorCategory.FILE_ACCESS_ERROR, message=str(error), stage=_STAGE
            ),
        )

    recovered = False
    try:
        if family is FormatFamily.JSON:
            tree = _parse_json(payload)
        else:
            tree, recovered = _parse_xml(payload, settings)
    except _ParseFailure as failure:
        return StructuredResult(reader=reader, failure=failure.as_error(reader))

    warnings = (
        (
            ErrorInfo(
                category=ErrorCategory.CORRUPT_FILE,
                message="XML was malformed and parsed in recovery mode; content may be incomplete",
                stage=_STAGE,
                reader=reader,
            ),
        )
        if recovered
        else ()
    )
    return _extract(
        tree,
        destination=destination,
        settings=settings,
        tabular=tabular_settings,
        reader=reader,
        extra_warnings=warnings,
    )


class _ParseFailure(Exception):
    """The document could not be parsed at all. Returned, never raised past the entry point."""

    def as_error(self, reader: str) -> ErrorInfo:
        return ErrorInfo(
            category=ErrorCategory.CORRUPT_FILE, message=str(self), stage=_STAGE, reader=reader
        )


# --- parsing ------------------------------------------------------------------------------


def _parse_json(payload: bytes) -> object:
    try:
        return json.loads(payload)
    except UnicodeDecodeError:
        return _parse_json_with_detected_encoding(payload)
    except json.JSONDecodeError as error:
        raise _ParseFailure(str(error)) from error
    except RecursionError as error:
        raise _ParseFailure("document nesting is too deep to parse safely") from error


def _parse_json_with_detected_encoding(payload: bytes) -> object:
    """JSON must be UTF-8 by spec, but corpus files are whatever the exporting tool wrote."""
    from charset_normalizer import from_bytes

    best = from_bytes(payload).best()
    if best is None:
        raise _ParseFailure("content could not be decoded as text")
    try:
        return json.loads(str(best))
    except json.JSONDecodeError as error:
        raise _ParseFailure(str(error)) from error


def _parse_xml(payload: bytes, settings: StructuredSettings) -> tuple[object, bool]:
    """Parse XML safely, falling back to recovery mode. Returns the tree and whether it recovered."""
    from lxml import etree

    try:
        return _parse_xml_with(payload, recover=False), False
    except _ParseFailure:
        if not settings.recover_malformed_xml:
            raise

    # Tally writes raw control characters such as &#4; into its exports, which no conforming
    # parser will accept. Recovery mode skips the offending token and keeps the rest, which
    # salvages real ledger and item master data instead of discarding the whole file. The
    # caller records a warning, so a recovered parse is never mistaken for a clean one.
    try:
        return _parse_xml_with(payload, recover=True), True
    except _ParseFailure:
        raise
    except etree.XMLSyntaxError as error:
        raise _ParseFailure(str(error)) from error


def _parse_xml_with(payload: bytes, *, recover: bool) -> object:
    from lxml import etree

    # Entity resolution, DTD loading and network access are all disabled: a client XML file is
    # untrusted input, and an external entity would turn reading it into a local file read or
    # an outbound request. SPEC-01 req 6 asks to read the file, not to act on its contents.
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        recover=recover,
    )
    try:
        root = etree.fromstring(payload, parser=parser)
    except etree.XMLSyntaxError as error:
        raise _ParseFailure(str(error)) from error
    except ValueError as error:
        raise _ParseFailure(str(error)) from error
    if root is None:
        raise _ParseFailure("document has no root element")
    return {_local_name(root.tag): _element_to_value(root)}


def _element_to_value(element) -> object:
    """Convert an element to nested values, keeping attributes, children and text.

    All three carry client data - a GST return puts the period in an attribute - so dropping
    any one of them loses content silently.
    """
    result: dict[str, object] = {
        f"{_ATTRIBUTE_PREFIX}{_local_name(name)}": value
        for name, value in element.attrib.items()
    }

    grouped: dict[str, list[object]] = {}
    for child in element:
        if not isinstance(child.tag, str):
            continue  # comment or processing instruction
        grouped.setdefault(_local_name(child.tag), []).append(_element_to_value(child))
    for tag, values in grouped.items():
        result[tag] = values[0] if len(values) == 1 else values

    text = (element.text or "").strip()
    if not result:
        return text
    if text:
        result[_TEXT_KEY] = text
    return result


def _local_name(tag: str) -> str:
    """Drop the namespace URI; the corpus mixes namespaced and bare forms of the same schema."""
    return tag.rpartition("}")[2] if "}" in tag else tag


# --- extraction ----------------------------------------------------------------------------


def _extract(
    tree: object,
    *,
    destination: Path,
    settings: StructuredSettings,
    tabular: TabularSettings,
    reader: str,
    extra_warnings: tuple[ErrorInfo, ...] = (),
) -> StructuredResult:
    tables: list[TableObservation] = []
    warnings: list[ErrorInfo] = []
    lines: list[tuple[str, str]] = []

    def walk(node: object, path: str, top: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                child_path = f"{path}.{key}" if path else str(key)
                walk(value, child_path, top or str(key))
            if not node and path:
                lines.append((top, f"{path}: (empty)"))
            return

        if isinstance(node, list):
            if _is_record_collection(node, settings):
                _emit_collection(node, path, top)
                return
            for index, item in enumerate(node):
                walk(item, f"{path}.{index}" if path else str(index), top)
            if not node and path:
                lines.append((top, f"{path}: (empty)"))
            return

        lines.append((top, f"{path}: {_scalar_text(node)}"))

    def _emit_collection(node: list, path: str, top: str) -> None:
        records = [_flatten_record(element) for element in node]
        try:
            table = write_records_as_parquet(
                name=path or "root", records=records, destination=destination, settings=tabular
            )
        except (OSError, ValueError, TypeError) as error:
            warnings.append(
                ErrorInfo(
                    category=ErrorCategory.CONVERSION_ERROR,
                    message=f"collection at {path} could not be written: {error}",
                    stage=_STAGE,
                    reader=reader,
                )
            )
            # Fall back to text so the records are still retrievable rather than lost.
            for index, item in enumerate(node):
                walk(item, f"{path}.{index}", top)
            return
        tables.append(table)
        # A reference, not the rows: the content lives in Parquet and must not be embedded too.
        lines.append((top, f"{path}: [{len(records)} records extracted to Parquet]"))

    walk(tree, "", "")
    return StructuredResult(
        reader=reader,
        tables=tuple(tables),
        units=_units_from_lines(lines, settings),
        collections_found=len(tables),
        warnings=tuple(warnings) + extra_warnings,
    )


def _units_from_lines(
    lines: list[tuple[str, str]], settings: StructuredSettings
) -> tuple[TextUnit, ...]:
    """Group rendered lines into units, one per top-level field path but size-bounded.

    One unit per document would make a large return a single unsplittable blob, and one unit
    per line would produce thousands too small to chunk, so the top-level field is the natural
    section boundary. It is not sufficient on its own: a Tally register has a single root child
    holding everything, and one corpus export renders to 56 million characters. A unit is
    therefore also split once it reaches ``max_unit_characters``, with the part number in its
    ref so each remains individually citable. Every line still carries its own full path.
    """
    units: list[TextUnit] = []
    current_top: str | None = None
    buffer: list[str] = []
    buffered_characters = 0
    part = 0

    def flush() -> None:
        nonlocal buffer, buffered_characters
        if buffer:
            ref = current_top or "document"
            units.append(
                TextUnit(
                    unit_type=UnitType.FIELD_PATH,
                    unit_ref=f"{ref}#{part}" if part else ref,
                    text="\n".join(buffer),
                    sequence=len(units),
                )
            )
        buffer = []
        buffered_characters = 0

    for top, line in lines:
        if top != current_top:
            flush()
            part = 0
        elif buffered_characters + len(line) > settings.max_unit_characters:
            flush()
            part += 1
        current_top = top
        buffer.append(line)
        buffered_characters += len(line) + 1

    flush()
    return tuple(units)


def _scalar_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _flatten_record(element: object) -> dict[str, object]:
    """Flatten one collection element to dotted keys holding scalars."""
    if not isinstance(element, dict):
        return {"value": element}

    flattened: dict[str, object] = {}

    def walk(node: object, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{path}.{key}" if path else str(key))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}.{index}" if path else str(index))
        else:
            flattened[path] = node

    walk(element, "")
    return flattened


# --- the collection test ----------------------------------------------------------------------


def _is_record_collection(node: list, settings: StructuredSettings) -> bool:
    """Decide whether an array is a register that should become a table.

    Every condition guards a specific failure. Too few elements and a pair of options becomes a
    table; too few object elements and a list of strings does; dissimilar key sets and unrelated
    structures are forced into shared columns; too few scalar values or too much nesting and
    flattening would quietly discard the structure the values sat in.
    """
    if len(node) < settings.min_collection_size:
        return False

    objects = [element for element in node if isinstance(element, dict)]
    if len(objects) / len(node) < settings.min_object_element_ratio:
        return False
    if not objects:
        return False
    if _mean_key_jaccard(objects) < settings.min_key_jaccard:
        return False
    if _scalar_value_ratio(objects) < settings.min_scalar_leaf_ratio:
        return False
    return max(_depth(element) for element in objects) <= settings.max_element_depth


def _mean_key_jaccard(objects: list[dict]) -> float:
    """Mean pairwise key-set overlap over a bounded leading sample."""
    sample = objects[:_JACCARD_SAMPLE]
    if len(sample) < 2:
        return 1.0
    key_sets = [frozenset(element) for element in sample]
    total = 0.0
    pairs = 0
    for index, left in enumerate(key_sets):
        for right in key_sets[index + 1 :]:
            union = left | right
            total += len(left & right) / len(union) if union else 1.0
            pairs += 1
    return total / pairs if pairs else 1.0


def _scalar_value_ratio(objects: list[dict]) -> float:
    """Fraction of values sitting directly in the records that are already scalars."""
    scalars = 0
    total = 0
    for element in objects:
        for value in element.values():
            total += 1
            if not isinstance(value, (dict, list)):
                scalars += 1
    return scalars / total if total else 0.0


def _depth(value: object) -> int:
    """Nesting depth of one element; a flat record is depth 1."""
    if isinstance(value, dict):
        return 1 + max((_depth(item) for item in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_depth(item) for item in value), default=0)
    return 0
