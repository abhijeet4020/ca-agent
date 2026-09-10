"""Purpose: runs the Silver layer end to end - discover, hash, deduplicate, decide, execute,
record, publish. The ordering here is the whole immutability contract in practice: the run
ordinal is allocated once so every output directory is unique by construction, `record.json`
is written last so a version directory without one is an abandoned attempt rather than a
partial result, and manifests are sealed only at the end so a crash leaves the previous run
active. Archive members re-enter the same loop, so a file inside a zip is processed exactly
like a loose one and gets its own record.
"""

from __future__ import annotations

import logging
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ca_agent.catalog.bundles import find_application_bundles, is_inside_bundle
from ca_agent.catalog.dedup import DedupOutcome, ScopeDedupIndex
from ca_agent.catalog.discovery import discover_source_files
from ca_agent.catalog.hashing import compute_content_hash
from ca_agent.config.credentials import CredentialStore, load_client_credentials
from ca_agent.config.fingerprint import section_fingerprint
from ca_agent.config.settings import PipelineSettings
from ca_agent.core.enums import ROUTE_CONFIG_SECTIONS, ErrorCategory, ProcessingStatus, Route
from ca_agent.core.model import (
    ArchiveRef,
    ContentHash,
    ErrorInfo,
    ProcessingRecord,
    SourceRef,
    WorkKey,
)
from ca_agent.docgen.model import FormatDocument, SourceIdentity
from ca_agent.docgen.renderer import render_format_document
from ca_agent.pipeline.executor import execute
from ca_agent.pipeline.routing import select_route
from ca_agent.readers.detection import detect_format
from ca_agent.storage.atomic import atomic_write_json, atomic_write_text
from ca_agent.storage.paths import SilverPaths
from ca_agent.versioning.allocator import allocate_run_ordinal, version_id_for
from ca_agent.versioning.manifest import ManifestEntry, load_active_manifest, publish_manifest
from ca_agent.versioning.reuse import RetryPolicy, WorkDecision, decide

_LOG = logging.getLogger("ca_agent.run")
#: The one corpus category whose directory is itself the client scope.
CATEGORY_IS_SCOPE = frozenset({"Mauli Hospital Tally Back up"})


@dataclass(frozen=True, slots=True)
class RunOptions:
    """Operator controls for one run."""

    category: str | None = None
    limit: int | None = None
    dry_run: bool = False
    policy: RetryPolicy = field(default_factory=RetryPolicy)
    progress_every: int = 100


@dataclass(slots=True)
class RunSummary:
    """What a run did, in the terms the end-of-run report needs."""

    run_id: str
    run_ordinal: int
    version_id: str
    discovered: int = 0
    excluded_bundle: int = 0
    processed: int = 0
    reused: int = 0
    duplicates: int = 0
    archive_members: int = 0
    chunks: int = 0
    pages_needing_vision: int = 0
    unscoped: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    by_error: dict[str, int] = field(default_factory=dict)
    failures: list[tuple[str, str, str]] = field(default_factory=list)

    def record(self, relpath: str, status: ProcessingStatus, error: ErrorInfo | None) -> None:
        self.by_status[status.value] = self.by_status.get(status.value, 0) + 1
        if error is not None:
            self.by_error[error.category.value] = self.by_error.get(error.category.value, 0) + 1
            if status in {ProcessingStatus.FAILED, ProcessingStatus.LOCKED}:
                self.failures.append((relpath, error.category.value, error.message))


@dataclass(slots=True)
class _Pending:
    """One unit of work waiting to run: a loose file or an extracted archive member.

    ``members`` is filled in after execution when the work turned out to be an archive; the
    executor returns them and the run loop queues them, so neither has to know about the other.
    """

    path: Path
    source: SourceRef
    members: tuple = ()


def run_pipeline(settings: PipelineSettings, options: RunOptions | None = None) -> RunSummary:
    """Process the corpus into the Silver layer and publish this run's manifests."""
    options = options or RunOptions()
    paths = SilverPaths(settings.paths.output_root)
    raw_root = settings.paths.raw_root

    run_ordinal = allocate_run_ordinal(paths)
    version_id = version_id_for(run_ordinal)
    run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{version_id}"
    summary = RunSummary(run_id=run_id, run_ordinal=run_ordinal, version_id=version_id)
    _LOG.info("run %s (ordinal %d) writing version %s", run_id, run_ordinal, version_id)

    credentials = _credentials(settings)
    bundles = find_application_bundles(raw_root)
    for bundle in bundles:
        _LOG.info("excluding unpacked application bundle (ADR-014): %s", bundle)

    discovery = discover_source_files(raw_root, CATEGORY_IS_SCOPE)
    summary.unscoped = len(discovery.unscoped_paths)
    queue = deque(_initial_queue(discovery, raw_root, bundles, options, summary))

    dedup: dict[str, ScopeDedupIndex] = {}
    manifests = {scope.scope_id: load_active_manifest(paths, scope.scope_id) for scope in discovery.scopes}
    entries: dict[str, list[ManifestEntry]] = {}
    fingerprints: dict[Route, str] = {}

    while queue:
        pending = queue.popleft()
        outcome = _guarded_process(
            pending,
            paths=paths,
            settings=settings,
            options=options,
            version_id=version_id,
            manifests=manifests,
            dedup=dedup,
            fingerprints=fingerprints,
            credentials=credentials,
            summary=summary,
        )
        if outcome is not None:
            entries.setdefault(pending.source.scope.scope_id, []).append(outcome)
        _enqueue_members(queue, pending, raw_root, summary)

        total = summary.processed + summary.reused + summary.duplicates
        if options.progress_every and total and total % options.progress_every == 0:
            _LOG.info(
                "%d processed, %d reused, %d duplicates, %d chunks, %d queued",
                summary.processed, summary.reused, summary.duplicates, summary.chunks, len(queue),
            )

    if not options.dry_run:
        for scope_id, scope_entries in entries.items():
            publish_manifest(
                paths,
                scope_id=scope_id,
                run_ordinal=run_ordinal,
                run_id=run_id,
                entries=scope_entries,
            )
    return summary


# --- the queue ------------------------------------------------------------------------------


def _initial_queue(discovery, raw_root: Path, bundles, options: RunOptions, summary: RunSummary):
    for source in discovery.sources:
        if options.category and not source.relative_path.as_posix().startswith(options.category):
            continue
        if is_inside_bundle(source.relative_path, bundles):
            summary.excluded_bundle += 1
            continue
        summary.discovered += 1
        if options.limit is not None and summary.discovered > options.limit:
            summary.discovered -= 1
            return
        yield _Pending(raw_root / source.relative_path, source)


def _enqueue_members(queue: deque, pending: _Pending, raw_root: Path, summary: RunSummary) -> None:
    """Archive members become ordinary units of work with their own lineage."""
    for member in pending.members:
        summary.archive_members += 1
        chain = (
            *pending.source.archive_chain,
            ArchiveRef(
                archive_hash=member.archive_hash,
                member_path=member.member_path,
                depth=member.depth,
            ),
        )
        queue.append(
            _Pending(
                member.target_path,
                SourceRef(
                    scope=pending.source.scope,
                    relative_path=pending.source.relative_path,
                    archive_chain=chain,
                    size_bytes=member.content.size_bytes,
                ),
            )
        )


# --- one unit of work ------------------------------------------------------------------------------


def _guarded_process(pending: _Pending, **kwargs) -> ManifestEntry | None:
    """The per-file boundary SPEC-01's exception discipline requires.

    Readers map the exceptions they know about; this catches the ones nobody predicted. It is
    the difference between one bad file costing one record and one bad file costing the whole
    run - which is exactly what happened before it existed, when pdfminer raised a sibling of
    the exception the PDF reader was catching and killed a run half way through 16,596 files.

    Catching Exception here is deliberate and is the one place it is correct: the alternative
    is not "a tidier failure", it is an aborted batch. Nothing is swallowed - the traceback is
    recorded against the file and logged.
    """
    summary: RunSummary = kwargs["summary"]
    try:
        return _process_one(pending, **kwargs)
    except Exception as error:  # noqa: BLE001 - the batch boundary; see the docstring
        relpath = pending.source.relative_path.as_posix()
        _LOG.exception("unexpected failure on %s", relpath)
        summary.processed += 1
        summary.record(
            relpath,
            ProcessingStatus.FAILED,
            ErrorInfo(
                category=ErrorCategory.UNEXPECTED_EXCEPTION,
                message=f"{type(error).__name__}: {error}",
                stage="worker",
                reader=None,
                traceback_text=traceback.format_exc(),
            ),
        )
        return None


def _process_one(
    pending: _Pending,
    *,
    paths: SilverPaths,
    settings: PipelineSettings,
    options: RunOptions,
    version_id: str,
    manifests: dict,
    dedup: dict[str, ScopeDedupIndex],
    fingerprints: dict[Route, str],
    credentials: CredentialStore,
    summary: RunSummary,
) -> ManifestEntry | None:
    scope = pending.source.scope
    relpath = pending.source.relative_path.as_posix()
    started = datetime.now(timezone.utc)

    try:
        content = compute_content_hash(pending.path)
        probe = detect_format(pending.path, settings.detection)
    except OSError as error:
        summary.record(relpath, ProcessingStatus.FAILED, _access_error(error))
        return None

    decision_route = select_route(
        probe,
        unrar_available=bool(settings.external_tools.unrar_path),
        soffice_available=bool(settings.external_tools.soffice_path),
    )
    route = decision_route.route
    fingerprint = fingerprints.setdefault(route, _route_fingerprint(settings, route))

    index = dedup.setdefault(scope.scope_id, ScopeDedupIndex(scope.scope_id))
    if index.register(pending.source, content) is DedupOutcome.DUPLICATE:
        summary.duplicates += 1
        summary.record(relpath, ProcessingStatus.SKIPPED_DUPLICATE, None)
        return None

    prior = manifests[scope.scope_id].latest_outcome(content.hexdigest, route)
    if decide(prior, fingerprint, options.policy) is WorkDecision.REUSE:
        summary.reused += 1
        summary.record(relpath, prior.status, None)
        return None

    if options.dry_run:
        summary.processed += 1
        summary.record(relpath, ProcessingStatus.SUCCESS, None)
        return None

    destination = paths.content_version_dir(scope.scope_id, content.hexdigest, version_id)
    result = execute(
        pending.path,
        route=route,
        family=probe.family,
        source=pending.source,
        content=content,
        destination=destination,
        extraction_root=paths.extracted_dir(scope.scope_id, content.hexdigest, version_id),
        settings=settings,
        route_fingerprint=fingerprint,
        passwords=credentials.passwords_for(scope),
    )
    pending.members = result.extracted_members

    summary.processed += 1
    summary.chunks += result.chunk_count
    summary.pages_needing_vision += len(result.pages_needing_vision)
    summary.record(relpath, result.status, result.error)

    _publish_records(
        paths,
        pending=pending,
        probe=probe,
        route=route,
        result=result,
        content=content,
        version_id=version_id,
        fingerprint=fingerprint,
        started=started,
    )
    return ManifestEntry(
        content_hexdigest=content.hexdigest,
        route=route,
        route_fingerprint=fingerprint,
        version_id=version_id,
        run_ordinal=int(version_id.lstrip("r")),
        status=result.status,
        source_paths=(relpath,),
        output_paths=tuple(output.relative_path.as_posix() for output in result.outputs),
    )


def _publish_records(
    paths: SilverPaths,
    *,
    pending: _Pending,
    probe,
    route: Route,
    result,
    content: ContentHash,
    version_id: str,
    fingerprint: str,
    started: datetime,
) -> None:
    """Write the format document, then `record.json` last.

    Ordering is the durability contract: a version directory without record.json is an
    abandoned attempt, so the record is only written once everything else is on disk.
    """
    scope = pending.source.scope
    archive = pending.source.archive_chain[-1] if pending.source.archive_chain else None
    identity = SourceIdentity(
        source_relpath=pending.source.relative_path.as_posix(),
        category=scope.category,
        client=scope.client,
        scope_id=scope.scope_id,
        extension=probe.declared_extension,
        detected_format=probe.family.value,
        detection_confidence=probe.confidence,
        content_sha256=content.hexdigest,
        size_bytes=content.size_bytes,
        version_id=version_id,
        extension_conflict=probe.extension_conflict,
        archive_id=archive.archive_hash if archive else None,
        member_path=archive.member_path if archive else None,
    )
    document = FormatDocument(
        identity=identity,
        route=route,
        status=result.status,
        reader=result.reader,
        sections=result.sections,
        outputs=result.outputs,
        error=result.error,
        warnings=result.warnings,
        retry_action=result.retry_action,
    )
    source_dir = paths.source_version_dir(scope.scope_id, pending.source.path_slug(), version_id)
    atomic_write_text(
        source_dir / identity.companion_relative_path().name, render_format_document(document)
    )

    record = ProcessingRecord(
        work_key=WorkKey(scope.scope_id, content.hexdigest, route, fingerprint),
        source_refs=(pending.source,),
        content=content,
        status=result.status,
        version_id=version_id,
        started_at=started,
        ended_at=datetime.now(timezone.utc),
        outputs=result.outputs,
        error=result.error,
        warnings=result.warnings,
    )
    atomic_write_json(
        paths.record_path(scope.scope_id, content.hexdigest, version_id), _record_json(record)
    )


def _record_json(record: ProcessingRecord) -> dict:
    return {
        "scope_id": record.work_key.scope_id,
        "content_sha256": record.content.hexdigest,
        "size_bytes": record.content.size_bytes,
        "route": record.work_key.route.value,
        "route_fingerprint": record.work_key.route_fingerprint,
        "version_id": record.version_id,
        "status": record.status.value,
        "started_at": record.started_at.isoformat(),
        "ended_at": record.ended_at.isoformat(),
        "source_paths": list(record.source_paths()),
        "archive_chain": [
            {"archive_hash": ref.archive_hash, "member_path": ref.member_path, "depth": ref.depth}
            for ref in record.source_refs[0].archive_chain
        ],
        "outputs": [
            {"kind": output.kind, "path": output.relative_path.as_posix(), "detail": output.detail}
            for output in record.outputs
        ],
        "error": _error_json(record.error),
        "warnings": [_error_json(warning) for warning in record.warnings],
    }


def _error_json(error: ErrorInfo | None) -> dict | None:
    if error is None:
        return None
    return {
        "category": error.category.value,
        "message": error.message,
        "stage": error.stage,
        "reader": error.reader,
    }


def _route_fingerprint(settings: PipelineSettings, route: Route) -> str:
    """Combine the fingerprints of exactly the sections that govern this route (req 8)."""
    import hashlib

    sections = ROUTE_CONFIG_SECTIONS.get(route, ())
    material = "\x00".join(
        section_fingerprint(name, getattr(settings, name)) for name in sections
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _credentials(settings: PipelineSettings) -> CredentialStore:
    path = settings.credentials.client_credentials_path
    return load_client_credentials(Path(path) if path else None)


def _access_error(error: OSError) -> ErrorInfo:
    return ErrorInfo(
        category=ErrorCategory.FILE_ACCESS_ERROR,
        message=str(error),
        stage="discovery",
        reader=None,
    )


def format_report(summary: RunSummary) -> str:
    """The end-of-run report. Failures must not drown the operator, so detail stays in JSONL."""
    lines = [
        f"run {summary.run_id} (version {summary.version_id})",
        "",
        f"  discovered:            {summary.discovered}",
        f"  excluded (bundle):     {summary.excluded_bundle}",
        f"  processed:             {summary.processed}",
        f"  reused:                {summary.reused}",
        f"  duplicates in scope:   {summary.duplicates}",
        f"  archive members:       {summary.archive_members}",
        f"  unscoped paths:        {summary.unscoped}",
        f"  chunks produced:       {summary.chunks}",
        f"  pages needing vision:  {summary.pages_needing_vision}",
        "",
        "  status:",
    ]
    lines.extend(
        f"    {status:22s} {count}" for status, count in sorted(summary.by_status.items())
    )
    if summary.by_error:
        lines.extend(["", "  error categories:"])
        lines.extend(
            f"    {category:30s} {count}"
            for category, count in sorted(summary.by_error.items(), key=lambda item: -item[1])
        )
    if summary.failures:
        lines.extend(["", f"  first {min(20, len(summary.failures))} failures:"])
        lines.extend(
            f"    [{category}] {relpath}: {message[:100]}"
            for relpath, category, message in summary.failures[:20]
        )
    return "\n".join(lines)
