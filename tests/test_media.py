"""Файлы истории на шаре (app/media.py): запись, превью, чтение с диапазонами, удаление."""
import asyncio
import dataclasses
import io

import pytest
from fastapi import HTTPException
from PIL import Image

import media
import state
from conftest import make_user, signin

DATA = bytes(range(250)) * 4               # 1000 байт


def files(root):
    return sorted(p for p in root.rglob("*") if p.is_file())


def png(width, height) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 90)).save(out, "PNG")
    return out.getvalue()


def test_save_and_read_back_with_ranges(studio, env):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    mid = media.save(uid, DATA, "video/mp4", "clip.mp4")
    row = state.DB.execute("SELECT * FROM media WHERE id = ?", (mid,)).fetchone()
    assert row["rel_path"] == f"u{uid}/2027/01/{mid}.mp4" and row["size"] == 1000 and row["has_thumb"] == 0
    assert files(env.media_dir) == [env.media_dir / row["rel_path"]]     # временного файла нет
    full = studio.c.get(f"/api/media/{mid}")
    assert full.status_code == 200 and full.content == DATA
    assert full.headers["accept-ranges"] == "bytes" and full.headers["content-type"] == "video/mp4"
    assert full.headers["cache-control"] == "private, max-age=86400"
    assert "content-disposition" not in full.headers
    part = studio.c.get(f"/api/media/{mid}", headers={"Range": "bytes=10-19"})
    assert part.status_code == 206 and part.content == DATA[10:20]
    assert part.headers["content-range"] == "bytes 10-19/1000" and part.headers["content-length"] == "10"


def test_suffix_and_bad_ranges(studio):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    mid = media.save(uid, DATA, "video/mp4", "clip.mp4")
    tail = studio.c.get(f"/api/media/{mid}", headers={"Range": "bytes=-5"})
    assert tail.status_code == 206 and tail.content == DATA[-5:]
    assert tail.headers["content-range"] == "bytes 995-999/1000"
    past = studio.c.get(f"/api/media/{mid}", headers={"Range": "bytes=2000-"})
    assert past.status_code == 416 and past.headers["content-range"] == "bytes */1000"
    assert media.byte_range("bytes=990-5000", 1000) == (990, 999)
    for bad in ("bytes=-0", "bytes=-", "bytes=5-1", "items=0-1", "bytes=0-1,3-4"):
        assert media.byte_range(bad, 1000) is None


def test_thumb_is_small_webp(studio, env):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    original = png(1216, 832)
    mid = media.save(uid, original, "image/png", "picture.png", thumb=True)
    r = studio.c.get(f"/api/media/{mid}?thumb=1")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    small = Image.open(io.BytesIO(r.content))
    assert small.format == "WEBP" and max(small.size) == media.THUMB_SIDE and small.size[0] > small.size[1]
    assert studio.c.get(f"/api/media/{mid}").content == original
    assert len(files(env.media_dir)) == 2
    bare = media.save(uid, original, "image/png", "bare.png")      # без превью thumb=1 отдаёт оригинал
    r = studio.c.get(f"/api/media/{bare}?thumb=1")
    assert r.status_code == 200 and r.content == original and r.headers["content-type"] == "image/png"


def test_other_users_media_is_404(studio):
    bob = make_user(studio, "bob"); make_user(studio, "owner"); signin(studio, "owner")
    mid = media.save(bob, DATA, "video/mp4", "clip.mp4")
    for url in (f"/api/media/{mid}", f"/api/media/{mid}?thumb=1", "/api/media/no-such-id"):
        r = studio.c.get(url)
        assert r.status_code == 404 and r.json()["detail"]["code"] == "gone"
    with pytest.raises(HTTPException) as e:
        media.path(make_user(studio, "eve"), mid)
    assert e.value.status_code == 404 and e.value.detail["code"] == "gone"
    assert media.path(bob, mid).read_bytes() == DATA


def test_save_into_unwritable_dir_raises_storage_error(studio, env, monkeypatch):
    uid = make_user(studio, "owner")
    blocker = env.media_dir / "not-a-dir"
    blocker.write_bytes(b"x")
    monkeypatch.setattr(state, "CFG", dataclasses.replace(state.CFG, media_dir=blocker))
    with pytest.raises(media.StorageError) as e:
        media.save(uid, DATA, "video/mp4", "clip.mp4")
    assert str(e.value) == "не удалось сохранить файл: хранилище недоступно"
    assert state.DB.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    assert files(env.media_dir) == [blocker]


def test_save_failure_mid_write_leaves_no_temp_file(studio, env, monkeypatch):
    uid = make_user(studio, "owner")

    def broken_replace(src, dst):
        raise OSError("share went away")

    monkeypatch.setattr(media.os, "replace", broken_replace)
    with pytest.raises(media.StorageError):
        media.save(uid, png(64, 64), "image/png", "p.png", thumb=True)
    assert files(env.media_dir) == []
    assert state.DB.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0


async def chunks(*parts):
    for part in parts:
        yield part


def test_stream_over_limit_leaves_nothing(studio, env):
    uid = make_user(studio, "owner")
    with pytest.raises(media.StorageError) as e:
        asyncio.run(media.save_stream(uid, chunks(b"a" * 8, b"b" * 12), "video/mp4", "clip.mp4", max_bytes=10))
    assert str(e.value) == "файл больше предела"
    assert files(env.media_dir) == []
    assert state.DB.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    mid = asyncio.run(media.save_stream(uid, chunks(DATA[:400], DATA[400:]), "video/mp4", "clip.mp4",
                                        max_bytes=1000))
    assert media.path(uid, mid).read_bytes() == DATA
    assert state.DB.execute("SELECT size FROM media WHERE id = ?", (mid,)).fetchone()["size"] == 1000


def test_download_name_is_attachment(studio):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    mid = media.save(uid, DATA, "video/mp4", "studio-video-42.mp4")
    r = studio.c.get(f"/api/media/{mid}?download=1")
    assert r.headers["content-disposition"] == 'attachment; filename="studio-video-42.mp4"'
    tricky = media.save(uid, DATA, "application/pdf", 'отчёт "q"\r\n.pdf')
    header = studio.c.get(f"/api/media/{tricky}?download=1").headers["content-disposition"]
    assert header.startswith('attachment; filename="') and "\r" not in header and "\n" not in header
    plain = header.split('filename="', 1)[1].split('"', 1)[0]
    assert plain.isascii() and '"' not in plain
    assert "filename*=UTF-8''%D0%BE%D1%82%D1%87%D1%91%D1%82%20%22q%22" in header


def test_history_wipe_removes_files(studio, env):
    uid = make_user(studio, "owner"); bob = make_user(studio, "bob"); signin(studio, "owner")
    mine = media.save(uid, png(500, 300), "image/png", "a.png", thumb=True)
    theirs = media.save(bob, DATA, "video/mp4", "b.mp4")
    assert len(files(env.media_dir)) == 3
    assert studio.c.delete("/api/history").status_code == 204
    assert files(env.media_dir) == [media.path(bob, theirs)]
    assert studio.c.get(f"/api/media/{mine}").status_code == 404
    media.delete(uid, mine)                                     # уже нет — не ошибка
    media.delete(bob, theirs)
    assert files(env.media_dir) == []
    assert state.DB.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
