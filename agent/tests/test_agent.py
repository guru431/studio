import asyncio
import logging
import secrets
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from fake_sdk import HOLD, FakeFactory, delta, result_ok
from studio_agent import files, projects, sessions, store
from studio_agent.agent import Agent
from studio_agent.pin import PinStore
from studio_agent.turns import Runner

SESSION = "3b1e" * 8
CHAT = "0b1e2c3d-4f50-4617-8899-aabbccddeeff"
MODELS = [{"id": "opus", "label": "Opus"}, {"id": "sonnet", "label": "Sonnet"}]


def req(op, session=SESSION, **kw):
    return {"t": "req", "id": secrets.token_hex(8), "op": op, "session": session, **kw}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(home, root, factory=None, clock=None) -> Agent:
    """Runner раньше Agent, `emit` — поздним связыванием (Ruling 5)."""
    agent = None
    runner = Runner(lambda f: agent.emit(f), choices=store.Choices(home), cli_path=None,
                    factory=factory or FakeFactory([result_ok()]))
    agent = Agent(store.Config(projects_root=str(root)), PinStore(home), runner, now=clock or Clock(),
                  cc_version="2.3.1", models=MODELS, efforts=["low", "high"])
    agent.sent = []
    agent.send = agent.sent.append
    return agent


@pytest.fixture
def root(tmp_path, monkeypatch):
    """Папка проектов с проектом `demo`; сессий SDK нет."""
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
def agent_with_pin(home, root):
    PinStore(home).set_pin("2468")
    return make(home, root)


async def settle(a: Agent) -> None:
    while a.runner._turns:
        await asyncio.wait({t.task for t in a.runner._turns.values()})


async def test_configurable_pin_timeout_is_checked_on_open_and_each_request(agent_with_pin):
    agent = agent_with_pin
    assert (await agent.handle(req("open", pin="2468", idle_sec=600)))[0]["ok"]
    agent.open._now.t += 601
    response = (await agent.handle(req("projects", idle_sec=600)))[0]
    assert response["code"] == "closed"
    response = (await agent.handle(req("open", pin=None, idle_sec=600)))[0]
    assert response["code"] == "pin_needed"
    assert (await agent.handle(req("open", pin="2468", idle_sec=600)))[0]["ok"]


@pytest.mark.parametrize("value", [True, 0, 59, 604801, None, "600"])
async def test_invalid_pin_timeout_never_opens_the_computer(agent_with_pin, value):
    response = (await agent_with_pin.handle(req("open", pin="2468", idle_sec=value)))[0]
    assert response["code"] == "invalid"
    assert not agent_with_pin.open.touch(SESSION)


async def opened(a: Agent, session=SESSION, pin=None) -> None:
    assert (await a.handle(req("open", session=session, pin=pin)))[0]["ok"] is True


async def test_turn_limit_counts_all_backends_and_changes_without_stopping_jobs(home, root):
    backends = [Runner(lambda f: None, choices=store.Choices(home), cli_path=None, factory=FakeFactory([HOLD]))
                for _ in range(3)]
    for backend in backends[1:]:
        backend.catalog = {"models": MODELS, "efforts": ["high"]}
        backend.models, backend.efforts = MODELS, ["high"]
    agent = Agent(store.Config(projects_root=str(root)), PinStore(home), backends[0], cc_version="test",
                  models=MODELS, efforts=["high"], codex=backends[1], opencode=backends[2])
    await opened(agent)

    async def start(number, engine="claude"):
        return (await agent.handle(req("turn", project=projects.pid("demo"),
                                       chat=f"00000000-0000-4000-8000-{number:012x}", text="Задание",
                                       model=None, effort=None, engine=engine)))[0]

    try:
        assert agent.cfg.max_turns == 8
        for number in range(8):
            assert (await start(number))["ok"]
        assert (await start(8, "opencode"))["code"] == "busy"
        agent.set_max_turns(10)
        assert [backend.max_turns for backend in backends] == [10, 10, 10]
        assert (await start(8, "opencode"))["ok"]
        assert (await start(9, "codex"))["ok"]
        assert (await start(10))["code"] == "busy"
        # Понижение предела не прерывает десять идущих заданий и не разрешает новые.
        before = [dict(backend._turns) for backend in backends]
        agent.set_max_turns(1)
        assert [dict(backend._turns) for backend in backends] == before
        assert (await start(10))["code"] == "busy"
        assert (await start(0))["code"] == "running"
        await asyncio.gather(*(backend.close() for backend in backends))
        assert (await start(10))["ok"]
    finally:
        await asyncio.gather(*(backend.close(final=True) for backend in backends))


async def test_activity_requires_pin_and_aggregates_all_backends(agent):
    assert (await agent.handle(req("activity")))[0]["code"] == "closed"
    await opened(agent)
    agent.runner.activity = lambda: [{"project": "a" * 16, "chat": CHAT, "running": True, "asking": False}]
    agent.codex = SimpleNamespace(catalog={"models": []}, activity=lambda: [
        {"project": "b" * 16, "chat": CHAT, "running": True, "asking": True}])
    result = (await agent.handle(req("activity")))[0]
    assert result["ok"] is True
    assert [(row["project"], row["engine"], row["asking"]) for row in result["data"]["items"]] == [
        ("a" * 16, "claude", False), ("b" * 16, "codex", True)]


async def test_project_summary_requires_pin_and_resolves_project(agent, root, monkeypatch):
    from studio_agent import project_summary

    frame = req("project_summary", project=projects.pid("demo"))
    assert (await agent.handle(frame))[0]["code"] == "closed"
    await opened(agent)
    seen = []
    monkeypatch.setattr(project_summary, "read", lambda path: seen.append(path) or {"findings": 2})
    assert (await agent.handle(frame))[0]["data"] == {"findings": 2}
    assert seen == [root / "demo"]
    assert (await agent.handle(req("project_summary", project="0" * 16)))[0]["code"] == "not_found"


async def test_file_stats_checks_pin_project_and_serializes_scans(agent, root, monkeypatch):
    from studio_agent import file_stats

    frame = req("file_stats", project=projects.pid("demo"), path="")
    assert (await agent.handle(frame))[0]["code"] == "closed"
    await opened(agent)
    seen = []
    monkeypatch.setattr(file_stats, "scan", lambda project, path: seen.append((project, path)) or {"items": []})
    assert (await agent.handle(frame))[0]["data"] == {"items": []}
    assert seen == [(root / "demo", "")]
    async with agent._file_stats_lock:
        assert (await agent.handle(frame))[0]["code"] == "busy"
    assert (await agent.handle(req("file_stats", project=projects.pid("demo"), path="../outside")))[0]["code"] == "invalid"


def test_hello(agent):
    assert agent.hello() == {"t": "hello", "v": 1, "cc_version": "2.3.1", "projects_root": agent.cfg.projects_root,
                             "has_pin": False, "models": MODELS, "efforts": ["low", "high"], "images": True, "chat_state": True,
                             "chat_control": True, "attachments": True, "project_activity": True,
                             "subscription_usage": True, "project_summary": True, "file_stats": True, "file_operations": True,
                             "file_suggestions": True, "chat_search": True, "queue_edit": True,
                             "chat_fork": True, "subagents": True, "worktrees": True, "pin_timeout": True}
    agent.set_models([{"id": "haiku", "label": "Haiku"}], ["max"])
    assert agent.hello()["models"] == [{"id": "haiku", "label": "Haiku"}] and agent.hello()["efforts"] == ["max"]


def test_hello_with_an_unreadable_pin_says_there_is_a_pin(home, root, caplog):
    (home / "pin.bin").write_bytes(b"not a dpapi blob")             # сброс пароля Windows, порча
    assert make(home, root).hello()["has_pin"] is True              # безопасная сторона, канал не падает
    assert "PIN не читается: OSError" in caplog.text


async def test_hello_does_not_read_the_pin_and_refresh_pin_does(agent, home, monkeypatch):
    def boom(self):
        raise AssertionError("pin.bin на цикле канала")

    PinStore(home).set_pin("2468")
    monkeypatch.setattr(PinStore, "has_pin", property(boom))
    assert agent.hello()["has_pin"] is False                        # в памяти: DPAPI на цикле не зовётся
    monkeypatch.undo()
    await agent.refresh_pin()                                       # новый канал ядра — заново, в потоке
    assert agent.hello()["has_pin"] is True


async def test_everything_but_open_is_closed_until_opened(agent):
    assert (await agent.handle(req("projects")))[0]["code"] == "closed"
    assert (await agent.handle(req("open", pin=None)))[0]["ok"] is True
    assert (await agent.handle(req("projects")))[0]["data"]["items"] == [{"pid": projects.pid("demo"), "name": "demo"}]


async def test_subscription_usage_requires_open_session_and_valid_refresh(agent):
    agent._usage.read = AsyncMock(return_value={"available": False})
    assert (await agent.handle(req("usage")))[0]["code"] == "closed"
    agent._usage.read.assert_not_called()
    await opened(agent)
    assert (await agent.handle(req("usage", refresh="true")))[0]["code"] == "invalid"
    assert (await agent.handle(req("usage", refresh=True)))[0]["data"] == {"available": False}
    agent._usage.read.assert_awaited_once_with("claude", agent.cfg.projects_root, agent.runner, refresh=True)
    agent.new_link()
    assert (await agent.handle(req("usage")))[0]["code"] == "closed"


async def test_answer_keeps_the_request_id(agent):
    r = req("projects")
    [res] = await agent.handle(r)
    assert res == {"t": "res", "id": r["id"], "ok": False, "code": "closed", "message": "компьютер закрыт"}


async def test_pin_flow_and_open_null_for_an_open_session(agent_with_pin):
    a = agent_with_pin
    assert a.hello()["has_pin"] is True
    assert (await a.handle(req("open", pin=None)))[0]["code"] == "pin_needed"
    assert (await a.handle(req("open", pin="1111")))[0] | {"id": None} == {"t": "res", "id": None, "ok": False,
        "code": "pin", "message": "неверный PIN", "left": 4}
    assert (await a.handle(req("open", pin="2468")))[0]["ok"]
    assert (await a.handle(req("open", pin=None)))[0]["ok"]           # уже открыта — без PIN


async def test_locked_pin_is_pin_locked_even_for_open_null(agent_with_pin):
    a = agent_with_pin
    codes = [(await a.handle(req("open", pin="1111")))[0]["code"] for _ in range(5)]
    assert codes == ["pin", "pin", "pin", "pin", "pin_locked"]
    assert (await a.handle(req("open", pin=None)))[0]["code"] == "pin_locked"
    res = (await a.handle(req("open", pin="2468")))[0]
    assert res["code"] == "pin_locked" and res["message"] == "PIN заблокирован — снимите блок на самом компьютере"


async def test_the_pin_lock_from_the_studio_is_reported_once(agent_with_pin):
    """Трей строит меню заранее: «Снять блок PIN» он узнаёт от `on_pin_locked`, а не от смены состояния канала."""
    a = agent_with_pin
    locks = []
    a.on_pin_locked = lambda: locks.append(1)
    for _ in range(4):
        await a.handle(req("open", pin="1111"))
    assert locks == []
    await a.handle(req("open", pin="1111"))                           # пятый неверный — блок
    await a.handle(req("open", pin="2468"))                           # блок уже стоит — не новость
    assert locks == [1]


async def test_an_idle_session_closes(home, root):
    clock = Clock()
    PinStore(home).set_pin("2468")
    a = make(home, root, clock=clock)
    await opened(a, pin="2468")
    clock.t += 1801
    assert (await a.handle(req("projects")))[0]["ok"]                  # полчаса больше не закрывают
    clock.t += 10800
    assert (await a.handle(req("projects")))[0]["ok"]                  # продлена
    clock.t += 10801
    assert (await a.handle(req("projects")))[0]["code"] == "closed"
    assert (await a.handle(req("open", pin=None)))[0]["code"] == "pin_needed"
    await opened(a, pin="2468")


async def test_close_and_new_link_close_sessions(agent):
    other = "ab" * 16
    await opened(agent)
    await opened(agent, other)
    agent.close_session(SESSION)
    assert (await agent.handle(req("projects")))[0]["code"] == "closed"
    assert (await agent.handle(req("projects", session=other)))[0]["ok"]
    await opened(agent)
    agent.new_link()
    assert (await agent.handle(req("projects")))[0]["code"] == "closed"
    assert (await agent.handle(req("projects", session=other)))[0]["code"] == "closed"


async def test_reconnect_preserves_pin_without_extending_idle_time(home, root):
    clock = Clock()
    PinStore(home).set_pin("2468")
    a = make(home, root, clock=clock)
    await opened(a, pin="2468")
    clock.t += 1801
    a.new_link(reconnect=True)
    assert (await a.handle(req("open", pin=None)))[0]["ok"]
    assert (await a.handle(req("projects", session="ab" * 16)))[0]["code"] == "closed"
    clock.t += 10799
    a.new_link(reconnect=True)
    clock.t += 2
    a.new_link(reconnect=True)
    assert (await a.handle(req("open", pin=None)))[0]["code"] == "pin_needed"


@pytest.mark.parametrize("reconnect", [False, True])
async def test_a_pin_checked_across_a_new_link_does_not_open_the_session(agent_with_pin, monkeypatch, reconnect):
    a = agent_with_pin
    real = a.pins.check

    def check(pin):                                                 # scrypt кончился уже после нового канала
        a.new_link(reconnect=reconnect)
        return real(pin)

    monkeypatch.setattr(a.pins, "check", check)
    res = (await a.handle(req("open", pin="2468")))[0]
    assert (res["ok"], res["code"]) == (False, "closed")
    assert (await a.handle(req("projects")))[0]["code"] == "closed"
    monkeypatch.undo()
    await opened(a, pin="2468")                                     # по новому каналу — открывается
    assert (await a.handle(req("projects")))[0]["ok"]


async def test_turn_events_go_to_send_and_only_when_it_is_set(home, root):
    a = make(home, root, FakeFactory([delta("При"), result_ok()]))
    await opened(a)
    res = (await a.handle(req("turn", project=projects.pid("demo"), chat=CHAT, text="Привет", model="opus",
                              effort=None)))[0]
    assert res["ok"] and isinstance(res["data"]["turn"], str)
    await settle(a)
    assert [f["e"]["k"] for f in a.sent] == ["text", "done"] and a.sent[0]["turn"] == res["data"]["turn"]
    a.send = None
    a.emit({"t": "ev"})                                                 # канала нет — событие пропадает


async def test_watch_sends_res_then_snapshot(home, root):
    a = make(home, root, FakeFactory([delta("При"), HOLD]))
    await opened(a)
    pid = projects.pid("demo")
    assert (await a.handle(req("watch", project=pid, chat=CHAT)))[0]["data"] == {"running": False}
    await a.handle(req("turn", project=pid, chat=CHAT, text="Привет", model=None, effort=None))
    for _ in range(2000):
        if a.sent:
            break
        await asyncio.sleep(0.001)
    out = await a.handle(req("watch", project=pid, chat=CHAT))
    assert [f["t"] for f in out] == ["res", "ev"]
    assert out[0]["data"] == {"running": True}
    assert out[1]["e"] == {"k": "snapshot", "text": "При", "steps": [], "asks": []}
    assert (await a.handle(req("stop", project=pid, chat=CHAT)))[0]["ok"]
    await settle(a)
    assert (await a.handle(req("stop", project=pid, chat=CHAT)))[0]["code"] == "idle"


async def test_answer_stop_watch_of_another_project_do_not_see_the_turn(home, root):
    (root / "other").mkdir()
    a = make(home, root, FakeFactory([("ask", "Bash", {"command": "ls"}, []), result_ok()]))
    await opened(a)
    pid, other = projects.pid("demo"), projects.pid("other")
    await a.handle(req("turn", project=pid, chat=CHAT, text="Привет", model=None, effort=None))
    for _ in range(2000):
        if a.runner.live().get(CHAT):
            break
        await asyncio.sleep(0.001)
    rid = a.sent[-1]["e"]["request_id"]
    out = await a.handle(req("watch", project=other, chat=CHAT))
    assert len(out) == 1 and out[0]["data"] == {"running": False}
    res = (await a.handle(req("answer", project=other, chat=CHAT, request_id=rid, decision="allow")))[0]
    assert res["code"] == "gone"
    assert (await a.handle(req("stop", project=other, chat=CHAT)))[0]["code"] == "idle"
    assert a.runner.live() == {CHAT: True}                              # ход и карточка — на месте
    assert (await a.handle(req("answer", project=pid, chat=CHAT, request_id=rid, decision="allow")))[0]["ok"]
    await settle(a)
    assert a.sent[-1]["e"] == {"k": "done", "status": "done"}


async def test_watch_without_a_turn_does_not_look_for_the_chat(agent):
    await opened(agent)
    out = await agent.handle(req("watch", project="0" * 16, chat=CHAT))
    assert len(out) == 1 and out[0]["data"] == {"running": False}


async def test_opencode_is_optional_and_never_falls_back_to_claude(agent):
    await opened(agent)
    res = (await agent.handle(req("state", engine="opencode", project=projects.pid("demo"), chat=CHAT)))[0]
    assert res["code"] == "unsupported" and res["message"] == "OpenCode не найден на компьютере"
    assert "opencode" not in agent.hello() and not agent.runner.live()


async def test_opencode_routes_use_its_catalog_and_share_the_turn_limit(agent, root, monkeypatch):
    model = "gateway/" + "model-" * 20
    backend = SimpleNamespace(models=[{"id": model, "label": "OpenCode"}], efforts=["default", "high"],
                              catalog={"version": "2.2.4", "models": [{"id": model, "label": "OpenCode"}],
                                       "efforts": ["default", "high"]},
                              chats=AsyncMock(return_value=[{"chat": CHAT, "engine": "opencode"}]),
                              history=AsyncMock(return_value={"messages": []}),
                              state=AsyncMock(return_value={"settings": {"model": model}}),
                              live=Mock(return_value={}), start=Mock(return_value="turn-id"),
                              watch=AsyncMock(return_value=(False, [])), answer=Mock(), stop=AsyncMock(), check_owner=Mock())
    agent.opencode = backend
    await opened(agent)
    assert agent.hello()["opencode"] == backend.catalog
    pid, project = projects.pid("demo"), root / "demo"
    frame = {"engine": "opencode", "project": pid, "chat": CHAT}
    res = (await agent.handle(req("chats", project=pid)))[0]
    assert res["data"]["items"][-1]["engine"] == "opencode"
    res = (await agent.handle(req("history", **frame, before=123, limit=20)))[0]
    assert res["ok"]
    backend.history.assert_awaited_once_with(project, CHAT, 123, 20)
    res = (await agent.handle(req("state", **frame, model=model, effort="high", access="auto")))[0]
    assert res["data"] == {"settings": {"model": model}}
    backend.state.assert_awaited_once_with(project, CHAT, model, "high", "auto")
    for invalid in ({"model": "opus"}, {"effort": "ultra"}, {"access": "danger-full-access"}):
        res = (await agent.handle(req("turn", **frame, text="тест", **invalid)))[0]
        assert res["code"] == "invalid"
    assert not backend.start.called
    monkeypatch.setattr(agent.runner, "live", lambda: {"claude-turn": False})
    agent.codex = SimpleNamespace(live=lambda: {"codex-turn": False})
    agent.set_max_turns(2)
    res = (await agent.handle(req("turn", **frame, text="тест", model=model, effort="high", access="default")))[0]
    assert res["code"] == "busy" and not backend.start.called
    agent.codex = None
    res = (await agent.handle(req("turn", **frame, text="тест", model=model, effort="high", access="default")))[0]
    assert res["ok"]
    backend.start.assert_called_once_with(project, pid, CHAT, "тест", model, "high", images=[], access="default", owner=None)
    assert (await agent.handle(req("watch", **frame)))[0]["data"] == {"running": False}
    backend.watch.assert_awaited_once_with(pid, CHAT)
    assert (await agent.handle(req("answer", **frame, request_id="1" * 16, decision="allow")))[0]["ok"]
    backend.answer.assert_called_once_with(pid, CHAT, "1" * 16, "allow", None, None)
    assert (await agent.handle(req("stop", **frame)))[0]["ok"]
    backend.stop.assert_awaited_once_with(pid, CHAT)


async def test_bad_fields_are_invalid(agent):
    await opened(agent)
    pid = projects.pid("demo")
    bad = [
        req("projects", session="a" * 31),
        req("chats", project="XYZ"),
        req("history", project=pid, chat=CHAT.upper(), before=None, limit=10),
        req("history", project=pid, chat=CHAT, before=None, limit=0),
        req("history", project=pid, chat=CHAT, before=None, limit=101),
        req("history", project=pid, chat=CHAT, before=None, limit=True),
        req("history", project=pid, chat=CHAT, before=-1, limit=10),
        req("files", project=pid, path="CON"),
        req("read", project=pid, path=None),
        req("turn", project=pid, chat=CHAT, text="x", model="gpt", effort=None),
        req("turn", project=pid, chat=CHAT, text="x", model=None, effort="max"),
        req("turn", project=pid, chat=CHAT, text="x" * 100_001, model=None, effort=None),
        req("save", project=pid, path="a.txt", text="x", version="0" * 63),
        req("answer", project=pid, chat=CHAT, request_id="a b", decision="allow", message=None, answers=None),
        req("open", pin=1234),
        req("nonsense"),
    ]
    for r in bad:
        res = (await agent.handle(r))[0]
        assert (res["id"], res["code"], res["message"]) == (r["id"], "invalid", "неверные поля"), r


async def test_bad_id_is_answered_with_it_or_not_at_all(agent):
    res = (await agent.handle(req("open", pin=None) | {"id": "zz"}))[0]
    assert res["id"] == "zz" and res["code"] == "invalid"
    assert await agent.handle(req("open", pin=None) | {"id": "z" * 65}) == []
    assert await agent.handle(req("open", pin=None) | {"id": 7}) == []


async def test_files_read_and_unknown_project(agent, root):
    (root / "demo" / "a.txt").write_bytes(b"hello")
    await opened(agent)
    pid = projects.pid("demo")
    assert (await agent.handle(req("read", project=pid, path="a.txt")))[0]["data"]["text"] == "hello"
    assert [i["name"] for i in (await agent.handle(req("files", project=pid, path="")))[0]["data"]["items"]] == ["a.txt"]
    res = (await agent.handle(req("chats", project="0" * 16)))[0]
    assert (res["code"], res["message"]) == ("not_found", "нет такого проекта")


async def test_a_crash_in_an_op_is_error_and_the_next_request_works(agent, monkeypatch, caplog):
    monkeypatch.setattr(sessions, "history", lambda *a: (_ for _ in ()).throw(ValueError("битый JSONL")))
    await opened(agent)
    r = req("history", project=projects.pid("demo"), chat=CHAT, before=None, limit=10)
    with caplog.at_level(logging.INFO, logger="studio_agent"):
        res = (await agent.handle(r))[0]
    assert res == {"t": "res", "id": r["id"], "ok": False, "code": "error", "message": "ошибка моста"}
    assert "history" in caplog.text and "ValueError" in caplog.text and "битый" not in caplog.text
    assert (await agent.handle(req("projects")))[0]["ok"]


async def test_oversized_answer_is_refused_too_large(agent, monkeypatch):
    monkeypatch.setattr(projects, "list_projects", lambda root: [{"pid": "0" * 16, "name": "x" * (17 * 2**20)}])
    await opened(agent)
    r = req("projects")
    res = (await agent.handle(r))[0]
    assert res == {"t": "res", "id": r["id"], "ok": False, "code": "too_large",
                   "message": "ответ больше предела канала"}


async def test_unencodable_answer_is_error(agent, monkeypatch):
    monkeypatch.setattr(files, "read_text", lambda project, path: {"text": float("nan"), "version": "0" * 64})
    await opened(agent)
    res = (await agent.handle(req("read", project=projects.pid("demo"), path="a.txt")))[0]
    assert res["code"] == "error"


async def test_journal_has_no_secrets(home, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(sessions, "exists", lambda project, chat: False)
    monkeypatch.setattr(sessions, "last_model", lambda project, chat: None)
    monkeypatch.setattr(sessions, "mark_studio", lambda project, chat: None)
    root = tmp_path / "projects"
    (root / "секретный-проект").mkdir(parents=True)
    (root / "секретный-проект" / "пароли.txt").write_text("тайна", encoding="utf-8")
    PinStore(home).set_pin("2468")
    a = make(home, root, FakeFactory([delta("тайный ответ"), result_ok()]))
    pid = projects.pid("секретный-проект")
    with caplog.at_level(logging.DEBUG):
        await a.handle(req("open", pin="1111"))
        await opened(a, pin="2468")
        await a.handle(req("projects"))
        await a.handle(req("files", project=pid, path=""))
        await a.handle(req("read", project=pid, path="пароли.txt"))
        await a.handle(req("read", project=pid, path="нет/пароли.txt"))
        await a.handle(req("turn", project=pid, chat=CHAT, text="тайное задание", model=None, effort=None))
        await settle(a)
        await a.handle(req("тайное задание"))
    assert caplog.text
    for secret in ("2468", "1111", "секретный-проект", "пароли.txt", "тайное задание", "тайна", "тайный"):
        assert secret not in caplog.text
