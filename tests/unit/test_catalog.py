"""Tests for discovery, content hashing and client-scoped deduplication (TP-01 Group C).

SPEC-01 requirement 7 is unusual in that its most important property is a negative one:
identical bytes in two different categories must NOT deduplicate against each other. The real
corpus contains near-mirrored client folders across categories, so this is a live hazard rather
than a theoretical one.
"""

import hashlib
from pathlib import PurePosixPath

import pytest

from ca_agent.catalog.dedup import DedupOutcome, ScopeDedupIndex
from ca_agent.catalog.discovery import discover_source_files
from ca_agent.catalog.hashing import HASH_BLOCK_BYTES, compute_content_hash
from ca_agent.core.model import ContentHash, SourceRef
from ca_agent.core.scope import ClientScope

_SCOPE_A = ClientScope("Business Clients", "ACME", PurePosixPath("Business Clients/ACME"))
_SCOPE_B = ClientScope("LLP", "ACME", PurePosixPath("LLP/ACME"))


def _source(scope: ClientScope, relative: str) -> SourceRef:
    return SourceRef(scope=scope, relative_path=PurePosixPath(relative), size_bytes=1)


# --- hashing --------------------------------------------------------------------------------


def test_content_hash_is_streamed_and_matches_hashlib(tmp_path):
    # Arrange - larger than one read block, so the streaming loop actually iterates
    payload = b"ITR-3 " * (HASH_BLOCK_BYTES // 2)
    target = tmp_path / "big.bin"
    target.write_bytes(payload)

    # Act
    result = compute_content_hash(target)

    # Assert
    assert result.hexdigest == hashlib.sha256(payload).hexdigest()
    assert result.size_bytes == len(payload)


def test_empty_file_hashes_to_the_empty_digest(tmp_path):
    # Arrange
    target = tmp_path / "empty.txt"
    target.write_bytes(b"")

    # Act
    result = compute_content_hash(target)

    # Assert
    assert result.size_bytes == 0
    assert result.hexdigest == hashlib.sha256(b"").hexdigest()


def test_hashing_a_missing_file_raises_oserror(tmp_path):
    # Arrange / Act / Assert - fail fast; the caller records FILE_ACCESS_ERROR
    with pytest.raises(OSError):
        compute_content_hash(tmp_path / "absent.pdf")


# --- client-scoped deduplication --------------------------------------------------------


def test_identical_bytes_in_two_categories_do_not_deduplicate():
    # Arrange - SPEC-01 req 7: scopes are independent even when client names match
    digest = ContentHash("d" * 64, 10)
    index_a = ScopeDedupIndex(_SCOPE_A.scope_id)
    index_b = ScopeDedupIndex(_SCOPE_B.scope_id)

    # Act
    first = index_a.register(_source(_SCOPE_A, "Business Clients/ACME/x.csv"), digest)
    second = index_b.register(_source(_SCOPE_B, "LLP/ACME/x.csv"), digest)

    # Assert
    assert first is DedupOutcome.FIRST
    assert second is DedupOutcome.FIRST, "a separate scope must never see a cross-scope duplicate"


def test_duplicate_within_client_shares_output_and_retains_both_source_paths():
    # Arrange
    digest = ContentHash("e" * 64, 10)
    index = ScopeDedupIndex(_SCOPE_A.scope_id)
    first_path = _source(_SCOPE_A, "Business Clients/ACME/AY 24-25/FS.xlsx")
    second_path = _source(_SCOPE_A, "Business Clients/ACME/backup/FS.xlsx")

    # Act
    first = index.register(first_path, digest)
    second = index.register(second_path, digest)

    # Assert
    assert first is DedupOutcome.FIRST
    assert second is DedupOutcome.DUPLICATE
    assert index.source_paths_for(digest.hexdigest) == (
        "Business Clients/ACME/AY 24-25/FS.xlsx",
        "Business Clients/ACME/backup/FS.xlsx",
    )


def test_same_name_different_content_within_client_processed_separately():
    # Arrange - SPEC-01 req 7 is explicit that both must be processed
    index = ScopeDedupIndex(_SCOPE_A.scope_id)

    # Act
    first = index.register(_source(_SCOPE_A, "Business Clients/ACME/a/FS.xlsx"), ContentHash("1" * 64, 5))
    second = index.register(_source(_SCOPE_A, "Business Clients/ACME/b/FS.xlsx"), ContentHash("2" * 64, 5))

    # Assert
    assert first is DedupOutcome.FIRST
    assert second is DedupOutcome.FIRST


def test_registering_the_same_path_twice_does_not_duplicate_lineage():
    # Arrange - a resumed run may re-walk a path already registered
    digest = ContentHash("f" * 64, 10)
    index = ScopeDedupIndex(_SCOPE_A.scope_id)
    source = _source(_SCOPE_A, "Business Clients/ACME/x.pdf")

    # Act
    index.register(source, digest)
    index.register(source, digest)

    # Assert
    assert index.source_paths_for(digest.hexdigest) == ("Business Clients/ACME/x.pdf",)


def test_index_rejects_a_source_from_another_scope():
    # Arrange - a mis-scoped registration would silently break dedup isolation
    index = ScopeDedupIndex(_SCOPE_A.scope_id)

    # Act / Assert
    with pytest.raises(ValueError):
        index.register(_source(_SCOPE_B, "LLP/ACME/x.csv"), ContentHash("9" * 64, 1))


def test_source_paths_for_unknown_digest_is_empty():
    assert ScopeDedupIndex(_SCOPE_A.scope_id).source_paths_for("0" * 64) == ()


def test_seeding_from_a_prior_run_preserves_dedup_across_runs():
    # Arrange - dedup must survive process restarts, not just live in memory
    digest = ContentHash("a" * 64, 10)
    index = ScopeDedupIndex(_SCOPE_A.scope_id)
    index.seed(digest.hexdigest, ("Business Clients/ACME/original.xlsx",))

    # Act
    outcome = index.register(_source(_SCOPE_A, "Business Clients/ACME/copy.xlsx"), digest)

    # Assert
    assert outcome is DedupOutcome.DUPLICATE
    assert index.source_paths_for(digest.hexdigest) == (
        "Business Clients/ACME/original.xlsx",
        "Business Clients/ACME/copy.xlsx",
    )


# --- discovery -----------------------------------------------------------------------------


def test_discovery_finds_every_file_including_hidden_and_extensionless(tmp_path):
    # Arrange - SPEC-01 req 6: no file may be excluded from processing
    client = tmp_path / "Business Clients" / "ACME"
    client.mkdir(parents=True)
    (client / "report.xlsx").write_bytes(b"x")
    (client / "noextension").write_bytes(b"y")
    (client / ".hidden").write_bytes(b"z")
    (client / "Thumbs.db").write_bytes(b"t")

    # Act
    found = discover_source_files(tmp_path, category_is_scope=frozenset())

    # Assert
    names = {item.relative_path.name for item in found.sources}
    assert names == {"report.xlsx", "noextension", ".hidden", "Thumbs.db"}


def test_discovery_assigns_the_correct_scope_to_each_file(tmp_path):
    # Arrange
    (tmp_path / "Business Clients" / "ACME").mkdir(parents=True)
    (tmp_path / "LLP" / "ACME").mkdir(parents=True)
    (tmp_path / "Business Clients" / "ACME" / "a.pdf").write_bytes(b"a")
    (tmp_path / "LLP" / "ACME" / "b.pdf").write_bytes(b"b")

    # Act
    found = discover_source_files(tmp_path, frozenset())
    by_name = {item.relative_path.name: item for item in found.sources}

    # Assert
    assert by_name["a.pdf"].scope.category == "Business Clients"
    assert by_name["b.pdf"].scope.category == "LLP"
    assert by_name["a.pdf"].scope.scope_id != by_name["b.pdf"].scope.scope_id


def test_discovery_handles_a_category_that_is_its_own_scope(tmp_path):
    # Arrange - the Mauli Hospital backup shape
    backup = tmp_path / "Mauli Hospital Tally Back up" / "Data" / "10001"
    backup.mkdir(parents=True)
    (backup / "Company.900").write_bytes(b"tally")

    # Act
    found = discover_source_files(tmp_path, frozenset({"Mauli Hospital Tally Back up"}))

    # Assert
    assert len(found.sources) == 1
    assert found.sources[0].scope.category_is_scope is True


def test_discovery_reports_files_that_belong_to_no_scope(tmp_path):
    # Arrange - a stray file directly under the corpus root has no client scope
    (tmp_path / "Business Clients" / "ACME").mkdir(parents=True)
    (tmp_path / "stray.pdf").write_bytes(b"s")

    # Act
    found = discover_source_files(tmp_path, frozenset())

    # Assert - the stray file is reported, never silently dropped (SPEC-01 req 6)
    assert found.sources == ()
    assert [path.name for path in found.unscoped_paths] == ["stray.pdf"]
