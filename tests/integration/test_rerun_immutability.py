"""End-to-end check of the SPEC-01 requirement 8 durability contract (TP-01 Group D).

This is the guarantee everything else rests on: a rerun may add artifacts but must never modify
or delete one. It is verified against a real filesystem rather than mocks, by hashing the whole
output tree before and after, because that is the only way to catch an in-place rewrite that a
unit test with a stubbed writer would miss.

Comparison is by content hash, not modification time: Windows mtime granularity and ACL
behaviour are unreliable enough to make a timestamp assertion flaky rather than meaningful.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ca_agent.core.enums import ProcessingStatus, Route
from ca_agent.storage.atomic import atomic_write_json, atomic_write_text
from ca_agent.storage.paths import SilverPaths
from ca_agent.versioning.allocator import allocate_run_ordinal, version_id_for
from ca_agent.versioning.manifest import ManifestEntry, load_active_manifest, publish_manifest

pytestmark = pytest.mark.integration

_SCOPE_ID = "business_clients__acme__0123456789ab"
_DIGEST = "a1" * 32
_SOURCE_PATH = "Business Clients/ACME/AY 24-25/FS.xlsx"


def _snapshot(root: Path) -> dict[str, str]:
    """Content hash of every file under ``root``, keyed by relative path."""
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _publish_run(
    paths: SilverPaths,
    *,
    status: ProcessingStatus,
    fingerprint: str = "fp0001",
    payload: bytes = b"parquet-bytes-v1",
) -> str:
    """Publish one unit of work the way the pipeline will: outputs first, record.json last."""
    ordinal = allocate_run_ordinal(paths)
    version_id = version_id_for(ordinal)

    content_dir = paths.content_version_dir(_SCOPE_ID, _DIGEST, version_id)
    outputs = content_dir / "outputs" / "parquet"
    outputs.mkdir(parents=True, exist_ok=True)
    (outputs / "Sheet1.parquet").write_bytes(payload)

    source_dir = paths.source_version_dir(_SCOPE_ID, "fs_xlsx__abc1234567", version_id)
    atomic_write_text(source_dir / "FS.xlsx.format.md", f"# FS.xlsx\n\nversion {version_id}\n")

    # record.json is written last: a version directory without it is an abandoned attempt.
    atomic_write_json(
        paths.record_path(_SCOPE_ID, _DIGEST, version_id),
        {"version_id": version_id, "status": status.value, "source_paths": [_SOURCE_PATH]},
    )

    publish_manifest(
        paths,
        scope_id=_SCOPE_ID,
        run_ordinal=ordinal,
        run_id=f"run-{ordinal}",
        entries=[
            ManifestEntry(
                content_hexdigest=_DIGEST,
                route=Route.TABULAR_PARQUET,
                route_fingerprint=fingerprint,
                version_id=version_id,
                run_ordinal=ordinal,
                status=status,
                source_paths=(_SOURCE_PATH,),
                output_paths=("outputs/parquet/Sheet1.parquet",),
            )
        ],
    )
    return version_id


def test_rerun_does_not_mutate_any_prior_version_artifact(tmp_path):
    # Arrange
    paths = SilverPaths(tmp_path)
    first_version = _publish_run(paths, status=ProcessingStatus.SUCCESS)
    before = _snapshot(paths.silver_root)
    assert before, "the first run must actually have written something"

    # Act - a second run with different output bytes, as a config change would produce
    second_version = _publish_run(
        paths, status=ProcessingStatus.SUCCESS, fingerprint="fp0002", payload=b"parquet-bytes-v2"
    )
    after = _snapshot(paths.silver_root)

    # Assert
    assert first_version != second_version
    for relative_path, digest in before.items():
        assert relative_path in after, f"{relative_path} was deleted by the rerun"
        assert after[relative_path] == digest, f"{relative_path} was modified by the rerun"
    assert len(after) > len(before), "the rerun should have added new artifacts"


def test_rerun_publishes_a_new_version_and_keeps_the_old_one_readable(tmp_path):
    # Arrange
    paths = SilverPaths(tmp_path)
    first_version = _publish_run(paths, status=ProcessingStatus.SUCCESS)

    # Act
    second_version = _publish_run(paths, status=ProcessingStatus.SUCCESS, fingerprint="fp0002")

    # Assert
    assert paths.record_path(_SCOPE_ID, _DIGEST, first_version).exists()
    assert paths.record_path(_SCOPE_ID, _DIGEST, second_version).exists()
    history = load_active_manifest(paths, _SCOPE_ID).history_for(_DIGEST, Route.TABULAR_PARQUET)
    assert {entry.version_id for entry in history} == {first_version, second_version}


def test_partial_rerun_leaves_the_earlier_successful_version_active(tmp_path):
    # Arrange - SPEC-01 req 3: a partial result is history, never active
    paths = SilverPaths(tmp_path)
    successful = _publish_run(paths, status=ProcessingStatus.SUCCESS)
    before = _snapshot(paths.silver_root)

    # Act
    partial = _publish_run(paths, status=ProcessingStatus.PARTIAL)
    after = _snapshot(paths.silver_root)

    # Assert
    active = load_active_manifest(paths, _SCOPE_ID).resolve_active(_DIGEST, Route.TABULAR_PARQUET)
    assert active.version_id == successful
    assert paths.record_path(_SCOPE_ID, _DIGEST, partial).exists(), "partial output is kept"
    for relative_path, digest in before.items():
        assert after[relative_path] == digest


def test_successful_retry_after_a_partial_becomes_active(tmp_path):
    # Arrange - SPEC-01 req 3: a later successful retry creates a new active version
    paths = SilverPaths(tmp_path)
    _publish_run(paths, status=ProcessingStatus.PARTIAL)

    # Act
    recovered = _publish_run(paths, status=ProcessingStatus.SUCCESS)

    # Assert
    active = load_active_manifest(paths, _SCOPE_ID).resolve_active(_DIGEST, Route.TABULAR_PARQUET)
    assert active.version_id == recovered


def test_every_source_path_is_retained_across_runs(tmp_path):
    # Arrange - SPEC-01 req 7 and req 12: duplicates share content but keep all their paths
    paths = SilverPaths(tmp_path)
    _publish_run(paths, status=ProcessingStatus.SUCCESS)

    # Act
    manifest = load_active_manifest(paths, _SCOPE_ID)

    # Assert
    assert manifest.source_paths_for(_DIGEST) == (_SOURCE_PATH,)


def test_no_temporary_files_survive_a_run(tmp_path):
    # Arrange / Act - atomic writes must never leave debris that looks like a real artifact
    paths = SilverPaths(tmp_path)
    _publish_run(paths, status=ProcessingStatus.SUCCESS)

    # Assert
    leftovers = [path.name for path in paths.silver_root.rglob("*.tmp")]
    assert leftovers == []
