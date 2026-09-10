"""Purpose: builds and reloads one FAISS index per client scope (SPEC-01 req 2). The property
everything else depends on is the vector-to-chunk mapping: FAISS returns row numbers, and a
mapping that drifts by a single row attributes one client's text to another - a failure that
looks like a working system right up until someone acts on the answer. The mapping is therefore
written as an explicit, ordered sidecar rather than inferred from insertion order at read time,
and reloading verifies the two agree before the index is usable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ca_agent.storage.atomic import atomic_write_text

_INDEX_FILENAME = "index.faiss"
_MAPPING_FILENAME = "chunks.jsonl"
_METADATA_FILENAME = "index.json"
#: Bumped when the on-disk layout changes in a way a previous reader could misinterpret.
_LAYOUT_VERSION = 1


class IndexError_(Exception):
    """The index could not be built or reloaded. Never downgraded to an empty result."""


@dataclass(frozen=True, slots=True)
class IndexedChunk:
    """One chunk as stored beside the index, carrying the lineage req 2 requires."""

    chunk_id: str
    client_scope_id: str
    category: str
    client: str
    source_relpath: str
    unit_type: str
    unit_ref: str
    unit_sequence: int
    seq: int
    text: str
    content_sha256: str
    archive_id: str | None = None
    member_path: str | None = None

    @classmethod
    def from_chunk_record(cls, payload: dict) -> IndexedChunk:
        return cls(
            chunk_id=payload["chunk_id"],
            client_scope_id=payload["client_scope_id"],
            category=payload["category"],
            client=payload["client"],
            source_relpath=payload["source_relpath"],
            unit_type=payload["unit_type"],
            unit_ref=payload["unit_ref"],
            unit_sequence=int(payload.get("unit_sequence", 0)),
            seq=int(payload["seq"]),
            text=payload["text"],
            content_sha256=payload["content_sha256"],
            archive_id=payload.get("archive_id"),
            member_path=payload.get("member_path"),
        )

    def to_json_dict(self) -> dict:
        return {
            "chunk_id": self.chunk_id,
            "client_scope_id": self.client_scope_id,
            "category": self.category,
            "client": self.client,
            "source_relpath": self.source_relpath,
            "unit_type": self.unit_type,
            "unit_ref": self.unit_ref,
            "unit_sequence": self.unit_sequence,
            "seq": self.seq,
            "text": self.text,
            "content_sha256": self.content_sha256,
            "archive_id": self.archive_id,
            "member_path": self.member_path,
        }


@dataclass(frozen=True, slots=True)
class IndexMetadata:
    """What req 2 requires stored with every index so it can be reloaded safely."""

    scope_id: str
    category: str
    client: str
    embedding_model: str
    dimension: int
    vector_count: int
    version_id: str
    silver_version_ids: tuple[str, ...] = ()
    skipped_empty: int = 0

    def to_json_dict(self) -> dict:
        return {
            "layout_version": _LAYOUT_VERSION,
            "scope_id": self.scope_id,
            "category": self.category,
            "client": self.client,
            "embedding_model": self.embedding_model,
            "dimension": self.dimension,
            "vector_count": self.vector_count,
            "version_id": self.version_id,
            "silver_version_ids": list(self.silver_version_ids),
            "skipped_empty": self.skipped_empty,
        }

    @classmethod
    def from_json_dict(cls, payload: dict) -> IndexMetadata:
        return cls(
            scope_id=payload["scope_id"],
            category=payload["category"],
            client=payload["client"],
            embedding_model=payload["embedding_model"],
            dimension=int(payload["dimension"]),
            vector_count=int(payload["vector_count"]),
            version_id=payload["version_id"],
            silver_version_ids=tuple(payload.get("silver_version_ids", ())),
            skipped_empty=int(payload.get("skipped_empty", 0)),
        )


@dataclass(frozen=True, slots=True)
class LoadedIndex:
    """A reloaded index and the chunks its rows correspond to, verified to agree."""

    metadata: IndexMetadata
    chunks: tuple[IndexedChunk, ...]
    faiss_index: object

    def chunk_for_row(self, row: int) -> IndexedChunk:
        """Resolve a FAISS row number to the chunk that produced it."""
        if not 0 <= row < len(self.chunks):
            raise IndexError_(f"row {row} is outside the {len(self.chunks)} chunks stored")
        return self.chunks[row]

    def search(self, query_vector: list[float], k: int = 5) -> list[tuple[IndexedChunk, float]]:
        """Return the nearest chunks with their scores, most similar first."""
        import numpy

        if not self.chunks:
            return []
        matrix = numpy.asarray([query_vector], dtype="float32")
        scores, rows = self.faiss_index.search(matrix, min(k, len(self.chunks)))
        return [
            (self.chunk_for_row(int(row)), float(score))
            for row, score in zip(rows[0], scores[0], strict=True)
            if row >= 0
        ]


def write_index(
    destination: Path,
    *,
    chunks: list[IndexedChunk],
    vectors: list[list[float]],
    metadata: IndexMetadata,
) -> Path:
    """Write index, mapping and metadata into one immutable version directory.

    The three are written together and the mapping is ordered to match the vectors exactly,
    because FAISS itself stores no notion of what a row means.
    """
    if len(chunks) != len(vectors):
        raise IndexError_(
            f"{len(vectors)} vectors for {len(chunks)} chunks; the mapping would be wrong"
        )

    import faiss
    import numpy

    destination.mkdir(parents=True, exist_ok=True)
    matrix = numpy.asarray(vectors, dtype="float32") if vectors else numpy.zeros(
        (0, metadata.dimension), dtype="float32"
    )
    if matrix.shape[1] != metadata.dimension:
        raise IndexError_(
            f"vectors are {matrix.shape[1]}-dimensional but metadata declares {metadata.dimension}"
        )

    # Inner product over normalised vectors is cosine similarity, and a flat index is exact:
    # at corpus scale an approximate index would trade correctness for a speedup nobody needs.
    index = faiss.IndexFlatIP(metadata.dimension)
    if len(matrix):
        index.add(matrix)
    faiss.write_index(index, str(destination / _INDEX_FILENAME))

    atomic_write_text(
        destination / _MAPPING_FILENAME,
        "".join(json.dumps(chunk.to_json_dict(), ensure_ascii=False) + "\n" for chunk in chunks),
    )
    atomic_write_text(
        destination / _METADATA_FILENAME,
        json.dumps(metadata.to_json_dict(), indent=2, ensure_ascii=False) + "\n",
    )
    return destination


def load_index(directory: Path) -> LoadedIndex:
    """Reload an index and verify its mapping still matches its vectors."""
    import faiss

    metadata_path = directory / _METADATA_FILENAME
    mapping_path = directory / _MAPPING_FILENAME
    index_path = directory / _INDEX_FILENAME
    for path in (metadata_path, mapping_path, index_path):
        if not path.is_file():
            raise IndexError_(f"{path.name} is missing from {directory}")

    metadata = IndexMetadata.from_json_dict(json.loads(metadata_path.read_text(encoding="utf-8")))
    chunks = tuple(
        IndexedChunk.from_chunk_record(json.loads(line))
        for line in mapping_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    )
    index = faiss.read_index(str(index_path))

    # A mapping that disagrees with the index is worse than no index: it answers confidently
    # with the wrong client's text. Refuse rather than serve it.
    if index.ntotal != len(chunks):
        raise IndexError_(
            f"{directory} holds {index.ntotal} vectors but {len(chunks)} chunks; mapping is broken"
        )
    if index.d != metadata.dimension:
        raise IndexError_(
            f"{directory} index is {index.d}-dimensional but metadata declares {metadata.dimension}"
        )
    return LoadedIndex(metadata=metadata, chunks=chunks, faiss_index=index)
