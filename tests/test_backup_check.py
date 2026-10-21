"""Проверка копии базы (manage.verify_backup, `manage.py verify-backup`, панель `/api/admin/backup-check`;
спека 2026-10-21-studio-export-restore-check, раздел 6)."""
import hashlib
import json
import logging
import shutil
import sqlite3
import threading
import time

import pytest
from cryptography.fernet import Fernet

import admin
import clock
import config
import db
import manage
import security
import state
from conftest import T0, make_user, signin


def add_rows(conn, users=1, tokens=3, files=(("a.png", True), ("b.png", True), ("gone.png", False))):
    for n in range(users):
        conn.execute("INSERT INTO users(login, name, pw_hash, created) VALUES (?, ?, 'x', ?)",
                     (f"secret-login-{n}", "name", T0))
    uid = conn.execute("SELECT MIN(id) FROM users").fetchone()[0]
    for n in range(tokens):
        conn.execute("INSERT INTO connections(user_id, name, kind, base_url, token_enc, sections, created)"
                     " VALUES (?, ?, 'openai', 'https://x.test/v1', ?, '[]', ?)",
                     (uid, f"c{n}", security.encrypt(f"key-{n}"), T0))
    for n, (name, present) in enumerate(files):
        rel = f"u{uid}/2027/01/{name}"
        conn.execute("INSERT INTO media(id, user_id, rel_path, mime, size, has_thumb, download_name, created)"
                     " VALUES (?, ?, ?, 'image/png', 1, 0, 'x', ?)", (f"m{n}", uid, rel, T0))
        if present:
            full = state.CFG.media_dir / rel
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(b"x")


@pytest.fixture
def base(env, monkeypatch):
    """Рабочая база с учёткой, тремя токенами и тремя файлами (одного на шаре нет)."""
    monkeypatch.setattr(state, "CFG", config.load())
    conn = db.connect(env.db_path)
    db.migrate(conn)
    yield conn
    conn.close()


def backup_of(conn, env, monkeypatch):
    monkeypatch.setattr(clock, "NOW", lambda: 1_792_454_400.0)   # 2026-10-20 00:00 UTC
    assert manage.main(["backup"]) == 0
    return env.media_dir / "backup" / "studio-20261020.db"


def tmp_files(env):
    folder = state.CFG.tmp_dir
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_verify_backup_of_a_whole_copy(base, env, monkeypatch):
    add_rows(base)
    copy = backup_of(base, env, monkeypatch)
    working = digest(env.db_path)
    out = manage.verify_backup()
    assert out["ok"] is True and out["problems"] == []
    assert out["backup"] == "studio-20261020.db" and out["size"] == copy.stat().st_size
    assert out["at"] == copy.stat().st_mtime
    assert out["integrity"] == "ok" and out["schema_from"] == out["schema_to"] == db.VERSION and out["users"] == 1
    assert out["tokens"] == {"ok": 3, "failed": 0}
    assert out["files"] == {"state": "ok", "present": 2, "missing": 1, "checked": 3, "total": 3}
    assert digest(env.db_path) == working                          # рабочая база не тронута
    assert tmp_files(env) == []                                    # временной копии не осталось
    text = json.dumps(out)
    assert "secret-login" not in text and "u1/" not in text and "key-" not in text


def test_verify_backup_of_an_old_schema_steps_it_up(base, env):
    """Копия прежней версии: шаги db.STEPS на временной копии проходят — «схема 8 → VERSION»."""
    folder = env.media_dir / "backup"
    folder.mkdir()
    old = sqlite3.connect(folder / "studio-20270101.db")
    old.executescript(db.SCHEMA.split(db.INDEXES_9)[0] + "\nPRAGMA user_version = 8;")   # шаги 9+ — после INDEXES_9
    old.execute("INSERT INTO users(login, name, pw_hash, created) VALUES ('a', 'a', 'x', 1)")
    old.commit()
    old.close()
    out = manage.verify_backup()
    assert out["ok"] is True and (out["schema_from"], out["schema_to"]) == (8, db.VERSION)
    assert sqlite3.connect(folder / "studio-20270101.db").execute("PRAGMA user_version").fetchone()[0] == 8


def test_verify_backup_of_garbage_fails_and_cleans_up(base, env):
    folder = env.media_dir / "backup"
    folder.mkdir()
    (folder / "studio-20270101.db").write_bytes(b"not a database at all " * 300)
    out = manage.verify_backup()
    assert out["ok"] is False and out["integrity"] == "broken" and out["problems"]
    assert tmp_files(env) == []


def test_verify_backup_of_a_damaged_copy_fails(base, env, monkeypatch):
    add_rows(base, files=())
    copy = backup_of(base, env, monkeypatch)
    raw = bytearray(copy.read_bytes())
    raw[100:4096] = b"\xa5" * (4096 - 100)                         # страница схемы испорчена, заголовок цел
    copy.write_bytes(bytes(raw))
    out = manage.verify_backup()
    assert out["ok"] is False and out["integrity"] == "broken"
    assert tmp_files(env) == []


def test_verify_backup_of_an_empty_base_is_not_a_pass(base, env):
    """Схема 0 — пустой или чужой файл: migrate налил бы в него свежую схему и «прошёл»."""
    folder = env.media_dir / "backup"
    folder.mkdir()
    other = sqlite3.connect(folder / "studio-20270101.db")
    other.execute("CREATE TABLE notes(x)")
    other.close()
    out = manage.verify_backup()
    assert out["ok"] is False and out["schema_from"] == 0 and out["schema_to"] is None
    assert any("пуст" in p for p in out["problems"])


def test_verify_backup_without_users_is_not_a_pass(base, env, monkeypatch):
    backup_of(base, env, monkeypatch)
    out = manage.verify_backup()
    assert out["ok"] is False and out["users"] == 0 and any("учёт" in p for p in out["problems"])


def test_verify_backup_with_another_key_cannot_read_the_tokens(base, env, monkeypatch):
    add_rows(base)
    backup_of(base, env, monkeypatch)
    import dataclasses
    monkeypatch.setattr(state, "CFG", dataclasses.replace(state.CFG, data_key=Fernet.generate_key().decode()))
    out = manage.verify_backup()
    assert out["ok"] is False and out["tokens"] == {"ok": 0, "failed": 3}
    assert any("STUDIO_DATA_KEY" in p for p in out["problems"])


def test_verify_backup_with_the_share_down(base, env, monkeypatch, tmp_path):
    """Копия — по явному пути, шары нет: «шара недоступна», файлы не проверены, итог — провал."""
    add_rows(base)
    copy = backup_of(base, env, monkeypatch)
    moved = tmp_path / "copy.db"
    copy.replace(moved)
    import shutil
    shutil.rmtree(env.media_dir)
    out = manage.verify_backup(moved)
    assert out["ok"] is False and out["files"]["state"] == "down" and out["files"]["checked"] == 0
    assert out["tokens"] == {"ok": 3, "failed": 0}
    assert any("шара" in p for p in out["problems"])


def test_verify_backup_checks_files_only_for_its_time(base, env, monkeypatch):
    add_rows(base)
    backup_of(base, env, monkeypatch)
    ticks = iter(range(0, 10_000, 100))
    monkeypatch.setattr(clock, "NOW", lambda: float(next(ticks)))
    monkeypatch.setattr(manage, "VERIFY_FILES_SEC", 250)
    out = manage.verify_backup()
    assert out["files"]["state"] == "partial" and 0 < out["files"]["checked"] < 3 and out["files"]["total"] == 3
    assert out["ok"] is True                                     # шара отвечает, проверено не всё — не провал


def test_verify_backup_file_time_starts_after_the_copy(base, env, monkeypatch):
    """Срок VERIFY_FILES_SEC — на файлы: медленная копия и migrate его не съедают («проверено 0» при ok)."""
    add_rows(base)
    backup_of(base, env, monkeypatch)
    now = [0.0]
    monkeypatch.setattr(clock, "NOW", lambda: now[0])
    real = shutil.copyfile

    def slow_copy(src, dst):
        now[0] += manage.VERIFY_FILES_SEC + 1
        return real(src, dst)

    monkeypatch.setattr(shutil, "copyfile", slow_copy)
    out = manage.verify_backup()
    assert out["files"] == {"state": "ok", "present": 2, "missing": 1, "checked": 3, "total": 3}
    assert out["ok"] is True and out["took"] > manage.VERIFY_FILES_SEC


def test_verify_backup_without_room_for_the_copy_does_not_copy(base, env, monkeypatch):
    """Временная копия — на диске рабочей базы: места меньше копии с запасом — не копировать (ENOSPC уронил
    бы запись рабочей базы), «мало места», провал."""
    add_rows(base)
    copy = backup_of(base, env, monkeypatch)
    room = copy.stat().st_size + manage.VERIFY_RESERVE - 1
    monkeypatch.setattr(shutil, "disk_usage", lambda path: shutil._ntuple_diskusage(10 ** 12, 0, room))
    copied = []
    monkeypatch.setattr(shutil, "copyfile", lambda src, dst: copied.append(dst))
    out = manage.verify_backup()
    assert out["ok"] is False and copied == [] and out["integrity"] is None
    assert out["backup"] == "studio-20261020.db" and any("мало места" in p for p in out["problems"])
    assert tmp_files(env) == []


def test_verify_backup_command_codes(base, env, monkeypatch, capsys, tmp_path):
    assert manage.main(["verify-backup"]) == 2                   # копий нет
    assert "копи" in json.loads(capsys.readouterr().out)["problems"][0]
    add_rows(base)
    copy = backup_of(base, env, monkeypatch)
    capsys.readouterr()
    assert manage.main(["verify-backup"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert manage.main(["verify-backup", str(copy)]) == 0
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"junk" * 1000)
    assert manage.main(["verify-backup", str(junk)]) == 1
    assert manage.main(["verify-backup", str(tmp_path / "nope.db")]) == 2
    assert manage.main(["verify-backup", "a", "b"]) == 2


# ---- панель -------------------------------------------------------------------------

@pytest.fixture
def check(monkeypatch):
    monkeypatch.setattr(admin, "CHECK", None)


def wait_done(studio):
    for _ in range(200):
        body = studio.c.get("/api/admin/backup-check").json()
        if body["state"] == "done":
            return body
        time.sleep(0.01)
    raise AssertionError("проверка не кончилась")


def test_admin_backup_check_runs_once(studio, monkeypatch, check, caplog):
    make_user(studio, "owner", is_admin=True); signin(studio, "owner")
    assert studio.c.get("/api/admin/backup-check").json() == {"state": "idle"}
    go = threading.Event()

    def slow(path=None):
        go.wait(5)
        return {"ok": True, "problems": []}

    monkeypatch.setattr(manage, "verify_backup", slow)
    with caplog.at_level(logging.INFO, logger="studio"):
        r = studio.c.post("/api/admin/backup-check")
        assert r.status_code == 202 and r.json() == {"state": "running"}
        again = studio.c.post("/api/admin/backup-check")
        assert again.status_code == 409 and again.json()["detail"]["code"] == "running"
        assert studio.c.get("/api/admin/backup-check").json() == {"state": "running"}
        go.set()
        assert wait_done(studio) == {"state": "done", "result": {"ok": True, "problems": []}}
    assert "backup-check: ok" in caplog.text
    assert studio.c.post("/api/admin/backup-check").status_code == 202      # кончилась — можно снова


def test_admin_backup_check_of_the_real_copy_says_only_numbers(studio, env, check, caplog):
    make_user(studio, "owner", is_admin=True); signin(studio, "owner")
    assert manage.main(["backup"]) == 0
    with caplog.at_level(logging.INFO, logger="studio"):
        assert studio.c.post("/api/admin/backup-check").status_code == 202
        body = wait_done(studio)
    result = body["result"]
    assert result["ok"] is True and result["users"] == 1 and result["backup"].startswith("studio-")
    assert "owner" not in json.dumps(body) and str(env.media_dir) not in json.dumps(body)
    assert "backup-check: ok" in caplog.text and "owner" not in caplog.text


def test_admin_backup_check_failure_is_said_and_logged(studio, monkeypatch, check, caplog):
    make_user(studio, "owner", is_admin=True); signin(studio, "owner")

    def broken(path=None):
        raise RuntimeError("/secret/path")

    monkeypatch.setattr(manage, "verify_backup", broken)
    with caplog.at_level(logging.INFO, logger="studio"):
        studio.c.post("/api/admin/backup-check")
        result = wait_done(studio)["result"]
    assert result["ok"] is False and result["problems"] and "/secret/path" not in json.dumps(result)
    assert "backup-check: fail" in caplog.text and "/secret/path" not in caplog.text


def test_admin_backup_check_is_only_for_admins(studio, check):
    make_user(studio, "bob"); signin(studio, "bob")
    assert studio.c.post("/api/admin/backup-check").status_code == 403
    assert studio.c.get("/api/admin/backup-check").status_code == 403
