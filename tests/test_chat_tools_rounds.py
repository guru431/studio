"""Встроенные инструменты чата в каждом ходе через сервер (спека 2026-11-21-studio-source-capabilities-design, п. 17):
шлюз, который не принимает `tools`, получает один повтор без них, и пара «подключение + модель» на
`chat.NO_TOOLS_SEC` идёт без встроенных инструментов. Круги инструментов — по настройке администратора, повторный
вызов с теми же аргументами не исполняется (п. 6)."""
import json

import httpx

import chat
import chat_tools
import web
from conftest import add_gateway, signin
from test_chat import messages, new_chat, sse, turn
from test_chat_pending import answer, confirm_setup, pending_of
from test_chat_web import call
from test_mcp_dialog import methods, name_of, setup


def test_rounds_follow_admin_setting(studio):
    sid, calls, _ = setup(studio)
    signin(studio, "admin")
    assert studio.c.get("/api/admin/chat").json() == {"tool_rounds": chat.TOOL_ROUNDS}
    for wrong in (0, 31):
        assert studio.c.put("/api/admin/chat", json={"tool_rounds": wrong}).status_code == 422
    saved = studio.c.put("/api/admin/chat", json={"tool_rounds": 2})
    assert saved.status_code == 200 and saved.json() == {"tool_rounds": 2}
    assert chat.tool_rounds() == 2
    signin(studio, "member")
    assert studio.c.get("/api/admin/chat").status_code == 403
    cid = new_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="1")]}, {"tool_calls": [call(name_of(sid), q="2")]},
                             ["Готово"]]
    assert sse(turn(studio, cid, "Use tool"))[-1] == ("done", {"status": "done"})
    first, second, third = studio.gw.chat_requests
    assert "tools" in first and "tools" in second
    assert "tools" not in third and "tool_choice" not in third        # 3-й круг — без инструментов
    assert [p["arguments"] for m, p in calls if m == "tools/call"] == [{"q": "1"}, {"q": "2"}]


def test_duplicate_call_skipped(studio):
    sid, calls, _ = setup(studio)
    cid = new_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="1")]},
                             {"tool_calls": [call(name_of(sid), q="1"), call(name_of(sid), q="2")]}, ["Готово"]]
    assert sse(turn(studio, cid, "Use tool"))[-1] == ("done", {"status": "done"})
    assert [p["arguments"] for m, p in calls if m == "tools/call"] == [{"q": "1"}, {"q": "2"}]
    assert messages(studio, cid)[-1]["tools"] == [
        {"kind": "mcp", "server": "Shared tools", "tool": "lookup"}, {"kind": "duplicate", "tool": "lookup"},
        {"kind": "mcp", "server": "Shared tools", "tool": "lookup"}]
    sent = [m for m in studio.gw.chat_requests[-1]["messages"] if m["role"] == "tool"]
    assert [m["content"] for m in sent] == ["result-lookup", chat.DUPLICATE, "result-lookup"]


def test_duplicate_after_resumed_pause_skipped(studio):
    """Вызовы до паузы помнит payload: после «Запустить» их повтор не исполняется и не спрашивает снова."""
    sid, calls = confirm_setup(studio)
    cid = new_chat(studio)
    lookup, translate = call(name_of(sid), q="1"), call(name_of(sid, 1), phrase="hi")
    studio.gw.chat_script = [{"tool_calls": [lookup]}, {"tool_calls": [translate]},
                             {"tool_calls": [lookup, translate]}, ["Готово."]]
    shown = pending_of(sse(turn(studio, cid, "Переведи")))
    events = sse(answer(studio, cid, shown["id"], "run"))
    assert events[-1] == ("done", {"status": "done"}) and not any(name == "pending" for name, _ in events)
    assert [p["name"] for m, p in calls if m == "tools/call"] == ["lookup", "translate"]
    assert [s["kind"] for s in messages(studio, cid)[-1]["tools"]] == ["mcp", "mcp", "duplicate", "duplicate"]


def test_builtin_error_goes_to_model_without_internet_note(studio):
    """Ошибка встроенного инструмента — не про интернет: без приписки web.NOTHING."""
    add_gateway(studio)
    cid = new_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call("ask_user", question="")]}, ["Ладно."]]
    assert sse(turn(studio, cid, "Спроси"))[-1] == ("done", {"status": "done"})
    sent = studio.gw.chat_requests[-1]["messages"][-1]
    assert sent["role"] == "tool" and sent["content"] == f"Ошибка инструмента: {chat_tools.BAD_ARGS}"
    assert web.NOTHING not in sent["content"]


def test_gateway_refusing_tools_retried_without_them(studio):
    conn = add_gateway(studio)
    cid = new_chat(studio)
    model, sent = studio.gw.builtin, []

    async def picky(request):
        body = json.loads(request.content)
        sent.append("tools" in body)
        if "tools" in body:
            return httpx.Response(400, json={"error": {"message": "tool_choice auto is not supported by this model"}})
        return await model(request)

    studio.gw.on["/v1/chat/completions"] = picky
    assert sse(turn(studio, cid))[-1] == ("done", {"status": "done"})
    assert sent == [True, False]
    assert chat.NO_TOOLS[(conn, "local-gemma-abl")] == studio.clock[0]
    assert sse(turn(studio, cid, "Ещё"))[-1] == ("done", {"status": "done"})
    assert sent == [True, False, False]                  # метка: сразу без инструментов
    studio.clock[0] += chat.NO_TOOLS_SEC
    sse(turn(studio, cid, "Снова"))
    assert sent[3:] == [True, False]

