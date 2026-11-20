"""Готовый Developer ID .app устанавливается целиком, без переподписи на компьютере."""
import hashlib
from contextlib import contextmanager
import os
from pathlib import Path
import platform
import plistlib
import re
import runpy
import shutil
import subprocess
import sys
import tempfile
import time

NAME = 'Studio Agent.app'
ID = 'com.studio.agent'
MAX_PKG = 2 << 30
READY_POLLS = 1800  # по 0,5 с — до 15 минут, как `update_helper.READY_POLLS`


class RolledBack(RuntimeError):
    """Новая копия вышла до готовности: прежнее приложение возвращено на место."""


class NotReady(RuntimeError):
    """Готовности не дождались: новая копия на месте и может ещё подняться — итог неизвестен."""


def info(app):
    with (app / 'Contents/Info.plist').open('rb') as source:
        return plistlib.load(source)


def sealed_app(repo):
    if repo.parts[-3:] == ('Contents', 'Resources', 'agent'):
        app = repo.parents[2]
        if app.name == NAME and info(app).get('StudioSigningTeam'):
            return app
    return None


def verify(app, team, version, *, capture=False):
    if not re.fullmatch(r'[A-Z0-9]{10}', team):
        raise ValueError('Некорректный Apple Team ID')
    data = info(app)
    if (app.is_symlink() or data.get('CFBundleIdentifier') != ID or data.get('StudioSigningTeam') != team
            or data.get('CFBundleShortVersionString') != version or (capture and not data.get('StudioPersistentCapture'))):
        raise ValueError('Не совпали приложение, версия или команда подписи')
    requirement = (f'=identifier "{ID}" and anchor apple generic and certificate leaf[subject.OU] = "{team}" '
                   'and certificate 1[field.1.2.840.113635.100.6.2.6] exists '
                   'and certificate leaf[field.1.2.840.113635.100.6.1.13] exists')
    subprocess.run(['/usr/bin/codesign', '--verify', '--strict', '--all-architectures', '-R', requirement, str(app)],
                   check=True, timeout=180)
    if data.get('StudioPersistentCapture'):
        if not (app / 'Contents/embedded.provisionprofile').is_file():
            raise ValueError('Нет профиля постоянного доступа')
        for arch in ('x86_64', 'arm64'):
            raw = subprocess.check_output(['/usr/bin/codesign', '-d', '--arch', arch, '--entitlements', ':-', str(app)],
                                          stderr=subprocess.DEVNULL, timeout=20)
            grants = plistlib.loads(raw)
            if (grants.get('com.apple.developer.persistent-content-capture') is not True
                    or grants.get('com.apple.developer.team-identifier') != team):
                raise ValueError('Нет разрешения на постоянный захват в подписи ' + arch)


def ready_marker(value):
    """Маркер единственного подготовленного выпуска, вне подписанных ресурсов .app."""
    file = Path(value)
    releases = Path.home() / 'Library/Application Support/Studio Agent/app/releases'
    return (file.name == '_update_ready' and file.parent.parent == releases
            and file.parent.name.startswith('release-') and file.parent.is_dir()
            and file.parent.resolve(strict=True) == file.parent)


def candidate(stage):
    return stage / 'expanded/bridge.pkg/Scripts' / NAME


def managed_app(app):
    return app.name == NAME and app.parent in (Path('/Applications'), Path.home() / 'Applications')


def interpreter(app):
    return app / 'Contents/Resources/runtime' / platform.machine() / 'bin/python3'


@contextmanager
def _lock(path):
    import fcntl
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def replace_app(source, target, team, version, *, wait_ready):
    """Копия и проверка на том же томе до переключения. Храним прежний пакет до готовности."""
    home = Path.home() / 'Library/Application Support/Studio Agent'
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = home / ('.instance-' + hashlib.sha256(b'Local\\StudioAgent').hexdigest()[:16])
    with _lock(home / '.signed-install.lock'):
        _replace_app(source, target, team, version, lock, wait_ready)


def _replace_app(source, target, team, version, lock, wait_ready):
    target.parent.mkdir(parents=True, exist_ok=True)
    marker = wait_ready / '_update_ready'
    if not ready_marker(str(marker)) or os.path.lexists(marker):
        raise ValueError('Неверный маркер запуска')
    work = Path(tempfile.mkdtemp(prefix='.studio-agent-', dir=target.parent))
    old = work / 'previous.app'
    pending = work / NAME
    keep_backup = False
    try:
        subprocess.run(['/usr/bin/ditto', str(source), str(pending)], check=True, timeout=600)
        with _lock(lock):
            if target.is_symlink():
                raise ValueError('Нельзя заменить ссылку на приложение')
            previous = {}
            if target.exists():
                previous = info(target)
                if previous.get('CFBundleIdentifier') != ID or previous.get('StudioSigningTeam', team) != team:
                    raise ValueError('На месте установки другое приложение')
            verify(pending, team, version, capture=bool(previous.get('StudioPersistentCapture')))
            if target.exists():
                target.rename(old)
                keep_backup = True
            try:
                pending.rename(target)
            except BaseException:
                if old.exists():
                    old.rename(target)
                raise
        def restore_after_exit():
            with _lock(lock):
                target.rename(pending)
                if old.exists():
                    old.rename(target)

        try:
            process = subprocess.Popen(['/usr/bin/open', '-n', '-W', '--env', 'STUDIO_AGENT_UPDATE_READY=' + str(marker),
                                        str(target)])
        except OSError:
            restore_after_exit()
            raise
        for _ in range(READY_POLLS):  # пока новая копия жива — холодный диск и проверка подписи бывают долгими
            if marker.is_file():
                keep_backup = False
                return
            if process.poll() is not None:
                restore_after_exit()
                raise RolledBack('Новый агент завершился до готовности')
            time.sleep(0.5)
        raise NotReady('Ожидание запуска истекло; предыдущий пакет сохранён')
    finally:
        if not keep_backup or not old.exists():
            shutil.rmtree(work)


def _finish(stage, result):
    """Выпуск после перехода. Неудача — от него остаётся папка с одним `_update_result`: прежняя копия при запуске
    сообщает итог студии (`update_helper.failed_update`), уборка выпусков потом удаляет и её."""
    if result is None:
        shutil.rmtree(stage)
        return
    try:
        for child in stage.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        (stage / '_update_result').write_text(result, encoding='ascii')
    except OSError:
        shutil.rmtree(stage, ignore_errors=True)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == '--install':
        source = Path(sys.argv[2]).resolve(strict=True)
        data = info(source)
        team, version = data['StudioSigningTeam'], data['CFBundleShortVersionString']
        verify(source, team, version)
        sys.path.insert(0, str(source / 'Contents/Resources/agent'))
        from studio_agent import host, macos_autostart

        target = macos_autostart.application_directory() / NAME
        releases = host.install_home() / 'releases'
        releases.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='release-', dir=releases) as stage:
            replace_app(source, target, team, version, wait_ready=Path(stage))
        macos_autostart.set_enabled(True, target / 'Contents/Resources/agent', app=target)
    elif len(sys.argv) == 5 and sys.argv[1] == '--update':
        stage = Path(sys.argv[2])
        if (not ready_marker(str(stage / '_update_ready'))
                or Path(sys.executable).resolve() != interpreter(candidate(stage)).resolve(strict=True)):
            raise ValueError('Хелпер запущен вне подготовленного выпуска')
        target = sealed_app(Path(__file__).resolve().parents[1])
        if target is None or not managed_app(target):
            raise ValueError('Неизвестный путь приложения')
        # Не сдаваться через 120 с: прежний агент может выходить дольше, а после него не запустился бы никто.
        wait_exit = runpy.run_path(str(Path(__file__).with_name('update_helper.py')))['wait_exit']
        result = None
        try:
            if not wait_exit(int(sys.argv[3])):
                result = 'old_running'
                raise BlockingIOError('Предыдущий агент продолжает работу')
            replace_app(candidate(stage), target, info(target)['StudioSigningTeam'], sys.argv[4], wait_ready=stage)
        except BlockingIOError:
            raise  # уже запущен другой экземпляр
        except Exception as error:
            if not isinstance(error, NotReady):
                result = 'rolled_back' if isinstance(error, RolledBack) else 'failed'
            # open без -n поднимет текущий экземпляр при неподтверждённом запуске.
            subprocess.run(['/usr/bin/open', str(target)], check=False, timeout=15)
            raise
        finally:
            _finish(stage, result)
    else:
        raise ValueError('Неизвестное действие')


if __name__ == '__main__':
    main()
