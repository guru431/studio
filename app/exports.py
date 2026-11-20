"""Выгрузки расшифровки (спека 2026-10-20-studio-sections-voice-design, 6.3): txt — реплики с
именами и временем, srt — субтитры не длиннее SRT_MAX_SEC и SRT_MAX_CHARS (спека
2026-10-21-studio-transcript-edit-chat-design, раздел 7), docx — то же в Word.

docx — минимальный OOXML без новых зависимостей: три части zip (`[Content_Types].xml`, `_rels/.rels`,
`word/document.xml`), заголовок — имя записи, на реплику — строка «Собеседник N · мм:сс» жирным и
абзац текста. Текст экранируется (xml.sax.saxutils): в расшифровке бывают `<`, `>` и `&`.
Управляющих знаков (CONTROL) в SRT и DOCX нет: пустая строка внутри субтитра для плеера — его
конец, а `\\x0b` и прочие C0 делают XML DOCX недействительным.
Сегмент — словарь `{"speaker", "start", "end", "text"}` (итог transcripts.py).

Распознанный документ (ocr.py) — `pages_txt`, `pages_md`, `pages_docx`: страница за страницей с
заголовком «Страница N»; в txt и docx Markdown модели — простым текстом (`md_plain`: таблицы — строками
с табуляцией, в docx — `<w:tab/>`), в md — как есть.

Язык выгрузки (`Lang`) — язык интерфейса устройства: страница передаёт его запросом (`?lang=`, у чата — поле
`lang`), без него — русский (скрипты, прежние страницы). Переводятся только слова Studio (`ENGLISH`, `word`):
имена собеседников, заданные пользователем, тексты, имена файлов и причины распознавания остаются как есть."""
import html
import io
import math
import re
import zipfile
from typing import Literal
from xml.sax.saxutils import escape

SRT_MAX_SEC = 7
SRT_MAX_CHARS = 84               # две строки по 42
SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")    # Unicode Cc
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TYPES = {"txt": "text/plain; charset=utf-8", "srt": "application/x-subrip; charset=utf-8", "docx": DOCX_MIME,
         "md": "text/markdown; charset=utf-8"}

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

Lang = Literal["ru", "en"]
#: слова скачиваемых файлов (расшифровка, сканы, перевод, чат) по-английски; где страница пишет то же самое
#: (`app/static/en.js`), перевод совпадает — сверяет tests/test_exports.py
ENGLISH = {
    "Собеседник {}": "Speaker {}",
    "Страница {}": "Page {}",
    "[пустая страница]": "[empty page]",
    "[не распознана: {}]": "[not recognized: {}]",
    "[не распознана]": "[not recognized]",
    "Перевод": "Translation",
    "Чат": "Chat",
    "Пользователь": "User",
    "Помощник": "Assistant",
    "(ответ не дописан)": "(answer not finished)",
    "Ошибка: {}": "Error: {}",
    "Поиск: «{}»": "Search: “{}”",
    "Поиск": "Search",
    "Страница: {}": "Page: {}",
    "Ролик: {}": "Video: {}",
    "Инструмент": "Tool",
    "Источники ({})": "Sources ({})",
    "Вызовы инструментов ({})": "Tool calls ({})",
    "Команда": "Command",
    "Рассуждение": "Reasoning",
    "Вложение: {}": "Attachment: {}",
    "Файл": "File",
}
_LABEL = re.compile(r"Собеседник (\d+)")       # speech.LABEL


def word(text: str, lang: str, *values) -> str:
    """Слова Studio в выгрузке на языке `lang`; `values` — подстановки `{}` (пользовательское — как есть)."""
    return (ENGLISH[text] if lang == "en" else text).format(*values)


def _speaker(seg: dict, lang: str) -> str:
    """Имя собеседника: заданное пользователем — как есть, метка сервера (`label`, speech.LABEL) — на языке выгрузки."""
    found = _LABEL.fullmatch(seg["speaker"]) if seg["speaker"] == seg.get("label") else None
    return word("Собеседник {}", lang, found[1]) if found else seg["speaker"]


def stamp(seconds: float) -> str:
    """мм:сс, с часом — ч:мм:сс."""
    s = max(0, int(seconds))
    h, rest = divmod(s, 3600)
    return f"{h}:{rest // 60:02d}:{rest % 60:02d}" if h else f"{rest // 60:02d}:{rest % 60:02d}"


def _head(seg: dict, lang: str) -> str:
    return f"{_speaker(seg, lang)} · {stamp(seg['start'])}" if seg.get("speaker") else stamp(seg["start"])


def txt(segments: list[dict], lang: Lang = "ru") -> str:
    return "\n".join(f"{_head(s, lang)}\n{s['text']}\n" for s in segments)


def _line(text: str) -> str:
    """Текст субтитра — одной строкой: управляющие знаки — пробелом, пробелы схлопнуты."""
    return " ".join(CONTROL.sub(" ", text).split())


def _srt_time(seconds: float) -> str:
    ms = max(0, round(seconds * 1000))
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    sec, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def _pack(parts: list[str], limit: int) -> list[str]:
    """Соседние части через пробел, пока кусок не длиннее limit (часть длиннее — отдельным куском)."""
    out: list[str] = []
    for part in parts:
        if out and len(out[-1]) + 1 + len(part) <= limit:
            out[-1] += " " + part
        else:
            out.append(part)
    return out


def _chunks(text: str, limit: int) -> list[str]:
    """Куски текста не длиннее limit: по концам предложений, затем по словам; слово длиннее
    SRT_MAX_CHARS режется, слово длиннее limit (но не SRT_MAX_CHARS) остаётся целым."""
    pieces: list[str] = []
    for sentence in SENTENCE_END.split(text.strip()):
        if len(sentence) <= limit:
            pieces.append(sentence)
            continue
        words = [w[i:i + SRT_MAX_CHARS] for w in sentence.split() for i in range(0, len(w), SRT_MAX_CHARS)]
        pieces += _pack(words, limit)
    return _pack([p for p in pieces if p], limit)


def _pieces(start: float, end: float, text: str) -> list[tuple[float, float, str]]:
    """Отрезок времени с текстом — субтитры: длинный по времени делится мельче (кусок — около
    SRT_MAX_SEC), время куска — доля отрезка по длине текста."""
    duration = max(0.0, end - start)
    limit = SRT_MAX_CHARS
    if duration > SRT_MAX_SEC:
        limit = min(SRT_MAX_CHARS, math.ceil(len(text) / math.ceil(duration / SRT_MAX_SEC)))
    chunks = _chunks(text, limit)
    total, done, out = sum(map(len, chunks)) or 1, 0, []
    for chunk in chunks:
        out.append((start + duration * done / total, start + duration * (done + len(chunk)) / total, chunk))
        done += len(chunk)
    return out


def srt(segments: list[dict], raw: "list[dict] | None" = None, lang: Lang = "ru") -> str:
    """Субтитры не длиннее SRT_MAX_SEC и SRT_MAX_CHARS. Реплика без правки текста — по своим сегментам
    распознавания (`raw`, поле `turn`), иначе — по своему тексту; имя — у первого субтитра реплики.
    Время не убывает, субтитры не перекрываются; без длительности (внешний сервис) — 1 с, но не дальше
    начала следующего."""
    by_turn: dict[int, list[dict]] = {}
    for r in raw or ():
        by_turn.setdefault(r.get("turn"), []).append(r)
    cues: list[tuple[float, float, str]] = []
    for n, seg in enumerate(segments):
        own = [] if seg.get("edited") else by_turn.get(n, [])
        spans = [(r["start"], r["end"], _line(r["text"])) for r in own] or [(seg["start"], seg["end"], _line(seg["text"]))]
        prefix = f"{_line(_speaker(seg, lang))}: " if seg.get("speaker") else ""
        for k, (start, end, text) in enumerate(spans):
            cues += _pieces(start, end, prefix + text if k == 0 else text)
    blocks, edge = [], 0.0
    for n, (start, end, text) in enumerate(cues):
        start = max(start, edge)
        end = min(end if end > start else start + 1, start + SRT_MAX_SEC)
        if n + 1 < len(cues) and cues[n + 1][0] > start:
            end = min(end, cues[n + 1][0])
        edge = end
        blocks.append(f"{n + 1}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}\n")
    return "\n".join(blocks)


def _paragraph(text: str, bold: bool = False, size: "int | None" = None) -> str:
    props = ("<w:b/>" if bold else "") + (f'<w:sz w:val="{size}"/>' if size else "")
    run_props = f"<w:rPr>{props}</w:rPr>" if props else ""
    return f'<w:p><w:r>{run_props}<w:t xml:space="preserve">{escape(CONTROL.sub(" ", text))}</w:t></w:r></w:p>'


def docx(segments: list[dict], title: str, lang: Lang = "ru") -> bytes:
    body = [_paragraph(title, bold=True, size=32)]
    for s in segments:
        body += [_paragraph(_head(s, lang), bold=True), _paragraph(s["text"])]
    return _package(body)


# ---- распознавание (спека 2026-10-27-studio-ocr-design, 6.4): страница — {"n", "state", "text", "detail"} ----

#: Тег — только элемент HTML по имени и в пределах строки: голые «<» и «>» (`<18`, `$x<5$`, `a < b > c`,
#: `x<y и z>w`) и обобщения вроде `List<int>` — текст, а не разметка; прежнее `<[^>]+>` резало их через абзацы.
HTML_TAG = re.compile(
    r"</?(?:a|abbr|b|bdi|bdo|big|blockquote|body|br|caption|center|cite|code|col|colgroup|dd|del|dfn|div|dl|dt|em|"
    r"figcaption|figure|font|h[1-6]|head|header|footer|hr|html|i|img|ins|kbd|li|mark|math|ol|p|pre|q|s|samp|small|"
    r"span|strike|strong|sub|sup|table|tbody|td|tfoot|th|thead|tr|tt|u|ul|var|wbr)"
    r"(?:\s[^<>\n]*)?/?>", re.I)

_SUP = str.maketrans("0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ")
_SUB = str.maketrans("0123456789+-=()", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎")
_SCRIPT = re.compile(r"<(sup|sub)\b[^<>\n]*>([^<>\n]*)</\1\s*>", re.I)
_TABLE = re.compile(r"<table\b[^>]*>(.*?)</table\s*>", re.I | re.S)
_CAPTION = re.compile(r"<caption\b[^>]*>(.*?)</caption\s*>", re.I | re.S)
_TR = re.compile(r"<tr\b[^>]*>(.*?)</tr\s*>", re.I | re.S)
_CELL = re.compile(r"<t[dh]\b([^>]*)>(.*?)</t[dh]\s*>", re.I | re.S)
SPAN_MAX = 50                    # colspan/rowspan больше — разметка сломана или чужая: не раздувать текст
_PIPE_RULE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$|^\s*\|\s*:?-{3,}:?\s*\|\s*$")
_PIPE = re.compile(r"(?<!\\)\|")


def _script(m: "re.Match") -> str:
    """`m<sup>2</sup>` → «m²», `H<sub>2</sub>O` → «H₂O»; знаков индекса нет — `^(…)` / `_(…)`."""
    table, mark = (_SUP, "^") if m.group(1).lower() == "sup" else (_SUB, "_")
    inner = m.group(2).strip()
    if inner and all(ord(c) in table for c in inner):
        return inner.translate(table)
    return f"{mark}{inner}" if len(inner) <= 1 else f"{mark}({inner})"


def _span(attrs: str, name: str) -> int:
    found = re.search(rf"\b{name}\s*=\s*[\"']?(\d+)", attrs, re.I)
    return max(1, min(SPAN_MAX, int(found.group(1)))) if found else 1


def _flat(cell: str) -> str:
    """Ячейка одной строкой: перенос внутри — пробел, иначе строка таблицы распалась бы."""
    return " ".join(re.sub(r"<br\s*/?>", " ", cell, flags=re.I).split())


def _table(m: "re.Match") -> str:
    """Таблица HTML — строки ячеек через табуляцию; объединённые ячейки (`colspan`, `rowspan`) — пустые места
    по сетке, чтобы ячейки ниже и правее не съезжали; подпись (`caption`) — строкой над таблицей."""
    body = m.group(1)
    lines = [_flat(c.group(1)) for c in _CAPTION.finditer(body)]
    below: dict[int, int] = {}       # столбец → сколько ещё строк его занимает ячейка сверху (rowspan)
    for row in _TR.finditer(body):
        out: list[str] = []

        def fill_spanned() -> None:
            while below.get(len(out), 0) > 0:
                below[len(out)] -= 1
                out.append("")

        for cell in _CELL.finditer(row.group(1)):
            fill_spanned()
            cols, rows = _span(cell.group(1), "colspan"), _span(cell.group(1), "rowspan")
            for k in range(cols):
                if rows > 1:
                    below[len(out)] = rows - 1
                out.append(_flat(cell.group(2)) if k == 0 else "")
        while any(n > 0 for c, n in below.items() if c >= len(out)):
            if below.get(len(out), 0) > 0:
                fill_spanned()
            else:
                out.append("")
        lines.append("\t".join(out).rstrip("\t"))
    return "\n" + "\n".join(lines) + "\n"


def _pipes(text: str) -> str:
    """Таблица Markdown (`| a | b |` и строка `|---|---|` под заголовком) — ячейки через табуляцию."""
    lines = text.split("\n")
    table = [False] * len(lines)
    for i, line in enumerate(lines):
        if i and _PIPE_RULE.match(line) and "|" in lines[i - 1]:
            table[i - 1] = table[i] = True
            j = i + 1
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                table[j] = True
                j += 1
    out = []
    for line, inside in zip(lines, table):
        if not inside:
            out.append(line)
        elif not _PIPE_RULE.match(line):
            cells = _PIPE.split(line.strip().removeprefix("|").removesuffix("|"))
            out.append("\t".join(c.strip().replace("\\|", "|") for c in cells).rstrip("\t"))
    return "\n".join(out)


def md_plain(md: str) -> str:
    """Markdown модели — простым текстом: таблица HTML и Markdown — строки ячеек через табуляцию (объединённые
    ячейки — пустыми местами, `_table`), индексы `sup`/`sub` — знаками индекса, прочие теги убраны (`<br>` —
    перенос), сущности раскрыты; знаки заголовков, `**`, `__` и `` ` `` сняты."""
    text = _pipes(_TABLE.sub(_table, _SCRIPT.sub(_script, md)))
    text = re.sub(r"<(?:table|thead|tbody|tfoot|tr|t[dh])\b[^>]*>\s*", "", text, flags=re.I)
    text = re.sub(r"\s*</t[dh]\s*>\s*", "\t", text, flags=re.I)
    text = re.sub(r"\t*</tr\s*>\s*", "\n", text, flags=re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = html.unescape(HTML_TAG.sub("", text))
    text = re.sub(r"^[ \t]{0,3}#{1,6}[ \t]+", "", text, flags=re.M)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip("\n ")       # табуляция — пустая первая ячейка, не пробел


def _page_body(page: dict, convert, lang: str) -> str:
    if page["state"] in ("done", "layer"):
        return convert(page["text"] or "")
    if page["state"] == "blank":
        return word("[пустая страница]", lang)
    return word("[не распознана: {}]", lang, page["detail"]) if page["detail"] else word("[не распознана]", lang)


def pages_txt(pages: list[dict], lang: Lang = "ru") -> str:
    return "\n\n".join(word("Страница {}", lang, p["n"]) + "\n\n" + _page_body(p, md_plain, lang) for p in pages) + "\n"


def pages_md(pages: list[dict], lang: Lang = "ru") -> str:
    return "\n\n".join("## " + word("Страница {}", lang, p["n"]) + "\n\n" + _page_body(p, str, lang) for p in pages) + "\n"


def _cells(line: str, rtl: bool = False) -> str:
    """Абзац DOCX: ячейки строки таблицы — через `<w:tab/>`; `rtl` — абзац справа налево (`w:bidi`, `w:rtl`)."""
    runs = "<w:tab/>".join(f'<w:t xml:space="preserve">{escape(CONTROL.sub(" ", cell))}</w:t>'
                           for cell in line.split("\t"))
    if rtl:
        return f"<w:p><w:pPr><w:bidi/></w:pPr><w:r><w:rPr><w:rtl/></w:rPr>{runs}</w:r></w:p>"
    return f"<w:p><w:r>{runs}</w:r></w:p>"


def pages_docx(pages: list[dict], title: str, lang: Lang = "ru") -> bytes:
    body = [_paragraph(title, bold=True, size=32)]
    for p in pages:
        body.append(_paragraph(word("Страница {}", lang, p["n"]), bold=True))
        body += [_cells(line) for line in _page_body(p, md_plain, lang).split("\n") if line.strip()]
    return _package(body)


def text_docx(text: str, title: str, markdown: bool = True, rtl: bool = False) -> bytes:
    """Перевод (спека 2026-10-28-studio-translator-design, 6.2): заголовок, затем абзац на строку — как
    pages_docx без страниц. Markdown распознавания (`markdown`) — через `md_plain` (строки таблиц — ячейки через
    табуляцию); простой текст — как есть: `List<int>` и обратные кавычки остаются. `rtl` — перевод на язык
    письма справа налево (`translate_text.RTL`): абзацы текста — справа налево."""
    body = [_paragraph(title, bold=True, size=32)]
    body += [_cells(line, rtl) for line in (md_plain(text) if markdown else text).split("\n") if line.strip()]
    return _package(body)


def _package(body: list[str]) -> bytes:
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f'<w:body>{"".join(body)}</w:body></w:document>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/document.xml", document)
    return out.getvalue()
