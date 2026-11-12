"""Обновление без остановки заданий: подготовить отдельную копию, дождаться простоя, выйти штатно."""
import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import VERSION, store, update_helper
from .errors import Refused

log = logging.getLogger(__name__)
VERSION_RE = re.compile(r'[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}')
PACKAGE_MAX = 16 * 1024 * 1024
UNPACKED_MAX = 64 * 1024 * 1024
REPO = Path(__file__).resolve().parent.parent


async def cleanup_releases(*, repo=None, sleep=asyncio.sleep):
    """Уборка новым кодом после подтверждённого запуска; helper перехода ещё может быть старой версии."""
    stage = Path(repo) if repo is not None else REPO
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


def prepare(h: Path, release: dict) -> Path:
    """Секрет — только в заголовке HTTPS. Новая среда остаётся на окончательном пути: venv не переносим."""
    cfg, key = store.load_config(h), store.load_key(h)
    if not key:
        raise ValueError('key')
    url = package_url(cfg.url)
    base = Path(os.environ['LOCALAPPDATA']) / 'Programs' / 'studio-agent' / 'releases'
    base.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='release-', dir=base))
    try:
        archive = stage / 'package.zip'
        request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + key})
        digest, size = hashlib.sha256(), 0
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=30) as response, archive.open('wb') as output:
            while block := response.read(128 * 1024):
                size += len(block)
                if size > release['size']:
                    raise ValueError('size')
                digest.update(block)
                output.write(block)
        if size != release['size'] or digest.hexdigest() != release['sha256']:
            raise ValueError('checksum')
        unpack(archive, stage, release['version'])
        archive.unlink()
        # Хелпер берём из уже работающей доверенной копии; он не импортирует пакет обновления.
        shutil.copyfile(REPO / 'studio_agent' / 'update_helper.py', stage / '_update_helper.py')
        env = os.environ.copy()
        env.pop('PYTHONPATH', None)
        env.pop('PYTHONHOME', None)
        commands = [
            [sys._base_executable, '-m', 'venv', str(stage / '.venv')],
            [str(stage / '.venv/Scripts/python.exe'), '-m', 'pip', 'install', '--disable-pip-version-check',
             '-q', '-r', str(stage / 'requirements.txt')],
            [str(stage / '.venv/Scripts/python.exe'), '-c', 'import studio_agent.app'],
        ]
        for command in commands:
            subprocess.run(command, cwd=stage, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW,
                           timeout=300, check=True)
        return stage
    except BaseException:
        discard(stage)
        raise


def discard(stage: Path) -> None:
    """Только собственная новая папка `release-*` в `releases`. Работающую/предыдущую копию не трогаем."""
    base = Path(os.environ['LOCALAPPDATA']) / 'Programs' / 'studio-agent' / 'releases'
    if stage.resolve().parent == base.resolve() and stage.name.startswith('release-'):
        shutil.rmtree(stage, ignore_errors=True)


def launch(stage: Path, h: Path) -> None:
    """Отдельный процесс ждёт завершения старого моста; ключей/PIN в аргументах и файле плана нет."""
    plan = {'pid': os.getpid(), 'old_repo': str(REPO), 'old_python': sys.executable, 'home': str(h)}
    (stage / '_update_plan.json').write_text(json.dumps(plan), encoding='utf-8')
    subprocess.Popen([sys.executable, str(stage / '_update_helper.py')], cwd=stage,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)


class Updater:
    """`holds()` — что держит ожидание: только числа (`tasks`, `asking`, `requests`) и `desktop`, без имён и текстов."""

    def __init__(self, h: Path, active, quit, *, prepare_fn=prepare, launch_fn=launch, sleep=asyncio.sleep,
                 holds=None, discard_fn=discard):
        self.h, self.active, self.quit = h, active, quit
        self.prepare, self.launch, self.sleep = prepare_fn, launch_fn, sleep
        self.holds, self.discard = holds, discard_fn
        self.state, self.version = 'idle', None
        self.task = None
        self._gen = 0  # отмена меняет поколение: подготовка и ожидание прежнего запуска сами уходят

    @property
    def busy(self) -> bool:
        return self.state in ('preparing', 'waiting', 'restarting')

    def status(self) -> dict:
        status = {'state': self.state, 'version': self.version}
        if self.state == 'waiting' and self.holds is not None:
            status['holds'] = self.holds()
        return status

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
        self.state, self.version = 'preparing', version
        self._gen += 1
        self.task = asyncio.create_task(self._run({'version': version, 'sha256': digest, 'size': size}, self._gen))
        return self.status()

    async def _run(self, release: dict, gen: int) -> None:
        stage = None
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
            self.quit()
        except Exception as error:
            if gen == self._gen:
                self.state = 'failed'
            log.warning('обновление: %s', type(error).__name__)
        finally:
            if stage is not None and gen != self._gen:  # отменено: подготовленная копия не нужна
                await asyncio.to_thread(self.discard, stage)
