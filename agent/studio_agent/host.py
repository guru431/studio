"""Платформенные пути, окружение и запуск дочерних процессов."""
import os
import ctypes
import errno
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

MACOS = sys.platform == "darwin"
LINUX = sys.platform == "linux"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def awake_clock(platform_name: str = sys.platform):
    """Монотонные часы, которые идут и во сне компьютера, — для сроков простоя (PIN-сессия, аренда рабочего стола).

    `time.monotonic` на Linux (`CLOCK_MONOTONIC`) и macOS (`mach_absolute_time`) во сне стоит: сессия, оставленная
    перед закрытием крышки, утром была бы ещё открыта. Linux — `CLOCK_BOOTTIME`, macOS — `CLOCK_MONOTONIC`
    (по `clock_gettime(3)` macOS идёт и во сне); Windows — `time.monotonic`, он сон уже учитывает. Перевод
    настенных часов на все три не действует."""
    if platform_name == "linux" and hasattr(time, "CLOCK_BOOTTIME"):
        return lambda: time.clock_gettime(time.CLOCK_BOOTTIME)
    if platform_name == "darwin" and hasattr(time, "CLOCK_MONOTONIC"):
        return lambda: time.clock_gettime(time.CLOCK_MONOTONIC)
    return time.monotonic


def platform_id() -> str:
    return "darwin" if MACOS else "linux" if LINUX else "win32"


def xdg_home(variable: str, default: str) -> Path:
    value = Path(os.environ.get(variable) or default)
    return value if value.is_absolute() else Path.home() / default


def data_home() -> Path:
    if MACOS:
        return Path.home() / "Library/Application Support/Studio Agent"
    if LINUX:
        return xdg_home("XDG_DATA_HOME", ".local/share") / "studio-agent"
    return Path(os.environ["LOCALAPPDATA"]) / "studio-agent"


def install_home() -> Path:
    if MACOS or LINUX:
        return data_home() / "app"
    return Path(os.environ["LOCALAPPDATA"]) / "Programs/studio-agent"


def python(repo: Path) -> Path:
    return repo / (".venv/bin/python" if MACOS or LINUX else ".venv/Scripts/pythonw.exe")


def vscode_platform() -> str:
    if MACOS or LINUX:
        arch = platform.machine().lower()
        return platform_id() + "-" + {"aarch64": "arm64", "x86_64": "x64", "amd64": "x64"}.get(arch, arch)
    return "win32-x64"


def prepare_path() -> None:
    """LaunchAgent не получает PATH терминала; добавляем обычные пользовательские установки."""
    if MACOS or LINUX:
        extra = [Path.home() / ".local/bin", Path.home() / ".cargo/bin", Path.home() / ".opencode/bin",
                 Path("/usr/local/bin")]
        if MACOS:
            extra.insert(-1, Path("/opt/homebrew/bin"))
        paths = [p for p in (os.environ.get("PATH") or os.defpath).split(os.pathsep) if os.path.isabs(p)]
        os.environ["PATH"] = os.pathsep.join(dict.fromkeys([*map(str, extra), *paths]))


def find_tool(name: str) -> Path | None:
    """Executable или Unix shebang; shell не используется."""
    prepare_path()
    found = shutil.which(name)
    return Path(found) if found else None


def process_group() -> dict:
    return {"start_new_session": True} if MACOS or LINUX else {}


def open_file(path: Path) -> None:
    if MACOS or LINUX:
        subprocess.Popen(["/usr/bin/open" if MACOS else "xdg-open", str(path)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        os.startfile(path)


def rename(source: Path, target: Path) -> None:
    """Без перезаписи даже при гонке между проверкой пути и перемещением."""
    if LINUX and not MACOS:
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            renameat2 = libc.renameat2
        except AttributeError:
            raise OSError(errno.ENOTSUP, "atomic no-replace rename unavailable") from None
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        if renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1):  # AT_FDCWD, RENAME_NOREPLACE
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        return
    if not MACOS:
        source.rename(target)
        return
    lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    lib.renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    lib.renamex_np.restype = ctypes.c_int
    if lib.renamex_np(os.fsencode(source), os.fsencode(target), 0x4):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
