"""JSONL-транспорт Codex: рукопожатие, корреляция ответов, ошибки и EOF; процесс и stdio подставлены."""

import asyncio
import json
from pathlib import Path

import pytest

from studio_agent import codex_rpc, limits
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
        self.stdout = asyncio.StreamReader(limit=limits.FRAME_MAX)  # как у spawn в `Client.connect`
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
    assert args[:4] == ("codex.exe", "app-server", "--listen", "stdio://")
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
    assert error.value.method == "thread/read" and error.value.code == -32600
    task = asyncio.create_task(client.request("thread/resume", {}))
    await asyncio.sleep(0)
    rid = process.sent[-1]["id"]
    process.stdout.feed_data(json.dumps({"id": rid, "error": {"code": -32600,
        "message": "thread private-path-and-text already has an active writer"}}).encode() + b"\n")
    with pytest.raises(RpcError) as error:
        await task
    assert error.value.writer_active and error.value.method == "thread/resume" and str(error.value) == ""
    assert "private" not in repr(error.value) and not error.value.transport
    task = asyncio.create_task(client.request("turn/start", {}))
    await asyncio.sleep(0)
    process.stdout.feed_eof()
    with pytest.raises(RpcError) as error:
        await task
    assert error.value.transport  # обрыв канала — не ответ сервера: общий процесс закрывается только тогда
    assert await client.inbox.get() is None
    await client.disconnect()
    assert not client.methods


async def _connected():
    process = Process()

    async def spawn(*args, **kwargs):
        return process

    client = Client(Path("codex.exe"), Path("C:/project"), spawn=spawn)
    await client.connect()
    return client, process


async def _asked(client, process, method):
    task = asyncio.create_task(client.request(method, {}))
    await asyncio.sleep(0)
    return task, process.sent[-1]["id"]


async def test_history_line_longer_than_link_frame_is_read(monkeypatch):
    """`thread/read` всей истории — одна строка больше `limits.FRAME_MAX`: читается, канал живёт."""
    monkeypatch.setattr(limits, "FRAME_MAX", limits.MIB)
    client, process = await _connected()
    task, rid = await _asked(client, process, "thread/read")
    image = "data:image/png;base64," + "A" * (limits.FRAME_MAX + limits.MIB)
    process.stdout.feed_data(json.dumps({"id": rid, "result": {"thread": {"image": image}}}).encode() + b"\n")
    assert len((await task)["thread"]["image"]) == len(image)
    assert await client.request("model/list", {}) == {"data": []}
    await client.disconnect()


@pytest.mark.parametrize("order", ["head", "tail"])
async def test_line_over_limit_fails_only_its_request(monkeypatch, order):
    """Длиннее `codex_rpc.LINE_MAX` — `too_large` ожидающему (id в начале или в конце строки);
    прочие запросы и уведомления идут дальше, `transport` нет."""
    monkeypatch.setattr(codex_rpc, "LINE_MAX", 4096)
    monkeypatch.setattr(codex_rpc, "CHUNK", 1000)  # строка приходит кусками, перевод — внутри куска
    client, process = await _connected()
    big, rid = await _asked(client, process, "thread/read")
    small, other = await _asked(client, process, "thread/list")
    body = '"result":{"turns":"' + "x" * 10_000 + '"}'
    line = f'{{"id":{rid},{body}}}' if order == "head" else f'{{{body},"id":{rid}}}'
    process.stdout.feed_data(line.encode() + b"\n" + json.dumps({"id": other, "result": {"data": []}}).encode()
                             + b'\n{"method":"thread/started","params":{}}\n')
    with pytest.raises(RpcError) as error:
        await big
    assert error.value.too_large and not error.value.transport and error.value.method == "thread/read"
    assert await small == {"data": []}
    assert (await client.inbox.get())["method"] == "thread/started"
    assert await client.request("model/list", {}) == {"data": []}
    await client.disconnect()


async def test_lines_split_chunks_and_keep_last_line_without_newline(monkeypatch):
    monkeypatch.setattr(codex_rpc, "CHUNK", 3)
    stream = asyncio.StreamReader()
    stream.feed_data(b'{"a":1}\n\n{"b":2}')
    stream.feed_eof()
    assert [bytes(line) async for line in codex_rpc.lines(stream)] == [b'{"a":1}', b"", b'{"b":2}']
