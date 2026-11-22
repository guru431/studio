"""«Прочитать» у ответа чата, результата «Перевода» и текста «Сканов» (п. 12, 28 спеки 2026-11-21):
POST /api/speeches/read — текст без разметки, источник — умолчание «Озвучки», голос — последний с этой моделью."""
import json
import logging

import speeches
import state
from conftest import add_gateway, make_user, signin
from test_speech_say import external_tts
from test_speeches import voice, wait_sp


def second_voice(s) -> str:
    r = s.c.post("/api/voices", data={"name": "Второй", "text": "другой образец"},
                 files={"audio": ("v.m4a", b"\x00" * 512, "audio/mp4")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def read(s, text, kind="chat", oid="m1", **extra):
    return s.c.post("/api/speeches/read", json={"text": text, "origin": {"kind": kind, "id": oid}, **extra})


def test_read_from_chat_answer_strips_markdown_and_uses_default_source(studio, fake_audio, caplog):
    caplog.set_level(logging.DEBUG)
    add_gateway(studio)
    other = voice(studio, fake_audio)                      # «Мой»
    first = second_voice(studio)                           # «Второй» — первый по алфавиту, как в выборе страницы
    # ни одной озвучки — первый доступный голос
    r = read(studio, "**Жирный** ответ\n\n# Заголовок")
    assert r.status_code == 202, r.text
    row = state.DB.execute("SELECT * FROM speeches WHERE chat_id = ?", (r.json()["chat_id"],)).fetchone()
    assert row["text"] == "Жирный ответ\n\nЗаголовок" and row["voice"] == first and row["model"] == "qwen3-tts"
    wait_sp(studio, row["id"], {"done"})
    # «Прочитать» берёт голос предыдущей озвучки; язык подбирается по текущему тексту.
    later = studio.c.post("/api/speeches", json={"text": "Привет.", "voice": other, "language": "en"}).json()
    wait_sp(studio, later["id"], {"done"})
    r = read(studio, "`код` и текст", kind="translate", oid="t1")
    row = state.DB.execute("SELECT * FROM speeches WHERE chat_id = ?", (r.json()["chat_id"],)).fetchone()
    assert row["text"] == "код и текст" and row["voice"] == other and row["language"] == "ru"
    assert read(studio, "Скан", kind="ocr", oid="s1").status_code == 202
    assert read(studio, "Текст", kind="mail").status_code == 422
    assert read(studio, "  ").status_code == 422
    assert "Жирный" not in caplog.text and "Заголовок" not in caplog.text


def test_read_of_a_new_account_uses_the_first_service_voice(studio, fake_audio, monkeypatch):
    """Внешний сервис: ни одной озвучки — первый голос speeches.SERVICE_VOICES; потом — голос последней готовой."""
    cid, seen = external_tts(studio, monkeypatch)
    r = read(studio, f"{'А' * 3000}.\n\n{'Б' * 3000}.")              # длиннее speeches.EXTERNAL_MAX — две записи
    assert r.status_code == 202, r.text
    rows = state.DB.execute("SELECT * FROM speeches WHERE chat_id = ? ORDER BY created", (r.json()["chat_id"],)).fetchall()
    assert len(rows) == 2
    assert {(x["voice"], x["voice_name"], x["model"], x["language"]) for x in rows} == {
        (speeches.SERVICE_VOICES[0], speeches.SERVICE_VOICES[0], "tts-1", None)}
    for x in rows:
        wait_sp(studio, x["id"], {"done"})
    assert [json.loads(q.content)["voice"] for q in seen] == [speeches.SERVICE_VOICES[0]] * 2
    studio.clock[0] += 1                                                 # позже второй части чата
    later =studio.c.post("/api/speeches", json={"text": "Hello.", "voice": "nova", "connection_id": cid, "model": "tts-1"})
    wait_sp(studio, later.json()["id"], {"done"})
    r = read(studio, "Текст")
    row = state.DB.execute("SELECT voice FROM speeches WHERE chat_id = ?", (r.json()["chat_id"],)).fetchone()
    assert row["voice"] == "nova"


def test_read_without_voices_says_where_to_add_one(studio, fake_audio):
    add_gateway(studio)
    r = read(studio, "Текст")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "voice"


def test_read_with_mcp_default_returns_read_mcp(studio):
    make_user(studio, "ann")
    signin(studio, "ann")
    r = read(studio, "Текст")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "no_model"
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'ann'").fetchone()[0]
    state.DB.execute("INSERT INTO mcp_servers(user_id, name, url, secret_enc, enabled)"
                     " VALUES (?, 'spark', 'http://10.77.1.15:4000/mcp', x'00', 1)", (uid,))
    r = read(studio, "Текст")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "read_mcp"
    assert state.DB.execute("SELECT count(*) FROM speeches").fetchone()[0] == 0
