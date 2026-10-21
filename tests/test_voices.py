"""Голоса озвучки по образцу (app/voices.py): запись 5–15 с, её текст и имя; образец — WAV в media,
только свой; имя и текст — не в журнал."""
import asyncio
import logging

import state
import voices
from conftest import add_gateway, make_user, signin

SAMPLE = {"audio": ("sample.m4a", b"\x00" * 2048, "audio/mp4")}


def create(s, name="Мой голос", text="Привет, это мой голос для озвучки."):
    return s.c.post("/api/voices", data={"name": name, "text": text}, files=SAMPLE)


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
    assert studio.c.delete("/api/history").status_code == 204
    assert state.DB.execute("SELECT count(*) FROM voices").fetchone()[0] == 0
