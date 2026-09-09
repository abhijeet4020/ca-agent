"""Purpose: the manifest is how SPEC-01 req 8's "active retrieval resolves to the latest
successful version" is actually implemented. Manifests are sealed, numbered, cumulative
snapshots: each run publishes a new ordinal that copies forward prior entries and overrides
only the ones it touched. Resolution reads the highest sealed ordinal, so there is deliberately
no mutable LATEST pointer to update - a pointer would itself be a mutation of prior state and a
torn-read hazard if a run crashed between writing outputs and repointing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ca_agent.core.enums import ProcessingStatus, Route
from ca_agent.storage.atomic import write_new_exclusive
from ca_agent.storage.paths import SilverPaths

_MANIFEST_SCHEMA_VERSION = 1
_ENCODING = "utf-8"


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One published version of one unit of work, as recorded in a scope manifest."""

    content_hexdigest: str
    route: Route
    route_fingerprint: str
    version_id: str
    run_ordinal: int
    status: ProcessingStatus
    source_paths: tuple[str, ...] = ()
    output_paths: tuple[str, ...] = ()

    def key(self) -> tuple[str, Route]:
        """Identity of the work this entry describes, independent of which version it is."""
        return (self.content_hexdigest, self.route)

    def to_json(self) -> dict[str, object]:
        return {
            "content_hexdigest": self.content_hexdigest,
            "route": self.route.value,
            "route_fingerprint": self.route_fingerprint,
            "version_id": self.version_id,
            "run_ordinal": self.run_ordinal,
            "status": self.status.value,
            "source_paths": list(self.source_paths),
            "output_paths": list(self.output_paths),
        }

    @classmethod
    def from_json(cls, payload: dict) -> ManifestEntry:
        return cls(
            content_hexdigest=payload["content_hexdigest"],
            route=Route(payload["route"]),
            route_fingerprint=payload["route_fingerprint"],
            version_id=payload["version_id"],
            run_ordinal=int(payload["run_ordinal"]),
            status=ProcessingStatus(payload["status"]),
            source_paths=tuple(payload.get("source_paths", ())),
            output_paths=tuple(payload.get("output_paths", ())),
        )


@dataclass(frozen=True, slots=True)
class Manifest:
    """An immutable snapshot of everything known about one scope at one run ordinal."""

    scope_id: str
    run_ordinal: int
    run_id: str
    _entries: tuple[ManifestEntry, ...]

    def entries(self) -> tuple[ManifestEntry, ...]:
        """All entries, newest ordinal last. Returns a copy-safe tuple, never internal state."""
        return self._entries

    def history_for(self, content_hexdigest: str, route: Route) -> tuple[ManifestEntry, ...]:
        """Every recorded version for this work, successful or not, oldest first."""
        matches = [entry for entry in self._entries if entry.key() == (content_hexdigest, route)]
        return tuple(sorted(matches, key=lambda entry: entry.run_ordinal))

    def resolve_active(
        self,
        content_hexdigest: str,
        route: Route,
        route_fingerprint: str | None = None,
    ) -> ManifestEntry | None:
        """Latest successful version for this work under the given configuration.

        Partial and failed attempts are skipped: SPEC-01 req 3 and req 8 keep them as history
        but exclude them from active retrieval.
        """
        candidates = [
            entry
            for entry in self.history_for(content_hexdigest, route)
            if entry.status.is_active_candidate()
            and (route_fingerprint is None or entry.route_fingerprint == route_fingerprint)
        ]
        return candidates[-1] if candidates else None

    def latest_outcome(
        self, content_hexdigest: str, route: Route
    ) -> ManifestEntry | None:
        """Most recent attempt of any status, which is what the reuse decision inspects."""
        history = self.history_for(content_hexdigest, route)
        return history[-1] if history else None

    def source_paths_for(self, content_hexdigest: str) -> tuple[str, ...]:
        """Every source path recorded against this content within this scope (req 7)."""
        paths: list[str] = []
        for entry in self._entries:
            if entry.content_hexdigest == content_hexdigest:
                paths.extend(path for path in entry.source_paths if path not in paths)
        return tuple(paths)


def build_manifest(
    *,
    scope_id: str,
    run_ordinal: int,
    run_id: str,
    entries: Iterable[ManifestEntry],
    carried_forward: Iterable[ManifestEntry] = (),
) -> Manifest:
    """Assemble a cumulative manifest from prior entries plus this run's entries.

    Prior entries are preserved verbatim so history is never lost; a rerun that touches one
    file must not drop the records for the other 16,595.
    """
    combined = list(carried_forward) + list(entries)
    ordered = tuple(sorted(combined, key=lambda entry: (entry.content_hexdigest, entry.route.value, entry.run_ordinal)))
    return Manifest(scope_id=scope_id, run_ordinal=run_ordinal, run_id=run_id, _entries=ordered)


def publish_manifest(
    paths: SilverPaths,
    *,
    scope_id: str,
    run_ordinal: int,
    run_id: str,
    entries: Iterable[ManifestEntry],
) -> Path:
    """Carry prior entries forward, add this run's entries, and seal the result.

    This is the only supported way to publish. build_manifest plus seal_manifest can express a
    manifest that silently drops history, so callers are given one operation that cannot.
    """
    manifest = build_manifest(
        scope_id=scope_id,
        run_ordinal=run_ordinal,
        run_id=run_id,
        entries=entries,
        carried_forward=carry_forward_entries(paths, scope_id),
    )
    return seal_manifest(paths, manifest)


def seal_manifest(paths: SilverPaths, manifest: Manifest) -> Path:
    """Write a manifest exclusively and mark it sealed.

    Only a sealed manifest participates in resolution, so a crash part-way through a run leaves
    the previous ordinal active rather than exposing a half-built snapshot.
    """
    body = {
        "schema_version": _MANIFEST_SCHEMA_VERSION,
        "scope_id": manifest.scope_id,
        "run_ordinal": manifest.run_ordinal,
        "run_id": manifest.run_id,
        "entries": [entry.to_json() for entry in manifest.entries()],
    }
    payload = json.dumps(body, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
    document = {
        **body,
        "sealed": True,
        "body_sha256": hashlib.sha256(payload.encode(_ENCODING)).hexdigest(),
    }
    target = paths.scope_manifest_path(manifest.scope_id, manifest.run_ordinal)
    text = json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False)
    write_new_exclusive(target, f"{text}\n".encode(_ENCODING))
    return target


def sealed_manifest_ordinals(paths: SilverPaths, scope_id: str) -> tuple[int, ...]:
    """Ordinals of every sealed manifest for a scope, ascending."""
    directory = paths.scope_manifest_dir(scope_id)
    if not directory.is_dir():
        return ()
    ordinals = [
        document["run_ordinal"]
        for path in sorted(directory.glob("manifest-*.json"))
        if (document := _read_sealed(path)) is not None
    ]
    return tuple(sorted(ordinals))


def load_active_manifest(paths: SilverPaths, scope_id: str) -> Manifest:
    """Load the highest sealed manifest for a scope, or an empty one if none exists."""
    directory = paths.scope_manifest_dir(scope_id)
    latest: dict | None = None
    if directory.is_dir():
        for path in sorted(directory.glob("manifest-*.json"), reverse=True):
            document = _read_sealed(path)
            if document is not None:
                latest = document
                break
    if latest is None:
        return Manifest(scope_id=scope_id, run_ordinal=0, run_id="", _entries=())
    return Manifest(
        scope_id=latest["scope_id"],
        run_ordinal=int(latest["run_ordinal"]),
        run_id=latest.get("run_id", ""),
        _entries=tuple(ManifestEntry.from_json(item) for item in latest["entries"]),
    )


def carry_forward_entries(paths: SilverPaths, scope_id: str) -> Sequence[ManifestEntry]:
    """Prior entries a new manifest must preserve."""
    return load_active_manifest(paths, scope_id).entries()


def _read_sealed(path: Path) -> dict | None:
    """Return the document only if it parses and is sealed; otherwise None.

    An unsealed or unparseable file is a torn write from a crashed run. It is skipped rather
    than repaired or deleted, because SPEC-01 req 8 prohibits mutating prior artifacts.
    """
    try:
        document = json.loads(path.read_text(encoding=_ENCODING))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(document, dict) or document.get("sealed") is not True:
        return None
    if "entries" not in document or "run_ordinal" not in document:
        return None
    return document
