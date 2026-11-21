"""Озвучка (app/speeches.py): текст по фразам голосом по образцу (Qwen3-TTS шлюза) или голосом
сервиса по имени, склейка в mp3, ожидание карты, отмена; текст — не в журнал."""
import asyncio
import json
import logging
import re

import httpx

import speech
import speeches
import state
import upstream
import voices
from conftest import add_connection, add_gateway, erase_history, make_user, signin
from fakes import WAV_FAKE
from http_checks import on_http
from test_transcripts import hold_temp_unlink
from test_voices import expire_budget_at_event

LONG_A = "А" * 150 + "."
LONG_B = "Б" * 150 + "."


def voice(s, fake_audio) -> str:
    fake_audio.duration = 8.0
    r = s.c.post("/api/voices", data={"name": "Мой", "text": "текст образца"},
                 files={"audio": ("v.m4a", b"\x00" * 512, "audio/mp4")})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def wait_sp(s, sid, states, limit=3000):
    async def poll(client):
        body = None
        for _ in range(limit):
            body = (await client.get(f"/api/speeches/{sid}")).json()
            if body["state"] in states:
                return body
        raise AssertionError(f"озвучка не дошла до {states}: {body}")

    return on_http(s, poll)


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


def test_f5_clones_the_selected_voice_without_releasing_other_models(studio, fake_audio):
    add_gateway(studio)
    vid = voice(studio, fake_audio)
    response = studio.c.post("/api/speeches", json={"text": "Проверка.", "voice": vid, "model": "f5-tts"})
    assert response.status_code == 202, response.text
    item = wait_sp(studio, response.json()["id"], {"done", "error"})
    assert item["state"] == "done" and item["model"] == "f5-tts"
    request, = studio.gw.tts_requests
    assert request.url.path == "/speech/tts-f5"
    assert b'name="ref_audio"' in request.content and "текст образца".encode() in request.content
    assert not studio.gw.releases


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
    # Повтор после 503 — снова с release: кончившаяся генерация картинок не держит карту арендой.
    paths = [q.url.path for q in studio.gw.requests if q.url.path in ("/speech/tts-qwen", "/slot/images/release")]
    assert re.sub("r+", "r", "".join("t" if p == "/speech/tts-qwen" else "r" for p in paths)) == "rttrt"


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


def test_repeated_cancel_waits_until_all_fragments_are_removed(studio, env, fake_audio, monkeypatch):
    add_gateway(studio)
    vid = voice(studio, fake_audio)
    studio.gw.tts_replies = [(200, WAV_FAKE), (200, WAV_FAKE), (503, {"detail": "busy"})]
    monkeypatch.setattr(speeches, "WAIT_MAX_SEC", 10 ** 9)
    unlinking, go = hold_temp_unlink(env, monkeypatch)
    text = f"{LONG_A} {LONG_B} {LONG_A}"
    sid = studio.c.post("/api/speeches", json={"text": text, "voice": vid}).json()["id"]
    for _ in range(3000):
        if studio.c.get(f"/api/speeches/{sid}").json()["parts"] == 2:
            break
    else:
        raise AssertionError("two parts were not generated")

    async def scenario():
        job = speeches.JOBS[sid]
        first = asyncio.create_task(speeches._cancel([job]))
        repeat = None
        try:
            assert await asyncio.to_thread(unlinking.wait, 5)
            repeat = asyncio.create_task(speeches._cancel([job]))
            await asyncio.sleep(0)
            assert not job.task.done()
            assert len(job.files) == 2
        finally:
            go.set()
            await asyncio.gather(first, *([repeat] if repeat else []))

    studio.c.portal.call(scenario)
    assert studio.c.get(f"/api/speeches/{sid}").json()["state"] == "cancelled"
    assert not list((env.db_path.parent / "tmp").iterdir()) and speeches.JOBS == {}


def busy_speech(s, fake_audio, monkeypatch) -> str:
    """Озвучка, которая ждёт карту, пока её не остановят."""
    vid = voice(s, fake_audio)
    monkeypatch.setattr(speeches, "WAIT_MAX_SEC", 10 ** 9)
    s.gw.tts_replies = [(503, {"detail": "busy"})]
    return s.c.post("/api/speeches", json={"text": "Привет.", "voice": vid}).json()["id"]


def test_history_wipe_drops_the_jobs_from_memory(studio, fake_audio, monkeypatch):
    add_gateway(studio)
    first = busy_speech(studio, fake_audio, monkeypatch)
    wait_sp(studio, first, {"working"})
    vid = state.DB.execute("SELECT voice FROM speeches").fetchone()[0]
    waiting = [studio.c.post("/api/speeches", json={"text": f"Фраза {n}.", "voice": vid}).json()
               for n in range(speeches.QUEUE_MAX)]
    assert [(w["state"], w["detail"]) for w in waiting] == [
        ("queued", f"в очереди · {n}") for n in range(1, speeches.QUEUE_MAX + 1)]
    r = studio.c.post("/api/speeches", json={"text": "Лишняя.", "voice": vid})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "busy"
    assert studio.c.post(f"/api/speeches/{waiting[0]['id']}/cancel").json() == {"state": "cancelled"}
    assert studio.c.get(f"/api/speeches/{waiting[1]['id']}").json()["detail"] == "в очереди · 1"
    assert erase_history(studio).status_code == 200
    assert state.DB.execute("SELECT count(*) FROM speeches").fetchone()[0] == 0 and speeches.JOBS == {}
    assert speeches.RUNNERS == {}


def test_ready_parts_can_be_heard_before_the_mp3_is_glued(studio, fake_audio, monkeypatch):
    """Готовые части идущей озвучки отдаются до склейки (n с 0); итоговый mp3 — прежний, части после него — 404."""
    add_gateway(studio)
    vid = voice(studio, fake_audio)
    monkeypatch.setattr(speeches, "WAIT_MAX_SEC", 10 ** 9)
    studio.gw.tts_replies = [(200, WAV_FAKE), (503, {"detail": "busy"})]       # вторая фраза ждёт карту
    sid = studio.c.post("/api/speeches", json={"text": f"{LONG_A} {LONG_B}", "voice": vid}).json()["id"]
    for _ in range(3000):
        item = studio.c.get(f"/api/speeches/{sid}").json()
        if item["parts"] == 1:
            break
    assert item["parts"] == 1 and item["state"] == "working"
    part = studio.c.get(f"/api/speeches/{sid}/parts/0")
    assert part.status_code == 200 and part.content == WAV_FAKE and part.headers["content-type"] == "audio/wav"
    assert part.headers["cache-control"] == "no-store"
    assert studio.c.get(f"/api/speeches/{sid}/parts/1").status_code == 404
    # Safari на iPhone играет звук только с ответом на Range — как /api/media
    piece = studio.c.get(f"/api/speeches/{sid}/parts/0", headers={"Range": "bytes=0-1"})
    assert piece.status_code == 206 and piece.content == WAV_FAKE[:2]
    assert piece.headers["content-range"] == f"bytes 0-1/{len(WAV_FAKE)}" and piece.headers["accept-ranges"] == "bytes"
    assert part.headers["accept-ranges"] == "bytes"
    beyond = studio.c.get(f"/api/speeches/{sid}/parts/0", headers={"Range": f"bytes={len(WAV_FAKE)}-"})
    assert beyond.status_code == 416 and beyond.headers["content-range"] == f"bytes */{len(WAV_FAKE)}"
    studio.gw.tts_replies = [(200, WAV_FAKE)]
    done = wait_sp(studio, sid, {"done", "error"})
    assert done["state"] == "done" and done["parts"] == 0 and ("mp3_of", 2) in fake_audio.calls
    assert studio.c.get(f"/api/speeches/{sid}/parts/0").status_code == 404


def test_voice_probe_says_one_short_phrase_and_keeps_nothing(studio, fake_audio, caplog):
    caplog.set_level(logging.DEBUG)
    add_gateway(studio)
    vid = voice(studio, fake_audio)
    media_before = state.DB.execute("SELECT count(*) FROM media").fetchone()[0]
    r = studio.c.post(f"/api/voices/{vid}/probe", json={"text": "  Привет,\n как слышно? "})
    assert r.status_code == 200 and r.content == WAV_FAKE and r.headers["cache-control"] == "no-store"
    (q,) = studio.gw.tts_requests
    assert "Привет, как слышно?".encode() in q.content and "текст образца".encode() in q.content
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == media_before
    assert state.DB.execute("SELECT count(*) FROM speeches").fetchone()[0] == 0 and "как слышно" not in caplog.text
    assert studio.c.post(f"/api/voices/{vid}/probe", json={"text": "я" * 201}).status_code == 422
    assert studio.c.post(f"/api/voices/{'0' * 32}/probe", json={"text": "Привет"}).status_code == 404
    studio.gw.tts_replies = [(503, {"detail": "busy"})]
    r = studio.c.post(f"/api/voices/{vid}/probe", json={"text": "Привет"})
    assert r.status_code == 503 and studio.sleeps.count(speeches.RETRY_SEC) == 0     # без ожидания карты


def test_voice_probe_held_by_the_gateway_ends_in_words_before_the_proxy(studio, fake_audio, monkeypatch):
    """Шлюз держит пробу (карта занята идущей генерацией): служба отвечает 504 словами раньше таймаута
    Apache (сверяет test_deploy), а не 502 прокси, и следующая проба не получает «проба уже идёт»."""
    add_gateway(studio)
    vid = voice(studio, fake_audio)
    reached = expire_budget_at_event(monkeypatch, voices.PROBE_BUDGET_SEC)
    real = speech.say

    async def held(*args, **kwargs):
        reached.set()
        await asyncio.Future()

    monkeypatch.setattr(speech, "say", held)
    r = studio.c.post(f"/api/voices/{vid}/probe", json={"text": "Привет"})
    assert r.status_code == 504 and r.json()["detail"]["code"] == "timeout"
    assert voices.PROBING == set()
    monkeypatch.setattr(speech, "say", real)
    monkeypatch.setattr(voices, "asyncio", asyncio)
    assert studio.c.post(f"/api/voices/{vid}/probe", json={"text": "Привет"}).status_code == 200


def external_speech(s, monkeypatch) -> int:
    """Внешний сервис в «Озвучке» с отмеченной tts-1 (адрес имени подменён публичным)."""
    state.DB.execute("UPDATE settings SET value = '[\"10.77.1.15:4000\", \"voice.cloud.test:443\"]' WHERE key = 'allowlist'")
    async def resolve(host, port): return ["93.184.216.34"]
    monkeypatch.setattr(upstream, "RESOLVE", resolve)
    cid = add_connection(s, base_url="https://voice.cloud.test/v1", token="k-1", sections=["speech"])
    assert s.c.put(f"/api/sections/speech/links/{cid}", json={"models": ["tts-1"]}).status_code == 200
    return cid


def test_voice_probe_picks_the_gateway_model_even_when_the_default_is_a_service(studio, fake_audio, monkeypatch):
    """Умолчание «Озвучки» — внешний сервис: проба без выбора берёт первую отмеченную модель шлюза; выбранная
    модель шлюза (F5-TTS) звучит своей, а внешний сервис голосом по образцу не говорит — 409 словами."""
    gateway = add_gateway(studio)
    vid = voice(studio, fake_audio)
    cloud = external_speech(studio, monkeypatch)
    assert studio.c.put("/api/sections/speech/default", json={"connection_id": cloud, "model": "tts-1"}).status_code == 200
    r = studio.c.post(f"/api/voices/{vid}/probe", json={"text": "Привет"})
    assert r.status_code == 200, r.text
    assert [q.url.path for q in studio.gw.tts_requests] == ["/speech/tts-qwen"]
    studio.gw.releases.clear()
    r = studio.c.post(f"/api/voices/{vid}/probe", json={"text": "Привет", "connection_id": gateway, "model": "f5-tts"})
    assert r.status_code == 200 and studio.gw.tts_requests[-1].url.path == "/speech/tts-f5" and not studio.gw.releases
    r = studio.c.post(f"/api/voices/{vid}/probe", json={"text": "Привет", "connection_id": cloud, "model": "tts-1"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "voice"
    r = studio.c.post(f"/api/voices/{vid}/probe", json={"text": "Привет", "connection_id": gateway, "model": "whisperx"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "model"


def test_queued_speeches_run_in_turn_and_survive_a_restart(studio, fake_audio):
    add_gateway(studio)
    vid = voice(studio, fake_audio)
    ids = [studio.c.post("/api/speeches", json={"text": f"Фраза {n}.", "voice": vid}).json()["id"] for n in range(2)]
    assert [wait_sp(studio, s, {"done", "error"})["state"] for s in ids] == ["done", "done"]
    state.DB.execute("INSERT INTO speeches(id, user_id, connection_id, model, voice, text, state, detail, created)"
                     " SELECT 'q1', user_id, connection_id, model, voice, 'Ещё.', 'queued', 'в очереди', created + 1"
                     " FROM speeches WHERE id = ?", (ids[0],))
    studio.c.__exit__(None, None, None)
    studio.c.__enter__()
    assert wait_sp(studio, "q1", {"done", "error"})["state"] == "done"


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
