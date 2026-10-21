"""Хранилище: свой объём и очередь удаления (`GET /api/me/storage`, profile.py), доступность шары,
возраст копии базы и размер очереди для администратора (`GET /api/admin/storage`, admin.py)."""
import logging
import os
import shutil
import threading

import pytest

import admin
import media
import state
from conftest import T0, make_user, signin


def add_media(uid, media_id, mime, size, name="secret-name.bin"):
    state.DB.execute("INSERT INTO media(id, user_id, rel_path, mime, size, has_thumb, download_name, created)"
                     " VALUES (?, ?, ?, ?, ?, 0, ?, ?)", (media_id, uid, f"u{uid}/2027/01/{media_id}", mime, size,
                                                          name, T0))


def queue_at(uid, media_id, when):
    state.DB.execute("INSERT INTO purge(media_id, user_id, rel_path, queued) VALUES (?, ?, ?, ?)",
                     (media_id, uid, f"u{uid}/2027/01/{media_id}.png", when))


@pytest.fixture
def probe(monkeypatch):
    """Проба шары — заново в каждом тесте (зависшая из прошлого теста не переходит в следующий)."""
    monkeypatch.setattr(admin, "PROBE", None)


# ---- свой объём ---------------------------------------------------------------

def test_storage_counts_own_files_by_kind(studio):
    me = make_user(studio, "owner"); other = make_user(studio, "bob"); signin(studio, "owner")
    add_media(me, "a", "image/png", 100)
    add_media(me, "b", "image/webp", 50)
    add_media(me, "c", "video/mp4", 1000)
    add_media(me, "d", "audio/mpeg", 300)
    add_media(me, "e", "application/pdf", 20)
    add_media(me, "f", "application/json", 5)            # итог расшифровки
    add_media(me, "g", media.DOCX, 7)
    add_media(me, "h", "application/octet-stream", 1)
    add_media(other, "x", "image/png", 9999)              # чужое не в счёт
    r = studio.c.get("/api/me/storage")
    assert r.status_code == 200
    body = r.json()
    assert body["kinds"] == [{"kind": "image", "files": 2, "bytes": 150}, {"kind": "video", "files": 1, "bytes": 1000},
                             {"kind": "audio", "files": 1, "bytes": 300}, {"kind": "document", "files": 3, "bytes": 32},
                             {"kind": "other", "files": 1, "bytes": 1}]
    assert body["total"] == {"files": 8, "bytes": 1483}
    assert body["pending"] == {"files": 0, "since": None}
    assert "secret-name" not in r.text and "u1/" not in r.text


def test_storage_of_an_empty_history_is_zeros(studio):
    make_user(studio, "owner"); signin(studio, "owner")
    body = studio.c.get("/api/me/storage").json()
    assert [k["files"] for k in body["kinds"]] == [0] * 5 and body["total"] == {"files": 0, "bytes": 0}


def test_storage_shows_only_own_queue_and_its_oldest_path(studio):
    """Ждут удаления: свои пути, кроме тех, что пишутся сейчас (SAVING); чужие не в счёт."""
    me = make_user(studio, "owner"); other = make_user(studio, "bob"); signin(studio, "owner")
    queue_at(me, "late", T0 - 10)
    queue_at(me, "early", T0 - 500)
    queue_at(me, "writing", T0 - 9000)
    media.SAVING.add("writing")
    queue_at(other, "theirs", T0 - 99999)
    r = studio.c.get("/api/me/storage")
    assert r.json()["pending"] == {"files": 2, "since": T0 - 500}
    assert "late" not in r.text and "u1/" not in r.text
    assert media.pending(me) == 2


def test_storage_needs_a_session(studio):
    assert studio.c.get("/api/me/storage").status_code == 401


# ---- администратор --------------------------------------------------------------

def backups(env, *days):
    folder = env.media_dir / "backup"
    folder.mkdir(exist_ok=True)
    for name, mtime in days:
        (folder / name).write_bytes(b"db")
        os.utime(folder / name, (mtime, mtime))


def test_admin_storage_shows_the_share_backup_age_and_queue(studio, env, probe):
    make_user(studio, "owner", is_admin=True); bob = make_user(studio, "bob"); signin(studio, "owner")
    backups(env, ("studio-20270113.db", T0 - 90000), ("studio-20270114.db", T0 - 3600),
            (".studio-20270115.db.part", T0 - 60), ("notes.txt", T0))
    queue_at(bob, "m1", T0 - 700)
    queue_at(1, "m2", T0 - 70)
    r = studio.c.get("/api/admin/storage")
    assert r.status_code == 200
    body = r.json()
    assert body["share"]["state"] == "ok" and body["share"]["free"] > 0 and body["share"]["total"] >= body["share"]["free"]
    assert body["backup"] == {"state": "ok", "copies": 2, "at": T0 - 3600, "age": 3600}
    assert body["pending"] == {"files": 2, "since": T0 - 700}
    # чужой истории и пользователей в ответе нет
    assert "bob" not in r.text and "u2/" not in r.text and "m1" not in r.text


def test_admin_storage_without_copies_says_so(studio, probe):
    make_user(studio, "owner", is_admin=True); signin(studio, "owner")
    body = studio.c.get("/api/admin/storage").json()
    assert body["share"]["state"] == "ok" and body["backup"] == {"state": "none"}


def test_admin_storage_with_the_share_gone_answers_in_words(studio, env, probe, caplog):
    make_user(studio, "owner", is_admin=True); signin(studio, "owner")
    shutil.rmtree(env.media_dir)                          # шара не смонтирована — каталога нет
    with caplog.at_level(logging.WARNING, logger="studio"):
        r = studio.c.get("/api/admin/storage")
    assert r.status_code == 200
    assert r.json()["share"] == {"state": "down", "message": "хранилище недоступно"}
    assert r.json()["backup"] == {"state": "unknown"}
    assert caplog.records and str(env.media_dir) not in caplog.text and "media" not in caplog.text


def test_admin_storage_with_a_stalled_share_answers_in_time_and_probes_once(studio, monkeypatch, probe, caplog):
    """Зависшая CIFS: ответ через PROBE_SEC словами; пока проба висит, новая не начинается (потоки не
    копятся); ответила — следующий запрос берёт свежую."""
    make_user(studio, "owner", is_admin=True); signin(studio, "owner")
    monkeypatch.setattr(admin, "PROBE_SEC", 0.05)
    go, calls, real = threading.Event(), [], admin._probe

    def hung(media_dir):
        calls.append(1)
        go.wait(5)
        return real(media_dir)

    monkeypatch.setattr(admin, "_probe", hung)
    with caplog.at_level(logging.WARNING, logger="studio"):
        for _ in range(2):
            r = studio.c.get("/api/admin/storage")
            assert r.status_code == 200
            assert r.json()["share"] == {"state": "stalled", "message": "хранилище не отвечает"}
            assert r.json()["backup"] == {"state": "unknown"}
    assert calls == [1]
    go.set()
    admin.PROBE.result(5)
    assert studio.c.get("/api/admin/storage").json()["share"]["state"] == "ok"
    assert calls == [1, 1]
    assert "media" not in caplog.text


def test_admin_storage_is_only_for_admins(studio, probe):
    make_user(studio, "bob"); signin(studio, "bob")
    assert studio.c.get("/api/admin/storage").status_code == 403
