"""JSONL-транспорт Codex: рукопожатие, корреляция ответов, ошибки и EOF; процесс и stdio подставлены."""

import asyncio
import json
from pathlib import Path

import pytest

from studio_agent.codex_rpc import Client, RpcError


def test_find_cli_handles_npm_native_package_and_vscode_without_shell(tmp_path, monkeypatch):
    from studio_agent import codex_rpc
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(codex_rpc.shutil, "which", lambda name: None)
    assert codex_rpc.find_cli() is None
    vscode = tmp_path / ".vscode/extensions/openai.chatgpt-1/bin/windows-x86_64/codex.exe"
    vscode.parent.mkdir(parents=True)
    vscode.touch()
    assert codex_rpc.find_cli() == vscode
    npm = tmp_path / "npm/node_modules/@openai/codex/node_modules/@openai/codex-win32-x64/vendor/windows/codex/codex.exe"
    npm.parent.mkdir(parents=True)
    npm.touch()
    assert codex_rpc.find_cli() == npm
    monkeypatch.setattr(codex_rpc.shutil, "which", lambda name: str(vscode))
    assert codex_rpc.find_cli() == vscode


class Writer:
    def __init__(self, process):
        self.process = process

    def write(self, line):
        frame = json.loads(line)
        self.process.sent.append(frame)
        if frame.get("method") == "initialize":
            self.process.stdout.feed_data(json.dumps({"id": frame["id"], "result": {"userAgent": "test"}}).encode() + b"\n")
        elif frame.get("method") == "model/list":
            self.process.stdout.feed_data(json.dumps({"id": frame["id"], "result": {"data": []}}).encode() + b"\n")

    async def drain(self):
        pass

    def close(self):
        self.process.stdout.feed_eof()


class Process:
    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.stdin = Writer(self)
        self.sent = []

    async def wait(self):
        return 0


async def test_handshake_stdio_env_and_responses_keep_requests_separate(monkeypatch):
    process = Process()
    calls = []
    monkeypatch.setenv("CODEX_THREAD_ID", "parent-thread")
    monkeypatch.setenv("CODEX_HOME", "profile-path")

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    client = Client(Path("codex.exe"), Path("C:/project"), spawn=spawn)
    await client.connect()
    assert [f["method"] for f in process.sent] == ["initialize", "initialized"]
    args, kwargs = calls[0]
    assert args == ("codex.exe", "app-server", "--listen", "stdio://")
    assert "CODEX_THREAD_ID" not in kwargs["env"] and kwargs["env"]["CODEX_HOME"] == "profile-path"
    assert kwargs["stderr"] == asyncio.subprocess.DEVNULL
    assert await client.request("model/list", {}) == {"data": []}
    # Запрос сервера с тем же id не является ответом на запрос клиента.
    process.stdout.feed_data(b'{"id":1,"method":"approval","params":{}}\n')
    assert (await client.inbox.get())["method"] == "approval"
    await client.disconnect()
    assert client.reader is None and not client.pending


async def test_eof_and_rpc_error_fail_pending_requests_without_exposing_body():
    process = Process()

    async def spawn(*args, **kwargs):
        return process

    client = Client(Path("codex.exe"), Path("C:/project"), spawn=spawn)
    await client.connect()
    task = asyncio.create_task(client.request("thread/read", {}))
    await asyncio.sleep(0)
    rid = process.sent[-1]["id"]
    process.stdout.feed_data(json.dumps({"id": rid, "error": {"message": "private prompt and path"}}).encode() + b"\n")
    with pytest.raises(RpcError) as error:
        await task
    assert str(error.value) == "" and not error.value.not_found
    task = asyncio.create_task(client.request("thread/read", {}))
    await asyncio.sleep(0)
    rid = process.sent[-1]["id"]
    process.stdout.feed_data(json.dumps({"id": rid, "error": {"code": -32600,
        "message": "thread not loaded: 11111111-2222-4333-8444-555555555555"}}).encode() + b"\n")
    with pytest.raises(RpcError) as error:
        await task
    assert error.value.not_found
    task = asyncio.create_task(client.request("turn/start", {}))
    await asyncio.sleep(0)
    process.stdout.feed_eof()
    with pytest.raises(RpcError):
        await task
    assert await client.inbox.get() is None
    await client.disconnect()
