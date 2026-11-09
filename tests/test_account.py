"""Сессии учётки, аварийный выход и журнал безопасности (account.py, security_log.py); очистка HTTP-кэша браузера
при выходе и стирании истории; стирание отметок «завершено, не прочитано» вместе с историей."""
import pytest

import account
import auth
import bridge
import security
import security_log
import state
import takeout
from conftest import login, make_user, signin
from test_chat_shares import create, regular

PHONE_UA = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"


def owner(studio):
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    return uid


def current(studio) -> str:
    return security.digest(studio.c.cookies[auth.COOKIE])


def kinds(uid) -> list:
    return [r[0] for r in state.DB.execute("SELECT kind FROM security_events WHERE user_id = ? ORDER BY id", (uid,))]


def agent(uid, name="PC") -> int:
    return state.DB.execute("INSERT INTO agents(user_id, name, key_hash, created) VALUES (?, ?, ?, 0)",
                            (uid, name, security.new_token())).lastrowid


def device_subs(digest, uid):
    """push и ntfy устройства сессии — прямо в базу."""
    state.DB.execute("INSERT INTO push_subs(endpoint, user_id, digest, p256dh, auth, created)"
                     " VALUES (?, ?, ?, 'k', 'a', 0)", ("https://fcm.googleapis.com/fcm/send/" + digest[:8], uid, digest))
    state.DB.execute("INSERT INTO ntfy_devices(digest, user_id, server, topic, enabled) VALUES (?, ?, 'https://n', 't', 1)",
                     (digest, uid))


# ---- стирание истории и кэш браузера ----------------------------------------------------------

def test_history_wipe_forgets_unread_finished_sessions_of_own_computers_only(studio):
    uid = owner(studio)
    other = make_user(studio, "other")
    mine, theirs = agent(uid), agent(other)
    state.DB.execute("INSERT INTO code_attention VALUES (?, 'p', 'c1', 'r', 1, 0)", (mine,))
    state.DB.execute("INSERT INTO code_attention VALUES (?, 'p', 'c2', 'r', 1, 0)", (theirs,))
    assert studio.c.delete("/api/history").status_code == 200
    assert [r[0] for r in state.DB.execute("SELECT agent_id FROM code_attention")] == [theirs]
    assert state.DB.execute("SELECT count(*) FROM agents").fetchone()[0] == 2


def test_logout_and_history_wipe_clear_only_the_http_cache(studio):
    """Clear-Site-Data: "cache" — медиа не остаются в HTTP-кэше; cookies и хранилища (кэш оболочки offline,
    служебный работник, флаги устройства) — не трогаются."""
    owner(studio)
    wiped = studio.c.delete("/api/history")
    assert wiped.status_code == 200 and wiped.json() == {"pending_files": 0}
    assert wiped.headers["clear-site-data"] == '"cache"'
    out = studio.c.post("/logout", follow_redirects=False)
    assert out.status_code == 303 and out.headers["clear-site-data"] == '"cache"'
    assert studio.c.get("/api/me").status_code == 401


# ---- список и завершение сессий -------------------------------------------------------------

def test_sessions_list_shows_devices_and_notifications_without_digests(studio):
    uid = owner(studio)
    assert login(studio, "owner", "password-123").status_code == 303           # прежняя сессия браузера — прочь
    studio.c.headers[auth.CSRF_HEADER] = studio.c.get("/api/me").json()["csrf"]
    phone, _ = auth.create_session(uid, auth.device_class(PHONE_UA))
    device_subs(security.digest(phone), uid)
    studio.clock[0] += 10
    items = studio.c.get("/api/me/sessions").json()["items"]
    assert [(i["current"], i["device"], i["push"], i["ntfy"]) for i in items] == [
        (True, "computer", False, False), (False, "phone", True, True)]
    assert items[1]["id"] == security_log.handle(security.digest(phone))
    assert all(security.digest(phone) not in str(i) and phone not in str(i) for i in items)
    assert items[0]["created"] == items[0]["active"] == studio.clock[0] - 10
    assert auth.device_class(None) is None and auth.device_class("Mozilla/5.0 (Windows NT 10.0)") == "computer"


def test_ending_another_session_cuts_its_access_streams_and_notifications_at_once(studio):
    uid = owner(studio)
    other, _ = auth.create_session(uid, "phone")
    digest = security.digest(other)
    device_subs(digest, uid)
    watch = bridge.Watch(bridge.tag(digest))
    bridge.WATCHERS[(1, "chat")] = {watch}
    handle = security_log.handle(digest)
    assert studio.c.delete("/api/me/sessions/" + security_log.handle(current(studio))).status_code == 409
    assert studio.c.delete("/api/me/sessions/0123456789abcdef").status_code == 404
    assert studio.c.delete("/api/me/sessions/" + handle).status_code == 204
    assert auth.session_user(other) is None
    assert watch.get_nowait() == ("closed", None)                              # поток «Агентов» закрыт сразу
    for table in ("push_subs", "ntfy_devices"):
        assert state.DB.execute(f"SELECT count(*) FROM {table} WHERE digest = ?", (digest,)).fetchone()[0] == 0
    assert studio.c.delete("/api/me/sessions/" + handle).status_code == 404
    assert studio.c.get("/api/me").status_code == 200                          # текущая жива
    row = state.DB.execute("SELECT detail, session FROM security_events WHERE kind = 'session_end'").fetchone()
    assert tuple(row) == (handle, security_log.handle(current(studio)))


def test_a_session_of_another_account_cannot_be_ended(studio):
    owner(studio)
    stranger = make_user(studio, "stranger")
    theirs, _ = auth.create_session(stranger)
    assert studio.c.delete("/api/me/sessions/" + security_log.handle(security.digest(theirs))).status_code == 404
    assert auth.session_user(theirs) is not None


# ---- аварийный выход ------------------------------------------------------------------------

@pytest.mark.parametrize("revoke_agents", [False, True])
def test_emergency_exit_ends_everything_with_the_password(studio, monkeypatch, revoke_agents):
    endpoint, uid = regular(studio)
    create(studio, endpoint)
    other, _ = auth.create_session(uid)
    device_subs(security.digest(other), uid)
    state.DB.execute("INSERT INTO ntfy_settings(user_id, server, topic) VALUES (?, 'https://n', 'shared')", (uid,))
    pc = agent(uid)
    dropped = []

    async def drop(agent_id, code=bridge.CLOSE_AUTH):
        dropped.append(agent_id)

    monkeypatch.setattr(bridge, "drop", drop)
    wrong = studio.c.post("/api/me/emergency", json={"password": "nope", "agents": revoke_agents})
    assert wrong.status_code == 403 and auth.session_user(other) is not None
    r = studio.c.post("/api/me/emergency", json={"password": "password-123", "agents": revoke_agents})
    assert r.status_code == 204
    assert r.headers["clear-site-data"] == '"cache"' and auth.COOKIE in r.headers["set-cookie"]
    assert studio.c.get("/api/me").status_code == 401
    assert auth.session_user(other) is None
    for table in ("sessions", "chat_shares", "push_subs", "ntfy_devices", "ntfy_settings"):
        assert state.DB.execute(f"SELECT count(*) FROM {table} WHERE user_id = ?", (uid,)).fetchone()[0] == 0, table
    left = [r[0] for r in state.DB.execute("SELECT id FROM agents WHERE user_id = ?", (uid,))]
    assert left == ([] if revoke_agents else [pc]) and dropped == ([pc] if revoke_agents else [])
    revokes = state.DB.execute("SELECT count(*) FROM agent_events WHERE kind = 'revoke'").fetchone()[0]
    assert revokes == int(revoke_agents)
    assert kinds(uid)[-1] == "emergency"
    assert state.DB.execute("SELECT count(*) FROM chats WHERE user_id = ?", (uid,)).fetchone()[0] == 1   # история — на месте


# ---- журнал безопасности --------------------------------------------------------------------

def test_security_journal_records_logins_unlocks_and_links_without_secrets(studio):
    uid = make_user(studio, "owner")
    assert login(studio, "owner", "wrong-password", ip="10.0.0.9").status_code == 401
    assert login(studio, "ghost", "wrong-password").status_code == 401           # неизвестный логин — никому
    signin(studio, "owner")
    assert studio.c.put("/api/me/pin", json={"pin": "1234", "password": "password-123"}).status_code == 200
    assert studio.c.post("/api/me/screen-lock", json={"enabled": True, "sections": ["*"], "timeout": 60}).status_code == 200
    assert studio.c.post("/api/lock/now").status_code == 204
    assert studio.c.post("/api/lock/pin", json={"pin": "9999"}).status_code == 403
    assert studio.c.post("/api/lock/pin", json={"pin": "1234"}).status_code == 204
    assert studio.c.post("/api/lock/password", json={"password": "bad"}).status_code == 403
    state.DB.execute("INSERT INTO chats(id, user_id, title, created, updated) VALUES ('c', ?, 't', 0, 0)", (uid,))
    assert studio.c.post("/api/chats/c/share", json={"pin": "0123"}).status_code == 201
    assert studio.c.delete("/api/chats/c/share").status_code == 204
    assert kinds(uid) == ["login_fail", "login", "unlock_fail", "unlock", "unlock_fail", "share_create", "share_revoke"]
    page = studio.c.get("/api/me/security-events").json()
    assert [i["kind"] for i in page["items"]][:2] == ["share_revoke", "share_create"] and page["next"] is None
    first = page["items"][-1]
    assert (first["kind"], first["detail"], first["ip"], first["session"], first["current"]) == (
        "login_fail", "login", "10.0.0.9", None, False)
    assert page["items"][0]["current"] is True and page["items"][0]["detail"] == "chat"
    dump = str(page)
    for secret in ("password-123", "1234", "9999", "wrong-password", current(studio)):
        assert secret not in dump


def test_pin_exhaustion_and_bridge_login_failures_are_recorded(studio, monkeypatch):
    import screenlock
    uid = owner(studio)
    monkeypatch.setattr(screenlock, "PIN_FAILS", 2)
    assert studio.c.put("/api/me/pin", json={"pin": "1234", "password": "password-123"}).status_code == 200
    for _ in range(2):
        assert studio.c.post("/api/lock/pin", json={"pin": "0000"}).status_code == 403
    assert studio.c.post("/api/agent/enroll", json={"login": "owner", "password": "x", "name": "PC"}).status_code == 401
    assert kinds(uid)[-4:] == ["unlock_fail", "unlock_fail", "pin_exhausted", "login_fail"]
    assert state.DB.execute("SELECT detail FROM security_events ORDER BY id DESC").fetchone()[0] == "bridge"


def test_security_journal_keeps_a_bounded_recent_tail(studio, monkeypatch):
    uid = owner(studio)
    monkeypatch.setattr(security_log, "KEEP_MAX", 3)
    state.DB.execute("INSERT INTO security_events(user_id, created, kind) VALUES (?, ?, 'login')",
                     (uid, studio.clock[0] - security_log.KEEP_SEC - 1))
    for _ in range(5):
        security_log.record(uid, "logout")
    rows = state.DB.execute("SELECT kind, created FROM security_events WHERE user_id = ?", (uid,)).fetchall()
    assert len(rows) == 3 and {r[0] for r in rows} == {"logout"}
    with pytest.raises(ValueError):
        security_log.record(uid, "typo")
    monkeypatch.setattr(account, "EVENTS_PAGE", 2)
    page = studio.c.get("/api/me/security-events").json()
    assert len(page["items"]) == 2 and page["next"] is not None
    rest = studio.c.get(f"/api/me/security-events?before={page['next']}").json()
    assert len(rest["items"]) == 1


def test_security_journal_survives_history_wipe_and_is_in_the_export(studio):
    uid = owner(studio)
    assert studio.c.delete("/api/history").status_code == 200
    assert kinds(uid) == ["login"]
    assert "security" in takeout.SECTIONS
    out = studio.c.post("/api/me/export", json={"sections": ["security"]}).json()
    body = studio.c.get(f"/api/me/export/{out['id']}").content
    assert b"security_events/0001.json" in body and b'"login"' in body
