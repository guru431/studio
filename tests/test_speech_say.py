"""Короткая озвучка «Разговора» (п. 13 спеки 2026-11-21): POST /api/speech/say — фраза до speeches.SAY_MAX
знаков звуком умолчания «Озвучки» через API-подключение; ничего не сохраняется — ни в историю, ни в журнал."""
import asyncio
import json
import logging

import httpx

import speech
import speeches
import state
import upstream
from conftest import add_connection, add_gateway, make_user, signin
from fakes import WAV_FAKE
from http_checks import on_http
from test_speeches import voice


def say(s, text):
    return s.c.post("/api/speech/say", json={"text": text})


def external_tts(s, monkeypatch) -> tuple[int, list]:
    """Умолчание «Озвучки» — внешний OpenAI-совместимый сервис `tts-1`; список его запросов `/v1/audio/speech`."""
    add_gateway(s)
    state.DB.execute("UPDATE settings SET value = '[\"10.77.1.15:4000\", \"voice.cloud.test:443\"]' WHERE key = 'allowlist'")
    async def resolve(host, port): return ["93.184.216.34"]
    monkeypatch.setattr(upstream, "RESOLVE", resolve)
    cid = add_connection(s, base_url="https://voice.cloud.test/v1", token="k-1", sections=["speech"])
    s.c.put(f"/api/sections/speech/links/{cid}", json={"models": ["tts-1"]})
    assert s.c.put("/api/sections/speech/default", json={"connection_id": cid, "model": "tts-1"}).status_code == 200
    seen = []
    async def speak(q):
        seen.append(q)
        return httpx.Response(200, content=b"ID3mp3", headers={"content-type": "audio/mpeg"})
    s.gw.on["/v1/audio/speech"] = speak
    return cid, seen


def test_say_of_a_new_account_uses_the_first_service_voice(studio, monkeypatch):
    """Внешний сервис без прежней озвучки — первый голос speeches.SERVICE_VOICES; s06 — по-прежнему 409 `voice`."""
    cid, seen = external_tts(studio, monkeypatch)
    r = say(studio, "First phrase")
    assert r.status_code == 200, r.text
    assert r.content == b"ID3mp3" and r.headers["content-type"].startswith("audio/mpeg")
    request, = seen
    assert json.loads(request.content) == {"model": "tts-1", "input": "First phrase", "voice": speeches.SERVICE_VOICES[0],
                                           "response_format": "mp3"}
    state.DB.execute("UPDATE connections SET s06 = 1 WHERE id = ?", (cid,))
    r = say(studio, "Second phrase")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "voice" and len(seen) == 1


def test_page_service_voices_match_the_server():
    """Подсказка голоса сервиса на странице (speech.js `SERVICE_VOICES`) — тот же список, что умолчание сервера."""
    import re
    from pathlib import Path
    source = (Path(__file__).resolve().parents[1] / "app/static/js/speech.js").read_text(encoding="utf-8")
    block = re.search(r"const SERVICE_VOICES = \[(.*?)\];", source, re.S)
    assert block, "нет SERVICE_VOICES в speech.js"
    assert tuple(re.findall(r"'([^']*)'", block.group(1))) == speeches.SERVICE_VOICES


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
    assert say(studio, "A standalone English sentence.").status_code == 200
    assert b'name="language"\r\n\r\nEnglish' in studio.gw.tts_requests[-1].content
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
