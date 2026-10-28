"""Один мост на учётку Windows: именованный мьютекс (автозапуск и ручной запуск не держат два канала)."""

import ctypes
from ctypes import wintypes

NAME = "Local\\StudioAgent"
ERROR_ALREADY_EXISTS = 183

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
_kernel32.CloseHandle.restype = wintypes.BOOL


class Mutex:
    """Держится до `close()` или выхода процесса."""

    def __init__(self, handle: int):
        self.handle = handle

    def close(self) -> None:
        if self.handle:
            _kernel32.CloseHandle(self.handle)
            self.handle = None


def acquire(name: str = NAME) -> Mutex | None:
    """Мьютекс наш — `Mutex`; уже есть у другого экземпляра — `None`."""
    handle = _kernel32.CreateMutexW(None, False, name)
    error = ctypes.get_last_error()
    if not handle:
        raise ctypes.WinError(error)
    if error == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return None
    return Mutex(handle)
