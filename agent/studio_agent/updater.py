"""Обновление без остановки заданий: подготовить отдельную копию, дождаться простоя, выйти штатно."""
import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import VERSION, host, macos_signed, store, update_helper

if not host.MACOS and not host.LINUX:
    from . import winacl
from .errors import Refused

log = logging.getLogger(__name__)
VERSION_RE = re.compile(r'[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}')
PACKAGE_MAX = 16 * 1024 * 1024
UNPACKED_MAX = 64 * 1024 * 1024
PIP_INSTALL_SEC = 1800
REPO = Path(__file__).resolve().parent.parent
# Слова для Студии об итоге прошлого перехода (`update_helper.FAILED_RESULTS`): только код, без путей.
FAILED_WORDS = {
    'rolled_back': 'Новая версия моста не запустилась — работает прежняя',
    'unconfirmed': 'Новая версия моста не подтвердила запуск — работает прежняя',
    'old_running': 'Мост не завершился вовремя — обновление не применено',
    'failed': 'Программа обновления моста завершилась с ошибкой — работает прежняя версия',
}


def new_dir(parent: Path, prefix: str) -> Path:
    """Новая папка выпуска (и временная внутри него). Windows — `mkdir` без режима: ACL наследуется от каталога
    установки; `tempfile.mkdtemp` дал бы защищённый DACL по владельцу (`winacl`), и после запуска с повышением
    обычный сеанс терял бы доступ к выпуску автозапуска."""
    if host.MACOS or host.LINUX:
        return Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    for _ in range(100):
        path = parent / (prefix + secrets.token_hex(6))
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return path
    raise FileExistsError(prefix)


def repair_acl(repo: Path) -> int:
    """Запуск установленной копии Windows: выпуски `release-*` с защищённым DACL прежних версий (`mkdtemp`) снова
    наследуют ACL `releases/`. Чинит сеанс, которому DACL разрешает это: владелец выпуска или сеанс с повышением.
    Выпуск, созданный с повышением, обычному сеансу недоступен даже для чтения DACL — он пропускается (такой выпуск
    обычный сеанс и не запустил бы). Разработческая копия папку установки не трогает. Итог — число исправленных."""
    repo = Path(repo)
    if (host.MACOS or host.LINUX or update_helper._managed_location(repo) is None
            and os.path.normcase(os.path.abspath(repo)) != os.path.normcase(os.path.abspath(host.install_home()))):
        return 0
    base = update_helper._checked_base(host.install_home() / 'releases')
    if base is None:
        return 0
    fixed = 0
    for item in sorted(base.iterdir()):
        try:
            if (update_helper.RELEASE_NAME.fullmatch(item.name) and update_helper._identity(item) is not None
                    and winacl.inherit(item)):
                fixed += 1
        except OSError:
            continue
    if fixed:
        log.info('обновление: права выпусков исправлены: %d', fixed)
    return fixed


async def cleanup_releases(*, repo=None, sleep=asyncio.sleep):
    """Уборка новым кодом после подтверждённого запуска; helper перехода ещё может быть старой версии."""
    stage = Path(repo) if repo is not None else REPO
    try:
        await asyncio.to_thread(repair_acl, stage)
    except OSError:
        pass
    for _ in range(240):
        if await asyncio.to_thread(update_helper.cleanup_started, stage):
            return
        await sleep(0.5)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Не отдавать ключ устройства другому адресу, в том числе при перенаправлении сервера."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def package_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise ValueError('url')
    return url.rstrip('/') + '/api/agent/package'


def unpack(archive: Path, target: Path, version: str) -> None:
    """До записи проверяем все пути, ссылки, дубликаты и общий распакованный размер."""
    with zipfile.ZipFile(archive) as source:
        infos = source.infolist()
        if len(infos) > 3000 or sum(info.file_size for info in infos) > UNPACKED_MAX:
            raise ValueError('size')
        prefix = 'studio-agent/' if 'studio-agent/studio_agent/__init__.py' in source.namelist() else ''
        seen = set()
        members = []
        for info in infos:
            if prefix and info.filename == prefix and info.is_dir():
                continue
            if not info.filename.startswith(prefix):
                raise ValueError('path')
            relative = info.filename[len(prefix):]
            parts = relative.rstrip('/').split('/')
            name = relative.rstrip('/').casefold()
            if (not parts or any(not p or p in ('.', '..') or '\\' in p or ':' in p
                                 or p.endswith((' ', '.')) or any(ord(c) < 32 for c in p)
                                 or p.split('.')[0].upper() in
                                 {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)),
                                  *(f'LPT{i}' for i in range(1, 10))} for p in parts)
                    or name in seen or stat.S_ISLNK(info.external_attr >> 16)):
                raise ValueError('path')
            seen.add(name)
            if not (target / relative).resolve().is_relative_to(target.resolve()):
                raise ValueError('path')
            members.append((info, target / relative))
        required = {'studio_agent.pyw', 'studio_agent/__init__.py', 'requirements.txt'}
        if not required <= seen:
            raise ValueError('package')
        init = source.getinfo(prefix + 'studio_agent/__init__.py')
        if init.file_size > 4096:
            raise ValueError('version')
        match = re.search(rb'^VERSION = "([0-9.]+)"\r?$', source.read(init), re.MULTILINE)
        if match is None or match[1].decode('ascii') != version:
            raise ValueError('version')
        for info, destination in members:
            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source.open(info) as contents, destination.open('xb') as output:
                    shutil.copyfileobj(contents, output)


def _download(url, key, archive, size, digest):
    request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + key})
    checksum, received = hashlib.sha256(), 0
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(request, timeout=30) as response, archive.open('xb') as output:
        while block := response.read(128 * 1024):
            received += len(block)
            if received > size:
                raise ValueError('size')
            checksum.update(block)
            output.write(block)
    if received != size or checksum.hexdigest() != digest:
        raise ValueError('checksum')


def _prepare_signed(app, stage, archive, url, key, release):
    if not macos_signed.managed_app(app):
        raise ValueError('Для обновления установите приложение в «Программы»')
    with zipfile.ZipFile(archive) as source:
        member = source.getinfo('studio-agent/macos/signed-package.json')
        if member.file_size > 256:
            raise ValueError('signed package metadata')
        data = json.loads(source.read(member))
    size, digest = data['size'], data['sha256']
    if type(size) is not int or not 0 < size <= macos_signed.MAX_PKG or not re.fullmatch('[0-9a-f]{64}', digest):
        raise ValueError('signed package metadata')
    pkg = stage / 'agent.pkg'
    _download(url + '?platform=macos', key, pkg, size, digest)
    subprocess.run(['/usr/sbin/pkgutil', '--expand-full', str(pkg), str(stage / 'expanded')],
                   check=True, timeout=600, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    previous = macos_signed.info(app)
    macos_signed.verify(macos_signed.candidate(stage), previous['StudioSigningTeam'], release['version'],
                        capture=bool(previous.get('StudioPersistentCapture')))
    pkg.unlink()
    archive.unlink()


def prepare(h: Path, release: dict) -> Path:
    """Секрет — только в заголовке HTTPS. Новая среда остаётся на окончательном пути: venv не переносим."""
    cfg, key = store.load_config(h), store.load_key(h)
    if not key:
        raise ValueError('key')
    url = package_url(cfg.url)
    base = host.install_home() / 'releases'
    base.mkdir(parents=True, exist_ok=True)
    stage = new_dir(base, 'release-')
    try:
        archive = stage / 'package.zip'
        _download(url, key, archive, release['size'], release['sha256'])
        if host.MACOS and (app := macos_signed.sealed_app(REPO)) is not None:
            _prepare_signed(app, stage, archive, url, key, release)
            return stage
        unpack(archive, stage, release['version'])
        archive.unlink()
        # Хелпер берём из уже работающей доверенной копии; он не импортирует пакет обновления.
        if not host.LINUX:
            shutil.copyfile(REPO / 'studio_agent' / 'update_helper.py', stage / '_update_helper.py')
        if not host.LINUX and not host.MACOS:
            shutil.copyfile(REPO / 'studio_agent' / 'shortcut.py', stage / '_shortcut.py')
        if host.MACOS:
            shutil.copyfile(REPO / 'studio_agent' / 'macos_autostart.py', stage / '_macos_autostart.py')
        env = os.environ.copy()
        env.pop('PYTHONPATH', None)
        env.pop('PYTHONHOME', None)
        pythonw = str(host.python(stage))
        # venv из pythonw запускает консольный ensurepip без CREATE_NO_WINDOW. Разделяем шаги:
        # ensurepip и его дочерний pip теперь работают через pythonw и не создают консоль.
        commands = [
            [sys._base_executable, '-m', 'venv', '--without-pip', str(stage / '.venv')],
            [pythonw, '-m', 'ensurepip', '--upgrade', '--default-pip'],
            [pythonw, '-m', 'pip', 'install', '--disable-pip-version-check',
             '-q', *(['--only-binary=:all:'] if host.MACOS else []),
             '-r', str(stage / 'requirements.txt'), '-c', str(stage / 'constraints.txt')],
            [pythonw, '-c', 'import studio_agent.app'],
        ]
        temporary = str(new_dir(stage, 'prepare-'))
        try:
            env.update(TMP=temporary, TEMP=temporary, TMPDIR=temporary, PIP_CACHE_DIR=temporary)
            for step, command in enumerate(commands, 1):
                log.info('обновление: шаг %d', step)
                timeout = PIP_INSTALL_SEC if command[1:4] == ['-m', 'pip', 'install'] else 300
                subprocess.run(command, cwd=stage, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, creationflags=host.NO_WINDOW,
                               timeout=timeout, check=True)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        return stage
    except BaseException:
        discard(stage)
        raise


def discard(stage: Path) -> None:
    """Только собственная новая папка `release-*` в `releases`. Работающую/предыдущую копию не трогаем."""
    base = host.install_home() / 'releases'
    if stage.resolve().parent == base.resolve() and stage.name.startswith('release-'):
        shutil.rmtree(stage, ignore_errors=True)


def launch(stage: Path, h: Path) -> None:
    """Отдельный процесс ждёт завершения старого моста; ключей/PIN в аргументах и файле плана нет."""
    if host.LINUX:
        from .linux_service import schedule_update
        schedule_update(stage)
        return
    if host.MACOS and (app := macos_signed.sealed_app(REPO)) is not None:
        new = macos_signed.candidate(stage)
        version = macos_signed.info(new)['CFBundleShortVersionString']
        previous = macos_signed.info(app)
        macos_signed.verify(new, previous['StudioSigningTeam'], version,
                            capture=bool(previous.get('StudioPersistentCapture')))
        subprocess.Popen([str(macos_signed.interpreter(new)), '-I', '-B', str(REPO / 'studio_agent/macos_signed.py'),
                          '--update', str(stage), str(os.getpid()), version], cwd=stage,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
        return
    plan = {'pid': os.getpid(), 'old_repo': str(REPO), 'old_python': sys.executable, 'home': str(h)}
    (stage / '_update_plan.json').write_text(json.dumps(plan), encoding='utf-8')
    pythonw = sys.executable if host.MACOS else str(Path(sys.executable).with_name('pythonw.exe'))
    subprocess.Popen([pythonw, str(stage / '_update_helper.py')], cwd=stage,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     **({'start_new_session': True} if host.MACOS else
                        {'creationflags': subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}))


class Updater:
    """`holds()` — что держит ожидание: только числа (`tasks`, `asking`, `requests`) и `desktop`, без имён и текстов."""

    def __init__(self, h: Path, active, quit, *, prepare_fn=prepare, launch_fn=launch, sleep=asyncio.sleep,
                 holds=None, discard_fn=discard):
        self.h, self.active, self.quit = h, active, quit
        self.prepare, self.launch, self.sleep = prepare_fn, launch_fn, sleep
        self.holds, self.discard = holds, discard_fn
        self.state, self.version = 'idle', None
        self.reason = None  # код неудачного прошлого перехода (`FAILED_WORDS`)
        self.task = None
        self._gen = 0  # отмена меняет поколение: подготовка и ожидание прежнего запуска сами уходят

    @property
    def busy(self) -> bool:
        return self.state in ('preparing', 'waiting', 'restarting')

    def status(self) -> dict:
        status = {'state': self.state, 'version': self.version}
        if self.state == 'waiting' and self.holds is not None:
            status['holds'] = self.holds()
        if self.state == 'failed' and self.reason in FAILED_WORDS:
            status['message'] = FAILED_WORDS[self.reason]
        return status

    def report(self, result: str) -> None:
        """Итог прошлого перехода, прочитанный при запуске (`update_helper.failed_update`): `failed` со словами."""
        if result in FAILED_WORDS and not self.busy:
            self.state, self.version, self.reason = 'failed', None, result

    def cancel(self) -> dict:
        """Отмена до перезапуска: сразу `idle` (новые задания снова принимаются), подготовленная копия удаляется,
        когда её подготовка кончится. В `restarting` поздно — выход уже запрошен."""
        if self.state in ('preparing', 'waiting'):
            self._gen += 1
            self.state, self.version = 'idle', None
            log.info('обновление: отменено')
        return self.status()

    def start(self, release: dict) -> dict:
        version, digest, size = (release.get(k) for k in ('version', 'sha256', 'size'))
        if (not isinstance(version, str) or not VERSION_RE.fullmatch(version)
                or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                or type(size) is not int or not 0 < size <= PACKAGE_MAX):
            raise Refused('invalid')
        if self.busy:
            return self.status()
        if tuple(map(int, version.split('.'))) <= tuple(map(int, VERSION.split('.'))):
            return {'state': 'idle', 'version': VERSION}
        self.state, self.version, self.reason = 'preparing', version, None
        self._gen += 1
        self.task = asyncio.create_task(self._run({'version': version, 'sha256': digest, 'size': size}, self._gen))
        return self.status()

    async def _run(self, release: dict, gen: int) -> None:
        stage = None
        launched = False
        try:
            stage = await asyncio.to_thread(self.prepare, self.h, release)
            if gen != self._gen:
                return
            self.state = 'waiting'
            while self.active():
                await self.sleep(1)
                if gen != self._gen:
                    return
            self.state = 'restarting'
            # Между последней проверкой ходов и запросом штатного выхода нет await.
            self.launch(stage, self.h)
            launched = True
            self.quit()
        except Exception as error:
            if gen == self._gen:
                self.state = 'failed'
            log.warning('обновление: %s', type(error).__name__)
        finally:
            if stage is not None and not launched and (gen != self._gen or self.state == 'failed'):
                # Хелпер ещё не запущен: отменённая/неудачная подготовка больше никому не нужна.
                await asyncio.to_thread(self.discard, stage)
