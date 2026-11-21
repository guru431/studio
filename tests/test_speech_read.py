"""«Прочитать» у ответа чата, результата «Перевода» и текста «Сканов» (п. 12, 28 спеки 2026-11-21):
POST /api/speeches/read — текст без разметки, источник — умолчание «Озвучки», голос — последний с этой моделью."""
import logging

import state
from conftest import add_gateway, make_user, signin
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
    first = voice(studio, fake_audio)
    other = second_voice(studio)
    # ни одной озвучки — первый доступный голос
    r = read(studio, "**Жирный** ответ\n\n# Заголовок")
    assert r.status_code == 202, r.text
    row = state.DB.execute("SELECT * FROM speeches WHERE chat_id = ?", (r.json()["chat_id"],)).fetchone()
    assert row["text"] == "Жирный ответ\n\nЗаголовок" and row["voice"] == first and row["model"] == "qwen3-tts"
    wait_sp(studio, row["id"], {"done"})
    # последняя озвучка этой моделью — другим голосом и по-английски: «Прочитать» берёт её голос и язык
    later = studio.c.post("/api/speeches", json={"text": "Привет.", "voice": other, "language": "en"}).json()
    wait_sp(studio, later["id"], {"done"})
    r = read(studio, "`код` и текст", kind="translate", oid="t1")
    row = state.DB.execute("SELECT * FROM speeches WHERE chat_id = ?", (r.json()["chat_id"],)).fetchone()
    assert row["text"] == "код и текст" and row["voice"] == other and row["language"] == "en"
    assert read(studio, "Скан", kind="ocr", oid="s1").status_code == 202
    assert read(studio, "Текст", kind="mail").status_code == 422
    assert read(studio, "  ").status_code == 422
    assert "Жирный" not in caplog.text and "Заголовок" not in caplog.text


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
