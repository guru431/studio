"""Установщик Studio Agent для Linux. Запуск: sh ./studio-agent-setup.sh."""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


def install(archive: Path, *, autostart: bool | None, start: bool):
    from studio_agent import host, linux_autostart, single

    mutex = single.acquire()
    if mutex is None:
        raise RuntimeError("Мост работает. Выберите выход после завершения заданий в меню studio-agent.")
    base = host.install_home()
    stage = None
    installed = False
    try:
        releases = base / "releases"
        releases.mkdir(mode=0o700, parents=True, exist_ok=True)
        link = base / "current"
        old = link.resolve(strict=True) if link.is_symlink() else None
        if old is not None and old.parent != releases.resolve():
            raise ValueError("Текущий выпуск находится вне каталога установщика")
        stage = Path(tempfile.mkdtemp(prefix="release-", dir=releases))
        with zipfile.ZipFile(archive) as source:
            for member in source.infolist():
                if member.filename == "__main__.py":
                    continue
                if not re.fullmatch(r"studio_agent/\w+\.py|requirements\.txt|constraints\.txt", member.filename):
                    raise ValueError("Некорректный состав установщика")
                target = stage / member.filename
                target.parent.mkdir(exist_ok=True)
                with source.open(member) as contents, target.open("xb") as output:
                    shutil.copyfileobj(contents, output)
        with tempfile.TemporaryDirectory(dir=stage, prefix="install-") as temp:
            env = dict(os.environ, TMPDIR=temp, PIP_CACHE_DIR=temp)
            env.pop("PYTHONHOME", None)
            env.pop("PYTHONPATH", None)
            python = str(host.python(stage))
            subprocess.run([sys.executable, "-m", "venv", str(stage / ".venv")], cwd=stage,
                           env=env, timeout=300, check=True)
            subprocess.run([python, "-m", "pip", "install", "--disable-pip-version-check", "-q",
                            "-r", "requirements.txt", "-c", "constraints.txt"],
                           cwd=stage, env=env, timeout=1800, check=True)
            subprocess.run([python, "-c", "import studio_agent.app, studio_agent.linux_menu"],
                           cwd=stage, env=env, timeout=30, check=True)
        linux_autostart.install_launcher(base)
        linux_autostart.switch_current(base, stage, old)
        installed = True
        if os.geteuid() == 0:
            import pwd
            from studio_agent import linux_dispatch
            linux_dispatch.checked_launcher(pwd.getpwuid(0))
        (stage / "_update_result").write_text("complete", encoding="ascii")
        try:
            enable = (old is None or linux_autostart.enabled(offline=True)) if autostart is None else autostart
            if enable:
                if start and not linux_autostart.prepare_linger():
                    print("Не удалось включить работу между сеансами. Администратор может выполнить "
                          f"loginctl enable-linger {os.geteuid()}.")
                linux_autostart.set_enabled(True)
            elif start or linux_autostart.unit_file().exists():
                linux_autostart.install_unit()
                linux_autostart.set_enabled(False)
        except (OSError, subprocess.SubprocessError):
            print("Служба systemd пользователя недоступна. Канал можно запустить командой studio-agent run.")
            start = False
    finally:
        if stage is not None and not installed:
            shutil.rmtree(stage)
        mutex.close()
    if start:
        linux_autostart.systemctl("start", linux_autostart.UNIT)
    return linux_autostart.launcher()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-autostart", action="store_true")
    parser.add_argument("--no-start", action="store_true")
    parser.add_argument("--no-menu", action="store_true")
    parser.add_argument("--system-command-for", metavar="USER",
                        help="зарегистрировать системную команду для существующей установки пользователя")
    args = parser.parse_args()
    if sys.platform != "linux" or sys.version_info < (3, 11):
        parser.error("Нужны Linux и Python 3.11+ с модулем venv")
    os.umask(0o077)
    import pwd
    from studio_agent import host, linux_dispatch, linux_register

    account = pwd.getpwuid(os.geteuid())
    if args.system_command_for is not None:
        if os.geteuid() != 0:
            parser.error("Для регистрации системной команды нужны права администратора")
        try:
            owner = pwd.getpwnam(args.system_command_for)
            binding = linux_register.register(owner)
        except (KeyError, ValueError, OSError) as error:
            print(f"Регистрация studio-agent: {error}", file=sys.stderr)
            return 1
        print("Системная команда studio-agent доступна.")
        if binding != (owner.pw_uid, owner.pw_name):
            print(f"Сохранена прежняя привязка для root без личного агента (UID {binding[0]}).")
        return 0
    try:
        if account.pw_uid == 0:
            if Path.home() != Path(account.pw_dir):
                raise ValueError("Для установки от root используйте домашнюю папку root")
            config = host.xdg_home("XDG_CONFIG_HOME", ".config")
            linux_dispatch.check_root_paths((Path.home() / ".local/bin", host.install_home() / "releases",
                Path(os.environ.get("STUDIO_AGENT_HOME") or host.data_home()), config / "studio-agent",
                config / "systemd/user"))
        command = install(Path(sys.argv[0]), autostart=False if args.no_autostart else None,
                          start=not args.no_start)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Установка Studio Agent: {error}", file=sys.stderr)
        return 1
    binding = None
    registration_available = Path.home() == Path(account.pw_dir)
    try:
        if registration_available:
            binding = linux_register.register(account)
    except (OSError, ValueError) as error:
        registration_available = False
        print(f"Регистрация системной команды: {error}", file=sys.stderr)
    print(f"Studio Agent установлен. Меню: {'studio-agent' if binding else command} menu", flush=True)
    if binding and binding != (account.pw_uid, account.pw_name):
        print(f"Сохранена прежняя привязка для root без личного агента (UID {binding[0]}).")
    if not binding and registration_available:
        print("Администратор может зарегистрировать команду из доверенного файла установки:")
        import shlex
        print(f"sudo /usr/bin/python3 -IS ./studio-agent-setup.sh --system-command-for {shlex.quote(account.pw_name)}")
    if sys.stdin.isatty() and not args.no_menu:
        os.execl(str(command), str(command), "menu")
    return 0


if __name__ == "__main__":
    sys.exit(main())
