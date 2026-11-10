"""Самостоятельный stdlib-хелпер: дождаться штатного выхода и запустить подготовленную копию."""
import atexit
import ctypes
import json
import os
import re
import shutil
import stat
import subprocess
import time
import uuid
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


RELEASE_NAME = re.compile(r'release-[A-Za-z0-9_-]{1,64}')
LEASE_NAME = re.compile(r'lease-[0-9]+-[0-9a-f]{32}')
_RUNTIME_LEASES = []


class ProcessReleases(set):
    def __init__(self, names=(), *, complete=True):
        super().__init__(names)
        self.complete = complete


def _lock_file(path, *, create=False, gate=False):
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                   wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    # FILE_SHARE_NONE; OPEN_REPARSE_POINT не позволяет открыть цель подставленной ссылки.
    disposition = 1 if create else 4 if gate else 3  # CREATE_NEW / OPEN_ALWAYS / OPEN_EXISTING
    handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, disposition, 0x00200080, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel, handle


def release_runtime_lease(token):
    kernel, handle = token
    kernel.CloseHandle(handle)
    if token in _RUNTIME_LEASES:
        _RUNTIME_LEASES.remove(token)


def _close_runtime_leases():
    for token in list(_RUNTIME_LEASES):
        release_runtime_lease(token)


atexit.register(_close_runtime_leases)


def acquire_runtime_lease(stage):
    """Каждый новый runtime держит собственный Windows-lock до выхода процесса, без прав администратора."""
    if _managed_location(stage) is None:
        return None
    base = _managed_base(stage)
    if base is None:
        raise OSError('managed release unavailable')
    identity = _identity(stage)
    if identity is None:
        raise OSError('managed release changed')
    gate = _gate_lock(base, stage, identity)
    try:
        if _managed_base(stage) != base or _identity(stage) != identity:
            raise OSError('managed release changed')
        folder = stage / '_runtime_leases'
        folder.mkdir(exist_ok=True)
        if _identity(folder) is None:
            raise OSError('invalid lease directory')
        token = _lock_file(folder / f'lease-{os.getpid()}-{uuid.uuid4().hex}', create=True)
        try:
            marker = stage / '_runtime_lease_version'
            if marker.exists():
                info = marker.lstat()
                if _reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_size != 1 or marker.read_text('ascii') != '1':
                    raise OSError('invalid lease marker')
            else:
                with marker.open('x', encoding='ascii') as output:
                    output.write('1')
        except BaseException:
            release_runtime_lease(token)
            raise
        _RUNTIME_LEASES.append(token)
        return token
    finally:
        release_runtime_lease(gate)


def _leases_inactive(item, *, partial=False):
    """Проверяется сам OS-lock, а не PID: закрытый процесс и повторно выданный PID не путаются."""
    marker = item / '_runtime_lease_version'
    try:
        try:
            info = marker.lstat()
            if _reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_size != 1 or marker.read_text('ascii') != '1':
                return False
        except FileNotFoundError:
            if not partial:
                return False
        folder = item / '_runtime_leases'
        try:
            if _identity(folder) is None:
                return False
        except FileNotFoundError:
            return partial
        leases = list(folder.iterdir())
        if not leases or len(leases) > 512:
            return partial and not leases
        for lease in leases:
            if not LEASE_NAME.fullmatch(lease.name) or not _empty_regular(lease):
                return False
            token = _lock_file(lease)
            token[0].CloseHandle(token[1])
        return True
    except (OSError, ValueError):
        return False


def _reparse(info):
    return bool(stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _managed_location(stage):
    local = os.environ.get('LOCALAPPDATA')
    if not local or not Path(local).is_absolute() or not RELEASE_NAME.fullmatch(stage.name):
        return None
    base = Path(local) / 'Programs/studio-agent/releases'
    if os.path.normcase(os.path.abspath(stage.parent)) != os.path.normcase(os.path.abspath(base)):
        return None
    return base


def _managed_base(stage):
    base = _managed_location(stage)
    if base is None:
        return None
    local = Path(os.environ['LOCALAPPDATA'])
    try:
        for path in (local, local / 'Programs', base.parent, base, stage):
            info = path.lstat()
            if _reparse(info) or not stat.S_ISDIR(info.st_mode):
                return None
        return base.resolve() if stage.resolve().parent == base.resolve() else None
    except OSError:
        return None


def _identity(path):
    info = path.lstat()
    if _reparse(info) or not stat.S_ISDIR(info.st_mode) or not info.st_ino:
        return None
    return info.st_dev, info.st_ino


def _intent(base, item, identity):
    # Отдельный пустой файл переживает частичный rmtree; file id не разрешает удалить подменённый каталог.
    return base / f'.prune-{item.name}.{identity[0]:x}.{identity[1]:x}'


def _gate_path(base, item, identity):
    return base / f'.gate-{item.name}.{identity[0]:x}.{identity[1]:x}'


def _gate_lock(base, item, identity):
    path = _gate_path(base, item, identity)
    token = _lock_file(path, gate=True)
    if not _empty_regular(path):
        release_runtime_lease(token)
        raise OSError('invalid cleanup gate')
    return token


def _empty_regular(path):
    try:
        info = path.lstat()
        return not _reparse(info) and stat.S_ISREG(info.st_mode) and info.st_size == 0
    except OSError:
        return False


def _confirmed(item):
    marker = item / '_update_result'
    try:
        info = marker.lstat()
        if _reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_size > 32:
            return False
        return marker.read_text(encoding='ascii').strip() == 'complete'
    except (OSError, ValueError):
        return False


def _safe_tree(root):
    """Не удаляем даже саму ссылку/junction внутри выпуска; нечитаемое дерево также сохраняем."""
    pending = [root]
    try:
        while pending:
            with os.scandir(pending.pop()) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    if _reparse(info):
                        return False
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(Path(entry.path))
    except OSError:
        return False
    return True


def running_releases(base):
    """CIM только читает процессы; наружу выходят имена выпусков, без команд, путей или аргументов."""
    script = r'''
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$releaseBase = ([Console]::In.ReadToEnd() | ConvertFrom-Json).Replace('/', '\').TrimEnd('\')
$releasePattern = [regex]::Escape($releaseBase) + '\\(release-[A-Za-z0-9_-]{1,64})(?:\\|$)'
$releaseNames = @()
$unknown = $false
foreach ($processInfo in @(Get-CimInstance Win32_Process -ErrorAction Stop)) {
    if ($processInfo.Name -in @('python.exe', 'pythonw.exe') -and -not $processInfo.CommandLine) { $unknown = $true }
    foreach ($value in @($processInfo.ExecutablePath, $processInfo.CommandLine)) {
        if (-not $value) { continue }
        foreach ($releaseMatch in [regex]::Matches($value.Replace('/', '\'), $releasePattern, 'IgnoreCase')) {
            $releaseNames += $releaseMatch.Groups[1].Value.ToLowerInvariant()
        }
    }
}
[Console]::Write((@{ unknown = $unknown; releases = @($releaseNames | Sort-Object -Unique) } | ConvertTo-Json -Compress))
'''
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                input=json.dumps(str(base), ensure_ascii=True), encoding='utf-8', capture_output=True,
                                timeout=15, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
        if len(result.stdout) > 8192:
            return None
        data = json.loads(result.stdout)
        if not isinstance(data, dict):
            return None
        names = data.get('releases')
        if type(data.get('unknown')) is not bool or not isinstance(names, list) or not all(
                isinstance(name, str) and RELEASE_NAME.fullmatch(name) for name in names):
            return None
        return ProcessReleases((name.casefold() for name in names), complete=not data['unknown'])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None  # отсутствие достоверного снимка процессов запрещает уборку


def _candidates(stage: Path, plan: dict, autostart: str | None = '', *, active=None):
    """План без удаления: только подтверждённые или ранее помеченные для повторной уборки выпуски."""
    base = _managed_base(stage)
    if (base is None or autostart is None or not isinstance(plan, dict)
            or not isinstance(plan.get('old_repo'), str) or not Path(plan['old_repo']).is_absolute()):
        return []
    active = running_releases(base) if active is None else active
    if active is None:
        return []
    keep = {os.path.normcase(os.path.realpath(path)) for path in (stage, plan['old_repo'])}
    command = os.path.normcase(os.path.expandvars(autostart).replace('/', os.sep))
    candidates = []
    for item in sorted(base.iterdir()):
        try:
            identity = _identity(item) if RELEASE_NAME.fullmatch(item.name) else None
            if (identity is None or os.path.normcase(os.path.realpath(item)) in keep
                    or item.name.casefold() in active or os.path.normcase(str(item)) + os.sep in command
                    or not (_confirmed(item) or _empty_regular(_intent(base, item, identity)))
                    or not _safe_tree(item)):
                continue
            intent = _intent(base, item, identity)
            leased_intent = intent.with_name(intent.name + '.leased')
            if not getattr(active, 'complete', True) and not _leases_inactive(item, partial=_empty_regular(leased_intent)):
                continue
            candidates.append((item, identity))
        except OSError:
            continue
    return candidates


def prune_candidates(stage: Path, plan: dict, autostart: str | None = '', *, active=None) -> list[Path]:
    """Read-only план с теми же проверками, что непосредственно перед уборкой."""
    return [item for item, _ in _candidates(stage, plan, autostart, active=active)]


def prune(stage: Path, plan: dict, autostart: str | None = '', *, processes=None) -> list[Path]:
    """Текущий выпуск, old_repo, автозапуск и живые процессы сохраняем; частичная ошибка допускает повтор."""
    base = _managed_base(stage)
    if base is None or autostart is None:
        return []
    processes = processes or running_releases
    active = processes(base)
    if active is None:
        return []
    removed = []
    for item, expected_identity in _candidates(stage, plan, autostart, active=active):
        gate = None
        gate_path = _gate_path(base, item, expected_identity)
        try:
            gate = _gate_lock(base, item, expected_identity)
            active = processes(base)  # перед удалением — свежий снимок, после возможного долгого обхода
            if active is None:
                break
            identity = _identity(item)
            if (item.name.casefold() in active or identity != expected_identity or _managed_base(stage) != base
                    or item.resolve().parent != base or not _safe_tree(item)):
                continue
            intent = _intent(base, item, identity)
            leased_intent = intent.with_name(intent.name + '.leased')
            leased = _leases_inactive(item, partial=_empty_regular(leased_intent))
            tracked = (item / '_runtime_lease_version').exists() or (item / '_runtime_leases').exists() or _empty_regular(leased_intent)
            if (not getattr(active, 'complete', True) or tracked) and not leased:
                continue
            if leased and not leased_intent.exists():
                with leased_intent.open('xb'):
                    pass
            if intent.exists():
                if not _empty_regular(intent):
                    continue
            else:
                with intent.open('xb'):
                    pass
            # Абсолютная цель проверена внутри управляемого releases; ошибок удаления не скрываем.
            shutil.rmtree(item)
            removed.append(item)
            if _empty_regular(intent):
                intent.unlink()
            if _empty_regular(leased_intent):
                leased_intent.unlink()
        except OSError:
            continue  # intent остаётся вне частично удалённой папки для следующего обновления
        finally:
            if gate is not None:
                release_runtime_lease(gate)
            if item in removed and _empty_regular(gate_path):
                try:
                    gate_path.unlink()
                except OSError:
                    pass
    return removed


def _autostart():
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\CurrentVersion\Run',
                            0, winreg.KEY_QUERY_VALUE) as key:
            value, _ = winreg.QueryValueEx(key, 'StudioAgent')
            return value if isinstance(value, str) else None
    except FileNotFoundError:
        return ''
    except OSError:
        return None


def cleanup_started(stage: Path) -> bool:
    """Новая версия повторяет уборку после подтверждения старого helper; False — подтверждение ещё ожидается."""
    if _managed_base(stage) is None:
        return True
    try:
        if not (stage / '_update_result').exists():
            return False
        if not _confirmed(stage):
            return True
        path = stage / '_update_plan.json'
        info = path.lstat()
        if _reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
            return True
        plan = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(plan, dict):
            prune(stage, plan, _autostart())
    except (OSError, ValueError):
        pass
    return True


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
            autostart = None
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as k:
                    value, _ = winreg.QueryValueEx(k, 'StudioAgent')
                    autostart = value if isinstance(value, str) else None
                    old = Path(plan['old_repo'])
                    expected = f'"{old / ".venv/Scripts/pythonw.exe"}" "{old / "studio_agent.pyw"}"'
                    if isinstance(value, str) and os.path.normcase(value.strip()) == os.path.normcase(expected):
                        command = f'"{stage / ".venv/Scripts/pythonw.exe"}" "{stage / "studio_agent.pyw"}"'
                        winreg.SetValueEx(k, 'StudioAgent', 0, winreg.REG_SZ, command)
                        autostart = command
            except FileNotFoundError:
                autostart = ''
            except OSError:
                pass  # значение автозапуска неизвестно — prune откажется от удаления
            try:
                prune(stage, plan, autostart)
            except OSError:
                pass  # уборка не мешает подтверждённому обновлению
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
