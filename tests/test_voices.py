"""Голоса озвучки по образцу (app/voices.py): запись 5–15 с, её текст и имя; образец — WAV в media,
только свой; имя и текст — не в журнал."""
import asyncio
import logging

import pytest

import speech
import state
import voices
from conftest import add_gateway, make_user, signin

SAMPLE = {"audio": ("sample.m4a", b"\x00" * 2048, "audio/mp4")}


def create(s, name="Мой голос", text="Привет, это мой голос для озвучки."):
    return s.c.post("/api/voices", data={"name": name, "text": text}, files=SAMPLE)


def test_sample_is_transcribed_before_saving_and_can_be_corrected(studio, fake_audio, caplog):
    caplog.set_level(logging.DEBUG)
    add_gateway(studio)
    fake_audio.duration = 25.0
    studio.gw.asr_reply = {"text": "  тайный текст образца  "}
    r = studio.c.post("/api/voices/transcribe", files=SAMPLE)
    assert r.status_code == 200, r.text
    assert r.json() == {"text": "тайный текст образца"}
    assert ("to_wav", voices.SAMPLE_MAX) in fake_audio.calls
    sent = studio.gw.asr_requests[0]
    assert sent.url.path == "/speech/lite/transcriptions"
    assert b"audio/wav" in sent.content and b"sample.wav" in sent.content
    assert state.DB.execute("SELECT count(*) FROM voices").fetchone()[0] == 0
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 0
    assert list(state.CFG.tmp_dir.iterdir()) == []
    assert "тайный текст" not in caplog.text and "sample.m4a" not in caplog.text
    vid = create(studio, text="Исправленный текст").json()["id"]
    assert studio.c.get(f"/api/voices/{vid}").json()["text"] == "Исправленный текст"
    assert len(studio.gw.asr_requests) == 1


@pytest.mark.parametrize("text", ["", "x" * 501])
def test_unusable_asr_text_is_refused_without_saving(studio, fake_audio, text):
    add_gateway(studio)
    fake_audio.duration = 8.0
    studio.gw.asr_reply = {"text": text}
    r = studio.c.post("/api/voices/transcribe", files=SAMPLE)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "text"
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 0


def test_sample_asr_checks_audio_and_uses_the_selected_dictation_model(studio, fake_audio):
    cid = add_gateway(studio)
    fake_audio.duration = 3.0
    assert studio.c.post("/api/voices/transcribe", files=SAMPLE).json()["detail"]["code"] == "short"
    assert studio.gw.asr_requests == []
    fake_audio.duration = 8.0
    studio.c.put("/api/sections/dictation/default", json={"connection_id": cid, "model": "whisperx"})
    studio.gw.asr_replies = [(503, {"error": "занято"})]
    assert studio.c.post("/api/voices/transcribe", files=SAMPLE).status_code == 200
    assert len(studio.gw.asr_requests) == 2 and studio.sleeps
    assert studio.gw.asr_requests[0].url.path == "/speech/transcriptions"
    studio.c.delete(f"/api/sections/dictation/links/{cid}")
    assert studio.c.post("/api/voices/transcribe", files=SAMPLE).status_code == 409


def test_sample_asr_failure_and_timeout_keep_no_files(studio, fake_audio, monkeypatch):
    add_gateway(studio)
    fake_audio.duration = 8.0
    studio.gw.asr_replies = [(503, {"error": "занято"})]
    assert studio.c.post("/api/voices/transcribe", files=SAMPLE).status_code == 503

    async def slow(*args, **kwargs):
        await asyncio.sleep(5)
        return "поздний текст"

    monkeypatch.setattr(voices, "TRANSCRIBE_BUDGET_SEC", 0.05)
    monkeypatch.setattr(speech, "dictate", slow)
    r = studio.c.post("/api/voices/transcribe", files=SAMPLE)
    assert r.status_code == 504 and r.json()["detail"]["code"] == "timeout"
    assert list(state.CFG.tmp_dir.iterdir()) == []
    assert state.DB.execute("SELECT count(*) FROM voices").fetchone()[0] == 0
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 0


def test_voice_is_kept_as_wav_and_listed_without_its_text(studio, fake_audio, caplog):
    caplog.set_level(logging.DEBUG)
    add_gateway(studio)
    fake_audio.duration = 8.0
    r = create(studio)
    assert r.status_code == 201
    voice = r.json()
    assert voice["name"] == "Мой голос" and voice["duration"] == 8.0 and "text" not in voice
    assert ("to_wav", voices.SAMPLE_MAX) in fake_audio.calls
    assert state.DB.execute("SELECT mime FROM media").fetchone()[0] == "audio/wav"
    assert [v["id"] for v in studio.c.get("/api/voices").json()["items"]] == [voice["id"]]
    assert studio.c.get(f"/api/voices/{voice['id']}").json()["text"] == "Привет, это мой голос для озвучки."
    assert "Мой голос" not in caplog.text and "Привет" not in caplog.text
    uid = state.DB.execute("SELECT id FROM users").fetchone()[0]
    wav, text = asyncio.run(voices.reference(uid, voice["id"]))
    assert wav == b"\x00" * 2048 and text.startswith("Привет")


def test_short_sample_and_bad_fields_are_refused(studio, fake_audio):
    add_gateway(studio)
    fake_audio.duration = 3.0
    r = create(studio)
    assert r.status_code == 422 and r.json()["detail"]["code"] == "short"
    fake_audio.duration = 8.0
    assert create(studio, name="").json()["detail"]["code"] == "name"
    assert create(studio, text="x" * 501).json()["detail"]["code"] == "text"
    assert state.DB.execute("SELECT count(*) FROM voices").fetchone()[0] == 0
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 0


def test_sample_that_outlasts_its_budget_is_refused_by_the_studio(studio, monkeypatch):
    """ffmpeg образца дольше BUDGET_SEC — 504 словами от службы раньше таймаута маршрута Apache
    (сверяет test_deploy); голоса и файла нет, временные файлы убраны."""
    import audio
    add_gateway(studio)

    async def slow(src, dst, *, max_sec):
        await asyncio.sleep(5)
        return 8.0

    monkeypatch.setattr(voices, "BUDGET_SEC", 0.05)
    monkeypatch.setattr(audio, "to_wav", slow)
    r = create(studio)
    assert r.status_code == 504 and r.json()["detail"]["code"] == "timeout"
    assert "ещё раз" in r.json()["detail"]["message"]
    assert state.DB.execute("SELECT count(*) FROM voices").fetchone()[0] == 0
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 0
    assert list(state.CFG.tmp_dir.iterdir()) == []


def test_voice_is_private_and_goes_with_delete_and_history(studio, env, fake_audio):
    add_gateway(studio)
    fake_audio.duration = 8.0
    vid = create(studio).json()["id"]
    make_user(studio, "bob"); signin(studio, "bob")
    assert studio.c.get(f"/api/voices/{vid}").status_code == 404
    assert studio.c.delete(f"/api/voices/{vid}").status_code == 404
    signin(studio, "owner")
    assert studio.c.delete(f"/api/voices/{vid}").status_code == 204
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 0
    create(studio)
    assert studio.c.delete("/api/history").status_code == 200
    assert state.DB.execute("SELECT count(*) FROM voices").fetchone()[0] == 0
