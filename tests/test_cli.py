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
    assert made[0].disconnected


async def test_probe_failure_is_empty_and_still_disconnects(tmp_path, caplog):
    made = []

    def factory(options):
        made.append(Client(options, fail=True))
        return made[-1]

    assert await cli.probe(str(tmp_path), None, factory) == ([], [])
    assert made[0].disconnected
    assert "RuntimeError" in caplog.text and "claude.exe" not in caplog.text
