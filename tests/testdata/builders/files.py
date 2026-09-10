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


#: Long enough to clear the default 120 non-whitespace character threshold for a text page,
#: which a single heading deliberately does not.
_SAMPLE_PAGE_TEXT = "\n".join(
    (
        "Statement of profit and loss for the year ended 31 March 2026 with all schedules",
        "Depreciation, finance costs and other expenses reconciled against the trial balance",
        "Reserves and surplus carried forward to the balance sheet as at the same date",
    )
)

#: US Letter, the size every corpus PDF uses. Area 484,704 square points.
_PAGE_WIDTH = 612
_PAGE_HEIGHT = 792


def pdf_bytes(pages: list[dict]) -> bytes:
    """Build a PDF whose pages carry a real text layer, a real image, or both.

    Each entry describes one page: ``text`` places that string in a Helvetica text object, and
    ``image_fraction`` places a JPEG covering that share of the page height. Both are needed
    because the whole text-versus-scanned classification turns on what a page actually contains,
    and a page with no content stream at all - which is all the older builder could make - only
    ever exercises the empty branch. Written by hand so no PDF-writing dependency is added.
    """
    count = len(pages)
    font_number = 3
    page_numbers = [4 + index for index in range(count)]
    content_numbers = [4 + count + index for index in range(count)]
    image_numbers = [4 + 2 * count + index for index in range(count)]

    kids = " ".join(f"{number} 0 R" for number in page_numbers)
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Count {count} /Kids [{kids}] >>".encode(),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    for index, page in enumerate(pages):
        resources = f"/Font << /F1 {font_number} 0 R >>"
        if page.get("image_fraction"):
            resources += f" /XObject << /Im1 {image_numbers[index]} 0 R >>"
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_PAGE_WIDTH} {_PAGE_HEIGHT}] "
                f"/Resources << {resources} >> /Contents {content_numbers[index]} 0 R >>"
            ).encode()
        )

    for page in pages:
        objects.append(_content_stream(page))

    for page in pages:
        objects.append(_image_object(page.get("image_fraction")))

    return _assemble_pdf(objects)


def _content_stream(page: dict) -> bytes:
    parts: list[str] = []
    fraction = page.get("image_fraction")
    if fraction:
        height = _PAGE_HEIGHT * float(fraction)
        parts.append(f"q {_PAGE_WIDTH} 0 0 {height:.2f} 0 0 cm /Im1 Do Q")
    text = page.get("text")
    if text:
        parts.append("BT /F1 12 Tf")
        for line_number, line in enumerate(str(text).splitlines() or [""]):
            offset = _PAGE_HEIGHT - 72 - line_number * 14
            parts.append(f"1 0 0 1 72 {offset} Tm ({_escape_pdf_text(line)}) Tj")
        parts.append("ET")
    body = "\n".join(parts).encode("latin-1", "replace")
    return b"<< /Length " + str(len(body)).encode() + b" >>\nstream\n" + body + b"\nendstream"


def _escape_pdf_text(value: str) -> str:
    return value.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _image_object(fraction: float | None) -> bytes:
    if not fraction:
        # An unreferenced placeholder, so object numbering stays trivial to compute.
        return b"<< /Type /XObject /Subtype /Form /BBox [0 0 1 1] /Length 0 >>\nstream\n\nendstream"
    jpeg = image_bytes("JPEG", (64, 64))
    header = (
        b"<< /Type /XObject /Subtype /Image /Width 64 /Height 64 /ColorSpace /DeviceRGB "
        b"/BitsPerComponent 8 /Filter /DCTDecode /Length " + str(len(jpeg)).encode() + b" >>"
    )
    return header + b"\nstream\n" + jpeg + b"\nendstream"


def _assemble_pdf(objects: list[bytes]) -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /ID [<AB> <AB>] >>\n".encode()
    out += f"startxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


def text_pdf_bytes(page_texts: list[str]) -> bytes:
    """A PDF whose every page carries a native text layer."""
    return pdf_bytes([{"text": text} for text in page_texts])


def scanned_pdf_bytes(page_count: int = 1) -> bytes:
    """An image-only PDF, the shape a flatbed or phone scan produces."""
    return pdf_bytes([{"image_fraction": 0.95} for _ in range(page_count)])


def encrypted_pdf_bytes(*, user_password: str = "secret") -> bytes:
    """A genuinely locked PDF: the empty password will not open it."""
    import io as _io

    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter()
    writer.append(PdfReader(_io.BytesIO(text_pdf_bytes([_SAMPLE_PAGE_TEXT]))))
    writer.encrypt(user_password=user_password, owner_password="owner")
    buffer = _io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def owner_password_only_pdf_bytes() -> bytes:
    """An ITR-V shaped PDF: encrypted, but the empty user password opens it (ADR-008).

    Hundreds of the corpus's most valuable filings are this shape, and treating the presence of
    an encryption dictionary as "locked" would discard all of them.
    """
    import io as _io

    from pypdf import PdfReader, PdfWriter

    page = f"Indian Income Tax Return Acknowledgement\n{_SAMPLE_PAGE_TEXT}"
    writer = PdfWriter()
    writer.append(PdfReader(_io.BytesIO(text_pdf_bytes([page]))))
    writer.encrypt(user_password="", owner_password="owner")
    buffer = _io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


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


def document_bytes(
    body: list[tuple[str, object]],
) -> bytes:
    """Build a genuine docx from a body description, in the order given.

    ``body`` is a list of ``(kind, content)`` pairs where kind is "heading", "paragraph" or
    "table"; a table's content is a list of rows. Order matters: python-docx exposes paragraphs
    and tables as two separate collections, so a reader that does not walk the XML body will
    silently reorder them, and this builder is what makes that visible.
    """
    import docx

    document = docx.Document()
    for kind, content in body:
        if kind == "heading":
            document.add_heading(str(content), level=1)
        elif kind == "paragraph":
            document.add_paragraph(str(content))
        elif kind == "table":
            rows = list(content)  # type: ignore[arg-type]
            table = document.add_table(rows=len(rows), cols=len(rows[0]))
            for row_index, row in enumerate(rows):
                for column_index, value in enumerate(row):
                    table.cell(row_index, column_index).text = str(value)
        else:
            raise ValueError(f"unknown docx body element {kind!r}")

    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def presentation_bytes(slides: list[list[str]]) -> bytes:
    """Build a pptx package carrying the given text runs, one entry per slide.

    Written by hand rather than with python-pptx: the corpus holds two presentations, which
    does not justify a dependency, so the reader parses slide XML directly and this fixture
    has to produce that same shape.
    """
    drawing_ns = "http://schemas.openxmlformats.org/drawingml/2006/main"
    slide_ns = "http://schemas.openxmlformats.org/presentationml/2006/main"
    members = {
        "[Content_Types].xml": b'<?xml version="1.0"?><Types/>',
        "ppt/presentation.xml": b'<?xml version="1.0"?><presentation/>',
    }
    for index, runs in enumerate(slides, start=1):
        paragraphs = "".join(f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>" for text in runs)
        members[f"ppt/slides/slide{index}.xml"] = (
            f'<?xml version="1.0"?>'
            f'<p:sld xmlns:p="{slide_ns}" xmlns:a="{drawing_ns}">'
            f"<p:cSld><p:spTree><p:sp><p:txBody>{paragraphs}</p:txBody></p:sp></p:spTree></p:cSld>"
            f"</p:sld>"
        ).encode()
    return _zip_bytes(members)


def email_bytes(
    *,
    subject: str = "GST return filed",
    body: str = "The return for September has been filed.",
    html_body: str | None = None,
) -> bytes:
    """Build a genuine RFC 5322 message. ``html_body`` alone produces an HTML-only email."""
    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = "accounts@example.com"
    message["To"] = "client@example.com"
    message["Subject"] = subject
    message["Date"] = "Tue, 01 Sep 2026 10:00:00 +0530"
    if html_body is not None:
        message.set_content(html_body, subtype="html")
    else:
        message.set_content(body)
    return message.as_bytes()


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
