"""JSON-RPC ACP: настоящий wire format, входящие requests, безопасный EOF/ошибки."""

import asyncio
import json
from pathlib import Path

import pytest

from studio_agent import opencode_rpc


def test_find_cli_resolves_installed_native_exe_without_cmd(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(opencode_rpc.shutil, "which", lambda _: None)
    assert opencode_rpc.find_cli() is None
    exe = tmp_path / "npm/node_modules/@opencode/cli/bin/opencode.exe"
    exe.parent.mkdir(parents=True)
    exe.touch()
    assert opencode_rpc.find_cli() == exe


class Process:
    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.stdin = self
        self.sent = []

    def write(self, line):
        frame = json.loads(line)
        self.sent.append(frame)
        if frame.get("method") == "initialize":
            self.feed({"id": frame["id"], "result": {"protocolVersion": 1, "agentInfo": {"version": "test"}}})

    def feed(self, frame):
        self.stdout.feed_data(json.dumps(frame).encode() + b"\n")

    async def drain(self):
        pass

    def close(self):
        self.stdout.feed_eof()

    async def wait(self):
        return 0


async def test_acp_handshake_incoming_requests_and_unlimited_prompt():
    process, calls = Process(), []

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    client = opencode_rpc.Client(Path("opencode.exe"), Path("C:/project"), spawn=spawn)
    await client.connect()
    assert calls[0][0] == ("opencode.exe", "acp")
    assert calls[0][1]["stderr"] == asyncio.subprocess.DEVNULL
    assert process.sent[0]["jsonrpc"] == "2.0"
    assert process.sent[0]["params"]["clientCapabilities"]["elicitation"]["form"]
    prompt = asyncio.create_task(client.request("session/prompt", {}, timeout=None))
    await asyncio.sleep(0)
    rid = process.sent[-1]["id"]
    process.feed({"id": rid, "method": "session/request_permission", "params": {}})
    assert (await client.inbox.get())["method"] == "session/request_permission"
    assert not prompt.done()
    process.feed({"id": rid, "result": {"stopReason": "end_turn"}})
    assert await prompt == {"stopReason": "end_turn"}
    await client.disconnect()
    assert not client.pending and not client.methods and client.process is None


async def test_load_response_is_a_barrier_for_ordered_replay_updates():
    process = Process()

    async def spawn(*args, **kwargs):
        return process

    client = opencode_rpc.Client(Path("opencode.exe"), Path("C:/project"), spawn=spawn)
    await client.connect()
    try:
        load = asyncio.create_task(client.request("session/load", {"sessionId": "ses_test"}))
        await asyncio.sleep(0)
        rid = process.sent[-1]["id"]
        for text in ("first", "last"):
            process.feed({"method": "session/update", "params": {"sessionId": "ses_test", "update": {
                "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}}})
            await asyncio.sleep(0)
            assert not load.done()
        process.feed({"id": rid, "result": {}})
        await load
        assert [client.inbox.get_nowait()["params"]["update"]["content"]["text"] for _ in range(2)] == ["first", "last"]
        assert client.inbox.empty()
    finally:
        await client.disconnect()


async def test_errors_eof_and_timeout_remove_waiters_without_private_content():
    process = Process()

    async def spawn(*args, **kwargs):
        return process

    client = opencode_rpc.Client(Path("opencode.exe"), Path("C:/project"), spawn=spawn)
    await client.connect()
    task = asyncio.create_task(client.request("session/load", {}))
    await asyncio.sleep(0)
    process.feed({"id": process.sent[-1]["id"], "error": {"code": -32000,
        "message": "session not found: private-text-and-path", "data": "secret"}})
    with pytest.raises(opencode_rpc.RpcError) as error:
        await task
    assert error.value.not_found and error.value.method == "session/load" and error.value.code == -32000
    assert str(error.value) == "" and "private" not in repr(error.value)
    with pytest.raises(TimeoutError):
        await client.request("session/list", {}, timeout=0)
    assert not client.pending and not client.methods
    task = asyncio.create_task(client.request("session/prompt", {}, timeout=None))
    await asyncio.sleep(0)
    process.close()
    with pytest.raises(opencode_rpc.RpcError):
        await task
    assert await client.inbox.get() is None
    await client.disconnect()
