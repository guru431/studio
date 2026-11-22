"""Общая основа ACP (`acp.AcpRunner`): движок, усилие и дети — от подкласса, без CLI и сети."""

import asyncio
import re
import uuid
from types import SimpleNamespace

from studio_agent import acp, projects
from studio_agent.turns import _Turn

CHAT = "11111111-2222-4333-8444-555555555555"
SESSION = "probe-session-1"


def configuration(effort="high"):
    return {"configOptions": [
        {"id": "model", "currentValue": "m1", "options": [{"value": "m1", "name": "M1"}]},
        {"id": "reasoning_effort", "currentValue": effort, "options": [
            {"value": value, "name": value} for value in ("off", "high", "max")]}]}


class FakeRpc:
    def __init__(self, project):
        self.project, self.info = project, {"agentInfo": {"version": "9.9.9"}}
        self.inbox, self.calls, self.config = asyncio.Queue(), [], configuration()

    async def disconnect(self):
        pass

    async def request(self, method, params, **kwargs):
        self.calls.append((method, params))
        if method == "session/new":
            return {"sessionId": SESSION, **self.config}
        if method == "session/set_config_option":
            assert params["configId"] in ("model", "reasoning_effort")
            if params["configId"] == "reasoning_effort":
                self.config = configuration(params["value"])
            return self.config
        if method == "session/list":
            return {"sessions": [{"sessionId": SESSION, "cwd": str(self.project)}]}
        raise AssertionError(method)


class Probe(acp.AcpRunner):
    ENGINE, LABEL = "probe", "Probe"
    SESSION_ID = re.compile(r"probe-[a-z0-9-]{1,100}")
    EFFORT_KEY = "reasoning_effort"
    ALIASES_FILE, CHOICES_DIR = "probe-chats.json", "probe"
    NAMESPACE = uuid.UUID("00000000-0000-4000-8000-000000000001")
    FAILED = UNAVAILABLE = "сбой"

    async def _connect(self, project, *, access=None):
        return self.rpc_factory(project)

    async def _open(self, rpc, project, target, directories=None):
        return dict(rpc.config)

    async def _discard(self, rpc, target):
        self.discarded.append(target)

    async def _history_items(self, project, target, row, *, image_blocks=None):
        return [{"id": "0", "role": "user", "text": "привет", "steps": [], "time": None}]


def runner_for(home):
    project = home / "demo"
    project.mkdir()
    runner = Probe(lambda _: None, home=home, factory=FakeRpc)
    runner.discarded, runner.project = [], project
    return runner


async def test_runner_reports_its_own_engine_and_effort_option(tmp_path):
    runner = runner_for(tmp_path)
    assert await runner.probe(runner.project)
    assert runner.catalog["efforts"] == ["high", "max", "off"]
    assert runner.catalog["defaults"]["effort"] == "high" and runner.discarded == [SESSION]
    items = await runner.chats(runner.project)
    assert items[0]["engine"] == "probe" and items[0]["chat"] == str(uuid.uuid5(Probe.NAMESPACE, SESSION))
    page = await runner.history(runner.project, items[0]["chat"], None, 10)
    assert page["engine"] == "probe" and page["items"][0]["text"] == "привет"
    state = await runner.state(runner.project, items[0]["chat"], effort="max")
    assert state["settings"]["effort"] == "max" and state["settings"]["commands"] == []
    await runner.close()


def test_runner_without_children_ignores_child_metadata(tmp_path):
    runner = runner_for(tmp_path)
    t = _Turn(runner.project, projects.pid(runner.project.name), CHAT, "t")
    t.client = SimpleNamespace(session=SESSION)
    update = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "текст"},
              "_meta": {"opencode/child-session": {"id": "ses_child", "parentID": SESSION, "depth": 1}}}
    runner._notification(t, update)
    assert t.feed.text == "текст" and "agents" not in t.progress and not t.opencode_children
    rpc = SimpleNamespace(inbox=asyncio.Queue())
    rpc.inbox.put_nowait({"method": "session/update", "params": {"sessionId": SESSION, "update": update}})
    assert runner._updates(rpc, SESSION) == [update]
