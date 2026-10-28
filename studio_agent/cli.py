"""Claude Code на машине: `claude.exe` расширения VS Code, его версия, модели и усилия; окружение моста."""

import asyncio
import contextlib
import logging
import os
import re
import subprocess
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from . import VERSION, limits

log = logging.getLogger(__name__)

VSCODE_EXTENSIONS = Path.home() / ".vscode" / "extensions"
EXTENSION = re.compile(r"anthropic\.claude-code-(\d+(?:\.\d+)*)-win32-x64")
BUILTIN = "встроенный в SDK"
VERSION_SEC = 30
VERSION_MAX = 64
PROBE_SEC = 60
DISCONNECT_SEC = 10
MODELS_MAX = 50
LABEL_MAX = 100
SETTINGS = ["user", "project", "local"]
ENV = {"CLAUDE_AGENT_SDK_CLIENT_APP": f"studio-agent/{VERSION}"}  # решение плана 4: только это
# Привязка к сессии того, кто запустил мост (Claude Code, терминал VS Code): ходам она чужая.
SESSION_ENV = frozenset({"CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
                         "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_SSE_PORT", "CLAUDE_PID", "CLAUDE_CODE_EXECPATH",
                         "CLAUDE_PLUGIN_DATA", "CLAUDE_EFFORT", "TRACEPARENT", "CLAUDE_CODE_INVOKED_SKILLS",
                         "ELECTRON_RUN_AS_NODE"})
SESSION_PREFIXES = ("CLAUDE_CODE_MESSAGING_", "VSCODE_", "CODEX_COMPANION_")
# git VS Code: помощники в его папке; без `VSCODE_GIT_IPC_HANDLE` (он уходит с `VSCODE_*`) они не работают.
VSCODE_GIT = frozenset({"GIT_ASKPASS", "GIT_EDITOR"})
VSCODE_MARKS = ("\\resources\\app\\extensions\\git\\", "vs code", "vscode")


def _vscode_path(value: str) -> bool:
    v = value.lower().replace("/", "\\")
    return any(mark in v for mark in VSCODE_MARKS)


def find_cli(extensions: Path = VSCODE_EXTENSIONS) -> Path | None:
    """`claude.exe` новейшего расширения; версии — числами (2.1.312 > 2.1.99), папка без файла пропускается."""
    found = []
    with contextlib.suppress(OSError):
        for entry in extensions.iterdir():
            match = EXTENSION.fullmatch(entry.name)
            exe = entry / "resources" / "native-binary" / "claude.exe"
            if match and exe.is_file():
                found.append((tuple(int(part) for part in match.group(1).split(".")), exe))
    return max(found, key=lambda item: item[0])[1] if found else None


def current(path: Path | None, find=find_cli) -> Path | None:
    """Прежний `claude.exe`, пока файл есть; иначе новейший заново (VS Code обновил расширение и удалил прежнюю
    папку — как `worker.py::_claude_bin`); нет — `None`, CLI встроенный в SDK. Блокирует — звать в потоке."""
    if path is not None and path.is_file():
        return path
    return find()


def drop_session_env(environ=os.environ) -> int:
    """Убрать из окружения моста привязку к чужой сессии (`SESSION_ENV`, `SESSION_PREFIXES`, `TERM_PROGRAM=vscode`,
    `GIT_ASKPASS`/`GIT_EDITOR` из папки VS Code); прочее окружение владельца (`ANTHROPIC_*`, настройки `CLAUDE_CODE_*`)
    не трогается. Итог — сколько убрано."""
    names = [k for k, v in environ.items() if k.upper() in SESSION_ENV or k.upper().startswith(SESSION_PREFIXES)
             or (k.upper() == "TERM_PROGRAM" and v.lower() == "vscode")
             or (k.upper() in VSCODE_GIT and _vscode_path(v))]
    for k in names:
        del environ[k]
    return len(names)


def cli_version(path: Path | None) -> str:
    """Первое слово `<cli> --version`; нет CLI или сбой — CLI встроенный в SDK."""
    if path is None:
        return BUILTIN
    try:
        r = subprocess.run([str(path), "--version"], capture_output=True, stdin=subprocess.DEVNULL,
                           timeout=VERSION_SEC, creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return BUILTIN
    words = r.stdout.decode("utf-8", "replace").split()
    if r.returncode != 0 or not words:
        return BUILTIN
    return words[0][:VERSION_MAX]


def models_from(info: dict | None) -> tuple[list[dict], list[str]]:
    """`models` ответа `get_server_info` → модели `{"id", "label"}` (без `default`) и усилия в порядке EFFORTS."""
    raw = info.get("models") if isinstance(info, dict) else None
    models, efforts = [], set()
    for m in raw if isinstance(raw, list) else []:
        if not isinstance(m, dict):
            continue
        value = m.get("value")
        if value == "default" or not isinstance(value, str) or not limits.MODEL_ID.fullmatch(value):
            continue
        if len(models) < MODELS_MAX:
            label = m.get("displayName")
            models.append({"id": value, "label": (label if isinstance(label, str) and label else value)[:LABEL_MAX]})
        levels = m.get("supportedEffortLevels")
        if isinstance(levels, list):
            efforts.update(level for level in levels if isinstance(level, str))
    return models, [e for e in limits.EFFORTS if e in efforts]


def _drop(_line: str) -> None:
    """stderr Claude Code — прочь: в журнал он не идёт."""


async def probe(root: str, cli_path: Path | None, factory=ClaudeSDKClient) -> tuple[list[dict], list[str]]:
    """Модели и усилия: короткий клиент в папке проектов (≤ 60 с); сбой — `([], [])`, в журнал — имя типа."""
    client = None

    async def ask():
        await client.connect()
        return await client.get_server_info()

    try:
        client = factory(options=ClaudeAgentOptions(cwd=root, cli_path=cli_path, setting_sources=SETTINGS,
                                                    env=dict(ENV), stderr=_drop))
        info = await asyncio.wait_for(ask(), PROBE_SEC)
    except Exception as e:
        log.warning("модели Claude Code: %s", type(e).__name__)
        return [], []
    finally:
        if client is not None:  # и при сбое: иначе claude.exe останется
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.disconnect(), DISCONNECT_SEC)
    return models_from(info)
