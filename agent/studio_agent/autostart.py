"""Автозапуск при входе в Windows: значение `StudioAgent` в `HKCU\\...\\Run` (Task Scheduler не трогаем)."""

import logging
import os
import time
from pathlib import Path

from . import host

if not host.MACOS and not host.LINUX:
    import winreg

    from . import shortcut, update_helper

log = logging.getLogger(__name__)
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "StudioAgent"
REPO = Path(__file__).resolve().parent.parent
REPAIR_SEC = 600
LAUNCHER_ENV = "STUDIO_AGENT_LAUNCHER"  # pid `Studio Agent.exe`: его ставит запускатель своему дочернему процессу
LAUNCHER_GRACE_SEC = 5  # антивирус убирает файл уже после завершения процесса


def command() -> str:
    """Windows: `"<repo>\\Studio Agent.exe"`, если запускатель прошёл пробу (`update_helper.launcher`), иначе
    `"<repo>\\.venv\\Scripts\\pythonw.exe" "<repo>\\studio_agent.pyw"` — без консоли."""
    if host.LINUX:
        from .linux_autostart import launcher
        return str(launcher()) + " run --console"
    if host.MACOS:
        import json
        from .macos_autostart import arguments
        from .macos_signed import sealed_app
        return json.dumps(arguments(REPO, app=sealed_app(REPO)))
    exe, pythonw = update_helper.startup_commands(REPO)
    return exe if update_helper.launcher(REPO) is not None else pythonw


def _read(key: str):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            value, _ = winreg.QueryValueEx(k, VALUE)
    except OSError:
        return None
    return value if isinstance(value, str) else None


def _ours(value) -> bool:
    """Значение запускает эту копию — запускателем или `pythonw.exe` (`update_helper.startup_commands`)."""
    return isinstance(value, str) and os.path.normcase(value.strip()) in {
        os.path.normcase(item) for item in update_helper.startup_commands(REPO)}


def enabled(key: str = RUN_KEY) -> bool:
    """Значение есть и запускает эту копию; указывает на другую копию моста — не включён."""
    if host.LINUX:
        from .linux_autostart import enabled as linux_enabled
        return linux_enabled()
    if host.MACOS:
        from .macos_autostart import arguments, open_arguments, read
        from .macos_signed import sealed_app
        data = read()
        app = sealed_app(REPO)
        return data.get("ProgramArguments") in (arguments(REPO, app=app),
                                                 open_arguments(REPO, data.get("EnvironmentVariables"), app=app))
    return _ours(_read(key))


def set_enabled(on: bool, key: str = RUN_KEY) -> None:
    if host.LINUX:
        from .linux_autostart import set_enabled as linux_set
        linux_set(on)
        return
    if host.MACOS:
        from .macos_autostart import set_enabled as macos_set
        from .macos_signed import sealed_app
        macos_set(on, REPO, app=sealed_app(REPO))
        return
    if on:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, command())
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, VALUE)
    except FileNotFoundError:
        pass


def repair(key: str = RUN_KEY, link=None) -> None:
    """Старт моста Windows: автозапуск и ярлык меню «Пуск», которые запускают эту копию, — на `Studio Agent.exe`, если
    он прошёл пробу; запускатель пропал или не исполняется (антивирус) — снова на `pythonw.exe`, и мост стартует при
    входе. Ярлык заодно получает AppUserModelID (`install.ps1` его не ставит). Другую копию, выключенный автозапуск и
    отсутствующий ярлык не трогаем."""
    if host.MACOS or host.LINUX:
        return
    want = command()
    launcher = want == update_helper.startup_commands(REPO)[0]
    value = _read(key)
    if _ours(value) and os.path.normcase(value.strip()) != os.path.normcase(want):
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, want)
        log.info("автозапуск: %s", "запускатель" if launcher else "pythonw")
    try:
        current = shortcut.read(link)
    except OSError:
        return
    programs = {os.path.normcase(shortcut.target(REPO, flag)[0]) for flag in (False, True)}
    if current is not None and os.path.normcase(current[0]) in programs and shortcut.ensure(REPO, link, launcher):
        log.info("ярлык: %s", "запускатель" if launcher else "pythonw")


def watch(sleep=time.sleep) -> None:
    """Поток моста Windows: `repair` при старте и раз в `REPAIR_SEC` — антивирус может убрать exe и у работающего
    моста, а автозапуск на пропавший файл при следующем входе не запустил бы ничего."""
    if host.MACOS or host.LINUX:
        return
    while True:
        try:
            repair()
        except OSError as error:
            log.warning("автозапуск: %s", type(error).__name__)
        sleep(REPAIR_SEC)


def _wait_launcher(pid_text: str, repo: Path) -> bool:
    """Ждёт выхода запускателя этой копии (`Studio Agent.exe` рядом с `repo`) по pid из `LAUNCHER_ENV`. False — следить
    не за кем: pid не число, процесс уже вышел или это не запускатель этой копии (pid мог достаться другому)."""
    import ctypes
    from ctypes import wintypes
    try:
        pid = int(pid_text)
    except ValueError:
        return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                  ctypes.POINTER(wintypes.DWORD))
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return False
        try:
            if not os.path.samefile(buffer.value, repo / update_helper.LAUNCHER):
                return False
        except OSError:
            return False
        kernel.WaitForSingleObject(handle, 0xFFFFFFFF)  # INFINITE
        return True
    finally:
        kernel.CloseHandle(handle)


def follow_launcher(pid_text: str, quit, repo: Path = REPO, wait=_wait_launcher, sleep=time.sleep, fix=None) -> None:
    """Поток моста Windows, запущенного `Studio Agent.exe`. Мост не в задании запускателя: антивирус, завершивший exe
    перед карантином, иначе убил бы и мост, и `repair` не успел бы вернуть автозапуск и ярлык на `pythonw.exe`.
    Запускатель вышел раньше моста — через `LAUNCHER_GRACE_SEC` проба: exe на месте и исполняется — его закрыли,
    мост штатно выходит (`quit`); пропал или запрещён — `repair` сразу (автозапуск и ярлык — на `pythonw.exe`), мост
    продолжает работу: его ходы и открытые сессии не теряются."""
    if host.MACOS or host.LINUX or not wait(pid_text, repo):
        return
    sleep(LAUNCHER_GRACE_SEC)
    if update_helper.launcher(repo) is not None:
        log.info("запускатель: закрыт — мост выходит")
        quit()
        return
    log.warning("запускатель: exe пропал или не исполняется — мост продолжает работу")
    try:
        (fix or repair)()
    except OSError as error:  # `watch` повторит через REPAIR_SEC
        log.warning("автозапуск: %s", type(error).__name__)
