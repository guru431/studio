"""Обновление: границы ZIP, подготовка без остановки, ожидание заданий и штатная передача управления."""
import asyncio
import hashlib
import io
import json
import subprocess
import sys
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from studio_agent import VERSION, store, update_helper, updater
from studio_agent.errors import Refused

FUTURE = {'version': '99.0.0', 'sha256': 'a' * 64, 'size': 100}


def bundle(path, version='99.0.0', extra=None, prefix=''):
    with zipfile.ZipFile(path, 'w') as archive:
        if prefix:
            archive.writestr(prefix, '')
            archive.writestr(prefix + 'studio_agent/', '')
        archive.writestr(prefix + 'studio_agent/__init__.py', f'VERSION = "{version}"\n')
        archive.writestr(prefix + 'studio_agent.pyw', '')
        archive.writestr(prefix + 'requirements.txt', '')
        archive.writestr(prefix + 'constraints.txt', '')
        if extra is not None:
            info = zipfile.ZipInfo(extra)
            info.filename = extra  # ZipInfo.__init__ на Windows нормализует разделители.
            archive.writestr(info, 'bad')
    return path


@pytest.mark.parametrize('prefix', ['', 'studio-agent/'])
def test_unpack_published_archive_places_package_at_runtime_root(tmp_path, prefix):
    archive = bundle(tmp_path / 'source.zip', prefix=prefix)
    target = tmp_path / 'runtime'
    updater.unpack(archive, target, '99.0.0')
    assert (target / 'studio_agent.pyw').is_file()
    assert (target / 'studio_agent/__init__.py').read_text() == 'VERSION = "99.0.0"\n'
    assert not (target / 'studio-agent').exists()


def test_prefixed_archive_rejects_files_outside_its_root(tmp_path):
    archive = bundle(tmp_path / 'source.zip', prefix='studio-agent/', extra='outside.py')
    with pytest.raises(ValueError, match='path'):
        updater.unpack(archive, tmp_path / 'runtime', '99.0.0')
    assert not (tmp_path / 'runtime').exists()


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'C:/drive', '..\\escape', 'a:stream',
                                 'a/../escape', 'a./b', 'CON', 'a/AUX.txt', 'STUDIO_AGENT.PYW'])
def test_unpack_rejects_unsafe_paths_before_writing(tmp_path, name):
    archive = bundle(tmp_path / 'source.zip', extra=name)
    target = tmp_path / 'target'
    target.mkdir()
    with pytest.raises(ValueError):
        updater.unpack(archive, target, '99.0.0')
    assert not list(target.iterdir())


def test_unpack_rejects_symlinks_size_bombs_and_wrong_version(tmp_path, monkeypatch):
    archive = bundle(tmp_path / 'source.zip')
    with zipfile.ZipFile(archive, 'a') as source:
        info = zipfile.ZipInfo('symlink')
        info.external_attr = 0o120777 << 16
        source.writestr(info, '../outside')
    with pytest.raises(ValueError):
        updater.unpack(archive, tmp_path / 'target', '99.0.0')
    bundle(archive)
    with pytest.raises(ValueError, match='version'):
        updater.unpack(archive, tmp_path / 'target', '98.0.0')
    monkeypatch.setattr(updater, 'UNPACKED_MAX', 1)
    with pytest.raises(ValueError, match='size'):
        updater.unpack(archive, tmp_path / 'target', '99.0.0')


@pytest.mark.parametrize('url', ['http://example.com', 'https://user:pass@example.com',
                                'https://example.com/?token=key', 'https://example.com/#fragment'])
def test_download_never_sends_key_over_http_or_to_a_redirect(url):
    with pytest.raises(ValueError):
        updater.package_url(url)
    assert updater.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.example') is None


def test_prepare_checks_checksum_and_builds_a_separate_venv(home, tmp_path, monkeypatch):
    archive = bundle(tmp_path / 'package.zip').read_bytes()
    key = 'private-device-key'
    store.save_config(home, store.Config(url='https://studio.example'))
    monkeypatch.setattr(store, 'load_key', lambda h: key)
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    request = Mock()
    request.open.side_effect = lambda *a, **kw: io.BytesIO(archive)
    monkeypatch.setattr(updater.urllib.request, 'build_opener', lambda *a: request)
    run = Mock()
    monkeypatch.setattr(updater.subprocess, 'run', run)
    release = {**FUTURE, 'size': len(archive), 'sha256': hashlib.sha256(archive).hexdigest()}
    stage = updater.prepare(home, release)
    assert stage.parent == tmp_path / 'local/Programs/studio-agent/releases'
    assert (stage / 'studio_agent.pyw').is_file() and (stage / '_update_helper.py').is_file()
    assert len(run.call_args_list) == 3
    assert '.venv' in str(run.call_args_list[0])
    assert key not in str(run.call_args_list)
    assert request.open.call_args.args[0].get_header('Authorization') == 'Bearer ' + key
    with pytest.raises(ValueError, match='checksum'):
        updater.prepare(home, {**release, 'sha256': '0' * 64})
    assert list(stage.parent.iterdir()) == [stage]
    assert store.load_config(home).url == 'https://studio.example'


async def test_update_waits_for_jobs_and_repeated_clicks_launch_only_once(home, tmp_path):
    active = [True]
    waiting = asyncio.Event()
    resume = asyncio.Event()

    async def sleep(seconds):
        waiting.set()
        await resume.wait()

    prepare, launch, quit = Mock(return_value=tmp_path), Mock(), Mock()
    update = updater.Updater(home, lambda: active[0], quit, prepare_fn=prepare, launch_fn=launch, sleep=sleep)
    assert update.start(FUTURE)['state'] == 'preparing'
    first = update.task
    await waiting.wait()
    assert update.status() == {'state': 'waiting', 'version': '99.0.0'}
    assert update.start(FUTURE)['state'] == 'waiting' and update.task is first
    launch.assert_not_called()
    quit.assert_not_called()
    active[0] = False
    resume.set()
    await first
    prepare.assert_called_once()
    launch.assert_called_once_with(tmp_path, home)
    quit.assert_called_once()
    assert update.state == 'restarting'


async def test_cancel_while_waiting_reopens_the_agent_and_removes_the_prepared_copy(home, tmp_path):
    """«Отменить обновление» в ожидании: сразу `idle` (не `busy`), копия удаляется, выхода нет; `holds` —
    что держало ожидание, только в `waiting`."""
    waiting, resume = asyncio.Event(), asyncio.Event()

    async def sleep(seconds):
        waiting.set()
        await resume.wait()

    discard, launch, quit = Mock(), Mock(), Mock()
    holds = {'tasks': 2, 'asking': 1, 'desktop': False, 'requests': 0}
    update = updater.Updater(home, lambda: True, quit, prepare_fn=Mock(return_value=tmp_path), launch_fn=launch,
                             sleep=sleep, holds=lambda: dict(holds), discard_fn=discard)
    assert update.cancel() == {'state': 'idle', 'version': None}            # нечего отменять
    assert 'holds' not in update.start(FUTURE)
    await waiting.wait()
    assert update.status() == {'state': 'waiting', 'version': '99.0.0', 'holds': holds} and update.busy
    assert update.cancel() == {'state': 'idle', 'version': None} and not update.busy
    resume.set()
    await update.task
    discard.assert_called_once_with(tmp_path)
    launch.assert_not_called()
    quit.assert_not_called()
    assert update.state == 'idle'


async def test_cancel_during_preparation_discards_the_copy_when_it_is_ready(home, tmp_path):
    """Подготовка в потоке не прерывается: отмена — сразу `idle`, готовая позже копия удаляется; новый запуск —
    своя копия и своё ожидание."""
    import threading
    gate = threading.Event()

    def prepare(h, release):
        gate.wait(5)
        return tmp_path / release['version']

    discard, launch = Mock(), Mock()
    update = updater.Updater(home, lambda: False, Mock(), prepare_fn=prepare, launch_fn=launch, discard_fn=discard)
    update.start(FUTURE)
    first = update.task
    assert update.cancel()['state'] == 'idle'
    update.start({**FUTURE, 'version': '99.0.1'})
    gate.set()
    await asyncio.gather(first, update.task)
    discard.assert_called_once_with(tmp_path / '99.0.0')
    launch.assert_called_once_with(tmp_path / '99.0.1', home)
    assert update.state == 'restarting' and update.cancel()['state'] == 'restarting'  # поздно


def test_discard_removes_only_own_release_folder(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    base = tmp_path / 'Programs' / 'studio-agent' / 'releases'
    for name in ('release-a', 'current'):
        (base / name / '.venv').mkdir(parents=True)
    outside = tmp_path / 'release-x'
    outside.mkdir()
    for path in (base / 'release-a', base / 'current', outside):
        updater.discard(path)
    assert sorted(p.name for p in base.iterdir()) == ['current'] and outside.is_dir()


async def test_preparation_failure_keeps_current_agent_and_allows_retry(home, caplog):
    quit = Mock()
    prepare = Mock(side_effect=OSError('secret/path/prompt'))
    update = updater.Updater(home, lambda: False, quit, prepare_fn=prepare)
    update.start(FUTURE)
    await update.task
    assert update.state == 'failed' and not update.busy
    quit.assert_not_called()
    assert 'secret/path/prompt' not in caplog.text
    update.start(FUTURE)
    await update.task
    assert prepare.call_count == 2


async def test_invalid_or_older_release_never_starts_installation(home):
    update = updater.Updater(home, lambda: False, Mock())
    for patch in ({'size': True}, {'size': updater.PACKAGE_MAX + 1}, {'version': '../x'}, {'sha256': 'bad'}):
        with pytest.raises(Refused):
            update.start({**FUTURE, **patch})
    assert update.start({**FUTURE, 'version': VERSION}) == {'state': 'idle', 'version': VERSION}
    assert update.start({**FUTURE, 'version': '0.1.0'})['state'] == 'idle'
    assert update.task is None


def test_helper_never_launches_while_old_agent_is_running(tmp_path):
    start = Mock()
    assert update_helper.activate(tmp_path, {'pid': 123}, wait=lambda pid: False, start=start) == 'old_running'
    start.assert_not_called()


def test_failed_new_process_restarts_untouched_previous_copy(tmp_path):
    old = tmp_path / 'old'
    plan = {'pid': 123, 'home': str(tmp_path / 'home'), 'old_repo': str(old), 'old_python': 'old-pythonw'}
    start = Mock(return_value=SimpleNamespace(poll=lambda: 1))
    result = update_helper.activate(tmp_path, plan, wait=lambda pid: True, start=start)
    assert result == 'rolled_back' and start.call_count == 2
    assert start.call_args.args[0] == ['old-pythonw', str(old / 'studio_agent.pyw')]
    assert 'STUDIO_AGENT_UPDATE_READY' not in start.call_args.kwargs['env']


def test_failed_launch_rolls_back_and_success_preserves_autostart(tmp_path, monkeypatch):
    import winreg

    old = tmp_path / 'old'
    plan = {'pid': 123, 'home': str(tmp_path / 'home'), 'old_repo': str(old), 'old_python': 'old-pythonw'}
    start = Mock(side_effect=[OSError(), SimpleNamespace()])
    assert update_helper.activate(tmp_path, plan, wait=lambda pid: True, start=start) == 'rolled_back'
    registry = Mock()
    registry.__enter__ = Mock(return_value=registry)
    registry.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(winreg, 'OpenKey', Mock(return_value=registry))
    monkeypatch.setattr(winreg, 'QueryValueEx', Mock(return_value=(
        f'"{old / ".venv/Scripts/pythonw.exe"}" "{old / "studio_agent.pyw"}"', winreg.REG_SZ)))
    set_value = Mock()
    monkeypatch.setattr(winreg, 'SetValueEx', set_value)
    (tmp_path / '_update_ready').touch()
    assert update_helper.activate(tmp_path, plan, wait=lambda pid: True, start=Mock()) == 'complete'
    assert str(tmp_path / 'studio_agent.pyw') in set_value.call_args.args[-1]
    monkeypatch.setattr(winreg, 'QueryValueEx', Mock(side_effect=FileNotFoundError()))
    set_value.reset_mock()
    assert update_helper.activate(tmp_path, plan, wait=lambda pid: True, start=Mock()) == 'complete'
    set_value.assert_not_called()


def test_confirmed_update_keeps_the_current_and_the_previous_release_only(tmp_path, monkeypatch):
    """После `complete` — текущий выпуск и предыдущий (откат); не трогаются выпуск без итога хелпера (готовится),
    выпуск автозапуска, ссылки и всё вне `releases`."""
    import _winapi
    import winreg

    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path))
    monkeypatch.setattr(update_helper, 'running_releases', lambda base: set())
    base = tmp_path / 'Programs' / 'studio-agent' / 'releases'
    names = ['release-old', 'release-run', 'release-stale', 'release-preparing', 'release-new']
    for name in names:
        (base / name).mkdir(parents=True)
        (base / name / 'studio_agent.pyw').write_text('')
    for name in names[:3]:
        (base / name / '_update_result').write_text('complete')
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / '_update_result').write_text('complete')
    _winapi.CreateJunction(str(outside), str(base / 'release-link'))
    stage = base / 'release-new'
    (stage / '_update_ready').touch()
    plan = {'pid': 1, 'home': str(tmp_path / 'home'), 'old_repo': str(base / 'release-old'), 'old_python': 'py'}
    registry = Mock()
    registry.__enter__ = Mock(return_value=registry)
    registry.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(winreg, 'OpenKey', Mock(return_value=registry))
    run = base / 'release-run'
    monkeypatch.setattr(winreg, 'QueryValueEx', Mock(return_value=(
        f'"{run / ".venv/Scripts/pythonw.exe"}" "{run / "studio_agent.pyw"}"', winreg.REG_SZ)))
    monkeypatch.setattr(winreg, 'SetValueEx', Mock())
    assert update_helper.activate(stage, plan, wait=lambda pid: True, start=Mock()) == 'complete'
    assert sorted(p.name for p in base.iterdir()) == ['release-link', 'release-new', 'release-old',
                                                      'release-preparing', 'release-run']
    assert (outside / '_update_result').is_file()


def test_unconfirmed_or_rolled_back_update_removes_no_release(tmp_path):
    base = tmp_path / 'releases'
    for name in ('release-a', 'release-b'):
        (base / name).mkdir(parents=True)
        (base / name / '_update_result').write_text('complete')
    stage = base / 'release-b'
    plan = {'pid': 1, 'home': str(tmp_path), 'old_repo': str(tmp_path / 'base'), 'old_python': 'py'}
    start = Mock(return_value=SimpleNamespace(poll=lambda: 1))
    assert update_helper.activate(stage, plan, wait=lambda pid: True, start=start) == 'rolled_back'
    assert sorted(p.name for p in base.iterdir()) == ['release-a', 'release-b']


@pytest.mark.integration
@pytest.mark.timeout(15)
def test_real_windows_process_handle_waits_for_natural_exit():
    old = subprocess.Popen([sys.executable, '-c', 'import sys; print("ready", flush=True); sys.stdin.readline()'],
                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        assert old.stdout.readline().strip() == b'ready'
        assert update_helper.wait_old(old.pid, timeout=0) is False
        old.stdin.close()
        old.wait(timeout=5)
        assert update_helper.wait_old(old.pid, timeout=100) is True
    finally:
        if not old.stdin.closed:
            old.stdin.close()
        old.wait(timeout=5)
        old.stdout.close()


@pytest.mark.integration
@pytest.mark.timeout(90)
def test_real_preparation_and_helper_handover_keep_the_previous_install(home, tmp_path, tmp_path_factory, monkeypatch):
    """Настоящие venv/pip и смена процессов, без запущенного рабочего моста, сети и квоты CLI."""
    trace = tmp_path / 'trace.txt'
    archive = tmp_path / 'package.zip'
    with zipfile.ZipFile(archive, 'w') as source:
        source.writestr('studio-agent/', '')
        source.writestr('studio-agent/studio_agent/', '')
        source.writestr('studio-agent/studio_agent/__init__.py', 'VERSION = "99.0.0"\n')
        source.writestr('studio-agent/studio_agent/app.py', '')
        source.writestr('studio-agent/requirements.txt', '')
        source.writestr('studio-agent/constraints.txt', '')
        source.writestr('studio-agent/studio_agent.pyw',
            'import os\nfrom pathlib import Path\n'
            f'with open({str(trace)!r}, "a") as out: out.write("new\\n")\n'
            'Path(os.environ["STUDIO_AGENT_UPDATE_READY"]).touch()\n')
    data = archive.read_bytes()
    store.save_config(home, store.Config(url='https://studio.example'))
    monkeypatch.setattr(store, 'load_key', lambda h: 'test-device-key')
    # Имя теста + releases/.venv + licenses pip превышали Windows MAX_PATH.
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path_factory.mktemp('install')))
    opener = Mock()
    opener.open.side_effect = lambda *a, **kw: io.BytesIO(data)
    monkeypatch.setattr(updater.urllib.request, 'build_opener', lambda *a: opener)
    stage = updater.prepare(home, {**FUTURE, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    assert (stage / '.venv/Scripts/pythonw.exe').is_file()
    old_repo = tmp_path / 'previous'
    old_repo.mkdir()
    (old_repo / 'studio_agent.pyw').write_text('previous source')
    old_code = ('import sys\nprint("ready", flush=True)\nsys.stdin.readline()\n'
                f'with open({str(trace)!r}, "a") as out: out.write("old-exit\\n")\n')
    old = subprocess.Popen([sys.executable, '-c', old_code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           creationflags=subprocess.CREATE_NO_WINDOW)
    helper = None
    try:
        assert old.stdout.readline().strip() == b'ready'
        plan = {'pid': old.pid, 'home': str(home), 'old_repo': str(old_repo), 'old_python': sys.executable}
        (stage / '_update_plan.json').write_text(json.dumps(plan), encoding='utf-8')
        helper = subprocess.Popen([sys.executable, str(stage / '_update_helper.py')], cwd=stage,
                                  creationflags=subprocess.CREATE_NO_WINDOW)
        assert not (stage / '_update_ready').exists()
        old.stdin.close()
        old.wait(timeout=5)
        helper.wait(timeout=15)
        assert helper.returncode == 0
        assert (stage / '_update_result').read_text() == 'complete'
        assert trace.read_text().splitlines() == ['old-exit', 'new']
        assert (old_repo / 'studio_agent.pyw').read_text() == 'previous source'
        assert store.load_config(home).url == 'https://studio.example'
    finally:
        if not old.stdin.closed:
            old.stdin.close()
        old.wait(timeout=5)
        old.stdout.close()
        if helper is not None:
            helper.wait(timeout=15)


def test_helper_plan_contains_no_device_key_or_pin(home, tmp_path, monkeypatch):
    popen = Mock()
    monkeypatch.setattr(updater.subprocess, 'Popen', popen)
    updater.launch(tmp_path, home)
    plan = json.loads((tmp_path / '_update_plan.json').read_text())
    assert set(plan) == {'pid', 'old_repo', 'old_python', 'home'}
    assert popen.call_args.kwargs['creationflags'] & subprocess.DETACHED_PROCESS
