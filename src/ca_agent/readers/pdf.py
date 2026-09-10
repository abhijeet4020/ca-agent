"""Purpose: decides, page by page, whether a PDF carries real text or is a scan (SPEC-01 req 2
versus req 3). This is the pipeline's most expensive decision - a page called SCANNED buys a
paid vision call, and one wrongly called TEXT silently yields nothing while the document still
reports success - so the call is made from measured characters and image coverage rather than
from the file's shape. Encryption is handled per ADR-008: an encryption dictionary is not the
same as a locked file, and treating it as one would discard hundreds of ITR-V filings.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ca_agent.config.settings import PdfSettings
from ca_agent.core.enums import ErrorCategory, PageKind, ProcessingStatus, UnitType
from ca_agent.core.model import ErrorInfo
from ca_agent.core.text import TextUnit

_STAGE = "pdf_classification"
_READER = "pdfplumber"
#: pypdf returns this from decrypt() when the supplied password did not open the document.
_NOT_DECRYPTED = 0


@dataclass(frozen=True, slots=True)
class PageObservation:
    """What one page turned out to contain, and why it was classified that way."""

    number: int
    kind: PageKind
    character_count: int
    image_coverage: float
    text: str = ""
    failure: ErrorInfo | None = None

    def has_text(self) -> bool:
        return bool(self.text.strip())


@dataclass(frozen=True, slots=True)
class PdfResult:
    """Everything one PDF produced.

    ``units`` holds native text for the pages that had it, and only those. Pages needing vision
    are deliberately left to the vision route rather than being given empty units here, so a
    later stage cannot mistake "not extracted yet" for "extracted and empty".
    """

    reader: str
    pages: tuple[PageObservation, ...] = ()
    units: tuple[TextUnit, ...] = ()
    status: ProcessingStatus = ProcessingStatus.SUCCESS
    encrypted: bool = False
    owner_password_only: bool = False
    failure: ErrorInfo | None = None
    warnings: tuple[ErrorInfo, ...] = ()

    @property
    def page_count(self) -> int:
        return len(self.pages)

    def requires_vision(self) -> bool:
        """SPEC-01 req 3: one scanned or mixed page makes the whole document a vision document."""
        return any(page.kind.needs_vision() for page in self.pages)

    def pages_needing_vision(self) -> tuple[int, ...]:
        """Page numbers the vision route must handle. A copy, never internal state."""
        return tuple(page.number for page in self.pages if page.kind.needs_vision())


def read_pdf(
    source: Path, *, settings: PdfSettings, passwords: Sequence[str] = ()
) -> PdfResult:
    """Classify every page of a PDF and extract native text where it exists.

    ``passwords`` are candidates the caller resolved from credentials the firm supplied for
    this client (ADR-008 amendment). They are tried only after the empty password, and nothing
    is ever generated here - the reader receives values or it does not.
    """
    encryption = _inspect_encryption(source, passwords)
    if encryption.failure is not None:
        return PdfResult(reader=_READER, status=encryption.status, failure=encryption.failure,
                         encrypted=encryption.encrypted)

    try:
        return _classify_pages(source, settings, encryption)
    except _PdfFailure as failure:
        return PdfResult(
            reader=_READER,
            status=ProcessingStatus.FAILED,
            encrypted=encryption.encrypted,
            owner_password_only=encryption.owner_password_only,
            failure=failure.as_error(),
        )


class _PdfFailure(Exception):
    """A whole-document failure. A single bad page is recorded on that page instead."""

    def __init__(self, category: ErrorCategory, message: str) -> None:
        super().__init__(message)
        self.category = category

    def as_error(self) -> ErrorInfo:
        return ErrorInfo(
            category=self.category, message=str(self), stage=_STAGE, reader=_READER
        )


# --- encryption (ADR-008) --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Encryption:
    encrypted: bool = False
    owner_password_only: bool = False
    #: The password that opened the document, needed again to read its pages. Never recorded.
    password: str = ""
    status: ProcessingStatus = ProcessingStatus.SUCCESS
    failure: ErrorInfo | None = None


def _inspect_encryption(source: Path, passwords: Sequence[str]) -> _Encryption:
    """Distinguish an owner-restricted PDF from a genuinely locked one.

    Many ITR-V and TIS filings carry an encryption dictionary whose *user* password is empty;
    the owner password only restricts printing and copying. Treating the marker alone as
    "locked" would discard hundreds of the most analytically valuable documents in the corpus.

    The empty password is tried first. Only then are the caller's supplied candidates tried -
    values the firm already holds for its own client, never anything generated here. No
    password reaches the returned error, because a processing record is a committed artifact.
    """
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(source)
        if not reader.is_encrypted:
            return _Encryption()
        opened, matched = _try_passwords(reader, passwords)
    except PdfReadError as error:
        return _Encryption(
            encrypted=True,
            status=ProcessingStatus.FAILED,
            failure=ErrorInfo(
                category=ErrorCategory.CORRUPT_FILE, message=str(error), stage=_STAGE,
                reader="pypdf",
            ),
        )
    except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
        return _Encryption(
            status=ProcessingStatus.FAILED,
            failure=ErrorInfo(
                category=ErrorCategory.CORRUPT_FILE, message=str(error), stage=_STAGE,
                reader="pypdf",
            ),
        )

    if int(opened) == _NOT_DECRYPTED:
        tried = "the empty password" if not passwords else (
            f"the empty password and {len(passwords)} supplied credential(s)"
        )
        return _Encryption(
            encrypted=True,
            status=ProcessingStatus.LOCKED,
            failure=ErrorInfo(
                category=ErrorCategory.PASSWORD_PROTECTED_FILE,
                # Deliberately counts the candidates rather than naming them: this message is
                # written into a processing record, and a record is a committed artifact.
                message=f"{tried} did not open this document; none is guessed",
                stage=_STAGE,
                reader="pypdf",
            ),
        )
    return _Encryption(
        encrypted=True, owner_password_only=not matched, password=matched
    )


def _try_passwords(reader, passwords: Sequence[str]) -> tuple[int, str]:
    """Try the empty password, then each supplied candidate. Returns the result and what worked."""
    opened = reader.decrypt("")
    if int(opened) != _NOT_DECRYPTED:
        return int(opened), ""
    for candidate in passwords:
        opened = reader.decrypt(candidate)
        if int(opened) != _NOT_DECRYPTED:
            return int(opened), candidate
    return _NOT_DECRYPTED, ""


# --- page classification ------------------------------------------------------------------------


def _classify_pages(source: Path, settings: PdfSettings, encryption: _Encryption) -> PdfResult:
    import pdfplumber
    from pdfminer.psexceptions import PSException

    observations: list[PageObservation] = []
    units: list[TextUnit] = []

    try:
        with pdfplumber.open(source, password=encryption.password) as document:
            for number, page in enumerate(document.pages, start=1):
                observation = _observe_page(page, number, settings)
                observations.append(observation)
                if observation.kind is PageKind.TEXT and observation.has_text():
                    units.append(
                        TextUnit(
                            unit_type=UnitType.PAGE,
                            unit_ref=f"page:{number}",
                            text=observation.text,
                            sequence=len(units),
                        )
                    )
    except PSException as error:
        # pdfminer's whole exception tree, not just PDFSyntaxError. A corpus PDF with a
        # malformed CMap raises PSSyntaxError, which is a *sibling* of PDFSyntaxError under
        # PSException rather than a subclass, so catching the specific type let it escape and
        # kill a run half way through. Catching the library's base class is the only version
        # of this that stays correct as pdfminer adds error types.
        raise _PdfFailure(ErrorCategory.CORRUPT_FILE, str(error)) from error
    except (OSError, ValueError, KeyError, IndexError, TypeError, AssertionError) as error:
        raise _PdfFailure(ErrorCategory.CORRUPT_FILE, str(error)) from error

    if not observations:
        raise _PdfFailure(ErrorCategory.CORRUPT_FILE, "document reports no pages")

    failed = [page for page in observations if page.failure is not None]
    status = ProcessingStatus.PARTIAL if failed else ProcessingStatus.SUCCESS
    return PdfResult(
        reader=_READER,
        pages=tuple(observations),
        units=tuple(units),
        status=status,
        encrypted=encryption.encrypted,
        owner_password_only=encryption.owner_password_only,
        warnings=tuple(page.failure for page in failed if page.failure is not None),
    )


def _observe_page(page, number: int, settings: PdfSettings) -> PageObservation:
    """Measure one page, recording a failure on the page rather than losing the document."""
    coverage = _image_coverage(page)
    try:
        text = _page_text(page)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError, OSError) as error:
        return PageObservation(
            number=number,
            kind=PageKind.SCANNED if coverage else PageKind.EMPTY,
            character_count=0,
            image_coverage=coverage,
            failure=ErrorInfo(
                category=ErrorCategory.EXTRACTION_ERROR,
                message=f"page {number} could not be read: {error}",
                stage=_STAGE,
                reader=_READER,
            ),
        )

    # Whitespace is not content: a page of layout spacing is empty, not textual.
    characters = len("".join(text.split()))
    return PageObservation(
        number=number,
        kind=_page_kind(characters, coverage, settings),
        character_count=characters,
        image_coverage=coverage,
        text=text,
    )


def _page_kind(characters: int, coverage: float, settings: PdfSettings) -> PageKind:
    """Apply the SPEC-01 req 2 versus req 3 thresholds, all of them configuration.

    Order matters. A page with enough characters is textual whatever else it holds, because
    its text layer is the content. Below that, an image covering the page means the content is
    a scan. Only a page with almost nothing either way is empty; anything left is a scan with
    a little text stamped over it, which is treated as scanned because its text is not the
    content it appears to be.
    """
    if characters >= settings.min_text_characters_for_text_page:
        return PageKind.TEXT
    if coverage >= settings.scanned_image_coverage_ratio:
        return PageKind.SCANNED if characters == 0 else PageKind.MIXED
    if (
        characters < settings.empty_page_max_characters
        and coverage < settings.empty_page_max_coverage_ratio
    ):
        return PageKind.EMPTY
    return PageKind.MIXED


def _page_text(page) -> str:
    """Extract a page's native text layer with layout preserved."""
    return page.extract_text() or ""


def _image_coverage(page) -> float:
    """Fraction of the page covered by images, clamped because images may overlap."""
    area = float(page.width) * float(page.height)
    if area <= 0:
        return 0.0
    covered = 0.0
    for image in page.images:
        width = float(image.get("x1", 0)) - float(image.get("x0", 0))
        height = float(image.get("bottom", 0)) - float(image.get("top", 0))
        if width > 0 and height > 0:
            covered += width * height
    return min(covered / area, 1.0)
