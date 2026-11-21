"""Короткая озвучка «Разговора» (п. 13 спеки 2026-11-21): POST /api/speech/say — фраза до speeches.SAY_MAX
знаков звуком умолчания «Озвучки» через API-подключение; ничего не сохраняется — ни в историю, ни в журнал."""
import asyncio
import logging

import speech
import state
from conftest import add_gateway, make_user, signin
from fakes import WAV_FAKE
from http_checks import on_http
from test_speeches import voice


def say(s, text):
    return s.c.post("/api/speech/say", json={"text": text})


def test_say_speaks_with_the_speech_default_and_keeps_nothing(studio, fake_audio, caplog):
    caplog.set_level(logging.DEBUG)
    add_gateway(studio)
    voice(studio, fake_audio)
    studio.gw.tts_requests.clear()
    tables = ("speeches", "media", "search_docs")                   # в media уже лежит образец голоса
    before = {t: state.DB.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables}
    r = say(studio, "**Секретная** фраза разговора.")
    assert r.status_code == 200, r.text
    assert r.content == WAV_FAKE and r.headers["content-type"].startswith("audio/wav")
    assert r.headers["cache-control"] == "no-store"
    request, = studio.gw.tts_requests
    assert request.url.path == "/speech/tts-qwen"                  # умолчание «Озвучки» шлюза
    assert "Секретная фраза разговора.".encode() in request.content and b"**" not in request.content
    assert "текст образца".encode() in request.content          # голос — как у «Прочитать»
    assert {t: state.DB.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in tables} == before
    assert "Секретная" not in caplog.text and "разговора" not in caplog.text
    assert say(studio, "я" * 1001).status_code == 422
    assert say(studio, "  ").status_code == 422


def test_say_is_one_request_per_account(studio, fake_audio, monkeypatch):
    add_gateway(studio)
    voice(studio, fake_audio)
    started, release = asyncio.Event(), asyncio.Event()

    async def slow(conn, model, text, voice_, language=None):
        started.set()
        await release.wait()
        return WAV_FAKE, "audio/wav"

    monkeypatch.setattr(speech, "say", slow)

    async def scenario(client):
        first = asyncio.create_task(client.post("/api/speech/say", json={"text": "Первая."}))
        waiting = asyncio.create_task(started.wait())
        await asyncio.wait([first, waiting], return_when=asyncio.FIRST_COMPLETED)   # отказ первой — не висеть
        waiting.cancel()
        second = await client.post("/api/speech/say", json={"text": "Вторая."})
        release.set()
        return await first, second

    first, second = on_http(studio, scenario)
    assert first.status_code == 200
    assert second.status_code == 429 and second.json()["detail"]["code"] == "busy"
    assert say(studio, "Третья.").status_code == 200                # первая кончилась — снова можно


def test_say_with_mcp_default_returns_say_mcp(studio):
    make_user(studio, "ann")
    signin(studio, "ann")
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'ann'").fetchone()[0]
    state.DB.execute("INSERT INTO mcp_servers(user_id, name, url, secret_enc, enabled)"
                     " VALUES (?, 'spark', 'http://10.77.1.15:4000/mcp', x'00', 1)", (uid,))
    r = say(studio, "Текст")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "say_mcp"
    assert state.DB.execute("SELECT count(*) FROM speeches").fetchone()[0] == 0
