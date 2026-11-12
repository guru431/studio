"""Список подзадач сохраняет завершённые записи и принимает живые lifecycle-события SDK."""

import asyncio
import json

import pytest
from claude_agent_sdk._internal.message_parser import parse_message

from fake_sdk import FakeClient, FakeFactory, assistant, result, result_ok, text
from claude_agent_sdk import ResultMessage
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


class BackgroundClient(FakeClient):
    """Как SDK: каждый receive_response читает следующий ответ до первого ResultMessage."""

    def __init__(self, *, options):
        super().__init__(options, [])
        self.items = asyncio.Queue()
        self.queried = asyncio.Queue()
        self.results = asyncio.Queue()
        self.stopped_tasks = []

    async def query(self, value):
        await super().query(value)
        self.queried.put_nowait(value)

    async def receive_response(self):
        while True:
            message = await self.items.get()
            yield message
            if isinstance(message, ResultMessage):
                self.results.put_nowait(message)
                return

    async def stop_task(self, task_id):
        self.stopped_tasks.append(task_id)


@pytest.fixture
async def background(home, project):
    sent = []
    factory = []

    def create(**kwargs):
        client = BackgroundClient(**kwargs)
        factory.append(client)
        ready.set()
        return client

    ready = asyncio.Event()
    runner = Runner(sent.append, choices=store.Choices(home), cli_path=None, factory=create)
    runner.start(project, PID, CHAT, "Задание", None, None)
    turn = runner._turns[CHAT]
    await ready.wait()
    client = factory[0]
    await client.queried.get()
    try:
        yield runner, client, turn, sent
    finally:
        if not turn.task.done():
            turn.task.cancel()
        await asyncio.gather(turn.task, return_exceptions=True)


@pytest.mark.parametrize("kind", ["local_agent", "local_workflow"])
async def test_background_work_survives_parent_result_until_its_final_response(background, kind):
    runner, client, turn, sent = background
    client.items.put_nowait(lifecycle("task_started", task_type=kind, description="Фоновая проверка"))
    client.items.put_nowait(result_ok())
    await client.results.get()
    assert CHAT in runner.live() and not turn.finishing and not client.disconnected
    assert not any(frame["e"]["k"] == "done" for frame in sent)
    client.items.put_nowait(lifecycle("task_updated", patch={"status": "completed"}))
    client.items.put_nowait(assistant(text("Итог фоновой проверки")))
    client.items.put_nowait(result_ok())
    await turn.task
    assert client.disconnected and turn.pending == 0
    assert [f["e"]["status"] for f in sent if f["e"]["k"] == "done"] == ["done"]
    assert any(f["e"].get("text") == "Итог фоновой проверки" for f in sent)


async def test_workflow_children_stream_through_hooks_and_queue_waits_for_the_workflow(background):
    runner, client, turn, sent = background
    client.items.put_nowait(lifecycle("task_started", "workflow", task_type="local_workflow", description="Проверка"))
    for child in ("first", "second"):
        hook = client.options.hooks["SubagentStart"][0].hooks[0]
        assert await hook({"hook_event_name": "SubagentStart", "agent_id": child,
                           "agent_type": "workflow-subagent"}, None, {}) == {}
    queued = await runner.steer(PID, CHAT, "Следующее задание", [], mode="queue")
    client.items.put_nowait(result_ok())
    await client.results.get()
    assert client.queried.empty()
    running, frames = await runner.watch(PID, CHAT)
    assert running and {r["id"] for r in frames[0]["e"]["progress"]["agents"]} == {"first", "second"}
    assert {r["status"] for r in runner.children(PID, CHAT)} == {"running"}
    for child in ("first", "second"):
        client.items.put_nowait(lifecycle("task_updated", child, patch={"status": "completed"}))
    client.items.put_nowait(lifecycle("task_notification", "workflow", status="completed",
                                    output_file="private", summary="private"))
    client.items.put_nowait(result_ok())
    assert await client.queried.get() == "Следующее задание"
    client.items.put_nowait(result_ok())
    await turn.task
    assert runner.choices.details(CHAT)["subagent_statuses"] == {"first": "completed", "second": "completed"}
    assert any(f["e"] == {"k": "followup_sent", "id": queued["id"]} for f in sent)


async def test_stopping_an_idle_parent_stops_its_background_work_without_waiting_for_another_result(background):
    runner, client, turn, sent = background
    client.items.put_nowait(lifecycle("task_started", task_type="local_agent", description="Проверка"))
    client.items.put_nowait(result_ok())
    await client.results.get()
    await runner.stop(PID, CHAT)
    await asyncio.gather(turn.task, return_exceptions=True)
    assert client.stopped_tasks == ["child"] and client.disconnected
    assert runner.choices.details(CHAT)["subagent_statuses"] == {"child": "stopped"}
    assert sent[-1]["e"]["status"] == "stopped"


async def test_background_completion_during_interrupt_does_not_cancel_disconnect(background):
    runner, client, turn, sent = background
    client.items.put_nowait(lifecycle("task_started", task_type="local_agent", description="Проверка"))
    client.items.put_nowait(result_ok())
    await client.results.get()
    disconnecting, release = asyncio.Event(), asyncio.Event()
    original = client.disconnect

    async def disconnect():
        disconnecting.set()
        await release.wait()
        await original()

    async def interrupt():
        client.items.put_nowait(lifecycle("task_updated", patch={"status": "completed"}))
        client.items.put_nowait(result_ok())
        await disconnecting.wait()

    client.disconnect = disconnect
    client.interrupt = interrupt
    try:
        await runner.stop(PID, CHAT)
        assert turn.finishing
        release.set()
        await turn.task
        assert client.disconnected and sent[-1]["e"]["status"] == "stopped"
    finally:
        release.set()


async def test_background_stream_ending_without_completion_is_an_error(home, project):
    sent = []
    factory = FakeFactory([lifecycle("task_started", task_type="local_workflow", description="Проверка"), result_ok()])
    runner = Runner(sent.append, choices=store.Choices(home), cli_path=None, factory=factory)
    runner.start(project, PID, CHAT, "Задание", None, None)
    await settle(runner)
    assert sent[-1]["e"]["status"] == "error" and factory.clients[0].disconnected


async def test_background_error_finishes_the_turn_and_releases_the_client(background):
    runner, client, turn, sent = background
    client.items.put_nowait(lifecycle("task_started", task_type="local_workflow", description="Проверка"))
    client.items.put_nowait(result("error_during_execution", is_error=True, result="Ошибка проверки"))
    await turn.task
    assert client.disconnected and sent[-1]["e"]["status"] == "error"


async def test_followup_image_is_in_the_live_snapshot_without_inline_bytes_and_survives_done(background):
    from test_image_input import PNG

    runner, client, turn, sent = background
    followup = await runner.steer(PID, CHAT, "Посмотри скриншот", [PNG])
    await client.queried.get()
    _, frames = await runner.watch(PID, CHAT)
    item = frames[0]["e"]["followups"][0]
    assert item["id"] == followup["id"]
    assert item["images"] == [{"index": 0, "name": "Изображение 1"}]
    assert PNG["data"] not in json.dumps(frames)
    assert runner.live_image(turn.project, CHAT, item["image_message_id"], 0) == PNG
    client.items.put_nowait(result_ok())
    client.items.put_nowait(result_ok())
    await turn.task
    assert runner.live_image(turn.project, CHAT, item["image_message_id"], 0) == PNG
    await runner.close(final=True)
    assert runner.live_image(turn.project, CHAT, item["image_message_id"], 0) is None


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
