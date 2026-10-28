"""Раздел «Claude Code» на сервере (app/claude_code.py): открыть компьютер PIN-ом моста, проекты, чаты (слияние сессий
машины с отметками студии), история; чужое и недопустимое — до моста. Мост — подставной (tests/fake_bridge.py)."""
import logging
import uuid

import bridge
import claude_code
import state
from conftest import make_user, signin
from fake_bridge import FakeMachine, connect, enroll, pid, settle
from test_auth import guarded_routes

A = "0f8e2a52-5d0e-4b8e-9a51-2f3c4d5e6f70"
B = "11111111-2222-4333-8444-555555555555"
ALPHA, BETA = pid("alpha"), pid("beta")

#: тело каждого маршрута раздела — верное по виду; маршрут без строки здесь роняет
#: test_someone_elses_agent_is_404_before_the_bridge. Задачи 5 и 6 дописывают свои маршруты
#: (тела: {"text": "x"}, {"request_id": "r1", "decision": "allow"}, {"path": ""},
#: {"path": "a", "text": "", "version": "v"}, {"path": "a.pdf", "page": 1}).
BODIES = {
    ("POST", "/api/code/agents/1/open"): {"pin": None},
    ("GET", "/api/code/agents/1/projects"): None,
    ("GET", "/api/code/agents/1/projects/1/chats"): None,
    ("POST", "/api/code/agents/1/projects/1/chats"): None,
    ("PATCH", "/api/code/agents/1/projects/1/chats/1"): {"state": "closed"},
    ("GET", "/api/code/agents/1/projects/1/chats/1"): None,
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


def events():
    return [dict(r) for r in state.DB.execute(
        "SELECT kind, agent_id, agent_name, device, ip FROM agent_events WHERE kind <> 'online' ORDER BY id")]


def test_someone_elses_agent_is_404_before_the_bridge(studio):
    aid, m, _ = setup(studio, opened=False)
    make_user(studio, "friend")
    signin(studio, "friend")
    routes = [(method, path) for method, path in guarded_routes() if path.startswith("/api/code/")]
    assert routes
    for method, path in routes:
        assert (method, path) in BODIES, f"нет тела для {method} {path} — допишите в BODIES"
        parts = path.split("/")
        for i in range(1, len(parts)):
            if parts[i] == "1":
                parts[i] = {"agents": str(aid), "projects": ALPHA, "chats": A}[parts[i - 1]]
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


def test_unopened_computer_is_409_closed_and_unlock_closes_it_again(studio):
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
    assert (r.status_code, detail(r)["code"]) == (409, "closed")


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
        {"chat": new, "title": None, "created": now, "modified": now, "origin": "studio", "state": "open",
         "running": False, "asking": False},
        {"chat": A, "title": "Чат студии", "created": 10, "modified": 30, "origin": "studio", "state": "open",
         "running": True, "asking": True},
        {"chat": B, "title": "Чат терминала", "created": 20, "modified": 40, "origin": "other", "state": "closed",
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


def test_history_pages_from_the_end(studio):
    aid, m, _ = setup(studio)
    data = studio.c.get(url(aid, "projects", ALPHA, "chats", A)).json()
    assert ([i["id"] for i in data["items"]], data["start"], data["total"]) == (
        [f"m{i}" for i in range(150, 250)], 150, 250)
    data = studio.c.get(url(aid, "projects", ALPHA, "chats", A), params={"before": 150}).json()
    assert ([i["id"] for i in data["items"]], data["start"]) == ([f"m{i}" for i in range(50, 150)], 50)
    assert m.requests[-1]["limit"] == claude_code.HISTORY_PAGE == 100
    r = studio.c.get(url(aid, "projects", ALPHA, "chats", "22222222-2222-4222-8222-222222222222"))
    assert (r.status_code, detail(r)["code"]) == (404, "not_found")


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
