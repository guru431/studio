"""Codex в общем разделе «Код»: совместимость моста, изоляция помощников, миграция и поток."""

import sqlite3

import pytest

import bridge
import db
import state
from test_chat import events_of
from test_code import ALPHA, A, detail, project_url, setup

CATALOG = {"version": "codex-test", "models": [{"id": "codex-test", "label": "Codex Test"}],
           "efforts": ["minimal", "high", "ultra"]}


def test_optional_hello_catalog_is_validated_separately_and_old_bridge_works(studio):
    aid, machine, link = setup(studio)
    assert "codex" not in studio.c.get("/api/agents").json()["items"][0]
    endpoint = project_url(aid, "chats")
    r = studio.c.post(endpoint + "?engine=codex")
    assert (r.status_code, detail(r)["code"]) == (409, "unsupported")
    assert state.DB.execute("SELECT count(*) FROM code_chats").fetchone()[0] == 0
    hello = bridge.parse_hello({**machine.hello, "codex": CATALOG})
    link.hello = hello
    assert studio.c.get("/api/agents").json()["items"][0]["codex"] == CATALOG
    with pytest.raises(bridge.ProtocolError):
        bridge.parse_hello({**machine.hello, "codex": {**CATALOG, "efforts": ["invented"]}})
    with pytest.raises(bridge.ProtocolError):
        bridge.parse_hello({**machine.hello, "codex": {**CATALOG, "models": [{"id": "bad/path", "label": "bad"}]}})


def test_codex_chat_remembers_engine_and_routes_history_turn_and_cards(studio):
    aid, machine, link = setup(studio)
    link.hello = bridge.parse_hello({**machine.hello, "codex": CATALOG})
    r = studio.c.post(project_url(aid, "chats") + "?engine=codex")
    assert r.status_code == 201 and r.json()["engine"] == "codex"
    chat = r.json()["chat"]
    endpoint = project_url(aid, "chats", chat)
    assert studio.c.get(endpoint).json() == {"items": [], "start": 0, "total": 0}
    assert machine.requests[-1]["engine"] == "codex"
    before = len(machine.requests)
    assert studio.c.post(endpoint + "/turn", json={"text": "x", "model": "opus"}).status_code == 422
    assert len(machine.requests) == before
    r = studio.c.post(endpoint + "/turn?engine=claude", json={"text": "x"})
    assert (r.status_code, detail(r)["code"]) == (409, "engine")
    machine.script = [{"k": "text", "text": "Ответ Codex"}, {"k": "done", "status": "done"}]
    r = studio.c.post(endpoint + "/turn", json={"text": "Задание", "model": "codex-test", "effort": "ultra"})
    assert r.status_code == 200 and ("text", {"text": "Ответ Codex"}) in list(events_of(r.iter_lines()))
    assert studio.c.post(endpoint + "/answer", json={"request_id": "p1", "decision": "allow"}).status_code == 410
    assert all(q["engine"] == "codex" for q in machine.requests if q.get("chat") == chat)
    found = studio.c.get(project_url(aid, "chats")).json()["items"]
    assert next(c for c in found if c["chat"] == chat)["engine"] == "codex"
    assert studio.c.patch(endpoint, json={"state": "closed"}).status_code == 200
    assert studio.c.patch(endpoint + "?engine=claude", json={"state": "open"}).status_code == 409


def test_external_codex_history_requires_its_engine_and_preserves_it_on_close(studio):
    aid, machine, link = setup(studio)
    link.hello = bridge.parse_hello({**machine.hello, "codex": CATALOG})
    machine.projects["alpha"]["chats"][A]["engine"] = "codex"
    endpoint = project_url(aid, "chats", A)
    assert studio.c.get(endpoint).status_code == 404
    assert studio.c.get(endpoint + "?engine=codex").status_code == 200
    assert studio.c.patch(endpoint + "?engine=codex", json={"state": "closed"}).status_code == 200
    assert state.DB.execute("SELECT engine FROM code_chats WHERE session_id = ?", (A,)).fetchone()[0] == "codex"
    assert studio.c.get(endpoint).status_code == 200


def test_step_22_keeps_user_settings_and_starts_with_native_defaults(env):
    import db
    conn = db.connect(env.db_path)
    db.migrate(conn)
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created, views) VALUES (1, 'u', 'U', 'h', 0, ?)",
                 ('{"photo":"table"}',))
    from test_foundation import DROP_V24
    conn.executescript(DROP_V24 + "ALTER TABLE users DROP COLUMN code_defaults; PRAGMA user_version = 21;")
    db.migrate(conn)
    row = conn.execute("SELECT views, code_defaults FROM users WHERE id = 1").fetchone()
    assert row[:] == ('{"photo":"table"}', '{}')


def test_step_21_keeps_existing_claude_chats_and_accepts_only_known_engines(env):
    conn = db.connect(env.db_path)
    db.migrate(conn)
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    conn.execute("INSERT INTO agents(id, user_id, name, key_hash, created) VALUES (1, 1, 'PC', 'h1', 0)")
    conn.execute("INSERT INTO code_chats(user_id, agent_id, project, session_id, state, created, updated)"
                 " VALUES (1, 1, 'project', 'session', 'open', 1, 2)")
    from test_foundation import DROP_V24
    conn.executescript(DROP_V24 + "ALTER TABLE code_chats DROP COLUMN engine; ALTER TABLE users DROP COLUMN code_defaults;"
                       " PRAGMA user_version = 20;")
    db.migrate(conn)
    db.migrate(conn)
    assert conn.execute("SELECT session_id, engine, state FROM code_chats").fetchone()[:] == ("session", "claude", "open")
    conn.execute("UPDATE code_chats SET engine = 'codex'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE code_chats SET engine = 'other'")
    conn.close()
