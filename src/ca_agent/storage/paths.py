"""Purpose: owns the Silver output directory layout so no other module hard-codes a path.
The scheme is what actually enforces SPEC-01 req 8: every version gets its own directory keyed
by run ordinal, so two workers - or two runs - can never target the same path and "never
overwrite" holds by construction rather than by discipline. Content is sharded by the first
byte of its digest to keep directory fan-out manageable across ~16,600 files.
"""

from __future__ import annotations

from pathlib import Path

_SILVER = "silver"
_SHARD_LENGTH = 2


class SilverPaths:
    """Path builder for the Silver layer output tree."""

    def __init__(self, output_root: Path) -> None:
        self._output_root = Path(output_root)

    @property
    def output_root(self) -> Path:
        return self._output_root

    @property
    def silver_root(self) -> Path:
        return self._output_root / _SILVER

    # --- run journals -------------------------------------------------------------------

    def runs_dir(self) -> Path:
        return self.silver_root / "runs"

    def run_dir(self, run_id: str) -> Path:
        return self.runs_dir() / run_id

    def worklist_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "worklist.jsonl"

    def completed_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "completed.jsonl"

    def run_journal_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "run.jsonl"

    # --- per-scope artifacts ------------------------------------------------------------

    def scopes_dir(self) -> Path:
        return self.silver_root / "scopes"

    def scope_dir(self, scope_id: str) -> Path:
        return self.scopes_dir() / scope_id

    def content_version_dir(self, scope_id: str, content_hexdigest: str, version_id: str) -> Path:
        """Directory holding one version of the outputs derived from one content hash."""
        shard = content_hexdigest[:_SHARD_LENGTH]
        return self.scope_dir(scope_id) / "content" / shard / content_hexdigest / version_id

    def record_path(self, scope_id: str, content_hexdigest: str, version_id: str) -> Path:
        """Written last. Its absence marks an abandoned attempt that resolution must ignore."""
        return self.content_version_dir(scope_id, content_hexdigest, version_id) / "record.json"

    def source_version_dir(self, scope_id: str, path_slug: str, version_id: str) -> Path:
        """Per-source-path directory holding the format document and lineage (req 4)."""
        return self.scope_dir(scope_id) / "sources" / path_slug / version_id

    def extracted_dir(self, scope_id: str, archive_hexdigest: str, version_id: str) -> Path:
        """Archive members are expanded here, never back into the source tree (req 7)."""
        return self.scope_dir(scope_id) / "extracted" / archive_hexdigest / version_id

    # --- manifests ----------------------------------------------------------------------

    def scope_manifest_dir(self, scope_id: str) -> Path:
        return self.scope_dir(scope_id) / "manifests"

    def scope_manifest_path(self, scope_id: str, run_ordinal: int) -> Path:
        return self.scope_manifest_dir(scope_id) / f"manifest-{run_ordinal:06d}.json"

    def root_manifest_dir(self) -> Path:
        return self.silver_root / "manifests"

    def root_manifest_path(self, run_ordinal: int) -> Path:
        return self.root_manifest_dir() / f"root-{run_ordinal:06d}.json"
