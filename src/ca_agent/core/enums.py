"""Purpose: SPEC-01 requires that every discovered file carry an explicit outcome and that
password errors stay distinct from corruption, missing readers and API failures. This module
is the single closed vocabulary for those outcomes - processing status, error category and
processing route - so no layer can invent an ad-hoc string and defeat that guarantee.
"""

from __future__ import annotations

from enum import Enum


class ProcessingStatus(str, Enum):
    """Terminal outcome of one unit of work. Only SUCCESS may become the active version."""

    SUCCESS = "success"
    PARTIAL = "partial"
    LOCKED = "locked"
    FAILED = "failed"
    NO_READER = "no_reader"
    EXCLUDED_NON_DATA = "excluded_non_data"
    SKIPPED_DUPLICATE = "skipped_duplicate"

    def is_active_candidate(self) -> bool:
        """SPEC-01 req 3 and 8: partial and failed results are history, never active."""
        return self is ProcessingStatus.SUCCESS


class ErrorCategory(str, Enum):
    """Why a unit of work did not succeed.

    SPEC-01 req 5 is explicit that PASSWORD_PROTECTED_FILE must not absorb every reader
    exception, so corruption, unsupported formats and API faults are separate members.
    """

    PASSWORD_PROTECTED_FILE = "PASSWORD_PROTECTED_FILE"
    NO_COMPATIBLE_READER = "NO_COMPATIBLE_READER"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    CORRUPT_FILE = "CORRUPT_FILE"
    EMPTY_FILE = "EMPTY_FILE"
    EXTRACTION_ERROR = "EXTRACTION_ERROR"
    CONVERSION_ERROR = "CONVERSION_ERROR"
    PARQUET_WRITE_ERROR = "PARQUET_WRITE_ERROR"
    FORMULA_NO_CACHED_VALUE = "FORMULA_NO_CACHED_VALUE"
    FORMAT_MISMATCH = "FORMAT_MISMATCH"
    API_ERROR = "API_ERROR"
    API_RATE_LIMITED = "API_RATE_LIMITED"
    API_TIMEOUT = "API_TIMEOUT"
    RESPONSE_PARSE_ERROR = "RESPONSE_PARSE_ERROR"
    ARCHIVE_DEPTH_LIMIT_EXCEEDED = "ARCHIVE_DEPTH_LIMIT_EXCEEDED"
    ARCHIVE_SIZE_LIMIT_EXCEEDED = "ARCHIVE_SIZE_LIMIT_EXCEEDED"
    ARCHIVE_MEMBER_LIMIT_EXCEEDED = "ARCHIVE_MEMBER_LIMIT_EXCEEDED"
    ARCHIVE_MEMBER_LOCKED = "ARCHIVE_MEMBER_LOCKED"
    UNSAFE_MEMBER_PATH = "UNSAFE_MEMBER_PATH"
    FILE_ACCESS_ERROR = "FILE_ACCESS_ERROR"
    EXECUTABLE_NOT_PROCESSED = "EXECUTABLE_NOT_PROCESSED"
    NON_DATA_ARTIFACT = "NON_DATA_ARTIFACT"
    UNSCOPED_PATH = "UNSCOPED_PATH"
    UNEXPECTED_EXCEPTION = "UNEXPECTED_EXCEPTION"
    CONFIG_ERROR = "CONFIG_ERROR"


class FormatFamily(str, Enum):
    """What a file actually is, decided from its bytes rather than its name.

    Probing the corpus showed extensions are unreliable at scale, so this vocabulary describes
    observed content. UNKNOWN is a legitimate, recorded outcome (SPEC-01 req 4 forbids
    inventing a classification), not a failure to try.
    """

    SPREADSHEET_OOXML = "spreadsheet_ooxml"
    SPREADSHEET_BIFF = "spreadsheet_biff"
    SPREADSHEET_XLSB = "spreadsheet_xlsb"
    DELIMITED_TEXT = "delimited_text"
    PDF = "pdf"
    IMAGE = "image"
    WORD_OOXML = "word_ooxml"
    WORD_OLE = "word_ole"
    PRESENTATION_OOXML = "presentation_ooxml"
    RTF = "rtf"
    HTML = "html"
    PLAIN_TEXT = "plain_text"
    EMAIL = "email"
    JSON = "json"
    XML = "xml"
    ARCHIVE_ZIP = "archive_zip"
    ARCHIVE_7Z = "archive_7z"
    ARCHIVE_RAR = "archive_rar"
    ARCHIVE_GZIP = "archive_gzip"
    ENCRYPTED_OOXML = "encrypted_ooxml"
    ENCRYPTED_OLE = "encrypted_ole"
    TALLY_BINARY = "tally_binary"
    JAVA_SERIALIZED = "java_serialized"
    KEYSTORE = "keystore"
    NON_DATA_ARTIFACT = "non_data_artifact"
    EXECUTABLE = "executable"
    EMPTY = "empty"
    UNKNOWN = "unknown"


class Route(str, Enum):
    """Processing route chosen by signature-based reader selection (SPEC-01 req 6)."""

    TABULAR_PARQUET = "tabular_parquet"
    PDF_TEXT = "pdf_text"
    PDF_VISION = "pdf_vision"
    TEXT_EXTRACT = "text_extract"
    STRUCTURED = "structured"
    VISION_IMAGE = "vision_image"
    ARCHIVE = "archive"
    NO_READER = "no_reader"
    NON_DATA = "non_data"


#: Config sections whose fingerprint governs reuse of a route's outputs (SPEC-01 req 8).
ROUTE_CONFIG_SECTIONS: dict[Route, tuple[str, ...]] = {
    Route.TABULAR_PARQUET: ("tabular",),
    Route.PDF_TEXT: ("pdf", "text", "chunking"),
    Route.PDF_VISION: ("pdf", "vision"),
    Route.TEXT_EXTRACT: ("text", "chunking"),
    Route.STRUCTURED: ("structured", "tabular", "chunking"),
    Route.VISION_IMAGE: ("vision",),
    Route.ARCHIVE: ("archive",),
    Route.NO_READER: ("detection",),
    Route.NON_DATA: ("detection",),
}
