"""Установка и обновление нативной оболочки до запуска GUI и заданий."""
import ctypes
import os
import plistlib
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


def _needs_resources(app, identifier):
    with (app / 'Contents/Info.plist').open('rb') as source:
        info = plistlib.load(source)
    if not isinstance(info, dict) or info.get('CFBundleIdentifier') != identifier:
        raise ValueError('invalid Studio Agent application')
    icon = info.get('CFBundleIconFile')
    purpose = info.get('NSScreenCaptureUsageDescription')
    return (not isinstance(icon, str) or not icon or Path(icon).name != icon
            or not (app / 'Contents/Resources' / icon).is_file()
            or not isinstance(purpose, str) or not purpose)


def ensure_native(repo):
    """После простоя обновить старую оболочку и сохранить маркер штатного запуска."""
    running = executable()
    native = running.name == 'studio-agent'
    if native and not os.environ.get('STUDIO_AGENT_UPDATE_READY'):
        return
    if not (repo / 'macos/studio-agent').is_file():
        return  # исходники без подготовленного launcher запускаются до сборки PKG как прежде
    from . import single
    from .macos_autostart import APP_NAME, LABEL, SYSTEM_APPLICATIONS, application_directory, open_arguments

    target = application_directory() / APP_NAME
    if native:
        installed = running.parents[2]
        if (running != installed / 'Contents/MacOS/studio-agent' or installed.name != APP_NAME
                or installed.parent not in (SYSTEM_APPLICATIONS, Path.home() / 'Applications')):
            return
        if not target.is_dir():
            target = installed

    mutex = single.acquire()
    if mutex is None:
        return
    try:
        refresh = True
        launcher = None
        if native:
            if target.is_symlink() or target.stat().st_uid != os.getuid():
                return
            refresh = _needs_resources(target, LABEL)
            if not refresh and target == installed:
                return
            launcher = target / 'Contents/MacOS/studio-agent'
        if refresh:
            from install_macos import bundle

            target = bundle(repo, target.parent, launcher=launcher)
        command = open_arguments(repo, os.environ, app=target)
    finally:
        mutex.close()
    os.execv(command[0], command)
