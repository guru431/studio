"""Поиск установок DeepSeek Harness (npm, Python-пакет, Desktop) и запуск `dsh --profile acp`: подставные каталоги."""

import json
import subprocess
from pathlib import Path

import pytest

from studio_agent import deepseek_rpc as rpc
from studio_agent import host

ENGINES = ">=24.9.0"
NEWER = "^24.22.3 || >=26.13.1"


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    """Настоящий реестр, PATH и Node тест не видит: всё подставное."""
    monkeypatch.setattr(rpc, "winreg", None)
    monkeypatch.setattr(rpc, "_which", lambda name, environ: None)
    monkeypatch.setattr(rpc, "_node_version", lambda node: "v26.7.0")


def platform(monkeypatch, name):
    monkeypatch.setattr(host, "MACOS", name == "mac")
    monkeypatch.setattr(host, "LINUX", name == "linux")


def which(monkeypatch, **found):
    monkeypatch.setattr(rpc, "_which", lambda name, environ: found.get(name))


def package(root: Path, engines=ENGINES, name="@deepseek-ai/dsh") -> Path:
    folder = root / "node_modules/@deepseek-ai/dsh"
    (folder / "lib").mkdir(parents=True)
    (folder / "package.json").write_text(json.dumps(
        {"name": name, "version": "0.2.2-rc.5", "engines": {"node": engines}, "bin": {"dsh": "lib/bin.js"}}), "utf-8")
    (folder / "lib/bin.js").write_text("#!/usr/bin/env node\n", "utf-8")
    return folder


def test_windows_npm_install_runs_node_with_bin_js_not_cmd_shim(tmp_path, monkeypatch):
    platform(monkeypatch, "win")
    folder = package(tmp_path / "npm")
    (tmp_path / "npm/dsh.cmd").write_text("@echo off\n", "utf-8")
    node = tmp_path / "node/node.exe"
    which(monkeypatch, **{"node.exe": node})
    found = rpc.find_installs(environ={"APPDATA": str(tmp_path)}, home=tmp_path)
    assert found == [rpc.Install("npm", (str(node), str(folder / "lib/bin.js")), {}, "web", folder, None)]
    assert not any(part.lower().endswith((".cmd", ".ps1")) for part in found[0].command)


def test_windows_npm_found_by_shim_directory_of_custom_prefix(tmp_path, monkeypatch):
    platform(monkeypatch, "win")
    folder = package(tmp_path / "prefix")
    shim = tmp_path / "prefix/dsh.cmd"
    shim.write_text("@echo off\n", "utf-8")
    which(monkeypatch, **{"node.exe": tmp_path / "node.exe", "dsh.cmd": shim})
    found = rpc.find_installs(environ={"APPDATA": str(tmp_path / "empty")}, home=tmp_path)
    assert [item.package_dir for item in found] == [folder]


def test_posix_npm_link_is_accepted(tmp_path, monkeypatch):
    platform(monkeypatch, "linux")
    folder = package(tmp_path / "lib")
    link = tmp_path / "bin/dsh"
    link.parent.mkdir()
    try:
        link.symlink_to(folder / "lib/bin.js")
    except OSError:  # ссылки без прав недоступны: проверяем сам путь внутри пакета
        link = folder / "lib/bin.js"
    node = tmp_path / "bin/node"
    which(monkeypatch, dsh=link, node=node)
    found = rpc.find_installs(environ={}, home=tmp_path)
    assert [(i.kind, i.profile, i.reason, i.package_dir.resolve()) for i in found] == [
        ("npm", "web", None, folder.resolve())]
    assert found[0].command == (str(node), str((folder / "lib/bin.js").resolve()))


def test_foreign_dsh_is_rejected(tmp_path, monkeypatch):
    platform(monkeypatch, "linux")
    foreign = tmp_path / "usr/bin/dsh"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("#!/bin/sh\n", "utf-8")
    (tmp_path / "usr/package.json").write_text(json.dumps({"name": "something-else"}), "utf-8")
    which(monkeypatch, dsh=foreign, node=tmp_path / "node")
    assert rpc.find_installs(environ={}, home=tmp_path) == []


def test_npm_dsh_behind_a_foreign_one_is_found_with_its_own_node(tmp_path, monkeypatch):
    """Minor-1: первым по PATH стоит чужой `dsh` — npm-DSH дальше всё равно найден; Node — из того же `bin`."""
    platform(monkeypatch, "linux")
    foreign = tmp_path / "usr/bin/dsh"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("#!/bin/sh\n", "utf-8")
    folder = package(tmp_path / "nvm/lib")
    launcher = tmp_path / "nvm/bin/dsh"
    launcher.parent.mkdir(parents=True)
    try:
        launcher.symlink_to(folder / "lib/bin.js")
    except OSError:  # ссылки без прав недоступны: проверяем сам путь внутри пакета
        launcher = folder / "lib/bin.js"
    node = launcher.parent / "node"
    node.write_text("", "utf-8")
    which(monkeypatch, dsh=foreign, node=tmp_path / "usr/bin/node")
    monkeypatch.setattr(rpc.shutil, "which", lambda name, path=None: str(
        {str(foreign.parent): foreign, str(launcher.parent): launcher}.get(path)) if path in (
        str(foreign.parent), str(launcher.parent)) else None)
    environ = {"PATH": f"{foreign.parent}{rpc.os.pathsep}{launcher.parent}"}
    (found,) = rpc.find_installs(environ=environ, home=tmp_path)
    assert found.package_dir.resolve() == folder.resolve() and found.command[0] == str(node)


def test_foreign_dsh_exe_on_windows_is_rejected(tmp_path, monkeypatch):
    platform(monkeypatch, "win")
    foreign = tmp_path / "tools/Scripts/dsh.exe"
    foreign.parent.mkdir(parents=True)
    foreign.touch()
    which(monkeypatch, **{"dsh.exe": foreign})
    assert rpc.find_installs(environ={}, home=tmp_path) == []


def test_old_node_is_reported_not_ready(tmp_path, monkeypatch):
    platform(monkeypatch, "win")
    package(tmp_path / "npm")
    which(monkeypatch, **{"node.exe": tmp_path / "node.exe"})
    monkeypatch.setattr(rpc, "_node_version", lambda node: "v22.1.0")
    (found,) = rpc.find_installs(environ={"APPDATA": str(tmp_path)}, home=tmp_path)
    assert found.reason == "node"
    assert rpc.pick([found], None) is None


def test_missing_node_is_reported_not_ready(tmp_path, monkeypatch):
    platform(monkeypatch, "win")
    package(tmp_path / "npm")
    (found,) = rpc.find_installs(environ={"APPDATA": str(tmp_path)}, home=tmp_path)
    assert found.reason == "node" and found.command[0] == "node"


def test_node_ok_ranges():
    assert rpc.node_ok("v26.7.0", ">=24.9.0") and not rpc.node_ok("v26.7.0", "^24.22.3 || >=26.13.1")
    assert rpc.node_ok("v24.22.3", "^24.22.3 || >=26.13.1") and not rpc.node_ok("v25.0.0", "^24.22.3 || >=26.13.1")
    assert rpc.node_ok("v24.30.1", NEWER) and rpc.node_ok("v26.13.1", NEWER) and not rpc.node_ok("v24.22.2", NEWER)
    assert not rpc.node_ok("v24.8.9", ENGINES) and rpc.node_ok("24.9.0", ENGINES)
    assert rpc.node_ok("v20.1.0", ">=18 <22") and not rpc.node_ok("v22.0.0", ">=18 <22")
    assert rpc.node_ok("v1.2.9", "~1.2.3") and not rpc.node_ok("v1.3.0", "~1.2.3")
    assert not rpc.node_ok("", ENGINES) and not rpc.node_ok("v26.7.0-nightly2026", "^24.22.3")
    assert rpc.node_ok("v26.7.0", "") and rpc.node_ok("v1.0.0", "weird range")


class Key:
    def __init__(self, values):
        self.values = values

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Registry(Key):
    """Подставной `winreg`: HKCU Uninstall с записями `{имя: {значение: данные}}`."""
    HKEY_CURRENT_USER = "HKCU"

    def __init__(self, entries):
        super().__init__(entries)

    def OpenKey(self, parent, name):
        if parent == "HKCU":
            assert name == rpc.UNINSTALL
            return self
        return Key(self.values[name])

    def EnumKey(self, key, index):
        try:
            return list(self.values)[index]
        except IndexError:
            raise OSError() from None

    def QueryValueEx(self, key, name):
        if name not in key.values:
            raise FileNotFoundError(name)
        return key.values[name], 1


def desktop_windows(root: Path) -> Path:
    (root / "resources").mkdir(parents=True)
    (root / "DeepSeek Harness.exe").touch()
    (root / "resources/app.asar").touch()
    return root


def test_desktop_install_found_per_platform_windows(tmp_path, monkeypatch):
    platform(monkeypatch, "win")
    root = desktop_windows(tmp_path / "Programs/DeepSeek Harness")
    monkeypatch.setattr(rpc, "winreg", Registry({"other": {"InstallLocation": str(tmp_path / "x")},
                                                      "nodisplay": {}, "dsh": {"InstallLocation": f'"{root}"'}}))
    (found,) = rpc.find_installs(environ={}, home=tmp_path)
    cli = root / "resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"
    assert found == rpc.Install("desktop", (str(root / "DeepSeek Harness.exe"), "--expose-internals", str(cli)),
                                {"ELECTRON_RUN_AS_NODE": "1"}, "desktop", root, None)


def test_desktop_windows_default_folder_and_registered_launcher(tmp_path, monkeypatch):
    platform(monkeypatch, "win")
    local = desktop_windows(tmp_path / "local/Programs/DeepSeek Harness")
    other = desktop_windows(tmp_path / "elsewhere")
    bin_dir = other / "resources/runtime/cli/bin"
    bin_dir.mkdir(parents=True)
    environ = {"LOCALAPPDATA": str(tmp_path / "local"), "PATH": f"{tmp_path};{bin_dir}"}
    found = rpc.find_installs(environ=environ, home=tmp_path)
    assert [item.package_dir for item in found] == [local, other]
    assert all(item.env == {"ELECTRON_RUN_AS_NODE": "1"} and item.profile == "desktop" for item in found)


def test_desktop_windows_incomplete_install_is_skipped(tmp_path, monkeypatch):
    platform(monkeypatch, "win")
    root = tmp_path / "Programs/DeepSeek Harness"
    root.mkdir(parents=True)
    (root / "DeepSeek Harness.exe").touch()
    assert rpc.find_installs(environ={"LOCALAPPDATA": str(tmp_path)}, home=tmp_path) == []


def desktop_mac(app: Path) -> Path:
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Resources/runtime/cli/bin").mkdir(parents=True)
    (app / "Contents/MacOS/DeepSeek Harness").touch()
    (app / "Contents/Resources/app.asar").touch()
    (app / "Contents/Resources/runtime/cli/bin/dsh").write_text("#!/bin/sh\n", "utf-8")
    return app


def test_desktop_install_found_per_platform_macos(tmp_path, monkeypatch):
    platform(monkeypatch, "mac")
    app = desktop_mac(tmp_path / "Applications/DeepSeek Harness.app")
    (found,) = rpc.find_installs(environ={}, home=tmp_path)
    cli = app / "Contents/Resources/app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"
    assert found == rpc.Install("desktop", (str(app / "Contents/MacOS/DeepSeek Harness"), "--expose-internals",
                                            str(cli)), {"ELECTRON_RUN_AS_NODE": "1"}, "desktop", app, None)


def test_registered_macos_launcher_is_recognized_as_desktop(tmp_path, monkeypatch):
    platform(monkeypatch, "mac")
    app = desktop_mac(tmp_path / "Volumes/Apps/DeepSeek Harness.app")
    link = tmp_path / "usr/local/bin/dsh"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(app / "Contents/Resources/runtime/cli/bin/dsh")
    except OSError:  # ссылки без прав недоступны: проверяем сам путь внутри приложения
        link = app / "Contents/Resources/runtime/cli/bin/dsh"
    which(monkeypatch, dsh=link)
    found = rpc.find_installs(environ={}, home=tmp_path)
    assert [(i.kind, i.package_dir.resolve()) for i in found] == [("desktop", app.resolve())]


def test_desktop_is_not_searched_on_linux(tmp_path, monkeypatch):
    platform(monkeypatch, "linux")
    desktop_windows(tmp_path / "Programs/DeepSeek Harness")
    assert rpc.find_installs(environ={"LOCALAPPDATA": str(tmp_path)}, home=tmp_path) == []


def python_package(site: Path) -> Path:
    runtime = site / "deepseek_harness_runtime"
    (runtime / "runtime").mkdir(parents=True)
    (runtime / "deepseek-harness-runtime.json").write_text("{}", "utf-8")
    exe = runtime / "runtime" / (f"deepseek-harness-sdk-runtime-{rpc._tag()}" + (".exe" if rpc._tag().startswith("win") else ""))
    exe.touch()
    return exe


def test_python_package_runs_bundled_runtime_with_explicit_dsh_home(tmp_path, monkeypatch):
    platform(monkeypatch, "win")
    exe = python_package(tmp_path / "venv/Lib/site-packages")
    launcher = tmp_path / "venv/Scripts/dsh.exe"
    launcher.parent.mkdir(parents=True)
    launcher.touch()
    which(monkeypatch, **{"dsh.exe": launcher})
    (found,) = rpc.find_installs(environ={}, home=tmp_path)
    assert found == rpc.Install("python", (str(exe),), {"DSH_HOME": str(tmp_path / ".dsh")}, "web", None, None)
    (found,) = rpc.find_installs(environ={"DSH_HOME": "D:/dsh"}, home=tmp_path)
    assert found.env == {"DSH_HOME": "D:/dsh"}


def test_python_package_on_posix_layout(tmp_path, monkeypatch):
    platform(monkeypatch, "linux")
    exe = python_package(tmp_path / ".local/lib/python3.13/site-packages")
    launcher = tmp_path / ".local/bin/dsh"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("#!/usr/bin/python3\n", "utf-8")
    which(monkeypatch, dsh=launcher)
    (found,) = rpc.find_installs(environ={"DSH_HOME": "/home/u/.dsh"}, home=tmp_path)
    assert (found.kind, found.command, found.reason) == ("python", (str(exe),), None)


def test_pick_follows_source_profile():
    npm = rpc.Install("npm", ("node", "bin.js"), {}, "web", Path("p"), None)
    python = rpc.Install("python", ("rt",), {}, "web", None, None)
    desktop = rpc.Install("desktop", ("app",), {}, "desktop", Path("d"), None)
    assert rpc.pick([desktop, python, npm], "desktop") is desktop
    assert rpc.pick([desktop, python, npm], "web") is npm
    assert rpc.pick([desktop, python], "web") is python
    assert rpc.pick([desktop, python, npm], None) is npm
    assert rpc.pick([desktop, python], None) is python
    assert rpc.pick([desktop], None) is desktop
    assert rpc.pick([npm], "desktop") is None and rpc.pick([], None) is None


def test_pick_skips_installs_that_are_not_ready():
    old = rpc.Install("npm", ("node", "bin.js"), {}, "web", Path("p"), "node")
    desktop = rpc.Install("desktop", ("app",), {}, "desktop", Path("d"), None)
    assert rpc.pick([old, desktop], "web") is None
    assert rpc.pick([old, desktop], None) is desktop


def test_client_command_and_env(tmp_path, monkeypatch):
    monkeypatch.delenv("DSH_PERMISSION_MODE", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-secret-value")
    install = rpc.Install("desktop", ("app.exe", "--expose-internals", "cli.js"), {"ELECTRON_RUN_AS_NODE": "1"},
                          "desktop", None, None)
    client = rpc.Client(install, tmp_path, access="read-only")
    assert client.command == ["app.exe", "--expose-internals", "cli.js", "--profile", "acp"]
    assert client.env["DSH_PERMISSION_MODE"] == "read-only" and client.env["ELECTRON_RUN_AS_NODE"] == "1"
    assert client.env["DEEPSEEK_API_KEY"] == "sk-secret-value"
    assert not any("sk-secret-value" in part for part in client.command)
    for access in ("workspace-write", "danger-full-access"):
        assert rpc.Client(install, tmp_path, access=access).env["DSH_PERMISSION_MODE"] == access
    for access in ("default", None, "nonsense"):
        assert "DSH_PERMISSION_MODE" not in rpc.Client(install, tmp_path, access=access).env
    overlay = tmp_path / "overlay.json"
    patched = rpc.Client(install, tmp_path, overlay=overlay)
    assert patched.command[-4:] == ["--profile", "acp", "--patch", str(overlay)]


def test_client_default_leaves_parent_permission_mode_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("DSH_PERMISSION_MODE", "read-only")
    install = rpc.Install("npm", ("node", "bin.js"), {}, "web", None, None)
    assert rpc.Client(install, tmp_path, access="default").env["DSH_PERMISSION_MODE"] == "read-only"
    assert rpc.Client(install, tmp_path, access="workspace-write").env["DSH_PERMISSION_MODE"] == "workspace-write"


async def test_client_spawns_with_command_and_env(tmp_path, monkeypatch):
    calls = []

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        raise RuntimeError("stop")

    install = rpc.Install("npm", ("node", "bin.js"), {}, "web", None, None)
    client = rpc.Client(install, tmp_path, access="read-only", spawn=spawn)
    with pytest.raises(RuntimeError):
        await client.connect()
    assert calls[0][0] == ("node", "bin.js", "--profile", "acp")
    assert calls[0][1]["env"]["DSH_PERMISSION_MODE"] == "read-only"


def test_version_reads_output_and_falls_back(monkeypatch):
    install = rpc.Install("python", ("rt",), {"DSH_HOME": "h"}, "web", None, None)
    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs["env"]["DSH_HOME"]))
        return subprocess.CompletedProcess(command, 0, b"0.2.2-rc.5\r\n", b"")

    monkeypatch.setattr(rpc.subprocess, "run", run)
    assert rpc.version(install) == "0.2.2-rc.5" and seen == [(["rt", "--version"], "h")]
    # в студию — только номер версии (Minor-10): произвольный вывод — запасное имя
    monkeypatch.setattr(rpc.subprocess, "run", lambda c, **k: subprocess.CompletedProcess(c, 0, b"x" * 100, b""))
    assert rpc.version(install) == "DeepSeek Harness"
    monkeypatch.setattr(rpc.subprocess, "run",
                        lambda c, **k: subprocess.CompletedProcess(c, 0, b"dsh 0.2.3 /home/user/path\n", b""))
    assert rpc.version(install) == "0.2.3"
    monkeypatch.setattr(rpc.subprocess, "run", lambda c, **k: subprocess.CompletedProcess(c, 1, b"boom", b""))
    assert rpc.version(install) == "DeepSeek Harness"
    monkeypatch.setattr(rpc.subprocess, "run", lambda c, **k: subprocess.CompletedProcess(c, 0, b"  \n", b""))
    assert rpc.version(install) == "DeepSeek Harness"

    def broken(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(rpc.subprocess, "run", broken)
    assert rpc.version(install) == "DeepSeek Harness"
