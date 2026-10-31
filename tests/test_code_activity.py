"""Статусы всех проектов: владелец и PIN, без истории чатов."""
import logging

import code_activity
import state
from conftest import make_user, signin
from fake_bridge import FakeMachine, connect, drop, enroll
from test_code import A, ALPHA, B, BETA, setup


def test_activity_reads_all_projects_without_loading_history(studio):
    aid, machine, link = setup(studio)
    link.hello["project_activity"] = True
    machine.running = {A: {"project": ALPHA, "asks": {}}, B: {"project": BETA, "asks": {"r1": {}}}}
    response = studio.c.get(f"/api/code/agents/{aid}/activity")
    assert response.status_code == 200
    assert response.json()["items"] == [
        {"project": ALPHA, "chat": A, "running": True, "asking": False, "engine": "claude"},
        {"project": BETA, "chat": B, "running": True, "asking": True, "engine": "claude"}]
    assert not any(q["op"] == "history" for q in machine.requests)


def test_activity_requires_supported_bridge_and_open_pin_session(studio):
    aid, machine, link = setup(studio, pin="2468", opened=False)
    endpoint = f"/api/code/agents/{aid}/activity"
    assert studio.c.get(endpoint).json()["detail"]["code"] == "unsupported"
    link.hello["project_activity"] = True
    assert studio.c.get(endpoint).json()["detail"]["code"] == "closed"
    assert studio.c.get(f"/api/code/agents/{aid + 1}/activity").status_code == 404


def test_activity_summary_combines_own_computers_and_engines_without_history(studio):
    aid, machine, link = setup(studio, opened=False)
    link.hello["project_activity"] = True
    machine.running = {A: {"project": ALPHA, "asks": {}, "engine": "codex"},
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
            {"project": ALPHA, "chat": A, "running": True, "asking": False, "engine": "codex"},
            {"project": BETA, "chat": B, "running": True, "asking": True, "engine": "opencode"}]},
        {"aid": second, "items": [
            {"project": ALPHA, "chat": A, "running": True, "asking": False, "engine": "claude"}]}]}
    assert not any(req["op"] in {"history", "chats"} for m in (machine, other) for req in m.requests)
    make_user(studio, "friend")
    signin(studio, "friend")
    before = len(machine.requests), len(other.requests)
    assert studio.c.get("/api/code/activity").json() == {"items": []}
    assert before == (len(machine.requests), len(other.requests))


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
        {"aid": aid, "items": [{"project": ALPHA, "chat": A, "running": True, "asking": False, "engine": "claude"}]},
        {"aid": second, "items": []}]}
