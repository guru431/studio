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


@pytest.mark.skipif(sys.platform != "linux", reason="Linux abstract Unix socket and peer credentials")
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
