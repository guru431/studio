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
заголовком «Страница N»; в txt Markdown модели — простым текстом (`md_plain`: таблицы — строками с
табуляцией), в md — как есть, в docx — структурой (`blocks`, `_Doc`): заголовки — стилями Heading, списки —
нумерацией Word, таблицы — `w:tbl` с объединёнными ячейками (`w:gridSpan`, `w:vMerge`). Таблицы документа
(`tables`) — ещё книгой `xlsx` (лист на таблицу) и `tables_csv`. Двуязычный перевод — `pairs_docx`.

Язык выгрузки (`Lang`) — язык интерфейса устройства: страница передаёт его запросом (`?lang=`, у чата — поле
`lang`), без него — русский (скрипты, прежние страницы). Переводятся только слова Studio: подписи выгрузки
(`ENGLISH`, `word`) и сохранённые в базе слова сервера — причина нераспознанной страницы, ошибка ответа и шага чата,
имя шага моста (`server_word`, как страница — `i18n_words.message`); имена собеседников, заданные пользователем,
тексты и имена файлов остаются как есть."""
import csv
import html
import io
import math
import re
import zipfile
from typing import Literal
from xml.sax.saxutils import escape

import i18n_words

SRT_MAX_SEC = 7
SRT_MAX_CHARS = 84               # две строки по 42
SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")    # Unicode Cc
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TYPES = {"txt": "text/plain; charset=utf-8", "srt": "application/x-subrip; charset=utf-8", "docx": DOCX_MIME,
         "md": "text/markdown; charset=utf-8", "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
         "csv": "text/csv; charset=utf-8", "zip": "application/zip"}

CONTENT_TYPES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                 '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                 '<Default Extension="xml" ContentType="application/xml"/>'
                 '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                 'wordprocessingml.document.main+xml"/>'
                 '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                 'wordprocessingml.styles+xml"/>'
                 '<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                 'wordprocessingml.numbering+xml"/></Types>')
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"
RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{_REL}officeDocument" Target="word/document.xml"/></Relationships>')
DOCUMENT_RELS = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 f'<Relationship Id="rId1" Type="{_REL}styles" Target="styles.xml"/>'
                 f'<Relationship Id="rId2" Type="{_REL}numbering" Target="numbering.xml"/></Relationships>')
_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
#: стили абзацев: Normal и встроенные «heading 1…6» (Word узнаёт их по имени: навигация, оглавление)
STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          f'<w:styles {_W}><w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/>'
          '</w:style>' + "".join(
              f'<w:style w:type="paragraph" w:styleId="Heading{n}"><w:name w:val="heading {n}"/>'
              '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/>'
              f'<w:spacing w:before="240" w:after="80"/><w:outlineLvl w:val="{n - 1}"/></w:pPr>'
              f'<w:rPr><w:b/><w:sz w:val="{size}"/></w:rPr></w:style>'
              for n, size in enumerate((32, 28, 26, 24, 22, 22), 1)) + '</w:styles>')

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
    "Исходник": "Source",
    "Таблица {}": "Table {}",
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
    "Навык: {}": "Skill: {}",
}
_LABEL = re.compile(r"Собеседник (\d+)")       # speech.LABEL


def word(text: str, lang: str, *values) -> str:
    """Слова Studio в выгрузке на языке `lang`; `values` — подстановки `{}` (пользовательское — как есть)."""
    return (ENGLISH[text] if lang == "en" else text).format(*values)


def server_word(text: str, lang: str) -> str:
    """Слова сервера или моста из базы (отказ, причина) на языке `lang` — так же, как их переводит страница."""
    return i18n_words.message(text) if lang == "en" else text


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


def _transcript_text(seg: dict, lang: str) -> str:
    return server_word(seg["text"], lang) if seg.get("system") else seg["text"]


def txt(segments: list[dict], lang: Lang = "ru") -> str:
    return "\n".join(f"{_head(s, lang)}\n{_transcript_text(s, lang)}\n" for s in segments)


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
        spans = [(r["start"], r["end"], _line(r["text"])) for r in own] or [
            (seg["start"], seg["end"], _line(_transcript_text(seg, lang)))]
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
        body += [_paragraph(_head(s, lang), bold=True), _paragraph(_transcript_text(s, lang))]
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


def _tags(name: str) -> tuple["re.Pattern", "re.Pattern"]:
    return re.compile(rf"<{name}\b", re.I), re.compile(rf"</{name}\s*>", re.I)


_TABLE, _CAPTION, _TR, _CELL = _tags("table"), _tags("caption"), _tags("tr"), _tags("t[dh]")


def _elements(tags: tuple["re.Pattern", "re.Pattern"], text: str) -> list[tuple[int, int, str, str]]:
    """Элементы `<имя атрибуты>тело</имя>` по порядку: начало, конец, атрибуты, тело до ближайшего закрывающего тега —
    как `<имя\\b([^>]*)>(.*?)</имя\\s*>`, но за один проход. Выражение на повторённом открывающем теге без `>` или без
    закрывающего искало бы конец с каждого начала — квадратично (FXg G1); здесь нет `>` или конца после одного начала —
    нет и после следующих."""
    opening, closing = tags
    out, pos = [], 0
    while m := opening.search(text, pos):
        gt = text.find(">", m.end())
        end = closing.search(text, gt + 1) if gt >= 0 else None
        if not end:
            break
        out.append((m.start(), end.end(), text[m.end():gt], text[gt + 1:end.start()]))
        pos = end.end()
    return out


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


#: ячейка таблицы: текст, colspan, rowspan, заголовок (`th`, первая строка таблицы Markdown)
Cell = tuple[str, int, int, bool]


def _html_table(body: str, clean) -> tuple[list[str], list[list[Cell]]]:
    """Тело `<table>` — подписи (`caption`) и строки ячеек; `clean` — текст ячейки и подписи из её HTML."""
    rows = [[(clean(inner), _span(attrs, "colspan"), _span(attrs, "rowspan"), row[start + 2] in "hH")
             for start, _, attrs, inner in _elements(_CELL, row)] for *_, row in _elements(_TR, body)]
    return [clean(inner) for *_, inner in _elements(_CAPTION, body)], rows


def _grid(rows: list[list[Cell]]) -> list[list[tuple]]:
    """Строки ячеек — сетка без сдвигов: место — `("cell", ячейка)` у её левого верхнего угла, `("up", ячейка)` —
    первый столбец ячейки сверху (rowspan), `("covered",)` — прочие места объединённой ячейки, `None` — пусто.
    Ячейки ниже и правее объединённой не съезжают; все строки — одной ширины. Ячейка, чей `colspan` налезает на
    столбец, занятый `rowspan` сверху (кривой HTML модели), укорачивается до него."""
    grid: list[list[tuple]] = []
    below: dict[int, list] = {}      # столбец → [сколько ещё строк занимает ячейка сверху, ячейка, первый ли столбец]
    for row in rows:
        out: list = []

        def fill_spanned() -> None:
            while below.get(len(out)):
                left, cell, first = below[len(out)]
                if left == 1:
                    del below[len(out)]
                else:
                    below[len(out)][0] -= 1
                out.append(("up", cell) if first else ("covered",))

        for cell in row:
            fill_spanned()
            span = 1                 # colspan до столбца, занятого сверху: объединения не пересекаются
            while span < cell[1] and not below.get(len(out) + span):
                span += 1
            cell = cell if span == cell[1] else (cell[0], span, *cell[2:])
            for k in range(cell[1]):
                if cell[2] > 1:
                    below[len(out)] = [cell[2] - 1, cell, k == 0]
                out.append(("cell", cell) if k == 0 else ("covered",))
        while any(c >= len(out) for c in below):
            if below.get(len(out)):
                fill_spanned()
            else:
                out.append(None)
        grid.append(out)
    width = max(map(len, grid), default=0)
    return [r + [None] * (width - len(r)) for r in grid]


def _texts(grid: list[list[tuple]]) -> list[list[str]]:
    """Сетка — тексты: ячейка — в левом верхнем углу, объединённые места — пустые."""
    return [[slot[1][0] if slot and slot[0] == "cell" else "" for slot in row] for row in grid]


def _table(body: str) -> str:
    """Таблица HTML — строки ячеек через табуляцию; объединённые ячейки (`colspan`, `rowspan`) — пустые места
    по сетке, чтобы ячейки ниже и правее не съезжали; подпись (`caption`) — строкой над таблицей."""
    captions, rows = _html_table(body, _flat)
    lines = captions + ["\t".join(r).rstrip("\t") for r in _texts(_grid(rows))]
    return "\n" + "\n".join(lines) + "\n"


def _tables(text: str) -> str:
    out, pos = [], 0
    for start, end, _, body in _elements(_TABLE, text):
        out += [text[pos:start], _table(body)]
        pos = end
    return "".join(out) + text[pos:]


def _pipe_lines(lines: list[str]) -> list[bool]:
    """Строки таблиц Markdown (`| a | b |` и строка `|---|---|` под заголовком)."""
    table = [False] * len(lines)
    for i, line in enumerate(lines):
        if i and _PIPE_RULE.match(line) and "|" in lines[i - 1]:
            table[i - 1] = table[i] = True
            j = i + 1
            while j < len(lines) and "|" in lines[j] and lines[j].strip():
                table[j] = True
                j += 1
    return table


def _pipe_cells(line: str) -> list[str]:
    return [c.strip().replace("\\|", "|") for c in _PIPE.split(line.strip().removeprefix("|").removesuffix("|"))]


def _pipes(text: str) -> str:
    """Таблица Markdown — ячейки через табуляцию."""
    lines = text.split("\n")
    out = []
    for line, inside in zip(lines, _pipe_lines(lines)):
        if not inside:
            out.append(line)
        elif not _PIPE_RULE.match(line):
            out.append("\t".join(_pipe_cells(line)).rstrip("\t"))
    return "\n".join(out)


_CELL_END = re.compile(r"</t[dh]\s*>\s*", re.I)
_ROW_END = re.compile(r"</tr\s*>\s*", re.I)
_LOOSE_OPEN = re.compile(r"<(?:table|thead|tbody|tfoot|tr|t[dh])\b", re.I)
_SPACES = re.compile(r"\s*")


def _loose(text: str) -> str:
    """Теги таблицы вне целой `<table>` (обрывок куска перевода): ячейки — табуляцией, строки — переносом.
    Пробелы перед закрывающим тегом снимает `rstrip`, а не `\\s*` в начале выражения: оно перебирало бы длинный
    пробельный ряд с каждой его позиции — квадратично (20 000 пробелов — полсекунды). Открывающий тег с пробелами
    за ним снимается проходом, как в `_elements`: `<td\\b[^>]*>` искал бы `>` с каждого повторённого `<td ` (FXg G1)."""
    out, pos = [], 0
    while (m := _LOOSE_OPEN.search(text, pos)) and (gt := text.find(">", m.end())) >= 0:
        out.append(text[pos:m.start()])
        pos = _SPACES.match(text, gt + 1).end()
    text = "".join(out) + text[pos:]
    parts = _CELL_END.split(text)
    text = "\t".join([part.rstrip() for part in parts[:-1]] + parts[-1:])
    parts = _ROW_END.split(text)
    return "\n".join([part.rstrip("\t") for part in parts[:-1]] + parts[-1:])


def md_plain(md: str) -> str:
    """Markdown модели — простым текстом: таблица HTML и Markdown — строки ячеек через табуляцию (объединённые
    ячейки — пустыми местами, `_table`), индексы `sup`/`sub` — знаками индекса, прочие теги убраны (`<br>` —
    перенос), сущности раскрыты; знаки заголовков, `**`, `__` и `` ` `` сняты."""
    text = _loose(_pipes(_tables(_SCRIPT.sub(_script, md))))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = html.unescape(HTML_TAG.sub("", text))
    text = re.sub(r"^[ \t]{0,3}#{1,6}[ \t]+", "", text, flags=re.M)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip("\n ")       # табуляция — пустая первая ячейка, не пробел


# ---- структура для docx, xlsx и csv (аудит 05:I4): заголовки, списки, таблицы с объединёнными ячейками ----

#: хвост «## Заголовок ##» снимает `_heading_text`, не выражение: `(.*?)(?:[ \t]+#+)?$` перебирало бы пробелы квадратично
_HEADING = re.compile(r"^[ \t]{0,3}(#{1,6})[ \t]+(.*)$")
_HTML_HEADING = re.compile(r"^\s*<h([1-6])\b[^>]*>(.*?)</h\1\s*>\s*$", re.I)
_ITEM = re.compile(r"^([ \t]*)(?:([-*+•])|(\d{1,9})([.)]))[ \t]+(.*)$")


def _heading_text(text: str) -> str:
    text = text.rstrip()
    bare = text.rstrip("#")
    return bare.rstrip() if bare != text and (not bare or bare[-1] in " \t") else text


def _inline(text: str) -> str:
    """Текст заголовка, пункта, ячейки: индексы — знаками, теги сняты, сущности раскрыты, `**`, `__`, `` ` `` сняты."""
    text = html.unescape(HTML_TAG.sub("", _SCRIPT.sub(_script, text)))
    return text.replace("**", "").replace("__", "").replace("`", "").strip(" \r\n")    # табуляция — пустая ячейка


def _text_blocks(text: str) -> list[tuple]:
    text = _loose(re.sub(r"<br\s*/?>", "\n", text, flags=re.I))
    lines = text.split("\n")
    pipes = _pipe_lines(lines)
    out: list[tuple] = []
    for i, line in enumerate(lines):
        if pipes[i]:
            if _PIPE_RULE.match(line):
                continue
            row = [(_inline(c), 1, 1, i + 1 < len(lines) and pipes[i + 1] and bool(_PIPE_RULE.match(lines[i + 1])))
                   for c in _pipe_cells(line)]
            if i and pipes[i - 1] and out and out[-1][0] == "table":
                out[-1][1].append(row)
            else:
                out.append(("table", [row]))
            continue
        heading = _HTML_HEADING.match(line) or _HEADING.match(line)
        item = None if heading else _ITEM.match(line)
        if heading:
            level, value = (int(heading[1]) if heading[1].isdigit() else len(heading[1])), _inline(_heading_text(heading[2]))
            block = ("h", level, value)
        elif item:
            value = _inline(item[5])
            numbered = (int(item[3]), item[4]) if item[3] else None
            block = ("li", numbered, min(len(item[1].expandtabs(4)) // 2, 8), value)
        else:
            value = _inline(line)
            block = ("p", value)
        if value.strip():
            out.append(block)
    return out


def blocks(md: str) -> list[tuple]:
    """Markdown распознавания — блоки: `("h", уровень, текст)`, `("li", (номер, "." | ")") | None, уровень, текст)`,
    `("table", строки ячеек Cell)`, `("p", текст)` (ячейки строки таблицы вне целой `<table>` — через табуляцию).
    Таблицы — HTML (как пишет HunyuanOCR, с `colspan`/`rowspan`) и Markdown; подпись таблицы — абзацем над ней."""
    out: list[tuple] = []
    pos = 0
    for start, end, _, body in _elements(_TABLE, md):
        out += _text_blocks(md[pos:start])
        captions, rows = _html_table(body, lambda cell: _inline(_flat(cell)))
        out += [("p", c) for c in captions if c]
        if any(rows):
            out.append(("table", [r for r in rows if r]))
        pos = end
    return out + _text_blocks(md[pos:])


def tables(pages: list[dict]) -> list[list[list[tuple]]]:
    """Таблицы распознанных страниц по порядку — сетками (`_grid`)."""
    return [_grid(b[1]) for p in pages if p["state"] in ("done", "layer") and p["text"]
            for b in blocks(p["text"]) if b[0] == "table"]


def _page_body(page: dict, convert, lang: str) -> str:
    if page["state"] in ("done", "layer"):
        return convert(page["text"] or "")
    if page["state"] == "blank":
        return word("[пустая страница]", lang)
    return (word("[не распознана: {}]", lang, server_word(page["detail"], lang)) if page["detail"]
            else word("[не распознана]", lang))


def pages_txt(pages: list[dict], lang: Lang = "ru") -> str:
    return "\n\n".join(word("Страница {}", lang, p["n"]) + "\n\n" + _page_body(p, md_plain, lang) for p in pages) + "\n"


def pages_md(pages: list[dict], lang: Lang = "ru") -> str:
    return "\n\n".join("## " + word("Страница {}", lang, p["n"]) + "\n\n" + _page_body(p, str, lang) for p in pages) + "\n"


def _cells(line: str, rtl: bool = False, style: str = "", num: "tuple[int, int] | None" = None,
           bold: bool = False) -> str:
    """Абзац DOCX: ячейки строки таблицы — через `<w:tab/>`; `style` — стиль абзаца (`Heading1`…), `num` — пункт
    списка (`w:numId`, уровень); `rtl` — абзац справа налево (`w:bidi`, `w:rtl`)."""
    runs = "<w:tab/>".join(f'<w:t xml:space="preserve">{escape(CONTROL.sub(" ", cell))}</w:t>'
                           for cell in line.split("\t"))
    para = ((f'<w:pStyle w:val="{style}"/>' if style else "")
            + (f'<w:numPr><w:ilvl w:val="{num[1]}"/><w:numId w:val="{num[0]}"/></w:numPr>' if num else "")
            + ("<w:bidi/>" if rtl else ""))
    run = ("<w:b/>" if bold else "") + ("<w:rtl/>" if rtl else "")
    return ((f"<w:p><w:pPr>{para}</w:pPr>" if para else "<w:p>")
            + (f"<w:r><w:rPr>{run}</w:rPr>" if run else "<w:r>") + f"{runs}</w:r></w:p>")


TEXT_WIDTH = 9638                # ширина текста A4 с полями по 2 см, твипы: столбцы таблицы делят её поровну
BORDERS = "".join(f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="auto"/>'
                  for side in ("top", "left", "bottom", "right", "insideH", "insideV"))


class _Doc:
    """Тело DOCX из блоков (`blocks`): заголовки — стилями Heading, списки — нумерацией Word, таблицы — `w:tbl` с
    `w:gridSpan`/`w:vMerge`. Нумерованный пункт продолжает список, только если его номер — следующий на том же
    уровне: иначе — новый `w:num` со своим началом, номера документа остаются на местах."""

    def __init__(self, rtl: bool = False):
        self.rtl = rtl
        self.nums: list[tuple[int, int, int]] = []      # (abstractNum, уровень, начало) для w:num 2, 3, …
        self.open: dict[tuple[int, str], tuple[int, int]] = {}   # (уровень, знак) → (numId, следующий номер)

    def render(self, items: list[tuple], rtl: "bool | None" = None) -> list[str]:
        rtl = self.rtl if rtl is None else rtl
        out = []
        for b in items:
            if b[0] != "li" or b[1] is None:
                self.open = {k: v for k, v in self.open.items() if b[0] == "li" and k[0] < b[2]}
            if b[0] == "h":
                out.append(_cells(b[2], rtl, style=f"Heading{b[1]}"))
            elif b[0] == "li":
                out.append(_cells(b[3], rtl, num=(self._number(b[1], b[2]) if b[1] else 1, b[2])))
            elif b[0] == "table":
                if out and out[-1].startswith("<w:tbl>"):
                    out.append("<w:p/>")             # таблицы подряд Word склеил бы в одну
                out.append(self.table(_grid(b[1]), rtl))
            else:
                out.append(_cells(b[1], rtl))
        return out

    def _number(self, numbered: tuple[int, str], level: int) -> int:
        value, mark = numbered
        self.open = {k: v for k, v in self.open.items() if k[0] <= level}
        known = self.open.get((level, mark))
        if known is None or known[1] != value:
            self.nums.append((1 if mark == "." else 2, level, value))
            known = (len(self.nums) + 1, value)
        self.open[(level, mark)] = (known[0], value + 1)
        return known[0]

    def table(self, grid: list[list[tuple]], rtl: bool, header: bool = False, cells=None) -> str:
        """Таблица из сетки (`_grid`); `cells(ячейка, столбец)` — XML содержимого ячейки (по умолчанию — абзац,
        заголовочная — жирным); `header` — первая строка повторяется на каждой странице."""
        width = len(grid[0]) if grid else 1
        col = TEXT_WIDTH // width
        rows = []
        for n, row in enumerate(grid):
            tcs = []
            for k, slot in enumerate(row):
                if slot and slot[0] == "covered":
                    continue
                cell = slot[1] if slot else ("", 1, 1, False)
                props = (f'<w:tcW w:w="{col * cell[1]}" w:type="dxa"/>'
                         + (f'<w:gridSpan w:val="{cell[1]}"/>' if cell[1] > 1 else "")
                         + ('<w:vMerge w:val="restart"/>' if slot and slot[0] == "cell" and cell[2] > 1 else "")
                         + ("<w:vMerge/>" if slot and slot[0] == "up" else ""))
                if not slot or slot[0] != "cell":
                    inner = "<w:p/>"
                else:
                    inner = cells(cell, k) if cells else _cells(cell[0], rtl, bold=cell[3])
                tcs.append(f"<w:tc><w:tcPr>{props}</w:tcPr>{inner}</w:tc>")
            head = "<w:trPr><w:tblHeader/></w:trPr>" if header and n == 0 else ""
            rows.append(f"<w:tr>{head}{''.join(tcs)}</w:tr>")
        cols = f'<w:gridCol w:w="{col}"/>' * width
        return (f'<w:tbl><w:tblPr>{"<w:bidiVisual/>" if rtl else ""}<w:tblW w:w="0" w:type="auto"/>'
                f"<w:tblBorders>{BORDERS}</w:tblBorders></w:tblPr>"
                f'<w:tblGrid>{cols}</w:tblGrid>{"".join(rows)}</w:tbl>')

    def cell(self, text: str, markdown: bool, rtl: bool) -> str:
        """Содержимое ячейки двуязычной таблицы: блоки Markdown или абзац на строку; ячейка кончается абзацем."""
        items = blocks(text) if markdown else [("p", line) for line in text.split("\n") if line.strip()]
        out = "".join(self.render(items, rtl))
        return out if out.endswith("</w:p>") else out + "<w:p/>"


def pages_docx(pages: list[dict], title: str, lang: Lang = "ru") -> bytes:
    """Страницы с заголовком «Страница N»: распознанное — блоками (`_Doc`), пометки страниц — абзацем."""
    doc = _Doc()
    body = [_paragraph(title, bold=True, size=32)]
    for p in pages:
        body.append(_paragraph(word("Страница {}", lang, p["n"]), bold=True))
        if p["state"] in ("done", "layer"):
            body += doc.render(blocks(p["text"] or ""))
        else:
            body.append(_cells(_page_body(p, str, lang)))
    return _package(body, doc.nums)


def text_docx(text: str, title: str, markdown: bool = True, rtl: bool = False) -> bytes:
    """Перевод (спека 2026-10-28-studio-translator-design, 6.2): заголовок, затем текст — как pages_docx без
    страниц. Markdown распознавания (`markdown`) — блоками (`_Doc`: заголовки, списки, таблицы); простой текст —
    абзац на строку как есть: `List<int>` и обратные кавычки остаются. `rtl` — перевод на язык письма справа
    налево (`translate_text.RTL`): абзацы и таблицы текста — справа налево."""
    doc = _Doc(rtl)
    items = blocks(text) if markdown else [("p", line) for line in text.split("\n") if line.strip()]
    return _package([_paragraph(title, bold=True, size=32), *doc.render(items)], doc.nums)


def pairs_docx(pairs: list[tuple[str, str]], title: str, heads: tuple[str, str], markdown: bool = True,
               rtl: tuple[bool, bool] = (False, False)) -> bytes:
    """Двуязычный перевод (аудит 05:I5): заголовок и таблица в два столбца — кусок исходника и его перевод строкой,
    шапка (`heads`) повторяется на каждой странице; `rtl` — письмо справа налево у исходника и у перевода."""
    doc = _Doc()
    grid = [[("cell", (h, 1, 1, True)) for h in heads]]
    grid += [[("cell", (source, 1, 1, False)), ("cell", (text, 1, 1, False))] for source, text in pairs]

    def cell(c: Cell, col: int) -> str:
        return _cells(c[0], rtl[col], bold=True) if c[3] else doc.cell(c[0], markdown, rtl[col])

    return _package([_paragraph(title, bold=True, size=32), doc.table(grid, False, header=True, cells=cell)], doc.nums)


def _package(body: list[str], nums: "list[tuple[int, int, int]]" = ()) -> bytes:
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f'<w:body>{"".join(body)}</w:body></w:document>')
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/_rels/document.xml.rels", DOCUMENT_RELS)
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", STYLES)
        z.writestr("word/numbering.xml", _numbering(nums))
    return out.getvalue()


def _levels(fmt: str, text) -> str:
    return "".join(f'<w:lvl w:ilvl="{k}"><w:start w:val="1"/><w:numFmt w:val="{fmt}"/><w:lvlText w:val="{text(k)}"/>'
                   f'<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="{720 * (k + 1)}" w:hanging="360"/></w:pPr></w:lvl>'
                   for k in range(9))


def _numbering(nums: "list[tuple[int, int, int]]") -> str:
    """Списки: `w:num` 1 — маркеры; 2, 3, … — нумерованные отрезки (`_Doc.nums`) со своим началом на своём уровне.
    Номер пункта — только своего уровня («5.», «5)»), как в распознанном тексте."""
    abstract = (f'<w:abstractNum w:abstractNumId="0">{_levels("bullet", lambda k: "•◦▪"[k % 3])}</w:abstractNum>'
                f'<w:abstractNum w:abstractNumId="1">{_levels("decimal", lambda k: f"%{k + 1}.")}</w:abstractNum>'
                f'<w:abstractNum w:abstractNumId="2">{_levels("decimal", lambda k: f"%{k + 1})")}</w:abstractNum>')
    own = "".join(f'<w:num w:numId="{n}"><w:abstractNumId w:val="{a}"/><w:lvlOverride w:ilvl="{level}">'
                  f'<w:startOverride w:val="{start}"/></w:lvlOverride></w:num>'
                  for n, (a, level, start) in enumerate(nums, 2))
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:numbering {_W}>{abstract}'
            f'<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>{own}</w:numbering>')


# ---- таблицы распознанного документа — книга .xlsx (лист на таблицу) и .csv (аудит 05:I4) ----

_SHEET = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NUMBER = re.compile(r"-?(?:0|[1-9]\d{0,14})(?:\.\d+)?")     # число — числом; «007», «1 250,00» — текстом


def _column(n: int) -> str:
    """0 → A, 25 → Z, 26 → AA."""
    out = ""
    n += 1
    while n:
        n, rest = divmod(n - 1, 26)
        out = chr(65 + rest) + out
    return out


def _sheet(grid: list[list[tuple]]) -> str:
    rows, merges = [], []
    for r, (texts, slots) in enumerate(zip(_texts(grid), grid), 1):
        cells = []
        for c, (text, slot) in enumerate(zip(texts, slots)):
            ref = f"{_column(c)}{r}"
            if slot and slot[0] == "cell" and (slot[1][1] > 1 or slot[1][2] > 1):
                merges.append(f'<mergeCell ref="{ref}:{_column(c + slot[1][1] - 1)}{r + slot[1][2] - 1}"/>')
            text = CONTROL.sub(" ", text)
            if _NUMBER.fullmatch(text):
                cells.append(f'<c r="{ref}"><v>{text}</v></c>')
            elif text:
                cells.append(f'<c r="{ref}" t="inlineStr"><is><t xml:space="preserve">{escape(text)}</t></is></c>')
        rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    merged = f'<mergeCells count="{len(merges)}">{"".join(merges)}</mergeCells>' if merges else ""
    return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="{_SHEET}">'
            f'<sheetData>{"".join(rows)}</sheetData>{merged}</worksheet>')


def xlsx(grids: list[list[list[tuple]]], lang: Lang = "ru") -> bytes:
    """Книга без новых зависимостей: лист «Таблица N» на таблицу (`tables`), объединённые ячейки — `mergeCells`."""
    sheet_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
    types = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
             '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
             '<Default Extension="xml" ContentType="application/xml"/>'
             '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.'
             'spreadsheetml.sheet.main+xml"/>'
             + "".join(f'<Override PartName="/xl/worksheets/sheet{n}.xml" ContentType="{sheet_type}"/>'
                       for n in range(1, len(grids) + 1)) + "</Types>")
    rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f'<Relationship Id="rId1" Type="{_REL}officeDocument" Target="xl/workbook.xml"/></Relationships>')
    sheets = "".join(f'<sheet name="{escape(word("Таблица {}", lang, n))}" sheetId="{n}" r:id="rId{n}"/>'
                     for n in range(1, len(grids) + 1))
    book = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<workbook xmlns="{_SHEET}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            f"<sheets>{sheets}</sheets></workbook>")
    book_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                 '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                 + "".join(f'<Relationship Id="rId{n}" Type="{_REL}worksheet" Target="worksheets/sheet{n}.xml"/>'
                           for n in range(1, len(grids) + 1)) + "</Relationships>")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", types)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", book)
        z.writestr("xl/_rels/workbook.xml.rels", book_rels)
        for n, grid in enumerate(grids, 1):
            z.writestr(f"xl/worksheets/sheet{n}.xml", _sheet(grid))
    return out.getvalue()


def _csv(grid: list[list[tuple]]) -> bytes:
    """Таблица CSV (RFC 4180, CRLF), UTF-8 с BOM — Excel иначе читает кириллицу в кодировке системы;
    объединённые ячейки — текст в левой верхней, остальные места пустые (как txt)."""
    out = io.StringIO()
    csv.writer(out, lineterminator="\r\n").writerows([CONTROL.sub(" ", t) for t in row] for row in _texts(grid))
    return ("﻿" + out.getvalue()).encode("utf-8")


def tables_csv(grids: list[list[list[tuple]]], stem: str) -> tuple[bytes, str]:
    """Таблицы CSV: одна — файл «<имя>.csv», несколько — ZIP «<имя>.csv.zip» с «<имя>-N.csv» (в CSV листов нет,
    а книга с листами — уже xlsx). Итог — (содержимое, имя файла)."""
    if len(grids) == 1:
        return _csv(grids[0]), f"{stem}.csv"
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for n, grid in enumerate(grids, 1):
            z.writestr(f"{stem}-{n}.csv", _csv(grid))
    return out.getvalue(), f"{stem}.csv.zip"
