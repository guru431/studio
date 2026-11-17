"""Источники версий, очередь обновлений, проверка результата и граница команд установщиков."""
import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from studio_agent import software
from studio_agent.errors import Refused
from test_agent import agent_with_pin, req, root


def item(tool='codex', **values):
    return {'id': tool, 'installed': True, 'version': '1.2.3', 'cli_version': '1.2.3', 'source': 'npm',
            'reason': None, '_path': Path('C:/npm/codex.exe'), '_prefix': Path('C:/npm'), **values}


def manager(**kwargs):
    backend = SimpleNamespace(live=lambda: {}, close=AsyncMock())
    agent = SimpleNamespace(runner=backend, codex=backend, opencode=backend, updater=None,
                            local_exit_pending=False, _software_requests=0)
    return software.Software(agent, **kwargs)


def test_latest_uses_fixed_registry_and_filters_platform_and_prerelease(monkeypatch):
    def read(request):
        if isinstance(request, str):
            assert request == 'https://registry.npmjs.org/@openai%2Fcodex/latest'
            return {'version': '1.2.10'}
        assert request.full_url == 'https://marketplace.visualstudio.com/_apis/public/gallery/extensionquery'
        assert json.loads(request.data)['filters'][0]['criteria'][0]['value'] == 'anthropic.claude-code'
        return {'results': [{'extensions': [{'versions': [
            {'version': '2.1.9', 'targetPlatform': 'win32-x64'},
            {'version': '2.1.10', 'targetPlatform': 'win32-x64'},
            {'version': '9.0.0', 'targetPlatform': 'linux-x64'},
            {'version': '8.0.0', 'properties': [{'key': 'Microsoft.VisualStudio.Code.PreRelease', 'value': 'true'}]}
        ]}]}]}

    monkeypatch.setattr(software, '_json', read)
    assert software.latest('codex', 'npm') == '1.2.10'
    assert software.latest('claude', 'vscode') == '2.1.10'
    assert software.version('codex-cli 1.2.3') == '1.2.3'
    assert software.version('1.2.3-alpha.1') == '1.2.3-alpha.1'
    assert software.newer('1.2.3', '1.2.3-alpha.1')
    assert not software.newer('1.2.2', '1.2.3-alpha.1')
    assert software.newer('1.2.10', '1.2.9')
    assert not software.newer('1.2.3', '2.0.0')


def test_homebrew_updates_only_known_casks_and_preserves_old_installations(monkeypatch):
    monkeypatch.setattr(software, '_json', lambda url: {'version': '1.2.10'} if
        url == 'https://formulae.brew.sh/api/cask/codex.json' else pytest.fail(url))
    assert software.latest('codex', 'homebrew', 'codex') == '1.2.10'
    installed = item(source='homebrew', _package='codex', _brew=Path('/opt/homebrew/bin/brew'))
    args, env = software.command(installed, '1.2.10')
    assert args == [str(Path('/opt/homebrew/bin/brew')), 'upgrade', '--cask', 'codex']
    assert env['HOMEBREW_NO_INSTALL_CLEANUP'] == '1'
    with pytest.raises(ValueError):
        software.command({**installed, '_package': 'untrusted'}, '1.2.10')
    with pytest.raises(ValueError):
        software.latest('codex', 'homebrew', '../untrusted')


def test_detects_homebrew_cask_from_the_resolved_cli_path(monkeypatch):
    path = Path('/opt/homebrew/Caskroom/codex/1.2.3/codex')
    monkeypatch.setattr(software.host, 'MACOS', True)
    monkeypatch.setattr(software.codex_rpc, 'find_cli', lambda: path)
    monkeypatch.setattr(Path, 'resolve', lambda path: path)
    monkeypatch.setattr(Path, 'is_file', lambda path: path == Path('/opt/homebrew/bin/brew'))
    monkeypatch.setattr(software.subprocess, 'run', lambda *args, **kwargs:
        SimpleNamespace(returncode=0, stdout=b'codex-cli 1.2.3'))
    result = software.inspect('codex')
    assert result['source'] == 'homebrew' and result['reason'] is None and result['_package'] == 'codex'


def test_native_claude_uses_its_installer_and_bundled_opencode_is_read_only(monkeypatch, tmp_path):
    monkeypatch.setattr(software.host, 'MACOS', True)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(software.subprocess, 'run', lambda *args, **kwargs:
        SimpleNamespace(returncode=0, stdout=b'1.2.3'))
    binary = tmp_path / '.local/share/claude/versions/1.2.3'
    monkeypatch.setattr(software.cli, 'find_cli', lambda: binary)
    result = software.inspect('claude')
    assert result['reason'] is None
    assert software.command(result, '1.2.10')[0] == [str(binary), 'update']
    bundled = tmp_path / 'OpenCode.app/Contents/Resources/opencode-cli'
    monkeypatch.setattr(software.opencode_rpc, 'find_cli', lambda: bundled)
    assert software.inspect('opencode')['reason'] == 'manual'


def test_json_lookup_refuses_redirects_and_limits_response(monkeypatch):
    handlers = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, maximum):
            return b'x' * maximum
    class Opener:
        def open(self, request, timeout):
            assert timeout == 4
            return Response()
    def build(handler):
        handlers.append(handler)
        return Opener()
    monkeypatch.setattr(software.urllib.request, 'build_opener', build)
    with pytest.raises(ValueError, match='size'):
        software._json('https://registry.npmjs.org/opencode-ai/latest')
    assert handlers[0].redirect_request(None, None, 302, '', {}, 'https://other.example') is None


@pytest.mark.parametrize('tool', ['codex', 'opencode'])
def test_detects_hoisted_npm_binary_and_preserves_install_prefix(monkeypatch, tmp_path, tool):
    modules = tmp_path / 'node_modules'
    primary = modules / software.TOOLS[tool]
    primary.mkdir(parents=True)
    (primary / 'package.json').write_text(json.dumps({'name': software.TOOLS[tool], 'version': '1.2.3'}))
    package = '@openai/codex-win32-x64' if tool == 'codex' else 'opencode-windows-x64'
    binary = modules / package / 'bin' / (tool + '.exe')
    binary.parent.mkdir(parents=True)
    binary.touch()
    (binary.parent.parent / 'package.json').write_text(json.dumps({'name': package}))
    monkeypatch.setattr(software.codex_rpc if tool == 'codex' else software.opencode_rpc, 'find_cli', lambda: binary)
    monkeypatch.setattr(software, '_npm', lambda: ['node.exe', 'npm-cli.js'])
    def run(args, **kwargs):
        assert args == [str(binary.resolve()), '--version'] and kwargs['timeout'] == 4
        assert 'shell' not in kwargs
        return SimpleNamespace(returncode=0, stdout=b'1.2.3\n')
    monkeypatch.setattr(software.subprocess, 'run', run)
    data = software.inspect(tool)
    assert data['source'] == 'npm' and data['_prefix'] == tmp_path
    command, _ = software.command(data, '1.2.10')
    assert command[:5] == ['node.exe', 'npm-cli.js', 'install', '--global', '--prefix']
    assert str(tmp_path) in command and software.TOOLS[tool] + '@1.2.10' in command
    with pytest.raises(ValueError):
        software.command(data, '1.2.10 & arbitrary-command')


def test_extension_versions_and_update_do_not_launch_shell(monkeypatch, tmp_path):
    extension = tmp_path / 'anthropic.claude-code-2.1.3-win32-x64'
    binary = extension / 'resources/native-binary/claude.exe'
    binary.parent.mkdir(parents=True)
    binary.touch()
    (extension / 'package.json').write_text(json.dumps({'name': 'claude-code', 'publisher': 'anthropic', 'version': '2.1.3'}))
    monkeypatch.setattr(software.cli, 'find_cli', lambda: binary)
    monkeypatch.setattr(software, '_vscode', lambda: ['Code.exe', 'cli.js'])
    monkeypatch.setattr(software.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=b'2.1.3 (Claude Code)'))
    data = software.inspect('claude')
    assert data['source'] == 'vscode' and data['reason'] is None and data['version'] == '2.1.3'
    command, env = software.command(data, '2.1.10')
    assert command == ['Code.exe', 'cli.js', '--install-extension', 'anthropic.claude-code@2.1.10', '--force']
    assert env['ELECTRON_RUN_AS_NODE'] == '1'


def test_vscode_versioned_resources_follow_launcher_without_shell(monkeypatch, tmp_path):
    launcher = tmp_path / 'bin/code.cmd'
    launcher.parent.mkdir()
    launcher.write_text('"%~dp0..\\Code.exe" "%~dp0..\\abcdef1234\\resources\\app\\out\\cli.js" %*')
    exe, script = tmp_path / 'Code.exe', tmp_path / 'abcdef1234/resources/app/out/cli.js'
    exe.touch()
    script.parent.mkdir(parents=True)
    script.touch()
    monkeypatch.setattr(software.shutil, 'which', lambda _: str(launcher))
    assert software._vscode() == [str(exe), str(script)]


async def test_install_cancellation_reaps_the_installer_and_closes_its_job(monkeypatch):
    entered = asyncio.Event()
    class Process:
        pid, returncode = 123, None
        killed = False
        async def wait(self):
            if self.killed:
                self.returncode = 1
                return 1
            entered.set()
            await asyncio.Event().wait()
        def kill(self):
            self.killed = True
    process = Process()
    closed = []
    async def spawn(*args, **kwargs):
        assert not kwargs.get('shell') and kwargs['stderr'] == subprocess.DEVNULL
        return process
    monkeypatch.setattr(software, 'command', lambda *_: (['node.exe', 'npm-cli.js'], {}))
    monkeypatch.setattr(software.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(software.jobs, 'attach', lambda pid: SimpleNamespace(close=lambda: closed.append(pid)))
    task = asyncio.create_task(software.install(item(), '1.2.10'))
    await entered.wait()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert closed == [123] and process.killed and process.returncode == 1


async def test_partial_lookup_failure_cache_and_unknown_installation(monkeypatch):
    m = manager(now=lambda: 100)
    monkeypatch.setattr(software, 'inspect', lambda tool: item(tool))
    calls = []
    def latest(tool, source, package):
        calls.append(tool)
        if tool == 'claude':
            raise OSError('private path')
        return '1.2.10'
    monkeypatch.setattr(software, 'latest', latest)
    data = await m.read()
    assert [r['latest'] for r in data['items']] == ['1.2.10', None, '1.2.10']
    assert not data['items'][1]['can_update']
    assert '_path' not in str(data) and 'private' not in str(data)
    await m.read()
    assert len(calls) == 3
    await m.read(True)
    assert len(calls) == 6


@pytest.mark.parametrize('change', ['newer_version', 'source'])
async def test_installation_changed_while_queued_never_downgrades_or_updates_another_source(monkeypatch, change):
    m = manager()
    m.items = {'codex': item(latest='1.2.10', can_update=True)}
    current = item(version='1.2.11') if change == 'newer_version' else item(source='vscode')
    monkeypatch.setattr(software, 'inspect', lambda _: current.copy())
    monkeypatch.setattr(software, 'latest', lambda *_: '1.2.10')
    install = AsyncMock()
    monkeypatch.setattr(software, 'install', install)
    m.start('codex')
    await m.task
    assert m.state == ('done' if change == 'newer_version' else 'error')
    install.assert_not_awaited()


@pytest.mark.parametrize('failure', ['none', 'installer', 'verification'])
async def test_update_waits_for_tasks_verifies_version_and_survives_dialog_disconnection(monkeypatch, failure):
    waiting, release = asyncio.Event(), asyncio.Event()
    async def sleep(_):
        waiting.set()
        await release.wait()
    m = manager(sleep=sleep)
    m.agent._software_requests = 1
    installed = False
    monkeypatch.setattr(software, 'inspect', lambda tool: item(tool, version='1.2.10' if installed else '1.2.3'))
    monkeypatch.setattr(software, 'latest', lambda *args: '1.2.10')
    async def install(*args):
        nonlocal installed
        assert not m.agent._software_requests and m.state == 'updating'
        if failure == 'installer':
            raise OSError('private installer output')
        installed = failure != 'verification'
    monkeypatch.setattr(software, 'install', install)
    await m.read()
    assert m.start('codex')['update']['state'] == 'waiting'
    try:
        await waiting.wait()
        assert m.busy and not installed
        with pytest.raises(Refused):
            m.start('opencode')
        m.agent._software_requests = 0
        release.set()
        await m.task
        assert m.state == ('done' if failure == 'none' else 'error')
        assert not m.busy
        m.agent.codex.close.assert_awaited_once()
    finally:
        await m.close()


async def test_cancel_waiting_update_never_installs_or_overwrites_next_update(monkeypatch):
    entered = asyncio.Event()
    async def sleep(_):
        entered.set()
        await asyncio.Event().wait()
    m = manager(sleep=sleep)
    m.agent._software_requests = 1
    m.items = {'codex': item(latest='1.2.10', can_update=True)}
    m.start('codex')
    await entered.wait()
    assert m.cancel()['update']['state'] == 'cancelled'
    with pytest.raises(Refused):
        m.start('codex')
    await asyncio.gather(m.task, return_exceptions=True)
    assert m.state == 'cancelled'


async def test_shutdown_before_the_update_task_starts_releases_busy_state():
    m = manager()
    m.items = {'codex': item(latest='1.2.10', can_update=True)}
    m.start('codex')
    await m.close()
    assert not m.busy and m.state == 'cancelled'


async def test_software_operations_require_pin_owner_and_valid_tool(agent_with_pin, monkeypatch):
    agent = agent_with_pin
    assert agent.hello()['software_versions'] is True
    agent.software.read = AsyncMock(return_value={'items': []})
    assert (await agent.handle(req('software_versions')))[0]['code'] == 'closed'
    assert not agent.software.read.called
    await agent.handle(req('open', pin='2468'))
    assert (await agent.handle(req('software_versions')))[0]['ok']
    assert (await agent.handle(req('software_update', software='cmd.exe')))[0]['code'] == 'invalid'
    assert (await agent.handle(req('software_versions', shared_projects=['a' * 16])))[0]['code'] == 'closed'
    agent.software.state = 'waiting'
    assert (await agent.handle(req('turn')))[0]['code'] == 'busy'
    assert (await agent.handle(req('agent_update')))[0]['code'] == 'busy'
    agent.software.state = 'idle'
    assert (await agent.handle(req('software_update_cancel')))[0]['ok']
