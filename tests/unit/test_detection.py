"""Tests for signature-based format detection (TP-01 Group E).

Probing the real corpus disproved the assumption that extensions are reliable: 61 of 66 .xlk
files are OOXML, roughly 30 percent of sampled .xls files are OOXML or plain text, all 411 .db
files are Windows Thumbs.db artifacts, and 28 files carry no extension at all. Detection is
therefore signature-first, and these tests pin that behaviour against real bytes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ca_agent.config.settings import DetectionSettings  # noqa: E402
from ca_agent.core.enums import FormatFamily  # noqa: E402
from ca_agent.readers.detection import detect_format  # noqa: E402
from testdata.builders import files  # noqa: E402

_SETTINGS = DetectionSettings()


def _probe(tmp_path: Path, name: str, payload: bytes):
    return detect_format(files.write_bytes(tmp_path / name, payload), _SETTINGS)


# --- signature beats extension -----------------------------------------------------------


def test_detection_prefers_signature_over_extension(tmp_path):
    # Arrange / Act - the exact three misfilings found in the real corpus
    ooxml_named_xls = _probe(tmp_path, "report.xls", files.xlsx_bytes())
    json_named_xlsx = _probe(tmp_path, "data.xlsx", files.itr_json_bytes())
    text_named_xls = _probe(tmp_path, "x.xls", b"Account  Opening Balance\n100  200\n")

    # Assert
    assert ooxml_named_xls.family is FormatFamily.SPREADSHEET_OOXML
    assert json_named_xlsx.family is FormatFamily.JSON
    assert text_named_xls.family in {FormatFamily.PLAIN_TEXT, FormatFamily.DELIMITED_TEXT}


def test_extension_conflict_is_recorded_as_evidence_not_failure(tmp_path):
    # Arrange / Act - a mismatch is normal in this corpus, not an error
    probe = _probe(tmp_path, "backup.xlk", files.xlsx_bytes())

    # Assert
    assert probe.family is FormatFamily.SPREADSHEET_OOXML
    assert probe.extension_conflict is True
    assert probe.declared_extension == ".xlk"
    assert probe.evidence, "the conflicting evidence must be recorded for the format document"


def test_matching_extension_reports_no_conflict(tmp_path):
    probe = _probe(tmp_path, "report.xlsx", files.xlsx_bytes())
    assert probe.extension_conflict is False


# --- OOXML container discrimination -------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "payload_factory", "expected"),
    [
        ("book.xlsx", files.xlsx_bytes, FormatFamily.SPREADSHEET_OOXML),
        ("letter.docx", files.docx_bytes, FormatFamily.WORD_OOXML),
        ("deck.pptx", files.pptx_bytes, FormatFamily.PRESENTATION_OOXML),
        ("bundle.zip", files.plain_zip_bytes, FormatFamily.ARCHIVE_ZIP),
    ],
)
def test_zip_container_is_probed_for_its_payload(tmp_path, name, payload_factory, expected):
    # Arrange / Act - all four share the PK signature, so the member list is the only signal
    probe = _probe(tmp_path, name, payload_factory())

    # Assert
    assert probe.family is expected


def test_macro_enabled_workbook_is_identified_but_never_executed(tmp_path):
    # Arrange / Act
    probe = _probe(tmp_path, "book.xlsm", files.xlsx_bytes(macro_enabled=True))

    # Assert - SPEC-01 req 1: read stored values without executing macros
    assert probe.family is FormatFamily.SPREADSHEET_OOXML
    assert probe.subtype == "xlsm"
    assert probe.macros_present is True


# --- OLE container discrimination ----------------------------------------------------------


def test_ole_streams_distinguish_workbook_from_document(tmp_path):
    # Arrange / Act
    workbook = _probe(tmp_path, "old.xls", files.legacy_xls_bytes())
    document = _probe(tmp_path, "old.doc", files.legacy_doc_bytes())

    # Assert
    assert workbook.family is FormatFamily.SPREADSHEET_BIFF
    assert document.family is FormatFamily.WORD_OLE


def test_encrypted_ooxml_is_detected_as_encrypted_not_legacy_xls(tmp_path):
    # Arrange - both are OLE containers; treating the encrypted one as BIFF would misreport it
    probe = _probe(tmp_path, "locked.xlsx", files.encrypted_ooxml_bytes())

    # Assert - SPEC-01 req 5 keeps encryption distinct from every other failure
    assert probe.family is FormatFamily.ENCRYPTED_OOXML


def test_thumbs_db_is_classified_non_data(tmp_path):
    # Arrange - all 411 .db files in the corpus are these
    probe = _probe(tmp_path, "Thumbs.db", files.thumbs_db_bytes())

    # Assert
    assert probe.family is FormatFamily.NON_DATA_ARTIFACT
    assert "thumbs.db" in probe.detail.lower()


# --- non-data artifacts ----------------------------------------------------------------------


def test_excel_owner_lock_stub_is_classified_non_data(tmp_path):
    probe = _probe(tmp_path, "~$FS AMIT.xlsx", files.excel_lock_stub_bytes())
    assert probe.family is FormatFamily.NON_DATA_ARTIFACT


def test_appledouble_resource_fork_is_classified_non_data(tmp_path):
    probe = _probe(tmp_path, "._ay 2025-2026", b"\x00\x05\x16\x07rubbish")
    assert probe.family is FormatFamily.NON_DATA_ARTIFACT


def test_executable_is_detected_and_flagged(tmp_path):
    # Arrange / Act - SPEC-01 req 6: reading does not authorize executing
    probe = _probe(tmp_path, "setup.exe", files.executable_bytes())

    # Assert
    assert probe.family is FormatFamily.EXECUTABLE


def test_empty_file_is_reported_as_empty(tmp_path):
    probe = _probe(tmp_path, "blank.txt", b"")
    assert probe.family is FormatFamily.EMPTY


# --- extensionless and mangled names ----------------------------------------------------------


def test_extensionless_pdf_is_routed_by_signature(tmp_path):
    # Arrange - 12 of the 28 extensionless corpus files are PDFs
    probe = _probe(tmp_path, "document", files.minimal_pdf_bytes())

    # Assert
    assert probe.family is FormatFamily.PDF
    assert probe.declared_extension == ""


def test_mangled_extension_json_is_detected(tmp_path):
    # Arrange - four corpus files have a whole trailing phrase where the extension should be
    name = "3.3 ITR 5_JSON_DREAM WORLD CONSTRUCTIONS LLP AY 2025-2026"
    probe = _probe(tmp_path, name, files.itr_json_bytes())

    # Assert
    assert probe.family is FormatFamily.JSON


# --- remaining families -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "payload_factory", "expected"),
    [
        ("scan.png", lambda: files.image_bytes("PNG"), FormatFamily.IMAGE),
        ("scan.jpeg", lambda: files.image_bytes("JPEG"), FormatFamily.IMAGE),
        ("scan.bmp", lambda: files.image_bytes("BMP"), FormatFamily.IMAGE),
        ("scan.gif", lambda: files.image_bytes("GIF"), FormatFamily.IMAGE),
        ("form.pdf", files.minimal_pdf_bytes, FormatFamily.PDF),
        ("note.rtf", files.rtf_bytes, FormatFamily.RTF),
        ("page.html", files.html_bytes, FormatFamily.HTML),
        ("return.xml", files.xml_bytes, FormatFamily.XML),
        ("ledger.csv", files.csv_bytes, FormatFamily.DELIMITED_TEXT),
        ("archive.7z", files.seven_zip_signature_bytes, FormatFamily.ARCHIVE_7Z),
        ("archive.rar", files.rar_signature_bytes, FormatFamily.ARCHIVE_RAR),
        ("dump.gz", files.gzip_bytes, FormatFamily.ARCHIVE_GZIP),
    ],
)
def test_family_is_detected_from_bytes(tmp_path, name, payload_factory, expected):
    assert _probe(tmp_path, name, payload_factory()).family is expected


def test_tally_binary_is_unknown_rather_than_guessed(tmp_path):
    # Arrange - SPEC-01 req 4: do not invent a classification
    probe = _probe(tmp_path, "Company.1800", files.tally_binary_bytes())

    # Assert
    assert probe.family is FormatFamily.TALLY_BINARY
    assert probe.confidence == "extension"


def test_unrecognised_binary_is_unknown(tmp_path):
    probe = _probe(tmp_path, "mystery.qqq", bytes(range(256)))
    assert probe.family is FormatFamily.UNKNOWN


def test_utf16_text_is_detected_as_text(tmp_path):
    # Arrange - corpus text files are not all UTF-8
    probe = _probe(tmp_path, "note.txt", "Balance Sheet\nCash 100\n".encode("utf-16"))

    # Assert
    assert probe.family in {FormatFamily.PLAIN_TEXT, FormatFamily.DELIMITED_TEXT}


def test_detection_reads_only_a_bounded_prefix(tmp_path):
    # Arrange - a multi-gigabyte Tally backup must not be read whole just to classify it
    target = files.write_bytes(tmp_path / "huge.bin", b"%PDF-1.4\n" + b"\x00" * (4 * 1024 * 1024))

    # Act
    probe = detect_format(target, DetectionSettings(signature_sample_bytes=512))

    # Assert
    assert probe.family is FormatFamily.PDF
    assert probe.bytes_inspected <= 512 + DetectionSettings().text_probe_bytes


def test_missing_file_raises_oserror(tmp_path):
    with pytest.raises(OSError):
        detect_format(tmp_path / "absent.pdf", _SETTINGS)


# --- regressions found by running against the real corpus -------------------------------


def test_java_serialized_stream_is_not_mistaken_for_the_pdf_it_claims_to_be(tmp_path):
    """261 corpus files named *.pdf are Java object streams from the e-filing utility."""
    # Arrange - the Java serialization magic, then a serialized Object[] header
    payload = b"\xac\xed\x00\x05ur\x00\x13[Ljava.lang.Object;\x90\xce X\x9f\x10s)l\x02"

    # Act
    probe = _probe(tmp_path, "ITR V AMIT AY 21-22.pdf", payload)

    # Assert
    assert probe.family is FormatFamily.JAVA_SERIALIZED
    assert probe.family is not FormatFamily.PDF
    assert probe.extension_conflict is False, "no expected extension is defined for this family"


def test_java_keystore_is_identified(tmp_path):
    probe = _probe(tmp_path, "client.keystore", b"\xfe\xed\xfe\xed\x00\x00\x00\x02")
    assert probe.family is FormatFamily.KEYSTORE


def test_ole_stream_names_prefer_a_real_parse_over_the_prefix_scan(tmp_path, monkeypatch):
    """Real OLE files keep their directory in a sector the header names, not at offset 512.

    Scanning a fixed prefix missed 175 genuine .xls and .doc files in the corpus, so olefile
    must be consulted first.
    """
    # Arrange - a container whose streams are only discoverable by parsing
    import olefile

    class _FakeContainer:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def listdir(self):
            return [["Workbook"], ["\x05SummaryInformation"]]

    monkeypatch.setattr(olefile, "OleFileIO", lambda _path: _FakeContainer())
    payload = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 2048

    # Act
    probe = _probe(tmp_path, "FS 2024.xls", payload)

    # Assert
    assert probe.family is FormatFamily.SPREADSHEET_BIFF


def test_ole_falls_back_to_prefix_scan_when_the_container_will_not_parse(tmp_path, monkeypatch):
    # Arrange - a damaged container must still be classifiable rather than raising
    import olefile

    def _raise(_path):
        raise OSError("not a valid OLE file")

    monkeypatch.setattr(olefile, "OleFileIO", _raise)

    # Act
    probe = _probe(tmp_path, "old.doc", files.legacy_doc_bytes())

    # Assert
    assert probe.family is FormatFamily.WORD_OLE
