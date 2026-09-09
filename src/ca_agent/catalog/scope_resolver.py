"""Purpose: maps a corpus-relative path to its owning ClientScope. Naive extraction of the
second path component is wrong for this corpus because the Mauli Hospital backup has no client
directory level, so resolution instead matches against an explicit scope table using the longest
registered prefix. A path matching nothing yields no scope at all rather than a guess, which
SPEC-01 req 4 requires ("do not invent ... content classifications").
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from pathlib import Path, PurePosixPath

from ca_agent.core.enums import ErrorCategory
from ca_agent.core.scope import ClientScope


class ScopeResolver:
    """Resolves corpus-relative paths to scopes by longest registered prefix."""

    def __init__(self, scopes: Iterable[ClientScope]) -> None:
        # Sorted longest-root-first so the first containing match is also the most specific.
        self._scopes: tuple[ClientScope, ...] = tuple(
            sorted(scopes, key=lambda scope: len(scope.scope_root.parts), reverse=True)
        )

    def __len__(self) -> int:
        return len(self._scopes)

    def scopes(self) -> tuple[ClientScope, ...]:
        """Registered scopes, most specific first. Returns a copy-safe immutable tuple."""
        return self._scopes

    def resolve(self, relative_path: PurePosixPath) -> ClientScope | None:
        """Return the owning scope, or None when the path belongs to no registered scope."""
        if relative_path.is_absolute():
            raise ValueError(f"expected a corpus-relative path, got absolute {relative_path}")
        for scope in self._scopes:
            if scope.contains(relative_path):
                return scope
        return None

    def unresolved_error_category(self) -> ErrorCategory:
        """Error category recorded for a path that resolves to no scope."""
        return ErrorCategory.UNSCOPED_PATH


def discover_scopes(
    raw_root: Path,
    category_is_scope: frozenset[str],
) -> tuple[ClientScope, ...]:
    """Build the scope table by inspecting the corpus layout.

    Ordinary categories contribute one scope per client subdirectory. Categories named in
    ``category_is_scope`` contribute a single scope for the category directory itself, which
    is how the Mauli Hospital backup root is represented. Categories with no subdirectories
    contribute nothing, so the empty "GST Audit Clients" directory is not an error.
    """
    return tuple(_iter_scopes(raw_root, category_is_scope))


def _iter_scopes(raw_root: Path, category_is_scope: frozenset[str]) -> Iterator[ClientScope]:
    for category_dir in sorted(entry for entry in raw_root.iterdir() if entry.is_dir()):
        category = category_dir.name
        if category in category_is_scope:
            yield ClientScope(
                category=category,
                client=category,
                scope_root=PurePosixPath(category),
                category_is_scope=True,
            )
            continue
        for client_dir in sorted(entry for entry in category_dir.iterdir() if entry.is_dir()):
            yield ClientScope(
                category=category,
                client=client_dir.name,
                scope_root=PurePosixPath(category) / client_dir.name,
            )
