"""Purpose: SPEC-01 req 7 requires a SHA-256 content hash computed before any conversion, and
that hash is what makes deduplication, reuse and version identity possible. Reading is streamed
in fixed blocks because the corpus contains multi-gigabyte Tally backups and archive members
that must never be loaded whole into memory.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from ca_agent.core.model import ContentHash

#: One megabyte. Large enough that syscall overhead is negligible, small enough that thousands
#: of concurrent hashes cannot exhaust memory.
HASH_BLOCK_BYTES = 1024 * 1024


def compute_content_hash(path: Path) -> ContentHash:
    """Stream a file and return its SHA-256 identity.

    OSError is allowed to propagate: the caller records FILE_ACCESS_ERROR against the source
    rather than this function inventing a partial result.
    """
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while block := handle.read(HASH_BLOCK_BYTES):
            digest.update(block)
            total += len(block)
    return ContentHash(hexdigest=digest.hexdigest(), size_bytes=total)


def hash_bytes(payload: bytes) -> ContentHash:
    """SHA-256 identity of an in-memory payload, used for archive members already decoded."""
    return ContentHash(hexdigest=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload))
