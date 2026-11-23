"""Выгрузки расшифровки (app/exports.py): txt, srt, docx без новых зависимостей; маршрут выгрузки."""
import ast
import io
import json
import re
import zipfile
from urllib.parse import quote
from xml.etree import ElementTree

import exports
from conftest import APP, add_gateway, make_user, signin
from test_transcripts import upload, wait_tr

SEGMENTS = [{"speaker": "Собеседник 1", "start": 0.9, "end": 3.1, "text": "Бюро <переводов> & Co."},
            {"speaker": "Собеседник 2", "start": 65.2, "end": 70.0, "text": "У меня вопрос."}]
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def test_txt_lists_speakers_and_times():
    assert exports.txt(SEGMENTS) == ("Собеседник 1 · 00:00\nБюро <переводов> & Co.\n\n"
                                     "Собеседник 2 · 01:05\nУ меня вопрос.\n")
    assert exports.txt([{"speaker": None, "start": 0, "end": 1, "text": "Раз."}]) == "00:00\nРаз.\n"


def test_srt_numbers_and_times_the_replicas():
    assert exports.srt(SEGMENTS) == ("1\n00:00:00,900 --> 00:00:03,100\nСобеседник 1: Бюро <переводов> & Co.\n\n"
                                     "2\n00:01:05,200 --> 00:01:10,000\nСобеседник 2: У меня вопрос.\n")
    assert exports.srt([{"speaker": None, "start": 0, "end": 0, "text": "Раз."}]) == "1\n00:00:00,000 --> 00:00:01,000\nРаз.\n"


def cues(text: str) -> list:
    """Субтитры SRT: (начало, конец, текст) в секундах."""
    def sec(stamp):
        h, m, rest = stamp.split(":")
        s, ms = rest.split(",")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000
    out = []
    for block in text.strip().split("\n\n"):
        _, times, body = block.split("\n", 2)
        start, end = times.split(" --> ")
        out.append((sec(start), sec(end), body))
    return out


def within_limits(got: list) -> None:
    for n, (start, end, body) in enumerate(got):
        assert end - start <= exports.SRT_MAX_SEC + 1e-9 and len(body) <= exports.SRT_MAX_CHARS, got[n]
        assert end > start, got[n]
        if n:
            assert start >= got[n - 1][1], got[n - 1:n + 1]


def test_srt_follows_the_recognized_segments_of_an_unedited_replica():
    turns = [{"speaker": "Иван", "start": 0.0, "end": 9.0, "text": "Раз два. Три четыре.", "edited": False},
             {"speaker": "Собеседник 2", "start": 9.0, "end": 12.0, "text": "Правлено целиком.", "edited": True}]
    raw = [{"start": 0.0, "end": 4.0, "speaker": "Собеседник 1", "text": "Раз два.", "turn": 0},
           {"start": 4.0, "end": 9.0, "speaker": "Собеседник 1", "text": "Три четыре.", "turn": 0},
           {"start": 9.0, "end": 12.0, "speaker": "Собеседник 2", "text": "Исходное.", "turn": 1}]
    assert cues(exports.srt(turns, raw)) == [(0.0, 4.0, "Иван: Раз два."), (4.0, 9.0, "Три четыре."),
                                             (9.0, 12.0, "Собеседник 2: Правлено целиком.")]


def test_srt_splits_a_long_replica_within_the_limits():
    words = [f"слово{n}" for n in range(90)]
    text = " ".join(words[:40]) + ". " + " ".join(words[40:]) + "."
    got = cues(exports.srt([{"speaker": "Собеседник 1", "start": 10.0, "end": 70.0, "text": text}]))
    within_limits(got)
    assert got[0][0] == 10.0 and got[-1][1] <= 70.0 and len(got) >= 9          # 60 с — не меньше девяти по 7 с
    assert " ".join(b for _, _, b in got) == "Собеседник 1: " + text
    assert all(not b.startswith("Собеседник") for _, _, b in got[1:])
    long_word = cues(exports.srt([{"speaker": None, "start": 0, "end": 5, "text": "я" * 200}]))
    within_limits(long_word)
    assert "".join(b for _, _, b in long_word) == "я" * 200


def test_srt_caps_a_short_word_on_a_long_replica_and_times_without_duration():
    assert cues(exports.srt([{"speaker": None, "start": 0, "end": 20, "text": "Да."}])) == [(0.0, 7.0, "Да.")]
    got = cues(exports.srt([{"speaker": None, "start": 0, "end": 0, "text": "Раз."},
                            {"speaker": None, "start": 0, "end": 0, "text": "Два."}]))
    assert got == [(0.0, 1.0, "Раз."), (1.0, 2.0, "Два.")]
    got = cues(exports.srt([{"speaker": None, "start": 0, "end": 0, "text": "Раз."},
                            {"speaker": None, "start": 0.4, "end": 2, "text": "Два."}]))
    assert got == [(0.0, 0.4, "Раз."), (0.4, 2.0, "Два.")]


def test_docx_is_a_valid_word_file_with_the_text_escaped():
    data = exports.docx(SEGMENTS, "Звонок Ивану")
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml"} <= set(z.namelist())
        root = ElementTree.fromstring(z.read("word/document.xml"))
    texts = [t.text for t in root.iter(f"{W}t")]
    assert texts[0] == "Звонок Ивану" and "Собеседник 2 · 01:05" in texts and "Бюро <переводов> & Co." in texts


def test_control_characters_break_neither_srt_nor_docx():
    """Пустая строка внутри субтитра — для плеера его конец; \\x0b в XML делает DOCX недействительным."""
    segs = [{"speaker": "Иван\x0b", "start": 0.0, "end": 3.0, "text": "Раз.\n\nДва\x0bтри\x00.", "edited": True}]
    assert cues(exports.srt(segs)) == [(0.0, 3.0, "Иван: Раз. Два три .")]
    with zipfile.ZipFile(io.BytesIO(exports.docx(segs, "Звонок\x01"))) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    assert [t.text for t in root.iter(f"{W}t")] == ["Звонок ", "Иван  · 00:00", "Раз.  Два три ."]


def test_md_plain_turns_tables_into_tab_rows_and_drops_the_marks():
    md = ("## Итоги\n\n**Важно:** `код` и __подчёркнуто__\n\n<table>\n<tr>\n<th>Товар</th>\n<th>Цена</th>\n</tr>\n"
          "<tr><td>Хлеб &amp; соль</td><td colspan=\"2\"> 50 </td></tr>\n</table>\n\nКонец<br>строки")
    assert exports.md_plain(md) == "Итоги\n\nВажно: код и подчёркнуто\n\nТовар\tЦена\nХлеб & соль\t50\n\nКонец\nстроки"


def test_md_plain_keeps_comparisons_formulas_and_generics_but_drops_real_tags():
    """Находка 2026-11-09: `<[^>]+>` резал всё от голого «<» до ближайшего «>», через абзацы."""
    md = ("Возраст пациента <18 лет — педиатрия.\n\nДавление >140 — гипертония.\n\n"
          "Формула $x<5$ и $y>3$, a < b > c, x<y и z>w.\n\nТип `List<int>` и Map<String, Integer>.\n\n"
          "<p align=\"center\">Абзац</p> <b>жирно</b><sup>2</sup> H<sub>2</sub>O <img src=\"x.png\"/>"
          "<div class=\"note\">\nзаметка\n</div>")
    assert exports.md_plain(md) == (
        "Возраст пациента <18 лет — педиатрия.\n\nДавление >140 — гипертония.\n\n"
        "Формула $x<5$ и $y>3$, a < b > c, x<y и z>w.\n\nТип List<int> и Map<String, Integer>.\n\n"
        "Абзац жирно² H₂O \nзаметка")


def test_md_plain_keeps_merged_cells_in_place_and_reads_pipe_tables():
    """Аудит 05:F9: `rowspan`/`colspan` сдвигали ячейки, `caption` и индексы терялись, таблица Markdown
    оставалась с палочками."""
    md = ("<table><caption>Цены</caption><tr><th rowspan=\"2\">Товар</th><th colspan=\"2\">Цена</th></tr>"
          "<tr><th>опт</th><th>розница</th></tr><tr><td>Хлеб<br>белый</td><td>40</td><td>50</td></tr></table>"
          "Площадь 12 m<sup>2</sup>, x<sup>a+b</sup>, CO<sub>2</sub>, a<sub>ij</sub>.\n\n"
          "| Товар | Цена |\n|:---|---:|\n| Хлеб \\| батон | 50 |\n\nКонец | не таблица")
    assert exports.md_plain(md) == (
        "Цены\nТовар\tЦена\n\tопт\tрозница\nХлеб белый\t40\t50\n"
        "Площадь 12 m², x^(a+b), CO₂, a_(ij).\n\nТовар\tЦена\nХлеб | батон\t50\n\nКонец | не таблица")
    rows = exports.md_plain('<table><tr><td rowspan="3">A</td><td>1</td></tr><tr><td>2</td></tr>'
                            '<tr><td>3</td></tr><tr><td colspan="99999">x</td><td>y</td></tr></table>')
    assert rows.split("\n")[:3] == ["A\t1", "\t2", "\t3"]
    assert rows.split("\n")[3] == "x" + "\t" * exports.SPAN_MAX + "y"


PAGES = [{"n": 1, "state": "done", "text": "# Заголовок\n\nТекст", "detail": None},
         {"n": 2, "state": "layer", "text": "Слой", "detail": None},
         {"n": 3, "state": "failed", "text": None, "detail": "на странице не найден текст"},
         {"n": 4, "state": "blank", "text": None, "detail": None}]


def test_pages_txt_and_md_mark_every_page():
    assert exports.pages_txt(PAGES) == ("Страница 1\n\nЗаголовок\n\nТекст\n\nСтраница 2\n\nСлой\n\n"
                                        "Страница 3\n\n[не распознана: на странице не найден текст]\n\n"
                                        "Страница 4\n\n[пустая страница]\n")
    assert exports.pages_md(PAGES) == ("## Страница 1\n\n# Заголовок\n\nТекст\n\n## Страница 2\n\nСлой\n\n"
                                       "## Страница 3\n\n[не распознана: на странице не найден текст]\n\n"
                                       "## Страница 4\n\n[пустая страница]\n")
    assert exports.TYPES["md"] == "text/markdown; charset=utf-8"


def test_pages_docx_is_a_valid_word_file():
    pages = PAGES + [{"n": 5, "state": "done", "text": "<table><tr><td>a&lt;b</td><td>c\x0bd</td></tr></table>",
                      "detail": None}]
    with zipfile.ZipFile(io.BytesIO(exports.pages_docx(pages, "Скан & Co"))) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    paragraphs = [[(t.tag, t.text) for t in p.iter() if t.tag in (f"{W}t", f"{W}tab")] for p in root.iter(f"{W}p")]
    assert paragraphs[:4] == [[(f"{W}t", "Скан & Co")], [(f"{W}t", "Страница 1")], [(f"{W}t", "Заголовок")],
                              [(f"{W}t", "Текст")]]
    assert [(f"{W}t", "[пустая страница]")] in paragraphs
    assert cells(root) == [[["a<b"], ["c d"]]]                     # таблица — таблицей Word, не табуляцией


def test_text_docx_has_a_title_and_a_paragraph_per_line():
    text = "## Договор\n\n**Срок** — 30 дней.\n<table><tr><td>a&lt;b</td><td>c\x0bd</td></tr></table>"
    with zipfile.ZipFile(io.BytesIO(exports.text_docx(text, "Договор & Co"))) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    paragraphs = [[(t.tag, t.text) for t in p.iter() if t.tag in (f"{W}t", f"{W}tab")] for p in root.iter(f"{W}p")]
    assert paragraphs == [[(f"{W}t", "Договор & Co")], [(f"{W}t", "Договор")], [(f"{W}t", "Срок — 30 дней.")],
                          [(f"{W}t", "a<b")], [(f"{W}t", "c d")]]
    assert styles(root)[1] == "Heading2" and cells(root) == [[["a<b"], ["c d"]]]


def cells(root) -> list:
    """Строки таблиц документа: ячейка — список её абзацев (текстом)."""
    return [[["".join(t.text or "" for t in p.iter(f"{W}t")) for p in tc.iter(f"{W}p")]
             for tc in tr.findall(f"{W}tc")] for tr in root.iter(f"{W}tr")]


def styles(root) -> list:
    """Стиль каждого абзаца документа («» — без стиля)."""
    return [next((s.get(f"{W}val") for s in p.iter(f"{W}pStyle")), "") for p in root.iter(f"{W}p")]


def docx_parts(data: bytes) -> dict:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return {name: ElementTree.fromstring(z.read(name)) for name in z.namelist() if name.endswith((".xml", ".rels"))}


def test_docx_keeps_merged_cells_on_the_grid():
    """Аудит 05:F9/I4: объединённые ячейки — `gridSpan`/`vMerge` по сетке, ячейки правее и ниже не съезжают; `th` —
    жирным; подпись — абзацем над таблицей; таблица Markdown — тоже таблицей, первая строка — заголовочная."""
    md = ("<table><caption>Цены</caption><tr><th rowspan=\"2\">Товар</th><th colspan=\"2\">Цена</th></tr>"
          "<tr><th>опт</th><th>розница</th></tr><tr><td>Хлеб<br>белый</td><td>4<sup>2</sup></td><td>50</td></tr>"
          "</table>\n\n| Товар | Цена |\n|:---|---:|\n| Хлеб \\| батон | 50 |")
    root = docx_parts(exports.text_docx(md, "Т"))["word/document.xml"]
    tables = list(root.iter(f"{W}tbl"))
    assert len(tables) == 2
    assert [len(t.find(f"{W}tblGrid")) for t in tables] == [3, 2]
    first = [[(tc.find(f"{W}tcPr/{W}gridSpan"), tc.find(f"{W}tcPr/{W}vMerge"),
               "".join(x.text or "" for x in tc.iter(f"{W}t"))) for tc in tr.findall(f"{W}tc")]
             for tr in tables[0].iter(f"{W}tr")]
    assert [[c[2] for c in row] for row in first] == [["Товар", "Цена"], ["", "опт", "розница"], ["Хлеб белый", "4²", "50"]]
    assert first[0][0][1].get(f"{W}val") == "restart" and first[1][0][1].get(f"{W}val") is None
    assert first[0][1][0].get(f"{W}val") == "2" and first[2][0][0] is None
    assert tables[0].find(f".//{W}b") is not None                       # заголовочные ячейки — жирным
    assert cells(tables[1]) == [[["Товар"], ["Цена"]], [["Хлеб | батон"], ["50"]]]
    assert ["".join(t.text for t in p.iter(f"{W}t")) for p in root.findall(f"{W}body/{W}p")][1] == "Цены"


def test_docx_headings_are_styles_and_lists_keep_their_numbers():
    """Аудит 05:I4: заголовки — стилями Heading (их описание — в styles.xml), маркеры и номера — нумерацией Word;
    номер, что не продолжает список, начинает свой (`startOverride`): «7.» после «5.» не станет «6.»."""
    md = ("# Договор\n<h3>Сроки</h3>\n- первое\n  - вложенное\n* второе\n\n5. пятый\n6. шестой\n\n"
          "текст\n7) седьмой\n9) девятый\n1.1. не список")
    parts = docx_parts(exports.text_docx(md, "Т"))
    root = parts["word/document.xml"]
    assert styles(root)[1:3] == ["Heading1", "Heading3"]
    defined = {s.get(f"{W}styleId") for s in parts["word/styles.xml"].iter(f"{W}style")}
    assert {"Heading1", "Heading3"} <= defined
    nums = [(p.find(f".//{W}numId").get(f"{W}val"), p.find(f".//{W}ilvl").get(f"{W}val"),
             "".join(t.text for t in p.iter(f"{W}t"))) for p in root.iter(f"{W}p") if p.find(f".//{W}numPr") is not None]
    assert nums == [("1", "0", "первое"), ("1", "1", "вложенное"), ("1", "0", "второе"),
                    ("2", "0", "пятый"), ("2", "0", "шестой"), ("3", "0", "седьмой"), ("4", "0", "девятый")]
    numbering = parts["word/numbering.xml"]
    own = {n.get(f"{W}numId"): (n.find(f"{W}abstractNumId").get(f"{W}val"),
                                n.find(f".//{W}startOverride").get(f"{W}val"))
           for n in numbering.iter(f"{W}num") if n.find(f".//{W}startOverride") is not None}
    assert own == {"2": ("1", "5"), "3": ("2", "7"), "4": ("2", "9")}
    assert "1.1. не список" in ["".join(t.text for t in p.iter(f"{W}t")) for p in root.iter(f"{W}p")]
    rels = parts["word/_rels/document.xml.rels"]
    assert {r.get("Target") for r in rels} == {"styles.xml", "numbering.xml"}
    types = parts["[Content_Types].xml"]
    assert {"/word/styles.xml", "/word/numbering.xml"} <= {o.get("PartName") for o in types}


def test_tables_go_to_xlsx_with_merges_and_to_csv():
    """Аудит 05:I4: лист «Таблица N» на таблицу, объединение — `mergeCell`, число — числом, «007» и «1 250,00» —
    текстом; CSV — UTF-8 с BOM, одна таблица — файл, несколько — ZIP."""
    pages = [{"n": 1, "state": "done", "detail": None, "text": (
                 "<table><tr><td colspan=\"2\">Итого</td><td rowspan=\"2\">1250.5</td></tr>"
                 "<tr><td>007</td><td>1 250,00</td></tr></table>")},
             {"n": 2, "state": "failed", "detail": None, "text": None},
             {"n": 3, "state": "done", "detail": None, "text": "| a | b |\n|---|---|\n| 1 | x\x0by |"}]
    grids = exports.tables(pages)
    assert len(grids) == 2 and exports.tables(pages[1:2]) == []
    sheet = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(exports.xlsx(grids, "en"))) as z:
        book = ElementTree.fromstring(z.read("xl/workbook.xml"))
        first = ElementTree.fromstring(z.read("xl/worksheets/sheet1.xml"))
        assert "xl/worksheets/sheet2.xml" in z.namelist()
    assert [s.get("name") for s in book.iter(f"{sheet}sheet")] == ["Table 1", "Table 2"]
    assert [m.get("ref") for m in first.iter(f"{sheet}mergeCell")] == ["A1:B1", "C1:C2"]
    values = {c.get("r"): (c.get("t"), "".join(x.text or "" for x in c.iter() if x.tag in (f"{sheet}v", f"{sheet}t")))
              for c in first.iter(f"{sheet}c")}
    assert values == {"A1": ("inlineStr", "Итого"), "C1": (None, "1250.5"), "A2": ("inlineStr", "007"),
                      "B2": ("inlineStr", "1 250,00")}
    body, name = exports.tables_csv(grids[:1], "Счёт")
    assert name == "Счёт.csv" and body.decode("utf-8") == "﻿Итого,,1250.5\r\n007,\"1 250,00\",\r\n"
    body, name = exports.tables_csv(grids, "Счёт")
    with zipfile.ZipFile(io.BytesIO(body)) as z:
        assert name == "Счёт.csv.zip" and z.namelist() == ["Счёт-1.csv", "Счёт-2.csv"]
        assert z.read("Счёт-2.csv").decode("utf-8") == "﻿a,b\r\n1,x y\r\n"


def test_colspan_over_a_rowspan_from_above_stops_at_it():
    """Ревью R3-3: `colspan`, налезший на столбец, занятый `rowspan` сверху, давал пересекающиеся `mergeCell`
    (Excel — «восстановить содержимое») и сдвигал строку ниже. Ячейка укорачивается до занятого столбца."""
    grid = exports.tables([{"n": 1, "state": "done", "detail": None, "text": (
        "<table><tr><td>A</td><td rowspan=2>B</td></tr><tr><td colspan=2>C</td></tr>"
        "<tr><td>D</td><td>E</td></tr></table>")}])
    assert exports._texts(grid[0]) == [["A", "B"], ["C", ""], ["D", "E"]]
    sheet = ElementTree.fromstring(exports._sheet(grid[0]))
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    assert [m.get("ref") for m in sheet.iter(f"{ns}mergeCell")] == ["B1:B2"]


def test_pairs_docx_puts_each_source_next_to_its_translation():
    """Аудит 05:I5: таблица в два столбца — шапка на каждой странице, строка — кусок и его перевод; Markdown скана —
    структурой внутри ячейки (ячейка кончается абзацем), письмо справа налево — у своей стороны."""
    pairs = [("Hello.\nSecond line.", "שלום."), ("<table><tr><td>a</td></tr></table>", "<table><tr><td>б</td></tr></table>")]
    root = docx_parts(exports.pairs_docx(pairs, "Договор", ("Source", "Translation"), markdown=True,
                                         rtl=(False, True)))["word/document.xml"]
    outer = root.find(f"{W}body/{W}tbl")
    rows = outer.findall(f"{W}tr")
    assert rows[0].find(f"{W}trPr/{W}tblHeader") is not None
    assert cells(outer)[:2] == [[["Source"], ["Translation"]], [["Hello.", "Second line."], ["שלום."]]]
    left, right = rows[1].findall(f"{W}tc")
    assert left.find(f".//{W}bidi") is None and right.find(f".//{W}bidi") is not None
    nested = rows[2].findall(f"{W}tc")
    assert all(tc.find(f"{W}tbl") is not None and tc[-1].tag == f"{W}p" for tc in nested)


def test_export_route_names_the_file_after_the_recording(studio, fake_audio):
    add_gateway(studio)
    tid = upload(studio).json()["id"]
    wait_tr(studio, tid, {"done"})
    r = studio.c.get(f"/api/transcripts/{tid}/export?format=docx")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert quote("Звонок Ивану.docx", safe="") in r.headers["content-disposition"]
    r = studio.c.get(f"/api/transcripts/{tid}/export?format=txt")
    assert r.status_code == 200 and "Собеседник 1 · 00:00\nЗдравствуйте." in r.text
    assert studio.c.get(f"/api/transcripts/{tid}/export?format=pdf").status_code == 422
    make_user(studio, "bob"); signin(studio, "bob")
    assert studio.c.get(f"/api/transcripts/{tid}/export?format=txt").status_code == 404


def test_exports_carry_the_edits_and_the_names(studio, fake_audio):
    add_gateway(studio)
    tid = upload(studio).json()["id"]
    wait_tr(studio, tid, {"done"})
    studio.c.patch(f"/api/transcripts/{tid}", json={"texts": {"1": "Добрый вечер."}, "names": {"Собеседник 1": "Иван"}})
    txt = studio.c.get(f"/api/transcripts/{tid}/export?format=txt").text
    assert txt == "Иван · 00:00\nЗдравствуйте.\n\nСобеседник 2 · 00:03\nДобрый вечер.\n"
    with zipfile.ZipFile(io.BytesIO(studio.c.get(f"/api/transcripts/{tid}/export?format=docx").content)) as z:
        texts = [t.text for t in ElementTree.fromstring(z.read("word/document.xml")).iter(f"{W}t")]
    assert "Иван · 00:00" in texts and "Добрый вечер." in texts and "Добрый день." not in texts
    srt = studio.c.get(f"/api/transcripts/{tid}/export?format=srt").text
    assert "Иван: Здравствуйте." in srt and "Собеседник 2: Добрый вечер." in srt


def test_english_exports_translate_speaker_labels_but_not_given_names(studio, fake_audio):
    """FINDINGS 04:F16: `lang=en` — метка сервера «Собеседник N» по-английски, имя пользователя — как есть."""
    add_gateway(studio)
    tid = upload(studio).json()["id"]
    wait_tr(studio, tid, {"done"})
    studio.c.patch(f"/api/transcripts/{tid}", json={"names": {"Собеседник 1": "Иван"}})
    txt = studio.c.get(f"/api/transcripts/{tid}/export?format=txt&lang=en").text
    assert txt == "Иван · 00:00\nЗдравствуйте.\n\nSpeaker 2 · 00:03\nДобрый день.\n"
    assert "Speaker 2: Добрый день." in studio.c.get(f"/api/transcripts/{tid}/export?format=srt&lang=en").text
    with zipfile.ZipFile(io.BytesIO(studio.c.get(f"/api/transcripts/{tid}/export?format=docx&lang=en").content)) as z:
        texts = [t.text for t in ElementTree.fromstring(z.read("word/document.xml")).iter(f"{W}t")]
    assert "Иван · 00:00" in texts and "Speaker 2 · 00:03" in texts
    assert "Собеседник 2 · 00:03" in studio.c.get(f"/api/transcripts/{tid}/export?format=txt&lang=ru").text
    assert studio.c.get(f"/api/transcripts/{tid}/export?format=txt&lang=de").status_code == 422


def test_english_pages_translate_headings_and_marks_not_the_text():
    """FINDINGS 05:F10: заголовки и пометки страниц — как у «Копировать» на английском (ocr.js), текст — как есть;
    причина нераспознанной страницы — слова сервера, по-английски тем же словарём, что на странице."""
    assert exports.pages_md(PAGES, "en") == ("## Page 1\n\n# Заголовок\n\nТекст\n\n## Page 2\n\nСлой\n\n"
                                             "## Page 3\n\n[not recognized: no text found on page]\n\n"
                                             "## Page 4\n\n[empty page]\n")
    assert exports.pages_txt(PAGES, "en").startswith("Page 1\n\nЗаголовок")
    with zipfile.ZipFile(io.BytesIO(exports.pages_docx(PAGES, "Скан", "en"))) as z:
        texts = [t.text for t in ElementTree.fromstring(z.read("word/document.xml")).iter(f"{W}t")]
    assert texts[:2] == ["Скан", "Page 1"] and "[empty page]" in texts


def _english_catalog() -> dict:
    source = (APP / "static" / "en.js").read_text(encoding="utf-8")
    return json.JSONDecoder().raw_decode(source.split("Object.freeze(")[1])[0]


def test_export_words_match_the_page_and_every_word_call_is_translated():
    """Скачанный файл и страница пишут одно и то же одинаково (`exports.ENGLISH` против `app/static/en.js`), а каждый
    литерал `word("…")` в app/*.py есть в `exports.ENGLISH`: иначе английская выгрузка упала бы KeyError."""
    page = _english_catalog()
    for source, english in exports.ENGLISH.items():
        key = source.replace("{}", "{0}")
        if key in page:
            assert page[key] == english.replace("{}", "{0}"), source
        assert source.count("{}") == english.count("{}"), source
    used = set()
    for path in APP.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        if not re.search(r"\bword\(", source):           # разбор всех модулей шёл дольше секунды
            continue
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", None)) == "word":
                for arg in node.args[:1]:
                    texts = [arg] if isinstance(arg, ast.Constant) else [arg.body, arg.orelse] if isinstance(arg, ast.IfExp) else []
                    used |= {t.value for t in texts if isinstance(t, ast.Constant)}
    assert used and used <= set(exports.ENGLISH), used - set(exports.ENGLISH)
    assert set(exports.ENGLISH) - used <= {"Перевод"}           # translate.TEXT_STEM — через имя константы
