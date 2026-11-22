"""DeepSeek Harness ACP по stdio: поиск установок (npm, Python-пакет, Desktop), проверка Node и запуск процесса."""

import asyncio
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from . import acp, host

try:
    import winreg
except ImportError:  # не Windows
    winreg = None

PACKAGE = "@deepseek-ai/dsh"
FALLBACK = "DeepSeek Harness"
ACCESS = ("read-only", "workspace-write", "danger-full-access")
UNINSTALL = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
APP = "DeepSeek Harness"
ASAR_CLI = "app.asar/dsh/node_modules/@deepseek-ai/dsh-desktop-host/lib/cli.js"
NODE_SEC = 10
VERSION_SEC = 30
_TAGS = {"linux": "linux", "darwin": "macos", "win32": "win"}
_ARCHS = {"x86_64": "x64", "amd64": "x64", "arm64": "arm64", "aarch64": "arm64"}


@dataclass(frozen=True)
class Install:
    """Одна найденная установка. `command` — без `--profile acp`; `reason="node"` — Node не подходит по `engines`.

    `package_dir`: npm — каталог пакета `@deepseek-ai/dsh`, Desktop — корень приложения, Python-пакет — `None`."""

    kind: Literal["npm", "python", "desktop"]
    command: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    profile: Literal["web", "desktop"] = "web"
    package_dir: Path | None = None
    reason: str | None = None


def _which(name: str, environ: Mapping[str, str]) -> Path | None:
    if environ is os.environ:
        return host.find_tool(name)
    found = shutil.which(name, path=environ.get("PATH") or os.defpath)
    return Path(found) if found else None


def _node_version(node: Path) -> str:
    try:
        result = subprocess.run([str(node), "--version"], capture_output=True, stdin=subprocess.DEVNULL,
                                timeout=NODE_SEC, creationflags=host.NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.decode("utf-8", "replace").strip() if result.returncode == 0 else ""


def _triple(text: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+].*)?", text.strip())
    return tuple(int(part or 0) for part in match.groups()) if match else None


def _comparator(version: tuple[int, int, int], token: str) -> bool | None:
    """Один операнд диапазона; `None` — запись не разобрана."""
    match = re.fullmatch(r"(>=|<=|>|<|=|\^|~)?\s*(v?\d+(?:\.\d+){0,2})", token)
    if not match:
        return None
    op, base = match.group(1) or "=", _triple(match.group(2))
    if op == ">=":
        return version >= base
    if op == ">":
        return version > base
    if op == "<=":
        return version <= base
    if op == "<":
        return version < base
    if op == "=":
        return version == base
    major, minor, patch = base
    if op == "~":
        upper = (major, minor + 1, 0)
    elif major:
        upper = (major + 1, 0, 0)
    elif minor:
        upper = (0, minor + 1, 0)
    else:
        upper = (0, 0, patch + 1)
    return base <= version < upper


def node_ok(version: str, engines: str) -> bool:
    """Версия Node (`v24.9.0`) удовлетворяет `engines.node`: `>=24.9.0`, `^24.22.3 || >=26.13.1`.

    Версию не разобрали — нет; запись `engines` не разобрали — да (чужой формат не запирает рабочий DSH)."""
    current = _triple(version)
    if current is None:
        return False
    if not engines.strip():
        return True
    unknown = False
    for alternative in engines.split("||"):
        tokens = re.findall(r"(?:>=|<=|>|<|=|\^|~)?\s*v?[\w.*-]+", alternative.strip())
        results = [_comparator(current, token.replace(" ", "")) for token in tokens]
        if not results or None in results:
            unknown = True
        elif all(results):
            return True
    return unknown


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _package_of(path: Path) -> Path | None:
    """Каталог пакета `@deepseek-ai/dsh`, внутри которого лежит `path` (разрешённый путь): иначе `None`."""
    for parent in list(path.parents)[:6]:
        if _read_json(parent / "package.json").get("name") == PACKAGE:
            return parent
    return None


def _npm(package: Path, environ: Mapping[str, str]) -> Install | None:
    meta = _read_json(package / "package.json")
    bins = meta.get("bin")
    relative = bins.get("dsh") if isinstance(bins, dict) else bins if isinstance(bins, str) else None
    bin_js = package / (relative if isinstance(relative, str) and relative else "lib/bin.js")
    if not bin_js.is_file():
        return None
    engines = meta.get("engines")
    engines = engines.get("node") if isinstance(engines, dict) else ""
    node = _which("node.exe" if not (host.MACOS or host.LINUX) else "node", environ)
    ready = node is not None and node_ok(_node_version(node), engines if isinstance(engines, str) else "")
    return Install("npm", (str(node or "node"), str(bin_js)), {}, "web", package, None if ready else "node")


def _npm_windows(environ: Mapping[str, str]) -> list[Install]:
    roots = []
    if appdata := environ.get("APPDATA"):
        roots.append(Path(appdata) / "npm/node_modules")
    if prefix := environ.get("NPM_CONFIG_PREFIX") or environ.get("npm_config_prefix"):
        roots.append(Path(prefix) / "node_modules")
    for shim in ("dsh.cmd", "dsh.ps1"):
        if found := _which(shim, environ):
            roots.append(found.parent / "node_modules")
    return _packages(roots, environ)


def _packages(roots: list[Path], environ: Mapping[str, str]) -> list[Install]:
    seen, found = set(), []
    for root in roots:
        package = root / PACKAGE
        key = _key(package)
        if key in seen or not package.is_dir():
            continue
        seen.add(key)
        if install := _npm(package, environ):
            found.append(install)
    return found


def _key(path: Path) -> str:
    try:
        return os.path.normcase(str(path.resolve()))
    except OSError:
        return os.path.normcase(str(path))


def _tag() -> str | None:
    system, arch = _TAGS.get(sys.platform), _ARCHS.get(platform.machine().lower())
    return f"{system}-{arch}" if system and arch else None


def _runtime_of(launcher: Path) -> Path | None:
    """Исполняемый файл Python-пакета рядом с командой `dsh` (`deepseek-harness-runtime-bin`): venv, `--user`, pipx."""
    tag = _tag()
    if tag is None:
        return None
    name = f"deepseek-harness-sdk-runtime-{tag}" + (".exe" if tag.startswith("win-") else "")
    roots = {launcher.parent.parent}
    try:
        roots.add(launcher.resolve().parent.parent)
    except OSError:
        pass
    for root in roots:
        sites = [root / "Lib/site-packages", root / "site-packages", root / "lib/site-packages",
                 *sorted(root.glob("lib/python*/site-packages"))]
        for site in sites:
            runtime = site / "deepseek_harness_runtime"
            if (runtime / "deepseek-harness-runtime.json").is_file() and (runtime / "runtime" / name).is_file():
                return runtime / "runtime" / name
    return None


def _python(launcher: Path, environ: Mapping[str, str], home: Path) -> Install | None:
    runtime = _runtime_of(launcher)
    if runtime is None:
        return None
    # консольная команда Python-пакета без явного DSH_HOME отказывает: берём тот же каталог, что у CLI
    dsh_home = environ.get("DSH_HOME", "").strip() or str(home / ".dsh")
    return Install("python", (str(runtime),), {"DSH_HOME": dsh_home}, "web", None, None)


def _desktop(root: Path, *, mac: bool) -> Install | None:
    """Приложение Desktop в `root`: Electron в режиме RunAsNode и CLI из ресурсов (`apps/desktop/cli/dsh.cmd`)."""
    resources = root / ("Contents/Resources" if mac else "resources")
    exe = root / ("Contents/MacOS" if mac else "") / (APP if mac else f"{APP}.exe")
    if not exe.is_file() or not (resources / "app.asar").exists():  # app.asar — архив: внутрь не заглядываем
        return None
    cli = resources / ASAR_CLI
    return Install("desktop", (str(exe), "--expose-internals", str(cli)), {"ELECTRON_RUN_AS_NODE": "1"},
                   "desktop", root, None)


def _registry_locations() -> list[Path]:
    """`InstallLocation` записей удаления текущего пользователя (installer.nsh Desktop)."""
    found = []
    if winreg is None:
        return found
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL) as root:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                index += 1
                try:
                    with winreg.OpenKey(root, name) as key:
                        value, _ = winreg.QueryValueEx(key, "InstallLocation")
                except OSError:
                    continue
                if isinstance(value, str) and value.strip().strip('"'):
                    found.append(Path(value.strip().strip('"')))
    except OSError:
        pass
    return found


def _app_of(path: Path) -> Path | None:
    """`*.app`, внутри которого лежит путь (лаунчер `/usr/local/bin/dsh` → ресурсы приложения)."""
    return next((parent for parent in path.parents if parent.suffix == ".app"), None)


def _desktops(environ: Mapping[str, str], home: Path) -> list[Install]:
    if host.LINUX and not host.MACOS:
        return []  # сборка Linux — AppImage без устойчивого каталога установки
    roots = []
    if host.MACOS:
        roots = [home / "Applications" / f"{APP}.app", Path("/Applications") / f"{APP}.app"]
        if launcher := _which("dsh", environ):
            try:
                app = _app_of(launcher.resolve())
            except OSError:
                app = None
            if app is not None:
                roots.insert(0, app)
    else:
        roots = _registry_locations()
        if local := environ.get("LOCALAPPDATA"):
            roots.append(Path(local) / "Programs" / APP)
        for entry in (environ.get("PATH") or "").split(os.pathsep):
            path = Path(entry) if entry else None
            if path is not None and path.parts[-4:] == ("resources", "runtime", "cli", "bin"):
                roots.append(path.parents[3])  # лаунчер «Manage dsh Command…» в PATH пользователя
    seen, found = set(), []
    for root in roots:
        if _key(root) in seen:
            continue
        seen.add(_key(root))
        if install := _desktop(root, mac=host.MACOS):
            found.append(install)
    return found


def find_installs(*, environ: Mapping[str, str] | None = None, home: Path | None = None) -> list[Install]:
    """Все установки DSH на компьютере: npm, Python-пакет, Desktop. Чужой `dsh` (distributed shell) не попадает."""
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    npm, python = [], []
    if host.MACOS or host.LINUX:
        if (launcher := _which("dsh", environ)) is not None:
            try:
                resolved = launcher.resolve()
            except OSError:
                resolved = launcher
            if (package := _package_of(resolved)) is not None:
                npm = _packages([package.parent.parent], environ)
            elif (install := _python(launcher, environ, home)) is not None:
                python = [install]
    else:
        npm = _npm_windows(environ)
        if (launcher := _which("dsh.exe", environ)) is not None and (install := _python(launcher, environ, home)):
            python = [install]
    return [*npm, *python, *_desktops(environ, home)]


def pick(installs: list[Install], source: str | None) -> Install | None:
    """Пригодная установка профиля-источника; без источника — npm, затем Python-пакет, затем Desktop."""
    order = ("npm", "python", "desktop")
    ready = sorted((install for install in installs if install.reason is None), key=lambda i: order.index(i.kind))
    if source is not None:
        ready = [install for install in ready if install.profile == source]
    return ready[0] if ready else None


def version(install: Install) -> str:
    try:
        result = subprocess.run([*install.command, "--version"], capture_output=True, stdin=subprocess.DEVNULL,
                                timeout=VERSION_SEC, creationflags=host.NO_WINDOW,
                                env={**os.environ, **install.env})
    except (OSError, subprocess.SubprocessError):
        return FALLBACK
    text = result.stdout.decode("utf-8", "replace").strip()[:64] if result.returncode == 0 else ""
    return text or FALLBACK


class Client(acp.Client):
    """`dsh --profile acp` по stdio; права чата — `DSH_PERMISSION_MODE` процессу, наложение настроек — `--patch`."""

    def __init__(self, install: Install, cwd: Path, *, access: str | None = None, overlay: Path | None = None,
                 spawn=asyncio.create_subprocess_exec):
        env = {**os.environ, **install.env}
        if access in ACCESS:
            env["DSH_PERMISSION_MODE"] = access
        command = [*install.command, "--profile", "acp", *(("--patch", str(overlay)) if overlay else ())]
        super().__init__(command, cwd, env=env, spawn=spawn)
        self.install = install
