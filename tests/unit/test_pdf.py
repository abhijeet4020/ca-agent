"""Tests for PDF page classification (TP-01 Group I, SPEC-01 requirements 2 and 3).

This is the most expensive decision in the pipeline. A page wrongly called SCANNED buys a paid
vision call that was never needed; a page wrongly called TEXT silently yields nothing at all,
and nobody notices because the document still "succeeded". The encryption branch matters just
as much: hundreds of the corpus's most valuable filings are ITR-V PDFs that carry an encryption
dictionary but open with an empty password, and treating that marker as "locked" would discard
all of them (ADR-008).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ca_agent.config.settings import PdfSettings  # noqa: E402
from ca_agent.core.enums import ErrorCategory, PageKind, ProcessingStatus  # noqa: E402
from ca_agent.readers.pdf import read_pdf  # noqa: E402
from testdata.builders import files  # noqa: E402

_SETTINGS = PdfSettings()
#: A realistic text page. The default threshold is 120 *non-whitespace* characters, so a single
#: short heading is deliberately not enough to qualify as a text page.
_LONG_LINE = "\n".join(
    (
        "Balance Sheet as at 31 March 2026 showing reserves and surplus carried forward",
        "Fixed assets net of depreciation, current assets, loans and advances as scheduled",
        "Total of capital account and secured loans reconciled with the trial balance",
    )
)


def _classify(tmp_path: Path, payload: bytes, settings: PdfSettings | None = None):
    source = files.write_bytes(tmp_path / "src" / "doc.pdf", payload)
    return read_pdf(source, settings=settings or _SETTINGS)


# --- per-page classification ----------------------------------------------------------------


def test_text_page_is_classified_from_its_character_count(tmp_path):
    # Arrange / Act
    result = _classify(tmp_path, files.text_pdf_bytes([_LONG_LINE]))
    page = result.pages[0]

    # Assert
    assert page.kind is PageKind.TEXT
    assert page.character_count >= _SETTINGS.min_text_characters_for_text_page


def test_image_only_page_is_classified_scanned(tmp_path):
    # Arrange / Act
    result = _classify(tmp_path, files.scanned_pdf_bytes(1))
    page = result.pages[0]

    # Assert
    assert page.kind is PageKind.SCANNED
    assert page.image_coverage >= _SETTINGS.scanned_image_coverage_ratio


def test_blank_page_is_classified_empty_and_never_embedded(tmp_path):
    # Arrange - SPEC-01 req 2 forbids embedding empty text; the page is recorded, not dropped
    result = _classify(tmp_path, files.pdf_bytes([{}]))

    # Assert
    assert result.pages[0].kind is PageKind.EMPTY
    assert result.units == ()


def test_page_with_little_text_over_an_image_is_classified_mixed(tmp_path):
    # Arrange - a stamped or signed scan, which must not pass as a text page
    payload = files.pdf_bytes([{"text": "Signed", "image_fraction": 0.95}])

    # Act
    result = _classify(tmp_path, payload)

    # Assert
    assert result.pages[0].kind is PageKind.MIXED


def test_classification_thresholds_come_from_configuration(tmp_path):
    # Arrange - SPEC-01 req 8 stamps these into the reuse fingerprint, so they cannot be
    # hard-coded; the same page must classify differently under a different setting
    payload = files.text_pdf_bytes([_LONG_LINE])

    # Act
    lenient = _classify(tmp_path, payload, PdfSettings(min_text_characters_for_text_page=10))
    strict = _classify(tmp_path, payload, PdfSettings(min_text_characters_for_text_page=5000))

    # Assert
    assert lenient.pages[0].kind is PageKind.TEXT
    assert strict.pages[0].kind is not PageKind.TEXT


# --- document-level decisions -------------------------------------------------------------------


def test_document_with_every_page_text_does_not_require_vision(tmp_path):
    # Arrange / Act
    result = _classify(tmp_path, files.text_pdf_bytes([_LONG_LINE] * 3))

    # Assert
    assert result.requires_vision() is False
    assert len(result.units) == 3
    assert all("Balance Sheet" in unit.text for unit in result.units)


def test_document_with_any_scanned_page_requires_vision(tmp_path):
    # Arrange - SPEC-01 req 3: one scanned page makes the whole document a vision document
    payload = files.pdf_bytes(
        [{"text": _LONG_LINE}, {"image_fraction": 0.95}, {"text": _LONG_LINE}]
    )

    # Act
    result = _classify(tmp_path, payload)

    # Assert - and the text pages keep their native text, so no vision call is wasted on them
    assert result.requires_vision() is True
    assert [page.kind for page in result.pages] == [
        PageKind.TEXT,
        PageKind.SCANNED,
        PageKind.TEXT,
    ]
    assert len(result.units) == 2


def test_page_units_are_numbered_in_page_order(tmp_path):
    # Arrange - a chunk citing "page 7" has to mean page 7
    result = _classify(tmp_path, files.text_pdf_bytes([f"{_LONG_LINE} {n}" for n in range(4)]))

    # Assert
    assert [unit.unit_ref for unit in result.units] == [
        "page:1",
        "page:2",
        "page:3",
        "page:4",
    ]


def test_page_count_is_recorded_even_when_pages_have_no_text(tmp_path):
    # Arrange / Act - req 6 wants the observation recorded whatever it turns out to be
    result = _classify(tmp_path, files.scanned_pdf_bytes(3))

    # Assert
    assert len(result.pages) == 3
    assert result.page_count == 3


# --- encryption (ADR-008) -------------------------------------------------------------------------


def test_owner_password_only_pdf_is_extracted_not_marked_locked(tmp_path):
    # Arrange - the ITR-V shape: hundreds of the corpus's most valuable filings look like this
    result = _classify(tmp_path, files.owner_password_only_pdf_bytes())

    # Assert
    assert result.status is not ProcessingStatus.LOCKED
    assert result.encrypted is True
    assert result.owner_password_only is True
    assert "Income Tax Return" in "\n".join(unit.text for unit in result.units)


def test_genuinely_locked_pdf_is_recorded_as_password_protected(tmp_path):
    # Arrange - a real user password, which is never guessed
    result = _classify(tmp_path, files.encrypted_pdf_bytes(user_password="secret"))

    # Assert
    assert result.status is ProcessingStatus.LOCKED
    assert result.failure is not None
    assert result.failure.category is ErrorCategory.PASSWORD_PROTECTED_FILE
    assert result.units == ()


# --- failures -----------------------------------------------------------------------------------------


def test_page_that_fails_extraction_is_marked_and_the_rest_survive(tmp_path, monkeypatch):
    # Arrange - SPEC-01 req 3: keep what worked, mark what did not, report partial not failed
    from ca_agent.readers import pdf as pdf_reader

    original = pdf_reader._page_text
    calls = {"count": 0}

    def _fail_on_second(page):
        calls["count"] += 1
        if calls["count"] == 2:
            raise ValueError("page content stream is damaged")
        return original(page)

    monkeypatch.setattr(pdf_reader, "_page_text", _fail_on_second)

    # Act
    result = _classify(tmp_path, files.text_pdf_bytes([_LONG_LINE] * 3))

    # Assert
    assert result.status is ProcessingStatus.PARTIAL
    assert result.pages[1].failure is not None
    assert len(result.units) == 2, "the pages that worked are still extracted"


def test_corrupt_pdf_is_reported_not_raised(tmp_path):
    # Arrange / Act - one damaged PDF cannot abort a 6,685-file route
    result = _classify(tmp_path, b"%PDF-1.4\nnot actually a pdf")

    # Assert
    assert result.failure is not None
    assert result.failure.category is ErrorCategory.CORRUPT_FILE
    assert result.status is ProcessingStatus.FAILED


def test_empty_document_is_recorded_without_failure(tmp_path):
    # Arrange / Act - a PDF of blank pages is a recorded outcome, not an error
    result = _classify(tmp_path, files.pdf_bytes([{}, {}]))

    # Assert
    assert result.failure is None
    assert all(page.kind is PageKind.EMPTY for page in result.pages)
    assert result.requires_vision() is False


@pytest.mark.parametrize("page_count", [1, 5])
def test_every_page_is_observed(tmp_path, page_count):
    # Arrange / Act - acceptance criterion 9 applies per page, not just per file
    result = _classify(tmp_path, files.scanned_pdf_bytes(page_count))

    # Assert
    assert [page.number for page in result.pages] == list(range(1, page_count + 1))
