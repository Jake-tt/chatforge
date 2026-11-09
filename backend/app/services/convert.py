"""File conversions that parse untrusted bytes: documents to Markdown, PDF pages and images
re-encoded for the model.

Pure functions with no app state, so they can run in a separate process (see
`attachments.run_isolated`): a parser stuck on a hostile file is killed there instead of
holding a thread and a CPU in the server.
"""

from __future__ import annotations

import io

MAX_MARKDOWN_CHARS = 200_000  # stored; what the model sees is cut far shorter


class TooLarge(Exception):
    """The file decodes to more than we accept (checked before decoding)."""


class Unreadable(Exception):
    """Not the format it claims, or damaged."""


def to_markdown(data: bytes, extension: str) -> str:
    """Convert with MarkItDown, told the type we detected (it doesn't guess from the bytes)."""
    from markitdown import MarkItDown, StreamInfo  # heavy: imported on first use

    result = MarkItDown(enable_plugins=False).convert_stream(
        io.BytesIO(data), stream_info=StreamInfo(extension=extension))
    return result.text_content.strip()[:MAX_MARKDOWN_CHARS]


def prepare_image(data: bytes, max_side: int, max_pixels: int) -> tuple[bytes, str]:
    """The image re-encoded for the model: scaled to `max_side`, metadata dropped."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as image:
            # Read from the header, before any pixels are decoded.
            if image.width * image.height > max_pixels:
                raise TooLarge
            image.load()
            keeps_alpha = image.mode in ("RGBA", "LA", "P") and image.format != "JPEG"
            image = image.convert("RGBA" if keeps_alpha else "RGB")  # first frame of a GIF
    except TooLarge:
        raise
    except Exception as exc:  # not really an image, or a damaged one
        raise Unreadable from exc
    image.thumbnail((max_side, max_side))
    out = io.BytesIO()
    # Saved without the original's EXIF: no camera, time or location goes along.
    if keeps_alpha:
        image.save(out, "PNG", optimize=True)
        return out.getvalue(), "image/png"
    image.save(out, "JPEG", quality=90)
    return out.getvalue(), "image/jpeg"


def render_pages(data: bytes, max_side: int, limit: int) -> tuple[list[bytes], int]:
    """The first `limit` pages as JPEG images sized for the model, and the page count."""
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(data)
    try:
        pages = []
        for i in range(min(len(pdf), limit)):
            page = pdf[i]
            width, height = page.get_size()  # points
            bitmap = page.render(scale=max_side / max(width, height))
            out = io.BytesIO()
            bitmap.to_pil().convert("RGB").save(out, "JPEG", quality=85)
            pages.append(out.getvalue())
            page.close()
        return pages, len(pdf)
    finally:
        pdf.close()
