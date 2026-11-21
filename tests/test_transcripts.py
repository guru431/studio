"""Расшифровка записей (app/transcripts.py): загрузка потоком на шару, задание в фоне, ожидание
карты, отмена, перезапуск, удаление; имя файла и текст — не в журнал."""
import asyncio
import json
import logging
import pathlib
import re
import threading
from types import SimpleNamespace
from urllib.parse import quote

import pytest

import clock
import media
import speeches
import state
import transcripts
from fastapi import HTTPException
from conftest import T0, add_gateway, erase_history, make_user, signin

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
    assert transcripts.JOBS == {}                     # кончившееся в памяти не живёт


def result_file(tid) -> pathlib.Path:
    row = state.DB.execute("SELECT user_id, result_media_id FROM transcripts WHERE id = ?", (tid,)).fetchone()
    return media.path(row["user_id"], row["result_media_id"])


def test_result_keeps_the_recognized_segments_and_an_old_one_still_reads(studio, fake_audio):
    """Файл итога новой расшифровки — ещё и `raw` (сегменты до склейки с номером реплики); старый
    файл без `raw` читается и выгружается как прежде."""
    add_gateway(studio)
    tid = upload(studio).json()["id"]
    wait_tr(studio, tid, {"done"})
    data = json.loads(result_file(tid).read_bytes())
    assert [(r["speaker"], r["text"], r["turn"]) for r in data["raw"]] == [("Собеседник 1", "Здравствуйте.", 0),
                                                                          ("Собеседник 2", "Добрый день.", 1)]
    del data["raw"]
    result_file(tid).write_bytes(json.dumps(data).encode())
    assert [s["text"] for s in studio.c.get(f"/api/transcripts/{tid}").json()["segments"]] == ["Здравствуйте.", "Добрый день."]
    srt = studio.c.get(f"/api/transcripts/{tid}/export?format=srt")
    assert srt.status_code == 200 and "Собеседник 2: Добрый день." in srt.text


def hold_temp_unlink(env, monkeypatch):
    """Удаление временного файла (каталог tmp) ждёт `go`; `unlinking` — оно началось."""
    tmp_dir, real = env.db_path.parent / "tmp", pathlib.Path.unlink
    unlinking, go = threading.Event(), threading.Event()

    def held(self, missing_ok=False):
        if self.parent == tmp_dir:
            unlinking.set()
            go.wait(5)
        return real(self, missing_ok=missing_ok)

    monkeypatch.setattr(pathlib.Path, "unlink", held)
    return unlinking, go


def test_done_is_written_after_the_temp_file_is_gone(studio, env, fake_audio, monkeypatch):
    """Итог пишется после удаления временного mp3: увидев «готово», временного файла не застать
    (под нагрузкой быстрый набор застал его — находка P3)."""
    add_gateway(studio)
    unlinking, go = hold_temp_unlink(env, monkeypatch)
    tid = upload(studio).json()["id"]
    assert unlinking.wait(5)
    assert studio.c.get(f"/api/transcripts/{tid}").json()["state"] == "working"
    go.set()
    assert wait_tr(studio, tid, {"done", "error"})["state"] == "done"
    assert not list((env.db_path.parent / "tmp").iterdir()) and transcripts.JOBS == {}


def test_cancellation_during_cleanup_waits_for_the_temp_file(studio, env, fake_audio, monkeypatch):
    add_gateway(studio)
    unlinking, go = hold_temp_unlink(env, monkeypatch)
    tid = upload(studio).json()["id"]
    assert unlinking.wait(5)

    async def scenario():
        job = transcripts.JOBS[tid]
        job.by_user = True
        try:
            for _ in range(2):
                job.task.cancel()
                await asyncio.sleep(0)
                assert not job.task.done(), "задание кончилось до уборки MP3"
                assert state.DB.execute("SELECT state FROM transcripts WHERE id = ?", (tid,)).fetchone()[0] == "working"
        finally:
            go.set()
            await asyncio.gather(job.task, return_exceptions=True)

    studio.c.portal.call(scenario)
    assert not list((env.db_path.parent / "tmp").iterdir()) and transcripts.JOBS == {}
    assert studio.c.get(f"/api/transcripts/{tid}").json()["state"] in transcripts.TERMINAL


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
    # Release перед каждым повтором: кончившаяся генерация картинок не держит карту арендой до конца ожидания.
    paths = [q.url.path for q in studio.gw.requests if q.url.path in ("/speech/asr", "/slot/images/release")]
    attempts = "".join("a" if p == "/speech/asr" else "r" for p in paths)
    assert re.sub("r+", "r", attempts) == "ra" * 4


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
    assert not list((env.db_path.parent / "tmp").iterdir()) and transcripts.JOBS == {}


@pytest.mark.parametrize("module, table", [(transcripts, "transcripts"), (speeches, "speeches")])
def test_cancel_before_job_starts_keeps_runner_alive(studio, monkeypatch, module, table):
    uid = make_user(studio, "owner")
    if module is transcripts:
        state.DB.executemany("INSERT INTO transcripts(id, user_id, model, name, source_media_id, state, detail, created)"
                             " VALUES (?, ?, 'm', 'name', 'media', 'queued', '', ?)", [("first", uid, 1), ("second", uid, 2)])
    else:
        state.DB.executemany("INSERT INTO speeches(id, user_id, model, voice, text, state, detail, created)"
                             " VALUES (?, ?, 'm', 'voice', 'text', 'queued', '', ?)", [("first", uid, 1), ("second", uid, 2)])

    real = asyncio.create_task
    cancels = []

    def spawn(coro, *args, **kwargs):
        if coro.cr_code is module.run.__code__ and "first" in module.JOBS:
            # Schedule the endpoint before the child gets its first event-loop step.
            request = SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(id=uid)))
            cancels.append(real(module.cancel("first", request)))
        return real(coro, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_task", spawn)

    async def scenario():
        module.kick(uid)
        await asyncio.gather(module.RUNNERS[uid], return_exceptions=True)
        assert await cancels.pop() == {"state": "cancelled"}

    studio.c.portal.call(scenario)
    assert [tuple(r) for r in state.DB.execute(f"SELECT id, state FROM {table} ORDER BY id")] == [
        ("first", "cancelled"), ("second", "error")]
    assert module.JOBS == {}


def test_retry_reuses_the_account_recording_until_last_attempt_is_deleted(studio, fake_audio):
    conn = add_gateway(studio)
    tid = upload(studio).json()["id"]
    original = wait_tr(studio, tid, {"done"})
    mid = original["source_media_id"]
    endpoint = f"/api/transcripts/{tid}/retry"
    make_user(studio, "bob")
    signin(studio, "bob")
    assert studio.c.post(endpoint, json={}).status_code == 404
    signin(studio, "owner")
    denied = studio.c.post(endpoint, json={"model": "unknown"})
    assert denied.status_code == 422 and denied.json()["detail"]["code"] == "model"
    response = studio.c.post(endpoint, json={"connection_id": conn, "model": "whisperx"})
    assert response.status_code == 202, response.text
    attempt = response.json()
    assert attempt["id"] != tid and attempt["source_media_id"] == mid
    assert wait_tr(studio, attempt["id"], {"done"})["segments"] == original["segments"]
    assert studio.c.get(f"/api/transcripts/{tid}").json() == original
    assert studio.c.delete(f"/api/transcripts/{tid}").status_code == 204
    assert studio.c.request("DELETE", f"/api/trash/transcripts/{tid}", json={"password": "password-123"}).status_code == 204
    assert studio.c.get(f"/api/media/{mid}").status_code == 200
    assert studio.c.delete(f"/api/transcripts/{attempt['id']}").status_code == 204
    assert studio.c.request("DELETE", f"/api/trash/transcripts/{attempt['id']}", json={"password": "password-123"}).status_code == 204
    assert studio.c.get(f"/api/media/{mid}").status_code == 404


def test_failed_transcript_retry_keeps_one_record_and_its_source(studio, fake_audio):
    conn = add_gateway(studio)
    studio.gw.asr_replies = [(400, {"error": "recognition failed"})]
    tid = upload(studio).json()["id"]
    failed = wait_tr(studio, tid, {"error"})
    studio.gw.asr_replies = []
    retry = studio.c.post(f"/api/transcripts/{tid}/retry",
                          json={"connection_id": conn, "model": failed["model"], "language": "en"})
    assert retry.status_code == 202, retry.text
    assert retry.json()["id"] == tid and retry.json()["source_media_id"] == failed["source_media_id"]
    done = wait_tr(studio, tid, {"done"})
    assert done["language"] == "en" and done["connection_id"] == conn
    assert [item["id"] for item in studio.c.get("/api/transcripts").json()["items"]] == [tid]
    assert studio.c.delete(f"/api/transcripts/{tid}").status_code == 204
    assert studio.c.post(f"/api/transcripts/{tid}/retry", json={}).status_code == 404


def test_playback_copy_supports_range_and_retry_after_the_original_is_removed(studio, fake_audio, monkeypatch):
    tid = done_tr(studio)
    row = studio.c.get(f"/api/transcripts/{tid}").json()
    source, listen = row["source_media_id"], row["listen_media_id"]
    assert source != listen
    cut = studio.c.get(f"/api/media/{listen}", headers={"Range": "bytes=0-9"})
    assert cut.status_code == 206 and cut.headers["content-type"].startswith("audio/mpeg")
    endpoint = f"/api/transcripts/{tid}/source"
    uid = state.DB.execute("SELECT user_id FROM transcripts WHERE id = ?", (tid,)).fetchone()[0]
    path = media.path(uid, listen)
    stat = pathlib.Path.stat

    def missing_copy(file, *args, **kwargs):
        if file == path:
            raise FileNotFoundError
        return stat(file, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(pathlib.Path, "stat", missing_copy)
        assert studio.c.delete(endpoint).status_code == 404
    assert studio.c.get(f"/api/media/{source}").status_code == 200
    assert studio.c.delete(endpoint).status_code == 204
    assert studio.c.get(f"/api/media/{source}").status_code == 404
    assert studio.c.get(f"/api/media/{listen}").status_code == 200
    assert studio.c.get(f"/api/transcripts/{tid}").json()["segments"] == row["segments"]
    retry = studio.c.post(f"/api/transcripts/{tid}/retry", json={}).json()
    retried = wait_tr(studio, retry["id"], {"done"})
    assert retried["source_media_id"] == retried["listen_media_id"] == listen
    assert fake_audio.calls.count(("compress",)) == 1


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


def test_name_and_speech_text_are_found_by_search_in_nfc(studio, fake_audio):
    """Имя из архива macOS (NFD: «и» + U+0306) — в NFC: поиск по составному «й» его находит."""
    add_gateway(studio)
    decomposed = "Чаи\u0306ковскии\u0306.m4a"
    r = upload(studio, name=decomposed)
    assert r.json()["name"] == "Чайковский.m4a"
    found = studio.c.post("/api/search", json={"q": "Чайковский"}).json()
    assert any(i.get("section") == "transcribe" for i in found["items"]), found


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


@pytest.mark.integration  # заполнение всей файловой очереди: > 1 с под нагрузкой набора
def test_uploads_queue_up_and_history_wipe_takes_all(studio, env, fake_audio, monkeypatch):
    """Очередь вместо 409: идёт одна запись, ждущих — до QUEUE_MAX («в очереди · N»), следующая — 409 busy;
    стирание истории снимает и идущую, и ждущие."""
    add_gateway(studio)
    monkeypatch.setattr(transcripts, "WAIT_MAX_SEC", 10 ** 9)
    studio.gw.asr_replies = [(503, {"error": "занято"})] * 10 ** 5
    first = upload(studio).json()["id"]
    wait_tr(studio, first, {"working"})
    waiting = [upload(studio).json() for _ in range(transcripts.QUEUE_MAX)]
    assert [w["state"] for w in waiting] == ["queued"] * transcripts.QUEUE_MAX
    assert [w["detail"] for w in waiting] == [f"в очереди · {n}" for n in range(1, transcripts.QUEUE_MAX + 1)]
    r = upload(studio)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "busy"
    assert state.DB.execute("SELECT count(*) FROM transcripts").fetchone()[0] == transcripts.QUEUE_MAX + 1
    assert studio.c.delete(f"/api/transcripts/{first}").status_code == 409        # идущую — сначала отменить
    assert studio.c.delete(f"/api/transcripts/{waiting[0]['id']}").status_code == 409   # и ждущую
    # ждущую можно снять: остальные сдвигаются
    assert studio.c.post(f"/api/transcripts/{waiting[0]['id']}/cancel").json() == {"state": "cancelled"}
    assert studio.c.get(f"/api/transcripts/{waiting[1]['id']}").json()["detail"] == "в очереди · 1"
    assert erase_history(studio).status_code == 200
    assert state.DB.execute("SELECT count(*) FROM transcripts").fetchone()[0] == 0
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 0
    assert transcripts.JOBS == {} and transcripts.RUNNERS == {}   # и задание стёртой истории не держится в памяти


def test_queued_records_run_in_turn_and_survive_a_restart(studio, fake_audio):
    """Ждущие идут по одной в порядке загрузки; перезапуск службы делает идущую «прервано», а ждущие
    продолжаются после старта (resume), не теряясь молча."""
    add_gateway(studio)
    ids = [upload(studio, name=f"r{n}.m4a").json()["id"] for n in range(3)]
    done = [wait_tr(studio, tid, {"done", "error"}) for tid in ids]
    assert [d["state"] for d in done] == ["done"] * 3
    assert [d["finished"] for d in done] == sorted(d["finished"] for d in done)
    uid = state.DB.execute("SELECT id FROM users").fetchone()[0]
    for tid, st in (("w1", "working"), ("q1", "queued")):
        state.DB.execute("INSERT INTO transcripts(id, user_id, connection_id, model, name, source_media_id, state,"
                         " detail, created) SELECT ?, user_id, connection_id, model, name, source_media_id, ?, ?, ?"
                         " FROM transcripts WHERE id = ?", (tid, st, st, T0 + 1, ids[0]))
    studio.c.__exit__(None, None, None)                                # служба остановилась
    studio.c.__enter__()                                               # и запустилась снова
    assert studio.c.get("/api/transcripts/w1").json()["detail"] == transcripts.INTERRUPTED
    assert wait_tr(studio, "q1", {"done", "error"})["state"] == "done"
    assert uid


def test_two_uploads_finishing_together_respect_the_queue_limit(studio, monkeypatch):
    add_gateway(studio)
    monkeypatch.setattr(transcripts, "QUEUE_MAX", 1)
    uid = state.DB.execute("SELECT id FROM users").fetchone()[0]
    save_stream = media.save_stream

    async def scenario():
        uploaded, held = asyncio.Event(), asyncio.Event()
        arrivals = 0

        async def save(*args):
            nonlocal arrivals
            source = await save_stream(*args)
            arrivals += 1
            if arrivals == 2:
                uploaded.set()
            await uploaded.wait()
            return source

        async def run(job):
            try:
                await held.wait()
            finally:
                transcripts.JOBS.pop(job.id, None)

        async def stream():
            yield b"recording"

        monkeypatch.setattr(media, "save_stream", save)
        monkeypatch.setattr(transcripts, "run", run)
        request = SimpleNamespace(state=SimpleNamespace(user=SimpleNamespace(id=uid)), stream=stream,
                                  headers={"content-type": "audio/mp4"})
        results = await asyncio.gather(transcripts.upload(request), transcripts.upload(request), return_exceptions=True)
        try:
            assert sum(isinstance(r, dict) for r in results) == 1
            error, = (r for r in results if isinstance(r, HTTPException))
            assert error.status_code == 409 and error.detail["code"] == "busy"
            assert state.DB.execute("SELECT count(*) FROM transcripts").fetchone()[0] == 1
            assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 1
        finally:
            await transcripts.stop_user(uid)

    studio.c.portal.call(scenario)


def test_deleting_a_user_drops_their_jobs_from_memory(studio, fake_audio, monkeypatch):
    make_user(studio, "owner", is_admin=True)
    bob = make_user(studio, "bob")
    signin(studio, "bob")
    add_gateway(studio)
    monkeypatch.setattr(transcripts, "WAIT_MAX_SEC", 10 ** 9)
    studio.gw.asr_replies = [(503, {"error": "занято"})] * 10 ** 5
    upload(studio)
    signin(studio, "owner")
    assert studio.c.delete(f"/api/admin/users/{bob}").status_code == 200
    assert transcripts.JOBS == {}


# ---- правка -----------------------------------------------------------------------

def done_tr(s) -> str:
    add_gateway(s)
    tid = upload(s).json()["id"]
    wait_tr(s, tid, {"done"})
    return tid


def edit(s, tid, **body):
    return s.c.patch(f"/api/transcripts/{tid}", json=body)


def shown(body) -> list:
    return [(x["speaker"], x["text"], x["edited"]) for x in body["segments"]]


def test_edit_texts_and_names_over_the_untouched_recognition(studio, fake_audio, caplog):
    caplog.set_level(logging.DEBUG)
    tid = done_tr(studio)
    before = result_file(tid).read_bytes()
    r = edit(studio, tid, texts={"1": "  Добрый вечер.  "}, names={"Собеседник 1": "Иван"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert shown(body) == [("Иван", "Здравствуйте.", False), ("Собеседник 2", "Добрый вечер.", True)]
    assert body["segments"][1]["original"] == "Добрый день." and "original" not in body["segments"][0]
    assert [x["label"] for x in body["segments"]] == ["Собеседник 1", "Собеседник 2"]
    assert body["names"] == {"Собеседник 1": "Иван"} and body["edited"] is True and body["speakers"] == 2
    # второй PATCH сливается с первым; GET — то же
    edit(studio, tid, texts={"0": "Привет."})
    assert shown(studio.c.get(f"/api/transcripts/{tid}").json()) == [
        ("Иван", "Привет.", True), ("Собеседник 2", "Добрый вечер.", True)]
    assert result_file(tid).read_bytes() == before                 # исходное распознавание — как было
    for secret in ("Добрый вечер", "Привет.", "Иван"):
        assert secret not in caplog.text, secret


def test_null_or_the_original_takes_an_edit_off_and_reset_returns_all(studio, fake_audio):
    tid = done_tr(studio)
    edit(studio, tid, texts={"0": "А.", "1": "Б."}, names={"Собеседник 1": "Иван", "Собеседник 2": "Пётр"})
    body = edit(studio, tid, texts={"0": None, "1": "Добрый день."}, names={"Собеседник 2": "Собеседник 2"}).json()
    assert shown(body) == [("Иван", "Здравствуйте.", False), ("Собеседник 2", "Добрый день.", False)]
    assert body["names"] == {"Собеседник 1": "Иван"}
    body = edit(studio, tid, names={"Собеседник 1": None}).json()
    assert body["edited"] is False and body["names"] == {}
    assert state.DB.execute("SELECT edits FROM transcripts").fetchone()[0] is None
    edit(studio, tid, texts={"1": "Б."}, names={"Собеседник 2": "Пётр"})
    r = studio.c.delete(f"/api/transcripts/{tid}/edits")
    assert r.status_code == 200 and shown(r.json()) == [("Собеседник 1", "Здравствуйте.", False),
                                                       ("Собеседник 2", "Добрый день.", False)]
    assert r.json()["edited"] is False


def test_speakers_count_the_shown_names(studio, fake_audio):
    tid = done_tr(studio)
    assert edit(studio, tid, names={"Собеседник 1": "Собеседник 2"}).json()["speakers"] == 1
    assert studio.c.get("/api/transcripts").json()["items"][0]["speakers"] == 1


def test_speaker_reassignment_updates_export_and_attachment_without_touching_recognition(studio, fake_audio):
    tid = done_tr(studio)
    before = result_file(tid).read_bytes()
    assert edit(studio, tid, speakers={"2": "Собеседник 1"}).status_code == 422
    assert edit(studio, tid, speakers={"1": "Собеседник 4"}).status_code == 422
    response = edit(studio, tid, speakers={"1": "Собеседник 1"}, names={"Собеседник 1": "Иван"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["speakers"] == 1 and body["edited"]
    second = body["segments"][1]
    assert (second["speaker"], second["original_label"], second["edited"]) == ("Иван", "Собеседник 2", False)
    assert studio.c.get("/api/transcripts").json()["items"][0]["speakers"] == 1
    assert "Иван: Добрый день." in studio.c.get(f"/api/transcripts/{tid}/export?format=srt").text
    attachment = studio.c.post(f"/api/transcripts/{tid}/attachments")
    assert attachment.status_code == 201 and attachment.json()["text"].count("Иван ·") == 2
    assert result_file(tid).read_bytes() == before
    restored = edit(studio, tid, speakers={"1": "Собеседник 2"})
    assert restored.json()["speakers"] == 2 and "original_label" not in restored.json()["segments"][1]


def test_edit_is_checked_in_words(studio, fake_audio):
    tid = done_tr(studio)
    for body in ({"texts": {"2": "x"}}, {"texts": {"-1": "x"}}, {"texts": {"a": "x"}}, {"texts": {"0": "   "}},
                 {"texts": {"0": "я" * (len("Здравствуйте.") + transcripts.EDIT_TEXT_MAX + 1)}},
                 {"texts": {"0": "\n\x0b\t"}}, {"names": {"Собеседник 3": "x"}},
                 {"names": {"Собеседник 1": "Иван\nПетров"}}, {"names": {"Собеседник 1": " "}},
                 {"names": {"Собеседник 1": "Ив\x0bан"}}, {"names": {"Собеседник 1": "Иван\u2028Петров"}},
                 {"names": {"Собеседник 1": "Иван\u2029Петров"}},
                 {"names": {"Собеседник 1": "я" * (transcripts.NAME_EDIT_MAX + 1)}}):
        r = edit(studio, tid, **body)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "input", body
        assert isinstance(r.json()["detail"]["message"], str)
    assert state.DB.execute("SELECT edits FROM transcripts").fetchone()[0] is None
    assert edit(studio, tid, texts={"0": "я" * transcripts.EDIT_TEXT_MAX}).status_code == 200


def test_a_long_replica_edits_within_its_own_length(studio, fake_audio, monkeypatch):
    """Реплика одного говорящего за 20+ минут длиннее EDIT_TEXT_MAX: предел — от исходной длины."""
    tid = done_tr(studio)
    monkeypatch.setattr(transcripts, "EDIT_TEXT_MAX", 5)          # «Здравствуйте.» — 13 знаков
    assert edit(studio, tid, texts={"0": "Здравствуйте, Иван"}).status_code == 200      # 13 + 5
    r = edit(studio, tid, texts={"0": "Здравствуйте, Иван!"})
    assert r.status_code == 422 and "18" in r.json()["detail"]["message"]


def test_edit_text_is_one_line_and_a_name_keeps_format_characters(studio, fake_audio):
    """Пустая строка в правке — разделитель реплик TXT и конец субтитра SRT, \\x0b ломает DOCX: текст —
    одной строкой. Имя отвергает только управляющие и разделители строк: ZWJ-эмодзи и мягкий перенос — можно."""
    tid = done_tr(studio)
    name = "Анна 👩‍💻 Пере­водчик"
    body = edit(studio, tid, texts={"0": " Первая\n\nвторая\x0bтретья\t  конец\r\n"},
                names={"Собеседник 1": name}).json()
    assert shown(body)[0] == (name, "Первая вторая третья конец", True)
    txt = studio.c.get(f"/api/transcripts/{tid}/export", params={"format": "txt"}).text
    assert txt.split("\n\n")[0] == f"{name} · 00:00\nПервая вторая третья конец"


def test_running_cannot_be_edited_and_foreign_is_gone(studio, fake_audio, monkeypatch):
    add_gateway(studio)
    monkeypatch.setattr(transcripts, "WAIT_MAX_SEC", 10 ** 9)
    studio.gw.asr_replies = [(503, {"error": "занято"})] * 10 ** 5
    running = upload(studio).json()["id"]
    r = edit(studio, running, texts={"0": "x"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "running"
    studio.c.post(f"/api/transcripts/{running}/cancel")
    studio.gw.asr_replies = []
    tid = upload(studio).json()["id"]
    wait_tr(studio, tid, {"done"})
    make_user(studio, "bob"); signin(studio, "bob")
    for r in (edit(studio, tid, texts={"0": "x"}), studio.c.delete(f"/api/transcripts/{tid}/edits")):
        assert r.status_code == 404 and r.json()["detail"]["code"] == "gone"


def found_names(s, q) -> list:
    return [i.get("title") or i.get("name") for i in s.c.post("/api/search", json={"q": q}).json()["items"]
            if i.get("section") == "transcribe"]


def test_rename_keeps_the_extension_and_search_finds_the_new_name(studio, fake_audio, caplog):
    caplog.set_level(logging.DEBUG)
    tid = done_tr(studio)
    r = edit(studio, tid, name="  Встреча\nс  Чайковским ")                 # NFD — в NFC
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Встреча с Чайковским.m4a" and len(r.json()["segments"]) == 2
    assert studio.c.get("/api/transcripts").json()["items"][0]["name"] == "Встреча с Чайковским.m4a"
    assert found_names(studio, "Чайковским") and not found_names(studio, "Звонок")
    disposition = studio.c.get(f"/api/transcripts/{tid}/export", params={"format": "txt"}).headers["content-disposition"]
    assert "%D0%92%D1%81%D1%82%D1%80%D0%B5%D1%87%D0%B0" in disposition                 # «Встреча…».txt
    assert edit(studio, tid, name="Итоги 12.03.M4A").json()["name"] == "Итоги 12.03.m4a"
    assert "Встреча" not in caplog.text and "Итоги" not in caplog.text
    for bad in ("", "  \n", ".m4a", "я" * transcripts.NAME_MAX):
        r = edit(studio, tid, name=bad)
        assert r.status_code == 422 and r.json()["detail"]["code"] == "input", bad
    assert studio.c.get(f"/api/transcripts/{tid}").json()["name"] == "Итоги 12.03.m4a"


def test_a_running_record_is_renamed_and_foreign_is_gone(studio, fake_audio, monkeypatch):
    add_gateway(studio)
    monkeypatch.setattr(transcripts, "WAIT_MAX_SEC", 10 ** 9)
    studio.gw.asr_replies = [(503, {"error": "занято"})] * 10 ** 5
    running = upload(studio).json()["id"]
    r = edit(studio, running, name="Планёрка")
    assert r.status_code == 200 and r.json()["name"] == "Планёрка.m4a" and r.json()["state"] in ("queued", "working")
    studio.c.post(f"/api/transcripts/{running}/cancel")
    make_user(studio, "bob"); signin(studio, "bob")
    assert edit(studio, running, name="x").status_code == 404


def test_delete_takes_the_recording_and_the_result(studio, env, fake_audio):
    add_gateway(studio)
    tid = upload(studio).json()["id"]
    wait_tr(studio, tid, {"done"})
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 3
    assert studio.c.delete(f"/api/transcripts/{tid}").status_code == 204
    assert studio.c.get(f"/api/transcripts/{tid}").status_code == 404
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 3      # в корзине — файлы на месте
    assert studio.c.request("DELETE", f"/api/trash/transcripts/{tid}", json={"password": "password-123"}).status_code == 204
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 0
