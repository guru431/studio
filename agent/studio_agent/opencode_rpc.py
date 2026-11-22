"""OpenCode ACP по stdio: только локальный процесс, без TCP и журналирования содержимого."""

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

from . import acp, host
from .acp import RPC_SEC, RpcError  # noqa: F401


def find_cli() -> Path | None:
    """Настоящий exe из PATH или npm; обёртки cmd/ps1 через shell не исполняются."""
    if host.MACOS:
        if path := host.find_tool("opencode"):
            return path
        for root in (Path.home() / "Applications", Path("/Applications")):
            path = root / "OpenCode.app/Contents/Resources/opencode-cli"
            if path.is_file() and os.access(path, os.X_OK):
                return path
        return None
    if host.LINUX:
        return host.find_tool("opencode")
    if exe := shutil.which("opencode.exe"):
        return Path(exe)
    npm = Path(os.environ.get("APPDATA", "")) / "npm/node_modules"
    for pattern in ("@opencode/cli/bin/opencode.exe", "@opencode-ai/cli/bin/opencode*.exe",
                    "opencode-windows-*/bin/opencode.exe", "opencode-ai/node_modules/opencode-windows-*/bin/opencode.exe"):
        if found := sorted(npm.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True):
            return found[0]
    return None


def version(path: Path) -> str:
    result = subprocess.run([str(path), "--version"], capture_output=True, stdin=subprocess.DEVNULL,
                            timeout=RPC_SEC, creationflags=host.NO_WINDOW)
    return result.stdout.decode("utf-8", "replace").strip()[:64] if result.returncode == 0 else "OpenCode"


class Client(acp.Client):
    """`opencode acp` по stdio."""

    def __init__(self, path: Path, cwd: Path, *, spawn=asyncio.create_subprocess_exec):
        super().__init__([str(path), "acp"], cwd, spawn=spawn)
        self.path = path
