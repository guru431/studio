"""Установка macOS без sudo: отдельный выпуск, среда, приложение и LaunchAgent пользователя."""
import argparse
import fcntl
import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import uuid
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
BUSY = "Дождитесь завершения заданий и выйдите из Studio Agent через меню"


def bundle(repo: Path, applications: Path, *, launcher: Path | None = None) -> Path:
    """Компилятор и codesign не могут повредить прежнее приложение при отказе."""
    applications.mkdir(parents=True, exist_ok=True)
    app = applications / "Studio Agent.app"
    if app.is_symlink():
        raise ValueError('приложение не должно быть ссылкой')
    with tempfile.TemporaryDirectory(prefix='.studio-agent-', dir=applications) as folder:
        pending = Path(folder) / 'Studio Agent.app'
        _write_bundle(pending, repo, launcher=launcher)
        previous = Path(folder) / 'previous.app'
        if app.exists():
            app.rename(previous)
        try:
            pending.rename(app)
        except BaseException:
            if previous.exists():
                previous.rename(app)
            raise
    return app


def _write_bundle(app: Path, repo: Path, *, launcher: Path | None = None) -> None:
    contents = app / "Contents"
    executable = contents / "MacOS/studio-agent"
    executable.parent.mkdir(parents=True, exist_ok=True)
    launcher = launcher if launcher is not None else SOURCE / 'macos/studio-agent'
    if launcher.is_file():
        shutil.copyfile(launcher, executable)
    else:
        # Установка из исходников требует инструменты разработчика; автономный PKG уже содержит launcher.
        subprocess.run(['/usr/bin/clang', '-framework', 'Foundation', '-I', sysconfig.get_path('include'),
                        str(SOURCE / 'macos/launcher.m'), '-o', str(executable)],
                       env=dict(os.environ, TMPDIR=str(app.parent),
                                CLANG_MODULE_CACHE_PATH=str(app.parent / 'clang-cache')),
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=120)
    executable.chmod(0o755)
    icon = contents / 'Resources/StudioAgent.icns'
    icon.parent.mkdir()
    env = dict(os.environ, TMPDIR=str(app.parent))
    env.pop('PYTHONHOME', None)
    env.pop('PYTHONPATH', None)
    subprocess.run([str(repo / '.venv/bin/python'), '-B', '-c',
                    'import sys; from studio_agent.tray import icon; icon(1024).save(sys.argv[1])', str(icon)],
                   cwd=repo, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=30)
    with (contents / "Info.plist").open("wb") as output:
        plistlib.dump({"CFBundleIdentifier": "com.studio.agent", "CFBundleName": "Studio Agent",
                       "CFBundleDisplayName": "Studio Agent",
                       "CFBundleExecutable": "studio-agent", "CFBundlePackageType": "APPL",
                       "CFBundleIconFile": icon.name,
                       "NSScreenCaptureUsageDescription": "Studio Agent shares your screen during a remote desktop session.",
                       "LSUIElement": True, "NSHighResolutionCapable": True}, output)
    subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', '--identifier', 'com.studio.agent', str(app)],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=30)


def install(base: Path, home: Path, applications: Path, *, start=True, autostart=True,
            runtime_source: Path | None = None, wheelhouse: Path | None = None) -> Path:
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
            raise RuntimeError(BUSY) from None
        releases = base / "releases"
        releases.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix="release-", dir=releases))
        shutil.copytree(SOURCE / "studio_agent", stage / "studio_agent", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for name in ("studio_agent.pyw", "requirements.txt", "constraints.txt", "install.command", "install_macos.py", "README.md"):
            shutil.copyfile(SOURCE / name, stage / name)
        if (SOURCE / 'macos').is_dir():
            shutil.copytree(SOURCE / 'macos', stage / 'macos')
        (stage / "install.command").chmod(0o755)
        interpreter = Path(sys.executable)
        if runtime_source is not None:
            # Runtime живёт отдельно от выпусков: venv обновлений продолжает ссылаться на него
            # после уборки прежнего исходного кода. Установленный Python пользователя не меняем.
            identity = (runtime_source / ".studio-runtime-id").read_text(encoding="ascii").strip()
            if len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
                raise ValueError("неверный runtime")
            runtimes = base / "runtimes"
            runtimes.mkdir(exist_ok=True)
            runtime = runtimes / identity
            if not runtime.exists():
                with tempfile.TemporaryDirectory(prefix="runtime-", dir=runtimes) as temporary:
                    pending = Path(temporary) / "python"
                    shutil.copytree(runtime_source, pending, symlinks=True)
                    pending.rename(runtime)
            interpreter = runtime / "bin/python3"
        python = stage / ".venv/bin/python"
        # pip/сборщики пишут промежуточные файлы только внутри создаваемого выпуска.
        with tempfile.TemporaryDirectory(prefix="install-", dir=stage) as temporary:
            env = dict(os.environ, TMPDIR=temporary, PIP_CACHE_DIR=temporary)
            env.pop("PYTHONHOME", None)
            env.pop("PYTHONPATH", None)
            env.pop("PYTHONPYCACHEPREFIX", None)
            env.pop("PYTHONDONTWRITEBYTECODE", None)
            subprocess.run([str(interpreter), "-m", "venv", str(stage / ".venv")], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=60)
            offline = ["--no-index", "--find-links", str(wheelhouse)] if wheelhouse is not None else []
            subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--only-binary=:all:",
                            *offline, "-r", str(stage / "requirements.txt"), "-c", str(stage / "constraints.txt")], env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=300)
            subprocess.run([str(python), "-c", "import tkinter, HIServices, studio_agent.app"], cwd=stage, env=env,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=30)
        app = bundle(stage, applications)
        # После появления внешних ссылок выпуск сохраняется и при сбое следующего шага.
        # Он уже проверен и может быть целью автозапуска; удалять его тогда нельзя.
        activated = True
        # Автозапуск включаем лишь после установки всех зависимостей; текущий процесс не перезапускается.
        sys.path.insert(0, str(stage))
        from studio_agent import macos_autostart
        if autostart:
            macos_autostart.set_enabled(True, stage, app=app)
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
    parser.add_argument("--runtime-source", type=Path)
    parser.add_argument("--wheelhouse", type=Path)
    args = parser.parse_args(argv)
    if sys.platform != "darwin" or sys.version_info < (3, 11):
        parser.error("Нужны macOS и Python 3.11+ с Tk")
    import tkinter  # понятная ошибка до копирования, если Python собран без Tk
    from studio_agent.macos_autostart import APP_NAME, application_directory

    home = Path(os.environ.get("STUDIO_AGENT_HOME") or Path.home() / "Library/Application Support/Studio Agent")
    applications = application_directory()
    try:
        install(home / "app", home, applications, start=not args.no_start, autostart=not args.no_autostart,
                runtime_source=args.runtime_source, wheelhouse=args.wheelhouse)
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as error:
        message = BUSY if isinstance(error, RuntimeError) and str(error) == BUSY else "Не удалось установить Studio Agent. Повторите установку."
        # В системный журнал установщика не попадают пути пользователя и вывод pip.
        print(message, file=sys.stderr)
        if not os.environ.get("COMMAND_LINE_INSTALL"):
            try:
                subprocess.run(["/usr/bin/osascript", "-e", 'on run argv\ndisplay alert "Studio Agent" message (item 1 of argv) as critical\nend run',
                                message], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass  # Ошибка установки уже сообщена; недоступное окно не меняет код возврата.
        return 1
    print(f"Studio Agent установлен. Откройте {applications / APP_NAME} и войдите в студию.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
