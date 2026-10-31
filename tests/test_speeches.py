"""Озвучка (app/speeches.py): текст по фразам голосом по образцу (Qwen3-TTS шлюза) или голосом
сервиса по имени, склейка в mp3, ожидание карты, отмена; текст — не в журнал."""
import json
import logging

import httpx

import speeches
import state
import upstream
from conftest import add_connection, add_gateway, make_user, signin
from fakes import WAV_FAKE
from test_transcripts import hold_temp_unlink

LONG_A = "А" * 150 + "."
LONG_B = "Б" * 150 + "."


def voice(s, fake_audio) -> str:
    fake_audio.duration = 8.0
    r = s.c.post("/api/voices", data={"name": "Мой", "text": "текст образца"},
                 files={"audio": ("v.m4a", b"\x00" * 512, "audio/mp4")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def wait_sp(s, sid, states, limit=3000):
    body = None
    for _ in range(limit):
        body = s.c.get(f"/api/speeches/{sid}").json()
        if body["state"] in states:
            return body
    raise AssertionError(f"озвучка не дошла до {states}: {body}")


def test_phrases_split_by_sentences_and_keep_the_text():
    assert speeches.phrases("Привет. Как дела?\nХорошо!") == ["Привет. Как дела? Хорошо!"]
    assert speeches.phrases(f"{LONG_A} {LONG_B}") == [LONG_A, LONG_B]
    words = " ".join(["слово"] * 120)                         # 719 знаков без точек
    pieces = speeches.phrases(words)
    assert all(len(p) <= speeches.PHRASE_MAX for p in pieces) and " ".join(pieces) == words
    assert speeches.phrases("  \n ") == []
    assert speeches.phrases("one  two\tthree   four", limit=10) == ["one two", "three four"]


def test_two_phrases_in_a_cloned_voice_become_one_mp3(studio, fake_audio, caplog):
    caplog.set_level(logging.DEBUG)
    add_gateway(studio)
    vid = voice(studio, fake_audio)
    r = studio.c.post("/api/speeches", json={"text": f"{LONG_A} {LONG_B}", "voice": vid})
    assert r.status_code == 202, r.text
    item = wait_sp(studio, r.json()["id"], {"done", "error"})
    assert item["state"] == "done" and item["media_id"] and item["duration"] == 8.0
    assert len(studio.gw.tts_requests) == 2
    assert all("текст образца".encode() in q.content for q in studio.gw.tts_requests)
    assert ("mp3_of", 2) in fake_audio.calls
    assert state.DB.execute("SELECT mime FROM media WHERE id = ?", (item["media_id"],)).fetchone()[0] == "audio/mpeg"
    paths = [q.url.path for q in studio.gw.requests]
    assert "/slot/images/release" in paths[:paths.index("/speech/tts-qwen")]
    assert "А" * 150 not in caplog.text
    assert speeches.JOBS == {}                        # кончившееся в памяти не живёт


def test_done_is_written_after_the_temp_files_are_gone(studio, env, fake_audio, monkeypatch):
    """Как у расшифровки: «готово» — уже без частей и mp3 во временном каталоге (находка P3)."""
    add_gateway(studio)
    vid = voice(studio, fake_audio)
    unlinking, go = hold_temp_unlink(env, monkeypatch)
    sid = studio.c.post("/api/speeches", json={"text": "Привет.", "voice": vid}).json()["id"]
    assert unlinking.wait(5)
    assert studio.c.get(f"/api/speeches/{sid}").json()["state"] == "working"
    go.set()
    assert wait_sp(studio, sid, {"done", "error"})["state"] == "done"
    assert not list((env.db_path.parent / "tmp").iterdir()) and speeches.JOBS == {}


def test_busy_phrase_is_retried(studio, fake_audio):
    add_gateway(studio)
    vid = voice(studio, fake_audio)
    studio.gw.tts_replies = [(200, WAV_FAKE), (503, {"detail": "busy: идёт другая генерация"}), (200, WAV_FAKE)]
    sid = studio.c.post("/api/speeches", json={"text": f"{LONG_A} {LONG_B}", "voice": vid}).json()["id"]
    assert wait_sp(studio, sid, {"done", "error"})["state"] == "done"
    assert len(studio.gw.tts_requests) == 3 and studio.sleeps.count(speeches.RETRY_SEC) == 1


def test_cancel_while_waiting_leaves_no_temp_files(studio, env, fake_audio, monkeypatch):
    add_gateway(studio)
    vid = voice(studio, fake_audio)
    monkeypatch.setattr(speeches, "WAIT_MAX_SEC", 10 ** 9)
    studio.gw.tts_replies = [(503, {"detail": "busy"})]
    sid = studio.c.post("/api/speeches", json={"text": "Привет.", "voice": vid}).json()["id"]
    for _ in range(3000):
        if len(studio.gw.tts_requests) >= 2:
            break
        studio.c.get(f"/api/speeches/{sid}")
    assert studio.c.post(f"/api/speeches/{sid}/cancel").status_code == 200
    assert wait_sp(studio, sid, {"cancelled", "error"})["state"] == "cancelled"
    assert not list((env.db_path.parent / "tmp").iterdir()) and speeches.JOBS == {}


def busy_speech(s, fake_audio, monkeypatch) -> str:
    """Озвучка, которая ждёт карту, пока её не остановят."""
    vid = voice(s, fake_audio)
    monkeypatch.setattr(speeches, "WAIT_MAX_SEC", 10 ** 9)
    s.gw.tts_replies = [(503, {"detail": "busy"})]
    return s.c.post("/api/speeches", json={"text": "Привет.", "voice": vid}).json()["id"]


def test_history_wipe_drops_the_jobs_from_memory(studio, fake_audio, monkeypatch):
    add_gateway(studio)
    busy_speech(studio, fake_audio, monkeypatch)
    assert studio.c.delete("/api/history").status_code == 200
    assert state.DB.execute("SELECT count(*) FROM speeches").fetchone()[0] == 0 and speeches.JOBS == {}


def test_deleting_a_user_drops_their_jobs_from_memory(studio, fake_audio, monkeypatch):
    make_user(studio, "owner", is_admin=True)
    bob = make_user(studio, "bob")
    signin(studio, "bob")
    add_gateway(studio)
    busy_speech(studio, fake_audio, monkeypatch)
    signin(studio, "owner")
    assert studio.c.delete(f"/api/admin/users/{bob}").status_code == 200
    assert speeches.JOBS == {}


def test_external_service_speaks_by_voice_name_in_one_request(studio, fake_audio, monkeypatch):
    add_gateway(studio)
    state.DB.execute("UPDATE settings SET value = '[\"10.77.1.15:4000\", \"voice.cloud.test:443\"]' WHERE key = 'allowlist'")
    async def resolve(host, port): return ["93.184.216.34"]
    monkeypatch.setattr(upstream, "RESOLVE", resolve)
    cid = add_connection(studio, base_url="https://voice.cloud.test/v1", token="k-1", sections=["speech"])
    studio.c.put(f"/api/sections/speech/links/{cid}", json={"models": ["tts-1"]})
    seen = []
    async def speak(q):
        seen.append(q)
        return httpx.Response(200, content=b"ID3mp3", headers={"content-type": "audio/mpeg"})
    studio.gw.on["/v1/audio/speech"] = speak
    r = studio.c.post("/api/speeches", json={"text": f"{LONG_A} {LONG_B}", "voice": "alloy",
                                             "connection_id": cid, "model": "tts-1"})
    assert wait_sp(studio, r.json()["id"], {"done", "error"})["state"] == "done"
    assert len(seen) == 1 and json.loads(seen[0].content)["voice"] == "alloy" and ("mp3_of", 1) in fake_audio.calls


def test_text_limits_and_foreign_voice(studio, fake_audio):
    add_gateway(studio)
    vid = voice(studio, fake_audio)
    assert studio.c.post("/api/speeches", json={"text": "я" * 2001, "voice": vid}).status_code == 422
    assert studio.c.post("/api/speeches", json={"text": " ", "voice": vid}).status_code == 422
    r = studio.c.post("/api/speeches", json={"text": "Привет.", "voice": "0" * 32})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "gone"
