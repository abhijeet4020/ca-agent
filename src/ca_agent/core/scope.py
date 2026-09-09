"""Purpose: SPEC-01 req 7 makes the category/client folder the unit of deduplication and
indexing, and forbids merging scopes that share a client name. Client name alone is therefore
never an identity. ClientScope binds category and client into one value whose scope_id is a
slug plus a SHA-1 discriminator, so two same-named clients in different categories can never
collide on disk or in a dedup index.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import PurePosixPath

#: Keeps a single path segment well under the Windows MAX_PATH budget once the
#: output directory tree is appended to it.
_MAX_SLUG_LENGTH = 40

_NON_SLUG_CHARACTERS = re.compile(r"[^a-z0-9]+")


def slugify(value: str) -> str:
    """Reduce an arbitrary directory name to a lowercase filesystem-safe token.

    The corpus contains spaces, ampersands, parentheses and Devanagari, none of which are
    safe as-is across Windows and POSIX. Collisions introduced by slugging are harmless
    because callers always pair the slug with a hash discriminator.
    """
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    collapsed = _NON_SLUG_CHARACTERS.sub("_", folded.lower()).strip("_")
    return collapsed[:_MAX_SLUG_LENGTH] or "unnamed"


@dataclass(frozen=True, slots=True)
class ClientScope:
    """One independent deduplication and indexing scope.

    ``scope_root`` and ``category_is_scope`` are excluded from equality: identity is the
    (category, client) pair, and two records describing the same pair must compare equal
    regardless of how the root was expressed.
    """

    category: str
    client: str
    scope_root: PurePosixPath = field(compare=False)
    category_is_scope: bool = field(default=False, compare=False)

    def __post_init__(self) -> None:
        if not self.category or not self.client:
            raise ValueError("ClientScope requires a non-empty category and client")
        if self.scope_root.is_absolute():
            raise ValueError(f"scope_root must be repository-relative, got {self.scope_root}")

    @property
    def scope_id(self) -> str:
        """Stable, filesystem-safe identifier for this scope.

        The SHA-1 suffix is a collision discriminator over the exact original names, not a
        security boundary; the NUL separator prevents ("ab", "c") colliding with ("a", "bc").
        """
        raw = f"{self.category}\x00{self.client}".encode()
        digest = hashlib.sha1(raw, usedforsecurity=False).hexdigest()[:12]
        return f"{slugify(self.category)}__{slugify(self.client)}__{digest}"

    def contains(self, relative_path: PurePosixPath) -> bool:
        """True when ``relative_path`` lies inside this scope's root."""
        return relative_path.is_relative_to(self.scope_root)
