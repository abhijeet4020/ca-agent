"""Purpose: the operator-facing entry point. SPEC-01 Phase 1 processes ~16,600 files, so the
CLI exists to make a run inspectable before it is expensive: `discover` walks and hashes the
corpus without converting anything, and `config hash` prints the per-section fingerprints that
govern reuse. Argument parsing and reporting live here; all decisions live in lower layers.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from pathlib import Path

from ca_agent.catalog.dedup import DedupOutcome, ScopeDedupIndex
from ca_agent.catalog.discovery import DiscoveryResult, discover_source_files
from ca_agent.catalog.hashing import compute_content_hash
from ca_agent.config.fingerprint import section_fingerprint
from ca_agent.config.settings import ConfigError, PipelineSettings, load_settings
from ca_agent.pipeline.routing import select_route
from ca_agent.readers.detection import detect_format

_LOG = logging.getLogger("ca_agent")
_FINGERPRINTED_SECTIONS = (
    "detection",
    "tabular",
    "text",
    "chunking",
    "pdf",
    "vision",
    "archive",
    "structured",
    "embedding",
)
#: The one category in the corpus whose directory is itself the client scope.
_CATEGORY_IS_SCOPE = frozenset({"Mauli Hospital Tally Back up"})

_EXIT_OK = 0
_EXIT_CONFIG_ERROR = 2
_EXIT_RUNTIME_ERROR = 1


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, dispatch, and translate configuration failures into an exit code."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stderr,
    )
    try:
        settings = _load(args)
    except ConfigError as error:
        _LOG.error("configuration error: %s", error)
        return _EXIT_CONFIG_ERROR
    return args.handler(args, settings)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ca-agent", description=__doc__)
    parser.add_argument("--config", type=Path, default=None, help="path to pipeline.toml")
    parser.add_argument("--verbose", action="store_true", help="emit debug logging")
    subcommands = parser.add_subparsers(dest="command", required=True)

    discover = subcommands.add_parser(
        "discover", help="walk and hash the corpus without converting anything"
    )
    discover.add_argument("--category", default=None, help="limit to one top-level category")
    discover.add_argument("--limit", type=int, default=None, help="stop after N files")
    discover.add_argument("--no-hash", action="store_true", help="skip hashing (walk only)")
    discover.add_argument(
        "--classify", action="store_true", help="detect format and route for every file"
    )
    discover.set_defaults(handler=_run_discover)

    run = subcommands.add_parser(
        "run", help="process the corpus into the Silver layer and publish a new version"
    )
    run.add_argument("--category", default=None, help="limit to one top-level category")
    run.add_argument("--limit", type=int, default=None, help="stop after N discovered files")
    run.add_argument(
        "--dry-run", action="store_true", help="decide everything, write nothing"
    )
    run.add_argument(
        "--force", action="store_true", help="reprocess even unchanged content (req 8)"
    )
    run.add_argument(
        "--no-retry-locked", action="store_true", help="leave previously locked files alone"
    )
    run.set_defaults(handler=_run_pipeline)

    gold = subcommands.add_parser(
        "gold",
        help="build per-client FAISS indexes from the chunks the pipeline published",
        description=(
            "Embeds Silver's chunk records and writes one FAISS index per client scope "
            "(SPEC-01 requirement 2). Requires the gold extra: uv pip install -e .[gold]. "
            "Run the pipeline first; this reads what it published."
        ),
    )
    gold.add_argument("--scope", default=None, help="build one scope id only")
    gold.add_argument("--limit", type=int, default=None, help="stop after N scopes")
    gold.add_argument(
        "--model", default=None, help="override the configured embedding model"
    )
    gold.set_defaults(handler=_run_gold)

    search = subcommands.add_parser(
        "search",
        help="ask a question of the indexed corpus",
        description=(
            "Embeds your question and searches the client FAISS indexes, printing passages "
            "with the client, document and page they came from. Indexes stay separate per "
            "client, so every result says whose data it is."
        ),
    )
    search.add_argument("query", help="what to look for, in plain language")
    search.add_argument("-k", "--top", type=int, default=5, help="results to show (default 5)")
    search.add_argument("--client", default=None, help="limit to clients matching this text")
    search.add_argument("--category", default=None, help="limit to one category")
    search.add_argument("--scope", default=None, help="limit to one exact scope id")
    search.add_argument("--full", action="store_true", help="print whole passages, not extracts")
    search.set_defaults(handler=_run_search)

    config = subcommands.add_parser("config", help="inspect effective configuration")
    config.add_argument("action", choices=("show", "hash"))
    config.set_defaults(handler=_run_config)

    extract = subcommands.add_parser(
        "vision-extract",
        help="extract structured data from one image or PDF page via the vision API",
        description=(
            "Sends a single image or PDF page to the configured OpenAI-compatible endpoint and "
            "prints the validated structured extraction. This spends money, one call per "
            "invocation, so it takes one file at a time and never walks the corpus. API "
            "details come from .env as CAAGENT__VISION__BASE_URL, __MODEL and __API_KEY."
        ),
    )
    extract.add_argument("path", type=Path, help="image or PDF to extract")
    extract.add_argument(
        "--page", type=int, default=1, help="page number, for a PDF source (default 1)"
    )
    extract.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
        help="markdown is the artifact SPEC-01 stores; json is the raw validated structure",
    )
    extract.add_argument(
        "--output", type=Path, default=None, help="write to this file instead of stdout"
    )
    extract.set_defaults(handler=_run_vision_extract)
    return parser


def _load(args: argparse.Namespace) -> PipelineSettings:
    """Load settings, tolerating a missing vision credential for read-only subcommands.

    `discover` performs no paid calls, so demanding an API key would block the very command an
    operator uses to estimate cost before supplying one. `run` is included because the routes
    that would spend money record their work as pending instead when vision is unavailable,
    so a full extraction run is possible with no credential at all.
    """
    needs_credentials = args.command not in {"discover", "config", "run", "gold", "search"}
    config_path = args.config if args.config and args.config.exists() else None
    try:
        return load_settings(config_path)
    except ConfigError:
        if needs_credentials:
            raise
        return load_settings(config_path, overrides={"vision": {"enabled": False}})


def _run_config(args: argparse.Namespace, settings: PipelineSettings) -> int:
    if args.action == "show":
        print(settings.model_dump_json(indent=2))
        return _EXIT_OK
    for name in _FINGERPRINTED_SECTIONS:
        print(f"{name:<12} {section_fingerprint(name, getattr(settings, name))}")
    return _EXIT_OK


def _run_pipeline(args: argparse.Namespace, settings: PipelineSettings) -> int:
    """Process the corpus and print the end-of-run report."""
    from ca_agent.pipeline.orchestrator import RunOptions, format_report, run_pipeline
    from ca_agent.versioning.reuse import RetryPolicy

    if not settings.paths.raw_root.is_dir():
        _LOG.error("corpus root %s does not exist", settings.paths.raw_root)
        return _EXIT_CONFIG_ERROR

    options = RunOptions(
        category=args.category,
        limit=args.limit,
        dry_run=args.dry_run,
        policy=RetryPolicy(force=args.force, retry_locked=not args.no_retry_locked),
    )
    summary = run_pipeline(settings, options)
    print(format_report(summary))
    return _EXIT_OK


def _run_search(args: argparse.Namespace, settings: PipelineSettings) -> int:
    """Answer a question from the Gold indexes, with citations."""
    from ca_agent.gold.builder import gold_root
    from ca_agent.gold.embedder import EmbeddingError, SentenceTransformerEmbedder
    from ca_agent.gold.search import available_indexes, search

    scopes_dir = gold_root(settings.paths.output_root) / "scopes"
    indexes = available_indexes(scopes_dir)
    if not indexes:
        _LOG.error("no indexes at %s; run `gold` after the pipeline first", scopes_dir)
        return _EXIT_RUNTIME_ERROR

    try:
        embedder = SentenceTransformerEmbedder(settings.embedding.model_name)
        vector = embedder.encode([args.query])[0]
        hits = search(
            scopes_dir,
            vector,
            k=args.top,
            client=args.client,
            category=args.category,
            scope_id=args.scope,
        )
    except (EmbeddingError, ValueError) as error:
        _LOG.error("search failed: %s", error)
        return _EXIT_RUNTIME_ERROR

    if not hits:
        print(f"No passages matched {args.query!r} in {len(indexes)} client index(es).")
        return _EXIT_OK

    print()
    print(f"{len(hits)} result(s) for {args.query!r}, from {len(indexes)} client index(es):")
    print()
    for position, hit in enumerate(hits, start=1):
        passage = hit.chunk.text if args.full else " ".join(hit.chunk.text.split())[:300]
        print(f"{position}. [{hit.score:.3f}] {hit.client}  ({hit.category})")
        print(f"   {hit.chunk.source_relpath}  -  {hit.chunk.unit_type}: {hit.chunk.unit_ref}")
        print(f"   {passage}")
        print()
    return _EXIT_OK


def _run_gold(args: argparse.Namespace, settings: PipelineSettings) -> int:
    """Build the Gold layer from what the pipeline published."""
    from ca_agent.gold.builder import (
        GoldBuildError,
        build_gold,
        format_gold_report,
        next_gold_version,
    )
    from ca_agent.gold.embedder import EmbeddingError, SentenceTransformerEmbedder

    output_root = settings.paths.output_root
    model = args.model or settings.embedding.model_name
    version_id = next_gold_version(output_root)
    _LOG.info("building gold %s with %s", version_id, model)

    try:
        summary = build_gold(
            output_root=output_root,
            embedder=SentenceTransformerEmbedder(model),
            version_id=version_id,
            scope_filter=args.scope,
            limit=args.limit,
        )
    except (GoldBuildError, EmbeddingError) as error:
        _LOG.error("gold build failed: %s", error)
        return _EXIT_RUNTIME_ERROR

    print(format_gold_report(summary))
    return _EXIT_OK if not summary.failures else _EXIT_RUNTIME_ERROR


def _run_vision_extract(args: argparse.Namespace, settings: PipelineSettings) -> int:
    """Extract one file through the vision API and print the validated structure.

    Deliberately one file per invocation. This is the only command that spends money, so the
    unit of work is small enough that an operator can see exactly what a call costs before
    the batch pipeline starts making them by the thousand.
    """
    import httpx

    from ca_agent.vision.client import VisionClient
    from ca_agent.vision.contract import render_markdown
    from ca_agent.vision.preprocess import PreprocessError, prepare_image, rasterise_pdf_page

    if not args.path.is_file():
        _LOG.error("%s is not a file", args.path)
        return _EXIT_CONFIG_ERROR

    try:
        if args.path.suffix.lower() == ".pdf":
            prepared = rasterise_pdf_page(
                args.path, page_number=args.page, dpi=settings.pdf.raster_dpi
            )
        else:
            prepared = prepare_image(
                args.path.read_bytes(), max_edge_pixels=settings.vision.max_image_edge_pixels
            )
    except PreprocessError as error:
        _LOG.error("could not prepare %s: %s", args.path, error)
        return _EXIT_RUNTIME_ERROR
    except OSError as error:
        _LOG.error("could not read %s: %s", args.path, error)
        return _EXIT_RUNTIME_ERROR

    _LOG.info(
        "sending %s (%dx%d) to %s", args.path.name, prepared.width, prepared.height,
        settings.vision.model,
    )
    with httpx.Client() as http_client:
        client = VisionClient(http_client, settings=settings.vision)
        result = client.extract(prepared.content, media_type=prepared.media_type)

    if not result.ok:
        _LOG.error(
            "extraction failed after %d attempt(s): %s - %s",
            result.attempts, result.error.category.value, result.error.message,
        )
        return _EXIT_RUNTIME_ERROR

    rendered = (
        render_markdown(result.extraction)
        if args.format == "markdown"
        else json.dumps(_as_dict(result.extraction), indent=2, ensure_ascii=False)
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        _LOG.info("wrote %s", args.output)
    else:
        print(rendered)
    return _EXIT_OK


def _as_dict(extraction) -> dict:
    """Flatten a validated extraction for JSON output, in the contract's own field order."""
    return {
        "document_type": extraction.document_type,
        "summary": extraction.summary,
        "visible_text": extraction.visible_text,
        "fields": [
            {"label": field.label, "value": field.value, "confidence": field.confidence}
            for field in extraction.fields
        ],
        "tables": [
            {
                "caption": table.caption,
                "columns": list(table.columns),
                "rows": [list(row) for row in table.rows],
            }
            for table in extraction.tables
        ],
        "uncertainties": list(extraction.uncertainties),
    }


def _run_discover(args: argparse.Namespace, settings: PipelineSettings) -> int:
    raw_root = settings.paths.raw_root
    if not raw_root.is_dir():
        _LOG.error("corpus root %s does not exist", raw_root)
        return _EXIT_CONFIG_ERROR

    _LOG.info("walking %s", raw_root)
    result = discover_source_files(raw_root, _CATEGORY_IS_SCOPE)
    sources = _filter(result, args)
    _report_walk(result, sources)

    if args.classify:
        _report_routes(sources, raw_root, settings)
    if args.no_hash:
        return _EXIT_OK
    _report_dedup(sources, raw_root)
    return _EXIT_OK


def _filter(result: DiscoveryResult, args: argparse.Namespace):
    sources = result.sources
    if args.category:
        sources = tuple(item for item in sources if item.scope.category == args.category)
    if args.limit is not None:
        sources = sources[: args.limit]
    return sources


def _report_walk(result: DiscoveryResult, selected) -> None:
    print(f"scopes discovered      : {len(result.scopes)}")
    print(f"files discovered       : {result.file_count()}")
    print(f"files in a client scope: {len(result.sources)}")
    print(f"files selected         : {len(selected)}")
    if result.unscoped_paths:
        print(f"files outside any scope: {len(result.unscoped_paths)} (recorded, not skipped)")
    if result.unreadable_paths:
        print(f"files that failed stat : {len(result.unreadable_paths)}")

    by_category = Counter(item.scope.category for item in result.sources)
    print("\nfiles per category:")
    for category, count in sorted(by_category.items()):
        print(f"  {count:>6}  {category}")

    extensions = Counter((item.relative_path.suffix or "(none)").lower() for item in result.sources)
    print("\ntop extensions:")
    for extension, count in extensions.most_common(15):
        print(f"  {count:>6}  {extension}")


def _report_dedup(sources, raw_root: Path) -> None:
    """Hash every selected file and report client-scoped duplicate savings."""
    indexes: dict[str, ScopeDedupIndex] = {}
    duplicates = 0
    failures = 0
    total_bytes = 0
    unique_bytes = 0

    for source in sources:
        index = indexes.setdefault(source.scope.scope_id, ScopeDedupIndex(source.scope.scope_id))
        try:
            content = compute_content_hash(raw_root / source.relative_path)
        except OSError as error:
            failures += 1
            _LOG.debug("cannot hash %s: %s", source.relative_path, error)
            continue
        total_bytes += content.size_bytes
        if index.register(source, content) is DedupOutcome.DUPLICATE:
            duplicates += 1
        else:
            unique_bytes += content.size_bytes

    unique = sum(len(index.content_digests()) for index in indexes.values())
    print("\ncontent hashing (client-scoped):")
    print(f"  distinct content items : {unique}")
    print(f"  duplicate source paths : {duplicates}")
    print(f"  unreadable files       : {failures}")
    print(f"  bytes walked           : {total_bytes / 1_048_576:,.1f} MiB")
    print(f"  bytes after dedup      : {unique_bytes / 1_048_576:,.1f} MiB")


def _report_routes(sources, raw_root: Path, settings: PipelineSettings) -> None:
    """Detect and route every selected file, proving SPEC-01 req 6 coverage on real data."""
    soffice = bool(settings.external_tools.soffice_path)
    unrar = bool(settings.external_tools.unrar_path)

    families: Counter[str] = Counter()
    routes: Counter[str] = Counter()
    conflicts = 0
    unroutable = 0

    for source in sources:
        try:
            probe = detect_format(raw_root / source.relative_path, settings.detection)
        except OSError as error:
            _LOG.debug("cannot inspect %s: %s", source.relative_path, error)
            families["(unreadable)"] += 1
            routes["(unreadable)"] += 1
            unroutable += 1
            continue
        families[probe.family.value] += 1
        conflicts += int(probe.extension_conflict)
        routes[select_route(probe, unrar_available=unrar, soffice_available=soffice).route.value] += 1

    print("\ndetected format families:")
    for family, count in families.most_common():
        print(f"  {count:>6}  {family}")
    print("\nroutes selected:")
    for route, count in routes.most_common():
        print(f"  {count:>6}  {route}")
    print(f"\n  files whose extension contradicts their content: {conflicts}")
    print(f"  files with no route at all (must be 0): {unroutable}")
