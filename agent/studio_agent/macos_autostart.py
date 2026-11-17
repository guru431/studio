"""LaunchAgent пользователя: запускается при следующем входе, без перезапуска работающего моста."""
import os
import plistlib
from pathlib import Path

LABEL = "com.studio.agent"


def path() -> Path:
    return Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")


def arguments(repo: Path) -> list[str]:
    return [str(repo / ".venv/bin/python"), str(repo / "studio_agent.pyw")]


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
