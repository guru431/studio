"""LaunchAgent пользователя: запускается при следующем входе, без перезапуска работающего моста."""
import os
import plistlib
from pathlib import Path

LABEL = "com.studio.agent"
APP_NAME = "Studio Agent.app"
SYSTEM_APPLICATIONS = Path('/Applications')


def application_directory() -> Path:
    """Обычная установка доступна из «Программ», личная — из домашней папки."""
    app = SYSTEM_APPLICATIONS / APP_NAME
    if not os.environ.get('STUDIO_AGENT_HOME') and os.access(SYSTEM_APPLICATIONS, os.W_OK | os.X_OK):
        try:
            if not app.is_symlink() and app.stat().st_uid == os.getuid():
                return SYSTEM_APPLICATIONS
        except FileNotFoundError:
            return SYSTEM_APPLICATIONS
    return Path.home() / 'Applications'


def path() -> Path:
    return Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")


def arguments(repo: Path, app: Path | None = None) -> list[str]:
    candidates = (app,) if app is not None else (
        application_directory() / APP_NAME, Path.home() / 'Applications' / APP_NAME)
    for candidate in dict.fromkeys(candidates):
        launcher = candidate / 'Contents/MacOS/studio-agent'
        if launcher.is_file():
            with launcher.open('rb') as source:
                if source.read(4) in (b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe', b'\xca\xfe\xba\xbe'):
                    return [str(launcher), '--studio-repo', str(repo)]
    return [str(repo / ".venv/bin/python"), str(repo / "studio_agent.pyw")]


def open_arguments(repo: Path, env=None, *, app: Path | None = None) -> list[str]:
    args = arguments(repo, app)
    if len(args) != 3:
        return args
    command = ['/usr/bin/open', '-n', '-W']
    for name in ('STUDIO_AGENT_HOME', 'STUDIO_AGENT_UPDATE_READY'):
        if env is not None and name in env:
            command += ['--env', name + '=' + env[name]]
    return [*command, str(Path(args[0]).parents[2]), '--args', *args[1:]]


def read() -> dict:
    try:
        with path().open("rb") as source:
            data = plistlib.load(source)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, plistlib.InvalidFileException):
        return {}


def set_enabled(on: bool, repo: Path, *, app: Path | None = None) -> None:
    target = path()
    if not on:
        target.unlink(missing_ok=True)
        return
    from .atomic import write_atomic
    target.parent.mkdir(parents=True, exist_ok=True)
    home = os.environ.get("STUDIO_AGENT_HOME")
    env = {"STUDIO_AGENT_HOME": home} if home else None
    data = {"Label": LABEL, "ProgramArguments": open_arguments(repo, env, app=app), "RunAtLoad": True,
            "LimitLoadToSessionType": "Aqua", "WorkingDirectory": str(repo),
            "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null"}
    if env is not None:
        data["EnvironmentVariables"] = env
    write_atomic(target, plistlib.dumps(data))
