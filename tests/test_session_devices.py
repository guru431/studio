"""Сведения сессии: распознавание без хранения UA, обновление старых входов и имена только владельцу."""
import logging

import pytest

import auth
import db
import security
import security_log
import session_devices
import state
from conftest import login, make_user, signin


WINDOWS = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/142.0.0.0 Safari/537.36 Edg/142.0.3595.65"
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6_2 like Mac OS X) AppleWebKit/605.1.15 Version/18.6 Mobile/15E148 Safari/604.1"


@pytest.mark.parametrize("ua,expected", [
    (WINDOWS, ("Edge 142.0.3595.65", "Windows 10/11", None)),
    (IPHONE, ("Safari 18.6", "iOS 18.6.2", "iPhone")),
    (IPHONE.replace("Version/18.6", "CriOS/142.0.7444.77"), ("Chrome 142.0.7444.77", "iOS 18.6.2", "iPhone")),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) Version/18.6 Mobile/15E148 Safari/604.1", ("Safari 18.6", "iPadOS", "iPad")),
    ("Mozilla/5.0 (Linux; Android 14; ru-ru; Pixel 8 Pro Build/AP2A) Chrome/142.0.0.0 Mobile Safari/537.36", ("Chrome 142", "Android 14", "Pixel 8 Pro")),
    ("Mozilla/5.0 (Linux; Android 10; K) Chrome/142.0.0.0 Mobile Safari/537.36", ("Chrome 142", "Android", None)),
    ("Mozilla/5.0 (X11; Linux x86_64; rv:144.0) Gecko/20100101 Firefox/144.0", ("Firefox 144", "Linux", None)),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_7_1) Version/18.6 Safari/605.1.15", ("Safari 18.6", "macOS 14.7.1", None)),
    (None, (None, None, None)),
    ("private-unknown-agent", (None, None, None)),
])
def test_device_description_handles_browser_precedence_and_hidden_details(ua, expected):
    assert session_devices.describe(ua) == expected


def test_login_records_details_and_only_own_heartbeat_backfills_them_without_marking_activity(studio, caplog):
    uid = make_user(studio, "owner")
    caplog.set_level(logging.INFO, logger="studio")
    studio.c.headers.update({"user-agent": WINDOWS, "x-forwarded-for": "198.51.100.8, 203.0.113.4"})
    assert login(studio, "owner", "password-123", ip="203.0.113.4", spoofed="198.51.100.8").status_code == 303
    studio.c.headers[auth.CSRF_HEADER] = studio.c.get("/api/me").json()["csrf"]
    item = studio.c.get("/api/me/sessions").json()["items"][0]
    assert (item["browser"], item["os"], item["model"], item["ip"]) == (*session_devices.describe(WINDOWS), "203.0.113.4")
    other, _ = auth.create_session(uid)
    digest = security.digest(studio.c.cookies[auth.COOKIE])
    state.DB.execute("UPDATE sessions SET browser = NULL, os = NULL, ip = NULL, label = 'Рабочий ПК' WHERE digest = ?", (digest,))
    active = item["active"]
    studio.clock[0] += 100
    studio.c.headers.update({"user-agent": IPHONE, "x-forwarded-for": "198.51.100.9, 2001:db8::1"})
    assert studio.c.post("/api/lock/alive", json={"active": False, "touch": True}).status_code == 200
    rows = studio.c.get("/api/me/sessions").json()["items"]
    mine = next(i for i in rows if i["current"])
    assert (mine["browser"], mine["os"], mine["model"], mine["ip"], mine["label"], mine["active"]) == (
        "Safari 18.6", "iOS 18.6.2", "iPhone", "2001:db8::1", "Рабочий ПК", active)
    assert next(i for i in rows if not i["current"])["browser"] is None
    assert auth.session_user(other) is not None
    before = state.DB.total_changes
    session_devices.remember(digest, IPHONE, "2001:db8::1", "phone")
    assert state.DB.total_changes == before
    for private in (WINDOWS, IPHONE, "Рабочий ПК", "2001:db8::1", digest):
        assert private not in caplog.text


def test_session_labels_are_scoped_persistent_clearable_and_protected(studio, caplog):
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    mine = studio.c.get("/api/me/sessions").json()["items"][0]
    path = "/api/me/sessions/" + mine["id"]
    assert studio.c.patch(path, json={"label": "  Рабочий ноутбук  "}).status_code == 204
    assert studio.c.get("/api/me/sessions").json()["items"][0]["label"] == "Рабочий ноутбук"
    assert studio.c.patch(path, json={"label": "\nподмена"}).status_code == 422
    assert studio.c.patch(path, json={"label": "a" * 81}).status_code == 422
    assert studio.c.patch(path, json={"label": "a\u202eb"}).status_code == 422
    assert studio.c.patch(path, json={"label": " "}).status_code == 204
    assert studio.c.get("/api/me/sessions").json()["items"][0]["label"] is None
    other, _ = auth.create_session(uid)
    other_path = "/api/me/sessions/" + security_log.handle(security.digest(other))
    assert studio.c.patch(other_path, json={"label": "Телефон"}).status_code == 204
    stranger, _ = auth.create_session(make_user(studio, "stranger"))
    foreign_path = "/api/me/sessions/" + security_log.handle(security.digest(stranger))
    assert studio.c.patch(foreign_path, json={"label": "Нельзя"}).status_code == 404
    csrf = studio.c.headers.pop(auth.CSRF_HEADER)
    assert studio.c.patch(path, json={"label": "Без CSRF"}).status_code == 403
    studio.c.headers[auth.CSRF_HEADER] = csrf
    assert studio.c.post("/api/me/screen-lock", json={"enabled": True, "sections": ["chat"], "timeout": 60}).status_code == 200
    assert studio.c.post("/api/lock/now").status_code == 204
    assert studio.c.patch(path, json={"label": "Заперто"}).status_code == 423
    assert "Рабочий ноутбук" not in caplog.text


def test_schema_37_keeps_existing_sessions_and_supports_old_inserts(env):
    conn = db.connect(env.db_path)
    conn.executescript(db.SCHEMA.removesuffix(db.EMAIL_40).removesuffix(db.AGENT_EVENTS_39).removesuffix(db.PROJECTS_38).removesuffix(db.DEVICES_37) + "\nPRAGMA user_version=36;")
    conn.execute("INSERT INTO users(id,login,name,pw_hash,created) VALUES(1,'u','U','h',0)")
    conn.execute("INSERT INTO sessions(digest,user_id,csrf,expires,renewed,active) VALUES('d',1,'c',900,1,5)")
    db.migrate(conn)
    db.migrate(conn)
    assert conn.execute("SELECT digest, active, browser, os, model, label, ip FROM sessions").fetchone()[:] == (
        "d", 5, None, None, None, None, None)
    conn.execute("INSERT INTO sessions(digest,user_id,csrf,expires,renewed) VALUES('old',1,'c',900,1)")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()
