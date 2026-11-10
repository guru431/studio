"""Запуск SDK из GUI скрывает обе консоли, сохраняя штатный транспорт Claude."""

import subprocess

import anyio
import pytest
from claude_agent_sdk import ClaudeAgentOptions, CLIConnectionError
from claude_agent_sdk._internal.transport import subprocess_cli

from studio_agent import cli  # подключает адаптер также при чтении каталога моделей
from studio_agent.turns import options_for


def test_turn_keeps_cli_prompt_and_merged_subagent_limits_without_overwriting_settings(monkeypatch, tmp_path):
    import json

    config = tmp_path / "config"
    project = tmp_path / "project"
    config.mkdir()
    (project / ".claude").mkdir(parents=True)
    user = config / "settings.json"
    local = project / ".claude/settings.local.json"
    user.write_text(json.dumps({"env": {cli.SUBAGENT_LIMITS[0]: "4", cli.SUBAGENT_LIMITS[1]: "1",
                                        "PRIVATE_TOKEN": "never-copy"}, "hooks": {"PreToolUse": [{"hooks": []}]}}), encoding="utf-8")
    local.write_text(json.dumps({"env": {cli.SUBAGENT_LIMITS[0]: "2"}}), encoding="utf-8")
    before = {p: p.read_bytes() for p in (user, local)}
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv(cli.SUBAGENT_LIMITS[0], "9")
    options = options_for(project, tmp_path / "claude.exe", "chat", True, None, None, lambda *a: None)
    assert options.env == {**cli.ENV, cli.SUBAGENT_LIMITS[0]: "2", cli.SUBAGENT_LIMITS[1]: "1"}
    assert options.system_prompt["preset"] == "claude_code"
    assert "at most 2 subagents" in options.system_prompt["append"]
    assert "spawn depth is 1" in options.system_prompt["append"]
    command = subprocess_cli.SubprocessCLITransport(prompt="", options=options)._build_command()
    assert "--system-prompt" not in command
    assert command[command.index("--append-system-prompt") + 1] == options.system_prompt["append"]
    assert "--setting-sources=user,project,local" in command
    assert options.hooks["PreToolUse"][0].hooks
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize("value", ["", "0", "-1", "1.5", "4; ignored", 4, None])
def test_subagent_limit_configuration_rejects_invalid_values(monkeypatch, tmp_path, value):
    import json

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    for name in cli.SUBAGENT_LIMITS:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "settings.json").write_text(json.dumps({"env": {cli.SUBAGENT_LIMITS[0]: value}}), encoding="utf-8")
    assert cli.subagent_limits(tmp_path) == {}


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
