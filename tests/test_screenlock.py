"""Блокировка на телефоне (app/screenlock.py, признак «заперто» в auth.guard): спека
2026-10-21-studio-screen-lock, раздел 6."""
import auth
import security
import state
from conftest import make_user, signin


def code(r) -> str:
    return r.json()["detail"]["code"]


def digest(s) -> str:
    return security.digest(s.c.cookies[auth.COOKIE])


def lock_on(uid) -> None:
    state.DB.execute("UPDATE users SET screen_lock = 1 WHERE id = ?", (uid,))


def phone(s) -> None:
    state.DB.execute("UPDATE sessions SET mobile = 1 WHERE digest = ?", (digest(s),))


def owner(s) -> int:
    uid = make_user(s, "owner")
    signin(s, "owner")
    return uid


# ---- признак «заперто» ------------------------------------------------------

def test_new_session_is_open_even_with_the_lock_on(studio):
    uid = make_user(studio, "owner")
    lock_on(uid)
    signin(studio, "owner")
    phone(studio)
    assert studio.c.get("/api/me").status_code == 200


def test_phone_session_locks_after_stale_sec(studio):
    uid = owner(studio)
    lock_on(uid); phone(studio)
    studio.clock[0] += 75
    assert studio.c.get("/api/me").status_code == 200
    studio.clock[0] += 1
    r = studio.c.get("/api/me")
    assert r.status_code == 423 and code(r) == "screen_locked"
    assert r.headers["cache-control"] == "no-store"
    assert studio.c.get("/api/media/x").status_code == 423
    r = studio.c.post("/api/connections", json={"name": "c", "kind": "openai", "base_url": "https://cloud.test/v1",
                                                 "token": "k", "sections": ["chat"]})
    assert r.status_code == 423


def test_lock_paths_are_not_refused_while_locked(studio):
    uid = owner(studio)
    lock_on(uid); phone(studio)
    studio.clock[0] += 3600
    assert studio.c.get("/api/me").status_code == 423
    for path in sorted(auth.SCREEN_LOCK_PATHS):
        assert studio.c.get(path).status_code != 423, path
        assert studio.c.post(path, json={}).status_code != 423, path


def test_computer_session_is_never_locked(studio):
    uid = owner(studio)
    lock_on(uid)
    studio.clock[0] += 3600
    assert studio.c.get("/api/me").status_code == 200


def test_lock_off_never_locks_a_phone(studio):
    owner(studio)
    phone(studio)
    studio.clock[0] += 3600
    assert studio.c.get("/api/me").status_code == 200


def test_lock_refusal_names_no_profile(studio):
    uid = owner(studio)
    lock_on(uid); phone(studio)
    studio.clock[0] += 3600
    r = studio.c.get("/api/me")
    assert r.status_code == 423 and "owner" not in r.text
