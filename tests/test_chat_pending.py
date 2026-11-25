"""Пауза и продолжение хода чата (спека 2026-11-21-studio-source-capabilities-design, п. 5 и 17): инструмент MCP с
подтверждением запускается только после «Запустить», отказ уходит модели; встроенный `ask_user` спрашивает кнопками;
ответ продолжает то же сообщение; второй и поздний ответ — 409, новый ход снимает паузу."""
import json
import logging

import access
import chat
import chat_pending
import mcp_sources
import push
import security
import state
from conftest import add_gateway
from test_chat import attach, messages, new_chat, png, sse, turn
from test_chat_ocr import on_loop
from test_chat_web import call
from test_mcp_dialog import methods, name_of, offered, setup, tool

def answer(studio, cid, pid, action, text=None, request_id=None):
    """Ответ на паузу; без `request_id` — свой у каждой пары «пауза + действие» (квитанция `chat_turn_requests`)."""
    request_id = request_id or f"r-{pid}-{action}"
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
    shown = pause_on(studio, cid, name_of(sid, "translate"), text="секретная просьба", phrase="секретный текст")
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
    shown = pause_on(studio, cid, name_of(sid, "translate"), phrase="hello")
    mid = messages(studio, cid)[-1]["id"]
    events = sse(answer(studio, cid, shown["id"], "run"))
    assert events[-1] == ("done", {"status": "done"})
    assert [p for m, p in calls if m == "tools/call"] == [{"name": "translate", "arguments": {"phrase": "hello"}}]
    after = messages(studio, cid)
    assert [m["id"] for m in after][-1] == mid and len(after) == 2
    assert after[-1]["status"] == "done" and after[-1]["content"] == "Сейчас переведу.\n\nГотово."
    assert after[-1]["tools"] == [{"kind": "mcp", "server": "Shared tools", "tool": "translate"}]
    assert studio.gw.chat_requests[-1]["messages"][-1]["content"] == "result-translate"


def test_repeated_answer_replays_the_same_message(studio):
    """Ответ «Запустить» потерян в сети, страница повторяет тот же `request_id`: повтор — ответ повтора хода
    (`replayed`, то же сообщение), второго вызова MCP нет; другой ответ с тем же id — `request_conflict`, другой id
    после ответа — `pending_gone`; без `request_id` — 422."""
    sid, calls = confirm_setup(studio)
    cid = new_chat(studio)
    shown = pause_on(studio, cid, name_of(sid, "translate"), phrase="hello")
    first = messages(studio, cid)
    rid = "pending-retry-0001"
    assert studio.c.post(f"/api/chats/{cid}/pending/{shown['id']}", json={"action": "run"}).status_code == 422
    assert sse(answer(studio, cid, shown["id"], "run", request_id=rid))[-1] == ("done", {"status": "done"})
    again = answer(studio, cid, shown["id"], "run", request_id=rid)
    assert again.status_code == 200 and again.json() == {
        "replayed": True, "user_message_id": first[0]["id"], "assistant_message_id": first[-1]["id"]}
    assert [p["name"] for m, p in calls if m == "tools/call"] == ["translate"]
    assert len(messages(studio, cid)) == 2
    conflict = answer(studio, cid, shown["id"], "refuse", request_id=rid)
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "request_conflict"
    gone = answer(studio, cid, shown["id"], "run", request_id="pending-retry-0002")
    assert gone.status_code == 409 and gone.json()["detail"]["code"] == "pending_gone"
    assert [p["name"] for m, p in calls if m == "tools/call"] == ["translate"]


def test_second_answer_and_late_answer_get_pending_gone(studio):
    sid, calls = confirm_setup(studio)
    cid = new_chat(studio)
    shown = pause_on(studio, cid, name_of(sid, "translate"))
    assert sse(answer(studio, cid, shown["id"], "refuse"))[-1] == ("done", {"status": "done"})
    again = answer(studio, cid, shown["id"], "run", request_id="pending-request-0002")
    assert again.status_code == 409 and again.json()["detail"]["code"] == "pending_gone"
    assert answer(studio, cid, shown["id"], "answer", "x").status_code == 422   # ответ текстом — только вопросу

    late = pause_on(studio, cid, name_of(sid, "translate"), text="Ещё раз")
    studio.clock[0] += chat_pending.PENDING_TTL_SEC + 1
    assert "pending" not in messages(studio, cid)[-1]
    gone = answer(studio, cid, late["id"], "run")
    assert gone.status_code == 409 and gone.json()["detail"]["code"] == "pending_gone"
    assert state.DB.execute("SELECT outcome FROM chat_pending WHERE id = ?", (late["id"],)).fetchone()[0] == "expired"
    assert "tools/call" not in methods(calls)


def test_pause_is_not_a_completed_job_and_deleted_chat_cannot_resume(studio, monkeypatch):
    sid, calls = confirm_setup(studio)
    cid = new_chat(studio)
    notified = []
    monkeypatch.setattr(push, "chat_answered", lambda *args: notified.append(args))
    shown = pause_on(studio, cid, name_of(sid, "translate"))
    mid = messages(studio, cid)[-1]["id"]
    assert mid not in chat.ANSWERED and notified == []

    async def delete_while_waiting_for_limit(conn, model):
        state.DB.execute("UPDATE chats SET deleted = ? WHERE id = ?", (studio.clock[0], cid))
        return chat.CONTEXT.get(model)

    with monkeypatch.context() as patch:
        patch.setattr(chat, "_limit", delete_while_waiting_for_limit)
        assert answer(studio, cid, shown["id"], "run").status_code == 404
    row = state.DB.execute("SELECT resolved FROM chat_pending WHERE id = ?", (shown["id"],)).fetchone()
    assert row["resolved"] is None and "tools/call" not in methods(calls)

    state.DB.execute("UPDATE chats SET deleted = NULL WHERE id = ?", (cid,))
    assert sse(answer(studio, cid, shown["id"], "run"))[-1] == ("done", {"status": "done"})
    assert mid in chat.ANSWERED and len(notified) == 1 and notified[0][2:] == (mid, True)


def test_new_message_supersedes_pending(studio):
    sid, calls = confirm_setup(studio)
    cid = new_chat(studio)
    shown = pause_on(studio, cid, name_of(sid, "translate"))
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
    shown = pause_on(studio, cid, name_of(sid, "translate"), phrase="hello")

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
    shown = pause_on(studio, cid, name_of(sid, "search"), text="Найди")
    assert shown["tool"] == "search"
    state.DB.execute("UPDATE chats SET web = 0 WHERE id = ?", (cid,))
    assert sse(answer(studio, cid, shown["id"], "run"))[-1] == ("done", {"status": "done"})
    step = messages(studio, cid)[-1]["tools"][-1]
    assert step["tool"] == "search" and step["error"]

    shown = pause_on(studio, cid, name_of(sid, "translate"), text="Переведи")
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

    shown = pause_on(studio, cid, name_of(sid, "translate"))
    assert payload(shown["id"])
    assert sse(answer(studio, cid, shown["id"], "run"))[-1] == ("done", {"status": "done"})
    assert payload(shown["id"]) == ""
    shown = pause_on(studio, cid, name_of(sid, "translate"), text="Ещё")
    studio.gw.chat_script = [["Хорошо, о другом."]]
    sse(turn(studio, cid, "О другом"))
    assert payload(shown["id"]) == ""                    # superseded
    shown = pause_on(studio, cid, name_of(sid, "translate"), text="Снова")
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
    assert sent["role"] == "tool" and json.loads(sent["content"]) == {"question": "Какой цвет?", "answer": "Красный"}
    assert messages(studio, cid)[-1]["content"] == "Выбран красный."

    studio.gw.chat_script = [{"tool_calls": [call("ask_user", question="Как вас зовут?")]}, ["Привет, Ася."]]
    shown = pending_of(sse(turn(studio, cid, "Поздоровайся")))
    assert shown["options"] == [] and shown["allow_text"] is True
    assert sse(answer(studio, cid, shown["id"], "answer", "Ася"))[-1] == ("done", {"status": "done"})
    assert json.loads(studio.gw.chat_requests[-1]["messages"][-1]["content"]) == {
        "question": "Как вас зовут?", "answer": "Ася"}

    studio.gw.chat_script = [{"tool_calls": [call("ask_user", question="Да или нет?", options=["Да", "Нет"],
                                                  allow_text=False)]}, ["Ладно."]]
    shown = pending_of(sse(turn(studio, cid, "Спроси")))
    assert answer(studio, cid, shown["id"], "answer", "Может быть").status_code == 422   # только варианты
    assert sse(answer(studio, cid, shown["id"], "refuse"))[-1] == ("done", {"status": "done"})
    assert json.loads(studio.gw.chat_requests[-1]["messages"][-1]["content"]) == {
        "question": "Да или нет?", "answer": None, "note": "пользователь не ответил"}


def test_pause_of_an_unchosen_variant_waits_until_it_is_chosen_again(studio):
    """Ревью R1-8: вариант A1 встал на паузу, выбран A0 — ответ на карточку A1 (вторая вкладка) — 409 `pending_gone`,
    скрытый вариант не продолжается; A1 снова выбран — пауза ждёт и продолжает его."""
    add_gateway(studio)
    cid = new_chat(studio)
    studio.gw.chat_reply = ["A0"]
    sse(turn(studio, cid, "Привет"))
    studio.gw.chat_script = [{"tool_calls": [call("ask_user", question="Какой цвет?", options=["Красный"])]},
                             ["Выбран красный."]]
    shown = pending_of(sse(turn(studio, cid, "", regenerate=True)))
    a0, a1 = messages(studio, cid)[1]["variants"]
    assert studio.c.post(f"/api/chats/{cid}/messages/{a0['id']}/choose").status_code == 204
    r = answer(studio, cid, shown["id"], "answer", "Красный")
    assert (r.status_code, r.json()["detail"]["code"]) == (409, "pending_gone")
    msgs = messages(studio, cid)
    assert msgs[1]["content"] == "A0" and msgs[1]["variants"][1]["content"] == ""
    assert studio.c.post(f"/api/chats/{cid}/messages/{a1['id']}/choose").status_code == 204
    assert sse(answer(studio, cid, shown["id"], "answer", "Красный"))[-1] == ("done", {"status": "done"})
    assert messages(studio, cid)[1]["content"] == "Выбран красный."


def test_superseded_or_expired_pause_keeps_the_question(studio):
    """Ревью A-I1: шаг паузы пишется сразу; новое сообщение или срок снимают паузу, а вопрос и инструмент остаются
    шагом — странице и модели следующего хода; ответ на карточке модель получает вместе с вопросом."""
    sid, calls = confirm_setup(studio)
    cid = new_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call("ask_user", question="Какой цвет?", options=["Красный", "Синий"])]}]
    shown = pending_of(sse(turn(studio, cid, "Нарисуй кота")))
    paused = messages(studio, cid)[-1]
    assert paused["content"] == "" and paused["pending"]["id"] == shown["id"]
    assert [s["question"] for s in paused["tools"]] == ["Какой цвет?"]
    studio.gw.chat_script = [["Рисую красного кота."]]
    assert sse(turn(studio, cid, "Красный"))[-1] == ("done", {"status": "done"})
    asked = studio.gw.chat_requests[-1]["messages"][-1]
    assert asked["role"] == "user" and "Какой цвет?" in asked["content"] and "Красный" in asked["content"]
    assert chat_pending.UNANSWERED in asked["content"]
    assert messages(studio, cid)[1]["tools"] == [{"kind": "ask", "question": "Какой цвет?"}]

    shown = pause_on(studio, cid, name_of(sid, "translate"), text="Переведи", phrase="hello")
    studio.gw.chat_script = [["Хорошо."]]
    assert sse(turn(studio, cid, "да, запусти"))[-1] == ("done", {"status": "done"})
    sent = studio.gw.chat_requests[-1]["messages"]       # шаг — перед текстом до паузы «Сейчас переведу.»
    assert [m["role"] for m in sent[-3:]] == ["user", "assistant", "user"] and sent[-1]["content"] == "да, запусти"
    assert chat_pending.NOT_RUN.format("translate", '{"phrase": "hello"}') in sent[-3]["content"]
    assert messages(studio, cid)[5]["tools"] == [
        {"kind": "mcp", "server": "Shared tools", "tool": "translate", "refused": True}]

    late = pause_on(studio, cid, name_of(sid, "translate"), text="Ещё", phrase="late")
    assert messages(studio, cid)[-1]["tools"][-1]["waiting"] is True
    studio.clock[0] += chat_pending.PENDING_TTL_SEC + 1
    reply = messages(studio, cid)[-1]
    assert "pending" not in reply and reply["tools"][-1] == {
        "kind": "mcp", "server": "Shared tools", "tool": "translate", "refused": True}
    assert state.DB.execute("SELECT outcome FROM chat_pending WHERE id = ?", (late["id"],)).fetchone()[0] == "expired"
    assert "tools/call" not in methods(calls)

    studio.gw.chat_script = [{"tool_calls": [call("ask_user", question="Какой размер?")]}, ["Ладно."]]
    shown = pending_of(sse(turn(studio, cid, "Ещё кота")))
    assert sse(answer(studio, cid, shown["id"], "answer", "Большой"))[-1] == ("done", {"status": "done"})
    assert json.loads(studio.gw.chat_requests[-1]["messages"][-1]["content"]) == {
        "question": "Какой размер?", "answer": "Большой"}
    assert messages(studio, cid)[-1]["tools"] == [{"kind": "ask", "question": "Какой размер?"}]


def test_pause_keeps_photos_by_media_id_and_reads_the_card_alone(studio):
    """Ревью A-I3: payload паузы хранит картинки ссылкой `media_id` (не base64 — десятки мегабайт на строку),
    продолжение читает файлы заново; выдача сообщений читает только вид карточки, не расшифровывая payload; паузу
    старше срока выдача снимает и стирает."""
    add_gateway(studio)
    cid = new_chat(studio)
    pics = [attach(studio, f"photo{i}.png", png(color), "image/png") for i, color in enumerate(("red", "blue"))]
    studio.gw.chat_script = [{"tool_calls": [call("ask_user", question="В каком регионе?")]}, ["Это фикус."]]
    shown = pending_of(sse(turn(studio, cid, "Что за растение?", attachment_ids=[p["id"] for p in pics])))
    first = studio.gw.chat_requests[0]["messages"][-1]["content"]
    assert [p["image_url"]["url"][:22] for p in first if p["type"] == "image_url"] == ["data:image/png;base64,"] * 2
    row = state.DB.execute("SELECT payload FROM chat_pending WHERE id = ?", (shown["id"],)).fetchone()
    stored = security.decrypt(row["payload"].encode("ascii"))
    assert "base64" not in stored and all(p["media_id"] in stored for p in pics)

    sealed = row["payload"]
    state.DB.execute("UPDATE chat_pending SET payload = 'broken' WHERE id = ?", (shown["id"],))
    assert messages(studio, cid)[-1]["pending"]["question"] == "В каком регионе?"   # payload не читался
    state.DB.execute("UPDATE chat_pending SET payload = ? WHERE id = ?", (sealed, shown["id"]))
    assert sse(answer(studio, cid, shown["id"], "answer", "Тропики"))[-1] == ("done", {"status": "done"})
    resumed = studio.gw.chat_requests[-1]["messages"]
    assert resumed[-1]["role"] == "tool"
    assert [p["image_url"]["url"] for p in resumed[-3]["content"] if p["type"] == "image_url"] == [
        p["image_url"]["url"] for p in first if p["type"] == "image_url"]

    studio.gw.chat_script = [{"tool_calls": [call("ask_user", question="Ещё?")]}]
    late = pending_of(sse(turn(studio, cid, "Ещё")))
    studio.clock[0] += chat_pending.PENDING_TTL_SEC + 1
    assert "pending" not in messages(studio, cid)[-1]
    row = state.DB.execute("SELECT outcome, payload, view FROM chat_pending WHERE id = ?", (late["id"],)).fetchone()
    assert (row["outcome"], row["payload"], row["view"]) == ("expired", "", "")


def test_confirmation_covers_only_the_resumed_call_not_a_reused_id(studio):
    """Ревью A-I2: «Запустить» относится к вызову, на котором стояла пауза, по месту, а не по id: шлюз с id по номеру
    в ответе (`call_0` в каждом круге) или модель, повторившая id, не запускают следующий вызов без карточки."""
    sid, calls = confirm_setup(studio)
    cid = new_chat(studio)
    translate = name_of(sid, "translate")
    studio.gw.chat_script = [{"tool_calls": [(*call(translate, phrase="one"), "call_0")]},
                             {"tool_calls": [(*call(translate, phrase="two"), "call_0")]}, ["Готово."]]
    shown = pending_of(sse(turn(studio, cid, "Переведи")))
    events = sse(answer(studio, cid, shown["id"], "run"))
    assert [p for m, p in calls if m == "tools/call"] == [{"name": "translate", "arguments": {"phrase": "one"}}]
    again = pending_of(events)
    assert again["id"] != shown["id"] and again["arguments"] == [["phrase", "two"]]


def test_gone_tool_does_not_mark_a_later_call_with_the_same_id(studio):
    """Ревью A-I2: инструмент, пропавший к продолжению, — шаг GONE только у вызовов продолжения; вызов следующего
    круга с тем же id исполняется."""
    sid, calls = confirm_setup(studio)
    cid = new_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [(*call(name_of(sid, "translate"), phrase="one"), "call_0")]},
                             {"tool_calls": [(*call(name_of(sid, "lookup"), q="x"), "call_0")]}, ["Готово."]]
    shown = pending_of(sse(turn(studio, cid, "Переведи")))
    access.set_tool_rules(sid, {"translate": []})
    assert sse(answer(studio, cid, shown["id"], "run"))[-1] == ("done", {"status": "done"})
    steps = messages(studio, cid)[-1]["tools"]
    assert steps[0]["tool"] == "translate" and steps[0]["error"] == chat_pending.GONE
    assert steps[1] == {"kind": "mcp", "server": "Shared tools", "tool": "lookup"}
    assert [p["name"] for m, p in calls if m == "tools/call"] == ["lookup"]
