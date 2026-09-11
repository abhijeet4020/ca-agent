"""Purpose: answers a question against the Gold indexes - the point of the whole pipeline, and
until now the one thing it could not do. Searching spans client indexes rather than merging
them, because requirement 7 keeps each client's vectors in its own index and merging would
destroy that boundary; every hit therefore carries the client it came from, so a result can
never be read as belonging to the wrong one. Results keep full lineage, so an answer can cite
the document and page it came from rather than asking the reader to trust it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ca_agent.gold.index import IndexedChunk, IndexMetadata, load_index


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One result, with enough lineage to cite it."""

    score: float
    chunk: IndexedChunk
    scope_id: str
    category: str
    client: str

    def citation(self) -> str:
        """How a person or an agent would refer to this passage."""
        where = f"{self.chunk.unit_type}:{self.chunk.unit_ref}"
        if self.chunk.member_path:
            return f"{self.client} - {self.chunk.source_relpath} ({self.chunk.member_path}, {where})"
        return f"{self.client} - {self.chunk.source_relpath} ({where})"


def available_indexes(gold_scopes_dir: Path) -> list[tuple[Path, IndexMetadata]]:
    """Every index that can be searched, newest version per scope.

    Only the highest version of each scope is offered: older versions are retained for history
    (req 2 forbids deleting them) but answering from a stale index would be misleading.
    """
    found: list[tuple[Path, IndexMetadata]] = []
    if not gold_scopes_dir.is_dir():
        return found

    for scope_dir in sorted(gold_scopes_dir.iterdir()):
        if not scope_dir.is_dir():
            continue
        versions = sorted(
            (entry for entry in scope_dir.iterdir() if entry.is_dir()),
            key=lambda entry: entry.name,
        )
        for version_dir in reversed(versions):
            try:
                loaded = load_index(version_dir)
            except Exception:  # noqa: BLE001 - a broken index must not hide the working ones
                continue
            found.append((version_dir, loaded.metadata))
            break
    return found


def search(
    gold_scopes_dir: Path,
    query_vector: list[float],
    *,
    k: int = 5,
    client: str | None = None,
    category: str | None = None,
    scope_id: str | None = None,
) -> list[SearchHit]:
    """Search across matching client indexes and return the best hits overall."""
    hits: list[SearchHit] = []

    for version_dir, metadata in available_indexes(gold_scopes_dir):
        if scope_id and metadata.scope_id != scope_id:
            continue
        if client and client.lower() not in metadata.client.lower():
            continue
        if category and category.lower() not in metadata.category.lower():
            continue

        loaded = load_index(version_dir)
        if loaded.metadata.dimension != len(query_vector):
            # A query embedded with a different model cannot be compared to these vectors.
            # Skipping is wrong and answering is worse, so say so.
            raise ValueError(
                f"{metadata.scope_id} was built with {metadata.embedding_model} "
                f"({metadata.dimension} dimensions); the query has {len(query_vector)}"
            )
        for chunk, score in loaded.search(query_vector, k=k):
            hits.append(
                SearchHit(
                    score=score,
                    chunk=chunk,
                    scope_id=metadata.scope_id,
                    category=metadata.category,
                    client=metadata.client,
                )
            )

    hits.sort(key=lambda hit: hit.score, reverse=True)
    return hits[:k]
