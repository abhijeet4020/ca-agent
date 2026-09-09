"""Tests for reader selection (TP-01 Group E, SPEC-01 requirement 6).

Requirement 6 is absolute: every discovered file enters reader selection and nothing is
silently skipped. The property that matters most here is total coverage - every FormatFamily
must map to a route - so a new family cannot be added without a routing decision being made
for it.
"""

from __future__ import annotations

import pytest

from ca_agent.core.enums import ErrorCategory, FormatFamily, ProcessingStatus, Route
from ca_agent.core.model import FormatProbe
from ca_agent.pipeline.routing import select_route


def _probe(family: FormatFamily, **overrides) -> FormatProbe:
    defaults = {"confidence": "signature", "declared_extension": ".bin"}
    return FormatProbe(family=family, **{**defaults, **overrides})


@pytest.mark.parametrize("family", list(FormatFamily))
def test_every_format_family_has_a_route(family):
    # Arrange / Act - SPEC-01 req 6 forbids any file falling through unrouted
    decision = select_route(_probe(family))

    # Assert
    assert isinstance(decision.route, Route)
    assert decision.reason, "every routing decision must record why it was made"


@pytest.mark.parametrize(
    ("family", "expected"),
    [
        (FormatFamily.SPREADSHEET_OOXML, Route.TABULAR_PARQUET),
        (FormatFamily.SPREADSHEET_BIFF, Route.TABULAR_PARQUET),
        (FormatFamily.SPREADSHEET_XLSB, Route.TABULAR_PARQUET),
        (FormatFamily.DELIMITED_TEXT, Route.TABULAR_PARQUET),
        (FormatFamily.PDF, Route.PDF_TEXT),
        (FormatFamily.IMAGE, Route.VISION_IMAGE),
        (FormatFamily.WORD_OOXML, Route.TEXT_EXTRACT),
        (FormatFamily.PRESENTATION_OOXML, Route.TEXT_EXTRACT),
        (FormatFamily.RTF, Route.TEXT_EXTRACT),
        (FormatFamily.HTML, Route.TEXT_EXTRACT),
        (FormatFamily.PLAIN_TEXT, Route.TEXT_EXTRACT),
        (FormatFamily.EMAIL, Route.TEXT_EXTRACT),
        (FormatFamily.JSON, Route.STRUCTURED),
        (FormatFamily.XML, Route.STRUCTURED),
        (FormatFamily.ARCHIVE_ZIP, Route.ARCHIVE),
        (FormatFamily.ARCHIVE_7Z, Route.ARCHIVE),
        (FormatFamily.ARCHIVE_GZIP, Route.ARCHIVE),
        (FormatFamily.TALLY_BINARY, Route.NO_READER),
        (FormatFamily.UNKNOWN, Route.NO_READER),
        (FormatFamily.NON_DATA_ARTIFACT, Route.NON_DATA),
        (FormatFamily.EXECUTABLE, Route.NON_DATA),
        (FormatFamily.EMPTY, Route.NON_DATA),
    ],
)
def test_family_routes_as_expected(family, expected):
    assert select_route(_probe(family)).route is expected


def test_pdf_route_is_provisional_until_pages_are_classified():
    # Arrange / Act - the text-versus-scanned split needs page inspection, not just the header
    decision = select_route(_probe(FormatFamily.PDF))

    # Assert
    assert decision.route is Route.PDF_TEXT
    assert decision.provisional is True, "the PDF classifier may still redirect this to vision"


def test_non_pdf_routes_are_final():
    assert select_route(_probe(FormatFamily.SPREADSHEET_OOXML)).provisional is False


def test_encrypted_ooxml_routes_to_no_reader_as_locked():
    # Arrange / Act - SPEC-01 req 5 keeps password protection distinct from every other failure
    decision = select_route(_probe(FormatFamily.ENCRYPTED_OOXML))

    # Assert
    assert decision.route is Route.NO_READER
    assert decision.error_category is ErrorCategory.PASSWORD_PROTECTED_FILE
    assert decision.terminal_status is ProcessingStatus.LOCKED


def test_tally_binary_carries_no_compatible_reader_not_a_generic_failure():
    # Arrange / Act
    decision = select_route(_probe(FormatFamily.TALLY_BINARY, declared_extension=".1800"))

    # Assert
    assert decision.error_category is ErrorCategory.NO_COMPATIBLE_READER
    assert decision.terminal_status is ProcessingStatus.NO_READER


def test_executable_is_marked_never_processed():
    # Arrange / Act - SPEC-01 req 6: reading does not authorize executing
    decision = select_route(_probe(FormatFamily.EXECUTABLE, declared_extension=".exe"))

    # Assert
    assert decision.error_category is ErrorCategory.EXECUTABLE_NOT_PROCESSED
    assert decision.terminal_status is ProcessingStatus.EXCLUDED_NON_DATA


def test_empty_file_is_recorded_as_empty_not_as_a_reader_failure():
    decision = select_route(_probe(FormatFamily.EMPTY))
    assert decision.error_category is ErrorCategory.EMPTY_FILE


def test_unreadable_routes_still_produce_a_record_and_format_document():
    # Arrange - SPEC-01 req 6 and AC9: an unreadable file is a recorded failure, not a skip
    for family in (FormatFamily.UNKNOWN, FormatFamily.TALLY_BINARY, FormatFamily.EXECUTABLE):
        decision = select_route(_probe(family))

        # Assert
        assert decision.requires_format_document is True
        assert decision.terminal_status is not None


def test_readable_routes_have_no_predetermined_terminal_status():
    # Arrange / Act - the outcome of a readable file is decided by the reader, not the router
    decision = select_route(_probe(FormatFamily.SPREADSHEET_OOXML))

    # Assert
    assert decision.terminal_status is None
    assert decision.error_category is None


def test_rar_requires_the_optional_external_tool():
    # Arrange / Act - rarfile shells out to an unrar binary that may not be installed
    with_tool = select_route(_probe(FormatFamily.ARCHIVE_RAR), unrar_available=True)
    without_tool = select_route(_probe(FormatFamily.ARCHIVE_RAR), unrar_available=False)

    # Assert
    assert with_tool.route is Route.ARCHIVE
    assert without_tool.route is Route.NO_READER
    assert without_tool.error_category is ErrorCategory.NO_COMPATIBLE_READER


def test_legacy_doc_requires_the_optional_converter():
    # Arrange / Act - 73 legacy .doc files need LibreOffice; without it they are recorded
    with_tool = select_route(_probe(FormatFamily.WORD_OLE), soffice_available=True)
    without_tool = select_route(_probe(FormatFamily.WORD_OLE), soffice_available=False)

    # Assert
    assert with_tool.route is Route.TEXT_EXTRACT
    assert without_tool.route is Route.NO_READER


def test_macro_workbook_still_routes_to_tabular_without_executing_macros():
    # Arrange / Act - SPEC-01 req 1: read stored values, never run the macro
    decision = select_route(_probe(FormatFamily.SPREADSHEET_OOXML, macros_present=True))

    # Assert
    assert decision.route is Route.TABULAR_PARQUET
    assert "macro" in decision.reason.lower()


def test_reason_mentions_the_evidence_for_an_extension_conflict():
    # Arrange / Act
    decision = select_route(
        _probe(FormatFamily.SPREADSHEET_OOXML, declared_extension=".xlk", extension_conflict=True)
    )

    # Assert - the format document must be able to explain why the extension was ignored
    assert "extension" in decision.reason.lower()
