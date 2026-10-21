"""Чат (app/chat.py): диалоги, вложения, сборка контекста, поток через сервер для подключений из
списка администратора и передача JSON браузеру для прямых. Шлюз — FakeGateway (`/v1/chat/completions`
потоком OpenAI SSE), часы — clock.NOW, паузы — clock.SLEEP: пинги идут без настоящего ожидания."""
import asyncio
import io
import json
import logging

from PIL import Image

import chat
import state
from conftest import add_connection, add_gateway, make_user, signin
from fakes import TOKEN
from test_documents import pdf

QUESTION = "Что сказано в договоре о сроке оплаты?"


def events_of(lines):
    """События SSE по строкам ответа: (имя, данные); комментарий `: ping` — ("ping", None)."""
    event, data = None, None
    for line in lines:
        if line == ": ping":
            yield "ping", None
        elif line.startswith("event: "):
            event = line[len("event: "):]
        elif line.startswith("data: "):
            data = json.loads(line[len("data: "):])
        elif line == "" and event is not None:
            yield event, data
            event, data = None, None


def sse(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    return list(events_of(response.iter_lines()))


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
    assert studio.gw.chat_requests[0]["messages"] == [{"role": "user", "content": "Привет"}]
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
    events = sse(turn(studio, cid))
    assert events[-1] == ("error", {"message": "ответ обрезан пределом длины модели"})
    answer = messages(studio, cid)[1]
    assert (answer["status"], answer["content"]) == ("error", "Здравствуйте!" + chat.CUT_LENGTH)


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
    sent = studio.gw.chat_requests[0]["messages"]
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
    gw = add_gateway(studio); cid = new_chat(studio)
    doc = attach(studio, "book.txt", b"word " * 80_000, "text/plain")        # 400 тыс. знаков ≈ 133 тыс. токенов
    r = turn(studio, cid, QUESTION, attachment_ids=[doc["id"]])
    assert r.status_code == 413
    assert r.json()["detail"] == {"code": "context", "message": (
        "Не помещается в контекст модели: ~133 тыс. токенов при пределе 131 тыс. "
        "Уберите документ или начните новый диалог.")}
    assert messages(studio, cid) == [] and not studio.gw.chat_requests
    assert studio.c.get(f"/api/chats/{cid}").json()["chat"]["title"] is None
    unbound = state.DB.execute("SELECT message_id FROM attachments WHERE id = ?", (doc["id"],)).fetchone()
    assert unbound["message_id"] is None
    # предел модели вне CONTEXT — из списка моделей подключения
    studio.c.put(f"/api/sections/chat/links/{gw}", json={"models": ["local-gemma-abl", "tiny"]})
    assert studio.c.patch(f"/api/chats/{cid}", json={"model": "tiny"}).json()["model"] == "tiny"
    studio.gw.models.append({"id": "tiny", "context_length": 8192})
    r = turn(studio, cid, "а" * 15_000)
    assert r.status_code == 413 and "при пределе 8 тыс." in r.json()["detail"]["message"]


def test_images_go_as_image_url_parts_up_to_four(studio):
    add_gateway(studio); cid = new_chat(studio)
    pics = [attach(studio, f"photo{i}.jpg", png(), "image/jpeg") for i in range(5)]
    assert {p["kind"] for p in pics} == {"image"} and all(p["chars"] == 0 for p in pics)
    r = turn(studio, cid, "Что на фото?", attachment_ids=[p["id"] for p in pics])
    assert r.status_code == 422 and r.json()["detail"]["code"] == "images"
    assert messages(studio, cid) == []
    sse(turn(studio, cid, "Что на фото?", attachment_ids=[p["id"] for p in pics[:4]]))
    (content,) = [m["content"] for m in studio.gw.chat_requests[0]["messages"]]
    assert content[0] == {"type": "text", "text": "Что на фото?"}
    assert [part["type"] for part in content[1:]] == ["image_url"] * 4
    assert all(part["image_url"]["url"].startswith("data:image/png;base64,") for part in content[1:])
    est = chat.estimate_tokens(studio.gw.chat_requests[0]["messages"])
    assert est == 4 + 4000                                                       # ceil(12 / 3) + 1000 × 4


def test_images_are_capped_across_the_whole_request(studio):
    """Два хода по четыре фото с ответом между ними: предел MAX_IMAGES — на весь запрос, а не на
    сообщение (аудит). Картинки — последнего хода, фото прежнего — пометкой, в истории — все."""
    add_gateway(studio); cid = new_chat(studio)
    for n in range(2):
        pics = [attach(studio, f"p{n}{i}.jpg", png(), "image/jpeg") for i in range(4)]
        sse(turn(studio, cid, f"Ход {n}", attachment_ids=[p["id"] for p in pics]))
    sent = studio.gw.chat_requests[1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[0]["content"] == [{"type": "text", "text": "Ход 0"}] + [{"type": "text", "text": chat.DROPPED}] * 4
    assert [p["type"] for p in sent[2]["content"]] == ["text"] + ["image_url"] * chat.MAX_IMAGES
    assert [len(m["attachments"]) for m in messages(studio, cid)] == [4, 0, 4, 0]
    # прямое подключение получает тот же собранный контекст
    assert sum(p["type"] == "image_url" for m in chat.compose(cid, messages(studio, cid)[2]["id"])
               if isinstance(m["content"], list) for p in m["content"]) == chat.MAX_IMAGES


def test_direct_turn_returns_messages_and_accepts_the_answer(studio):
    add_connection(studio); cid = new_chat(studio)
    r = turn(studio, cid, "Привет")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["url"] == "https://cloud.test/v1/chat/completions" and body["model"] == "local-gemma-abl"
    assert body["messages"] == [{"role": "user", "content": "Привет"}]
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
    assert turn(studio, cid, "Как дела?").json()["messages"][1] == {"role": "assistant", "content": "Здравствуйте!"}


def test_direct_answer_with_a_lone_surrogate_is_refused_in_words(studio):
    """Половина эмодзи (одиночный суррогат \\ud83d) в PUT ответа браузера: json.loads её примет, а
    sqlite3 закодировать не сможет — отказ 422 словами, а не 500; ответ остаётся `streaming`."""
    add_connection(studio); cid = new_chat(studio)
    aid = turn(studio, cid, "Привет").json()["assistant_message_id"]
    r = studio.c.put(f"/api/chats/{cid}/messages/{aid}", content='{"content": "ok \\ud83d", "status": "error"}',
                     headers={"Content-Type": "application/json"})
    assert r.status_code == 422 and r.json()["detail"]["message"], r.text
    assert messages(studio, cid)[1]["status"] == "streaming"


def test_regenerate_replaces_the_last_answer(studio):
    add_gateway(studio); cid = new_chat(studio)
    sse(turn(studio, cid, "Привет"))
    studio.gw.chat_reply = ["Другой ", "ответ"]
    events = sse(turn(studio, cid, "", regenerate=True))
    msgs = messages(studio, cid)
    assert [(m["role"], m["content"]) for m in msgs] == [("user", "Привет"), ("assistant", "Другой ответ")]
    assert events[0][1]["user_message_id"] == msgs[0]["id"]
    assert studio.gw.chat_requests[1]["messages"] == [{"role": "user", "content": "Привет"}]
    empty = new_chat(studio)
    r = turn(studio, empty, "", regenerate=True)
    assert r.status_code == 409


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
    assert studio.c.delete(f"/api/chats/{cid}").status_code == 204
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
    url = studio.gw.chat_requests[0]["messages"][0]["content"][1]["image_url"]["url"]
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
    first = studio.gw.chat_requests[1]["messages"][0]["content"]
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
    assert msgs[1]["content"] == "хранилище недоступно" and not studio.gw.chat_requests
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
    _, r, answers = _during_photo_read(studio, monkeypatch, lambda cid: studio.c.delete("/api/history"))
    assert answers == [200] and r.status_code == 404
    assert not studio.gw.chat_requests and not chat.STREAMS


def _row(cid, mid, role, content, status, created):
    state.DB.execute("INSERT INTO messages(id, chat_id, role, content, status, via, model, created)"
                     " VALUES (?, ?, ?, ?, ?, 'server', 'm', ?)", (mid, cid, role, content, status, created))


def test_compose_merges_user_messages_left_without_an_answer(studio):
    """Q1 → (остановлен до первого слова) → Q2: модели — одно сообщение пользователя, роли чередуются
    (шаблоны Gemma иначе отказывают; ревью A2-I5). Картинок в слитом — не больше MAX_IMAGES."""
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
    assert [p["type"] for p in last[1:]] == ["text", "text"] + ["image_url"] * chat.MAX_IMAGES
    assert {p["text"] for p in last[1:3]} == {chat.DROPPED}


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


def test_history_wipe_stops_a_turn_started_during_it(studio, monkeypatch):
    """Ход без фото, начатый из другой вкладки, пока стирание удаляет файлы: его поток
    останавливается и после удаления диалога, а не держит карту до конца ответа (ревью B2-M7)."""
    import media
    add_gateway(studio); cid = new_chat(studio)
    attach(studio, "a.txt", b"x", "text/plain")                 # файл есть: стирание идёт в поток
    real, streams = media.remove_files, []

    def racing(rows):                                         # поток стирания: цикл свободен
        if not streams:
            with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": "Привет"}) as r:
                events = events_of(r.iter_lines())
                assert next(events)[0] == "turn"
                assert next(d for e, d in events if e == "delta") == {"text": "Здрав"}
                streams.extend(chat.STREAMS.values())
        return real(rows)

    monkeypatch.setattr(media, "remove_files", racing)
    studio.gw.chat_hold = True
    try:
        assert studio.c.delete("/api/history").status_code == 200
        (stream,) = streams
        assert stream.task.done() and not chat.STREAMS
    finally:
        studio.gw.chat_hold = False
    assert studio.c.get("/api/chats").json() == {"items": []}


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
        assert studio.c.delete("/api/history").status_code == 200
        assert stream.task.done() and not chat.STREAMS
    finally:
        studio.gw.chat_hold = False
    assert studio.c.get("/api/chats").json() == {"items": []}


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


def test_new_chat_takes_the_sections_default(studio):
    cid = add_gateway(studio)
    studio.c.put(f"/api/sections/chat/links/{cid}", json={"models": ["local-gemma-abl", "gemma4-abl"]})
    studio.c.put("/api/sections/chat/default", json={"connection_id": cid, "model": "gemma4-abl"})
    assert studio.c.post("/api/chats", json={}).json()["model"] == "gemma4-abl"
    assert studio.c.post("/api/chats", json={"connection_id": cid, "model": "other"}).status_code == 422
