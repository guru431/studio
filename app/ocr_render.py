"""Картинки страниц для распознавания (спека 2026-10-27-studio-ocr, 6.2, 6.4, 6.5).

- PDF рисует pypdfium2 (pypdf рисовать не умеет): 300 DPI, длинная сторона — не больше MAX_SIDE.
- Фото — PNG, JPEG, WebP до image_upload.VIDEO_PIXELS, с поворотом по EXIF и тем же пределом.
- Страница PDF в чате требует распознавания (`needs_ocr`), если текста слоя у pypdf нет или его меньше
  LAYER_MIN_CHARS знаков без пробельных и на странице есть картинка; пустая — почти однотонная картинка
  (ImageStat).
- Всё синхронно (зовётся через asyncio.to_thread). PDFium не потокобезопасен даже на разных
  документах — вызовы идут под одним замком; документ открывается и закрывается внутри вызова.
- Отказы — ScanError со словами для человека; ни байты, ни текст в журнал не идут.
"""
import io
import threading
import warnings

import pypdfium2
import pypdfium2.raw as pdfium_c
from PIL import Image, ImageOps, ImageStat, UnidentifiedImageError

import documents
import image_upload

RENDER_DPI = 300
MAX_SIDE = 2200
LAYER_MIN_CHARS = 40
PAGES_MAX = 50
BLANK_STDDEV = 2.0
PHOTO_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
PHOTO_FORMATS = ("PNG", "JPEG", "WEBP")

BROKEN = documents.BROKEN
PASSWORD = "PDF защищён паролем"
NOT_PHOTO = "нужны PDF или фото PNG, JPEG, WebP"
PHOTO_TOO_BIG = f"фото больше {image_upload.VIDEO_PIXELS // 1_000_000} Мп"

PDFIUM = threading.Lock()


class ScanError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def has_layer(text: str) -> bool:
    return sum(not c.isspace() for c in text) >= LAYER_MIN_CHARS


def _has_image(resources, depth: int = 0) -> bool:
    """В ресурсах страницы есть картинка — XObject `/Image`, в том числе внутри `/Form` (до 3 уровней).
    Ресурсы не разбираются — True: тогда страница распознаётся, как по прежнему правилу."""
    try:
        xobjects = resources.get_object().get("/XObject") if resources is not None else None
        if xobjects is None:
            return False
        for ref in xobjects.get_object().values():
            obj = ref.get_object()
            subtype = obj.get("/Subtype")
            if subtype == "/Image":
                return True
            if subtype == "/Form" and depth < 3 and _has_image(obj.get("/Resources"), depth + 1):
                return True
        return False
    except Exception:                                 # noqa: BLE001 — битые ресурсы pypdf роняет по-разному
        return True


def needs_ocr(page, text: str) -> bool:
    """Страница PDF в чате требует распознавания: текста слоя нет, или его меньше LAYER_MIN_CHARS знаков без
    пробелов и на странице есть картинка (скан с водяным знаком «Scanned with CamScanner»). Короткая страница
    без картинки (одна строка, подписи) берётся из слоя как есть."""
    return not text or (not has_layer(text) and _has_image(page.get("/Resources")))


def pdf_scan_pages(data: bytes) -> list[tuple[str, bool]]:
    """(текст слоя, нужно ли распознавание) каждой страницы PDF для чата; отказы — DocumentError
    documents.pdf_page_texts (битый файл, текст длиннее предела). Раздел «Распознавание» слой не читает."""
    return documents.pdf_pages(data, lambda page, text: (text, needs_ocr(page, text)))


# ---- PDF ---------------------------------------------------------------------

def _open(data: bytes) -> pypdfium2.PdfDocument:
    try:
        return pypdfium2.PdfDocument(data)
    except pypdfium2.PdfiumError as e:
        raise ScanError(PASSWORD if e.err_code == pdfium_c.FPDF_ERR_PASSWORD else BROKEN) from None


def pdf_page_count(data: bytes) -> int:
    with PDFIUM:
        pdf = _open(data)
        try:
            return len(pdf)
        finally:
            pdf.close()


def render_page(data: bytes, index: int) -> bytes:
    """PNG страницы `index` (с 0): 300 DPI, длинная сторона не больше MAX_SIDE."""
    with PDFIUM:
        pdf = _open(data)
        try:
            if not 0 <= index < len(pdf):
                raise ScanError(BROKEN)
            page = pdf[index]
            try:
                width, height = page.get_size()
                scale = min(RENDER_DPI / 72, MAX_SIDE / max(width, height, 1))
                image = page.render(scale=scale).to_pil().convert("RGB")   # копия из буфера PDFium
            except pypdfium2.PdfiumError:
                raise ScanError(BROKEN) from None
            finally:
                page.close()
        finally:
            pdf.close()
    image.thumbnail((MAX_SIDE, MAX_SIDE))                 # округление масштаба не даст 2201
    return _png(image)


# ---- фото --------------------------------------------------------------------

def check_photo(raw: bytes) -> str:
    """Только заголовок: PNG, JPEG или WebP (иначе 415) не больше VIDEO_PIXELS (иначе 413). Итог —
    формат Pillow (PHOTO_FORMATS)."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                fmt, pixels = image.format, image.width * image.height
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise ScanError(PHOTO_TOO_BIG, 413) from None
    except (UnidentifiedImageError, OSError, ValueError):
        raise ScanError(NOT_PHOTO, 415) from None
    if fmt not in PHOTO_FORMATS:
        raise ScanError(NOT_PHOTO, 415)
    if pixels > image_upload.VIDEO_PIXELS:
        raise ScanError(PHOTO_TOO_BIG, 413)
    return fmt


def photo_png(raw: bytes) -> bytes:
    """PNG фото: поворот по EXIF, без метаданных, длинная сторона не больше MAX_SIDE.

    Декодирование — как image_upload.normalize_image, но с пределом MAX_SIDE: тот ужимает до
    ~2 Мп для шлюза картинок, а распознаванию нужна страница в 2200 точек (спека, 6.2)."""
    check_photo(raw)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.format == "JPEG":
                    image.draft("RGB", (MAX_SIDE, MAX_SIDE))   # декодер сразу уменьшает в 2–8 раз
                image.load()
                image = ImageOps.exif_transpose(image).convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError,
            Image.DecompressionBombWarning):
        raise ScanError(NOT_PHOTO, 415) from None
    image.thumbnail((MAX_SIDE, MAX_SIDE))
    return _png(image)


# ---- картинка ----------------------------------------------------------------

def _png(image: Image.Image) -> bytes:
    image.info.clear()
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def is_blank(png: bytes) -> bool:
    """Почти однотонная страница: разброс яркости меньше BLANK_STDDEV."""
    with Image.open(io.BytesIO(png)) as image:
        return ImageStat.Stat(image.convert("L")).stddev[0] < BLANK_STDDEV
