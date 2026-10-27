"""Рендер страниц для распознавания (app/ocr_render.py): PDF и фото в PNG, слой, пустые страницы."""
import io

import pytest
from PIL import Image

import documents
import ocr_render
from ocr_render import ScanError
from scans import photo, png_header, scan_pdf


def size(png: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(png)) as img:
        assert img.format == "PNG"
        return img.size


def test_pages_render_at_300_dpi_capped_at_2200():
    png = ocr_render.render_page(scan_pdf("ink"), 0)                 # 612×792 pt: 300 DPI дало бы 3300
    assert size(png) == (1700, 2200)
    assert ocr_render.pdf_page_count(scan_pdf("ink", "Payment is due in 30 days", "blank")) == 3


def test_small_page_renders_at_300_dpi():
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(144, 72)                                        # 2×1 дюйма
    out = io.BytesIO()
    w.write(out)
    assert size(ocr_render.render_page(out.getvalue(), 0)) == (600, 300)


def test_layer_needs_forty_letters():
    assert not ocr_render.has_layer("Scanned with CamScanner")
    assert not ocr_render.has_layer("x " * 39 + "\n\t")
    assert ocr_render.has_layer("x" * 40)


def test_blank_page_is_detected():
    assert ocr_render.is_blank(ocr_render.render_page(scan_pdf("blank"), 0))
    assert not ocr_render.is_blank(ocr_render.render_page(scan_pdf("ink"), 0))


def test_photo_is_turned_by_exif_and_capped():
    raw = photo("JPEG", (4000, 3000), orientation=6)
    ocr_render.check_photo(raw)
    assert size(ocr_render.photo_png(raw)) == (1650, 2200)
    assert size(ocr_render.photo_png(photo("WEBP", (800, 600)))) == (800, 600)     # меньшее не растягивается


def test_heic_and_huge_photos_are_refused_in_words():
    for raw in (b"\x00\x00\x00\x18ftypheic" + b"\x00" * 64, photo("GIF"), b""):
        with pytest.raises(ScanError) as e:
            ocr_render.check_photo(raw)
        assert (e.value.status, str(e.value)) == (415, "нужны PDF или фото PNG, JPEG, WebP")
    with pytest.raises(ScanError) as e:
        ocr_render.check_photo(png_header(8000, 7000))               # 56 Мп > VIDEO_PIXELS
    assert e.value.status == 413 and "50 Мп" in str(e.value)
    with pytest.raises(ScanError) as e:
        ocr_render.photo_png(png_header(8000, 7000))
    assert e.value.status == 413
    ocr_render.check_photo(png_header(5000, 10_000))                 # ровно предел — можно


def test_page_texts_per_page():
    assert documents.pdf_page_texts(scan_pdf("Payment is due in 30 days", "blank")) == \
        ["Payment is due in 30 days", ""]


def test_page_texts_refuse_broken_and_too_long(monkeypatch):
    with pytest.raises(documents.DocumentError) as e:
        documents.pdf_page_texts(b"%PDF-1.4 junk")
    assert str(e.value) == documents.BROKEN
    monkeypatch.setattr(documents, "TEXT_MAX_CHARS", 30)
    with pytest.raises(documents.DocumentError) as e:
        documents.pdf_page_texts(scan_pdf("Payment is due in 30 days", "Second page"))
    assert str(e.value) == documents.TOO_LONG


def test_broken_pdf_is_words():
    for call in (lambda: ocr_render.pdf_page_count(b"%PDF-1.4 junk"),
                 lambda: ocr_render.render_page(b"%PDF-1.4 junk", 0),
                 lambda: ocr_render.render_page(scan_pdf("ink"), 1)):
        with pytest.raises(ScanError) as e:
            call()
        assert (e.value.status, str(e.value)) == (422, "документ повреждён")


def test_password_pdf_is_words():
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    w.append(PdfReader(io.BytesIO(scan_pdf("ink"))))
    w.encrypt("secret", algorithm="RC4-128")
    out = io.BytesIO()
    w.write(out)
    with pytest.raises(ScanError) as e:
        ocr_render.pdf_page_count(out.getvalue())
    assert (e.value.status, str(e.value)) == (422, "PDF защищён паролем")
