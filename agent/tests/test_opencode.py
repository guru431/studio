"""Контракт OpenCode ACP ↔ мост без CLI, сети и квоты владельца."""

import asyncio
from pathlib import Path

import pytest

from studio_agent import opencode, opencode_rpc, projects
from studio_agent.errors import Refused

CHAT = "11111111-2222-4333-8444-555555555555"
SESSION = "ses_test123"


def configuration(model="gateway/glm", effort="default"):
    return {"configOptions": [
        {"id": "model", "currentValue": model, "options": [
            {"value": "gateway/glm", "name": "GLM"}, {"value": "gateway/gpt", "name": "GPT"}]},
        {"id": "effort", "currentValue": effort, "options": [
            {"value": value, "name": value} for value in
            (("default", "high") if model == "gateway/glm" else ("default", "low", "xhigh"))]}]}


class FakeRpc:
    def __init__(self, factory, path, cwd):
        self.factory, self.cwd = factory, cwd
        self.info = {"agentInfo": {"version": "2.2.4"}}
        self.inbox = asyncio.Queue()
        self.closed = False
        self.config = configuration()
        self.response = asyncio.get_running_loop().create_future()

    async def connect(self):
        pass

    async def disconnect(self):
        self.closed = True

    def update(self, update, target=SESSION):
        self.inbox.put_nowait({"method": "session/update", "params": {"sessionId": target, "update": update}})

    async def request(self, method, params, **kwargs):
        self.factory.calls.append((method, params))
        if method == "session/new":
            return {"sessionId": SESSION, **self.config}
        if method == "session/delete":
            self.factory.deleted.append(params["sessionId"])
            return {}
        if method == "session/set_config_option":
            self.config = configuration(params["value"] if params["configId"] == "model"
                                        else self.config["configOptions"][0]["currentValue"],
                                        params["value"] if params["configId"] == "effort" else "default")
            return self.config
        if method == "session/load":
            if self.factory.missing:
                raise opencode_rpc.RpcError(not_found=True)
            for update in self.factory.history:
                self.update(update, params["sessionId"])
            return self.config
        if method == "session/list":
            return {"sessions": [
                {"sessionId": SESSION, "cwd": str(self.factory.project), "title": "Внешняя сессия",
                 "updatedAt": "2026-01-01T12:00:00Z"},
                {"sessionId": "ses_other", "cwd": str(self.factory.project / "other")}]}
        if method == "session/prompt":
            self.update({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Ответ"}})
            self.update({"sessionUpdate": "tool_call", "toolCallId": "step1", "title": "Shell",
                         "rawInput": {"command": "git status"}, "status": "in_progress"})
            self.inbox.put_nowait(self.factory.ask)
            self.factory.prompted.set()
            return await self.response
        raise AssertionError(method)

    async def send(self, frame):
        self.factory.replies.append(frame)
        if frame.get("method") == "session/cancel":
            self.response.set_result({"stopReason": "cancelled"})
        elif "result" in frame and not self.response.done():
            self.update({"sessionUpdate": "tool_call_update", "toolCallId": "step1", "status": "completed",
                         "content": [{"type": "content", "content": {"type": "text", "text": "чисто"}}]})
            self.update({"sessionUpdate": "usage_update", "used": 100, "size": 1000})
            self.response.set_result({"stopReason": "end_turn"})


class Factory:
    def __init__(self, project):
        self.project = project
        self.calls, self.replies, self.clients, self.deleted, self.history = [], [], [], [], []
        self.missing = False
        self.prompted = asyncio.Event()
        self.ask = {"id": 91, "method": "session/request_permission", "params": {"sessionId": SESSION,
            "toolCall": {"title": "Shell", "rawInput": {"command": "git status"}}, "options": [
                {"optionId": "once", "kind": "allow_once"}, {"optionId": "always", "kind": "allow_always"},
                {"optionId": "reject", "kind": "reject_once"}]}}

    def __call__(self, path, cwd):
        rpc = FakeRpc(self, path, cwd)
        self.clients.append(rpc)
        return rpc


@pytest.fixture
def runner(home):
    project = home / "demo"
    project.mkdir()
    factory, sent = Factory(project), []
    runner = opencode.OpenCodeRunner(sent.append, home=home, find_cli=lambda: Path("opencode.exe"), factory=factory)
    runner.catalog = {"version": "test", "models": [], "efforts": []}
    runner.fake, runner.sent, runner.project, runner.pid = factory, sent, project, projects.pid(project.name)
    return runner


async def asking(runner):
    ready, emit = asyncio.Event(), runner.emit

    def observe(frame):
        emit(frame)
        if frame["e"]["k"] == "ask":
            ready.set()

    runner.emit = observe
    try:
        if not any(t.asks for t in runner._turns.values()):
            await asyncio.wait_for(ready.wait(), 2)
        return next(iter(runner._turns[CHAT].asks))
    finally:
        runner.emit = emit


async def test_probe_models_and_all_efforts_without_prompt_deletes_only_temporary(runner):
    assert await runner.probe(runner.project)
    assert runner.models == [{"id": "gateway/glm", "label": "GLM"}, {"id": "gateway/gpt", "label": "GPT"}]
    assert runner.efforts == ["default", "high", "low", "xhigh"]
    assert runner.catalog["defaults"] == {"model": "gateway/glm", "effort": "default", "access": "default"}
    assert not any(method == "session/prompt" for method, _ in runner.fake.calls)
    assert runner.fake.deleted == [SESSION] and all(c.closed for c in runner.fake.clients)


async def test_external_sessions_map_to_uuid_keep_project_and_history(runner, home):
    rows = await runner.chats(runner.project)
    assert len(rows) == 1
    row = rows[0]
    assert row["engine"] == "opencode" and row["tag"] is None and row["modified"] == 1767268800
    assert opencode.limits.CHAT_ID.fullmatch(row["chat"])
    assert runner.aliases.target(runner.project, row["chat"]) == SESSION
    assert (await runner.chats(runner.project))[0]["chat"] == row["chat"]
    with pytest.raises(Refused) as error:
        runner.aliases.target(runner.project / "other", row["chat"])
    assert error.value.code == "not_found"
    runner.fake.history = [
        {"sessionUpdate": "user_message_chunk", "messageId": "u1", "content": {"type": "text", "text": "Скажи "}},
        {"sessionUpdate": "user_message_chunk", "messageId": "u1", "content": {"type": "image"}},
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Ответ"}},
        {"sessionUpdate": "tool_call", "toolCallId": "s1", "title": "Shell"},
        {"sessionUpdate": "tool_call_update", "toolCallId": "s1", "status": "completed",
         "content": [{"type": "content", "content": {"type": "text", "text": "итог"}}]}]
    page = await runner.history(runner.project, row["chat"], None, 1)
    assert page["total"] == 2 and page["start"] == 1
    assert page["items"][0]["text"] == "Ответ" and page["items"][0]["steps"][0]["text"] == "итог"
    assert "Скажи" not in (home / "opencode-chats.json").read_text("utf-8")
    runner.fake.missing = True
    with pytest.raises(Refused) as error:
        await runner.history(runner.project, row["chat"], None, 10)
    assert error.value.code == "not_found"


async def test_turn_mapping_resume_permissions_snapshot_and_usage(runner, home):
    runner.start(runner.project, runner.pid, CHAT, "Привет", "gateway/gpt", "xhigh")
    task = runner._turns[CHAT].task
    rid = await asking(runner)
    running, snapshots = await runner.watch(runner.pid, CHAT)
    assert running and snapshots[0]["e"]["text"] == "Ответ"
    assert snapshots[0]["e"]["asks"][0]["always"]
    with pytest.raises(Refused):
        runner.answer("other", CHAT, rid, "allow", None, None)
    runner.answer(runner.pid, CHAT, rid, "always", None, None)
    await task
    assert runner.fake.replies[0] == {"id": 91, "result": {"outcome": {"outcome": "selected", "optionId": "always"}}}
    assert runner.sent[-1]["e"] == {"k": "done", "status": "done"}
    assert not runner.live() and runner.fake.clients[0].closed
    assert runner.aliases.target(runner.project, CHAT) == SESSION
    assert runner.choices.details(CHAT)["context"] == {"used": 100, "limit": 1000, "percent": 10}
    assert "Привет" not in (home / "opencode-chats.json").read_text("utf-8")
    runner.start(runner.project, runner.pid, CHAT, "Дальше", None, None)
    task = runner._turns[CHAT].task
    rid = await asking(runner)
    runner.answer(runner.pid, CHAT, rid, "deny", "не надо", None)
    await task
    assert len([1 for m, _ in runner.fake.calls if m == "session/new"]) == 1
    assert any(m == "session/load" for m, _ in runner.fake.calls)
    assert runner.fake.replies[-1]["result"]["outcome"]["optionId"] == "reject"


async def test_auto_explicitly_selects_once_and_images_use_embedded_data(runner):
    runner.start(runner.project, runner.pid, CHAT, "", None, None, access="auto",
                 images=[{"mime": "image/png", "data": "cG5n"}])
    await runner._turns[CHAT].task
    assert not any(f["e"]["k"] == "ask" for f in runner.sent)
    assert runner.fake.replies[0]["result"]["outcome"] == {"outcome": "selected", "optionId": "once"}
    prompt = next(p for m, p in runner.fake.calls if m == "session/prompt")
    assert prompt["prompt"] == [{"type": "image", "mimeType": "image/png", "data": "cG5n"}]


async def test_stop_rejects_cards_cancels_prompt_and_close_prevents_new_work(runner):
    runner.start(runner.project, runner.pid, CHAT, "привет", None, None)
    task = runner._turns[CHAT].task
    await asking(runner)
    await runner.stop(runner.pid, CHAT)
    await task
    assert runner.sent[-1]["e"]["status"] == "stopped"
    assert runner.fake.clients[0].closed and not runner.live()
    await runner.close(hold=True)
    with pytest.raises(Refused) as error:
        runner.start(runner.project, runner.pid, CHAT, "снова", None, None)
    assert error.value.code == "busy"


async def test_state_reads_original_but_configures_only_temporary(runner):
    runner.aliases.put(runner.project, CHAT, "ses_original")
    runner.fake.history = [{"sessionUpdate": "usage_update", "used": 25, "size": 100}]
    data = await runner.state(runner.project, CHAT, "gateway/gpt", "xhigh", "auto")
    assert data == {"settings": {"model": "gateway/gpt", "effort": "xhigh", "access": "auto"},
                    "context": {"used": 25, "limit": 100, "percent": 25}}
    assert all(p["sessionId"] == SESSION for m, p in runner.fake.calls if m == "session/set_config_option")
    assert runner.fake.deleted == [SESSION]
    assert not any(m == "session/prompt" for m, _ in runner.fake.calls)
    with pytest.raises(Refused):
        await runner.state(runner.project, CHAT, "gateway/glm", "xhigh")
    assert all(c.closed for c in runner.fake.clients)


async def test_question_schema_options_map_labels_back_to_values(runner):
    runner.fake.ask = {"id": 92, "method": "elicitation/create", "params": {"sessionId": SESSION, "mode": "form",
        "message": "Выберите менеджер", "requestedSchema": {"type": "object", "properties": {
            "pm": {"type": "string", "title": "Менеджер пакетов", "oneOf": [{"const": "bun", "title": "Bun"}]},
            "pm_custom": {"type": "string"}}, "required": ["pm"]}}}
    runner.start(runner.project, runner.pid, CHAT, "вопрос", None, None)
    task = runner._turns[CHAT].task
    rid = await asking(runner)
    assert runner._turns[CHAT].asks[rid].event["questions"][0]["question"] == "Менеджер пакетов"
    runner.answer(runner.pid, CHAT, rid, "allow", None, {"Менеджер пакетов": "Bun"})
    await task
    assert runner.fake.replies[-1] == {"id": 92, "result": {"action": "accept", "content": {"pm": "bun"}}}


async def test_foreign_request_is_rejected_without_card(runner):
    runner.fake.ask["params"]["sessionId"] = "ses_foreign"
    runner.start(runner.project, runner.pid, CHAT, "привет", None, None)
    task = runner._turns[CHAT].task
    await runner.fake.prompted.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert runner.fake.replies[0]["error"]["code"] == -32601
    assert not any(f["e"]["k"] == "ask" for f in runner.sent)
    await runner.stop(runner.pid, CHAT)
    await task


async def test_unavailable_cli_reports_error_without_prompt(runner):
    runner.find_opencode = lambda: None
    assert not await runner.probe(runner.project)
    runner.start(runner.project, runner.pid, CHAT, "привет", None, None)
    await runner._turns[CHAT].task
    assert runner.sent[-1]["e"]["status"] == "error" and not runner.fake.calls


async def test_probe_cleanup_failure_does_not_break_other_backends(runner):
    async def unavailable_cleanup(*_):
        raise opencode_rpc.RpcError(method="session/delete", code=-32601)

    runner._delete = unavailable_cleanup
    previous = runner.catalog
    assert not await runner.probe(runner.project)
    assert runner.catalog == previous and all(c.closed for c in runner.fake.clients)
