"""Ярлык «Studio Agent» в меню «Пуск» пользователя (Windows): та же команда и рабочая папка, что у автозапуска
HKCU Run (`autostart.command`), — `pythonw.exe` без консоли; значок — студии (`ICON`), а не Python.

Только stdlib и ctypes (COM `IShellLinkW`): `updater.prepare` копирует модуль в выпуск как `_shortcut.py`, и хелпер
обновления читает его без импорта пакета. Путь и аргументы передаются полями ярлыка, не командной строкой: в пути
Windows кавычки быть не может, аргумент — один путь в кавычках."""
import ctypes
import os
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path

NAME = 'Studio Agent.lnk'
DESCRIPTION = 'Studio Agent'
ICON = 'studio_agent/studio-agent.ico'  # в копии моста; рисунок — `tray.save_ico`
CLSID_SHELL_LINK = '{00021401-0000-0000-C000-000000000046}'
IID_SHELL_LINK = '{000214F9-0000-0000-C000-000000000046}'
IID_PERSIST_FILE = '{0000010B-0000-0000-C000-000000000046}'
RPC_E_CHANGED_MODE = -2147417850  # COM потока уже инициализирован другой моделью — пользуемся им


class _Guid(ctypes.Structure):
    _fields_ = [('a', wintypes.DWORD), ('b', wintypes.WORD), ('c', wintypes.WORD), ('d', ctypes.c_ubyte * 8)]


def path() -> Path:
    """`%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Studio Agent.lnk` — папка пользователя, без повышения."""
    return Path(os.environ['APPDATA']) / 'Microsoft/Windows/Start Menu/Programs' / NAME


def target(repo) -> tuple[str, str, str, str]:
    """(программа, аргументы, рабочая папка, значок) для копии `repo` — команда как `autostart.command` и запуск
    хелпера."""
    repo = Path(repo)
    return str(repo / '.venv/Scripts/pythonw.exe'), f'"{repo / "studio_agent.pyw"}"', str(repo), str(repo / ICON)


def _ole32():
    ole32 = ctypes.WinDLL('ole32')
    ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoCreateInstance.argtypes = (ctypes.POINTER(_Guid), ctypes.c_void_p, wintypes.DWORD,
                                       ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p))
    ole32.CoCreateInstance.restype = ctypes.HRESULT
    ole32.CLSIDFromString.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(_Guid))
    ole32.CLSIDFromString.restype = ctypes.HRESULT
    return ole32


def _guid(ole32, text):
    guid = _Guid()
    ole32.CLSIDFromString(text, ctypes.byref(guid))
    return guid


def _method(pointer, index, *argtypes):
    """Метод COM по номеру в таблице; ошибочный HRESULT — `OSError` (ctypes)."""
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    function = ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(table[index])
    return lambda *args: function(pointer, *args)


def _release(pointer):
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(table[2])(pointer)


@contextmanager
def _link():
    """(IShellLinkW, IPersistFile) в своём апартаменте потока."""
    ole32 = _ole32()
    hr = ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
    if hr not in (0, 1, RPC_E_CHANGED_MODE):
        raise ctypes.WinError(hr)
    try:
        shell = ctypes.c_void_p()
        ole32.CoCreateInstance(ctypes.byref(_guid(ole32, CLSID_SHELL_LINK)), None, 1,  # CLSCTX_INPROC_SERVER
                               ctypes.byref(_guid(ole32, IID_SHELL_LINK)), ctypes.byref(shell))
        try:
            persist = ctypes.c_void_p()
            _method(shell, 0, ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p))(
                ctypes.byref(_guid(ole32, IID_PERSIST_FILE)), ctypes.byref(persist))
            try:
                yield shell, persist
            finally:
                _release(persist)
        finally:
            _release(shell)
    finally:
        if hr in (0, 1):
            ctypes.WinDLL('ole32').CoUninitialize()


def read(link=None) -> tuple[str, str, str, str] | None:
    """(программа, аргументы, рабочая папка, значок) существующего ярлыка; нет ярлыка — None, нечитаемый —
    `OSError`. Ярлык прежних версий без значка — пустая строка значка."""
    link = Path(link) if link is not None else path()
    if not link.is_file():
        return None
    with _link() as (shell, persist):
        _method(persist, 5, wintypes.LPCWSTR, wintypes.DWORD)(str(link), 0)  # Load, STGM_READ
        values = []
        icon_index = ctypes.c_int()
        # GetPath (SLGP_RAWPATH — без подстановки), GetArguments, GetWorkingDirectory, GetIconLocation
        for index, extra, args in ((3, (ctypes.c_void_p, wintypes.DWORD), (None, 4)), (10, (), ()), (8, (), ()),
                                   (16, (ctypes.POINTER(ctypes.c_int),), (ctypes.byref(icon_index),))):
            buffer = ctypes.create_unicode_buffer(32768)
            _method(shell, index, wintypes.LPWSTR, ctypes.c_int, *extra)(buffer, len(buffer), *args)
            values.append(buffer.value)
    return tuple(values)


def write(repo, link=None) -> None:
    """Ярлык на копию `repo` (папка меню создаётся при необходимости); прежний ярлык перезаписывается."""
    link = Path(link) if link is not None else path()
    program, arguments, folder, icon = target(repo)
    link.parent.mkdir(parents=True, exist_ok=True)
    with _link() as (shell, persist):
        _method(shell, 20, wintypes.LPCWSTR)(program)        # SetPath
        _method(shell, 11, wintypes.LPCWSTR)(arguments)      # SetArguments
        _method(shell, 9, wintypes.LPCWSTR)(folder)          # SetWorkingDirectory
        _method(shell, 7, wintypes.LPCWSTR)(DESCRIPTION)     # SetDescription
        _method(shell, 17, wintypes.LPCWSTR, ctypes.c_int)(icon, 0)  # SetIconLocation
        _method(persist, 6, wintypes.LPCWSTR, wintypes.BOOL)(str(link), True)  # Save


def ensure(repo, link=None) -> bool:
    """Ярлык указывает на `repo`; нет, нечитаемый, на другую копию или без значка студии — переписать. True —
    переписан."""
    try:
        current = read(link)
    except OSError:
        current = None
    if current is not None and [os.path.normcase(value) for value in current] == [
            os.path.normcase(value) for value in target(repo)]:
        return False
    write(repo, link)
    return True
