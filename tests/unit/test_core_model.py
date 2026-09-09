"""Tests for the core domain value objects (TP-01 Group A and D support).

These types carry the lineage that SPEC-01 requirements 4, 7 and 8 depend on, so the tests
pin the two properties that are easy to break silently: companion-document naming that must
not collide across folders and extensions, and work identity that must include the scope.
"""

from pathlib import PurePosixPath

import pytest

from ca_agent.core.enums import ProcessingStatus, Route
from ca_agent.core.model import ArchiveRef, ContentHash, SourceRef, WorkKey
from ca_agent.core.scope import ClientScope

_SCOPE = ClientScope("Business Clients", "ACME", PurePosixPath("Business Clients/ACME"))
_OTHER_SCOPE = ClientScope("LLP", "ACME", PurePosixPath("LLP/ACME"))
_HASH = ContentHash(hexdigest="a" * 64, size_bytes=1024)


def _source(relative: str, archive_chain: tuple[ArchiveRef, ...] = ()) -> SourceRef:
    return SourceRef(
        scope=_SCOPE,
        relative_path=PurePosixPath(relative),
        archive_chain=archive_chain,
        size_bytes=1024,
        mtime_ns=1_700_000_000_000_000_000,
    )


def test_companion_name_keeps_full_filename_including_extension():
    # Arrange - SPEC-01 req 4 gives report.xlsx.format.md as the required shape
    source = _source("Business Clients/ACME/AY 24-25/report.xlsx")

    # Act
    companion = source.companion_name()

    # Assert
    assert companion == "report.xlsx.format.md"


def test_companion_paths_do_not_collide_across_extensions_or_folders():
    # Arrange - req 4 calls out exactly this collision risk
    excel = _source("Business Clients/ACME/a/report.xlsx")
    pdf = _source("Business Clients/ACME/b/report.pdf")

    # Act
    excel_path = excel.companion_relative_path()
    pdf_path = pdf.companion_relative_path()

    # Assert
    assert excel_path != pdf_path
    assert excel_path.name == "report.xlsx.format.md"
    assert pdf_path.name == "report.pdf.format.md"


def test_path_slug_is_stable_and_distinguishes_same_named_files():
    # Arrange
    first = _source("Business Clients/ACME/a/report.xlsx")
    second = _source("Business Clients/ACME/b/report.xlsx")

    # Act / Assert
    assert first.path_slug() == _source("Business Clients/ACME/a/report.xlsx").path_slug()
    assert first.path_slug() != second.path_slug()


def test_content_hash_rejects_a_malformed_digest():
    # Arrange / Act / Assert - fail fast rather than writing a corrupt output path
    with pytest.raises(ValueError):
        ContentHash(hexdigest="not-a-digest", size_bytes=10)


def test_work_key_is_scoped_so_identical_bytes_in_two_categories_differ():
    # Arrange - SPEC-01 req 7 forbids cross-category reuse
    first = WorkKey(_SCOPE.scope_id, _HASH.hexdigest, Route.TABULAR_PARQUET, "fp0001")
    second = WorkKey(_OTHER_SCOPE.scope_id, _HASH.hexdigest, Route.TABULAR_PARQUET, "fp0001")

    # Act / Assert
    assert first != second
    assert len({first, second}) == 2


def test_work_key_distinguishes_route_and_fingerprint():
    # Arrange
    base = WorkKey(_SCOPE.scope_id, _HASH.hexdigest, Route.TABULAR_PARQUET, "fp0001")
    other_route = WorkKey(_SCOPE.scope_id, _HASH.hexdigest, Route.TEXT_EXTRACT, "fp0001")
    other_fingerprint = WorkKey(_SCOPE.scope_id, _HASH.hexdigest, Route.TABULAR_PARQUET, "fp0002")

    # Act / Assert
    assert len({base, other_route, other_fingerprint}) == 3


def test_archive_chain_is_recorded_as_lineage():
    # Arrange - SPEC-01 req 7 requires archive identity and member path be retained
    member = _source(
        "Business Clients/ACME/AY 24-25/bundle.zip",
        archive_chain=(ArchiveRef(archive_hash=_HASH.hexdigest, member_path="sub/report.xlsx", depth=1),),
    )

    # Act
    lineage = member.archive_chain

    # Assert
    assert lineage[0].member_path == "sub/report.xlsx"
    assert lineage[0].depth == 1
    assert member.is_archive_member() is True


def test_plain_file_reports_no_archive_lineage():
    # Arrange / Act / Assert
    assert _source("Business Clients/ACME/x.pdf").is_archive_member() is False


def test_source_ref_rejects_a_path_outside_its_scope():
    # Arrange / Act / Assert - a mis-scoped lineage record would corrupt dedup isolation
    with pytest.raises(ValueError):
        SourceRef(
            scope=_SCOPE,
            relative_path=PurePosixPath("LLP/ACME/x.pdf"),
            archive_chain=(),
            size_bytes=1,
            mtime_ns=0,
        )


def test_only_success_is_an_active_candidate():
    # Arrange - SPEC-01 req 3 and 8: partial and failed stay history
    assert ProcessingStatus.SUCCESS.is_active_candidate() is True
    for status in (
        ProcessingStatus.PARTIAL,
        ProcessingStatus.FAILED,
        ProcessingStatus.LOCKED,
        ProcessingStatus.NO_READER,
    ):
        assert status.is_active_candidate() is False
