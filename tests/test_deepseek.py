"""DeepSeek Harness как четвёртый помощник на сервере: каталог `hello.deepseek`, шаг схемы, маршруты, лимиты."""

import copy
import sqlite3
from contextlib import closing

import pytest

import bridge
import code_usage
import computer_info
import db
import state
from fake_bridge import Refused
from historical_schema import install_schema
from test_chat import events_of
from test_code import A, ALPHA, DONE, chat_url, detail, project_url, sent, setup, url

MODEL = '["deepseek-official","deepseek-chat"]'
CATALOG = {"version": "0.1.0-rc.6", "models": [{"id": MODEL, "label": "DeepSeek Chat"}], "efforts": ["high", "off"],
           "defaults": {"model": MODEL, "effort": None, "access": "default"},
           "settings": {"profile": "web", "applied": ["mcp-manager"],
                        "skipped": [{"name": "llm-pi-ai:custom", "reason": "env_key"}]}}
USAGE = {"engine": "deepseek", "available": True, "source": "api.deepseek.com", "checked_at": 1000, "cache_ttl": 60,
         "plan": None, "windows": [{"id": "five_hour", "label": "5 часов", "available": False,
                                    "reason": "У DeepSeek нет окон лимита — расходуется баланс"}],
         "statistics": {"available": False, "source": "", "summary": {}, "daily": []},
         "balance": {"available": True, "items": [{"currency": "CNY", "total": 10.5, "granted": 0.5, "topped_up": 10.0}]},
         "links": []}


def enable(machine, link):
    machine.deepseek, machine.deepseek_models, machine.deepseek_usage = CATALOG, True, True
    link.hello = bridge.parse_hello(machine.hello)


def external(machine, studio, aid):
    """Чат DSH на машине и его отметка в студии."""
    machine.projects["alpha"]["chats"][A]["engine"] = "deepseek"
    assert studio.c.patch(chat_url(aid) + "?engine=deepseek", json={"state": "open"}).status_code == 200


def test_hello_deepseek_catalog_parsed_and_bad_rejected(studio):
    _, machine, _ = setup(studio)
    machine.deepseek, machine.deepseek_models, machine.deepseek_usage = CATALOG, True, True
    hello = bridge.parse_hello(machine.hello)
    assert hello["deepseek"] == CATALOG
    assert hello["deepseek_models"] is True and hello["deepseek_usage"] is True
    unknown = {**CATALOG, "settings": {"profile": None, "applied": [], "skipped": []}}
    assert bridge.parse_hello({**machine.hello, "deepseek": unknown})["deepseek"]["settings"]["profile"] is None
    for bad in (None, {**CATALOG, "models": [{"id": "deepseek-chat", "label": "x"}]},
                {**CATALOG, "models": [{"id": '["a","b\\"c"]', "label": "x"}]},
                {**CATALOG, "models": CATALOG["models"] * 501},
                {**CATALOG, "efforts": ["bad\neffort"]},
                {key: value for key, value in CATALOG.items() if key != "settings"},
                {**CATALOG, "settings": {**CATALOG["settings"], "profile": "console"}},
                {**CATALOG, "settings": {"applied": [], "skipped": []}},           # без `profile` — не KeyError
                {**CATALOG, "settings": {**CATALOG["settings"], "applied": ["x" * 101]}},
                {**CATALOG, "settings": {**CATALOG["settings"], "applied": ["x"] * 101}},
                {**CATALOG, "settings": {**CATALOG["settings"], "skipped": [{"name": "x", "reason": "other"}]}},
                {**CATALOG, "settings": {**CATALOG["settings"], "skipped": [{"name": "x", "reason": "unknown"}] * 101}}):
        with pytest.raises(bridge.ProtocolError):
            bridge.parse_hello({**machine.hello, "deepseek": bad})
    with pytest.raises(bridge.ProtocolError):
        bridge.parse_hello({**machine.hello, "deepseek_usage": "yes"})


def test_step_64_widens_engine_check_and_keeps_origin(env):
    with closing(db.connect(env.db_path)) as conn:
        install_schema(conn, 63)
        conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
        conn.execute("INSERT INTO agents(id, user_id, name, key_hash, created) VALUES (1, 1, 'PC', 'h1', 0)")
        for cid, engine, status, origin in ((7, "claude", "open", "studio"), (9, "opencode", "closed", "other")):
            conn.execute("INSERT INTO code_chats(id, user_id, agent_id, project, session_id, state, created, updated,"
                         " engine, origin) VALUES (?, 1, 1, 'project', ?, ?, 10, 20, ?, ?)",
                         (cid, f"s{cid}", status, engine, origin))
        before = [tuple(row) for row in conn.execute("SELECT * FROM code_chats ORDER BY id")]
        db.migrate(conn)
        db.migrate(conn)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION == 64
        assert [tuple(row) for row in conn.execute("SELECT * FROM code_chats ORDER BY id")] == before
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'code_chats_user'").fetchone() is not None
        conn.execute("INSERT INTO code_chats(user_id, agent_id, project, session_id, state, created, updated, engine,"
                     " origin) VALUES (1, 1, 'project', 's10', 'open', 1, 1, 'deepseek', 'studio')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE code_chats SET engine = 'other' WHERE id = 7")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE code_chats SET origin = 'other-tool' WHERE id = 7")
        migrated = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'code_chats'").fetchone()[0]
        conn.execute("DELETE FROM agents WHERE id = 1")
        assert conn.execute("SELECT count(*) FROM code_chats").fetchone()[0] == 0
    with closing(sqlite3.connect(":memory:")) as fresh:
        fresh.executescript(db.SCHEMA)
        assert fresh.execute("SELECT sql FROM sqlite_master WHERE name = 'code_chats'").fetchone()[0] == migrated


def test_chat_with_deepseek_engine_routes_to_bridge(studio):
    aid, machine, link = setup(studio)
    enable(machine, link)
    assert studio.c.get("/api/agents").json()["items"][0]["deepseek"] == CATALOG
    response = studio.c.post(project_url(aid, "chats") + "?engine=deepseek")
    assert response.status_code == 201 and response.json()["engine"] == "deepseek"
    chat = response.json()["chat"]
    endpoint = project_url(aid, "chats", chat)
    settings = {"model": MODEL, "effort": "high", "access": "workspace-write"}
    assert studio.c.post(endpoint + "/state", json=settings).json()["settings"] == settings
    before = len(machine.requests)
    for extra in ({"model": "opus"}, {"effort": "max"}, {"access": "auto"}, {"agent_mode": "plan"}):
        assert studio.c.post(endpoint + "/turn", json={"text": "x", **extra}).status_code == 422
    assert len(machine.requests) == before
    machine.script = [{"k": "text", "text": "Готово"}, DONE]
    with studio.c.stream("POST", endpoint + "/turn", json={"text": "Задание", **settings}) as response:
        assert list(events_of(response.iter_lines()))[-2] == ("text", {"text": "Готово"})
    assert all(req["engine"] == "deepseek" for req in machine.requests if req.get("chat") == chat)
    assert sent(machine, "turn")[-1]["model"] == MODEL
    found = studio.c.get(project_url(aid, "chats")).json()["items"]
    assert next(item for item in found if item["chat"] == chat)["engine"] == "deepseek"


def test_fork_and_subagents_refused_for_deepseek(studio):
    aid, machine, link = setup(studio)
    machine.chat_fork = machine.subagents = True
    enable(machine, link)
    external(machine, studio, aid)
    before = len(machine.requests)
    for response in (studio.c.post(chat_url(aid, "fork"), json={}),
                     studio.c.get(chat_url(aid, "subagents")),
                     studio.c.post(chat_url(aid, "subagent"), json={"request_id": "r1"})):
        assert (response.status_code, detail(response)["code"]) == (409, "unsupported")
    assert len(machine.requests) == before
    assert state.DB.execute("SELECT count(*) FROM code_chats").fetchone()[0] == 1


def test_old_bridge_without_deepseek_gets_409(studio):
    aid, machine, link = setup(studio)
    assert "deepseek" not in studio.c.get("/api/agents").json()["items"][0]
    before = len(machine.requests)
    for response in (studio.c.post(project_url(aid, "chats") + "?engine=deepseek"),
                     studio.c.get(chat_url(aid) + "?engine=deepseek"),
                     studio.c.post(chat_url(aid, "state") + "?engine=deepseek", json={}),
                     studio.c.post(chat_url(aid, "turn") + "?engine=deepseek", json={"text": "x"})):
        assert (response.status_code, detail(response)["code"]) == (409, "unsupported")
        assert "DeepSeek Harness" in detail(response)["message"]
    assert len(machine.requests) == before
    assert state.DB.execute("SELECT count(*) FROM code_chats").fetchone()[0] == 0


def test_writer_active_refusal_reaches_the_page_as_409(studio):
    aid, machine, link = setup(studio)
    enable(machine, link)
    external(machine, studio, aid)
    words = "Чат открыт в DeepSeek Harness на компьютере — закройте DeepSeek Harness и повторите"

    def busy(req, events):
        raise Refused("writer_active", words)
    machine._op_state = busy
    response = studio.c.post(chat_url(aid, "state"), json={})
    assert response.status_code == 409 and detail(response) == {"code": "writer_active", "message": words}


def test_usage_balance_passthrough_and_validation(studio):
    aid, machine, link = setup(studio)
    enable(machine, link)
    link.hello["subscription_usage"] = True
    data = copy.deepcopy(USAGE)
    machine._op_usage = lambda req, events: data
    endpoint = url(aid, "usage") + "?engine=deepseek"
    response = studio.c.get(endpoint + "&model=" + MODEL)
    assert response.status_code == 200
    assert response.json()["balance"] == USAGE["balance"]
    assert response.json()["links"] == code_usage.LINKS["deepseek"]
    assert [link["url"].rsplit("/", 1)[1] for link in code_usage.LINKS["deepseek"]] == ["usage", "top_up"]
    assert machine.requests[-1]["engine"] == "deepseek" and machine.requests[-1]["model"] == MODEL
    data["balance"] = {"available": False, "items": [], "reason": "На компьютере нет ключа DeepSeek"}
    assert studio.c.get(endpoint).json()["balance"] == data["balance"]
    before = len(machine.requests)
    assert studio.c.get(endpoint + "&model=gateway%2Fmodel").status_code == 422
    assert len(machine.requests) == before
    for bad in ([{"currency": "C" * 9, "total": 1, "granted": 0, "topped_up": 1}],
                [{"currency": "CNY", "total": -1, "granted": 0, "topped_up": 1}],
                [{"currency": "CNY", "total": 1, "granted": 0, "topped_up": 1}] * 11):
        data["balance"] = {"available": True, "items": bad}
        response = studio.c.get(endpoint)
        assert (response.status_code, detail(response)["code"]) == (502, "bridge")


def test_history_notice_passthrough(studio):
    aid, machine, link = setup(studio)
    enable(machine, link)
    external(machine, studio, aid)
    original = machine._op_history
    machine._op_history = lambda req, events: {**original(req, events), "notice": "unsupported_format"}
    response = studio.c.get(chat_url(aid))
    assert response.status_code == 200 and response.json()["notice"] == "unsupported_format"
    assert sent(machine, "history")[-1]["engine"] == "deepseek"


def test_share_and_export_of_unreadable_history_are_refused(studio):
    """M4: мост не прочёл историю (`notice`) — не пустой снимок, а 409 со словами заметки."""
    import chat_shares
    aid, machine, link = setup(studio)
    enable(machine, link)
    external(machine, studio, aid)
    machine._op_history = lambda req, events: {"items": [], "start": 0, "total": 0, "engine": "deepseek",
                                               "notice": "unsupported_format"}
    for response in (studio.c.post(chat_url(aid, "share"), json={"pin": "0123"}),
                     studio.c.post(chat_url(aid, "export"), json={"format": "md"})):
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == {"code": "history_unavailable", "message": chat_shares.UNREADABLE}
    assert state.DB.execute("SELECT COUNT(*) FROM chat_shares").fetchone()[0] == 0


def test_share_link_of_deepseek_chat_keeps_its_engine(studio):
    from test_chat_shares import create, unlock

    aid, machine, link = setup(studio)
    enable(machine, link)
    external(machine, studio, aid)
    _, token = create(studio, chat_url(aid, "share"))
    assert len(unlock(studio, token).json()["messages"]) == 250
    assert state.DB.execute("SELECT engine FROM chat_shares").fetchone()[0] == "deepseek"
    assert {req["engine"] for req in sent(machine, "history")} == {"deepseek"}


def test_computer_info_keeps_four_tools():
    item = {"installed": True, "version": "0.1.0-rc.6", "source": "npm", "reason": None}
    data = computer_info.software({"items": [{"id": tool, **item} for tool in
                                             ("claude", "codex", "opencode", "deepseek", "deepseek")],
                                   "update": {"tool": "all", "state": "done",
                                              "results": {"deepseek": "done", "opencode": "error"}}})
    assert [row["id"] for row in data["items"]] == ["claude", "codex", "opencode", "deepseek"]
    assert data["update"]["results"] == {"opencode": "error", "deepseek": "done"}
    assert computer_info.software({"items": [], "update": {"tool": "deepseek", "state": "waiting"}})["update"]["tool"] == "deepseek"


def test_software_update_accepts_deepseek(studio):
    aid, machine, link = setup(studio)
    link.hello["software_versions"] = True
    assert studio.c.post(url(aid, "software", "deepseek", "update")).status_code == 200
    assert machine.requests[-1]["software"] == "deepseek"


def test_mcp_settings_accept_deepseek(studio):
    aid, machine, link = setup(studio)
    link.hello["mcp"] = True
    machine._op_mcp_get = lambda req, events: {"config": {"mcpServers": {}}}
    response = studio.c.get(url(aid, "projects", ALPHA, "mcp") + "?engine=deepseek")
    assert response.status_code == 200 and machine.requests[-1]["engine"] == "deepseek"


def test_member_does_not_see_owner_overlay_summary(studio):
    """M5: сводка наложения (имена строк профиля владельца) — участнику совместного проекта не отдаётся."""
    from conftest import signin
    from test_project_access import share

    aid, machine, link, _, _ = share(studio)
    enable(machine, link)
    assert studio.c.get("/api/agents").json()["items"][0]["deepseek"]["settings"] == CATALOG["settings"]
    signin(studio, "friend")
    catalog = studio.c.get("/api/agents").json()["items"][0]["deepseek"]
    assert "settings" not in catalog and catalog["models"] == CATALOG["models"]
