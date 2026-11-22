"""Соединение socket.io с Open WebUI (`owui_link`) на поддельном сокете в памяти: события → наблюдатели SSE,
немедленный отказ обратным вызовам, `user-join` после обновления токена, переподключение с растущей паузой,
отзыв сессии → `relogin`, закрытие по простою, версия сервера (спека, «Соединение socket.io», «Поток на страницу»)."""
import asyncio
import json
import logging

import httpx
import pytest
from fastapi import HTTPException

import clock
import owui_link
import owui_tokens
import owui_view
import state
from conftest import make_user
from fake_owui import FakeOwui, attach
from test_owui_tokens import add_account


@pytest.fixture
def setup(studio, monkeypatch):
    user = make_user(studio, "owner")
    fake = attach(studio)
    monkeypatch.setattr(owui_link, "CONNECT", fake.socket_connect)

    async def fast_sleep(seconds):         # пауза двигает часы и только уступает цикл: тест не ждёт и миллисекунд
        studio.sleeps.append(seconds)
        studio.clock[0] += seconds
        await asyncio.sleep(0)

    monkeypatch.setattr(clock, "SLEEP", fast_sleep)
    return studio, fake, add_account(fake, user)


async def until(check, what: str = "условие"):
    """Уступать циклу, пока `check()` не станет истинным (без ожидания по часам)."""
    for _ in range(2000):
        if check():
            return
        await asyncio.sleep(0)
    raise AssertionError(f"не дождались: {what}")


def drain(q: asyncio.Queue) -> list[dict]:
    found = []
    while not q.empty():
        found.append(q.get_nowait())
    return found


def ev(kind: str, data) -> dict:
    return {"type": kind, "data": data}


def links(seen: list[dict]) -> list[str]:
    return [e["state"] for e in seen if e["type"] == "link"]


def test_normalize_maps_probe_events():
    def one(kind, data, chat="c1", message="m1"):
        return owui_view.normalize({"chat_id": chat, "message_id": message, "data": ev(kind, data)})

    assert one("response:completion", {"type": "response.output_text.delta", "delta": "Hi", "item_id": "x"}) == [
        {"type": "delta", "chat_id": "c1", "message_id": "m1", "kind": "text", "text": "Hi"}]
    assert one("response:completion", {"type": "response.reasoning_text.delta", "delta": "hmm"}) == [
        {"type": "delta", "chat_id": "c1", "message_id": "m1", "kind": "reasoning", "text": "hmm"}]
    assert one("chat:completion", {"type": "response.output_text.delta", "delta": "!"})[0]["kind"] == "text"
    assert one("response:completion", {"type": "response.output_item.added", "item": {
        "type": "function_call", "call_id": "k1", "name": "search", "status": "in_progress"}}) == [
        {"type": "item", "chat_id": "c1", "message_id": "m1",
         "tool": {"call_id": "k1", "name": "search", "status": "in_progress"}}]
    assert one("chat:completion", {"done": True, "output": []}) == [
        {"type": "done", "chat_id": "c1", "message_id": "m1", "error": None}]
    assert one("chat:message:error", {"error": {"content": "boom"}, "done": True}) == [
        {"type": "done", "chat_id": "c1", "message_id": "m1", "error": "boom"}]
    assert one("chat:active", {"active": True, "pending_approval": True}) == [
        {"type": "active", "chat_id": "c1", "active": True},
        {"type": "pending", "chat_id": "c1", "message_id": "m1"}]
    assert one("chat:active", {"active": False, "folder_id": None}) == [
        {"type": "active", "chat_id": "c1", "active": False}]
    # пауза 0.13.4: снимок без `done` с вызовом в статусе паузы и без результата
    assert one("chat:completion", {"output": [{"type": "function_call", "call_id": "k2", "name": "ask_user",
                                               "status": "pending"}]}) == [
        {"type": "pending", "chat_id": "c1", "message_id": "m1"}]
    assert one("chat:completion", {"output": [{"type": "function_call", "call_id": "k3", "status": "in_progress"}]}) == []
    assert one("chat:title", "New title") == [{"type": "title", "chat_id": "c1", "title": "New title"}]
    assert one("chat:list", {"chat_id": "c2", "folder_id": "f1"}, chat=None) == [
        {"type": "list", "chat_id": "c2", "folder_id": "f1"}]
    assert one("chat:tags", {"tags": []}) == []
    assert one("response:completion", {"type": "response.created"}) == []
    assert owui_view.normalize({"chat_id": "c1", "data": "text"}) == []
    assert owui_view.normalize(["events"]) == []


def test_watch_opens_link_and_relays_events(setup):
    s, fake, acc = setup

    async def scenario():
        q = await owui_link.watch(acc)
        await until(lambda: not q.empty(), "link up")
        assert drain(q) == [{"type": "link", "state": "up"}]
        assert fake.ws_targets[0].path == "/ws/socket.io/?EIO=4&transport=websocket"
        auth = json.loads(fake.received[0][2:])
        assert fake.received[0].startswith("40") and auth == {"token": owui_tokens.TOKENS[acc.id][0]}
        assert fake.socket.user == fake.user_id
        fake.emit_event("c1", "m1", ev("response:completion", {"type": "response.output_text.delta", "delta": "Hi"}))
        fake.emit_event("c1", None, ev("chat:tags", {}))                  # неизвестное — молча
        fake.emit_event("c1", "m1", ev("chat:completion", {"done": True}))
        await until(lambda: q.qsize() >= 2, "события")
        assert drain(q) == [{"type": "delta", "chat_id": "c1", "message_id": "m1", "kind": "text", "text": "Hi"},
                            {"type": "done", "chat_id": "c1", "message_id": "m1", "error": None}]
        assert await owui_link.ensure(acc) == fake.socket.sid
        fake.ping()
        await until(lambda: "3" in fake.received, "pong")
        owui_link.unwatch(acc.id, q)
        await owui_link.drop_account(acc.id)
        assert owui_link.LINKS == {} and fake.socket is None

    asyncio.run(scenario())


def test_callbacks_are_refused_at_once(setup):
    s, fake, acc = setup

    async def scenario():
        q = await owui_link.watch(acc)
        await until(lambda: not q.empty(), "link up")
        drain(q)
        fake.call("execute:python", {"code": "print(1)"}, ack=1)
        fake.call("request:elicitation", {"message": "?"}, ack=27)
        await until(lambda: any(f.startswith("4327") for f in fake.received), "отказы")
        refusals = [f for f in fake.received if f.startswith("43")]
        assert [f[:4] for f in refusals] == ["431[", "4327"]
        assert json.loads(refusals[0][3:]) == [{"error": owui_link.REFUSAL}]
        assert json.loads(refusals[1][4:]) == [{"error": owui_link.REFUSAL}]
        assert drain(q) == []                                              # наблюдателям — ничего

    asyncio.run(scenario())


def test_token_refresh_rejoins_socket(setup):
    s, fake, acc = setup

    async def scenario():
        q = await owui_link.watch(acc)
        await until(lambda: not q.empty(), "link up")
        drain(q)
        first = owui_tokens.TOKENS[acc.id][0]
        refreshes = fake.refreshes
        s.clock[0] += 700                                                 # до конца 200 с — рано
        fake.ping()
        await until(lambda: fake.received.count("3") == 1, "pong")
        assert fake.refreshes == refreshes and not [f for f in fake.received if "user-join" in f]
        s.clock[0] += 100                                                 # до конца 100 < 120
        fake.ping()
        await until(lambda: any("user-join" in f for f in fake.received), "user-join")
        fresh = owui_tokens.TOKENS[acc.id][0]
        assert fresh != first and fake.refreshes == refreshes + 1
        join = [json.loads(f[2:]) for f in fake.received if "user-join" in f]
        assert join == [["user-join", {"auth": {"token": fresh}}]]
        assert fake.socket.token == fresh and fake.socket.user == fake.user_id
        assert drain(q) == []                                             # связь не прерывалась

    asyncio.run(scenario())


def test_reconnects_after_drop_and_tells_watchers(setup, caplog):
    s, fake, acc = setup
    caplog.set_level(logging.DEBUG, logger="studio")

    async def scenario():
        q = await owui_link.watch(acc)
        await until(lambda: not q.empty(), "link up")
        first = fake.socket
        fake.drop()
        await until(lambda: len(fake.sockets) == 2 and fake.socket is not None and fake.socket.sid, "второе соединение")
        await until(lambda: links(list(q._queue)) == ["up", "down", "up"], "down, up")
        assert s.sleeps == [1] and first.closed
        drain(q)
        fake.down = True                                                  # сервер пропал: паузы растут до 30 с
        fake.drop()
        await until(lambda: len(s.sleeps) >= 8, "попытки")
        assert s.sleeps[1:8] == [1, 2, 4, 8, 16, 30, 30]
        fake.down = False
        await until(lambda: links(list(q._queue))[-1:] == ["up"], "снова на связи")
        assert links(drain(q)) == ["down", "up"]
        fake.emit_event("c1", "m1", ev("chat:title", "T"))
        await until(lambda: not q.empty(), "событие после переподключения")
        assert drain(q) == [{"type": "title", "chat_id": "c1", "title": "T"}]

    asyncio.run(scenario())
    log = "\n".join(r.getMessage() for r in caplog.records if r.name == "studio")
    assert "owui" in log
    secret = owui_tokens.load(acc.id)
    for leaked in (FakeOwui.ENTRY, "10.77.1.20", secret.refresh_token, owui_tokens.TOKENS[acc.id][0], "c1", "m1"):
        assert leaked not in log


def test_silent_socket_counts_as_drop_and_reconnects(setup):
    """Ревью M3: сервер молчит дольше `pingInterval + pingTimeout` из пакета открытия (здесь 20 мс вместо 45 с) —
    соединение оборвано, наблюдателям `down`, переподключение с паузой."""
    s, fake, acc = setup
    fake.ping_ms = (10, 10)

    async def scenario():
        q = await owui_link.watch(acc)
        for _ in range(2000):                                             # молчание — по настоящему времени
            if len(fake.sockets) >= 2 and fake.sockets[0].closed:
                break
            await asyncio.sleep(0.001)
        assert len(fake.sockets) >= 2 and fake.sockets[0].closed
        assert links(drain(q))[:2] == ["up", "down"] and s.sleeps[0] == 1
        await owui_link.drop_account(acc.id)

    asyncio.run(scenario())


def test_check_refusing_fresh_token_turns_link_into_relogin_without_more_rotations(setup):
    """Ревью I1: проверка `GET /api/v1/auths/` отвергает и только что выданный токен — `relogin`, а не новая ротация
    на каждой попытке переподключения."""
    s, fake, acc = setup
    served = fake.handler

    async def handler(request):
        if request.url.path == "/api/v1/auths/":
            fake.requests.append(request)
            return httpx.Response(401, json={"detail": "Not authenticated"})
        return await served(request)

    s.gw.hosts[FakeOwui.ENTRY] = handler

    async def scenario():
        q = await owui_link.watch(acc)
        await until(lambda: acc.id not in owui_link.LINKS, "соединение закрыто")
        assert links(drain(q)) == ["relogin"]

    asyncio.run(scenario())
    assert fake.refreshes == 2 and fake.sockets == []                    # рабочий токен в память + одно на отказ
    assert state.DB.execute("SELECT state FROM owui_accounts WHERE id = ?", (acc.id,)).fetchone()[0] == "relogin"


def test_revoked_session_turns_link_into_relogin(setup):
    s, fake, acc = setup

    async def scenario():
        q = await owui_link.watch(acc)
        await until(lambda: not q.empty(), "link up")
        sock = fake.socket
        fake.revoke_all()                                                 # сессию «Studio» отозвали в Open WebUI
        fake.drop()                                                       # Open WebUI отключает сокет с отозванным токеном
        await until(lambda: acc.id not in owui_link.LINKS, "соединение закрыто")
        assert links(drain(q))[-1] == "relogin"
        assert sock.closed and fake.socket is None
        assert state.DB.execute("SELECT state FROM owui_accounts WHERE id = ?", (acc.id,)).fetchone()[0] == "relogin"
        with pytest.raises(HTTPException) as e:                           # отправка без сокета — тоже relogin
            await owui_link.ensure(acc)
        assert e.value.status_code == 409 and e.value.detail["code"] == "relogin"

    asyncio.run(scenario())


def test_link_closes_after_idle(setup):
    s, fake, acc = setup

    async def scenario():
        q = await owui_link.watch(acc)
        await until(lambda: not q.empty(), "link up")
        sock = fake.socket
        owui_link.unwatch(acc.id, q)
        s.clock[0] += owui_link.IDLE_CLOSE_SEC - 1
        fake.ping()
        await until(lambda: "3" in sock.received, "pong")
        await asyncio.sleep(0)
        assert acc.id in owui_link.LINKS
        s.clock[0] += 1                                                   # 60 с без причин
        fake.ping()
        await until(lambda: acc.id not in owui_link.LINKS, "закрыто по простою")
        assert sock.closed and len(fake.sockets) == 1

    asyncio.run(scenario())


def test_pause_releases_tracked_turn(setup):
    s, fake, acc = setup

    async def scenario():
        await owui_link.ensure(acc)
        sock = fake.socket
        owui_link.track(acc, "c1", "m1")
        s.clock[0] += 120                                                 # ответ идёт — простоя нет
        fake.ping()
        await until(lambda: sock.received.count("3") == 1, "pong")
        await asyncio.sleep(0)
        assert acc.id in owui_link.LINKS
        fake.emit_event("c1", "m1", ev("chat:active", {"active": True}))
        fake.emit_event("c1", "m1", ev("chat:completion", {"output": [
            {"type": "function_call", "call_id": "k1", "name": "ask_user", "status": "pending"}]}))
        await until(lambda: not owui_link.LINKS[acc.id].turns, "пауза снята с ответа")
        s.clock[0] += owui_link.IDLE_CLOSE_SEC - 1
        fake.ping()
        await until(lambda: sock.received.count("3") == 2, "pong")
        await asyncio.sleep(0)
        assert acc.id in owui_link.LINKS
        s.clock[0] += 1                                                   # 60 с после паузы

        fake.ping()
        await until(lambda: acc.id not in owui_link.LINKS, "закрыто после паузы")
        assert sock.closed

    asyncio.run(scenario())


def test_default_transport_connects_to_target_without_redirects():
    """`owui_link.CONNECT` по умолчанию: на `host:port` цели, `Host` — из цели, путь socket.io, без переадресаций."""
    from websockets.asyncio.server import serve
    from websockets.exceptions import InvalidStatus

    import upstream

    seen = []

    def process_request(connection, request):
        seen.append((request.path, request.headers.get("Host"), request.headers.get("User-Agent")))
        if request.path.startswith("/moved"):
            response = connection.respond(302, "")
            response.headers["Location"] = "/ws/socket.io/?EIO=4&transport=websocket"
            return response
        return None

    async def handler(ws):
        await ws.send("0{}")
        await ws.recv()

    async def scenario():
        async with serve(handler, "127.0.0.1", 0, process_request=process_request) as server:
            port = server.sockets[0].getsockname()[1]

            def target(path):
                return upstream.WsTarget(host="127.0.0.1", port=port, path=path, ssl=None, server_hostname=None,
                                         host_header=f"owui.example.com:{port}")

            transport = await owui_link.CONNECT(target(owui_link.SOCKET_PATH), {"User-Agent": owui_link.USER_AGENT})
            assert await transport.recv() == "0{}"
            await transport.send("40")
            await transport.close()
            with pytest.raises(InvalidStatus):
                await owui_link.CONNECT(target("/moved"), {"User-Agent": owui_link.USER_AGENT})
        assert seen == [(owui_link.SOCKET_PATH, f"owui.example.com:{port}", "Studio"),
                        ("/moved", f"owui.example.com:{port}", "Studio")]          # переадресация не выполнена

    asyncio.run(scenario())


def test_link_records_version_and_refuses_old_server(setup):
    s, fake, acc = setup

    async def scenario():
        await owui_link.ensure(acc)
        assert state.DB.execute("SELECT version FROM owui_accounts WHERE id = ?", (acc.id,)).fetchone()[0] == "0.13.4"
        await owui_link.drop_account(acc.id)
        fake.version = "0.12.9"
        q = await owui_link.watch(acc)
        with pytest.raises(HTTPException) as e:
            await owui_link.ensure(acc)
        assert e.value.status_code == 422 and e.value.detail["code"] == "unsupported"
        assert links(drain(q)) == ["down"]
        assert len(fake.ws_targets) == 1                                  # к старому серверу сокет не открывался

    asyncio.run(scenario())
