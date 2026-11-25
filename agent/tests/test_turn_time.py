"""Время хода (протокол §26, мост 0.48.0): `done` несёт `turn_ms` и `session_ms`, `state` — сохранённые числа."""
import asyncio
import contextlib
import threading
from unittest.mock import AsyncMock

from claude_agent_sdk import PermissionResultDeny

from fake_sdk import FakeFactory, result_ok
from studio_agent import projects, store
from studio_agent.turns import Runner, _Ask, _Turn
from test_agent import agent, opened, req, root  # noqa: F401 — фикстуры
from test_turns import CHAT, PID, marked, project, settle  # noqa: F401 — фикстуры


async def test_done_carries_turn_and_session_time(home, project):
    sent = []
    ticks = iter([100.0, 101.5, 200.0, 200.25])                                      # начало и конец двух ходов
    r = Runner(sent.append, choices=store.Choices(home), cli_path=None, factory=FakeFactory([result_ok()] * 2),
               monotonic=lambda: next(ticks))
    r.start(project, PID, CHAT, "Раз", None, None)
    await settle(r)
    assert sent[-1]["e"] == {"k": "done", "status": "done", "turn_ms": 1500, "session_ms": 1500}
    r.start(project, PID, CHAT, "Два", None, None)
    await settle(r)
    assert (sent[-1]["e"]["turn_ms"], sent[-1]["e"]["session_ms"]) == (250, 1750)
    assert {k: v for k, v in store.Choices(home).details(CHAT).items() if k.endswith("_ms")} == {
        "turn_ms": 250, "session_ms": 1750}


async def test_turn_that_never_reached_the_cli_has_no_time(home, project):
    sent = []
    r = Runner(sent.append, choices=store.Choices(home), cli_path=None, factory=FakeFactory([result_ok()]),
               monotonic=lambda: 5.0)
    r.start(project, PID, CHAT, "Раз", None, None)
    await r.stop(PID, CHAT)                                                          # до connect
    await settle(r)
    assert sent[-1]["e"]["status"] == "stopped" and "turn_ms" not in sent[-1]["e"]
    assert "session_ms" not in store.Choices(home).details(CHAT)


async def test_cancel_while_saving_the_time_still_denies_the_cards(home, project):
    """Отмена в конце хода, пока пишется время сессии, — карточки уже получили отказ, ход не висит на них."""
    entered, release = threading.Event(), threading.Event()

    class SlowChoices(store.Choices):
        def add_turn(self, chat, turn_ms):
            entered.set()
            release.wait(5)
            return turn_ms

    sent = []
    r = Runner(sent.append, choices=SlowChoices(home), cli_path=None, monotonic=lambda: 1.0)
    t = _Turn(project, PID, CHAT, "turn", accepted=True)
    r._turns[CHAT] = t
    ask = _Ask({"k": "ask"}, asyncio.get_running_loop().create_future(), {}, [])
    t.asks["a" * 16] = ask
    finish = asyncio.create_task(r._finish(t, False, None))
    try:
        while not entered.is_set():
            await asyncio.sleep(0.001)
        finish.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await finish
        assert ask.future.done() and isinstance(ask.future.result(), PermissionResultDeny)
        assert t.asks == {} and sent[-1]["e"]["k"] == "done" and CHAT not in r._turns
    finally:
        release.set()


async def test_state_adds_the_saved_time(agent, monkeypatch):
    monkeypatch.setattr(agent.runner, "state", AsyncMock(side_effect=lambda *a, **k: {"settings": {}, "context": None}))
    await opened(agent)
    frame = req("state", project=projects.pid("demo"), chat=CHAT, model=None, effort=None, access=None)
    data = (await agent.handle(frame))[0]["data"]
    assert "turn_ms" not in data and "session_ms" not in data
    agent.runner.choices.add_turn(CHAT, 700)
    data = (await agent.handle({**frame, "id": "1" * 16}))[0]["data"]
    assert data["turn_ms"] == data["session_ms"] == 700
    agent.runner.choices.update(CHAT, turn_ms=-1, session_ms="x")                    # порча — не отдаётся
    data = (await agent.handle({**frame, "id": "2" * 16}))[0]["data"]
    assert "turn_ms" not in data and "session_ms" not in data
