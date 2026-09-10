"""Purpose: turns extracted text units into chunk records (SPEC-01 req 2). Two properties do
the real work. Character offsets are resolved by locating each chunk back in its own unit, so
a chunk's claim about where it came from is verified rather than assumed. And the chunk id is
derived from scope, content, unit and configuration - never from a counter or a timestamp - so
a rerun over unchanged content reproduces identical ids, which req 8's reuse contract needs.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from ca_agent.chunking.records import ChunkingError, ChunkRecord, ChunkSet
from ca_agent.config.settings import ChunkingSettings
from ca_agent.core.model import ContentHash, SourceRef
from ca_agent.core.text import TextUnit

#: Long enough to make a collision implausible, short enough to keep JSONL readable.
_CHUNK_ID_LENGTH = 32


def chunk_units(
    units: Iterable[TextUnit],
    *,
    source: SourceRef,
    content: ContentHash,
    settings: ChunkingSettings,
    extraction_config_version: str,
) -> ChunkSet:
    """Split text units into chunk records carrying full lineage."""
    splitter = _splitter(settings)
    records: list[ChunkRecord] = []
    skipped_empty = 0
    skipped_short = 0

    for unit in units:
        if not unit.has_content():
            skipped_empty += 1
            continue
        for text, start in _located_pieces(splitter.split_text(unit.text), unit):
            if len(text) < settings.min_chunk_characters:
                skipped_short += 1
                continue
            records.append(
                _record(
                    text=text,
                    start=start,
                    unit=unit,
                    seq=len(records),
                    source=source,
                    content=content,
                    extraction_config_version=extraction_config_version,
                )
            )

    return ChunkSet(
        chunks=tuple(records),
        skipped_empty_units=skipped_empty,
        skipped_short_chunks=skipped_short,
    )


def _splitter(settings: ChunkingSettings):
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    return RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
    )


def _located_pieces(pieces: list[str], unit: TextUnit) -> list[tuple[str, int]]:
    """Pair each chunk with its start offset in the unit.

    The splitter only ever strips whitespace from the ends of a piece, so every piece is still
    a contiguous substring of the unit and can be found. The cursor advances by one rather than
    by the piece length because consecutive chunks overlap by design.
    """
    located: list[tuple[str, int]] = []
    cursor = 0
    for piece in pieces:
        if not piece.strip():
            continue
        start = unit.text.find(piece, cursor)
        if start < 0:
            start = unit.text.find(piece)
        if start < 0:
            raise ChunkingError(
                f"chunk of unit {unit.unit_ref!r} could not be located in its own text; "
                "recording an offset here would be a false lineage claim"
            )
        located.append((piece, start))
        cursor = start + 1
    return located


def _record(
    *,
    text: str,
    start: int,
    unit: TextUnit,
    seq: int,
    source: SourceRef,
    content: ContentHash,
    extraction_config_version: str,
) -> ChunkRecord:
    scope = source.scope
    archive = source.archive_chain[-1] if source.archive_chain else None
    return ChunkRecord(
        chunk_id=_chunk_id(
            scope_id=scope.scope_id,
            content_hexdigest=content.hexdigest,
            extraction_config_version=extraction_config_version,
            unit_ref=unit.unit_ref,
            seq=seq,
        ),
        client_scope_id=scope.scope_id,
        category=scope.category,
        client=scope.client,
        source_relpath=source.relative_path.as_posix(),
        content_sha256=content.hexdigest,
        unit_type=unit.unit_type,
        unit_ref=unit.unit_ref,
        seq=seq,
        char_start=start,
        char_end=start + len(text),
        text=text,
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        extraction_config_version=extraction_config_version,
        archive_id=archive.archive_hash if archive else None,
        member_path=archive.member_path if archive else None,
    )


def _chunk_id(
    *,
    scope_id: str,
    content_hexdigest: str,
    extraction_config_version: str,
    unit_ref: str,
    seq: int,
) -> str:
    """Derive a stable id.

    The source path is deliberately excluded: req 7 has duplicate files within a scope share
    one set of outputs, so two paths holding identical bytes must produce identical chunk ids.
    The scope is included for the opposite reason - identical bytes in two scopes must not.
    """
    material = "\x00".join(
        (scope_id, content_hexdigest, extraction_config_version, unit_ref, str(seq))
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:_CHUNK_ID_LENGTH]
