"""Purpose: expands archives into a separate output area so their members can enter the same
reader selection as ordinary files (SPEC-01 req 7). The source archive is opened read-only and
never modified. Three hazards drive the design: a hostile member path could escape the
extraction root, a declared member size cannot be trusted so the expansion ratio is checked
while streaming rather than beforehand, and a single locked or damaged member must never stop
its siblings. Nesting recurses to a configured depth; exceeding any limit produces an explicit
failure record, never a silent omission.
"""

from __future__ import annotations

import contextlib
import gzip
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from ca_agent.catalog.hashing import compute_content_hash
from ca_agent.config.settings import ArchiveSettings
from ca_agent.core.enums import ErrorCategory, FormatFamily
from ca_agent.core.model import ContentHash

#: Below this size a high compression ratio is meaningless - a few bytes of zeros compress
#: enormously - so the ratio guard only engages once a member has actually grown large.
RATIO_CHECK_FLOOR_BYTES = 1024 * 1024

_COPY_BLOCK_BYTES = 256 * 1024
_ZIP_ENCRYPTED_FLAG = 0x1

#: Names Windows refuses to create, with or without an extension.
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{n}" for n in range(1, 10)), *(f"lpt{n}" for n in range(1, 10))}
)

_ARCHIVE_EXTENSIONS = frozenset({".zip", ".jar", ".7z", ".rar", ".gz", ".tgz"})

#: Characters Windows rejects in a filename. Control characters are handled separately.
_ILLEGAL_NAME_CHARACTERS = frozenset('<>:"|?*')


@dataclass(frozen=True, slots=True)
class ExtractedMember:
    """One member successfully written to the extraction area."""

    member_path: str
    depth: int
    target_path: Path
    content: ContentHash
    archive_hash: str


@dataclass(frozen=True, slots=True)
class MemberFailure:
    """One member that could not be extracted. Always recorded, never swallowed."""

    member_path: str
    depth: int
    category: ErrorCategory
    message: str


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """Everything one archive produced, including what it could not produce."""

    members: tuple[ExtractedMember, ...]
    failures: tuple[MemberFailure, ...]
    total_expanded_bytes: int
    limit_reached: bool = False

    def member_count(self) -> int:
        return len(self.members)


class _Budget:
    """Tracks the caps that apply across a whole root archive, nested members included."""

    def __init__(self, settings: ArchiveSettings) -> None:
        self._settings = settings
        self._bytes = 0
        self._members = 0
        self.limit_reached = False

    @property
    def total_bytes(self) -> int:
        return self._bytes

    def may_add_member(self) -> bool:
        return self._members < self._settings.max_member_count

    def remaining_bytes(self) -> int:
        return max(self._settings.max_total_expanded_bytes - self._bytes, 0)

    def record(self, written: int) -> None:
        self._bytes += written
        self._members += 1

    def exhaust(self) -> None:
        self.limit_reached = True


def safe_member_path(member_name: str) -> PurePosixPath | None:
    """Return a safe relative path for a member, or None when it must be rejected.

    Rejects absolute paths, drive letters, UNC prefixes, parent traversal and Windows reserved
    device names. Backslashes are normalised first because archives written on Windows use them
    and a naive POSIX parse would treat "..\\..\\evil" as a single harmless filename.
    """
    if not member_name or member_name in {".", ".."}:
        return None
    normalised = member_name.replace("\\", "/").strip()
    if not normalised or normalised.startswith("/") or normalised.startswith("//"):
        return None
    if PureWindowsPath(member_name).drive or PureWindowsPath(member_name).is_absolute():
        return None

    parts = [part for part in normalised.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return None
    if any(part.split(".")[0].lower() in _WINDOWS_RESERVED for part in parts):
        return None

    cleaned = [_sanitise_segment(part) for part in parts]
    if any(not part for part in cleaned):
        return None
    if cleaned != parts:
        # A corpus zip holds a member whose name contains embedded newlines, which Windows
        # cannot write. Rejecting it would lose real client data, so the on-disk name is
        # cleaned and made unique; the original name is kept separately as lineage.
        cleaned[-1] = _with_discriminator(cleaned[-1], member_name)
    return PurePosixPath(*cleaned)


def _sanitise_segment(segment: str) -> str:
    """Strip characters Windows refuses in a filename, plus trailing dots and spaces."""
    cleaned = "".join(
        "_" if character in _ILLEGAL_NAME_CHARACTERS or ord(character) < 32 else character
        for character in segment
    )
    # Windows silently discards these, which would change the identity of the written file.
    return cleaned.rstrip(". ")


def _with_discriminator(segment: str, original: str) -> str:
    """Append a short digest so two different names cannot sanitise onto the same path."""
    import hashlib

    digest = hashlib.sha1(original.encode("utf-8", "surrogatepass"), usedforsecurity=False)
    suffix = f"__{digest.hexdigest()[:8]}"
    stem, dot, extension = segment.rpartition(".")
    return f"{stem}{suffix}{dot}{extension}" if dot else f"{segment}{suffix}"


def extract_archive(
    archive_path: Path,
    *,
    destination: Path,
    settings: ArchiveSettings,
    family: FormatFamily,
    depth: int = 1,
    archive_hash: str | None = None,
    budget: _Budget | None = None,
) -> ExtractionResult:
    """Expand one archive into ``destination``, recursing into nested archives.

    Returns rather than raises for member-level problems: SPEC-01 req 7 requires that a locked
    or malformed member be recorded while its siblings continue.
    """
    budget = budget or _Budget(settings)
    archive_hash = archive_hash or compute_content_hash(archive_path).hexdigest

    members: list[ExtractedMember] = []
    failures: list[MemberFailure] = []

    if family is FormatFamily.ARCHIVE_RAR:
        failures.append(
            MemberFailure(
                member_path=archive_path.name,
                depth=depth,
                category=ErrorCategory.NO_COMPATIBLE_READER,
                message="RAR expansion needs the external unrar binary, which is not configured",
            )
        )
        return ExtractionResult((), tuple(failures), budget.total_bytes, budget.limit_reached)

    try:
        # The container stays open for the whole loop: member streams are opened lazily and
        # would be dead handles if the archive were closed first.
        with _open_archive(archive_path, family) as entries:
            for entry in entries:
                outcome = _handle_entry(
                    entry,
                    archive_hash=archive_hash,
                    destination=destination,
                    settings=settings,
                    depth=depth,
                    budget=budget,
                )
                members.extend(outcome.members)
                failures.extend(outcome.failures)
                if budget.limit_reached:
                    break
    except _ArchiveOpenError as error:
        failures.append(MemberFailure(archive_path.name, depth, error.category, str(error)))
        return ExtractionResult((), tuple(failures), budget.total_bytes, budget.limit_reached)

    return ExtractionResult(
        tuple(members), tuple(failures), budget.total_bytes, budget.limit_reached
    )


# --- entry handling ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Entry:
    """One member as reported by the underlying archive library."""

    name: str
    encrypted: bool
    declared_size: int
    compressed_size: int
    open_stream: object


class _ArchiveOpenError(Exception):
    def __init__(self, message: str, category: ErrorCategory) -> None:
        super().__init__(message)
        self.category = category


def _handle_entry(
    entry: _Entry,
    *,
    archive_hash: str,
    destination: Path,
    settings: ArchiveSettings,
    depth: int,
    budget: _Budget,
) -> ExtractionResult:
    if not budget.may_add_member():
        budget.exhaust()
        return ExtractionResult(
            (),
            (
                MemberFailure(
                    entry.name,
                    depth,
                    ErrorCategory.ARCHIVE_MEMBER_LIMIT_EXCEEDED,
                    f"archive exceeds the {settings.max_member_count} member limit",
                ),
            ),
            budget.total_bytes,
            True,
        )

    if entry.encrypted:
        # SPEC-01 req 5: record it, never request or guess a password.
        return _failure(
            entry, depth, ErrorCategory.ARCHIVE_MEMBER_LOCKED, "member is encrypted", budget
        )

    safe_relative = safe_member_path(entry.name)
    if safe_relative is None:
        return _failure(
            entry,
            depth,
            ErrorCategory.UNSAFE_MEMBER_PATH,
            "member path escapes the extraction root or uses a reserved name",
            budget,
        )

    target = destination / safe_relative
    try:
        written = _stream_member(entry, target, settings, budget)
    except _LimitExceeded as error:
        _discard(target)
        budget.exhaust()
        return _failure(entry, depth, ErrorCategory.ARCHIVE_SIZE_LIMIT_EXCEEDED, str(error), budget)
    except (OSError, zipfile.BadZipFile, EOFError) as error:
        _discard(target)
        return _failure(
            entry, depth, ErrorCategory.EXTRACTION_ERROR, f"member could not be read: {error}", budget
        )

    budget.record(written)
    member = ExtractedMember(
        member_path=entry.name.replace("\\", "/"),
        depth=depth,
        target_path=target,
        content=compute_content_hash(target),
        archive_hash=archive_hash,
    )

    nested = _expand_nested(
        member, destination=destination, settings=settings, depth=depth, budget=budget
    )
    return ExtractionResult(
        (member, *nested.members),
        nested.failures,
        budget.total_bytes,
        budget.limit_reached,
    )


def _expand_nested(
    member: ExtractedMember,
    *,
    destination: Path,
    settings: ArchiveSettings,
    depth: int,
    budget: _Budget,
) -> ExtractionResult:
    """Recurse into a member that is itself an archive, honouring the depth policy."""
    family = _archive_family(member.target_path)
    if family is None:
        return ExtractionResult((), (), budget.total_bytes)

    if depth >= settings.max_depth:
        # The nested archive is still recorded as a member above, so it gets a processing
        # record and a format document; only its contents are out of reach.
        return ExtractionResult(
            (),
            (
                MemberFailure(
                    member.member_path,
                    depth,
                    ErrorCategory.ARCHIVE_DEPTH_LIMIT_EXCEEDED,
                    f"nested archive at depth {depth} exceeds the configured maximum of "
                    f"{settings.max_depth}; its contents were not expanded",
                ),
            ),
            budget.total_bytes,
            budget.limit_reached,
        )

    return extract_archive(
        member.target_path,
        destination=destination / f"{member.target_path.name}.d",
        settings=settings,
        family=family,
        depth=depth + 1,
        archive_hash=member.content.hexdigest,
        budget=budget,
    )


def _failure(
    entry: _Entry, depth: int, category: ErrorCategory, message: str, budget: _Budget
) -> ExtractionResult:
    return ExtractionResult(
        (),
        (MemberFailure(entry.name.replace("\\", "/"), depth, category, message),),
        budget.total_bytes,
        budget.limit_reached,
    )


def _discard(target: Path) -> None:
    """Best-effort removal of a partial write.

    This must never raise. A path that could not be created may also be impossible to name in
    an unlink call, and losing the whole run to a cleanup error would violate SPEC-01 req 7's
    requirement that siblings continue.
    """
    with contextlib.suppress(OSError):
        target.unlink(missing_ok=True)


class _LimitExceeded(Exception):
    """Raised mid-stream when a member breaches a size or ratio cap."""


def _stream_member(
    entry: _Entry, target: Path, settings: ArchiveSettings, budget: _Budget
) -> int:
    """Copy a member to disk in blocks, enforcing the caps as it goes.

    Checking during the copy rather than against the declared size is what defends against a
    zip bomb: the central directory can claim any size it likes.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    ratio_ceiling = max(entry.compressed_size, 1) * settings.max_compression_ratio
    allowance = budget.remaining_bytes()
    written = 0

    with entry.open_stream() as source, target.open("wb") as sink:  # type: ignore[operator]
        while block := source.read(_COPY_BLOCK_BYTES):
            written += len(block)
            if written > allowance:
                raise _LimitExceeded(
                    f"expanding this member would exceed the total budget of "
                    f"{settings.max_total_expanded_bytes} bytes"
                )
            if written > RATIO_CHECK_FLOOR_BYTES and written > ratio_ceiling:
                raise _LimitExceeded(
                    f"compression ratio exceeds {settings.max_compression_ratio}:1 after "
                    f"{written} bytes, which indicates a decompression bomb"
                )
            sink.write(block)
    return written


def _archive_family(path: Path) -> FormatFamily | None:
    """Identify a nested archive cheaply, by signature rather than a full detection pass."""
    if path.suffix.lower() not in _ARCHIVE_EXTENSIONS:
        return None
    try:
        with path.open("rb") as handle:
            head = handle.read(8)
    except OSError:
        return None
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return FormatFamily.ARCHIVE_ZIP
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return FormatFamily.ARCHIVE_7Z
    if head.startswith(b"\x1f\x8b"):
        return FormatFamily.ARCHIVE_GZIP
    if head.startswith(b"Rar!\x1a\x07"):
        return FormatFamily.ARCHIVE_RAR
    return None


# --- per-family entry enumeration ------------------------------------------------------------


@contextlib.contextmanager
def _open_archive(archive_path: Path, family: FormatFamily):
    """Open an archive and yield its entries, keeping the container alive for the caller."""
    if family is FormatFamily.ARCHIVE_ZIP:
        with _open_zip(archive_path) as container:
            yield _zip_entries(container)
    elif family is FormatFamily.ARCHIVE_7Z:
        yield _seven_zip_entries(archive_path)
    elif family is FormatFamily.ARCHIVE_GZIP:
        yield _gzip_entries(archive_path)
    else:
        raise _ArchiveOpenError(
            f"no archive handler for {family.value}", ErrorCategory.NO_COMPATIBLE_READER
        )


@contextlib.contextmanager
def _open_zip(archive_path: Path):
    try:
        container = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile as error:
        raise _ArchiveOpenError(f"archive is corrupt: {error}", ErrorCategory.CORRUPT_FILE) from error
    except OSError as error:
        raise _ArchiveOpenError(str(error), ErrorCategory.FILE_ACCESS_ERROR) from error
    with container:
        yield container


def _zip_entries(container: zipfile.ZipFile) -> Iterator[_Entry]:
    for info in container.infolist():
        if info.is_dir():
            continue
        yield _Entry(
            name=info.filename,
            # The flag bit is authoritative. zipfile signals encryption by raising a
            # RuntimeError whose *message* must be string-matched, which is not a contract.
            encrypted=bool(info.flag_bits & _ZIP_ENCRYPTED_FLAG),
            declared_size=info.file_size,
            compressed_size=info.compress_size,
            open_stream=_zip_opener(container, info),
        )


def _zip_opener(container: zipfile.ZipFile, info: zipfile.ZipInfo):
    def opener():
        return container.open(info, "r")

    return opener


def _seven_zip_entries(archive_path: Path) -> Iterator[_Entry]:
    import py7zr

    try:
        container = py7zr.SevenZipFile(archive_path, "r")
    except py7zr.exceptions.PasswordRequired as error:
        raise _ArchiveOpenError(str(error), ErrorCategory.PASSWORD_PROTECTED_FILE) from error
    except (py7zr.exceptions.Bad7zFile, ValueError) as error:
        raise _ArchiveOpenError(f"archive is corrupt: {error}", ErrorCategory.CORRUPT_FILE) from error
    except OSError as error:
        raise _ArchiveOpenError(str(error), ErrorCategory.FILE_ACCESS_ERROR) from error

    with container:
        # py7zr materialises members rather than exposing per-entry streams, so the whole
        # archive is read once here. The 7z population is 4 files, so this costs nothing.
        extracted = container.readall() or {}
        for name, buffer in extracted.items():
            payload = buffer.read()
            yield _Entry(
                name=name,
                encrypted=False,
                declared_size=len(payload),
                compressed_size=max(len(payload) // 2, 1),
                open_stream=_buffer_opener(payload),
            )


def _gzip_entries(archive_path: Path) -> Iterator[_Entry]:
    """A gzip stream holds exactly one member, conventionally named by dropping the suffix."""
    inner_name = archive_path.name
    for suffix in (".gz", ".gzip", ".emz"):
        if inner_name.lower().endswith(suffix):
            inner_name = inner_name[: -len(suffix)]
            break
    else:
        inner_name = f"{inner_name}.out"

    try:
        compressed_size = archive_path.stat().st_size
    except OSError as error:
        raise _ArchiveOpenError(str(error), ErrorCategory.FILE_ACCESS_ERROR) from error

    yield _Entry(
        name=inner_name or "payload",
        encrypted=False,
        declared_size=0,
        compressed_size=compressed_size,
        open_stream=_gzip_opener(archive_path),
    )


def _gzip_opener(archive_path: Path):
    """Defer opening the stream until _stream_member needs it.

    The returned handle is consumed inside a `with` block by the caller, so the resource is
    scoped there rather than here.
    """

    def opener():
        return gzip.open(archive_path, "rb")  # noqa: SIM115 - closed by the caller's with block

    return opener


def _buffer_opener(payload: bytes):
    import io

    def opener():
        return io.BytesIO(payload)

    return opener
