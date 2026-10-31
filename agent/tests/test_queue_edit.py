import asyncio

import pytest

from fake_sdk import FakeFactory, result_ok
from studio_agent import sessions
from studio_agent.errors import Refused
from studio_agent.turns import Runner, _Turn
from test_turns import CHAT, PID, runner, until


async def test_queue_edit_keeps_attachments_and_serializes_versions(home, tmp_path):
    sent = []
    r = Runner(sent.append, choices=None, cli_path=None)
    t = _Turn(tmp_path, PID, CHAT, "turn", owner="owner", accepted=True)
    r._turns[CHAT] = t
    item = await r.steer(PID, CHAT, "first", [], owner="owner", mode="queue")
    result = await r.edit_queue(PID, CHAT, item["id"], 1, "corrected", owner="owner")
    assert t.queued[0][1] == "corrected" and result["items"][0]["version"] == 2
    with pytest.raises(Refused, match="conflict"):
        await r.edit_queue(PID, CHAT, item["id"], 1, "stale", owner="owner")
    with pytest.raises(Refused, match="owned"):
        await r.edit_queue(PID, CHAT, item["id"], 2, None, owner="other")
    await r.edit_queue(PID, CHAT, item["id"], 2, None, owner="owner")
    assert not t.queued and not t.followups
    with pytest.raises(Refused, match="gone"):
        await r.edit_queue(PID, CHAT, item["id"], 2, "late", owner="owner")


@pytest.mark.parametrize("finish", ["resume", "stop"])
async def test_claude_queue_runs_only_after_current_result(home, tmp_path, monkeypatch, finish):
    monkeypatch.setattr(sessions, "exists", lambda *_: False)
    monkeypatch.setattr(sessions, "mark_studio", lambda *_: None)
    sent, release = [], asyncio.Event()
    base = FakeFactory([])

    def factory(options):
        client = base(options)

        async def receive():
            if len(client.queries) == 1:
                await release.wait()
            yield result_ok()

        client.receive_response = receive
        return client

    r = runner(home, factory, sent)
    r.start(tmp_path, PID, CHAT, "first", None, None)
    await until(lambda: bool(r._turns[CHAT].accepted))
    client = base.clients[0]
    queued = await r.steer(PID, CHAT, "second", [], mode="queue")
    assert client.queries == ["first"]
    await r.pause_queue(PID, CHAT, True)
    release.set()
    await until(lambda: r._turns[CHAT].pending == 0)
    assert client.queries == ["first"] and not r._turns[CHAT].finishing
    await r.edit_queue(PID, CHAT, queued["id"], 1, "corrected")
    if finish == "resume":
        await r.pause_queue(PID, CHAT, False)
    else:
        await r.stop(PID, CHAT)
    await asyncio.gather(*(t.task for t in list(r._turns.values())))
    assert client.queries == (["first", "corrected"] if finish == "resume" else ["first"])
    assert any(frame["e"]["k"] == "followup_sent" for frame in sent) == (finish == "resume")
