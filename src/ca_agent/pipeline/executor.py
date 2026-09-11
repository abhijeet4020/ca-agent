"""Purpose: runs one file down its chosen route and returns everything the run needs to record
it - outputs written, observations for the format document, and the terminal status. This is
the only place that knows all the readers, which is why it sits in L4: ARCHITECTURE.md forbids
an L3 reader from knowing its siblings exist. Every route returns a result rather than raising,
because SPEC-01 req 6 requires a record for every discovered file and a batch of 16,596 cannot
be allowed to end on the first damaged one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ca_agent.chunking.records import ChunkingError
from ca_agent.chunking.splitter import chunk_units
from ca_agent.config.settings import PipelineSettings, VisionSettings
from ca_agent.core.enums import ErrorCategory, FormatFamily, PageKind, ProcessingStatus, Route
from ca_agent.core.model import ContentHash, ErrorInfo, OutputRef, SourceRef
from ca_agent.core.text import TextUnit
from ca_agent.docgen.model import UNKNOWN, DocumentSection, DocumentTable
from ca_agent.readers.archives import ExtractedMember, extract_archive
from ca_agent.readers.pdf import read_pdf
from ca_agent.readers.structured import read_structured
from ca_agent.readers.tabular import read_tabular
from ca_agent.readers.text import read_text
from ca_agent.storage.atomic import atomic_write_text
from ca_agent.vision.client import VisionClient
from ca_agent.vision.contract import VisionDocument, VisionPage, render_vision_document
from ca_agent.vision.preprocess import PreprocessError, prepare_image, rasterise_pdf_page

_TEXT_FILENAME = "text/document.txt"
_CHUNKS_FILENAME = "chunks/chunks.jsonl"

_RETRY_LOCKED = (
    "Supply the client's PAN and date of birth in the credential file (ADR-016), or replace "
    "the source with an unlocked copy. The next run retries it and publishes a new version."
)
_RETRY_NO_READER = (
    "No reader exists for this format. Re-export it from the originating application in a "
    "supported format if its contents are needed."
)
_RETRY_VISION_DISABLED = (
    "Enable the vision route and supply CAAGENT__VISION__API_KEY, then rerun; the pages are "
    "already classified so only the scanned ones will be sent."
)

#: Where the combined vision Markdown is published inside a version directory (SPEC-01 req 3).
_VISION_FILENAME = "vision/document.md"
_VISION_STAGE = "vision_extraction"
_VISION_READER = "vision"
_PDF_STAGE = "pdf_classification"


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """What running one file produced, whether or not it worked."""

    status: ProcessingStatus
    reader: str | None = None
    outputs: tuple[OutputRef, ...] = ()
    sections: tuple[DocumentSection, ...] = ()
    error: ErrorInfo | None = None
    warnings: tuple[ErrorInfo, ...] = ()
    retry_action: str | None = None
    #: Archive members to feed back through the router. Empty for every other route.
    extracted_members: tuple[ExtractedMember, ...] = field(default=())
    chunk_count: int = 0
    pages_needing_vision: tuple[int, ...] = ()
    #: Set only when the classifier redirected a provisional route - a PDF that turned out to
    #: need vision. The run records the effective route so reuse keys on the right fingerprint.
    effective_route: Route | None = None


def execute(
    source_path: Path,
    *,
    route: Route,
    family: FormatFamily,
    source: SourceRef,
    content: ContentHash,
    destination: Path,
    extraction_root: Path,
    settings: PipelineSettings,
    route_fingerprint: str,
    passwords: tuple[str, ...] = (),
) -> ExecutionResult:
    """Run one file down ``route``. Never raises for a per-file problem."""
    if route is Route.TABULAR_PARQUET:
        return _tabular(source_path, family, destination, settings)
    if route is Route.TEXT_EXTRACT:
        return _text(source_path, family, source, content, destination, settings, route_fingerprint)
    if route is Route.STRUCTURED:
        return _structured(
            source_path, family, source, content, destination, settings, route_fingerprint
        )
    if route in {Route.PDF_TEXT, Route.PDF_VISION}:
        return _pdf(
            source_path, source, content, destination, settings, route_fingerprint, passwords
        )
    if route is Route.ARCHIVE:
        return _archive(source_path, family, content, extraction_root, settings)
    if route is Route.VISION_IMAGE:
        return _vision_image(source_path, source, settings, destination)
    return _no_extraction(route)


# --- tabular ---------------------------------------------------------------------------


def _tabular(
    source_path: Path, family: FormatFamily, destination: Path, settings: PipelineSettings
) -> ExecutionResult:
    result = read_tabular(
        source_path,
        destination=destination / "parquet",
        settings=settings.tabular,
        family=family,
    )
    if result.failure is not None:
        return ExecutionResult(
            status=ProcessingStatus.FAILED, reader=result.reader, error=result.failure
        )

    outputs = tuple(
        OutputRef("parquet", PurePosixPath("parquet") / table.output_relative_path, table.name)
        for table in result.tables
        if table.output_relative_path is not None
    )
    summary = DocumentTable(
        caption="Worksheets and tables",
        columns=("Name", "Rows", "Columns", "Empty", "Encoding", "Delimiter", "Parquet output"),
        rows=tuple(
            (
                table.name,
                str(table.row_count),
                str(table.column_count),
                "yes" if table.is_empty else "no",
                table.encoding or "",
                repr(table.delimiter) if table.delimiter else "",
                table.output_relative_path.as_posix() if table.output_relative_path else "",
            )
            for table in result.tables
        ),
    )
    columns = DocumentTable(
        caption="Columns",
        columns=("Table", "Column", "Arrow type", "Type inferred", "Nulls", "Why not typed"),
        rows=tuple(
            (
                table.name,
                column.name,
                column.arrow_type,
                "yes" if column.inferred else "no",
                str(column.null_count),
                column.inference_rejected_reason or "",
            )
            for table in result.tables
            for column in table.columns
        ),
    )
    warnings = tuple(warning for table in result.tables for warning in table.warnings)
    return ExecutionResult(
        status=ProcessingStatus.SUCCESS,
        reader=result.reader,
        outputs=outputs,
        sections=(
            DocumentSection("Tabular structure", table=summary),
            DocumentSection("Columns", table=columns),
        ),
        warnings=warnings + result.warnings,
    )


# --- text and structured ----------------------------------------------------------------


def _text(
    source_path: Path,
    family: FormatFamily,
    source: SourceRef,
    content: ContentHash,
    destination: Path,
    settings: PipelineSettings,
    route_fingerprint: str,
) -> ExecutionResult:
    result = read_text(
        source_path,
        settings=settings.text,
        family=family,
        soffice_path=settings.external_tools.soffice_path,
    )
    if result.failure is not None:
        return ExecutionResult(
            status=ProcessingStatus.FAILED, reader=result.reader, error=result.failure
        )

    outputs, chunk_count, chunk_error = _publish_text(
        result.units, source, content, destination, settings, route_fingerprint
    )
    if chunk_error is not None:
        return ExecutionResult(
            status=ProcessingStatus.FAILED, reader=result.reader, error=chunk_error
        )

    units = DocumentTable(
        caption="Extracted units",
        columns=("Unit", "Type", "Characters"),
        rows=tuple(
            (unit.unit_ref, unit.unit_type.value, str(len(unit.text))) for unit in result.units
        ),
    )
    rows = (
        ("Encoding", result.encoding or UNKNOWN),
        ("Tables detected", str(result.tables_detected)),
        ("Tables extracted", str(result.tables_extracted)),
        ("Chunks produced", str(chunk_count)),
    )
    return ExecutionResult(
        status=ProcessingStatus.SUCCESS,
        reader=result.reader,
        outputs=outputs,
        sections=(
            DocumentSection("Text extraction", rows=rows),
            DocumentSection("Units", table=units),
        ),
        warnings=result.warnings,
        chunk_count=chunk_count,
    )


def _structured(
    source_path: Path,
    family: FormatFamily,
    source: SourceRef,
    content: ContentHash,
    destination: Path,
    settings: PipelineSettings,
    route_fingerprint: str,
) -> ExecutionResult:
    result = read_structured(
        source_path,
        destination=destination / "parquet",
        settings=settings.structured,
        tabular_settings=settings.tabular,
        family=family,
    )
    if result.failure is not None:
        return ExecutionResult(
            status=ProcessingStatus.FAILED, reader=result.reader, error=result.failure
        )

    outputs, chunk_count, chunk_error = _publish_text(
        result.units, source, content, destination, settings, route_fingerprint
    )
    if chunk_error is not None:
        return ExecutionResult(
            status=ProcessingStatus.FAILED, reader=result.reader, error=chunk_error
        )
    outputs += tuple(
        OutputRef("parquet", PurePosixPath("parquet") / table.output_relative_path, table.name)
        for table in result.tables
        if table.output_relative_path is not None
    )

    collections = DocumentTable(
        caption="Collections routed to Parquet",
        columns=("Field path", "Rows", "Columns", "Output"),
        rows=tuple(
            (
                table.name,
                str(table.row_count),
                str(table.column_count),
                table.output_relative_path.as_posix() if table.output_relative_path else "",
            )
            for table in result.tables
        ),
    )
    paths = DocumentTable(
        caption="Field-path units",
        columns=("Unit", "Characters"),
        rows=tuple((unit.unit_ref, str(len(unit.text))) for unit in result.units),
    )
    return ExecutionResult(
        status=ProcessingStatus.SUCCESS,
        reader=result.reader,
        outputs=outputs,
        sections=(
            DocumentSection("Structural routing", table=collections),
            DocumentSection("Field paths", table=paths),
        ),
        warnings=result.warnings,
        chunk_count=chunk_count,
    )


# --- pdf -------------------------------------------------------------------------------------


def _pdf(
    source_path: Path,
    source: SourceRef,
    content: ContentHash,
    destination: Path,
    settings: PipelineSettings,
    route_fingerprint: str,
    passwords: tuple[str, ...],
) -> ExecutionResult:
    result = read_pdf(source_path, settings=settings.pdf, passwords=passwords)

    pages = DocumentTable(
        caption="Pages",
        columns=("Page", "Classification", "Characters", "Image coverage", "Note"),
        rows=tuple(
            (
                str(page.number),
                page.kind.value,
                str(page.character_count),
                f"{page.image_coverage:.0%}",
                page.failure.message if page.failure else "",
            )
            for page in result.pages
        ),
    )
    encryption = DocumentSection(
        "Encryption",
        rows=(
            ("Encrypted", "yes" if result.encrypted else "no"),
            ("Owner restrictions only", "yes" if result.owner_password_only else "no"),
        ),
    )

    if result.status is ProcessingStatus.LOCKED:
        return ExecutionResult(
            status=ProcessingStatus.LOCKED,
            reader=result.reader,
            error=result.failure,
            sections=(encryption,),
            retry_action=_RETRY_LOCKED,
        )
    if result.status is ProcessingStatus.FAILED:
        return ExecutionResult(
            status=ProcessingStatus.FAILED, reader=result.reader, error=result.failure
        )

    needing_vision = result.pages_needing_vision()
    if needing_vision:
        # The classifier has redirected this file: SPEC-01 req 3 makes one scanned or mixed
        # page turn the whole document into a vision document, so it takes the combined
        # Markdown route rather than the text route.
        return _pdf_vision(
            source_path,
            source,
            result,
            pages,
            encryption,
            needing_vision,
            destination,
            settings,
        )

    outputs, chunk_count, chunk_error = _publish_text(
        result.units, source, content, destination, settings, route_fingerprint
    )
    if chunk_error is not None:
        return ExecutionResult(
            status=ProcessingStatus.FAILED, reader=result.reader, error=chunk_error
        )

    error = None
    if result.status is ProcessingStatus.PARTIAL:
        error = ErrorInfo(
            category=ErrorCategory.EXTRACTION_ERROR,
            message=f"{sum(1 for page in result.pages if page.failure)} page(s) could not be read",
            stage=_PDF_STAGE,
            reader=result.reader,
        )
    return ExecutionResult(
        status=result.status,
        reader=result.reader,
        outputs=outputs,
        sections=(
            _pdf_counts(result, needing_vision, chunk_count),
            encryption,
            DocumentSection("Pages", table=pages),
        ),
        error=error,
        warnings=result.warnings,
        chunk_count=chunk_count,
    )


def _pdf_vision(
    source_path: Path,
    source: SourceRef,
    result,
    pages_table: DocumentTable,
    encryption: DocumentSection,
    needing_vision: tuple[int, ...],
    destination: Path,
    settings: PipelineSettings,
) -> ExecutionResult:
    """The paid route for a scanned or mixed PDF (SPEC-01 req 3).

    One combined Markdown is produced in page order: native text for the pages that carried
    it, vision output for the scanned ones, and a mixed page's native layer only when it says
    something the transcription did not. A page that fails is marked and the document becomes
    partial rather than failed, so the pages that worked are preserved. No chunks and no
    Parquet are published - req 3 keeps a scanned PDF's combined output out of retrieval.
    """
    if not settings.vision.enabled:
        return ExecutionResult(
            status=ProcessingStatus.PARTIAL,
            reader=result.reader,
            error=ErrorInfo(
                category=ErrorCategory.API_ERROR,
                message=(
                    f"{len(needing_vision)} page(s) need vision extraction but the vision "
                    "route is disabled"
                ),
                stage=_PDF_STAGE,
                reader=result.reader,
            ),
            retry_action=_RETRY_VISION_DISABLED,
            sections=(
                _pdf_counts(result, needing_vision, 0),
                encryption,
                DocumentSection("Pages", table=pages_table),
            ),
            pages_needing_vision=needing_vision,
            effective_route=Route.PDF_VISION,
        )

    vision_pages: list[VisionPage] = []
    failures: list[ErrorInfo] = []
    width = height = 0
    for page in result.pages:
        if not page.kind.needs_vision():
            vision_pages.append(VisionPage(number=page.number, native_text=page.text))
            continue

        try:
            raster = rasterise_pdf_page(
                source_path, page_number=page.number, dpi=settings.pdf.raster_dpi
            )
            # The raster DPI is chosen for legibility, but a full-page scan rendered at it can
            # run to several megabytes - large enough that a local server rejects the request
            # outright with HTTP 502. Bounding the longest edge here puts a page under the same
            # limit the image route already applies to its input.
            prepared = prepare_image(
                raster.content, max_edge_pixels=settings.vision.max_image_edge_pixels
            )
        except PreprocessError as error:
            failures.append(_page_failure(page.number, str(error)))
            vision_pages.append(VisionPage(number=page.number, failure=str(error)))
            continue
        if not width:
            width, height = prepared.width, prepared.height

        outcome = _extract(prepared.content, prepared.media_type, settings)
        if not outcome.ok:
            failures.append(outcome.error)
            vision_pages.append(
                VisionPage(number=page.number, failure=outcome.error.message)
            )
            continue
        vision_pages.append(_extracted_page(page, outcome.extraction, settings))

    status = ProcessingStatus.PARTIAL if failures else ProcessingStatus.SUCCESS
    document = VisionDocument(
        display_name=source_path.name,
        source_path=source.relative_path.as_posix(),
        image_format="PDF",
        width=width,
        height=height,
        page_count=result.page_count,
        model=settings.vision.model,
        status=status.value,
        pages=tuple(vision_pages),
        limitations=tuple(error.message for error in failures),
    )
    output = _write_vision_markdown(document, destination)
    return ExecutionResult(
        status=status,
        reader=result.reader,
        outputs=(output,),
        sections=(
            _pdf_counts(result, needing_vision, 0),
            encryption,
            DocumentSection("Pages", table=pages_table),
            _vision_section(document),
        ),
        error=_partial_error(failures),
        warnings=result.warnings,
        pages_needing_vision=needing_vision,
        effective_route=Route.PDF_VISION,
    )


def _extracted_page(page, extraction, settings: PipelineSettings) -> VisionPage:
    """A scanned or mixed page's vision output, plus its native layer when it is not duplicate.

    A MIXED page's native text is appended only when it differs from the transcription
    (token Jaccard below the configured threshold); otherwise it would repeat what the model
    already read, which req 3 requires the combined document to avoid.
    """
    native = page.text if page.kind is PageKind.MIXED else ""
    if native and _token_jaccard(native, extraction.visible_text) < (
        settings.pdf.native_text_duplicate_jaccard
    ):
        return VisionPage(number=page.number, extraction=extraction, native_text=native)
    return VisionPage(number=page.number, extraction=extraction)


def _page_failure(page_number: int, message: str) -> ErrorInfo:
    return ErrorInfo(
        category=ErrorCategory.EXTRACTION_ERROR,
        message=f"page {page_number} could not be rendered for vision: {message}",
        stage=_VISION_STAGE,
        reader=_VISION_READER,
    )


def _partial_error(failures: list[ErrorInfo]) -> ErrorInfo | None:
    """One explaining error for a partial document, keeping the first page's own category."""
    if not failures:
        return None
    return ErrorInfo(
        category=failures[0].category,
        message=(
            f"{len(failures)} page(s) could not be extracted; first: {failures[0].message}"
        ),
        stage=_VISION_STAGE,
        reader=_VISION_READER,
    )


def _pdf_counts(result, needing_vision: tuple[int, ...], chunk_count: int) -> DocumentSection:
    return DocumentSection(
        "PDF structure",
        rows=(
            ("Page count", str(result.page_count)),
            ("Text pages", str(_count(result, PageKind.TEXT))),
            ("Scanned pages", str(_count(result, PageKind.SCANNED))),
            ("Mixed pages", str(_count(result, PageKind.MIXED))),
            ("Empty pages", str(_count(result, PageKind.EMPTY))),
            ("Pages needing vision", str(len(needing_vision))),
            ("Chunks produced", str(chunk_count)),
        ),
    )


def _token_jaccard(left: str, right: str) -> float:
    """Token overlap between a page's native text and its vision transcription."""
    left_tokens = set(left.split())
    right_tokens = set(right.split())
    union = left_tokens | right_tokens
    if not union:
        return 1.0
    return len(left_tokens & right_tokens) / len(union)


def _count(result, kind: PageKind) -> int:
    return sum(1 for page in result.pages if page.kind is kind)


# --- archives, images and the routes with no extraction ------------------------------------------


def _archive(
    source_path: Path,
    family: FormatFamily,
    content: ContentHash,
    extraction_root: Path,
    settings: PipelineSettings,
) -> ExecutionResult:
    result = extract_archive(
        source_path,
        destination=extraction_root,
        settings=settings.archive,
        family=family,
        archive_hash=content.hexdigest,
    )
    members = DocumentTable(
        caption="Members extracted",
        columns=("Member path", "Depth", "SHA-256", "Bytes"),
        rows=tuple(
            (member.member_path, str(member.depth), member.content.hexdigest, str(member.content.size_bytes))
            for member in result.members
        ),
    )
    failures = DocumentTable(
        caption="Members not extracted",
        columns=("Member path", "Depth", "Category", "Message"),
        rows=tuple(
            (failure.member_path, str(failure.depth), failure.category.value, failure.message)
            for failure in result.failures
        ),
    )
    warnings = tuple(
        ErrorInfo(
            category=failure.category,
            message=f"{failure.member_path}: {failure.message}",
            stage="archive_extraction",
            reader="archive",
        )
        for failure in result.failures
    )
    # Members are separate units of work with their own records, so an unreadable member makes
    # this archive partial rather than failed - the siblings were still extracted.
    status = ProcessingStatus.PARTIAL if result.failures else ProcessingStatus.SUCCESS
    error = (
        ErrorInfo(
            category=ErrorCategory.EXTRACTION_ERROR,
            message=f"{len(result.failures)} member(s) could not be extracted",
            stage="archive_extraction",
            reader="archive",
        )
        if result.failures
        else None
    )
    return ExecutionResult(
        status=status,
        reader="archive",
        error=error,
        sections=(
            DocumentSection(
                "Archive",
                rows=(
                    ("Members extracted", str(result.member_count())),
                    ("Members failed", str(len(result.failures))),
                    ("Total expanded bytes", str(result.total_expanded_bytes)),
                    ("Limit reached", "yes" if result.limit_reached else "no"),
                ),
            ),
            DocumentSection("Members", table=members),
            DocumentSection("Member failures", table=failures),
        ),
        warnings=warnings,
        extracted_members=result.members,
    )


def _vision_image(
    source_path: Path, source: SourceRef, settings: PipelineSettings, destination: Path
) -> ExecutionResult:
    """Extract one image through the paid vision route (SPEC-01 req 3).

    An image is a one-page document, so it shares the combined-Markdown shape a scanned PDF
    produces. When the route is disabled the file is still recorded as pending rather than
    skipped, because req 6 wants every discovered file accounted for even when no call is made.
    """
    if not settings.vision.enabled:
        return ExecutionResult(
            status=ProcessingStatus.PARTIAL,
            reader=_VISION_READER,
            sections=(
                DocumentSection("Vision", rows=(("Vision route", "disabled; no call was made"),)),
            ),
            error=ErrorInfo(
                category=ErrorCategory.API_ERROR,
                message="image needs vision extraction but the vision route is disabled",
                stage=_VISION_STAGE,
                reader=_VISION_READER,
            ),
            retry_action=_RETRY_VISION_DISABLED,
        )

    try:
        prepared = prepare_image(
            source_path.read_bytes(), max_edge_pixels=settings.vision.max_image_edge_pixels
        )
    except (PreprocessError, OSError) as error:
        return ExecutionResult(
            status=ProcessingStatus.FAILED,
            reader=_VISION_READER,
            error=ErrorInfo(
                category=ErrorCategory.EXTRACTION_ERROR,
                message=f"image could not be prepared for vision: {error}",
                stage=_VISION_STAGE,
                reader=_VISION_READER,
            ),
        )

    outcome = _extract(prepared.content, prepared.media_type, settings)
    if not outcome.ok:
        return ExecutionResult(
            status=ProcessingStatus.FAILED, reader=_VISION_READER, error=outcome.error
        )

    document = VisionDocument(
        display_name=source_path.name,
        source_path=source.relative_path.as_posix(),
        image_format=_format_label(prepared.media_type),
        width=prepared.width,
        height=prepared.height,
        page_count=1,
        model=settings.vision.model,
        status=ProcessingStatus.SUCCESS.value,
        pages=(VisionPage(number=1, extraction=outcome.extraction),),
    )
    return ExecutionResult(
        status=ProcessingStatus.SUCCESS,
        reader=_VISION_READER,
        outputs=(_write_vision_markdown(document, destination),),
        sections=(_vision_section(document),),
    )


# --- the paid vision call -------------------------------------------------------------------------


def _vision_transport(settings: VisionSettings):
    """Build the HTTP transport the vision route posts through.

    ADR-011 keeps the transport injectable so retry and failure paths are exercised with no
    socket at all; the suite replaces this with an httpx.MockTransport.
    """
    import httpx

    return httpx.Client()


def _extract(image: bytes, media_type: str, settings: PipelineSettings):
    """One paid extraction, through a transport that is closed even when the call fails."""
    with _vision_transport(settings.vision) as http_client:
        return VisionClient(http_client, settings=settings.vision).extract(
            image, media_type=media_type
        )


def _write_vision_markdown(document: VisionDocument, destination: Path) -> OutputRef:
    """Publish the combined Markdown and describe it as an output of this version."""
    markdown = render_vision_document(document)
    atomic_write_text(destination / _VISION_FILENAME, markdown)
    return OutputRef("vision", PurePosixPath(_VISION_FILENAME), f"{len(markdown)} characters")


def _vision_section(document: VisionDocument) -> DocumentSection:
    return DocumentSection(
        "Vision",
        rows=(
            ("Model", document.model),
            ("Pages", str(document.page_count)),
            ("Pages extracted", str(sum(1 for page in document.pages if page.extraction))),
            ("Pages failed", str(sum(1 for page in document.pages if page.failure))),
        ),
    )


def _format_label(media_type: str) -> str:
    """'image/png' becomes 'PNG', the human name req 3 records in the document."""
    return media_type.rsplit("/", 1)[-1].upper()


def _no_extraction(route: Route) -> ExecutionResult:
    """NO_READER and NON_DATA still produce a full record; only the category differs."""
    if route is Route.NON_DATA:
        return ExecutionResult(
            status=ProcessingStatus.EXCLUDED_NON_DATA,
            sections=(DocumentSection("Observed structure", notes=(UNKNOWN,)),),
            error=ErrorInfo(
                category=ErrorCategory.NON_DATA_ARTIFACT,
                message="recorded as a non-data artifact; no extraction attempted",
                stage="reader_selection",
            ),
        )
    return ExecutionResult(
        status=ProcessingStatus.NO_READER,
        sections=(DocumentSection("Observed structure", notes=(UNKNOWN,)),),
        error=ErrorInfo(
            category=ErrorCategory.NO_COMPATIBLE_READER,
            message="no compatible reader is available for this format",
            stage="reader_selection",
        ),
        retry_action=_RETRY_NO_READER,
    )


# --- shared text publication ------------------------------------------------------------------------


def _publish_text(
    units: tuple[TextUnit, ...],
    source: SourceRef,
    content: ContentHash,
    destination: Path,
    settings: PipelineSettings,
    route_fingerprint: str,
) -> tuple[tuple[OutputRef, ...], int, ErrorInfo | None]:
    """Write extracted text and its chunk records. Shared by text, structured and PDF."""
    import json

    if not units:
        return (), 0, None

    body = "\n\n".join(unit.text for unit in units if unit.has_content())
    outputs: list[OutputRef] = []
    if body.strip():
        text_path = destination / _TEXT_FILENAME
        atomic_write_text(text_path, body)
        outputs.append(OutputRef("text", PurePosixPath(_TEXT_FILENAME), f"{len(body)} characters"))

    try:
        chunk_set = chunk_units(
            units,
            source=source,
            content=content,
            settings=settings.chunking,
            extraction_config_version=route_fingerprint,
        )
    except ChunkingError as error:
        return tuple(outputs), 0, ErrorInfo(
            category=ErrorCategory.CONVERSION_ERROR,
            message=str(error),
            stage="chunking",
            reader="chunking",
        )

    if chunk_set.chunks:
        payload = "\n".join(
            json.dumps(chunk.to_json_dict(), ensure_ascii=False) for chunk in chunk_set.chunks
        )
        atomic_write_text(destination / _CHUNKS_FILENAME, payload + "\n")
        outputs.append(
            OutputRef("chunks", PurePosixPath(_CHUNKS_FILENAME), f"{len(chunk_set.chunks)} chunks")
        )
    return tuple(outputs), len(chunk_set.chunks), None
