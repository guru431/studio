"""User service and stable launcher shared by the installer and settings."""
import os
import shlex
import subprocess
import time
import uuid
from pathlib import Path

from . import host
from .atomic import write_atomic

UNIT = "studio-agent.service"
HEADER = "# Managed by Studio Agent\n"


def launcher() -> Path:
    return Path.home() / ".local/bin/studio-agent"


def unit_file() -> Path:
    return host.xdg_home("XDG_CONFIG_HOME", ".config") / "systemd/user" / UNIT


def install_launcher(base: Path) -> None:
    target = launcher()
    current = shlex.quote(str(base / "current"))
    # SSH and the user manager need not inherit the same environment. Pin the
    # effective defaults too, so both address the same profile and key store.
    profile = {"XDG_DATA_HOME": host.xdg_home("XDG_DATA_HOME", ".local/share"),
               "XDG_CONFIG_HOME": host.xdg_home("XDG_CONFIG_HOME", ".config"),
               "STUDIO_AGENT_HOME": Path(os.environ.get("STUDIO_AGENT_HOME") or host.data_home()).resolve()}
    paths = "".join(f"export {name}={shlex.quote(str(value))}\n" for name, value in profile.items())
    content = f'#!/bin/sh\n{HEADER}set -eu\n{paths}cd {current}\nexec .venv/bin/python -m studio_agent "$@"\n'
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or (target.exists() and not target.read_bytes().startswith(("#!/bin/sh\n" + HEADER).encode())):
        raise ValueError("Другой файл уже занимает путь команды studio-agent")
    write_atomic(target, content.encode(), mode=0o755)


def switch_current(base: Path, stage: Path, expected: Path | None) -> None:
    link = base / "current"
    if stage.resolve().parent != (base / "releases").resolve() or stage.is_symlink():
        raise ValueError("Некорректный каталог выпуска")
    if link.is_symlink():
        if expected is None or link.resolve(strict=True) != expected.resolve(strict=True):
            raise ValueError("Текущий выпуск изменился")
    elif expected is not None or link.exists():
        raise ValueError("Некорректный указатель текущего выпуска")
    pending = base / ("current-" + uuid.uuid4().hex)
    try:
        pending.symlink_to(stage)
        os.replace(pending, link)
    finally:
        pending.unlink(missing_ok=True)


def systemctl(*args, check=True, timeout=None):
    env = dict(os.environ)
    if not env.get("XDG_RUNTIME_DIR") and not env.get("DBUS_SESSION_BUS_ADDRESS"):
        env["XDG_RUNTIME_DIR"] = f"/run/user/{os.geteuid()}"
    if timeout is None:
        timeout = 110 if args[0] == "start" else 15
    return subprocess.run(["systemctl", "--user", *args], stdin=subprocess.DEVNULL,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          env=env, timeout=timeout, check=check)


def prepare_linger() -> bool:
    uid = os.geteuid()
    loginctl = ["/usr/bin/loginctl", "--no-ask-password"]
    options = dict(stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    try:
        status = subprocess.run([*loginctl, "show-user", "-p", "Linger", "--value", str(uid)], **options)
        if status.stdout.strip() != b"yes":
            command = [*loginctl, "enable-linger", str(uid)]
            result = subprocess.run(command, **options)
            if result.returncode and uid != 0:
                result = subprocess.run(["/usr/bin/sudo", "-n", "--", *command], **options)
            if result.returncode:
                return False
    except (OSError, subprocess.SubprocessError):
        return False

    # logind starts the user manager asynchronously when there is no login session.
    deadline = time.monotonic() + 10
    while systemctl("show", "--property=Version", check=False, timeout=2).returncode:
        if time.monotonic() >= deadline:
            raise OSError("Пользовательская служба systemd недоступна")
        time.sleep(0.2)
    return True


def install_unit() -> None:
    path = unit_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or (path.exists() and not path.read_bytes().startswith(HEADER.encode())):
        raise ValueError("Другой файл уже занимает путь studio-agent.service")
    # %h is expanded by systemd and stays a single argument even for a home with spaces.
    content = (HEADER + "[Unit]\nDescription=Studio Agent\n\n[Service]\nType=notify\nTimeoutStartSec=100\n"
               "ExecStart=\"%h/.local/bin/studio-agent\" run --console\nUMask=0077\nRestart=on-failure\n"
               "RestartSec=5\n\n[Install]\nWantedBy=default.target\n")
    write_atomic(path, content.encode())
    # The login that runs the installer may use another XDG_CONFIG_HOME than
    # the already-running user manager. Link the explicit file into its search path.
    systemctl("link", str(path))
    systemctl("daemon-reload")


def enabled(*, offline=False) -> bool:
    try:
        # The installer must also read an existing boot setting without a user manager.
        flags = ("--root=/",) if offline else ()
        return systemctl(*flags, "is-enabled", "--quiet", UNIT, check=False).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def set_enabled(on: bool) -> None:
    if not on and not unit_file().exists():
        return
    try:
        if on:
            install_unit()
        systemctl("enable" if on else "disable", UNIT)
    except (OSError, subprocess.SubprocessError) as error:
        raise OSError("Пользовательская служба systemd недоступна") from error
