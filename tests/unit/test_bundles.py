"""Tests for unpacked-application detection (TP-01 Group C, SPEC-01 requirement 6).

One client folder in the real corpus holds an entire unpacked copy of the Income Tax e-filing
utility. Its bundled reference data - every ISIN on the exchange, every bank IFSC code - is 19
percent of all extractable text in the corpus and is not client data at all. These tests pin
the marker rule that finds it and, just as importantly, the false positives it must not catch:
clients really do have folders called ITR, and they really do hold .cer and .pfx files.
"""

from __future__ import annotations

import sys
from pathlib import Path, PurePosixPath

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ca_agent.catalog.bundles import find_application_bundles, is_inside_bundle  # noqa: E402
from testdata.builders import files  # noqa: E402


def _bundle_tree(root: Path) -> Path:
    """The real corpus shape: an ITR folder holding Config/ with a keystore, plus properties."""
    utility = root / "Business Clients" / "A CLIENT" / "AY 2019-20" / "ITR"
    files.write_bytes(utility / "Config" / "efilekeystore.keystore", b"\xfe\xed\xfe\xedstore")
    files.write_bytes(utility / "Config" / "ISIN_LIST.properties", b"INE305C01029=PANAMA\n")
    files.write_bytes(utility / "Common" / "scripts" / "jquery-1.8.0.js", b"/* jquery */")
    return utility


def test_unpacked_application_bundle_is_detected_from_its_markers(tmp_path):
    # Arrange
    utility = _bundle_tree(tmp_path)

    # Act
    bundles = find_application_bundles(tmp_path)

    # Assert - the root is the folder holding Config, not Config itself
    assert bundles == (PurePosixPath(utility.relative_to(tmp_path).as_posix()),)


def test_client_folder_named_like_a_utility_is_not_treated_as_a_bundle(tmp_path):
    # Arrange - clients really do keep their filings in a folder called ITR
    genuine = tmp_path / "Business Clients" / "A CLIENT" / "AY 2019-20" / "ITR"
    files.write_bytes(genuine / "ITR-3 acknowledgement.pdf", files.minimal_pdf_bytes())
    files.write_bytes(genuine / "computation.xlsx", files.xlsx_bytes())

    # Act / Assert - the folder name is never the signal
    assert find_application_bundles(tmp_path) == ()


def test_digital_signature_files_alone_do_not_make_a_bundle(tmp_path):
    # Arrange - .cer and .pfx are ordinary client digital-signature files
    client = tmp_path / "Business Clients" / "A CLIENT"
    files.write_bytes(client / "dsc" / "IncomeTaxPublicKey.cer", b"-----BEGIN CERTIFICATE-----")
    files.write_bytes(client / "dsc" / "signature.pfx", b"\x30\x82pfx")

    # Act / Assert
    assert find_application_bundles(tmp_path) == ()


def test_a_keystore_without_properties_is_not_enough(tmp_path):
    # Arrange - two independent Java-application markers are required, not one
    client = tmp_path / "Business Clients" / "A CLIENT"
    files.write_bytes(client / "Config" / "some.keystore", b"\xfe\xed\xfe\xedstore")

    # Act / Assert
    assert find_application_bundles(tmp_path) == ()


def test_every_file_under_a_bundle_is_excluded_including_images(tmp_path):
    # Arrange - the real bundle holds 18 icons that would otherwise reach the paid vision route
    utility = _bundle_tree(tmp_path)
    files.write_bytes(utility / "Common" / "images" / "logo.png", files.image_bytes())
    files.write_bytes(utility / "Config" / "IFSC.gz", files.gzip_bytes())
    files.write_bytes(utility / "Config" / "IFSC.txt", b"HDFC0000001 MUMBAI\n")
    bundles = find_application_bundles(tmp_path)

    # Act
    inside = [
        is_inside_bundle(
            PurePosixPath(path.relative_to(tmp_path).as_posix()), bundles
        )
        for path in (
            utility / "Common" / "images" / "logo.png",
            utility / "Config" / "IFSC.gz",
            utility / "Config" / "IFSC.txt",
        )
    ]

    # Assert
    assert all(inside)


def test_a_sibling_client_folder_is_not_swept_up_by_the_bundle(tmp_path):
    # Arrange - exclusion must stop at the bundle root, not the client
    _bundle_tree(tmp_path)
    genuine = PurePosixPath("Business Clients/A CLIENT/AY 2019-20/computation.xlsx")

    # Act
    bundles = find_application_bundles(tmp_path)

    # Assert
    assert not is_inside_bundle(genuine, bundles)


def test_a_corpus_with_no_bundle_reports_none(tmp_path):
    # Arrange
    files.write_bytes(tmp_path / "LLP" / "A CLIENT" / "notes.txt", b"opening balance")

    # Act / Assert
    assert find_application_bundles(tmp_path) == ()
