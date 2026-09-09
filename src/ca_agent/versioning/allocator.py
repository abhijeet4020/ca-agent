"""Purpose: allocates the run ordinal that names every version directory a run publishes.
Allocation happens once, single-threaded, at run start, and the resulting version_id is shared
by every worker. That is what makes each worker's output directory unique by construction, so
SPEC-01 req 8's never-overwrite rule holds without locks and without trusting the system clock.
"""

from __future__ import annotations

from ca_agent.storage.paths import SilverPaths

_ORDINAL_WIDTH = 6


def version_id_for(run_ordinal: int) -> str:
    """Zero-padded version identifier, so lexical and numeric ordering agree on disk."""
    if run_ordinal < 1:
        raise ValueError(f"run ordinal starts at 1, got {run_ordinal}")
    return f"r{run_ordinal:0{_ORDINAL_WIDTH}d}"


def allocate_run_ordinal(paths: SilverPaths) -> int:
    """Return one past the highest ordinal any manifest has already claimed.

    Scope manifests and root manifests are both consulted so a partially completed prior run
    can never have its ordinal reused.
    """
    return _highest_claimed_ordinal(paths) + 1


def _highest_claimed_ordinal(paths: SilverPaths) -> int:
    highest = 0
    for directory, pattern, prefix in (
        (paths.root_manifest_dir(), "root-*.json", "root-"),
        (paths.scopes_dir(), "*/manifests/manifest-*.json", "manifest-"),
    ):
        if not directory.is_dir():
            continue
        for path in directory.glob(pattern):
            ordinal = _ordinal_from_name(path.name, prefix)
            if ordinal > highest:
                highest = ordinal
    return highest


def _ordinal_from_name(name: str, prefix: str) -> int:
    """Parse the ordinal out of a manifest filename, ignoring anything unrecognisable."""
    stem = name.removeprefix(prefix).removesuffix(".json")
    return int(stem) if stem.isdigit() else 0
