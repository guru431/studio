"""Путь событий Codex до истории и ограниченного снимка переподключения."""
import json

from studio_agent import codex, codex_progress, events, limits, sessions, timeline
from studio_agent.turns import _Turn
from test_codex import CHAT, runner

CHILD = "01999999-2222-7333-8444-888888888888"
PATCH = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1,2 @@\n-old\n+new\n+extra\n"


def test_followup_position_counts_published_events_in_utf16():
    feed = events.Feed()
    # Разобранные, но ещё не отправленные данные не сдвигают место уточнения.
    feed.text = 'Будущий текст'
    feed.steps = [{'id': 'future'}]
    assert feed.position() == {'text': 0, 'steps': 0}
    feed.note({'k': 'text', 'text': 'До 🛠'})
    feed.note({'k': 'step', 'id': 'cmd'})
    before = feed.position()
    feed.note({'k': 'text', 'text': 'После'})
    assert before == {'text': 5, 'steps': 1}
    assert feed.position() == {'text': 10, 'steps': 1}
    feed._ordered = False
    assert feed.position() is None


async def test_subagent_list_merges_live_states_with_completed_history(runner):
    from unittest.mock import AsyncMock

    old = "01999999-2222-7333-8444-777777777777"
    t = _Turn(runner.project, "a" * 16, CHAT, "turn")
    t.progress["agents"] = [{"id": CHILD, "title": "/root/current", "status": "running"}]
    runner._turns[CHAT] = t
    runner._metadata = AsyncMock(return_value={"thread": {"cwd": str(runner.project), "turns": [{"items": [
        {"type": "subAgentActivity", "agentThreadId": old, "kind": "completed"},
        {"type": "subAgentActivity", "agentThreadId": CHILD, "kind": "completed"}]}]}})
    children = (await runner.subagents(runner.project, CHAT))["items"]
    assert {row["id"]: row["status"] for row in children} == {old: "completed", CHILD: "running"}
    assert children[-1]["title"] == "/root/current"


async def test_subagent_archive_cache_coalesces_reads_but_live_state_and_expiry_stay_current(runner):
    import asyncio
    from unittest.mock import AsyncMock

    now = [100.0]
    runner.now = lambda: now[0]
    runner._metadata = AsyncMock(return_value={"thread": {"cwd": str(runner.project), "turns": []}})
    await asyncio.gather(runner.subagents(runner.project, CHAT), runner.subagents(runner.project, CHAT))
    assert runner._metadata.await_count == 1
    t = _Turn(runner.project, "a" * 16, CHAT, "turn")
    t.progress["agents"] = [{"id": CHILD, "title": "Подзадача", "status": "running"}]
    runner._turns[CHAT] = t
    assert (await runner.subagents(runner.project, CHAT))["items"][0]["status"] == "running"
    t.progress["agents"][0]["status"] = "completed"
    assert (await runner.subagents(runner.project, CHAT))["items"][0]["status"] == "completed"
    assert runner._metadata.await_count == 1
    now[0] += codex.CHILDREN_CACHE_SEC
    await runner.subagents(runner.project, CHAT)
    assert runner._metadata.await_count == 2


def test_agent_history_keeps_all_children_and_live_summary_prioritizes_active_ones():
    items = [{"type": "subAgentActivity", "agentThreadId": f"01999999-2222-7333-8444-{index:012d}",
              "kind": "completed"} for index in range(codex_progress.AGENTS_MAX + 5)]
    items.append({"type": "subAgentActivity", "agentThreadId": CHILD, "kind": "started"})
    summary = codex_progress.children(items)
    assert len(summary) == codex_progress.AGENTS_MAX and summary[0]["id"] == CHILD
    assert len(codex_progress.children(items, limit=None)) == len(items)


async def test_stream_output_status_diff_agents_plan_and_reconnect_share_one_snapshot(runner):
    t = _Turn(runner.project, "a" * 16, CHAT, "turn")
    runner._turns[CHAT] = t
    def notify(method, **data):
        runner._notification(t, method, data)
    notify("item/agentMessage/delta", itemId="intro", delta="Проверю 🛠")
    command = {"id": "cmd", "type": "commandExecution", "command": "pytest", "cwd": str(runner.project),
               "status": "inProgress", "processId": "321", "aggregatedOutput": None}
    notify("item/started", item=command)
    notify("item/commandExecution/outputDelta", itemId="cmd", delta="Проверка идёт\n")
    assert t.feed.steps[0]["text"] == "Проверка идёт\n"
    notify("item/commandExecution/terminalInteraction", itemId="cmd", processId="321", stdin="не показывать")
    notify("turn/diff/updated", diff=PATCH)
    notify("turn/plan/updated", plan=[{"step": "Проверить", "status": "inProgress"}])
    notify("item/completed", item={"id": "spawn", "type": "collabAgentToolCall", "tool": "spawnAgent",
        "status": "completed", "receiverThreadIds": [CHILD], "prompt": "Проверка GPU",
        "agentsStates": {CHILD: {"status": "running"}}})
    notify("item/completed", item={"id": "child-done", "type": "subAgentActivity", "agentThreadId": CHILD,
                                  "agentPath": "/root/gpu", "kind": "completed"})
    notify("item/completed", item={**command, "status": "completed", "exitCode": 1,
                                  "durationMs": 1220})
    notify("item/agentMessage/delta", itemId="final", delta="Исправляю")
    running, [snapshot] = await runner.watch(t.pid, CHAT)
    data = snapshot["e"]
    assert running and data["text"] == "Проверю 🛠Исправляю"
    assert data["timeline"] == [{"kind": "text", "start": 0, "end": 10, "block": "intro"},
        {"kind": "step", "id": "cmd"}, {"kind": "step", "id": "spawn"},
        {"kind": "text", "start": 10, "end": 19, "block": "final"}]
    assert data["steps"][0]["status"] == "failed" and data["steps"][0]["duration_ms"] == 1220
    assert data["steps"][0]["exit_code"] == 1
    assert data["steps"][0]["text"] == "Проверка идёт\n"
    assert data["progress"]["changes"]["files"] == [{"path": "src/a.py", "added": 2, "removed": 1}]
    assert data["progress"]["agents"][0]["title"] == "/root/gpu"
    assert data["progress"]["agents"][0]["status"] == "completed"
    assert data["progress"]["plan"][0]["status"] == "inProgress"
    assert "не показывать" not in json.dumps(data, ensure_ascii=False)


def test_history_preserves_text_tool_order_and_agent_phase_without_reasoning():
    messages = codex.normalize([{"items": [
        {"id": "u", "type": "userMessage", "content": [{"type": "text", "text": "Задание"}]},
        {"id": "intro", "type": "agentMessage", "text": "Смотрю 🛠", "phase": "commentary"},
        {"id": "hidden", "type": "reasoning", "content": ["private reasoning"]},
        {"id": "edit", "type": "fileChange", "status": "completed", "changes": [{"path": "a.py", "diff": PATCH}]},
        {"id": "final", "type": "agentMessage", "text": "Готово", "phase": "final_answer"}]}])
    answer = messages[1]
    assert [p["kind"] for p in answer["timeline"]] == ["text", "step", "text"]
    assert answer["timeline"][0]["phase"] == "commentary"
    assert answer["timeline"][-1]["end"] == timeline.width(answer["text"])
    assert "private reasoning" not in str(answer)


def test_output_and_partial_snapshot_are_bounded_and_do_not_keep_invalid_offsets(runner):
    t = _Turn(runner.project, "a" * 16, CHAT, "turn")
    runner._notification(t, "item/started", {"item": {"id": "cmd", "type": "commandExecution", "status": "inProgress"}})
    runner._notification(t, "item/commandExecution/outputDelta", {"itemId": "cmd", "delta": "a" * (limits.CLIP + 50)})
    assert len(t.feed.steps[0]["text"]) == limits.CLIP and t.feed.steps[0]["output_truncated"]
    item = {"text": "x" * 30000, "steps": t.feed.steps, "timeline": [{"kind": "text", "start": 0, "end": 30000}],
            "progress": {"changes": codex_progress.patch_summary(PATCH * 100)}}
    [small] = sessions.fit([item], 4000)
    assert small["timeline"][-1]["end"] == timeline.width(small["text"])
    assert "progress" not in small
    assert len(json.dumps([small], ensure_ascii=True)) <= 4000
    changes = codex_progress.patch_summary(PATCH + "x" * codex_progress.DIFF_MAX)
    assert changes["truncated"] and len(changes["diff"]) == codex_progress.DIFF_MAX


def test_summary_counts_binary_empty_renamed_deleted_and_unicode_paths():
    patch = ('diff --git a/a.png b/a.png\nBinary files a/a.png and b/a.png differ\n'
             'diff --git a/old b/new\nsimilarity index 100%\nrename from old\nrename to new\n'
             'diff --git a/empty b/empty\nnew file mode 100644\n'
             'diff --git a/deleted b/deleted\n--- a/deleted\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n'
             'diff --git "a/\\320\\277.py" "b/\\320\\277.py"\n--- "a/\\320\\277.py"\n+++ "b/\\320\\277.py"\n@@ -0,0 +1 @@\n+new\n')
    result = codex_progress.patch_summary(patch)
    assert result['files'] == [{'path': 'a.png', 'added': 0, 'removed': 0},
        {'path': 'new', 'added': 0, 'removed': 0}, {'path': 'empty', 'added': 0, 'removed': 0},
        {'path': 'deleted', 'added': 0, 'removed': 1}, {'path': 'п.py', 'added': 1, 'removed': 0}]
    assert not result['truncated']
    result = codex_progress.patch_summary('diff --git a/b/a.txt b/b/a.txt\n--- a/b/a.txt\n+++ b/b/a.txt\n@@ -1 +1 @@\n--- value\n+++ value\n')
    assert result['files'] == [{'path': 'b/a.txt', 'added': 1, 'removed': 1}]


def test_tool_failure_payload_does_not_look_like_successful_completion():
    for item in [{'type': 'dynamicToolCall', 'success': False}, {'type': 'mcpToolCall', 'error': {'message': 'failed'}}]:
        step = codex.step_of({'id': 'tool', 'tool': 'check', 'status': 'completed', **item})
        assert step['error'] and step['status'] == 'failed'


def test_web_search_keeps_completed_input_and_results_in_stream_and_history(runner):
    t = _Turn(runner.project, 'a' * 16, CHAT, 'turn')
    started = {'id': 'search', 'type': 'webSearch', 'query': '', 'action': None}
    completed = {**started, 'query': 'Codex caching', 'action': {'type': 'search', 'query': 'Codex caching'},
                 'results': [{'title': 'Prompt caching', 'url': 'https://example.com/caching', 'snippet': 'Cached input.'}]}
    runner._notification(t, 'item/started', {'item': started})
    runner._notification(t, 'item/completed', {'item': completed})
    result = next(frame['e'] for frame in runner.sent if frame['e']['k'] == 'result')
    assert result['input']['query'] == 'Codex caching'
    assert result['text'] == 'Prompt caching\nhttps://example.com/caching\nCached input.'
    [message] = codex.normalize([{'items': [completed]}])
    [saved] = message['steps']
    assert saved['input'] == result['input'] == t.feed.steps[0]['input']
    assert saved['text'] == result['text'] == t.feed.steps[0]['text']
