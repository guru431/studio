"""Файлы истории на шаре (app/media.py): запись, превью, чтение с диапазонами, удаление."""
import asyncio
import dataclasses
import io
import logging
import sqlite3
import threading
import time
import uuid
import zipfile
from datetime import datetime

import pytest
from fastapi import HTTPException
from PIL import Image

import auth
import media
import state
from conftest import T0, make_user, signin

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


def test_file_part_needs_no_database(studio, env, monkeypatch):
    """write() идёт в asyncio.to_thread и базы не касается; строку ставит insert() на цикле."""
    uid = make_user(studio, "owner")
    db = state.DB
    monkeypatch.setattr(state, "DB", None)
    written = media.write(uid, png(64, 64), "image/png", "p.png", thumb=True)
    monkeypatch.setattr(state, "DB", db)
    assert len(files(env.media_dir)) == 2 and db.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    mid = media.insert(written)
    assert media.path(uid, mid).read_bytes() == png(64, 64)
    assert asyncio.run(media.save_async(uid, DATA, "video/mp4", "c.mp4")) in {
        r["id"] for r in db.execute("SELECT id FROM media")}


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


def test_history_wipe_removes_files(studio, env, unlinks):
    uid = make_user(studio, "owner"); bob = make_user(studio, "bob"); signin(studio, "owner")
    mine = media.save(uid, png(500, 300), "image/png", "a.png", thumb=True)
    theirs = media.save(bob, DATA, "video/mp4", "b.mp4")
    assert len(files(env.media_dir)) == 3
    r = studio.c.delete("/api/history")
    assert r.status_code == 200 and r.json() == {"pending_files": 0}
    assert files(env.media_dir) == [media.path(bob, theirs)]
    assert studio.c.get(f"/api/media/{mine}").status_code == 404
    asyncio.run(media.delete_async(uid, mine))                  # уже нет — не ошибка
    asyncio.run(media.delete_async(bob, theirs))
    assert files(env.media_dir) == []
    assert state.DB.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    assert unlinks and not any(unlinks)                         # ни одного удаления с цикла событий


def test_history_wipe_removes_all_files_in_one_thread_call(studio, env, monkeypatch):
    """Тысячи файлов истории — одним вызовом в потоке, не по строке с возвратом на цикл (ревью A2-I2);
    файл, который не удалился, строки не держит — его путь ждёт в очереди удаления."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    ids = {media.save(uid, png(64, 64), "image/png", f"{n}.png", thumb=True) for n in range(3)}
    calls, real = [], media.remove_files

    def spy(rows):
        rows = list(rows)
        calls.append({media_id for media_id, _ in rows})
        return real(rows[1:]) + rows[:1]             # первая строка «не удалилась»

    monkeypatch.setattr(media, "remove_files", spy)
    assert studio.c.delete("/api/history").json() == {"pending_files": 1}
    assert calls == [ids]
    assert state.DB.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    assert len(files(env.media_dir)) == 2                       # файл и превью первой строки остались
    assert state.DB.execute("SELECT COUNT(*) FROM purge").fetchone()[0] == 1


def test_attachment_uploaded_during_the_wipe_leaves_no_orphan(studio, env, monkeypatch):
    """Вложение, загруженное из другой вкладки, пока стирание удаляет файлы: ни строки без файла,
    ни файла без строки (ревью N1)."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    media.save(uid, png(20, 10), "image/png", "old.png", thumb=True)
    real, uploads = media.remove_files, []

    def racing(rows):                                # поток стирания: цикл свободен
        if not uploads:
            uploads.append(studio.c.post("/api/attachments",
                                         files={"file": ("p.png", png(20, 10), "image/png")}).status_code)
        return real(rows)

    monkeypatch.setattr(media, "remove_files", racing)
    assert studio.c.delete("/api/history").status_code == 200
    assert uploads == [201]
    assert state.DB.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    assert files(env.media_dir) == []


def broken(*args):
    raise OSError("share went away")


def queued():
    return [tuple(r) for r in state.DB.execute("SELECT media_id, user_id, tries FROM purge ORDER BY media_id")]


def test_delete_queues_the_path_when_the_share_fails(studio, env, monkeypatch, unlinks):
    """Шара отказала при удалении файла: строки media уже нет (исходником его не взять), путь — в
    очереди удаления; проход purge доудаляет, когда шара вернулась (находка аудита P1)."""
    uid = make_user(studio, "owner")
    mid = media.save(uid, png(64, 64), "image/png", "a.png", thumb=True)
    real = media._remove
    monkeypatch.setattr(media, "_remove", broken)
    asyncio.run(media.delete_async(uid, mid))                    # не ошибка: путь не потерян
    assert state.DB.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    assert queued() == [(mid, uid, 1)] and len(files(env.media_dir)) == 2
    asyncio.run(media.purge())                                   # шара всё ещё недоступна
    assert queued() == [(mid, uid, 2)]
    monkeypatch.setattr(media, "_remove", real)
    asyncio.run(media.purge())
    assert files(env.media_dir) == [] and queued() == []
    assert unlinks and not any(unlinks)


def test_wipe_keeps_the_paths_the_share_refused_until_it_is_back(studio, env, monkeypatch):
    """Шара отказала посреди «Удалить всю историю»: история уходит, пути файлов ждут в очереди, ответ
    говорит, сколько их; следующее стирание доудаляет (находка аудита P1)."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    kept = media.save(uid, png(64, 64), "image/png", "a.png", thumb=True)
    media.save(uid, DATA, "video/mp4", "b.mp4")
    real = media._remove

    def refusing(media_id, rel_path):
        if media_id == kept:
            raise OSError("share went away")
        real(media_id, rel_path)

    monkeypatch.setattr(media, "_remove", refusing)
    r = studio.c.delete("/api/history")
    assert r.status_code == 200 and r.json() == {"pending_files": 1}
    assert state.DB.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    assert queued() == [(kept, uid, 0)] and len(files(env.media_dir)) == 2     # файл и превью — ещё на шаре
    assert studio.c.delete("/api/history").json() == {"pending_files": 1}      # шара всё ещё недоступна
    assert queued() == [(kept, uid, 1)]
    monkeypatch.setattr(media, "_remove", real)
    assert studio.c.delete("/api/history").json() == {"pending_files": 0}
    assert files(env.media_dir) == [] and queued() == []


def test_files_of_a_deleted_account_wait_in_the_queue(studio, env, monkeypatch):
    """Удаление учётки при недоступной шаре: учётки нет, а пути её файлов — в очереди (без каскада),
    ответ говорит, сколько их; проход purge доудаляет (находка аудита P1)."""
    make_user(studio, "owner", is_admin=True); bob = make_user(studio, "bob"); signin(studio, "owner")
    mid = media.save(bob, png(64, 64), "image/png", "a.png", thumb=True)
    real = media._remove
    monkeypatch.setattr(media, "_remove", broken)
    r = studio.c.delete(f"/api/admin/users/{bob}")
    assert r.status_code == 200 and r.json() == {"pending_files": 1}
    assert state.DB.execute("SELECT COUNT(*) FROM users WHERE id = ?", (bob,)).fetchone()[0] == 0
    assert queued() == [(mid, bob, 0)]
    monkeypatch.setattr(media, "_remove", real)
    asyncio.run(media.purge())
    assert files(env.media_dir) == [] and queued() == []


@pytest.mark.parametrize("where", ["before", "between", "after"])
def test_a_cancelled_save_leaves_no_file_without_a_row(studio, env, monkeypatch, where):
    """Отмена ожидания save_async поток записи не останавливает: он дописывает оригинал и превью, а
    строки media нет. Путь ждёт в очереди удаления, проход purge не трогает его, пока поток пишет, и
    убирает после (находка аудита P2). Отмена — до записи, между оригиналом и превью, после записи."""
    uid = make_user(studio, "owner")
    reached, go = threading.Event(), threading.Event()

    def hold():
        reached.set()
        go.wait(5)

    if where == "before":
        real_thumb = media._thumbnail
        monkeypatch.setattr(media, "_thumbnail", lambda data: (hold(), real_thumb(data))[1])
    elif where == "between":
        real_replace, done = media.os.replace, []

        def replace(src, dst):
            real_replace(src, dst)
            done.append(dst)
            if len(done) == 1:
                hold()

        monkeypatch.setattr(media.os, "replace", replace)
    else:
        real_write = media.write
        monkeypatch.setattr(media, "write", lambda *a, **kw: (real_write(*a, **kw), hold())[0])

    async def scenario():
        task = asyncio.create_task(media.save_async(uid, png(64, 64), "image/png", "p.png", thumb=True))
        assert await asyncio.to_thread(reached.wait, 5)
        await media.purge()                        # запись идёт — проход её не трогает
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        go.set()
        for _ in range(5000):                      # поток дописывает уже после отмены
            if not media.SAVING:
                break
            await asyncio.sleep(0.001)
        written = files(env.media_dir)
        await media.purge()
        return written

    assert len(asyncio.run(scenario())) == 2
    assert state.DB.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    assert files(env.media_dir) == [] and queued() == []


def test_a_stream_is_queued_until_its_row_is_in(studio, env):
    """save_stream: пока строки media нет, путь — в очереди (остановка службы посреди потока оставила
    бы временный файл — его уберёт проход purge при старте); строка снимает путь с очереди."""
    uid = make_user(studio, "owner")
    seen = []

    async def watched():
        yield DATA[:400]
        seen.extend(r[0] for r in queued())
        yield DATA[400:]

    mid = asyncio.run(media.save_stream(uid, watched(), "video/mp4", "clip.mp4", max_bytes=1000))
    assert seen == [mid] and queued() == []
    leftover = env.media_dir / f"u{uid}" / "2027" / "01" / ".x.mp4.tmp"       # оборванная запись
    leftover.write_bytes(b"x")
    media.queue(uid, [("x", f"u{uid}/2027/01/x.mp4")])
    asyncio.run(media.purge())
    assert files(env.media_dir) == [media.path(uid, mid)] and queued() == []


@pytest.mark.parametrize("where", ["open", "write", "replace"])
def test_a_cancelled_stream_leaves_no_file_without_a_row(studio, env, monkeypatch, where):
    """Отмена save_stream, пока шаг в потоке идёт (создание файла, запись куска, перенос на место), и
    проход очереди тут же: путь в SAVING, пока поток не кончил, — проход его не трогает, файлы
    убирает сама запись, а следующий проход — путь (хвост правки очереди удаления)."""
    uid = make_user(studio, "owner")
    reached, go = threading.Event(), threading.Event()

    def hold():
        reached.set()
        go.wait(5)

    if where == "replace":
        real_replace = media.os.replace
        monkeypatch.setattr(media.os, "replace", lambda src, dst: (hold(), real_replace(src, dst)))
    else:
        class Held:
            def __init__(self, f):
                self.f = f

            def write(self, data):
                hold()
                return self.f.write(data)

            def close(self):
                self.f.close()

        def held_open(path, mode):
            if where == "open":
                hold()
            f = open(path, mode)
            return Held(f) if where == "write" else f

        monkeypatch.setattr(media, "open", held_open, raising=False)

    async def scenario():
        task = asyncio.create_task(media.save_stream(uid, chunks(DATA), "video/mp4", "clip.mp4", max_bytes=1000))
        assert await asyncio.to_thread(reached.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await media.purge()                        # поток ещё идёт — проход путь не трогает
        go.set()
        for _ in range(5000):
            if not media.SAVING:
                break
            await asyncio.sleep(0.001)
        await media.purge()

    asyncio.run(scenario())
    assert files(env.media_dir) == [] and queued() == []
    assert state.DB.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0


@pytest.mark.parametrize("how", ["async", "stream"])
def test_a_stalled_share_answers_in_time_and_the_queue_takes_the_file_later(studio, env, monkeypatch, how):
    """Зависшая шара: запись не дождались за SAVE_TIMEOUT_SEC — StorageError «не отвечает» сразу, а не
    502 Apache через 150 с; поток дописывает сам, проход очереди убирает записанное после него."""
    uid = make_user(studio, "owner")
    monkeypatch.setattr(media, "SAVE_TIMEOUT_SEC", 0.05)
    go = threading.Event()
    if how == "async":
        real_write = media.write
        monkeypatch.setattr(media, "write", lambda *a, **kw: (go.wait(5), real_write(*a, **kw))[1])
        save = media.save_async(uid, png(64, 64), "image/png", "p.png", thumb=True)
    else:
        real_replace = media.os.replace
        monkeypatch.setattr(media.os, "replace", lambda src, dst: (go.wait(5), real_replace(src, dst)))
        save = media.save_stream(uid, chunks(DATA), "video/mp4", "clip.mp4", max_bytes=1000)

    async def scenario():
        with pytest.raises(media.StorageError) as e:
            await save
        assert str(e.value) == media.STALLED
        await media.purge()                        # поток ещё пишет — проход путь не трогает
        assert queued() and media.SAVING
        go.set()
        for _ in range(5000):
            if not media.SAVING:
                break
            await asyncio.sleep(0.001)
        await media.purge()

    asyncio.run(scenario())
    assert files(env.media_dir) == [] and queued() == []
    assert state.DB.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0


def test_a_stalled_share_is_a_503_in_words(studio, monkeypatch):
    make_user(studio, "owner"); signin(studio, "owner")
    monkeypatch.setattr(media, "SAVE_TIMEOUT_SEC", 0.05)
    go, real_write = threading.Event(), media.write
    monkeypatch.setattr(media, "write", lambda *a, **kw: (go.wait(5), real_write(*a, **kw))[1])
    r = studio.c.post("/api/attachments", files={"file": ("p.png", png(20, 10), "image/png")})
    go.set()
    assert r.status_code == 503 and r.json()["detail"] == {"code": "storage", "message": media.STALLED}
    for _ in range(5000):                          # поток дописывает, пока цикл службы жив
        if not media.SAVING:
            break
        time.sleep(0.001)
    assert not media.SAVING


def test_a_refused_row_takes_the_files_away_off_the_loop(studio, env, unlinks):
    """Строка media не встала (учётки уже нет — внешний ключ): файлы удаляются в потоке, не с цикла
    событий, и путь уходит с очереди (хвост правки очереди удаления)."""
    for save in (lambda: media.save_async(999, png(64, 64), "image/png", "p.png", thumb=True),
                 lambda: media.save_stream(999, chunks(DATA), "video/mp4", "clip.mp4", max_bytes=1000)):
        with pytest.raises(sqlite3.IntegrityError):
            asyncio.run(save())
    assert files(env.media_dir) == [] and queued() == []
    assert unlinks and not any(unlinks)


def test_bulk_file_removal_needs_no_database(studio, env, monkeypatch):
    """remove_files идёт в asyncio.to_thread (стирание истории): базы не касается, неудачи возвращает."""
    uid = make_user(studio, "owner")
    ids = [media.save(uid, png(64, 64), "image/png", f"{n}.png", thumb=True) for n in range(3)]
    rows = [tuple(r) for r in state.DB.execute("SELECT id, rel_path FROM media ORDER BY id")]
    rows.append(("gone", f"u{uid}/2027/01/gone.png"))                        # файла нет — не неудача
    db = state.DB
    monkeypatch.setattr(state, "DB", None)
    assert media.remove_files(rows) == []
    monkeypatch.setattr(state, "DB", db)
    assert files(env.media_dir) == [] and len(ids) == 3
    blocker = env.media_dir / f"u{uid}" / "2027" / "01" / "dir.png"
    blocker.mkdir(parents=True)                                              # каталог не удалить как файл
    row = ("dir", blocker.relative_to(env.media_dir).as_posix())
    assert media.remove_files([row]) == [row]


# ---- архив ZIP выделенного ----------------------------------------------------

def archive(studio, ids):
    return studio.c.post("/api/media/archives", json={"media_ids": ids})


def test_archive_streams_the_chosen_files_in_order(studio, caplog):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    a = media.save(uid, png(64, 64), "image/png", "studio-lustify-42.png", thumb=True)
    b = media.save(uid, DATA, "video/mp4", "studio-video-7.mp4")
    c = media.save(uid, png(32, 32), "image/png", "studio-lustify-42.png")
    media.save(uid, DATA, "video/mp4", "not-chosen.mp4")
    with caplog.at_level(logging.INFO, logger="studio"):
        r = archive(studio, [b, a, c, a])
        assert r.status_code == 200 and r.json()["files"] == 3
        z = studio.c.get(f"/api/media/archives/{r.json()['id']}")
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    day = datetime.fromtimestamp(T0).strftime("%Y-%m-%d")
    assert z.headers["content-disposition"] == f'attachment; filename="studio-{day}.zip"'
    assert z.headers["cache-control"] == "no-store" and z.headers["x-content-type-options"] == "nosniff"
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    assert zf.testzip() is None
    assert zf.namelist() == ["studio-video-7.mp4", "studio-lustify-42.png", "studio-lustify-42-2.png"]
    assert {i.compress_type for i in zf.infolist()} == {zipfile.ZIP_STORED}
    assert zf.read("studio-video-7.mp4") == DATA and zf.read("studio-lustify-42-2.png") == png(32, 32)
    assert "archive: 3" in caplog.text
    for secret in ("studio-lustify", "studio-video", a, b, c):
        assert secret not in caplog.text


def test_archive_names_are_safe_inside(studio):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    ids = [media.save(uid, DATA, "application/pdf", name) for name in ("../отчёт\r\n.pdf", ".hidden", "")]
    token = archive(studio, ids).json()["id"]
    names = zipfile.ZipFile(io.BytesIO(studio.c.get(f"/api/media/archives/{token}").content)).namelist()
    assert names == ["__отчёт__.pdf", "_hidden", "file"]          # ведущие точки — одним «_»


def test_archive_takes_only_own_known_ids(studio):
    bob = make_user(studio, "bob"); uid = make_user(studio, "owner"); signin(studio, "owner")
    mine, theirs = media.save(uid, DATA, "video/mp4", "m.mp4"), media.save(bob, DATA, "video/mp4", "t.mp4")
    assert archive(studio, [theirs, uuid.uuid4().hex, mine]).json()["files"] == 1
    r = archive(studio, [theirs])
    assert r.status_code == 404 and r.json()["detail"]["code"] == "gone"


def test_archive_refuses_bad_lists(studio):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    mine = media.save(uid, DATA, "video/mp4", "m.mp4")
    for bad in ([], ["../etc"], [mine.upper()], [uuid.uuid4().hex for _ in range(media.ARCHIVE_MAX + 1)]):
        r = archive(studio, bad)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid", bad
    assert archive(studio, [mine] * (media.ARCHIVE_MAX + 1)).json()["files"] == 1   # повторы схлопнуты


def test_archive_token_is_personal_reusable_and_expires(studio):
    uid = make_user(studio, "owner"); make_user(studio, "bob"); signin(studio, "owner")
    token = archive(studio, [media.save(uid, DATA, "video/mp4", "m.mp4")]).json()["id"]
    url = f"/api/media/archives/{token}"
    assert studio.c.get(url).status_code == 200 and studio.c.get(url).status_code == 200
    studio.clock[0] += media.ARCHIVE_TTL_SEC + 1
    assert studio.c.get(url).status_code == 404
    token = archive(studio, [media.save(uid, DATA, "video/mp4", "n.mp4")]).json()["id"]
    signin(studio, "bob")
    r = studio.c.get(f"/api/media/archives/{token}")
    assert r.status_code == 404 and r.json()["detail"]["code"] == "gone"


def test_archive_keeps_few_tokens_per_user(studio):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    mid = media.save(uid, DATA, "video/mp4", "m.mp4")
    tokens = [archive(studio, [mid]).json()["id"] for _ in range(media.ARCHIVE_KEEP + 1)]
    assert studio.c.get(f"/api/media/archives/{tokens[0]}").status_code == 404
    assert studio.c.get(f"/api/media/archives/{tokens[-1]}").status_code == 200
    assert len(media.ARCHIVES) == media.ARCHIVE_KEEP


def test_archive_skips_rows_and_files_gone_after_the_list(studio, env):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    a, b, c = (media.save(uid, DATA, "video/mp4", f"{n}.mp4") for n in "abc")
    token = archive(studio, [a, b, c]).json()["id"]
    asyncio.run(media.delete_async(uid, a))                      # строка ушла после POST
    media.path(uid, b).unlink()                                  # файла нет, строка есть
    names = zipfile.ZipFile(io.BytesIO(studio.c.get(f"/api/media/archives/{token}").content)).namelist()
    assert names == ["c.mp4"]
    for mid in (b, c):                                           # строк не осталось — 404
        asyncio.run(media.delete_async(uid, mid))
    assert studio.c.get(f"/api/media/archives/{token}").status_code == 404


def test_archive_with_the_share_down_is_503_in_words(studio, env, monkeypatch):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    mid = media.save(uid, DATA, "video/mp4", "m.mp4")
    real = media.Path.stat

    def stat(self, **kwargs):
        if env.media_dir in self.parents:
            raise OSError("share went away")
        return real(self, **kwargs)

    monkeypatch.setattr(media.Path, "stat", stat)
    r = archive(studio, [mid])
    assert r.status_code == 503 and r.json()["detail"]["code"] == "storage"


def test_archive_read_failure_breaks_the_answer_without_names(studio, env, monkeypatch, caplog):
    """Шара оборвала чтение посреди файла: ответ рвётся (а не кончается «целым» архивом без хвоста),
    в журнале — ни пути, ни id, ни имени."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    mid = media.save(uid, DATA * 200, "video/mp4", "studio-video-9.mp4")
    token = archive(studio, [mid]).json()["id"]
    real_open = open

    class Broken:
        def __init__(self, f):
            self.f, self.reads = f, 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.f.close()

        def fileno(self):
            return self.f.fileno()

        def read(self, n):
            self.reads += 1
            if self.reads > 1:
                raise OSError(f"[Errno 5] Input/output error: '{self.f.name}'")
            return self.f.read(n)

    monkeypatch.setattr(media, "open", lambda file, mode="r": Broken(real_open(file, mode)), raising=False)
    monkeypatch.setattr(media, "CHUNK", 1000)
    with caplog.at_level(logging.INFO, logger="studio"), pytest.raises(media.StorageError):
        studio.c.get(f"/api/media/archives/{token}")
    assert "оборвало чтение" in caplog.text
    for secret in (mid, "studio-video", str(env.media_dir)):
        assert secret not in caplog.text


def test_archive_needs_a_session_and_csrf(studio):
    uid = make_user(studio, "owner")
    mid = media.save(uid, DATA, "video/mp4", "m.mp4")
    assert archive(studio, [mid]).status_code == 401
    csrf = signin(studio, "owner")
    del studio.c.headers[auth.CSRF_HEADER]
    assert archive(studio, [mid]).status_code == 403
    studio.c.headers[auth.CSRF_HEADER] = csrf
    assert archive(studio, [mid]).status_code == 200
