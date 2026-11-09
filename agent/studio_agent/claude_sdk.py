"""Штатный клиент Claude SDK со скрытым запуском Windows-процессов транспорта."""

import os
import subprocess

import anyio
from claude_agent_sdk import ClaudeSDKClient
from claude_agent_sdk._internal.transport import subprocess_cli


class _WindowsAnyIO:
    """Локальный адаптер модуля транспорта; сам `anyio.open_process` не меняется.

    SDK 0.2.174 запускает через него и `claude -v`, и основной процесс. Остальные
    операции остаются штатными, включая отмену, потоки, окружение и очистку CLI.
    """

    def __getattr__(self, name):
        return getattr(anyio, name)

    async def open_process(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        return await anyio.open_process(*args, **kwargs)


if os.name == "nt":
    subprocess_cli.anyio = _WindowsAnyIO()
