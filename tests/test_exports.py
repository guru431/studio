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
