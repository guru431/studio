"""Codex в общем разделе «Код»: совместимость моста, изоляция помощников, миграция и поток."""

import sqlite3

import pytest

import bridge
import db
import state
from historical_schema import install_schema
from test_chat import events_of
from test_code import ALPHA, A, detail, project_url, setup

CATALOG = {"version": "codex-test", "models": [{"id": "codex-test", "label": "Codex Test"}],
           "efforts": ["minimal", "high", "ultra"]}


@pytest.mark.parametrize("engine", ["opencode", "codex"])
def test_explicit_refresh_discovers_new_cli_without_reconnecting(studio, engine):
    aid, machine, link = setup(studio)
    machine.passive_requests = True
    link.hello = bridge.parse_hello({**machine.hello, engine + "_models": True})
    assert engine not in studio.c.get("/api/agents").json()["items"][0]
    catalog = {"version": "2.3.0", "models": [{"id": "model", "label": "Model"}], "efforts": []}
    machine._op_models = lambda req, events: {engine: catalog}
    assert studio.c.get("/api/agents?refresh=true").json()["items"][0][engine] == catalog
    assert bridge.LINKS[aid] is link
    request = next(req for req in machine.requests if req["op"] == "models")
    assert request["engine"] == engine and request["passive"] is True


def test_explicit_refresh_replaces_codex_catalog_on_the_same_link(studio):
    aid, machine, link = setup(studio)
    machine.codex, machine.codex_models, machine.passive_requests = CATALOG, True, True
    link.hello = bridge.parse_hello(machine.hello)
    new = {**CATALOG, "models": [*CATALOG["models"], {"id": "gpt-6.2-sol", "label": "GPT-6.2 Sol"}]}
    machine.codex = new
    assert studio.c.get("/api/agents").json()["items"][0]["codex"] == CATALOG
    assert not any(req["op"] == "models" for req in machine.requests)
    assert studio.c.get("/api/agents?refresh=true").json()["items"][0]["codex"] == new
    assert bridge.LINKS[aid] is link
    request = next(req for req in machine.requests if req["op"] == "models")
    assert request["engine"] == "codex" and request["passive"] is True


@pytest.mark.parametrize("case", ["old", "closed", "invalid"])
def test_refresh_preserves_catalog_for_old_closed_or_invalid_bridge(studio, case):
    aid, machine, link = setup(studio, opened=case != "closed")
    machine.codex, machine.codex_models = CATALOG, case != "old"
    link.hello = bridge.parse_hello(machine.hello)
    machine.codex = {**CATALOG, "efforts": ["invalid"]}
    assert studio.c.get("/api/agents?refresh=true").json()["items"][0]["codex"] == CATALOG
    assert bridge.LINKS[aid] is link
    if case == "old":
        assert not any(req["op"] == "models" for req in machine.requests)
    if case == "closed":
        assert not machine.open_sessions


def test_refresh_does_not_return_computers_after_logout_during_the_probe(studio):
    _, machine, link = setup(studio)
    machine.codex, machine.codex_models = CATALOG, True
    link.hello = bridge.parse_hello(machine.hello)

    def logout(req, events):
        state.DB.execute("DELETE FROM sessions")
        return {"codex": CATALOG}

    machine._op_models = logout
    result = studio.c.get("/api/agents?refresh=true")
    assert result.status_code == 409 and detail(result)["code"] == "closed"
    assert "items" not in result.json()


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
    conn = db.connect(env.db_path)
    install_schema(conn, 21)
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created, views) VALUES (1, 'u', 'U', 'h', 0, ?)",
                 ('{"photo":"table"}',))
    db.migrate(conn)
    row = conn.execute("SELECT views, code_defaults FROM users WHERE id = 1").fetchone()
    assert row[:] == ('{"photo":"table"}', '{}')


def test_step_21_keeps_existing_claude_chats_and_accepts_only_known_engines(env):
    conn = db.connect(env.db_path)
    install_schema(conn, 20)
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    conn.execute("INSERT INTO agents(id, user_id, name, key_hash, created) VALUES (1, 1, 'PC', 'h1', 0)")
    conn.execute("INSERT INTO code_chats(user_id, agent_id, project, session_id, state, created, updated)"
                 " VALUES (1, 1, 'project', 'session', 'open', 1, 2)")
    db.migrate(conn)
    db.migrate(conn)
    assert conn.execute("SELECT session_id, engine, state FROM code_chats").fetchone()[:] == ("session", "claude", "open")
    conn.execute("UPDATE code_chats SET engine = 'codex'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE code_chats SET engine = 'other'")
    conn.close()
