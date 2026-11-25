"""Штатный клиент Claude SDK со скрытым запуском Windows-процессов транспорта."""

import os
import subprocess

import anyio
from claude_agent_sdk import ClaudeSDKClient as _SDKClient
from claude_agent_sdk._internal import sessions as sdk_sessions
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


class _WindowsSubprocess:
    """Скрытый Git при поиске сессий и worktree; общий subprocess не меняется."""

    def __getattr__(self, name):
        return getattr(subprocess, name)

    def run(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        return subprocess.run(*args, **kwargs)


class ClaudeSDKClient(_SDKClient):
    """`query(..., uuid=)` — ввод со своим uuid: CLI с `--replay-user-messages` повторяет его в потоке, когда ввод
    принят в работу, в том числе поглощённый идущим ответом без отдельного `ResultMessage`."""

    async def query(self, prompt, session_id: str = "default", *, uuid: str | None = None) -> None:
        if uuid is None:
            return await super().query(prompt, session_id)

        async def stamped():
            if isinstance(prompt, str):
                yield {"type": "user", "message": {"role": "user", "content": prompt}, "parent_tool_use_id": None,
                       "uuid": uuid}
                return
            async for message in prompt:
                yield {**message, "uuid": uuid}  # свой uuid сильнее любого в сообщении: по нему ждём повтор

        await super().query(stamped(), session_id)


if os.name == "nt":
    subprocess_cli.anyio = _WindowsAnyIO()
    sdk_sessions.subprocess = _WindowsSubprocess()
