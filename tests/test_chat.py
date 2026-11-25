"""Чат (app/chat.py): диалоги, вложения, сборка контекста, поток через сервер для подключений из
списка администратора и передача JSON браузеру для прямых. Шлюз — FakeGateway (`/v1/chat/completions`
потоком OpenAI SSE), часы — clock.NOW, паузы — clock.SLEEP: пинги идут без настоящего ожидания."""
import asyncio
import io
import json
import logging

import httpx
import pytest
from PIL import Image

import chat
import documents
import profiles
import state
from conftest import T0, add_connection, add_gateway, erase_history, make_user, signin
from fakes import TOKEN
from test_documents import pdf
from test_images import describe, wait_description

QUESTION = "Что сказано в договоре о сроке оплаты?"


METRICS = ("usage", "gen_ms", "total_ms")


def events_of(lines, metrics=False):
    """События SSE по строкам ответа: (имя, данные); комментарий `: ping` — ("ping", None). Без `metrics` у события
    `done` нет ключей `usage`, `gen_ms`, `total_ms` (время хода в тестах двигают пинги — сравнения были бы случайными)."""
    event, data = None, None
    for line in lines:
        if line == ": ping":
            yield "ping", None
        elif line.startswith("event: "):
            event = line[len("event: "):]
        elif line.startswith("data: "):
            data = json.loads(line[len("data: "):])
        elif line == "" and event is not None:
            if event == "done" and not metrics and isinstance(data, dict):
                data = {k: v for k, v in data.items() if k not in METRICS}
            yield event, data
            event, data = None, None


def sse(response, metrics=False):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return list(events_of(response.iter_lines(), metrics))


def new_chat(s, **body) -> str:
    r = s.c.post("/api/chats", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def turn(s, chat_id, text="Привет", **extra):
    return s.c.post(f"/api/chats/{chat_id}/turns", json={"text": text} | extra)


def messages(s, chat_id):
    return s.c.get(f"/api/chats/{chat_id}").json()["messages"]


def attach(s, name, data, mime="application/octet-stream"):
    r = s.c.post("/api/attachments", files={"file": (name, data, mime)})
    assert r.status_code == 201, r.text
    return r.json()


def png(color="red") -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (20, 10), color).save(out, "PNG")
    return out.getvalue()


def wait(s, stream):
    s.c.portal.call(asyncio.wait_for, stream.task, 5)


# ---- поток через сервер ----------------------------------------------------

def test_proxied_turn_streams_and_saves(studio):
    add_gateway(studio); chat = studio.c.post("/api/chats", json={}).json()
    events = sse(studio.c.post(f"/api/chats/{chat['id']}/turns", json={"text": "Привет"}))
    assert [e for e, _ in events][0] == "turn" and "".join(d["text"] for e, d in events if e == "delta") == "Здравствуйте!"
    msgs = studio.c.get(f"/api/chats/{chat['id']}").json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"] and msgs[1]["status"] == "done"
    assert studio.gw.chat_requests[0]["model"] == "local-gemma-abl" and studio.gw.chat_requests[0]["stream"] is True
    # подробнее: ход называет оба сообщения, ответ — целиком, ключ подключения дошёл до шлюза
    turn_ev = events[0][1]
    assert set(turn_ev) == {"user_message_id", "assistant_message_id"}
    assert [m["id"] for m in msgs] == [turn_ev["user_message_id"], turn_ev["assistant_message_id"]]
    assert msgs[1]["content"] == "Здравствуйте!" and msgs[1]["model"] == "local-gemma-abl"
    assert events[-1] == ("done", {"status": "done"})
    assert studio.gw.chat_requests[0]["messages"][1:] == [{"role": "user", "content": "Привет"}]
    assert studio.gw.chat_auth == [f"Bearer {TOKEN}"]
    assert chat["id"] not in {st.chat_id for st in chat_streams()}


def chat_streams():
    return list(chat.STREAMS.values())


def test_answer_is_saved_when_the_client_disconnects(studio):
    add_gateway(studio); cid = new_chat(studio)
    studio.gw.chat_hold = True                       # шлюз отдаёт первый кусок и ждёт
    with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": "Привет"}) as r:
        events = events_of(r.iter_lines())
        name, ids = next(events)
        assert name == "turn"
        assert next(d for e, d in events if e == "delta") == {"text": "Здрав"}
        stream = chat.STREAMS[ids["assistant_message_id"]]
    # ответ закрыт посреди потока — клиент ушёл; модель дочитывается до конца
    assert not stream.task.done()
    studio.gw.chat_hold = False
    wait(studio, stream)
    answer = messages(studio, cid)[1]
    assert (answer["status"], answer["content"]) == ("done", "Здравствуйте!")
    assert ids["assistant_message_id"] not in chat.STREAMS


def test_pings_keep_a_slow_model_alive(studio):
    add_gateway(studio); cid = new_chat(studio)
    studio.gw.chat_delay = 40                        # модель молчит 40 «секунд» по часам студии
    names = [e for e, _ in sse(turn(studio, cid))]
    first = names.index("delta")
    assert names[0] == "turn" and names[1:first].count("ping") >= 2
    assert set(names[1:first]) == {"ping"} and names[-1] == "done"
    assert chat.PING_SEC in studio.sleeps


def test_stop_keeps_the_partial_answer(studio):
    add_gateway(studio); cid = new_chat(studio)
    studio.gw.chat_hold = True
    try:
        with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": "Привет"}) as r:
            events = events_of(r.iter_lines())
            assert next(events)[0] == "turn"
            assert next(d for e, d in events if e == "delta") == {"text": "Здрав"}
            assert studio.c.post(f"/api/chats/{cid}/stop").status_code == 204
            rest = [(e, d) for e, d in events if e != "ping"]
    finally:
        studio.gw.chat_hold = False
    assert rest == [("done", {"status": "stopped"})]
    answer = messages(studio, cid)[1]
    assert (answer["status"], answer["content"]) == ("stopped", "Здрав")
    assert not chat.STREAMS
    assert studio.c.post(f"/api/chats/{cid}/stop").status_code == 204          # нечего останавливать


def test_gateway_refusal_ends_the_stream_with_error(studio):
    add_gateway(studio); cid = new_chat(studio)
    studio.gw.chat_error = (401, {"error": {"message": "Authentication Error"}})
    events = sse(turn(studio, cid))
    assert events[-1][0] == "error" and "«srvllm»" in events[-1][1]["message"]
    assert messages(studio, cid)[1]["status"] == "error"


def test_stream_cut_without_an_ending_is_not_a_finished_answer(studio):
    """Поток кончился без `[DONE]` и `finish_reason` — ответ оборван, а не готов (аудит): прочитанное
    сохраняется ошибкой. Так же — 200 с HTML вместо потока и пустой ответ."""
    import httpx
    add_gateway(studio); cid = new_chat(studio)
    studio.gw.chat_ending = None
    events = sse(turn(studio, cid))
    assert "".join(d["text"] for e, d in events if e == "delta") == "Здравствуйте!"
    assert events[-1][0] == "error" and "без признака конца" in events[-1][1]["message"]
    assert (messages(studio, cid)[1]["status"], messages(studio, cid)[1]["content"]) == ("error", "Здравствуйте!")
    for reply in (httpx.Response(200, text="<html><body>Bad gateway</body></html>", headers={"content-type": "text/html"}),
                  httpx.Response(200, headers={"content-type": "text/event-stream"})):
        async def odd(request, reply=reply):
            return reply
        studio.gw.on["/v1/chat/completions"] = odd
        events = sse(turn(studio, cid))
        assert events[-1][0] == "error" and "без признака конца" in events[-1][1]["message"]
        assert (messages(studio, cid)[-1]["status"], messages(studio, cid)[-1]["content"]) == ("error", "")


def test_answer_cut_by_the_models_length_limit_is_marked(studio):
    """finish_reason `length` — ответ обрезан пределом длины модели: ошибка с пометкой в тексте."""
    add_gateway(studio); cid = new_chat(studio)
    studio.gw.chat_ending = "length"
    studio.gw.chat_script = [{"text": ["Здравствуйте!"]}, {"text": []}, {"text": []}]
    events = sse(turn(studio, cid))
    assert events[-1] == ("error", {"message": "ответ обрезан пределом длины модели"})
    answer = messages(studio, cid)[1]
    assert (answer["status"], answer["content"]) == ("error", "Здравствуйте!" + chat.CUT_LENGTH)
    assert len(studio.gw.chat_requests) == 3


def test_length_retries_without_long_reasoning_or_executing_partial_tool_calls(studio):
    add_gateway(studio)
    cid = new_chat(studio)
    studio.gw.chat_script = [
        {"ending": "length", "reasoning": ["Long thought"], "tool_calls": [("unused", '{"truncated":')]},
        {"ending": "length", "text": ["Первая часть. "]},
        {"text": ["Вторая часть."]},
    ]
    events = sse(turn(studio, cid, "Подготовь документ"))
    assert ("recovery", {"stage": "continue"}) in events and events[-1][0] == "done"
    answer = messages(studio, cid)[-1]
    assert answer["status"] == "done" and answer["content"] == "Первая часть. Вторая часть."
    assert answer["tools"] == []
    first, second, third = studio.gw.chat_requests
    assert first["messages"][-1]["content"] == "Подготовь документ"
    assert second["chat_template_kwargs"] == {"enable_thinking": False}
    assert "Подготовь документ" in second["messages"][-1]["content"]
    assert third["messages"][-2] == {"role": "assistant", "content": "Первая часть. "}


def test_context_is_summarized_before_request_and_original_history_is_kept(studio):
    gateway = add_gateway(studio)
    cid = new_chat(studio)
    old = ("ABCD " * 2800).rstrip()
    studio.gw.chat_reply = ["Создали out/план.docx."]
    sse(turn(studio, cid, old))
    studio.c.put(f"/api/sections/chat/links/{gateway}", json={"models": ["local-gemma-abl", "tiny"]})
    studio.c.patch(f"/api/chats/{cid}", json={"model": "tiny"})
    studio.gw.models.append({"id": "tiny", "context_length": 8192})
    chat.LIMITS.clear()
    studio.gw.chat_script = [{"text": ["Обсудили риски и записали их в out/план.docx."]}] * 3   # три партии пересказа
    studio.gw.chat_reply = ["Детализация по отделам"]
    events = sse(turn(studio, cid, "Дополни документ по отделам"))
    assert ("recovery", {"stage": "summary"}) in events and events[-1][0] == "done"
    history = messages(studio, cid)
    assert history[0]["content"] == old and history[-1]["content"] == "Детализация по отделам"
    calls = studio.gw.chat_requests[1:]
    assert all("tools" not in request for request in calls[:-1])
    assert all(request["model"] == "tiny" for request in calls)
    current = calls[-1]["messages"][-1]["content"]
    assert "out/план.docx" in current and current.endswith("Дополни документ по отделам") and old not in current


def test_server_answer_is_capped_by_size(studio, monkeypatch):
    """Ответ через сервер — не больше ANSWER_MAX знаков (как PUT ответа браузера): дальше модель не
    читается, прочитанное — ошибкой с пометкой (аудит)."""
    monkeypatch.setattr(chat, "ANSWER_MAX", 25)
    add_gateway(studio); cid = new_chat(studio)
    studio.gw.chat_reply = ["0123456789"] * 50
    events = sse(turn(studio, cid))
    assert events[-1][0] == "error" and "длинный" in events[-1][1]["message"]
    answer = messages(studio, cid)[1]
    assert (answer["status"], answer["content"]) == ("error", ("0123456789" * 3)[:25] + chat.CUT_SIZE)
    assert sum(e == "delta" for e, _ in events) == 3                           # дальше модель не читалась


def test_browser_cuts_a_direct_answer_like_the_server():
    """Прямое подключение (chat.js readBrowser) обрезает ответ тем же пределом и пометкой, что и сервер,
    а PUT принимает обрезанный с пометкой: иначе длинный ответ получал 422 и пропадал."""
    from pathlib import Path
    import re
    js = (Path(chat.__file__).parent / "static" / "js" / "chat.js").read_text(encoding="utf-8")
    consts = {name: value for name, value in re.findall(r"^const (CUT_\w+|ANSWER_MAX) = (.+?);", js, re.M)}
    assert int(consts["ANSWER_MAX"]) == chat.ANSWER_MAX
    for name in ("CUT_SIZE", "CUT_LENGTH"):
        assert json.loads(consts[name].replace("'", '"')) == getattr(chat, name)
    longest = chat.ANSWER_MAX + max(len(chat.CUT_SIZE), len(chat.CUT_LENGTH))
    chat.AnswerIn(content="x" * longest, status="error")


def test_server_answer_is_capped_by_time(studio):
    """Модель шлёт данные, но ответ идёт дольше STREAM_TOTAL_SEC: чтение кончается, прочитанное —
    ошибкой с пометкой (read timeout такую модель не ловит; аудит)."""
    add_gateway(studio); cid = new_chat(studio)
    studio.gw.chat_hold = True
    try:
        with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": "Привет"}) as r:
            events = events_of(r.iter_lines())
            ids = next(events)[1]
            assert next(d for e, d in events if e == "delta") == {"text": "Здрав"}
            stream = chat.STREAMS[ids["assistant_message_id"]]
            stream.deadline = studio.clock[0] - 1                               # срок вышел
            studio.gw.chat_hold = False
            rest = [(e, d) for e, d in events if e != "ping"]
    finally:
        studio.gw.chat_hold = False
    assert rest[-1] == ("error", {"message": "ответ шёл слишком долго — обрезан"})
    assert chat.CUT_TIME == "\n\n[ответ обрезан: шёл дольше 30 минут]"
    answer = messages(studio, cid)[1]
    assert (answer["status"], answer["content"]) == ("error", "Здрав" + chat.CUT_TIME)


def test_gone_listener_gets_no_queue(studio):
    """Клиент ушёл: ответ дочитывается и сохраняется, но события в очередь, которую никто не читает,
    больше не копятся (аудит)."""
    add_gateway(studio); cid = new_chat(studio)
    studio.gw.chat_hold = True
    studio.gw.chat_reply = ["Здрав"] + ["x" * 100] * 20
    try:
        with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": "Привет"}) as r:
            events = events_of(r.iter_lines())
            ids = next(events)[1]
            assert next(d for e, d in events if e == "delta") == {"text": "Здрав"}
            stream = chat.STREAMS[ids["assistant_message_id"]]
        assert stream.listening is False
    finally:
        studio.gw.chat_hold = False
    wait(studio, stream)
    assert stream.queue.qsize() == 0
    answer = messages(studio, cid)[1]
    assert (answer["status"], answer["content"]) == ("done", "Здрав" + "x" * 2000)


def test_stream_the_client_left_before_its_first_chunk_gets_no_queue():
    """Клиент ушёл, пока ответ слал заголовки: тело генератора не начиналось, его finally не сработал
    бы, и очередь ответа копилась бы без читателя (ревью)."""
    st = chat.StreamTask(user_id=1, chat_id="c", message_id="m", conn=None, model="x", messages=[])

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        raise OSError("клиент ушёл")

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"}, "method": "POST", "headers": []}
    try:
        asyncio.run(chat._EventStream(st, "u")(scope, receive, send))
    except Exception:                                  # noqa: BLE001 — Starlette зовёт это ClientDisconnect
        pass
    assert st.listening is False
    st.put("delta", "никто не прочтёт")
    assert st.queue.qsize() == 0


def test_user_streams_are_limited(studio):
    """Не больше USER_STREAMS ответов через сервер у пользователя сразу: следующий ход — 429, в
    диалоге ничего не записано (аудит)."""
    add_gateway(studio)
    for n in range(chat.USER_STREAMS):                    # идущие ответы других диалогов
        other = new_chat(studio)
        _row(other, f"u{n}", "user", "?", "done", 1)
        _row(other, f"a{n}", "assistant", "", "streaming", 2)
    cid = new_chat(studio)
    r = turn(studio, cid)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "streams"
    assert messages(studio, cid) == [] and not studio.gw.chat_requests
    state.DB.execute("UPDATE messages SET status = 'done' WHERE id = 'a0'")
    assert sse(turn(studio, cid))[-1] == ("done", {"status": "done"})


# ---- контекст ----------------------------------------------------------------

def test_document_goes_in_full_before_the_question(studio):
    add_gateway(studio); cid = new_chat(studio)
    doc = attach(studio, "договор.pdf", pdf("Payment is due in 30 days", "Penalty 1% per day"), "application/pdf")
    assert {k: doc[k] for k in ("kind", "name", "chars")} == {"kind": "document", "name": "договор.pdf", "chars": 45}
    sse(turn(studio, cid, QUESTION, attachment_ids=[doc["id"]]))
    sent = studio.gw.chat_requests[0]["messages"][1:]
    assert sent == [{"role": "user", "content": '<document name="договор.pdf">\n'
                                                "Payment is due in 30 days\n\nPenalty 1% per day\n"
                                                "</document>\n\n" + QUESTION}]
    user = messages(studio, cid)[0]
    assert user["content"] == QUESTION
    assert user["attachments"] == [{"id": doc["id"], "kind": "document", "name": "договор.pdf",
                                    "media_id": doc["media_id"], "chars": 45}]
    assert studio.c.get(f"/api/media/{doc['media_id']}").content.startswith(b"%PDF")
    # вложение уже привязано: второй ход с ним — отказ
    r = turn(studio, cid, "ещё", attachment_ids=[doc["id"]])
    assert r.status_code == 404 and r.json()["detail"]["code"] == "gone"


def test_too_big_document_is_refused_before_saving(studio):
    """Документ больше контекста модель получает выдержками (knowledge.py, test_knowledge.py), а сообщение больше
    контекста — 413 до записи хода, и вложение остаётся неотправленным."""
    gw = add_gateway(studio); cid = new_chat(studio)
    # предел модели вне CONTEXT — из списка моделей подключения
    studio.c.put(f"/api/sections/chat/links/{gw}", json={"models": ["local-gemma-abl", "tiny"]})
    assert studio.c.patch(f"/api/chats/{cid}", json={"model": "tiny"}).json()["model"] == "tiny"
    studio.gw.models.append({"id": "tiny", "context_length": 8192})
    doc = attach(studio, "note.txt", b"word " * 100, "text/plain")
    r = turn(studio, cid, "а" * 30_000, attachment_ids=[doc["id"]])         # ≈ 10 тыс. токенов
    assert r.status_code == 413
    assert r.json()["detail"] == {"code": "context", "message": (
        "Не помещается в контекст модели: ~10 тыс. токенов при пределе 8 тыс. "
        "Уберите документ или начните новый диалог.")}
    assert messages(studio, cid) == [] and not studio.gw.chat_requests
    assert studio.c.get(f"/api/chats/{cid}").json()["chat"]["title"] is None
    unbound = state.DB.execute("SELECT message_id FROM attachments WHERE id = ?", (doc["id"],)).fetchone()
    assert unbound["message_id"] is None
    # документ больше контекста модели (400 тыс. знаков ≈ 133 тыс. токенов при 131 тыс.) — выдержками, ход проходит
    assert studio.c.patch(f"/api/chats/{cid}", json={"model": "local-gemma-abl"}).status_code == 200
    book = attach(studio, "book.txt", b"word " * 80_000, "text/plain")
    sse(turn(studio, cid, QUESTION, attachment_ids=[book["id"]]))
    assert '<document name="book.txt" excerpts="true">' in studio.gw.chat_requests[-1]["messages"][-1]["content"]


def test_model_list_for_the_context_limit_is_remembered(studio):
    """Предел модели вне CONTEXT — из списка моделей подключения, запомненного на CONTEXT_TTL_SEC (у OpenRouter
    список — сотни КБ на каждый ход): ходы подряд — один GET; после срока — снова; неудача не запоминается."""
    import httpx
    gw = add_gateway(studio)
    studio.c.put(f"/api/sections/chat/links/{gw}", json={"models": ["local-gemma-abl", "tiny"]})
    cid = new_chat(studio, connection_id=gw, model="tiny")
    studio.gw.models.append({"id": "tiny", "context_length": 8192})
    listed, fail = [], []

    async def models(request):
        listed.append(True)
        if fail:
            return httpx.Response(fail.pop())
        return await studio.gw.builtin(request)

    studio.gw.on["/v1/models"] = models
    sse(turn(studio, cid, "раз"))
    r = turn(studio, cid, "а" * 15_000)
    assert r.status_code == 413 and "при пределе 8 тыс." in r.json()["detail"]["message"]
    sse(turn(studio, cid, "два"))
    assert len(listed) == 1
    studio.clock[0] += chat.CONTEXT_TTL_SEC
    sse(turn(studio, cid, "три"))
    assert len(listed) == 2
    studio.clock[0] += chat.CONTEXT_TTL_SEC
    fail.append(503)                                         # не узнали — ход без проверки, и спросим снова
    r = turn(studio, cid, "а" * 15_000)
    assert r.status_code == 200 and len(listed) == 3
    sse(r)
    r = turn(studio, cid, "а" * 15_000)
    assert r.status_code == 413 and len(listed) == 4
    assert len(chat.LIMITS) == 1


@pytest.mark.parametrize("entry,limit", [
    ({"id": "qwen-27b", "max_model_len": 262144}, 262144),                    # vLLM
    ({"id": "qwen-27b", "max_input_tokens": 98304}, 98304),                   # LiteLLM
    ({"id": "qwen-27b", "context_length": 4096, "max_model_len": 262144}, 4096),
    ({"id": "qwen-27b", "context_length": True, "max_model_len": 0, "max_input_tokens": 98304}, 98304),
])
def test_context_limit_from_vllm_and_litellm_model_lists(studio, entry, limit):
    """Найдено вживую 2026-11-22: vLLM шлёт предел полем `max_model_len`, LiteLLM — `max_input_tokens`, не
    `context_length`; кольцо контекста (`/estimate`) и проверка 413 берут первое годное из connections.CONTEXT_FIELDS."""
    gw = add_gateway(studio)
    studio.c.put(f"/api/sections/chat/links/{gw}", json={"models": ["local-gemma-abl", "qwen-27b"]})
    cid = new_chat(studio, connection_id=gw, model="qwen-27b")
    studio.gw.models.append(entry)
    r = studio.c.post(f"/api/chats/{cid}/estimate", json={"text": ""})
    assert r.status_code == 200 and r.json()["limit"] == limit


def test_too_long_turn_is_refused_by_vllm_max_model_len(studio):
    gw = add_gateway(studio)
    studio.c.put(f"/api/sections/chat/links/{gw}", json={"models": ["local-gemma-abl", "qwen-27b"]})
    cid = new_chat(studio, connection_id=gw, model="qwen-27b")
    studio.gw.models.append({"id": "qwen-27b", "max_model_len": 8192})
    r = turn(studio, cid, "а" * 30_000)                                      # ≈ 10 тыс. токенов
    assert r.status_code == 413 and r.json()["detail"]["code"] == "context"
    assert "при пределе 8 тыс." in r.json()["detail"]["message"] and not studio.gw.chat_requests


def test_images_go_as_image_url_parts_up_to_four(studio):
    add_gateway(studio); cid = new_chat(studio)
    pics = [attach(studio, f"photo{i}.jpg", png(), "image/jpeg") for i in range(4)]
    assert {p["kind"] for p in pics} == {"image"} and all(p["chars"] == 0 for p in pics)
    sse(turn(studio, cid, "Что на фото?", attachment_ids=[p["id"] for p in pics]))
    (content,) = [m["content"] for m in studio.gw.chat_requests[0]["messages"][1:]]
    assert content[0] == {"type": "text", "text": "Что на фото?"}
    assert [part["type"] for part in content[1:]] == ["image_url"] * 4
    assert all(part["image_url"]["url"].startswith("data:image/png;base64,") for part in content[1:])
    est = chat.estimate_tokens(studio.gw.chat_requests[0]["messages"][1:])
    assert est == 4 + 4000                                                       # ceil(12 / 3) + 1000 × 4


def test_photo_is_recognized_by_content_and_refusals_name_photos(studio):
    """02:F11: JPEG с расширением `.jfif` и PNG без расширения — фото; GIF и HEIC — отказ, называющий и фото."""
    add_gateway(studio)
    jpeg = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(jpeg, "JPEG")
    for name, data in (("scan.jfif", jpeg.getvalue()), ("снимок", png())):
        out = attach(studio, name, data)
        assert out["kind"] == "image" and out["name"] == name
    gif = io.BytesIO()
    Image.new("RGB", (8, 8)).save(gif, "GIF")
    for name, data in (("photo.gif", gif.getvalue()), ("IMG_0001.HEIC", b"\0\0\0\x18ftypheic")):
        r = studio.c.post("/api/attachments", files={"file": (name, data, "image/heic")})
        assert r.status_code == 422 and r.json()["detail"]["message"] == documents.UNSUPPORTED


@pytest.mark.integration  # восемь файлов и два полных хода: > 1 с под нагрузкой набора
def test_images_are_capped_across_the_whole_request(studio):
    """Два хода по четыре фото с ответом между ними: предел подключения (`profiles.DEFAULT_MAX_IMAGES`) — на весь
    запрос, а не на сообщение (аудит). Картинки — последнего хода, фото прежнего — пометкой, в истории — все."""
    add_gateway(studio); cid = new_chat(studio)
    for n in range(2):
        pics = [attach(studio, f"p{n}{i}.jpg", png(), "image/jpeg") for i in range(4)]
        sse(turn(studio, cid, f"Ход {n}", attachment_ids=[p["id"] for p in pics]))
    sent = studio.gw.chat_requests[1]["messages"][1:]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    dropped = {"type": "text", "text": chat.DROPPED.format(profiles.DEFAULT_MAX_IMAGES)}
    assert sent[0]["content"] == [{"type": "text", "text": "Ход 0"}] + [dropped] * 4
    assert [p["type"] for p in sent[2]["content"]] == ["text"] + ["image_url"] * profiles.DEFAULT_MAX_IMAGES
    assert [len(m["attachments"]) for m in messages(studio, cid)] == [4, 0, 4, 0]
    # прямое подключение получает тот же собранный контекст
    assert sum(p["type"] == "image_url" for m in chat.compose(cid, messages(studio, cid)[2]["id"])
               if isinstance(m["content"], list) for p in m["content"]) == profiles.DEFAULT_MAX_IMAGES


def test_direct_turn_returns_messages_and_accepts_the_answer(studio):
    add_connection(studio); cid = new_chat(studio)
    r = turn(studio, cid, "Привет")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["url"] == "https://cloud.test/v1/chat/completions" and body["model"] == "local-gemma-abl"
    assert body["messages"][1:] == [{"role": "user", "content": "Привет"}]
    assert not studio.gw.requests                                                # сервер модель не зовёт
    aid, uid_ = body["assistant_message_id"], body["user_message_id"]
    assert messages(studio, cid)[1]["status"] == "streaming"
    url = f"/api/chats/{cid}/messages/{aid}"
    assert studio.c.put(url, json={"content": "Здравствуйте!", "status": "done"}).status_code == 200
    answer = messages(studio, cid)[1]
    assert (answer["content"], answer["status"]) == ("Здравствуйте!", "done")
    r = studio.c.put(url, json={"content": "другое", "status": "done"})
    assert r.status_code == 409
    assert studio.c.put(f"/api/chats/{cid}/messages/{uid_}", json={"content": "x", "status": "done"}).status_code == 409
    assert studio.c.put(url, json={"content": "x", "status": "streaming"}).status_code == 422
    # следующий ход видит сохранённый ответ браузера
    assert turn(studio, cid, "Как дела?").json()["messages"][2] == {"role": "assistant", "content": "Здравствуйте!"}


def test_repeated_direct_answer_put_is_idempotent(studio):
    """Ответ на первый PUT потерялся, страница повторяет тот же итог — 200, а не «уже сохранён»; иной итог — 409."""
    add_connection(studio); cid = new_chat(studio)
    aid = turn(studio, cid, "Привет").json()["assistant_message_id"]
    url = f"/api/chats/{cid}/messages/{aid}"
    body = {"content": "Здравствуйте!", "status": "done"}
    first, again = studio.c.put(url, json=body), studio.c.put(url, json=body)
    assert first.status_code == again.status_code == 200 and again.json() == first.json()
    assert studio.c.put(url, json={**body, "status": "error"}).status_code == 409
    assert studio.c.put(url, json={**body, "content": "другое"}).status_code == 409
    answer = messages(studio, cid)[1]
    assert (answer["content"], answer["status"]) == ("Здравствуйте!", "done")


def test_direct_answer_keeps_its_error_words_after_reload(studio):
    """Слова ошибки ответа из браузера («Сервис ответил ошибкой (401)…») хранятся, как у ответа через сервер."""
    add_connection(studio); cid = new_chat(studio)
    aid = turn(studio, cid, "Привет").json()["assistant_message_id"]
    url = f"/api/chats/{cid}/messages/{aid}"
    body = {"content": "", "status": "error", "error": "Сервис ответил ошибкой (401): bad key"}
    assert studio.c.put(url, json=body).status_code == 200
    assert studio.c.put(url, json=body).status_code == 200                       # повтор после обрыва — тот же итог
    answer = messages(studio, cid)[1]
    assert (answer["status"], answer["error"]) == ("error", "Сервис ответил ошибкой (401): bad key")
    assert studio.c.put(url, json={**body, "error": "x" * (chat.ERROR_MAX + 1)}).status_code == 422
    aid = turn(studio, cid, "Ещё").json()["assistant_message_id"]                # у готового ответа слов нет
    studio.c.put(f"/api/chats/{cid}/messages/{aid}", json={"content": "Да", "status": "done", "error": "лишнее"})
    assert messages(studio, cid)[3]["error"] is None


def test_chat_on_a_dropped_address_refuses_before_writing_history(studio):
    """Адрес подключения убрали из списка: ни новый диалог, ни ход не начинаются («токен не выдаётся» из браузера
    был бы непонятен), в диалоге не остаётся сообщения с пустым ответом-ошибкой; раздел «Чат» не готов."""
    add_gateway(studio); cid = new_chat(studio)
    state.DB.execute("UPDATE settings SET value = '[]' WHERE key = 'allowlist'")
    r = turn(studio, cid, "Привет")
    assert r.status_code == 409 and "убран из списка" in r.json()["detail"]["message"], r.text
    assert messages(studio, cid) == []
    r = studio.c.post("/api/chats", json={})
    assert r.status_code == 409 and "убран из списка" in r.json()["detail"]["message"]
    assert studio.c.get("/api/me").json()["capabilities"]["chat"] is False
    assert studio.c.get(f"/api/chats/{cid}").json()["chat"]["dropped"] is True


def test_today_line_names_the_moscow_date():
    assert chat.today_line(1_800_000_000) == "Сегодня пятница, 15 января 2027 года (Москва)."
    # 2026-10-21T21:00:00Z — в Москве уже полночь следующего дня
    assert chat.today_line(1_792_616_400) == "Сегодня четверг, 22 октября 2026 года (Москва)."


def test_today_line_uses_the_selected_zone_at_midnight_and_dst():
    from datetime import datetime, timezone

    def at(iso):
        return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()

    assert chat.today_line(at("2026-10-21T21:00:00"), "America/New_York") == (
        "Сегодня среда, 21 октября 2026 года (America/New_York).")
    assert chat.today_line(at("2026-03-08T04:30:00"), "America/New_York") == (
        "Сегодня суббота, 7 марта 2026 года (America/New_York).")
    assert chat.today_line(at("2026-03-08T07:30:00"), "America/New_York") == (
        "Сегодня воскресенье, 8 марта 2026 года (America/New_York).")


def test_chat_date_matches_the_account_zone_for_server_and_direct_connections(studio):
    studio.clock[0] = 1_792_616_400  # Moscow is already on October 22; New York is still on October 21.
    add_gateway(studio)
    assert studio.c.patch("/api/me", json={"timezone": "America/New_York"}).status_code == 200
    expected = "Сегодня среда, 21 октября 2026 года (America/New_York)."
    sse(turn(studio, new_chat(studio), "Привет"))
    assert studio.gw.chat_requests[-1]["messages"][0]["content"] == expected
    direct = add_connection(studio)
    body = turn(studio, new_chat(studio, connection_id=direct), "Привет").json()
    assert body["messages"][0]["content"] == expected


def test_every_chat_request_starts_with_today(studio):
    today = {"role": "system", "content": chat.today_line(T0)}
    add_gateway(studio); cid = new_chat(studio)
    sse(turn(studio, cid, "Привет"))
    assert studio.gw.chat_requests[0]["messages"] == [today, {"role": "user", "content": "Привет"}]
    sse(turn(studio, cid, "", regenerate=True))                                 # повтор — тоже
    assert studio.gw.chat_requests[1]["messages"] == [today, {"role": "user", "content": "Привет"}]
    # прямое подключение получает сообщения уже с датой
    direct = add_connection(studio)
    body = turn(studio, new_chat(studio, connection_id=direct), "Привет").json()
    assert body["messages"] == [today, {"role": "user", "content": "Привет"}]
    # описание фото — без даты
    assert wait_description(studio, describe(studio).json()["id"])["state"] == "done"
    (desc,) = studio.gw.descriptions
    assert [m["role"] for m in desc["messages"]] == ["user"]


def test_direct_answer_with_a_lone_surrogate_is_refused_in_words(studio):
    """Половина эмодзи (одиночный суррогат \\ud83d) в PUT ответа браузера: json.loads её примет, а
    sqlite3 закодировать не сможет — отказ 422 словами, а не 500; ответ остаётся `streaming`."""
    add_connection(studio); cid = new_chat(studio)
    aid = turn(studio, cid, "Привет").json()["assistant_message_id"]
    r = studio.c.put(f"/api/chats/{cid}/messages/{aid}", content='{"content": "ok \\ud83d", "status": "error"}',
                     headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and r.json()["detail"]["message"], r.text
    assert messages(studio, cid)[1]["status"] == "streaming"


def test_regenerate_keeps_the_previous_answer_as_a_variant(studio):
    """«Ответить заново» не удаляет прежний ответ: он — невыбранный вариант (`messages.unchosen`), страница получает
    `variants` у выбранного. Выбранный вариант (`choose`) — и только он — идёт в следующий ход, поиск, «Поделиться» и
    выгрузку истории; новый ход фиксирует выбор (прочие варианты удалены), «Удалить ход» уносит все варианты."""
    import chat_shares
    import takeout
    add_gateway(studio); cid = new_chat(studio)
    uid = studio.c.get("/api/me").json()["id"]
    studio.gw.chat_reply = ["Первый ", "ответ"]
    sse(turn(studio, cid, "Привет"))
    studio.gw.chat_reply = ["Другой ", "ответ"]
    events = sse(turn(studio, cid, "", regenerate=True))
    msgs = messages(studio, cid)
    assert [(m["role"], m["content"]) for m in msgs] == [("user", "Привет"), ("assistant", "Другой ответ")]
    assert events[0][1]["user_message_id"] == msgs[0]["id"]
    assert studio.gw.chat_requests[1]["messages"][1:] == [{"role": "user", "content": "Привет"}]
    first, second = msgs[1]["variants"]
    assert (first["content"], second["id"]) == ("Первый ответ", msgs[1]["id"])
    assert all("variants" not in v for v in msgs[1]["variants"])

    def found(word):
        return [i["title"] for i in studio.c.post("/api/search", json={"q": word}).json()["items"]]

    assert found("Другой") and not found("Первый")
    url = f"/api/chats/{cid}/messages/{{}}/choose"
    assert studio.c.post(url.format(msgs[0]["id"])).status_code == 409             # не вариант ответа
    assert studio.c.post(url.format(first["id"])).status_code == 204
    msgs = messages(studio, cid)
    assert msgs[1]["id"] == first["id"] and len(msgs[1]["variants"]) == 2
    assert found("Первый") and not found("Другой")
    shared = chat_shares.regular_snapshot(uid, cid)[0]["messages"]
    assert [m["text"] for m in shared] == ["Привет", "Первый ответ"]
    assert [m["content"] for m in takeout._chat(uid, cid, {})["messages"]] == ["Привет", "Первый ответ"]

    sse(turn(studio, cid, "Ещё"))
    assert studio.gw.chat_requests[-1]["messages"][1:3] == [
        {"role": "user", "content": "Привет"}, {"role": "assistant", "content": "Первый ответ"}]
    msgs = messages(studio, cid)
    assert len(msgs) == 4 and "variants" not in msgs[1]
    assert state.DB.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (cid,)).fetchone()[0] == 4
    assert studio.c.post(url.format(first["id"])).status_code == 409             # выбор зафиксирован
    sse(turn(studio, cid, "", regenerate=True))
    assert len(messages(studio, cid)[3]["variants"]) == 2
    assert studio.c.delete(f"/api/chats/{cid}/turns/{msgs[2]['id']}").status_code == 204
    assert state.DB.execute("SELECT COUNT(*) FROM messages WHERE chat_id = ?", (cid,)).fetchone()[0] == 2
    empty = new_chat(studio)
    r = turn(studio, empty, "", regenerate=True)
    assert r.status_code == 409


def test_receipt_edit_links_and_export_files_follow_the_chosen_variant(studio):
    """Ревью R1-9: квитанция хода, ставшего невыбранным, — 409 `request_gone`, после повторного выбора снова
    действует; правка сверяет последний выбранный ответ; адреса для read_page и файлы результатов экспорта — только
    из выбранного варианта."""
    import chat_exports
    add_gateway(studio); cid = new_chat(studio)
    uid = studio.c.get("/api/me").json()["id"]
    r1, r2 = "33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444"
    studio.gw.chat_reply = ["Первый"]
    sse(turn(studio, cid, "Привет", request_id=r1))
    studio.gw.chat_reply = ["Второй"]
    sse(turn(studio, cid, "", regenerate=True, request_id=r2))

    def receipt(key):
        r = studio.c.get(f"/api/chats/{cid}/turns/{key}")
        return r.json()["detail"]["code"] if r.status_code == 409 else r.json()["assistant_message_id"]

    first, second = messages(studio, cid)[1]["variants"]
    assert (receipt(r1), receipt(r2)) == ("request_gone", second["id"])
    for n, variant in ((1, first), (2, second)):
        steps = [{"kind": "search", "query": "q", "results": [{"title": "t", "url": f"https://example.test/{n}",
                                                                "published": ""}], "text": "t"},
                 {"kind": "mcp", "server": "s", "tool": "t", "sid": 1, "stamp": "x",
                  "files": [{"name": f"{n}.txt", "aid": f"a{n}", "mime": "text/plain"}]}]
        state.DB.execute("UPDATE messages SET tools = ? WHERE id = ?", (json.dumps(steps), variant["id"]))
    assert list(chat._known_links(cid).values()) == ["https://example.test/2"]
    assert studio.c.post(f"/api/chats/{cid}/messages/{first['id']}/choose").status_code == 204
    assert (receipt(r1), receipt(r2)) == (first["id"], "request_gone")
    assert list(chat._known_links(cid).values()) == ["https://example.test/1"]
    assert [f[0] for f in chat_exports._plan(uid, cid, "ru")[3]] == ["1.txt"]
    user = messages(studio, cid)[0]["id"]
    stale = turn(studio, cid, "Правка", edit={"id": user, "last_id": second["id"]})
    assert (stale.status_code, stale.json()["detail"]["code"]) == (409, "edit_stale")
    assert sse(turn(studio, cid, "Правка", edit={"id": user, "last_id": first["id"]}))[-1] == ("done", {"status": "done"})


def test_regenerate_drops_an_empty_failed_answer(studio):
    """Ответ-ошибка без единого слова вариантом не остаётся — сравнивать нечего; с текстом — остаётся."""
    add_gateway(studio); cid = new_chat(studio)
    sse(turn(studio, cid, "Привет"))
    state.DB.execute("UPDATE messages SET content = '', status = 'error' WHERE chat_id = ? AND role = 'assistant'",
                     (cid,))
    sse(turn(studio, cid, "", regenerate=True))
    msgs = messages(studio, cid)
    assert len(msgs) == 2 and "variants" not in msgs[1] and msgs[1]["status"] == "done"


def test_title_comes_from_the_first_line(studio):
    add_gateway(studio); cid = new_chat(studio)
    first = "  Вопрос" + "о" * 100 + "\nвторая строка"
    assert studio.c.get(f"/api/chats/{cid}").json()["chat"]["title"] is None
    sse(turn(studio, cid, first))
    sse(turn(studio, cid, "другой вопрос"))
    title = studio.c.get(f"/api/chats/{cid}").json()["chat"]["title"]
    assert title == ("Вопрос" + "о" * 100)[:chat.TITLE_CHARS] and len(title) == 60
    items = studio.c.get("/api/chats").json()["items"]
    assert [(i["id"], i["title"]) for i in items] == [(cid, title)]
    other = new_chat(studio)
    assert [i["id"] for i in studio.c.get("/api/chats").json()["items"]] == [other, cid]   # новые сверху
    r = studio.c.patch(f"/api/chats/{cid}", json={"title": "Своё название"})
    assert r.status_code == 200 and r.json()["title"] == "Своё название"


def test_chats_are_private(studio):
    add_gateway(studio); cid = new_chat(studio)
    sse(turn(studio, cid))
    doc = attach(studio, "a.txt", b"secret", "text/plain")
    make_user(studio, "bob"); signin(studio, "bob")
    gone = [studio.c.get(f"/api/chats/{cid}"), studio.c.patch(f"/api/chats/{cid}", json={"title": "x"}),
            studio.c.delete(f"/api/chats/{cid}"), turn(studio, cid), studio.c.post(f"/api/chats/{cid}/stop")]
    for r in gone:
        assert r.status_code == 404 and r.json()["detail"]["code"] == "gone", r.text
    assert studio.c.get("/api/chats").json() == {"items": []}
    mine = add_connection(studio, name="bob-cloud")
    own = new_chat(studio, connection_id=mine)
    r = turn(studio, own, "x", attachment_ids=[doc["id"]])                     # чужое вложение
    assert r.status_code == 404 and r.json()["detail"]["code"] == "gone"
    owner_conn = state.DB.execute("SELECT id FROM connections WHERE name = 'srvllm'").fetchone()["id"]
    r = studio.c.post("/api/chats", json={"connection_id": owner_conn})        # чужое подключение
    assert r.status_code == 404 and r.json()["detail"]["code"] == "gone"
    assert studio.c.delete(f"/api/chats/{own}").status_code == 204
    assert studio.c.get(f"/api/chats/{own}").status_code == 404


def test_deleting_a_chat_removes_its_files_off_the_loop(studio, env, unlinks):
    add_gateway(studio); cid = new_chat(studio)
    pic = attach(studio, "photo.jpg", png(), "image/jpeg")
    sse(turn(studio, cid, "Что на фото?", attachment_ids=[pic["id"]]))
    assert [p for p in env.media_dir.rglob("*") if p.is_file()]
    assert studio.c.delete(f"/api/chats/{cid}").status_code == 204            # в корзину: файлы на месте
    assert [p for p in env.media_dir.rglob("*") if p.is_file()] and not unlinks
    assert studio.c.request("DELETE", f"/api/trash/chats/{cid}", json={"password": "password-123"}).status_code == 204      # окончательно
    assert [p for p in env.media_dir.rglob("*") if p.is_file()] == []
    assert state.DB.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 0
    assert unlinks and not any(unlinks)                  # файлы удалены не с цикла событий


def on_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def test_photos_are_read_off_the_loop(studio, monkeypatch):
    """Каждый ход перечитывает фото диалога с шары: не на цикле событий (ревью A2-I3)."""
    import pathlib
    add_gateway(studio); cid = new_chat(studio)
    pic = attach(studio, "photo.jpg", png(), "image/jpeg")
    reads, real = [], pathlib.Path.read_bytes

    def spy(self):
        reads.append(on_loop())
        return real(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", spy)
    sse(turn(studio, cid, "Что на фото?", attachment_ids=[pic["id"]]))
    assert reads and not any(reads)
    url = studio.gw.chat_requests[0]["messages"][1]["content"][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")


def test_missing_photo_becomes_a_note_not_a_broken_chat(studio, env):
    """Фото, которого нет на шаре, не ломает диалог навсегда (ревью A2-M9)."""
    add_gateway(studio); cid = new_chat(studio)
    pic = attach(studio, "photo.jpg", png(), "image/jpeg")
    sse(turn(studio, cid, "Что на фото?", attachment_ids=[pic["id"]]))
    for p in env.media_dir.rglob("*"):
        if p.is_file():
            p.unlink()
    assert sse(turn(studio, cid, "А теперь?"))[-1] == ("done", {"status": "done"})
    first = studio.gw.chat_requests[1]["messages"][1]["content"]
    assert [part["type"] for part in first] == ["text", "text"] and "фото" in first[1]["text"]


def test_unreadable_share_marks_the_answer(studio, env, monkeypatch):
    """Шара не отдаёт фото: ход — 503 «хранилище недоступно», ответ в диалоге — ошибка, а не вечная загрузка."""
    import pathlib
    add_gateway(studio); cid = new_chat(studio)
    pic = attach(studio, "photo.jpg", png(), "image/jpeg")
    real = pathlib.Path.read_bytes

    def broken(self):
        if env.media_dir in self.parents:
            raise PermissionError("share went away")
        return real(self)

    monkeypatch.setattr(pathlib.Path, "read_bytes", broken)
    r = turn(studio, cid, "Что на фото?", attachment_ids=[pic["id"]])
    assert r.status_code == 503 and r.json()["detail"] == {"code": "storage", "message": "хранилище недоступно"}
    msgs = messages(studio, cid)
    assert [(m["role"], m["status"]) for m in msgs] == [("user", "done"), ("assistant", "error")]
    assert (msgs[1]["content"], msgs[1]["error"]) == ("", "хранилище недоступно") and not studio.gw.chat_requests
    assert not chat.STREAMS


def _during_photo_read(studio, monkeypatch, action):
    """Ход с фото, а пока он читает фото в потоке (поток ответа ещё не запущен) — action(cid)."""
    add_gateway(studio); cid = new_chat(studio)
    pic = attach(studio, "photo.jpg", png(), "image/jpeg")
    real, answers = chat._read_photos, []

    def racing(paths):                               # поток чтения фото: цикл свободен
        answers.append(action(cid).status_code)
        return real(paths)

    monkeypatch.setattr(chat, "_read_photos", racing)
    return cid, turn(studio, cid, "Что на фото?", attachment_ids=[pic["id"]]), answers


def test_stop_while_the_turn_reads_its_photos_stops_the_answer(studio, monkeypatch):
    """«Стоп», пока ход читает фото: ответ остановлен, модель не зовётся (ревью N2)."""
    cid, r, answers = _during_photo_read(studio, monkeypatch, lambda cid: studio.c.post(f"/api/chats/{cid}/stop"))
    assert answers == [204]
    events = sse(r)
    assert [e for e, _ in events] == ["turn", "done"] and events[-1] == ("done", {"status": "stopped"})
    assert messages(studio, cid)[-1]["status"] == "stopped"
    assert not studio.gw.chat_requests and not chat.STREAMS


def test_chat_deleted_while_the_turn_reads_its_photos_gets_no_model_call(studio, monkeypatch):
    """Диалог удалили, пока ход читает фото: модели ход уже не зовёт (ревью N2)."""
    _, r, answers = _during_photo_read(studio, monkeypatch, lambda cid: studio.c.delete(f"/api/chats/{cid}"))
    assert answers == [204] and r.status_code == 404
    assert not studio.gw.chat_requests and not chat.STREAMS


def test_history_wipe_while_the_turn_reads_its_photos_gets_no_model_call(studio, monkeypatch):
    """Стирание истории, пока ход читает фото: модели ход уже не зовёт (ревью N2)."""
    _, r, answers = _during_photo_read(studio, monkeypatch, lambda cid: erase_history(studio))
    assert answers == [200] and r.status_code == 409
    assert r.json()["detail"]["code"] == "history_changed"
    assert not studio.gw.chat_requests and not chat.STREAMS


def _row(cid, mid, role, content, status, created):
    state.DB.execute("INSERT INTO messages(id, chat_id, role, content, status, via, model, created)"
                     " VALUES (?, ?, ?, ?, ?, 'server', 'm', ?)", (mid, cid, role, content, status, created))


def test_compose_merges_user_messages_left_without_an_answer(studio):
    """Q1 → (остановлен до первого слова) → Q2: модели — одно сообщение пользователя, роли чередуются
    (шаблоны Gemma иначе отказывают; ревью A2-I5). Картинок в слитом — не больше предела `max_images`."""
    import media
    add_gateway(studio); cid = new_chat(studio)
    uid = state.DB.execute("SELECT user_id FROM chats WHERE id = ?", (cid,)).fetchone()[0]
    _row(cid, "u1", "user", "Первый", "done", 1)
    _row(cid, "a1", "assistant", "", "stopped", 2)
    _row(cid, "u2", "user", "Второй", "done", 3)
    _row(cid, "a2", "assistant", "сбой", "error", 4)
    _row(cid, "u3", "user", "Третий", "done", 5)
    assert chat.compose(cid, "u3") == [{"role": "user", "content": "Первый\n\nВторой\n\nТретий"}]
    _row(cid, "a3", "assistant", "Ответ", "done", 6)
    _row(cid, "u4", "user", "", "done", 7)
    _row(cid, "a4", "assistant", "", "stopped", 8)
    _row(cid, "u5", "user", "Что на них?", "done", 9)
    for n, mid in enumerate(["u4"] * 3 + ["u5"] * 3):
        media_id = media.save(uid, png(), "image/png", f"{n}.png")
        state.DB.execute("INSERT INTO attachments(id, user_id, message_id, kind, name, media_id, created)"
                         " VALUES (?, ?, ?, 'image', ?, ?, ?)", (f"att{n}", uid, mid, f"{n}.png", media_id, 10 + n))
    out = chat.compose(cid, "u5")
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    last = out[2]["content"]
    assert last[0] == {"type": "text", "text": "Что на них?"}
    assert [p["type"] for p in last[1:]] == ["text", "text"] + ["image_url"] * profiles.DEFAULT_MAX_IMAGES
    assert {p["text"] for p in last[1:3]} == {chat.DROPPED.format(profiles.DEFAULT_MAX_IMAGES)}
    last = chat.compose(cid, "u5", max_images=1)[2]["content"]
    assert [p["type"] for p in last[1:]] == ["text"] * 5 + ["image_url"]
    assert {p["text"] for p in last[1:6]} == {chat.DROPPED.format(1)}


def test_photo_refused_by_the_model_does_not_lock_the_dialog(studio):
    """02:F4: фото ушло модели без зрения — шлюз 400, ответ-ошибка без слова. Следующий ход той же модели фото не
    несёт (пометка REFUSED), «Ответить заново» — несёт, другая модель — тоже."""
    add_gateway(studio); cid = new_chat(studio)

    async def blind(request):
        body = json.loads(request.content)
        if any(isinstance(m["content"], list) and any(p["type"] == "image_url" for p in m["content"])
               for m in body["messages"]):
            studio.gw.chat_requests.append(body)
            return httpx.Response(400, json={"error": {"message": "model does not support image input"}})
        return await studio.gw.builtin(request)

    studio.gw.on["/v1/chat/completions"] = blind
    pic = attach(studio, "кот.jpg", png(), "image/jpeg")
    events = sse(turn(studio, cid, "Что на фото?", attachment_ids=[pic["id"]]))
    assert events[-1][0] == "error"
    sse(turn(studio, cid, "Тогда просто привет"))
    sent = studio.gw.chat_requests[-1]["messages"][1:]
    assert sent == [{"role": "user", "content": chat.REFUSED.format("кот.jpg") + "\nЧто на фото?\n\nТогда просто привет"}]
    assert [m["status"] for m in messages(studio, cid)] == ["done", "error", "done", "done"]
    # ход, чьё фото отклонено, переспрошен: «Ответить заново» снова несёт его фото
    other = new_chat(studio)
    pic = attach(studio, "пёс.jpg", png(), "image/jpeg")
    sse(turn(studio, other, "А тут?", attachment_ids=[pic["id"]]))
    sse(turn(studio, other, "", regenerate=True))
    assert [p["type"] for p in studio.gw.chat_requests[-1]["messages"][-1]["content"]] == ["text", "image_url"]
    # другой модели — фото снова: отказала только та, что ответила ошибкой
    last_user = messages(studio, cid)[-2]["id"]
    assert [p["type"] for p in chat.compose(cid, last_user, "other-model")[-1]["content"]] == ["text", "image_url"]
    assert chat.compose(cid, last_user, "local-gemma-abl") == sent


def test_new_turn_ends_a_browser_answer_nobody_saves(studio):
    """Вкладка с прямым ответом закрылась (признака жизни нет дольше ALIVE_SEC): следующий ход
    помечает его ошибкой, а не вечной загрузкой (ревью A2-M11); `via` в сообщении отличает ответ
    браузера от потока сервера."""
    add_connection(studio); cid = new_chat(studio)
    first = turn(studio, cid, "Привет").json()
    assert {m["via"] for m in messages(studio, cid)} == {"browser"}
    studio.clock[0] += chat.ALIVE_SEC
    assert messages(studio, cid)[1]["live"] is False
    assert turn(studio, cid, "Ты тут?").status_code == 200
    statuses = {m["id"]: m["status"] for m in messages(studio, cid)}
    assert statuses[first["assistant_message_id"]] == "error"
    late = studio.c.put(f"/api/chats/{cid}/messages/{first['assistant_message_id']}",
                        json={"content": "поздно", "status": "done"})
    assert late.status_code == 409


def test_browser_answer_another_tab_still_reads_is_live(studio):
    """Две вкладки: ответ, который читает первая (шлёт признак жизни), вторая видит идущим, а не
    «прервано», и её новый ход ждёт его, а не помечает ошибкой (ревью B1-M4)."""
    add_connection(studio); cid = new_chat(studio)
    first = turn(studio, cid, "Привет").json()
    aid = first["assistant_message_id"]
    alive = f"/api/chats/{cid}/messages/{aid}/alive"
    assert messages(studio, cid)[1]["live"] is True
    for _ in range(3):                                        # вкладка читает дольше ALIVE_SEC
        studio.clock[0] += chat.ALIVE_SEC - 1
        assert studio.c.post(alive).status_code == 204
    assert messages(studio, cid)[1]["live"] is True
    busy = turn(studio, cid, "Ты тут?")
    assert busy.status_code == 409 and "в другой вкладке" in busy.json()["detail"]["message"]
    assert [m["status"] for m in messages(studio, cid)] == ["done", "streaming"]
    url = f"/api/chats/{cid}/messages/{aid}"
    assert studio.c.put(url, json={"content": "Здравствуйте!", "status": "done"}).status_code == 200
    assert studio.c.post(alive).status_code == 409             # ответ сохранён — вкладка перестаёт слать
    assert messages(studio, cid)[1]["live"] is False
    assert turn(studio, cid, "Ты тут?").status_code == 200
    user = messages(studio, cid)[0]["id"]
    assert studio.c.post(f"/api/chats/{cid}/messages/{user}/alive").status_code == 409
    make_user(studio, "bob"); signin(studio, "bob")
    assert studio.c.post(alive).status_code == 404                # чужой диалог


def test_history_wipe_refuses_a_turn_started_during_it(studio, monkeypatch):
    """Ход из другой вкладки во время удаления файлов не запускает модель."""
    import media
    add_gateway(studio); cid = new_chat(studio)
    attach(studio, "a.txt", b"x", "text/plain")                 # файл есть: стирание идёт в поток
    real, responses = media.remove_files, []

    def racing(rows):                                         # поток стирания: цикл свободен
        if not responses:
            responses.append(studio.c.post(f"/api/chats/{cid}/turns", json={"text": "Привет"}))
        return real(rows)

    monkeypatch.setattr(media, "remove_files", racing)
    assert erase_history(studio).status_code == 200
    assert len(responses) == 1 and responses[0].status_code == 409
    assert responses[0].json()["detail"]["code"] == "history_changed"
    assert not studio.gw.chat_requests and not chat.STREAMS
    assert studio.c.get("/api/chats").json() == {"items": []}


# ---- замена подключения во всех диалогах -------------------------------------------

def test_move_chats_takes_only_own_listed_chats_of_one_connection(studio, monkeypatch):
    """POST /api/chats/move: свои диалоги без подключения (удалено) — одним запросом на пару раздела «Чат»; корзина,
    чужие и диалоги другого подключения не трогаются; неотмеченная модель и чужое подключение — отказ без перемен;
    диалог с MCP на прямую модель не переходит (`kept`)."""
    gw = add_gateway(studio)
    cloud = add_connection(studio, name="Облако", chat_models=("cloud-chat",))
    gone, kept, trashed, mcp = (new_chat(studio, connection_id=cloud, model="cloud-chat") for _ in range(4))
    stay = new_chat(studio)
    model = studio.c.get(f"/api/chats/{stay}").json()["chat"]["model"]
    assert studio.c.delete(f"/api/chats/{trashed}").status_code == 204
    other = make_user(studio, "other")
    state.DB.execute("INSERT INTO chats(id, user_id, connection_id, model, created, updated) VALUES ('x', ?, NULL, 'm', 0, 0)",
                     (other,))
    assert studio.c.delete(f"/api/connections/{cloud}").status_code == 204
    refused = studio.c.post("/api/chats/move", json={"connection_id": gw, "model": "no-such-model"})
    assert refused.status_code == 422
    assert studio.c.post("/api/chats/move", json={"connection_id": cloud, "model": "cloud-chat"}).status_code == 404
    second = add_connection(studio, name="Прямое", chat_models=("cloud-chat",))
    state.DB.execute("UPDATE chats SET mcp_ids = '[7]' WHERE id = ?", (mcp,))
    monkeypatch.setattr(chat.mcp_client, "present", lambda uid, raw: [7] if raw == "[7]" else [])
    r = studio.c.post("/api/chats/move", json={"connection_id": second, "model": "cloud-chat"})
    assert r.status_code == 200 and r.json() == {"moved": 2, "kept": 1}
    r = studio.c.post("/api/chats/move", json={"from_connection_id": None, "connection_id": gw, "model": model})
    assert r.json() == {"moved": 1, "kept": 0}                 # MCP через сервер — можно
    rows = {r["id"]: (r["connection_id"], r["model"]) for r in state.DB.execute("SELECT * FROM chats")}
    assert rows[gone] == rows[kept] == (second, "cloud-chat") and rows[mcp] == (gw, model)
    assert rows[trashed] == (None, "cloud-chat") and rows["x"] == (None, "m") and rows[stay] == (gw, model)
    r = studio.c.post("/api/chats/move", json={"from_connection_id": second, "connection_id": gw, "model": model})
    assert r.json() == {"moved": 2, "kept": 0}


# ---- неотправленные вложения ---------------------------------------------------

def _media_count() -> int:
    return state.DB.execute("SELECT COUNT(*) FROM media").fetchone()[0]


def test_unsent_attachment_is_deleted_with_its_file(studio, env):
    add_gateway(studio); cid = new_chat(studio)
    doc = attach(studio, "a.txt", b"draft", "text/plain")
    assert studio.c.delete(f"/api/attachments/{doc['id']}").status_code == 204
    assert _media_count() == 0 and [p for p in env.media_dir.rglob("*") if p.is_file()] == []
    assert studio.c.delete(f"/api/attachments/{doc['id']}").status_code == 404
    sent = attach(studio, "b.txt", b"sent", "text/plain")
    sse(turn(studio, cid, "вот", attachment_ids=[sent["id"]]))
    r = studio.c.delete(f"/api/attachments/{sent['id']}")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "sent" and _media_count() == 1
    mine = attach(studio, "c.txt", b"mine", "text/plain")
    make_user(studio, "bob"); signin(studio, "bob")
    assert studio.c.delete(f"/api/attachments/{mine['id']}").status_code == 404
    assert _media_count() == 2


def test_sweep_drops_attachments_left_unsent_for_a_day(studio, env):
    add_gateway(studio); cid = new_chat(studio)
    old = attach(studio, "old.txt", b"old", "text/plain")
    sent = attach(studio, "sent.txt", b"sent", "text/plain")
    sse(turn(studio, cid, "вот", attachment_ids=[sent["id"]]))
    studio.clock[0] += chat.ORPHAN_SEC + 1
    fresh = attach(studio, "fresh.txt", b"fresh", "text/plain")
    studio.c.portal.call(chat.sweep_attachments)
    left = {r["id"] for r in state.DB.execute("SELECT id FROM attachments")}
    assert left == {sent["id"], fresh["id"]} and _media_count() == 2
    assert studio.c.get(f"/api/media/{old['media_id']}").status_code == 404


def test_sweep_drops_empty_untitled_chats_older_than_a_day(studio):
    """02:F19: диалог, под который первый ход так и не прошёл, уходит уборкой; названный человеком, с сообщениями и
    свежий — остаются."""
    add_gateway(studio)
    empty, named, talked = new_chat(studio), new_chat(studio), new_chat(studio)
    assert studio.c.patch(f"/api/chats/{named}", json={"title": "Мой план"}).status_code == 200
    sse(turn(studio, talked))
    studio.clock[0] += chat.ORPHAN_SEC + 1
    fresh = new_chat(studio)
    studio.c.portal.call(chat.sweep_attachments)
    assert {i["id"] for i in studio.c.get("/api/chats").json()["items"]} == {named, talked, fresh}
    assert empty not in {r[0] for r in state.DB.execute("SELECT id FROM chats")}
    # страница убирает пустой сразу (`?empty=1`), а диалог с сообщениями так не удалить
    assert studio.c.delete(f"/api/chats/{fresh}?empty=1").status_code == 204
    r = studio.c.delete(f"/api/chats/{talked}?empty=1")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "not_empty"
    assert len(messages(studio, talked)) == 2


def test_history_wipe_stops_the_users_streams(studio):
    """Стирание истории посреди ответа: поток через сервер останавливается, карта свободна (ревью A2-M1)."""
    add_gateway(studio); cid = new_chat(studio)
    studio.gw.chat_hold = True
    try:
        with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": "Привет"}) as r:
            events = events_of(r.iter_lines())
            assert next(events)[0] == "turn"
            assert next(d for e, d in events if e == "delta") == {"text": "Здрав"}
            (stream,) = chat.STREAMS.values()
        assert erase_history(studio).status_code == 200
        assert stream.task.done() and not chat.STREAMS
    finally:
        studio.gw.chat_hold = False
    assert studio.c.get("/api/chats").json() == {"items": []}


@pytest.mark.file_db
def test_graceful_shutdown_saves_the_partial_answer_as_interrupted(studio):
    """Остановка службы посреди ответа (stop_all): прочитанное сохраняется с пометкой о перезапуске."""
    add_gateway(studio); cid = new_chat(studio)
    studio.gw.chat_hold = True
    try:
        with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": "Привет"}) as r:
            events = events_of(r.iter_lines())
            assert next(events)[0] == "turn"
            assert next(d for e, d in events if e == "delta") == {"text": "Здрав"}
        studio.c.__exit__(None, None, None)
    finally:
        studio.gw.chat_hold = False
    studio.c.__enter__()
    answer = messages(studio, cid)[1]
    assert (answer["status"], answer["content"]) == ("error", "Здрав\n\n[прервано перезапуском студии]")


def test_a_lone_surrogate_in_the_model_stream_does_not_hang_the_answer(studio):
    """Одиночный суррогат UTF-16 (`\\ud800` в JSON потока) SQLite не кодирует: текст модели очищается на входе
    (одиночный — U+FFFD, пара — свой символ), ответ сохраняется и уходит из STREAMS, следующий ход не 409 busy."""
    import httpx
    add_gateway(studio); cid = new_chat(studio)

    def line(delta, finish=None):              # ensure_ascii — как у шлюзов: суррогаты экранированы
        return "data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"
    body = (line({"reasoning_content": "мысль\udc00"}) + line({"content": "При\ud800вет "}) +
            line({"content": "😀"}) + line({}, "stop") + "data: [DONE]\n\n")

    async def reply(request):
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})
    studio.gw.on["/v1/chat/completions"] = reply
    assert sse(turn(studio, cid))[-1] == ("done", {"status": "done"})
    answer = messages(studio, cid)[1]
    assert (answer["status"], answer["content"]) == ("done", "При�вет \U0001f600")
    assert not chat.STREAMS
    studio.gw.on.clear()
    assert sse(turn(studio, cid, "Ещё"))[-1] == ("done", {"status": "done"})


def test_one_failing_answer_does_not_stop_the_others_on_shutdown(studio, monkeypatch):
    """Остановка службы: итог одного ответа не записался — остальные всё равно сохраняются, STREAMS пуст."""
    add_gateway(studio)
    first, second = new_chat(studio), new_chat(studio)
    studio.gw.chat_hold = True
    real = chat._finish

    def broken(message_id, chat_id, *args, **kwargs):
        if chat_id == first:
            raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")
        return real(message_id, chat_id, *args, **kwargs)
    try:
        for cid in (first, second):
            with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": "Привет"}) as r:
                events = events_of(r.iter_lines())
                assert next(d for e, d in events if e == "delta") == {"text": "Здрав"}
        monkeypatch.setattr(chat, "_finish", broken)
        studio.c.portal.call(chat.stop_all)
    finally:
        studio.gw.chat_hold = False
    assert not chat.STREAMS
    assert messages(studio, second)[1]["status"] == "error"
    assert messages(studio, second)[1]["content"].endswith("[прервано перезапуском студии]")


def test_restart_marks_server_streams_interrupted(studio):
    add_gateway(studio); cid = new_chat(studio)
    for mid, via in (("a" * 32, "server"), ("b" * 32, "browser")):
        state.DB.execute("INSERT INTO messages(id, chat_id, role, content, status, via, model, created)"
                         " VALUES (?, ?, 'assistant', 'Частичный ответ', 'streaming', ?, 'm', 1)", (mid, cid, via))
    chat.recover()
    rows = {r["id"]: (r["status"], r["content"]) for r in state.DB.execute("SELECT * FROM messages")}
    assert rows["a" * 32] == ("error", "Частичный ответ\n\n[прервано перезапуском студии]")
    assert rows["b" * 32] == ("streaming", "Частичный ответ")


def test_chat_text_never_reaches_the_log(studio, caplog):
    caplog.set_level(logging.DEBUG)
    add_gateway(studio); cid = new_chat(studio)
    doc = attach(studio, "тайный-договор.txt", "Секретный пункт о неустойке".encode(), "text/plain")
    bad = studio.c.post("/api/attachments", files={"file": ("личное.exe", b"MZ", "application/octet-stream")})
    assert bad.status_code == 422 and "личное" not in bad.text
    sse(turn(studio, cid, QUESTION, attachment_ids=[doc["id"]]))
    studio.gw.chat_error = (500, {"error": {"message": "boom"}})
    sse(turn(studio, cid, "Второй секретный вопрос"))
    for secret in (QUESTION, "Второй секретный вопрос", "Здравствуйте", "тайный-договор", "Секретный пункт",
                   "личное", TOKEN):
        assert secret not in caplog.text, secret
    assert caplog.records, "служба ничего не пишет в журнал"


# ---- расшифровка в чат -----------------------------------------------------------

TRANSCRIPT_TEXT = ("Расшифровка «Звонок Ивану.m4a» · 01:01\n\n"
                   "Иван · 00:00\nЗдравствуйте.\n\nСобеседник 2 · 00:03\nДобрый день.\n")


def done_transcript(s) -> str:
    from test_transcripts import upload, wait_tr
    tid = upload(s).json()["id"]
    wait_tr(s, tid, {"done"})
    return tid


def from_transcript(s, tid):
    return s.c.post(f"/api/transcripts/{tid}/attachments")


def test_transcript_becomes_an_unsent_document_with_its_edits(studio, fake_audio):
    add_gateway(studio); tid = done_transcript(studio)
    studio.c.patch(f"/api/transcripts/{tid}", json={"names": {"Собеседник 1": "Иван"}})
    r = from_transcript(studio, tid)
    assert r.status_code == 201, r.text
    a = r.json()
    assert {k: a[k] for k in ("kind", "name", "chars", "text")} == {
        "kind": "document", "name": "Звонок Ивану.txt", "chars": len(TRANSCRIPT_TEXT), "text": TRANSCRIPT_TEXT}
    assert a["tokens"] == chat.estimate_tokens([{"role": "user", "content": TRANSCRIPT_TEXT}])
    assert studio.c.get(f"/api/media/{a['media_id']}").content.decode() == TRANSCRIPT_TEXT
    row = state.DB.execute("SELECT message_id, source_transcript_id FROM attachments WHERE id = ?", (a["id"],)).fetchone()
    assert tuple(row) == (None, tid)
    cid = new_chat(studio)
    sse(turn(studio, cid, "Итоги?", attachment_ids=[a["id"]]))
    assert studio.gw.chat_requests[0]["messages"][1:] == [{"role": "user", "content": (
        f'<document name="Звонок Ивану.txt">\n{TRANSCRIPT_TEXT}\n</document>\n\nИтоги?')}]
    assert messages(studio, cid)[0]["attachments"][0]["source"] == {"transcript_id": tid, "exists": True}
    assert studio.c.delete(f"/api/transcripts/{tid}").status_code == 204
    # запись в корзине — ссылка «к записи» вела бы на 404; «Вернуть» — снова есть
    assert messages(studio, cid)[0]["attachments"][0]["source"] == {"transcript_id": tid, "exists": False}
    assert studio.c.post(f"/api/trash/transcripts/{tid}/restore").status_code == 204
    assert messages(studio, cid)[0]["attachments"][0]["source"] == {"transcript_id": tid, "exists": True}
    assert studio.c.delete(f"/api/transcripts/{tid}").status_code == 204
    assert studio.c.request("DELETE", f"/api/trash/transcripts/{tid}", json={"password": "password-123"}).status_code == 204
    assert messages(studio, cid)[0]["attachments"][0]["source"] == {"transcript_id": tid, "exists": False}
    assert studio.c.get(f"/api/media/{a['media_id']}").status_code == 200       # вложение — своим файлом
    doc = attach(studio, "a.txt", b"x", "text/plain")
    sse(turn(studio, cid, "ещё", attachment_ids=[doc["id"]]))
    assert "source" not in messages(studio, cid)[2]["attachments"][0]


def test_transcript_attachment_is_dropped_like_any_unsent_one(studio, fake_audio):
    add_gateway(studio); tid = done_transcript(studio)
    gone = from_transcript(studio, tid).json()
    assert studio.c.delete(f"/api/attachments/{gone['id']}").status_code == 204
    assert studio.c.get(f"/api/media/{gone['media_id']}").status_code == 404
    left = from_transcript(studio, tid).json()
    studio.clock[0] += chat.ORPHAN_SEC + 1
    studio.c.portal.call(chat.sweep_attachments)
    assert state.DB.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
    assert studio.c.get(f"/api/media/{left['media_id']}").status_code == 404
    assert studio.c.get(f"/api/transcripts/{tid}").json()["state"] == "done"      # запись — на месте


def test_transcript_attachment_is_own_done_and_fits(studio, fake_audio, monkeypatch):
    import documents
    gw = add_gateway(studio); tid = done_transcript(studio)
    uid = state.DB.execute("SELECT id FROM users").fetchone()[0]
    state.DB.execute("INSERT INTO transcripts(id, user_id, model, name, source_media_id, state, detail, created)"
                     " VALUES ('w1', ?, 'whisperx', 'a.m4a', 'm', 'working', 'распознаю', 0)", (uid,))
    r = from_transcript(studio, "w1")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "running"
    # предел контекста — у отправки, как у документа
    studio.c.put(f"/api/sections/chat/links/{gw}", json={"models": ["local-gemma-abl", "local-tiny"]})
    studio.gw.models.append({"id": "local-tiny", "context_length": 4200})
    cid = new_chat(studio, connection_id=gw, model="local-tiny")
    a = from_transcript(studio, tid).json()
    r = turn(studio, cid, "а" * 900, attachment_ids=[a["id"]])
    assert r.status_code == 413 and r.json()["detail"]["code"] == "context"
    monkeypatch.setattr(documents, "TEXT_MAX_CHARS", 50)
    r = from_transcript(studio, tid)
    assert r.status_code == 413 and r.json()["detail"]["code"] == "too_big"
    assert state.DB.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 1
    make_user(studio, "bob"); signin(studio, "bob")
    r = from_transcript(studio, tid)
    assert r.status_code == 404 and r.json()["detail"]["code"] == "gone"


def test_transcript_goes_to_a_cloud_model_only_when_confirmed(studio, fake_audio, caplog):
    caplog.set_level(logging.DEBUG)
    gw = add_gateway(studio); tid = done_transcript(studio)
    studio.c.put(f"/api/sections/chat/links/{gw}", json={"models": ["local-gemma-abl", "gemma4-abl"]})
    cid = new_chat(studio, connection_id=gw, model="gemma4-abl")
    a = from_transcript(studio, tid).json()
    r = turn(studio, cid, "Итоги?", attachment_ids=[a["id"]])
    assert r.status_code == 409 and r.json()["detail"] == {
        "code": "external", "message": "Расшифровка уйдёт во внешний сервис «srvllm» — подтвердите"}
    assert messages(studio, cid) == [] and not studio.gw.chat_requests
    sse(turn(studio, cid, "Итоги?", attachment_ids=[a["id"]], external_ok=True))
    assert studio.gw.chat_requests[0]["model"] == "gemma4-abl"
    doc = attach(studio, "a.txt", b"x", "text/plain")                      # обычный документ — без вопроса
    sse(turn(studio, cid, "ещё", attachment_ids=[doc["id"]]))
    for secret in ("Здравствуйте", "Добрый день", "Звонок", "Итоги?"):
        assert secret not in caplog.text, secret


def test_transcript_in_the_history_asks_again_after_the_model_changes(studio, fake_audio):
    """Ход (и «Ответить заново») уносит модели всю историю: расшифровка из прежнего хода к облачной
    модели — тоже только с подтверждением. Подтверждение помнит ответ той же модели после вложения."""
    gw = add_gateway(studio); tid = done_transcript(studio)
    studio.c.put(f"/api/sections/chat/links/{gw}", json={"models": ["local-gemma-abl", "gemma4-abl", "chat"]})
    cid = new_chat(studio, connection_id=gw, model="local-gemma-abl")
    a = from_transcript(studio, tid).json()
    sse(turn(studio, cid, "Итоги?", attachment_ids=[a["id"]]))                 # локальная — без вопроса
    view = studio.c.get(f"/api/chats/{cid}").json()["chat"]
    assert (view["local"], view["external"]) == (True, False)
    view = studio.c.patch(f"/api/chats/{cid}", json={"model": "gemma4-abl"}).json()
    assert (view["local"], view["external"]) == (False, True)
    for body in ({"text": "ещё"}, {"regenerate": True}):
        r = studio.c.post(f"/api/chats/{cid}/turns", json=body)
        assert r.status_code == 409 and r.json()["detail"]["code"] == "external", body
    assert len(studio.gw.chat_requests) == 1 and len(messages(studio, cid)) == 2   # отказ ничего не тронул
    sse(studio.c.post(f"/api/chats/{cid}/turns", json={"regenerate": True, "external_ok": True}))
    assert studio.gw.chat_requests[-1]["model"] == "gemma4-abl"
    assert studio.c.get(f"/api/chats/{cid}").json()["chat"]["external"] is False
    sse(turn(studio, cid, "ещё"))                                              # подтверждено для этой модели
    sse(studio.c.post(f"/api/chats/{cid}/turns", json={"regenerate": True}))
    studio.c.patch(f"/api/chats/{cid}", json={"model": "chat"})                # другая облачная — снова вопрос
    r = turn(studio, cid, "и ещё")
    assert r.status_code == 409 and r.json()["detail"]["code"] == "external"
    studio.c.patch(f"/api/chats/{cid}", json={"model": "local-gemma-abl"})
    sse(turn(studio, cid, "и ещё"))
    # обычный диалог без расшифровки — без вопроса и с признаками в ответе
    plain = studio.c.post("/api/chats", json={"connection_id": gw, "model": "chat"}).json()
    assert (plain["local"], plain["external"]) == (False, False)
    sse(turn(studio, plain["id"], "Привет"))


def test_new_chat_takes_the_sections_default(studio):
    cid = add_gateway(studio)
    studio.c.put(f"/api/sections/chat/links/{cid}", json={"models": ["local-gemma-abl", "gemma4-abl"]})
    studio.c.put("/api/sections/chat/default", json={"connection_id": cid, "model": "gemma4-abl"})
    assert studio.c.post("/api/chats", json={}).json()["model"] == "gemma4-abl"
    assert studio.c.post("/api/chats", json={"connection_id": cid, "model": "other"}).status_code == 422
