"""Отправка в Open WebUI через Студию (спека 2026-11-22-studio-open-webui-design, «Отправка», «Поток на страницу»,
«Push»): новый чат и продолжение — серверный ход `POST /api/chat/completions` с `session_id` сокета Студии, очередь,
стоп, решение паузы, вложения с ожиданием обработки, SSE учётки и push по концу и паузе хода из Студии."""
import asyncio
import json
import logging
import uuid
from urllib.parse import quote

import pytest

import auth
import ntfy
import owui
import owui_link
import push
import state
from conftest import make_user, signin
from fake_owui import FakeOwui, attach
from http_checks import on_http

BASE = "/api/owui/accounts"


@pytest.fixture
def setup(studio, monkeypatch):
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    fake = attach(studio)
    monkeypatch.setattr(owui_link, "CONNECT", fake.socket_connect)
    r = studio.c.post(BASE, json={"label": "s06", "url": FakeOwui.URL, "email": FakeOwui.EMAIL,
                                  "password": FakeOwui.PASSWORD})
    assert r.status_code == 201, r.text
    fake.add_model("m1")
    return studio, fake, r.json()["id"], uid


@pytest.fixture
def pushes(monkeypatch):
    """Отправки push (настоящий `notify_later` с его `on_screen`, без сети): (uid, слова, ссылка, метка, заголовок, skip)."""
    sent = []

    async def record(user_id, body, href, tag, *, title=push.TITLES, skip=frozenset()):
        sent.append((user_id, body, href, tag, title, skip))

    async def quiet(*args, **kwargs):
        return None

    monkeypatch.setattr(push, "notify", record)
    monkeypatch.setattr(ntfy, "notify", quiet)
    return sent


async def until(check, what: str = "условие"):
    for _ in range(3000):
        if check():
            return
        await asyncio.sleep(0.001)
    raise AssertionError(f"не дождались: {what}")


def ev(kind: str, data) -> dict:
    return {"type": kind, "data": data}


def talk(fake) -> str:
    return fake.add_chat("Talk", models=["m1"], messages=[
        {"id": "u1", "parentId": None, "role": "user", "content": "hi", "timestamp": 1},
        {"id": "a1", "parentId": "u1", "role": "assistant", "content": "hello", "model": "m1", "done": True,
         "timestamp": 2}])


def sse(text: str) -> list[tuple[str, dict]]:
    found = []
    for block in text.split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if not line.startswith(":"))
        if "event" in lines:
            found.append((lines["event"], json.loads(lines["data"])))
    return found


def test_new_chat_sends_server_side_turn(setup):
    s, fake, aid, _ = setup
    r = s.c.post(f"{BASE}/{aid}/chats", json={"model": "m1", "text": "hello there"})
    assert r.status_code == 201, r.text
    sent = fake.completions[-1]
    assert sent["parent_id"] is None and "chat_id" not in sent
    assert sent["session_id"] == fake.socket.sid
    assert sent["background_tasks"] == {"title_generation": True}
    assert sent["stream"] is True and sent["model"] == "m1"
    assert sent["messages"] == [{"role": "user", "content": "hello there"}]
    message = sent["user_message"]
    assert uuid.UUID(message["id"]) and uuid.UUID(sent["id"]) and message["id"] != sent["id"]
    assert (message["role"], message["content"], message["parentId"], message["childrenIds"], message["models"]) == (
        "user", "hello there", None, [], ["m1"])
    assert isinstance(message["timestamp"], int)
    chat_id = next(iter(fake.chats))
    assert r.json() == {"chat_id": chat_id, "message_id": sent["id"]}
    assert owui_link.LINKS[aid].turns == {sent["id"]: chat_id}          # ответ держит соединение до итога


def test_continue_sends_only_the_new_message(setup):
    s, fake, aid, _ = setup
    chat = talk(fake)
    r = s.c.post(f"{BASE}/{aid}/chats/{chat}/messages", json={"model": "m1", "text": "and more", "parent_id": "a1"})
    assert r.status_code == 200, r.text
    sent = fake.completions[-1]
    assert sent["messages"] == [{"role": "user", "content": "and more"}]
    assert sent["chat_id"] == chat and sent["parent_id"] == "a1" and sent["user_message"]["parentId"] == "a1"
    assert "title_generation" not in sent.get("background_tasks", {})
    assert r.json() == {"message_id": sent["id"], "queued": False}
    assert s.c.post(f"{BASE}/{aid}/chats/nope/messages",
                    json={"model": "m1", "text": "x", "parent_id": "a1"}).status_code == 404
    assert s.c.post(f"{BASE}/{aid}/chats/{chat}/messages",
                    json={"model": "agent:x", "text": "x", "parent_id": "a1"}).json()["detail"]["code"] == "model"
    assert s.c.post(f"{BASE}/{aid}/chats/{chat}/messages",
                    json={"model": "m1", "text": "  ", "parent_id": "a1"}).status_code == 422


def test_continue_keeps_earlier_chat_documents_in_files(setup):
    """Ревью I2: Open WebUI 0.13.4 перезаписывает `chat.files` присланным `files` и ищет по документам только в них —
    продолжение шлёт прежние документы чата (те, что есть в сообщениях ветки) и новые, без повторов, как страница."""
    s, fake, aid, _ = setup
    d1 = {"type": "file", "id": "d1", "url": "d1", "name": "old.pdf", "content_type": "application/pdf"}
    gone = {"type": "file", "id": "gone", "url": "gone", "name": "x.pdf"}     # сообщения с ним уже нет в ветке
    chat = fake.add_chat("Docs", models=["m1"], files=[d1, gone], messages=[
        {"id": "u1", "parentId": None, "role": "user", "content": "read", "files": [d1], "timestamp": 1},
        {"id": "a1", "parentId": "u1", "role": "assistant", "content": "ok", "done": True, "timestamp": 2}])
    r = s.c.post(f"{BASE}/{aid}/chats/{chat}/messages", json={"model": "m1", "text": "more", "parent_id": "a1"})
    assert r.status_code == 200, r.text
    assert fake.completions[-1]["files"] == [d1]
    fake.add_file("d2", b"new doc", "text/plain")
    fake.add_file("p1", b"\x89PNG", "image/png")
    r = s.c.post(f"{BASE}/{aid}/chats/{chat}/messages",
                 json={"model": "m1", "text": "and this", "parent_id": "a1", "files": ["d2", "p1", "d2"]})
    assert r.status_code == 200, r.text
    assert [f["id"] for f in fake.completions[-1]["files"]] == ["d1", "d2"]   # картинка — только в сообщении
    assert [f["id"] for f in fake.chats[chat]["files"]] == ["d1", "d2"]       # и страница Open WebUI их не теряет


def test_early_end_of_answer_does_not_revive_the_turn(setup):
    """Ревью M1: итог ответа пришёл раньше ответа POST — ход снят и после POST не возвращается."""
    s, fake, aid, _ = setup
    served = fake.handler

    async def handler(request):
        response = await served(request)
        if request.url.path == "/api/chat/completions":
            body = json.loads(request.content)
            fake.emit_event(json.loads(response.content)["chat_id"], body["id"], ev("chat:completion", {"done": True}))
            await until(lambda: body["id"] not in owui_link.LINKS[aid].turns, "итог разобран")
        return response

    s.gw.hosts[FakeOwui.ENTRY] = handler
    r = s.c.post(f"{BASE}/{aid}/chats", json={"model": "m1", "text": "x"})
    assert r.status_code == 201, r.text
    assert owui_link.LINKS[aid].turns == {}


def test_approval_mode_full_is_never_sent(setup):
    s, fake, aid, _ = setup
    seen = {}
    for mode in ("full", "ask", "auto", "bogus"):
        fake.user_settings = {"params": {"tool_approval_mode": mode}}
        assert s.c.post(f"{BASE}/{aid}/chats", json={"model": "m1", "text": "x"}).status_code == 201
        seen[mode] = fake.completions[-1].get("params", {}).get("tool_approval_mode")
    assert seen == {"full": None, "ask": "ask", "auto": "auto", "bogus": None}
    fake.user_settings = {}
    assert s.c.post(f"{BASE}/{aid}/chats", json={"model": "m1", "text": "x"}).status_code == 201
    assert "tool_approval_mode" not in fake.completions[-1].get("params", {})
    assert all("full" not in json.dumps(body.get("params", {})) for body in fake.completions)


def test_code_interpreter_dropped_for_pyodide(setup):
    s, fake, aid, _ = setup
    fake.add_model("m2", meta={"toolIds": ["tool-a", "server:b"],
                               "defaultFeatureIds": ["code_interpreter", "web_search", "image_generation"]},
                   image_generation=False)
    fake.config_features = {"enable_code_interpreter": True, "enable_web_search": True,
                            "enable_image_generation": True, "enable_tools": True, "enable_tool_servers": False}
    fake.interpreter_engine = "pyodide"                                    # интерпретатор — в браузере собеседника
    assert s.c.post(f"{BASE}/{aid}/chats", json={"model": "m2", "text": "x"}).status_code == 201
    sent = fake.completions[-1]
    assert "code_interpreter" not in sent["features"]
    assert sent["features"]["web_search"] is True
    assert sent["features"]["image_generation"] is False                   # модель не умеет
    assert sent["tool_ids"] == ["tool-a"]                                  # серверы инструментов выключены
    fake.interpreter_engine = "jupyter"
    assert s.c.post(f"{BASE}/{aid}/chats", json={"model": "m2", "text": "x"}).status_code == 201
    assert fake.completions[-1]["features"]["code_interpreter"] is True
    assert s.c.post(f"{BASE}/{aid}/chats", json={"model": "m1", "text": "x"}).status_code == 201
    assert fake.completions[-1]["features"]["web_search"] is False         # не возможность модели по умолчанию


def test_send_while_answer_runs_reports_queued(setup):
    s, fake, aid, uid = setup
    chat = talk(fake)
    fake.queue_next = True

    async def scenario(client):
        q = await owui_link.watch(owui.load(uid, aid))
        r = await client.post(f"{BASE}/{aid}/chats/{chat}/messages",
                              json={"model": "m1", "text": "one more", "parent_id": "a1"})
        assert r.status_code == 200, r.text
        assert r.json()["queued"] is True
        found = [q.get_nowait() for _ in range(q.qsize())]
        assert {"type": "queued", "chat_id": chat} in found
        assert r.json()["message_id"] not in owui_link.LINKS[aid].turns    # его ответа не будет — не держит сокет
        owui_link.unwatch(aid, q)

    on_http(s, scenario)
    assert fake.completions[-1]["chat_id"] == chat


def test_send_after_revoke_is_409_relogin(setup):
    s, fake, aid, _ = setup
    assert s.c.post(f"{BASE}/{aid}/chats", json={"model": "m1", "text": "x"}).status_code == 201
    sent = len(fake.completions)
    fake.revoke_all()                                                     # сессию «Studio» отозвали в Open WebUI
    r = s.c.post(f"{BASE}/{aid}/chats", json={"model": "m1", "text": "x"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "relogin"
    assert len(fake.completions) == sent
    assert state.DB.execute("SELECT state FROM owui_accounts WHERE id = ?", (aid,)).fetchone()[0] == "relogin"


def test_resolve_answer_and_reject(setup):
    s, fake, aid, _ = setup
    chat = talk(fake)
    label, description = "Yes" * 100, "Do it" * 450
    fake.chats[chat]["messages"]["a1"]["output"] = [{
        "type": "function_call", "name": "ask_user", "call_id": "k1", "status": "pending", "arguments": {
            "questions": [
                {"id": "q1", "question": "Do it?", "options": [{"label": label, "description": description}]},
                {"id": "q2", "question": "How?", "options": []},
            ],
        },
    }]
    answers = {"q1": {"type": "option", "option_index": 0, "label": "display label", "description": "display description"},
               "q2": {"type": "other", "text": "my own words"}}
    url = f"{BASE}/{aid}/chats/{chat}/resolve"
    r = s.c.post(url, json={"message_id": "a1", "call_id": "k1", "action": "answer", "answers": answers})
    assert r.status_code == 204, r.text
    expected = {**answers, "q1": {**answers["q1"], "label": label, "description": description}}
    assert fake.resolved[-1] == (chat, "a1", {"call_id": "k1", "action": "answer", "answers": expected})
    assert owui_link.LINKS[aid].turns == {"a1": chat}                     # после решения ответ снова идёт
    again = s.c.post(url, json={"message_id": "a1", "call_id": "k1", "action": "answer", "answers": answers})
    assert again.status_code == 409 and again.json()["detail"]["code"] == "resolved"
    assert s.c.post(url, json={"message_id": "a1", "call_id": "k2", "action": "reject"}).status_code == 204
    assert fake.resolved[-1] == (chat, "a1", {"call_id": "k2", "action": "reject"})
    assert s.c.post(url, json={"message_id": "a1", "call_id": "k3", "action": "answer"}).status_code == 422
    assert s.c.post(url, json={"message_id": "a1", "call_id": "k3", "action": "answer",
                               "answers": {"q1": {"type": "file", "path": "/etc"}}}).status_code == 422
    assert s.c.post(url, json={"message_id": "a1", "call_id": "k1", "action": "answer",
                               "answers": {**answers, "q1": {"type": "option", "option_index": 1}}}).status_code == 422
    assert len(fake.resolved) == 2


def test_stop_calls_task_stop(setup):
    s, fake, aid, _ = setup
    chat = talk(fake)
    assert s.c.post(f"{BASE}/{aid}/chats/{chat}/stop").status_code == 204
    assert fake.stopped == [chat]
    assert s.c.post(f"{BASE}/{aid}/chats/nope/stop").status_code == 404


def test_upload_waits_for_processing_and_limits_size(setup, monkeypatch):
    s, fake, aid, _ = setup
    assert owui.FILE_MAX == 20 * 1024 * 1024
    monkeypatch.setattr(owui, "FILE_MAX", 1000)
    before = len(fake.requests)
    r = s.c.post(f"{BASE}/{aid}/files", content=b"x" * 1001, headers={"X-Studio-Name": "big.bin"})
    assert r.status_code == 413 and r.json()["detail"]["code"] == "too_large"
    assert len(fake.requests) == before                                   # к Open WebUI не ходили
    fake.processing = ["pending", "processing", "completed"]
    sleeps = len(s.sleeps)
    r = s.c.post(f"{BASE}/{aid}/files", content=b"report body",
                 headers={"X-Studio-Name": quote("отчёт.txt"), "Content-Type": "text/plain"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"id": body["id"], "name": "отчёт.txt", "kind": "file"}
    assert fake.files[body["id"]] == ("text/plain", b"report body") and fake.file_names[body["id"]] == "отчёт.txt"
    assert s.sleeps[sleeps:] == [owui.FILE_POLL_SEC, owui.FILE_POLL_SEC]  # ждал, пока Open WebUI обработает
    sleeps = len(s.sleeps)
    picture = s.c.post(f"{BASE}/{aid}/files", content=b"\x89PNG", headers={"X-Studio-Name": "p.png",
                                                                          "Content-Type": "image/png"}).json()
    # картинку страница Open WebUI грузит без обработки (`process=false`) и не ждёт: её статус так и остаётся
    # `pending` — опрос ждал бы FILE_WAIT_SEC и отвечал 504 (живая приёмка 2026-11-22: обработка картинки — `failed`)
    assert picture == {"id": picture["id"], "name": "p.png", "kind": "image"}
    upload = next(r for r in reversed(fake.requests) if r.method == "POST" and r.url.path == "/api/v1/files/")
    assert upload.url.params.get("process") == "false" and s.sleeps[sleeps:] == []
    assert not any(r.url.path == f"/api/v1/files/{picture['id']}/process/status" for r in fake.requests)
    # вложения уходят с сообщением: картинка — только в сообщении, документ — ещё и в `files` запроса
    assert s.c.post(f"{BASE}/{aid}/chats", json={"model": "m1", "text": "see", "files": [body["id"], picture["id"]]}
                    ).status_code == 201
    sent = fake.completions[-1]
    assert [f["id"] for f in sent["user_message"]["files"]] == [body["id"], picture["id"]]
    assert sent["user_message"]["files"][1]["content_type"] == "image/png"
    assert [f["id"] for f in sent["files"]] == [body["id"]]
    assert s.c.post(f"{BASE}/{aid}/chats", json={"model": "m1", "text": "x", "files": ["missing"]}).status_code == 404
    assert s.c.post(f"{BASE}/{aid}/files", content=b"", headers={"X-Studio-Name": "e.txt"}).status_code == 422


def test_only_unsent_studio_uploads_can_be_discarded(setup):
    s, fake, aid, _ = setup
    url = f"{BASE}/{aid}/files"

    def upload(name):
        response = s.c.post(url, content=b"attachment", headers={"X-Studio-Name": name, "Content-Type": "image/png"})
        assert response.status_code == 200, response.text
        return response.json()["id"]

    fake.add_file("external", b"same upstream account", "image/png")
    assert s.c.delete(f"{url}/external").status_code == 404
    discarded = upload("discard.png")
    assert s.c.delete(f"{url}/{discarded}").status_code == 204
    assert discarded not in fake.files
    sent = upload("keep.png")
    assert s.c.post(f"{BASE}/{aid}/chats", json={"model": "m1", "text": "see", "files": [sent]}).status_code == 201
    assert s.c.delete(f"{url}/{sent}").status_code == 404
    assert set(fake.files) == {"external", sent}
    assert [r.url.path for r in fake.requests if r.method == "DELETE"] == [f"/api/v1/files/{discarded}"]


def test_events_stream_relays_normalized_events(setup, monkeypatch):
    s, fake, aid, uid = setup
    monkeypatch.setattr(owui, "PING_SEC", 0.02)

    async def scenario(client):
        task = asyncio.create_task(client.get(f"{BASE}/{aid}/events"))
        await until(lambda: aid in owui_link.LINKS and owui_link.LINKS[aid].watchers
                    and owui_link.LINKS[aid].shown == "up", "наблюдатель на связи")
        await asyncio.sleep(owui.PING_SEC * 3)                            # без событий — пинг-комментарий
        fake.emit_event("c1", "m1", ev("response:completion", {"type": "response.output_text.delta", "delta": "Hi"}))
        fake.emit_event("c1", "m1", ev("chat:completion", {"done": True}))
        fake.revoke_all()                                                 # сессию отозвали, Open WebUI рвёт сокет —
        fake.drop()                                                       # после кадров выше, по порядку
        return await task

    r = on_http(s, scenario)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-store"
    assert sse(r.text) == [("link", {"state": "up"}),
                           ("delta", {"chat_id": "c1", "message_id": "m1", "kind": "text", "text": "Hi"}),
                           ("done", {"chat_id": "c1", "message_id": "m1", "error": None}),
                           ("link", {"state": "down"}),
                           ("link", {"state": "relogin"})]                # поток кончается: соединения больше нет
    assert ": ping" in r.text


def test_events_stream_ends_with_studio_session(setup, monkeypatch):
    s, fake, aid, uid = setup
    monkeypatch.setattr(owui, "PING_SEC", 0.02)

    async def scenario(client):
        task = asyncio.create_task(client.get(f"{BASE}/{aid}/events"))
        await until(lambda: aid in owui_link.LINKS and owui_link.LINKS[aid].watchers, "наблюдатель")
        auth.end_sessions(uid)
        r = await task
        assert owui_link.LINKS.get(aid) is None or not owui_link.LINKS[aid].watchers     # наблюдатель снят
        return r

    r = on_http(s, scenario)
    assert sse(r.text)[-1] == ("closed", {})
    assert s.c.get(f"{BASE}/{aid}/events").status_code == 401             # без сессии нового потока нет


def test_push_only_off_screen_and_for_studio_turns(setup, pushes):
    s, fake, aid, uid = setup
    digest = state.DB.execute("SELECT digest FROM sessions WHERE user_id = ?", (uid,)).fetchone()[0]

    async def scenario(client):
        r = await client.post(f"{BASE}/{aid}/chats", json={"model": "m1", "text": "secret prompt"})
        chat, message = r.json()["chat_id"], r.json()["message_id"]
        fake.emit_event(chat, message, ev("response:completion", {"type": "response.output_text.delta",
                                                                  "delta": "secret answer"}))
        fake.emit_event(chat, message, ev("chat:completion", {"done": True}))
        await until(lambda: pushes, "push")
        assert pushes == [(uid, push.CHAT_NOTICES[0], f"owui/{aid}/{chat}", message, push.OWUI_TITLE, frozenset())]
        push.screen(digest, True)                                          # страница Студии на экране
        r = await client.post(f"{BASE}/{aid}/chats/{chat}/messages",
                              json={"model": "m1", "text": "again", "parent_id": message})
        second = r.json()["message_id"]
        fake.emit_event(chat, second, ev("chat:message:error", {"error": {"content": "boom"}}))
        await until(lambda: len(pushes) == 2, "второй push")
        assert pushes[1][1] == push.CHAT_NOTICES[1] and digest in pushes[1][5]     # этой сессии не шлётся
        fake.emit_event(chat, "not-from-studio", ev("chat:completion", {"done": True}))
        fake.emit_event(chat, message, ev("chat:completion", {"done": True}))       # уже кончился — повтора нет
        fake.emit_event("c-other", None, ev("chat:title", "T"))
        await until(lambda: owui_link.LINKS[aid].watchers == [] and not any(
            sock._inbox.qsize() for sock in fake.sockets if not sock.closed), "события разобраны")
        await asyncio.sleep(0.01)
        assert len(pushes) == 2

    on_http(s, scenario)
    assert "secret" not in repr(pushes)
    assert push.OWUI_TITLE == "Open WebUI"


def test_pause_pushes_decision_needed(setup, pushes):
    s, fake, aid, uid = setup

    async def scenario(client):
        r = await client.post(f"{BASE}/{aid}/chats", json={"model": "m1", "text": "x"})
        chat, message = r.json()["chat_id"], r.json()["message_id"]
        fake.emit_event(chat, message, ev("chat:active", {"active": True}))
        fake.emit_event(chat, message, ev("chat:completion", {"output": [
            {"type": "function_call", "call_id": "k1", "name": "ask_user", "status": "pending",
             "arguments": json.dumps({"questions": [{"id": "q1", "question": "Private question?",
                                                     "options": [{"label": "A", "description": "B"}]}]})}]}))
        await until(lambda: pushes, "push паузы")
        assert pushes == [(uid, push.DECISION_NOTICE, f"owui/{aid}/{chat}", message, push.OWUI_TITLE, frozenset())]
        assert message not in owui_link.LINKS[aid].turns                  # пауза отпускает сокет

    on_http(s, scenario)
    assert "Private" not in repr(pushes)
    assert push.DECISION_NOTICE == {"ru": "нужно ваше решение", "en": "your decision is needed"}


def test_send_log_has_no_text_or_ids(setup, caplog):
    s, fake, aid, _ = setup
    caplog.set_level(logging.DEBUG, logger="studio")
    upload = s.c.post(f"{BASE}/{aid}/files", content=b"data", headers={"X-Studio-Name": "private-name.txt"}).json()
    r = s.c.post(f"{BASE}/{aid}/chats", json={"model": "m1", "text": "private text", "files": [upload["id"]]})
    chat, message = r.json()["chat_id"], r.json()["message_id"]
    s.c.post(f"{BASE}/{aid}/chats/{chat}/messages", json={"model": "m1", "text": "more private", "parent_id": message})
    s.c.post(f"{BASE}/{aid}/chats/{chat}/stop")
    s.c.post(f"{BASE}/{aid}/chats/{chat}/resolve", json={"message_id": message, "call_id": "k1", "action": "reject"})
    s.c.post(f"{BASE}/{aid}/chats/{chat}/resolve", json={"message_id": message, "call_id": "k1", "action": "reject"})
    log = "\n".join(r.getMessage() for r in caplog.records if r.name == "studio")
    for leaked in ("private", upload["id"], chat, message, FakeOwui.ENTRY, FakeOwui.EMAIL):
        assert leaked not in log
