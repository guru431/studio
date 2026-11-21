"""MCP в диалоге (спека 2026-11-21-studio-source-foundation-design, §4 и §7a п. 2, 5): общие серверы включены по
умолчанию, личные — только вручную, выключенные серверы и инструменты, каталог из кэша, пометки «интернет» и
«с подтверждением», политика администратора, правила серверов в системном сообщении."""
import asyncio
import json
import time

import httpx
import pytest

import access
import chat_system
import mcp_client
import mcp_sources
import state
from conftest import add_connection, make_user, signin
from test_access_groups import shared_tools
from test_chat import messages, new_chat, sse, turn
from test_chat_web import call
from test_mcp import TOKEN as MCP_KEY

HOST = "http://10.77.1.15:4000"
RULES = "private-rules: отвечай по-деловому"


def tool(name, **extra):
    return {"name": name, "description": f"desc {name}", "inputSchema": {"type": "object"}, **extra}


def server(studio, path="/mcp", *, tools=None, instructions=None, prompts=None, hang=False, fail=()):
    """Мок MCP на `path`: вызовы — в возвращаемом списке; `hang` — каждый запрос висит 5 с (настоящих);
    `fail` — методы, которые один раз отвечают 500; инструмент `gone` отвечает «нет такого инструмента»."""
    calls, fail = [], list(fail)
    tools = [tool("lookup")] if tools is None else tools

    async def handler(request):
        if request.method == "DELETE":
            calls.append(("close", {}))
            return httpx.Response(204)
        payload = json.loads(request.content)
        method, params = payload["method"], payload.get("params", {})
        calls.append((method, params))
        if hang:
            await asyncio.sleep(5)
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method in fail:
            fail.remove(method)
            return httpx.Response(500)
        if method == "initialize":
            capabilities = {"tools": {}, **({"prompts": {}} if prompts else {})}
            result = {"protocolVersion": "2025-06-18", "capabilities": capabilities, "instructions": instructions,
                      "serverInfo": {"name": "fake", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": tools}
        elif method == "prompts/list":
            result = {"prompts": prompts}
        elif method == "prompts/get":
            result = {"messages": [{"role": "user", "content": {
                "type": "text", "text": f"Обзор: {params['arguments'].get('topic', '')}"}}]}
        elif params.get("name") == "gone":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"],
                                             "error": {"code": -32602, "message": "Unknown tool: gone"}})
        else:
            result = {"content": [{"type": "text", "text": f"result-{params.get('name')}"}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
                              headers={"mcp-session-id": "private-session"})

    studio.gw.on[path] = handler
    return calls


def methods(calls):
    return [method for method, _ in calls]


def offered(request):
    """Инструменты MCP запроса к модели: «Сервер: инструмент»."""
    return [t["function"]["description"].split(". ", 1)[0] for t in request.get("tools", [])
            if t["function"]["name"].startswith("mcp_")]


def add_shared(studio, name, path):
    """Ещё один общий MCP (вход — администратором)."""
    response = studio.c.post("/api/admin/shared/mcp", json={"name": name, "url": HOST + path, "token": MCP_KEY})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def setup(studio, **kwargs):
    """Администратор, участник, общий API моделей и общий MCP «Shared tools» на /mcp; вход — участником."""
    make_user(studio, "admin", is_admin=True)
    member = make_user(studio, "member")
    sid = shared_tools(studio)
    calls = server(studio, **kwargs)
    signin(studio, "member")
    return sid, calls, member


def view(studio, cid):
    return studio.c.get(f"/api/chats/{cid}").json()["chat"]


def name_of(sid, n=0):
    return f"mcp_{sid}_{n}"


# ---- набор диалога ----------------------------------------------------------------------------------------

def test_shared_server_on_by_default(studio):
    sid, calls, _ = setup(studio)
    cid = new_chat(studio)
    shown = view(studio, cid)
    assert shown["mcp_ids"] == [sid] and shown["mcp_off"] == [] and shown["mcp_tools_off"] == {}
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid))]}, ["Готово"]]
    assert sse(turn(studio, cid, "Use tool"))[-1] == ("done", {"status": "done"})
    assert offered(studio.gw.chat_requests[0]) == ["Shared tools: lookup"]
    assert methods(calls) == ["initialize", "notifications/initialized", "tools/list", "tools/call", "close"]


def test_turned_off_server_not_polled(studio):
    sid, calls, _ = setup(studio)
    cid = new_chat(studio)
    changed = studio.c.patch(f"/api/chats/{cid}", json={"mcp_off": [sid]})
    assert changed.status_code == 200 and changed.json()["mcp_ids"] == [] and changed.json()["mcp_off"] == [sid]
    studio.gw.chat_script = [["Готово"]]
    assert sse(turn(studio, cid))[-1] == ("done", {"status": "done"})
    assert calls == [] and offered(studio.gw.chat_requests[-1]) == []
    # явное включение снимает сервер из выключенных
    back = studio.c.patch(f"/api/chats/{cid}", json={"mcp_ids": [sid]}).json()
    assert back["mcp_ids"] == [sid] and back["mcp_off"] == []


def test_closed_by_groups_not_included(studio):
    sid, calls, _ = setup(studio)
    access.set_rule("mcp", sid, [])
    cid = new_chat(studio)
    assert view(studio, cid)["mcp_ids"] == []
    studio.gw.chat_script = [["Готово"]]
    assert sse(turn(studio, cid))[-1] == ("done", {"status": "done"})
    assert calls == []


def test_personal_server_only_explicit(studio):
    sid, calls, _ = setup(studio)
    mine_calls = server(studio, "/mine", tools=[tool("note")])
    mine = studio.c.post("/api/mcp", json={"name": "Mine", "url": HOST + "/mine"}).json()["id"]
    cid = new_chat(studio)
    assert view(studio, cid)["mcp_ids"] == [sid]
    studio.gw.chat_script = [["Готово"]]
    sse(turn(studio, cid))
    assert mine_calls == [] and offered(studio.gw.chat_requests[-1]) == ["Shared tools: lookup"]
    assert studio.c.patch(f"/api/chats/{cid}", json={"mcp_ids": [mine]}).json()["mcp_ids"] == [mine, sid]
    studio.gw.chat_script = [["Готово"]]
    sse(turn(studio, cid))
    assert offered(studio.gw.chat_requests[-1]) == ["Mine: note", "Shared tools: lookup"]


def test_tool_off_not_offered_and_refused_midturn(studio):
    sid, calls, _ = setup(studio, tools=[tool("lookup"), tool("other")])
    cid = new_chat(studio)
    changed = studio.c.patch(f"/api/chats/{cid}", json={"mcp_tools_off": {str(sid): ["other"]}})
    assert changed.status_code == 200 and changed.json()["mcp_tools_off"] == {str(sid): ["other"]}
    studio.gw.chat_script = [["Готово"]]
    sse(turn(studio, cid))
    assert offered(studio.gw.chat_requests[-1]) == ["Shared tools: lookup"]
    model = studio.gw.builtin

    async def switching(request):                      # инструмент выключают после discover, до вызова
        state.DB.execute("UPDATE chats SET mcp_tools_off = ? WHERE id = ?", (json.dumps({str(sid): ["lookup"]}), cid))
        return await model(request)

    studio.gw.on["/v1/chat/completions"] = switching
    calls.clear()
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid))]}, ["Готово"]]
    assert sse(turn(studio, cid, "Use tool"))[-1] == ("done", {"status": "done"})
    assert "tools/call" not in methods(calls)
    assert messages(studio, cid)[-1]["tools"] == [{"kind": "mcp", "server": "Shared tools", "tool": "lookup",
                                                   "error": mcp_client.TOOL_OFF}]


def test_patch_validates_and_resets_web_consent(studio):
    sid, _, _ = setup(studio)
    cid = new_chat(studio)
    for body in ({"mcp_tools_off": {str(sid): ["bad name"]}}, {"mcp_tools_off": {"x": ["lookup"]}},
                 {"mcp_tools_off": {str(sid): ["t"] * 129}}, {"mcp_off": [sid, sid]},
                 {"mcp_tools_off": {str(n): ["t"] for n in range(1, 18)}}):
        assert studio.c.patch(f"/api/chats/{cid}", json=body).status_code == 422, body
    state.DB.execute("UPDATE chats SET web_consent = 1 WHERE id = ?", (cid,))
    assert studio.c.patch(f"/api/chats/{cid}", json={"mcp_off": []}).status_code == 200
    assert state.DB.execute("SELECT web_consent FROM chats WHERE id = ?", (cid,)).fetchone()[0] == 1   # без смены
    studio.c.patch(f"/api/chats/{cid}", json={"mcp_tools_off": {str(sid): ["lookup"]}})
    assert state.DB.execute("SELECT web_consent FROM chats WHERE id = ?", (cid,)).fetchone()[0] is None
    # выключенные и явно включённые инструменты заменяются независимо
    studio.c.patch(f"/api/chats/{cid}", json={"mcp_tools_on": {str(sid): ["danger"]}})
    shown = view(studio, cid)
    assert shown["mcp_tools_off"] == {str(sid): ["lookup"]} and shown["mcp_tools_on"] == {str(sid): ["danger"]}


def test_direct_dialog_without_409(studio):
    sid, calls, _ = setup(studio)
    cloud = add_connection(studio, name="cloud")
    cid = new_chat(studio, connection_id=cloud, model="local-gemma-abl")
    shown = view(studio, cid)
    assert shown["mcp_ids"] == [] and not shown["mcp_ok"]
    response = turn(studio, cid)
    assert response.status_code == 200 and "url" in response.json()
    assert calls == []
    assert studio.c.patch(f"/api/chats/{cid}", json={"mcp_ids": [sid]}).status_code == 409


def test_second_turn_without_tools_list(studio):
    sid, calls, _ = setup(studio)
    cid = new_chat(studio)
    studio.gw.chat_script = [["Раз"]]
    sse(turn(studio, cid))
    assert methods(calls) == ["initialize", "notifications/initialized", "tools/list", "close"]
    calls.clear()
    studio.gw.chat_script = [["Два"]]
    sse(turn(studio, cid))
    assert calls == []                                 # каталог из кэша, вызовов не было — знакомства нет
    assert offered(studio.gw.chat_requests[-1]) == ["Shared tools: lookup"]
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid))]}, ["Три"]]
    sse(turn(studio, cid, "Use tool"))
    assert methods(calls) == ["initialize", "notifications/initialized", "tools/call", "close"]


def test_cache_reset_by_edit_and_failure_not_cached(studio):
    sid, calls, _ = setup(studio, fail=["tools/list"])
    cid = new_chat(studio)
    studio.gw.chat_script = [["Раз"]]
    sse(turn(studio, cid))
    assert messages(studio, cid)[1]["tools"][0]["error"] and mcp_sources.CACHE == {}
    studio.gw.chat_script = [["Два"]]
    sse(turn(studio, cid))
    assert offered(studio.gw.chat_requests[-1]) == ["Shared tools: lookup"]
    signin(studio, "admin")
    assert studio.c.put(f"/api/admin/shared/mcp/{sid}", json={"name": "Renamed", "url": HOST + "/mcp"}).status_code == 200
    assert mcp_sources.CACHE == {}
    signin(studio, "member")
    calls.clear()
    studio.gw.chat_script = [["Три"]]
    sse(turn(studio, cid))
    assert "tools/list" in methods(calls)


def test_unknown_tool_reply_forgets_catalog(studio):
    sid, calls, _ = setup(studio, tools=[tool("gone")])
    cid = new_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid))]}, ["Готово"]]
    sse(turn(studio, cid, "Use tool"))
    assert "tools/call" in methods(calls) and messages(studio, cid)[1]["tools"][0]["error"]
    assert mcp_sources.CACHE == {}


# ---- каталог и подсказки --------------------------------------------------------------------------------

def test_catalog_with_descriptions_marks_and_prompts(studio, caplog):
    tools = [tool("lookup", title="Поиск"), tool("search", annotations={"openWorldHint": True}),
             tool("danger", _meta={"studio/confirm": True}), tool("closed")]
    sid, calls, _ = setup(studio, tools=tools, instructions=RULES, prompts=[{"name": "brief", "title": "Кратко"}])
    access.set_tool_rules(sid, {"closed": []})
    off = studio.c.post("/api/mcp", json={"name": "Off", "url": HOST + "/off", "enabled": False}).json()["id"]
    off_calls = server(studio, "/off")
    items = studio.c.get("/api/mcp/catalog").json()["items"]
    assert [i["id"] for i in items] == [off, sid]
    assert items[0] == {"id": off, "name": "Off", "shared": False, "state": "off", "instructions": None,
                        "tools": [], "prompts": []}
    shared = items[1]
    assert shared["state"] == "ok" and shared["shared"] and shared["instructions"] == RULES
    assert shared["tools"] == [
        {"name": "lookup", "title": "Поиск", "description": "desc lookup", "open_world": False, "confirm": False},
        {"name": "search", "title": "", "description": "desc search", "open_world": True, "confirm": False},
        {"name": "danger", "title": "", "description": "desc danger", "open_world": False, "confirm": True}]
    assert shared["prompts"] == [{"name": "brief", "title": "Кратко", "description": "", "arguments": []}]
    assert off_calls == [] and methods(calls).count("tools/list") == 1
    studio.c.get("/api/mcp/catalog?refresh=1")
    assert methods(calls).count("tools/list") == 1      # моложе REFRESH_MIN_SEC — из кэша
    studio.clock[0] += mcp_sources.REFRESH_MIN_SEC
    studio.c.get("/api/mcp/catalog?refresh=1")
    assert methods(calls).count("tools/list") == 2
    assert RULES not in caplog.text and "lookup" not in caplog.text


def test_catalog_names_failed_server(studio):
    _, _, _ = setup(studio, fail=["initialize"])
    item, = studio.c.get("/api/mcp/catalog").json()["items"]
    assert item["state"] == "error" and item["message"]


def test_catalog_survives_unexpected_server_error(studio, monkeypatch, caplog):
    sid, _, _ = setup(studio)
    signin(studio, "admin")
    other = add_shared(studio, "Other", "/other")
    server(studio, "/other")
    signin(studio, "member")

    async def broken(session, timeout, refresh=False):
        if session.sid == sid:
            raise KeyError("private-secret-detail")
        return await real(session, timeout, refresh=refresh)

    real = mcp_sources.catalog
    monkeypatch.setattr(mcp_sources, "catalog", broken)
    response = studio.c.get("/api/mcp/catalog")
    assert response.status_code == 200
    items = {i["id"]: i for i in response.json()["items"]}
    assert items[sid] == {"id": sid, "name": "Shared tools", "shared": True, "state": "error",
                          "message": mcp_client.CATALOG_FAILED, "instructions": None, "tools": [], "prompts": []}
    assert items[other]["state"] == "ok" and items[other]["tools"]
    assert "private-secret-detail" not in caplog.text and "private-secret-detail" not in response.text


def test_prompt_route(studio):
    sid, calls, _ = setup(studio, prompts=[{"name": "brief"}])
    response = studio.c.post(f"/api/mcp/{sid}/prompt", json={"name": "brief", "arguments": {"topic": "кино"}})
    assert response.status_code == 200 and response.json() == {"text": "Обзор: кино"}
    assert studio.c.post(f"/api/mcp/{sid}/prompt", json={"name": "bad name"}).status_code == 422
    access.set_rule("mcp", sid, [])
    assert studio.c.post(f"/api/mcp/{sid}/prompt", json={"name": "brief"}).status_code == 404


# ---- правила серверов в системном сообщении ----------------------------------------------------------------

def test_rules_framed_trimmed_and_only_from_participants(studio):
    sid, _, _ = setup(studio, instructions="x" * 5000)
    signin(studio, "admin")
    quiet = add_shared(studio, "Quiet", "/quiet")
    server(studio, "/quiet", tools=[tool("hidden")], instructions="quiet-rules")
    signin(studio, "member")
    cid = new_chat(studio)
    studio.c.patch(f"/api/chats/{cid}", json={"mcp_tools_off": {str(quiet): ["hidden"]}})
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid))]}, ["Готово"]]
    sse(turn(studio, cid, "Use tool"))
    for request in studio.gw.chat_requests[-2:]:        # во всех кругах хода
        system = request["messages"][0]["content"]
        assert chat_system.MCP_FRAME in system and '<mcp-rules server="Shared tools">' in system
        assert chat_system.CUT in system and "x" * mcp_sources.INSTRUCTIONS_MAX not in system
        assert "quiet-rules" not in system and "Quiet" not in system


# ---- §7a п. 5: каталог не ломает чат ---------------------------------------------------------------------

def test_unreachable_default_server_does_not_block_turn(studio, monkeypatch):
    monkeypatch.setattr(mcp_sources, "DEFAULT_WAIT_SEC", 0.2)
    sid, _, _ = setup(studio, hang=True)
    signin(studio, "admin")
    add_shared(studio, "Also down", "/down")
    server(studio, "/down", hang=True)
    signin(studio, "member")
    cid = new_chat(studio)
    studio.gw.chat_script = [["Готово"]]
    started = time.monotonic()
    events = sse(turn(studio, cid))
    assert time.monotonic() - started < 3                # общий срок, а не DISCOVER_SEC на сервер
    assert events[-1] == ("done", {"status": "done"})
    assert ("tool", {"kind": "mcp", "label": "Shared tools", "skipped": True}) in events
    assert [s["error"] for s in messages(studio, cid)[1]["tools"]] == ["MCP не ответил вовремя"] * 2
    assert offered(studio.gw.chat_requests[-1]) == [] and mcp_sources.CACHE == {}


def test_default_servers_capped(studio, monkeypatch):
    sid, _, _ = setup(studio)
    signin(studio, "admin")
    extra = [add_shared(studio, f"S{n}", f"/s{n}") for n in range(2)]
    for n in range(2):
        server(studio, f"/s{n}", tools=[tool(f"t{n}")])
    signin(studio, "member")
    monkeypatch.setattr(mcp_client, "MAX_SERVERS", 2)
    cid = new_chat(studio)
    assert view(studio, cid)["mcp_ids"] == [sid, extra[0]]
    studio.gw.chat_script = [["Готово"]]
    assert sse(turn(studio, cid))[-1] == ("done", {"status": "done"})
    assert offered(studio.gw.chat_requests[-1]) == ["Shared tools: lookup", "S0: t0"]
    # явно включённый — первым, лишний общий по умолчанию выпадает
    assert studio.c.patch(f"/api/chats/{cid}", json={"mcp_ids": [extra[1]]}).json()["mcp_ids"] == [extra[1], sid]


def test_tools_over_limit_trimmed_not_refused(studio, monkeypatch):
    sid, _, _ = setup(studio, tools=[tool("a"), tool("b")])
    server(studio, "/mine", tools=[tool("c"), tool("d")])
    mine = studio.c.post("/api/mcp", json={"name": "Mine", "url": HOST + "/mine"}).json()["id"]
    cid = new_chat(studio)
    studio.c.patch(f"/api/chats/{cid}", json={"mcp_ids": [mine]})
    monkeypatch.setattr(mcp_client, "MAX_TOOLS", 3)
    studio.gw.chat_script = [["Готово"]]
    events = sse(turn(studio, cid))
    assert events[-1] == ("done", {"status": "done"})
    assert offered(studio.gw.chat_requests[-1]) == ["Mine: c", "Mine: d", "Shared tools: a"]
    assert messages(studio, cid)[1]["tools"] == [{"kind": "mcp", "server": "Shared tools",
                                                 "error": mcp_client.TRIMMED}]


def test_server_over_tool_limit_trimmed_in_catalog_and_turn(studio, monkeypatch):
    """Сервер предлагает больше MAX_TOOLS — первые MAX_TOOLS, заметка в ходе и `trimmed` в каталоге (§7a п. 5)."""
    sid, _, _ = setup(studio, tools=[tool(n) for n in "abcd"])
    monkeypatch.setattr(mcp_client, "MAX_TOOLS", 3)
    item, = studio.c.get("/api/mcp/catalog").json()["items"]
    assert item["state"] == "ok" and item["trimmed"] and [t["name"] for t in item["tools"]] == ["a", "b", "c"]
    cid = new_chat(studio)
    studio.gw.chat_script = [["Готово"]]
    assert sse(turn(studio, cid))[-1] == ("done", {"status": "done"})
    assert offered(studio.gw.chat_requests[-1]) == ["Shared tools: a", "Shared tools: b", "Shared tools: c"]
    assert messages(studio, cid)[1]["tools"] == [{"kind": "mcp", "server": "Shared tools",
                                                 "error": mcp_client.TRIMMED}]


def test_failing_prompts_keep_the_server_in_chat_and_catalog(studio):
    sid, calls, _ = setup(studio, prompts=[{"name": "brief"}], fail=["prompts/list"] * 2)
    item, = studio.c.get("/api/mcp/catalog").json()["items"]
    assert item["state"] == "ok" and item["prompts"] == [] and [t["name"] for t in item["tools"]] == ["lookup"]
    mcp_sources.CACHE.clear()
    calls.clear()
    cid = new_chat(studio)
    studio.gw.chat_script = [["Готово"]]
    assert sse(turn(studio, cid))[-1] == ("done", {"status": "done"})
    assert "prompts/list" in methods(calls) and messages(studio, cid)[1]["tools"] == []
    assert offered(studio.gw.chat_requests[-1]) == ["Shared tools: lookup"]


# ---- §7a п. 2: «интернет» и «с подтверждением» -----------------------------------------------------------

def test_open_world_only_with_internet_and_rechecked_midturn(studio):
    sid, calls, _ = setup(studio, tools=[tool("lookup"), tool("search", annotations={"openWorldHint": True})])
    cid = new_chat(studio)
    studio.gw.chat_script = [["Готово"]]
    sse(turn(studio, cid))
    assert offered(studio.gw.chat_requests[-1]) == ["Shared tools: lookup"]
    state.DB.execute("UPDATE chats SET web = 1 WHERE id = ?", (cid,))
    model = studio.gw.builtin

    async def switching(request):                      # «Интернет» выключают посреди ответа
        state.DB.execute("UPDATE chats SET web = 0 WHERE id = ?", (cid,))
        return await model(request)

    studio.gw.on["/v1/chat/completions"] = switching
    calls.clear()
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid, 1))]}, ["Готово"]]
    assert sse(turn(studio, cid, "Search"))[-1] == ("done", {"status": "done"})
    assert offered(studio.gw.chat_requests[-2]) == ["Shared tools: lookup", "Shared tools: search"]
    assert calls == []                                 # ни знакомства, ни вызова
    assert messages(studio, cid)[-1]["tools"] == [{"kind": "mcp", "server": "Shared tools", "tool": "search",
                                                   "error": mcp_client.WEB_OFF}]


def test_confirm_tools_offered_and_paused_by_condition(studio):
    """Спека 2026-11-21-studio-source-capabilities-design, п. 5: инструмент «с подтверждением» предлагается модели
    всегда, а вызов, для которого выполнено условие, ждёт «Запустить» (tests/test_chat_pending.py)."""
    tools = [tool("lookup"), tool("translate", _meta={"studio/confirm": True}),
             tool("video", _meta={"studio/confirm": {"when": [{"arg": "seconds", "gt": 30}]}})]
    sid, calls, _ = setup(studio, tools=tools)
    cid = new_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid, 2), seconds=10)]}, ["Готово"]]
    events = sse(turn(studio, cid, "Short video"))
    assert offered(studio.gw.chat_requests[0]) == ["Shared tools: lookup", "Shared tools: translate", "Shared tools: video"]
    assert not any(name == "pending" for name, _ in events)
    assert ("tools/call", {"name": "video", "arguments": {"seconds": 10}}) in calls
    calls.clear()
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid, 2), seconds=60)]}, ["Готово"]]
    events = sse(turn(studio, cid, "Long video"))
    assert next(data for name, data in events if name == "pending")["tool"] == "video"
    assert "tools/call" not in methods(calls)
    assert messages(studio, cid)[-1]["pending"]["arguments"] == [["seconds", "60"]]


def test_admin_tool_policy_overrides_server_hints(studio):
    tools = [tool("lookup"), tool("search", annotations={"openWorldHint": True}),
             tool("danger", _meta={"studio/confirm": True}), tool("quiet")]
    sid, _, _ = setup(studio, tools=tools)
    path = f"/api/admin/shared/mcp/{sid}/policy"
    assert studio.c.get(path).status_code == 403
    signin(studio, "admin")
    assert studio.c.get(path).json() == {"tools": {}}
    policy = {"lookup": {"open_world": True}, "search": {"open_world": False}, "danger": {"confirm": "never"},
              "quiet": {"confirm": "always"}, "plain": {}}
    saved = studio.c.put(path, json={"tools": policy})
    assert saved.status_code == 200, saved.text
    assert saved.json() == {"tools": {"danger": {"open_world": None, "confirm": "never"},
                                      "lookup": {"open_world": True, "confirm": None},
                                      "quiet": {"open_world": None, "confirm": "always"},
                                      "search": {"open_world": False, "confirm": None}}}
    assert studio.c.put(path, json={"tools": {"bad name": {"open_world": True}}}).status_code == 422
    assert studio.c.put(path, json={"tools": {"x": {"confirm": "sometimes"}}}).status_code == 422
    personal = studio.c.post("/api/mcp", json={"name": "Mine", "url": HOST + "/mine"}).json()["id"]
    assert studio.c.get(f"/api/admin/shared/mcp/{personal}/policy").status_code == 404
    signin(studio, "member")
    cid = new_chat(studio)
    studio.gw.chat_script = [["Готово"]]
    sse(turn(studio, cid))
    assert offered(studio.gw.chat_requests[-1]) == ["Shared tools: search", "Shared tools: danger", "Shared tools: quiet"]
    marks = {t["name"]: (t["open_world"], t["confirm"])
             for t in studio.c.get("/api/mcp/catalog").json()["items"][0]["tools"]}
    assert marks == {"lookup": (True, False), "search": (False, False), "danger": (False, False),
                     "quiet": (False, True)}
