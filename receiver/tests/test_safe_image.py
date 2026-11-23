"""Untrusted images (security review finding 1): a phone-sent "photo" must never reach Ghostscript
or another exotic Pillow plugin, and a small file must not decode into gigabytes."""
import base64, io, struct, sys, warnings, zlib
from pathlib import Path
import pytest

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402
import safe_image  # noqa: E402
from safe_image import UnsafeImage, open_image  # noqa: E402

EPS = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nnewpath 0 0 moveto 10 10 lineto stroke\nshowpage\n%%EOF\n"


def eps_as_jpg(tmp_path) -> Path:
    p = tmp_path / "scan.jpg"          # the name and mime say JPEG, the content is PostScript
    p.write_bytes(EPS)
    return p


def png_with_size(tmp_path, w, h) -> Path:
    """A tiny PNG whose IHDR claims w×h (Image.open only parses the header)."""
    buf = io.BytesIO(); Image.new("L", (1, 1)).save(buf, "PNG")
    data = bytearray(buf.getvalue())
    ihdr = data.index(b"IHDR")
    data[ihdr + 4:ihdr + 12] = struct.pack(">II", w, h)
    data[ihdr + 17:ihdr + 21] = struct.pack(">I", zlib.crc32(bytes(data[ihdr:ihdr + 17])))
    p = tmp_path / "bomb.png"; p.write_bytes(bytes(data))
    return p


def photo(tmp_path, fmt="JPEG", name="p.jpg", size=(64, 48)) -> Path:
    p = tmp_path / name
    Image.new("RGB", size, (200, 120, 40)).save(p, fmt)
    return p


def test_plain_pillow_would_treat_it_as_eps(tmp_path):
    # sanity check of the threat: content sniffing picks EPS (open is lazy, so gs is not run here)
    with Image.open(eps_as_jpg(tmp_path)) as im:
        assert im.format == "EPS"


def test_eps_disguised_as_jpg_is_refused(tmp_path, monkeypatch):
    called = []
    import PIL.EpsImagePlugin as eps
    monkeypatch.setattr(eps, "Ghostscript", lambda *a, **k: called.append(a) or (_ for _ in ()).throw(AssertionError("gs")))
    with pytest.raises(UnsafeImage):
        open_image(eps_as_jpg(tmp_path))
    assert called == []


@pytest.mark.parametrize("fmt,name", [("JPEG", "a.jpg"), ("PNG", "a.png"), ("WEBP", "a.webp"), ("BMP", "a.bmp"), ("TIFF", "a.tif")])
def test_allowed_formats_open(tmp_path, fmt, name):
    if fmt == "WEBP" and not PIL.features.check("webp"):
        pytest.skip("Pillow built without WebP")
    with open_image(photo(tmp_path, fmt, name)) as im:
        assert im.size == (64, 48)


def test_multi_picture_jpeg_still_opens(tmp_path):
    p = tmp_path / "samsung.jpg"           # MPO: what some phone cameras write for a normal photo
    a, b = Image.new("RGB", (64, 48), "red"), Image.new("RGB", (64, 48), "blue")
    a.save(p, "MPO", save_all=True, append_images=[b])
    with open_image(p) as im:
        assert im.format == "MPO" and im.size == (64, 48)


def test_pixel_bomb_is_refused_before_decoding(tmp_path):
    p = png_with_size(tmp_path, 12000, 11000)   # 132 MP: over our cap, under Pillow's own hard limit
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)
        with pytest.raises(UnsafeImage, match="too large"):
            open_image(p)


def test_cap_is_configurable(tmp_path):
    with pytest.raises(UnsafeImage):
        open_image(photo(tmp_path), max_pixels=1000)


def test_outbox_thumb_ignores_eps_and_works_for_photos(tmp_path):
    import outbox
    assert outbox.make_thumb(str(eps_as_jpg(tmp_path))) is None
    t = outbox.make_thumb(str(photo(tmp_path, size=(1200, 900))))
    assert t and base64.b64decode(t).startswith(b"\xff\xd8\xff")


def test_document_conversion_keeps_the_file_instead_of_running_gs(tmp_path, monkeypatch):
    pytest.importorskip("cv2"); pytest.importorskip("img2pdf"); pytest.importorskip("pikepdf")
    pytest.importorskip("doc_convert")   # document drop is not in the public build
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    import doc_convert as dc
    monkeypatch.setattr(dc, "_engine", None); monkeypatch.setattr(dc, "_engine_error", None)
    dest = tmp_path / "save"; dest.mkdir()
    src = dest / "scan.jpg"; src.write_bytes(EPS)
    conv = dc.DocConverter(ocr=False, stage_dir=tmp_path / "stage")
    assert conv.fast([src], dest) is None           # conversion refused → caller saves the photo as is
    assert src.exists() and not list(dest.glob("*.pdf"))


def test_formats_exclude_ghostscript_and_pdf_plugins():
    assert not {"EPS", "PS", "PDF", "PSD", "ICNS"} & set(safe_image.FORMATS)
