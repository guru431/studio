"""Каталог MCP (`mcp_sources`): кэш по (сервер, отпечаток ключа, пользователь), правила и подсказки сервера,
пометки инструментов; знакомство `mcp_client.Session` хранит `instructions` и `capabilities`."""
import asyncio
import json

from fastapi import HTTPException
import httpx
import pytest

import access
import mcp_client
import mcp_sources
import state
from conftest import make_user
from test_access_groups import shared_tools
from test_mcp import URL, add

RULES = "private-rules: отвечай кратко"


@pytest.fixture(autouse=True)
def empty_cache():
    mcp_sources.CACHE.clear()
    yield
    mcp_sources.CACHE.clear()


def serve(studio, *, prompts=None, fail=None, instructions=RULES, get=None):
    """Мок MCP: `prompts` — страницы `prompts/list` (список списков; None — без возможности prompts);
    `fail` — список методов, которые один раз отвечают 500."""
    calls, fail = [], list(fail or [])

    async def handler(request):
        if request.method == "DELETE":
            return httpx.Response(204)
        payload = json.loads(request.content)
        method, params = payload["method"], payload.get("params", {})
        calls.append((method, params))
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method in fail:
            fail.remove(method)
            return httpx.Response(500)
        if method == "initialize":
            capabilities = {"tools": {}} if prompts is None else {"tools": {}, "prompts": {}}
            result = {"protocolVersion": "2025-06-18", "capabilities": capabilities, "instructions": instructions,
                      "serverInfo": {"name": "fake", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "lookup", "inputSchema": {"type": "object"}}]}
        elif method == "prompts/list":
            page = int(params.get("cursor", "0"))
            result = {"prompts": prompts[page]}
            if page + 1 < len(prompts):
                result["nextCursor"] = str(page + 1)
        elif method == "prompts/get":
            result = get
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result},
                              headers={"mcp-session-id": "private-session"})

    studio.gw.on["/mcp"] = handler
    return calls


def owner(sid):
    return state.DB.execute("SELECT user_id FROM mcp_servers WHERE id=?", (sid,)).fetchone()[0]


def fetch(studio, sid, uid, **kwargs):
    async def run():
        session = mcp_client.Session(mcp_client.row(uid, sid), uid=uid)
        try:
            return await mcp_sources.catalog(session, mcp_sources.LIST_SEC, **kwargs)
        finally:
            await session.close()
    return studio.c.portal.call(run)


def listed(calls):
    return sum(method == "tools/list" for method, _ in calls)


def test_catalog_cached_by_server_stamp_and_user_until_ttl(studio, caplog):
    sid = add(studio)
    uid = owner(sid)
    calls = serve(studio, prompts=[[{"name": "brief", "title": "Кратко"}]])
    first = fetch(studio, sid, uid)
    assert first.instructions == RULES and first.capabilities == {"tools": {}, "prompts": {}}
    assert [t["name"] for t in first.tools] == ["lookup"]
    assert first.prompts == [{"name": "brief", "title": "Кратко", "description": "", "arguments": []}]
    assert fetch(studio, sid, uid) is first and listed(calls) == 1
    key, = mcp_sources.CACHE
    assert key[0] == sid and key[2] == uid       # личный сервер — ключ по id учётки, без X-Studio-User
    studio.clock[0] += mcp_sources.CATALOG_TTL_SEC
    assert fetch(studio, sid, uid) is not first and listed(calls) == 2
    assert RULES not in caplog.text and "brief" not in caplog.text and "lookup" not in caplog.text


def test_refresh_respects_minimum_interval(studio):
    sid = add(studio)
    uid = owner(sid)
    calls = serve(studio)
    first = fetch(studio, sid, uid)
    assert fetch(studio, sid, uid, refresh=True) is first and listed(calls) == 1
    studio.clock[0] += mcp_sources.REFRESH_MIN_SEC
    assert fetch(studio, sid, uid, refresh=True) is not first and listed(calls) == 2


def test_failure_is_not_cached(studio):
    sid = add(studio)
    uid = owner(sid)
    calls = serve(studio, fail=["tools/list"])
    with pytest.raises(HTTPException):
        fetch(studio, sid, uid)
    assert mcp_sources.CACHE == {}
    assert [t["name"] for t in fetch(studio, sid, uid).tools] == ["lookup"]
    assert listed(calls) == 2


def test_forget_on_update_check_and_delete(studio):
    sid = add(studio)
    uid = owner(sid)
    serve(studio)
    for method, path, body in [("PUT", f"/api/mcp/{sid}", {"name": "Edited", "url": URL}),
                               ("POST", f"/api/mcp/{sid}/check", None), ("DELETE", f"/api/mcp/{sid}", None)]:
        fetch(studio, sid, uid)
        other = ("other", "stamp", 0)
        mcp_sources.CACHE[other] = mcp_sources.CACHE[next(iter(mcp_sources.CACHE))]
        assert studio.c.request(method, path, json=body).status_code in (200, 204)
        assert list(mcp_sources.CACHE) == [other]     # записи других серверов остаются
        mcp_sources.CACHE.clear()


def test_cache_keeps_bounded_number_of_entries(studio, monkeypatch):
    monkeypatch.setattr(mcp_sources, "CACHE_KEEP", 2)
    sid = add(studio)
    uid = owner(sid)
    serve(studio)
    mcp_sources.CACHE.update({("old", "a", 1): None, ("old", "b", 1): None})
    fetch(studio, sid, uid)
    assert len(mcp_sources.CACHE) == 2 and ("old", "a", 1) not in mcp_sources.CACHE


def test_shared_server_keyed_by_studio_user(studio):
    make_user(studio, "admin", is_admin=True)
    member = make_user(studio, "member")
    second = make_user(studio, "second")
    sid = shared_tools(studio)
    calls = serve(studio)
    fetch(studio, sid, member)
    fetch(studio, sid, member)
    fetch(studio, sid, second)
    assert {key[2] for key in mcp_sources.CACHE} == {access.studio_user(member, sid), access.studio_user(second, sid)}
    assert listed(calls) == 2


def test_prompts_paged_trimmed_and_limited(studio):
    sid = add(studio)
    uid = owner(sid)
    long = {"name": "long", "title": "T" * 500, "description": "D" * 500,
            "arguments": [{"name": "topic", "description": "тема", "required": True}, {"name": "bad name"}, "x"]}
    pages = [[long, {"name": "bad name"}, {"title": "без имени"}],
             [{"name": f"p{n}"} for n in range(40)]]
    calls = serve(studio, prompts=pages)
    prompts = fetch(studio, sid, uid).prompts
    assert len(prompts) == mcp_client.PROMPTS_MAX
    assert prompts[0] == {"name": "long", "title": "T" * mcp_client.PROMPT_TITLE_MAX,
                          "description": "D" * mcp_client.PROMPT_DESCRIPTION_MAX,
                          "arguments": [{"name": "topic", "description": "тема", "required": True}]}
    assert [p["name"] for p in prompts[1:3]] == ["p0", "p1"]
    assert [params for method, params in calls if method == "prompts/list"] == [{}, {"cursor": "1"}]


def test_prompts_not_requested_without_capability(studio):
    sid = add(studio)
    uid = owner(sid)
    calls = serve(studio, prompts=None, instructions=None)
    item = fetch(studio, sid, uid)
    assert item.prompts == [] and item.instructions is None
    assert "prompts/list" not in [method for method, _ in calls]


def test_prompts_refuse_repeated_cursor(studio):
    """Повтор курсора подсказок — отказ `prompts`, но каталог остаётся: инструменты есть, подсказок нет."""
    sid = add(studio)
    uid = owner(sid)
    calls = serve(studio, prompts=[[{"name": "a"}], [{"name": "b"}], [{"name": "c"}]])

    async def handler(request, inner=studio.gw.on["/mcp"]):
        response = await inner(request)
        payload = json.loads(request.content) if request.method == "POST" else {}
        if payload.get("method") == "prompts/list":
            data = response.json()
            data["result"]["nextCursor"] = "1"
            return httpx.Response(200, json=data)
        return response

    studio.gw.on["/mcp"] = handler
    with pytest.raises(HTTPException):
        studio.c.portal.call(lambda: mcp_client.Session(mcp_client.row(uid, sid), uid=uid).prompts())
    item = fetch(studio, sid, uid)
    assert [t["name"] for t in item.tools] == ["lookup"] and item.prompts == [] and calls


def test_failing_prompts_keep_the_catalog(studio):
    """`prompts/list` с ошибкой — каталог без подсказок, а не отказ всего сервера."""
    sid = add(studio)
    uid = owner(sid)
    serve(studio, prompts=[[{"name": "brief"}]], fail=["prompts/list"])
    item = fetch(studio, sid, uid)
    assert [t["name"] for t in item.tools] == ["lookup"] and item.prompts == []


def test_tools_over_limit_trimmed_with_paging_stopped(monkeypatch):
    """Больше MAX_TOOLS — первые MAX_TOOLS и признак `trimmed`, листание дальше не идёт; повтор курсора — отказ."""
    monkeypatch.setattr(mcp_client, "MAX_TOOLS", 3)
    pages = {None: ([{"name": "a"}, {"name": "b"}], "1"), "1": ([{"name": "c"}, {"name": "d"}], "2"),
             "2": ([{"name": "e"}], None)}

    def session_with(pages):
        session = mcp_client.Session({"id": 1, "user_id": 1, "url": URL, "secret_enc": "x"}, uid=1)
        session.version, seen = "2025-06-18", []

        async def rpc(method, params=None, **kwargs):
            cursor = (params or {}).get("cursor")
            seen.append(cursor)
            batch, after = pages[cursor]
            return {"tools": batch, **({"nextCursor": after} if after else {})}

        session.rpc = rpc
        return session, seen

    session, seen = session_with(pages)
    assert [t["name"] for t in asyncio.run(session.tools())] == ["a", "b", "c"]
    assert session.trimmed and seen == [None, "1"]
    session, _ = session_with({None: ([{"name": "a"}, {"name": "b"}, {"name": "c"}], None)})
    assert len(asyncio.run(session.tools())) == 3 and not session.trimmed
    session, _ = session_with({None: ([{"name": "a"}], "1"), "1": ([{"name": "b"}], "1")})
    with pytest.raises(HTTPException):
        asyncio.run(session.tools())


def test_prompt_returns_user_text_only(studio):
    sid = add(studio)
    uid = owner(sid)
    get = {"messages": [{"role": "user", "content": {"type": "text", "text": "Сделай обзор"}},
                        {"role": "assistant", "content": {"type": "text", "text": "assistant-text"}},
                        {"role": "user", "content": {"type": "image", "data": "x"}},
                        {"role": "user", "content": {"type": "text", "text": "x" * 30_000}}]}
    calls = serve(studio, prompts=[[{"name": "brief"}]], get=get)

    async def run(name, args):
        session = mcp_client.Session(mcp_client.row(uid, sid), uid=uid)
        try:
            return await session.prompt(name, args)
        finally:
            await session.close()

    text = studio.c.portal.call(run, "brief", {"topic": "кино"})
    assert text.startswith("Сделай обзор\n\nxxx") and len(text) == mcp_client.PROMPT_TEXT_MAX
    assert "assistant-text" not in text
    assert ("prompts/get", {"name": "brief", "arguments": {"topic": "кино"}}) in calls
    initialized = [method for method, _ in calls].count("initialize")
    for name, args in [("bad name", {}), ("brief", {"topic": 1}), ("brief", [])]:
        with pytest.raises(HTTPException) as refused:
            studio.c.portal.call(run, name, args)
        assert refused.value.status_code == 422
    assert [method for method, _ in calls].count("initialize") == initialized   # отказ — до запроса к серверу


def test_session_initializes_once():
    """`ensure` знакомит один раз; `tools` и `prompts` в одной сессии не повторяют initialize."""
    session = mcp_client.Session({"id": 1, "user_id": 1, "url": URL, "secret_enc": "x"}, uid=1)
    seen = []

    async def rpc(method, params=None, **kwargs):
        seen.append(method)
        if method == "initialize":
            return {"protocolVersion": "2025-06-18", "capabilities": "wrong", "instructions": 5}
        return {"tools": []} if method == "tools/list" else {"prompts": []} if method == "prompts/list" else {}

    session.rpc = rpc
    asyncio.run(session.tools())
    asyncio.run(session.prompts())
    assert seen == ["initialize", "notifications/initialized", "tools/list", "prompts/list"]
    assert session.capabilities == {} and session.instructions is None


@pytest.mark.parametrize("annotations, expected", [
    (None, False), ({}, False), ({"openWorldHint": False}, False), ({"openWorldHint": "true"}, False),
    ({"openWorldHint": 1}, False), ({"openWorldHint": True}, True)])
def test_open_world_only_explicit_true(annotations, expected):
    tool = {"name": "t"} if annotations is None else {"name": "t", "annotations": annotations}
    assert mcp_sources.hints(tool)["open_world"] is expected


def test_confirm_hint_and_conditions():
    def hint(value):
        return mcp_sources.hints({"name": "t", "_meta": {"studio/confirm": value}})

    assert hint(True)["confirm"] == {"message": None, "when": []}
    assert mcp_sources.confirm_needed(hint(True), {})
    for value in (False, "true", 1, None, []):
        assert hint(value)["confirm"] is None and not mcp_sources.confirm_needed(hint(value), {"seconds": 99})
    assert mcp_sources.hints({"name": "t"}) == {"open_world": False, "confirm": None, "chat_files": None}

    video = hint({"message": "  Долгий ролик  ", "when": [{"arg": "seconds", "gt": 30}]})
    assert video["confirm"] == {"message": "Долгий ролик", "when": [{"arg": "seconds", "gt": 30}]}
    assert mcp_sources.confirm_needed(video, {"seconds": 31})
    assert mcp_sources.confirm_needed(video, {"seconds": "45"})
    for args in ({"seconds": 30}, {"seconds": 5}, {}, {"seconds": True}, {"seconds": "много"}, None):
        assert not mcp_sources.confirm_needed(video, args)

    playlist = hint({"when": [{"arg": "playlist_items", "nonempty": True}]})
    assert playlist["confirm"]["message"] is None
    assert mcp_sources.confirm_needed(playlist, {"playlist_items": "1-5"})
    assert mcp_sources.confirm_needed(playlist, {"playlist_items": [1]})
    for args in ({"playlist_items": ""}, {"playlist_items": "  "}, {"playlist_items": []}, {"playlist_items": None}, {}):
        assert not mcp_sources.confirm_needed(playlist, args)

    both = hint({"when": [{"arg": "seconds", "gt": 30}, {"arg": "playlist_items", "nonempty": True}]})
    assert mcp_sources.confirm_needed(both, {"seconds": 5, "playlist_items": "2"})
    assert not mcp_sources.confirm_needed(both, {"seconds": 5})

    assert len(hint({"message": "m" * 500})["confirm"]["message"]) == mcp_sources.CONFIRM_MESSAGE_MAX
    # Неверное условие разметки не снимает подтверждение: оно становится безусловным.
    for when in ([{"arg": "seconds", "gt": "30"}], [{"arg": "seconds", "gt": 30}, {"arg": "x"}],
                 [{"arg": "seconds", "nonempty": False}], [{"arg": "s", "gt": 1, "nonempty": True}], "seconds",
                 [{"arg": "seconds", "gt": float("nan")}], [{"arg": "seconds", "gt": 1}] * 17):
        assert hint({"when": when})["confirm"]["when"] == []
        assert mcp_sources.confirm_needed(hint({"when": when}), {})


@pytest.mark.parametrize("meta, expected", [
    (None, None), ({}, None), ({"studio/chat-files": True}, None), ({"studio/chat-files": {}}, None),
    ({"studio/chat-files": {"max_bytes": 0}}, None), ({"studio/chat-files": {"max_bytes": "10"}}, None),
    ({"studio/chat-files": {"max_bytes": True}}, None), ({"studio/chat-files": {"max_bytes": 1.5}}, None),
    ({"studio/chat-files": {"max_bytes": 52_428_800}}, 52_428_800)])
def test_chat_files_limit(meta, expected):
    tool = {"name": "artifact_upload"} if meta is None else {"name": "artifact_upload", "_meta": meta}
    assert mcp_sources.hints(tool)["chat_files"] == expected
