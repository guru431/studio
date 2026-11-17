"""Различия Windows и macOS: пути, окружение и запуск дочерних процессов."""
import os
import ctypes
import platform
import shutil
import subprocess
import sys
from pathlib import Path

MACOS = sys.platform == "darwin"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def data_home() -> Path:
    if MACOS:
        return Path.home() / "Library/Application Support/Studio Agent"
    return Path(os.environ["LOCALAPPDATA"]) / "studio-agent"


def install_home() -> Path:
    if MACOS:
        return data_home() / "app"
    return Path(os.environ["LOCALAPPDATA"]) / "Programs/studio-agent"


def python(repo: Path) -> Path:
    return repo / (".venv/bin/python" if MACOS else ".venv/Scripts/pythonw.exe")


def vscode_platform() -> str:
    return "darwin-" + ("arm64" if platform.machine() == "arm64" else "x64") if MACOS else "win32-x64"


def prepare_path() -> None:
    """LaunchAgent не получает PATH терминала; добавляем обычные пользовательские установки."""
    if MACOS:
        extra = [Path.home() / ".local/bin", Path.home() / ".cargo/bin", Path.home() / ".opencode/bin",
                 Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
        paths = [p for p in (os.environ.get("PATH") or os.defpath).split(os.pathsep) if p and p != "."]
        os.environ["PATH"] = os.pathsep.join(dict.fromkeys([*map(str, extra), *paths]))


def find_tool(name: str) -> Path | None:
    """Executable или Unix shebang; shell не используется."""
    prepare_path()
    found = shutil.which(name)
    return Path(found) if found else None


def process_group() -> dict:
    return {"start_new_session": True} if MACOS else {}


def open_file(path: Path) -> None:
    if MACOS:
        subprocess.Popen(["/usr/bin/open", str(path)], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        os.startfile(path)


def rename(source: Path, target: Path) -> None:
    """Без перезаписи даже при гонке: Windows MoveFile, macOS renamex_np(RENAME_EXCL)."""
    if not MACOS:
        source.rename(target)
        return
    lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    lib.renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    lib.renamex_np.restype = ctypes.c_int
    if lib.renamex_np(os.fsencode(source), os.fsencode(target), 0x4):
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))
