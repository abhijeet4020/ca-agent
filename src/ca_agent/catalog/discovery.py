"""Purpose: walks the untouched Bronze corpus and produces one SourceRef per file, which is the
input to everything downstream. SPEC-01 req 6 forbids excluding any file, so the walk keeps
hidden files, extensionless files and shell artifacts and leaves every classification decision
to reader selection. Files that belong to no registered scope are returned separately rather
than dropped, because a silent omission would defeat the "every discovered file has a record"
guarantee in acceptance criterion 9.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ca_agent.catalog.scope_resolver import ScopeResolver, discover_scopes
from ca_agent.core.model import SourceRef
from ca_agent.core.scope import ClientScope


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """Everything the walk found, including what it could not place."""

    sources: tuple[SourceRef, ...]
    unscoped_paths: tuple[PurePosixPath, ...]
    scopes: tuple[ClientScope, ...]
    unreadable_paths: tuple[PurePosixPath, ...] = ()

    def file_count(self) -> int:
        """Total files seen, whether or not they resolved to a scope."""
        return len(self.sources) + len(self.unscoped_paths) + len(self.unreadable_paths)


def discover_source_files(
    raw_root: Path,
    category_is_scope: frozenset[str] = frozenset(),
) -> DiscoveryResult:
    """Enumerate every file under the corpus root and assign it to a client scope."""
    scopes = discover_scopes(raw_root, category_is_scope)
    resolver = ScopeResolver(scopes)

    sources: list[SourceRef] = []
    unscoped: list[PurePosixPath] = []
    unreadable: list[PurePosixPath] = []

    for absolute in _iter_files(raw_root):
        relative = PurePosixPath(absolute.relative_to(raw_root).as_posix())
        scope = resolver.resolve(relative)
        if scope is None:
            unscoped.append(relative)
            continue
        try:
            stat_result = absolute.stat()
        except OSError:
            # Recorded, never skipped: the caller emits FILE_ACCESS_ERROR for these.
            unreadable.append(relative)
            continue
        sources.append(
            SourceRef(
                scope=scope,
                relative_path=relative,
                size_bytes=stat_result.st_size,
                mtime_ns=stat_result.st_mtime_ns,
            )
        )

    return DiscoveryResult(
        sources=tuple(sources),
        unscoped_paths=tuple(unscoped),
        scopes=tuple(scopes),
        unreadable_paths=tuple(unreadable),
    )


def _iter_files(root: Path) -> Iterator[Path]:
    """Depth-first walk yielding regular files only, in a stable order.

    Symlinks are not followed: the corpus contains Windows shortcuts, and following links could
    escape the corpus root or loop.
    """
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.is_dir():
            yield from _iter_files(entry)
        elif entry.is_file():
            yield entry
