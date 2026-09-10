"""Purpose: the chunk record the Gold layer will consume verbatim, and the only artifact that
survives once the source document is out of hand. Every field exists so a retrieved chunk can
be traced back to exactly one place - which scope, which source path, which archive member,
which unit of the document, which characters of it. Embedding fields are deliberately absent:
Phase 1 stops at chunk production and Gold adds the model and dimension.
"""

from __future__ import annotations

from dataclasses import dataclass

from ca_agent.core.enums import UnitType


class ChunkingError(Exception):
    """A chunk could not be produced truthfully - raised rather than recording false lineage."""


@dataclass(frozen=True, slots=True)
class ChunkRecord:
    """One chunk of text with the lineage needed to cite it."""

    chunk_id: str
    client_scope_id: str
    category: str
    client: str
    source_relpath: str
    content_sha256: str
    unit_type: UnitType
    unit_ref: str
    seq: int
    char_start: int
    char_end: int
    text: str
    text_sha256: str
    extraction_config_version: str
    archive_id: str | None = None
    member_path: str | None = None

    def __post_init__(self) -> None:
        if self.char_start < 0 or self.char_end < self.char_start:
            raise ValueError(f"chunk {self.chunk_id} has an unusable character range")
        if not self.text.strip():
            raise ValueError("SPEC-01 req 2 forbids emitting an empty chunk")

    def to_json_dict(self) -> dict[str, object]:
        """Flat mapping for JSONL. Flat because the Gold layer reads these as table rows."""
        return {
            "chunk_id": self.chunk_id,
            "client_scope_id": self.client_scope_id,
            "category": self.category,
            "client": self.client,
            "source_relpath": self.source_relpath,
            "archive_id": self.archive_id,
            "member_path": self.member_path,
            "content_sha256": self.content_sha256,
            "unit_type": self.unit_type.value,
            "unit_ref": self.unit_ref,
            "seq": self.seq,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "text": self.text,
            "text_sha256": self.text_sha256,
            "extraction_config_version": self.extraction_config_version,
        }


@dataclass(frozen=True, slots=True)
class ChunkSet:
    """Everything one document's text produced, including what was deliberately not emitted.

    The skip counts are part of the output rather than a log line: SPEC-01 req 2 requires the
    format document to report empty text that was not embedded, which is unprovable if the
    count is discarded.
    """

    chunks: tuple[ChunkRecord, ...] = ()
    skipped_empty_units: int = 0
    skipped_short_chunks: int = 0

    def is_empty(self) -> bool:
        return not self.chunks
