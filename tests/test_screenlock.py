"""Блокировка на телефоне (app/screenlock.py, признак «заперто» в auth.guard): спека
2026-10-21-studio-screen-lock, раздел 6."""
import logging

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


# ---- отметки, «заперто», пароль, включение ----------------------------------

def alive(s, touch=False):
    return s.c.post("/api/lock/alive", json={"touch": touch})


def session_row(s):
    return state.DB.execute("SELECT mobile, seen FROM sessions WHERE digest = ?", (digest(s),)).fetchone()


def locked_owner(s) -> int:
    """Учётка с блокировкой, сессия телефона, студия только что заперлась."""
    uid = owner(s)
    lock_on(uid); phone(s)
    assert s.c.post("/api/lock/now").status_code == 204
    return uid


def second_session(s, login_name="owner"):
    """Ещё один браузер той же учётки (компьютер): свой клиент, своя cookie."""
    from fastapi.testclient import TestClient

    import main
    other = TestClient(main.app, base_url="https://testserver")
    assert other.post("/login", data={"login": login_name, "password": "password-123"},
                      follow_redirects=False).status_code == 303
    other.headers[auth.CSRF_HEADER] = other.get("/api/me").json()["csrf"]
    return other


def test_mobile_mark_is_a_one_way_latch(studio):
    owner(studio)
    assert alive(studio, touch=True).json() == {"locked": False}
    assert session_row(studio)["mobile"] == 1
    assert alive(studio, touch=False).json() == {"locked": False}
    assert session_row(studio)["mobile"] == 1


def test_first_mark_of_an_old_phone_session_finds_it_locked(studio):
    """Сессия телефона, открытая до выкладки (seen пусто): первая отметка не должна её открыть."""
    uid = owner(studio)
    lock_on(uid)
    state.DB.execute("UPDATE sessions SET seen = NULL WHERE digest = ?", (digest(studio),))
    assert alive(studio, touch=True).json() == {"locked": True}
    assert studio.c.get("/api/me").status_code == 423


def test_marks_keep_a_phone_open_and_never_open_a_locked_one(studio):
    uid = owner(studio)
    lock_on(uid); phone(studio)
    for _ in range(3):
        studio.clock[0] += 70
        assert alive(studio).json() == {"locked": False}
    assert studio.c.get("/api/me").status_code == 200
    seen = session_row(studio)["seen"]
    studio.clock[0] += 76
    assert alive(studio).json() == {"locked": True}
    assert session_row(studio)["seen"] == seen
    assert studio.c.get("/api/me").status_code == 423


def test_lock_now_locks_at_once(studio):
    uid = owner(studio)
    lock_on(uid); phone(studio)
    assert studio.c.get("/api/me").status_code == 200
    assert studio.c.post("/api/lock/now").status_code == 204
    assert studio.c.get("/api/me").status_code == 423


def test_lock_state_gives_csrf_and_keys_but_no_profile(studio):
    uid = make_user(studio, "owner")
    csrf = signin(studio, "owner")
    lock_on(uid); phone(studio)
    studio.c.post("/api/lock/now")
    r = studio.c.get("/api/lock")
    assert r.status_code == 200 and r.json() == {"locked": True, "keys": False, "csrf": csrf}
    assert "owner" not in r.text
    assert studio.c.post("/api/lock/password", json={"password": "password-123"}).status_code == 204
    assert studio.c.get("/api/lock").json()["locked"] is False


def test_lock_state_without_a_session_sends_to_login(studio):
    """Сессию закрыли (выход на другом устройстве, смена пароля), пока студия заперта: 401 — на вход."""
    locked_owner(studio)
    state.DB.execute("DELETE FROM sessions")
    for r in (studio.c.get("/api/lock"), studio.c.post("/api/lock/alive", json={"touch": True}),
              studio.c.post("/api/lock/password", json={"password": "password-123"})):
        assert r.status_code == 401 and code(r) == "login"


def test_password_opens_and_shares_the_password_pause(studio):
    locked_owner(studio)
    for _ in range(5):
        r = studio.c.post("/api/lock/password", json={"password": "wrong-password"})
        assert r.status_code == 403 and code(r) == "password"
    r = studio.c.post("/api/lock/password", json={"password": "password-123"})
    assert r.status_code == 429 and code(r) == "locked"
    # пауза — та же, что у смены пароля: с компьютера той же учётки пароль сменить нельзя
    r = second_session(studio).post("/api/me/password", json={"current": "password-123", "new": "new-password-456"})
    assert r.status_code == 429 and code(r) == "locked"
    assert studio.c.get("/api/me").status_code == 423
    studio.clock[0] += 900
    assert studio.c.post("/api/lock/password", json={"password": "password-123"}).status_code == 204
    assert studio.c.get("/api/me").status_code == 200


def test_lock_is_enabled_freely_and_disabled_only_with_the_password(studio):
    owner(studio)
    r = studio.c.post("/api/me/screen-lock", json={"enabled": True})
    assert r.status_code == 200 and r.json() == {"screen_lock": True}
    assert studio.c.get("/api/me").json()["screen_lock"] is True
    for body in ({"enabled": False}, {"enabled": False, "password": ""}, {"enabled": False, "password": "wrong-pw"}):
        r = studio.c.post("/api/me/screen-lock", json=body)
        assert r.status_code == 403 and code(r) == "password", body
    assert studio.c.get("/api/me").json()["screen_lock"] is True
    r = studio.c.post("/api/me/screen-lock", json={"enabled": False, "password": "password-123"})
    assert r.status_code == 200 and r.json() == {"screen_lock": False}
    assert studio.c.get("/api/me").json()["screen_lock"] is False


def test_locked_studio_cannot_turn_its_lock_off(studio):
    """Выключить блокировку — только из открытой студии: запертой этот запрос — 423 и с верным паролем."""
    uid = locked_owner(studio)
    r = studio.c.post("/api/me/screen-lock", json={"enabled": False, "password": "password-123"})
    assert r.status_code == 423
    assert state.DB.execute("SELECT screen_lock FROM users WHERE id = ?", (uid,)).fetchone()[0] == 1


def test_lock_events_log_only_ids(studio, caplog):
    caplog.set_level(logging.INFO)
    uid = locked_owner(studio)
    studio.c.post("/api/lock/password", json={"password": "wrong-secret-9"})
    studio.c.post("/api/lock/password", json={"password": "password-123"})
    studio.c.post("/api/me/screen-lock", json={"enabled": False, "password": "password-123"})
    assert f"пользователь {uid}: заперто" in caplog.text
    assert f"пользователь {uid}: открыто (пароль)" in caplog.text
    assert "wrong-secret-9" not in caplog.text and "password-123" not in caplog.text
