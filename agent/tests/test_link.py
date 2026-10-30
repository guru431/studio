import asyncio
import logging

import pytest
from websockets.asyncio.client import connect as real_connect

from fake_sdk import FakeFactory, delta, result_ok
from fake_studio import FakeStudio
from studio_agent import VERSION, link, limits, projects, sessions, store
from studio_agent.agent import Agent
from studio_agent.link import Link
from studio_agent.pin import PinStore
from studio_agent.turns import Runner

KEY = "device-key-not-for-the-journal"
CHAT = "0b1e2c3d-4f50-4617-8899-aabbccddeeff"


class Pauses:
    """Подставная `sleep`: паузы — в список, `hook(n)` после каждой; реального ожидания нет."""

    def __init__(self, hook=None):
        self.list = []
        self.hook = hook

    async def __call__(self, sec):
        self.list.append(sec)
        if self.hook is not None:
            self.hook(len(self.list))
        await asyncio.sleep(0)


def make(home, root, factory=None) -> Agent:
    agent = None
    runner = Runner(lambda f: agent.emit(f), choices=store.Choices(home), cli_path=None,
                    factory=factory or FakeFactory([result_ok()]))
    agent = Agent(store.Config(projects_root=str(root)), PinStore(home), runner, cc_version="2.3.1",
                  models=[{"id": "opus", "label": "Opus"}], efforts=["low", "high"])
    return agent


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(sessions, "exists", lambda project, chat: False)
    monkeypatch.setattr(sessions, "last_model", lambda project, chat: None)
    monkeypatch.setattr(sessions, "mark_studio", lambda project, chat: None)
    r = tmp_path / "projects"
    (r / "demo").mkdir(parents=True)
    return r


@pytest.fixture
def agent(home, root):
    return make(home, root)


@pytest.fixture
async def fake_studio():
    async with FakeStudio(KEY) as studio:
        yield studio


async def until(check, sec: float = 5):
    """Ждать условия: события идут через сеть localhost — короткими шагами, не дольше `sec`."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + sec
    while not check():
        if loop.time() > deadline:
            raise AssertionError("не дождались")
        await asyncio.sleep(0.001)


def start(studio, agent, key=KEY, sleep=None, **kw):
    states = []
    lk = Link(studio.url, key, agent, states.append, sleep=sleep or Pauses(), **kw)
    return lk, states, asyncio.create_task(lk.run())


async def test_hello_first_then_requests_are_answered(fake_studio, agent, caplog):
    calls = []

    def connect(uri, **kw):
        calls.append((uri, kw))
        return real_connect(uri, **kw)

    with caplog.at_level(logging.DEBUG):
        lk, states, task = start(fake_studio, agent, connect=connect)
        await fake_studio.connected()
        assert fake_studio.hellos[0]["v"] == 1 and fake_studio.hellos[0]["has_pin"] is False
        assert fake_studio.hellos[0] == agent.hello()
        await fake_studio.send_raw("не json")                          # пропуск, канал жив
        await fake_studio.send_raw("[1]")
        await fake_studio.send_raw('{"t": "что-то новое"}')
        assert (await fake_studio.request("open", pin=None))["ok"] is True
        assert (await fake_studio.request("projects"))["data"]["items"] == [{"pid": projects.pid("demo"),
                                                                             "name": "demo"}]
        assert agent.send == lk.send
        await lk.stop()
        assert await task == "stopped"
    assert states == ["connecting", "online", "stopped"]
    uri, kw = calls[0]
    assert uri == link.ws_url(fake_studio.url)
    assert kw == {"additional_headers": {"Authorization": f"Bearer {KEY}"}, "max_size": limits.FRAME_MAX,
                  "ping_interval": 20, "ping_timeout": 20, "open_timeout": 15,
                  "user_agent_header": f"studio-agent/{VERSION}"}
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("studio_agent"))  # журнал моста
    assert "канал: на связи" in ours and KEY not in ours and "demo" not in ours


async def test_welcome_goes_to_on_welcome_before_online(fake_studio, agent):
    seen = []
    lk, states, task = start(fake_studio, agent, on_welcome=lambda f: seen.append((dict(f), list(states))))
    await fake_studio.connected()
    await until(lambda: states[-1:] == ["online"])
    assert seen == [({"t": "welcome", "agent_id": 7, "name": "Рабочий ПК", "login": "owner"}, ["connecting"])]
    await lk.stop()
    await task


async def test_a_failing_on_welcome_does_not_break_the_link(fake_studio, agent):
    def broken(frame):
        raise KeyError("x")

    lk, states, task = start(fake_studio, agent, on_welcome=broken)
    await fake_studio.connected()
    assert (await fake_studio.request("open", pin=None))["ok"] is True
    await lk.stop()
    assert await task == "stopped"


async def test_revoked_stops_and_replaced_stops(fake_studio, agent):
    lk, states, task = start(fake_studio, agent)
    await fake_studio.connected()
    await fake_studio.close(4401)
    assert await task == "revoked" and states[-1] == "revoked"
    await asyncio.sleep(0.05)
    assert fake_studio.attempts == 1                                   # подключений больше нет

    lk, states, task = start(fake_studio, agent)
    await fake_studio.connected(2)
    await fake_studio.close(4409)
    assert await task == "replaced" and states == ["connecting", "online", "replaced"]
    await asyncio.sleep(0.05)
    assert fake_studio.attempts == 2


async def test_unknown_key_is_revoked_without_pauses(fake_studio, agent):
    pauses = Pauses()
    lk, states, task = start(fake_studio, agent, key="other-key", sleep=pauses)
    assert await task == "revoked"
    assert pauses.list == [] and states == ["connecting", "revoked"]


async def test_restart_reconnects_and_closes_open_sessions(fake_studio, agent):
    pauses = Pauses()
    lk, states, task = start(fake_studio, agent, sleep=pauses)
    await fake_studio.connected()
    assert (await fake_studio.request("open", pin=None))["ok"] is True
    await fake_studio.close(1012)
    await fake_studio.connected(2)
    assert (await fake_studio.request("projects"))["code"] == "closed"  # новый канал — сессии прочь
    assert pauses.list == [1]
    fake_studio.drop()                                                  # обрыв без закрытия
    await fake_studio.connected(3)
    assert pauses.list == [1, 1]                                        # счёт сброшен после welcome
    await until(lambda: states.count("online") == 3)
    assert states == ["connecting", "online", "offline"] * 2 + ["connecting", "online"]
    await lk.stop()
    assert await task == "stopped"


async def test_protocol_close_pauses_a_minute(fake_studio, agent):
    pauses = Pauses()
    lk, states, task = start(fake_studio, agent, sleep=pauses)
    await fake_studio.connected()
    await fake_studio.close(4400)
    await fake_studio.connected(2)
    assert pauses.list == [limits.PROTOCOL_PAUSE_SEC]
    await lk.stop()
    await task


async def test_handshake_rejection_is_retried_not_revoked(fake_studio, agent):
    fake_studio.reject_handshake = 401

    def hook(n):
        if n == 2:
            fake_studio.reject_handshake = None

    pauses = Pauses(hook)
    lk, states, task = start(fake_studio, agent, sleep=pauses)
    await fake_studio.connected()
    await until(lambda: states[-1] == "online")
    assert pauses.list == [1, 2]
    await lk.stop()
    await task


async def test_backoff_grows_to_the_last_step(fake_studio, agent):
    fake_studio.reject_handshake = 503

    def hook(n):
        if n == 9:
            fake_studio.reject_handshake = None

    pauses = Pauses(hook)
    lk, states, task = start(fake_studio, agent, sleep=pauses)
    await fake_studio.connected()
    assert pauses.list == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    await lk.stop()
    await task


async def test_no_welcome_closes_and_pauses(fake_studio, agent, monkeypatch):
    monkeypatch.setattr(link, "WELCOME_SEC", 0.05)
    fake_studio.silent = True

    def hook(n):
        fake_studio.silent = False

    pauses = Pauses(hook)
    lk, states, task = start(fake_studio, agent, sleep=pauses)
    await fake_studio.connected()
    assert pauses.list == [1] and len(fake_studio.hellos) == 2
    await until(lambda: states[-1] == "online")
    assert states == ["connecting", "offline", "connecting", "online"]
    await lk.stop()
    await task


async def test_turn_res_comes_before_its_events_and_a_card_survives_reconnect(fake_studio, home, root):
    factory = FakeFactory([delta("При"), ("ask", "Bash", {"command": "ls"}, []), delta("вет"), result_ok()])
    agent = make(home, root, factory)
    pid = projects.pid("demo")
    pauses = Pauses()
    lk, states, task = start(fake_studio, agent, sleep=pauses)
    await fake_studio.connected()
    await fake_studio.request("open", pin=None)
    res = await fake_studio.request("turn", project=pid, chat=CHAT, text="Привет", model=None, effort=None)
    assert res["ok"] is True
    await until(lambda: any(e["e"]["k"] == "ask" for e in fake_studio.events))
    first = [f for n, f in fake_studio.frames if n == 0]
    at_res = next(i for i, f in enumerate(first) if f.get("id") == res["id"])
    at_ev = next(i for i, f in enumerate(first) if f["t"] == "ev")
    assert at_res < at_ev and first[at_ev]["e"] == {"k": "text", "text": "При"}

    await fake_studio.close(1012)                                       # карточка открыта
    await fake_studio.connected(2)
    assert (await fake_studio.request("watch", project=pid, chat=CHAT))["code"] == "closed"
    assert (await fake_studio.request("open", pin=None))["ok"] is True
    watch = await fake_studio.request("watch", project=pid, chat=CHAT)
    assert watch["data"] == {"running": True}
    await until(lambda: any(f.get("e", {}).get("k") == "snapshot" for n, f in fake_studio.frames if n == 1))
    second = [f for n, f in fake_studio.frames if n == 1]
    at_watch = next(i for i, f in enumerate(second) if f.get("id") == watch["id"])
    snapshot = second[at_watch + 1]["e"]
    assert snapshot["k"] == "snapshot" and snapshot["text"] == "При"
    [ask] = snapshot["asks"]
    assert ask["tool"] == "Bash" and ask["kind"] == "permission"

    answer = await fake_studio.request("answer", project=pid, chat=CHAT, request_id=ask["request_id"],
                                       decision="allow", message=None, answers=None)
    assert answer["ok"] is True
    await until(lambda: any(f.get("e", {}).get("k") == "done" for n, f in fake_studio.frames))
    [(n, done)] = [(n, f) for n, f in fake_studio.frames if f.get("e", {}).get("k") == "done"]
    assert n == 1 and done["e"] == {"k": "done", "status": "done"}
    assert [f["e"]["text"] for n, f in fake_studio.frames if n == 1 and f.get("e", {}).get("k") == "text"] == ["вет"]
    assert factory.clients[0].decisions[0].updated_input == {"command": "ls"}
    assert pauses.list == [1]
    await lk.stop()
    await task


async def test_events_without_a_channel_are_dropped(fake_studio, agent):
    def hook(n):
        agent.emit({"t": "ev", "e": {"k": "text", "text": "между каналами"}})

    states = []
    lk = Link(fake_studio.url, KEY, agent, states.append, sleep=Pauses(hook))
    lk.send({"t": "ev", "e": {"k": "text", "text": "до канала"}})        # канала нет — пропуск
    task = asyncio.create_task(lk.run())
    await fake_studio.connected()
    await fake_studio.close(1012)
    await fake_studio.connected(2)
    await until(lambda: states.count("online") == 2)
    agent.emit({"t": "ev", "e": {"k": "text", "text": "в новый канал"}})
    await until(lambda: fake_studio.events)
    await asyncio.sleep(0.02)
    assert [e["e"]["text"] for e in fake_studio.events] == ["в новый канал"]
    await lk.stop()
    await task


async def test_an_unexpected_failure_pauses_and_reconnects(fake_studio, agent, caplog):
    real, calls = agent.hello, []

    def hello():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("сбой вне сети")
        return real()

    agent.hello = hello
    pauses = Pauses()
    lk, states, task = start(fake_studio, agent, sleep=pauses)
    await fake_studio.connected()                               # не конец канала навсегда: пауза и снова
    await until(lambda: states[-1] == "online")
    assert pauses.list == [1] and states == ["connecting", "offline", "connecting", "online"]
    assert "канал: сбой: RuntimeError" in caplog.text
    await lk.stop()
    assert await task == "stopped"


async def test_stop_sends_what_is_queued_before_closing(fake_studio, agent):
    def connect(uri, **kw):
        return Slow(real_connect(uri, **kw))

    class Slow:
        """Канал с медленной отправкой: без досылки кадры очереди пропали бы при `stop()`."""

        def __init__(self, cm):
            self.cm = cm

        def __await__(self):
            return self._open().__await__()

        async def _open(self):
            self.ws = await self.cm
            return self

        async def send(self, text):
            await asyncio.sleep(0.005)
            await self.ws.send(text)

        def __getattr__(self, name):
            return getattr(self.ws, name)

    lk, states, task = start(fake_studio, agent, connect=connect)
    await fake_studio.connected()
    await until(lambda: states[-1] == "online")
    for n in range(20):
        lk.send({"t": "ev", "e": {"k": "done", "status": "error", "message": f"ход прерван {n}"}})
    await lk.stop()
    assert await task == "stopped"
    await until(lambda: len(fake_studio.events) == 20)
    assert [e["e"]["message"] for e in fake_studio.events] == [f"ход прерван {n}" for n in range(20)]


async def test_stop_before_run(agent):
    lk = Link("http://127.0.0.1:9", KEY, agent, sleep=Pauses())
    await lk.stop()
    assert await lk.run() == "stopped"


def test_ws_url_rules():
    assert link.ws_url("https://studio.example.com") == "wss://studio.example.com/api/agent/link"
    assert link.ws_url("https://studio.example.com/") == "wss://studio.example.com/api/agent/link"
    assert link.ws_url("http://127.0.0.1:8000") == "ws://127.0.0.1:8000/api/agent/link"
    assert link.ws_url("http://localhost:8000") == "ws://localhost:8000/api/agent/link"
    assert link.ws_url("http://[::1]:8000") == "ws://[::1]:8000/api/agent/link"
    for bad in ("http://studio.example.com", "ftp://studio.example.com", "studio.example.com", "https://", "https://a/b",
                "https://u@studio.example.com", "https://studio.example.com?x=1"):
        with pytest.raises(ValueError):
            link.ws_url(bad)
