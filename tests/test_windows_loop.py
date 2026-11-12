"""Отмена внутреннего чтения IOCP не лишает Runner пробуждений из потоков."""
import os
import subprocess
import sys

import pytest

from conftest import ROOT


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "win32", reason="Windows IOCP")
def test_aborted_self_read_keeps_thread_wakeups_and_runner_shutdown(tmp_path):
    case = tmp_path / "aborted_self_read.py"
    case.write_text('''import asyncio
import threading
from tools.pytest_support import new_event_loop


async def work():
    loop = asyncio.get_running_loop()
    errors = []
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    await asyncio.sleep(0)
    reader = loop._self_reading_future
    reader._ov.cancel()  # настоящий CancelIoEx; Future не отменён — завершится WinError 995
    while not reader.done():
        await asyncio.sleep(0)
    await asyncio.sleep(0)

    polling = threading.Event()
    select = loop._proactor.select

    def selecting(timeout=None):
        if timeout is None:
            polling.set()
        return select(timeout)

    loop._proactor.select = selecting
    received = loop.create_future()

    def send():
        assert polling.wait(2), "цикл не перешёл к ожиданию IOCP"
        loop.call_soon_threadsafe(received.set_result, "delivered")

    thread = threading.Thread(target=send)
    thread.start()
    assert await received == "delivered"
    await asyncio.to_thread(thread.join)
    assert not errors, errors


with asyncio.Runner(loop_factory=new_event_loop) as runner:
    runner.run(work())
print("thread delivered; runner closed")
''', encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(case)], cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
        capture_output=True, encoding="utf-8", errors="replace", timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "thread delivered; runner closed" in result.stdout
