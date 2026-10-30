"""Локальный Codex app-server по JSONL/stdio: без TCP и без записи stderr в журнал."""

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from . import VERSION, limits

RPC_SEC = 25


def find_cli() -> Path | None:
    """Настоящий exe из PATH, npm или расширения VS Code; cmd/ps1 не запускаются через shell."""
    exe = shutil.which("codex.exe")
    if exe:
        return Path(exe)
    roots = [Path(os.environ.get("APPDATA", "")) / "npm/node_modules/@openai",
             Path.home() / ".vscode/extensions"]
    for root, pattern in ((roots[0], "codex*/vendor/*/codex/codex.exe"),
                          (roots[0], "codex/node_modules/@openai/codex-*/vendor/*/codex/codex.exe"),
                          (roots[1], "openai.chatgpt-*/bin/windows-x86_64/codex.exe")):
        found = sorted(root.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
        if found:
            return found[0]
    return None


def version(path: Path) -> str:
    result = subprocess.run([str(path), "--version"], capture_output=True, stdin=subprocess.DEVNULL,
                            timeout=RPC_SEC, creationflags=subprocess.CREATE_NO_WINDOW)
    return result.stdout.decode("utf-8", "replace").strip()[:64] if result.returncode == 0 else "Codex"


class RpcError(Exception):
    """Тело ошибки может содержать пути и тексты: его нельзя писать в журнал."""

    def __init__(self, *, not_found=False):
        super().__init__()
        self.not_found = not_found


class Client:
    def __init__(self, path: Path, cwd: Path, *, spawn=asyncio.create_subprocess_exec):
        self.path, self.cwd, self.spawn = path, cwd, spawn
        self.process = None
        self.reader = None
        self.pending = {}
        self.inbox = asyncio.Queue()
        self.seq = 0

    async def connect(self):
        env = {k: v for k, v in os.environ.items() if k not in
               {"CODEX_THREAD_ID", "CODEX_INTERNAL_ORIGINATOR_OVERRIDE"}}
        self.process = await self.spawn(str(self.path), "app-server", "--listen", "stdio://", cwd=str(self.cwd),
                                        env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                        stderr=asyncio.subprocess.DEVNULL, limit=limits.FRAME_MAX,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
        self.reader = asyncio.create_task(self._read())
        try:
            await self.request("initialize", {"clientInfo": {"name": "studio_agent", "title": "Studio Agent",
                                                             "version": VERSION}})
            await self.send({"method": "initialized", "params": {}})
        except BaseException:
            await self.disconnect()
            raise

    async def send(self, frame):
        self.process.stdin.write((json.dumps(frame, ensure_ascii=True, allow_nan=False) + "\n").encode())
        await self.process.stdin.drain()

    async def request(self, method, params):
        self.seq += 1
        rid = self.seq
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        try:
            await self.send({"id": rid, "method": method, "params": params})
            return await asyncio.wait_for(future, RPC_SEC)
        finally:
            self.pending.pop(rid, None)

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
                        message = str((frame.get("error") or {}).get("message", "")).lower()
                        future.set_exception(RpcError(not_found="thread not found" in message
                                                       or "thread not loaded" in message
                                                       or "no rollout found" in message))
                    else:
                        future.set_result(frame.get("result", {}))
        except (ValueError, OSError, RpcError):
            pass
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(RpcError())
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
        if self.reader is not None:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
        self.process = self.reader = None
