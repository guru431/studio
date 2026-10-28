"""Раздел «Claude Code» на сервере (app/claude_code.py): открыть компьютер PIN-ом моста, проекты, чаты (слияние сессий
машины с отметками студии), история; чужое и недопустимое — до моста. Мост — подставной (tests/fake_bridge.py)."""
import asyncio
import logging
import uuid

import bridge
import claude_code
import fake_bridge
import state
from conftest import make_user, signin
from fake_bridge import FakeMachine, connect, enroll, pid, settle
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
    ("POST", "/api/code/agents/1/open"): {"pin": None},
    ("GET", "/api/code/agents/1/projects"): None,
    ("GET", "/api/code/agents/1/projects/1/chats"): None,
    ("POST", "/api/code/agents/1/projects/1/chats"): None,
    ("PATCH", "/api/code/agents/1/projects/1/chats/1"): {"state": "closed"},
    ("GET", "/api/code/agents/1/projects/1/chats/1"): None,
    ("POST", "/api/code/agents/1/projects/1/chats/1/turn"): {"text": "x"},
    ("GET", "/api/code/agents/1/projects/1/chats/1/live"): None,
    ("POST", "/api/code/agents/1/projects/1/chats/1/answer"): {"request_id": "r1", "decision": "allow"},
    ("POST", "/api/code/agents/1/projects/1/chats/1/stop"): None,
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
                   ("result", {"id": "s1", "text": "a.txt", "error": False}), ("done", {"status": "done"})]
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
    assert rest == [("asked", {"request_id": "r1"}), ("text", {"text": "Готово"}), ("done", {"status": "done"})]
    [req] = sent(m, "answer")
    assert {k: req[k] for k in ("project", "chat", "request_id", "decision", "message", "answers")} == {
        "project": ALPHA, "chat": A, "request_id": "r1", "decision": "always", "message": None, "answers": None}


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
        assert list(events) == [("asked", {"request_id": "q1"}), ("done", {"status": "done"})]
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
            tail = [("asked", {"request_id": "r1"}), ("text", {"text": "Готово"}), ("done", {"status": "done"})]
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


def test_stop_ends_the_turn_stopped(studio):
    aid, m, _ = setup(studio)
    m.script = [text("Удаляю"), ASK, text("Готово"), DONE]
    with studio.c.stream("POST", chat_url(aid, "turn"), json={"text": "Убери a.txt"}) as r:
        events = events_of(r.iter_lines())
        until(events, "ask")
        s = studio.c.post(chat_url(aid, "stop"))
        assert (s.status_code, s.content) == (204, b"")
        assert list(events) == [("asked", {"request_id": "r1"}), ("done", {"status": "stopped"})]
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
