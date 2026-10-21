"""Кнопка «Интернет» диалога (`chats.web`) и описания инструментов (спека 2026-10-21-studio-chat-internet,
6.3 и 6.8), цикл инструментов в ответе через сервер (6.3, 6.4, 6.7, 6.9)."""
import asyncio
import json
import logging

import chat
import state
import web
from conftest import T0, add_connection, add_gateway, make_user, signin
from test_chat import done_transcript, events_of, from_transcript, messages, new_chat, sse, turn
from test_web import CANON, FakeYtDlp

EXA_KEY = "exa-test-key"


def chat_of(s, cid) -> dict:
    r = s.c.get(f"/api/chats/{cid}")
    assert r.status_code == 200, r.text
    return r.json()["chat"]


def patch(s, cid, **body) -> dict:
    r = s.c.patch(f"/api/chats/{cid}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_switch_is_stored_and_tools_follow_the_exa_key(studio):
    add_gateway(studio); cid = new_chat(studio)
    chat = chat_of(studio, cid)
    assert chat["web"] is False and chat["web_tools"] == ["youtube"]      # кнопке есть что включать
    assert patch(studio, cid, web=True)["web"] is True
    chat = chat_of(studio, cid)
    assert chat["web"] is True and chat["web_tools"] == ["youtube"]
    assert studio.c.put("/api/me/exa-key", json={"key": EXA_KEY}).status_code == 204
    assert chat_of(studio, cid)["web_tools"] == ["search", "page", "youtube"]
    before = chat_of(studio, cid)
    assert patch(studio, cid, web=None)["web"] is True                  # null — не трогает
    assert patch(studio, cid, title="Новости")["web"] is True           # другое поле — не трогает
    after = chat_of(studio, cid)
    assert (after["connection_id"], after["model"]) == (before["connection_id"], before["model"])
    assert patch(studio, cid, web=False)["web"] is False
    assert chat_of(studio, cid)["web"] is False


def test_switch_of_someone_elses_chat_is_gone(studio):
    add_gateway(studio); cid = new_chat(studio)
    make_user(studio, "bob"); signin(studio, "bob")
    r = studio.c.patch(f"/api/chats/{cid}", json={"web": True})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "gone"
    signin(studio, "owner")
    assert chat_of(studio, cid)["web"] is False


def test_direct_connection_keeps_the_switch_but_offers_no_tools(studio):
    gateway = add_gateway(studio); direct = add_connection(studio)
    assert studio.c.put("/api/me/exa-key", json={"key": EXA_KEY}).status_code == 204
    cid = new_chat(studio, connection_id=direct)
    chat = patch(studio, cid, web=True)
    assert chat["web"] is True and chat["web_tools"] == []
    chat = patch(studio, cid, connection_id=gateway)                     # туда и обратно — web остаётся
    assert chat["web"] is True and chat["web_tools"] == ["search", "page", "youtube"]
    chat = patch(studio, cid, connection_id=direct)
    assert chat["web"] is True and chat["web_tools"] == []


def test_tools_for_decides_by_the_path_and_the_key(studio):
    import connections
    gateway = add_gateway(studio); direct = add_connection(studio)
    uid = studio.c.get("/api/me").json()["id"]
    proxied, browser = connections.get(uid, gateway), connections.get(uid, direct)
    assert web.tools_for(None, True) == []
    assert web.tools_for(browser, True) == [] and web.tools_for(browser, False) == []
    assert web.tools_for(proxied, False) == ["youtube"]
    assert web.tools_for(proxied, True) == list(web.KINDS) == ["search", "page", "youtube"]


def test_specs_are_openai_functions_with_one_required_string():
    only = web.specs(["youtube"])
    assert [t["function"]["name"] for t in only] == ["youtube_subtitles"]
    tools = web.specs(list(web.KINDS))
    assert {t["function"]["name"]: web.NAMES[t["function"]["name"]] for t in tools} == {
        "web_search": "search", "read_page": "page", "youtube_subtitles": "youtube"}
    params = {"web_search": "query", "read_page": "url", "youtube_subtitles": "url"}
    for t in tools:
        f = t["function"]
        assert t["type"] == "function" and f["description"].strip()
        p, schema = params[f["name"]], f["parameters"]
        assert schema["type"] == "object" and list(schema["properties"]) == [p] and schema["required"] == [p]
        assert schema["properties"][p]["type"] == "string" and schema["properties"][p]["description"].strip()
    assert web.specs([]) == []


# ---- цикл инструментов в ответе через сервер ------------------------------------------------

ALL = "web_search, read_page, youtube_subtitles"


def web_chat(s, key=True) -> str:
    """Диалог шлюза с включённым «Интернетом»; key — с ключом Exa у учётки."""
    add_gateway(s)
    if key:
        assert s.c.put("/api/me/exa-key", json={"key": EXA_KEY}).status_code == 204
    cid = new_chat(s)
    patch(s, cid, web=True)
    return cid


def call(name, **args) -> tuple[str, str]:
    return name, json.dumps(args, ensure_ascii=False)


def steps_of(answer_id) -> "list | None":
    raw = state.DB.execute("SELECT tools FROM messages WHERE id = ?", (answer_id,)).fetchone()[0]
    return None if raw is None else json.loads(raw)


def noted(tools=ALL) -> dict:
    return {"role": "system", "content": f"{chat.today_line(T0)}\n\n" + chat.WEB_NOTE.format(tools=tools)}


def names(request) -> list[str]:
    return [t["function"]["name"] for t in request.get("tools", [])]


def test_web_note_goes_only_with_tools(studio):
    cid = web_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call("web_search", query="новости")]}, ["Готово."]]
    sse(turn(studio, cid, "Что нового?"))
    first, second = studio.gw.chat_requests
    assert first["messages"][0] == second["messages"][0] == noted()      # одна строка: история не тронута
    assert names(first) == names(second) == ALL.split(", ") and first["tool_choice"] == "auto"
    patch(studio, cid, web=False)
    sse(turn(studio, cid, "А ещё?"))
    third = studio.gw.chat_requests[2]
    assert "tools" not in third and "tool_choice" not in third
    assert third["messages"][0] == {"role": "system", "content": chat.today_line(T0)}


def test_web_off_sends_no_tools_and_calls_nothing(studio, monkeypatch):
    """Анти-цель 1: «Интернет» выключен — ни `tools`, ни Exa, ни yt-dlp, даже если в тексте ссылка YouTube и
    модель сама шлёт вызовы."""
    fake = FakeYtDlp()
    monkeypatch.setattr(web, "RUN", fake)
    add_gateway(studio)
    assert studio.c.put("/api/me/exa-key", json={"key": EXA_KEY}).status_code == 204
    cid = new_chat(studio)
    studio.gw.chat_script = [{"text": ["Смотрю"], "tool_calls": [call("youtube_subtitles", url=CANON),
                                                                 call("web_search", query="ролик")]}]
    events = sse(turn(studio, cid, f"Перескажи {CANON}"))
    (request,) = studio.gw.chat_requests
    assert "tools" not in request and "tool_choice" not in request
    assert request["messages"][0] == {"role": "system", "content": chat.today_line(T0)}
    assert studio.gw.exa_requests == [] and fake.calls == []
    assert "tool" not in [e for e, _ in events]
    answer = messages(studio, cid)[1]
    assert answer["tools"] == [] and steps_of(answer["id"]) is None


def test_model_searches_and_answers(studio):
    cid = web_chat(studio)
    query = "последняя версия Python"
    studio.gw.chat_script = [{"tool_calls": [call("web_search", query=query)]}, ["Python ", "3.14."]]
    events = [(e, d) for e, d in sse(turn(studio, cid, "Какая сейчас версия Python? Мой пароль 12345"))
              if e != "ping"]
    assert [e for e, _ in events] == ["turn", "tool", "delta", "delta", "done"]
    assert events[1][1] == {"kind": "search", "label": query} and events[-1][1] == {"status": "done"}
    # в Exa — только запрос модели и ключ
    assert studio.gw.exa_requests == [("/search", {"query": query, "type": "auto", "numResults": web.SEARCH_RESULTS,
                                                   "contents": {"highlights": True}}, EXA_KEY)]
    answer = messages(studio, cid)[1]
    (step,) = steps_of(answer["id"])
    assert step["kind"] == "search" and step["query"] == query and "error" not in step
    first, second = studio.gw.chat_requests
    assistant, result = second["messages"][2:]
    (tc,) = assistant["tool_calls"]
    assert assistant == {"role": "assistant", "content": None, "tool_calls": [
        {"id": tc["id"], "type": "function", "function": {"name": "web_search", "arguments": json.dumps(
            {"query": query}, ensure_ascii=False)}}]}
    assert tc["id"].startswith("chatcmpl-tool-")
    assert result == {"role": "tool", "tool_call_id": tc["id"], "content": step["text"]}
    assert "Download Python" in step["text"]
    assert (answer["status"], answer["content"]) == ("done", "Python 3.14.")
    assert answer["tools"] == [web.public(step)] and "text" not in answer["tools"][0]
    assert first["messages"][1:] == [{"role": "user", "content": "Какая сейчас версия Python? Мой пароль 12345"}]


def test_answer_without_search_when_not_needed(studio):
    cid = web_chat(studio)
    studio.gw.chat_script = [["Привет!"]]
    events = sse(turn(studio, cid, "Привет"))
    (request,) = studio.gw.chat_requests
    assert names(request) == ALL.split(", ") and "tool" not in [e for e, _ in events]
    assert studio.gw.exa_requests == []
    answer = messages(studio, cid)[1]
    assert (answer["status"], answer["content"], answer["tools"]) == ("done", "Привет!", [])
    assert steps_of(answer["id"]) is None


def test_broken_or_unknown_calls_are_error_steps_and_the_model_goes_on(studio):
    """Review Focus 1: битый JSON аргументов, неизвестное имя, не та форма — шаг-ошибка словами, модель отвечает
    дальше; битые аргументы в запрос не возвращаются (vLLM разбирает их и ответил бы 400)."""
    cid = web_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [("web_search", '{"query": '), ("delete_everything", '{"x": 1}'),
                                             ("read_page", '{"url": 42}'), ("web_search", '["a"]'),
                                             ("web_search", '{"query": "  "}')]},
                             ["Не вышло."]]
    events = sse(turn(studio, cid))
    assert events[-1] == ("done", {"status": "done"})
    answer = messages(studio, cid)[1]
    steps = steps_of(answer["id"])
    assert [s["kind"] for s in steps] == ["unknown"] * 5 and all(s["error"] for s in steps)
    assert studio.gw.exa_requests == []
    second = studio.gw.chat_requests[1]["messages"]
    assistant, results = second[2], second[3:]
    assert [tc["function"]["arguments"] for tc in assistant["tool_calls"]] == [
        "{}", '{"x": 1}', '{"url": 42}', "{}", '{"query": "  "}']
    assert [r["tool_call_id"] for r in results] == [tc["id"] for tc in assistant["tool_calls"]]
    assert all(r["content"].startswith("Ошибка инструмента: ") for r in results)
    assert (answer["status"], answer["content"]) == ("done", "Не вышло.")


def test_calls_of_one_round_run_in_order_each_with_its_id(studio):
    """Review Focus 2: несколько вызовов в одном круге — по порядку, каждому свой `role: tool`."""
    cid = web_chat(studio)
    url = "https://example.test/a"
    studio.gw.chat_script = [{"tool_calls": [call("web_search", query="q1"), call("read_page", url=url)]}, ["ok"]]
    events = sse(turn(studio, cid))
    assert [d for e, d in events if e == "tool"] == [{"kind": "search", "label": "q1"}, {"kind": "page", "label": url}]
    assert [p for p, _, _ in studio.gw.exa_requests] == ["/search", "/contents"]
    assert studio.gw.exa_requests[1][1]["urls"] == [url]
    steps = steps_of(messages(studio, cid)[1]["id"])
    assert [s["kind"] for s in steps] == ["search", "page"]
    second = studio.gw.chat_requests[1]["messages"]
    ids = [tc["id"] for tc in second[2]["tool_calls"]]
    assert len(set(ids)) == 2
    assert second[3:] == [{"role": "tool", "tool_call_id": i, "content": s["text"]} for i, s in zip(ids, steps)]


def test_text_before_the_calls_stays_apart_from_the_answer(studio):
    cid = web_chat(studio)
    studio.gw.chat_script = [{"text": ["Сейчас поищу."], "tool_calls": [call("web_search", query="q")]}, ["Нашёл."]]
    events = sse(turn(studio, cid))
    assert "".join(d["text"] for e, d in events if e == "delta") == "Сейчас поищу.\n\nНашёл."
    assert messages(studio, cid)[1]["content"] == "Сейчас поищу.\n\nНашёл."
    assistant = studio.gw.chat_requests[1]["messages"][2]
    assert assistant["role"] == "assistant" and assistant["content"] == "Сейчас поищу."


def test_rounds_are_capped(studio):
    cid = web_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call("web_search", query=f"q{n}")]} for n in range(chat.TOOL_ROUNDS)]
    events = sse(turn(studio, cid))
    requests = studio.gw.chat_requests
    assert len(requests) == chat.TOOL_ROUNDS + 1
    assert all(names(r) == ALL.split(", ") and r["messages"][0] == noted() for r in requests[:-1])
    assert "tools" not in requests[-1] and "tool_choice" not in requests[-1]
    assert requests[-1]["messages"][0] == {"role": "system", "content": chat.today_line(T0)}
    assert sum(m["role"] == "tool" for m in requests[-1]["messages"]) == chat.TOOL_ROUNDS
    answer = messages(studio, cid)[1]
    assert len(steps_of(answer["id"])) == chat.TOOL_ROUNDS
    assert (answer["status"], answer["content"]) == ("done", "Здравствуйте!") and events[-1][0] == "done"


def test_tool_result_is_cut_to_the_context(studio, monkeypatch):
    """Review Focus 4: страница не помещается в остаток контекста модели — обрезка с пометкой, а не 400 модели;
    шаг хранит то, что получила модель. Остатка нет вовсе — ошибка инструмента словами."""
    limit = chat.RESERVE_TOKENS + 1000
    monkeypatch.setattr(chat, "CONTEXT", {"local-gemma-abl": limit})
    cid = web_chat(studio)
    studio.gw.exa_page_text = "слово " * 5000
    studio.gw.chat_script = [{"tool_calls": [call("read_page", url="https://example.test/long")]}, ["Кратко."]]
    assert sse(turn(studio, cid, "Прочитай"))[-1] == ("done", {"status": "done"})
    sent = studio.gw.chat_requests[1]["messages"]
    assert sent[-1]["content"].endswith(chat.CUT_CONTEXT) and len(sent[-1]["content"]) > 500
    assert chat.estimate_tokens(sent) <= limit - chat.RESERVE_TOKENS
    (step,) = steps_of(messages(studio, cid)[1]["id"])
    assert step["text"] == sent[-1]["content"]

    monkeypatch.setattr(chat, "CONTEXT", {"local-gemma-abl": chat.RESERVE_TOKENS + 150})
    other = new_chat(studio)
    patch(studio, other, web=True)
    studio.gw.chat_script = [{"tool_calls": [call("read_page", url="https://example.test/long")]}, ["Не влезло."]]
    assert sse(turn(studio, other, "Прочитай"))[-1] == ("done", {"status": "done"})
    assert studio.gw.chat_requests[-1]["messages"][-1]["content"] == (
        "Ошибка инструмента: не помещается в контекст модели")
    (step,) = steps_of(messages(studio, other)[1]["id"])
    assert step["error"] == "не помещается в контекст модели" and "text" not in step


def test_model_without_tools_answers_in_words(studio):
    cid = web_chat(studio)
    studio.gw.chat_error = (400, {"error": {"message": "\"auto\" tool choice requires --enable-auto-tool-choice"}})
    events = sse(turn(studio, cid))
    assert events[-1] == ("error", {"message": chat.NO_TOOLS})
    assert messages(studio, cid)[1]["status"] == "error"
    patch(studio, cid, web=False)                                         # без tools — прежние слова шлюза
    events = sse(turn(studio, cid))
    assert events[-1][0] == "error" and events[-1][1]["message"].startswith("шлюз отклонил запрос")


def test_stop_during_a_tool_keeps_the_done_steps(studio, monkeypatch):
    killed = []

    async def hanging(argv, timeout):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            killed.append(argv[-1])
            raise

    monkeypatch.setattr(web, "RUN", hanging)
    cid = web_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call("web_search", query="q"), call("youtube_subtitles", url=CANON)]}]
    with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": "Привет"}) as r:
        events = events_of(r.iter_lines())
        assert next(events)[0] == "turn"
        tools = []
        for e, d in events:
            tools += [d] if e == "tool" else []
            if len(tools) == 2:
                break
        assert tools[1] == {"kind": "youtube", "label": f"субтитры {CANON}"}
        live = messages(studio, cid)[1]
        assert [s["kind"] for s in live["tools"]] == ["search"] and "text" not in live["tools"][0]
        assert studio.c.post(f"/api/chats/{cid}/stop").status_code == 204
        rest = [(e, d) for e, d in events if e != "ping"]
    assert rest == [("done", {"status": "stopped"})]
    assert killed == [CANON]
    answer = messages(studio, cid)[1]
    (step,) = steps_of(answer["id"])
    assert answer["status"] == "stopped" and step["kind"] == "search" and answer["tools"] == [web.public(step)]
    assert not chat.STREAMS


def test_web_never_reaches_the_log(studio, monkeypatch, caplog):
    from test_web import VID
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(web, "RUN", FakeYtDlp())
    cid = web_chat(studio)
    studio.gw.exa_page_text = "Секретный текст страницы"
    studio.gw.chat_script = [{"tool_calls": [call("web_search", query="тайный запрос"),
                                             call("read_page", url="https://secret-host.test/private"),
                                             call("youtube_subtitles", url=CANON)]}, ["ok"]]
    sse(turn(studio, cid))
    studio.gw.exa_replies["/search"] = (401, {"error": "Invalid API key", "tag": "INVALID_API_KEY"})
    studio.gw.chat_script = [{"tool_calls": [call("web_search", query="второй тайный запрос")]}, ["ok"]]
    sse(turn(studio, cid))
    for secret in ("тайный запрос", "secret-host", "Секретный текст", EXA_KEY, "Download Python", VID, "Интенсив",
                   "Академия", "Invalid API key", "ключ Exa не подходит"):
        assert secret not in caplog.text, secret
    for kind, result in (("search", "ok"), ("page", "ok"), ("youtube", "ok"), ("search", "ошибка")):
        assert f"шаг {kind}: {result}" in caplog.text


def test_without_a_key_only_youtube_is_offered(studio):
    cid = web_chat(studio, key=False)
    studio.gw.chat_script = [{"tool_calls": [call("web_search", query="q")]}, ["ok"]]
    sse(turn(studio, cid))
    first = studio.gw.chat_requests[0]
    assert names(first) == ["youtube_subtitles"] and first["messages"][0] == noted("youtube_subtitles")
    (step,) = steps_of(messages(studio, cid)[1]["id"])                    # не предложенный — не исполняется
    assert step["kind"] == "unknown" and step["error"] and studio.gw.exa_requests == []


def test_unreadable_exa_key_answers_in_words(studio):
    """Сменили STUDIO_DATA_KEY — ключ Exa не расшифровывается: шаг говорит это, Exa не зовётся."""
    cid = web_chat(studio)
    state.DB.execute("UPDATE users SET exa_key = ?", (b"not-a-fernet-token",))
    studio.gw.chat_script = [{"tool_calls": [call("web_search", query="q")]}, ["ok"]]
    assert sse(turn(studio, cid))[-1] == ("done", {"status": "done"})
    assert names(studio.gw.chat_requests[0]) == ALL.split(", ")
    (step,) = steps_of(messages(studio, cid)[1]["id"])
    assert step["error"] == "ключ Exa не читается — введите его заново в Настройках"
    assert studio.gw.exa_requests == []


# ---- найденное в следующих ходах (6.5) --------------------------------------------------------

def test_found_goes_to_the_next_turn_inside_the_previous_user_message(studio):
    cid = web_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call("web_search", query="версия Python")]}, ["Python 3.14."]]
    sse(turn(studio, cid, "Какая версия Python?"))
    (step,) = steps_of(messages(studio, cid)[1]["id"])
    studio.gw.chat_script = [["Да."]]
    sse(turn(studio, cid, "Точно?"))
    sent = studio.gw.chat_requests[-1]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert sent[1]["content"] == f'<web kind="search">\n{step["text"]}\n</web>\n\nКакая версия Python?'
    assert sent[2]["content"] == "Python 3.14." and sent[3]["content"] == "Точно?"
    assert all("tool_calls" not in m for m in sent)
    assert len(studio.gw.exa_requests) == 1                               # Exa не зовётся снова


def test_history_block_holds_each_step_with_its_model_text():
    steps = [{"kind": "search", "query": "q", "results": [], "text": "найдено"},
             {"kind": "page", "url": "u", "error": "страница не найдена"}]
    assert web.history_block(steps) == (
        '<web kind="search">\nнайдено\n</web>\n\n'
        '<web kind="page">\nОшибка инструмента: страница не найдена\n</web>\n\n')
    assert web.history_block([]) == ""


def test_found_goes_into_the_first_text_part_of_a_message_with_a_photo():
    """compose без сервера: сообщение пользователя частями (фото) — блок в начале первой текстовой части;
    частей без текста — новая текстовая часть первой."""
    block = '<web kind="search">\nнайдено\n</web>\n\n'
    assert chat._prefix(block, "вопрос") == block + "вопрос"
    parts = [{"type": "text", "text": "вопрос"}, {"type": "image_url", "media_id": "m"}]
    assert chat._prefix(block, parts) == [{"type": "text", "text": block + "вопрос"}, parts[1]]
    assert chat._prefix(block, [parts[1]]) == [{"type": "text", "text": block}, parts[1]]


def test_found_that_does_not_fit_refuses_the_next_turn(studio, monkeypatch):
    cid = web_chat(studio)
    studio.gw.exa_page_text = "слово " * 6000                           # ~12 тыс. токенов
    studio.gw.chat_script = [{"tool_calls": [call("read_page", url="https://example.test/long")]}, ["Кратко."]]
    sse(turn(studio, cid, "Прочитай"))
    monkeypatch.setattr(chat, "CONTEXT", {"local-gemma-abl": chat.RESERVE_TOKENS + 5000})
    before = len(messages(studio, cid))
    r = turn(studio, cid, "Ещё?")
    assert r.status_code == 413 and r.json()["detail"]["code"] == "context"
    assert len(messages(studio, cid)) == before


def test_error_answer_gives_no_found_to_the_next_turn(studio):
    cid = web_chat(studio)
    studio.gw.chat_ending = None                                          # второй круг рвётся без конца
    studio.gw.chat_script = [{"tool_calls": [call("web_search", query="q")]}, ["Обры"]]
    assert sse(turn(studio, cid, "Найди"))[-1][0] == "error"
    answer = messages(studio, cid)[1]
    assert answer["status"] == "error" and steps_of(answer["id"])
    studio.gw.chat_ending = "stop"
    studio.gw.chat_script = [["Ок."]]
    sse(turn(studio, cid, "Ещё раз"))
    sent = studio.gw.chat_requests[-1]["messages"]
    assert [m["role"] for m in sent] == ["system", "user"] and sent[1]["content"] == "Найди\n\nЕщё раз"
    assert "<web" not in json.dumps(sent, ensure_ascii=False)


def test_history_search_does_not_look_into_the_found(studio):
    """Поиск по истории — по `messages.content`: слово только из выдержки Exa диалог не находит."""
    cid = web_chat(studio)
    studio.gw.exa_replies["/search"] = (200, {"results": [
        {"title": "Сайт", "url": "https://example.test/", "highlights": ["Зюзюкамбра цветёт весной"]}]})
    studio.gw.chat_script = [{"tool_calls": [call("web_search", query="q")]}, ["Растение найдено."]]
    sse(turn(studio, cid, "Найди"))
    assert "Зюзюкамбра" in steps_of(messages(studio, cid)[1]["id"])[0]["text"]

    def items(q):
        r = studio.c.post("/api/search", json={"q": q})
        assert r.status_code == 200, r.text
        return r.json()["items"]

    assert items("Растение")                                              # ответ модели — находится
    assert items("Зюзюкамбра") == []


# ---- расшифровка в диалоге и Exa (6.10) -------------------------------------------------------

WEB_ASK = {"code": "external_web", "message": "Запросы, составленные из расшифровки, могут уйти в Exa — подтвердите"}


def consent_of(cid):
    return state.DB.execute("SELECT web_consent FROM chats WHERE id = ?", (cid,)).fetchone()[0]


def test_transcript_goes_to_exa_only_when_confirmed(studio, fake_audio):
    cid = web_chat(studio); tid = done_transcript(studio)
    a = from_transcript(studio, tid).json()
    r = turn(studio, cid, "Итоги?", attachment_ids=[a["id"]])
    assert r.status_code == 409 and r.json()["detail"] == WEB_ASK
    assert messages(studio, cid) == [] and not studio.gw.chat_requests and studio.gw.exa_requests == []
    assert consent_of(cid) is None                                       # отказ ничего не тронул
    studio.gw.chat_script = [{"tool_calls": [call("web_search", query="Иван")]}, ["Итоги."]]
    sse(turn(studio, cid, "Итоги?", attachment_ids=[a["id"]], web_ok=True))
    first = studio.clock[0]
    assert len(studio.gw.exa_requests) == 1 and consent_of(cid) == first  # расшифровка этого хода покрыта
    assert chat_of(studio, cid)["external_web"] is False
    sse(turn(studio, cid, "Ещё?"))                                        # подтверждено — без вопроса
    # позже вложенная расшифровка (при выключенном «Интернете») — вопрос снова, и у хода, и у повтора
    studio.clock[0] += 1
    patch(studio, cid, web=False)
    b = from_transcript(studio, tid).json()
    sse(turn(studio, cid, "А это?", attachment_ids=[b["id"]]))
    assert chat_of(studio, cid)["external_web"] is False                  # выключен — Exa не дан
    assert patch(studio, cid, web=True)["external_web"] is True
    before = (len(messages(studio, cid)), len(studio.gw.chat_requests))
    for body in ({"text": "И?"}, {"regenerate": True}):
        r = studio.c.post(f"/api/chats/{cid}/turns", json=body)
        assert r.status_code == 409 and r.json()["detail"] == WEB_ASK, body
    assert (len(messages(studio, cid)), len(studio.gw.chat_requests)) == before and consent_of(cid) == first
    sse(studio.c.post(f"/api/chats/{cid}/turns", json={"regenerate": True, "web_ok": True}))
    assert consent_of(cid) == first + 1 and chat_of(studio, cid)["external_web"] is False


def test_transcript_with_youtube_only_or_web_off_asks_nothing(studio, fake_audio):
    cid = web_chat(studio, key=False); tid = done_transcript(studio)      # без ключа — только YouTube
    sse(turn(studio, cid, "Итоги?", attachment_ids=[from_transcript(studio, tid).json()["id"]]))
    assert chat_of(studio, cid)["external_web"] is False and consent_of(cid) is None
    assert studio.c.put("/api/me/exa-key", json={"key": EXA_KEY}).status_code == 204
    assert chat_of(studio, cid)["external_web"] is True                   # ключ появился — Exa будет дан
    off = new_chat(studio)                                                # «Интернет» выключен
    sse(turn(studio, off, "Итоги?", attachment_ids=[from_transcript(studio, tid).json()["id"]]))
    assert chat_of(studio, off)["external_web"] is False and consent_of(off) is None
    assert studio.gw.exa_requests == []


def test_cloud_model_asks_for_both_one_after_another(studio, fake_audio):
    gw = add_gateway(studio); tid = done_transcript(studio)
    assert studio.c.put("/api/me/exa-key", json={"key": EXA_KEY}).status_code == 204
    studio.c.put(f"/api/sections/chat/links/{gw}", json={"models": ["local-gemma-abl", "gemma4-abl"]})
    cid = new_chat(studio, connection_id=gw, model="gemma4-abl")
    patch(studio, cid, web=True)
    a = from_transcript(studio, tid).json()
    r = turn(studio, cid, "Итоги?", attachment_ids=[a["id"]])
    assert r.status_code == 409 and r.json()["detail"]["code"] == "external"
    r = turn(studio, cid, "Итоги?", attachment_ids=[a["id"]], external_ok=True)
    assert r.status_code == 409 and r.json()["detail"] == WEB_ASK
    r = turn(studio, cid, "Итоги?", attachment_ids=[a["id"]], web_ok=True)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "external"
    assert messages(studio, cid) == [] and not studio.gw.chat_requests and consent_of(cid) is None
    sse(turn(studio, cid, "Итоги?", attachment_ids=[a["id"]], external_ok=True, web_ok=True))
    view = chat_of(studio, cid)
    assert (view["external"], view["external_web"]) == (False, False) and consent_of(cid) == studio.clock[0]


def test_labels_and_model_text_of_steps():
    assert web.label("search", {"query": "  новости\n  Python "}) == "новости Python"
    assert web.label("page", {"url": "https://example.test/" + "a" * 300}) == ("https://example.test/" + "a" * 300)[:200]
    assert web.label("youtube", {"url": CANON}) == f"субтитры {CANON}"
    assert web.label("unknown", {}) == "" and web.label("search", {"query": 42}) == ""
    assert web.for_model({"kind": "page", "url": "u", "text": "текст"}) == "текст"
    assert web.for_model({"kind": "page", "url": "u", "error": "страница не найдена"}) == (
        "Ошибка инструмента: страница не найдена")
    assert web.public({"kind": "page", "url": "u", "title": "t", "text": "текст"}) == {
        "kind": "page", "url": "u", "title": "t"}
