"""MCP проекта: настройки владельца под DPAPI, адаптеры трёх движков.

Конфиг не попадает в репозиторий проекта и не меняет настройки установленных CLI.
Чтение и запись вызываются из рабочего потока; имена/секреты в журнал не идут.
"""

import asyncio
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from . import dpapi, store
from .errors import Refused

MAX_BYTES = 64 * 1024
MAX_SERVERS = 32
ENGINES = ("claude", "codex", "opencode")
NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _text(value, maximum=8192, *, empty=False):
    return (isinstance(value, str) and (empty or bool(value)) and len(value) <= maximum
            and not any(ord(c) < 32 or 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _mapping(value, *, headers=False):
    if not isinstance(value, dict) or len(value) > 64:
        return False
    return all(_text(k, 128) and (not headers or re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", k))
               and _text(v, empty=True) and "=" not in k for k, v in value.items())


def validate(config, engine):
    """Один переносимый формат mcpServers; неизвестные поля отклоняются, а не теряются."""
    if engine not in ENGINES or not isinstance(config, dict) or set(config) != {"mcpServers"}:
        raise Refused("invalid", "Нужен JSON с объектом mcpServers")
    servers = config["mcpServers"]
    if not isinstance(servers, dict) or len(servers) > MAX_SERVERS:
        raise Refused("invalid", "В mcpServers должно быть не больше 32 серверов")
    for name, item in servers.items():
        if not isinstance(name, str) or not NAME.fullmatch(name) or not isinstance(item, dict):
            raise Refused("invalid", "Имя MCP: латинские буквы, цифры, _ и -, до 64 знаков")
        kind = item.get("type", "http" if "url" in item else "stdio")
        if kind not in ("stdio", "http", "sse") or type(item.get("disabled", False)) is not bool:
            raise Refused("invalid", "Тип MCP: stdio, http или sse; disabled — true или false")
        common = {"type", "disabled"}
        if kind == "stdio":
            if (set(item) - common - {"command", "args", "env"}
                    or not _text(item.get("command"), 1024)
                    or not isinstance(item.get("args", []), list) or len(item.get("args", [])) > 128
                    or any(not _text(v, empty=True) for v in item.get("args", []))
                    or not _mapping(item.get("env", {}))):
                raise Refused("invalid", "Для stdio задайте command, args (список) и env (объект строк)")
        else:
            try:
                url = urlsplit(item.get("url", ""))
                valid_url = (url.scheme in ("http", "https") and url.hostname and url.port != 0
                             and url.username is None and url.password is None and not url.fragment)
            except (TypeError, ValueError):
                valid_url = False
            if (set(item) - common - {"url", "headers"} or not _text(item.get("url"))
                    or not valid_url or not _mapping(item.get("headers", {}), headers=True)):
                raise Refused("invalid", "Для HTTP/SSE задайте URL без пароля и headers (объект строк)")
            if engine == "codex" and kind == "sse":
                raise Refused("invalid", "Codex поддерживает stdio и HTTP; для SSE выберите другой движок")
    raw = json.dumps(config, ensure_ascii=True, allow_nan=False).encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise Refused("too_large", "Настройки MCP больше 64 КиБ")
    return config


def _path(home, project, engine):
    if engine not in ENGINES:
        raise Refused("invalid")
    key = hashlib.sha256(os.path.normcase(str(Path(project).resolve())).encode("utf-8")).hexdigest()
    return Path(home) / "mcp" / f"{key}-{engine}.bin"


def load(home, project, engine):
    path = _path(home, project, engine)
    try:
        if path.stat().st_size > MAX_BYTES * 2:
            raise ValueError()
        config = json.loads(dpapi.unprotect(path.read_bytes()))
    except FileNotFoundError:
        return {"mcpServers": {}}
    except (OSError, ValueError):
        raise Refused("error", "Настройки MCP не читаются на этом компьютере") from None
    return validate(config, engine)


def save(home, project, engine, config):
    validate(config, engine)
    path = _path(home, project, engine)
    path.parent.mkdir(parents=True, exist_ok=True)
    store.write_atomic(path, dpapi.protect(json.dumps(config, ensure_ascii=True).encode("utf-8")))


def claude(config):
    return {name: {k: v for k, v in item.items() if k != "disabled"}
            for name, item in config["mcpServers"].items() if not item.get("disabled")}


class ClaudeConfig:
    """SDK получает путь вместо JSON в argv; временный открытый конфиг живёт до disconnect."""

    def __init__(self, home, project):
        self.directory = None
        self.path = {}
        servers = claude(load(home, project, "claude"))
        if servers:
            root = Path(home) / "mcp"
            root.mkdir(parents=True, exist_ok=True)
            self.directory = tempfile.TemporaryDirectory(prefix="runtime-", dir=root)
            try:
                path = Path(self.directory.name) / "config.json"
                path.write_text(json.dumps({"mcpServers": servers}, ensure_ascii=True), encoding="utf-8")
                self.path = str(path)
            except BaseException:
                self.cleanup()
                raise

    def cleanup(self):
        if self.directory is not None:
            self.directory.cleanup()
            self.directory = None


def sweep(home) -> int:
    """При старте ядра, до первого хода: открытые конфиги (`runtime-*`) ходов, оборванных падением моста или
    выключением компьютера, — заголовки и `env` MCP иначе лежали бы открытым текстом без срока."""
    gone = 0
    try:
        stale = [p for p in (Path(home) / "mcp").glob("runtime-*") if p.is_dir() and not p.is_symlink()]
    except OSError:
        return 0
    for path in stale:
        shutil.rmtree(path, ignore_errors=True)
        gone += not path.exists()
    return gone


async def claude_config(project):
    """Отмена не теряет файл, который ещё создаётся в рабочем потоке."""
    work = asyncio.create_task(asyncio.to_thread(lambda: ClaudeConfig(store.home(), project)))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        try:
            config = await work
        except Exception:
            pass
        else:
            await asyncio.to_thread(config.cleanup)
        raise


def codex(config):
    result = {}
    for name, item in config["mcpServers"].items():
        value = {k: v for k, v in item.items() if k not in ("type", "disabled", "headers")}
        if "headers" in item:
            value["http_headers"] = item["headers"]
        value["enabled"] = not item.get("disabled", False)
        result[name] = value
    return {"mcp_servers": result} if result else {}


def opencode(config, info):
    capabilities = info.get("agentCapabilities", {}).get("mcpCapabilities", {})
    result = []
    for name, item in config["mcpServers"].items():
        if item.get("disabled"):
            continue
        kind = item.get("type", "http" if "url" in item else "stdio")
        if kind == "stdio":
            result.append({"name": name, "command": item["command"], "args": item.get("args", []),
                           "env": [{"name": k, "value": v} for k, v in item.get("env", {}).items()]})
        else:
            if capabilities.get(kind) is not True:
                raise Refused("unsupported", "Эта версия OpenCode не поддерживает выбранный транспорт MCP")
            result.append({"name": name, "type": kind, "url": item["url"],
                           "headers": [{"name": k, "value": v} for k, v in item.get("headers", {}).items()]})
    return result
