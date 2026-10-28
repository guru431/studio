import subprocess

from studio_agent import cli


def _ext(root, version, binary=True):
    d = root / f"anthropic.claude-code-{version}-win32-x64" / "resources" / "native-binary"
    d.mkdir(parents=True)
    if binary:
        (d / "claude.exe").write_bytes(b"")
    return d / "claude.exe"


def test_find_cli_takes_the_newest_version_with_the_binary(tmp_path):
    _ext(tmp_path, "2.1.99")
    newest = _ext(tmp_path, "2.1.312")
    _ext(tmp_path, "2.1.400", binary=False)
    (tmp_path / "anthropic.claude-code-2.2.0-darwin-arm64").mkdir()
    assert cli.find_cli(tmp_path) == newest


def test_find_cli_without_extensions_is_none(tmp_path):
    assert cli.find_cli(tmp_path) is None
    assert cli.find_cli(tmp_path / "нет") is None


def test_cli_version_is_the_first_word(monkeypatch, tmp_path):
    seen = {}

    def run(args, **kw):
        seen.update(args=args, **kw)
        return subprocess.CompletedProcess(args, 0, b"2.1.312 (Claude Code)\n", b"")

    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli.cli_version(tmp_path / "claude.exe") == "2.1.312"
    assert seen["args"] == [str(tmp_path / "claude.exe"), "--version"]
    assert seen["timeout"] == 30 and seen["creationflags"] == subprocess.CREATE_NO_WINDOW


def test_cli_version_falls_back_to_the_sdk(monkeypatch, tmp_path):
    assert cli.cli_version(None) == "встроенный в SDK"
    assert cli.cli_version(tmp_path / "нет.exe") == "встроенный в SDK"

    def slow(args, **kw):
        raise subprocess.TimeoutExpired(args, 30)

    monkeypatch.setattr(cli.subprocess, "run", slow)
    assert cli.cli_version(tmp_path / "claude.exe") == "встроенный в SDK"
    monkeypatch.setattr(cli.subprocess, "run", lambda a, **k: subprocess.CompletedProcess(a, 0, b"v" * 100, b""))
    assert cli.cli_version(tmp_path / "claude.exe") == "v" * 64


def test_models_from_server_info():
    info = {"models": [
        {"value": "default", "displayName": "По умолчанию", "supportedEffortLevels": ["xhigh"]},
        {"value": "opus", "displayName": "Opus 5.5", "supportedEffortLevels": ["low", "high"]},
        {"value": "sonnet[1m]", "displayName": "Sonnet 1M", "supportedEffortLevels": ["medium", "max"]},
        {"value": "bad id", "displayName": "x"}]}
    assert cli.models_from(info) == (
        [{"id": "opus", "label": "Opus 5.5"}, {"id": "sonnet[1m]", "label": "Sonnet 1M"}],
        ["low", "medium", "high", "max"])
    assert cli.models_from(None) == ([], [])
    assert cli.models_from({"models": "мусор"}) == ([], [])


def test_models_from_limits_count_and_label():
    info = {"models": [{"value": f"m{i}", "displayName": "Д" * 150} for i in range(60)]}
    models, efforts = cli.models_from(info)
    assert len(models) == 50 and len(models[0]["label"]) == 100 and efforts == []


class Client:
    def __init__(self, options=None, fail=False):
        self.options = options
        self.fail = fail
        self.disconnected = False

    async def connect(self):
        if self.fail:
            raise RuntimeError("нет claude.exe")

    async def get_server_info(self):
        return {"models": [{"value": "opus", "displayName": "Opus", "supportedEffortLevels": ["high"]}]}

    async def disconnect(self):
        self.disconnected = True


async def test_probe_reads_models_and_disconnects(tmp_path):
    made = []

    def factory(options):
        made.append(Client(options))
        return made[-1]

    assert await cli.probe(str(tmp_path), tmp_path / "claude.exe", factory) == (
        [{"id": "opus", "label": "Opus"}], ["high"])
    opts = made[0].options
    assert opts.cwd == str(tmp_path) and opts.cli_path == tmp_path / "claude.exe"
    assert opts.setting_sources == ["user", "project", "local"] and opts.stderr is not None
    assert opts.env == {"CLAUDE_AGENT_SDK_CLIENT_APP": "studio-agent/0.1.0"}
    assert made[0].disconnected


async def test_probe_failure_is_empty_and_still_disconnects(tmp_path, caplog):
    made = []

    def factory(options):
        made.append(Client(options, fail=True))
        return made[-1]

    assert await cli.probe(str(tmp_path), None, factory) == ([], [])
    assert made[0].disconnected
    assert "RuntimeError" in caplog.text and "claude.exe" not in caplog.text


def test_current_keeps_the_path_while_the_file_exists_then_finds_the_newest(tmp_path):
    old = _ext(tmp_path, "2.1.311")
    new = _ext(tmp_path, "2.1.312")
    find = lambda: cli.find_cli(tmp_path)  # noqa: E731
    assert cli.current(old, find) == old                       # прежний ещё на месте — не меняется
    old.unlink()                                               # VS Code удалил прежнюю папку
    assert cli.current(old, find) == new
    assert cli.current(None, find) == new                      # встроенный в SDK — ищется снова
    new.unlink()
    assert cli.current(new, find) is None                      # ничего нет — CLI SDK


def test_drop_session_env_removes_only_the_launching_session():
    env = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "claude-vscode", "CLAUDE_CODE_SESSION_ID": "s",
           "CLAUDE_CODE_CHILD_SESSION": "1", "CLAUDE_CODE_SESSION_ATTENDED": "1", "CLAUDE_CODE_SSE_PORT": "1",
           "CLAUDE_PID": "1", "CLAUDE_CODE_EXECPATH": "x", "CLAUDE_PLUGIN_DATA": "x",
           "CLAUDE_CODE_MESSAGING_SOCKET": "x", "CLAUDE_CODE_MESSAGING_TOKEN": "x", "VSCODE_PID": "1",
           "VSCODE_GIT_IPC_HANDLE": "x", "TERM_PROGRAM": "vscode",
           "ANTHROPIC_BASE_URL": "x", "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1", "CLAUDE_CONFIG_DIR": "x",
           "CLAUDE_EFFORT": "high", "TRACEPARENT": "00-x-y-01", "CLAUDE_CODE_INVOKED_SKILLS": "x",
           "CODEX_COMPANION_SESSION_ID": "s", "CODEX_COMPANION_TRANSCRIPT_PATH": "x", "ELECTRON_RUN_AS_NODE": "1",
           "GIT_ASKPASS": r"c:\Users\u\AppData\Local\Programs\Microsoft VS Code\resources\app\extensions\git\dist"
                          r"\askpass.sh",
           "GIT_EDITOR": r'"C:/Program Files/Microsoft VS Code/resources/app/extensions/git/dist/git-editor.sh"',
           "CLAUDE_CODE_EFFORT_LEVEL": "high", "PATH": "x"}
    assert cli.drop_session_env(env) == 22
    assert env == {"ANTHROPIC_BASE_URL": "x", "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1", "CLAUDE_CONFIG_DIR": "x",
                   "CLAUDE_CODE_EFFORT_LEVEL": "high", "PATH": "x"}   # настройка усилия владельца — остаётся
    other = {"TERM_PROGRAM": "WezTerm", "GIT_ASKPASS": r"C:\Tools\askpass.exe", "GIT_EDITOR": "notepad"}
    kept = dict(other)
    assert cli.drop_session_env(other) == 0 and other == kept
    insiders = {"GIT_EDITOR": r"C:\Users\u\AppData\Local\Programs\Microsoft VS Code Insiders\bin\code --wait"}
    assert cli.drop_session_env(insiders) == 1 and insiders == {}
