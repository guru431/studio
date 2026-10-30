"""Канал моста Claude Code (app/bridge.py): вход ключом устройства, знакомство, замена канала, просьбы с
ответами, события наблюдателям, закрытие сессий, отзыв; подставная машина tests/fake_bridge.py — по протоколу
docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md."""
import asyncio
import contextlib
import logging
import re
import time

import pytest
from starlette.websockets import WebSocketDisconnect

import auth
import bridge
import security
import state
from conftest import T0, make_user, signin
from fake_bridge import FakeMachine, InProcessBridge, connect, drop, emit, enroll, pid, settle
from test_screenlock import add_key, open_with

CHAT = "0f8e2a52-5d0e-4b8e-9a51-2f3c4d5e6f70"
SESSION = "t" * 32
HELLO = FakeMachine(pin="1234").hello


def link_ws(s, key):
    headers = {} if key is None else {"Authorization": f"Bearer {key}"}
    return s.c.websocket_connect("/api/agent/link", headers=headers)


@contextlib.contextmanager
def online(s, key, hello=HELLO):
    with link_ws(s, key) as ws:
        ws.send_json(hello)
        assert ws.receive_json()["t"] == "welcome"
        yield ws


def closed_with(ws) -> int:
    with pytest.raises(WebSocketDisconnect) as e:
        ws.receive_json()
    return e.value.code


def kinds(uid=None):
    rows = state.DB.execute("SELECT kind FROM agent_events ORDER BY id DESC").fetchall()
    return [r["kind"] for r in rows]


def agents(s):
    return s.c.get("/api/agents").json()["items"]


def start_call(s, aid, op="projects", **args):
    return s.c.portal.start_task_soon(lambda: bridge.call(aid, op, SESSION, **args))


def answer(ws, **res):
    req = ws.receive_json()
    ws.send_json({"t": "res", "id": req["id"], **res})
    return req


def test_link_needs_a_known_key(studio):
    uid = make_user(studio, "owner")
    aid, key = enroll(studio)
    for headers in ({}, {"Authorization": "Bearer nope"}, {"Authorization": key}):
        with studio.c.websocket_connect("/api/agent/link", headers=headers) as ws:
            assert closed_with(ws) == bridge.CLOSE_AUTH
    state.DB.execute("UPDATE users SET disabled = 1 WHERE id = ?", (uid,))
    with link_ws(studio, key) as ws:
        assert closed_with(ws) == bridge.CLOSE_AUTH
    assert kinds() == ["enroll"] and not bridge.LINKS


def test_hello_brings_the_agent_online(studio):
    make_user(studio, "owner")
    aid, key = enroll(studio)
    signin(studio, "owner")
    with link_ws(studio, key) as ws:
        ws.send_json(HELLO)
        assert ws.receive_json() == {"t": "welcome", "agent_id": aid, "name": "Рабочий ПК", "login": "owner"}
        [item] = agents(studio)
        assert {k: item[k] for k in ("online", "has_pin", "cc_version", "projects_root", "models", "efforts",
                                     "last_seen")} == {
            "online": True, "has_pin": True, "cc_version": "2.3.1", "projects_root": "C:\\work\\projects",
            "models": [{"id": "opus", "label": "Opus"}, {"id": "sonnet", "label": "Sonnet"}],
            "efforts": ["low", "high"], "last_seen": T0}
        assert bridge.LINKS[aid].ip == "testclient"
        studio.clock[0] += 60
    [item] = agents(studio)
    assert (item["online"], item["last_seen"], item["has_pin"]) == (False, T0 + 60, True)
    assert (item["models"], item["efforts"]) == ([], [])
    assert kinds() == ["offline", "online", "enroll"] and not bridge.LINKS


def test_bad_hello_or_silence_closes_4400(studio, monkeypatch):
    make_user(studio, "owner")
    _, key = enroll(studio)
    no_models = {k: v for k, v in HELLO.items() if k != "models"}
    for bad in ({**HELLO, "v": 2}, no_models, {**HELLO, "efforts": ["huge"]}, {**HELLO, "t": "res"},
                {**HELLO, "models": [{"id": "a b", "label": "x"}]}, {**HELLO, "has_pin": 1}, [HELLO]):
        with link_ws(studio, key) as ws:
            ws.send_json(bad)
            assert closed_with(ws) == bridge.CLOSE_PROTOCOL, bad
    monkeypatch.setattr(bridge, "HELLO_SEC", 0.05)
    with link_ws(studio, key) as ws:
        assert closed_with(ws) == bridge.CLOSE_PROTOCOL
    assert kinds() == ["enroll"] and not bridge.LINKS


def test_parse_hello_keeps_only_the_protocol_fields():
    assert bridge.parse_hello({**HELLO, "extra": 1, "efforts": ["max", "low", "max"]}) == {
        "cc_version": "2.3.1", "projects_root": "C:\\work\\projects", "has_pin": True,
        "models": [{"id": "opus", "label": "Opus"}, {"id": "sonnet", "label": "Sonnet"}], "efforts": ["max", "low"],
        "images": True, "chat_state": True, "defaults": {"model": "opus", "effort": "high", "access": "auto"}}
    for bad in ({**HELLO, "cc_version": "x" * 65}, {**HELLO, "projects_root": "x" * 1025},
                {**HELLO, "models": [{"id": "m", "label": "x"}] * 51}, {**HELLO, "models": [{"id": "m"}]},
                {**HELLO, "v": True}, {**HELLO, "efforts": "low"}):
        with pytest.raises(bridge.ProtocolError):
            bridge.parse_hello(bad)


def test_new_link_replaces_the_old_one(studio):
    make_user(studio, "owner")
    aid, key = enroll(studio)
    signin(studio, "owner")
    first = online(studio, key)
    ws1 = first.__enter__()
    with online(studio, key) as ws2:
        assert closed_with(ws1) == bridge.CLOSE_REPLACED
        first.__exit__(None, None, None)
        assert agents(studio)[0]["online"] is True
        assert kinds() == ["online", "online", "enroll"]
        fut = start_call(studio, aid)
        answer(ws2, ok=True, data={"items": []})
        assert fut.result(1) == {"items": []}
    assert kinds() == ["offline", "online", "online", "enroll"]


def test_replacement_tells_watchers_offline_and_drops_the_old_links_frames(studio, monkeypatch):
    """Новый канал (4409 прежнему): начатые просьбы — `Offline`, наблюдатели — `offline` (сессии моста закрыты,
    события «дырки» потеряны), а кадры прежнего канала, что ещё дочитываются, — никому и без push."""
    make_user(studio, "owner")
    aid, _ = enroll(studio)
    pushed = []
    monkeypatch.setattr(bridge.push, "notify_later", lambda *a: pushed.append(a))
    old = connect(studio, aid, FakeMachine())

    async def silent(frame):
        old.sent.append(frame)

    monkeypatch.setattr(old, "send", silent)
    fut = start_call(studio, aid)
    q = studio.c.portal.call(bridge.watch, aid, CHAT)
    new = connect(studio, aid, FakeMachine())
    settle(studio)                                   # прежний канал закрывается фоном
    assert isinstance(fut.exception(1), bridge.Offline) and old.closed_with == bridge.CLOSE_REPLACED
    assert studio.c.portal.call(q.get_nowait) == ("offline", None)
    ask = {"k": "ask", "request_id": "r1", "kind": "permission", "tool": "Bash", "input": {}, "always": False}
    emit(studio, old, pid("p"), CHAT, {"k": "text", "text": "старое"})
    assert studio.c.portal.call(q.empty)
    studio.c.portal.call(bridge.unwatch, aid, CHAT, q)
    emit(studio, old, pid("p"), CHAT, ask)           # наблюдателей нет, но и push от прежнего канала нет
    assert pushed == []
    emit(studio, new, pid("p"), CHAT, ask)
    assert len(pushed) == 1
    drop(studio, new)                                # снят — его кадры тоже никуда
    emit(studio, new, pid("p"), CHAT, ask)
    assert len(pushed) == 1


def test_forget_closes_only_the_streams_of_that_session(studio):
    uid = make_user(studio, "owner")
    aid, _ = enroll(studio)
    mine = studio.c.portal.call(bridge.watch, aid, CHAT, bridge.tag("d" * 64))
    other = studio.c.portal.call(bridge.watch, aid, CHAT, bridge.tag("e" * 64))
    studio.c.portal.call(bridge.forget, uid, "d" * 64)
    assert studio.c.portal.call(mine.get_nowait) == ("closed", None)
    assert studio.c.portal.call(other.empty)


def test_revoked_or_disabled_during_hello_is_4401(studio):
    """Ключ верен на входе, а до `hello` компьютер отозвали или учётку отключили: 4401, на связь не встаёт."""
    uid = make_user(studio, "owner")
    aid, key = enroll(studio)
    _, key2 = enroll(studio, name="Ноутбук")
    with link_ws(studio, key) as ws:
        state.DB.execute("DELETE FROM agents WHERE id = ?", (aid,))
        ws.send_json(HELLO)
        assert closed_with(ws) == bridge.CLOSE_AUTH
    with link_ws(studio, key2) as ws:
        state.DB.execute("UPDATE users SET disabled = 1 WHERE id = ?", (uid,))
        ws.send_json(HELLO)
        assert closed_with(ws) == bridge.CLOSE_AUTH
    assert not bridge.LINKS and "online" not in kinds()


def test_call_round_trip_and_errors(studio, monkeypatch):
    make_user(studio, "owner")
    aid, key = enroll(studio)
    with pytest.raises(bridge.Offline):
        studio.c.portal.call(lambda: bridge.call(aid, "projects", SESSION))
    with online(studio, key) as ws:
        fut = start_call(studio, aid, "chats", project=pid("studio"))
        req = answer(ws, ok=True, data={"items": [1]})
        assert req == {"t": "req", "id": req["id"], "op": "chats", "session": SESSION, "project": pid("studio")}
        assert re.fullmatch(r"[0-9a-f]{16}", req["id"])
        assert fut.result(1) == {"items": [1]}
        fut = start_call(studio, aid)
        answer(ws, ok=True, data=[1])
        assert fut.result(1) == {}
        fut = start_call(studio, aid, "open", pin="0000")
        answer(ws, ok=False, code="pin", message="неверный PIN", left=3, version=7, junk=1)
        e = fut.exception(1)
        assert isinstance(e, bridge.BridgeError)
        assert (e.code, e.message, e.extra) == ("pin", "неверный PIN", {"left": 3})
        fut = start_call(studio, aid)
        answer(ws, ok=False, message="м" * 500, version="1.2", left=True)
        e = fut.exception(1)
        assert (e.code, e.message, e.extra) == ("bridge", "м" * bridge.MESSAGE_MAX, {"version": "1.2"})
        fut = start_call(studio, aid)
        answer(ws, ok=False, code="c" * 50, message=None)
        e = fut.exception(1)
        assert (e.code, e.message, e.extra) == ("c" * 40, "", {})
        monkeypatch.setattr(bridge, "CALL_SEC", 0.05)
        fut = start_call(studio, aid)
        ws.receive_json()
        assert isinstance(fut.exception(1), TimeoutError)
        assert not bridge.LINKS[aid].pending
        monkeypatch.setattr(bridge, "CALL_SEC", 2)
        fut = start_call(studio, aid)
        ws.receive_json()
        started = time.monotonic()
        ws.close()                                   # мост оборвал канал посреди просьбы
        e = fut.exception(5)                         # ждём дольше срока: без Offline был бы TimeoutError через 2 с
        assert isinstance(e, bridge.Offline) and time.monotonic() - started < 1


def test_odd_frames_are_ignored_or_close_4400(studio, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    make_user(studio, "owner")
    aid, key = enroll(studio)
    with online(studio, key) as ws:
        q = studio.c.portal.call(bridge.watch, aid, CHAT)
        good = {"k": "text", "text": "При"}
        for frame in ({"t": "res", "id": "0" * 16, "ok": True, "data": {}}, {"t": "res", "id": ["x"]},
                      {"t": "ev", "project": pid("p"), "chat": CHAT, "e": {"text": "x"}},
                      {"t": "ev", "project": pid("p"), "chat": CHAT, "e": {"k": "huge"}},
                      {"t": "ev", "project": pid("p"), "chat": "not-a-uuid", "e": good},
                      {"t": "ev", "project": pid("p"), "chat": CHAT.upper(), "e": good},
                      {"t": "ev", "project": "xyz", "chat": CHAT, "e": good},
                      {"t": "ev", "project": pid("p"), "chat": CHAT, "e": "text"},
                      {"t": "ev", "project": pid("p"), "chat": CHAT, "turn": "t-1", "e": good}):
            ws.send_json(frame)
        fut = start_call(studio, aid)
        answer(ws, ok=True, data={})
        assert fut.result(1) == {}
        assert studio.c.portal.call(q.get_nowait) == ("ev", good)
        assert studio.c.portal.call(q.empty)
        assert bridge.watching(aid, CHAT)
        ws.send_json({"t": "xyz"})
        assert closed_with(ws) == bridge.CLOSE_PROTOCOL
    assert studio.c.portal.call(q.get_nowait) == ("offline", None)
    studio.c.portal.call(bridge.unwatch, aid, CHAT, q)
    assert not bridge.watching(aid, CHAT) and not bridge.WATCHERS
    assert f"агент {aid}: неверное событие" in caplog.text
    monkeypatch.setattr(bridge, "FRAME_MAX", 1024)
    for send in (lambda ws: ws.send_json({"t": "ev", "pad": "x" * 1024}), lambda ws: ws.send_text("{not json"),
                 lambda ws: ws.send_json([1]), lambda ws: ws.send_bytes(b"{}")):
        with online(studio, key) as ws:
            send(ws)
            assert closed_with(ws) == bridge.CLOSE_PROTOCOL
    assert not bridge.LINKS


def test_revoke_and_disable_drop_the_link(studio):
    uid = make_user(studio, "owner")
    make_user(studio, "admin", is_admin=True)
    aid, key = enroll(studio)
    aid2, key2 = enroll(studio, name="Ноутбук")
    signin(studio, "owner")
    with online(studio, key) as ws:
        assert studio.c.delete(f"/api/agents/{aid}").status_code == 204
        assert closed_with(ws) == bridge.CLOSE_AUTH
        assert aid not in bridge.LINKS
    with link_ws(studio, key) as ws:
        assert closed_with(ws) == bridge.CLOSE_AUTH
    with online(studio, key2) as ws:
        signin(studio, "admin")
        assert studio.c.patch(f"/api/admin/users/{uid}", json={"name": "Владелец"}).status_code == 200
        assert aid2 in bridge.LINKS
        assert studio.c.patch(f"/api/admin/users/{uid}", json={"disabled": True}).status_code == 200
        assert closed_with(ws) == bridge.CLOSE_AUTH
        assert not bridge.LINKS
    with link_ws(studio, key2) as ws:
        assert closed_with(ws) == bridge.CLOSE_AUTH


def test_admin_password_reset_and_user_delete(studio):
    """Пароль от администратора ключ устройства не гасит (спека 6.2); удаление учётки — 4401."""
    uid = make_user(studio, "owner")
    make_user(studio, "admin", is_admin=True)
    machine = FakeMachine()
    aid, _ = enroll(studio)
    link = connect(studio, aid, machine)
    signin(studio, "admin")
    assert studio.c.patch(f"/api/admin/users/{uid}", json={"password": "password-456"}).status_code == 200
    assert link.closed_with is None and bridge.LINKS[aid] is link
    assert studio.c.delete(f"/api/admin/users/{uid}").status_code == 200
    settle(studio)
    assert link.closed_with == bridge.CLOSE_AUTH and not bridge.LINKS


def test_password_change_keeps_the_device_key(studio):
    make_user(studio, "owner")
    aid, key = enroll(studio)
    signin(studio, "owner")
    with online(studio, key) as ws:
        r = studio.c.post("/api/me/password", json={"current": "password-123", "new": "password-456"})
        assert r.status_code == 204
        fut = start_call(studio, aid)
        answer(ws, ok=True, data={"items": []})
        assert fut.result(1) == {"items": []}
    with online(studio, key):
        assert aid in bridge.LINKS


def test_lock_and_unlock_close_the_session_on_the_bridge(studio):
    uid = make_user(studio, "owner")
    make_user(studio, "friend")
    signin(studio, "owner")
    machine, other = FakeMachine(), FakeMachine()
    aid, _ = enroll(studio)
    link = connect(studio, aid, machine)
    foreign = connect(studio, enroll(studio, login="friend")[0], other)
    session = bridge.tag(security.digest(studio.c.cookies[auth.COOKIE]))
    close = {"t": "close", "session": session}
    assert link.sent == [{"t": "welcome", "agent_id": aid, "name": "Рабочий ПК", "login": "owner"}]

    def closes():
        settle(studio)
        return [f for f in link.sent if f["t"] == "close"]

    machine.open_sessions.add(session)
    assert studio.c.post("/api/lock/now").status_code == 204
    assert closes() == [close] and session not in machine.open_sessions
    assert studio.c.post("/api/lock/password", json={"password": "wrong-password"}).status_code == 403
    assert closes() == [close]
    assert studio.c.post("/api/lock/password", json={"password": "password-123"}).status_code == 204
    assert closes() == [close] * 2
    assert studio.c.put("/api/me/pin", json={"pin": "2468", "password": "password-123"}).status_code == 200
    assert studio.c.post("/api/lock/pin", json={"pin": "1111"}).status_code == 403
    assert closes() == [close] * 2
    assert studio.c.post("/api/lock/pin", json={"pin": "2468"}).status_code == 204
    assert closes() == [close] * 3
    key = add_key(studio)
    assert open_with(studio, key).status_code == 204
    assert closes() == [close] * 4
    assert [f["t"] for f in foreign.sent] == ["welcome"]
    assert state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0] == uid


def test_forget_survives_a_dead_link(studio, caplog):
    caplog.set_level(logging.DEBUG)
    uid = make_user(studio, "owner")
    aid, _ = enroll(studio)
    link = connect(studio, aid, FakeMachine())
    link.closed_with = 1006                      # сокет уже мёртв, а detach ещё не пришёл
    studio.c.portal.call(bridge.forget, uid, "d" * 64)
    settle(studio)
    assert f"агент {aid}: закрытие сессии не отправлено (ConnectionError)" in caplog.text and not bridge.TASKS
    assert bridge.LINKS[aid] is link             # отправка не удалась — канал снимет его обрыв, а не forget


def test_in_process_bridge_answers_and_streams(studio):
    """Подставной мост в процессе: просьба — ответ машины, события хода — наблюдателям, обрыв — offline."""
    make_user(studio, "owner")
    aid, _ = enroll(studio)
    machine = FakeMachine({"studio": {"files": {}, "chats": {}, "git": None}})
    link = connect(studio, aid, machine)
    with pytest.raises(bridge.BridgeError) as e:
        studio.c.portal.call(lambda: bridge.call(aid, "projects", SESSION))
    assert e.value.code == "closed"
    assert studio.c.portal.call(lambda: bridge.call(aid, "open", SESSION, pin=None)) == {}
    assert studio.c.portal.call(lambda: bridge.call(aid, "projects", SESSION)) == {
        "items": [{"pid": pid("studio"), "name": "studio"}]}
    q = studio.c.portal.call(bridge.watch, aid, CHAT)
    machine.script = [{"k": "text", "text": "При"}, {"k": "done", "status": "done"}]
    data = studio.c.portal.call(lambda: bridge.call(aid, "turn", SESSION, project=pid("studio"), chat=CHAT,
                                                    text="Привет", model=None, effort=None))
    assert data == {"turn": "t-1"}
    emit(studio, link, pid("studio"), CHAT, {"k": "text", "text": "вет"})
    got = [studio.c.portal.call(q.get_nowait) for _ in range(3)]
    assert got == [("ev", {"k": "text", "text": "При"}), ("ev", {"k": "done", "status": "done"}),
                   ("ev", {"k": "text", "text": "вет"})]
    drop(studio, link)
    assert studio.c.portal.call(q.get_nowait) == ("offline", None)
    assert not bridge.LINKS and kinds()[:2] == ["offline", "online"]


def test_stop_and_drop_user_close_links(studio):
    owner = make_user(studio, "owner")
    make_user(studio, "friend")
    mine = connect(studio, enroll(studio)[0], FakeMachine())
    theirs = connect(studio, enroll(studio, login="friend")[0], FakeMachine())
    studio.c.portal.call(bridge.drop_user, owner)
    settle(studio)
    assert (mine.closed_with, theirs.closed_with) == (bridge.CLOSE_AUTH, None)
    studio.c.portal.call(bridge.stop)
    assert theirs.closed_with == bridge.CLOSE_RESTART and not bridge.LINKS


def test_welcome_carries_login(studio):
    """Мост показывает учётку, под которой компьютер на связи: логин владельца компьютера."""
    make_user(studio, "owner")
    make_user(studio, "friend")
    aid, _ = enroll(studio, login="friend", name="Ноутбук")
    link = connect(studio, aid, FakeMachine())
    assert link.sent == [{"t": "welcome", "agent_id": aid, "name": "Ноутбук", "login": "friend"}]


def test_hanging_close_holds_neither_the_new_link_nor_the_revoke(studio, monkeypatch):
    """Закрытие сокета ждёт замок отправки, а висящая отправка держит его до таймаута пинга: прежний канал при
    замене и снятый `drop` закрываются фоном — новый сразу получает `welcome`, отзыв не ждёт."""
    uid = make_user(studio, "owner")
    aid, _ = enroll(studio)
    release = studio.c.portal.call(asyncio.Event)

    def hanging(link):
        async def close(code):
            await release.wait()
            link.closed_with = code
        monkeypatch.setattr(link, "close", close)

    old = connect(studio, aid, FakeMachine())
    hanging(old)
    new = InProcessBridge(aid, uid, "Рабочий ПК", FakeMachine())
    studio.c.portal.start_task_soon(bridge.attach, new).result(2)
    assert [f["t"] for f in new.sent] == ["welcome"] and bridge.LINKS[aid] is new and old.closed_with is None
    hanging(new)
    studio.c.portal.start_task_soon(bridge.drop, aid).result(2)
    assert aid not in bridge.LINKS and new.closed_with is None
    studio.c.portal.call(release.set)
    settle(studio)
    assert (old.closed_with, new.closed_with) == (bridge.CLOSE_REPLACED, bridge.CLOSE_AUTH)


def test_lagging_watcher_gets_offline_and_is_dropped(studio, monkeypatch, caplog):
    """Очередь наблюдателя ограничена: застрявший поток SSE получает `offline` и снимается (страница дочитает
    снимком), прочие наблюдатели чата получают события дальше."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(bridge, "WATCH_MAX", 3)
    make_user(studio, "owner")
    aid, _ = enroll(studio)
    link = connect(studio, aid, FakeMachine())
    slow = studio.c.portal.call(bridge.watch, aid, CHAT)
    quick = studio.c.portal.call(bridge.watch, aid, CHAT)
    texts = [{"k": "text", "text": str(i)} for i in range(5)]
    emit(studio, link, pid("p"), CHAT, *texts[:3])
    assert [studio.c.portal.call(quick.get_nowait) for _ in range(3)] == [("ev", e) for e in texts[:3]]
    emit(studio, link, pid("p"), CHAT, *texts[3:])
    got = [studio.c.portal.call(slow.get_nowait) for _ in range(4)]
    assert got == [("ev", e) for e in texts[:3]] + [("offline", None)] and studio.c.portal.call(slow.empty)
    assert bridge.WATCHERS[(aid, CHAT)] == {quick}
    assert [studio.c.portal.call(quick.get_nowait) for _ in range(2)] == [("ev", e) for e in texts[3:]]
    assert f"агент {aid}: наблюдатель отстал — снят" in caplog.text


def test_link_log_holds_no_key(studio, caplog):
    caplog.set_level(logging.DEBUG)
    make_user(studio, "owner")
    aid, key = enroll(studio)
    with link_ws(studio, "foreign-key-123") as ws:
        closed_with(ws)
    with online(studio, key) as ws:
        ws.send_json({"t": "ev", "project": pid("Секретный"), "chat": CHAT, "e": {"text": "тайна"}})
        ws.send_json({"t": "xyz", "text": "тайна"})
        closed_with(ws)
    text = caplog.text
    for line in ("канал: неизвестный ключ с testclient", f"агент {aid}: на связи", f"агент {aid}: неверное событие",
                 f"агент {aid}: нарушение протокола", f"агент {aid}: связь потеряна"):
        assert line in text, line
    for secret in (key, security.digest(key), "foreign-key-123", "Рабочий", "ПК", "AI\\projects", "тайна",
                   "Секретный", pid("Секретный")):
        assert secret not in text, secret


def test_fake_machine_follows_the_protocol():
    files = {"README.md": b"# Hi", "src/a.py": b"print(1)\n", "bin/x.dat": b"\0\1", "big.txt": b"x" * (2 * 1024 * 1024 + 1)}
    chat = {"title": "Старый", "created": 1, "modified": 2, "tag": None,
            "messages": [{"id": f"m{i}", "role": "user", "text": str(i), "steps": [], "time": i} for i in range(5)]}
    m = FakeMachine({"studio": {"files": files, "chats": {CHAT: chat}, "git": None},
                     "alpha": {"files": {}, "chats": {}, "git": {"branch": "main", "upstream": None, "ahead": 0,
                                                                "behind": 0, "files": []}}}, pin="1234")
    n = iter(range(1000))

    def req(op, session=SESSION, **args):
        return m.handle({"t": "req", "id": f"{next(n):016x}", "op": op, "session": session, **args})

    def ok(op, **args):
        res, evs = req(op, **args)
        assert res["ok"] is True, res
        return res["data"], [e["e"] for e in evs]

    def refused(op, **args):
        res, evs = req(op, **args)
        assert res["ok"] is False and not evs, res
        return res

    assert m.hello["has_pin"] is True and bridge.parse_hello(m.hello)["efforts"] == ["low", "high"]
    assert refused("projects")["code"] == "closed"
    assert refused("open", pin=None)["code"] == "pin_needed"
    assert {k: v for k, v in refused("open", pin="0000").items() if k in ("code", "left")} == {"code": "pin", "left": 4}
    data, _ = ok("open", pin="1234")
    assert data == {} and m.pin_fails == 0 and SESSION in m.open_sessions
    for left in (4, 3, 2, 1):
        assert refused("open", session="s2", pin="9999").get("left") == left
    assert refused("open", session="s2", pin="9999")["code"] == "pin_locked"
    assert refused("open", session="s2", pin="1234")["code"] == "pin_locked" and m.pin_fails == 5
    assert ok("projects")[0] == {"items": [{"pid": pid("alpha"), "name": "alpha"}, {"pid": pid("studio"), "name": "studio"}]}
    p = pid("studio")
    assert refused("chats", project="0" * 16)["code"] == "not_found"
    data, _ = ok("history", project=p, chat=CHAT, before=None, limit=2)
    assert ([i["id"] for i in data["items"]], data["start"], data["total"]) == (["m3", "m4"], 3, 5)
    data, _ = ok("history", project=p, chat=CHAT, before=3, limit=10)
    assert ([i["id"] for i in data["items"]], data["start"]) == (["m0", "m1", "m2"], 0)
    assert refused("history", project=p, chat=CHAT, before=None, limit=0)["code"] == "invalid"
    new = "11111111-2222-4333-8444-555555555555"
    ask = {"k": "ask", "request_id": "r1", "kind": "permission", "tool": "Bash", "input": {"command": "ls"},
           "always": True}
    m.script = [{"k": "text", "text": "Смотрю"}, ask, {"k": "text", "text": "Готово"}, {"k": "done", "status": "done"}]
    res, evs = req("turn", project=p, chat=new, text="Покажи файлы", model=None, effort=None)
    assert res["data"] == {"turn": "t-1"} and m.script == []
    assert [(e["t"], e["project"], e["chat"], e["turn"]) for e in evs] == [("ev", p, new, "t-1")] * 2
    assert [e["e"] for e in evs] == [{"k": "text", "text": "Смотрю"}, ask]
    assert refused("turn", project=p, chat=new, text="ещё", model=None, effort=None)["code"] == "running"
    chats = ok("chats", project=p)[0]["items"]
    assert (chats[0]["chat"], chats[0]["tag"], chats[0]["running"], chats[0]["asking"]) == (new, "studio", True, True)
    data, evs = ok("watch", project=p, chat=new)
    assert data == {"running": True}
    assert evs == [{"k": "snapshot", "text": "Смотрю", "steps": [], "asks": [
        {"request_id": "r1", "kind": "permission", "tool": "Bash", "input": {"command": "ls"}, "always": True}]}]
    assert refused("answer", project=p, chat=new, request_id="nope", decision="allow", message=None,
                   answers=None)["code"] == "gone"
    _, evs = ok("answer", project=p, chat=new, request_id="r1", decision="allow", message=None, answers=None)
    assert evs == [{"k": "asked", "request_id": "r1"}, {"k": "text", "text": "Готово"}, {"k": "done", "status": "done"}]
    assert ok("watch", project=p, chat=new) == ({"running": False}, [])
    assert refused("stop", project=p, chat=new)["code"] == "idle"
    data, _ = ok("history", project=p, chat=new, before=None, limit=100)
    assert [(i["role"], i["text"]) for i in data["items"]] == [("user", "Покажи файлы"), ("assistant", "СмотрюГотово")]
    m.script = [ask]
    ok("turn", project=p, chat=new, text="Ещё", model="opus", effort="high")
    _, evs = ok("stop", project=p, chat=new)
    assert evs == [{"k": "asked", "request_id": "r1"}, {"k": "done", "status": "stopped"}]
    assert ok("files", project=p, path="")[0]["items"] == [
        {"name": "bin", "dir": True, "size": 0, "modified": m.now}, {"name": "src", "dir": True, "size": 0, "modified": m.now},
        {"name": "README.md", "dir": False, "size": 4, "modified": m.now},
        {"name": "big.txt", "dir": False, "size": 2 * 1024 * 1024 + 1, "modified": m.now}]
    assert [i["name"] for i in ok("files", project=p, path="src")[0]["items"]] == ["a.py"]
    assert refused("files", project=p, path="../x")["code"] == "outside"
    data, _ = ok("read", project=p, path="src/a.py")
    assert data["text"] == "print(1)\n"
    assert refused("read", project=p, path="bin/x.dat")["code"] == "binary"
    assert refused("read", project=p, path="big.txt")["code"] == "too_large"
    assert refused("read", project=p, path="nope.txt")["code"] == "not_found"
    assert ok("raw", project=p, path="bin/x.dat")[0]["data"] == "AAE="
    conflict = refused("save", project=p, path="src/a.py", text="x", version="0" * 64)
    assert (conflict["code"], conflict["version"]) == ("conflict", data["version"])
    saved, _ = ok("save", project=p, path="src/a.py", text="print(2)\n", version=data["version"])
    assert ok("read", project=p, path="src/a.py")[0] == {"text": "print(2)\n", "version": saved["version"]}
    assert refused("git", project=p)["code"] == "not_git"
    assert ok("git", project=pid("alpha"))[0]["branch"] == "main"
    assert refused("nope")["code"] == "invalid"
    assert all(r["op"] for r in m.requests) and len(m.requests) == next(n)


def test_fake_machine_opens_an_open_session_without_the_pin_again():
    m = FakeMachine(pin="2468")
    res, _ = m.handle({"t": "req", "id": "a" * 16, "op": "open", "session": "s" * 32, "pin": "2468"})
    assert res["ok"] is True
    res, _ = m.handle({"t": "req", "id": "b" * 16, "op": "open", "session": "s" * 32, "pin": None})
    assert res == {"t": "res", "id": "b" * 16, "ok": True, "data": {}}
    res, _ = m.handle({"t": "req", "id": "c" * 16, "op": "open", "session": "t" * 32, "pin": None})
    assert res["code"] == "pin_needed"
