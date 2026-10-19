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
    add_gateway(studio); cid = new_chat(studio)
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
    studio.c.patch(f"/api/chats/{cid}", json={"model": "tiny"})
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
