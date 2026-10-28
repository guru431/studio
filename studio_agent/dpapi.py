"""DPAPI области пользователя: ключ устройства и хеш PIN читает только эта учётка Windows."""

import ctypes
from ctypes import wintypes

ENTROPY = b"studio-agent\0v1"
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


_crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_args = [
    ctypes.POINTER(DATA_BLOB),  # данные
    wintypes.LPCWSTR,  # описание
    ctypes.POINTER(DATA_BLOB),  # энтропия
    ctypes.c_void_p,  # зарезервировано
    ctypes.c_void_p,  # подсказка
    wintypes.DWORD,  # флаги
    ctypes.POINTER(DATA_BLOB),  # ответ
]
_crypt32.CryptProtectData.argtypes = _args
_crypt32.CryptProtectData.restype = wintypes.BOOL
_crypt32.CryptUnprotectData.argtypes = [_args[0], ctypes.POINTER(wintypes.LPWSTR), *_args[2:]]
_crypt32.CryptUnprotectData.restype = wintypes.BOOL
_kernel32.LocalFree.argtypes = [ctypes.c_void_p]
_kernel32.LocalFree.restype = ctypes.c_void_p


def _blob(data: bytes) -> tuple[DATA_BLOB, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _call(fn, data: bytes, *mid) -> bytes:
    src, _keep = _blob(data)
    ent, _keep_ent = _blob(ENTROPY)
    out = DATA_BLOB()
    if not fn(ctypes.byref(src), *mid, ctypes.byref(ent), None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect(data: bytes) -> bytes:
    return _call(_crypt32.CryptProtectData, data, None)


def unprotect(blob: bytes) -> bytes:
    """Расшифровать; чужой или битый блоб — OSError."""
    return _call(_crypt32.CryptUnprotectData, blob, None)
