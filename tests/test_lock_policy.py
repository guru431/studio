"""Область PIN, два независимых таймера и защита общих входов в историю."""
import pytest
from fastapi import HTTPException

import db
import bridge
import claude_code
import lock_policy
import media
import auth
import security
import state
from conftest import make_user, signin
from test_code import setup, url
from test_foundation import DROP_V24
from test_search import CHAT, IMAGE, MESSAGE


def owner(studio):
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    return uid


def enable(studio, sections, timeout=300):
    response = studio.c.post("/api/me/screen-lock", json={"enabled": True, "sections": sections, "timeout": timeout})
    assert response.status_code == 200, response.text
    return response.json()


def expire(studio):
    assert studio.c.post("/api/lock/now").status_code == 204


def test_migration_preserves_pin_and_sessions_and_adds_compatible_defaults(env):
    conn = db.connect(env.db_path)
    db.migrate(conn)
    conn.execute("INSERT INTO users(id, login, name, pw_hash, pin_hash, screen_lock, created)"
                 " VALUES (1, 'owner', 'Owner', 'pw', 'pin', 1, 0)")
    conn.execute("INSERT INTO sessions(digest,user_id,csrf,expires,renewed,seen) VALUES ('d',1,'c',99,0,12)")
    conn.executescript(DROP_V24 + "PRAGMA user_version = 23;")
    db.migrate(conn)
    db.migrate(conn)
    row = conn.execute("SELECT pin_hash, screen_lock, screen_lock_sections, screen_lock_timeout, computer_pin_timeout FROM users").fetchone()
    assert tuple(row) == ('pin', 1, '["*"]', 60, 10800)
    assert conn.execute("SELECT seen FROM sessions").fetchone()[0] == 12
    conn.close()


@pytest.mark.parametrize("selected,endpoint", [
    ("chat", "/api/chats"), ("photo", "/api/images"), ("video", "/api/videos"),
    ("transcribe", "/api/transcripts"), ("speech", "/api/speeches"),
    ("ocr", "/api/ocr"), ("translate", "/api/translate"), ("code", "/api/code/place"),
])
def test_each_of_eight_sections_can_be_protected_without_locking_the_shell(studio, selected, endpoint):
    owner(studio)
    enable(studio, [selected])
    expire(studio)
    assert studio.c.get(endpoint).status_code == 423
    assert studio.c.get("/api/me").status_code == 200
    assert studio.c.get("/api/lock").json()["locked"] is False
    assert studio.c.get("/api/lock", params={"section": selected}).json()["locked"] is True
    assert studio.c.get("/api/lock?section=security").json()["locked"] is True
    for section in set(lock_policy.SECTIONS) - {selected}:
        assert studio.c.get("/api/lock", params={"section": section}).json()["locked"] is False
    assert studio.c.get("/api/images" if selected != "photo" else "/api/chats").status_code == 200


def test_all_eight_is_distinct_from_whole_app_and_changes_need_password(studio):
    owner(studio)
    enable(studio, list(lock_policy.SECTIONS))
    for body in ({"enabled": True, "sections": ["code"]}, {"enabled": True, "timeout": 3600}):
        assert studio.c.post("/api/me/screen-lock", json=body).status_code == 403
    assert studio.c.post("/api/me/screen-lock", json={"enabled": True, "sections": ["*"], "timeout": 3600,
                                                      "password": "password-123"}).status_code == 200
    expire(studio)
    assert studio.c.get("/api/me").status_code == 423


@pytest.mark.parametrize("extra", [{"sections": []}, {"sections": ["*", "chat"]}, {"sections": ["dictation"]},
                                   {"sections": ["unknown"]}, {"timeout": -1}, {"timeout": 86401},
                                   {"timeout": True}, {"timeout": "60"}, {"timeout": None}])
def test_invalid_policy_does_not_change_the_account(studio, extra):
    owner(studio)
    assert studio.c.post("/api/me/screen-lock", json={"enabled": True, **extra}).status_code == 422
    assert studio.c.get("/api/me").json()["screen_lock"] is False


def test_disabled_policy_can_be_prepared_without_a_hidden_password_field(studio):
    owner(studio)
    response = studio.c.post("/api/me/screen-lock", json={"enabled": False, "sections": ["photo", "code"], "timeout": 900})
    assert response.status_code == 200
    assert response.json() == {"screen_lock": False, "screen_lock_sections": ["photo", "code"], "screen_lock_timeout": 900}


def test_background_polling_does_not_extend_activity_or_reopen_expired_sections(studio):
    owner(studio)
    enable(studio, ["code"], timeout=300)
    studio.clock[0] += 200
    assert studio.c.post("/api/lock/alive", json={"active": False, "section": "code"}).json() == {"locked": False}
    studio.clock[0] += 116
    assert studio.c.post("/api/lock/alive", json={"active": True, "section": "chat"}).json() == {"locked": False}
    assert studio.c.get("/api/lock?section=code").json()["locked"] is True
    assert studio.c.post("/api/lock/password", json={"password": "password-123"}).status_code == 204
    studio.clock[0] += 250
    assert studio.c.post("/api/lock/alive", json={"active": True, "section": "code"}).json() == {"locked": False}
    studio.clock[0] += 250
    assert studio.c.get("/api/lock?section=code").json()["locked"] is False


@pytest.mark.parametrize("selected,accessible", [("photo", True), ("code", False), ("*", False)])
def test_code_stream_is_closed_only_when_the_code_section_is_protected(studio, monkeypatch, selected, accessible):
    owner(studio)
    enable(studio, [selected])
    forgotten = []
    monkeypatch.setattr(bridge, "forget", lambda *a, **kw: forgotten.append(a))
    expire(studio)
    digest = security.digest(studio.c.cookies[auth.COOKIE])
    assert claude_code._session_open(digest) is accessible
    assert bool(forgotten) is not accessible


def test_partial_lock_hides_search_results_models_and_jobs_and_guards_settings(studio):
    uid = owner(studio)
    state.DB.execute(CHAT, ("chat", uid, "secret", "private-model", 1, 1))
    state.DB.execute(MESSAGE, ("message", "chat", "user", "secret text", "private-model", 2))
    state.DB.execute(IMAGE, ("image", uid, "flux", "secret photo", 3))
    enable(studio, ["chat"])
    expire(studio)
    result = studio.c.post("/api/search", json={"q": "secret"}).json()
    assert [(item["section"], item["id"]) for item in result["items"]] == [("photo", "image")]
    assert studio.c.get("/api/search/models").json()["models"] == {"photo": ["flux"]}
    assert all(item["section"] != "chat" for item in studio.c.get("/api/jobs").json()["running"])
    for path, method, body in [("/api/me/screen-lock", "POST", {"enabled": False, "password": "password-123"}),
                               ("/api/history", "DELETE", None), ("/api/me/export", "POST", {"sections": ["chat"]})]:
        assert studio.c.request(method, path, json=body).status_code == 423


def test_media_associated_with_a_closed_section_cannot_be_downloaded(studio):
    uid = owner(studio)
    secret = media.save(uid, b"private", "text/plain", "private.txt")
    public = media.save(uid, b"public", "text/plain", "public.txt")
    state.DB.execute(IMAGE, ("image", uid, "flux", "secret", 1))
    state.DB.execute("UPDATE images SET media_id = ? WHERE id = 'image'", (secret,))
    enable(studio, ["photo"])
    expire(studio)
    assert studio.c.get("/api/media/" + secret).status_code == 423
    response = studio.c.get("/api/media/" + public)
    assert response.status_code == 200 and response.content == b"public"
    assert response.headers["cache-control"] == "no-store"


def test_a_protected_file_cannot_be_reused_as_input_in_an_open_section(studio):
    uid = owner(studio)
    secret = media.save(uid, b"private", "image/png", "private.png")
    state.DB.execute(IMAGE, ("image", uid, "flux", "secret", 1))
    state.DB.execute("UPDATE images SET media_id = ? WHERE id = 'image'", (secret,))
    token = lock_policy.RESTRICTED.set(frozenset({"photo"}))
    try:
        with pytest.raises(HTTPException) as raised:
            media.path(uid, secret)
        assert raised.value.status_code == 423
    finally:
        lock_policy.RESTRICTED.reset(token)


def test_computer_timer_is_persisted_and_relayed_to_capable_bridge(studio):
    aid, machine, link = setup(studio)
    response = studio.c.patch("/api/me", json={"computer_pin_timeout": 600})
    assert response.status_code == 200 and response.json()["computer_pin_timeout"] == 600
    before = len(machine.requests)
    assert studio.c.get(url(aid, "projects")).status_code == 409  # старый агент не игнорирует настройку молча
    assert len(machine.requests) == before
    link.hello["pin_timeout"] = True
    assert studio.c.get(url(aid, "projects")).status_code == 200
    assert machine.requests[-1]["idle_sec"] == 600
    assert studio.c.get("/api/me").json()["screen_lock_timeout"] == 60


@pytest.mark.parametrize("value", [0, 59, 604801, True, None, "600"])
def test_invalid_computer_timer_is_rejected(studio, value):
    owner(studio)
    assert studio.c.patch("/api/me", json={"computer_pin_timeout": value}).status_code == 422
    assert studio.c.get("/api/me").json()["computer_pin_timeout"] == 10800
