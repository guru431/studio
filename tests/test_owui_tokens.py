"""Учётки Open WebUI: таблица (шаг 63) и постоянная сессия — рабочий токен в памяти, обновление с ротацией,
восстановление по `request_id`, `relogin`."""
import asyncio
import json
from contextlib import closing

import pytest

import clock
import db
import owui
import owui_tokens
import security
import state
from conftest import make_user
from fake_owui import FakeOwui, attach
from historical_schema import install_schema


def run(coro):
    return asyncio.run(coro)


def add_account(fake: FakeOwui, user_id: int, state_: str = "ok") -> owui.Account:
    """Учётка прямо в базе, с долгим токеном от `fake.issue_session()` (вход и пароль не нужны)."""
    secret = fake.issue_session()
    who = {"owui_user_id": fake.user_id, "name": fake.name, "email": fake.EMAIL}
    cur = state.DB.execute(
        "INSERT INTO owui_accounts(user_id, label, base_url, who_enc, secret_enc, state, created) VALUES (?,?,?,?,?,?,?)",
        (user_id, "s06 · work", fake.URL, security.encrypt(json.dumps(who)),
         security.encrypt(json.dumps({"session_id": secret.session_id, "refresh_token": secret.refresh_token,
                                      "pending_request_id": None})), state_, clock.NOW()))
    return owui.Account(cur.lastrowid, user_id, "s06 · work", fake.URL, None, state_)


@pytest.fixture
def setup(studio):
    user = make_user(studio, "owner")
    fake = attach(studio)
    return studio, fake, add_account(fake, user)


def test_schema_has_owui_accounts_and_step_63(env):
    def check(conn):
        cols = [r[1] for r in conn.execute("PRAGMA table_info(owui_accounts)")]
        assert cols == ["id", "user_id", "label", "base_url", "who_enc", "secret_enc", "cert_pem", "state",
                        "version", "created", "used"]
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION
        assert "owui_accounts_user" in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        conn.execute("INSERT INTO users(id,login,name,pw_hash,created) VALUES(1,'owner','Owner','hash',0)")
        add = ("INSERT INTO owui_accounts(user_id,label,base_url,who_enc,secret_enc,state,created)"
               " VALUES(1,'a','u',x'00',x'00',?,0)")
        assert conn.execute(add, ("ok",)).lastrowid == 1
        with pytest.raises(Exception, match="CHECK"):
            conn.execute(add, ("broken",))
        conn.execute("DELETE FROM owui_accounts")
        assert conn.execute(add, ("relogin",)).lastrowid == 2          # AUTOINCREMENT: id удалённой не достаётся новой
        conn.execute("DELETE FROM users WHERE id = 1")                  # учётка уходит с пользователем
        assert conn.execute("SELECT count(*) FROM owui_accounts").fetchone()[0] == 0

    assert db.VERSION >= 63 and db.STEPS[63] == db.OWUI_63
    with closing(db.connect(env.db_path)) as conn:                      # новая база
        db.migrate(conn)
        check(conn)
    for suffix in ("", "-wal", "-shm"):
        env.db_path.with_name(env.db_path.name + suffix).unlink(missing_ok=True)
    with closing(db.connect(env.db_path)) as conn:                      # база 62 после шага 63
        install_schema(conn, 62)
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name = 'owui_accounts'").fetchone()[0] == 0
        db.migrate(conn)
        check(conn)


def test_access_refreshes_ahead_of_expiry(setup):
    s, fake, acc = setup
    token = run(owui_tokens.access(acc))                  # в памяти токена нет (после перезапуска) — обновление
    assert fake.refreshes == 1 and "authorization" not in fake.requests[-1].headers
    s.clock[0] += 10
    assert run(owui_tokens.access(acc)) == token and fake.refreshes == 1       # 890 с до конца
    s.clock[0] += 771                                                          # прошло 781 с: до конца 119 < 120
    fresh = run(owui_tokens.access(acc))
    assert fresh != token and fake.refreshes == 2
    assert state.DB.execute("SELECT used FROM owui_accounts WHERE id = ?", (acc.id,)).fetchone()[0] == s.clock[0]


def test_rotation_recovers_with_the_same_request_id(setup, monkeypatch):
    s, fake, acc = setup
    first = owui_tokens.load(acc.id)
    real_save = owui_tokens.save

    def crash_after_answer(aid, secret):
        if secret.pending_request_id is None:             # запись новой пары — Студия «падает» до неё
            raise OSError("crash")
        real_save(aid, secret)

    monkeypatch.setattr(owui_tokens, "save", crash_after_answer)
    with pytest.raises(OSError):
        run(owui_tokens.renew(acc))
    assert fake.refreshes == 1 and acc.id not in owui_tokens.TOKENS
    stuck = owui_tokens.load(acc.id)
    assert stuck.pending_request_id and stuck.refresh_token == first.refresh_token     # прежний токен, id запомнен

    monkeypatch.setattr(owui_tokens, "save", real_save)
    token = run(owui_tokens.renew(acc))
    assert fake.refresh_log[0] == fake.refresh_log[1] == (first.refresh_token, stuck.pending_request_id)
    assert fake.refreshes == 1                            # Open WebUI вернул ту же выдачу, а не новую пару
    done = owui_tokens.load(acc.id)
    assert done.pending_request_id is None and done.refresh_token != first.refresh_token
    assert owui_tokens.TOKENS[acc.id][0] == token

    run(owui_tokens.renew(acc, force=True))               # следующее обновление — новый request_id
    assert fake.refresh_log[2][0] == done.refresh_token and fake.refresh_log[2][1] != stuck.pending_request_id
    assert fake.refreshes == 2


def test_refused_refresh_turns_account_into_relogin(setup):
    s, fake, acc = setup
    run(owui_tokens.access(acc))
    assert acc.id in owui_tokens.TOKENS
    fake.revoke_all()
    s.clock[0] += 800
    with pytest.raises(owui_tokens.Relogin):
        run(owui_tokens.access(acc))
    assert state.DB.execute("SELECT state FROM owui_accounts WHERE id = ?", (acc.id,)).fetchone()[0] == "relogin"
    assert acc.id not in owui_tokens.TOKENS
    asked = len(fake.requests)                             # учётка уже в relogin — Open WebUI не спрашивают
    with pytest.raises(owui_tokens.Relogin):
        run(owui_tokens.access(owui.Account(acc.id, acc.user_id, acc.label, acc.base_url, None, "relogin")))
    assert len(fake.requests) == asked


def test_unreachable_server_is_unavailable_and_keeps_the_session(setup):
    s, fake, acc = setup
    fake.down = True
    with pytest.raises(owui_tokens.Unavailable) as e:
        run(owui_tokens.access(acc))
    assert e.value.code is None
    assert state.DB.execute("SELECT state FROM owui_accounts WHERE id = ?", (acc.id,)).fetchone()[0] == "ok"
    stuck = owui_tokens.load(acc.id)
    assert stuck.pending_request_id                        # запрос не дошёл, но id уже записан
    fake.down = False
    assert run(owui_tokens.access(acc))                    # тот же долгий токен, тот же request_id
    assert fake.refresh_log == [(stuck.refresh_token, stuck.pending_request_id)] and fake.refreshes == 1


def test_parallel_calls_refresh_once(setup):
    s, fake, acc = setup

    async def five():
        return await asyncio.gather(*[owui_tokens.access(acc) for _ in range(5)])

    tokens = run(five())
    assert fake.refreshes == 1 and len(set(tokens)) == 1


def test_secret_is_never_plain_in_the_database(setup):
    s, fake, acc = setup
    before = owui_tokens.load(acc.id)
    run(owui_tokens.renew(acc, force=True))
    after = owui_tokens.load(acc.id)
    raw = state.DB.execute("SELECT who_enc, secret_enc FROM owui_accounts WHERE id = ?", (acc.id,)).fetchone()
    blob = bytes(raw["who_enc"]) + bytes(raw["secret_enc"])
    for plain in (before.refresh_token, after.refresh_token, after.session_id, fake.EMAIL,
                  owui_tokens.TOKENS[acc.id][0]):
        assert plain.encode() not in blob
    assert json.loads(security.decrypt(raw["secret_enc"]))["refresh_token"] == after.refresh_token
