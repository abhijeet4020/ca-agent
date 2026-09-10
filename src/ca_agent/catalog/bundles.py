"""Purpose: finds directories that are an unpacked desktop application rather than client
records. One corpus client folder holds a whole copy of the Income Tax e-filing utility, whose
bundled reference data - every ISIN on the exchange, every bank IFSC code - is 19 percent of
all extractable text in the corpus and would both poison retrieval and pay to embed junk. The
signal is two co-occurring Java-application markers, never a folder name, because clients
legitimately keep their real filings in folders called ITR.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path, PurePosixPath

#: A Java KeyStore is program credential storage. Client digital-signature files are .cer and
#: .pfx, which are deliberately not markers - the corpus is full of genuine ones.
_KEYSTORE_SUFFIX = ".keystore"
#: Java configuration. Alone it proves little; alongside a keystore it settles the question.
_PROPERTIES_SUFFIX = ".properties"


def find_application_bundles(raw_root: Path) -> tuple[PurePosixPath, ...]:
    """Locate unpacked application directories under the corpus root.

    A bundle root is the parent of the directory holding a Java keystore, provided that same
    subtree also contains a .properties file. Requiring both markers is what keeps a client's
    own ITR folder - or a folder of .cer and .pfx signature files - from being caught.
    """
    bundles: list[PurePosixPath] = []
    for keystore in _find_by_suffix(raw_root, _KEYSTORE_SUFFIX):
        candidate = keystore.parent.parent
        if not candidate.is_relative_to(raw_root) or candidate == raw_root:
            continue
        if not any(_find_by_suffix(candidate, _PROPERTIES_SUFFIX)):
            continue
        bundles.append(PurePosixPath(candidate.relative_to(raw_root).as_posix()))
    return tuple(sorted(set(bundles)))


def is_inside_bundle(
    relative_path: PurePosixPath, bundles: tuple[PurePosixPath, ...]
) -> bool:
    """True when this corpus-relative path lies within a detected application bundle.

    Every such file still gets a processing record; SPEC-01 req 6 forbids dropping it silently.
    Only the route changes, to NON_DATA.
    """
    return any(relative_path.is_relative_to(bundle) for bundle in bundles)


def _find_by_suffix(root: Path, suffix: str) -> Iterator[Path]:
    """Yield files with the given suffix, skipping directories that cannot be read."""
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.is_dir():
            yield from _find_by_suffix(entry, suffix)
        elif entry.is_file() and entry.suffix.lower() == suffix:
            yield entry
