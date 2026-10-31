from studio_agent import diffs, events
from studio_agent.codex import step_of as codex_step
from studio_agent.opencode import step_of as acp_step
from fake_sdk import assistant, tool_result, tool_use, user


def test_claude_edit_live_keeps_a_fragment_and_errors_do_not_claim_a_change():
    feed = events.Feed()
    feed.events(assistant(tool_use("edit", "Edit", {"file_path": "src/a.py", "old_string": "old", "new_string": "new"})))
    result = feed.events(user(tool_result("edit", "done")))[0]
    assert result["changes"] == [{"path": "src/a.py", "before": "old", "after": "new"}]
    assert feed.steps[0]["changes"] == result["changes"]
    assert diffs.for_tool("Write", {"file_path": "a", "content": "new"}) == [{"path": "a", "after": "new"}]


def test_codex_and_acp_keep_patch_and_old_text():
    assert codex_step({"id": "edit", "type": "fileChange", "status": "completed", "changes": [
        {"path": "a", "diff": "@@ -1 +1 @@\n-old\n+new"}]})["changes"][0]["patch"].endswith("+new")
    step = acp_step({"toolCallId": "edit", "status": "completed", "content": [
        {"type": "diff", "path": "a", "oldText": "old", "newText": "new"}]})
    assert step["changes"] == [{"path": "a", "before": "old", "after": "new"}]
    assert "changes" not in codex_step({"id": "edit", "type": "fileChange", "status": "failed", "changes": []})


def test_large_edits_are_explicitly_partial():
    row = diffs.change("a", before="x" * (diffs.MAX_CHARS + 1), after="new")
    assert row["truncated"] and len(row["before"]) == diffs.MAX_CHARS


def test_event_budget_does_not_silently_shorten_a_diff(monkeypatch):
    monkeypatch.setattr(events.limits, "EVENT_MAX", 300)
    result = events.bound({"input": {"command": "x" * 5000}, "changes": [{"path": "a", "patch": "x" * 5000}]})
    assert result["changes"][0]["truncated"] is True
    assert len(result["changes"][0]["patch"]) <= events.limits.CLIP_TIGHT
