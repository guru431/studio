"""Ожидание карточки учитывает занятый файловый поток и раннее завершение хода."""
import asyncio
import threading

import pytest

from studio_agent import store
from test_codex import CHAT, runner as codex_runner
from test_opencode import runner as opencode_runner
from turn_events import asking


@pytest.mark.parametrize("backend", ["codex_runner", "opencode_runner"])
async def test_asking_waits_for_file_write_and_accepts_an_existing_card(request, monkeypatch, backend):
    runner = request.getfixturevalue(backend)
    entered, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    write = store.write_atomic

    def held_write(*args):
        loop.call_soon_threadsafe(entered.set)
        release.wait()
        return write(*args)

    monkeypatch.setattr(store, "write_atomic", held_write)
    runner.start(runner.project, "project", CHAT, "test", None, None)
    turn = runner._turns[CHAT]
    previous = runner.emit
    waiting = asyncio.create_task(asking(runner))
    try:
        await entered.wait()
        assert not waiting.done() and not turn.asks
        release.set()
        rid = await waiting
        assert await asking(runner) == rid
        assert runner.emit == previous
        runner.answer("project", CHAT, rid, "allow", None, None)
        await turn.task
        assert runner.sent[-1]["e"]["status"] == "done"
    finally:
        release.set()
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        if not turn.task.done():
            await runner.stop("project", CHAT)
        await turn.task


@pytest.mark.parametrize("backend", ["codex_runner", "opencode_runner"])
async def test_asking_reports_early_completion_and_restores_emitter(request, monkeypatch, backend):
    runner = request.getfixturevalue(backend)
    cli = "find_codex" if backend == "codex_runner" else "find_opencode"
    monkeypatch.setattr(runner, cli, lambda: None)
    runner.start(runner.project, "project", CHAT, "test", None, None)
    turn = runner._turns[CHAT]
    previous = runner.emit
    try:
        with pytest.raises(AssertionError, match="Ход завершился без карточки"):
            await asking(runner)
        assert turn.task.done() and runner.emit == previous
        assert runner.sent[-1]["e"]["status"] == "error"
    finally:
        if not turn.task.done():
            await runner.stop("project", CHAT)
        await turn.task
