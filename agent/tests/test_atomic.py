"""Атомарная замена: временный Windows lock не теряет прежние настройки."""
import ctypes
import sys

import pytest

from studio_agent import atomic


@pytest.mark.parametrize('code', [5, 32, 33])
def test_transient_lock_retries_keep_old_data_until_success(tmp_path, monkeypatch, code):
    path = tmp_path / 'config.json'
    path.write_bytes(b'old')
    replace = atomic.os.replace
    attempts, pauses = [], []

    def held(source, target):
        attempts.append(1)
        assert path.read_bytes() == b'old'
        if len(attempts) < 3:
            error = PermissionError('sharing')
            error.winerror = code
            raise error
        replace(source, target)

    monkeypatch.setattr(atomic, '_WINDOWS', True)
    monkeypatch.setattr(atomic.os, 'replace', held)
    monkeypatch.setattr(atomic, '_sleep', pauses.append)
    atomic.write_atomic(path, b'new')
    assert path.read_bytes() == b'new' and len(attempts) == 3
    assert pauses == [0.01, 0.02] and list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize('windows,code,attempt_count', [(True, 5, 5), (True, 123, 1), (False, 5, 1)])
def test_permanent_or_unrelated_denial_stays_an_error(tmp_path, monkeypatch, windows, code, attempt_count):
    path = tmp_path / 'config.json'
    path.write_bytes(b'old')
    attempts, pauses = [], []

    def denied(*args):
        attempts.append(1)
        error = PermissionError('denied')
        error.winerror = code
        raise error

    monkeypatch.setattr(atomic, '_WINDOWS', windows)
    monkeypatch.setattr(atomic.os, 'replace', denied)
    monkeypatch.setattr(atomic, '_sleep', pauses.append)
    with pytest.raises(PermissionError):
        atomic.write_atomic(path, b'new')
    assert len(attempts) == attempt_count and len(pauses) == attempt_count - 1
    assert path.read_bytes() == b'old' and list(tmp_path.iterdir()) == [path]


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != 'win32', reason='Windows sharing flags')
def test_windows_reader_can_release_before_atomic_replace(tmp_path, monkeypatch):
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    path = tmp_path / 'config.json'
    path.write_bytes(b'old')
    handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0x80, None)
    assert handle not in (None, ctypes.c_void_p(-1).value)

    def release(_):
        nonlocal handle
        assert path.read_bytes() == b'old'
        assert kernel.CloseHandle(handle)
        handle = None

    monkeypatch.setattr(atomic, '_sleep', release, raising=False)
    try:
        atomic.write_atomic(path, b'new')
        assert path.read_bytes() == b'new'
        assert list(tmp_path.iterdir()) == [path]
    finally:
        if handle is not None:
            kernel.CloseHandle(handle)
