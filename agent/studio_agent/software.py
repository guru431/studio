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

from . import cli, codex_rpc, deepseek_rpc, deepseek_settings, host, jobs, limits, opencode_rpc
from .errors import Refused

TOOLS = {'codex': '@openai/codex', 'claude': '@anthropic-ai/claude-code', 'opencode': 'opencode-ai',
         'deepseek': deepseek_rpc.PACKAGE}
PACKAGES = {tool: (package,) for tool, package in TOOLS.items()}
PACKAGES['opencode'] = ('opencode-ai', '@opencode/cli', '@opencode-ai/cli')
EXTENSIONS = {'codex': 'openai.chatgpt', 'claude': 'anthropic.claude-code'}
HOMEBREW = {'codex': ('codex',), 'claude': ('claude-code', 'claude-code@latest')}
CLAUDE_RELEASES = f"https://{limits.HOSTS['claude_releases']}/claude-code-releases"
NPM = f"https://{limits.HOSTS['npm']}"
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
        # Предварительные выпуски (`0.2.2-rc.5` < `0.2.2-rc.6` < `0.2.2`) сравниваются по полям, как в semver.
        core, _, pre = value.partition('-')
        fields = tuple((0, int(part), '') if part.isdigit() else (1, 0, part) for part in pre.split('.')) if pre else ()
        return (*map(int, core.split('.')), not bool(pre), fields)
    return key(left) > key(right)


def _payload(request, limit):
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    with urllib.request.build_opener(NoRedirect).open(request, timeout=4) as response:
        raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('size')
    return raw


def _json(request):
    return json.loads(_payload(request, 4 * 1024 * 1024))


def latest(tool, source, package=None):
    if source == 'homebrew':
        if package not in HOMEBREW.get(tool, ()):
            raise ValueError('package')
        data = _json(f"https://{limits.HOSTS['homebrew']}/api/cask/" + package + '.json')
        value = data.get('version')
        return value if isinstance(value, str) and VERSION.fullmatch(value) else None
    if host.MACOS and tool == 'claude' and source in ('native', 'sdk'):
        config = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
        channel = 'stable' if _manifest(config / 'settings.json').get('autoUpdatesChannel') == 'stable' else 'latest'
        value = _payload(f'{CLAUDE_RELEASES}/{channel}', 64).decode('ascii').strip()
        return value if VERSION.fullmatch(value) else None
    if source == 'vscode':
        body = {'filters': [{'criteria': [{'filterType': 7, 'value': EXTENSIONS[tool]}]}], 'flags': 17}
        request = urllib.request.Request(
            f"https://{limits.HOSTS['marketplace']}/_apis/public/gallery/extensionquery",
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
    data = _json(NPM + '/' + package.replace('/', '%2F') + '/latest')
    value = data.get('version')
    # У DeepSeek Harness пока только предварительные выпуски: тег `latest` указывает на `-rc.N`.
    pattern = INSTALLED_VERSION if tool == 'deepseek' else VERSION
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


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
    if host.LINUX:
        code = host.find_tool('code')
        return [str(code)] if code else None
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
    if host.MACOS or host.LINUX:
        node, npm = host.find_tool('node'), host.find_tool('npm')
        return [str(node), str(npm.resolve())] if node and npm else None
    node = shutil.which('node.exe')
    if node:
        script = Path(node).parent / 'node_modules/npm/bin/npm-cli.js'
        if script.is_file():
            return [node, str(script)]
    return None


def _npm_prefix(modules):
    # Global npm uses PREFIX/lib/node_modules on Unix and PREFIX/node_modules on Windows.
    parent = modules.parent
    return parent.parent if (host.MACOS or host.LINUX) and parent.name == 'lib' else parent


def _bundled_claude():
    import claude_agent_sdk

    cli_name = 'claude' if host.MACOS or host.LINUX else 'claude.exe'
    path = Path(claude_agent_sdk.__file__).parent / '_bundled' / cli_name
    return path.resolve() if path.is_file() else None


def _inspect_deepseek():
    """Установка DeepSeek Harness, которой идут ходы (`deepseek_rpc.choose`): npm обновляется мостом, Python-пакет и
    Desktop — вручную. Пригодной нет, а установка есть — она же с причиной `node` (Node.js старше нужного)."""
    order = ('npm', 'python', 'desktop')
    try:
        found = sorted(deepseek_rpc.find_installs(), key=lambda install: order.index(install.kind))
        install = deepseek_rpc.choose(found, deepseek_settings.source(deepseek_settings.dsh_home()))
    except (OSError, ValueError):
        found, install = [], None
    result = {'id': 'deepseek', 'installed': False, 'version': None, 'cli_version': None, 'source': 'native',
              'reason': 'not_installed', '_path': None, '_prefix': None, '_package': TOOLS['deepseek'], '_brew': None}
    if not found:
        return result
    install = install or found[0]
    path = (install.package_dir or Path(install.command[0])).resolve()
    cli_version = None
    try:
        done = subprocess.run([*install.command, '--version'], stdin=subprocess.DEVNULL, capture_output=True,
                              timeout=4, creationflags=host.NO_WINDOW, env={**os.environ, **install.env})
        if done.returncode == 0:
            cli_version = version(done.stdout.decode('utf-8', 'replace').strip())
    except (OSError, subprocess.SubprocessError):
        pass
    source, installed, prefix = 'native', None, None
    modules = path.parent.parent
    if install.kind == 'npm' and modules.name == 'node_modules':
        source, prefix = 'npm', _npm_prefix(modules)
        installed = version(_manifest(path / 'package.json').get('version'))
    result.update(installed=True, version=installed or cli_version, cli_version=cli_version, source=source,
                  _path=path, _prefix=prefix)
    result['reason'] = ('node' if install.reason == 'node' else 'manager' if source == 'npm' and not _npm()
                        else 'manual' if source == 'native' else None)
    return result


def inspect(tool):
    """Тот же поиск exe, что у ходов; для расширения показываем также версию его пакета."""
    if tool == 'deepseek':
        return _inspect_deepseek()
    path = {'claude': cli.find_cli, 'codex': codex_rpc.find_cli, 'opencode': opencode_rpc.find_cli}[tool]()
    source, installed, prefix, package = 'native', None, None, TOOLS[tool]
    if path is None and tool == 'claude':
        # SDK поставляет собственный CLI, обновляемый вместе с мостом.
        path = _bundled_claude()
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
                prefix = _npm_prefix(modules)
                break
            # npm может вынести платформенный пакет в соседний node_modules, вне основного пакета.
            candidate = manifest.get('name')
            if isinstance(candidate, str) and (tool == 'codex' and candidate.startswith('@openai/codex-')
                    or tool == 'opencode' and candidate.startswith(('opencode-windows-', 'opencode-darwin-', 'opencode-linux-'))):
                modules = parent.parent.parent if tool == 'codex' else parent.parent
                primary = _manifest(modules / TOOLS[tool] / 'package.json')
                if primary.get('name') == TOOLS[tool]:
                    source, installed, prefix = 'npm', version(primary.get('version')), _npm_prefix(modules)
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
            cask = next((name for name in HOMEBREW[tool] if path.is_relative_to(root / 'Caskroom' / name)), None)
            if cask and (root / 'bin/brew').is_file():
                source, package, brew = 'homebrew', cask, root / 'bin/brew'
                break
    native_claude = ((host.MACOS or host.LINUX) and tool == 'claude' and path is not None
                     and path.is_relative_to(Path.home() / '.local/share/claude/versions'))
    bundled_opencode = (tool == 'opencode' and path is not None and
                        path.name == 'opencode-cli' and path.parent.name == 'Resources')
    reason = ('not_installed' if path is None else 'bridge' if source == 'sdk' and not host.MACOS else
              'manager' if source == 'vscode' and not _vscode() or source == 'npm' and not _npm() else
              'manual' if source == 'native' and not native_claude and (tool != 'opencode' or bundled_opencode) else None)
    return {'id': tool, 'installed': path is not None, 'version': installed or cli_version,
            'cli_version': cli_version, 'source': source, 'reason': reason,
            '_path': path, '_prefix': prefix, '_package': package, '_brew': brew}


def command(item, target):
    """Ни shell, ни полученных с сервера адресов, пакетов или аргументов."""
    tool = item['id']
    if tool not in TOOLS or not isinstance(target, str) or not (
            INSTALLED_VERSION if tool == 'deepseek' else VERSION).fullmatch(target):
        raise ValueError('target')
    env = dict(os.environ)
    if item['source'] == 'homebrew':
        package = item.get('_package')
        brew = item.get('_brew')
        if package not in HOMEBREW.get(tool, ()) or brew not in (
                Path('/opt/homebrew/bin/brew'), Path('/usr/local/bin/brew')):
            raise ValueError('manager')
        env['HOMEBREW_NO_INSTALL_CLEANUP'] = '1'
        return [str(brew), 'upgrade', '--cask', package], env
    if item['source'] == 'sdk' and tool == 'claude' and host.MACOS:
        binary = _bundled_claude()
        if binary is None or item.get('_path') != binary:
            raise ValueError('manager')
        return [str(binary), 'install', target], env
    if item['source'] == 'native' and tool == 'claude' and item.get('reason') is None:
        if not (host.MACOS or host.LINUX) or not item['_path'].is_relative_to(Path.home() / '.local/share/claude/versions'):
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
                package + '@' + target, '--registry=' + NPM, '--no-audit', '--no-fund'], env
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
        self.results = None
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
                'update': {'tool': self.tool, 'state': self.state,
                           **({'results': dict(self.results)} if self.results is not None else {})}}

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
        names = TOOLS if tool == 'all' else (tool,)
        candidates = [(name, item['latest'], item['source'], item.get('_package', TOOLS[name]))
                      for name in names if (item := self.items.get(name)) and item['can_update']
                      and not (tool == 'all' and item['source'] == 'sdk')]
        if not candidates:
            raise Refused('unsupported', 'Сначала проверьте доступные обновления программ')
        self.tool, self.state = tool, 'waiting'
        self.results = {} if tool == 'all' else None
        self.task = asyncio.create_task(self._run(candidates))
        return self.snapshot()

    def cancel(self):
        if self.state == 'waiting':
            self.task.cancel()
            self.state = 'cancelled'
        return self.snapshot()

    async def _run(self, candidates):
        try:
            while (self.agent._software_requests or getattr(self.agent, "mirror_active", False) or
                    any(backend is not None and backend.live()
                    for backend in (self.agent.runner, self.agent.codex, self.agent.opencode,
                                    self.agent.deepseek))):
                await self.sleep(1)
            self.state = 'updating'
            failed = False
            for candidate in candidates:
                try:
                    await self._update(*candidate)
                    status = 'done'
                except Exception:
                    # Установщик и исключения могут содержать пути и ключи.
                    status = 'error'
                    failed = True
                if self.results is not None:
                    self.results[candidate[0]] = status
            self.state = 'error' if failed else 'done'
        except asyncio.CancelledError:
            self.state = 'cancelled'
            raise

    async def _update(self, tool, target, source, package):
        backend = {'claude': self.agent.runner, 'codex': self.agent.codex, 'opencode': self.agent.opencode,
                   'deepseek': self.agent.deepseek}[tool]
        if backend is not None:
            await backend.close()
        item = await asyncio.to_thread(inspect, tool)
        if (not item['installed'] or item['reason'] or item['source'] != source
                or item.get('_package', TOOLS[tool]) != package or item['version'] is None):
            raise ValueError('installation')
        # Автообновление вне моста может опередить очередь.
        if newer(target, item['version']):
            await install(item, target)
        item = await self._read(tool, True)
        self.items[tool] = item
        if (item['version'] is None or newer(target, item['version'])
                or source == 'sdk' and (item['source'] != 'native' or item['reason'] is not None)):
            raise ValueError('verification')
        if tool == 'claude':
            self.agent.runner.cli_path = await asyncio.to_thread(cli.find_cli)

    async def close(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.state = 'cancelled'
