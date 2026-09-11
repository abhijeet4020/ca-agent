"""Tests for the Gold layer (TP-01 Group L, SPEC-01 requirement 2).

Two properties carry all the weight. The vector-to-chunk mapping must survive a save and a
reload exactly, because FAISS returns row numbers and a mapping that drifts by one row
attributes one client's text to another - a failure that looks like a working system right up
until someone acts on the answer. And indexes must never be shared across scopes, because two
clients with the same name in different categories is a real condition in this corpus.

The embedder is injected, so nothing here loads a 2 GB model.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ca_agent.gold.builder import GoldBuildError, build_gold, gold_root  # noqa: E402
from ca_agent.gold.index import load_index  # noqa: E402

faiss = pytest.importorskip("faiss", reason="the gold extra is not installed")


class FakeEmbedder:
    """Deterministic bag-of-characters vectors: similar text gives similar vectors."""

    def __init__(self, dimension: int = 16, model_name: str = "fake-embedder-v1") -> None:
        self._dimension = dimension
        self._model_name = model_name
        self.calls = 0

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        vectors = []
        for text in texts:
            counts = [0.0] * self._dimension
            for character in text.lower():
                counts[ord(character) % self._dimension] += 1.0
            norm = sum(value * value for value in counts) ** 0.5 or 1.0
            vectors.append([value / norm for value in counts])
        return vectors


def _chunk(scope_id: str, seq: int, text: str, *, category="GST Company", client="ACME") -> dict:
    return {
        "chunk_id": f"{scope_id}-{seq:04d}",
        "client_scope_id": scope_id,
        "category": category,
        "client": client,
        "source_relpath": f"{category}/{client}/doc.docx",
        "archive_id": None,
        "member_path": None,
        "content_sha256": "a" * 64,
        "unit_type": "heading",
        "unit_ref": f"Section {seq}",
        "unit_sequence": seq,
        "seq": seq,
        "char_start": 0,
        "char_end": len(text),
        "text": text,
        "text_sha256": "b" * 64,
        "extraction_config_version": "cfg0001",
    }


def _write_silver(output_root: Path, scope_id: str, chunks: list[dict], version="r000001") -> None:
    """Lay chunks out exactly where the Silver layer publishes them."""
    target = (
        output_root / "silver" / "scopes" / scope_id / "content" / "aa" / ("a" * 64) / version
        / "chunks" / "chunks.jsonl"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "".join(json.dumps(chunk) + "\n" for chunk in chunks), encoding="utf-8"
    )


# --- scope isolation (requirement 2) ------------------------------------------------------


def test_each_client_scope_gets_its_own_index(tmp_path):
    # Arrange - the real corpus has LIC Employees under two different categories
    output = tmp_path / "out"
    _write_silver(output, "coop__lic__aaa", [_chunk("coop__lic__aaa", 0, "cooperative audit fees")])
    _write_silver(output, "gstp__lic__bbb", [_chunk("gstp__lic__bbb", 0, "proprietor gst return")])

    # Act
    summary = build_gold(output_root=output, embedder=FakeEmbedder(), version_id="g000001")

    # Assert
    assert summary.scopes_indexed == 2
    first = load_index(gold_root(output) / "scopes" / "coop__lic__aaa" / "g000001")
    second = load_index(gold_root(output) / "scopes" / "gstp__lic__bbb" / "g000001")
    assert {chunk.chunk_id for chunk in first.chunks}.isdisjoint(
        chunk.chunk_id for chunk in second.chunks
    )


def test_a_chunk_from_another_scope_is_refused_not_indexed(tmp_path):
    # Arrange - if this ever happens upstream, indexing it would be invisible at query time
    output = tmp_path / "out"
    _write_silver(output, "scope__a__aaa", [_chunk("scope__b__bbb", 0, "text from elsewhere")])

    # Act
    summary = build_gold(output_root=output, embedder=FakeEmbedder(), version_id="g000001")

    # Assert - recorded as a failure rather than quietly mixed in
    assert summary.scopes_indexed == 0
    assert summary.failures
    assert "refusing to index" in summary.failures[0][1]


# --- the mapping (requirement 2) ------------------------------------------------------------


def test_vector_to_chunk_mapping_survives_save_and_load(tmp_path):
    # Arrange - a mapping off by one row attributes text to the wrong document
    output = tmp_path / "out"
    chunks = [_chunk("s__acme__aaa", index, f"ledger entry number {index}") for index in range(12)]
    _write_silver(output, "s__acme__aaa", chunks)

    # Act
    build_gold(output_root=output, embedder=FakeEmbedder(), version_id="g000001")
    loaded = load_index(gold_root(output) / "scopes" / "s__acme__aaa" / "g000001")

    # Assert - every row resolves, and the count matches the vectors FAISS holds
    assert loaded.faiss_index.ntotal == len(chunks)
    assert len(loaded.chunks) == len(chunks)
    for row in range(len(chunks)):
        assert loaded.chunk_for_row(row).chunk_id


def test_index_metadata_records_the_model_and_dimension(tmp_path):
    # Arrange - req 2 lists both; without them a stored index cannot be safely reloaded
    output = tmp_path / "out"
    _write_silver(output, "s__acme__aaa", [_chunk("s__acme__aaa", 0, "depreciation schedule")])

    # Act
    build_gold(output_root=output, embedder=FakeEmbedder(dimension=16), version_id="g000001")
    loaded = load_index(gold_root(output) / "scopes" / "s__acme__aaa" / "g000001")

    # Assert
    assert loaded.metadata.embedding_model == "fake-embedder-v1"
    assert loaded.metadata.dimension == 16
    assert loaded.metadata.category == "GST Company"
    assert loaded.metadata.silver_version_ids == ("r000001",)


def test_lineage_is_preserved_alongside_the_vectors(tmp_path):
    # Arrange - a retrieved chunk has to be traceable back to its document
    output = tmp_path / "out"
    _write_silver(output, "s__acme__aaa", [_chunk("s__acme__aaa", 3, "closing stock at cost")])

    # Act
    build_gold(output_root=output, embedder=FakeEmbedder(), version_id="g000001")
    chunk = load_index(gold_root(output) / "scopes" / "s__acme__aaa" / "g000001").chunks[0]

    # Assert
    assert chunk.source_relpath.endswith("doc.docx")
    assert chunk.unit_ref == "Section 3"
    assert chunk.unit_sequence == 3
    assert chunk.content_sha256 == "a" * 64


# --- what is not embedded --------------------------------------------------------------------


def test_empty_text_is_never_embedded(tmp_path):
    # Arrange - req 2, explicitly
    output = tmp_path / "out"
    _write_silver(
        output,
        "s__acme__aaa",
        [
            _chunk("s__acme__aaa", 0, "real content worth embedding"),
            _chunk("s__acme__aaa", 1, "   \n\t "),
        ],
    )

    # Act
    summary = build_gold(output_root=output, embedder=FakeEmbedder(), version_id="g000001")

    # Assert - skipped and counted, not silently dropped
    assert summary.chunks_embedded == 1
    assert summary.chunks_skipped_empty == 1


def test_a_scope_with_no_chunks_produces_no_index(tmp_path):
    # Arrange - an empty index would be indistinguishable from one never built
    output = tmp_path / "out"
    _write_silver(output, "s__acme__aaa", [_chunk("s__acme__aaa", 0, "  ")])

    # Act
    summary = build_gold(output_root=output, embedder=FakeEmbedder(), version_id="g000001")

    # Assert
    assert summary.scopes_indexed == 0
    assert summary.scopes_empty == 1
    assert not (gold_root(output) / "scopes" / "s__acme__aaa" / "g000001").exists()


# --- immutability (requirement 2) ---------------------------------------------------------------


def test_rebuilding_publishes_a_new_version_and_leaves_the_old_untouched(tmp_path):
    # Arrange - req 2: publish new immutable versions, never modify existing artifacts
    import hashlib

    output = tmp_path / "out"
    _write_silver(output, "s__acme__aaa", [_chunk("s__acme__aaa", 0, "trial balance")])
    build_gold(output_root=output, embedder=FakeEmbedder(), version_id="g000001")
    first_dir = gold_root(output) / "scopes" / "s__acme__aaa" / "g000001"
    before = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(first_dir.iterdir())
    }

    # Act
    build_gold(output_root=output, embedder=FakeEmbedder(), version_id="g000002")

    # Assert
    assert (gold_root(output) / "scopes" / "s__acme__aaa" / "g000002").is_dir()
    after = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(first_dir.iterdir())
    }
    assert after == before


def test_two_builds_of_the_same_input_produce_the_same_row_order(tmp_path):
    # Arrange - stable ordering is what makes an index comparable across builds
    output = tmp_path / "out"
    chunks = [_chunk("s__acme__aaa", index, f"entry {index}") for index in range(6)]
    _write_silver(output, "s__acme__aaa", chunks)

    # Act
    build_gold(output_root=output, embedder=FakeEmbedder(), version_id="g000001")
    build_gold(output_root=output, embedder=FakeEmbedder(), version_id="g000002")
    first = load_index(gold_root(output) / "scopes" / "s__acme__aaa" / "g000001")
    second = load_index(gold_root(output) / "scopes" / "s__acme__aaa" / "g000002")

    # Assert
    assert [c.chunk_id for c in first.chunks] == [c.chunk_id for c in second.chunks]


# --- retrieval -------------------------------------------------------------------------------------


def test_search_returns_the_chunk_whose_text_matches(tmp_path):
    # Arrange - an index that cannot answer a query is not evidence of anything
    output = tmp_path / "out"
    _write_silver(
        output,
        "s__acme__aaa",
        [
            _chunk("s__acme__aaa", 0, "depreciation on plant and machinery"),
            _chunk("s__acme__aaa", 1, "zzzz yyyy xxxx wwww"),
        ],
    )
    embedder = FakeEmbedder()
    build_gold(output_root=output, embedder=embedder, version_id="g000001")
    loaded = load_index(gold_root(output) / "scopes" / "s__acme__aaa" / "g000001")

    # Act
    query = embedder.encode(["depreciation on plant and machinery"])[0]
    results = loaded.search(query, k=2)

    # Assert
    assert results
    assert "depreciation" in results[0][0].text


# --- failure handling ----------------------------------------------------------------------------------


def test_building_without_silver_output_is_reported_clearly(tmp_path):
    # Arrange / Act / Assert
    with pytest.raises(GoldBuildError, match="run the pipeline"):
        build_gold(output_root=tmp_path / "empty", embedder=FakeEmbedder(), version_id="g000001")


def test_a_corrupt_chunk_file_is_reported_not_skipped(tmp_path):
    # Arrange - silently indexing 9 of 10 chunks would be undetectable
    output = tmp_path / "out"
    _write_silver(output, "s__acme__aaa", [_chunk("s__acme__aaa", 0, "fine")])
    target = next((output / "silver").rglob("chunks.jsonl"))
    target.write_text('{"chunk_id": "broken"\n', encoding="utf-8")

    # Act
    summary = build_gold(output_root=output, embedder=FakeEmbedder(), version_id="g000001")

    # Assert
    assert summary.scopes_indexed == 0
    assert summary.failures


def test_an_embedder_returning_the_wrong_count_is_refused(tmp_path):
    # Arrange - a short batch would shift every subsequent row's meaning
    class ShortEmbedder(FakeEmbedder):
        def encode(self, texts):
            return super().encode(texts)[:-1] if len(texts) > 1 else super().encode(texts)

    output = tmp_path / "out"
    _write_silver(
        output, "s__acme__aaa", [_chunk("s__acme__aaa", i, f"text {i}") for i in range(3)]
    )

    # Act
    summary = build_gold(output_root=output, embedder=ShortEmbedder(), version_id="g000001")

    # Assert
    assert summary.scopes_indexed == 0
    assert "mapping would be wrong" in summary.failures[0][1]


# --- search (the app surface) -----------------------------------------------------------------


def _build_two_clients(tmp_path):
    output = tmp_path / "out"
    _write_silver(
        output,
        "biz__acme__aaa",
        [_chunk("biz__acme__aaa", 0, "depreciation on plant and machinery for the year",
                category="Business Clients", client="ACME TRADING")],
    )
    _write_silver(
        output,
        "llp__zeta__bbb",
        [_chunk("llp__zeta__bbb", 0, "partner remuneration and interest on capital",
                category="LLP", client="ZETA LLP")],
    )
    build_gold(output_root=output, embedder=FakeEmbedder(), version_id="g000001")
    return output


def test_search_finds_the_matching_passage_across_clients(tmp_path):
    # Arrange
    from ca_agent.gold.builder import gold_root as _root
    from ca_agent.gold.search import search

    output = _build_two_clients(tmp_path)
    embedder = FakeEmbedder()

    # Act
    hits = search(
        _root(output) / "scopes",
        embedder.encode(["depreciation on plant and machinery for the year"])[0],
        k=3,
    )

    # Assert - the right passage, attributed to the right client
    assert hits
    assert "depreciation" in hits[0].chunk.text
    assert hits[0].client == "ACME TRADING"


def test_search_can_be_limited_to_one_client(tmp_path):
    # Arrange - requirement 7: a client's data must be addressable on its own
    from ca_agent.gold.builder import gold_root as _root
    from ca_agent.gold.search import search

    output = _build_two_clients(tmp_path)

    # Act
    hits = search(
        _root(output) / "scopes", FakeEmbedder().encode(["anything at all"])[0], k=5, client="ZETA"
    )

    # Assert
    assert hits
    assert {hit.client for hit in hits} == {"ZETA LLP"}


def test_every_hit_can_be_cited_back_to_its_document(tmp_path):
    # Arrange - an answer nobody can verify is not an answer
    from ca_agent.gold.builder import gold_root as _root
    from ca_agent.gold.search import search

    output = _build_two_clients(tmp_path)

    # Act
    hits = search(_root(output) / "scopes", FakeEmbedder().encode(["depreciation"])[0], k=1)

    # Assert
    citation = hits[0].citation()
    assert hits[0].client in citation
    assert "doc.docx" in citation


def test_a_query_from_a_different_model_is_refused_not_answered(tmp_path):
    # Arrange - comparing vectors across models produces confident nonsense
    from ca_agent.gold.builder import gold_root as _root
    from ca_agent.gold.search import search

    output = _build_two_clients(tmp_path)

    # Act / Assert
    with pytest.raises(ValueError, match="dimensions"):
        search(_root(output) / "scopes", [0.1] * 99, k=3)
