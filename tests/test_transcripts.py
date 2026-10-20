"""Расшифровка записей (app/transcripts.py): загрузка потоком на шару, задание в фоне, ожидание
карты, отмена, перезапуск, удаление; имя файла и текст — не в журнал."""
import logging
from urllib.parse import quote

import clock
import state
import transcripts
from conftest import T0, add_gateway

NAME = "Звонок Ивану.m4a"


def upload(s, body=b"\x00" * 1024, name=NAME, mime="audio/mp4"):
    return s.c.post("/api/transcripts", content=body, headers={"content-type": mime, "x-studio-name": quote(name)})


def wait_tr(s, tid, states, limit=3000):
    body = None
    for _ in range(limit):
        body = s.c.get(f"/api/transcripts/{tid}").json()
        if body["state"] in states:
            return body
    raise AssertionError(f"расшифровка не дошла до {states}: {body}")


def files(env) -> list:
    return [p for p in env.media_dir.rglob("*") if p.is_file()]


def test_upload_streams_to_the_share_and_reads_by_speakers(studio, env, fake_audio, caplog):
    caplog.set_level(logging.DEBUG)
    add_gateway(studio)
    r = upload(studio)
    assert r.status_code == 202 and r.json()["name"] == NAME
    item = wait_tr(studio, r.json()["id"], {"done"})
    assert item["duration"] == 61.0 and item["speakers"] == 2
    assert [(s["speaker"], s["text"]) for s in item["segments"]] == [("Собеседник 1", "Здравствуйте."),
                                                                      ("Собеседник 2", "Добрый день.")]
    assert ("compress",) in fake_audio.calls
    paths = [q.url.path for q in studio.gw.requests]
    asr = paths.index("/speech/asr")
    assert {"/slot/images/release", "/slot/images/descriptions/release"} <= set(paths[:asr])
    assert NAME not in caplog.text and "Здравствуйте" not in caplog.text
    assert not list((env.db_path.parent / "tmp").iterdir())


def test_card_busy_waits_and_says_so(studio, fake_audio, monkeypatch):
    add_gateway(studio)
    studio.gw.asr_replies = [(503, {"error": "слот asr не взят: no_capacity"})] * 3
    details, real = [], clock.SLEEP

    async def spy(seconds):
        details.extend(transcripts.detail(j) for j in transcripts.JOBS.values())
        await real(seconds)
    monkeypatch.setattr(clock, "SLEEP", spy)
    item = wait_tr(studio, upload(studio).json()["id"], {"done", "error"})
    assert item["state"] == "done" and len(studio.gw.asr_requests) == 4
    assert details and all(d.startswith("жду карту · ") for d in details)
    assert studio.sleeps.count(transcripts.RETRY_SEC) == 3


def test_card_busy_too_long_is_an_error_in_words(studio, fake_audio, monkeypatch):
    add_gateway(studio)
    monkeypatch.setattr(transcripts, "WAIT_MAX_SEC", 30)
    studio.gw.asr_replies = [(503, {"error": "слот asr не взят"})] * 10
    item = wait_tr(studio, upload(studio).json()["id"], {"done", "error"})
    assert item["state"] == "error" and "карта занята" in item["detail"]


def test_cancel_while_waiting_for_the_card(studio, env, fake_audio, monkeypatch):
    add_gateway(studio)
    monkeypatch.setattr(transcripts, "WAIT_MAX_SEC", 10 ** 9)
    studio.gw.asr_replies = [(503, {"error": "занято"})] * 10 ** 5
    tid = upload(studio).json()["id"]
    for _ in range(3000):
        if len(studio.gw.asr_requests) >= 2:
            break
        studio.c.get(f"/api/transcripts/{tid}")
    assert studio.c.post(f"/api/transcripts/{tid}/cancel").status_code == 200
    item = wait_tr(studio, tid, {"cancelled", "error"})
    assert item["state"] == "cancelled"
    assert not list((env.db_path.parent / "tmp").iterdir())


def test_too_big_or_unknown_upload_leaves_nothing(studio, env, fake_audio, monkeypatch):
    add_gateway(studio)
    before = files(env)
    monkeypatch.setattr(transcripts, "UPLOAD_MAX", 1024)
    r = upload(studio, body=b"\x00" * 2048)
    assert r.status_code == 413 and r.json()["detail"]["code"] == "too_big"
    r = upload(studio, name="notes.txt", mime="text/plain")
    assert r.status_code == 415 and r.json()["detail"]["code"] == "format"
    assert state.DB.execute("SELECT count(*) FROM transcripts").fetchone()[0] == 0
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 0
    assert files(env) == before and not list((env.db_path.parent / "tmp").iterdir())


def test_type_comes_from_the_name_when_the_browser_gives_none(studio, fake_audio):
    add_gateway(studio)
    r = upload(studio, name="call.mp3", mime="application/octet-stream")
    assert r.status_code == 202
    assert state.DB.execute("SELECT mime FROM media").fetchone()[0] == "audio/mpeg"


def test_restart_marks_running_as_interrupted(studio):
    add_gateway(studio)
    uid = state.DB.execute("SELECT id FROM users").fetchone()[0]
    state.DB.execute("INSERT INTO transcripts(id, user_id, model, name, source_media_id, state, detail, created)"
                     " VALUES ('t1', ?, 'whisperx', 'a.m4a', 'm1', 'working', 'распознаю', ?)", (uid, T0))
    transcripts.recover()
    row = state.DB.execute("SELECT state, detail FROM transcripts WHERE id = 't1'").fetchone()
    assert tuple(row) == ("error", transcripts.INTERRUPTED)


def test_one_running_per_user_and_history_wipe_takes_all(studio, env, fake_audio, monkeypatch):
    add_gateway(studio)
    monkeypatch.setattr(transcripts, "WAIT_MAX_SEC", 10 ** 9)
    studio.gw.asr_replies = [(503, {"error": "занято"})] * 10 ** 5
    first = upload(studio).json()["id"]
    r = upload(studio)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "busy"
    assert studio.c.delete(f"/api/transcripts/{first}").status_code == 409        # идущую — сначала отменить
    assert studio.c.delete("/api/history").status_code == 204
    assert state.DB.execute("SELECT count(*) FROM transcripts").fetchone()[0] == 0
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 0


def test_delete_takes_the_recording_and_the_result(studio, env, fake_audio):
    add_gateway(studio)
    tid = upload(studio).json()["id"]
    wait_tr(studio, tid, {"done"})
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 2
    assert studio.c.delete(f"/api/transcripts/{tid}").status_code == 204
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 0
    assert studio.c.get(f"/api/transcripts/{tid}").status_code == 404
