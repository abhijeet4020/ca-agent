"""Tests for archive expansion (TP-01 Group G, SPEC-01 requirement 7).

Three properties matter here and each has bitten real pipelines: the source archive is never
modified, a hostile or malformed member cannot escape the extraction root or exhaust the disk,
and a single bad member never stops the others. The depth-3 policy resolves the specification's
Open Decision, so exceeding it must produce an explicit record rather than a silent omission.
"""

from __future__ import annotations

import hashlib
import io
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ca_agent.config.settings import ArchiveSettings  # noqa: E402
from ca_agent.core.enums import ErrorCategory, FormatFamily  # noqa: E402
from ca_agent.readers.archives import (  # noqa: E402
    RATIO_CHECK_FLOOR_BYTES,
    extract_archive,
    safe_member_path,
)
from testdata.builders import files  # noqa: E402


def _archive(tmp_path: Path, name: str, payload: bytes) -> Path:
    return files.write_bytes(tmp_path / "src" / name, payload)


def _extract(tmp_path: Path, archive: Path, settings: ArchiveSettings | None = None, **kwargs):
    return extract_archive(
        archive,
        destination=tmp_path / "out",
        settings=settings or ArchiveSettings(),
        family=kwargs.pop("family", FormatFamily.ARCHIVE_ZIP),
        **kwargs,
    )


# --- the source is never touched -----------------------------------------------------------


def test_extraction_never_modifies_the_source_archive(tmp_path):
    # Arrange
    archive = _archive(tmp_path, "bundle.zip", files.plain_zip_bytes())
    before = hashlib.sha256(archive.read_bytes()).hexdigest()

    # Act
    _extract(tmp_path, archive)

    # Assert
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == before


def test_members_are_written_under_the_destination_only(tmp_path):
    # Arrange
    archive = _archive(tmp_path, "bundle.zip", files.plain_zip_bytes())

    # Act
    result = _extract(tmp_path, archive)

    # Assert
    destination = (tmp_path / "out").resolve()
    for member in result.members:
        assert member.target_path.resolve().is_relative_to(destination)


def test_extracted_member_content_is_hashed_for_deduplication(tmp_path):
    # Arrange - SPEC-01 req 7 dedupes members against ordinary files within a client scope
    archive = _archive(tmp_path, "bundle.zip", files.plain_zip_bytes({"a.csv": b"col\n1\n"}))

    # Act
    result = _extract(tmp_path, archive)

    # Assert
    assert result.members[0].content.hexdigest == hashlib.sha256(b"col\n1\n").hexdigest()


def test_member_retains_archive_lineage(tmp_path):
    # Arrange
    archive = _archive(tmp_path, "bundle.zip", files.plain_zip_bytes({"sub/report.csv": b"a\n1\n"}))

    # Act
    result = _extract(tmp_path, archive)

    # Assert
    member = result.members[0]
    assert member.member_path == "sub/report.csv"
    assert member.depth == 1
    assert member.archive_hash


# --- nesting -------------------------------------------------------------------------------


def test_nested_archives_are_expanded_up_to_the_configured_depth(tmp_path):
    # Arrange
    archive = _archive(tmp_path, "deep.zip", files.nested_zip_bytes(depth=3))

    # Act
    result = _extract(tmp_path, archive, ArchiveSettings(max_depth=3))

    # Assert - the innermost CSV is reached
    assert any(member.member_path.endswith("innermost.csv") for member in result.members)
    assert max(member.depth for member in result.members) == 3


def test_nested_zip_beyond_max_depth_records_explicit_failure_not_silent_skip(tmp_path):
    # Arrange - four levels against a depth-3 policy
    archive = _archive(tmp_path, "deep.zip", files.nested_zip_bytes(depth=4))

    # Act
    result = _extract(tmp_path, archive, ArchiveSettings(max_depth=3))

    # Assert
    categories = {failure.category for failure in result.failures}
    assert ErrorCategory.ARCHIVE_DEPTH_LIMIT_EXCEEDED in categories
    assert not any(member.member_path.endswith("innermost.csv") for member in result.members)
    # The archive that could not be opened is still recorded as a member, so it gets a record.
    assert any(member.depth == 3 for member in result.members)


def test_depth_one_still_records_the_nested_archive_it_did_not_open(tmp_path):
    # Arrange - SPEC-01 req 6 forbids a silent omission at any depth
    archive = _archive(tmp_path, "deep.zip", files.nested_zip_bytes(depth=2))

    # Act
    result = _extract(tmp_path, archive, ArchiveSettings(max_depth=1))

    # Assert
    assert result.failures
    assert all(failure.member_path for failure in result.failures)


# --- resource guards --------------------------------------------------------------------------


def test_zip_bomb_ratio_guard_aborts_mid_stream(tmp_path):
    # Arrange - highly compressible payload above the ratio-check floor
    payload = b"\x00" * (RATIO_CHECK_FLOOR_BYTES * 4)
    archive = _archive(tmp_path, "bomb.zip", files.plain_zip_bytes({"bomb.bin": payload}))

    # Act - a declared size is never trusted, so the ratio is checked while writing
    result = _extract(tmp_path, archive, ArchiveSettings(max_compression_ratio=5.0))

    # Assert
    assert any(
        failure.category is ErrorCategory.ARCHIVE_SIZE_LIMIT_EXCEEDED
        for failure in result.failures
    )
    assert not any(member.member_path == "bomb.bin" for member in result.members)


def test_partially_written_member_is_removed_when_a_cap_trips(tmp_path):
    # Arrange
    payload = b"\x00" * (RATIO_CHECK_FLOOR_BYTES * 4)
    archive = _archive(tmp_path, "bomb.zip", files.plain_zip_bytes({"bomb.bin": payload}))

    # Act
    _extract(tmp_path, archive, ArchiveSettings(max_compression_ratio=5.0))

    # Assert - no oversized partial file is left looking like a real output
    written = [path.name for path in (tmp_path / "out").rglob("*") if path.is_file()]
    assert "bomb.bin" not in written


def test_total_expanded_size_cap_stops_extraction(tmp_path):
    # Arrange
    members = {f"file{index}.bin": b"A" * 4096 for index in range(10)}
    archive = _archive(tmp_path, "big.zip", files.plain_zip_bytes(members))

    # Act
    result = _extract(tmp_path, archive, ArchiveSettings(max_total_expanded_bytes=8192))

    # Assert
    assert any(
        failure.category is ErrorCategory.ARCHIVE_SIZE_LIMIT_EXCEEDED
        for failure in result.failures
    )
    assert result.total_expanded_bytes <= 8192 + 4096


def test_member_count_cap_stops_extraction(tmp_path):
    # Arrange
    members = {f"file{index}.txt": b"x" for index in range(20)}
    archive = _archive(tmp_path, "many.zip", files.plain_zip_bytes(members))

    # Act
    result = _extract(tmp_path, archive, ArchiveSettings(max_member_count=5))

    # Assert
    assert any(
        failure.category is ErrorCategory.ARCHIVE_MEMBER_LIMIT_EXCEEDED
        for failure in result.failures
    )
    assert len(result.members) <= 5


# --- path safety ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    [
        "../../evil.txt",
        "..\\..\\evil.txt",
        "/etc/passwd",
        "C:\\Windows\\System32\\evil.dll",
        "\\\\server\\share\\evil.txt",
        "sub/../../escape.txt",
        "CON",
        "sub/NUL.txt",
        "LPT1.csv",
    ],
)
def test_unsafe_member_path_is_rejected(hostile):
    # Arrange / Act / Assert - traversal must never escape the extraction root
    assert safe_member_path(hostile) is None


@pytest.mark.parametrize(
    "benign", ["report.csv", "sub/report.csv", "AY 24-25/FS AMIT.xlsx", "a/b/c/deep.pdf"]
)
def test_ordinary_member_paths_are_accepted(benign):
    assert safe_member_path(benign) is not None


def test_traversal_member_is_recorded_and_siblings_continue(tmp_path):
    # Arrange
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive_file:
        archive_file.writestr("../escape.txt", b"evil")
        archive_file.writestr("good.csv", b"col\n1\n")
    archive = _archive(tmp_path, "mixed.zip", buffer.getvalue())

    # Act
    result = _extract(tmp_path, archive)

    # Assert
    assert any(f.category is ErrorCategory.UNSAFE_MEMBER_PATH for f in result.failures)
    assert [member.member_path for member in result.members] == ["good.csv"]
    assert not (tmp_path / "escape.txt").exists()


# --- locked members ---------------------------------------------------------------------------------


def test_encrypted_zip_member_is_recorded_and_siblings_continue(tmp_path):
    # Arrange - SPEC-01 req 7: record locked members, keep processing the rest
    import pyzipper

    target = tmp_path / "src" / "locked.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    with pyzipper.AESZipFile(target, "w", encryption=pyzipper.WZ_AES) as archive_file:
        archive_file.setpassword(b"secret")
        archive_file.writestr("locked.csv", b"col\n1\n")
    with zipfile.ZipFile(target, "a") as archive_file:
        archive_file.writestr("open.csv", b"col\n2\n")

    # Act
    result = _extract(tmp_path, target)

    # Assert
    locked = [f for f in result.failures if f.category is ErrorCategory.ARCHIVE_MEMBER_LOCKED]
    assert len(locked) == 1
    assert locked[0].member_path == "locked.csv"
    assert [member.member_path for member in result.members] == ["open.csv"]


def test_no_password_is_ever_supplied_or_guessed(tmp_path):
    # Arrange
    import pyzipper

    target = tmp_path / "src" / "locked.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    with pyzipper.AESZipFile(target, "w", encryption=pyzipper.WZ_AES) as archive_file:
        archive_file.setpassword(b"secret")
        archive_file.writestr("locked.csv", b"sensitive")

    # Act
    result = _extract(tmp_path, target)

    # Assert - the member stays locked; nothing was decrypted
    assert result.members == ()
    assert not any(path.name == "locked.csv" for path in (tmp_path / "out").rglob("*"))


# --- damaged archives ------------------------------------------------------------------------------


def test_corrupt_archive_is_recorded_as_corrupt_not_locked(tmp_path):
    # Arrange - SPEC-01 req 5 insists these stay distinct
    archive = _archive(tmp_path, "broken.zip", b"PK\x03\x04" + b"\x00" * 64)

    # Act
    result = _extract(tmp_path, archive)

    # Assert
    assert any(f.category is ErrorCategory.CORRUPT_FILE for f in result.failures)
    assert not any(f.category is ErrorCategory.PASSWORD_PROTECTED_FILE for f in result.failures)


def test_directory_entries_are_not_treated_as_members(tmp_path):
    # Arrange
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive_file:
        archive_file.writestr("folder/", b"")
        archive_file.writestr("folder/report.csv", b"col\n1\n")
    archive = _archive(tmp_path, "dirs.zip", buffer.getvalue())

    # Act
    result = _extract(tmp_path, archive)

    # Assert
    assert [member.member_path for member in result.members] == ["folder/report.csv"]


# --- other archive families ---------------------------------------------------------------------------


def test_gzip_stream_is_expanded_to_a_single_member(tmp_path):
    # Arrange
    archive = _archive(tmp_path, "dump.csv.gz", files.gzip_bytes(b"col\n1\n"))

    # Act
    result = _extract(tmp_path, archive, family=FormatFamily.ARCHIVE_GZIP)

    # Assert
    assert len(result.members) == 1
    assert result.members[0].target_path.read_bytes() == b"col\n1\n"


def test_seven_zip_archive_is_expanded(tmp_path):
    # Arrange
    import py7zr

    target = tmp_path / "src" / "bundle.7z"
    target.parent.mkdir(parents=True, exist_ok=True)
    with py7zr.SevenZipFile(target, "w") as archive_file:
        # py7zr's writestr takes the data first and the archive name second.
        archive_file.writestr("col\n1\n", "report.csv")

    # Act
    result = _extract(tmp_path, target, family=FormatFamily.ARCHIVE_7Z)

    # Assert
    assert [member.member_path for member in result.members] == ["report.csv"]


def test_rar_without_the_external_tool_reports_no_compatible_reader(tmp_path):
    # Arrange - 24 corpus archives; unrar may not be installed
    archive = _archive(tmp_path, "bundle.rar", files.rar_signature_bytes())

    # Act
    result = _extract(tmp_path, archive, family=FormatFamily.ARCHIVE_RAR)

    # Assert
    assert any(f.category is ErrorCategory.NO_COMPATIBLE_READER for f in result.failures)
    assert result.members == ()


# --- regressions found by extracting the real corpus -------------------------------------


def test_member_name_with_control_characters_is_sanitised_not_rejected(tmp_path):
    """A corpus zip holds a member whose name contains embedded newlines.

    Windows cannot create such a filename. Rejecting the member would lose real client data,
    so the on-disk name is sanitised while the original stays as lineage.
    """
    # Arrange
    hostile_name = "G D CONS\nINV NO 297\n10-03-2020.pdf"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive_file:
        archive_file.writestr(hostile_name, b"%PDF-1.4 invoice")
    archive = _archive(tmp_path, "newline.zip", buffer.getvalue())

    # Act
    result = _extract(tmp_path, archive)

    # Assert
    assert len(result.members) == 1, "the member must be extracted, not discarded"
    member = result.members[0]
    assert member.member_path == hostile_name, "the original name is retained as lineage"
    assert "\n" not in member.target_path.name
    assert member.target_path.read_bytes() == b"%PDF-1.4 invoice"


@pytest.mark.parametrize(
    "illegal",
    ["inv<oice.txt", "inv>oice.txt", 'inv"oice.txt', "inv|oice.txt", "inv?oice.txt", "inv*oice.txt",
     "report:final.txt"],
)
def test_windows_illegal_characters_are_sanitised(tmp_path, illegal):
    # Arrange / Act
    safe = safe_member_path(illegal)

    # Assert
    assert safe is not None
    assert not set(safe.name) & set('<>:"|?*')


@pytest.mark.parametrize("drive_like", ["a:b.txt", "C:report.txt", "z:/data.csv"])
def test_single_letter_drive_prefix_is_rejected_not_sanitised(drive_like):
    # Arrange / Act / Assert - Windows parses "a:" as drive A, so writing it could escape the
    # extraction root entirely. Sanitising the colon would mask a real traversal attempt.
    assert safe_member_path(drive_like) is None


def test_sanitised_names_do_not_collide(tmp_path):
    # Arrange - two different members that would sanitise to the same string
    first = safe_member_path("a\nb.txt")
    second = safe_member_path("a\tb.txt")

    # Assert - a collision would let one member overwrite the other, breaking immutability
    assert first != second


def test_trailing_dots_and_spaces_are_stripped(tmp_path):
    # Arrange / Act - Windows silently drops these, which would change the file identity
    safe = safe_member_path("report.pdf. ")

    # Assert
    assert safe is not None
    assert not safe.name.endswith((".", " "))


def test_a_member_that_cannot_be_written_is_recorded_and_siblings_continue(tmp_path, monkeypatch):
    # Arrange - cleanup after a write failure must not itself raise and abort the run
    import ca_agent.readers.archives as archives_module

    real_open = Path.open

    def _fail_for_one(self, *args, **kwargs):
        if self.name.startswith("bad"):
            raise OSError(22, "Invalid argument")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _fail_for_one)
    archive = _archive(
        tmp_path, "mixed.zip", files.plain_zip_bytes({"bad.txt": b"x", "good.csv": b"col\n1\n"})
    )

    # Act
    result = extract_archive(
        archive,
        destination=tmp_path / "out",
        settings=ArchiveSettings(),
        family=FormatFamily.ARCHIVE_ZIP,
    )

    # Assert
    assert any(f.category is ErrorCategory.EXTRACTION_ERROR for f in result.failures)
    assert [m.member_path for m in result.members] == ["good.csv"]
    assert archives_module is not None
