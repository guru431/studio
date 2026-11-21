"""Пауза и продолжение хода чата (спека 2026-11-21-studio-source-capabilities-design, п. 5 и 17): инструмент MCP с
подтверждением запускается только после «Запустить», отказ уходит модели; встроенный `ask_user` спрашивает кнопками;
ответ продолжает то же сообщение; второй и поздний ответ — 409, новый ход снимает паузу."""
import json
import logging

import access
import chat
import chat_pending
import mcp_sources
import state
from conftest import add_gateway
from test_chat import messages, new_chat, sse, turn
from test_chat_ocr import on_loop
from test_chat_web import call
from test_mcp_dialog import methods, name_of, offered, setup, tool

REQUEST = "pending-request-0001"


def answer(studio, cid, pid, action, text=None, request_id=REQUEST):
    body = {"request_id": request_id, "action": action} | ({"answer": text} if text is not None else {})
    return studio.c.post(f"/api/chats/{cid}/pending/{pid}", json=body)


def pending_of(events) -> dict:
    return next(data for name, data in events if name == "pending")


def confirm_setup(studio, **extra):
    tools = [tool("lookup"), tool("translate", _meta={"studio/confirm": True}), *extra.pop("more", [])]
    sid, calls, _ = setup(studio, tools=tools, **extra)
    return sid, calls


def pause_on(studio, cid, name, text="Переведи", before="Сейчас переведу.", **args):
    studio.gw.chat_script = [{"text": [before], "tool_calls": [call(name, **args)]}, ["Готово."]]
    events = sse(turn(studio, cid, text))
    assert events[-1] == ("done", {"status": "done"})
    return pending_of(events)


def test_confirm_tool_pauses_and_refuse_never_calls_mcp(studio, caplog):
    caplog.set_level(logging.INFO)
    sid, calls = confirm_setup(studio)
    cid = new_chat(studio)
    shown = pause_on(studio, cid, name_of(sid, 1), text="секретная просьба", phrase="секретный текст")
    assert offered(studio.gw.chat_requests[0]) == ["Shared tools: lookup", "Shared tools: translate"]
    assert shown["kind"] == "confirm" and shown["server"] == "Shared tools" and shown["tool"] == "translate"
    assert shown["arguments"] == [["phrase", "секретный текст"]]
    reply = messages(studio, cid)[-1]
    assert (reply["status"], reply["content"]) == ("done", "Сейчас переведу.")
    assert reply["pending"]["id"] == shown["id"] and reply["pending"]["tool"] == "translate"
    assert "tools/call" not in methods(calls)
    row = state.DB.execute("SELECT payload FROM chat_pending WHERE id = ?", (shown["id"],)).fetchone()
    assert "секретный" not in row[0]                     # payload — шифром

    events = sse(answer(studio, cid, shown["id"], "refuse"))
    assert events[0] == ("turn", {"user_message_id": messages(studio, cid)[0]["id"], "assistant_message_id": reply["id"]})
    assert events[-1] == ("done", {"status": "done"})
    assert "tools/call" not in methods(calls)
    sent = studio.gw.chat_requests[-1]["messages"][-1]
    assert sent["role"] == "tool" and sent["content"] == chat_pending.REFUSED
    after = messages(studio, cid)
    assert len(after) == 2 and after[-1]["id"] == reply["id"] and "pending" not in after[-1]
    assert after[-1]["content"] == "Сейчас переведу.\n\nГотово."
    outcome = state.DB.execute("SELECT outcome FROM chat_pending WHERE id = ?", (shown["id"],)).fetchone()[0]
    assert outcome == "refuse"
    for secret in ("секретный текст", "секретная просьба", "phrase"):
        assert secret not in caplog.text


def test_run_calls_once_and_answer_continues_same_message(studio):
    sid, calls = confirm_setup(studio)
    cid = new_chat(studio)
    shown = pause_on(studio, cid, name_of(sid, 1), phrase="hello")
    mid = messages(studio, cid)[-1]["id"]
    events = sse(answer(studio, cid, shown["id"], "run"))
    assert events[-1] == ("done", {"status": "done"})
    assert [p for m, p in calls if m == "tools/call"] == [{"name": "translate", "arguments": {"phrase": "hello"}}]
    after = messages(studio, cid)
    assert [m["id"] for m in after][-1] == mid and len(after) == 2
    assert after[-1]["status"] == "done" and after[-1]["content"] == "Сейчас переведу.\n\nГотово."
    assert after[-1]["tools"] == [{"kind": "mcp", "server": "Shared tools", "tool": "translate"}]
    assert studio.gw.chat_requests[-1]["messages"][-1]["content"] == "result-translate"


def test_second_answer_and_late_answer_get_pending_gone(studio):
    sid, calls = confirm_setup(studio)
    cid = new_chat(studio)
    shown = pause_on(studio, cid, name_of(sid, 1))
    assert sse(answer(studio, cid, shown["id"], "refuse"))[-1] == ("done", {"status": "done"})
    again = answer(studio, cid, shown["id"], "run", request_id="pending-request-0002")
    assert again.status_code == 409 and again.json()["detail"]["code"] == "pending_gone"
    assert answer(studio, cid, shown["id"], "answer", "x").status_code == 422   # ответ текстом — только вопросу

    late = pause_on(studio, cid, name_of(sid, 1), text="Ещё раз")
    studio.clock[0] += chat_pending.PENDING_TTL_SEC + 1
    assert "pending" not in messages(studio, cid)[-1]
    gone = answer(studio, cid, late["id"], "run")
    assert gone.status_code == 409 and gone.json()["detail"]["code"] == "pending_gone"
    assert state.DB.execute("SELECT outcome FROM chat_pending WHERE id = ?", (late["id"],)).fetchone()[0] == "expired"
    assert "tools/call" not in methods(calls)


def test_new_message_supersedes_pending(studio):
    sid, calls = confirm_setup(studio)
    cid = new_chat(studio)
    shown = pause_on(studio, cid, name_of(sid, 1))
    studio.gw.chat_script = [["Хорошо, о другом."]]
    assert sse(turn(studio, cid, "Не надо, давай о другом"))[-1] == ("done", {"status": "done"})
    row = state.DB.execute("SELECT resolved, outcome FROM chat_pending WHERE id = ?", (shown["id"],)).fetchone()
    assert row["outcome"] == "superseded" and row["resolved"] is not None
    assert all("pending" not in m for m in messages(studio, cid))
    gone = answer(studio, cid, shown["id"], "run")
    assert gone.status_code == 409 and gone.json()["detail"]["code"] == "pending_gone"
    assert "tools/call" not in methods(calls)


def test_pending_survives_restart(studio):
    sid, calls = confirm_setup(studio)
    cid = new_chat(studio)
    shown = pause_on(studio, cid, name_of(sid, 1), phrase="hello")

    async def restart():                                 # остановка службы и старт на той же базе
        await chat.stop_all()
        chat.recover()
    on_loop(studio, restart)
    mcp_sources.CACHE.clear()
    reply = messages(studio, cid)[-1]
    assert reply["status"] == "done" and reply["pending"]["id"] == shown["id"]
    assert reply["pending"]["arguments"] == [["phrase", "hello"]]
    assert sse(answer(studio, cid, shown["id"], "run"))[-1] == ("done", {"status": "done"})
    assert [p["name"] for m, p in calls if m == "tools/call"] == ["translate"]


def test_web_off_or_access_removed_before_answer_refuses(studio):
    web_tool = tool("search", annotations={"openWorldHint": True}, _meta={"studio/confirm": True})
    sid, calls = confirm_setup(studio, more=[web_tool])
    cid = new_chat(studio)
    state.DB.execute("UPDATE chats SET web = 1 WHERE id = ?", (cid,))
    shown = pause_on(studio, cid, name_of(sid, 2), text="Найди")
    assert shown["tool"] == "search"
    state.DB.execute("UPDATE chats SET web = 0 WHERE id = ?", (cid,))
    assert sse(answer(studio, cid, shown["id"], "run"))[-1] == ("done", {"status": "done"})
    step = messages(studio, cid)[-1]["tools"][-1]
    assert step["tool"] == "search" and step["error"]

    shown = pause_on(studio, cid, name_of(sid, 1), text="Переведи")
    access.set_tool_rules(sid, {"translate": []})
    assert sse(answer(studio, cid, shown["id"], "run"))[-1] == ("done", {"status": "done"})
    step = messages(studio, cid)[-1]["tools"][-1]
    assert step["tool"] == "translate" and step["error"]
    assert "tools/call" not in methods(calls)


def test_resolved_pause_drops_payload(studio):
    """payload паузы (сообщения модели с картинками) после любого исхода не хранится: база не растёт."""
    sid, _ = confirm_setup(studio)
    cid = new_chat(studio)

    def payload(pid):
        return state.DB.execute("SELECT payload FROM chat_pending WHERE id = ?", (pid,)).fetchone()[0]

    shown = pause_on(studio, cid, name_of(sid, 1))
    assert payload(shown["id"])
    assert sse(answer(studio, cid, shown["id"], "run"))[-1] == ("done", {"status": "done"})
    assert payload(shown["id"]) == ""
    shown = pause_on(studio, cid, name_of(sid, 1), text="Ещё")
    studio.gw.chat_script = [["Хорошо, о другом."]]
    sse(turn(studio, cid, "О другом"))
    assert payload(shown["id"]) == ""                    # superseded
    shown = pause_on(studio, cid, name_of(sid, 1), text="Снова")
    studio.clock[0] += chat_pending.PENDING_TTL_SEC + 1
    assert answer(studio, cid, shown["id"], "refuse").status_code == 409
    assert payload(shown["id"]) == ""                    # expired


def test_confirm_upload_offered_only_when_turned_on(studio):
    """`artifact_upload` «с подтверждением» модели предлагается, только когда включён в диалоге явно — как в окне
    MCP (`chat-mcp.js`, mcp_files.CONFIRM)."""
    sid, _ = confirm_setup(studio, more=[tool("artifact_upload", _meta={"studio/confirm": True})])
    cid = new_chat(studio)
    studio.gw.chat_script = [["Готово"]]
    sse(turn(studio, cid))
    assert offered(studio.gw.chat_requests[-1]) == ["Shared tools: lookup", "Shared tools: translate"]
    on = studio.c.patch(f"/api/chats/{cid}", json={"mcp_tools_on": {str(sid): ["artifact_upload"]}})
    assert on.status_code == 200, on.text
    studio.gw.chat_script = [["Готово"]]
    sse(turn(studio, cid, "Ещё"))
    assert offered(studio.gw.chat_requests[-1]) == ["Shared tools: lookup", "Shared tools: translate",
                                                    "Shared tools: artifact_upload"]


def test_ask_user_option_and_own_text_reach_model(studio):
    add_gateway(studio)
    cid = new_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call("ask_user", question="Какой цвет?", options=["Красный", "Синий"])]},
                             ["Выбран красный."]]
    events = sse(turn(studio, cid, "Нарисуй"))
    assert "ask_user" in [t["function"]["name"] for t in studio.gw.chat_requests[0]["tools"]]
    shown = pending_of(events)
    assert (shown["kind"], shown["question"], shown["options"], shown["allow_text"]) == (
        "ask", "Какой цвет?", ["Красный", "Синий"], True)
    assert messages(studio, cid)[-1]["pending"]["question"] == "Какой цвет?"
    assert answer(studio, cid, shown["id"], "run").status_code == 422          # запуск — только подтверждению
    assert sse(answer(studio, cid, shown["id"], "answer", "Красный"))[-1] == ("done", {"status": "done"})
    sent = studio.gw.chat_requests[-1]["messages"][-1]
    assert sent["role"] == "tool" and json.loads(sent["content"]) == {"answer": "Красный"}
    assert messages(studio, cid)[-1]["content"] == "Выбран красный."

    studio.gw.chat_script = [{"tool_calls": [call("ask_user", question="Как вас зовут?")]}, ["Привет, Ася."]]
    shown = pending_of(sse(turn(studio, cid, "Поздоровайся")))
    assert shown["options"] == [] and shown["allow_text"] is True
    assert sse(answer(studio, cid, shown["id"], "answer", "Ася"))[-1] == ("done", {"status": "done"})
    assert json.loads(studio.gw.chat_requests[-1]["messages"][-1]["content"]) == {"answer": "Ася"}

    studio.gw.chat_script = [{"tool_calls": [call("ask_user", question="Да или нет?", options=["Да", "Нет"],
                                                  allow_text=False)]}, ["Ладно."]]
    shown = pending_of(sse(turn(studio, cid, "Спроси")))
    assert answer(studio, cid, shown["id"], "answer", "Может быть").status_code == 422   # только варианты
    assert sse(answer(studio, cid, shown["id"], "refuse"))[-1] == ("done", {"status": "done"})
    assert json.loads(studio.gw.chat_requests[-1]["messages"][-1]["content"]) == {
        "answer": None, "note": "пользователь не ответил"}
