"""Кнопка «Рассуждение» диалога (`chats.thinking`) и рассуждение модели в потоке ответа (спека
2026-10-22-studio-chat-tools-badge-reasoning, раздел 4)."""
import asyncio
import json
import logging

import pytest

import chat
import state
from conftest import add_connection, add_gateway
from test_chat import events_of, messages, new_chat, sse, turn
from test_chat_web import EXA_KEY, call, chat_of, patch


def test_switch_is_stored_only_for_a_local_model(studio):
    gw = add_gateway(studio); cid = new_chat(studio)                      # local-gemma-abl
    assert (chat_of(studio, cid)["thinking"], chat_of(studio, cid)["thinking_ok"]) == (False, True)
    assert patch(studio, cid, thinking=True)["thinking"] is True
    assert patch(studio, cid, thinking=None)["thinking"] is True
    studio.c.put(f"/api/sections/chat/links/{gw}", json={"models": ["local-gemma-abl", "gemma4-abl"]})
    view = patch(studio, cid, model="gemma4-abl")                          # облачная роль: хранится, не действует
    assert (view["thinking"], view["thinking_ok"]) == (True, False)
    other = new_chat(studio, connection_id=gw, model="gemma4-abl")
    r = studio.c.patch(f"/api/chats/{other}", json={"thinking": True, "title": "x"})
    assert r.status_code == 409 and r.json()["detail"] == {"code": "no_thinking",
                                                           "message": "эта модель не умеет рассуждать по кнопке"}
    assert chat_of(studio, other)["title"] is None
    assert patch(studio, other, thinking=False)["thinking"] is False      # выключить можно всегда


def test_request_carries_enable_thinking_only_for_a_local_model(studio):
    gw = add_gateway(studio); cid = new_chat(studio)
    sse(turn(studio, cid)); patch(studio, cid, thinking=True); sse(turn(studio, cid, "ещё"))
    assert [r["chat_template_kwargs"] for r in studio.gw.chat_requests] == [
        {"enable_thinking": False}, {"enable_thinking": True}]
    studio.c.put(f"/api/sections/chat/links/{gw}", json={"models": ["local-gemma-abl", "gemma4-abl"]})
    patch(studio, cid, model="gemma4-abl"); sse(turn(studio, cid, "и ещё"))
    assert "chat_template_kwargs" not in studio.gw.chat_requests[-1]
    direct = new_chat(studio, connection_id=add_connection(studio))       # прямое: тело собирает браузер
    assert "chat_template_kwargs" not in turn(studio, direct).json()


# ---- рассуждение в потоке ----------------------------------------------------------------------

@pytest.fixture
def no_pings(monkeypatch):
    """Пинги ответа двигают подменённые часы на PING_SEC каждую миллисекунду: секунды рассуждения и «не чаще
    раза в секунду» сверяются без них."""
    async def idle(self):
        await asyncio.Event().wait()
    monkeypatch.setattr(chat.StreamTask, "_pings", idle)


def thinking_chat(s, on=True) -> str:
    add_gateway(s); cid = new_chat(s)
    patch(s, cid, thinking=on)
    return cid


def answer_of(s, cid) -> dict:
    return messages(s, cid)[-1]


def thinks(events) -> list:
    return [d for e, d in events if e == "think"]


def test_reasoning_is_shown_and_stored_when_the_switch_is_on(studio):
    cid = thinking_chat(studio)
    studio.gw.chat_script = [{"reasoning": ["Считаю ", "17×23."], "text": ["410"]}]
    events = [(e, d) for e, d in sse(turn(studio, cid, "Сколько будет 17×23?")) if e != "ping"]
    assert [e for e, _ in events] == ["turn", "think", "think", "delta", "done"]
    assert thinks(events) == [{"text": "Считаю "}, {"text": "17×23."}]
    answer = answer_of(studio, cid)
    assert (answer["status"], answer["content"], answer["reasoning"]) == ("done", "410", "Считаю 17×23.")
    assert isinstance(answer["reasoning_sec"], float)


def test_reasoning_field_named_reasoning_is_read_too(studio):
    cid = thinking_chat(studio)
    studio.gw.reasoning_field = "reasoning"
    studio.gw.chat_script = [{"reasoning": ["Считаю ", "17×23."], "text": ["410"]}]
    assert thinks(sse(turn(studio, cid))) == [{"text": "Считаю "}, {"text": "17×23."}]
    answer = answer_of(studio, cid)
    assert (answer["content"], answer["reasoning"]) == ("410", "Считаю 17×23.")


def test_seconds_run_from_the_first_thought_to_the_first_word(studio, no_pings):
    """Длительность — от первого куска рассуждения до первого куска ответа, по часам студии; конец потока несёт
    те же секунды, что записаны: страница пишет их, а не свои часы."""
    cid = thinking_chat(studio)
    studio.gw.chat_script = [{"reasoning": ["Считаю"], "text": ["410"]}]
    studio.gw.think_hold = True
    try:
        with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": "Привет"}) as r:
            events = events_of(r.iter_lines())
            assert next(d for e, d in events if e == "think") == {"text": "Считаю"}
            studio.clock[0] += 7
            studio.gw.think_hold = False
            assert [(e, d) for e, d in events if e != "ping"][-1] == ("done", {"status": "done", "reasoning_sec": 7.0})
    finally:
        studio.gw.think_hold = False
    assert answer_of(studio, cid)["reasoning_sec"] == 7.0


def test_switch_off_shows_only_thinking_and_keeps_nothing(studio, no_pings):
    cid = thinking_chat(studio, on=False)
    studio.gw.chat_script = [{"reasoning": ["thought", "thought"], "text": ["Ок."]}]
    events = sse(turn(studio, cid))
    assert thinks(events) == [{}]                                          # не чаще раза в секунду
    answer = answer_of(studio, cid)
    assert (answer["content"], answer["reasoning"], answer["reasoning_sec"]) == ("Ок.", None, None)
    assert state.DB.execute("SELECT reasoning, reasoning_sec FROM messages WHERE id = ?",
                            (answer["id"],)).fetchone()[:] == (None, None)


def test_switch_off_thinking_goes_again_after_a_second(studio, no_pings):
    """Ревью партии, пункт 6: `think {}` — не чаще THINK_EVERY_SEC, но и не один раз на ответ."""
    cid = thinking_chat(studio, on=False)
    studio.gw.chat_script = [{"reasoning": ["thought"] * 3, "text": ["Ок."]}]
    studio.gw.think_hold = True
    try:
        with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": "Привет"}) as r:
            events = events_of(r.iter_lines())
            assert next(d for e, d in events if e == "think") == {}
            studio.clock[0] += chat.THINK_EVERY_SEC
            studio.gw.think_hold = False
            rest = [(e, d) for e, d in events if e != "ping"]
    finally:
        studio.gw.think_hold = False
    assert rest == [("think", {}), ("delta", {"text": "Ок."}), ("done", {"status": "done"})]


def test_reasoning_after_text_in_the_same_round_is_set_apart(studio):
    """Ревью партии, пункт 5: модель снова думает после текста того же круга — через пустую строку, как в
    новом круге."""
    cid = thinking_chat(studio)
    studio.gw.chat_script = [{"reasoning": ["Считаю."], "text": ["410"],
                              "then": {"reasoning": ["Проверяю."], "text": [" — верно."]}}]
    assert thinks(sse(turn(studio, cid))) == [{"text": "Считаю."}, {"text": "\n\nПроверяю."}]
    answer = answer_of(studio, cid)
    assert (answer["content"], answer["reasoning"]) == ("410 — верно.", "Считаю.\n\nПроверяю.")


def test_model_without_switch_shows_its_reasoning(studio):
    gw = add_gateway(studio)
    studio.c.put(f"/api/sections/chat/links/{gw}", json={"models": ["local-gemma-abl", "gemma4-abl"]})
    cid = new_chat(studio, connection_id=gw, model="gemma4-abl")
    studio.gw.chat_script = [{"reasoning": ["Думаю."], "text": ["Ответ."]}]
    assert thinks(sse(turn(studio, cid))) == [{"text": "Думаю."}]
    assert answer_of(studio, cid)["reasoning"] == "Думаю."


def test_stray_reasoning_over_the_limit_stops_the_answer(studio, caplog):
    caplog.set_level(logging.INFO)
    cid = thinking_chat(studio, on=False)
    studio.gw.chat_script = [{"reasoning": ["thought"] * 300, "text": ["не дойдёт"]}]   # 2 100 знаков
    events = sse(turn(studio, cid))
    assert events[-1] == ("error", {"message": "модель зациклилась — ответ остановлен"})
    answer = answer_of(studio, cid)
    assert (answer["status"], answer["content"], answer["reasoning"]) == ("error", "", None)
    assert "рассуждение сверх предела" in caplog.text


def test_stray_limit_counts_every_round(studio):
    """STRAY_MAX — на весь ответ: два круга по 1 400 знаков — уже петля."""
    add_gateway(studio)
    assert studio.c.put("/api/me/exa-key", json={"key": EXA_KEY}).status_code == 204
    cid = new_chat(studio)
    patch(studio, cid, web=True)
    studio.gw.chat_script = [{"reasoning": ["thought"] * 200, "tool_calls": [call("web_search", query="q")]},
                             {"reasoning": ["thought"] * 200, "text": ["не дойдёт"]}]
    assert sse(turn(studio, cid))[-1] == ("error", {"message": chat.LOOPED})


def test_reasoning_over_the_limit_is_cut(studio):
    cid = thinking_chat(studio)
    studio.gw.chat_script = [{"reasoning": ["x" * 1000] * (chat.REASONING_MAX // 1000) + ["y" * 10],
                              "text": ["не дойдёт"]}]
    events = sse(turn(studio, cid))
    assert events[-1] == ("error", {"message": "рассуждение слишком длинное — ответ остановлен"})
    answer = answer_of(studio, cid)
    assert answer["status"] == "error" and len(answer["reasoning"]) == chat.REASONING_MAX


def test_model_without_switch_reasoning_over_the_limit_is_cut_and_the_answer_goes_on(studio, caplog):
    """Решение владельца 2026-10-22: облачная модель без кнопки думает дольше REASONING_MAX — рассуждение обрезано
    пометкой, дальше не идёт и не хранится (и в следующем круге), а оплаченный ответ дочитывается."""
    caplog.set_level(logging.INFO)
    gw = add_gateway(studio)
    assert studio.c.put("/api/me/exa-key", json={"key": EXA_KEY}).status_code == 204
    studio.c.put(f"/api/sections/chat/links/{gw}", json={"models": ["local-gemma-abl", "gemma4-abl"]})
    cid = new_chat(studio, connection_id=gw, model="gemma4-abl")
    patch(studio, cid, web=True)
    studio.gw.chat_script = [{"reasoning": ["x" * 1000] * (chat.REASONING_MAX // 1000) + ["y" * 10, "z" * 10],
                              "text": ["Ищу."], "tool_calls": [call("web_search", query="q")]},
                             {"reasoning": ["Второй круг."], "text": [" Готово."]}]
    events = sse(turn(studio, cid))
    answer = answer_of(studio, cid)
    assert events[-1] == ("done", {"status": "done", "reasoning_sec": answer["reasoning_sec"]})
    assert thinks(events)[-1] == {"text": chat.REASONING_CUT}
    assert (answer["status"], answer["content"]) == ("done", "Ищу.\n\n Готово.")
    assert answer["reasoning"] == "x" * chat.REASONING_MAX + chat.REASONING_CUT
    assert "рассуждение обрезано" in caplog.text


def test_reasoning_limit_counts_every_round(studio):
    """REASONING_MAX — на весь ответ (спека, разделы 4.3 и 5): два круга чуть больше половины предела — обрезано."""
    add_gateway(studio)
    assert studio.c.put("/api/me/exa-key", json={"key": EXA_KEY}).status_code == 204
    cid = new_chat(studio)
    patch(studio, cid, web=True); patch(studio, cid, thinking=True)
    half = ["x" * 1000] * (chat.REASONING_MAX // 2000 + 1)
    studio.gw.chat_script = [{"reasoning": half, "tool_calls": [call("web_search", query="q")]},
                             {"reasoning": half, "text": ["не дойдёт"]}]
    assert sse(turn(studio, cid))[-1] == ("error", {"message": chat.THINK_LONG})
    answer = answer_of(studio, cid)
    assert answer["status"] == "error" and len(answer["reasoning"]) == chat.REASONING_MAX


def test_reasoning_never_goes_back_to_the_model(studio):
    """Review Focus 4: рассуждение не уходит модели — ни во втором круге, ни в следующем ходе."""
    add_gateway(studio)
    assert studio.c.put("/api/me/exa-key", json={"key": EXA_KEY}).status_code == 204
    cid = new_chat(studio)
    patch(studio, cid, web=True); patch(studio, cid, thinking=True)
    studio.gw.chat_script = [{"reasoning": ["Надо поискать."], "tool_calls": [call("web_search", query="q")]},
                             {"reasoning": ["Нашёл, отвечаю."], "text": ["Готово."]}]
    sse(turn(studio, cid, "Найди"))
    answer = answer_of(studio, cid)
    assert (answer["content"], answer["reasoning"]) == ("Готово.", "Надо поискать.\n\nНашёл, отвечаю.")
    studio.gw.chat_script = [["Да."]]
    sse(turn(studio, cid, "Точно?"))
    for request in (studio.gw.chat_requests[1], studio.gw.chat_requests[2]):
        sent = json.dumps(request["messages"], ensure_ascii=False)
        assert "Надо поискать" not in sent and "Нашёл, отвечаю" not in sent and "reasoning" not in sent


def test_reasoning_of_a_live_answer_is_in_the_chat(studio):
    """Review Focus 3: страницу перезагрузили посреди рассуждения — GET диалога отдаёт прочитанное."""
    cid = thinking_chat(studio)
    studio.gw.chat_script = [{"reasoning": ["Первый кусок. ", "Второй."], "text": ["Ответ."]}]
    studio.gw.think_hold = True
    try:
        with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": "Привет"}) as r:
            events = events_of(r.iter_lines())
            assert next(d for e, d in events if e == "think") == {"text": "Первый кусок. "}
            live = answer_of(studio, cid)
            assert (live["status"], live["content"], live["reasoning"]) == ("streaming", "", "Первый кусок. ")
            assert isinstance(live["reasoning_sec"], float)
            studio.gw.think_hold = False
            assert [e for e, _ in events][-1] == "done"
    finally:
        studio.gw.think_hold = False
    assert answer_of(studio, cid)["reasoning"] == "Первый кусок. Второй."


def test_stop_while_thinking_keeps_the_reasoning(studio):
    """Review Focus 2: «Стоп» во время рассуждения — ответ остановлен, рассуждение сохранено."""
    cid = thinking_chat(studio)
    studio.gw.chat_script = [{"reasoning": ["Думаю долго"], "text": ["не дойдёт"]}]
    studio.gw.think_hold = True
    try:
        with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": "Привет"}) as r:
            events = events_of(r.iter_lines())
            assert next(d for e, d in events if e == "think") == {"text": "Думаю долго"}
            assert studio.c.post(f"/api/chats/{cid}/stop").status_code == 204
            rest = [(e, d) for e, d in events if e != "ping"]
    finally:
        studio.gw.think_hold = False
    answer = answer_of(studio, cid)
    assert rest == [("done", {"status": "stopped", "reasoning_sec": answer["reasoning_sec"]})]
    assert (answer["status"], answer["content"], answer["reasoning"]) == ("stopped", "", "Думаю долго")
    assert isinstance(answer["reasoning_sec"], float)
    assert not chat.STREAMS


def test_old_answer_has_no_reasoning(studio):
    """Review Focus 1: ответ до шага 15 (столбцы NULL) — без рассуждения, шаги «Интернета» на месте."""
    add_gateway(studio); cid = new_chat(studio)
    state.DB.execute("INSERT INTO messages(id, chat_id, role, content, status, via, model, created, tools)"
                     " VALUES ('old', ?, 'assistant', 'Старый ответ', 'done', 'server', 'm', 1, ?)",
                     (cid, json.dumps([{"kind": "search", "query": "q", "results": [], "text": "t"}])))
    (answer,) = messages(studio, cid)
    assert (answer["content"], answer["reasoning"], answer["reasoning_sec"]) == ("Старый ответ", None, None)
    assert [s["kind"] for s in answer["tools"]] == ["search"]


def test_reasoning_never_reaches_the_log(studio, caplog):
    caplog.set_level(logging.DEBUG)
    cid = thinking_chat(studio)
    studio.gw.chat_script = [{"reasoning": ["Тайная мысль модели"], "text": ["ok"]},
                             {"reasoning": ["Вторая тайная мысль " * 4000], "text": ["не дойдёт"]}]
    sse(turn(studio, cid))
    sse(turn(studio, cid, "ещё"))
    assert "Тайная мысль" not in caplog.text and "тайная мысль" not in caplog.text
    assert "рассуждение сверх предела" in caplog.text
