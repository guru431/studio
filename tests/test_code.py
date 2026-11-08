"""Раздел «Claude Code» на сервере (app/claude_code.py): открыть компьютер PIN-ом моста, проекты, чаты (слияние сессий
машины с отметками студии), история; чужое и недопустимое — до моста. Мост — подставной (tests/fake_bridge.py)."""
import asyncio
import hashlib
import json
import logging
import uuid

import pytest

import auth
import bridge
import claude_code
import code_attention
import fake_bridge
import ocr_render
import security
import state
from conftest import make_user, signin
from fake_bridge import FakeMachine, Refused, connect, enroll, pid, settle
from scans import scan_pdf
from test_auth import guarded_routes
from test_chat import events_of

A = "0f8e2a52-5d0e-4b8e-9a51-2f3c4d5e6f70"
B = "11111111-2222-4333-8444-555555555555"
ALPHA, BETA = pid("alpha"), pid("beta")

#: тело каждого маршрута раздела — верное по виду; маршрут без строки здесь роняет
#: test_someone_elses_agent_is_404_before_the_bridge. Задачи 5 и 6 дописывают свои маршруты
#: (тела: {"text": "x"}, {"request_id": "r1", "decision": "allow"}, {"path": ""},
#: {"path": "a", "text": "", "version": "v"}, {"path": "a.pdf", "page": 1}).
BODIES = {
    ("POST", "/api/code/agents/1/projects/1/visualization"): {"source": "a.html"},
    ("GET", "/api/code/agents/1/projects/1/visualization/1"): None,
    ("POST", "/api/code/agents/1/projects/1/chats/1/seen"): {"attention": "0" * 32},
    ("GET", "/api/code/agents/1/desktop"): None,
    ("POST", "/api/code/agents/1/desktop/start"): {},
    ("POST", "/api/code/agents/1/desktop/frame"): {"control": "0" * 32, "monitor": 0},
    ("POST", "/api/code/agents/1/desktop/input"): {"control": "0" * 32, "events": [{"type": "text", "text": "x"}]},
    ("POST", "/api/code/agents/1/desktop/clipboard"): {"control": "0" * 32, "text": None},
    ("POST", "/api/code/agents/1/desktop/stop"): {"control": "0" * 32},
    ("GET", "/api/code/agents/1/info"): None,
    ("GET", "/api/code/agents/1/access"): None,
    ("POST", "/api/code/agents/1/projects/1/chats/1/queue/pause"): {"paused": False},
    ("GET", "/api/code/agents/1/projects/1/worktrees"): None,
    ("POST", "/api/code/agents/1/projects/1/worktrees"): {"name": "task"},
    ("POST", "/api/code/agents/1/projects/1/chats/1/fork"): {},
    ("GET", "/api/code/agents/1/projects/1/chats/1/subagents"): None,
    ("POST", "/api/code/agents/1/projects/1/chats/1/subagent"): {"request_id": "child"},
    ("POST", "/api/code/agents/1/projects/1/files/suggest"): {"query": "a"},
    ("POST", "/api/code/agents/1/projects/1/search"): {"query": "a"},
    ("POST", "/api/code/agents/1/projects/1/chats/1/queue"): {"request_id": "0" * 16, "version": 1, "text": "a"},
    ("GET", "/api/code/agents/1/usage"): None,
    ("POST", "/api/code/agents/1/open"): {"pin": None},
    ("GET", "/api/code/agents/1/projects"): None,
    ("GET", "/api/code/agents/1/projects/1/summary"): None,
    ("GET", "/api/code/agents/1/activity"): None,
    ("GET", "/api/code/agents/1/projects/1/chats"): None,
    ("POST", "/api/code/agents/1/projects/1/chats"): None,
    ("PATCH", "/api/code/agents/1/projects/1/chats/1"): {"state": "closed"},
    ("GET", "/api/code/agents/1/projects/1/chats/1"): None,
    ("POST", "/api/code/agents/1/projects/1/chats/1/turn"): {"text": "x"},
    ("POST", "/api/code/agents/1/projects/1/chats/1/steer"): {"text": "x"},
    ("POST", "/api/code/agents/1/projects/1/chats/1/claim"): {},
    ("POST", "/api/code/agents/1/projects/1/chats/1/state"): {},
    ("GET", "/api/code/agents/1/projects/1/chats/1/live"): None,
    ("POST", "/api/code/agents/1/projects/1/chats/1/answer"): {"request_id": "r1", "decision": "allow"},
    ("POST", "/api/code/agents/1/projects/1/chats/1/stop"): None,
    ("POST", "/api/code/agents/1/projects/1/files"): {"path": ""},
    ("POST", "/api/code/agents/1/projects/1/files/stats"): {"path": ""},
    ("POST", "/api/code/agents/1/projects/1/files/operate"): {"operation": "delete", "path": "a.txt"},
    ("POST", "/api/code/agents/1/projects/1/read"): {"path": ""},
    ("PUT", "/api/code/agents/1/projects/1/file"): {"path": "a", "text": "", "version": "v"},
    ("POST", "/api/code/agents/1/projects/1/pdf"): {"path": "a.pdf", "page": 1},
    ("POST", "/api/code/agents/1/projects/1/image"): {"path": "a.png"},
    ("GET", "/api/code/agents/1/projects/1/git"): None,
    ("POST", "/api/code/agents/1/update"): None,
    ("GET", "/api/code/agents/1/update"): None,
}


def machine(pin=None) -> FakeMachine:
    messages = [{"id": f"m{i}", "role": "user" if i % 2 == 0 else "assistant", "text": str(i), "steps": [],
                 "time": i} for i in range(250)]
    return FakeMachine({
        "alpha": {"files": {}, "git": None, "chats": {
            A: {"title": "Чат студии", "created": 10, "modified": 30, "tag": "studio", "messages": messages},
            B: {"title": "Чат терминала", "created": 20, "modified": 40, "tag": None, "messages": []}}},
        "beta": {"files": {}, "git": None, "chats": {}}}, pin=pin)


def setup(s, pin=None, opened=True):
    """Владелец вошёл, его компьютер на связи (и открыт этой сессией, если `opened`)."""
    make_user(s, "owner")
    aid, _ = enroll(s)
    signin(s, "owner")
    m = machine(pin)
    link = connect(s, aid, m)
    if opened:
        assert s.c.post(f"/api/code/agents/{aid}/open", json={"pin": pin}).status_code == 204
    return aid, m, link


def url(aid, *rest) -> str:
    return "/".join([f"/api/code/agents/{aid}", *rest])


def detail(r) -> dict:
    return r.json()["detail"]


@pytest.mark.parametrize("operation", ["history", "read"])
@pytest.mark.parametrize("revocation", ["logout", "expiry", "disabled", "lock"])
def test_late_bridge_reply_requires_a_live_unlocked_session(studio, operation, revocation):
    aid, machine, _ = setup(studio)
    uid = state.DB.execute("SELECT user_id FROM agents WHERE id = ?", (aid,)).fetchone()[0]
    original = getattr(machine, "_op_" + operation)

    def revoke(req, events):
        result = original(req, events)
        if revocation == "logout":
            auth.end_sessions(uid)
        elif revocation == "expiry":
            state.DB.execute("UPDATE sessions SET expires = ?", (studio.clock[0] - 1,))
        elif revocation == "disabled":
            state.DB.execute("UPDATE users SET disabled = 1")
        else:
            state.DB.execute("UPDATE users SET screen_lock = 1")
            state.DB.execute("UPDATE sessions SET mobile = 1, seen = NULL")
        return result

    setattr(machine, "_op_" + operation, revoke)
    if operation == "history":
        response = studio.c.get(url(aid, "projects", ALPHA, "chats", A))
    else:
        machine.projects["alpha"]["files"]["private.txt"] = b"private file"
        response = studio.c.post(url(aid, "projects", ALPHA, "read"), json={"path": "private.txt"})
    assert response.status_code == 409, response.text
    assert detail(response)["code"] == "closed"
    assert "private file" not in response.text


def test_late_bridge_refusal_does_not_reveal_its_message_to_a_closed_session(studio):
    aid, machine, _ = setup(studio)

    def revoke(req, events):
        state.DB.execute("DELETE FROM sessions")
        raise Refused("invalid", "private refusal")

    machine._op_history = revoke
    response = studio.c.get(url(aid, "projects", ALPHA, "chats", A))
    assert response.status_code == 409 and detail(response)["code"] == "closed"
    assert "private refusal" not in response.text


def events():
    return [dict(r) for r in state.DB.execute(
        "SELECT kind, agent_id, agent_name, device, ip FROM agent_events WHERE kind <> 'online' ORDER BY id")]


def test_someone_elses_agent_is_404_before_the_bridge(studio):
    aid, m, _ = setup(studio, opened=False)
    make_user(studio, "friend")
    signin(studio, "friend")
    # у рабочего места компьютер не в адресе — чужой проверяет test_workspace.py
    routes = [(method, path) for method, path in guarded_routes() if path.startswith("/api/code/agents/")]
    assert routes
    for method, path in routes:
        assert (method, path) in BODIES, f"нет тела для {method} {path} — допишите в BODIES"
        parts = path.split("/")
        for i in range(1, len(parts)):
            if parts[i] == "1":
                parts[i] = {"agents": str(aid), "projects": ALPHA, "chats": A,
                            "visualization": "0" * 32}[parts[i - 1]]
        body = BODIES[(method, path)]
        r = studio.c.request(method, "/".join(parts), **({} if body is None else {"json": body}))
        assert (r.status_code, detail(r)["code"]) == (404, "not_found"), (method, path, r.text)
    assert set(routes) == set(BODIES)            # и в таблице нет строк маршрутов, которых уже нет
    assert m.requests == []
    assert state.DB.execute("SELECT count(*) FROM code_chats").fetchone()[0] == 0


def test_offline_agent_is_409_offline(studio):
    make_user(studio, "owner")
    aid, _ = enroll(studio)
    signin(studio, "owner")
    for r in (studio.c.post(url(aid, "open"), json={"pin": None}), studio.c.get(url(aid, "projects")),
              studio.c.get(url(aid, "projects", ALPHA, "chats", A))):
        assert (r.status_code, detail(r)) == (409, {"code": "offline", "message": "компьютер не на связи"})


def test_pin_opens_and_failures_are_recorded(studio):
    aid, m, _ = setup(studio, pin="2580", opened=False)
    r = studio.c.post(url(aid, "open"), json={"pin": "1111"})
    assert r.status_code == 403 and {k: detail(r)[k] for k in ("code", "left")} == {"code": "pin", "left": 4}
    [fail] = events()[1:]
    assert fail["kind"] == "pin_fail" and fail["agent_id"] == aid and fail["agent_name"] == "Рабочий ПК"
    assert fail["device"].startswith("браузер ") and fail["ip"] == "testclient"
    assert studio.c.post("/api/lock/alive", json={"touch": True}).status_code == 200
    r = studio.c.post(url(aid, "open"), json={"pin": None})
    assert (r.status_code, detail(r)["code"]) == (409, "pin_needed")
    assert studio.c.post(url(aid, "open"), json={"pin": "2580"}).status_code == 204
    ok = events()[-1]
    assert ok["kind"] == "pin_ok" and ok["device"].startswith("телефон ")
    assert studio.c.get(url(aid, "projects")).status_code == 200
    for left in (4, 3, 2, 1):
        assert detail(studio.c.post(url(aid, "open"), json={"pin": "0000"})).get("left") == left
    for pin in ("0000", "2580"):                 # пятый неверный и дальше — даже верный
        r = studio.c.post(url(aid, "open"), json={"pin": pin})
        assert (r.status_code, detail(r)["code"]) == (403, "pin_locked")
    assert [e["kind"] for e in events()] == ["enroll", "pin_fail", "pin_ok"] + ["pin_fail"] * 6
    assert studio.c.post(url(aid, "open"), json={"pin": "1" * 65}).status_code == 422
    # компьютер без PIN открывается сразу
    m.pin, m.pin_fails = None, 0
    assert studio.c.post(url(aid, "open"), json={"pin": None}).status_code == 204
    assert events()[-1]["kind"] == "open"
    assert studio.c.post(url(aid, "open"), json={}).status_code == 204


def test_pin_sent_to_a_computer_without_pin_is_recorded_as_open(studio):
    """«открыт PIN» в неудаляемой истории — только у компьютера с PIN: строка от страницы у компьютера без PIN
    ничего не проверяла."""
    aid, _, _ = setup(studio, opened=False)
    assert studio.c.post(url(aid, "open"), json={"pin": "1234"}).status_code == 204
    assert events()[-1]["kind"] == "open"


def test_lone_surrogates_are_422_before_the_bridge(studio):
    """Одинокий суррогат из JSON запроса в кадр UTF-8 не ляжет: 422 до моста, а не 409 «не на связи»."""
    aid, m, link = setup(studio)
    before = len(m.requests)
    raw = {"Content-Type": "application/json"}
    for method, path, body in (
            ("POST", chat_url(aid, "turn"), b'{"text": "\\ud83d"}'),
            ("POST", chat_url(aid, "answer"), b'{"request_id": "r1", "decision": "deny", "message": "\\udc00"}'),
            ("POST", chat_url(aid, "answer"), b'{"request_id": "r1", "decision": "allow", "answers": {"a": "\\ud83d"}}'),
            ("POST", url(aid, "open"), b'{"pin": "\\ud83d"}')):
        r = studio.c.request(method, path, content=body, headers=raw)
        assert (r.status_code, detail(r)["code"]) == (422, "invalid"), body
    r = studio.c.post(project_url(aid, "read"), content=b'{"path": "a\\ud83d"}', headers=raw)
    assert (r.status_code, detail(r)["code"]) == (422, "path")
    assert len(m.requests) == before and link.closed_with is None and not bridge.WATCHERS


def test_unopened_computer_is_409_closed_and_screen_unlock_preserves_access(studio):
    aid, _, _ = setup(studio, opened=False)
    r = studio.c.get(url(aid, "projects"))
    assert (r.status_code, detail(r)["code"]) == (409, "closed")
    assert studio.c.post(url(aid, "open"), json={"pin": None}).status_code == 204
    r = studio.c.get(url(aid, "projects"))
    assert r.status_code == 200 and r.json() == {"items": [{"pid": ALPHA, "name": "alpha"},
                                                           {"pid": BETA, "name": "beta"}]}
    assert studio.c.post("/api/lock/now").status_code == 204
    assert studio.c.post("/api/lock/password", json={"password": "password-123"}).status_code == 204
    settle(studio)
    r = studio.c.get(url(aid, "projects"))
    assert r.status_code == 200


def test_chats_merge_machine_sessions_with_studio_marks(studio):
    aid, m, _ = setup(studio)
    r = studio.c.patch(url(aid, "projects", ALPHA, "chats", B), json={"state": "closed"})
    assert (r.status_code, r.json()) == (200, {"state": "closed"})
    studio.clock[0] += 5
    new = studio.c.post(url(aid, "projects", ALPHA, "chats")).json()["chat"]
    studio.c.post(url(aid, "projects", BETA, "chats"))                  # чужой проект — не в этом списке
    m.running[A] = {"asks": {"r1": {}}}
    items = studio.c.get(url(aid, "projects", ALPHA, "chats")).json()["items"]
    now = studio.clock[0]
    assert items == [
        {"chat": new, "engine": "claude", "title": None, "created": now, "modified": now, "origin": "studio", "state": "open",
         "running": False, "asking": False},
        {"chat": A, "engine": "claude", "title": "Чат студии", "created": 10, "modified": 30, "origin": "studio", "state": "open",
         "running": True, "asking": True},
        {"chat": B, "engine": "claude", "title": "Чат терминала", "created": 20, "modified": 40, "origin": "other", "state": "closed",
         "running": False, "asking": False}]
    assert studio.c.patch(url(aid, "projects", ALPHA, "chats", B), json={"state": "open"}).json() == {"state": "open"}
    assert [i["chat"] for i in studio.c.get(url(aid, "projects", ALPHA, "chats")).json()["items"]] == [new, B, A]
    assert studio.c.patch(url(aid, "projects", ALPHA, "chats", B), json={"state": "gone"}).status_code == 422


def test_new_chat_writes_a_row_and_an_event_but_not_the_bridge(studio):
    aid, m, _ = setup(studio)
    before = len(m.requests)
    r = studio.c.post(url(aid, "projects", ALPHA, "chats"))
    assert r.status_code == 201
    chat = r.json()["chat"]
    assert str(uuid.UUID(chat)) == chat
    row = state.DB.execute("SELECT agent_id, project, session_id, state, created, updated FROM code_chats").fetchone()
    assert tuple(row) == (aid, ALPHA, chat, "open", studio.clock[0], studio.clock[0])
    assert events()[-1]["kind"] == "session_start" and events()[-1]["device"].startswith("браузер ")
    assert len(m.requests) == before


def test_new_chats_per_computer_are_limited(studio):
    """«Новый чат» моста не зовёт — открытость компьютера его не сдерживает; поэтому строк `code_chats` и
    событий `session_start` в неудаляемой истории — не больше NEW_CHATS_PER_MIN на компьютер в минуту."""
    aid, _, _ = setup(studio)
    limit = claude_code.NEW_CHATS_PER_MIN
    assert limit == 10
    for _ in range(limit):
        assert studio.c.post(url(aid, "projects", ALPHA, "chats")).status_code == 201
        studio.clock[0] += 1
    r = studio.c.post(url(aid, "projects", BETA, "chats"))
    assert (r.status_code, detail(r)) == (429, {"code": "too_many", "message": claude_code.TOO_MANY})
    count = lambda: state.DB.execute("SELECT count(*) FROM code_chats").fetchone()[0]       # noqa: E731
    assert count() == limit and [e["kind"] for e in events()].count("session_start") == limit
    studio.clock[0] += 50                            # первая отметка — минуту назад: место освободилось
    assert studio.c.post(url(aid, "projects", ALPHA, "chats")).status_code == 201
    assert studio.c.post(url(aid, "projects", ALPHA, "chats")).status_code == 429
    assert count() == limit + 1


def test_closed_foreign_session_does_not_come_back_as_ghost(studio):
    """Отметку о чате, которого студия не заводила, PATCH пишет, только если сессия есть на машине (иначе 404
    и строки нет; закрыт компьютер — `closed`); строка без сессии на машине в списке — только открытая: закрытая
    сессия терминала, которую Claude Code вычистил по сроку, не возвращается «Новым чатом» студии."""
    aid, m, _ = setup(studio)
    other = "33333333-3333-4333-8333-333333333333"
    count = lambda: state.DB.execute("SELECT count(*) FROM code_chats").fetchone()[0]       # noqa: E731
    r = studio.c.patch(url(aid, "projects", ALPHA, "chats", other), json={"state": "closed"})
    assert (r.status_code, detail(r)["code"]) == (404, "not_found")
    assert count() == 0
    assert studio.c.patch(url(aid, "projects", ALPHA, "chats", B), json={"state": "closed"}).status_code == 200
    new = studio.c.post(url(aid, "projects", ALPHA, "chats")).json()["chat"]
    before = len(m.requests)
    for value in ("closed", "open"):                 # своя строка есть — мост не спрашивают
        assert studio.c.patch(url(aid, "projects", ALPHA, "chats", new), json={"state": value}).json() == {
            "state": value}
    assert len(m.requests) == before
    del m.projects["alpha"]["chats"][B]              # Claude Code вычистил старую сессию
    items = studio.c.get(url(aid, "projects", ALPHA, "chats")).json()["items"]
    assert [i["chat"] for i in items] == [new, A]
    assert studio.c.patch(url(aid, "projects", ALPHA, "chats", new), json={"state": "closed"}).status_code == 200
    assert [i["chat"] for i in studio.c.get(url(aid, "projects", ALPHA, "chats")).json()["items"]] == [A]
    assert studio.c.post("/api/lock/now").status_code == 204
    assert studio.c.post("/api/lock/password", json={"password": "password-123"}).status_code == 204
    settle(studio)
    r = studio.c.patch(url(aid, "projects", ALPHA, "chats", other), json={"state": "open"})
    assert (r.status_code, detail(r)["code"]) == (404, "not_found")
    assert count() == 2


def test_history_pages_from_the_end(studio):
    aid, m, _ = setup(studio)
    before, seen = None, []
    while True:
        data = studio.c.get(url(aid, "projects", ALPHA, "chats", A),
                            params={} if before is None else {"before": before}).json()
        assert data["total"] == 250 and len(data["items"]) <= 30
        assert m.requests[-1]["limit"] == (claude_code.HISTORY_TAIL if before is None else claude_code.HISTORY_PAGE)
        seen[0:0] = [item["id"] for item in data["items"]]
        if data["start"] == 0:
            break
        assert before is None or data["start"] < before
        before = data["start"]
    assert seen == [f"m{i}" for i in range(250)]
    assert m.requests[-1]["limit"] == claude_code.HISTORY_PAGE == 30
    r = studio.c.get(url(aid, "projects", ALPHA, "chats", "22222222-2222-4222-8222-222222222222"))
    assert (r.status_code, detail(r)["code"]) == (404, "not_found")


def test_cached_preview_access_probe_checks_computer_pin(studio):
    aid, machine, _ = setup(studio, opened=False)
    path = url(aid, "access")
    result = studio.c.get(path)
    assert result.status_code == 409 and detail(result)["code"] == "closed"
    assert studio.c.post(url(aid, "open"), json={"pin": None}).status_code == 204
    assert studio.c.get(path).status_code == 204
    assert all(row["op"] in ("projects", "open") for row in machine.requests)


def test_bad_ids_are_422(studio):
    aid, m, _ = setup(studio)
    before = len(m.requests)
    for p in ("ABC", "a" * 17):
        for r in (studio.c.get(url(aid, "projects", p, "chats")), studio.c.post(url(aid, "projects", p, "chats")),
                  studio.c.get(url(aid, "projects", p, "chats", A))):
            assert (r.status_code, detail(r)) == (422, {"code": "project", "message": "неверный проект"}), p
    for c in ("x", A.upper()):
        for r in (studio.c.get(url(aid, "projects", ALPHA, "chats", c)),
                  studio.c.patch(url(aid, "projects", ALPHA, "chats", c), json={"state": "closed"})):
            assert (r.status_code, detail(r)) == (422, {"code": "chat", "message": "неверный чат"}), c
    assert len(m.requests) == before
    assert state.DB.execute("SELECT count(*) FROM code_chats").fetchone()[0] == 0


def test_odd_bridge_answers_are_502_and_long_words_are_cut(studio, monkeypatch):
    aid, m, _ = setup(studio)
    answer = {}
    monkeypatch.setattr(m, "handle", lambda req: ({"t": "res", "id": req["id"], **answer}, []))
    answer.update(ok=False, code="zzz", message="что-то")
    r = studio.c.get(url(aid, "projects"))
    assert (r.status_code, detail(r)) == (502, {"code": "bridge", "message": "мост ответил непонятно"})
    answer.clear()
    answer.update(ok=False, message="без кода")
    assert detail(studio.c.get(url(aid, "projects")))["code"] == "bridge"
    answer.update(ok=False, code="busy", message="м" * 1000)
    r = studio.c.get(url(aid, "projects"))
    assert (r.status_code, detail(r)) == (409, {"code": "busy", "message": "м" * 300})
    answer.update(ok=False, code="conflict", message="", version="v2")
    r = studio.c.get(url(aid, "projects"))
    assert (r.status_code, detail(r)) == (409, {"code": "conflict", "message": "", "version": "v2"})
    answer.clear()
    answer.update(ok=True, data="строка")
    r = studio.c.get(url(aid, "projects", ALPHA, "chats", A))
    assert (r.status_code, r.json()) == (200, {})
    assert studio.c.get(url(aid, "projects")).json() == {"items": []}
    assert studio.c.get(url(aid, "projects", ALPHA, "chats")).json() == {"items": []}
    answer.update(ok=True, data={"items": [{"pid": "../x", "name": "x"}, "x", {"pid": ALPHA, "name": "alpha"}]})
    assert studio.c.get(url(aid, "projects")).json() == {"items": [{"pid": ALPHA, "name": "alpha"}]}
    answer.update(ok=True, data={"items": [{"chat": 5}, "x", {"chat": A, "title": "t", "modified": "вчера"}]})
    [item] = studio.c.get(url(aid, "projects", ALPHA, "chats")).json()["items"]
    assert (item["chat"], item["origin"], item["state"], item["running"]) == (A, "other", "open", False)
    # чат не UUID — мимо; не строка и не число там, где их ждут, — None; NaN — не JSON
    answer.update(ok=True, data={"items": [{"chat": "../.."}, {"chat": A.upper()}, {"chat": A + "/x"},
                                           {"chat": A, "title": {"x": 1}, "created": float("nan"),
                                            "modified": float("inf")}]})
    [item] = studio.c.get(url(aid, "projects", ALPHA, "chats")).json()["items"]
    assert (item["chat"], item["title"], item["created"], item["modified"]) == (A, None, None, None)
    # одинокий суррогат (Node режет UTF-16 посреди эмодзи) — проходит как есть, а не 500
    answer.update(ok=True, data={"items": [{"text": "\ud83d"}], "start": 0, "total": 1})
    r = studio.c.get(url(aid, "projects", ALPHA, "chats", A))
    assert (r.status_code, r.json()["items"]) == (200, [{"text": "\ud83d"}])
    for odd in ({"total": float("nan")}, {"size": float("-inf")}):
        answer.update(ok=True, data=odd)
        for r in (studio.c.get(url(aid, "projects", ALPHA, "chats", A)),
                  studio.c.get(url(aid, "projects", ALPHA, "git")),
                  studio.c.post(url(aid, "projects", ALPHA, "files"), json={"path": ""})):
            assert (r.status_code, detail(r)) == (502, {"code": "bridge", "message": "мост ответил непонятно"}), odd
    answer.update(ok=True, data={"text": "\ud83d", "version": "v"})
    r = studio.c.post(url(aid, "projects", ALPHA, "read"), json={"path": "a"})
    assert (r.status_code, r.json()) == (200, {"text": "\ud83d", "version": "v"})
    # и в отказе: слова и version идут обычным ответом FastAPI (UTF-8) — не 500
    answer.clear()
    answer.update(ok=False, code="conflict", message="файл \ud83d изменился", version="v\ud83d")
    r = studio.c.post(url(aid, "projects", ALPHA, "read"), json={"path": "a"})
    assert (r.status_code, detail(r)) == (409, {"code": "conflict", "message": "файл � изменился"})
    answer.update(ok=False, code="zz\ud83d", message="x")
    r = studio.c.post(url(aid, "projects", ALPHA, "read"), json={"path": "a"})
    assert (r.status_code, detail(r)["code"]) == (502, "bridge")


def test_deeply_nested_bridge_answer_is_relayed_not_500(studio, monkeypatch):
    """Ответ моста идёт браузеру мимо `jsonable_encoder` FastAPI: его рекурсия (предел ~1000) на вложенности,
    которую пропускает разбор кадра, давала 500. Теперь — как есть или 502 `bridge`."""
    aid, m, _ = setup(studio)
    deep: list = []
    for _ in range(2000):
        deep = [deep]
    monkeypatch.setattr(m, "handle", lambda req: (
        {"t": "res", "id": req["id"], "ok": True, "data": {"items": deep}}, []))
    for r in (studio.c.get(url(aid, "projects", ALPHA, "chats", A)),
              studio.c.get(url(aid, "projects", ALPHA, "git")),
              studio.c.post(url(aid, "projects", ALPHA, "files"), json={"path": ""})):
        assert r.status_code in (200, 502), (r.url, r.status_code)
        if r.status_code == 502:
            assert detail(r) == {"code": "bridge", "message": "мост ответил непонятно"}
        else:
            assert r.headers["content-type"] == "application/json" and r.content.startswith(b'{"items":[[[[')


def test_odd_events_neither_break_nor_stall_the_stream(studio):
    """Одинокий суррогат в событии — проходит (ASCII-JSON), событие с NaN — мимо, `done` с NaN — всё же конец."""
    aid, m, _ = setup(studio)
    m.script = [text("\ud83d"), {**STEP, "input": {"n": float("nan")}}, {"k": "done", "status": float("nan")}]
    with studio.c.stream("POST", chat_url(aid, "turn"), json={"text": "Привет"}) as r:
        assert list(events_of(r.iter_lines())) == [("turn", {"turn": "t-1"}), ("text", {"text": "\ud83d"}),
                                                   ("done", {})]
    no_watchers(studio)


def test_slow_bridge_is_504(studio, monkeypatch):
    aid, m, link = setup(studio)

    async def silent(frame):
        link.sent.append(frame)

    monkeypatch.setattr(link, "send", silent)
    monkeypatch.setattr(bridge, "CALL_SEC", 0.05)
    r = studio.c.get(url(aid, "projects"))
    assert (r.status_code, detail(r)) == (504, {"code": "timeout", "message": "компьютер не ответил"})
    assert link.sent[-1]["op"] == "projects" and not link.pending


def test_code_log_holds_no_names(studio, caplog):
    caplog.set_level(logging.DEBUG)
    aid, m, _ = setup(studio, pin="2580", opened=False)
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]
    studio.c.get(url(aid, "projects"))
    studio.c.post(url(aid, "open"), json={"pin": "1357"})
    studio.c.post(url(aid, "open"), json={"pin": "2580"})
    studio.c.get(url(aid, "projects"))
    studio.c.get(url(aid, "projects", ALPHA, "chats"))
    studio.c.get(url(aid, "projects", ALPHA, "chats", A))
    studio.c.post(url(aid, "projects", ALPHA, "chats"))
    studio.c.get(url(aid, "projects", BETA, "chats", B))
    text = "\n".join(r.getMessage() for r in caplog.records if r.name == "studio")    # не клиент тестов httpx
    for line in (f"пользователь {uid}: компьютер {aid} — projects: closed",
                 f"пользователь {uid}: компьютер {aid} — open: pin",
                 f"пользователь {uid}: компьютер {aid} — history: not_found"):
        assert line in text, line
    for secret in ("alpha", "beta", "Чат студии", "Чат терминала", "1357", "2580", "Рабочий", ALPHA, BETA, A, B):
        assert secret not in text, secret


# ---- ход потоком, наблюдение, карточки, остановка (задача 5) ------------------------

def text(t: str) -> dict:
    return {"k": "text", "text": t}


STEP = {"k": "step", "id": "s1", "tool": "Bash", "input": {"command": "ls"}}
RESULT = {"k": "result", "id": "s1", "text": "a.txt", "error": False}
DONE = {"k": "done", "status": "done"}
ASK = {"k": "ask", "request_id": "r1", "kind": "permission", "tool": "Bash", "input": {"command": "rm a.txt"},
       "always": True}
QUESTION = {"k": "ask", "request_id": "q1", "kind": "question", "questions": [
    {"question": "Какой цвет?", "header": "Цвет", "multi": False,
     "options": [{"label": "Синий", "description": ""}, {"label": "Красный", "description": ""}]}]}
CARD = {k: v for k, v in ASK.items() if k != "k"}


def chat_url(aid, *rest) -> str:
    return url(aid, "projects", ALPHA, "chats", A, *rest)


def done_data(aid, status="done"):
    return {"status": status, "attention": code_attention.revision(aid, ALPHA, A)}


def sent(m: FakeMachine, op: str) -> list[dict]:
    return [r for r in m.requests if r["op"] == op]


def no_watchers(s) -> None:
    """Поток кончился — его очередь снята (finally ответа идёт на цикле службы чуть позже конца тела)."""
    for _ in range(3000):
        if not s.c.portal.call(lambda: bool(bridge.WATCHERS)):
            return
        s.c.portal.call(asyncio.sleep, 0.001)
    raise AssertionError("очередь наблюдателя не снята")


def until(events, kind: str) -> list:
    """События потока до `kind` включительно."""
    out = []
    for e, d in events:
        out.append((e, d))
        if e == kind:
            return out
    raise AssertionError(f"поток кончился без {kind}: {out}")


def test_turn_streams_until_done(studio):
    aid, m, _ = setup(studio)
    m.script = [text("При"), text("вет"), STEP, RESULT, DONE]
    with studio.c.stream("POST", chat_url(aid, "turn"), json={"text": "Привет"}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        got = list(events_of(r.iter_lines()))
    assert got == [("turn", {"turn": "t-1"}), ("text", {"text": "При"}), ("text", {"text": "вет"}),
                   ("step", {"id": "s1", "tool": "Bash", "input": {"command": "ls"}}),
                   ("result", {"id": "s1", "text": "a.txt", "error": False}), ("done", done_data(aid))]
    [req] = sent(m, "turn")
    assert {k: req[k] for k in ("project", "chat", "text", "model", "effort")} == {
        "project": ALPHA, "chat": A, "text": "Привет", "model": None, "effort": None}
    assert req["session"] == m.requests[0]["session"]           # сессия, открывшая компьютер
    no_watchers(studio)


def test_turn_checks_text_model_and_effort(studio):
    aid, m, _ = setup(studio)
    words = {"text": "задание пустое или длиннее 100 000 знаков", "model": "такой модели нет на компьютере",
             "effort": "такого усилия нет на компьютере"}
    for body, code in (({"text": ""}, "text"), ({"text": " \n\t"}, "text"),
                       ({"text": "x" * (claude_code.TEXT_MAX + 1)}, "text"),
                       ({"text": "Привет", "model": "gpt"}, "model"),
                       ({"text": "Привет", "effort": "max"}, "effort")):
        r = studio.c.post(chat_url(aid, "turn"), json=body)
        assert (r.status_code, detail(r)) == (422, {"code": code, "message": words[code]}), body
    assert sent(m, "turn") == [] and not bridge.WATCHERS
    assert claude_code.TEXT_MAX == 100_000
    for body in ({"text": "Привет", "model": "sonnet", "effort": "high"}, {"text": "x" * claude_code.TEXT_MAX}):
        m.script = [DONE]
        with studio.c.stream("POST", chat_url(aid, "turn"), json=body) as r:
            assert [e for e, _ in events_of(r.iter_lines())] == ["turn", "done"]
    assert [(q["model"], q["effort"], len(q["text"])) for q in sent(m, "turn")] == [
        ("sonnet", "high", 6), (None, None, claude_code.TEXT_MAX)]


def test_second_turn_in_a_running_chat_is_409_running(studio):
    aid, m, _ = setup(studio)
    m.script = [text("При"), ASK]
    with studio.c.stream("POST", chat_url(aid, "turn"), json={"text": "Привет"}) as r:
        events = events_of(r.iter_lines())            # ссылка держит поток: брошенный генератор закрыл бы ответ
        until(events, "ask")
        r2 = studio.c.post(chat_url(aid, "turn"), json={"text": "Ещё"})
        assert (r2.status_code, detail(r2)) == (409, {"code": "running", "message": "в чате уже идёт ход"})
        assert len(studio.c.portal.call(lambda: bridge.WATCHERS[(aid, A)])) == 1     # отказ свою очередь снял
    assert len(sent(m, "turn")) == 2


def test_permission_card_round_trip(studio):
    aid, m, _ = setup(studio)
    m.script = [text("Удаляю"), ASK, text("Готово"), DONE]
    with studio.c.stream("POST", chat_url(aid, "turn"), json={"text": "Убери a.txt"}) as r:
        events = events_of(r.iter_lines())
        assert until(events, "ask") == [("turn", {"turn": "t-1"}), ("text", {"text": "Удаляю"}), ("ask", CARD)]
        a = studio.c.post(chat_url(aid, "answer"), json={"request_id": "r1", "decision": "always"})
        assert (a.status_code, a.content) == (204, b"")
        rest = list(events)
    assert rest == [("asked", {"request_id": "r1"}), ("text", {"text": "Готово"}), ("done", done_data(aid))]
    [req] = sent(m, "answer")
    assert {k: req[k] for k in ("project", "chat", "request_id", "decision", "message", "answers")} == {
        "project": ALPHA, "chat": A, "request_id": "r1", "decision": "always", "message": None, "answers": None}


def test_question_selection_ids_reach_the_bridge_without_string_conversion(studio):
    aid, machine, _ = setup(studio)
    for selections in (["option-0"], ["option-0", "option-1"]):
        machine.running[A] = {"project": ALPHA, "asks": {"q1": QUESTION}, "rest": []}
        answers = {"city": selections}
        response = studio.c.post(chat_url(aid, "answer"), json={
            "request_id": "q1", "decision": "allow", "answers": answers})
        assert response.status_code == 204, response.text
        assert sent(machine, "answer")[-1]["answers"] == answers
    for invalid in (["x"] * 101, ["x" * 2001], [1], [["x"]]):
        response = studio.c.post(chat_url(aid, "answer"), json={
            "request_id": "q1", "decision": "allow", "answers": {"city": invalid}})
        assert response.status_code == 422


def test_question_answers_reach_the_bridge(studio):
    aid, m, _ = setup(studio)
    m.script = [QUESTION, DONE]
    with studio.c.stream("POST", chat_url(aid, "turn"), json={"text": "Покрась кнопку"}) as r:
        events = events_of(r.iter_lines())
        assert until(events, "ask")[-1] == ("ask", {k: v for k, v in QUESTION.items() if k != "k"})
        before = len(m.requests)
        for bad in ({"request_id": "q 1", "decision": "allow"}, {"request_id": "q" * 65, "decision": "allow"},
                    {"request_id": "q1", "decision": "yes"},
                    {"request_id": "q1", "decision": "allow", "message": "не к отказу"},
                    {"request_id": "q1", "decision": "deny", "message": "м" * 2001},
                    {"request_id": "q1", "decision": "allow", "answers": {f"в{i}": "о" for i in range(21)}},
                    {"request_id": "q1", "decision": "allow", "answers": {"Какой цвет?": "о" * 2001}},
                    {"request_id": "q1", "decision": "allow", "answers": {"в" * 2001: "о"}}):
            assert studio.c.post(chat_url(aid, "answer"), json=bad).status_code == 422, bad
        assert len(m.requests) == before                       # неверное — до моста
        answers = {"Какой цвет?": "Синий"}
        assert studio.c.post(chat_url(aid, "answer"), json={"request_id": "q1", "decision": "allow",
                                                              "answers": answers}).status_code == 204
        assert list(events) == [("asked", {"request_id": "q1"}), ("done", done_data(aid))]
    assert {k: sent(m, "answer")[-1][k] for k in ("decision", "message", "answers")} == {
        "decision": "allow", "message": None, "answers": answers}
    m.script = [ASK, DONE]
    with studio.c.stream("POST", chat_url(aid, "turn"), json={"text": "Убери a.txt"}) as r:
        events = events_of(r.iter_lines())
        until(events, "ask")
        assert studio.c.post(chat_url(aid, "answer"), json={"request_id": "r1", "decision": "deny",
                                                              "message": "Сначала покажи"}).status_code == 204
        assert [e for e, _ in events] == ["asked", "done"]
    assert {k: sent(m, "answer")[-1][k] for k in ("decision", "message", "answers")} == {
        "decision": "deny", "message": "Сначала покажи", "answers": None}


def test_two_watchers_see_the_card_and_its_close(studio):
    aid, m, _ = setup(studio)
    m.script = [text("Удаляю"), ASK, text("Готово"), DONE]
    with studio.c.stream("POST", chat_url(aid, "turn"), json={"text": "Убери a.txt"}) as r:
        first = events_of(r.iter_lines())
        until(first, "ask")
        with studio.c.stream("GET", chat_url(aid, "live")) as r2:
            assert r2.status_code == 200 and r2.headers["content-type"].startswith("text/event-stream")
            second = events_of(r2.iter_lines())
            assert next(second) == ("snapshot", {"text": "Удаляю", "steps": [], "asks": [CARD]})
            assert studio.c.post(chat_url(aid, "answer"), json={"request_id": "r1", "decision": "allow"}).status_code == 204
            tail = [("asked", {"request_id": "r1"}), ("text", {"text": "Готово"}), ("done", done_data(aid))]
            assert list(second) == tail
            # снимок раздаётся всем наблюдателям чата («вместо прежнего» — потоку хода он не мешает)
            assert list(first) == [("snapshot", {"text": "Удаляю", "steps": [], "asks": [CARD]}), *tail]
    assert [r["op"] for r in m.requests][-3:] == ["turn", "watch", "answer"]
    no_watchers(studio)


def test_live_of_an_idle_chat_says_idle(studio):
    aid, m, _ = setup(studio)
    with studio.c.stream("GET", chat_url(aid, "live")) as r:
        assert list(events_of(r.iter_lines())) == [("idle", {})]
    [req] = sent(m, "watch")
    assert (req["project"], req["chat"]) == (ALPHA, A)
    no_watchers(studio)
    r = studio.c.get(url(aid, "projects", ALPHA, "chats", "22222222-2222-4222-8222-222222222222", "live"))
    assert (r.status_code, detail(r)["code"]) == (404, "not_found")
    assert not bridge.WATCHERS


def test_new_studio_chat_has_empty_history_and_idle_live(studio):
    """«Новый чат» без хода: на машине сессии нет (мост — `not_found`), а строка студии есть — пустая история и
    `idle`, не 404. Без строки (чужой проект с тем же чатом) — 404, как раньше."""
    aid, m, _ = setup(studio)
    chat = studio.c.post(url(aid, "projects", ALPHA, "chats")).json()["chat"]
    r = studio.c.get(url(aid, "projects", ALPHA, "chats", chat))
    assert (r.status_code, r.json()) == (200, {"items": [], "start": 0, "total": 0})
    with studio.c.stream("GET", url(aid, "projects", ALPHA, "chats", chat, "live")) as r:
        assert list(events_of(r.iter_lines())) == [("idle", {})]
    assert not bridge.WATCHERS
    for path in ((), ("live",)):
        r = studio.c.get(url(aid, "projects", BETA, "chats", chat, *path))
        assert (r.status_code, detail(r)["code"]) == (404, "not_found"), path
    assert [q["op"] for q in m.requests][-4:] == ["history", "watch", "history", "watch"]


def test_new_studio_chat_refusal_is_not_a_warning(studio, caplog):
    """`not_found` нового чата студии — норма, не WARNING на каждое открытие; без строки студии — WARNING."""
    caplog.set_level(logging.DEBUG)
    aid, m, _ = setup(studio)
    chat = studio.c.post(url(aid, "projects", ALPHA, "chats")).json()["chat"]
    assert studio.c.get(url(aid, "projects", ALPHA, "chats", chat)).status_code == 200
    with studio.c.stream("GET", url(aid, "projects", ALPHA, "chats", chat, "live")) as r:
        assert list(events_of(r.iter_lines())) == [("idle", {})]
    warned = lambda: [r.getMessage() for r in caplog.records if r.name == "studio" and r.levelno >= logging.WARNING]  # noqa: E731
    assert warned() == []
    for path in ((), ("live",)):
        assert studio.c.get(url(aid, "projects", BETA, "chats", chat, *path)).status_code == 404
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]
    assert warned() == [f"пользователь {uid}: компьютер {aid} — history: not_found",
                        f"пользователь {uid}: компьютер {aid} — watch: not_found"]


def test_live_skips_events_before_the_first_snapshot(studio, monkeypatch):
    """Мост мог успеть послать события хода раньше снимка — они уже в снимке: поток `live` их пропускает.
    `done` до снимка — ход кончился, поток тоже."""
    aid, m, link = setup(studio)
    m.running[A] = {"turn": "t-9", "project": ALPHA, "rest": [], "text": "Уже", "steps": [], "asks": {}}
    real = m._op_watch
    before = []

    def watch(req, evs):
        data = real(req, evs)
        evs[:0] = [m._ev(A, e) for e in before]
        return data

    monkeypatch.setattr(m, "_op_watch", watch)
    before[:] = [text("раньше снимка"), STEP]
    with studio.c.stream("GET", chat_url(aid, "live")) as r:
        events = events_of(r.iter_lines())
        assert next(events) == ("snapshot", {"text": "Уже", "steps": [], "asks": []})
        fake_bridge.emit(studio, link, ALPHA, A, text("дальше"), DONE)
        assert list(events) == [("text", {"text": "дальше"}), ("done", done_data(aid))]
    before[:] = [text("x"), DONE]
    with studio.c.stream("GET", chat_url(aid, "live")) as r:
        assert list(events_of(r.iter_lines())) == [("done", done_data(aid))]
    no_watchers(studio)


def test_lock_ends_the_streams_of_the_session_with_closed(studio):
    """Блокировка обрывает поток; после отпирания ход возобновляется без повторного PIN компьютера."""
    aid, m, link = setup(studio)
    m.script = [text("Удаляю"), ASK]
    with studio.c.stream("POST", chat_url(aid, "turn"), json={"text": "Убери a.txt"}) as r:
        events = events_of(r.iter_lines())
        until(events, "ask")
        assert studio.c.post("/api/lock/now").status_code == 204
        assert list(events) == [("closed", {})]
    no_watchers(studio)
    assert A in m.running
    assert studio.c.post("/api/lock/password", json={"password": "password-123"}).status_code == 204
    settle(studio)
    with studio.c.stream("GET", chat_url(aid, "live")) as r:
        assert r.status_code == 200
        events = events_of(r.iter_lines())
        assert next(events)[0] == "snapshot"
        fake_bridge.emit(studio, link, ALPHA, A, DONE)
        assert list(events) == [("done", done_data(aid))]


def test_streams_end_closed_when_the_session_ends_or_locks_by_time(studio, monkeypatch):
    """Выход, смена пароля, отключение (`forget` не зовётся) и блокировка по сроку отметки: поток ловит это сам —
    на тике пинга и на событии, если с прошлой сверки прошло `CHECK_SEC`."""
    aid, m, link = setup(studio)
    digest = security.digest(studio.c.cookies[auth.COOKIE])
    m.script = [ASK]
    with studio.c.stream("POST", chat_url(aid, "turn"), json={"text": "Убери a.txt"}) as r:
        events = events_of(r.iter_lines())
        until(events, "ask")
        state.DB.execute("DELETE FROM sessions WHERE digest = ?", (digest,))
        fake_bridge.emit(studio, link, ALPHA, A, text("сразу"))         # сверка была только что — не сверяет
        assert next(events) == ("text", {"text": "сразу"})
        studio.clock[0] += claude_code.CHECK_SEC
        fake_bridge.emit(studio, link, ALPHA, A, text("тайна"))
        assert list(events) == [("closed", {})]
    no_watchers(studio)
    monkeypatch.setattr(claude_code, "PING_SEC", 0.01)
    signin(studio, "owner")
    assert studio.c.post(url(aid, "open"), json={"pin": None}).status_code == 204
    state.DB.execute("UPDATE users SET screen_lock = 1")
    assert studio.c.post("/api/lock/alive", json={"touch": True}).status_code == 200      # телефон, на экране
    m.running.clear()
    m.script = [ASK]
    with studio.c.stream("POST", chat_url(aid, "turn"), json={"text": "Убери a.txt"}) as r:
        events = events_of(r.iter_lines())
        until(events, "ask")
        assert next(events) == ("ping", None)
        studio.clock[0] += auth.STALE_SEC + 1                          # отметок нет — заперто, без lock/now
        assert [e for e, _ in events if e != "ping"] == ["closed"]
    no_watchers(studio)


def test_stop_ends_the_turn_stopped(studio):
    aid, m, _ = setup(studio)
    m.script = [text("Удаляю"), ASK, text("Готово"), DONE]
    with studio.c.stream("POST", chat_url(aid, "turn"), json={"text": "Убери a.txt"}) as r:
        events = events_of(r.iter_lines())
        until(events, "ask")
        s = studio.c.post(chat_url(aid, "stop"))
        assert (s.status_code, s.content) == (204, b"")
        assert list(events) == [("asked", {"request_id": "r1"}), ("done", done_data(aid, "stopped"))]
    assert {k: sent(m, "stop")[0][k] for k in ("project", "chat")} == {"project": ALPHA, "chat": A}
    r = studio.c.post(chat_url(aid, "stop"))
    assert (r.status_code, detail(r)["code"]) == (409, "idle")


def test_lost_link_ends_streams_with_offline(studio):
    aid, m, link = setup(studio)
    m.script = [text("Удаляю"), ASK]
    with studio.c.stream("POST", chat_url(aid, "turn"), json={"text": "Убери a.txt"}) as r:
        events = events_of(r.iter_lines())
        until(events, "ask")
        fake_bridge.drop(studio, link)
        assert list(events) == [("offline", {})]
    no_watchers(studio)
    r = studio.c.post(chat_url(aid, "answer"), json={"request_id": "r1", "decision": "allow"})
    assert (r.status_code, detail(r)) == (409, {"code": "offline", "message": "компьютер не на связи"})
    r = studio.c.get(chat_url(aid, "live"))
    assert (r.status_code, detail(r)["code"]) == (409, "offline")
    assert not bridge.WATCHERS


def test_answer_needs_no_memory_of_the_card(studio):
    aid, m, _ = setup(studio)
    r = studio.c.post(chat_url(aid, "answer"), json={"request_id": "never-seen_1", "decision": "allow"})
    assert (r.status_code, detail(r)) == (410, {"code": "gone", "message": "запроса уже нет"})
    assert sent(m, "answer")[0]["request_id"] == "never-seen_1"
    # карточку видел поток, которого уже нет: ответ всё равно доходит
    m.script = [ASK, DONE]
    with studio.c.stream("POST", chat_url(aid, "turn"), json={"text": "Убери a.txt"}) as r:
        until(events_of(r.iter_lines()), "ask")
    no_watchers(studio)
    assert studio.c.post(chat_url(aid, "answer"), json={"request_id": "r1", "decision": "allow"}).status_code == 204
    assert A not in m.running


def test_idle_stream_pings(studio, monkeypatch):
    monkeypatch.setattr(claude_code, "PING_SEC", 0.01)
    aid, m, _ = setup(studio)
    m.script = [ASK, DONE]
    with studio.c.stream("POST", chat_url(aid, "turn"), json={"text": "Убери a.txt"}) as r:
        events = events_of(r.iter_lines())
        until(events, "ask")
        assert next(events) == ("ping", None)
        assert studio.c.post(chat_url(aid, "answer"), json={"request_id": "r1", "decision": "allow"}).status_code == 204
        assert [e for e, _ in events if e != "ping"] == ["asked", "done"]


def test_turn_text_never_reaches_the_log(studio, caplog):
    caplog.set_level(logging.DEBUG)
    aid, m, _ = setup(studio)
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]
    m.script = [text("Ответ-секрет"), ASK, DONE]
    with studio.c.stream("POST", chat_url(aid, "turn"), json={"text": "Задание-секрет 7319", "model": "sonnet",
                                                            "effort": "high"}) as r:
        events = events_of(r.iter_lines())
        until(events, "ask")
        assert studio.c.post(chat_url(aid, "turn"), json={"text": "Второе-секрет"}).status_code == 409
        assert studio.c.post(chat_url(aid, "answer"), json={"request_id": "r1", "decision": "deny",
                                                              "message": "Отказ-секрет"}).status_code == 204
        list(events)
    studio.c.post(chat_url(aid, "answer"), json={"request_id": "r1", "decision": "allow",
                                                 "answers": {"Вопрос-секрет": "Ответ-секрет"}})
    studio.c.post(chat_url(aid, "stop"))
    with studio.c.stream("GET", chat_url(aid, "live")) as r:
        list(events_of(r.iter_lines()))
    text_ = "\n".join(r.getMessage() for r in caplog.records if r.name == "studio")
    for line in (f"пользователь {uid}: компьютер {aid} — ход",
                 f"пользователь {uid}: компьютер {aid} — turn: running",
                 f"пользователь {uid}: компьютер {aid} — answer: gone",
                 f"пользователь {uid}: компьютер {aid} — stop: idle"):
        assert line in text_, line
    for secret in ("секрет", "7319", "Bash", "a.txt", "sonnet", "alpha", "Рабочий", ALPHA, A, "r1"):
        assert secret not in text_, secret


# ---- файлы, правка, просмотр PDF, git (задача 6) ------------------------------------

PDF = scan_pdf("first page", "second page")
APP = "print('привет')\n".encode()


def with_files(s):
    """Владелец, компьютер открыт; у «alpha» — README, исходник, двухстраничный PDF и двоичный файл."""
    aid, m, link = setup(s)
    m.projects["alpha"]["files"] = {"README.md": b"# alpha\n", "src/app.py": APP, "docs/a.pdf": PDF,
                                    "bin/x.dat": b"ab\0cd"}
    return aid, m, link


def project_url(aid, *rest) -> str:
    return url(aid, "projects", ALPHA, *rest)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_files_list_and_read_and_save(studio):
    aid, m, _ = with_files(studio)
    r = studio.c.post(project_url(aid, "files"), json={"path": ""})
    assert r.status_code == 200
    assert [(i["name"], i["dir"]) for i in r.json()["items"]] == [
        ("bin", True), ("docs", True), ("src", True), ("README.md", False)]
    [item] = studio.c.post(project_url(aid, "files"), json={"path": "src"}).json()["items"]
    assert (item["name"], item["dir"], item["size"]) == ("app.py", False, len(APP))
    assert {k: m.requests[-1][k] for k in ("op", "project", "path")} == {"op": "files", "project": ALPHA,
                                                                         "path": "src"}
    r = studio.c.post(project_url(aid, "read"), json={"path": "src/app.py"})
    assert (r.status_code, r.json()) == (200, {"text": "print('привет')\n", "version": sha(APP)})
    new = "print('пока')\r\n"
    r = studio.c.put(project_url(aid, "file"), json={"path": "src/app.py", "text": new, "version": sha(APP)})
    assert (r.status_code, r.json()) == (200, {"version": sha(new.encode())})
    assert m.projects["alpha"]["files"]["src/app.py"] == new.encode()              # концы строк как есть
    r = studio.c.put(project_url(aid, "file"), json={"path": "src/app.py", "text": "x", "version": sha(APP)})
    assert r.status_code == 409
    assert {k: detail(r)[k] for k in ("code", "version")} == {"code": "conflict", "version": sha(new.encode())}
    assert m.projects["alpha"]["files"]["src/app.py"] == new.encode()


def test_bad_paths_never_reach_the_bridge(studio):
    aid, m, _ = with_files(studio)
    before = len(m.requests)
    bad = ("../x", "/etc", "a\\b", "a//b", "./a", "a/..", "a\x00b", "a\nb", "a/", "x" * 1025, 5, None, ["a"])
    for path in bad:
        for r in (studio.c.post(project_url(aid, "files"), json={"path": path}),
                  studio.c.post(project_url(aid, "read"), json={"path": path}),
                  studio.c.put(project_url(aid, "file"), json={"path": path, "text": "", "version": "v"}),
                  studio.c.post(project_url(aid, "pdf"), json={"path": path, "page": 1})):
            assert (r.status_code, detail(r)) == (422, {"code": "path", "message": "неверный путь"}), (path, r.url)
    assert len(m.requests) == before
    r = studio.c.post(project_url(aid, "read"), json={"path": "x" * claude_code.PATH_MAX})     # длина на пределе
    assert (r.status_code, detail(r)["code"]) == (404, "not_found")
    assert claude_code.check_path("") == "" and claude_code.check_path("a/b.c/..d") == "a/b.c/..d"


def test_bridge_refusals_map_to_statuses(studio, monkeypatch):
    aid, m, _ = with_files(studio)
    m.projects["alpha"]["files"]["big.txt"] = b"x" * (claude_code.FILE_MAX + 1)
    for path, status, code in (("bin/x.dat", 415, "binary"), ("big.txt", 413, "too_large"),
                               ("nope.txt", 404, "not_found")):
        r = studio.c.post(project_url(aid, "read"), json={"path": path})
        assert (r.status_code, detail(r)["code"]) == (status, code), path

    def outside(req, events):
        raise Refused("outside", "путь вне проекта")

    monkeypatch.setattr(m, "_op_read", outside)
    r = studio.c.post(project_url(aid, "read"), json={"path": "link/secret"})
    assert (r.status_code, detail(r)) == (403, {"code": "outside", "message": "путь вне проекта"})


def test_read_only_file_save_is_409_with_the_bridge_words(studio):
    """Файл «только для чтения» мост не пишет — отказ `read_only` его словами, а не «мост ответил непонятно»."""
    aid, m, _ = with_files(studio)
    m.read_only.add("src/app.py")
    r = studio.c.put(project_url(aid, "file"), json={"path": "src/app.py", "text": "x", "version": sha(APP)})
    assert (r.status_code, detail(r)) == (409, {"code": "read_only", "message": "файл только для чтения"})
    assert m.projects["alpha"]["files"]["src/app.py"] == APP


def test_file_body_over_the_limit_is_413_before_the_bridge(studio):
    aid, m, _ = with_files(studio)
    before = len(m.requests)
    words = {"code": "too_large", "message": "файл больше 2 МиБ"}
    for text in ("x" * (claude_code.FILE_MAX + 1),                    # тело в пределе, текст — нет
                 "я" * (claude_code.FILE_MAX // 2 + 1),               # знаков меньше предела, байтов больше
                 "x" * claude_code.FILE_BODY_MAX):                    # тело больше предела
        body = json.dumps({"path": "src/app.py", "text": text, "version": sha(APP)}, ensure_ascii=False).encode()
        r = studio.c.put(project_url(aid, "file"), content=body, headers={"Content-Type": "application/json"})
        assert (r.status_code, detail(r)) == (413, words), len(body)
    for body in (b"not json", b"[]", json.dumps({"path": "src/app.py", "text": 5, "version": "v"}).encode(),
                 json.dumps({"path": "src/app.py", "text": "x"}).encode(),
                 b'{"path": "src/app.py", "text": "\\ud800", "version": "v"}'):
        r = studio.c.put(project_url(aid, "file"), content=body, headers={"Content-Type": "application/json"})
        assert (r.status_code, detail(r)["code"]) == (422, "invalid"), body
    assert len(m.requests) == before
    assert m.projects["alpha"]["files"]["src/app.py"] == APP
    text = "x" * claude_code.FILE_MAX                                 # ровно предел — проходит
    r = studio.c.put(project_url(aid, "file"), json={"path": "src/app.py", "text": text, "version": sha(APP)})
    assert (r.status_code, r.json()) == (200, {"version": sha(text.encode())})


def test_pdf_pages_render_once_per_fetch(studio, monkeypatch):
    monkeypatch.setattr(ocr_render, "MAX_SIDE", 200)                  # рендер дёшев: тест не о качестве
    aid, m, _ = with_files(studio)

    def page(n, path="docs/a.pdf"):
        return studio.c.post(project_url(aid, "pdf"), json={"path": path, "page": n})

    r = page(1)
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG") and r.headers["x-studio-pages"] == "2"
    [raw] = sent(m, "raw")
    assert (raw["project"], raw["path"]) == (ALPHA, "docs/a.pdf")
    r = page(2)
    assert r.status_code == 200 and r.content.startswith(b"\x89PNG") and r.headers["x-studio-pages"] == "2"
    assert len(sent(m, "raw")) == 1                                   # тот же файл — из памяти
    r = page(3)
    assert (r.status_code, detail(r)) == (404, {"code": "page", "message": "нет такой страницы"})
    assert len(sent(m, "raw")) == 1
    studio.clock[0] += claude_code.PDF_TTL_SEC
    assert page(1).status_code == 200 and len(sent(m, "raw")) == 2   # срок вышел — заново с машины
    r = page(1, "README.md")
    assert (r.status_code, detail(r)) == (415, {"code": "not_pdf", "message": "это не PDF"})
    assert len(sent(m, "raw")) == 3
    assert page(2).status_code == 200 and len(sent(m, "raw")) == 3   # не PDF не вытеснил прежний
    m.projects["alpha"]["files"]["docs/b.pdf"] = PDF
    assert page(1, "docs/b.pdf").status_code == 200 and len(sent(m, "raw")) == 4
    assert page(1).status_code == 200 and len(sent(m, "raw")) == 5   # в памяти — один файл пользователя
    for n in (0, -1, "1", True, 1.5, None):
        assert page(n).status_code == 422, n
    assert len(sent(m, "raw")) == 5


def test_pdf_odd_bytes(studio, monkeypatch):
    aid, m, _ = with_files(studio)
    m.projects["alpha"]["files"]["bad.pdf"] = b"%PDF-1.7 garbage"
    r = studio.c.post(project_url(aid, "pdf"), json={"path": "bad.pdf", "page": 1})
    assert (r.status_code, detail(r)) == (422, {"code": "pdf", "message": ocr_render.BROKEN})
    assert claude_code.PDFS == {}                                     # битое не запоминается
    real = m._op_raw
    monkeypatch.setattr(m, "_op_raw", lambda req, events: {**real(req, events), "data": "не base64!"})
    r = studio.c.post(project_url(aid, "pdf"), json={"path": "docs/a.pdf", "page": 1})
    assert (r.status_code, detail(r)) == (502, {"code": "bridge", "message": "мост ответил непонятно"})


def test_pdf_memory_is_per_session(studio, monkeypatch):
    """Страницы из памяти — только той сессии, что открыла компьютер: другая сессия того же пользователя
    без PIN получает `closed` от моста, а не чужую память."""
    monkeypatch.setattr(ocr_render, "MAX_SIDE", 200)
    aid, m, _ = with_files(studio)
    assert studio.c.post(project_url(aid, "pdf"), json={"path": "docs/a.pdf", "page": 1}).status_code == 200
    studio.c.cookies.clear()
    signin(studio, "owner")
    r = studio.c.post(project_url(aid, "pdf"), json={"path": "docs/a.pdf", "page": 2})
    assert (r.status_code, detail(r)["code"]) == (409, "closed")


def test_pdf_memory_still_asks_the_bridge(studio, monkeypatch):
    """Страница из памяти — только после просьбы `projects` той же сессии: компьютер не на связи — `offline`.
    Заперлась и отперлась — память уходит (`bridge.forget`), файл тянется заново без повторного PIN."""
    monkeypatch.setattr(ocr_render, "MAX_SIDE", 200)
    aid, m, link = with_files(studio)

    def page(n):
        return studio.c.post(project_url(aid, "pdf"), json={"path": "docs/a.pdf", "page": n})

    assert page(1).status_code == 200
    assert page(2).status_code == 200 and len(sent(m, "raw")) == 1 and sent(m, "projects")
    assert studio.c.post("/api/lock/now").status_code == 204
    assert studio.c.post("/api/lock/password", json={"password": "password-123"}).status_code == 204
    settle(studio)
    assert page(2).status_code == 200 and len(sent(m, "raw")) == 2
    assert page(1).status_code == 200 and len(sent(m, "raw")) == 2
    fake_bridge.drop(studio, link)
    r = page(2)
    assert (r.status_code, detail(r)["code"]) == (409, "offline")


def test_pdf_cache_expires_by_ttl_in_background(studio, monkeypatch):
    """Память PDF уходит по сроку при любом обращении к разделу, а не только при следующем `pdf`."""
    monkeypatch.setattr(ocr_render, "MAX_SIDE", 200)
    aid, m, _ = with_files(studio)
    assert studio.c.post(project_url(aid, "pdf"), json={"path": "docs/a.pdf", "page": 1}).status_code == 200
    assert len(claude_code.PDFS) == 1
    studio.clock[0] += claude_code.PDF_TTL_SEC - 1
    assert studio.c.get(url(aid, "projects")).status_code == 200 and len(claude_code.PDFS) == 1
    studio.clock[0] += 1
    assert studio.c.get(url(aid, "projects")).status_code == 200
    assert claude_code.PDFS == {}


def test_pdf_cache_dropped_on_forget(studio, monkeypatch):
    """Сессия заперлась — память её PDF уходит сразу (`bridge.forget`), не дожидаясь срока."""
    monkeypatch.setattr(ocr_render, "MAX_SIDE", 200)
    aid, m, _ = with_files(studio)
    assert studio.c.post(project_url(aid, "pdf"), json={"path": "docs/a.pdf", "page": 1}).status_code == 200
    assert len(claude_code.PDFS) == 1
    assert studio.c.post("/api/lock/now").status_code == 204
    assert claude_code.PDFS == {}


def test_pdf_over_the_raw_limit_is_413(studio, monkeypatch):
    aid, m, _ = with_files(studio)
    for limit in (len(PDF) - 1, 10):                 # байтом меньше файла и далеко меньше
        monkeypatch.setattr(claude_code, "RAW_MAX", limit)
        r = studio.c.post(project_url(aid, "pdf"), json={"path": "docs/a.pdf", "page": 1})
        assert (r.status_code, detail(r)) == (413, {"code": "too_large", "message": "файл больше 10 МиБ"}), limit
    assert claude_code.PDFS == {} and len(sent(m, "raw")) == 2


def test_git_status_passes_through_and_not_git_is_409(studio):
    aid, m, _ = with_files(studio)
    r = studio.c.get(project_url(aid, "git"))
    assert (r.status_code, detail(r)["code"]) == (409, "not_git")
    status = {"branch": "main", "upstream": "origin/main", "ahead": 2, "behind": 0,
              "files": [{"path": "src/app.py", "status": "M"}]}
    m.projects["alpha"]["git"] = status
    r = studio.c.get(project_url(aid, "git"))
    assert (r.status_code, r.json()) == (200, status)
    assert {k: m.requests[-1][k] for k in ("op", "project")} == {"op": "git", "project": ALPHA}


def test_file_paths_never_reach_the_log(studio, caplog, monkeypatch):
    monkeypatch.setattr(ocr_render, "MAX_SIDE", 200)
    caplog.set_level(logging.DEBUG)
    aid, m, _ = with_files(studio)
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]
    m.projects["alpha"]["files"]["тайное/план-секрет.md"] = "Содержимое-секрет".encode()
    m.projects["alpha"]["files"]["тайное/скан-секрет.pdf"] = PDF
    studio.c.post(project_url(aid, "files"), json={"path": "тайное"})
    studio.c.post(project_url(aid, "read"), json={"path": "тайное/план-секрет.md"})
    studio.c.post(project_url(aid, "read"), json={"path": "тайное/нет-секрет.md"})
    studio.c.post(project_url(aid, "read"), json={"path": "../секрет"})
    studio.c.put(project_url(aid, "file"), json={"path": "тайное/план-секрет.md", "text": "Новое-секрет",
                                                 "version": "старая-секрет"})
    studio.c.post(project_url(aid, "pdf"), json={"path": "тайное/скан-секрет.pdf", "page": 1})
    studio.c.post(project_url(aid, "pdf"), json={"path": "тайное/скан-секрет.pdf", "page": 9})
    studio.c.post(project_url(aid, "pdf"), json={"path": "тайное/план-секрет.md", "page": 1})
    studio.c.get(project_url(aid, "git"))
    text_ = "\n".join(r.getMessage() for r in caplog.records if r.name == "studio")
    for line in (f"пользователь {uid}: компьютер {aid} — read: not_found",
                 f"пользователь {uid}: компьютер {aid} — save: conflict",
                 f"пользователь {uid}: компьютер {aid} — git: not_git"):
        assert line in text_, line
    for secret in ("секрет", "тайное", "alpha", "Рабочий", ALPHA, sha(PDF)):
        assert secret not in text_, secret


# ---- стирание истории ----------------------------------------------------------------

def test_wipe_forgets_code_chats_but_keeps_agents_events_and_push(studio):
    """«Удалить всю историю» стирает отметки студии (code_chats), но не компьютеры, не историю подключений и
    не подписки push (спека 6.1); сами сессии — на машине, мост ничего не получает."""
    from test_push import Device
    aid, m, link = setup(studio)
    assert studio.c.post(url(aid, "projects", ALPHA, "chats")).status_code == 201
    assert studio.c.patch(url(aid, "projects", ALPHA, "chats", B), json={"state": "closed"}).status_code == 200
    assert studio.c.post("/api/me/push", json=Device().body()).status_code == 204
    count = lambda table: state.DB.execute(f"SELECT count(*) FROM {table}").fetchone()[0]     # noqa: E731
    before = {t: count(t) for t in ("agents", "agent_events", "push_subs")}
    assert count("code_chats") == 2 and all(before.values())
    requests, frames = len(m.requests), len(link.sent)
    assert studio.c.delete("/api/history").status_code == 200
    assert count("code_chats") == 0
    assert {t: count(t) for t in ("agents", "agent_events", "push_subs")} == before
    assert len(m.requests) == requests and len(link.sent) == frames and link.closed_with is None
    items = studio.c.get(url(aid, "projects", ALPHA, "chats")).json()["items"]
    assert {i["chat"]: i["state"] for i in items} == {A: "open", B: "open"}          # сессии машины остались
