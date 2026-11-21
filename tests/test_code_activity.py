"""Статусы всех проектов: владелец и PIN, без истории чатов."""
import logging

import code_activity
import code_attention
import state
from conftest import make_user, signin
from fake_bridge import FakeMachine, connect, drop, emit, enroll
from test_code import A, ALPHA, B, BETA, setup


def test_activity_summary_combines_own_computers_and_engines_without_history(studio):
    aid, machine, link = setup(studio, opened=False)
    link.hello["project_activity"] = True
    machine.running = {A: {"project": ALPHA, "asks": {}, "engine": "codex", "queue_paused": True},
                       B: {"project": BETA, "asks": {"r": {}}, "engine": "opencode"}}
    second, _ = enroll(studio, name="Второй ПК")
    other = FakeMachine()
    other.project_activity = True
    other.running = {A: {"project": ALPHA, "asks": {}}}
    connect(studio, second, other)
    assert studio.c.post(f"/api/code/agents/{second}/open", json={"pin": None}).status_code == 204
    result = studio.c.get("/api/code/activity")
    assert result.status_code == 200
    assert result.json() == {"items": [
        {"aid": aid, "items": [
            {"project": ALPHA, "chat": A, "running": True, "asking": False, "engine": "codex", "title": "Чат студии",
             "queue_paused": True},
            {"project": BETA, "chat": B, "running": True, "asking": True, "engine": "opencode", "title": None}]},
        {"aid": second, "items": [
            {"project": ALPHA, "chat": A, "running": True, "asking": False, "engine": "claude", "title": None}]}]}
    assert not any(req["op"] == "history" for m in (machine, other) for req in m.requests)
    make_user(studio, "friend")
    signin(studio, "friend")
    before = len(machine.requests), len(other.requests)
    assert studio.c.get("/api/code/activity").json() == {"items": []}
    assert before == (len(machine.requests), len(other.requests))


def test_background_open_of_a_computer_without_pin_is_recorded_once_in_connection_history(studio):
    """Опрос значка открывает компьютер без PIN для новой сессии — в неудаляемой истории та же строка `open`
    с устройством и адресом, что у явного входа; уже открытый компьютер новой строки не даёт."""
    aid, machine, link = setup(studio, opened=False)
    link.hello["project_activity"] = True

    def opens():
        return [e for e in studio.c.get("/api/agents/events").json()["items"] if e["kind"] == "open"]

    assert opens() == []
    assert studio.c.get("/api/code/activity").status_code == 200
    rows = opens()
    assert len(rows) == 1 and rows[0]["agent_id"] == aid and rows[0]["device"].startswith("браузер ")
    assert "ip" in rows[0]
    assert [req["op"] for req in machine.requests][-3:] == ["activity", "open", "activity"]
    assert studio.c.get("/api/code/activity").status_code == 200
    assert len(opens()) == 1


def test_activity_summary_never_opens_a_computer_of_a_shared_project(studio, monkeypatch):
    """Чужой компьютер участника открывает только владелец: на `closed` опрос значка `open` не шлёт."""
    import claude_code
    aid, machine, link = setup(studio)
    link.hello["project_activity"] = True
    make_user(studio, "friend")
    assert studio.c.post(f"/api/code/agents/{aid}/projects/{ALPHA}/members", json={"login": "friend"}).status_code == 201
    signin(studio, "friend")
    machine.handle = lambda req: ({"t": "res", "id": req.get("id"), "ok": False, "code": "closed",
                                   "message": "x"}, [])
    ops = []
    original = claude_code.ask
    monkeypatch.setattr(claude_code, "ask", lambda request, computer, op, **kw: ops.append(op) or original(
        request, computer, op, **kw))
    assert studio.c.get("/api/code/activity").json() == {"items": [{"aid": aid, "items": []}]}
    assert "open" not in ops
    assert not [e for e in studio.c.get("/api/agents/events").json()["items"] if e["kind"] == "open"]


def test_activity_summary_keeps_pin_closed_and_drops_offline_or_unsupported(studio, caplog):
    aid, machine, link = setup(studio, pin="2468", opened=False)
    link.hello["project_activity"] = True
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="studio"):
        assert studio.c.get("/api/code/activity").json() == {"items": [{"aid": aid, "items": []}]}
    assert [req["op"] for req in machine.requests] == ["activity"]
    assert machine.open_sessions == set() and machine.pin_fails == 0
    assert not caplog.records
    link.hello["project_activity"] = False
    assert studio.c.get("/api/code/activity").json() == {"items": []}
    link.hello["project_activity"] = True
    drop(studio, link)
    assert studio.c.get("/api/code/activity").json() == {"items": []}
    assert [req["op"] for req in machine.requests] == ["activity"]


def test_activity_summary_does_not_contact_bridge_when_section_is_locked(studio):
    aid, machine, link = setup(studio)
    link.hello["project_activity"] = True
    state.DB.execute("UPDATE users SET screen_lock = 1, screen_lock_sections = '[\"code\"]'")
    state.DB.execute("UPDATE sessions SET seen = 0")
    before = len(machine.requests)
    assert studio.c.get("/api/code/activity").status_code == 423
    assert len(machine.requests) == before


def test_activity_summary_keeps_other_computers_when_one_times_out(studio, monkeypatch):
    aid, machine, link = setup(studio)
    link.hello["project_activity"] = True
    second, _ = enroll(studio, name="Не отвечает")
    other = FakeMachine()
    other.project_activity = True
    connect(studio, second, other)

    async def ask(request, computer, op, **kwargs):
        if computer == second:
            raise TimeoutError
        return {"items": [{"project": ALPHA, "chat": A, "running": True, "asking": False, "engine": "claude"}]}

    monkeypatch.setattr(code_activity, "ask", ask)
    assert studio.c.get("/api/code/activity").json() == {"items": [
        {"aid": aid, "items": [{"project": ALPHA, "chat": A, "running": True, "asking": False, "engine": "claude", "title": None}]},
        {"aid": second, "items": []}]}


def test_completed_session_keeps_title_and_unread_until_that_result_is_viewed(studio):
    aid, machine, link = setup(studio)
    link.hello["project_activity"] = True
    path = f"/api/code/agents/{aid}/projects/{ALPHA}/chats/{A}"
    emit(studio, link, ALPHA, A, {"k": "done", "status": "done"})
    first = code_attention.revision(aid, ALPHA, A)
    studio.clock[0] += 7200
    item = studio.c.get("/api/code/activity").json()["items"][0]["items"][0]
    assert item == {"project": ALPHA, "chat": A, "running": False, "asking": False, "engine": "claude",
                    "title": "Чат студии", "unread": True, "attention": first, "finished": studio.clock[0] - 7200}
    assert studio.c.get(path).json()["attention"] == first
    assert studio.c.get("/api/code/activity").json()["items"][0]["items"][0]["unread"]
    # Новое завершение между загрузкой истории и подтверждением просмотра.
    emit(studio, link, ALPHA, A, {"k": "done", "status": "done"})
    latest = code_attention.revision(aid, ALPHA, A)
    assert latest != first
    assert studio.c.post(path + "/seen", json={"attention": first}).status_code == 204
    assert studio.c.get("/api/code/activity").json()["items"][0]["items"][0]["attention"] == latest
    assert studio.c.post(path + "/seen", json={"attention": latest}).status_code == 204
    assert studio.c.get("/api/code/activity").json() == {"items": [{"aid": aid, "items": []}]}
    assert [r["name"] for r in state.DB.execute("PRAGMA table_info(code_attention)")] == [
        "agent_id", "project", "chat", "revision", "finished", "seen"]


def test_unread_is_per_user_and_member_sees_only_completions_after_the_grant(studio):
    """FINDINGS 07:F2: просмотр участника не снимает «непрочитано» у владельца и наоборот; завершения до выдачи
    участнику непрочитанными не показываются."""
    aid, machine, link = setup(studio)
    link.hello["project_activity"] = True
    third = "33333333-4444-4555-8666-777777777777"
    emit(studio, link, ALPHA, third, {"k": "done", "status": "done"})      # до выдачи
    studio.clock[0] += 60
    make_user(studio, "friend")
    assert studio.c.post(f"/api/code/agents/{aid}/projects/{ALPHA}/members", json={"login": "friend"}).status_code == 201
    studio.clock[0] += 60
    emit(studio, link, ALPHA, A, {"k": "done", "status": "done"})
    token = code_attention.revision(aid, ALPHA, A)
    path = f"/api/code/agents/{aid}/projects/{ALPHA}/chats/{A}/seen"

    def unread():
        return {i["chat"] for g in studio.c.get("/api/code/activity").json()["items"] for i in g["items"] if i.get("unread")}

    assert unread() == {A, third}
    signin(studio, "friend")
    assert unread() == {A}
    assert studio.c.post(path, json={"attention": token}).status_code == 204
    assert unread() == set()
    signin(studio, "owner")
    assert unread() == {A, third}                                          # у владельца отметка на месте
    assert studio.c.post(path, json={"attention": token}).status_code == 204
    assert unread() == {third}
    emit(studio, link, ALPHA, A, {"k": "done", "status": "done"})          # новое завершение — снова обоим
    assert unread() == {A, third}
    signin(studio, "friend")
    assert unread() == {A}


def test_step_53_keeps_seen_marks_for_owner_and_members(env):
    """Шаг 53: прежняя общая отметка `seen` переходит владельцу и участникам проекта; удаление отметки или учётки
    уносит их строки."""
    from historical_schema import install_schema
    import db
    conn = db.connect(env.db_path)
    install_schema(conn, 52)
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'o', 'O', 'h', 0), (2, 'm', 'M', 'h', 0)")
    conn.execute("INSERT INTO agents(id, user_id, name, key_hash, created) VALUES (1, 1, 'PC', 'k', 0)")
    conn.execute("INSERT INTO project_members(agent_id, project, user_id, created) VALUES (1, 'p', 2, 0)")
    conn.execute("INSERT INTO code_attention VALUES (1, 'p', 'seen', 'r1', 1, 1), (1, 'p', 'new', 'r2', 1, 0),"
                 " (1, 'q', 'other', 'r3', 1, 1)")
    db.migrate(conn)
    assert sorted(tuple(r) for r in conn.execute("SELECT chat, user_id, revision FROM code_attention_seen")) == [
        ("other", 1, "r3"), ("seen", 1, "r1"), ("seen", 2, "r1")]
    conn.execute("DELETE FROM users WHERE id = 2")
    conn.execute("DELETE FROM code_attention WHERE chat = 'other'")
    assert [tuple(r) for r in conn.execute("SELECT chat, user_id FROM code_attention_seen")] == [("seen", 1)]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_unread_and_seen_require_own_open_computer(studio):
    aid, machine, link = setup(studio, pin="2468")
    link.hello["project_activity"] = True
    emit(studio, link, ALPHA, A, {"k": "done", "status": "error"})
    token = code_attention.revision(aid, ALPHA, A)
    path = f"/api/code/agents/{aid}/projects/{ALPHA}/chats/{A}/seen"
    machine.open_sessions.clear()
    assert studio.c.get("/api/code/activity").json() == {"items": [{"aid": aid, "items": []}]}
    assert studio.c.post(path, json={"attention": token}).json()["detail"]["code"] == "closed"
    assert state.DB.execute("SELECT seen FROM code_attention").fetchone()[0] == 0
    make_user(studio, "friend")
    signin(studio, "friend")
    assert studio.c.post(path, json={"attention": token}).status_code == 404
    assert studio.c.get("/api/code/activity").json() == {"items": []}


def test_project_seen_marks_only_listed_revisions_of_that_project(studio):
    """«Отметить прочитанными» проекта (D2): снимаются только переданные ревизии этого проекта; завершение, пришедшее
    после показа панели, и сессии другого проекта остаются непрочитанными."""
    aid, machine, link = setup(studio)
    link.hello["project_activity"] = True
    third = "33333333-4444-4555-8666-777777777777"
    emit(studio, link, ALPHA, A, {"k": "done", "status": "done"})
    emit(studio, link, ALPHA, third, {"k": "done", "status": "done"})
    emit(studio, link, BETA, B, {"k": "done", "status": "done"})
    shown = {chat: code_attention.revision(aid, ALPHA, chat) for chat in (A, third)}
    emit(studio, link, ALPHA, third, {"k": "done", "status": "done"})      # новое завершение после показа
    path = f"/api/code/agents/{aid}/projects/{ALPHA}/seen"
    body = {"items": [{"chat": chat, "attention": revision} for chat, revision in shown.items()]}
    assert studio.c.post(path, json=body).status_code == 204
    left = {i["chat"] for i in studio.c.get("/api/code/activity").json()["items"][0]["items"] if i.get("unread")}
    assert left == {third, B}
    # ревизия чужого проекта по адресу этого не снимается
    other = {"items": [{"chat": B, "attention": code_attention.revision(aid, BETA, B)}]}
    assert studio.c.post(path, json=other).status_code == 204
    assert code_attention.revision(aid, BETA, B) is not None
    assert state.DB.execute("SELECT seen FROM code_attention WHERE chat = ?", (B,)).fetchone()[0] == 0
    for bad in ({"items": [{"chat": "x", "attention": "0" * 32}]}, {"items": [{"chat": A, "attention": "x"}]},
                {"items": [{"chat": A, "attention": "0" * 32}] * 201}, {}):
        assert studio.c.post(path, json=bad).status_code == 422
    assert studio.c.post(f"/api/code/agents/{aid}/projects/ALPHA/seen", json=body).status_code == 422


def test_project_seen_requires_own_open_computer(studio):
    aid, machine, link = setup(studio, pin="2468")
    link.hello["project_activity"] = True
    emit(studio, link, ALPHA, A, {"k": "done", "status": "done"})
    body = {"items": [{"chat": A, "attention": code_attention.revision(aid, ALPHA, A)}]}
    path = f"/api/code/agents/{aid}/projects/{ALPHA}/seen"
    machine.open_sessions.clear()
    assert studio.c.post(path, json=body).json()["detail"]["code"] == "closed"
    assert state.DB.execute("SELECT seen FROM code_attention").fetchone()[0] == 0
    make_user(studio, "friend")
    signin(studio, "friend")
    assert studio.c.post(path, json=body).status_code == 404
    assert state.DB.execute("SELECT seen FROM code_attention").fetchone()[0] == 0


def test_history_does_not_acknowledge_completion_that_arrived_during_its_read(studio, monkeypatch):
    aid, machine, link = setup(studio)
    emit(studio, link, ALPHA, A, {"k": "done", "status": "done"})
    previous = code_attention.revision(aid, ALPHA, A)
    original = machine._op_history

    def history(req, events):
        data = original(req, events)
        events.append({"t": "ev", "project": ALPHA, "chat": A, "e": {"k": "done", "status": "done"}})
        return data

    monkeypatch.setattr(machine, "_op_history", history)
    response = studio.c.get(f"/api/code/agents/{aid}/projects/{ALPHA}/chats/{A}")
    assert response.json()["attention"] == previous
    assert code_attention.revision(aid, ALPHA, A) != previous
