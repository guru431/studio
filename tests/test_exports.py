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
