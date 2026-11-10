"""Список подзадач сохраняет завершённые записи и принимает живые lifecycle-события SDK."""

import json

import pytest
from claude_agent_sdk._internal.message_parser import parse_message

from fake_sdk import FakeFactory, result_ok
from studio_agent import sessions, store, subagents
from studio_agent.turns import Runner, _Turn

from test_turns import CHAT, PID, project, marked, settle


def lifecycle(subtype, child="child", **fields):
    return parse_message({"type": "system", "subtype": subtype, "task_id": child,
                          "uuid": "message", "session_id": CHAT, **fields})


@pytest.mark.parametrize("ending,fields,status", [
    ("task_notification", {"status": "completed", "output_file": "private", "summary": "private summary"}, "completed"),
    ("task_notification", {"status": "failed", "output_file": "private", "summary": "private summary"}, "failed"),
    ("task_updated", {"patch": {"status": "killed"}}, "stopped"),
])
async def test_claude_subagent_lifecycle_streams_terminal_status_and_persists_only_metadata(
        home, project, monkeypatch, ending, fields, status):
    sent = []
    factory = FakeFactory([
        lifecycle("task_started", description="Проверка приватного запроса", task_type="local_agent"),
        lifecycle("task_started", "shell", description="Команда", task_type="local_bash"),
        lifecycle(ending, **fields), result_ok()])
    runner = Runner(sent.append, choices=store.Choices(home), cli_path=None, factory=factory)
    runner.start(project, PID, CHAT, "Задание", None, None)
    await settle(runner)
    progress = [frame["e"]["agents"] for frame in sent if frame["e"]["k"] == "progress"]
    assert [rows[0]["status"] for rows in progress] == ["running", status]
    assert len(progress[-1]) == 1 and progress[-1][0]["id"] == "child"
    saved = runner.choices.details(CHAT)
    assert saved["subagent_statuses"] == {"child": status}
    assert "приватного" not in json.dumps(saved, ensure_ascii=False)
    monkeypatch.setattr(sessions, "exists", lambda *a: True)
    monkeypatch.setattr(subagents.claude_agent_sdk, "list_subagents", lambda *a, **kw: ["old", "child"])
    children = subagents.list_children(project, CHAT, choices=runner.choices)["items"]
    assert {row["id"]: row["status"] for row in children} == {"old": "unknown", "child": status}


async def test_background_subagent_is_not_persisted_as_forever_running(home, project):
    factory = FakeFactory([lifecycle("task_started", description="Фоновая проверка", task_type="local_agent"), result_ok()])
    runner = Runner(lambda frame: None, choices=store.Choices(home), cli_path=None, factory=factory)
    runner.start(project, PID, CHAT, "Задание", None, None)
    await settle(runner)
    assert runner.choices.details(CHAT)["subagent_statuses"] == {"child": "unknown"}
    factory.script = [lifecycle("task_updated", patch={"status": "completed"}), result_ok()]
    runner.start(project, PID, CHAT, "Продолжай", None, None)
    await settle(runner)
    assert runner.choices.details(CHAT)["subagent_statuses"] == {"child": "completed"}


def test_live_subagents_exist_before_the_first_transcript_and_history_keeps_completed(home, tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "exists", lambda *a: False)
    monkeypatch.setattr(subagents.claude_agent_sdk, "list_subagents", lambda *a, **kw: [])
    live = [{"id": "new", "title": "Проверка", "status": "running"}]
    assert subagents.list_children(tmp_path, CHAT, live=live)["items"] == live
    choices = store.Choices(home)
    runner = Runner(lambda frame: None, choices=choices, cli_path=None)
    turn = _Turn(tmp_path, PID, CHAT, "turn")
    runner._turns[CHAT] = turn
    runner._subtask(turn, lifecycle("task_started", description="Проверка", task_type="local_agent"))
    runner._subtask(turn, lifecycle("task_updated", patch={"status": "completed"}))
    assert runner.children(PID, CHAT)[0]["status"] == "completed"
    assert runner.children("b" * 16, CHAT) == []
    snapshot = runner.children(PID, CHAT)
    snapshot[0]["status"] = "running"
    assert runner.children(PID, CHAT)[0]["status"] == "completed"


def test_completed_subagent_history_still_uses_parent_membership(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "exists", lambda *a: True)
    monkeypatch.setattr(subagents.claude_agent_sdk, "list_subagents", lambda *a, **kw: ["done"])
    monkeypatch.setattr(subagents.claude_agent_sdk, "get_subagent_messages", lambda *a, **kw: [])
    assert subagents.history(tmp_path, CHAT, "done") == {"items": [], "complete": True}
    with pytest.raises(subagents.Refused):
        subagents.history(tmp_path, CHAT, "foreign")


def test_subagent_status_uses_a_bounded_cached_tail_and_does_not_guess_an_unfinished_reply(tmp_path, monkeypatch):
    root = tmp_path / "subagents"
    root.mkdir()
    child = root / "agent-done.jsonl"
    first = json.dumps({"type": "user", "message": {"content": "x" * (subagents.TAIL_BYTES * 2)}}) + "\n"
    final = json.dumps({"type": "assistant", "message": {"stop_reason": "end_turn", "content": "private"}}) + "\n"
    child.write_text(first + final, encoding="utf-8")
    monkeypatch.setattr(subagents, "_resolve_subagents_dir", lambda *a: root)
    subagents._tail_status.cache_clear()
    assert subagents._transcript_statuses(tmp_path, CHAT, {"done"}) == {"done": "completed"}
    assert subagents._transcript_statuses(tmp_path, CHAT, {"done"}) == {"done": "completed"}
    assert subagents._tail_status.cache_info().hits == 1
    with child.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "assistant", "message": {"stop_reason": "tool_use"}}) + "\n")
    assert subagents._transcript_statuses(tmp_path, CHAT, {"done"}) == {"done": "unknown"}
    with child.open("a", encoding="utf-8") as stream:
        stream.write('{"type":"assistant"')
    assert subagents._transcript_statuses(tmp_path, CHAT, {"done"}) == {"done": "unknown"}
    assert subagents._transcript_statuses(tmp_path, CHAT, {"foreign"}) == {}
