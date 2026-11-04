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
import state
from conftest import add_gateway, make_user
from fakes import PNG_REAL, video_object


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
