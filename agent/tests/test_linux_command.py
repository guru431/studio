"""The system dispatcher must select the correct installation before opening its launcher."""
import sys
import os
from types import SimpleNamespace
from pathlib import Path

import pytest

from studio_agent import linux_dispatch, linux_register


@pytest.fixture
def dispatch_root(monkeypatch):
    root = SimpleNamespace(pw_uid=0, pw_gid=0, pw_name="root", pw_dir="/root")
    owner = SimpleNamespace(pw_uid=1012, pw_gid=1013, pw_name="alice", pw_dir="/home/alice")
    monkeypatch.setitem(sys.modules, "pwd", SimpleNamespace(getpwuid={0: root, 1012: owner}.__getitem__))
    calls = []
    mock = SimpleNamespace(geteuid=lambda: 0, environ={"PYTHONPATH": "/bad", "ENV": "/bad", "TERM": "xterm"},
                           initgroups=lambda *args: calls.append(("initgroups", args)),
                           setresgid=lambda *args: calls.append(("gid", args)),
                           setresuid=lambda *args: calls.append(("uid", args)),
                           umask=lambda value: None, chdir=lambda path: None,
                           execve=lambda *args: calls.append(("exec", args)))
    monkeypatch.setattr(linux_dispatch, "os", mock)
    return calls


def test_root_prefers_its_installed_agent_when_two_accounts_exist(dispatch_root, monkeypatch):
    def installed(account):
        dispatch_root.append(("inspect", account.pw_name))
        return account.pw_dir + "/.local/bin/studio-agent"

    monkeypatch.setattr(linux_dispatch, "checked_launcher", installed)
    linux_dispatch.run_personal(1012, "alice", ["status"])
    assert [op for op, _ in dispatch_root] == ["inspect", "exec"]
    assert dispatch_root[0][1] == "root"
    path, args, env = dispatch_root[1][1]
    assert args == [path, "status"] and path == "/root/.local/bin/studio-agent"
    assert env["HOME"] == "/root" and "PYTHONPATH" not in env and "ENV" not in env


def test_root_without_a_personal_agent_opens_user_files_only_after_dropping_ids(dispatch_root, monkeypatch):
    def installed(account):
        dispatch_root.append(("inspect", account.pw_name))
        if account.pw_uid == 0:
            raise FileNotFoundError
        return account.pw_dir + "/.local/bin/studio-agent"

    monkeypatch.setattr(linux_dispatch, "checked_launcher", installed)
    linux_dispatch.run_personal(1012, "alice", ["menu"])
    assert dispatch_root[:5] == [("inspect", "root"), ("initgroups", ("alice", 1013)),
                                 ("gid", (1013, 1013, 1013)), ("uid", (1012, 1012, 1012)), ("inspect", "alice")]
    path, args, env = dispatch_root[-1][1]
    assert path == "/home/alice/.local/bin/studio-agent" and args == [path, "menu"]
    assert env["HOME"] == "/home/alice" and env["XDG_RUNTIME_DIR"] == "/run/user/1012"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1012/bus"
    assert "PYTHONPATH" not in env and "ENV" not in env


def test_insecure_root_launcher_fails_closed_without_switching_accounts(dispatch_root, monkeypatch):
    def unsafe(_):
        raise PermissionError

    monkeypatch.setattr(linux_dispatch, "checked_launcher", unsafe)
    with pytest.raises(PermissionError):
        linux_dispatch.run_personal(1012, "alice", [])
    assert not dispatch_root


def test_no_sudo_keeps_personal_install_and_invokes_only_isolated_system_python(monkeypatch):
    def missing(*args):
        raise FileNotFoundError
    monkeypatch.setattr(linux_dispatch, "safe_directory", missing)
    monkeypatch.setattr(linux_register.os, "geteuid", lambda: 1012, raising=False)
    monkeypatch.setattr(linux_register.os, "getuid", lambda: 1012, raising=False)
    calls = []

    def unavailable(args, **kwargs):
        calls.append(args)
        assert kwargs["stdin"] == linux_register.subprocess.DEVNULL
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(linux_register.subprocess, "run", unavailable)
    assert not linux_register.register(SimpleNamespace(pw_uid=1012))
    (args,) = calls
    assert args[:-1] == ["/usr/bin/sudo", "-n", "--", "/usr/bin/python3", "-IS", "-c"]
    compile(args[-1], "<system command registration>", "exec")


@pytest.mark.parametrize("binding", [(1013, "first"), None])
def test_install_reports_the_available_command_for_a_second_owner(binding, tmp_path, monkeypatch, capsys):
    import install_linux
    user = SimpleNamespace(pw_uid=1012, pw_name="second", pw_dir=str(tmp_path))
    monkeypatch.setitem(sys.modules, "pwd", SimpleNamespace(getpwuid=lambda uid: user))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "argv", ["setup.sh", "--no-menu"])
    monkeypatch.setattr(os, "geteuid", lambda: 1012, raising=False)
    monkeypatch.setattr(os, "umask", lambda value: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    personal = tmp_path / ".local/bin/studio-agent"
    monkeypatch.setattr(install_linux, "install", lambda *args, **kwargs: personal)
    monkeypatch.setattr(linux_register, "register", lambda account: binding)
    assert install_linux.main() == 0
    message = capsys.readouterr().out
    if binding is not None:
        assert "Меню: studio-agent menu" in message and "UID 1013" in message
        assert "--system-command-for" not in message
    else:
        assert f"Меню: {personal} menu" in message and "--system-command-for second" in message


@pytest.mark.skipif(sys.platform != "linux", reason="Linux dirfd and atomic hard links")
def test_racing_registrations_preserve_one_owner_and_foreign_files(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import Barrier

    @contextmanager
    def test_directory(path, owners, *, create):
        assert path == tmp_path and owners == {0} and create
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            yield fd
        finally:
            os.close(fd)

    def root_file(fd):
        info = os.fstat(fd)
        return SimpleNamespace(st_uid=0, st_mode=info.st_mode, st_size=info.st_size)
    file_os = dict(vars(os), geteuid=lambda: 0, fstat=root_file)
    monkeypatch.setattr(linux_register, "os", SimpleNamespace(**file_os))
    dummy = SimpleNamespace(safe_directory=test_directory, checked_launcher=lambda account: None)
    sync = Barrier(2)
    def attempt(uid):
        owner = SimpleNamespace(pw_uid=uid, pw_name=f"user{uid}")
        sync.wait(2)
        return linux_register.publish_command(owner, dummy, b"# fixture\n", tmp_path)

    with ThreadPoolExecutor(2) as pool:
        first, second = pool.map(attempt, (1012, 1013))
    assert first == second
    owner = first[0]
    (command,) = tmp_path.iterdir()
    assert command.read_text().endswith(f"dispatch({owner}, 'user{owner}')\n")
    foreign = command.read_bytes().replace(b"# fixture", b"# foreign")
    command.write_bytes(foreign)
    with pytest.raises(FileExistsError):
        linux_register.publish_command(SimpleNamespace(pw_uid=owner, pw_name=f"user{owner}"),
                                       dummy, b"# fixture\n", tmp_path)
    assert command.read_bytes() == foreign
