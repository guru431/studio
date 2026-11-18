"""Standalone body of the system command. It runs with the distribution Python -IS."""
import contextlib
import os
import shlex
import stat
import sys
from pathlib import Path


@contextlib.contextmanager
def safe_directory(path, owners, *, create=False):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or path.anchor != "/":
        raise ValueError("Некорректный абсолютный путь")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        for part in (*path.parts[1:], None):
            info = os.fstat(fd)
            if info.st_uid not in owners or info.st_mode & 0o022:
                raise PermissionError("Небезопасный владелец или права каталога")
            if part is None:
                break
            if create:
                created = False
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                    created = True
                except FileExistsError:
                    pass
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            if create and created:
                os.fchmod(fd, 0o755)
        yield fd
    finally:
        os.close(fd)


def check_root_paths(paths):
    for path in paths:
        try:
            with safe_directory(path, {0}):
                pass
        except FileNotFoundError:
            pass  # All existing ancestors were checked. The installer creates the rest.


def _check_root_runtime(shell_body):
    # The personal launcher executes a Python environment under a movable
    # 'current' symlink. A root-owned shell file alone cannot vouch for its target.
    words = shlex.split(shell_body.decode(), comments=True)
    if words[:2] != ["set", "-eu"] or words[-5:] != ["exec", ".venv/bin/python", "-m", "studio_agent", "$@"]:
        raise ValueError("Неизвестный формат команды root")
    if words[-7:-6] != ["cd"]:
        raise ValueError("Неизвестный каталог выпуска root")
    settings = words[2:-7]
    paths = []
    while settings:
        if len(settings) < 2 or settings[0] != "export":
            raise ValueError("Неизвестная настройка команды root")
        name, equals, value = settings[1].partition("=")
        if not equals or name not in ("STUDIO_AGENT_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
            raise ValueError("Неизвестная переменная команды root")
        paths.append(value)
        settings = settings[2:]
    check_root_paths(paths)
    current = Path(words[-6])
    with safe_directory(current.parent, {0}) as base:
        info = os.stat("current", dir_fd=base, follow_symlinks=False)
        if current.name != "current" or info.st_uid != 0 or not stat.S_ISLNK(info.st_mode):
            raise PermissionError("Некорректный указатель текущего выпуска root")
        stage = Path(os.readlink("current", dir_fd=base))
    if stage.parent != current.parent / "releases" or not stage.name.startswith("release-"):
        raise PermissionError("Выпуск root расположен за пределами установки")
    with safe_directory(stage / "studio_agent", {0}):
        pass
    python = stage / ".venv/bin/python"
    for _ in range(40):
        with safe_directory(python.parent, {0}) as directory:
            info = os.stat(python.name, dir_fd=directory, follow_symlinks=False)
            if info.st_uid != 0:
                break
            if not stat.S_ISLNK(info.st_mode):
                if stat.S_ISREG(info.st_mode) and not info.st_mode & 0o022 and info.st_mode & 0o100:
                    return
                break
            python = python.parent / os.readlink(python.name, dir_fd=directory)
    raise PermissionError("Небезопасный интерпретатор выпуска root")


def checked_launcher(account):
    path = Path(account.pw_dir) / ".local/bin/studio-agent"
    with safe_directory(path.parent, {0, account.pw_uid}) as directory:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            header = b"#!/bin/sh\n# Managed by Studio Agent\n"
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != account.pw_uid
                    or info.st_mode & 0o022 or not info.st_mode & stat.S_IXUSR
                    or stream.read(len(header)) != header):
                raise PermissionError("Личная команда Studio Agent имеет другого владельца, права или содержимое")
            if account.pw_uid == 0:
                if info.st_size > 32768:
                    raise ValueError("Слишком большой файл команды root")
                try:
                    _check_root_runtime(stream.read(32768))
                except FileNotFoundError as error:
                    raise ValueError("Установка root не завершена") from error
    return str(path)


def run_personal(owner_uid, owner_name, argv):
    import pwd

    uid = os.geteuid()
    account = pwd.getpwuid(uid)
    if uid == 0:
        try:
            launcher = checked_launcher(account)
        except FileNotFoundError:
            if owner_uid == 0:
                raise
            account = pwd.getpwuid(owner_uid)
            if account.pw_name != owner_name:
                raise PermissionError("Учётная запись владельца изменилась")
            os.initgroups(account.pw_name, account.pw_gid)
            os.setresgid(account.pw_gid, account.pw_gid, account.pw_gid)
            os.setresuid(owner_uid, owner_uid, owner_uid)
            launcher = checked_launcher(account)
        runtime = f"/run/user/{account.pw_uid}"
        env = {key: os.environ[key] for key in ("LANG", "LC_ALL", "LC_CTYPE", "TERM", "COLORTERM")
               if key in os.environ}
        env.update(HOME=account.pw_dir, USER=account.pw_name, LOGNAME=account.pw_name,
                   PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                   XDG_RUNTIME_DIR=runtime, DBUS_SESSION_BUS_ADDRESS="unix:path=" + runtime + "/bus")
        os.umask(0o077)
        os.chdir(account.pw_dir)
    else:
        launcher = checked_launcher(account)
        env = os.environ.copy()
    os.execve(launcher, [launcher, *argv], env)


def dispatch(owner_uid, owner_name):
    try:
        run_personal(owner_uid, owner_name, sys.argv[1:])
    except FileNotFoundError:
        sys.exit("Для этой учётной записи Studio Agent не установлен.")
    except (OSError, KeyError, ValueError) as error:
        sys.exit(f"Studio Agent: {error}")
