"""Ожидание фоновых задач отдаёт цикл callback-ам, даже если gather завершён сразу."""

import asyncio
from types import SimpleNamespace

import pytest

import bridge
import push
from fake_bridge import settle as bridge_settle
from test_push import settle as push_settle


@pytest.mark.parametrize("settle,tasks", [(bridge_settle, bridge.TASKS), (push_settle, push.TASKS)],
                         ids=["bridge", "push"])
def test_settle_yields_to_done_callback_and_waits_for_followup(monkeypatch, settle, tasks):
    real_gather = asyncio.gather
    seen, work = [], []
    sentinel = None

    def checked_gather(*awaitables, **kwargs):
        if sentinel in awaitables:
            seen.append(sentinel)
            assert len(seen) == 1, "settle spins on a done task without running its cleanup callback"
        return real_gather(*awaitables, **kwargs)

    async def run(wait):
        nonlocal sentinel

        async def finish():
            await asyncio.sleep(0)
            work.append("followup finished")

        def cleanup(task):
            tasks.discard(task)
            followup = asyncio.create_task(finish())
            tasks.add(followup)
            followup.add_done_callback(tasks.discard)

        sentinel = asyncio.get_running_loop().create_future()
        sentinel.set_result(None)
        tasks.add(sentinel)
        sentinel.add_done_callback(cleanup)  # queued, but has not run yet
        await wait()
        assert not tasks and work == ["followup finished"]

    monkeypatch.setattr(asyncio, "gather", checked_gather)
    portal = SimpleNamespace(call=lambda wait: asyncio.run(run(wait)))
    try:
        settle(SimpleNamespace(c=SimpleNamespace(portal=portal)))
    finally:
        tasks.clear()
