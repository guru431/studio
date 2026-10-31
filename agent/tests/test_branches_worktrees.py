from pathlib import Path
from types import SimpleNamespace

import claude_agent_sdk
import pytest

from studio_agent import branches, sessions, subagents, worktrees
from studio_agent.errors import Refused


def test_claude_forks_before_user_message_through_native_sdk(tmp_path, monkeypatch):
    messages = [SimpleNamespace(uuid="first", type="user", parent_tool_use_id=None, message={"content": "one"}),
                SimpleNamespace(uuid="last-response", type="assistant", parent_tool_use_id=None, message={"content": "answer"}),
                SimpleNamespace(uuid="next", type="user", parent_tool_use_id=None, message={"content": "change this"})]
    monkeypatch.setattr(sessions, "get_session_messages", lambda *_a, **_k: messages)
    calls = []
    monkeypatch.setattr(claude_agent_sdk, "fork_session", lambda *a, **kw: calls.append((a, kw)) or SimpleNamespace(session_id="fork"))
    assert branches.claude(tmp_path, "source", "next") == {"chat": "fork", "draft": "change this"}
    assert calls[0][1] == {"directory": str(tmp_path), "up_to_message_id": "last-response"}
    empty = branches.claude(tmp_path, "source", "first")
    assert empty["draft"] == "one" and empty["chat"] != "source" and len(calls) == 1
    with pytest.raises(Refused, match="invalid"):
        branches.claude(tmp_path, "source", "missing")


def test_subagent_history_is_limited_to_children_of_the_selected_parent(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "exists", lambda *_: True)
    monkeypatch.setattr(claude_agent_sdk, "list_subagents", lambda *_a, **_k: ["child"])
    calls = []
    monkeypatch.setattr(claude_agent_sdk, "get_subagent_messages", lambda *a, **kw: calls.append(a) or [])
    with pytest.raises(Refused, match="not_found"):
        subagents.history(tmp_path, "parent", "someone-elses-child")
    assert calls == []
    assert subagents.history(tmp_path, "parent", "child") == {"items": [], "complete": True}
    assert calls == [("parent", "child")]


def test_worktree_stays_in_project_root_and_does_not_move_existing_changes(tmp_path, monkeypatch):
    project = tmp_path / "demo"
    project.mkdir()
    calls = []

    def git(_project, args):
        calls.append(args)
        if args[0] == "rev-parse":
            return str(project)
        Path(args[-2]).mkdir()
        return ""

    monkeypatch.setattr(worktrees, "_git", git)
    result = worktrees.create(project, tmp_path, "task")
    assert result["name"] == "demo--task" and (tmp_path / result["name"]).is_dir()
    assert calls[-1][:3] == ["worktree", "add", "-b"] and calls[-1][-1] == "HEAD"
    assert not any("stash" in call or "remove" in call or "--force" in call for call in calls)
    with pytest.raises(Refused, match="conflict"):
        worktrees.create(project, tmp_path, "task")
    with pytest.raises(Refused, match="invalid"):
        worktrees.create(project, tmp_path, "../outside")


def test_worktree_list_never_exposes_checkouts_outside_allowed_root(tmp_path, monkeypatch):
    project = tmp_path / "demo"
    project.mkdir()
    outside = tmp_path.parent / "outside"
    monkeypatch.setattr(worktrees, "_git", lambda *_: f"worktree {project}\0HEAD abc\0branch refs/heads/main\0\0worktree {outside}\0HEAD def\0\0")
    rows = worktrees.list_worktrees(project, tmp_path)["items"]
    assert len(rows) == 1 and rows[0]["name"] == "demo" and rows[0]["current"]
