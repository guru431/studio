"""Контракт Codex app-server ↔ мост: реальных моделей, сети и профиля владельца здесь нет."""

import asyncio
from pathlib import Path

import pytest

from studio_agent import codex, codex_rpc, projects
from studio_agent.errors import Refused
from studio_agent.turns import _Turn

CHAT = "11111111-2222-4333-8444-555555555555"
THREAD = "01999999-2222-7333-8444-555555555555"
EPHEMERAL = "01999999-2222-7333-8444-666666666666"
FORK = "01999999-2222-7333-8444-777777777777"


class FakeRpc:
    def __init__(self, factory, path, cwd):
        self.factory, self.cwd = factory, cwd
        self.inbox = asyncio.Queue()
        self.closed = False

    async def connect(self):
        pass

    async def disconnect(self):
        self.closed = True
        for thread in [key for key, writer in self.factory.writers.items() if writer is self]:
            del self.factory.writers[thread]

    async def request(self, method, params):
        self.factory.calls.append((method, params))
        if method == "thread/read":
            if self.factory.read_error is not None:
                raise self.factory.read_error
            if params["threadId"] != THREAD or not self.factory.exists:
                raise codex_rpc.RpcError(not_found=True)
            return {"thread": {"id": THREAD, "cwd": str(self.factory.project), "turns": self.factory.turns}}
        if method in ("thread/start", "thread/resume", "thread/fork"):
            ephemeral = params.get("ephemeral", False)
            target = EPHEMERAL if ephemeral else FORK if method == "thread/fork" else THREAD
            if (writer := self.factory.writers.get(target)) is not None and writer is not self:
                raise codex_rpc.RpcError(writer_active=True, method=method, code=-32600)
            self.factory.writers[target] = self
            if not ephemeral:
                self.factory.exists = True
            return {"thread": {"id": target, "cwd": str(self.factory.project), "ephemeral": ephemeral},
                    "model": params.get("model", "codex-test"),
                    "reasoningEffort": "high", "sandbox": {"type": {"read-only": "readOnly",
                        "danger-full-access": "dangerFullAccess"}.get(params.get("sandbox"), "workspaceWrite")},
                    "approvalPolicy": params.get("approvalPolicy", "on-request")}
        if method == "turn/start":
            self.inbox.put_nowait({"method": "item/agentMessage/delta", "params": {"threadId": THREAD, "delta": "Ответ"}})
            self.inbox.put_nowait({"id": 91, "method": self.factory.ask,
                                  "params": {"threadId": THREAD, "turnId": "turn1", "itemId": "step1",
                                             "command": "git status", **self.factory.ask_params}})
            return {"turn": {"id": "turn1"}}
        if method == "turn/steer":
            return {"turnId": "turn1"}
        if method == "turn/interrupt":
            self.done("interrupted")
            return {}
        if method == "thread/list":
            return {"data": [{"id": THREAD, "cwd": str(self.factory.project), "preview": "Тест", "createdAt": 1,
                              "updatedAt": 2}, {"id": CHAT, "cwd": str(self.cwd / "other")}], "nextCursor": None}
        if method == "model/list":
            if self.factory.read_error is not None:
                raise self.factory.read_error
            return {"data": [{"model": "codex-test", "displayName": "Codex", "supportedReasoningEfforts": [],
                              "isDefault": True, "defaultReasoningEffort": "high"}]}
        if method == "config/read":
            return {"config": {"model": "codex-test", "model_reasoning_effort": "high"}}
        if method == "thread/unsubscribe":
            if self.factory.writers.get(params["threadId"]) is self:
                del self.factory.writers[params["threadId"]]
            return {}
        raise AssertionError(method)

    def done(self, status="completed"):
        self.inbox.put_nowait({"method": "turn/completed", "params": {"threadId": THREAD,
                                                                      "turn": {"id": "turn1", "status": status}}})

    async def send(self, frame):
        self.factory.replies.append(frame)
        self.done()


class Factory:
    def __init__(self, project):
        self.project = project
        self.calls, self.replies, self.clients, self.turns = [], [], [], []
        self.exists = False
        self.read_error = None
        self.ask = "item/commandExecution/requestApproval"
        self.ask_params = {}
        self.writers = {}

    def __call__(self, path, cwd):
        client = FakeRpc(self, path, cwd)
        self.clients.append(client)
        return client


@pytest.fixture
def runner(home):
    project = home / "demo"
    project.mkdir()
    factory = Factory(project)
    sent = []
    runner = codex.CodexRunner(sent.append, home=home, find_cli=lambda: Path("codex.exe"), factory=factory)
    runner.catalog = {"version": "test", "models": [], "efforts": []}
    runner.sent, runner.fake, runner.project = sent, factory, project
    return runner


async def test_native_fork_releases_only_its_own_writer(runner):
    runner.fake.exists = True
    result = await runner.fork_chat(runner.project, THREAD)
    assert result["chat"] != THREAD and result["draft"] == ""
    assert runner.aliases.target(runner.project, result["chat"]) == FORK
    assert FORK not in runner.fake.writers
    assert ("thread/unsubscribe", {"threadId": FORK}) in runner.fake.calls
    assert not any(method == "thread/resume" for method, _ in runner.fake.calls)


async def test_fork_before_selected_user_turn_verifies_cutoff(runner, monkeypatch):
    runner.fake.exists = True
    runner.fake.turns = [
        {"id": "turn-one", "status": "completed", "items": [{"id": "user-one", "type": "userMessage", "content": [{"type": "text", "text": "one"}]}]},
        {"id": "turn-two", "status": "completed", "items": [{"id": "user-two", "type": "userMessage", "content": [{"type": "text", "text": "two"}]}]}]
    original = FakeRpc.request

    async def request(self, method, params):
        if method == "thread/read" and params["threadId"] == FORK:
            return {"thread": {"cwd": str(runner.project), "turns": runner.fake.turns[:1]}}
        return await original(self, method, params)

    monkeypatch.setattr(FakeRpc, "request", request)
    result = await runner.fork_chat(runner.project, THREAD, "user-two")
    assert result["draft"] == "two"
    assert any(method == "thread/fork" and params.get("lastTurnId") == "turn-one" for method, params in runner.fake.calls)
    assert FORK not in runner.fake.writers
    assert len(runner.fake.turns) == 2


async def test_codex_cannot_silently_drop_messages_before_a_mid_turn_fork(runner):
    runner.fake.exists = True
    runner.fake.turns = [{"id": "turn-one", "items": [
        {"id": "first", "type": "userMessage", "content": [{"type": "text", "text": "first"}]},
        {"id": "steered", "type": "userMessage", "content": [{"type": "text", "text": "steered"}]}]}]
    with pytest.raises(Refused, match="unsupported"):
        await runner.fork_chat(runner.project, THREAD, "steered")
    assert not any(method == "thread/fork" for method, _ in runner.fake.calls)
    assert codex.normalize(runner.fake.turns)[1]["forkable"] is False


async def asking(runner):
    if not any(t.asks for t in runner._turns.values()):
        ready, previous = asyncio.Event(), runner.emit

        def emit(frame):
            previous(frame)
            if frame["e"]["k"] == "ask":
                ready.set()

        runner.emit = emit
        try:
            await asyncio.wait_for(ready.wait(), 2)
        finally:
            runner.emit = previous
    return next(iter(next(iter(runner._turns.values())).asks))


async def test_codex_queue_starts_a_new_turn_after_completion(runner):
    pid = projects.pid(runner.project.name)
    delivered = asyncio.Event()
    original_emit = runner.emit

    def emit(frame):
        original_emit(frame)
        if frame["e"]["k"] == "followup_sent":
            delivered.set()

    runner.emit = emit
    runner.start(runner.project, pid, CHAT, "first", None, None)
    task = runner._turns[CHAT].task
    first = await asking(runner)
    queued = await runner.steer(pid, CHAT, "second", [], mode="queue")
    await runner.edit_queue(pid, CHAT, queued["id"], 1, "corrected")
    assert len([1 for method, _ in runner.fake.calls if method == "turn/start"]) == 1
    runner.answer(pid, CHAT, first, "allow", None, None)
    await asyncio.wait_for(delivered.wait(), 2)
    second = await asking(runner)
    runner.answer(pid, CHAT, second, "allow", None, None)
    await task
    starts = [params for method, params in runner.fake.calls if method == "turn/start"]
    assert len(starts) == 2 and starts[1]["input"] == [{"type": "text", "text": "corrected"}]
    assert runner.sent[-1]["e"]["status"] == "done"


async def test_new_thread_mapping_resume_and_permission_snapshot(runner, home):
    pid = projects.pid(runner.project.name)
    runner.start(runner.project, pid, CHAT, "Привет", "model-test", "high")
    task = runner._turns[CHAT].task
    rid = await asking(runner)
    runner._notification(runner._turns[CHAT], "item/reasoning/textDelta", {"delta": "внутреннее рассуждение"})
    running, snapshot = await runner.watch(pid, CHAT)
    assert running and snapshot[0]["e"]["text"] == "Ответ"
    assert snapshot[0]["e"]["asks"][0]["tool"] == "Shell"
    assert snapshot[0]["e"]["activity"] == {"state": "waiting"}
    runner.answer(pid, CHAT, rid, "always", None, None)
    await task
    assert runner.fake.replies == [{"id": 91, "result": {"decision": "acceptForSession"}}]
    assert runner.sent[-1]["e"] == {"k": "done", "status": "done"}
    assert [f["e"]["state"] for f in runner.sent if f["e"]["k"] == "activity"] == [
        "thinking", "responding", "waiting", "thinking"]
    assert not runner.live() and runner.fake.clients[0].closed
    assert codex.Aliases(home).target(runner.project, CHAT) == THREAD
    assert "Привет" not in (home / "codex-chats.json").read_text("utf-8")

    # После перезапуска моста та же вкладка продолжает настоящий thread, модель и усилие не теряются.
    again = codex.CodexRunner(runner.sent.append, home=home, find_cli=lambda: Path("codex.exe"), factory=runner.fake)
    again.start(runner.project, pid, CHAT, "Дальше", None, None)
    task = again._turns[CHAT].task
    rid = await asking(again)
    again.answer(pid, CHAT, rid, "deny", None, None)
    await task
    assert len([m for m, _ in runner.fake.calls if m == "thread/start"]) == 1
    assert ("thread/resume", {"threadId": THREAD, "cwd": str(runner.project), "model": "model-test"}) in runner.fake.calls
    assert [p for m, p in runner.fake.calls if m == "turn/start"][-1]["effort"] == "high"


async def test_native_activity_tracks_reasoning_tools_and_response_without_reasoning_text(runner):
    pid = projects.pid(runner.project.name)
    t = _Turn(runner.project, pid, CHAT, "turn1")
    runner._turns[CHAT] = t
    runner._notification(t, "item/started", {"item": {"id": "r1", "type": "reasoning"}})
    runner._notification(t, "item/reasoning/textDelta", {"delta": "не показывать рассуждение"})
    runner._notification(t, "item/agentMessage/delta", {"delta": "Проверяю"})
    item = {"id": "step1", "type": "commandExecution", "command": "git status", "cwd": str(runner.project)}
    runner._notification(t, "item/started", {"item": item})
    running, [snapshot] = await runner.watch(pid, CHAT)
    assert running and snapshot["e"]["activity"] == {"state": "tool", "tool": "Shell"}
    runner._notification(t, "item/completed", {"item": {**item, "aggregatedOutput": "ok", "status": "completed"}})
    runner._notification(t, "item/started", {"item": {"id": "answer", "type": "agentMessage"}})
    runner._notification(t, "item/agentMessage/delta", {"delta": "Готово"})
    activities = [f["e"] for f in runner.sent if f["e"]["k"] == "activity"]
    assert activities == [{"k": "activity", "state": "thinking"}, {"k": "activity", "state": "responding"},
                          {"k": "activity", "state": "tool", "tool": "Shell"},
                          {"k": "activity", "state": "thinking"}, {"k": "activity", "state": "responding"}]
    assert t.feed.text == "Проверяю\n\nГотово" and t.feed.steps[0]["text"] == "ok"
    assert all("не показывать" not in str(frame) for frame in runner.sent)
    del runner._turns[CHAT]


async def test_access_and_context_use_native_policy_and_last_request(runner):
    pid = projects.pid(runner.project.name)
    runner.start(runner.project, pid, CHAT, "Задание", None, None, access="read-only")
    task = runner._turns[CHAT].task
    rid = await asking(runner)
    start = next(p for method, p in runner.fake.calls if method == "thread/start")
    assert start["sandbox"] == "read-only" and start["approvalPolicy"] == "on-request"
    t = runner._turns[CHAT]
    runner._notification(t, "thread/tokenUsage/updated", {"tokenUsage": {
        "last": {"totalTokens": 32000}, "total": {"totalTokens": 900000}, "modelContextWindow": 128000}})
    _, snapshot = await runner.watch(pid, CHAT)
    assert snapshot[0]["e"]["context"] == {"used": 32000, "limit": 128000, "percent": 25,
                                          "usage": {"total": 900000}}
    assert snapshot[0]["e"]["settings"]["access"] == "read-only"
    runner.answer(pid, CHAT, rid, "deny", None, None)
    await task
    assert runner.choices.details(CHAT)["context"]["used"] == 32000


async def test_reading_existing_chat_state_does_not_hold_its_writer(runner):
    runner.fake.exists = True
    await asyncio.to_thread(runner.aliases.put, runner.project, CHAT, THREAD)
    try:
        state = await runner.state(runner.project, CHAT)
        assert state["settings"]["model"] == "codex-test"
        assert THREAD not in runner.fake.writers
        assert EPHEMERAL not in runner.fake.writers
        assert not any(method == "turn/start" for method, _ in runner.fake.calls)
        pid = projects.pid(runner.project.name)
        runner.start(runner.project, pid, CHAT, "Продолжить", None, None)
        task = runner._turns[CHAT].task
        rid = await asking(runner)
        runner.answer(pid, CHAT, rid, "deny", None, None)
        await task
        assert runner.sent[-1]["e"] == {"k": "done", "status": "done"}
        assert any(method == "thread/resume" for method, _ in runner.fake.calls)
    finally:
        await runner.close(final=True)


async def test_state_uses_a_temporary_copy_even_when_another_client_owns_the_chat(runner):
    runner.fake.exists = True
    external = runner.fake(Path("codex.exe"), runner.project)
    await external.request("thread/resume", {"threadId": THREAD})
    await asyncio.to_thread(runner.aliases.put, runner.project, CHAT, THREAD)
    try:
        state = await runner.state(runner.project, CHAT)
        assert state["settings"]["model"] == "codex-test"
        assert runner.fake.writers.get(THREAD) is external and EPHEMERAL not in runner.fake.writers
        runner.start(runner.project, "a" * 16, CHAT, "Задание", None, None)
        await runner._turns[CHAT].task
        assert "занят другим процессом Codex" in runner.sent[-1]["e"]["message"]
    finally:
        await runner.close(final=True)
        await external.disconnect()


async def test_temporary_state_writer_is_released_after_catalog_failure(runner, monkeypatch):
    runner.fake.exists = True
    await asyncio.to_thread(runner.aliases.put, runner.project, CHAT, THREAD)
    previous = runner._metadata
    async def failing(project, method, params):
        if method == "model/list":
            raise codex_rpc.RpcError()
        return await previous(project, method, params)
    monkeypatch.setattr(runner, "_metadata", failing)
    try:
        with pytest.raises(codex_rpc.RpcError):
            await runner.state(runner.project, CHAT)
        assert not runner.fake.writers
    finally:
        await runner.close(final=True)


async def test_images_are_codex_input_items_without_local_files(runner):
    from test_image_input import PNG
    pid = projects.pid(runner.project.name)
    for text in ("", "Что на скрине?"):
        runner.start(runner.project, pid, CHAT, text, None, None, images=[PNG])
        task = runner._turns[CHAT].task
        rid = await asking(runner)
        runner.answer(pid, CHAT, rid, "deny", None, None)
        await task
        content = [p for m, p in runner.fake.calls if m == "turn/start"][-1]["input"]
        assert content == [{"type": "image", "url": "data:image/png;base64," + PNG["data"]}] + (
            [{"type": "text", "text": text}] if text else [])
    assert list(runner.project.iterdir()) == []


async def test_stop_pending_question_and_close_prevent_new_turns(runner):
    runner.fake.ask = "item/tool/requestUserInput"
    runner.fake.ask_params = {"questions": [{"id": "q1", "question": "Как?", "header": "Выбор", "options": []}]}
    runner.start(runner.project, "a" * 16, CHAT, "Задание", None, None)
    task = runner._turns[CHAT].task
    rid = await asking(runner)
    with pytest.raises(Refused, match="gone"):
        runner.answer("b" * 16, CHAT, rid, "allow", None, {"Как?": "Так"})
    runner.answer("a" * 16, CHAT, rid, "allow", None, {"Как?": "Так"})
    await task
    assert runner.fake.replies[-1] == {"id": 91, "result": {"answers": {"q1": {"answers": ["Так"]}}}}

    runner.start(runner.project, "a" * 16, CHAT, "Ещё", None, None)
    task = runner._turns[CHAT].task
    await asking(runner)
    await runner.stop("a" * 16, CHAT)
    await task
    assert runner.sent[-1]["e"]["status"] == "stopped"
    assert ("turn/interrupt", {"threadId": THREAD, "turnId": "turn1"}) in runner.fake.calls
    await runner.close(hold=True)
    with pytest.raises(Refused, match="busy"):
        runner.start(runner.project, "a" * 16, CHAT, "Нельзя", None, None)


async def test_history_and_lists_cannot_cross_project_and_alias_survives(runner):
    runner.fake.exists = True
    runner.fake.turns = [{"id": "turn1", "items": [
        {"id": "u1", "type": "userMessage", "content": [{"type": "text", "text": "Вопрос"}]},
        {"id": "s1", "type": "commandExecution", "command": "git status", "cwd": str(runner.project),
         "status": "completed", "aggregatedOutput": "clean"},
        {"id": "a1", "type": "agentMessage", "text": "Ответ"}]}]
    await asyncio.to_thread(runner.aliases.put, runner.project, CHAT, THREAD)
    items = await runner.chats(runner.project)
    assert len(items) == 1 and items[0]["chat"] == CHAT and items[0]["tag"] == "studio"
    history = await runner.history(runner.project, CHAT, None, 1)
    assert history["start"] == 1 and history["total"] == 2
    assert history["items"][0]["steps"][0]["text"] == "clean" and history["items"][0]["text"] == "Ответ"
    with pytest.raises(Refused, match="not_found"):
        await runner.history(runner.project.parent, CHAT, None, 10)
    await runner.close(final=True)
    assert all(c.closed for c in runner.fake.clients)


async def test_unknown_server_request_is_rejected_without_granting_permissions(runner):
    runner.fake.ask = "unknown/request"
    runner.start(runner.project, "a" * 16, CHAT, "Задание", None, None)
    await runner._turns[CHAT].task
    assert runner.fake.replies[0]["error"]["code"] == -32601
    assert not any(f["e"]["k"] == "ask" for f in runner.sent)


async def test_missing_codex_finishes_without_using_claude(runner):
    runner.find_codex = lambda: None
    runner.start(runner.project, "a" * 16, CHAT, "Задание", None, None)
    await runner._turns[CHAT].task
    assert runner.sent[-1]["e"] == {"k": "done", "status": "error", "message": "Codex не найден на компьютере", "not_started": True}
    assert not runner.fake.calls


async def test_read_error_does_not_silently_start_a_new_conversation(runner):
    runner.fake.read_error = codex_rpc.RpcError()
    runner.start(runner.project, "a" * 16, CHAT, "Продолжить", None, None)
    await runner._turns[CHAT].task
    assert runner.sent[-1]["e"]["status"] == "error"
    assert not any(m in ("thread/start", "turn/start") for m, _ in runner.fake.calls)
    with pytest.raises(codex_rpc.RpcError):
        await runner.history(runner.project, CHAT, None, 10)


async def test_probe_reports_a_failure_even_with_a_saved_catalog(runner, monkeypatch):
    monkeypatch.setattr(codex_rpc, "version", lambda path: "test")
    assert await runner.probe(str(runner.project)) is True
    assert runner.catalog["models"] == [{"id": "codex-test", "label": "Codex"}]
    runner.fake.read_error = codex_rpc.RpcError()
    assert await runner.probe(str(runner.project)) is False
    assert runner.catalog["models"] == [{"id": "codex-test", "label": "Codex"}]
    await runner.close(final=True)


async def test_agent_dispatches_models_requests_and_shared_limit(runner, home, monkeypatch):
    from studio_agent import sessions, store
    from studio_agent.agent import Agent
    from studio_agent.pin import PinStore
    from studio_agent.turns import Runner
    from test_agent import opened, req

    monkeypatch.setattr(sessions, "chats", lambda *args: [{"chat": "claude-session"}])
    runner.models, runner.efforts = [{"id": "codex-test", "label": "Codex"}], ["ultra"]
    runner.catalog = {"version": "test", "models": runner.models, "efforts": runner.efforts}
    claude = Runner(lambda f: None, choices=store.Choices(home), cli_path=None)
    agent = Agent(store.Config(projects_root=str(home), max_turns=2), PinStore(home), claude, cc_version="test",
                  models=[{"id": "opus", "label": "Opus"}], efforts=["high"], codex=runner)
    await opened(agent)
    pid = projects.pid(runner.project.name)
    out = await agent.handle(req("chats", project=pid))
    assert [c.get("engine", "claude") for c in out[0]["data"]["items"]] == ["claude", "codex"]
    assert agent.hello()["codex"]["efforts"] == ["ultra"]
    out = await agent.handle(req("turn", project=pid, chat=CHAT, text="Тест", engine="codex", model="opus", effort=None))
    assert out[0]["code"] == "invalid" and not runner.live()
    # Общий предел — два хода обоих помощников; недействительная просьба не стартует третий процесс.
    monkeypatch.setattr(claude, "live", lambda: {"one": False, "two": False})
    out = await agent.handle(req("turn", project=pid, chat=CHAT, text="Тест", engine="codex", model="codex-test", effort="ultra"))
    assert out[0]["code"] == "busy" and not runner.live()
    monkeypatch.setattr(claude, "live", lambda: {})
    out = await agent.handle(req("turn", project=pid, chat=CHAT, text="Тест", engine="codex", model="codex-test", effort="ultra"))
    assert out[0]["ok"]
    task = runner._turns[CHAT].task
    rid = await asking(runner)
    out = await agent.handle(req("answer", project=pid, chat=CHAT, engine="codex", request_id=rid, decision="allow"))
    assert out[0]["ok"]
    await task
    await runner.close(final=True)
