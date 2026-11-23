"""Opening untrusted images with Pillow (files the phone sent, or that a user drags back to it).

Pillow picks a decoder from the file's *content*, not its name. Without a format list a "scan.jpg"
that really is EPS/PostScript is handed to Ghostscript (EpsImagePlugin), and other rarely used
plugins (PSD, ICNS, PCX, …) become attack surface too. Only the raster formats DropTouch actually
handles are allowed here, and the pixel count is checked from the header before anything is decoded
(Image.open is lazy), so a small file cannot make us allocate gigabytes.
"""
from PIL import Image

# "JPEG" also covers MPO (multi-picture JPEG from Samsung/Fujifilm cameras): Pillow's JPEG opener
# returns an MpoImageFile for those; MPO has no opener of its own to list here.
FORMATS = ("JPEG", "PNG", "WEBP", "BMP", "TIFF")
# ≈ 108 MP phone photos still pass; Pillow's own hard limit is ~179 MP.
MAX_PIXELS = 120_000_000


class UnsafeImage(ValueError):
    """Not an allowed raster format, or too many pixels."""


def open_image(src, max_pixels: int = MAX_PIXELS) -> Image.Image:
    """Image.open() restricted to FORMATS and max_pixels. Raises UnsafeImage (or OSError for
    unreadable files). Use it as a context manager like Image.open()."""
    try:
        im = Image.open(src, formats=FORMATS)
    except Image.UnidentifiedImageError as e:
        raise UnsafeImage(f"not an allowed image format ({', '.join(FORMATS)})") from e
    w, h = im.size
    if w <= 0 or h <= 0 or w * h > max_pixels:
        im.close()
        raise UnsafeImage(f"image too large: {w}x{h} px (limit {max_pixels // 1_000_000} MP)")
    return im
