"""Purpose: defines what the model is asked to return and proves that it did (SPEC-01 req 3).
The wire format is a strict JSON schema rather than prose, because "did this extraction work"
then becomes a checkable question instead of a judgement about text. The reply is validated
here regardless of what the endpoint promised, since not every OpenAI-compatible server
honours response_format. Validated output is rendered to the Markdown the specification asks
for on disk, so the JSON never becomes the artifact.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

#: Values the model must use rather than inventing something plausible. An invented figure in
#: an audit file is the worst failure this pipeline can produce, so the marker is part of the
#: contract and is preserved verbatim all the way to the rendered document.
UNREADABLE_MARKER = "[UNREADABLE]"
_CONFIDENCE_VALUES = ("high", "medium", "low")

#: The JSON schema sent as response_format. Kept flat and closed - no nested objects beyond one
#: level, every property required, additionalProperties false - because strict-mode structured
#: output is only supported for schemas of that shape across the endpoints this may run against.
RESPONSE_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": ["document_type", "summary", "visible_text", "fields", "tables", "uncertainties"],
    "properties": {
        "document_type": {
            "type": "string",
            "description": "What this document appears to be, for example 'GST payment challan'.",
        },
        "summary": {
            "type": "string",
            "description": "One or two sentences describing the content.",
        },
        "visible_text": {
            "type": "string",
            "description": "Every piece of text visible in the image, transcribed exactly.",
        },
        "fields": {
            "type": "array",
            "description": "Labelled values such as PAN, GSTIN, invoice number, dates, amounts.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["label", "value", "confidence"],
                "properties": {
                    "label": {"type": "string"},
                    "value": {"type": "string"},
                    "confidence": {"type": "string", "enum": list(_CONFIDENCE_VALUES)},
                },
            },
        },
        "tables": {
            "type": "array",
            "description": "Tabular data, one entry per table, rows in the order shown.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["caption", "columns", "rows"],
                "properties": {
                    "caption": {"type": "string"},
                    "columns": {"type": "array", "items": {"type": "string"}},
                    "rows": {
                        "type": "array",
                        "items": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        },
        "uncertainties": {
            "type": "array",
            "description": "Anything unreadable, ambiguous or partly obscured.",
            "items": {"type": "string"},
        },
    },
}

EXTRACTION_PROMPT = f"""Extract the contents of this document image for a chartered accountant's
records. Return only JSON matching the provided schema.

Rules that matter more than completeness:
- Transcribe values exactly as printed. Preserve leading zeros, punctuation and spacing in
  identifiers such as PAN, GSTIN, TAN, account numbers and invoice numbers.
- Never infer, correct, complete or calculate a value. If a value is unreadable write
  {UNREADABLE_MARKER} and describe the problem in uncertainties.
- Do not translate, summarise or reformat amounts and dates. Copy what is shown.
- If the image is blank or contains no readable content, say so in summary and leave
  visible_text empty rather than describing the paper.
"""


class ContractError(Exception):
    """The reply did not satisfy the contract. Never downgraded to a warning."""


@dataclass(frozen=True, slots=True)
class ExtractedField:
    """One labelled value read off the document."""

    label: str
    value: str
    confidence: str

    def is_unreadable(self) -> bool:
        return UNREADABLE_MARKER in self.value


@dataclass(frozen=True, slots=True)
class ExtractedTable:
    """One table, rows in the order they appear."""

    caption: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class VisionExtraction:
    """A validated extraction. Constructing one is the proof that the reply was usable."""

    document_type: str
    summary: str
    visible_text: str
    fields: tuple[ExtractedField, ...]
    tables: tuple[ExtractedTable, ...]
    uncertainties: tuple[str, ...]

    def has_content(self) -> bool:
        """Distinguishes a real extraction from an empty one.

        A model that returns a well-formed but wholly empty object has not read the document,
        and SPEC-01 req 3 forbids reporting that as a success.
        """
        return bool(self.visible_text.strip() or self.fields or self.tables)


def parse_extraction(content: str) -> VisionExtraction:
    """Validate a reply against the contract. Raises ContractError rather than guessing."""
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ContractError(f"reply was not JSON: {error}") from error

    if not isinstance(payload, dict):
        raise ContractError("reply was not a JSON object")

    missing = [key for key in RESPONSE_SCHEMA["required"] if key not in payload]
    if missing:
        raise ContractError(f"reply is missing required key(s): {', '.join(sorted(missing))}")

    return VisionExtraction(
        document_type=_text(payload, "document_type"),
        summary=_text(payload, "summary"),
        visible_text=_text(payload, "visible_text"),
        fields=tuple(_field(item) for item in _sequence(payload, "fields")),
        tables=tuple(_table(item) for item in _sequence(payload, "tables")),
        uncertainties=tuple(str(item) for item in _sequence(payload, "uncertainties")),
    )


def _text(payload: dict, key: str) -> str:
    value = payload[key]
    if not isinstance(value, str):
        raise ContractError(f"{key} must be a string, got {type(value).__name__}")
    return value


def _sequence(payload: dict, key: str) -> list:
    value = payload[key]
    if not isinstance(value, list):
        raise ContractError(f"{key} must be an array, got {type(value).__name__}")
    return value


def _field(item: object) -> ExtractedField:
    if not isinstance(item, dict) or not {"label", "value"} <= set(item):
        raise ContractError("each field must be an object with label and value")
    confidence = str(item.get("confidence", "medium")).lower()
    return ExtractedField(
        label=str(item["label"]),
        value=str(item["value"]),
        confidence=confidence if confidence in _CONFIDENCE_VALUES else "medium",
    )


def _table(item: object) -> ExtractedTable:
    if not isinstance(item, dict) or "rows" not in item:
        raise ContractError("each table must be an object with rows")
    rows = item["rows"]
    if not isinstance(rows, list):
        raise ContractError("table rows must be an array")
    return ExtractedTable(
        caption=str(item.get("caption", "")),
        columns=tuple(str(column) for column in item.get("columns", []) or []),
        rows=tuple(
            tuple(str(cell) for cell in row) for row in rows if isinstance(row, list)
        ),
    )


# --- rendering ------------------------------------------------------------------------------


def render_markdown(extraction: VisionExtraction) -> str:
    """Render a validated extraction as the Markdown SPEC-01 req 3 asks for on disk.

    Section order is fixed so two runs over the same page produce comparable documents, and
    every section is emitted even when empty, so a reader can tell "nothing found" from
    "never looked".
    """
    lines: list[str] = []
    if extraction.document_type:
        lines.append(f"**Document type:** {extraction.document_type}")
    if extraction.summary:
        lines.append("")
        lines.append(extraction.summary)

    lines.extend(["", "## Visible Text", "", extraction.visible_text.strip() or "_None._"])

    lines.extend(["", "## Fields and Values", ""])
    if extraction.fields:
        lines.append("| Field | Value | Confidence |")
        lines.append("| --- | --- | --- |")
        lines.extend(
            f"| {_cell(field.label)} | {_cell(field.value)} | {_cell(field.confidence)} |"
            for field in extraction.fields
        )
    else:
        lines.append("_None._")

    lines.extend(["", "## Tables", ""])
    if extraction.tables:
        for table in extraction.tables:
            lines.extend(_render_table(table))
    else:
        lines.append("_None._")

    lines.extend(["", "## Uncertainties", ""])
    if extraction.uncertainties:
        lines.extend(f"- {item}" for item in extraction.uncertainties)
    else:
        lines.append("_None reported._")

    return "\n".join(lines).strip() + "\n"


def _render_table(table: ExtractedTable) -> list[str]:
    lines: list[str] = []
    if table.caption:
        lines.append(f"**{table.caption}**")
        lines.append("")
    width = max(len(table.columns), *(len(row) for row in table.rows), 0)
    if not width:
        return [*lines, "_Empty table._", ""]

    headers = [*table.columns, *([""] * (width - len(table.columns)))]
    lines.append("| " + " | ".join(_cell(header) for header in headers) + " |")
    lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
    for row in table.rows:
        padded = [*row, *([""] * (width - len(row)))]
        lines.append("| " + " | ".join(_cell(cell) for cell in padded) + " |")
    lines.append("")
    return lines


def _cell(value: str) -> str:
    """Escape a value for a Markdown table cell.

    A pipe inside a transcribed value would silently split the row into different columns,
    which turns a correct extraction into a wrong table.
    """
    return value.replace("\\", "\\\\").replace("|", r"\|").replace("\n", " ").strip()
