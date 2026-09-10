"""Purpose: builds the Gold layer from Silver's chunk records - one FAISS index per client
scope, never shared (SPEC-01 req 2). Scope isolation is enforced structurally here: chunks are
gathered per scope directory and a chunk whose recorded scope disagrees with the directory it
was found in is refused rather than indexed, because two clients sharing a name across
categories is a real corpus condition and mixing them would be undetectable at query time.
Each build publishes a new version directory; nothing existing is ever rewritten.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from ca_agent.gold.embedder import Embedder, EmbeddingError
from ca_agent.gold.index import IndexedChunk, IndexMetadata, write_index
from ca_agent.storage.paths import SilverPaths

_LOG = logging.getLogger("ca_agent.gold")
_CHUNK_FILENAME = "chunks.jsonl"
#: Batching keeps peak memory bounded on a scope holding tens of thousands of chunks.
_BATCH_SIZE = 256


class GoldBuildError(Exception):
    """The Gold build could not proceed. Raised rather than publishing a partial index."""


@dataclass(slots=True)
class GoldSummary:
    """What a Gold build produced, in the terms the end-of-run report needs."""

    version_id: str
    scopes_indexed: int = 0
    scopes_empty: int = 0
    chunks_embedded: int = 0
    chunks_skipped_empty: int = 0
    vectors_written: int = 0
    embedding_model: str = ""
    dimension: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)


def gold_root(output_root: Path) -> Path:
    """Gold sits beside Silver under the same output root, never inside it."""
    return output_root / "gold"


def next_gold_version(output_root: Path) -> str:
    """One past the highest gold version already published.

    Mirrors the Silver allocator's rule for the same reason: the version is part of the output
    path, so a new build cannot collide with an existing one by construction.
    """
    highest = 0
    scopes = gold_root(output_root) / "scopes"
    if scopes.is_dir():
        for scope_dir in scopes.iterdir():
            for version_dir in scope_dir.iterdir() if scope_dir.is_dir() else ():
                stem = version_dir.name.removeprefix("g")
                if version_dir.is_dir() and stem.isdigit():
                    highest = max(highest, int(stem))
    return f"g{highest + 1:06d}"


def build_gold(
    *,
    output_root: Path,
    embedder: Embedder,
    version_id: str,
    scope_filter: str | None = None,
    limit: int | None = None,
) -> GoldSummary:
    """Build one FAISS index per client scope from the chunks Silver published."""
    paths = SilverPaths(output_root)
    summary = GoldSummary(version_id=version_id, embedding_model=embedder.model_name)

    scopes_dir = paths.scopes_dir()
    if not scopes_dir.is_dir():
        raise GoldBuildError(
            f"no Silver output at {scopes_dir}; run the pipeline before building Gold"
        )

    scope_dirs = sorted(entry for entry in scopes_dir.iterdir() if entry.is_dir())
    for scope_dir in scope_dirs:
        scope_id = scope_dir.name
        if scope_filter and scope_filter != scope_id:
            continue
        if limit is not None and summary.scopes_indexed >= limit:
            break
        try:
            _build_one_scope(
                scope_dir=scope_dir,
                scope_id=scope_id,
                destination=gold_root(output_root) / "scopes" / scope_id / version_id,
                embedder=embedder,
                version_id=version_id,
                summary=summary,
            )
        except (EmbeddingError, GoldBuildError, OSError) as error:
            _LOG.error("scope %s failed: %s", scope_id, error)
            summary.failures.append((scope_id, str(error)))
    return summary


def _build_one_scope(
    *,
    scope_dir: Path,
    scope_id: str,
    destination: Path,
    embedder: Embedder,
    version_id: str,
    summary: GoldSummary,
) -> None:
    chunks, skipped, silver_versions = _gather_chunks(scope_dir, scope_id)
    summary.chunks_skipped_empty += skipped
    if not chunks:
        summary.scopes_empty += 1
        _LOG.info("scope %s has no embeddable chunks", scope_id)
        return

    vectors: list[list[float]] = []
    for start in range(0, len(chunks), _BATCH_SIZE):
        batch = chunks[start : start + _BATCH_SIZE]
        encoded = embedder.encode([chunk.text for chunk in batch])
        if len(encoded) != len(batch):
            raise GoldBuildError(
                f"embedder returned {len(encoded)} vectors for {len(batch)} chunks; "
                "the vector-to-chunk mapping would be wrong"
            )
        vectors.extend(encoded)

    metadata = IndexMetadata(
        scope_id=scope_id,
        category=chunks[0].category,
        client=chunks[0].client,
        embedding_model=embedder.model_name,
        dimension=embedder.dimension,
        vector_count=len(vectors),
        version_id=version_id,
        silver_version_ids=tuple(sorted(silver_versions)),
        skipped_empty=skipped,
    )
    write_index(destination, chunks=chunks, vectors=vectors, metadata=metadata)

    summary.scopes_indexed += 1
    summary.chunks_embedded += len(chunks)
    summary.vectors_written += len(vectors)
    summary.dimension = metadata.dimension
    _LOG.info("scope %s indexed %d chunks", scope_id, len(chunks))


def _gather_chunks(scope_dir: Path, scope_id: str) -> tuple[list[IndexedChunk], int, set[str]]:
    """Collect every chunk Silver wrote for this scope, in a stable order.

    Ordering is by content hash then sequence rather than by directory iteration, so two
    builds over the same Silver output produce the same rows in the same positions.
    """
    collected: list[IndexedChunk] = []
    skipped = 0
    silver_versions: set[str] = set()

    for chunk_file in sorted(scope_dir.rglob(_CHUNK_FILENAME)):
        # .../content/<hh>/<content_hash>/<version_id>/chunks/chunks.jsonl
        version = chunk_file.parent.parent.name
        silver_versions.add(version)
        for line in chunk_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                chunk = IndexedChunk.from_chunk_record(payload)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
                raise GoldBuildError(f"{chunk_file} holds an unreadable chunk: {error}") from error

            if chunk.client_scope_id != scope_id:
                # Requirement 2 forbids sharing an index across clients. A chunk carrying
                # another scope's id in this directory means something upstream crossed the
                # streams, and indexing it would be invisible at query time.
                raise GoldBuildError(
                    f"{chunk_file} holds a chunk from scope {chunk.client_scope_id}; "
                    f"refusing to index it under {scope_id}"
                )
            if not chunk.text.strip():
                # Requirement 2, explicitly: do not embed empty text.
                skipped += 1
                continue
            collected.append(chunk)

    collected.sort(key=lambda chunk: (chunk.content_sha256, chunk.seq))
    return collected, skipped, silver_versions


def format_gold_report(summary: GoldSummary) -> str:
    """End-of-build report."""
    lines = [
        f"gold build {summary.version_id}",
        "",
        f"  embedding model:      {summary.embedding_model}",
        f"  vector dimension:     {summary.dimension}",
        f"  scopes indexed:       {summary.scopes_indexed}",
        f"  scopes with no text:  {summary.scopes_empty}",
        f"  chunks embedded:      {summary.chunks_embedded}",
        f"  chunks skipped empty: {summary.chunks_skipped_empty}",
        f"  vectors written:      {summary.vectors_written}",
    ]
    if summary.failures:
        lines.extend(["", f"  failures ({len(summary.failures)}):"])
        lines.extend(f"    {scope}: {message[:100]}" for scope, message in summary.failures)
    return "\n".join(lines)
