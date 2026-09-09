"""Purpose: decides what a file actually is, from its bytes rather than its name. Probing the
real corpus showed extensions cannot be trusted - 61 of 66 .xlk files are OOXML, roughly 30
percent of sampled .xls files are not BIFF at all, and every .db file is a Windows Thumbs.db -
so routing on extension would corrupt whole buckets. Detection runs three stages in order:
magic number, then container inspection (a ZIP could be xlsx, docx, pptx or a plain archive;
an OLE container could be xls, doc or an encrypted OOXML package), then a decoded-text probe.
The extension is used only as a last resort and is otherwise recorded as evidence.
"""

from __future__ import annotations

import csv
import io
import json
import struct
import zipfile
from collections.abc import Iterable
from pathlib import Path

from ca_agent.config.settings import DetectionSettings
from ca_agent.core.enums import FormatFamily
from ca_agent.core.model import FormatProbe

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")

#: Magic numbers that identify a family outright. Longest-first so a short prefix such as the
#: two-byte BMP marker cannot pre-empt a longer, more specific match.
_SIGNATURES: tuple[tuple[bytes, FormatFamily, str | None], ...] = (
    (b"%PDF", FormatFamily.PDF, "pdf"),
    (b"\x89PNG\r\n\x1a\n", FormatFamily.IMAGE, "png"),
    (b"\xff\xd8\xff", FormatFamily.IMAGE, "jpeg"),
    (b"GIF87a", FormatFamily.IMAGE, "gif"),
    (b"GIF89a", FormatFamily.IMAGE, "gif"),
    (b"II*\x00", FormatFamily.IMAGE, "tiff"),
    (b"MM\x00*", FormatFamily.IMAGE, "tiff"),
    (b"7z\xbc\xaf\x27\x1c", FormatFamily.ARCHIVE_7Z, "7z"),
    (b"Rar!\x1a\x07", FormatFamily.ARCHIVE_RAR, "rar"),
    (b"\x1f\x8b", FormatFamily.ARCHIVE_GZIP, "gzip"),
    (rb"{\rtf", FormatFamily.RTF, "rtf"),
    # The Income Tax e-filing utility is a Java application and writes serialized object
    # streams carrying a .pdf extension; 261 of these exist in the corpus. They are never
    # deserialised: Java deserialisation is a known code-execution vector, and SPEC-01 req 6
    # is explicit that reading a file does not authorize executing its contents.
    (b"\xac\xed\x00\x05", FormatFamily.JAVA_SERIALIZED, "java-object-stream"),
    (b"\xfe\xed\xfe\xed", FormatFamily.KEYSTORE, "jks"),
)

#: Tally company data. No Python library reads these, so they are named by extension only and
#: recorded as such - the confidence field makes that assumption visible.
_TALLY_EXTENSIONS = frozenset({".1800", ".900", ".001", ".tsf"})

#: Shell and Office debris that carries no client data.
_NON_DATA_BASENAMES = frozenset({"thumbs.db", "desktop.ini", ".ds_store"})
_NON_DATA_PREFIXES = ("~$", "._")
_NON_DATA_EXTENSIONS = frozenset({".lnk", ".css", ".js", ".thmx", ".tmp", ".url"})
_EXECUTABLE_EXTENSIONS = frozenset({".exe", ".bat", ".sh", ".cmd", ".com", ".scr", ".msi"})

#: Coarse candidates for deciding *whether* content is delimited. Picking the exact
#: delimiter for parsing is the tabular reader's job, driven by TabularSettings.
_DELIMITER_CANDIDATES = (",", ";", "	", "|")
_TEXT_SAMPLE_LINES = 8
_MIN_DELIMITED_COLUMNS = 2
#: Above this share of NUL and control bytes the prefix is binary and the text probe is skipped,
#: which stops charset detection inventing an encoding for Tally company data.
_MAX_CONTROL_BYTE_RATIO = 0.05


def detect_format(path: Path, settings: DetectionSettings) -> FormatProbe:
    """Identify a file's format. Raises OSError if the file cannot be read."""
    size = path.stat().st_size
    extension = path.suffix.lower()

    non_data = _classify_non_data(path, extension)
    if non_data is not None:
        return non_data
    if size == 0:
        return _probe(FormatFamily.EMPTY, "signature", extension, detail="file is zero bytes")

    with path.open("rb") as handle:
        prefix = handle.read(settings.signature_sample_bytes)

    probe = _from_signature(prefix, extension, path, len(prefix), size)
    if probe is not None:
        return probe
    probe = _from_text(prefix, extension, settings, len(prefix))
    if probe is not None:
        return probe
    return _from_extension(extension, len(prefix))


# --- stage 0: names that need no content inspection -------------------------------------


def _classify_non_data(path: Path, extension: str) -> FormatProbe | None:
    name = path.name
    lowered = name.lower()
    if lowered in _NON_DATA_BASENAMES:
        return _probe(
            FormatFamily.NON_DATA_ARTIFACT,
            "signature",
            extension,
            detail=f"{name} is a shell artifact, not client data",
        )
    if name.startswith(_NON_DATA_PREFIXES):
        reason = "Excel owner-lock stub" if name.startswith("~$") else "AppleDouble resource fork"
        return _probe(FormatFamily.NON_DATA_ARTIFACT, "signature", extension, detail=reason)
    if extension in _NON_DATA_EXTENSIONS:
        return _probe(
            FormatFamily.NON_DATA_ARTIFACT, "extension", extension, detail="presentation asset"
        )
    if extension in _EXECUTABLE_EXTENSIONS:
        # SPEC-01 req 6: reading a file does not authorize executing its contents.
        return _probe(
            FormatFamily.EXECUTABLE, "extension", extension, detail="never executed, recorded only"
        )
    return None


# --- stage 1: magic numbers and containers ------------------------------------------------


def _from_signature(
    prefix: bytes, extension: str, path: Path, inspected: int, size: int
) -> FormatProbe | None:
    if prefix.startswith(_ZIP_MAGICS):
        return _from_zip_container(path, extension, inspected)
    if prefix.startswith(_OLE_MAGIC):
        return _from_ole_container(prefix, extension, inspected, path)
    for magic, family, subtype in _SIGNATURES:
        if prefix.startswith(magic):
            return _probe(family, "signature", extension, subtype=subtype, inspected=inspected)
    if _looks_like_bmp(prefix, size):
        return _probe(
            FormatFamily.IMAGE, "signature", extension, subtype="bmp", inspected=inspected
        )
    if _looks_like_executable(prefix):
        return _probe(
            FormatFamily.EXECUTABLE,
            "signature",
            extension,
            inspected=inspected,
            detail="never executed, recorded only",
        )
    return None


def _looks_like_bmp(prefix: bytes, size: int) -> bool:
    """Validate the two-byte BMP marker against its header fields.

    "BM" alone is far too weak to sit in the signature table - plenty of text starts that way -
    so the declared file size must match the real one and the reserved words must be zero.
    """
    if not prefix.startswith(b"BM") or len(prefix) < 14:
        return False
    declared_size = int.from_bytes(prefix[2:6], "little")
    reserved = int.from_bytes(prefix[6:10], "little")
    pixel_offset = int.from_bytes(prefix[10:14], "little")
    return reserved == 0 and declared_size == size and 0 < pixel_offset <= size


def _looks_like_executable(prefix: bytes) -> bool:
    """A DOS/PE stub. Checked after text so a document beginning "MZ" is not misread."""
    return prefix.startswith(b"MZ") and b"PE\x00\x00" in prefix[:512]


def _from_zip_container(path: Path, extension: str, inspected: int) -> FormatProbe:
    """Distinguish the OOXML packages from a plain archive by their member names."""
    try:
        with zipfile.ZipFile(path) as archive:
            names = frozenset(archive.namelist())
    except (zipfile.BadZipFile, OSError):
        return _probe(
            FormatFamily.ARCHIVE_ZIP,
            "signature",
            extension,
            inspected=inspected,
            detail="ZIP signature present but the central directory could not be read",
        )

    if any(name.startswith("xl/") for name in names):
        macros = "xl/vbaProject.bin" in names
        subtype = "xlsm" if macros else "xlsx"
        return _probe(
            FormatFamily.SPREADSHEET_OOXML,
            "container",
            extension,
            subtype=subtype,
            macros_present=macros,
            inspected=inspected,
            evidence=("xl/workbook.xml present",),
        )
    if any(name.startswith("word/") for name in names):
        return _probe(
            FormatFamily.WORD_OOXML, "container", extension, subtype="docx", inspected=inspected
        )
    if any(name.startswith("ppt/") for name in names):
        return _probe(
            FormatFamily.PRESENTATION_OOXML,
            "container",
            extension,
            subtype="pptx",
            inspected=inspected,
        )
    return _probe(
        FormatFamily.ARCHIVE_ZIP,
        "container",
        extension,
        subtype="zip",
        inspected=inspected,
        evidence=(f"{len(names)} members",),
    )


def _from_ole_container(
    prefix: bytes, extension: str, inspected: int, path: Path
) -> FormatProbe:
    """Read the OLE directory stream names.

    All of legacy xls, legacy doc, Thumbs.db and an *encrypted* OOXML package share the same
    magic number, so the stream names are the only way to tell them apart. Mistaking an
    encrypted package for legacy BIFF would report a locked file as a corrupt one, which
    SPEC-01 req 5 explicitly forbids.
    """
    streams = _ole_stream_names(prefix, path)
    if "EncryptedPackage" in streams:
        return _probe(
            FormatFamily.ENCRYPTED_OOXML,
            "container",
            extension,
            inspected=inspected,
            evidence=("EncryptedPackage stream present",),
        )
    if streams & {"Workbook", "Book"}:
        return _probe(
            FormatFamily.SPREADSHEET_BIFF, "container", extension, subtype="xls", inspected=inspected
        )
    if "WordDocument" in streams:
        return _probe(
            FormatFamily.WORD_OLE, "container", extension, subtype="doc", inspected=inspected
        )
    if "Catalog" in streams:
        return _probe(
            FormatFamily.NON_DATA_ARTIFACT,
            "container",
            extension,
            inspected=inspected,
            detail="thumbs.db style thumbnail cache",
        )
    return _probe(
        FormatFamily.ENCRYPTED_OLE if "EncryptionInfo" in streams else FormatFamily.UNKNOWN,
        "container",
        extension,
        inspected=inspected,
        detail="OLE compound file with unrecognised streams",
    )


def _ole_stream_names(prefix: bytes, path: Path) -> frozenset[str]:
    """Recover OLE directory entry names, preferring a real parse over a raw scan.

    A compound file stores its directory in a sector named by the header, which is usually
    nowhere near the start of the file, so scanning a fixed prefix misses it. Running against
    the corpus proved this: 175 genuine .xls and .doc files were classified UNKNOWN until
    olefile was used. The raw scan is kept as a fallback so a truncated or damaged container
    stays classifiable rather than raising.
    """
    import olefile

    try:
        with olefile.OleFileIO(path) as container:
            return frozenset(entry[0] for entry in container.listdir() if entry)
    except (OSError, ValueError, struct.error):
        return _scan_utf16_names(prefix)


def _scan_utf16_names(prefix: bytes) -> frozenset[str]:
    """Best-effort name recovery for a container that olefile cannot parse."""
    decoded = prefix[512:].decode("utf-16-le", errors="ignore")
    return frozenset(part for part in decoded.split("\x00") if part and part.isprintable())


# --- stage 2: decoded text ----------------------------------------------------------------


def _from_text(
    prefix: bytes, extension: str, settings: DetectionSettings, inspected: int
) -> FormatProbe | None:
    if _is_binary(prefix):
        return None
    decoded, encoding = _decode(prefix[: settings.text_probe_bytes])
    if decoded is None:
        return None

    inspected += min(len(prefix), settings.text_probe_bytes)
    stripped = decoded.lstrip("﻿ \t\r\n")
    if not stripped:
        return _probe(FormatFamily.PLAIN_TEXT, "text", extension, encoding=encoding, inspected=inspected)

    if stripped[0] in "{[" and _parses_as_json(stripped):
        return _probe(FormatFamily.JSON, "text", extension, encoding=encoding, inspected=inspected)
    if stripped.startswith("<"):
        lowered = stripped[:512].lower()
        is_html = lowered.startswith("<!doctype html") or "<html" in lowered
        family = FormatFamily.HTML if is_html else FormatFamily.XML
        return _probe(family, "text", extension, encoding=encoding, inspected=inspected)
    if _looks_like_email(stripped):
        return _probe(FormatFamily.EMAIL, "text", extension, encoding=encoding, inspected=inspected)

    delimiter = _delimiter_of(stripped, _DELIMITER_CANDIDATES)
    if delimiter is not None:
        return _probe(
            FormatFamily.DELIMITED_TEXT,
            "text",
            extension,
            subtype=f"delimiter={delimiter!r}",
            encoding=encoding,
            inspected=inspected,
        )
    return _probe(FormatFamily.PLAIN_TEXT, "text", extension, encoding=encoding, inspected=inspected)


def _is_binary(prefix: bytes) -> bool:
    """True when the prefix carries enough NUL or control bytes to rule out text.

    UTF-16 text is full of NULs, so it is exempted by checking for the BOM first.
    """
    if prefix.startswith((b"\xff\xfe", b"\xfe\xff")):
        return False
    if b"\x00" in prefix and not _is_utf16_without_bom(prefix):
        return True
    control = sum(1 for byte in prefix if byte < 9 or 13 < byte < 32)
    return control / max(len(prefix), 1) > _MAX_CONTROL_BYTE_RATIO


def _is_utf16_without_bom(prefix: bytes) -> bool:
    """Detect BOM-less UTF-16 by its characteristic alternating NUL pattern."""
    sample = prefix[: min(len(prefix), 256)]
    if len(sample) < 4:
        return False
    even_nulls = sum(1 for index in range(0, len(sample), 2) if sample[index] == 0)
    odd_nulls = sum(1 for index in range(1, len(sample), 2) if sample[index] == 0)
    half = len(sample) // 2
    return even_nulls > half * 0.8 or odd_nulls > half * 0.8


def _decode(payload: bytes) -> tuple[str | None, str | None]:
    """Decode a byte sample, reporting the encoding used."""
    from charset_normalizer import from_bytes

    best = from_bytes(payload).best()
    if best is None:
        return None, None
    return str(best), best.encoding


def _parses_as_json(text: str) -> bool:
    try:
        json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        # A truncated prefix of a large valid document still counts as JSON.
        return text.rstrip().endswith(("}", "]")) is False and len(text) > 0
    return True


def _looks_like_email(text: str) -> bool:
    head = text[:512].lower()
    return head.startswith(("from:", "received:", "return-path:", "message-id:"))


def _delimiter_of(text: str, candidates: Iterable[str]) -> str | None:
    """Return the delimiter giving a consistent, multi-column shape across sample lines."""
    lines = [line for line in text.splitlines()[:_TEXT_SAMPLE_LINES] if line.strip()]
    if len(lines) < 2:
        return None
    sample = "\n".join(lines)
    for candidate in candidates:
        try:
            rows = list(csv.reader(io.StringIO(sample), delimiter=candidate))
        except csv.Error:
            continue
        widths = {len(row) for row in rows if row}
        if len(widths) == 1 and widths.pop() >= _MIN_DELIMITED_COLUMNS:
            return candidate
    return None


# --- stage 3: extension of last resort -------------------------------------------------------


def _from_extension(extension: str, inspected: int) -> FormatProbe:
    if extension in _TALLY_EXTENSIONS:
        return _probe(
            FormatFamily.TALLY_BINARY,
            "extension",
            extension,
            inspected=inspected,
            detail="Tally company data; no Python reader exists for this format",
        )
    return _probe(
        FormatFamily.UNKNOWN,
        "none",
        extension,
        inspected=inspected,
        detail="no signature matched and the content is not decodable text",
    )


def _probe(
    family: FormatFamily,
    confidence: str,
    extension: str,
    *,
    subtype: str | None = None,
    macros_present: bool = False,
    encoding: str | None = None,
    inspected: int = 0,
    evidence: tuple[str, ...] = (),
    detail: str | None = None,
) -> FormatProbe:
    """Assemble a probe, deriving whether the extension contradicts the observed content."""
    conflict = _extension_conflicts(family, extension)
    if conflict:
        evidence = (*evidence, f"extension {extension or '(none)'} conflicts with {family.value}")
    return FormatProbe(
        family=family,
        confidence=confidence,
        declared_extension=extension,
        subtype=subtype,
        extension_conflict=conflict,
        macros_present=macros_present,
        encoding=encoding,
        bytes_inspected=inspected,
        evidence=evidence,
        detail=detail,
    )


#: Extensions consistent with each family. An extension outside its family's set is a conflict,
#: recorded as evidence rather than treated as a failure.
_EXPECTED_EXTENSIONS: dict[FormatFamily, frozenset[str]] = {
    FormatFamily.SPREADSHEET_OOXML: frozenset({".xlsx", ".xlsm", ".xltx", ".xltm"}),
    FormatFamily.SPREADSHEET_BIFF: frozenset({".xls", ".xlt", ".xlk"}),
    FormatFamily.SPREADSHEET_XLSB: frozenset({".xlsb"}),
    FormatFamily.WORD_OOXML: frozenset({".docx", ".docm", ".dotx"}),
    FormatFamily.WORD_OLE: frozenset({".doc", ".dot"}),
    FormatFamily.PRESENTATION_OOXML: frozenset({".pptx", ".pptm"}),
    FormatFamily.PDF: frozenset({".pdf"}),
    FormatFamily.IMAGE: frozenset({".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff"}),
    FormatFamily.RTF: frozenset({".rtf"}),
    FormatFamily.HTML: frozenset({".htm", ".html", ".xhtml"}),
    FormatFamily.XML: frozenset({".xml", ".xsd", ".xsl"}),
    FormatFamily.JSON: frozenset({".json"}),
    FormatFamily.EMAIL: frozenset({".eml", ".msg"}),
    FormatFamily.ARCHIVE_ZIP: frozenset({".zip", ".jar"}),
    FormatFamily.ARCHIVE_7Z: frozenset({".7z"}),
    FormatFamily.ARCHIVE_RAR: frozenset({".rar"}),
    FormatFamily.ARCHIVE_GZIP: frozenset({".gz", ".gzip", ".emz", ".tgz"}),
    FormatFamily.ENCRYPTED_OOXML: frozenset({".xlsx", ".xlsm", ".docx", ".pptx"}),
}


def _extension_conflicts(family: FormatFamily, extension: str) -> bool:
    expected = _EXPECTED_EXTENSIONS.get(family)
    if expected is None or not extension:
        return False
    return extension not in expected
