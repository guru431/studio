"""Запуск SDK из GUI скрывает обе консоли, сохраняя штатный транспорт Claude."""

import subprocess

import anyio
import pytest
from claude_agent_sdk import ClaudeAgentOptions, CLIConnectionError
from claude_agent_sdk._internal.transport import subprocess_cli

from studio_agent import cli  # подключает адаптер также при чтении каталога моделей


@pytest.mark.parametrize("skip_version", [False, True])
async def test_sdk_hides_version_and_turn_without_changing_global_anyio(monkeypatch, tmp_path, skip_version):
    calls = []
    stopped = []

    class Output:
        async def receive(self):
            return b"2.1.312 (Claude Code)\n"

    class Version:
        stdout = Output()

        def terminate(self):
            stopped.append(True)

        async def wait(self):
            return 0

    async def spawn(command, **kwargs):
        calls.append((command, kwargs))
        if "-v" in command:
            return Version()
        raise OSError("fixture")

    monkeypatch.setattr(anyio, "open_process", spawn)
    monkeypatch.delenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", raising=False)
    if skip_version:
        monkeypatch.setenv("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK", "1")
    options = ClaudeAgentOptions(cli_path=tmp_path / "claude.exe", cwd=tmp_path,
                                 env={"STUDIO_TEST": "kept"}, stderr=cli._drop)
    transport = subprocess_cli.SubprocessCLITransport(prompt="", options=options)
    with pytest.raises(CLIConnectionError):
        await transport.connect()
    assert len(calls) == (1 if skip_version else 2)
    assert stopped == ([] if skip_version else [True])
    assert all(kw.get("creationflags", 0) & subprocess.CREATE_NO_WINDOW for _, kw in calls)
    command, kwargs = calls[-1]
    assert command[0] == str(options.cli_path)
    assert kwargs["stdin"] == kwargs["stdout"] == kwargs["stderr"] == subprocess.PIPE
    assert kwargs["cwd"] == str(tmp_path) and kwargs["env"]["STUDIO_TEST"] == "kept"
    assert kwargs["env"]["CLAUDE_CODE_ENTRYPOINT"] == "sdk-py"
    # Посторонние процессы AnyIO не получают флаг транспорта Claude.
    assert anyio.open_process is spawn
    with pytest.raises(OSError):
        await anyio.open_process(["unrelated.exe"])
    assert "creationflags" not in calls[-1][1]
