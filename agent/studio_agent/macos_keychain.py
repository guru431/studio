"""Ключ Fernet в Keychain пользователя: секретов нет в argv, stdout или открытых файлах."""
import ctypes
import functools
import hashlib
import os
import threading

from cryptography.fernet import Fernet, InvalidToken

from .host import data_home

SERVICE = b"com.studio.agent.secrets.v1"
_LOCK = threading.Lock()


@functools.lru_cache(maxsize=1)
def _security():
    lib = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    ptr, uint = ctypes.c_void_p, ctypes.c_uint32
    lib.SecKeychainFindGenericPassword.argtypes = [ptr, uint, ptr, uint, ptr,
                                                  ctypes.POINTER(uint), ctypes.POINTER(ptr), ctypes.POINTER(ptr)]
    lib.SecKeychainAddGenericPassword.argtypes = [ptr, uint, ptr, uint, ptr, uint, ptr, ctypes.POINTER(ptr)]
    lib.SecKeychainItemFreeContent.argtypes = [ptr, ptr]
    for name in ("SecKeychainFindGenericPassword", "SecKeychainAddGenericPassword", "SecKeychainItemFreeContent"):
        getattr(lib, name).restype = ctypes.c_int32
    return lib


def _account() -> bytes:
    home = os.path.realpath(os.environ.get("STUDIO_AGENT_HOME") or data_home())
    return hashlib.sha256(os.fsencode(home)).hexdigest().encode("ascii")


def _read_existing(account: bytes) -> bytes:
    lib, size, data = _security(), ctypes.c_uint32(), ctypes.c_void_p()
    code = lib.SecKeychainFindGenericPassword(None, len(SERVICE), SERVICE, len(account), account,
                                             ctypes.byref(size), ctypes.byref(data), None)
    if code:
        raise OSError(code, "Keychain unavailable")
    try:
        return ctypes.string_at(data, size.value)
    finally:
        lib.SecKeychainItemFreeContent(None, data)


@functools.lru_cache(maxsize=8)
def _key(account: bytes) -> bytes:
    with _LOCK:
        try:
            return _read_existing(account)
        except OSError as error:
            if error.errno != -25300:  # errSecItemNotFound; отказ/блокировка не заменяют существующий ключ
                raise
        key = Fernet.generate_key()
        code = _security().SecKeychainAddGenericPassword(None, len(SERVICE), SERVICE, len(account), account,
                                                        len(key), key, None)
        if code == -25299:  # другой процесс успел создать: перечитать
            return _read_existing(account)
        if code:
            raise OSError(code, "Keychain unavailable")
        return key


def protect(data: bytes) -> bytes:
    return Fernet(_key(_account())).encrypt(data)


def unprotect(blob: bytes) -> bytes:
    try:
        return Fernet(_key(_account())).decrypt(blob)
    except (InvalidToken, ValueError) as error:
        raise OSError("invalid protected data") from error
