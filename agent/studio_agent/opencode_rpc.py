"""OpenCode ACP по stdio: только локальный процесс, без TCP и журналирования содержимого."""

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from . import VERSION, jobs, limits

RPC_SEC = 60


def find_cli() -> Path | None:
    """Настоящий exe из PATH или npm; обёртки cmd/ps1 через shell не исполняются."""
    if exe := shutil.which("opencode.exe"):
        return Path(exe)
    npm = Path(os.environ.get("APPDATA", "")) / "npm/node_modules"
    for pattern in ("@opencode/cli/bin/opencode.exe", "@opencode-ai/cli/bin/opencode*.exe",
                    "opencode-windows-*/bin/opencode.exe", "opencode-ai/node_modules/opencode-windows-*/bin/opencode.exe"):
        if found := sorted(npm.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True):
            return found[0]
    if os.name != "nt" and (exe := shutil.which("opencode")):
        return Path(exe)
    return None


def version(path: Path) -> str:
    result = subprocess.run([str(path), "--version"], capture_output=True, stdin=subprocess.DEVNULL,
                            timeout=RPC_SEC, creationflags=subprocess.CREATE_NO_WINDOW)
    return result.stdout.decode("utf-8", "replace").strip()[:64] if result.returncode == 0 else "OpenCode"


class RpcError(Exception):
    """Тело ошибки может содержать пути/тексты; в исключении только метод и код.

    `transport` — канал с ACP оборвался; без него — ответ живого процесса."""

    def __init__(self, *, method=None, code=None, not_found=False, transport=False):
        super().__init__()
        self.method, self.code, self.not_found = method, code if type(code) is int else None, not_found
        self.transport = transport


class Client:
    def __init__(self, path: Path, cwd: Path, *, spawn=asyncio.create_subprocess_exec):
        self.path, self.cwd, self.spawn = path, cwd, spawn
        self.process = self.reader = self.job = None
        self.pending, self.methods = {}, {}
        self.inbox = asyncio.Queue()
        self.seq = 0
        self.info = {}

    async def connect(self):
        self.process = await self.spawn(str(self.path), "acp", cwd=str(self.cwd),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=limits.FRAME_MAX, creationflags=subprocess.CREATE_NO_WINDOW)
        # MCP-серверы и шеллы CLI — в его job: disconnect не оставит их сиротами.
        self.job = jobs.attach(getattr(self.process, "pid", None))
        self.reader = asyncio.create_task(self._read())
        try:
            self.info = await self.request("initialize", {"protocolVersion": 1,
                "clientInfo": {"name": "studio-agent", "version": VERSION},
                "clientCapabilities": {"elicitation": {"form": True}}})
        except BaseException:
            await self.disconnect()
            raise

    async def send(self, frame):
        self.process.stdin.write((json.dumps({"jsonrpc": "2.0", **frame}, ensure_ascii=True,
                                             allow_nan=False) + "\n").encode())
        await self.process.stdin.drain()

    async def request(self, method, params, *, timeout=RPC_SEC):
        self.seq += 1
        rid = self.seq
        future = asyncio.get_running_loop().create_future()
        self.pending[rid], self.methods[rid] = future, method
        try:
            await self.send({"id": rid, "method": method, "params": params})
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(rid, None)
            self.methods.pop(rid, None)

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                frame = json.loads(line)
                if not isinstance(frame, dict):
                    raise RpcError()
                if "method" in frame:
                    self.inbox.put_nowait(frame)
                elif (future := self.pending.get(frame.get("id"))) is not None and not future.done():
                    if "error" in frame:
                        error = frame.get("error") or {}
                        message = str(error.get("message", "")).lower()
                        future.set_exception(RpcError(method=self.methods.get(frame.get("id")), code=error.get("code"),
                            not_found="not found" in message or "does not exist" in message))
                    else:
                        future.set_result(frame.get("result", {}))
        except (ValueError, OSError, RpcError, TypeError, AttributeError):
            pass
        finally:
            for rid, future in list(self.pending.items()):
                if not future.done():
                    future.set_exception(RpcError(method=self.methods.get(rid), transport=True))
            self.inbox.put_nowait(None)

    async def disconnect(self):
        if self.process is not None:
            if self.process.stdin is not None:
                self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
                await self.process.wait()
        if self.job is not None:
            self.job.close()  # оставшиеся потомки CLI завершаются вместе с job
            self.job = None
        if self.reader is not None:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
        self.process = self.reader = None
