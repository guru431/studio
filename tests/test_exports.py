"""Выгрузки расшифровки (app/exports.py): txt, srt, docx без новых зависимостей; маршрут выгрузки."""
import io
import zipfile
from urllib.parse import quote
from xml.etree import ElementTree

import exports
from conftest import add_gateway, make_user, signin
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
        "Абзац жирно2 H2O \nзаметка")


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
    assert paragraphs[-1] == [(f"{W}t", "a<b"), (f"{W}tab", None), (f"{W}t", "c d")]


def test_text_docx_has_a_title_and_a_paragraph_per_line():
    text = "## Договор\n\n**Срок** — 30 дней.\n<table><tr><td>a&lt;b</td><td>c\x0bd</td></tr></table>"
    with zipfile.ZipFile(io.BytesIO(exports.text_docx(text, "Договор & Co"))) as z:
        root = ElementTree.fromstring(z.read("word/document.xml"))
    paragraphs = [[(t.tag, t.text) for t in p.iter() if t.tag in (f"{W}t", f"{W}tab")] for p in root.iter(f"{W}p")]
    assert paragraphs == [[(f"{W}t", "Договор & Co")], [(f"{W}t", "Договор")], [(f"{W}t", "Срок — 30 дней.")],
                          [(f"{W}t", "a<b"), (f"{W}tab", None), (f"{W}t", "c d")]]


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
