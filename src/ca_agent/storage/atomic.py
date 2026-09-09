"""Purpose: SPEC-01 req 8 forbids ever modifying or overwriting a published artifact, and a
crash mid-write must not leave a half-written file that a later run mistakes for real output.
Every write here therefore goes to a temporary file in the destination directory and is then
moved into place with os.replace, which is atomic on NTFS and POSIX alike. write_new_exclusive
adds a create-only guarantee for manifests, so sealing an ordinal twice fails loudly.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

_ENCODING = "utf-8"
#: O_BINARY exists only on Windows; on POSIX the flag is a no-op.
_BINARY_FLAG = getattr(os, "O_BINARY", 0)


class AtomicWriteError(OSError):
    """Raised when a write cannot be completed without violating the immutability contract."""


def _write_then_replace(target: Path, payload: bytes) -> None:
    """Write to a sibling temporary file, flush to disk, then move into place atomically.

    The temporary file must share the destination directory: os.replace is only atomic within
    a single filesystem.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(target)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise AtomicWriteError(f"failed atomic write to {target}: {error}") from error


def atomic_write_bytes(target: Path, payload: bytes) -> None:
    """Publish bytes at ``target`` atomically."""
    _write_then_replace(target, payload)


def atomic_write_text(target: Path, payload: str) -> None:
    """Publish text at ``target`` atomically, always UTF-8.

    The encoding is explicit because the corpus contains Devanagari client names that the
    default Windows ANSI codepage cannot represent.
    """
    _write_then_replace(target, payload.encode(_ENCODING))


def atomic_write_json(target: Path, payload: Any) -> None:
    """Publish a JSON document at ``target`` atomically, with stable key ordering."""
    text = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
    _write_then_replace(target, f"{text}\n".encode(_ENCODING))


def write_new_exclusive(target: Path, payload: bytes) -> None:
    """Create ``target`` only if it does not already exist.

    Used for sealed manifests: republishing an ordinal would rewrite history, so the operating
    system refuses it rather than relying on caller discipline.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _BINARY_FLAG)
    except FileExistsError as error:
        raise AtomicWriteError(f"refusing to overwrite existing artifact {target}") from error
    except OSError as error:
        raise AtomicWriteError(f"cannot create {target}: {error}") from error
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:
        raise AtomicWriteError(f"failed writing {target}: {error}") from error


def append_jsonl(target: Path, record: Any) -> None:
    """Append one JSON line to an append-only journal.

    Journals are the one place the pipeline appends rather than publishes whole; callers must
    serialise access through a single writer so lines never interleave.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False)
    try:
        with target.open("a", encoding=_ENCODING, newline="\n") as handle:
            handle.write(f"{line}\n")
    except OSError as error:
        raise AtomicWriteError(f"cannot append to {target}: {error}") from error
