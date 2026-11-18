"""Переход из старого Python-launcher до запуска GUI и любых заданий."""
import ctypes
import os
from pathlib import Path


def executable():
    library = ctypes.CDLL(None)
    library._NSGetExecutablePath.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    library._NSGetExecutablePath.restype = ctypes.c_int
    size = ctypes.c_uint32(0)
    library._NSGetExecutablePath(None, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value)
    if library._NSGetExecutablePath(buffer, ctypes.byref(size)) != 0:
        raise OSError('executable')
    return Path(os.fsdecode(buffer.value)).resolve()


def ensure_native(repo):
    """Старый обновлятор может запустить новый выпуск напрямую: заменяем этот же процесс."""
    if executable().name == 'studio-agent':
        return
    if not (repo / 'macos/studio-agent').is_file():
        return  # исходники без подготовленного launcher запускаются до сборки PKG как прежде
    from install_macos import bundle
    from . import host
    from .macos_autostart import application_directory, open_arguments
    app = bundle(host.install_home(), application_directory())
    command = open_arguments(repo, os.environ, app=app)
    os.execv(command[0], command)
