"""Purpose: implements the deduplication rule in SPEC-01 req 7 - identical content is processed
once per client scope while every source path is retained, and scopes never deduplicate against
each other. One index instance exists per scope and refuses registrations from any other scope,
so cross-scope reuse is impossible by construction rather than by caller discipline. The real
corpus makes this matter: several clients appear in two categories with near-identical files.
"""

from __future__ import annotations

from enum import Enum

from ca_agent.core.model import ContentHash, SourceRef


class DedupOutcome(str, Enum):
    """Whether this registration introduced new content or another path to known content."""

    FIRST = "first"
    DUPLICATE = "duplicate"


class ScopeDedupIndex:
    """Content-hash to source-path index for exactly one client scope."""

    def __init__(self, scope_id: str) -> None:
        self._scope_id = scope_id
        self._paths_by_digest: dict[str, list[str]] = {}

    @property
    def scope_id(self) -> str:
        return self._scope_id

    def seed(self, hexdigest: str, source_paths: tuple[str, ...]) -> None:
        """Preload known content from a prior run's manifest.

        Without seeding, dedup would only hold within a single process and a rerun would
        re-derive outputs it already has.
        """
        known = self._paths_by_digest.setdefault(hexdigest, [])
        known.extend(path for path in source_paths if path not in known)

    def register(self, source: SourceRef, content: ContentHash) -> DedupOutcome:
        """Record a source path against its content and report whether the content is new."""
        if source.scope.scope_id != self._scope_id:
            raise ValueError(
                f"source in scope {source.scope.scope_id} cannot be registered "
                f"against index for scope {self._scope_id}"
            )
        path = source.relative_path.as_posix()
        known = self._paths_by_digest.get(content.hexdigest)
        if known is None:
            self._paths_by_digest[content.hexdigest] = [path]
            return DedupOutcome.FIRST
        if path not in known:
            known.append(path)
        return DedupOutcome.DUPLICATE

    def source_paths_for(self, hexdigest: str) -> tuple[str, ...]:
        """Every retained source path for this content. Returns a copy, never internal state."""
        return tuple(self._paths_by_digest.get(hexdigest, ()))

    def content_digests(self) -> tuple[str, ...]:
        """Distinct content hashes seen in this scope."""
        return tuple(self._paths_by_digest)
