"""Уточнения не заводят второго writer; управление передаётся между окнами явно."""
import asyncio

import pytest

from studio_agent.errors import Refused
from studio_agent.turns import Runner, _Turn
from studio_agent import store
from test_codex import CHAT, THREAD, FORK, asking, runner
from test_opencode import runner as opencode_runner, asking as opencode_asking
from test_turns import marked, project
from fake_sdk import result_ok

PID = "a1b2c3d4e5f60718"


async def test_codex_steers_the_existing_turn_and_reconnect_keeps_the_followup(runner):
    runner.start(runner.project, PID, CHAT, "Задание", None, None, owner="a" * 32)
    await asking(runner)
    t = runner._turns[CHAT]
    try:
        result = await runner.steer(PID, CHAT, "Уточнение", [], owner="a" * 32)
        assert result["mode"] == "steer"
        call = next(p for m, p in runner.fake.calls if m == "turn/steer")
        assert call == {"threadId": THREAD, "expectedTurnId": "turn1",
                        "input": [{"type": "text", "text": "Уточнение"}]}
        assert len([m for m, _ in runner.fake.calls if m == "turn/start"]) == 1
        _, snapshot = await runner.watch(PID, CHAT)
        assert snapshot[0]["e"]["followups"][0]["text"] == "Уточнение"
        assert snapshot[0]["e"]["owner"] == "a" * 32
    finally:
        await runner.stop(PID, CHAT)
        await t.task


async def test_claim_moves_control_without_stopping_the_task(home):
    sent = []
    r = Runner(sent.append, choices=store.Choices(home), cli_path=None)
    t = _Turn(home, PID, CHAT, "turn", owner="a" * 32)
    r._turns[CHAT] = t
    with pytest.raises(Refused) as error:
        r.check_owner(PID, CHAT, "b" * 32)
    assert error.value.code == "owned"
    r.claim(PID, CHAT, "b" * 32)
    assert not t.stopped and not t.closing
    r.check_owner(PID, CHAT, "b" * 32)
    assert sent[-1]["e"] == {"k": "ownership", "owner": "b" * 32}
    with pytest.raises(Refused):
        r.check_owner(PID, CHAT, "a" * 32)
    assert r._turn("wrong-project", CHAT) is None


async def test_claude_followup_keeps_the_client_until_both_results(home, project):
    class Client:
        def __init__(self, **kwargs):
            self.items = asyncio.Queue()
            self.prompts = []
            self.first = asyncio.Event()

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def query(self, value):
            self.prompts.append(value)
            self.first.set()

        async def receive_response(self):
            yield await self.items.get()

    r = Runner(lambda _: None, choices=store.Choices(home), cli_path=None, factory=Client)
    r.start(project, PID, CHAT, 'Задача', None, None, owner='a' * 32)
    t = r._turns[CHAT]
    while t.client is None:
        await asyncio.sleep(0)
    await t.client.first.wait()
    try:
        assert (await r.steer(PID, CHAT, 'Уточнение', [], owner='a' * 32))['mode'] == 'steer'
        assert t.client.prompts == ['Задача', 'Уточнение']
        t.client.items.put_nowait(result_ok())
        # Первый результат не завершает клиента, пока остаётся принятое уточнение.
        t.client.items.put_nowait(result_ok())
        await asyncio.wait_for(t.task, 2)
        assert not r.live()
    finally:
        if not t.task.done():
            t.task.cancel()
            await asyncio.gather(t.task, return_exceptions=True)


async def test_opencode_followup_runs_after_current_prompt_on_the_same_session(opencode_runner):
    r = opencode_runner
    r.start(r.project, r.pid, CHAT, 'Задание', None, None, owner='a' * 32)
    await opencode_asking(r)
    t = r._turns[CHAT]
    assert (await r.steer(r.pid, CHAT, 'Следом', [], owner='a' * 32))['mode'] == 'queue'
    assert len([1 for method, _ in r.fake.calls if method == 'session/prompt']) == 1
    t.client.rpc.response.set_result({'stopReason': 'end_turn'})
    await asyncio.wait_for(t.task, 2)
    calls = [p for method, p in r.fake.calls if method == 'session/prompt']
    assert len(calls) == 2 and calls[0]['sessionId'] == calls[1]['sessionId']
    assert calls[1]['prompt'] == [{'type': 'text', 'text': 'Следом'}]


async def test_stopping_opencode_returns_the_unexecuted_followup(opencode_runner):
    r = opencode_runner
    r.start(r.project, r.pid, CHAT, 'Задание', None, None, owner='a' * 32)
    await opencode_asking(r)
    t = r._turns[CHAT]
    result = await r.steer(r.pid, CHAT, 'Не потерять', [], owner='a' * 32)
    await r.stop(r.pid, CHAT)
    await t.task
    assert r.sent[-1]['e']['unsent'] == [{'id': result['id'], 'text': 'Не потерять'}]
    assert r.sent[-1]['e']['status'] == 'stopped'
    assert len([1 for method, _ in r.fake.calls if method == 'session/prompt']) == 1


async def test_external_writer_is_reported_and_probe_releases_its_own_writer(runner):
    runner.fake.exists = True
    other = object()
    runner.fake.writers[THREAD] = other
    result = await runner.takeover(runner.project, PID, THREAD, 'a' * 32)
    assert result == {'available': False, 'external': True}
    assert runner.fake.writers[THREAD] is other
    del runner.fake.writers[THREAD]
    result = await runner.takeover(runner.project, PID, THREAD, 'a' * 32)
    assert result['available']
    assert not runner.fake.writers
    assert not any(method == 'turn/start' for method, _ in runner.fake.calls)


async def test_external_writer_can_be_left_running_while_explicitly_forking_its_history(runner):
    runner.fake.exists = True
    other = object()
    runner.fake.writers[THREAD] = other
    result = await runner.takeover(runner.project, PID, THREAD, 'a' * 32, fork=True)
    assert result == {'available': True, 'forked': True}
    assert runner.aliases.target(runner.project, THREAD) == FORK
    assert runner.fake.writers == {THREAD: other}
    assert ('thread/fork', {'threadId': THREAD}) in runner.fake.calls
    assert not any(method in ('turn/start', 'turn/interrupt') for method, _ in runner.fake.calls)


async def test_rejected_codex_start_says_to_restore_the_draft(runner):
    runner.fake.exists = True
    runner.fake.writers[THREAD] = object()
    runner.start(runner.project, PID, THREAD, 'Не потерять', None, None, owner='a' * 32)
    await runner._turns[THREAD].task
    event = runner.sent[-1]['e']
    assert event['status'] == 'error' and event['not_started'] and event['code'] == 'writer_active'
    assert not any(method == 'turn/start' for method, _ in runner.fake.calls)


async def test_missing_turn_start_ack_is_not_proof_that_cli_rejected_input(runner, monkeypatch):
    from test_codex import FakeRpc
    request = FakeRpc.request

    async def lost_ack(self, method, params):
        if method == 'turn/start':
            raise TimeoutError()
        return await request(self, method, params)

    monkeypatch.setattr(FakeRpc, 'request', lost_ack)
    runner.start(runner.project, PID, CHAT, 'Не повторять автоматически', None, None, owner='a' * 32)
    await runner._turns[CHAT].task
    assert runner.sent[-1]['e']['status'] == 'error'
    assert 'not_started' not in runner.sent[-1]['e']
