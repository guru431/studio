"""Блокировка на телефоне (app/screenlock.py, признак «заперто» в auth.guard): спека
2026-10-21-studio-screen-lock, раздел 6."""
import asyncio
import base64
import hashlib
import html
import json
import logging
import re
import time

import httpx

import auth
import security
import state
from conftest import make_user, signin
from screenlock import PIN_FAILS, PIN_MAX
from softkey import SoftKey, unb64u


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


def test_computer_session_uses_the_app_lock_too(studio):
    uid = owner(studio)
    lock_on(uid)
    studio.clock[0] += 3600
    assert studio.c.get("/api/me").status_code == 423


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


def test_shell_bootstrap_is_session_bound_and_has_no_account_secrets(studio):
    uid = locked_owner(studio)
    set_pin(uid, '0007')
    state.DB.execute('UPDATE users SET screen_lock_sections = ?, pin_fails = ? WHERE id = ?', ('["photo"]', PIN_FAILS, uid))
    response = studio.c.get('/')
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    snapshot = json.loads(html.unescape(re.search(r'data-lock-state="([^"]+)"', response.text)[1]))
    assert snapshot == studio.c.get('/api/lock?section=security').json()
    assert snapshot['locked'] and snapshot['sections'] == ['photo']
    assert snapshot['pin'] and snapshot['pin_left'] == 0
    assert pin_hash(uid) not in response.text and studio.c.cookies[auth.COOKIE] not in response.text
    assert pin_fails(uid) == PIN_FAILS and session_row(studio)['seen'] is None
    other = make_user(studio, 'other')
    signin(studio, 'other')
    another = studio.c.get('/')
    other_snapshot = json.loads(html.unescape(re.search(r'data-lock-state="([^"]+)"', another.text)[1]))
    assert not other_snapshot['locked'] and not other_snapshot['pin']
    assert other_snapshot['csrf'] != snapshot['csrf'] and other != uid


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
    assert r.status_code == 200 and r.json() == {"locked": True, "keys": False, "csrf": csrf, "pin": False,
                                                 "pin_left": 10, "enabled": True, "sections": ["*"], "timeout": 60}
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
    assert r.status_code == 200 and r.json() == {"screen_lock": True, "screen_lock_sections": ["*"], "screen_lock_timeout": 60}
    assert studio.c.get("/api/me").json()["screen_lock"] is True
    for body in ({"enabled": False}, {"enabled": False, "password": ""}, {"enabled": False, "password": "wrong-pw"}):
        r = studio.c.post("/api/me/screen-lock", json=body)
        assert r.status_code == 403 and code(r) == "password", body
    assert studio.c.get("/api/me").json()["screen_lock"] is True
    r = studio.c.post("/api/me/screen-lock", json={"enabled": False, "password": "password-123"})
    assert r.status_code == 200 and r.json() == {"screen_lock": False, "screen_lock_sections": ["*"], "screen_lock_timeout": 60}
    assert studio.c.get("/api/me").json()["screen_lock"] is False


def test_locked_studio_cannot_turn_its_lock_off(studio):
    """Выключить блокировку — только из открытой студии: запертой этот запрос — 423 и с верным паролем."""
    uid = locked_owner(studio)
    r = studio.c.post("/api/me/screen-lock", json={"enabled": False, "password": "password-123"})
    assert r.status_code == 423
    assert state.DB.execute("SELECT screen_lock FROM users WHERE id = ?", (uid,)).fetchone()[0] == 1


def test_password_changed_during_the_check_confirms_nothing(studio, monkeypatch):
    """Пароль сменили, пока считался хеш: отпирание, выключение блокировки и варианты ключа — отказ (аудит P1)."""
    uid = locked_owner(studio)
    computer = second_session(studio)
    old_hash = state.DB.execute("SELECT pw_hash FROM users WHERE id = ?", (uid,)).fetchone()[0]
    new_hash, real = security.hash_password("changed-pass-1"), security.verify_password

    def racing(password, stored):
        ok = real(password, stored)
        state.DB.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (new_hash, uid))
        return ok

    monkeypatch.setattr(security, "verify_password", racing)
    for client, path, body in ((studio.c, "/api/lock/password", {"password": "password-123"}),
                               (computer, "/api/me/screen-lock", {"enabled": False, "password": "password-123"}),
                               (computer, "/api/passkeys/options", {"password": "password-123"})):
        state.DB.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (old_hash, uid))
        r = client.post(path, json=body)
        assert r.status_code == 403 and code(r) == "password", path
    assert studio.c.get("/api/me").status_code == 423
    assert state.DB.execute("SELECT screen_lock FROM users WHERE id = ?", (uid,)).fetchone()[0] == 1


def test_lock_events_log_only_ids(studio, caplog):
    caplog.set_level(logging.INFO)
    uid = locked_owner(studio)
    studio.c.post("/api/lock/password", json={"password": "wrong-secret-9"})
    studio.c.post("/api/lock/password", json={"password": "password-123"})
    set_pin(uid, "0007")
    studio.c.post("/api/lock/now")
    assert pin(studio, "9876").status_code == 403
    assert pin(studio, "0007").status_code == 204
    studio.c.post("/api/lock/now")
    for _ in range(PIN_FAILS):
        pin(studio, "9876")
    assert code(pin(studio, "9876")) == "pin_exhausted"
    studio.c.post("/api/lock/password", json={"password": "password-123"})
    studio.c.post("/api/me/screen-lock", json={"enabled": False, "password": "password-123"})
    assert f"пользователь {uid}: заперто" in caplog.text
    assert f"пользователь {uid}: открыто (пароль)" in caplog.text
    assert f"пользователь {uid}: открыто (PIN)" in caplog.text
    assert f"пользователь {uid}: PIN неверен" in caplog.text
    assert f"пользователь {uid}: PIN исчерпан" in caplog.text
    assert "wrong-secret-9" not in caplog.text and "password-123" not in caplog.text
    assert "0007" not in caplog.text and "9876" not in caplog.text


# ---- ключи passkey -----------------------------------------------------------

def handle(key: SoftKey) -> str:
    """id ключа в API — не credential id (его строка запроса понесла бы в журнал Apache), а хеш от него."""
    return hashlib.sha256(key.id.encode()).hexdigest()[:16]


def add_key(s, key=None, name="iPhone") -> SoftKey:
    key = key or SoftKey()
    options = s.c.post("/api/passkeys/options", json={"password": "password-123"}).json()
    r = s.c.post("/api/passkeys", json={"credential": key.register(options), "name": name})
    assert r.status_code == 201, r.text
    return key


def insert_key(uid, key: SoftKey) -> None:
    """Ключ мимо API (у API нет пути добавить чужой или без проверки пользователя)."""
    state.DB.execute("INSERT INTO passkeys(id, user_id, public_key, sign_count, transports, name, created)"
                     " VALUES (?, ?, ?, 0, ?, 'iPhone', 0)", (key.id, uid, key.public_key, '["internal"]'))


def open_with(s, key: SoftKey):
    options = s.c.post("/api/lock/challenge").json()
    return s.c.post("/api/lock/open", json={"credential": key.assertion(options)})


def key_row(key: SoftKey):
    return state.DB.execute("SELECT sign_count, used FROM passkeys WHERE id = ?", (key.id,)).fetchone()


def test_adding_a_key_needs_the_password(studio):
    uid = owner(studio)
    for body in ({}, {"password": "wrong-pw"}):
        r = studio.c.post("/api/passkeys/options", json=body)
        assert r.status_code == 403 and code(r) == "password", body
    r = studio.c.post("/api/passkeys/options", json={"password": "password-123"})
    assert r.status_code == 200
    options = r.json()
    assert options["rp"] == {"id": "testserver", "name": "Студия"}
    assert options["authenticatorSelection"]["userVerification"] == "required"
    assert options["attestation"] == "none"
    assert options["user"]["name"] == options["user"]["displayName"] == "owner"
    assert unb64u(options["user"]["id"]) == f"studio-{uid}".encode()
    assert len(unb64u(options["challenge"])) == 32


def test_key_is_added_listed_and_excluded_next_time(studio):
    owner(studio)
    key = SoftKey()
    options = studio.c.post("/api/passkeys/options", json={"password": "password-123"}).json()
    assert options["excludeCredentials"] == []
    r = studio.c.post("/api/passkeys", json={"credential": key.register(options), "name": "iPhone"})
    assert r.status_code == 201
    assert r.json() == {"id": handle(key), "name": "iPhone", "created": studio.clock[0], "used": None}
    studio.clock[0] += 1
    second = add_key(studio, name="iPad")
    assert [k["id"] for k in studio.c.get("/api/passkeys").json()["items"]] == [handle(second), handle(key)]
    options = studio.c.post("/api/passkeys/options", json={"password": "password-123"}).json()
    assert sorted(c["id"] for c in options["excludeCredentials"]) == sorted([key.id, second.id])
    # тот же ключ ещё раз (браузер с excludeCredentials так не сделает) — 409
    r = studio.c.post("/api/passkeys", json={"credential": key.register(options), "name": "iPhone"})
    assert r.status_code == 409 and code(r) == "exists"


def test_registration_needs_a_fresh_single_use_challenge(studio):
    owner(studio)
    key = SoftKey()
    options = studio.c.post("/api/passkeys/options", json={"password": "password-123"}).json()
    answer = key.register(options)
    assert studio.c.post("/api/passkeys", json={"credential": answer, "name": "iPhone"}).status_code == 201
    other = SoftKey()
    for replay in (answer, other.register(options)):     # вызов уже взят
        r = studio.c.post("/api/passkeys", json={"credential": replay, "name": "iPhone"})
        assert r.status_code == 400 and code(r) == "passkey"
    options = studio.c.post("/api/passkeys/options", json={"password": "password-123"}).json()
    studio.clock[0] += 121
    r = studio.c.post("/api/passkeys", json={"credential": other.register(options), "name": "iPhone"})
    assert r.status_code == 400
    options = studio.c.post("/api/passkeys/options", json={"password": "password-123"}).json()
    r = studio.c.post("/api/passkeys", json={"credential": SoftKey(uv=False).register(options), "name": "iPhone"})
    assert r.status_code == 400                          # без проверки пользователя (Face ID, код) — не ключ
    assert len(studio.c.get("/api/passkeys").json()["items"]) == 1


def test_unknown_key_name_is_refused(studio):
    owner(studio)
    options = studio.c.post("/api/passkeys/options", json={"password": "password-123"}).json()
    r = studio.c.post("/api/passkeys", json={"credential": SoftKey().register(options), "name": "Мой телефон"})
    assert r.status_code == 422


def test_passkey_opens_a_locked_phone(studio):
    uid = owner(studio)
    key = add_key(studio)
    lock_on(uid); phone(studio)
    assert studio.c.post("/api/lock/now").status_code == 204
    assert studio.c.get("/api/lock").json() == {"locked": True, "keys": True, "csrf": studio.c.headers[auth.CSRF_HEADER],
                                                "pin": False, "pin_left": 10, "enabled": True, "sections": ["*"], "timeout": 60}
    options = studio.c.post("/api/lock/challenge").json()
    assert [(c["id"], c["transports"]) for c in options["allowCredentials"]] == [(key.id, ["internal"])]
    assert options["userVerification"] == "required" and options["rpId"] == "testserver"
    r = studio.c.post("/api/lock/open", json={"credential": key.assertion(options)})
    assert r.status_code == 204
    assert studio.c.get("/api/me").status_code == 200
    assert key_row(key)[:] == (1, studio.clock[0])
    # пауза пароля passkey не касается
    studio.c.post("/api/lock/now")
    for _ in range(5):
        studio.c.post("/api/lock/password", json={"password": "wrong-password"})
    assert studio.c.post("/api/lock/password", json={"password": "password-123"}).status_code == 429
    assert open_with(studio, key).status_code == 204
    assert studio.c.get("/api/me").status_code == 200


def test_open_refuses_a_foreign_key_a_replay_no_uv_and_a_stale_challenge(studio):
    foreign = SoftKey()
    insert_key(make_user(studio, "other"), foreign)
    uid = owner(studio)
    key = add_key(studio)
    lock_on(uid); phone(studio)
    studio.c.post("/api/lock/now")
    r = open_with(studio, foreign)
    assert r.status_code == 403 and code(r) == "passkey"
    answer = key.assertion(studio.c.post("/api/lock/challenge").json())
    assert studio.c.post("/api/lock/open", json={"credential": answer}).status_code == 204
    studio.c.post("/api/lock/now")
    assert studio.c.post("/api/lock/open", json={"credential": answer}).status_code == 403     # повтор
    weak = SoftKey(uv=False)
    insert_key(uid, weak)
    assert open_with(studio, weak).status_code == 403                                            # без UV
    options = studio.c.post("/api/lock/challenge").json()
    studio.clock[0] += 121
    assert studio.c.post("/api/lock/open", json={"credential": key.assertion(options)}).status_code == 403
    for broken in ({}, {"id": key.id}, {"id": key.id, "rawId": "%%%", "response": {}}):
        studio.c.post("/api/lock/challenge")
        assert studio.c.post("/api/lock/open", json={"credential": broken}).status_code == 403, broken
    assert studio.c.get("/api/me").status_code == 423


def test_foreign_origin_or_rp_id_neither_adds_nor_opens(studio, caplog):
    """Origin и RP ID — из STUDIO_ORIGIN, а не из запроса; отказ в журнале — с типом ошибки, без origin."""
    caplog.set_level(logging.INFO)
    uid = owner(studio)
    for key in (SoftKey(origin="https://evil.test"), SoftKey(rp_id="evil.test")):
        options = studio.c.post("/api/passkeys/options", json={"password": "password-123"}).json()
        r = studio.c.post("/api/passkeys", json={"credential": key.register(options), "name": "iPhone"},
                          headers={"Host": "evil.test", "Origin": "https://evil.test"})
        assert r.status_code == 400 and code(r) == "passkey"
    assert studio.c.get("/api/passkeys").json() == {"items": []}
    assert f"пользователь {uid}: ключ не добавлен (InvalidRegistrationResponse)" in caplog.text
    lock_on(uid); phone(studio)
    studio.c.post("/api/lock/now")
    for key in (SoftKey(origin="https://evil.test"), SoftKey(rp_id="evil.test")):
        insert_key(uid, key)
        options = studio.c.post("/api/lock/challenge").json()
        r = studio.c.post("/api/lock/open", json={"credential": key.assertion(options)},
                          headers={"Host": "evil.test", "Origin": "https://evil.test"})
        assert r.status_code == 403 and code(r) == "passkey"
    assert studio.c.get("/api/me").status_code == 423
    assert f"пользователь {uid}: ключ не подошёл (InvalidAuthenticationResponse)" in caplog.text
    assert "evil.test" not in caplog.text


def test_challenge_without_keys_is_409(studio):
    owner(studio)
    r = studio.c.post("/api/lock/challenge")
    assert r.status_code == 409 and code(r) == "no_keys"


def test_keys_are_removed_one_by_one_and_with_the_account(studio):
    foreign = SoftKey()
    insert_key(make_user(studio, "other"), foreign)
    uid = owner(studio)
    key = add_key(studio)
    r = studio.c.delete(f"/api/passkeys/{handle(foreign)}")
    assert r.status_code == 404 and code(r) == "not_found"
    assert studio.c.delete(f"/api/passkeys/{handle(key)}").status_code == 204
    assert studio.c.get("/api/passkeys").json() == {"items": []}
    assert studio.c.delete(f"/api/passkeys/{handle(key)}").status_code == 404
    add_key(studio)
    state.DB.execute("DELETE FROM users WHERE id = ?", (uid,))
    assert state.DB.execute("SELECT count(*) FROM passkeys WHERE user_id = ?", (uid,)).fetchone()[0] == 0
    assert state.DB.execute("SELECT count(*) FROM passkeys").fetchone()[0] == 1          # чужой цел


def test_passkey_events_log_no_key_material(studio, caplog):
    caplog.set_level(logging.INFO)
    uid = owner(studio)
    key = add_key(studio)
    lock_on(uid); phone(studio)
    studio.c.post("/api/lock/now")
    options = studio.c.post("/api/lock/challenge").json()
    answer = key.assertion(options)
    assert studio.c.post("/api/lock/open", json={"credential": answer}).status_code == 204
    studio.c.post("/api/lock/open", json={"credential": answer})
    studio.c.delete(f"/api/passkeys/{handle(key)}")
    assert f"пользователь {uid}: ключ добавлен" in caplog.text
    assert f"пользователь {uid}: открыто (passkey)" in caplog.text
    assert f"пользователь {uid}: ключ удалён" in caplog.text
    for secret in (key.id, options["challenge"], answer["response"]["signature"],
                   answer["response"]["clientDataJSON"], base64.urlsafe_b64encode(key.public_key).decode().rstrip("=")):
        assert secret not in caplog.text


# ---- PIN ---------------------------------------------------------------------

def pin_hash(uid):
    return state.DB.execute("SELECT pin_hash FROM users WHERE id = ?", (uid,)).fetchone()[0]


def test_pin_is_set_only_with_the_password_and_removed_without(studio):
    uid = owner(studio)
    for body in ({"pin": "0007"}, {"password": "wrong-pw", "pin": "0007"}):
        r = studio.c.put("/api/me/pin", json=body)
        assert r.status_code == 403 and code(r) == "password", body
    assert studio.c.get("/api/me").json()["pin"] is False
    r = studio.c.put("/api/me/pin", json={"password": "password-123", "pin": "0007"})
    assert r.status_code == 200 and r.json() == {"pin": True}
    assert studio.c.get("/api/me").json()["pin"] is True
    h = pin_hash(uid)
    assert security.verify_password("0007", h) and not security.verify_password("7", h)
    assert studio.c.delete("/api/me/pin").status_code == 204
    assert studio.c.get("/api/me").json()["pin"] is False
    assert pin_hash(uid) is None


def test_pin_format_is_four_to_eight_ascii_digits(studio):
    uid = owner(studio)
    for pin in ("123", "123456789", "12a4", "１２３４", "١٢٣٤", " 1234", "", "12 34"):
        r = studio.c.put("/api/me/pin", json={"password": "password-123", "pin": pin})
        assert r.status_code == 422 and code(r) == "pin_format", pin
        assert ("pw", uid) not in auth.FAILS
    assert pin_hash(uid) is None
    for pin in ("1234", "12345678"):
        assert studio.c.put("/api/me/pin", json={"password": "password-123", "pin": pin}).status_code == 200, pin


def test_locked_studio_cannot_set_or_remove_the_pin(studio):
    uid = owner(studio)
    assert studio.c.put("/api/me/pin", json={"password": "password-123", "pin": "0007"}).status_code == 200
    before = pin_hash(uid)
    lock_on(uid); phone(studio)
    assert studio.c.post("/api/lock/now").status_code == 204
    assert studio.c.put("/api/me/pin", json={"password": "password-123", "pin": "1111"}).status_code == 423
    assert studio.c.delete("/api/me/pin").status_code == 423
    assert pin_hash(uid) == before


def test_pin_events_log_only_ids(studio, caplog):
    caplog.set_level(logging.INFO)
    uid = owner(studio)
    studio.c.put("/api/me/pin", json={"password": "password-123", "pin": "0007"})
    h = pin_hash(uid)
    studio.c.delete("/api/me/pin")
    assert f"пользователь {uid}: PIN задан" in caplog.text
    assert f"пользователь {uid}: PIN убран" in caplog.text
    assert "0007" not in caplog.text and h.split("$")[-1] not in caplog.text


# ---- отпирание PIN-кодом ------------------------------------------------------

def set_pin(uid, value) -> None:
    """Хеш прямо в базу — мимо пароля и паузы."""
    state.DB.execute("UPDATE users SET pin_hash = ? WHERE id = ?", (security.hash_password(value), uid))


def pin(s, value):
    client = s.c if hasattr(s, "c") else s
    return client.post("/api/lock/pin", json={"pin": value})


def pin_fails(uid) -> int:
    return state.DB.execute("SELECT pin_fails FROM users WHERE id = ?", (uid,)).fetchone()[0]


def locked_phone(s, client):
    """Вторая сессия той же учётки — тоже телефон, и тоже заперта."""
    state.DB.execute("UPDATE sessions SET mobile = 1 WHERE digest = ?", (security.digest(client.cookies[auth.COOKIE]),))
    assert client.post("/api/lock/now").status_code == 204
    return client


def test_right_pin_opens_and_resets_the_count(studio):
    uid = locked_owner(studio)
    set_pin(uid, "0007")
    for left, wrong in zip((9, 8, 7), ("1234", "7", "007")):
        r = pin(studio, wrong)
        assert r.status_code == 403 and r.json()["detail"] == {"code": "pin", "message": "неверный PIN", "left": left}
        assert studio.c.get("/api/me").status_code == 423
    studio.clock[0] += 5
    assert pin(studio, "0007").status_code == 204
    assert studio.c.get("/api/me").status_code == 200
    assert pin_fails(uid) == 0 and session_row(studio)["seen"] == studio.clock[0]


def test_ten_wrong_pins_leave_only_the_password(studio):
    uid = locked_owner(studio)
    set_pin(uid, "0007")
    for left in range(PIN_FAILS - 1, -1, -1):
        r = pin(studio, "1111")
        assert r.status_code == 403 and code(r) == "pin" and r.json()["detail"]["left"] == left
    r = pin(studio, "0007")
    assert r.status_code == 403 and r.json()["detail"] == {
        "code": "pin_exhausted", "message": "PIN не принимается после 10 ошибок — откройте паролем учётки"}
    assert studio.c.get("/api/me").status_code == 423
    state_ = studio.c.get("/api/lock").json()
    assert state_["pin"] is True and state_["pin_left"] == 0
    assert studio.c.post("/api/lock/password", json={"password": "password-123"}).status_code == 204
    assert pin_fails(uid) == 0 and studio.c.get("/api/lock").json()["pin_left"] == PIN_FAILS
    assert studio.c.post("/api/lock/now").status_code == 204
    assert pin(studio, "0007").status_code == 204


def test_wrong_pins_of_two_sessions_add_up_and_a_password_elsewhere_resets(studio):
    uid = locked_owner(studio)
    set_pin(uid, "0007")
    other = locked_phone(studio, second_session(studio))
    for client in (studio.c, other):
        for _ in range(5):
            assert code(pin(client, "1111")) == "pin"
    for client in (studio.c, other):
        assert code(pin(client, "0007")) == "pin_exhausted"
    assert other.post("/api/lock/password", json={"password": "password-123"}).status_code == 204
    assert pin(studio, "0007").status_code == 204
    assert studio.c.get("/api/me").status_code == 200


def test_new_pin_and_new_login_do_not_reset_the_count(studio):
    uid = locked_owner(studio)
    set_pin(uid, "0007")
    for _ in range(PIN_FAILS):
        pin(studio, "1111")
    computer = second_session(studio)
    assert computer.put("/api/me/pin", json={"password": "password-123", "pin": "4321"}).status_code == 200
    second_session(studio)                                       # и ещё один вход логином и паролем
    assert pin_fails(uid) == PIN_FAILS
    for value in ("0007", "4321"):
        assert code(pin(studio, value)) == "pin_exhausted", value
    assert studio.c.get("/api/me").status_code == 423


def test_the_attempt_is_counted_before_the_hash_is_checked(studio, monkeypatch):
    uid = locked_owner(studio)
    set_pin(uid, "0007")
    seen, real = [], security.verify_password

    def counting(value, stored):
        seen.append(pin_fails(uid))
        return real(value, stored)

    monkeypatch.setattr(security, "verify_password", counting)
    pin(studio, "1111")
    assert seen == [1]
    seen.clear()
    state.DB.execute("UPDATE users SET pin_fails = ? WHERE id = ?", (PIN_FAILS - 1, uid))
    assert pin(studio, "1111").json()["detail"]["left"] == 0
    assert code(pin(studio, "0007")) == "pin_exhausted"
    assert seen == [PIN_FAILS]


def test_parallel_pins_of_two_sessions_check_the_hash_once(studio, monkeypatch):
    """Пачка параллельных попыток из двух сессий при девяти ошибках: место в счёте занимает одна, и
    scrypt считается один раз (спека 6.2, 6.6)."""
    import main
    uid = locked_owner(studio)
    set_pin(uid, "0007")
    other = locked_phone(studio, second_session(studio))
    state.DB.execute("UPDATE users SET pin_fails = ? WHERE id = ?", (PIN_FAILS - 1, uid))
    calls, real = [], security.verify_password

    def slow(value, stored):
        calls.append(value)
        time.sleep(0.05)                 # scrypt идёт в потоке — остальные запросы пачки успевают прийти
        return real(value, stored)

    monkeypatch.setattr(security, "verify_password", slow)
    sessions = [(c.cookies[auth.COOKIE], c.headers[auth.CSRF_HEADER]) for c in (studio.c, other)]

    async def batch():
        clients = [httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="https://testserver",
                                     cookies={auth.COOKIE: cookie}, headers={auth.CSRF_HEADER: csrf})
                   for cookie, csrf in sessions]
        try:
            return await asyncio.gather(*(clients[i % 2].post("/api/lock/pin", json={"pin": f"{1000 + i}"})
                                          for i in range(8)))
        finally:
            for c in clients:
                await c.aclose()

    answers = studio.c.portal.call(batch)
    assert len(calls) == 1
    assert sorted(r.json()["detail"]["code"] for r in answers) == ["pin"] + ["pin_exhausted"] * 7
    assert pin_fails(uid) == PIN_FAILS


def test_pin_changed_during_the_check_opens_nothing(studio, monkeypatch):
    uid = locked_owner(studio)
    set_pin(uid, "0007")
    new_hash, real = security.hash_password("4321"), security.verify_password

    def racing(value, stored):
        ok = real(value, stored)
        state.DB.execute("UPDATE users SET pin_hash = ? WHERE id = ?", (new_hash, uid))
        return ok

    monkeypatch.setattr(security, "verify_password", racing)
    r = pin(studio, "0007")
    assert r.status_code == 403 and code(r) == "pin" and r.json()["detail"]["left"] == PIN_FAILS - 1
    assert studio.c.get("/api/me").status_code == 423


def test_malformed_pin_is_a_wrong_pin(studio):
    uid = locked_owner(studio)
    set_pin(uid, "1234")
    for left, value in zip((9, 8, 7, 6), ("１２３４", "١٢٣٤", "12a4", "")):
        r = pin(studio, value)
        assert r.status_code == 403 and code(r) == "pin" and r.json()["detail"]["left"] == left, value
    assert pin(studio, "1" * (PIN_MAX + 1)).status_code == 422
    assert pin_fails(uid) == 4
    assert studio.c.get("/api/me").status_code == 423


def test_pin_without_a_pin_is_409_and_passkey_still_opens_an_exhausted_one(studio):
    uid = owner(studio)
    key = add_key(studio)
    lock_on(uid); phone(studio)
    assert studio.c.post("/api/lock/now").status_code == 204
    r = pin(studio, "0007")
    assert r.status_code == 409 and r.json()["detail"] == {"code": "no_pin", "message": "PIN не задан — откройте паролем"}
    assert pin_fails(uid) == 0
    set_pin(uid, "0007")
    for _ in range(PIN_FAILS):
        pin(studio, "1111")
    assert open_with(studio, key).status_code == 204
    assert studio.c.get("/api/me").status_code == 200
    assert pin_fails(uid) == PIN_FAILS                           # passkey счёт не трогает


def test_pin_and_password_pauses_are_apart(studio):
    uid = locked_owner(studio)
    set_pin(uid, "0007")
    for _ in range(PIN_FAILS):
        pin(studio, "1111")
    assert ("pw", uid) not in auth.FAILS
    assert studio.c.post("/api/lock/password", json={"password": "password-123"}).status_code == 204
    studio.c.post("/api/lock/now")
    for _ in range(5):
        studio.c.post("/api/lock/password", json={"password": "wrong-password"})
    assert studio.c.post("/api/lock/password", json={"password": "password-123"}).status_code == 429
    assert pin(studio, "0007").status_code == 204
    assert studio.c.get("/api/me").status_code == 200
