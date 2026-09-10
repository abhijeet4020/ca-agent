"""Purpose: turns whatever the corpus holds into an image the vision API will accept, at a
size worth paying for. Two jobs: normalise formats endpoints commonly reject (the corpus has
bmp, gif and tiff) and bound the longest edge, since request size and per-image cost both scale
with it. PDF pages are rasterised with pypdfium2 - never PyMuPDF, which is AGPL-3.0 and
incompatible with this codebase (ADR-010).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

_PNG_MEDIA_TYPE = "image/png"
#: A PDF user-space unit is 1/72 inch, so this converts a requested DPI to a render scale.
_POINTS_PER_INCH = 72.0


class PreprocessError(Exception):
    """The source could not be turned into an image. Recorded, never silently skipped."""


@dataclass(frozen=True, slots=True)
class PreparedImage:
    """Image bytes ready to send, with the media type to declare for them."""

    content: bytes
    media_type: str
    width: int
    height: int


def prepare_image(payload: bytes, *, max_edge_pixels: int) -> PreparedImage:
    """Normalise an image and bound its longest edge.

    An image already within the limit and already a PNG is returned untouched: re-encoding
    costs detail the model needs to read small print, for no benefit.
    """
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(payload)) as image:
            image.load()
            original_format = (image.format or "").upper()
            width, height = image.size
            if max(width, height) <= max_edge_pixels and original_format == "PNG":
                return PreparedImage(payload, _PNG_MEDIA_TYPE, width, height)

            prepared = image.convert("RGB")
            if max(width, height) > max_edge_pixels:
                prepared = _downscale(prepared, max_edge_pixels)
            return _to_png(prepared)
    except UnidentifiedImageError as error:
        raise PreprocessError(f"content is not a readable image: {error}") from error
    except (OSError, ValueError) as error:
        raise PreprocessError(f"image could not be prepared: {error}") from error


def _downscale(image, max_edge_pixels: int):
    from PIL import Image

    width, height = image.size
    scale = max_edge_pixels / float(max(width, height))
    # Rounding is applied to the shorter edge only, so the longest edge lands exactly on the
    # limit and the aspect ratio is preserved rather than drifting.
    if width >= height:
        size = (max_edge_pixels, max(1, round(height * scale)))
    else:
        size = (max(1, round(width * scale)), max_edge_pixels)
    return image.resize(size, Image.LANCZOS)


def _to_png(image) -> PreparedImage:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return PreparedImage(buffer.getvalue(), _PNG_MEDIA_TYPE, image.width, image.height)


def rasterise_pdf_page(source: Path, *, page_number: int, dpi: int) -> PreparedImage:
    """Render one PDF page to a PNG at the requested DPI (ADR-010: pypdfium2, not PyMuPDF)."""
    import pypdfium2

    try:
        document = pypdfium2.PdfDocument(source)
    except (OSError, ValueError, TypeError) as error:
        raise PreprocessError(f"PDF could not be opened for rasterisation: {error}") from error

    try:
        if not 1 <= page_number <= len(document):
            raise PreprocessError(
                f"page {page_number} is outside this document's {len(document)} pages"
            )
        page = document[page_number - 1]
        try:
            bitmap = page.render(scale=dpi / _POINTS_PER_INCH)
            image = bitmap.to_pil()
            try:
                return _to_png(image.convert("RGB"))
            finally:
                image.close()
        except (OSError, ValueError, TypeError) as error:
            raise PreprocessError(f"page {page_number} could not be rendered: {error}") from error
        finally:
            page.close()
    finally:
        document.close()
