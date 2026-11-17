"""Установка macOS без sudo: отдельный выпуск, среда, приложение и LaunchAgent пользователя."""
import argparse
import fcntl
import hashlib
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

SOURCE = Path(__file__).resolve().parent


def bundle(base: Path, applications: Path) -> Path:
    app = applications / "Studio Agent.app"
    contents = app / "Contents"
    executable = contents / "MacOS/studio-agent"
    executable.parent.mkdir(parents=True, exist_ok=True)
    current = base / "current"
    executable.write_text("#!/bin/sh\nexec " + shlex.quote(str(current / ".venv/bin/python")) + " "
                          + shlex.quote(str(current / "studio_agent.pyw")) + "\n", encoding="utf-8")
    executable.chmod(0o755)
    with (contents / "Info.plist").open("wb") as output:
        plistlib.dump({"CFBundleIdentifier": "com.studio.agent", "CFBundleName": "Studio Agent",
                       "CFBundleExecutable": "studio-agent", "CFBundlePackageType": "APPL",
                       "LSUIElement": True, "NSHighResolutionCapable": True}, output)
    return app


def install(base: Path, home: Path, applications: Path, *, start=True, autostart=True) -> Path:
    """Работающий экземпляр не останавливаем и не меняем; настройки, PIN и чаты остаются в home."""
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = home / (".instance-" + hashlib.sha256(b"Local\\StudioAgent").hexdigest()[:16])
    lock = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    stage = None
    activated = False
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Дождитесь завершения заданий и выйдите из Studio Agent через меню") from None
        releases = base / "releases"
        releases.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="release-", dir=releases))
        shutil.copytree(SOURCE / "studio_agent", stage / "studio_agent", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("studio_agent.pyw", "requirements.txt", "constraints.txt", "install.command", "install_macos.py", "README.md"):
            shutil.copyfile(SOURCE / name, stage / name)
        (stage / "install.command").chmod(0o755)
        python = stage / ".venv/bin/python"
        subprocess.run([sys.executable, "-m", "venv", str(stage / ".venv")], check=True, timeout=60)
        # pip/сборщики пишут промежуточные файлы только внутри создаваемого выпуска.
        with tempfile.TemporaryDirectory(prefix="install-", dir=stage) as temporary:
            env = dict(os.environ, TMPDIR=temporary, PIP_CACHE_DIR=temporary)
            env.pop("PYTHONHOME", None)
            env.pop("PYTHONPATH", None)
            env.pop("PYTHONPYCACHEPREFIX", None)
            env.pop("PYTHONDONTWRITEBYTECODE", None)
            subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--only-binary=:all:",
                            "-r", str(stage / "requirements.txt"), "-c", str(stage / "constraints.txt")], env=env, check=True, timeout=300)
            subprocess.run([str(python), "-c", "import tkinter, studio_agent.app"], cwd=stage, env=env, check=True, timeout=30)
        app = bundle(base, applications)
        # После появления внешних ссылок выпуск сохраняется и при сбое следующего шага.
        # Он уже проверен и может быть целью автозапуска; удалять его тогда нельзя.
        activated = True
        # Автозапуск включаем лишь после установки всех зависимостей; текущий процесс не перезапускается.
        sys.path.insert(0, str(stage))
        from studio_agent import macos_autostart
        if autostart:
            macos_autostart.set_enabled(True, stage)
        link = base / ("current-" + uuid.uuid4().hex)
        try:
            link.symlink_to(stage, target_is_directory=True)
            os.replace(link, base / "current")
        finally:
            link.unlink(missing_ok=True)
    finally:
        os.close(lock)
        if stage is not None and not activated:
            shutil.rmtree(stage)
    if start:
        subprocess.run(["/usr/bin/open", str(app)], check=True, timeout=15)
    return stage


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-start", action="store_true")
    parser.add_argument("--no-autostart", action="store_true")
    args = parser.parse_args(argv)
    if sys.platform != "darwin" or sys.version_info < (3, 11):
        parser.error("Нужны macOS и Python 3.11+ с Tk")
    import tkinter  # понятная ошибка до копирования, если Python собран без Tk
    home = Path(os.environ.get("STUDIO_AGENT_HOME") or Path.home() / "Library/Application Support/Studio Agent")
    install(home / "app", home, Path.home() / "Applications", start=not args.no_start, autostart=not args.no_autostart)
    print("Studio Agent установлен. Откройте ~/Applications/Studio Agent.app и войдите в студию.")


if __name__ == "__main__":
    main()
