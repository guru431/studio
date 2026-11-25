"""Ответ по частям (спека 2026-11-25-studio-skills-status-local-models, п. 7, решения 14 и 15; app/chat_parts.py):
`run_part` предлагается, пока последний навык диалога «по частям»; часть — отдельный запрос той же модели тем же путём
с чистым контекстом, её итог возвращается ходу; свой счёт (10 на ответ, одна за круг), свой срок, «Остановить»
прерывает и часть, защищённый источник внутри части помечает диалог. В журнал — ни названия, ни задания, ни итога."""
import json
import logging

import chat
import chat_parts
import chat_tools
import mcp_client
import state
import web
from conftest import add_gateway
from test_chat import events_of, messages, new_chat, sse, turn, window
from test_chat_pending import answer as answer_card, pending_of
from test_chat_web import EXA_KEY, call
from test_cleaner_route_chat import cleaner_gw, routed_chat  # noqa: F401 (фикстура)
from test_mcp_dialog import methods, name_of, offered, setup, tool
from test_protected_source import ALL, flags, names, protect, web_dialog

SKILL = {"name": "По частям", "text": "Дели задачу.", "scope": "chat", "parts": True}


def new_skill(s, **body) -> str:
    r = s.c.post("/api/skills", json=SKILL | body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def parts_chat(s) -> tuple[str, str]:
    """Диалог шлюза и навык «по частям»; память учётки включена — у части есть встроенный инструмент
    (`memory_forget`; `memory_save` внутри части не предлагается)."""
    add_gateway(s)
    state.DB.execute("UPDATE users SET memory_on = 1")
    return new_chat(s), new_skill(s)


def part(title="Часть 1", task="Сделай первое") -> tuple[str, str]:
    return call("run_part", title=title, task=task)


def tool_names(request) -> list[str]:
    return [t["function"]["name"] for t in request.get("tools", [])]


def parts_of(answer) -> list[dict]:
    return [s for s in answer["tools"] if s["kind"] == "part" and "part" not in s]


def stored_steps(answer_id) -> list[dict]:
    return json.loads(state.DB.execute("SELECT tools FROM messages WHERE id = ?", (answer_id,)).fetchone()[0])


def test_run_part_is_offered_only_while_the_last_skill_has_parts(studio):
    cid, sid = parts_chat(studio)
    plain = new_skill(studio, name="Просто", parts=False)
    sse(turn(studio, cid, "Без навыка"))
    assert "run_part" not in tool_names(studio.gw.chat_requests[-1])
    sse(turn(studio, cid, "Задача", skill_id=sid))
    assert tool_names(studio.gw.chat_requests[-1])[-1] == "run_part"          # последним из встроенных
    sse(turn(studio, cid, "Дальше без навыка"))
    assert "run_part" in tool_names(studio.gw.chat_requests[-1])
    sse(turn(studio, cid, "Другой навык", skill_id=plain))
    assert "run_part" not in tool_names(studio.gw.chat_requests[-1])
    assert chat_tools.parts_offered(cid) is False


def test_part_request_has_clean_context_and_its_result_returns_to_the_turn(studio):
    cid, sid = parts_chat(studio)
    studio.gw.chat_script = [["Старый ответ"], {"tool_calls": [part()]}, ["Итог первой части"], ["Общий ответ"]]
    sse(turn(studio, cid, "Секрет прошлого хода"))
    studio.gw.chat_usage = {"prompt_tokens": 100, "completion_tokens": 10}
    events = sse(turn(studio, cid, "Большая задача", skill_id=sid), metrics=True)
    _, main, sub, last = studio.gw.chat_requests
    assert [m["role"] for m in sub["messages"]] == ["system", "user"] and sub["model"] == main["model"]
    assert sub["messages"][0] == main["messages"][0]                       # системное сообщение хода
    body = json.dumps(sub["messages"], ensure_ascii=False)
    assert "Сделай первое" in body and '<part title="Часть 1">' in sub["messages"][1]["content"]
    assert not any(w in body for w in ("Секрет прошлого хода", "Старый ответ", "Дели задачу.", "Большая задача"))
    assert not chat_tools.PART_EXCLUDED & set(tool_names(sub)) and "run_part" in tool_names(main)
    assert last["messages"][-1]["role"] == "tool" and last["messages"][-1]["content"] == "Итог первой части"
    answer = messages(studio, cid)[-1]
    step = answer["tools"][0]
    assert answer["content"] == "Общий ответ"                            # текст части в ответ не попал
    assert {**step, "ms": 0} == {"kind": "part", "n": 1, "title": "Часть 1", "state": "done", "ms": 0,
                                 "text": "Итог первой части"}
    assert ("part", {"n": 1, "title": "Часть 1", "state": "running"}) in events
    assert ("tool", {"kind": "part", "label": "Часть 1"}) in events
    ended = [d for e, d in events if e == "part" and d["state"] == "done"]
    assert len(ended) == 1 and set(ended[0]) == {"n", "title", "state", "ms"}
    assert not any(e == "delta" and "Итог" in d["text"] for e, d in events)
    assert answer["usage"] == {"in": 300, "out": 30}                       # запрос части — в расходе ответа


def test_second_part_request_carries_the_result_of_the_first(studio):
    cid, sid = parts_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [part()]}, ["Итог первой части"],
                             {"tool_calls": [part("Часть 2", "Сделай второе")]}, ["Итог второй"], ["Готово"]]
    sse(turn(studio, cid, "Задача", skill_id=sid))
    second = json.dumps(studio.gw.chat_requests[3]["messages"], ensure_ascii=False)
    assert "Итог первой части" in second and '<done-part n=\\"1\\" title=\\"Часть 1\\">' in second
    assert "Сделай второе" in second and "Сделай первое" not in second
    assert [s["state"] for s in parts_of(messages(studio, cid)[-1])] == ["done", "done"]


def test_earlier_part_results_are_excerpted_in_the_next_part(studio):
    """Р7: итог прежней части в запросе следующей — выжимкой `chat.history_excerpt`; модели хода — целиком."""
    cid, sid = parts_chat(studio)
    first = "я" * 3000
    studio.gw.chat_script = [{"tool_calls": [part()]}, [first], {"tool_calls": [part("Часть 2", "Ещё")]}, ["Два"],
                             ["Готово"]]
    sse(turn(studio, cid, "Задача", skill_id=sid))
    main_after = studio.gw.chat_requests[2]
    assert main_after["messages"][-1]["content"] == first
    second = studio.gw.chat_requests[3]["messages"][-1]["content"]
    assert chat.history_excerpt(first) in second and first not in second


def test_eleventh_part_is_refused_in_words_and_part_rounds_are_not_counted(studio):
    cid, sid = parts_chat(studio)
    chat.set_tool_rounds(2)                                              # 11 кругов по одному run_part, затем текст
    script = []
    for i in range(1, 11):
        script += [{"tool_calls": [part(f"Часть {i}", f"Задание {i}")]}, [f"Итог {i}"]]
    studio.gw.chat_script = script + [{"tool_calls": [part("Часть 11", "Задание 11")]}, ["Ответ"]]
    sse(turn(studio, cid, "Задача", skill_id=sid))
    answer = messages(studio, cid)[-1]
    steps = answer["tools"]
    assert len([s for s in steps if s["kind"] == "part" and s["state"] == "done"]) == 10
    assert steps[-1]["error"] == chat_parts.LIMIT and "n" not in steps[-1] and "ms" not in steps[-1]
    assert answer["status"] == "done" and answer["content"] == "Ответ"
    assert "tools" in studio.gw.chat_requests[-1]                        # последний главный запрос — ещё с инструментами


def test_second_part_of_one_round_is_refused(studio):
    cid, sid = parts_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [part("А", "раз"), part("Б", "два")]}, ["Итог А"], ["Готово"]]
    sse(turn(studio, cid, "Задача", skill_id=sid))
    answer = messages(studio, cid)[-1]
    assert [s.get("error") for s in parts_of(answer)] == [None, chat_parts.ONE_PER_ROUND]
    assert len(studio.gw.chat_requests) == 3                             # части «Б» запроса не было


def test_part_result_is_cut_and_part_rounds_are_capped(studio, monkeypatch):
    assert (chat_tools.PART_RESULT_MAX, chat_tools.PART_ROUNDS, chat_tools.PARTS_MAX) == (4000, 8, 10)
    cid, sid = parts_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [part()]}, ["ж" * 5000], ["Готово"]]
    sse(turn(studio, cid, "Задача", skill_id=sid))
    step = parts_of(messages(studio, cid)[-1])[0]
    assert len(step["text"]) == 4000 and studio.gw.chat_requests[-1]["messages"][-1]["content"] == "ж" * 4000
    # кругов инструментов в части — не больше PART_ROUNDS, затем запрос без них; ask_user в части — отказ словами
    monkeypatch.setattr(chat_tools, "PART_ROUNDS", 2)
    studio.gw.chat_requests.clear()
    studio.gw.chat_script = [{"tool_calls": [part("Ещё", "Снова")]},
                             {"tool_calls": [call("ask_user", question="Можно?")]},
                             {"tool_calls": [call("todo_write", items=[])]}, ["Итог"], ["Готово"]]
    sse(turn(studio, cid, "Ещё задача"))
    _, sub1, sub2, sub3, _ = studio.gw.chat_requests
    assert "tools" in sub1 and "tools" in sub2 and "tools" not in sub3
    inner = [s for s in messages(studio, cid)[-1]["tools"] if "part" in s]
    assert [(s["kind"], s["error"]) for s in inner] == [("ask", chat_tools.NOT_IN_PART),
                                                         ("todo", chat_tools.NOT_IN_PART)]
    assert state.DB.execute("SELECT COUNT(*) FROM chat_pending").fetchone()[0] == 0
    # run_part в диалоге без навыка «по частям» — отказ словами, карточки вопроса нет
    other = new_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [part()]}, ["Ответ"]]
    sse(turn(studio, other, "Без навыка"))
    refused = messages(studio, other)[-1]["tools"]
    assert refused == [{"kind": "part", "title": "Часть 1", "state": "error", "error": web.NOT_OFFERED}]
    assert state.DB.execute("SELECT COUNT(*) FROM chat_pending").fetchone()[0] == 0


def test_part_result_is_cut_to_the_context(studio):
    """Ревью, п. 2: итог части (в нём результаты «Интернета», MCP и знаний) — через `_fit`, как результат
    инструмента: малое окно — обрезка с пометкой chat.CUT_CONTEXT, шаг хранит то, что получила модель; остатка нет —
    ошибка части NO_ROOM."""
    cid, sid = parts_chat(studio)
    base = studio.c.post(f"/api/chats/{cid}/estimate", json={"text": "Задача", "skill_id": sid}).json()["tokens"]
    window(studio, chat.RESERVE_TOKENS + base + 1000)
    studio.gw.chat_script = [{"tool_calls": [part()]}, ["слово " * 660], ["Готово"]]
    assert sse(turn(studio, cid, "Задача", skill_id=sid))[-1] == ("done", {"status": "done"})
    sent = studio.gw.chat_requests[-1]["messages"][-1]["content"]
    assert sent.endswith(chat.CUT_CONTEXT) and 500 < len(sent) < 3960
    (step,) = parts_of(messages(studio, cid)[-1])
    assert (step["state"], step["text"]) == ("done", sent)
    other = new_chat(studio)
    window(studio, chat.RESERVE_TOKENS + base + 150)
    studio.gw.chat_script = [{"tool_calls": [part()]}, ["слово " * 660], ["Не влезло"]]
    assert sse(turn(studio, other, "Задача", skill_id=sid))[-1] == ("done", {"status": "done"})
    (step,) = parts_of(messages(studio, other)[-1])
    assert (step["state"], step["error"], step["text"]) == ("error", chat.NO_ROOM, "")
    assert "слово" not in studio.gw.chat_requests[-1]["messages"][-1]["content"]


def test_answer_with_parts_outlives_1800_seconds_and_a_part_has_its_own_limit(studio, monkeypatch, no_pings):
    monkeypatch.setattr(chat, "STREAM_TOTAL_SEC", 1800)
    assert chat.PARTS_TOTAL_SEC == 7200
    cid, sid = parts_chat(studio)
    step = [1700]

    async def slow(request):
        if b"<part " in request.content:
            studio.clock[0] += step[0]
        return await studio.gw.builtin(request)

    studio.gw.on["/v1/chat/completions"] = slow
    studio.gw.chat_script = [{"tool_calls": [part()]}, ["Итог 1"], {"tool_calls": [part("Часть 2", "Второе")]},
                             ["Итог 2"], ["Готово"]]
    sse(turn(studio, cid, "Задача", skill_id=sid))
    answer = messages(studio, cid)[-1]
    assert answer["status"] == "done" and chat.CUT_TIME not in answer["content"]            # прошло 3400 с
    assert [s["state"] for s in parts_of(answer)] == ["done", "done"]
    # другой диалог: часть дольше срока обычного ответа — её ошибка, ответ идёт дальше
    step[0] = 1801
    other = new_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [part()]}, ["Итог"], ["Готово"]]
    sse(turn(studio, other, "Задача", skill_id=sid))
    answer = messages(studio, other)[-1]
    (only,) = parts_of(answer)
    assert (only["state"], only["error"], answer["status"]) == ("error", chat_parts.TIME, "done")


def test_looping_reasoning_stops_the_part_and_the_answer_goes_on(studio):
    """Ревью, п. 1: рассуждение круга части не хранится и не идёт на страницу, но сверх STRAY_MAX (кнопка
    выключена) или REASONING_MAX — ошибка части, а не 1800 с на карте; ход отвечает дальше."""
    cid, sid = parts_chat(studio)                    # локальная модель: кнопка «Рассуждение» выключена
    studio.gw.chat_script = [{"tool_calls": [part()]}, {"reasoning": ["thought" * 100] * 3, "text": ["не дойдёт"]},
                             ["Готово"]]
    events = sse(turn(studio, cid, "Задача", skill_id=sid))
    answer = messages(studio, cid)[-1]
    (only,) = parts_of(answer)
    assert (only["state"], only["error"]) == ("error", chat_parts.LOOPED)
    assert (answer["status"], answer["content"], answer["reasoning"]) == ("done", "Готово", None)
    assert not any(e == "think" for e, _ in events) and events[-1] == ("done", {"status": "done"})
    # кнопка включена — предел REASONING_MAX
    assert studio.c.patch(f"/api/chats/{cid}", json={"thinking": True}).json()["thinking"] is True
    studio.gw.chat_script = [{"tool_calls": [part("Ещё", "Снова")]},
                             {"reasoning": ["x" * chat.REASONING_MAX, "y"], "text": ["не дойдёт"]}, ["Готово 2"]]
    events = sse(turn(studio, cid, "Ещё"))
    answer = messages(studio, cid)[-1]
    (only,) = parts_of(answer)
    assert (only["state"], only["error"]) == ("error", chat_parts.THINK_LONG)
    assert (answer["status"], answer["content"], answer["reasoning"]) == ("done", "Готово 2", None)
    assert not any(e == "think" for e, _ in events)


def test_part_time_runs_out_between_calls_and_rounds(studio, monkeypatch, no_pings):
    """Ревью, п. 3: срок части проверяется не только при чтении потока — после вызова, съевшего срок, ни следующий
    вызов круга, ни следующий запрос части не идут."""
    monkeypatch.setattr(chat, "STREAM_TOTAL_SEC", 1800)
    add_gateway(studio)
    assert studio.c.put("/api/me/exa-key", json={"key": EXA_KEY}).status_code == 204
    cid = new_chat(studio)
    assert studio.c.patch(f"/api/chats/{cid}", json={"web": True}).json()["web"] is True
    sid = new_skill(studio)

    real = web.call

    async def slow_call(*args, **kwargs):
        studio.clock[0] += 1801
        return await real(*args, **kwargs)

    monkeypatch.setattr(web, "call", slow_call)
    studio.gw.chat_script = [{"tool_calls": [part()]},
                             {"tool_calls": [call("web_search", query="раз"), call("web_search", query="два")]},
                             ["Готово"]]
    sse(turn(studio, cid, "Задача", skill_id=sid))
    answer = messages(studio, cid)[-1]
    (only,) = parts_of(answer)
    assert (only["state"], only["error"]) == ("error", chat_parts.TIME)
    assert (answer["status"], answer["content"]) == ("done", "Готово")
    assert len([r for r in studio.gw.exa_requests if r[0] == "/search"]) == 1   # второй вызов круга не исполнен
    assert len(studio.gw.chat_requests) == 3                                    # ход, часть, ход — второго круга нет
    # второй вызов отдельным кругом: срок вышел между кругами — запроса части нет
    studio.gw.chat_requests.clear()
    studio.gw.chat_script = [{"tool_calls": [part("Ещё", "Снова")]}, {"tool_calls": [call("web_search", query="три")]},
                             ["Готово 2"]]
    sse(turn(studio, cid, "Ещё"))
    answer = messages(studio, cid)[-1]
    (only,) = parts_of(answer)
    assert only["error"] == chat_parts.TIME and answer["content"] == "Готово 2" and len(studio.gw.chat_requests) == 3
    assert studio.gw.chat_requests[-1]["messages"][-1]["role"] == "tool"


def test_stop_interrupts_the_part(studio):
    cid, sid = parts_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [part()]}, ["Итог", " части"], ["Готово"]]
    studio.gw.chat_hold = True
    try:
        with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": "Задача", "skill_id": sid}) as r:
            events = events_of(r.iter_lines())
            next(d for e, d in events if e == "part")
            assert studio.c.post(f"/api/chats/{cid}/stop").status_code == 204
            rest = [(e, d) for e, d in events if e != "ping"]
    finally:
        studio.gw.chat_hold = False
    assert rest[-1] == ("done", {"status": "stopped"})
    answer = messages(studio, cid)[-1]
    assert (answer["status"], parts_of(answer)[0]["state"], parts_of(answer)[0]["error"]) == (
        "stopped", "error", chat_parts.STOPPED)


def test_run_part_in_a_protected_dialog_gets_no_web_and_protection_inside_a_part_sticks(studio):
    sid_mcp, calls, _ = setup(studio, tools=[tool("lookup"), tool("search", annotations={"openWorldHint": True})])
    protect(studio, sid_mcp)
    cid = web_dialog(studio)
    sid = new_skill(studio)
    studio.gw.chat_script = [{"tool_calls": [part()]}, {"tool_calls": [call(name_of(sid_mcp), q="1")]}, ["Итог"],
                             ["Готово"]]
    sse(turn(studio, cid, "Задача", skill_id=sid))
    main, sub1, sub2, last = studio.gw.chat_requests
    assert names(sub1) == ALL and names(sub2) == [] and names(last) == []    # сработал внутри части — «Интернета» нет
    assert offered(sub2) == ["Shared tools: lookup"] and offered(last) == ["Shared tools: lookup"]
    assert flags(cid) == (1, 0)
    # следующий ход того же диалога: запрос части без «Интернета»; вызов MCP со ссылкой внутри части — отказ без вызова
    studio.gw.chat_requests.clear()
    before = methods(calls).count("tools/call")
    studio.gw.chat_script = [{"tool_calls": [part("Ещё", "Проверь")]},
                             {"tool_calls": [call(name_of(sid_mcp), q="https://evil.example/x")]}, ["Итог"], ["Готово"]]
    sse(turn(studio, cid, "Ещё"))
    _, sub, _, _ = studio.gw.chat_requests
    assert names(sub) == []
    inner = [s for s in messages(studio, cid)[-1]["tools"] if "part" in s]
    assert inner[0]["error"] == mcp_client.URL_REFUSED and methods(calls).count("tools/call") == before
    refused = studio.c.patch(f"/api/chats/{cid}", json={"web": True})
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "protected"


def test_part_refuses_mcp_tools_that_need_confirmation(studio):
    sid_mcp, calls, _ = setup(studio, tools=[tool("lookup"), tool("translate", _meta={"studio/confirm": True})])
    cid = new_chat(studio)
    sid = new_skill(studio)
    studio.gw.chat_script = [{"tool_calls": [part()]}, {"tool_calls": [call(name_of(sid_mcp, "translate"), q="1")]},
                             ["Итог"], ["Готово"]]
    sse(turn(studio, cid, "Задача", skill_id=sid))
    _, sub, _, _ = studio.gw.chat_requests
    assert offered(sub) == ["Shared tools: lookup"]
    inner = [s for s in messages(studio, cid)[-1]["tools"] if "part" in s]
    assert inner[0]["error"] == chat_parts.NEEDS_CONFIRM and "tools/call" not in methods(calls)
    assert state.DB.execute("SELECT COUNT(*) FROM chat_pending").fetchone()[0] == 0


def test_part_goes_through_cleaner_with_the_dialog_context(cleaner_gw):  # провайдеру мимо Cleaner — ничего
    s, gw = cleaner_gw
    cid = routed_chat(s, gw)
    sid = new_skill(s)
    gw.script = [{"tool_calls": [part()]}, ["Итог"], ["Готово"]]
    assert sse(turn(s, cid, "Задача", skill_id=sid))[-1] == ("done", {"status": "done"})
    assert len(gw.turns) == 3 and len({r.headers["x-cleaner-chat"] for r in gw.turns}) == 1
    sub = json.loads(gw.turns[1].content)
    assert [m["role"] for m in sub["messages"]] == ["system", "user"] and "Сделай первое" in sub["messages"][1]["content"]
    assert parts_of(messages(s, cid)[-1])[0]["text"] == "Итог"


def test_next_turn_gets_the_part_result_but_not_its_inner_steps(studio):
    add_gateway(studio)
    assert studio.c.put("/api/me/exa-key", json={"key": EXA_KEY}).status_code == 204
    cid = new_chat(studio)
    assert studio.c.patch(f"/api/chats/{cid}", json={"web": True}).json()["web"] is True
    sid = new_skill(studio)
    studio.gw.chat_script = [{"tool_calls": [part()]}, {"tool_calls": [call("web_search", query="новости")]},
                             ["Итог первой части"], ["Готово"]]
    sse(turn(studio, cid, "Задача", skill_id=sid))
    answer = messages(studio, cid)[-1]
    search = next(s for s in answer["tools"] if s["kind"] == "search")
    assert search["part"] == 1 and "text" not in search and search["results"]
    stored = next(s for s in stored_steps(answer["id"]) if s["kind"] == "search")
    assert stored["text"] == "" and stored["part"] == 1                  # Р1: ключ text есть, текста выдачи нет
    found = search["results"][0]["url"]
    hits = [r for r in studio.gw.exa_requests if r[0] == "/search"]
    assert hits                                                           # поиск шёл
    studio.gw.chat_script = [{"tool_calls": [call("read_page", url=found)]}, ["Прочитал"]]
    sse(turn(studio, cid, "Прочитай найденное"))
    nxt = studio.gw.chat_requests[-2]
    body = json.dumps(nxt["messages"], ensure_ascii=False)
    assert "Итог первой части" in body
    assert all(r["title"] not in body for r in search["results"] if r.get("title"))
    page = messages(studio, cid)[-1]["tools"][0]
    assert page["kind"] == "page" and page.get("error") != web.NOT_KNOWN


def test_pause_keeps_part_counters(studio):
    cid, sid = parts_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [part("Часть 0", "Нулевое"), call("ask_user", question="Дальше?")]},
                             ["Итог 0"]]
    events = sse(turn(studio, cid, "Задача", skill_id=sid))
    pid = pending_of(events)["id"]
    script = []                                      # ещё 9 частей и десятая сверх предела, затем текст
    for i in range(1, 10):
        script += [{"tool_calls": [part(f"Часть {i}", f"Задание {i}")]}, [f"Итог {i}"]]
    studio.gw.chat_script = script + [{"tool_calls": [part("Часть 10", "Задание 10")]}, ["Ответ"]]
    sse(answer_card(studio, cid, pid, "answer", "Да"))
    answer = messages(studio, cid)[-1]
    done = [s for s in parts_of(answer) if s["state"] == "done"]
    assert len(done) == 10 and parts_of(answer)[-1]["error"] == chat_parts.LIMIT


def test_parts_never_reach_the_log(studio, caplog):
    cid, sid = parts_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [part("Тайное название", "Тайное задание")]}, ["Тайный итог"], ["Готово"]]
    with caplog.at_level(logging.INFO, logger="studio"):
        sse(turn(studio, cid, "Задача", skill_id=sid))
    assert "часть 1: done" in caplog.text
    assert not any(w in caplog.text for w in ("Тайное", "Тайный", "По частям", "Дели задачу"))
