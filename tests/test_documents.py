"""Текст документов чата (app/documents.py): PDF, DOCX, TXT и MD; отказы — словами."""
import asyncio
import io
import unicodedata
import zipfile

import httpx
import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

import documents
from documents import DocumentError


def pdf(*pages: str) -> bytes:
    """PDF со страницей текста (Helvetica, ASCII) на каждую строку `pages`; без строк — пустая страница."""
    w = PdfWriter()
    for text in pages or (None,):
        page = w.add_blank_page(612, 792)
        if text is None:
            continue
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


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def docx(*paragraphs: list[str], xml: str | None = None) -> bytes:
    """DOCX — zip с word/document.xml: абзац — список кусков текста (отдельные w:r)."""
    body = "".join("<w:p>" + "".join(f'<w:r><w:t xml:space="preserve">{t}</w:t></w:r>' for t in p) + "</w:p>"
                   for p in paragraphs)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", xml if xml is not None else
                   f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>')
    return out.getvalue()


NFD = unicodedata.normalize("NFD", "Чайковский")      # «й» из архива macOS: «и» и U+0306


def test_names_and_texts_are_composed_to_nfc(monkeypatch):
    """Поиск: FTS5 с remove_diacritics сводит разложенное «й» к «и», а составное «й» запроса — нет; имя и текст
    документа поэтому пишутся в NFC (находка 2026-11-09 «Поиск не находит текст в NFD»)."""
    assert NFD != "Чайковский"
    assert documents.clean_name(f"папка/{NFD}.txt") == "Чайковский.txt"
    assert documents.extract("a.txt", f"{NFD} и Шопен".encode("utf-8")) == "Чайковский и Шопен"
    assert documents.extract("a.docx", docx([NFD])) == "Чайковский"

    class Page:
        def extract_text(self):
            return f" {NFD} "

    monkeypatch.setattr(documents, "PdfReader", lambda _: type("R", (), {"pages": [Page(), Page()]})())
    assert documents.pdf_page_texts(b"%PDF") == ["Чайковский", "Чайковский"]
    assert documents.extract("a.pdf", b"%PDF") == "Чайковский\n\nЧайковский"


def refusal(name, data) -> str:
    with pytest.raises(DocumentError) as e:
        documents.extract(name, data)
    return str(e.value)


def test_pdf_docx_txt_md_are_extracted():
    assert documents.extract("договор.pdf", pdf("Payment is due in 30 days", "Second page")) == \
        "Payment is due in 30 days\n\nSecond page"
    assert documents.extract("Записка.DOCX", docx(["Первый", " абзац"], ["Второй"])) == "Первый абзац\nВторой"
    assert documents.extract("notes.txt", "Привет, мир\nвторая".encode("utf-8")) == "Привет, мир\nвторая"
    assert documents.extract("readme.md", "﻿# Заголовок".encode("utf-8")) == "# Заголовок"   # без BOM


def test_cp1251_text_is_decoded():
    assert documents.extract("старый.txt", "Счёт на оплату №5".encode("cp1251")) == "Счёт на оплату №5"


def test_scanned_pdf_is_explained():
    assert refusal("скан.pdf", pdf()) == "в PDF нет текстового слоя — это скан"


def test_unknown_or_broken_files_are_refused():
    assert refusal("program.exe", b"MZ...") == "поддерживаются PDF, DOCX, TXT и MD"
    assert refusal("no-suffix", b"text") == "поддерживаются PDF, DOCX, TXT и MD"
    broken = "документ повреждён"
    assert refusal("x.pdf", b"garbage, not a pdf") == broken
    assert refusal("x.pdf", pdf("Cut")[:200]) == broken
    assert refusal("x.docx", b"not a zip") == broken
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr("other.xml", "<a/>")
    assert refusal("x.docx", out.getvalue()) == broken                 # zip без word/document.xml
    assert refusal("x.docx", docx(xml="<w:document><unclosed")) == broken
    assert refusal("big.txt", b"a" * (documents.TEXT_MAX_CHARS + 1)) == "текст длиннее 2 млн знаков"
    assert len(documents.extract("edge.txt", b"a" * documents.TEXT_MAX_CHARS)) == documents.TEXT_MAX_CHARS


def test_pdf_extraction_stops_once_the_text_is_too_long(monkeypatch):
    """Гигабайты текста по многим страницам не копятся в памяти: страницы читаются до предела (ревью A2-M7)."""
    from pypdf import PageObject
    calls, real = [], PageObject.extract_text

    def counted(self, *args, **kwargs):
        calls.append(1)
        return real(self, *args, **kwargs)

    monkeypatch.setattr(PageObject, "extract_text", counted)
    monkeypatch.setattr(documents, "TEXT_MAX_CHARS", 30)
    assert refusal("long.pdf", pdf(*["a" * 20] * 6)) == documents.TOO_LONG
    assert len(calls) == 2
    calls.clear()
    assert documents.extract("fits.pdf", pdf("a" * 14, "b" * 14)) == "a" * 14 + "\n\n" + "b" * 14   # 30 — ровно предел


def test_docx_xml_is_limited_to_sixteen_mebibytes():
    """document.xml разворачивается в ElementTree примерно вдесятеро: 64 МиБ ×2 загрузки — у MemoryMax=2G."""
    head = f'<?xml version="1.0" encoding="UTF-8"?><w:document xmlns:w="{W}"><w:body>'
    tail = "<w:p><w:r><w:t>x</w:t></w:r></w:p></w:body></w:document>"
    fits = head + " " * (16 * 1024 * 1024 - len(head) - len(tail)) + tail
    assert documents.extract("fits.docx", docx(xml=fits)) == "x"
    assert refusal("over.docx", docx(xml=head + " " * (16 * 1024 * 1024) + tail)) == "документ повреждён"


class Upload:
    """Запрос с телом multipart, как его отдаёт Starlette: `headers` и поток `stream()`."""

    def __init__(self, files):
        built = httpx.Request("POST", "http://studio.test/", files=files)
        self.body, self.headers = built.read(), {"content-type": built.headers["content-type"]}

    async def stream(self):
        for n in range(0, len(self.body), 65536):
            yield self.body[n:n + 65536]


def uploads(files, max_files=3):
    return asyncio.run(documents.read_uploads(Upload(files), max_files))


def upload_refusal(files, max_files=3) -> tuple[int, str]:
    with pytest.raises(DocumentError) as e:
        uploads(files, max_files)
    return e.value.status, str(e.value)


def test_read_uploads_takes_several_files_in_order_within_the_limits(monkeypatch):
    got = uploads([("file", ("a/b.jpg", b"one", "image/jpeg")), ("file", ("c.png", b"two", "image/png"))])
    assert got == [("b.jpg", b"one"), ("c.png", b"two")]
    assert upload_refusal([("file", (f"{n}.png", b"x", "image/png")) for n in range(4)]) == (
        422, "больше 3 файлов за раз")
    assert upload_refusal([("file", ("a.png", b"x", "image/png")), ("other", ("b.png", b"y", "image/png"))])[0] == 400
    assert upload_refusal([("file", ("a.png", b"", "image/png"))])[0] == 413
    monkeypatch.setattr(documents, "DOC_MAX", 5)
    status, message = upload_refusal([("file", ("a.png", b"xxx", "image/png")), ("file", ("b.png", b"yyy", "image/png"))])
    assert status == 413 and "вместе" in message
