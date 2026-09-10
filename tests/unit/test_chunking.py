"""Tests for chunk production (TP-01 Group J, SPEC-01 requirement 2).

Phase 1 stops at chunk records; embedding them is Gold-layer work. What matters here is that a
chunk can be traced all the way back - scope, source path, archive member, content hash, unit
and character offsets - because once these records reach the Gold layer the original document
is no longer in hand. Determinism matters for the same reason req 8 does: a rerun that reuses
content must not renumber its chunks.
"""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ca_agent.chunking.splitter import chunk_units  # noqa: E402
from ca_agent.config.settings import ChunkingSettings  # noqa: E402
from ca_agent.core.enums import UnitType  # noqa: E402
from ca_agent.core.model import ArchiveRef, ContentHash, SourceRef  # noqa: E402
from ca_agent.core.scope import ClientScope  # noqa: E402
from ca_agent.core.text import TextUnit  # noqa: E402

_FINGERPRINT = "textcfg0000000001"
_CONTENT = ContentHash("a" * 64, size_bytes=2048)


def _scope(category: str = "GST Company", client: str = "Finoracles Services") -> ClientScope:
    return ClientScope(
        category=category, client=client, scope_root=PurePosixPath(f"{category}/{client}")
    )


def _source(scope: ClientScope | None = None, *, archived: bool = False) -> SourceRef:
    resolved = scope or _scope()
    chain = (
        (ArchiveRef(archive_hash="b" * 64, member_path="returns/september.docx", depth=1),)
        if archived
        else ()
    )
    return SourceRef(
        scope=resolved,
        relative_path=resolved.scope_root / "RETURNS/notes.docx",
        archive_chain=chain,
    )


def _units(*texts: str) -> tuple[TextUnit, ...]:
    return tuple(
        TextUnit(unit_type=UnitType.HEADING, unit_ref=f"Section {index}", text=text, sequence=index)
        for index, text in enumerate(texts)
    )


def _chunk(units, *, source=None, settings=None):
    return chunk_units(
        units,
        source=source or _source(),
        content=_CONTENT,
        settings=settings or ChunkingSettings(),
        extraction_config_version=_FINGERPRINT,
    )


# --- lineage ---------------------------------------------------------------------------


def test_chunk_metadata_carries_full_lineage():
    # Arrange
    scope = _scope()

    # Act
    result = _chunk(_units("Depreciation charged for the year is 12,500."), source=_source(scope))
    chunk = result.chunks[0]

    # Assert - every field the Gold layer needs to cite this text without the document
    assert chunk.client_scope_id == scope.scope_id
    assert chunk.category == "GST Company"
    assert chunk.client == "Finoracles Services"
    assert chunk.source_relpath.endswith("RETURNS/notes.docx")
    assert chunk.content_sha256 == _CONTENT.hexdigest
    assert chunk.unit_type is UnitType.HEADING
    assert chunk.unit_ref == "Section 0"
    assert chunk.seq == 0
    assert chunk.extraction_config_version == _FINGERPRINT
    assert chunk.text_sha256


def test_archive_member_lineage_is_carried_into_chunks():
    # Arrange - SPEC-01 req 12: a chunk from inside an archive must name where it came from
    result = _chunk(_units("Invoice total 45,000."), source=_source(archived=True))

    # Act
    chunk = result.chunks[0]

    # Assert
    assert chunk.archive_id == "b" * 64
    assert chunk.member_path == "returns/september.docx"


def test_loose_file_chunks_record_no_archive_lineage():
    # Arrange / Act
    chunk = _chunk(_units("Opening balance.")).chunks[0]

    # Assert
    assert chunk.archive_id is None
    assert chunk.member_path is None


def test_repeated_unit_refs_stay_individually_addressable():
    """Corpus regression: unit_ref is a human citation, not an identifier.

    One corpus bank statement extracts to 784 units carrying only 167 distinct refs - the
    heading "Receipt" appears 250 times. A chunk naming only a ref and an offset could not be
    resolved back to one place, which is exactly what the lineage fields exist to guarantee.
    """
    # Arrange - two different sections that happen to share a heading
    units = (
        TextUnit(unit_type=UnitType.HEADING, unit_ref="Receipt", text="First receipt.", sequence=0),
        TextUnit(unit_type=UnitType.HEADING, unit_ref="Receipt", text="Second receipt.", sequence=1),
    )

    # Act
    chunks = _chunk(units).chunks

    # Assert - the ref is shared, but the record still names exactly one unit
    assert [chunk.unit_ref for chunk in chunks] == ["Receipt", "Receipt"]
    assert [chunk.unit_sequence for chunk in chunks] == [0, 1]
    assert chunks[0].chunk_id != chunks[1].chunk_id


def test_offsets_resolve_against_the_unit_named_by_unit_sequence():
    # Arrange - the offsets of the second unit are meaningless against the first
    units = (
        TextUnit(unit_type=UnitType.HEADING, unit_ref="Receipt", text="short", sequence=0),
        TextUnit(
            unit_type=UnitType.HEADING,
            unit_ref="Receipt",
            text="a much longer second receipt body",
            sequence=1,
        ),
    )

    # Act
    chunks = _chunk(units).chunks

    # Assert - each chunk resolves against the unit its sequence names
    for chunk in chunks:
        unit = units[chunk.unit_sequence]
        assert unit.text[chunk.char_start : chunk.char_end] == chunk.text


# --- offsets and ordering ----------------------------------------------------------------


def test_chunk_character_offsets_locate_the_text_in_its_unit():
    # Arrange - an offset that does not resolve back to the source makes a chunk untraceable
    body = " ".join(f"Ledger entry {index} of the trial balance." for index in range(80))
    units = _units(body)

    # Act
    result = _chunk(units, settings=ChunkingSettings(chunk_size=200, chunk_overlap=20))

    # Assert
    assert len(result.chunks) > 1
    for chunk in result.chunks:
        assert body[chunk.char_start : chunk.char_end] == chunk.text


def test_chunk_sequence_is_continuous_across_units():
    # Arrange - seq orders the whole document, not each unit separately
    result = _chunk(_units("First section text.", "Second section text."))

    # Act
    sequences = [chunk.seq for chunk in result.chunks]

    # Assert
    assert sequences == list(range(len(sequences)))


def test_chunk_overlap_is_applied_between_adjacent_chunks():
    # Arrange
    body = " ".join(f"word{index}" for index in range(200))

    # Act
    result = _chunk(_units(body), settings=ChunkingSettings(chunk_size=120, chunk_overlap=40))

    # Assert - adjacent chunks must overlap in the source, not merely abut
    first, second = result.chunks[0], result.chunks[1]
    assert second.char_start < first.char_end


# --- empty text ---------------------------------------------------------------------------


def test_empty_text_produces_no_chunks():
    # Arrange - SPEC-01 req 2: never embed empty text
    units = (
        TextUnit(unit_type=UnitType.PAGE, unit_ref="page:1", text="   \n\t  ", sequence=0),
        TextUnit(unit_type=UnitType.PAGE, unit_ref="page:2", text="", sequence=1),
    )

    # Act
    result = _chunk(units)

    # Assert - and the count is retained so the format document can report it
    assert result.chunks == ()
    assert result.skipped_empty_units == 2


def test_chunks_below_the_minimum_character_count_are_not_emitted():
    # Arrange
    units = (
        TextUnit(unit_type=UnitType.PAGE, unit_ref="page:1", text="ok", sequence=0),
    )

    # Act
    result = _chunk(units, settings=ChunkingSettings(min_chunk_characters=10))

    # Assert
    assert result.chunks == ()
    assert result.skipped_short_chunks == 1


# --- determinism and scope isolation --------------------------------------------------------


def test_chunk_ids_are_deterministic_across_runs():
    # Arrange - SPEC-01 req 8: a rerun must not renumber or rename its chunks
    units = _units("Depreciation schedule for the year.", "Notes to the accounts.")

    # Act
    first = _chunk(units)
    second = _chunk(units)

    # Assert
    assert [chunk.chunk_id for chunk in first.chunks] == [
        chunk.chunk_id for chunk in second.chunks
    ]


def test_identical_text_in_two_scopes_produces_different_chunk_ids():
    # Arrange - SPEC-01 req 7: LIC Employees exists under two categories and must stay separate
    units = _units("Identical text in both scopes.")
    cooperative = _scope("Cooperative Audits", "LIC Employees")
    proprietor = _scope("GST Proprietor", "LIC Employees")

    # Act
    first = _chunk(units, source=_source(cooperative))
    second = _chunk(units, source=_source(proprietor))

    # Assert
    assert first.chunks[0].chunk_id != second.chunks[0].chunk_id


def test_chunk_id_changes_when_the_extraction_configuration_changes():
    # Arrange - a chunk produced under different settings is a different artifact
    units = _units("Trial balance as at year end.")

    # Act
    baseline = _chunk(units).chunks[0]
    reconfigured = chunk_units(
        units,
        source=_source(),
        content=_CONTENT,
        settings=ChunkingSettings(),
        extraction_config_version="differentfingerprint",
    ).chunks[0]

    # Assert
    assert baseline.chunk_id != reconfigured.chunk_id


# --- serialisation --------------------------------------------------------------------------


def test_chunk_serialises_to_a_flat_json_object():
    # Arrange - these records are written as JSONL for the Gold layer to consume verbatim
    chunk = _chunk(_units("Closing stock valued at cost.")).chunks[0]

    # Act
    payload = chunk.to_json_dict()

    # Assert
    assert payload["unit_type"] == UnitType.HEADING.value
    assert payload["chunk_id"] == chunk.chunk_id
    assert all(not isinstance(value, (dict, list)) for value in payload.values())
