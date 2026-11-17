"""Контракты macOS: защита секретов, выбор executable и переключение установленной копии."""
import os
import plistlib
import subprocess
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from studio_agent import cli, codex_rpc, host, macos_autostart, macos_keychain, opencode_rpc, update_helper


def test_macos_secret_blobs_authenticate_and_never_fall_back_to_plaintext(monkeypatch):
    key = Fernet.generate_key()
    monkeypatch.setattr(macos_keychain, '_key', lambda account: key)
    blob = macos_keychain.protect(b'device secret')
    assert b'device secret' not in blob
    assert macos_keychain.unprotect(blob) == b'device secret'
    with pytest.raises(OSError):
        macos_keychain.unprotect(blob[:-2] + b'xx')
    monkeypatch.setattr(macos_keychain, '_key', lambda account: Fernet.generate_key())
    with pytest.raises(OSError):
        macos_keychain.unprotect(blob)


def test_locked_keychain_never_creates_a_replacement_key(monkeypatch):
    macos_keychain._key.cache_clear()
    def denied(account):
        raise OSError(-25308, 'locked')
    monkeypatch.setattr(macos_keychain, '_read_existing', denied)
    monkeypatch.setattr(macos_keychain, '_security', lambda: pytest.fail('must not add key'))
    with pytest.raises(OSError) as error:
        macos_keychain.protect(b'secret')
    assert error.value.errno == -25308


@pytest.mark.parametrize('machine,suffix', [('arm64', 'arm64'), ('x86_64', 'x64')])
def test_macos_finds_only_its_native_claude_extension(tmp_path, monkeypatch, machine, suffix):
    monkeypatch.setattr(host, 'MACOS', True)
    monkeypatch.setattr(host.platform, 'machine', lambda: machine)
    monkeypatch.setattr(host, 'find_tool', lambda name: None)
    for version, platform in [('2.1.9', f'darwin-{suffix}'), ('2.1.10', f'darwin-{suffix}'), ('9.0.0', 'win32-x64')]:
        folder = tmp_path / f'anthropic.claude-code-{version}-{platform}/resources/native-binary'
        folder.mkdir(parents=True)
        (folder / ('claude.exe' if platform.startswith('win') else 'claude')).touch()
    assert cli.find_cli(tmp_path).parent.parent.parent.name == f'anthropic.claude-code-2.1.10-darwin-{suffix}'
    assert host.python(tmp_path) == tmp_path / '.venv/bin/python'


def test_macos_codex_and_opencode_use_executable_without_a_shell(tmp_path, monkeypatch):
    monkeypatch.setattr(host, 'MACOS', True)
    monkeypatch.setattr(host, 'find_tool', lambda name: tmp_path / name)
    assert codex_rpc.find_cli() == tmp_path / 'codex'
    assert opencode_rpc.find_cli() == tmp_path / 'opencode'
    assert host.process_group() == {'start_new_session': True}


def test_launchagent_path_never_adds_current_directory_when_path_is_missing(monkeypatch):
    monkeypatch.setattr(host, 'MACOS', True)
    monkeypatch.delenv('PATH', raising=False)
    host.prepare_path()
    paths = os.environ['PATH'].split(os.pathsep)
    assert paths and '' not in paths and '.' not in paths


def test_macos_launchagent_preserves_argv_and_does_not_start_or_stop_processes(tmp_path, monkeypatch):
    target = tmp_path / 'LaunchAgents/com.studio.agent.plist'
    monkeypatch.setattr(macos_autostart, 'path', lambda: target)
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: pytest.fail('must not touch running bridge'))
    repo = tmp_path / 'Studio Agent/release-one'
    macos_autostart.set_enabled(True, repo)
    data = plistlib.loads(target.read_bytes())
    assert data['ProgramArguments'] == [str(repo / '.venv/bin/python'), str(repo / 'studio_agent.pyw')]
    assert data['LimitLoadToSessionType'] == 'Aqua' and data['RunAtLoad'] is True
    assert 'KeepAlive' not in data
    assert macos_autostart.read() == data
    macos_autostart.set_enabled(False, repo)
    assert not target.exists()


@pytest.mark.skipif(os.name == 'nt', reason='права POSIX')
def test_saving_an_executable_preserves_mode_and_readonly_is_refused(tmp_path):
    from studio_agent import files
    from studio_agent.errors import Refused
    path = tmp_path / 'script.sh'
    path.write_bytes(b'old')
    path.chmod(0o750)
    files.save_text(tmp_path, 'script.sh', 'new', files.version(b'old'))
    assert path.read_bytes() == b'new' and path.stat().st_mode & 0o777 == 0o750
    path.chmod(0o440)
    with pytest.raises(Refused, match='read_only'):
        files.save_text(tmp_path, 'script.sh', 'changed', files.version(b'new'))
    assert path.read_bytes() == b'new'


@pytest.mark.skipif(not host.MACOS, reason='настоящая группа процессов macOS')
@pytest.mark.integration
def test_macos_job_ends_the_cli_and_its_children():
    import signal
    import sys
    from studio_agent import jobs
    process = subprocess.Popen([sys.executable, '-c',
        'import subprocess,sys,time; p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(120)"]); '
        'print(p.pid,flush=True); time.sleep(120)'], stdout=subprocess.PIPE, start_new_session=True, text=True)
    try:
        child = int(process.stdout.readline())
        job = jobs.attach(process.pid)
        assert job is not None
        job.close()
        assert process.wait(5) < 0
        result = subprocess.run(['/bin/ps', '-p', str(child), '-o', 'stat='], capture_output=True, text=True, timeout=3)
        assert result.returncode != 0 or result.stdout.strip().startswith('Z')
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(5)
        process.stdout.close()
