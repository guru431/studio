"""Самопроверка моста одной кнопкой — окно моста и «Сведения» компьютера в Студии (просьба `self_check`).

Проверки только читают (кроме пробного файла в собственном каталоге моста) и у каждой свой предел
`limits.SELF_CHECK_SEC`. В ответе — признаки, короткие коды, версии и числа: ни путей, ни имён, ни текстов.

Пункт — `{"id", "state", ...}`: `state` — `ok`, `warn`, `fail` или `off` (выключено владельцем или не поддерживается);
необязательные `code` (`[a-z_]`), `version`, `login` (`yes|no|unknown`), `count`/`total`, `free` (байт)."""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from functools import partial
from pathlib import Path

from . import deepseek_settings, deepseek_usage, host, limits

log = logging.getLogger(__name__)

ENGINES = ("claude", "codex", "opencode", "deepseek")
VERSION = re.compile(r"\d{1,6}(?:\.\d{1,6}){1,3}")
LOGIN_KEYS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def _version(text) -> str | None:
    match = VERSION.search(text) if isinstance(text, str) else None
    return match.group(0) if match else None


def git() -> dict:
    """`git --version` из PATH, как у чтений git моста."""
    try:
        r = subprocess.run(["git", "--version"], capture_output=True, stdin=subprocess.DEVNULL,
                           timeout=limits.SELF_CHECK_SEC, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        return {"state": "fail", "code": "missing"}
    except subprocess.TimeoutExpired:
        return {"state": "fail", "code": "timeout"}
    version = _version(r.stdout.decode("ascii", "replace"))
    return {"state": "ok", "version": version} if r.returncode == 0 and version else {"state": "fail", "code": "error"}


def _settings_env(config: Path) -> bool:
    """Вход Claude Code через `env` его настроек (`claude-switch`): только имена ключей, значения не читаются в ответ."""
    try:
        path = config / "settings.json"
        if path.stat().st_size > 512 * 1024:
            return False
        env = json.loads(path.read_text(encoding="utf-8")).get("env")
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(env, dict) and any(env.get(key) for key in LOGIN_KEYS)


def login(engine: str, environ=None, home=None) -> str:
    """Признак входа без сети: `yes` — найден файл или переменная входа, `no` — нет, `unknown` — вход может лежать
    там, где мост его не ищет (Keychain macOS, ключи провайдеров в настройках OpenCode и DeepSeek Harness)."""
    env = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    if engine == "claude":
        config = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude")
        if any(env.get(key) for key in LOGIN_KEYS) or (config / ".credentials.json").is_file() or _settings_env(config):
            return "yes"
        return "unknown" if host.MACOS else "no"
    if engine == "codex":
        found = env.get("OPENAI_API_KEY") or (Path(env.get("CODEX_HOME") or home / ".codex") / "auth.json").is_file()
        return "yes" if found else "no"
    if engine == "opencode":
        data = Path(env.get("XDG_DATA_HOME") or home / ".local/share") / "opencode/auth.json"
        try:
            return "yes" if data.stat().st_size > 2 else "unknown"
        except OSError:
            return "unknown"
    return "yes" if deepseek_usage.key(deepseek_settings.dsh_home(env), env) else "unknown"


def helper(engine: str, agent) -> dict:
    """Помощник найден и отдал каталог моделей (последняя проверка моста) и признак входа."""
    if engine == "claude":
        ready, version, code = bool(agent.models), _version(agent.cc_version), "missing"
    else:
        backend = getattr(agent, engine)
        catalog = getattr(backend, "catalog", None)
        ready = catalog is not None
        version = _version(catalog.get("version")) if ready else None
        code = "node" if getattr(backend, "unavailable", None) == "node" else "missing"
    signed = login(engine)
    item = {"version": version, "login": signed}
    if not ready:
        return {"state": "fail", "code": code, **item}
    return {"state": "warn", "code": "login", **item} if signed == "no" else {"state": "ok", **item}


def projects(roots: list[str]) -> dict:
    """Папки проектов читаются (первая запись каталога)."""
    readable = 0
    for root in roots:
        try:
            with os.scandir(root) as entries:
                next(entries, None)
            readable += 1
        except OSError:
            pass
    item = {"count": readable, "total": len(roots)}
    if not roots:
        return {"state": "fail", "code": "none", **item}
    if readable == len(roots):
        return {"state": "ok", **item}
    return {"state": "fail" if readable == 0 else "warn", "code": "unreadable", **item}


def home(h: Path) -> dict:
    """Запись в каталог моста: пробный файл создаётся и сразу удаляется (`os.access` на Windows не видит ACL)."""
    try:
        with tempfile.NamedTemporaryFile(dir=h, prefix=".selfcheck-") as probe:
            probe.write(b"1")
            probe.flush()
    except OSError:
        return {"state": "fail", "code": "read_only"}
    return {"state": "ok"}


def link(state: str) -> dict:
    return {"state": "ok"} if state == "online" else {"state": "fail", "code": re.sub(r"[^a-z_]", "", state)[:24] or "error"}


def desktop(screen) -> dict:
    """Удалённый рабочий стол: поддержка, выключатель окна моста, на macOS — разрешения (preflight, без запроса)."""
    if not screen.capable:
        return {"state": "off", "code": "unsupported"}
    if not screen.status()["enabled"]:
        return {"state": "off", "code": "disabled"}
    if host.MACOS:
        from .desktop_macos import permission_status

        status = permission_status()
        for key in ("screen", "input"):
            if status.get(key) is not True:
                return {"state": "warn", "code": key}
    return {"state": "ok"}


def disk(h: Path) -> dict:
    free = shutil.disk_usage(h).free
    return {"state": "ok" if free >= limits.LOW_DISK else "warn", **({} if free >= limits.LOW_DISK else {"code": "low"}),
            "free": free}


async def run(agent, h: Path, link_state: str, *, wall=time.time) -> dict:
    """Все проверки параллельно; не уложилась в предел — `timeout`, исключение — `error` (в журнал — имя типа)."""
    checks = [("git", git), *((engine, partial(helper, engine, agent)) for engine in ENGINES),
              ("projects", partial(projects, list(agent.cfg.projects_roots))), ("home", partial(home, h)),
              ("link", partial(link, link_state)), ("desktop", partial(desktop, agent.desktop)), ("disk", partial(disk, h))]

    async def one(name, check):
        try:
            result = await asyncio.wait_for(asyncio.to_thread(check), limits.SELF_CHECK_SEC)
        except TimeoutError:
            result = {"state": "fail", "code": "timeout"}
        except Exception as e:
            log.info("самопроверка %s: %s", name, type(e).__name__)
            result = {"state": "fail", "code": "error"}
        return {"id": name, **result}

    return {"items": list(await asyncio.gather(*(one(name, check) for name, check in checks))), "checked_at": wall()}
