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
        (stage / "_update_result").write_text("complete", encoding="ascii")
        try:
            enable = (old is None or linux_autostart.enabled()) if autostart is None else autostart
            if enable:
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
    args = parser.parse_args()
    if sys.platform != "linux" or sys.version_info < (3, 11):
        parser.error("Нужны Linux и Python 3.11+ с модулем venv")
    os.umask(0o077)
    try:
        command = install(Path(sys.argv[0]), autostart=False if args.no_autostart else None,
                          start=not args.no_start)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Установка Studio Agent: {error}", file=sys.stderr)
        return 1
    print(f"Studio Agent установлен. Меню: {command} menu", flush=True)
    if sys.stdin.isatty() and not args.no_menu:
        os.execl(str(command), str(command), "menu")
    return 0


if __name__ == "__main__":
    sys.exit(main())
