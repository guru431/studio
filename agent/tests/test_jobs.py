"""Job Object: закрытие описателя завершает CLI вместе с его детьми; настоящие процессы Python."""

import asyncio
import ctypes
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

from studio_agent import codex_rpc, jobs, opencode_rpc

SYNCHRONIZE = 0x00100000
PARENT = ("import subprocess, sys, time\n"
          "sys.stdin.readline()\n"  # ребёнок — только после назначения job, как MCP-сервер после старта CLI
          "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
          "print(child.pid, flush=True)\n"
          "time.sleep(60)\n")


def gone(pid, timeout_ms):
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    handle = k.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return True
    try:
        return k.WaitForSingleObject(handle, timeout_ms) == 0
    finally:
        k.CloseHandle(wintypes.HANDLE(handle))


def test_closing_the_job_ends_the_process_and_its_later_children():
    parent = subprocess.Popen([sys.executable, "-c", PARENT], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              creationflags=subprocess.CREATE_NO_WINDOW, text=True)
    child = None
    try:
        job = jobs.attach(parent.pid)
        assert job is not None
        parent.stdin.write("go\n")
        parent.stdin.flush()
        child = int(parent.stdout.readline())
        assert not gone(child, 0)
        job.close()
        job.close()  # повторное закрытие безвредно
        assert parent.wait(5) is not None and gone(child, 5000)
    finally:
        parent.kill()
        parent.wait()
        if child is not None and not gone(child, 0):
            subprocess.run(["taskkill", "/F", "/PID", str(child)], capture_output=True)


def test_without_a_real_process_there_is_no_job():
    assert jobs.attach(None) is None and jobs.attach("1") is None


class Stdin:
    def write(self, data):
        pass

    async def drain(self):
        pass

    def close(self):
        pass


class Process:
    pid = 4242

    def __init__(self):
        self.stdin, self.stdout = Stdin(), asyncio.StreamReader()
        self.stdout.feed_eof()

    async def wait(self):
        return 0


async def test_rpc_clients_put_the_cli_into_a_job_and_close_it_after_the_process(monkeypatch):
    for module, args in ((codex_rpc, ("codex.exe", "C:/project")), (opencode_rpc, ("opencode.exe", "C:/project"))):
        events = []

        class Job:
            def close(self):
                events.append("job closed")

        monkeypatch.setattr(jobs, "attach", lambda pid: events.append(("attach", pid)) or Job())
        monkeypatch.setattr(module.chrome if module is codex_rpc else module, "overrides", lambda cwd: [],
                            raising=False)

        async def spawn(*a, **kw):
            events.append("spawned")
            return Process()

        client = module.Client(Path(args[0]), Path(args[1]), spawn=spawn)
        try:
            await client.connect()
        except module.RpcError:
            pass  # EOF до ответа initialize: connect сам вызывает disconnect
        assert events == ["spawned", ("attach", 4242), "job closed"]
        assert client.process is None
