"""Purpose: the value objects that carry lineage from a raw byte range to every derived
artifact, which SPEC-01 requirements 4, 7 and 8 all depend on. SourceRef records where content
came from (including its archive chain), ContentHash records what it was, and WorkKey is the
scope-qualified identity used for scheduling, deduplication and reuse. All types are frozen so
a record cannot be edited after it is written, mirroring the immutability rule for outputs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath

from ca_agent.core.enums import ErrorCategory, FormatFamily, ProcessingStatus, Route
from ca_agent.core.scope import ClientScope, slugify

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_COMPANION_SUFFIX = ".format.md"
#: Enough of the digest to make same-named files in different folders distinguishable on disk
#: while keeping directory names short on Windows.
_PATH_DISCRIMINATOR_LENGTH = 10


@dataclass(frozen=True, slots=True)
class ContentHash:
    """SHA-256 identity of a byte stream, computed before any conversion (SPEC-01 req 7)."""

    hexdigest: str
    size_bytes: int
    algorithm: str = "sha256"

    def __post_init__(self) -> None:
        if not _SHA256_PATTERN.match(self.hexdigest):
            raise ValueError(f"expected a lowercase hex sha256 digest, got {self.hexdigest!r}")
        if self.size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")

    def shard(self) -> str:
        """First byte of the digest, used to fan out content directories."""
        return self.hexdigest[:2]


@dataclass(frozen=True, slots=True)
class ArchiveRef:
    """One hop in an archive chain: which archive, which member, at what nesting depth."""

    archive_hash: str
    member_path: str
    depth: int

    def __post_init__(self) -> None:
        if self.depth < 1:
            raise ValueError("archive depth starts at 1 for a top-level member")


@dataclass(frozen=True, slots=True)
class SourceRef:
    """Where one piece of content was found.

    Several SourceRefs may share a ContentHash: SPEC-01 req 7 requires that duplicates within a
    client scope be processed once while every source path is retained.
    """

    scope: ClientScope
    relative_path: PurePosixPath
    archive_chain: tuple[ArchiveRef, ...] = ()
    size_bytes: int = 0
    mtime_ns: int = field(default=0, compare=False)

    def __post_init__(self) -> None:
        if self.relative_path.is_absolute():
            raise ValueError(f"relative_path must be corpus-relative, got {self.relative_path}")
        if not self.scope.contains(self.relative_path):
            raise ValueError(
                f"{self.relative_path} does not lie within scope root {self.scope.scope_root}"
            )

    def is_archive_member(self) -> bool:
        return bool(self.archive_chain)

    def display_name(self) -> str:
        """Filename as it appears to the user: the member name when inside an archive."""
        if self.archive_chain:
            return PurePosixPath(self.archive_chain[-1].member_path).name
        return self.relative_path.name

    def companion_name(self) -> str:
        """Companion document name, preserving the full filename (SPEC-01 req 4).

        Keeping the original extension is what stops report.xlsx and report.pdf colliding.
        """
        return f"{self.display_name()}{_COMPANION_SUFFIX}"

    def companion_relative_path(self) -> PurePosixPath:
        """Companion path preserving the source-relative directory structure (req 4)."""
        return self.relative_path.parent / self.companion_name()

    def path_slug(self) -> str:
        """Filesystem-safe, collision-resistant token for this exact source location.

        Two files with the same name in different folders, and the same file reached through
        different archives, must land in different output directories.
        """
        import hashlib

        chain = "|".join(f"{ref.archive_hash}:{ref.member_path}" for ref in self.archive_chain)
        raw = f"{self.relative_path.as_posix()}\x00{chain}".encode()
        digest = hashlib.sha1(raw, usedforsecurity=False).hexdigest()[:_PATH_DISCRIMINATOR_LENGTH]
        return f"{slugify(self.display_name())}__{digest}"


@dataclass(frozen=True, slots=True)
class FormatProbe:
    """What detection observed about one file.

    ``confidence`` records how the family was decided - by magic number, by inspecting a
    container's members, by decoding text, or only by falling back to the extension. That
    provenance goes into the format document so a reader can tell an observed fact from an
    assumption, which SPEC-01 req 4 requires.
    """

    family: FormatFamily
    confidence: str
    declared_extension: str
    subtype: str | None = None
    extension_conflict: bool = False
    macros_present: bool = False
    encoding: str | None = None
    bytes_inspected: int = 0
    evidence: tuple[str, ...] = ()
    detail: str | None = None

    def is_readable(self) -> bool:
        """False for outcomes that have no extraction route at all."""
        return self.family not in {
            FormatFamily.UNKNOWN,
            FormatFamily.TALLY_BINARY,
            FormatFamily.NON_DATA_ARTIFACT,
            FormatFamily.EXECUTABLE,
            FormatFamily.EMPTY,
        }


@dataclass(frozen=True, slots=True)
class WorkKey:
    """Scope-qualified identity of one unit of work.

    Including scope_id is what makes cross-category deduplication structurally impossible
    (SPEC-01 req 7). Including the route fingerprint is what makes a configuration change
    invalidate exactly the affected outputs (req 8).
    """

    scope_id: str
    content_hexdigest: str
    route: Route
    route_fingerprint: str


@dataclass(frozen=True, slots=True)
class ErrorInfo:
    """Why a unit of work did not succeed. Never collapsed into a bare message string."""

    category: ErrorCategory
    message: str
    stage: str
    reader: str | None = None
    traceback_text: str | None = None


@dataclass(frozen=True, slots=True)
class OutputRef:
    """One derived artifact produced by a unit of work."""

    kind: str
    relative_path: PurePosixPath
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ProcessingRecord:
    """The per-file outcome that SPEC-01 req 6 requires for every discovered file.

    A record exists even for non-data artifacts and unreadable formats; only the status and
    error category differ. That is what makes "no silent skips" a checkable property.
    """

    work_key: WorkKey
    source_refs: tuple[SourceRef, ...]
    content: ContentHash
    status: ProcessingStatus
    version_id: str
    started_at: datetime
    ended_at: datetime
    outputs: tuple[OutputRef, ...] = ()
    error: ErrorInfo | None = None
    duplicate_of: str | None = None
    warnings: tuple[ErrorInfo, ...] = ()

    def __post_init__(self) -> None:
        if self.status is ProcessingStatus.SUCCESS and self.error is not None:
            raise ValueError("a successful record cannot carry an error")
        if self.status is not ProcessingStatus.SUCCESS and self.error is None:
            raise ValueError(f"status {self.status.value} requires an error to be recorded")

    def is_active_candidate(self) -> bool:
        """Only a fully successful record may become the active version (req 3 and 8)."""
        return self.status.is_active_candidate()

    def source_paths(self) -> tuple[str, ...]:
        """All retained source paths for this content. Returns a copy, never internal state."""
        return tuple(ref.relative_path.as_posix() for ref in self.source_refs)


@dataclass(frozen=True, slots=True)
class OutputVersion:
    """One published version of a unit of work's outputs."""

    version_id: str
    run_ordinal: int
    run_id: str
    created_at: datetime
    status: ProcessingStatus
    route_fingerprint: str
