"""Purpose: maps an observed format to the processing route that will handle it. This lives in
L4 because choosing between the tabular, text, structured and vision routes requires knowing all
of them exist, and ARCHITECTURE.md forbids an L3 module depending on its siblings. SPEC-01 req 6
demands total coverage - every file enters reader selection and none is silently skipped - so the
table below is exhaustive over FormatFamily and a missing entry raises rather than defaulting.
"""

from __future__ import annotations

from dataclasses import dataclass

from ca_agent.core.enums import ErrorCategory, FormatFamily, ProcessingStatus, Route
from ca_agent.core.model import FormatProbe


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Which route handles this file, and - when it cannot be read - why.

    ``terminal_status`` is set only for families that can never be extracted. A readable family
    leaves it None because the outcome belongs to the reader, not the router.
    """

    route: Route
    reason: str
    provisional: bool = False
    terminal_status: ProcessingStatus | None = None
    error_category: ErrorCategory | None = None
    requires_format_document: bool = True


#: Families that have a working extraction path. Everything else is recorded, never skipped.
_READABLE_ROUTES: dict[FormatFamily, tuple[Route, str]] = {
    FormatFamily.SPREADSHEET_OOXML: (Route.TABULAR_PARQUET, "OOXML workbook"),
    FormatFamily.SPREADSHEET_BIFF: (Route.TABULAR_PARQUET, "legacy BIFF workbook"),
    FormatFamily.SPREADSHEET_XLSB: (Route.TABULAR_PARQUET, "binary workbook"),
    FormatFamily.DELIMITED_TEXT: (Route.TABULAR_PARQUET, "delimited text"),
    FormatFamily.IMAGE: (Route.VISION_IMAGE, "raster image requires vision extraction"),
    FormatFamily.WORD_OOXML: (Route.TEXT_EXTRACT, "OOXML word document"),
    FormatFamily.PRESENTATION_OOXML: (Route.TEXT_EXTRACT, "OOXML presentation"),
    FormatFamily.RTF: (Route.TEXT_EXTRACT, "rich text"),
    FormatFamily.HTML: (Route.TEXT_EXTRACT, "HTML document"),
    FormatFamily.PLAIN_TEXT: (Route.TEXT_EXTRACT, "plain text"),
    FormatFamily.EMAIL: (Route.TEXT_EXTRACT, "email message"),
    FormatFamily.JSON: (Route.STRUCTURED, "JSON routed by observed structure"),
    FormatFamily.XML: (Route.STRUCTURED, "XML routed by observed structure"),
    FormatFamily.ARCHIVE_ZIP: (Route.ARCHIVE, "ZIP archive"),
    FormatFamily.ARCHIVE_7Z: (Route.ARCHIVE, "7z archive"),
    FormatFamily.ARCHIVE_GZIP: (Route.ARCHIVE, "gzip stream"),
}

#: Families with no extraction path. Each still produces a record and a format document, which
#: is what makes acceptance criterion 9 ("every discovered file has a processing record") hold.
_UNREADABLE: dict[FormatFamily, tuple[Route, ProcessingStatus, ErrorCategory, str]] = {
    FormatFamily.ENCRYPTED_OOXML: (
        Route.NO_READER,
        ProcessingStatus.LOCKED,
        ErrorCategory.PASSWORD_PROTECTED_FILE,
        "encrypted OOXML package; no password is requested or guessed",
    ),
    FormatFamily.ENCRYPTED_OLE: (
        Route.NO_READER,
        ProcessingStatus.LOCKED,
        ErrorCategory.PASSWORD_PROTECTED_FILE,
        "encrypted OLE container; no password is requested or guessed",
    ),
    FormatFamily.TALLY_BINARY: (
        Route.NO_READER,
        ProcessingStatus.NO_READER,
        ErrorCategory.NO_COMPATIBLE_READER,
        "Tally company data; no Python library reads this format",
    ),
    FormatFamily.JAVA_SERIALIZED: (
        Route.NO_READER,
        ProcessingStatus.NO_READER,
        ErrorCategory.NO_COMPATIBLE_READER,
        "Java serialized object stream written by the e-filing utility, not the PDF its "
        "extension claims; never deserialised, because that is a code-execution vector",
    ),
    FormatFamily.KEYSTORE: (
        Route.NO_READER,
        ProcessingStatus.NO_READER,
        ErrorCategory.NO_COMPATIBLE_READER,
        "Java keystore holding key material, not client data",
    ),
    FormatFamily.UNKNOWN: (
        Route.NO_READER,
        ProcessingStatus.NO_READER,
        ErrorCategory.NO_COMPATIBLE_READER,
        "no signature matched and the content is not decodable text",
    ),
    FormatFamily.NON_DATA_ARTIFACT: (
        Route.NON_DATA,
        ProcessingStatus.EXCLUDED_NON_DATA,
        ErrorCategory.NON_DATA_ARTIFACT,
        "shell or application artifact carrying no client data",
    ),
    FormatFamily.EXECUTABLE: (
        Route.NON_DATA,
        ProcessingStatus.EXCLUDED_NON_DATA,
        ErrorCategory.EXECUTABLE_NOT_PROCESSED,
        "executable recorded but never run",
    ),
    FormatFamily.EMPTY: (
        Route.NON_DATA,
        ProcessingStatus.EXCLUDED_NON_DATA,
        ErrorCategory.EMPTY_FILE,
        "file contains no bytes",
    ),
}

#: Families whose route depends on an optional external binary being present.
_ARCHIVE_RAR_REASON = "RAR archive requires the external unrar binary"
_WORD_OLE_REASON = "legacy Word document requires an external LibreOffice converter"


def select_route(
    probe: FormatProbe,
    *,
    unrar_available: bool = False,
    soffice_available: bool = False,
) -> RouteDecision:
    """Choose the processing route for an observed format.

    The two optional-tool families degrade to a recorded NO_COMPATIBLE_READER rather than an
    exception, so a machine without LibreOffice or unrar still produces a complete, honest run.
    """
    family = probe.family

    if family is FormatFamily.ARCHIVE_RAR:
        return _optional_tool_route(probe, Route.ARCHIVE, unrar_available, _ARCHIVE_RAR_REASON)
    if family is FormatFamily.WORD_OLE:
        return _optional_tool_route(probe, Route.TEXT_EXTRACT, soffice_available, _WORD_OLE_REASON)
    if family is FormatFamily.PDF:
        return RouteDecision(
            route=Route.PDF_TEXT,
            reason=_with_evidence("PDF; page classification decides text versus vision", probe),
            # The header cannot tell a text PDF from a scanned one. The classifier may redirect
            # this to PDF_VISION once it has inspected every page (SPEC-01 req 2 versus req 3).
            provisional=True,
        )

    readable = _READABLE_ROUTES.get(family)
    if readable is not None:
        route, reason = readable
        return RouteDecision(route=route, reason=_with_evidence(reason, probe))

    unreadable = _UNREADABLE.get(family)
    if unreadable is None:
        raise ValueError(
            f"no routing decision defined for {family.value}; SPEC-01 req 6 requires every "
            "format to be routed rather than falling through"
        )
    route, status, category, reason = unreadable
    return RouteDecision(
        route=route,
        reason=_with_evidence(reason, probe),
        terminal_status=status,
        error_category=category,
    )


def _optional_tool_route(
    probe: FormatProbe, route: Route, available: bool, reason: str
) -> RouteDecision:
    if available:
        return RouteDecision(route=route, reason=_with_evidence(reason, probe))
    return RouteDecision(
        route=Route.NO_READER,
        reason=_with_evidence(f"{reason}, which is not configured", probe),
        terminal_status=ProcessingStatus.NO_READER,
        error_category=ErrorCategory.NO_COMPATIBLE_READER,
    )


def _with_evidence(reason: str, probe: FormatProbe) -> str:
    """Append the observations that justified this decision, for the format document."""
    notes = [reason]
    if probe.macros_present:
        notes.append("macros present but never executed")
    if probe.extension_conflict:
        notes.append(
            f"declared extension {probe.declared_extension or '(none)'} conflicts with "
            f"detected {probe.family.value}; content wins"
        )
    if probe.confidence == "extension":
        notes.append("identified by extension only, content was not conclusive")
    return "; ".join(notes)
