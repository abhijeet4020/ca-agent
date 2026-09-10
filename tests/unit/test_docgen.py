"""Tests for per-file format documents (TP-01 Group K, SPEC-01 requirement 4).

Requirement 4 asks for a document describing what was actually observed in one specific file,
and forbids inventing anything it could not see. Both halves matter. A document that omits a
section is ambiguous between "nothing found" and "never looked", and a document that presents
an inferred column type as though the source declared it is worse than no document at all -
it launders a guess into a fact that a later agent will rely on.
"""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ca_agent.core.enums import ErrorCategory, ProcessingStatus, Route  # noqa: E402
from ca_agent.core.model import ErrorInfo, OutputRef  # noqa: E402
from ca_agent.docgen.model import (  # noqa: E402
    UNKNOWN,
    DocumentSection,
    DocumentTable,
    FormatDocument,
    SourceIdentity,
)
from ca_agent.docgen.renderer import render_format_document  # noqa: E402


def _identity(**overrides) -> SourceIdentity:
    defaults = dict(
        source_relpath="GST Company/ACME/RETURNS/report.xlsx",
        category="GST Company",
        client="ACME",
        scope_id="gst_company__acme__abc123",
        extension=".xlsx",
        detected_format="spreadsheet_ooxml",
        detection_confidence="container",
        content_sha256="a" * 64,
        size_bytes=10809,
        version_id="r000004",
    )
    defaults.update(overrides)
    return SourceIdentity(**defaults)


def _document(**overrides) -> FormatDocument:
    defaults = dict(
        identity=_identity(),
        route=Route.TABULAR_PARQUET,
        status=ProcessingStatus.SUCCESS,
        reader="openpyxl",
        sections=(),
        outputs=(),
    )
    defaults.update(overrides)
    return FormatDocument(**defaults)


# --- identity (requirement 4, first bullet) --------------------------------------------


def test_format_document_records_every_required_identity_field():
    # Arrange / Act
    rendered = render_format_document(
        _document(outputs=(OutputRef("parquet", PurePosixPath("data/sheet1.parquet")),))
    )

    # Assert - each item requirement 4 lists by name
    for expected in (
        "GST Company/ACME/RETURNS/report.xlsx",
        "GST Company",
        "ACME",
        ".xlsx",
        "spreadsheet_ooxml",
        "a" * 64,
        "r000004",
        "success",
        "data/sheet1.parquet",
    ):
        assert expected in rendered, f"requirement 4 requires {expected!r}"


def test_archive_member_document_names_its_archive_and_member_path():
    # Arrange - requirement 4: members retain their originating archive and member path
    identity = _identity(archive_id="b" * 64, member_path="returns/september/report.xlsx")

    # Act
    rendered = render_format_document(_document(identity=identity))

    # Assert
    assert "b" * 64 in rendered
    assert "returns/september/report.xlsx" in rendered


def test_extension_conflicting_with_content_is_reported_as_evidence():
    # Arrange - 61 of 66 .xlk files are really OOXML; the document must say so
    identity = _identity(extension=".xls", detected_format="spreadsheet_ooxml", extension_conflict=True)

    # Act
    rendered = render_format_document(_document(identity=identity))

    # Assert
    assert "conflict" in rendered.lower()


# --- inferred versus observed (requirement 4's explicit prohibition) -----------------------


def test_tabular_document_distinguishes_inferred_types_from_source_information():
    # Arrange - an inferred type presented as declared is a guess laundered into a fact
    section = DocumentSection(
        heading="Columns",
        table=DocumentTable(
            caption="Sheet1",
            columns=("Column", "Type", "Inferred", "Why not typed"),
            rows=(
                ("Amount", "int64", "yes", ""),
                ("PAN", "string", "no", "values parse as integers but do not round-trip"),
            ),
        ),
    )

    # Act
    rendered = render_format_document(_document(sections=(section,)))

    # Assert
    assert "Inferred" in rendered
    assert "do not round-trip" in rendered


def test_unknown_information_is_marked_unknown_not_invented():
    # Arrange - a Tally binary: nothing about its structure can be inspected
    document = _document(
        identity=_identity(detected_format=UNKNOWN, detection_confidence=UNKNOWN),
        route=Route.NO_READER,
        status=ProcessingStatus.NO_READER,
        reader=None,
        error=ErrorInfo(
            category=ErrorCategory.NO_COMPATIBLE_READER,
            message="no Python library reads this format",
            stage="reader_selection",
        ),
        sections=(DocumentSection(heading="Observed structure", notes=(UNKNOWN,)),),
    )

    # Act
    rendered = render_format_document(document)

    # Assert
    assert UNKNOWN in rendered
    assert "NO_COMPATIBLE_READER" in rendered


def test_a_section_with_nothing_to_report_says_so_explicitly():
    # Arrange - an absent section cannot be told from one that was never attempted
    section = DocumentSection(heading="Tables", table=None, rows=(), notes=())

    # Act
    rendered = render_format_document(_document(sections=(section,)))

    # Assert
    assert "## Tables" in rendered
    assert "None" in rendered


# --- failures (requirement 4's last table row) -----------------------------------------------


def test_locked_file_document_records_stage_status_and_retry_action():
    # Arrange
    document = _document(
        route=Route.PDF_TEXT,
        status=ProcessingStatus.LOCKED,
        error=ErrorInfo(
            category=ErrorCategory.PASSWORD_PROTECTED_FILE,
            message="the empty password does not open this document; none is guessed",
            stage="pdf_classification",
            reader="pypdf",
        ),
        retry_action="Supply the client's PAN and date of birth in the credential file, or "
        "replace the source with an unlocked copy; the next run retries it.",
    )

    # Act
    rendered = render_format_document(document)

    # Assert
    assert "locked" in rendered
    assert "PASSWORD_PROTECTED_FILE" in rendered
    assert "pdf_classification" in rendered
    assert "credential file" in rendered


def test_warnings_are_listed_without_becoming_failures():
    # Arrange - a formula with no cached value is worth recording but is not a failure
    document = _document(
        warnings=(
            ErrorInfo(
                category=ErrorCategory.FORMULA_NO_CACHED_VALUE,
                message="Sheet1!C2 holds a formula with no cached value",
                stage="tabular_conversion",
            ),
        )
    )

    # Act
    rendered = render_format_document(document)

    # Assert
    assert "FORMULA_NO_CACHED_VALUE" in rendered
    assert "success" in rendered, "a warning must not change the recorded status"


# --- rendering ------------------------------------------------------------------------------------


def test_rendered_document_escapes_pipes_in_values():
    # Arrange - a pipe in a transcribed value would split the row into different columns
    section = DocumentSection(
        heading="Columns",
        table=DocumentTable(caption="S", columns=("Column",), rows=(("a | b",),)),
    )

    # Act
    rendered = render_format_document(_document(sections=(section,)))

    # Assert
    assert r"a \| b" in rendered


def test_label_value_rows_render_as_a_definition_list():
    # Arrange
    section = DocumentSection(heading="Encoding", rows=(("Detected encoding", "utf_16"),))

    # Act
    rendered = render_format_document(_document(sections=(section,)))

    # Assert
    assert "- **Detected encoding:** utf_16" in rendered


def test_document_starts_with_the_filename_as_its_title():
    # Arrange / Act - the file is the subject, so it is the heading
    rendered = render_format_document(_document())

    # Assert
    assert rendered.startswith("# report.xlsx\n")


def test_companion_name_preserves_the_full_filename():
    # Arrange - requirement 4: report.xlsx and report.pdf must not collide
    spreadsheet = _identity(source_relpath="c/ACME/a/report.xlsx", extension=".xlsx")
    portable = _identity(source_relpath="c/ACME/b/report.pdf", extension=".pdf")

    # Act / Assert
    assert spreadsheet.companion_relative_path() == PurePosixPath("c/ACME/a/report.xlsx.format.md")
    assert portable.companion_relative_path() == PurePosixPath("c/ACME/b/report.pdf.format.md")


def test_two_files_of_the_same_name_in_different_folders_get_different_documents():
    # Arrange - requirement 4: no collision across folders within one client
    first = _identity(source_relpath="c/ACME/a/report.xlsx")
    second = _identity(source_relpath="c/ACME/b/report.xlsx")

    # Act / Assert
    assert first.companion_relative_path() != second.companion_relative_path()
