"""Creation already awaiting conversion/upstream cannot outlive history wipe or disable."""
import asyncio
import concurrent.futures
import threading
from types import SimpleNamespace

import httpx
import pytest

import admin
import audio
import history
import media
import operations
import speeches
import state
import transcripts
from conftest import add_gateway, make_user
from fakes import PNG_REAL, WAV_FAKE, video_object
from test_speeches import busy_speech, voice, wait_sp
from test_transcripts import upload, wait_tr


async def disable(uid, administrator, *, reenable=False):
    await admin.patch_user(uid, admin.UserPatch(disabled=True), administrator)
    if reenable:
        await admin.patch_user(uid, admin.UserPatch(disabled=False), administrator)


@pytest.mark.parametrize("action", ["wipe", "disable", "reenable"])
def test_voice_conversion_cannot_recreate_history(studio, env, monkeypatch, action):
    add_gateway(studio)
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]
    administrator = SimpleNamespace(id=make_user(studio, "administrator", is_admin=True))

    async def convert(src, dst, *, max_sec):
        if action == "wipe":
            await history.delete_all(uid)
        else:
            await disable(uid, administrator, reenable=action == "reenable")
        dst.write_bytes(b"wav")
        return 8.0

    monkeypatch.setattr(audio, "to_wav", convert)
    result = studio.c.post("/api/voices", data={"name": "sample", "text": "reference"},
                           files={"audio": ("sample.m4a", b"sample", "audio/mp4")})
    assert result.status_code == (401 if action == "disable" else 409), result.text
    assert state.DB.execute("SELECT count(*) FROM voices").fetchone()[0] == 0
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 0
    assert list(env.media_dir.rglob("*.wav")) == []
    assert list(state.CFG.tmp_dir.iterdir()) == []
    if action != "disable":
        async def fresh_conversion(src, dst, *, max_sec):
            dst.write_bytes(b"wav")
            return 8.0

        monkeypatch.setattr(audio, "to_wav", fresh_conversion)
        if action == "reenable":
            from conftest import signin
            signin(studio, "owner")
        fresh = studio.c.post("/api/voices", data={"name": "fresh", "text": "reference"},
                              files={"audio": ("sample.m4a", b"sample", "audio/mp4")})
        assert fresh.status_code == 201, fresh.text


@pytest.mark.parametrize("reenable", [False, True])
def test_video_gateway_reply_after_disable_is_rolled_back(studio, env, reenable):
    add_gateway(studio)
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]
    administrator = SimpleNamespace(id=make_user(studio, "administrator", is_admin=True))
    gateway_id = "video_" + "1" * 24

    async def accepted(request):
        studio.gw.videos[gateway_id] = 42
        await disable(uid, administrator, reenable=reenable)
        return httpx.Response(200, json=video_object(gateway_id))

    studio.gw.on["/slot/videos"] = accepted
    result = studio.c.post("/api/videos", files={"image": ("source.png", PNG_REAL, "image/png")})
    assert result.status_code == (409 if reenable else 401), result.text
    assert state.DB.execute("SELECT count(*) FROM videos").fetchone()[0] == 0
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 0
    assert studio.gw.video_deleted == [gateway_id]
    assert [p for p in env.media_dir.rglob("*") if p.is_file()] == []


def test_creation_started_during_wipe_is_rejected_after_wipe_finishes(studio, monkeypatch):
    """The wipe must also invalidate requests admitted while its file cleanup was waiting."""
    add_gateway(studio)
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]
    studio.c.portal.call(media.save_async, uid, b"old", "text/plain", "old.txt")
    cleaning, converting, release_cleanup = threading.Event(), threading.Event(), threading.Event()
    real_remove = media.remove_files

    def held_remove(rows):
        cleaning.set()
        assert release_cleanup.wait(5)
        return real_remove(rows)

    async def held_conversion(src, dst, *, max_sec):
        converting.set()
        async with asyncio.timeout(5):
            while state.DB.execute("SELECT 1 FROM media WHERE user_id = ?", (uid,)).fetchone():
                await asyncio.sleep(0)
        dst.write_bytes(b"wav")
        return 8.0

    monkeypatch.setattr(media, "remove_files", held_remove)
    monkeypatch.setattr(audio, "to_wav", held_conversion)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        wiping = executor.submit(studio.c.delete, "/api/history")
        try:
            assert cleaning.wait(5)
            creation = executor.submit(studio.c.post, "/api/voices", data={"name": "late", "text": "reference"},
                                       files={"audio": ("sample.m4a", b"sample", "audio/mp4")})
            assert converting.wait(5)
        finally:
            release_cleanup.set()
        assert wiping.result(timeout=5).status_code == 200
        result = creation.result(timeout=5)
    assert result.status_code == 409, result.text
    assert state.DB.execute("SELECT count(*) FROM voices").fetchone()[0] == 0
    assert state.DB.execute("SELECT count(*) FROM media").fetchone()[0] == 0


def test_a_transcript_uploaded_while_history_is_wiped_is_rejected_and_its_file_removed(studio, fake_audio, monkeypatch):
    """Загрузка записи кончилась уже в новом поколении истории: строки нет, файл убран, ответ — 409 словами
    (а не `queued`, которая упала бы потом «сбой службы»)."""
    add_gateway(studio)
    uid = owner_id()
    real_save = media.save_stream

    async def saved_then_wiped(*args, **kwargs):
        source = await real_save(*args, **kwargs)
        with operations.barrier(uid):            # «Удалить всю историю» началась, пока запрос дописывал файл
            pass
        return source

    monkeypatch.setattr(media, "save_stream", saved_then_wiped)
    result = upload(studio)
    assert result.status_code == 409 and result.json()["detail"]["code"] == "history_changed", result.text
    assert state.DB.execute("SELECT count(*) FROM transcripts").fetchone()[0] == 0
    studio.c.portal.call(media.purge)
    assert state.DB.execute("SELECT count(*) FROM media WHERE user_id = ?", (uid,)).fetchone()[0] == 0


# ---- очереди на пользователя: эпоха — у задания, а не у раннера ------------------------

def owner_id() -> int:
    return state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]


def wiped(studio, uid) -> None:
    """Поколение истории сменилось (как после «Удалить всю историю»), а раннер очереди ещё жив."""
    async def bump():
        with operations.barrier(uid):
            pass
    studio.c.portal.call(bump)


def test_a_queued_speech_keeps_its_own_epoch_not_the_runner_one(studio, fake_audio, monkeypatch):
    """S1 принят до очистки, S3 — после, в очередь живого раннера: S3 сохраняется, S1 — нет."""
    add_gateway(studio)
    uid = owner_id()
    first = busy_speech(studio, fake_audio, monkeypatch)
    wait_sp(studio, first, {"working"})
    wiped(studio, uid)
    vid = state.DB.execute("SELECT voice FROM speeches").fetchone()[0]
    later = studio.c.post("/api/speeches", json={"text": "Позже.", "voice": vid})
    assert later.status_code == 202 and later.json()["state"] == "queued"
    studio.gw.tts_replies = [(200, WAV_FAKE)]
    failed = wait_sp(studio, first, speeches.TERMINAL)
    # причина словами барьера, а не «сбой службы: HTTPException»
    assert failed["state"] == "error" and failed["detail"] == "история очищена или очищается — повторите действие"
    done = wait_sp(studio, later.json()["id"], speeches.TERMINAL)
    assert done["state"] == "done", done
    assert speeches.EPOCHS == {}


def test_a_speech_resumed_after_restart_is_bound_to_an_epoch(studio, fake_audio, monkeypatch):
    """Ждущая с прошлого запуска — с эпохой старта: очистка истории после него её сохранение запрещает."""
    add_gateway(studio)
    uid = owner_id()
    vid = voice(studio, fake_audio)
    sid = studio.c.post("/api/speeches", json={"text": "Привет.", "voice": vid}).json()["id"]
    assert wait_sp(studio, sid, speeches.TERMINAL)["state"] == "done"
    state.DB.execute("INSERT INTO speeches(id, user_id, connection_id, model, voice, text, state, detail, created)"
                     " SELECT 'q1', user_id, connection_id, model, voice, 'Ещё.', 'queued', 'в очереди', created + 1"
                     " FROM speeches WHERE id = ?", (sid,))
    monkeypatch.setattr(speeches, "WAIT_MAX_SEC", 10 ** 9)
    studio.gw.tts_replies = [(503, {"detail": "busy"})]
    studio.c.__exit__(None, None, None)
    studio.c.__enter__()
    wait_sp(studio, "q1", {"working"})
    wiped(studio, uid)
    studio.gw.tts_replies = [(200, WAV_FAKE)]
    assert wait_sp(studio, "q1", speeches.TERMINAL)["state"] == "error"
    assert state.DB.execute("SELECT count(*) FROM media WHERE mime = 'audio/mpeg'").fetchone()[0] == 1


def test_a_queued_transcript_keeps_its_own_epoch_not_the_runner_one(studio, fake_audio, monkeypatch):
    add_gateway(studio)
    uid = owner_id()
    monkeypatch.setattr(transcripts, "WAIT_MAX_SEC", 10 ** 9)
    studio.gw.asr_replies = [(503, {"error": "занято"})] * 10 ** 5
    first = upload(studio).json()["id"]
    wait_tr(studio, first, {"working"})
    wiped(studio, uid)
    later = upload(studio)
    assert later.status_code == 202 and later.json()["state"] == "queued"
    studio.gw.asr_replies = []
    failed = wait_tr(studio, first, transcripts.TERMINAL)
    assert failed["state"] == "error" and failed["detail"] == "история очищена или очищается — повторите действие"
    done = wait_tr(studio, later.json()["id"], transcripts.TERMINAL)
    assert done["state"] == "done", done
    assert transcripts.EPOCHS == {}


def test_a_transcript_resumed_after_restart_is_bound_to_an_epoch(studio, fake_audio, monkeypatch):
    add_gateway(studio)
    uid = owner_id()
    tid = upload(studio).json()["id"]
    assert wait_tr(studio, tid, transcripts.TERMINAL)["state"] == "done"
    state.DB.execute("INSERT INTO transcripts(id, user_id, connection_id, model, name, source_media_id, state,"
                     " detail, created) SELECT 'q1', user_id, connection_id, model, name, source_media_id, 'queued',"
                     " 'в очереди', created + 1 FROM transcripts WHERE id = ?", (tid,))
    monkeypatch.setattr(transcripts, "WAIT_MAX_SEC", 10 ** 9)
    studio.gw.asr_replies = [(503, {"error": "занято"})] * 10 ** 5
    studio.c.__exit__(None, None, None)
    studio.c.__enter__()
    wait_tr(studio, "q1", {"working"})
    wiped(studio, uid)
    studio.gw.asr_replies = []
    assert wait_tr(studio, "q1", transcripts.TERMINAL)["state"] == "error"
    assert state.DB.execute("SELECT count(*) FROM media WHERE mime = 'application/json'").fetchone()[0] == 1
