"""OpenCode как третий помощник: каталог, маршрутизация, настройки и сохранность миграции."""

import sqlite3

import pytest

import bridge
import db
import state
from conftest import make_user, signin
from test_chat import events_of
from test_code import A, ASK, CARD, DONE, chat_url, detail, project_url, sent, setup, until

MODEL = "opencode-go/" + "model-" * 20
CATALOG = {"version": "opencode v2.2.4", "models": [{"id": MODEL, "label": "OpenCode Model"}],
           "efforts": ["default", "thinking"],
           "defaults": {"model": MODEL, "effort": "default", "access": "default"}}


def enable(machine, link):
    machine.opencode = CATALOG
    link.hello = bridge.parse_hello(machine.hello)


def test_optional_opencode_catalog_and_old_bridge_rejection(studio):
    aid, machine, link = setup(studio)
    assert "opencode" not in studio.c.get("/api/agents").json()["items"][0]
    before = len(machine.requests)
    for response in (studio.c.post(project_url(aid, "chats") + "?engine=opencode"),
                     studio.c.get(chat_url(aid) + "?engine=opencode"),
                     studio.c.post(chat_url(aid, "state") + "?engine=opencode", json={}),
                     studio.c.post(chat_url(aid, "turn") + "?engine=opencode", json={"text": "x", "model": MODEL})):
        assert (response.status_code, detail(response)["code"]) == (409, "unsupported")
    assert len(machine.requests) == before
    assert state.DB.execute("SELECT count(*) FROM code_chats").fetchone()[0] == 0
    enable(machine, link)
    assert studio.c.get("/api/agents").json()["items"][0]["opencode"] == CATALOG


@pytest.mark.parametrize("bad", [None, {**CATALOG, "models": [{"id": "bad model", "label": "x"}]},
                                 {**CATALOG, "models": [{"id": "x" * 301, "label": "x"}]},
                                 {**CATALOG, "models": CATALOG["models"] * 501},
                                 {**CATALOG, "efforts": ["bad\nvariant"]},
                                 {**CATALOG, "efforts": [None]}])
def test_opencode_catalog_rejects_malformed_ids_and_variants(studio, bad):
    _, machine, _ = setup(studio)
    with pytest.raises(bridge.ProtocolError):
        bridge.parse_hello({**machine.hello, "opencode": bad})


def test_opencode_chat_routes_state_history_turn_and_permission_cards(studio):
    aid, machine, link = setup(studio)
    enable(machine, link)
    response = studio.c.post(project_url(aid, "chats") + "?engine=opencode")
    assert response.status_code == 201 and response.json()["engine"] == "opencode"
    chat = response.json()["chat"]
    endpoint = project_url(aid, "chats", chat)
    assert studio.c.get(endpoint).json() == {"items": [], "start": 0, "total": 0}
    settings = {"model": MODEL, "effort": "thinking", "access": "default"}
    assert studio.c.post(endpoint + "/state", json=settings).json()["settings"] == settings
    assert studio.c.post(endpoint + "/turn?engine=codex", json={"text": "x"}).status_code == 409
    before = len(machine.requests)
    for extra in ({"model": "opus"}, {"effort": "high"}, {"access": "plan"}):
        assert studio.c.post(endpoint + "/turn", json={"text": "x", **extra}).status_code == 422
    assert len(machine.requests) == before
    machine.script = [ASK, {"k": "text", "text": "Готово"}, DONE]
    with studio.c.stream("POST", endpoint + "/turn", json={"text": "Задание", **settings}) as response:
        stream = events_of(response.iter_lines())
        assert until(stream, "ask")[-1] == ("ask", CARD)
        assert studio.c.post(endpoint + "/answer", json={"request_id": "r1", "decision": "allow"}).status_code == 204
        assert list(stream) == [("asked", {"request_id": "r1"}), ("text", {"text": "Готово"}),
                                ("done", {"status": "done"})]
    assert studio.c.get(endpoint).json()["items"][-1]["text"] == "Готово"
    assert all(req["engine"] == "opencode" for req in machine.requests if req.get("chat") == chat)
    assert sent(machine, "turn")[-1]["model"] == MODEL
    found = studio.c.get(project_url(aid, "chats")).json()["items"]
    assert next(item for item in found if item["chat"] == chat)["engine"] == "opencode"


def test_external_opencode_chat_keeps_engine_in_marks_and_stop(studio):
    aid, machine, link = setup(studio)
    enable(machine, link)
    machine.projects["alpha"]["chats"][A]["engine"] = "opencode"
    endpoint = chat_url(aid)
    assert studio.c.get(endpoint).status_code == 404
    assert studio.c.get(endpoint + "?engine=opencode").status_code == 200
    assert studio.c.patch(endpoint + "?engine=opencode", json={"state": "closed"}).status_code == 200
    assert state.DB.execute("SELECT engine FROM code_chats WHERE session_id = ?", (A,)).fetchone()[0] == "opencode"
    machine.script = [ASK]
    with studio.c.stream("POST", endpoint + "/turn", json={"text": "x"}) as response:
        stream = events_of(response.iter_lines())
        until(stream, "ask")
        assert studio.c.post(endpoint + "/stop").status_code == 204
        assert list(stream)[-1] == ("done", {"status": "stopped"})
    assert sent(machine, "stop")[-1]["engine"] == "opencode"


def test_opencode_profile_defaults_preserve_legacy_models_and_efforts(studio):
    make_user(studio, "owner"); signin(studio, "owner")
    saved = {"opencode": {"model": MODEL, "effort": "thinking", "access": "auto"},
             "codex": {"model": "gpt-test", "effort": "ultra"}}
    assert studio.c.patch("/api/me", json={"code_defaults": saved}).json()["code_defaults"] == saved
    for invalid in ({"codex": {"model": MODEL}}, {"claude": {"effort": "thinking"}},
                    {"codex": {"effort": "thinking"}}, {"opencode": {"access": "danger-full-access"}}):
        assert studio.c.patch("/api/me", json={"code_defaults": invalid}).status_code == 422
    assert studio.c.patch("/api/me", json={"code_defaults": {"opencode": {"model": None}}}).json()["code_defaults"] == {
        **saved, "opencode": {**saved["opencode"], "model": None}}
    make_user(studio, "friend"); signin(studio, "friend")
    assert studio.c.get("/api/me").json()["code_defaults"] == {}


def test_step_23_preserves_old_chat_ids_engines_constraints_and_cascades(env):
    conn = db.connect(env.db_path)
    conn.executescript(db.SCHEMA.removesuffix(db.CLEANER_26).removesuffix(db.CODE_25).removesuffix(db.LOCK_24).removesuffix(db.CODE_23)
                       + "\nPRAGMA user_version = 22;")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    conn.execute("INSERT INTO agents(id, user_id, name, key_hash, created) VALUES (1, 1, 'PC', 'h1', 0)")
    for cid, engine, status in ((7, "claude", "open"), (9, "codex", "closed")):
        conn.execute("INSERT INTO code_chats(id, user_id, agent_id, project, session_id, state, created, updated, engine)"
                     " VALUES (?, 1, 1, 'project', ?, ?, 10, 20, ?)", (cid, f"s{cid}", status, engine))
    before = [tuple(row) for row in conn.execute("SELECT * FROM code_chats ORDER BY id")]
    db.migrate(conn); db.migrate(conn)
    assert [tuple(row) for row in conn.execute("SELECT * FROM code_chats ORDER BY id")] == before
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'code_chats_user'").fetchone() is not None
    conn.execute("UPDATE code_chats SET engine = 'opencode' WHERE id = 9")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE code_chats SET engine = 'other'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO code_chats(user_id, agent_id, project, session_id, state, created, updated)"
                     " VALUES (1, 1, 'project', 's7', 'open', 1, 1)")
    conn.execute("DELETE FROM agents WHERE id = 1")
    assert conn.execute("SELECT count(*) FROM code_chats").fetchone()[0] == 0
    conn.close()
