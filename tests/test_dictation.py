"""Голосовой ввод (app/dictation.py): запись → текст модели раздела «Голосовой ввод»; звук не
хранится и в журнал не попадает."""
import asyncio
import logging

import dictation
import speech
import state
from conftest import add_gateway


def test_dictation_returns_text_and_keeps_nothing(studio, caplog):
    caplog.set_level(logging.DEBUG)
    add_gateway(studio)
    studio.gw.asr_reply = {"text": "тайная диктовка"}
    media0 = state.DB.execute("SELECT count(*) FROM media").fetchone()[0]
    r = studio.c.post("/api/dictation", files={"audio": ("voice.m4a", b"\x00" * 128, "audio/mp4")})
    assert r.status_code == 200 and r.json() == {"text": "тайная диктовка"}
    assert studio.gw.asr_requests[0].url.path == "/speech/lite/transcriptions"
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == media0
    assert "тайная диктовка" not in caplog.text and "voice.m4a" not in caplog.text


def test_dictation_takes_the_type_from_the_name_when_the_browser_gives_none(studio):
    add_gateway(studio)
    r = studio.c.post("/api/dictation", files={"audio": ("voice.webm", b"\x00" * 128, "application/octet-stream")})
    assert r.status_code == 200 and b"audio/webm" in studio.gw.asr_requests[0].content


def test_dictation_refuses_non_audio_and_too_big(studio):
    add_gateway(studio)
    r = studio.c.post("/api/dictation", files={"audio": ("x.txt", b"hi", "text/plain")})
    assert r.status_code == 415 and r.json()["detail"]["code"] == "format"
    big = b"\x00" * (8 * 1024 * 1024 + 1)
    assert studio.c.post("/api/dictation", files={"audio": ("v.webm", big, "audio/webm")}).status_code == 413
    assert studio.gw.asr_requests == []


def test_dictation_that_outlasts_its_budget_is_a_word_to_retry(studio, monkeypatch, caplog):
    """Распознавание дольше BUDGET_SEC — 504 словами от службы, а не обрыв Apache (его таймаут
    маршрута больше бюджета — сверяет test_deploy) с потерянным текстом."""
    caplog.set_level(logging.DEBUG)
    add_gateway(studio)

    async def slow(*args, **kwargs):
        await asyncio.sleep(5)
        return "поздний текст"

    monkeypatch.setattr(dictation, "BUDGET_SEC", 0.05)
    monkeypatch.setattr(speech, "dictate", slow)
    r = studio.c.post("/api/dictation", files={"audio": ("voice.m4a", b"\x00" * 128, "audio/mp4")})
    assert r.status_code == 504 and r.json()["detail"]["code"] == "timeout"
    assert "ещё раз" in r.json()["detail"]["message"]
    assert "поздний текст" not in caplog.text and "voice.m4a" not in caplog.text


def whisperx(studio):
    cid = add_gateway(studio)
    r = studio.c.put("/api/sections/dictation/default", json={"connection_id": cid, "model": "whisperx"})
    assert r.status_code == 200, r.text
    return cid


def test_whisperx_frees_the_card_and_waits_for_it(studio):
    """WhisperX шлюза делит карту с картинками (аренда 600 с): голосовой ввод сначала освобождает
    модели картинок и gemma-abl, а на 503 ждёт и повторяет в пределах бюджета — как расшифровка."""
    whisperx(studio)
    studio.gw.asr_replies = [(503, {"error": "слот asr не взят: no_capacity"})] * 2
    studio.gw.asr_reply = {"text": "кот на окне"}
    r = studio.c.post("/api/dictation", files={"audio": ("v.webm", b"\x00" * 64, "audio/webm")})
    assert r.status_code == 200 and r.json() == {"text": "кот на окне"}
    paths = [q.url.path for q in studio.gw.requests]
    asr = paths.index("/speech/transcriptions")
    assert {"/slot/images/release", "/slot/images/descriptions/release"} <= set(paths[:asr])
    assert len(studio.gw.asr_requests) == 3 and studio.sleeps.count(dictation.RETRY_SEC) == 2


def test_whisperx_busy_past_the_budget_is_a_word_to_retry(studio):
    whisperx(studio)
    studio.gw.asr_replies = [(503, {"error": "занято"})] * 1000
    r = studio.c.post("/api/dictation", files={"audio": ("v.webm", b"\x00" * 64, "audio/webm")})
    assert r.status_code == 504 and r.json()["detail"]["code"] == "busy"
    assert "карта занята" in r.json()["detail"]["message"] and "ещё раз" in r.json()["detail"]["message"]
    assert sum(studio.sleeps) <= dictation.BUDGET_SEC


def test_dictation_busy_and_no_model_are_words(studio):
    cid = add_gateway(studio)
    studio.gw.asr_replies = [(503, {"error": "занято"})]
    r = studio.c.post("/api/dictation", files={"audio": ("v.webm", b"\x00" * 64, "audio/webm")})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "busy"
    studio.c.delete(f"/api/sections/dictation/links/{cid}")
    r = studio.c.post("/api/dictation", files={"audio": ("v.webm", b"\x00" * 64, "audio/webm")})
    assert r.status_code == 409 and "«Голосовой ввод»" in r.json()["detail"]["message"]
