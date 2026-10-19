"""Текст документов чата (app/documents.py): PDF, DOCX, TXT и MD; отказы — словами."""
import io
import zipfile

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
