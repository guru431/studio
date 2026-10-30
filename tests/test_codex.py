"""Контракт Codex app-server ↔ мост: реальных моделей, сети и профиля владельца здесь нет."""

import asyncio
from pathlib import Path

import pytest

from studio_agent import codex, codex_rpc, projects
from studio_agent.errors import Refused

CHAT = "11111111-2222-4333-8444-555555555555"
THREAD = "01999999-2222-7333-8444-555555555555"


class FakeRpc:
    def __init__(self, factory, path, cwd):
        self.factory, self.cwd = factory, cwd
        self.inbox = asyncio.Queue()
        self.closed = False

    async def connect(self):
        pass

    async def disconnect(self):
        self.closed = True

    async def request(self, method, params):
        self.factory.calls.append((method, params))
        if method == "thread/read":
            if self.factory.read_error is not None:
                raise self.factory.read_error
            if params["threadId"] != THREAD or not self.factory.exists:
                raise codex_rpc.RpcError(not_found=True)
            return {"thread": {"id": THREAD, "cwd": str(self.factory.project), "turns": self.factory.turns}}
        if method in ("thread/start", "thread/resume"):
            self.factory.exists = True
            return {"thread": {"id": THREAD, "cwd": str(self.factory.project)}}
        if method == "turn/start":
            self.inbox.put_nowait({"method": "item/agentMessage/delta", "params": {"threadId": THREAD, "delta": "Ответ"}})
            self.inbox.put_nowait({"id": 91, "method": self.factory.ask,
                                  "params": {"threadId": THREAD, "turnId": "turn1", "itemId": "step1",
                                             "command": "git status", **self.factory.ask_params}})
            return {"turn": {"id": "turn1"}}
        if method == "turn/interrupt":
            self.done("interrupted")
            return {}
        if method == "thread/list":
            return {"data": [{"id": THREAD, "cwd": str(self.factory.project), "preview": "Тест", "createdAt": 1,
                              "updatedAt": 2}, {"id": CHAT, "cwd": str(self.cwd / "other")}], "nextCursor": None}
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


async def test_new_thread_mapping_resume_and_permission_snapshot(runner, home):
    pid = projects.pid(runner.project.name)
    runner.start(runner.project, pid, CHAT, "Привет", "model-test", "high")
    task = runner._turns[CHAT].task
    rid = await asking(runner)
    running, snapshot = await runner.watch(pid, CHAT)
    assert running and snapshot[0]["e"]["text"] == "Ответ"
    assert snapshot[0]["e"]["asks"][0]["tool"] == "Shell"
    runner.answer(pid, CHAT, rid, "always", None, None)
    await task
    assert runner.fake.replies == [{"id": 91, "result": {"decision": "acceptForSession"}}]
    assert runner.sent[-1]["e"] == {"k": "done", "status": "done"}
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
    assert runner.sent[-1]["e"] == {"k": "done", "status": "error", "message": "Codex не найден на компьютере"}
    assert not runner.fake.calls


async def test_read_error_does_not_silently_start_a_new_conversation(runner):
    runner.fake.read_error = codex_rpc.RpcError()
    runner.start(runner.project, "a" * 16, CHAT, "Продолжить", None, None)
    await runner._turns[CHAT].task
    assert runner.sent[-1]["e"]["status"] == "error"
    assert not any(m in ("thread/start", "turn/start") for m, _ in runner.fake.calls)
    with pytest.raises(codex_rpc.RpcError):
        await runner.history(runner.project, CHAT, None, 10)


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
    agent = Agent(store.Config(projects_root=str(home)), PinStore(home), claude, cc_version="test",
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
