"""Самостоятельный stdlib-хелпер: дождаться штатного выхода и запустить подготовленную копию."""
import ctypes
import json
import os
import subprocess
import time
from pathlib import Path


def wait_old(pid: int, timeout: int = 120000) -> bool:
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE, без прав завершения процесса
    if not handle:
        return ctypes.get_last_error() == 87  # процесс уже вышел
    try:
        return kernel.WaitForSingleObject(handle, timeout) == 0
    finally:
        kernel.CloseHandle(handle)


def activate(stage: Path, plan: dict, *, wait=wait_old, start=subprocess.Popen, sleep=time.sleep) -> str:
    if not wait(plan['pid']):
        return 'old_running'
    env = os.environ.copy()
    env['STUDIO_AGENT_HOME'] = plan['home']
    env['STUDIO_AGENT_UPDATE_READY'] = str(stage / '_update_ready')

    def rollback():
        fallback = env.copy()
        fallback.pop('STUDIO_AGENT_UPDATE_READY', None)
        start([plan['old_python'], str(Path(plan['old_repo']) / 'studio_agent.pyw')],
              cwd=plan['old_repo'], env=fallback, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        return 'rolled_back'

    try:
        new = start([str(stage / '.venv/Scripts/pythonw.exe'), str(stage / 'studio_agent.pyw')],
                    cwd=stage, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError:
        return rollback()
    for _ in range(120):
        if (stage / '_update_ready').is_file():
            # Сохраняем прежний выбор автозапуска; чужое значение не перезаписываем.
            import winreg
            key = r'Software\Microsoft\Windows\CurrentVersion\Run'
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as k:
                    value, _ = winreg.QueryValueEx(k, 'StudioAgent')
                    old = Path(plan['old_repo'])
                    expected = f'"{old / ".venv/Scripts/pythonw.exe"}" "{old / "studio_agent.pyw"}"'
                    if isinstance(value, str) and os.path.normcase(value.strip()) == os.path.normcase(expected):
                        command = f'"{stage / ".venv/Scripts/pythonw.exe"}" "{stage / "studio_agent.pyw"}"'
                        winreg.SetValueEx(k, 'StudioAgent', 0, winreg.REG_SZ, command)
            except OSError:
                pass
            return 'complete'
        if new.poll() is not None:
            return rollback()
        sleep(0.5)
    return 'unconfirmed'


if __name__ == '__main__':
    folder = Path(__file__).resolve().parent
    try:
        plan = json.loads((folder / '_update_plan.json').read_text(encoding='utf-8'))
        result = activate(folder, plan)
    except Exception:
        result = 'failed'
    (folder / '_update_result').write_text(result, encoding='ascii')
