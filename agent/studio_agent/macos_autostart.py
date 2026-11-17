"""LaunchAgent пользователя: запускается при следующем входе, без перезапуска работающего моста."""
import os
import plistlib
from pathlib import Path

LABEL = "com.studio.agent"


def path() -> Path:
    return Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")


def arguments(repo: Path) -> list[str]:
    launcher = Path.home() / 'Applications/Studio Agent.app/Contents/MacOS/studio-agent'
    if launcher.is_file():
        with launcher.open('rb') as source:
            if source.read(4) in (b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe', b'\xca\xfe\xba\xbe'):
                return [str(launcher), '--studio-repo', str(repo)]
    return [str(repo / ".venv/bin/python"), str(repo / "studio_agent.pyw")]


def open_arguments(repo: Path, env=None) -> list[str]:
    args = arguments(repo)
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


def set_enabled(on: bool, repo: Path) -> None:
    target = path()
    if not on:
        target.unlink(missing_ok=True)
        return
    from .atomic import write_atomic
    target.parent.mkdir(parents=True, exist_ok=True)
    data = {"Label": LABEL, "ProgramArguments": arguments(repo), "RunAtLoad": True,
            "LimitLoadToSessionType": "Aqua", "WorkingDirectory": str(repo),
            "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null"}
    if home := os.environ.get("STUDIO_AGENT_HOME"):
        data["EnvironmentVariables"] = {"STUDIO_AGENT_HOME": home}
    write_atomic(target, plistlib.dumps(data))
