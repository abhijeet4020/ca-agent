"""Tests for the immutability and versioning contract (TP-01 Group D).

SPEC-01 requirement 8 is the hardest guarantee in the specification: nothing may ever be
modified, overwritten or deleted, reruns publish new versions, and active retrieval resolves
to the latest successful version for the relevant processing configuration. These tests are
written before any reader exists because every later route depends on this being right.
"""

import json
from datetime import datetime, timezone
from pathlib import PurePosixPath

import pytest

from ca_agent.core.enums import ProcessingStatus, Route
from ca_agent.storage.atomic import (
    AtomicWriteError,
    atomic_write_json,
    atomic_write_text,
    write_new_exclusive,
)
from ca_agent.storage.paths import SilverPaths
from ca_agent.versioning.allocator import allocate_run_ordinal, version_id_for
from ca_agent.versioning.manifest import (
    ManifestEntry,
    load_active_manifest,
    publish_manifest,
)
from ca_agent.versioning.reuse import RetryPolicy, WorkDecision, decide

_SCOPE_ID = "business_clients__acme__0123456789ab"
_DIGEST = "b" * 64
_NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _entry(
    version_id: str,
    status: ProcessingStatus,
    *,
    fingerprint: str = "fp0001",
    digest: str = _DIGEST,
) -> ManifestEntry:
    return ManifestEntry(
        content_hexdigest=digest,
        route=Route.TABULAR_PARQUET,
        route_fingerprint=fingerprint,
        version_id=version_id,
        run_ordinal=int(version_id.lstrip("r")),
        status=status,
        source_paths=("Business Clients/ACME/x.xlsx",),
        output_paths=(),
    )


def _publish(paths: SilverPaths, ordinal: int, entries: list[ManifestEntry]) -> None:
    publish_manifest(
        paths,
        scope_id=_SCOPE_ID,
        run_ordinal=ordinal,
        run_id=f"run{ordinal}",
        entries=entries,
    )


# --- atomic write primitives -------------------------------------------------------------


def test_atomic_write_leaves_no_temporary_file_behind(tmp_path):
    # Arrange
    target = tmp_path / "nested" / "record.json"

    # Act
    atomic_write_json(target, {"status": "success"})

    # Assert
    assert json.loads(target.read_text(encoding="utf-8")) == {"status": "success"}
    assert [p.name for p in target.parent.iterdir()] == ["record.json"]


def test_write_new_exclusive_refuses_to_overwrite(tmp_path):
    # Arrange - manifests are sealed exclusively so a rerun can never clobber history
    target = tmp_path / "root-000001.json"
    write_new_exclusive(target, b"first")

    # Act / Assert
    with pytest.raises(AtomicWriteError):
        write_new_exclusive(target, b"second")
    assert target.read_bytes() == b"first"


def test_atomic_write_text_is_utf8_regardless_of_platform_locale(tmp_path):
    # Arrange - client names contain Devanagari; the Windows ANSI codepage would corrupt them
    target = tmp_path / "note.md"

    # Act
    atomic_write_text(target, "पाटील  &  Co")

    # Assert
    assert target.read_text(encoding="utf-8") == "पाटील  &  Co"


# --- run ordinal allocation --------------------------------------------------------------


def test_first_run_allocates_ordinal_one(tmp_path):
    # Arrange
    paths = SilverPaths(tmp_path)

    # Act / Assert
    assert allocate_run_ordinal(paths) == 1
    assert version_id_for(1) == "r000001"


def test_ordinal_increments_past_the_highest_sealed_manifest(tmp_path):
    # Arrange
    paths = SilverPaths(tmp_path)
    _publish(paths, 1, [_entry("r000001", ProcessingStatus.SUCCESS)])
    _publish(paths, 4, [_entry("r000004", ProcessingStatus.SUCCESS)])

    # Act / Assert - ordering is a total integer order needing no clock and no lock
    assert allocate_run_ordinal(paths) == 5


# --- active version resolution -----------------------------------------------------------


def test_active_version_resolves_to_newest_successful_version(tmp_path):
    # Arrange
    paths = SilverPaths(tmp_path)
    _publish(paths, 1, [_entry("r000001", ProcessingStatus.SUCCESS)])
    _publish(paths, 2, [_entry("r000002", ProcessingStatus.SUCCESS)])
    _publish(paths, 3, [_entry("r000003", ProcessingStatus.SUCCESS)])

    # Act
    manifest = load_active_manifest(paths, _SCOPE_ID)
    active = manifest.resolve_active(_DIGEST, Route.TABULAR_PARQUET)

    # Assert
    assert active.version_id == "r000003"


def test_partial_version_retained_in_history_but_absent_from_active_manifest(tmp_path):
    # Arrange - SPEC-01 req 3: a partial PDF stays history until a fully successful retry
    paths = SilverPaths(tmp_path)
    _publish(paths, 1, [_entry("r000001", ProcessingStatus.SUCCESS)])
    _publish(paths, 2, [_entry("r000002", ProcessingStatus.PARTIAL)])

    # Act
    manifest = load_active_manifest(paths, _SCOPE_ID)
    active = manifest.resolve_active(_DIGEST, Route.TABULAR_PARQUET)
    history = manifest.history_for(_DIGEST, Route.TABULAR_PARQUET)

    # Assert
    assert active.version_id == "r000001"
    assert {item.version_id for item in history} == {"r000001", "r000002"}
    assert paths.scope_manifest_path(_SCOPE_ID, 1).exists(), "prior manifest must survive"


def test_failed_retry_does_not_displace_successful_active_version(tmp_path):
    # Arrange
    paths = SilverPaths(tmp_path)
    _publish(paths, 1, [_entry("r000001", ProcessingStatus.SUCCESS)])
    _publish(paths, 2, [_entry("r000002", ProcessingStatus.FAILED)])

    # Act
    active = load_active_manifest(paths, _SCOPE_ID).resolve_active(_DIGEST, Route.TABULAR_PARQUET)

    # Assert
    assert active.version_id == "r000001"


def test_active_resolution_is_keyed_by_route_fingerprint(tmp_path):
    # Arrange - SPEC-01 req 8 scopes reuse to the relevant processing configuration
    paths = SilverPaths(tmp_path)
    _publish(paths, 1, [_entry("r000001", ProcessingStatus.SUCCESS, fingerprint="fp0001")])

    # Act
    manifest = load_active_manifest(paths, _SCOPE_ID)
    matching = manifest.resolve_active(_DIGEST, Route.TABULAR_PARQUET, route_fingerprint="fp0001")
    stale = manifest.resolve_active(_DIGEST, Route.TABULAR_PARQUET, route_fingerprint="fp0002")

    # Assert
    assert matching is not None
    assert stale is None


def test_manifest_is_cumulative_across_runs(tmp_path):
    # Arrange - a later run touching one file must not drop entries for untouched files
    paths = SilverPaths(tmp_path)
    other_digest = "c" * 64
    _publish(
        paths,
        1,
        [_entry("r000001", ProcessingStatus.SUCCESS), _entry("r000001", ProcessingStatus.SUCCESS, digest=other_digest)],
    )
    _publish(paths, 2, [_entry("r000002", ProcessingStatus.SUCCESS)])

    # Act
    manifest = load_active_manifest(paths, _SCOPE_ID)

    # Assert
    assert manifest.resolve_active(other_digest, Route.TABULAR_PARQUET).version_id == "r000001"
    assert manifest.resolve_active(_DIGEST, Route.TABULAR_PARQUET).version_id == "r000002"


def test_unsealed_manifest_is_ignored_by_resolution(tmp_path):
    # Arrange - a crash mid-write must leave the previous manifest active
    paths = SilverPaths(tmp_path)
    _publish(paths, 1, [_entry("r000001", ProcessingStatus.SUCCESS)])
    torn = paths.scope_manifest_path(_SCOPE_ID, 2)
    atomic_write_text(torn, '{"sealed": false, "entries": []}')

    # Act
    active = load_active_manifest(paths, _SCOPE_ID).resolve_active(_DIGEST, Route.TABULAR_PARQUET)

    # Assert
    assert active.version_id == "r000001"


def test_sealing_the_same_ordinal_twice_is_refused(tmp_path):
    # Arrange - proves the never-overwrite rule at the manifest layer
    paths = SilverPaths(tmp_path)
    _publish(paths, 1, [_entry("r000001", ProcessingStatus.SUCCESS)])

    # Act / Assert
    with pytest.raises(AtomicWriteError):
        _publish(paths, 1, [_entry("r000001", ProcessingStatus.SUCCESS)])


def test_abandoned_version_directory_without_record_is_ignored_not_deleted(tmp_path):
    # Arrange - a crashed worker leaves outputs with no record.json
    paths = SilverPaths(tmp_path)
    abandoned = paths.content_version_dir(_SCOPE_ID, _DIGEST, "r000009")
    abandoned.mkdir(parents=True)
    (abandoned / "partial.parquet").write_bytes(b"x")
    _publish(paths, 1, [_entry("r000001", ProcessingStatus.SUCCESS)])

    # Act
    manifest = load_active_manifest(paths, _SCOPE_ID)

    # Assert
    assert manifest.resolve_active(_DIGEST, Route.TABULAR_PARQUET).version_id == "r000001"
    assert abandoned.exists(), "SPEC-01 req 8 prohibits deleting any output artifact"


def test_no_mutable_latest_pointer_exists(tmp_path):
    # Arrange - resolution scans sealed ordinals; a pointer file would be mutable state
    paths = SilverPaths(tmp_path)
    _publish(paths, 1, [_entry("r000001", ProcessingStatus.SUCCESS)])

    # Act
    names = {p.name.upper() for p in paths.scope_manifest_dir(_SCOPE_ID).iterdir()}

    # Assert
    assert not {"LATEST", "LATEST.JSON", "CURRENT", "CURRENT.JSON"} & names


# --- reuse decision ------------------------------------------------------------------------


def test_reuse_when_content_and_fingerprint_are_unchanged():
    assert decide(_entry("r000001", ProcessingStatus.SUCCESS), "fp0001", RetryPolicy()) is WorkDecision.REUSE


def test_process_new_when_there_is_no_prior_outcome():
    assert decide(None, "fp0001", RetryPolicy()) is WorkDecision.PROCESS_NEW


def test_config_change_forces_reprocess():
    prior = _entry("r000001", ProcessingStatus.SUCCESS, fingerprint="fp0001")
    assert decide(prior, "fp0002", RetryPolicy()) is WorkDecision.REPROCESS_CONFIG_CHANGED


def test_partial_result_is_retried():
    # SPEC-01 req 3: a later successful retry creates a new version
    prior = _entry("r000001", ProcessingStatus.PARTIAL)
    assert decide(prior, "fp0001", RetryPolicy()) is WorkDecision.RETRY


@pytest.mark.parametrize(
    "status", [ProcessingStatus.LOCKED, ProcessingStatus.FAILED]
)
def test_recoverable_failures_are_retried(status):
    # SPEC-01 req 5: after a manual unlock, a rerun must pick the file up again
    assert decide(_entry("r000001", status), "fp0001", RetryPolicy()) is WorkDecision.RETRY


def test_no_compatible_reader_is_not_retried_until_configuration_changes():
    # Retrying 1,839 unreadable Tally binaries on every run would waste the whole run budget
    prior = _entry("r000001", ProcessingStatus.NO_READER)
    assert decide(prior, "fp0001", RetryPolicy()) is WorkDecision.REUSE
    assert decide(prior, "fp0002", RetryPolicy()) is WorkDecision.REPROCESS_CONFIG_CHANGED


def test_force_policy_reprocesses_even_a_successful_result():
    prior = _entry("r000001", ProcessingStatus.SUCCESS)
    assert decide(prior, "fp0001", RetryPolicy(force=True)) is WorkDecision.REPROCESS_FORCED


def test_non_data_artifacts_are_not_reprocessed():
    prior = _entry("r000001", ProcessingStatus.EXCLUDED_NON_DATA)
    assert decide(prior, "fp0001", RetryPolicy()) is WorkDecision.REUSE


def test_companion_and_content_paths_are_version_scoped(tmp_path):
    # Arrange - SPEC-01 req 4: companions live in immutable version-specific directories
    paths = SilverPaths(tmp_path)

    # Act
    first = paths.source_version_dir(_SCOPE_ID, "report_xlsx__abc123", "r000001")
    second = paths.source_version_dir(_SCOPE_ID, "report_xlsx__abc123", "r000002")

    # Assert
    assert first != second
    assert PurePosixPath(first.as_posix()).parts[-1] == "r000001"
