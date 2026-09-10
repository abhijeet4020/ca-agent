"""Purpose: the neutral description of one processed file that becomes its `*.format.md`
(SPEC-01 req 4). ARCHITECTURE.md puts docgen and readers in the same layer, so docgen cannot
see a `TabularResult` or a `PdfResult`; the orchestrator translates whichever reader ran into
this shape. That indirection is what keeps every reader free of document-writing code. The
one rule the model enforces structurally is requirement 4's prohibition on invention: a value
that was not observed is the explicit UNKNOWN string, never an omission or a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from ca_agent.core.enums import ProcessingStatus, Route
from ca_agent.core.model import ErrorInfo, OutputRef

#: Written wherever something could not be observed. Requirement 4 forbids inventing a value
#: and an omission is ambiguous, so absence is stated rather than implied.
UNKNOWN = "unknown"
_COMPANION_SUFFIX = ".format.md"


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    """Everything requirement 4's first bullet requires a document to identify."""

    source_relpath: str
    category: str
    client: str
    scope_id: str
    extension: str
    detected_format: str
    detection_confidence: str
    content_sha256: str
    size_bytes: int
    version_id: str
    extension_conflict: bool = False
    archive_id: str | None = None
    member_path: str | None = None

    def display_name(self) -> str:
        """The filename as the user knows it - the member name when inside an archive."""
        if self.member_path:
            return PurePosixPath(self.member_path).name
        return PurePosixPath(self.source_relpath).name

    def companion_relative_path(self) -> PurePosixPath:
        """Companion path preserving the source-relative directory and the full filename.

        Keeping the extension is what stops report.xlsx and report.pdf colliding, and keeping
        the directory is what stops two folders' report.xlsx colliding within one client.
        """
        source = PurePosixPath(self.source_relpath)
        return source.parent / f"{self.display_name()}{_COMPANION_SUFFIX}"


@dataclass(frozen=True, slots=True)
class DocumentTable:
    """A table of observations, such as one row per worksheet or per page."""

    caption: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]

    def is_empty(self) -> bool:
        return not self.rows


@dataclass(frozen=True, slots=True)
class DocumentSection:
    """One heading of the document.

    A section carries label/value rows, a table, free notes, or nothing at all. An empty
    section is still rendered, because "no tables were found" and "tables were never looked
    for" must not read the same.
    """

    heading: str
    rows: tuple[tuple[str, str], ...] = ()
    table: DocumentTable | None = None
    notes: tuple[str, ...] = ()

    def is_empty(self) -> bool:
        return not self.rows and not self.notes and (self.table is None or self.table.is_empty())


@dataclass(frozen=True, slots=True)
class FormatDocument:
    """The complete description of one processed file."""

    identity: SourceIdentity
    route: Route
    status: ProcessingStatus
    reader: str | None = None
    sections: tuple[DocumentSection, ...] = ()
    outputs: tuple[OutputRef, ...] = ()
    error: ErrorInfo | None = None
    warnings: tuple[ErrorInfo, ...] = ()
    retry_action: str | None = None

    def succeeded(self) -> bool:
        return self.status is ProcessingStatus.SUCCESS
