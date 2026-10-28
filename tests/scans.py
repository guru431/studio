"""Помощники тестов распознавания: PDF из сканов и текстовых страниц, фото.

Страница скана — картинка 850×1100 точек при 100 DPI (612×792 pt), без текстового слоя."""
import io
import struct
import zlib

from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

SCAN_SIZE = (850, 1100)


def _text_page(text: str) -> bytes:
    """Страница 612×792 pt с текстом (Helvetica, ASCII) — как `pdf` в test_documents."""
    w = PdfWriter()
    page = w.add_blank_page(612, 792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): w._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 720 Td (" + text.encode("ascii") + b") Tj ET")
    page[NameObject("/Contents")] = w._add_object(stream)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def _image_page(ink: bool) -> bytes:
    img = Image.new("RGB", SCAN_SIZE, "white")
    if ink:
        draw = ImageDraw.Draw(img)
        for y in range(100, 1000, 60):
            draw.rectangle((80, y, 770, y + 14), fill=(20, 20, 20))
    out = io.BytesIO()
    img.save(out, "PDF", resolution=100.0)
    return out.getvalue()


def _text_over_ink(text: str) -> bytes:
    """Скан с текстом слоя поверх картинки — как водяной знак «Scanned with CamScanner»."""
    w = PdfWriter()
    w.append(PdfReader(io.BytesIO(_image_page(True))))
    w.pages[0].merge_page(PdfReader(io.BytesIO(_text_page(text))).pages[0])
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def scan_pdf(*pages: str) -> bytes:
    """PDF: "blank" — белая картинка, "empty" — страница без текста и картинки, "ink" — картинка с тёмными
    полосами, "ink+текст" — она же с текстом слоя поверх, иначе — страница с текстом."""
    w = PdfWriter()
    for page in pages:
        if page == "empty":
            w.add_blank_page(612, 792)
            continue
        one = (_image_page(page == "ink") if page in ("blank", "ink") else
               _text_over_ink(page[len("ink+"):]) if page.startswith("ink+") else _text_page(page))
        w.append(PdfReader(io.BytesIO(one)))
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def photo(fmt: str = "JPEG", size: tuple[int, int] = (800, 600), orientation: int | None = None) -> bytes:
    """Фото в формате Pillow `fmt`: серый фон и тёмный прямоугольник; `orientation` — тег EXIF 0x0112."""
    img = Image.new("RGB", size, (200, 200, 200))
    ImageDraw.Draw(img).rectangle((0, 0, size[0] // 4, size[1] // 4), fill=(10, 10, 10))
    out = io.BytesIO()
    if orientation is not None:
        exif = Image.Exif()
        exif[0x0112] = orientation
        img.save(out, fmt, exif=exif)
    else:
        img.save(out, fmt)
    return out.getvalue()


def png_header(width: int, height: int) -> bytes:
    """PNG 1×1 с размерами `width`×`height` в IHDR: заголовок для проверок, декодировать нельзя."""
    raw = photo("PNG", (1, 1))
    ihdr = raw[12:16] + struct.pack(">II", width, height) + raw[24:29]
    return raw[:12] + ihdr + struct.pack(">I", zlib.crc32(ihdr)) + raw[33:]
