"""Расшифровка записей (app/transcripts.py): загрузка потоком на шару, задание в фоне, ожидание
карты, отмена, перезапуск, удаление; имя файла и текст — не в журнал."""
import asyncio
import json
import logging
import pathlib
import threading
from urllib.parse import quote

import clock
import media
import state
import transcripts
from conftest import T0, add_gateway, make_user, signin

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
    assert studio.c.delete("/api/history").status_code == 200
    assert state.DB.execute("SELECT count(*) FROM transcripts").fetchone()[0] == 0
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 0
    assert transcripts.JOBS == {}                     # и задание стёртой истории не держится в памяти


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


def test_edit_is_checked_in_words(studio, fake_audio):
    tid = done_tr(studio)
    for body in ({"texts": {"2": "x"}}, {"texts": {"-1": "x"}}, {"texts": {"a": "x"}}, {"texts": {"0": "   "}},
                 {"texts": {"0": "я" * (len("Здравствуйте.") + transcripts.EDIT_TEXT_MAX + 1)}},
                 {"texts": {"0": "\n\x0b\t"}}, {"names": {"Собеседник 3": "x"}},
                 {"names": {"Собеседник 1": "Иван\nПетров"}}, {"names": {"Собеседник 1": " "}},
                 {"names": {"Собеседник 1": "Ив\x0bан"}}, {"names": {"Собеседник 1": "Иван Петров"}},
                 {"names": {"Собеседник 1": "Иван Петров"}},
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


def test_delete_takes_the_recording_and_the_result(studio, env, fake_audio):
    add_gateway(studio)
    tid = upload(studio).json()["id"]
    wait_tr(studio, tid, {"done"})
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 2
    assert studio.c.delete(f"/api/transcripts/{tid}").status_code == 204
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 0
    assert studio.c.get(f"/api/transcripts/{tid}").status_code == 404
