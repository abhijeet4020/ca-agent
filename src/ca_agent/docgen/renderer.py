"""Purpose: renders a FormatDocument as the per-file Markdown SPEC-01 req 4 requires. The
rendering rules exist to keep the document honest rather than tidy: every section is emitted
even when empty, because an omitted section cannot be told from one nobody attempted; absent
values print as UNKNOWN rather than blank; and table cells are escaped, since a pipe inside a
transcribed column name would otherwise reshape the table and turn a correct observation into
a wrong one.
"""

from __future__ import annotations

from ca_agent.core.model import ErrorInfo
from ca_agent.docgen.model import UNKNOWN, DocumentSection, DocumentTable, FormatDocument

_NONE_FOUND = "_None._"


def render_format_document(document: FormatDocument) -> str:
    """Render one file's format document."""
    identity = document.identity
    lines = [f"# {identity.display_name()}", ""]
    lines.extend(_identity_section(document))
    lines.extend(_outcome_section(document))

    for section in document.sections:
        lines.extend(_section(section))

    lines.extend(_outputs_section(document))
    lines.extend(_diagnostics_section(document))
    return "\n".join(lines).rstrip() + "\n"


def _identity_section(document: FormatDocument) -> list[str]:
    identity = document.identity
    rows: list[tuple[str, str]] = [
        ("Source path", identity.source_relpath),
        ("Category", identity.category),
        ("Client", identity.client),
        ("Client scope", identity.scope_id),
        ("Extension", identity.extension or "(none)"),
        ("Detected format", identity.detected_format or UNKNOWN),
        ("Detected by", identity.detection_confidence or UNKNOWN),
        ("Content SHA-256", identity.content_sha256 or UNKNOWN),
        ("Size in bytes", str(identity.size_bytes)),
        ("Output version", identity.version_id or UNKNOWN),
    ]
    if identity.extension_conflict:
        rows.append(
            (
                "Extension conflict",
                f"the extension {identity.extension or '(none)'} does not match the detected "
                f"format {identity.detected_format}; content decided the route",
            )
        )
    if identity.archive_id or identity.member_path:
        rows.append(("Originating archive", identity.archive_id or UNKNOWN))
        rows.append(("Member path", identity.member_path or UNKNOWN))

    return ["## Source", "", *_definition_list(rows), ""]


def _outcome_section(document: FormatDocument) -> list[str]:
    rows = [
        ("Processing route", document.route.value),
        ("Processing status", document.status.value),
        ("Reader selected", document.reader or UNKNOWN),
    ]
    return ["## Processing", "", *_definition_list(rows), ""]


def _outputs_section(document: FormatDocument) -> list[str]:
    lines = ["## Outputs", ""]
    if not document.outputs:
        return [*lines, _NONE_FOUND, ""]
    lines.append("| Kind | Path | Detail |")
    lines.append("| --- | --- | --- |")
    lines.extend(
        f"| {_cell(output.kind)} | {_cell(output.relative_path.as_posix())} "
        f"| {_cell(output.detail or '')} |"
        for output in document.outputs
    )
    return [*lines, ""]


def _diagnostics_section(document: FormatDocument) -> list[str]:
    lines: list[str] = []
    if document.error is not None:
        lines.extend(["## Failure", "", *_definition_list(_error_rows(document.error)), ""])
        if document.retry_action:
            lines.extend(["**Retry action:** " + document.retry_action, ""])

    lines.extend(["## Warnings", ""])
    if not document.warnings:
        return [*lines, "_None._", ""]
    lines.append("| Category | Stage | Message |")
    lines.append("| --- | --- | --- |")
    lines.extend(
        f"| {_cell(warning.category.value)} | {_cell(warning.stage)} "
        f"| {_cell(warning.message)} |"
        for warning in document.warnings
    )
    return [*lines, ""]


def _error_rows(error: ErrorInfo) -> list[tuple[str, str]]:
    return [
        ("Error category", error.category.value),
        ("Processing stage", error.stage),
        ("Reader", error.reader or UNKNOWN),
        ("Message", error.message),
    ]


def _section(section: DocumentSection) -> list[str]:
    lines = [f"## {section.heading}", ""]
    if section.is_empty():
        return [*lines, _NONE_FOUND, ""]

    if section.rows:
        lines.extend(_definition_list(list(section.rows)))
        lines.append("")
    if section.table is not None and not section.table.is_empty():
        lines.extend(_table(section.table))
    if section.notes:
        lines.extend(section.notes)
        lines.append("")
    return lines


def _table(table: DocumentTable) -> list[str]:
    lines: list[str] = []
    if table.caption:
        lines.extend([f"**{table.caption}**", ""])
    width = max(len(table.columns), *(len(row) for row in table.rows), 0)
    if not width:
        return [*lines, _NONE_FOUND, ""]

    headers = [*table.columns, *([""] * (width - len(table.columns)))]
    lines.append("| " + " | ".join(_cell(header) for header in headers) + " |")
    lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
    for row in table.rows:
        padded = [*row, *([""] * (width - len(row)))]
        lines.append("| " + " | ".join(_cell(cell) for cell in padded) + " |")
    return [*lines, ""]


def _definition_list(rows: list[tuple[str, str]]) -> list[str]:
    return [f"- **{label}:** {value if value != '' else UNKNOWN}" for label, value in rows]


def _cell(value: str) -> str:
    """Escape a value for a Markdown table cell.

    A pipe inside a transcribed column name or message would split the row into different
    columns, silently turning a correct observation into a wrong table.
    """
    return value.replace("\\", "\\\\").replace("|", r"\|").replace("\n", " ").strip()
