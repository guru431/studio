"""Документы чата: файл с устройства (multipart, одно поле `file`) и его текст (спека, раздел 7.7).

- PDF — текстовый слой pypdf (страницы через пустую строку); без слоя — отказ «это скан».
- DOCX — `word/document.xml` из zip: абзацы `w:p`, текст `w:t`.
- TXT и MD — UTF-8 (BOM снимается), иначе cp1251.
- Отказы — DocumentError со словами для человека; текст, имя файла и байты в журнал не идут:
  журнал pypdf (он пишет начало битого файла) приглушён.
"""
import asyncio
import io
import logging
import re
import zipfile
from email import policy
from email.parser import BytesParser
from pathlib import PurePosixPath
from xml.etree import ElementTree

from pypdf import PdfReader

DOC_MAX = 20 * 1024 * 1024
TEXT_MAX_CHARS = 2_000_000
SUFFIXES = (".pdf", ".docx", ".txt", ".md")
BODY_MAX = DOC_MAX + 128 * 1024
NAME_MAX = 200
# распакованный document.xml: дальше — zip-бомба, а не документ; ElementTree берёт памяти примерно
# вдесятеро, две загрузки по 64 МиБ подошли бы к MemoryMax=2G службы
XML_MAX = 16 * 1024 * 1024
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

UNSUPPORTED = "поддерживаются PDF, DOCX, TXT и MD"
SCAN = "в PDF нет текстового слоя — это скан"
BROKEN = "документ повреждён"
TOO_LONG = "текст длиннее 2 млн знаков"

logging.getLogger("pypdf").setLevel(logging.CRITICAL + 1)


class DocumentError(ValueError):
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


# ---- загрузка ----------------------------------------------------------------

def clean_name(filename: str) -> str:
    """Имя без каталогов и управляющих знаков, не длиннее NAME_MAX."""
    name = PurePosixPath(filename.replace("\\", "/")).name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return name[:NAME_MAX] or "file"


def _parts(raw: bytes, content_type: str, max_files: int) -> list[tuple[str, bytes]]:
    """Части `file` по порядку: от 1 до max_files, каждая непустая, вместе — не больше DOC_MAX."""
    wrong = "нужен ровно один файл в поле file" if max_files == 1 else "нужны файлы в поле file"
    if not content_type.lower().startswith("multipart/form-data;"):
        raise DocumentError("ожидается multipart/form-data с файлом в поле file", 400)
    try:
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + content_type.encode("ascii") + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw)
    except UnicodeError:
        raise DocumentError("некорректный Content-Type", 400) from None
    if not message.is_multipart() or message.defects:
        raise DocumentError("некорректный multipart", 400)
    parts = list(message.iter_parts())
    if not parts or (max_files == 1 and len(parts) != 1):
        raise DocumentError(wrong, 400)
    if len(parts) > max_files:
        raise DocumentError(f"больше {max_files} файлов за раз", 422)
    out = []
    for part in parts:
        filename = part.get_filename()
        data = part.get_payload(decode=True)
        if (part.defects or part.get_param("name", header="content-disposition") != "file"
                or not filename or not isinstance(data, bytes)):
            raise DocumentError(wrong, 400)
        if not data or len(data) > DOC_MAX:
            raise DocumentError(f"файл должен быть непустым и не больше {DOC_MAX >> 20} МиБ", 413)
        out.append((clean_name(filename), data))
    if sum(len(data) for _, data in out) > DOC_MAX:
        raise DocumentError(f"файлы вместе больше {DOC_MAX >> 20} МиБ", 413)
    return out


def _parse(raw: bytes, content_type: str) -> tuple[str, bytes]:
    return _parts(raw, content_type, 1)[0]


async def _body(request) -> bytes:
    """Тело потоком до BODY_MAX."""
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > BODY_MAX:
            raise DocumentError(f"файл больше {DOC_MAX >> 20} МиБ", 413)
    return bytes(raw)


async def read_upload(request) -> tuple[str, bytes]:
    """(имя файла, байты) единственного поля `file`; тело читается потоком до BODY_MAX."""
    return await asyncio.to_thread(_parse, await _body(request), request.headers.get("content-type", ""))


async def read_uploads(request, max_files: int) -> list[tuple[str, bytes]]:
    """(имя, байты) всех частей `file` по порядку: от 1 до max_files, вместе не больше DOC_MAX, тело —
    до BODY_MAX; отказы — DocumentError."""
    return await asyncio.to_thread(_parts, await _body(request), request.headers.get("content-type", ""), max_files)


# ---- текст -------------------------------------------------------------------

def pdf_page_texts(data: bytes) -> list[str]:
    """Текст слоя pypdf каждой страницы (пустой — "") до предела TEXT_MAX_CHARS на все страницы:
    гигабайты текста по многим страницам в память не идут."""
    return pdf_pages(data, lambda page, text: text)


def pdf_pages(data: bytes, probe) -> list:
    """`probe(страница pypdf, её текст)` каждой страницы по порядку — с пределом и отказами pdf_page_texts."""
    pages, chars = [], 0
    try:
        for page in PdfReader(io.BytesIO(data)).pages:
            text = (page.extract_text() or "").strip()
            pages.append(probe(page, text))
            chars += len(text)
            if chars > TEXT_MAX_CHARS:
                break
    except Exception:                                 # noqa: BLE001 — pypdf падает на битом файле по-разному
        raise DocumentError(BROKEN) from None
    if chars > TEXT_MAX_CHARS:
        raise DocumentError(TOO_LONG)
    return pages


def _pdf(data: bytes) -> str:
    text = "\n\n".join(page for page in pdf_page_texts(data) if page)
    if not text:
        raise DocumentError(SCAN)
    return text


def _docx(data: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            info = z.getinfo("word/document.xml")
            if info.file_size > XML_MAX:
                raise DocumentError(BROKEN)
            root = ElementTree.fromstring(z.read(info))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, OSError, ValueError, EOFError) as e:
        if isinstance(e, DocumentError):
            raise
        raise DocumentError(BROKEN) from None
    paragraphs = ("".join(t.text or "" for t in p.iter(f"{W}t")) for p in root.iter(f"{W}p"))
    return "\n".join(paragraphs)


def _plain(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1251", errors="replace")


def extract(name: str, data: bytes) -> str:
    """Текст документа по расширению имени; отказ — DocumentError."""
    suffix = PurePosixPath(name.lower()).suffix
    if suffix not in SUFFIXES:
        raise DocumentError(UNSUPPORTED)
    text = _pdf(data) if suffix == ".pdf" else _docx(data) if suffix == ".docx" else _plain(data)
    if len(text) > TEXT_MAX_CHARS:
        raise DocumentError(TOO_LONG)
    return text
