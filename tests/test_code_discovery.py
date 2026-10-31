from test_code import A, ALPHA, setup


def test_discovery_requires_capabilities_pin_and_project_ownership(studio):
    aid, machine, link = setup(studio, pin="2468", opened=False)
    base = f"/api/code/agents/{aid}/projects/{ALPHA}"
    for suffix, capability in (("/files/suggest", "file_suggestions"), ("/search", "chat_search")):
        assert studio.c.post(base + suffix, json={"query": "source"}).json()["detail"]["code"] == "unsupported"
        link.hello[capability] = True
        assert studio.c.post(base + suffix, json={"query": "source"}).json()["detail"]["code"] == "closed"
    assert studio.c.post(f"/api/code/agents/{aid}/open", json={"pin": "2468"}).status_code == 204
    machine.projects["alpha"]["files"]["src/source.py"] = b"text"
    assert studio.c.post(base + "/files/suggest", json={"query": "source"}).json()["items"] == ["src/source.py"]
    machine.projects["alpha"]["chats"][A]["messages"] = [{"id": "m", "role": "assistant", "text": "source answer"}]
    response = studio.c.post(base + "/search", json={"query": "source"})
    assert response.status_code == 200 and response.json()["items"][0]["chat"] == A
    assert "source" not in str(response.request.url)
    assert studio.c.post(base + "/search", json={"query": "x" * 201}).status_code == 422
    assert studio.c.post(base + "/files/suggest", json={"query": "a\nb"}).status_code == 422


def test_queue_endpoint_checks_version_and_keeps_text_out_of_url(studio):
    aid, machine, link = setup(studio)
    link.hello["queue_edit"] = machine.queue_edit = True
    path = f"/api/code/agents/{aid}/projects/{ALPHA}/chats/{A}"
    machine.running[A] = {"project": ALPHA, "turn": "turn", "asks": {}, "followups": [], "owner": None}
    result = studio.c.post(path + "/steer", json={"text": "queued source", "mode": "queue"}).json()
    assert result["mode"] == "queue"
    body = {"request_id": result["id"], "version": 1, "text": "corrected"}
    assert studio.c.post(path + "/queue", json={"request_id": result["id"], "version": 1}).status_code == 422
    response = studio.c.post(path + "/queue", json=body)
    assert response.status_code == 200 and response.json()["items"][0]["input_text"] == "corrected"
    assert studio.c.post(path + "/queue", json=body).json()["detail"]["code"] == "conflict"
    assert studio.c.post(path + "/queue", json={**body, "version": 2, "text": None}).json() == {"items": [], "paused": False}


def test_fork_creates_a_separate_owned_record_without_persisting_draft(studio):
    import state

    aid, machine, link = setup(studio)
    link.hello["chat_fork"] = True
    path = f"/api/code/agents/{aid}/projects/{ALPHA}/chats/{A}/fork"
    response = studio.c.post(path, json={"message_id": "m2"})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["chat"] != A and data["draft"] == "2"
    assert len(machine.projects["alpha"]["chats"][data["chat"]]["messages"]) == 2
    record = state.DB.execute("SELECT * FROM code_chats WHERE session_id = ?", (data["chat"],)).fetchone()
    assert record["engine"] == "claude" and record["project"] == ALPHA
    assert "draft" not in record.keys()


def test_worktree_and_subagent_routes_follow_capability_and_child_ownership(studio):
    aid, machine, link = setup(studio)
    base = f"/api/code/agents/{aid}/projects/{ALPHA}"
    link.hello.update(worktrees=True, subagents=True)
    assert studio.c.get(base + "/worktrees").status_code == 200
    assert studio.c.post(base + "/worktrees", json={"name": "../escape"}).status_code == 422
    assert studio.c.post(base + "/worktrees", json={"name": "task"}).json()["name"] == "alpha--task"
    machine.projects["alpha"]["chats"][A]["children"] = {"child": {"messages": []}}
    assert studio.c.get(base + f"/chats/{A}/subagents").json()["items"][0]["id"] == "child"
    assert studio.c.post(base + f"/chats/{A}/subagent", json={"request_id": "child"}).status_code == 200
    assert studio.c.post(base + f"/chats/{A}/subagent", json={"request_id": "someone-else"}).status_code == 404
