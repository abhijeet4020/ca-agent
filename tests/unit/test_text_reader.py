"""Tests for document text extraction (TP-01 Group J, SPEC-01 requirement 2).

The risk in this route is quieter than in the tabular one: a reader that returns *some* text
looks like it worked. These tests pin the things that silently go wrong instead - body order
recovered from the XML rather than assumed, script and style bodies excluded, tables counted as
detected separately from extracted, and every failure returned rather than raised.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ca_agent.config.settings import TextSettings  # noqa: E402
from ca_agent.core.enums import ErrorCategory, FormatFamily, UnitType  # noqa: E402
from ca_agent.readers.text import read_text  # noqa: E402
from testdata.builders import files  # noqa: E402

_SETTINGS = TextSettings()


def _extract(tmp_path: Path, name: str, payload: bytes, family: FormatFamily):
    source = files.write_bytes(tmp_path / "src" / name, payload)
    return read_text(source, settings=_SETTINGS, family=family)


def _all_text(result) -> str:
    return "\n".join(unit.text for unit in result.units)


# --- word documents --------------------------------------------------------------------


def test_docx_paragraphs_and_tables_are_extracted_in_document_order(tmp_path):
    # Arrange - python-docx exposes paragraphs and tables as two separate collections, so a
    # reader that does not walk the body XML will emit the table after both paragraphs
    payload = files.document_bytes(
        [
            ("paragraph", "Opening balance carried forward."),
            ("table", [["Particulars", "Amount"], ["Depreciation", "12,500"]]),
            ("paragraph", "Closing balance as above."),
        ]
    )

    # Act
    result = _extract(tmp_path, "notes.docx", payload, FormatFamily.WORD_OOXML)
    combined = _all_text(result)

    # Assert
    assert result.failure is None
    assert combined.index("Opening balance") < combined.index("Depreciation")
    assert combined.index("Depreciation") < combined.index("Closing balance")


def test_docx_headings_become_their_own_units(tmp_path):
    # Arrange
    payload = files.document_bytes(
        [
            ("heading", "Balance Sheet"),
            ("paragraph", "As at 31 March 2026."),
            ("heading", "Profit and Loss"),
            ("paragraph", "For the year then ended."),
        ]
    )

    # Act
    result = _extract(tmp_path, "fs.docx", payload, FormatFamily.WORD_OOXML)
    headings = [unit for unit in result.units if unit.unit_type is UnitType.HEADING]

    # Assert - the heading is the unit reference an agent would cite
    assert [unit.unit_ref for unit in headings] == ["Balance Sheet", "Profit and Loss"]
    assert "As at 31 March 2026." in headings[0].text
    assert "For the year then ended." in headings[1].text


def test_detected_tables_are_distinguished_from_extracted_tables(tmp_path, monkeypatch):
    # Arrange - SPEC-01 req 2 forbids reporting detection as extraction
    from ca_agent.readers import text as text_reader

    payload = files.document_bytes(
        [
            ("table", [["a", "1"]]),
            ("table", [["b", "2"]]),
            ("table", [["c", "3"]]),
        ]
    )
    original = text_reader._docx_table_text
    calls = {"count": 0}

    def _fail_on_second(table):
        calls["count"] += 1
        if calls["count"] == 2:
            raise ValueError("table XML is malformed")
        return original(table)

    monkeypatch.setattr(text_reader, "_docx_table_text", _fail_on_second)

    # Act
    result = _extract(tmp_path, "tables.docx", payload, FormatFamily.WORD_OOXML)

    # Assert
    assert result.tables_detected == 3
    assert result.tables_extracted == 2
    assert result.warnings, "the unreadable table must be recorded, not silently dropped"


def test_corrupt_document_is_reported_not_raised(tmp_path):
    # Arrange / Act - one damaged document cannot abort a 16,000-file batch
    result = _extract(tmp_path, "broken.docx", b"PK\x03\x04not-a-real-package", FormatFamily.WORD_OOXML)

    # Assert
    assert result.units == ()
    assert result.failure is not None
    assert result.failure.category is ErrorCategory.CORRUPT_FILE


# --- html ------------------------------------------------------------------------------


def test_html_script_and_style_content_is_not_extracted_as_text(tmp_path):
    # Arrange
    payload = (
        b"<!DOCTYPE html><html><head><style>.total{color:red}</style>"
        b"<script>var secret = 42;</script></head>"
        b"<body><p>Total tax payable 45,000</p></body></html>"
    )

    # Act
    result = _extract(tmp_path, "return.html", payload, FormatFamily.HTML)
    combined = _all_text(result)

    # Assert
    assert "Total tax payable 45,000" in combined
    assert "var secret" not in combined
    assert "color:red" not in combined


def test_html_tables_are_counted_as_detected_and_extracted(tmp_path):
    # Arrange - GST portal pages are mostly tables
    payload = (
        b"<html><body><table><tr><td>IGST</td><td>1000</td></tr></table>"
        b"<table><tr><td>CGST</td><td>500</td></tr></table></body></html>"
    )

    # Act
    result = _extract(tmp_path, "gstr.html", payload, FormatFamily.HTML)

    # Assert
    assert result.tables_detected == 2
    assert result.tables_extracted == 2
    assert "IGST" in _all_text(result)


# --- email -----------------------------------------------------------------------------


def test_email_headers_and_body_are_both_extracted(tmp_path):
    # Arrange - for an email the routing headers carry real analytical value
    payload = files.email_bytes(subject="GSTR-1 filed", body="Acknowledgement attached.")

    # Act
    result = _extract(tmp_path, "note.eml", payload, FormatFamily.EMAIL)
    combined = _all_text(result)

    # Assert
    assert "GSTR-1 filed" in combined
    assert "accounts@example.com" in combined
    assert "Acknowledgement attached." in combined


def test_email_html_only_body_is_extracted_as_text(tmp_path):
    # Arrange - many client emails carry no plain-text alternative
    payload = files.email_bytes(html_body="<html><body><p>Payment received</p></body></html>")

    # Act
    result = _extract(tmp_path, "html.eml", payload, FormatFamily.EMAIL)
    combined = _all_text(result)

    # Assert
    assert "Payment received" in combined
    assert "<p>" not in combined


# --- presentations, rtf and plain text ---------------------------------------------------


def test_pptx_slides_are_extracted_without_a_new_dependency(tmp_path):
    # Arrange - two presentations in the whole corpus does not justify python-pptx
    payload = files.presentation_bytes([["Audit findings"], ["Recommendations", "Next steps"]])

    # Act
    result = _extract(tmp_path, "deck.pptx", payload, FormatFamily.PRESENTATION_OOXML)

    # Assert
    assert len(result.units) == 2
    assert "Audit findings" in result.units[0].text
    assert "Next steps" in result.units[1].text


def test_rtf_control_words_are_stripped(tmp_path):
    # Arrange / Act
    result = _extract(tmp_path, "note.rtf", files.rtf_bytes(), FormatFamily.RTF)
    combined = _all_text(result)

    # Assert
    assert "Balance Sheet" in combined
    assert "rtf1" not in combined


def test_plain_text_encoding_is_detected_and_recorded(tmp_path):
    # Arrange - corpus text files mix UTF-8, UTF-16 and cp1252, with Devanagari client names
    payload = "पाटील अँड असोसिएट्स\nOpening balance 1,00,000\n".encode("utf-16")

    # Act
    result = _extract(tmp_path, "notes.txt", payload, FormatFamily.PLAIN_TEXT)

    # Assert
    assert result.encoding is not None
    assert "पाटील" in _all_text(result)


def test_empty_document_yields_no_units_and_no_failure(tmp_path):
    # Arrange - an empty file is a recorded outcome, not an error (SPEC-01 req 6)
    result = _extract(tmp_path, "blank.txt", b"   \n\t\n", FormatFamily.PLAIN_TEXT)

    # Assert
    assert result.failure is None
    assert all(not unit.has_content() for unit in result.units)


@pytest.mark.parametrize("family", [FormatFamily.PDF, FormatFamily.SPREADSHEET_OOXML])
def test_non_text_family_is_rejected(tmp_path, family):
    # Arrange / Act / Assert - fail fast rather than returning meaningless empty text
    with pytest.raises(ValueError):
        _extract(tmp_path, "wrong.bin", b"data", family)


# --- legacy .doc via LibreOffice ---------------------------------------------------------


def test_legacy_doc_without_libreoffice_is_no_compatible_reader(tmp_path):
    """Without the converter a .doc is unreadable, not broken.

    Before this route existed, routing sent WORD_OLE to the text reader whenever soffice was
    configured and the reader rejected the family outright - so enabling LibreOffice turned a
    clean "no reader" record into a spurious unexpected exception.
    """
    # Arrange / Act
    source = files.write_bytes(tmp_path / "src" / "old.doc", files.legacy_doc_bytes())
    result = read_text(source, settings=_SETTINGS, family=FormatFamily.WORD_OLE)

    # Assert
    assert result.failure is not None
    assert result.failure.category is ErrorCategory.NO_COMPATIBLE_READER
    assert "SOFFICE_PATH" in result.failure.message


def test_a_misconfigured_libreoffice_path_is_reported_clearly(tmp_path):
    # Arrange - a typo in the path must say so rather than fail obscurely later
    source = files.write_bytes(tmp_path / "src" / "old.doc", files.legacy_doc_bytes())

    # Act
    result = read_text(
        source,
        settings=_SETTINGS,
        family=FormatFamily.WORD_OLE,
        soffice_path=str(tmp_path / "nowhere" / "soffice.exe"),
    )

    # Assert
    assert result.failure is not None
    assert result.failure.category is ErrorCategory.CONFIG_ERROR


def test_libreoffice_conversion_is_read_with_the_docx_reader(tmp_path, monkeypatch):
    # Arrange - converting to docx rather than plain text is what preserves headings and tables
    import subprocess

    converted = files.document_bytes(
        [("heading", "Balance Sheet"), ("paragraph", "Reserves and surplus carried forward.")]
    )
    fake_soffice = files.write_bytes(tmp_path / "bin" / "soffice.exe", b"stub")
    source = files.write_bytes(tmp_path / "src" / "old.doc", files.legacy_doc_bytes())

    def _fake_run(command, **kwargs):
        outdir = Path(command[command.index("--outdir") + 1])
        (outdir / "old.docx").write_bytes(converted)
        return subprocess.CompletedProcess(command, 0, b"", b"")

    # _read_word_ole imports subprocess at call time, so patching the module reaches it.
    monkeypatch.setattr(subprocess, "run", _fake_run)

    # Act
    result = read_text(
        source, settings=_SETTINGS, family=FormatFamily.WORD_OLE, soffice_path=str(fake_soffice)
    )

    # Assert - the docx reader's structure recovery is reused, not reimplemented
    assert result.failure is None
    assert result.reader == "libreoffice+python-docx"
    assert any(unit.unit_ref == "Balance Sheet" for unit in result.units)
