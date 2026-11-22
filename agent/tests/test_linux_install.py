"""Linux package, local settings and supervised update contracts."""
import runpy
import shlex
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from studio_agent import linux_menu, linux_service
from studio_agent import linux_autostart
from studio_agent.errors import Refused

ROOT = Path(__file__).resolve().parents[2]


def test_installer_bundles_only_the_profiled_runtime(tmp_path):
    archive, installer = tmp_path / "release.zip", tmp_path / "setup.sh"
    with zipfile.ZipFile(archive, "w") as output:
        for name in ("install_linux.py", "requirements.txt", "constraints.txt", "studio_agent/__init__.py",
                     "studio_agent/linux_menu.py", "tests/test_install.py", "macos/studio-agent"):
            output.writestr("studio-agent/" + name, name)
    runpy.run_path(str(ROOT / "deploy/build-agent-linux.py"))["build"](archive, installer)
    assert b'exec /usr/bin/python3 -IS "$0" "$@"' in installer.read_bytes().split(b"PK\x03\x04", 1)[0]
    with zipfile.ZipFile(installer) as packed:
        assert {item.date_time for item in packed.infolist()} == {(1980, 1, 1, 0, 0, 0)}
        assert set(packed.namelist()) == {"__main__.py", "requirements.txt", "constraints.txt",
                                         "studio_agent/__init__.py", "studio_agent/linux_menu.py"}
        assert packed.read("__main__.py") == b"install_linux.py"
        assert packed.read("studio_agent/linux_menu.py") == b"studio_agent/linux_menu.py"


@pytest.mark.parametrize("autostart, start, linger_available, expected", [
    (None, True, True, ["linger", ("enable", True), "start"]),
    (None, True, False, ["linger", ("enable", True), "start"]),
    (False, True, True, ["unit", ("enable", False), "start"]),
    (None, False, True, [("enable", True)]),
])
def test_installer_prepares_linger_for_an_unattended_service(tmp_path, monkeypatch, capsys,
                                                             autostart, start, linger_available, expected):
    install = runpy.run_path(str(ROOT / "agent/install_linux.py"))["install"]
    archive = tmp_path / "setup.sh"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("__main__.py", "")
    calls = []

    def prepare_linger():
        calls.append("linger")
        return linger_available

    monkeypatch.setattr(linux_autostart.os, "geteuid", lambda: 1003, raising=False)
    monkeypatch.setattr(linux_autostart.host, "install_home", lambda: tmp_path / "app")
    monkeypatch.setattr(linux_service.single, "acquire", lambda: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(linux_autostart.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(linux_autostart, "install_launcher", lambda _: None)
    monkeypatch.setattr(linux_autostart, "switch_current", lambda *args: None)
    monkeypatch.setattr(linux_autostart, "prepare_linger", prepare_linger)
    monkeypatch.setattr(linux_autostart, "install_unit", lambda: calls.append("unit"))
    monkeypatch.setattr(linux_autostart, "set_enabled", lambda on: calls.append(("enable", on)))
    monkeypatch.setattr(linux_autostart, "systemctl", lambda command, unit: calls.append(command))
    install(archive, autostart=autostart, start=start)

    assert calls == expected
    assert ("loginctl enable-linger" in capsys.readouterr().out) == (not linger_available)


def test_linger_waits_for_root_manager_without_a_session_environment(monkeypatch):
    monkeypatch.setattr(linux_autostart.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)
    calls, sleeps = [], []
    answers = iter([(1, b""), (0, b""), (1, b""), (0, b"")])

    def run(args, **kwargs):
        calls.append(args)
        if args[0] == "systemctl":
            assert kwargs["env"]["XDG_RUNTIME_DIR"] == "/run/user/0"
        returncode, output = next(answers)
        return SimpleNamespace(returncode=returncode, stdout=output)

    monkeypatch.setattr(linux_autostart.subprocess, "run", run)
    monkeypatch.setattr(linux_autostart.time, "sleep", sleeps.append)
    assert linux_autostart.prepare_linger()
    assert calls == [
        ["/usr/bin/loginctl", "--no-ask-password", "show-user", "-p", "Linger", "--value", "0"],
        ["/usr/bin/loginctl", "--no-ask-password", "enable-linger", "0"],
        ["systemctl", "--user", "show", "--property=Version"],
        ["systemctl", "--user", "show", "--property=Version"],
    ]
    assert len(sleeps) == 1


def test_linger_sudo_fallback_targets_the_installing_user(monkeypatch):
    monkeypatch.setattr(linux_autostart.os, "geteuid", lambda: 1003, raising=False)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=int(args[0] != "/usr/bin/sudo"), stdout=b"")

    monkeypatch.setattr(linux_autostart.subprocess, "run", run)
    monkeypatch.setattr(linux_autostart, "systemctl", lambda *a, **kw: SimpleNamespace(returncode=0))
    assert linux_autostart.prepare_linger()
    assert calls[-2] == ["/usr/bin/loginctl", "--no-ask-password", "enable-linger", "1003"]
    assert calls[-1] == ["/usr/bin/sudo", "-n", "--", *calls[-2]]


def test_supervisor_rolls_back_when_prepared_release_dies_before_ready(tmp_path, monkeypatch):
    old = tmp_path / "releases/release-old"
    stage = tmp_path / "releases/release-new"
    stage.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(linux_service.updater, "REPO", old)
    monkeypatch.setattr(linux_service.host, "install_home", lambda: tmp_path)
    monkeypatch.setattr(linux_service.single, "acquire", lambda: SimpleNamespace(close=lambda: calls.append("unlock")))
    monkeypatch.setattr(linux_service.update_helper, "acquire_runtime_lease", lambda path: calls.append("lease"))
    monkeypatch.setattr(linux_service.update_helper, "_managed_base", lambda path: tmp_path / "releases")
    outcomes = iter([("update", stage.name), ("failed", ""), ("done", "")])

    def run(repo, previous, home):
        calls.append((repo, previous))
        return next(outcomes)

    monkeypatch.setattr(linux_service, "_run_release", run)
    assert linux_service.supervise(tmp_path) == 0
    assert calls == ["lease", (old, None), (stage, old), (old, None), "unlock"]
    assert (stage / "_update_result").read_text() == "rolled_back"


def test_launcher_pins_effective_xdg_profile_for_the_user_service(tmp_path, monkeypatch):
    monkeypatch.setattr(linux_autostart.host, "LINUX", True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data with spaces"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("STUDIO_AGENT_HOME", raising=False)
    captured = {}
    monkeypatch.setattr(linux_autostart, "write_atomic", lambda target, data, **kw: captured.update(data=data))
    linux_autostart.install_launcher(tmp_path / "app")
    script = captured["data"].decode()
    for name, path in [("XDG_DATA_HOME", tmp_path / "data with spaces"), ("XDG_CONFIG_HOME", tmp_path / ".config"),
                       ("STUDIO_AGENT_HOME", tmp_path / "data with spaces/studio-agent")]:
        assert f"export {name}={shlex.quote(str(path))}\n" in script


def test_local_desktop_consent_failure_keeps_desktop_disabled():
    def deny():
        raise Refused("desktop_unavailable", "Локальное разрешение не получено")

    ctrl = SimpleNamespace(agent=SimpleNamespace(desktop=SimpleNamespace(authorize_local=deny)),
                           set_desktop_enabled=lambda value: pytest.fail("unexpected opt-in"))
    with pytest.raises(Refused):
        linux_menu.dispatch(ctrl, "desktop", True)


@pytest.mark.parametrize("cmd", ["status", "enroll", "set-pin", "clear-pin", "reset-pin"])
def test_console_command_reports_busy_bridge_without_traceback(tmp_path, monkeypatch, capsys, cmd):
    def busy(*_):
        raise RuntimeError("Мост запускается или обновляется. Повторите действие.")

    monkeypatch.setattr(linux_menu, "invoke", busy)
    monkeypatch.setattr(linux_menu.getpass, "getpass", lambda *_: "2468")
    assert linux_menu.command(tmp_path, cmd) == 1
    assert capsys.readouterr().out.strip() == "Мост запускается или обновляется. Повторите действие."


@pytest.mark.parametrize("cmd, op, words", [("clear-pin", ("pin", None), "PIN убран."),
                                            ("reset-pin", ("reset",), "Блок PIN снят.")])
def test_console_pin_commands_confirm_success(tmp_path, monkeypatch, capsys, cmd, op, words):
    calls = []
    monkeypatch.setattr(linux_menu, "invoke", lambda h, *a: calls.append(a) or [])
    assert linux_menu.command(tmp_path, cmd) == 0
    assert calls == [op] and capsys.readouterr().out.strip() == words


def test_menu_number_prompts_refuse_non_digits_in_russian(tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: "много")
    monkeypatch.setattr(linux_menu, "invoke", lambda *a: pytest.fail("invoke with a bad number"))
    with pytest.raises(ValueError, match="Введите число"):
        linux_menu._action(tmp_path, "7")


def test_menu_socket_is_a_file_in_the_bridge_home_not_an_abstract_name():
    # Абстрактное имя без прав файловой системы мог занять другой пользователь — мост не стартовал бы.
    home = Path(Path.cwd().anchor) / "home" / "studio-agent"   # адрес только вычисляется, каталог не создаётся
    assert linux_menu._address(home) == home.resolve() / "run" / "menu.sock"


def test_long_bridge_home_puts_the_menu_socket_into_the_runtime_directory(monkeypatch):
    runtime = Path(Path.cwd().anchor) / "rt"
    home = runtime / ("x" * 120)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    address = linux_menu._address(home)
    assert address.parent.parent == runtime and address.parent.name.startswith("studio-agent-")
    assert address != linux_menu._address(runtime / ("y" * 120))
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    with pytest.raises(OSError):
        linux_menu._address(home)


def test_menu_socket_directory_is_private_and_owned(tmp_path, monkeypatch):
    folder = tmp_path / "run"
    modes = []
    monkeypatch.setattr(linux_menu.os, "chmod", lambda path, mode: modes.append(mode))
    monkeypatch.setattr(linux_menu.os, "geteuid", lambda: tmp_path.stat().st_uid, raising=False)
    linux_menu._private(folder)
    assert folder.is_dir() and modes == [0o700]
    monkeypatch.setattr(linux_menu.os, "geteuid", lambda: tmp_path.stat().st_uid + 1, raising=False)
    with pytest.raises(PermissionError):
        linux_menu._private(folder)                           # каталог другой учётной записи
    (tmp_path / "file").write_text("")
    with pytest.raises(OSError):
        linux_menu._private(tmp_path / "file")


def test_menu_socket_directory_refuses_a_symlink(tmp_path, monkeypatch):
    (tmp_path / "elsewhere").mkdir()
    try:
        (tmp_path / "run").symlink_to(tmp_path / "elsewhere", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not available")
    monkeypatch.setattr(linux_menu.os, "geteuid", lambda: (tmp_path / "run").lstat().st_uid, raising=False)
    monkeypatch.setattr(linux_menu.os, "chmod", lambda path, mode: pytest.fail("chmod through a symlink"))
    with pytest.raises(PermissionError):
        linux_menu._private(tmp_path / "run")


@pytest.mark.skipif(sys.platform != "linux", reason="Linux Unix socket and peer credentials")
def test_local_control_reports_rejected_action_and_preserves_listener(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    def set_limit(n):
        if n < 1:
            raise ValueError("limit")
    ctrl = SimpleNamespace(set_max_turns=set_limit)
    with linux_menu.listener(tmp_path) as server, ThreadPoolExecutor(1) as pool:
        first = pool.submit(linux_menu.handle_one, server, ctrl)
        with pytest.raises(RuntimeError):
            linux_menu.invoke(tmp_path, "limit", 0)
        first.result(timeout=1)
        second = pool.submit(linux_menu.handle_one, server, ctrl)
        assert linux_menu.invoke(tmp_path, "limit", 2) == []
        second.result(timeout=1)
