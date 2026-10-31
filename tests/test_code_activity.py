"""Статусы всех проектов: владелец и PIN, без истории чатов."""
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
