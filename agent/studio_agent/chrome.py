"""Документированный Chrome DevTools MCP для открытого профиля пользователя.

Не переносит контекст и подписи настольного Codex. Chrome сам подтверждает подключение.
"""

import json
import shutil
from pathlib import Path

PACKAGE = "chrome-devtools-mcp@1.12.0"


def config(project, which=shutil.which):
    node, npx = which("node.exe"), which("npx.cmd")
    if not node or not npx:
        return None
    entry = Path(npx).parent / "node_modules/npm/bin/npx-cli.js"
    if not entry.is_file():
        return None
    return {"command": node, "args": [str(entry), "-y", PACKAGE, "--autoConnect", "--no-usage-statistics",
            "--no-performance-crux", f"--filesystemRoot={project}"], "startup_timeout_sec": 90,
            "env": {"CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS": "1"}}


def overrides(project):
    value = config(project)
    if value is None:
        return []
    # CUA browser служит приложению Codex и требует его host. Нативный Computer Use (@oai/sky)
    # остаётся в node_repl; для Chrome в этом процессе объявлен поддерживаемый прямой MCP.
    values = {"mcp_servers.studio_chrome": value,
              "mcp_servers.cua_repl": {"command": value["command"], "enabled": False}}
    result = []
    for key, item in values.items():
        # inline TOML; аргументы передаются процессу списком, через shell не проходят.
        encoded = _toml(item)
        result.extend(["-c", f"{key}={encoded}"])
    return result


def _toml(value):
    if isinstance(value, dict):
        return "{" + ",".join(f"{json.dumps(k)}={_toml(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_toml(v) for v in value) + "]"
    return json.dumps(value)
