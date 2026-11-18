"""Версии локальных CLI и обновление штатным установщиком; адреса и команды задаёт только мост."""
import asyncio
import contextlib
import http.client
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from . import cli, codex_rpc, host, jobs, opencode_rpc
from .errors import Refused

TOOLS = {'codex': '@openai/codex', 'claude': '@anthropic-ai/claude-code', 'opencode': 'opencode-ai'}
PACKAGES = {tool: (package,) for tool, package in TOOLS.items()}
PACKAGES['opencode'] = ('opencode-ai', '@opencode/cli', '@opencode-ai/cli')
EXTENSIONS = {'codex': 'openai.chatgpt', 'claude': 'anthropic.claude-code'}
HOMEBREW = {'codex': 'codex', 'claude': 'claude-code'}
VERSION = re.compile(r'[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}')
INSTALLED_VERSION = re.compile(VERSION.pattern + r'(?:-[A-Za-z0-9][A-Za-z0-9.-]{0,48})?')
UPDATE_SEC = 300
CACHE_SEC = 600


def version(value):
    if not isinstance(value, str) or len(value) > 128:
        return None
    match = re.search(r'(?<![\w.])v?(' + INSTALLED_VERSION.pattern + r')(?![\w.+-])', value)
    return match[1] if match else None


def newer(left, right):
    if not left or not right:
        return False
    def key(value):
        core, _, pre = value.partition('-')
        return (*map(int, core.split('.')), not bool(pre))
    return key(left) > key(right)


def _json(request):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    with urllib.request.build_opener(NoRedirect).open(request, timeout=4) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError('size')
    return json.loads(raw)


def latest(tool, source, package=None):
    if source == 'homebrew':
        if package != HOMEBREW.get(tool):
            raise ValueError('package')
        data = _json('https://formulae.brew.sh/api/cask/' + package + '.json')
        value = data.get('version')
        return value if isinstance(value, str) and VERSION.fullmatch(value) else None
    if source == 'vscode':
        body = {'filters': [{'criteria': [{'filterType': 7, 'value': EXTENSIONS[tool]}]}], 'flags': 17}
        request = urllib.request.Request(
            'https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery',
            data=json.dumps(body).encode(), headers={'Content-Type': 'application/json',
                'Accept': 'application/json;api-version=7.2-preview.1'})
        releases = _json(request)['results'][0]['extensions'][0]['versions']
        candidates = [v['version'] for v in releases
            if v.get('targetPlatform', 'universal') in ('universal', host.vscode_platform())
            and VERSION.fullmatch(v.get('version', ''))
            and not any(p.get('key') == 'Microsoft.VisualStudio.Code.PreRelease' and p.get('value') == 'true'
                        for p in v.get('properties', []))]
        return max(candidates, key=lambda v: tuple(map(int, v.split('.')))) if candidates else None
    package = package or TOOLS[tool]
    if package not in PACKAGES[tool]:
        raise ValueError('package')
    data = _json('https://registry.npmjs.org/' + package.replace('/', '%2F') + '/latest')
    value = data.get('version')
    return value if isinstance(value, str) and VERSION.fullmatch(value) else None


def _manifest(path):
    try:
        data = json.loads(path.read_text('utf-8-sig'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _vscode():
    if host.MACOS:
        for root in (Path('/Applications'), Path.home() / 'Applications'):
            app = root / 'Visual Studio Code.app/Contents'
            exe, script = app / 'MacOS/Electron', app / 'Resources/app/out/cli.js'
            if exe.is_file() and script.is_file():
                return [str(exe), str(script)]
        return None
    command = shutil.which('code.cmd')
    if command:
        root = Path(command).parent.parent
        exe, script = root / 'Code.exe', root / 'resources/app/out/cli.js'
        if not script.is_file():
            # Новые установки VS Code держат resources в каталоге текущего commit hash.
            try:
                match = re.search(r'%~dp0\.\.[\\/]([0-9a-f]{6,40})[\\/]resources[\\/]app[\\/]out[\\/]cli\.js',
                                  Path(command).read_text('utf-8-sig'))
                if match:
                    script = root / match[1] / 'resources/app/out/cli.js'
            except (OSError, ValueError):
                pass
        if exe.is_file() and script.is_file():
            return [str(exe), str(script)]
    return None


def _npm():
    if host.MACOS:
        node, npm = host.find_tool('node'), host.find_tool('npm')
        return [str(node), str(npm.resolve())] if node and npm else None
    node = shutil.which('node.exe')
    if node:
        script = Path(node).parent / 'node_modules/npm/bin/npm-cli.js'
        if script.is_file():
            return [node, str(script)]
    return None


def inspect(tool):
    """Тот же поиск exe, что у ходов; для расширения показываем также версию его пакета."""
    path = {'claude': cli.find_cli, 'codex': codex_rpc.find_cli, 'opencode': opencode_rpc.find_cli}[tool]()
    source, installed, prefix, package = 'native', None, None, TOOLS[tool]
    if path is None and tool == 'claude':
        # SDK поставляет собственный CLI, обновляемый вместе с мостом.
        import claude_agent_sdk
        bundled = Path(claude_agent_sdk.__file__).parent / '_bundled' / ('claude' if host.MACOS else 'claude.exe')
        path = bundled if bundled.is_file() else None
        source = 'sdk'
    cli_version = None
    if path is not None:
        path = path.resolve()
        for parent in path.parents:
            manifest = _manifest(parent / 'package.json')
            if str(manifest.get('publisher', '')).lower() + '.' + str(manifest.get('name', '')).lower() == EXTENSIONS.get(tool):
                source, installed = 'vscode', version(manifest.get('version'))
                break
            if manifest.get('name') in PACKAGES[tool] and parent.parent.name in ('node_modules', '@openai', '@anthropic-ai', '@opencode', '@opencode-ai'):
                source, installed = 'npm', version(manifest.get('version'))
                package = manifest['name']
                modules = parent.parent if parent.parent.name == 'node_modules' else parent.parent.parent
                prefix = modules.parent
                break
            # npm может вынести платформенный пакет в соседний node_modules, вне основного пакета.
            candidate = manifest.get('name')
            if isinstance(candidate, str) and (tool == 'codex' and candidate.startswith('@openai/codex-')
                    or tool == 'opencode' and candidate.startswith(('opencode-windows-', 'opencode-darwin-'))):
                modules = parent.parent.parent if tool == 'codex' else parent.parent
                primary = _manifest(modules / TOOLS[tool] / 'package.json')
                if primary.get('name') == TOOLS[tool]:
                    source, installed, prefix = 'npm', version(primary.get('version')), modules.parent
                    break
        try:
            result = subprocess.run([str(path), '--version'], stdin=subprocess.DEVNULL,
                capture_output=True, timeout=4, creationflags=host.NO_WINDOW)
            if result.returncode == 0:
                cli_version = version(result.stdout.decode('utf-8', 'replace').strip())
        except (OSError, subprocess.SubprocessError):
            pass
    brew = None
    if host.MACOS and source == 'native' and path is not None and tool in HOMEBREW:
        for root in (Path('/opt/homebrew'), Path('/usr/local')):
            if path.is_relative_to(root / 'Caskroom' / HOMEBREW[tool]) and (root / 'bin/brew').is_file():
                source, package, brew = 'homebrew', HOMEBREW[tool], root / 'bin/brew'
                break
    native_claude = (host.MACOS and tool == 'claude' and path is not None
                     and path.is_relative_to(Path.home() / '.local/share/claude/versions'))
    bundled_opencode = (tool == 'opencode' and path is not None and
                        path.name == 'opencode-cli' and path.parent.name == 'Resources')
    reason = ('not_installed' if path is None else 'bridge' if source == 'sdk' else
              'manager' if source == 'vscode' and not _vscode() or source == 'npm' and not _npm() else
              'manual' if source == 'native' and not native_claude and (tool != 'opencode' or bundled_opencode) else None)
    return {'id': tool, 'installed': path is not None, 'version': installed or cli_version,
            'cli_version': cli_version, 'source': source, 'reason': reason,
            '_path': path, '_prefix': prefix, '_package': package, '_brew': brew}


def command(item, target):
    """Ни shell, ни полученных с сервера адресов, пакетов или аргументов."""
    tool = item['id']
    if tool not in TOOLS or not isinstance(target, str) or not VERSION.fullmatch(target):
        raise ValueError('target')
    env = dict(os.environ)
    if item['source'] == 'homebrew':
        package = HOMEBREW.get(tool)
        brew = item.get('_brew')
        if not package or item.get('_package') != package or brew not in (
                Path('/opt/homebrew/bin/brew'), Path('/usr/local/bin/brew')):
            raise ValueError('manager')
        env['HOMEBREW_NO_INSTALL_CLEANUP'] = '1'
        return [str(brew), 'upgrade', '--cask', package], env
    if item['source'] == 'native' and tool == 'claude' and item.get('reason') is None:
        if not host.MACOS or not item['_path'].is_relative_to(Path.home() / '.local/share/claude/versions'):
            raise ValueError('manager')
        return [str(item['_path']), 'update'], env
    if item['source'] == 'native' and tool == 'opencode':
        return [str(item['_path']), 'upgrade', target], env
    if item['source'] == 'vscode' and (launcher := _vscode()):
        env['ELECTRON_RUN_AS_NODE'] = '1'
        return [*launcher, '--install-extension', EXTENSIONS[tool] + '@' + target, '--force'], env
    if item['source'] == 'npm' and item['_prefix'] is not None and (launcher := _npm()):
        package = item.get('_package', TOOLS[tool])
        if package not in PACKAGES[tool]:
            raise ValueError('package')
        return [*launcher, 'install', '--global', '--prefix', str(item['_prefix']),
                package + '@' + target, '--registry=https://registry.npmjs.org', '--no-audit', '--no-fund'], env
    raise ValueError('manager')


async def install(item, target):
    args, env = await asyncio.to_thread(command, item, target)
    process = await asyncio.create_subprocess_exec(*args, env=env, cwd=str(item['_path'].parent),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=host.NO_WINDOW, **host.process_group())
    job = None
    try:
        job = jobs.attach(process.pid)
        if await asyncio.wait_for(process.wait(), UPDATE_SEC) != 0:
            raise ValueError('installer')
    finally:
        if job is not None:
            job.close()
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


class Software:
    def __init__(self, agent, *, now=time.monotonic, sleep=asyncio.sleep):
        self.agent, self.now, self.sleep = agent, now, sleep
        self.items, self.cache = {}, {}
        self.checked = None
        self.state, self.tool, self.task = 'idle', None, None
        self.lock = asyncio.Lock()

    @property
    def busy(self):
        return self.state in ('waiting', 'updating')

    async def _read(self, tool, refresh):
        try:
            item = await asyncio.to_thread(inspect, tool)
        except (OSError, ValueError):
            item = {'id': tool, 'installed': False, 'version': None, 'cli_version': None,
                    'source': 'native', 'reason': 'check', '_path': None, '_prefix': None, '_package': TOOLS[tool]}
        key = (tool, item['source'], item.get('_package', TOOLS[tool]))
        saved = self.cache.get(key)
        if refresh or saved is None or self.now() - saved[0] >= CACHE_SEC:
            try:
                value = await asyncio.to_thread(latest, *key)
            except (OSError, ValueError, KeyError, IndexError, TypeError, http.client.HTTPException):
                value = None
            self.cache[key] = (self.now(), value)
        item['latest'] = self.cache[key][1]
        item['update_available'] = newer(item['latest'], item['version'])
        item['can_update'] = bool(item['update_available'] and not item['reason'])
        return item

    def snapshot(self):
        return {'items': [{k: v for k, v in item.items() if not k.startswith('_')} for item in self.items.values()],
                'update': {'tool': self.tool, 'state': self.state}}

    async def read(self, refresh=False):
        async with self.lock:
            if not self.items or not self.busy and (refresh or self.checked is None or self.now() - self.checked >= 60):
                values = await asyncio.gather(*(self._read(tool, refresh) for tool in TOOLS))
                self.items = {item['id']: item for item in values}
                self.checked = self.now()
        return self.snapshot()

    def start(self, tool):
        if (self.busy or self.task is not None and not self.task.done() or self.agent.local_exit_pending
                or self.lock.locked() or self.agent.updater is not None and self.agent.updater.busy):
            raise Refused('busy')
        item = self.items.get(tool)
        if item is None or not item['can_update']:
            raise Refused('unsupported', 'Сначала проверьте доступные обновления программ')
        self.tool, self.state = tool, 'waiting'
        self.task = asyncio.create_task(self._run(tool, item['latest'], item['source'], item.get('_package', TOOLS[tool])))
        return self.snapshot()

    def cancel(self):
        if self.state == 'waiting':
            self.task.cancel()
            self.state = 'cancelled'
        return self.snapshot()

    async def _run(self, tool, target, source, package):
        try:
            while (self.agent._software_requests or any(backend is not None and backend.live()
                    for backend in (self.agent.runner, self.agent.codex, self.agent.opencode))):
                await self.sleep(1)
            self.state = 'updating'
            backend = {'claude': self.agent.runner, 'codex': self.agent.codex, 'opencode': self.agent.opencode}[tool]
            if backend is not None:
                await backend.close()
            item = await asyncio.to_thread(inspect, tool)
            if (not item['installed'] or item['reason'] or item['source'] != source
                    or item.get('_package', TOOLS[tool]) != package or item['version'] is None):
                raise ValueError('installation')
            # Автообновление вне моста могло опередить очередь: установленную новую версию не понижаем.
            if newer(target, item['version']):
                await install(item, target)
            item = await self._read(tool, True)
            self.items[tool] = item
            if item['version'] is None or newer(target, item['version']):
                raise ValueError('verification')
            if tool == 'claude':
                self.agent.runner.cli_path = await asyncio.to_thread(cli.find_cli)
            self.state = 'done'
        except asyncio.CancelledError:
            self.state = 'cancelled'
            raise
        except Exception:
            # Вывод установщика и исключения могут содержать пути/ключи: наружу только состояние.
            self.state = 'error'

    async def close(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.state = 'cancelled'
