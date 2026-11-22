"""Наложение настроек DSH человека на профиль `acp`: подставной Node (разбор YAML — словарём текстов фикстур)."""

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from studio_agent import deepseek_settings as ds
from studio_agent.deepseek_rpc import Install

FIXTURES = Path(__file__).parent / "fixtures" / "deepseek"
WEB_TEXT = (FIXTURES / "web_patch.yml").read_text("utf-8")
CREDENTIALS_TEXT = (FIXTURES / "credentials.example.yml").read_text("utf-8")
INSTALL = Install("npm", ("node.exe", "C:/dsh/lib/bin.js"), {}, "web", Path("C:/dsh"))
PRESETS = {"read-only": {"sandbox": "read-only", "approval": "ask"},
           "workspace-write": {"sandbox": "workspace-write", "approval": "ask"},
           "danger-full-access": {"sandbox": "danger-full-access", "approval": "never"}}

# web_patch.yml и acp_patch.yml так, как их отдаёт js-yaml (проверено на DSH 0.2.2-rc.5)
PROVIDERS = {"srvllm": {"displayName": "srvllm", "apiKeyEnv": "SRVLLM_API_KEY", "api": "openai-completions",
                        "baseURL": "https://llm.example.com/v1",
                        "compat": {"supportsDeveloperRole": False, "maxTokensField": "max_tokens"},
                        "models": [{"id": "stub-tools", "name": "Stub tools", "contextWindow": 131072,
                                    "maxTokens": 8192, "reasoningEfforts": {"off": None, "high": "high"}}]}}
SERVERS = {"probe": {"transport": "stdio", "command": "python", "args": ["<HOME>\\mcp_echo.py"], "enabled": True}}
WEB = [{"id": "llm-pi-ai", "config": {"providers": PROVIDERS}},
       {"id": "agent-default-model", "config": {"provider": "srvllm", "model": "stub-tools", "reasoningEffort": "high"}},
       {"id": "mcp-manager", "config": {"servers": SERVERS}},
       {"id": "locale", "config": {"locale": "ru"}},
       {"id": "permission", "config": {"defaultPreset": "workspace-write"}}]
ACP = [WEB[0], WEB[1], {"id": "acp", "config": {"provider": "srvllm", "model": "stub-tools"}},
       {"insert": [{"id": "mcp-manager", "name": "@deepseek-ai/dsh-mcp-manager", "inject": ["credentials"],
                    "config": {"servers": SERVERS}}]}]
PERMISSION = {"id": "permission", "config": {"presets": PRESETS, "defaultPreset": "workspace-write"}}
CREDENTIALS = {"version": 1, "refs": {"SRVLLM_API_KEY": "sk-test", "DEEPSEEK_API_KEY": "sk-test"}}


def node(parsed: dict, calls: list | None = None, presets=PRESETS):
    """Подставной помощник Node: известный текст — его разбор, пустой — null, прочее — ошибка разбора."""
    def run(command, *, input, **kwargs):
        if calls is not None:
            calls.append((command, input, kwargs))
        texts = json.loads(input)["texts"]
        docs = [{"ok": copy.deepcopy(parsed[t])} if t in parsed else {"ok": None} if not t else {"error": 1}
                for t in texts]
        return subprocess.CompletedProcess(command, 0, json.dumps({"docs": docs, "presets": presets}).encode(), b"")
    return run


def dsh(tmp_path, web=WEB_TEXT, *, acp=None, credentials=CREDENTIALS_TEXT, profile="web") -> Path:
    home = tmp_path / "dsh"
    (home / "profiles" / profile).mkdir(parents=True)
    (home / "profiles" / profile / "cordis.patch.yml").write_bytes(web.encode())  # LF: ключ словаря разборов
    if acp is not None:
        (home / "profiles/acp").mkdir(parents=True)
        (home / "profiles/acp/cordis.patch.yml").write_bytes(acp.encode())
    if credentials is not None:
        (home / ".credentials.yaml").write_bytes(credentials.encode())
    return home


def parsed(**extra) -> dict:
    return {WEB_TEXT: WEB, CREDENTIALS_TEXT: CREDENTIALS, **extra}


def rows(path: Path) -> list:
    return json.loads(path.read_text("ascii"))


def test_newest_profile_is_the_source(tmp_path):
    assert ds.source(tmp_path) is None
    for name, stamp in (("web", 1_700_000_000), ("desktop", 1_700_000_100)):
        (tmp_path / "profiles" / name).mkdir(parents=True)
        (tmp_path / "profiles" / name / "cordis.patch.yml").write_text("[]\n", "utf-8")
        os.utime(tmp_path / "profiles" / name / "cordis.patch.yml", (stamp, stamp))
    assert ds.source(tmp_path) == "desktop"
    os.utime(tmp_path / "profiles/web/cordis.patch.yml", (1_700_000_200, 1_700_000_200))
    assert ds.source(tmp_path) == "web"


def test_dsh_home_is_variable_or_platform_default(tmp_path, monkeypatch):
    assert ds.dsh_home({"DSH_HOME": str(tmp_path / "x")}) == tmp_path / "x"
    monkeypatch.setattr(ds.host, "MACOS", False)
    monkeypatch.setattr(ds.host, "LINUX", False)
    assert ds.dsh_home({"USERPROFILE": str(tmp_path), "DSH_HOME": " "}) == tmp_path / ".dsh"
    monkeypatch.setattr(ds.host, "LINUX", True)
    assert ds.dsh_home({"HOME": str(tmp_path / "u")}) == tmp_path / "u" / ".dsh"


def test_known_rows_applied_unknown_skipped_by_name_only(tmp_path):
    home, calls = dsh(tmp_path), []
    path, summary = ds.build(home, {}, tmp_path / "bridge", INSTALL, run=node(parsed(), calls))
    assert summary["profile"] == "web"
    assert summary["applied"] == ["llm-pi-ai", "agent-default-model", "mcp-manager", "permission"]
    assert summary["skipped"] == [{"name": "locale", "reason": "unknown"}]
    assert rows(path) == [*ACP, PERMISSION]  # перевод — как acp_patch.yml пробы, плюс presets состава acp
    text = json.dumps(summary)
    for secret in ("baseURL", "llm.example.com", "sk-test", "SRVLLM_API_KEY", "<HOME>", "mcp_echo", str(tmp_path)):
        assert secret not in text
    # YAML уходит помощнику текстом в stdin; в argv — только Node, помощник и вход CLI установки
    (command, job, options), = calls
    assert command == ["node.exe", "-e", ds.HELPER, "C:/dsh/lib/bin.js"]
    assert str(home) not in job.decode("ascii") and options["timeout"] == ds.PARSE_SEC


def test_rows_defined_in_users_acp_profile_are_not_overridden(tmp_path):
    acp = "- id: acp\n- insert:\n    - id: mcp-manager\n"
    own = [{"id": "acp", "config": {"provider": "deepseek-official", "model": "x"}},
           {"insert": [{"id": "mcp-manager", "name": "@deepseek-ai/dsh-mcp-manager", "config": {}}]}]
    home = dsh(tmp_path, acp=acp)
    path, summary = ds.build(home, {}, tmp_path / "bridge", INSTALL, run=node(parsed(**{acp: own})))
    assert {"name": "agent-default-model", "reason": "acp_own"} in summary["skipped"]
    assert {"name": "mcp-manager", "reason": "acp_own"} in summary["skipped"]
    written = rows(path)
    assert [row.get("id") for row in written] == ["llm-pi-ai", "permission"]
    assert "insert" not in json.dumps(written)


def test_rows_of_the_shared_patch_are_not_overridden_and_base_urls_noted(tmp_path):
    """Minor-4: общий `$DSH_HOME/cordis.patch.yml` в DSH сильнее патча профиля — `--patch` его строки не перекрывает.
    Явный `baseURL` строки `llm-deepseek` — в `notes` для баланса (Minor-3), не в сводку студии."""
    shared = "- id: agent-default-model\n- id: llm-deepseek\n"
    shared_doc = [{"id": "agent-default-model", "config": {"provider": "deepseek-official", "model": "x"}},
                  {"id": "llm-deepseek", "config": {"baseURL": "https://proxy.example/anthropic"}}]
    home = dsh(tmp_path)
    (home / "cordis.patch.yml").write_bytes(shared.encode())
    notes = {}
    path, summary = ds.build(home, {}, tmp_path / "bridge", INSTALL, run=node(parsed(**{shared: shared_doc})),
                             notes=notes)
    assert {"name": "agent-default-model", "reason": "acp_own"} in summary["skipped"]
    assert "acp" not in [row.get("id") for row in rows(path)]
    assert notes == {"base_urls": ["https://proxy.example/anthropic"]} and "proxy" not in json.dumps(summary)
    (home / "cordis.patch.yml").write_bytes(b"- [\n")
    _, summary = ds.build(home, {}, tmp_path / "bridge", INSTALL, run=node(parsed()))
    assert summary["skipped"] == [{"name": "cordis.patch.yml", "reason": "unreadable"}]


def test_provider_key_from_missing_env_var_is_skipped(tmp_path):
    home = dsh(tmp_path, credentials=None)
    path, summary = ds.build(home, {"PATH": "x"}, tmp_path / "bridge", INSTALL, run=node(parsed()))
    assert {"name": "llm-pi-ai:srvllm", "reason": "env_key"} in summary["skipped"]
    assert {"name": "agent-default-model", "reason": "env_key"} in summary["skipped"]
    assert "llm-pi-ai" not in summary["applied"] and "SRVLLM" not in json.dumps(summary)
    assert [row.get("id") for row in rows(path)] == [None, "permission"]  # MCP и пресет без провайдера остаются
    # ключ у процесса моста или в $DSH_HOME/.env — провайдер применяется
    for environ in ({"SRVLLM_API_KEY": "sk-test"}, {}):
        if not environ:
            (home / ".env").write_text("# ключи\nexport SRVLLM_API_KEY='sk-test'\n", "utf-8")
        _, summary = ds.build(home, environ, tmp_path / "bridge", INSTALL, run=node(parsed()))
        assert summary["applied"][:2] == ["llm-pi-ai", "agent-default-model"]


def test_unreadable_profile_yields_no_overlay_and_reason(tmp_path):
    home = dsh(tmp_path, "- id: [broken\n")
    assert ds.build(home, {}, tmp_path / "bridge", INSTALL, run=node(parsed())) == (
        None, {"profile": "web", "applied": [], "skipped": [{"name": "web", "reason": "unreadable"}]})
    # своего Node для скриптов у Python-пакета нет: разобрать нечем — то же «unreadable», без процесса
    home = dsh(tmp_path / "py")
    python = Install("python", ("runtime.exe",), {}, "web")
    assert ds.build(home, {}, tmp_path / "bridge", python, run=None)[0] is None
    assert not (tmp_path / "bridge").exists()
    # пробному процессу хранилища во временном каталоге — при любом исходе разбора (C1)
    acp_big = dsh(tmp_path / "big", acp="#" * (ds.FILE_MAX + 1))
    for where, install in ((home, python), (acp_big, INSTALL), (dsh(tmp_path / "broken", "- [\n"), INSTALL)):
        path, summary = ds.build(where, {}, tmp_path / "bridge", install, run=node(parsed()), scratch=tmp_path / "s")
        assert summary["applied"] == [] and summary["skipped"][0]["reason"] == "unreadable"
        assert rows(path) == [{"id": "session-persistence-jsonl", "config": {"root": str(tmp_path / "s" / "sessions")}},
                              {"id": "storage-json", "config": {"root": str(tmp_path / "s" / "storages")}}]


def test_js_rows_explicit_access_and_scratch(tmp_path):
    text = WEB_TEXT + "- id: llm-deepseek\n  disabled: !!js x\n"
    js = [*WEB, {"id": "llm-deepseek", "disabled": {ds.JS: 1}}]
    home = dsh(tmp_path, text)
    path, summary = ds.build(home, {}, tmp_path / "bridge", INSTALL, run=node(parsed(**{text: js})),
                             access="read-only")
    assert {"name": "llm-deepseek", "reason": "unreadable"} in summary["skipped"]
    assert "permission" in summary["applied"]
    assert "permission" not in [row.get("id") for row in rows(path)]  # явные права чата сильнее defaultPreset
    scratch = tmp_path / "scratch"
    path, probe = ds.build(home, {}, tmp_path / "bridge", INSTALL, run=node(parsed(**{text: js})), scratch=scratch)
    assert probe == summary
    written = rows(path)
    assert "insert" not in json.dumps(written)  # пробному процессу MCP не нужен
    assert written[-2:] == [{"id": "session-persistence-jsonl", "config": {"root": str(scratch / "sessions")}},
                            {"id": "storage-json", "config": {"root": str(scratch / "storages")}}]


def test_console_only_user_gets_no_overlay(tmp_path):
    assert ds.build(tmp_path, {}, tmp_path / "bridge", INSTALL, run=None) == (
        None, {"profile": None, "applied": [], "skipped": []})


def test_overlay_written_privately_and_swept(tmp_path):
    bridge = tmp_path / "bridge"
    path, _ = ds.build(dsh(tmp_path), {}, bridge, INSTALL, run=node(parsed()))
    assert path.parent == bridge / "deepseek" and re.fullmatch(r"overlay-[0-9a-f]{16}\.json", path.name)
    (bridge / "deepseek" / "choices.json").write_text("{}", "utf-8")
    assert ds.sweep(bridge) == 1
    assert not path.exists() and (bridge / "deepseek" / "choices.json").exists()
    assert ds.sweep(tmp_path / "nowhere") == 0


def test_dsh_files_are_not_modified(tmp_path):
    home = dsh(tmp_path, acp="[]\n")
    (home / ".env").write_text("A=1\n", "utf-8")

    def snapshot():
        return {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
                for p in sorted(home.rglob("*")) if p.is_file()}

    before = snapshot()
    ds.build(home, {}, tmp_path / "bridge", INSTALL, run=node(parsed(**{"[]\n": []})))
    ds.build(home, {}, tmp_path / "bridge", INSTALL, run=node(parsed(**{"[]\n": []})), scratch=tmp_path / "s")
    assert snapshot() == before


@pytest.mark.integration
def test_real_dsh_helper_reads_yaml_and_presets(tmp_path):
    """Настоящий js-yaml установки: `DSH_TEST_PACKAGE` — каталог пакета `@deepseek-ai/dsh` (npm)."""
    package = os.environ.get("DSH_TEST_PACKAGE")
    if not package or not shutil.which("node"):
        pytest.skip("DSH_TEST_PACKAGE не задан")
    install = Install("npm", (shutil.which("node"), str(Path(package) / "lib/bin.js")), {}, "web", Path(package))
    home = dsh(tmp_path, WEB_TEXT + "- id: hmr\n  disabled: !!js \"process.platform === 'x'\"\n")
    path, summary = ds.build(home, dict(os.environ), tmp_path / "bridge", install)
    assert summary["skipped"] == [{"name": "locale", "reason": "unknown"}, {"name": "hmr", "reason": "unknown"}]
    assert rows(path) == [*ACP, PERMISSION]
