"""Purpose: builds small synthetic files that stand in for the real corpus during tests.

Real client data is confidential and gitignored, so it can never be a fixture. Every builder
here writes a genuine file of the format it names - a real OOXML zip, a real OLE container, a
real PDF - because the whole point of the detection tests is that signatures are read from
actual bytes. Builders are deterministic (fixed timestamps, fixed PDF identifiers) so hash
assertions are exact.
"""

from __future__ import annotations

import gzip
import io
import json
import struct
import zipfile
from pathlib import Path

#: Fixed member timestamp so archive bytes are reproducible across runs.
_FIXED_ZIP_TIME = (2026, 1, 1, 0, 0, 0)


def write_bytes(target: Path, payload: bytes) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return target


def write_text(target: Path, payload: str, encoding: str = "utf-8") -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload.encode(encoding))
    return target


# --- OOXML and ZIP ---------------------------------------------------------------------


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            info = zipfile.ZipInfo(name, date_time=_FIXED_ZIP_TIME)
            # A ZipInfo carries its own compress_type, which overrides the ZipFile's. Without
            # this the member is STORED, and a "zip bomb" fixture would not actually compress.
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, payload)
    return buffer.getvalue()


def xlsx_bytes(*, macro_enabled: bool = False) -> bytes:
    """Minimal but structurally genuine OOXML spreadsheet package."""
    members = {
        "[Content_Types].xml": b'<?xml version="1.0"?><Types/>',
        "xl/workbook.xml": b'<?xml version="1.0"?><workbook/>',
        "xl/worksheets/sheet1.xml": b'<?xml version="1.0"?><worksheet/>',
    }
    if macro_enabled:
        members["xl/vbaProject.bin"] = b"\xd0\xcf\x11\xe0macro"
    return _zip_bytes(members)


def docx_bytes() -> bytes:
    return _zip_bytes(
        {
            "[Content_Types].xml": b'<?xml version="1.0"?><Types/>',
            "word/document.xml": b'<?xml version="1.0"?><document/>',
        }
    )


def pptx_bytes() -> bytes:
    return _zip_bytes(
        {
            "[Content_Types].xml": b'<?xml version="1.0"?><Types/>',
            "ppt/presentation.xml": b'<?xml version="1.0"?><presentation/>',
        }
    )


def plain_zip_bytes(members: dict[str, bytes] | None = None) -> bytes:
    return _zip_bytes(members or {"readme.txt": b"hello", "sub/report.csv": b"a,b\n1,2\n"})


def nested_zip_bytes(depth: int) -> bytes:
    """A zip containing a zip containing ... `depth` levels deep, innermost holding a CSV."""
    payload = _zip_bytes({"innermost.csv": b"col\n1\n"})
    for level in range(depth - 1, 0, -1):
        payload = _zip_bytes({f"level{level}.zip": payload})
    return payload


def gzip_bytes(payload: bytes = b"col\n1\n") -> bytes:
    return gzip.compress(payload)


def seven_zip_signature_bytes() -> bytes:
    """7z magic followed by filler. Enough for signature detection, not for extraction."""
    return b"7z\xbc\xaf\x27\x1c" + b"\x00" * 32


def rar_signature_bytes() -> bytes:
    """RAR5 magic followed by filler."""
    return b"Rar!\x1a\x07\x01\x00" + b"\x00" * 32


# --- OLE compound files ----------------------------------------------------------------

_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def ole_bytes(stream_names: tuple[str, ...]) -> bytes:
    """An OLE2 header plus UTF-16 stream names in the directory area.

    This is not a valid compound file, but it carries the two signals detection actually reads:
    the magic number and which stream names are present. Building a real OLE container would
    require a write-side dependency that production code does not need.
    """
    header = _OLE_MAGIC + b"\x00" * 504
    directory = b"".join(name.encode("utf-16-le") + b"\x00\x00" for name in stream_names)
    return header + directory + b"\x00" * 128


def legacy_xls_bytes() -> bytes:
    return ole_bytes(("Workbook",))


def legacy_doc_bytes() -> bytes:
    return ole_bytes(("WordDocument",))


def encrypted_ooxml_bytes() -> bytes:
    """An encrypted xlsx is an OLE container holding an EncryptedPackage stream."""
    return ole_bytes(("EncryptionInfo", "EncryptedPackage"))


# --- PDF -------------------------------------------------------------------------------


def minimal_pdf_bytes(page_count: int = 1) -> bytes:
    """A structurally valid PDF built by hand, with no third-party writer dependency."""
    objects: list[bytes] = []
    kids = " ".join(f"{3 + index} 0 R" for index in range(page_count))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Count {page_count} /Kids [{kids}] >>".encode())
    for _ in range(page_count):
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << >> >>"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /ID [<AB> <AB>] >>\n".encode()
    out += f"startxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


# --- images ----------------------------------------------------------------------------


def image_bytes(image_format: str = "PNG", size: tuple[int, int] = (16, 16)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", size, color=(200, 200, 200)).save(buffer, format=image_format)
    return buffer.getvalue()


# --- structured text --------------------------------------------------------------------


def itr_json_bytes(record_count: int = 4) -> bytes:
    """ITR-shaped JSON with a homogeneous record array and zero-padded identifiers."""
    document = {
        "ITR": {
            "ITR3": {
                "PersonalInfo": {"PAN": "0001234A", "Name": "TEST CLIENT"},
                "ScheduleBP": [
                    {"SrNo": index, "Code": f"{index:04d}", "Amount": index * 1000}
                    for index in range(1, record_count + 1)
                ],
            }
        }
    }
    return json.dumps(document, indent=2).encode("utf-8")


def nested_json_bytes() -> bytes:
    """Document-like JSON with no homogeneous collection, so it routes to chunking."""
    return json.dumps({"a": {"b": {"c": {"note": "deeply nested prose value"}}}}).encode("utf-8")


def xml_bytes() -> bytes:
    return (
        b'<?xml version="1.0" encoding="UTF-8"?>\n'
        b"<ITRReturn><PersonalInfo><PAN>0001234A</PAN></PersonalInfo></ITRReturn>"
    )


def html_bytes() -> bytes:
    return b"<!DOCTYPE html>\n<html><body><table><tr><td>1</td></tr></table></body></html>"


def rtf_bytes() -> bytes:
    return rb"{\rtf1\ansi\deff0 {\fonttbl {\f0 Times;}}\f0\fs24 Balance Sheet\par}"


def csv_bytes(delimiter: str = ",") -> bytes:
    header = delimiter.join(("PAN", "GSTIN", "Amount"))
    row_one = delimiter.join(("0001234A", "27AAAAA0000A1Z5", "1000"))
    row_two = delimiter.join(("0005678B", "27BBBBB0000B1Z5", "2000"))
    return f"{header}\n{row_one}\n{row_two}\n".encode()


# --- non-data artifacts ------------------------------------------------------------------


def thumbs_db_bytes() -> bytes:
    """Windows thumbnail cache. All 411 .db files in the real corpus are these."""
    return ole_bytes(("Catalog",))


def excel_lock_stub_bytes() -> bytes:
    """The 38-byte owner-lock stub Excel leaves beside an open workbook."""
    return b"\x00\x06Abhijeet Sutar" + b"\x20" * 22


def tally_binary_bytes() -> bytes:
    """Opaque Tally company data. No Python reader exists for this format."""
    return struct.pack("<4sI", b"TLY\x00", 1800) + bytes(range(256)) * 2


def executable_bytes() -> bytes:
    """A DOS/PE header. Must be recorded and never executed."""
    return b"MZ\x90\x00" + b"\x00" * 60 + b"PE\x00\x00" + b"\x00" * 64


# --- real spreadsheets (written with openpyxl so the bytes are genuine OOXML) --------------


def workbook_bytes(
    sheets: dict[str, list[list]],
    *,
    text_columns: dict[str, tuple[int, ...]] | None = None,
    formulas: dict[str, dict[str, str]] | None = None,
) -> bytes:
    """Build a genuine xlsx workbook.

    ``text_columns`` forces the given zero-based column indexes to be written as Excel text,
    which is how identifiers such as PAN and GSTIN keep their leading zeros in the real corpus.
    ``formulas`` writes a raw formula string into a named cell; openpyxl stores no cached
    result for it, which reproduces the uncached-formula case SPEC-01 req 1 requires reporting.
    """
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.remove(workbook.active)
    forced_text = text_columns or {}

    for sheet_name, rows in sheets.items():
        sheet = workbook.create_sheet(title=sheet_name)
        text_indexes = set(forced_text.get(sheet_name, ()))
        for row in rows:
            sheet.append(list(row))
        for column_index in text_indexes:
            for cell in tuple(sheet.iter_cols(min_col=column_index + 1, max_col=column_index + 1))[0]:
                cell.number_format = "@"
        for reference, expression in (formulas or {}).get(sheet_name, {}).items():
            sheet[reference] = expression

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
