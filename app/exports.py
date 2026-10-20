"""Выгрузки расшифровки (спека 2026-10-20-studio-sections-voice-design, 6.3): txt — реплики с
именами и временем, srt — субтитры по репликам, docx — то же в Word.

docx — минимальный OOXML без новых зависимостей: три части zip (`[Content_Types].xml`, `_rels/.rels`,
`word/document.xml`), заголовок — имя записи, на реплику — строка «Собеседник N · мм:сс» жирным и
абзац текста. Текст экранируется (xml.sax.saxutils): в расшифровке бывают `<`, `>` и `&`.
Сегмент — словарь `{"speaker", "start", "end", "text"}` (итог transcripts.py)."""
import io
import zipfile
from xml.sax.saxutils import escape

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TYPES = {"txt": "text/plain; charset=utf-8", "srt": "application/x-subrip; charset=utf-8", "docx": DOCX_MIME}

CONTENT_TYPES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                 '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                 '<Default Extension="xml" ContentType="application/xml"/>'
                 '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                 'wordprocessingml.document.main+xml"/></Types>')
RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
        'officeDocument" Target="word/document.xml"/></Relationships>')


def stamp(seconds: float) -> str:
    """мм:сс, с часом — ч:мм:сс."""
    s = max(0, int(seconds))
    h, rest = divmod(s, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60:02d}:{rest % 60:02d}"


def _head(seg: dict) -> str:
    return f"{seg['speaker']} · {stamp(seg['start'])}" if seg.get("speaker") else stamp(seg["start"])


def txt(segments: list[dict]) -> str:
    return "\n".join(f"{_head(s)}\n{s['text']}\n" for s in segments)


def _srt_time(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    sec, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def srt(segments: list[dict]) -> str:
    blocks = []
    for n, s in enumerate(segments, 1):
        end = s["end"] if s["end"] > s["start"] else s["start"] + 1   # у внешнего сервиса времени может не быть
        text = f"{s['speaker']}: {s['text']}" if s.get("speaker") else s["text"]
        blocks.append(f"{n}\n{_srt_time(s['start'])} --> {_srt_time(end)}\n{text}\n")
    return "\n".join(blocks)


def _paragraph(text: str, bold: bool = False, size: "int | None" = None) -> str:
    props = ("<w:b/>" if bold else "") + (f'<w:sz w:val="{size}"/>' if size else "")
    run_props = f"<w:rPr>{props}</w:rPr>" if props else ""
    return f'<w:p><w:r>{run_props}<w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


def docx(segments: list[dict], title: str) -> bytes:
    body = [_paragraph(title, bold=True, size=32)]
    for s in segments:
        body += [_paragraph(_head(s), bold=True), _paragraph(s["text"])]
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f'<w:body>{"".join(body)}</w:body></w:document>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/document.xml", document)
    return out.getvalue()
