"""Удаление старых выпусков: повтор после частичной ошибки и строгие границы/живые процессы."""

import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from studio_agent import update_helper, updater


@pytest.fixture
def releases(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    base = tmp_path / "local/Programs/studio-agent/releases"
    for name in ("release-current", "release-previous", "release-stale"):
        folder = base / name
        folder.mkdir(parents=True)
        (folder / "studio_agent.pyw").write_text("source", encoding="utf-8")
        (folder / "_update_result").write_text("complete", encoding="ascii")
    stage = base / "release-current"
    (stage / "_update_ready").touch()
    plan = {"old_repo": str(base / "release-previous")}
    monkeypatch.setattr(update_helper, "running_releases", lambda base: set(), raising=False)
    return base, stage, plan


def test_partial_removal_is_retried_even_when_rmtree_already_removed_the_result_marker(releases, monkeypatch):
    base, stage, plan = releases
    stale = base / "release-stale"
    remove = update_helper.shutil.rmtree
    attempts = []

    def locked(path, **kwargs):
        attempts.append(Path(path))
        if len(attempts) == 1:
            (stale / "_update_result").unlink()
            if kwargs.get("ignore_errors"):
                return  # поведение rmtree(ignore_errors=True) после занятого Windows-файла
            raise PermissionError("fixture: file is in use")
        return remove(path, **kwargs)

    monkeypatch.setattr(update_helper.shutil, "rmtree", locked)
    update_helper.prune(stage, plan)
    assert stale.is_dir()
    update_helper.prune(stage, plan)
    assert attempts == [stale, stale]
    assert not stale.exists()


def test_prune_preserves_current_rollback_autostart_active_and_unconfirmed_folders(releases, monkeypatch):
    base, stage, plan = releases
    preserved = ('release-active', 'release-autostart', 'release-unknown', 'release-failed', 'notes')
    for name in (*preserved, *(f'release-extra-{index}' for index in range(12))):
        folder = base / name
        folder.mkdir()
        if name != 'release-unknown':
            (folder / '_update_result').write_text('failed' if name == 'release-failed' else 'complete', encoding='ascii')
    monkeypatch.setattr(update_helper, 'running_releases', lambda base: {'release-active'})
    command = f'"{base / "release-autostart/.venv/Scripts/pythonw.exe"}"'.replace('\\', '/')
    before = set(base.iterdir())
    candidates = update_helper.prune_candidates(stage, plan, command)
    assert len(candidates) == 13 and set(base.iterdir()) == before  # dry-run ничего не меняет
    assert len(update_helper.prune(stage, plan, command)) == 13
    assert {path.name for path in base.iterdir()} == {*preserved, 'release-current', 'release-previous'}


def test_unknown_process_state_or_autostart_forbids_all_deletion(releases, monkeypatch):
    base, stage, plan = releases
    scan = Mock(return_value=None)
    monkeypatch.setattr(update_helper, 'running_releases', scan)
    assert update_helper.prune(stage, plan) == []
    assert (base / 'release-stale').is_dir()
    scan.reset_mock()
    assert update_helper.prune(stage, plan, None) == []
    scan.assert_not_called()


@pytest.mark.parametrize('plan', [{}, {'old_repo': None}, {'old_repo': '../unknown'}, []])
def test_unknown_rollback_plan_forbids_cleanup(releases, plan):
    base, stage, _ = releases
    assert update_helper.prune(stage, plan) == []
    assert (base / 'release-stale').is_dir()


def test_same_named_releases_outside_the_managed_programs_directory_are_untouched(releases, tmp_path):
    base, stage, plan = releases
    outside = tmp_path / 'elsewhere/releases'
    outside.mkdir(parents=True)
    for name in ('release-current', 'release-stale'):
        folder = outside / name
        folder.mkdir()
        (folder / '_update_result').write_text('complete', encoding='ascii')
    assert update_helper.prune(outside / stage.name, plan) == []
    assert (outside / 'release-stale/_update_result').read_text('ascii') == 'complete'


@pytest.mark.parametrize('nested', [False, True])
def test_root_and_nested_junctions_are_preserved_with_their_targets(releases, tmp_path, nested):
    import _winapi

    base, stage, plan = releases
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'keep.txt').write_text('untouched', encoding='ascii')
    link = base / 'release-stale/linked' if nested else base / 'release-linked'
    _winapi.CreateJunction(str(outside), str(link))
    update_helper.prune(stage, plan)
    assert link.is_dir() and (outside / 'keep.txt').read_text('ascii') == 'untouched'
    if nested:
        assert (base / 'release-stale').is_dir()


def test_junction_managed_base_is_not_a_cleanup_boundary(releases):
    import _winapi

    base, stage, plan = releases
    moved = base.with_name('original-releases')
    base.rename(moved)
    _winapi.CreateJunction(str(moved), str(base))
    assert update_helper.prune(stage, plan) == []
    assert (moved / 'release-stale').is_dir()


def test_a_process_starting_after_planning_protects_its_release(releases):
    base, stage, plan = releases
    scan = Mock(side_effect=[set(), {'release-stale'}])
    assert update_helper.prune(stage, plan, processes=scan) == []
    assert (base / 'release-stale').is_dir()
    assert not list(base.glob('.prune-*'))


def test_replacing_a_planned_directory_does_not_authorize_the_new_directory(releases):
    base, stage, plan = releases
    stale = base / 'release-stale'
    calls = []

    def scan(base):
        calls.append(True)
        if len(calls) == 2:
            stale.rename(base / 'preserved-original')
            stale.mkdir()
            (stale / 'unknown.txt').write_text('keep', encoding='ascii')
        return set()

    assert update_helper.prune(stage, plan, processes=scan) == []
    assert (stale / 'unknown.txt').read_text('ascii') == 'keep'


def test_partial_cleanup_intent_does_not_authorize_a_recreated_directory(releases, monkeypatch):
    base, stage, plan = releases
    stale = base / 'release-stale'

    def partial(path):
        (path / '_update_result').unlink()
        raise PermissionError('fixture')

    monkeypatch.setattr(update_helper.shutil, 'rmtree', partial)
    assert update_helper.prune(stage, plan) == []
    assert list(base.glob('.prune-release-stale.*'))
    stale.rename(base / 'preserved-partial')
    stale.mkdir()
    (stale / 'unknown.txt').write_text('keep', encoding='ascii')
    assert update_helper.prune(stage, plan) == []
    assert (stale / 'unknown.txt').read_text('ascii') == 'keep'


async def test_new_version_cleans_after_the_old_helper_confirms_startup(releases, monkeypatch):
    base, stage, plan = releases
    (stage / '_update_result').unlink()
    (stage / '_update_plan.json').write_text(json.dumps(plan), encoding='utf-8')
    monkeypatch.setattr(update_helper, '_autostart', lambda: '')
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)
        assert (base / 'release-stale').is_dir()
        (stage / '_update_result').write_text('complete', encoding='ascii')

    await updater.cleanup_releases(repo=stage, sleep=sleep)
    assert sleeps == [0.5] and not (base / 'release-stale').exists()


@pytest.mark.parametrize('status', ['failed', 'unconfirmed', 'rolled_back'])
def test_unsuccessful_or_unconfirmed_startup_does_not_prune(releases, status):
    base, stage, plan = releases
    (stage / '_update_result').write_text(status, encoding='ascii')
    (stage / '_update_plan.json').write_text(json.dumps(plan), encoding='utf-8')
    assert update_helper.cleanup_started(stage) is True
    assert (base / 'release-stale').is_dir()


def test_activation_preserves_releases_when_the_registry_value_cannot_be_read(releases, monkeypatch):
    import winreg

    base, stage, plan = releases
    monkeypatch.setattr(winreg, 'OpenKey', Mock(side_effect=PermissionError('fixture')))
    assert update_helper.activate(stage, {**plan, 'pid': 123, 'home': 'home', 'old_python': 'py'},
                                  wait=lambda pid: True, start=Mock()) == 'complete'
    assert (base / 'release-stale').is_dir()


@pytest.mark.parametrize('data,expected,complete', [
    ({'unknown': False, 'releases': ['release-running']}, {'release-running'}, True),
    ({'unknown': True, 'releases': []}, set(), False), ({'unknown': False, 'releases': ['../escape']}, None, None),
    ({'unknown': False, 'releases': 'release-running'}, None, None), ([], None, None)])
def test_process_snapshot_parser_fails_closed_and_never_exposes_command_lines(releases, monkeypatch, data, expected, complete):
    base, _, _ = releases
    run = Mock(return_value=SimpleNamespace(stdout=json.dumps(data)))
    monkeypatch.setattr(update_helper.subprocess, 'run', run)
    # Фикстура releases подменила реальный scanner; импорт функции берём до её временной подмены ниже.
    result = PROCESS_SCAN(base)
    assert result == expected
    if result is not None:
        assert result.complete is complete
    assert run.call_args.kwargs['creationflags'] & subprocess.CREATE_NO_WINDOW
    assert run.call_args.kwargs['timeout'] == 15


PROCESS_SCAN = update_helper.running_releases


@pytest.mark.integration
@pytest.mark.timeout(30)
def test_real_cim_snapshot_detects_a_python_process_and_preserves_fail_closed_for_other_users(releases, monkeypatch):
    base, _, _ = releases
    run = update_helper.subprocess.run
    snapshots = []

    def capture(*args, **kwargs):
        result = run(*args, **kwargs)
        snapshots.append(json.loads(result.stdout))
        return result

    monkeypatch.setattr(update_helper.subprocess, 'run', capture)
    child = subprocess.Popen([sys.executable, '-c', 'import sys; print("ready", flush=True); sys.stdin.readline()',
                              str(base / 'release-stale/studio_agent.pyw')], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        assert child.stdout.readline().strip() == b'ready'
        active = PROCESS_SCAN(base)
        assert 'release-stale' in snapshots[-1]['releases']
        assert active is not None and 'release-stale' in active
        assert active.complete is not snapshots[-1]['unknown']
    finally:
        child.stdin.close()
        child.wait(timeout=5)
        child.stdout.close()


@pytest.fixture
def lease_locks(monkeypatch):
    locked = set()

    class Kernel:
        def CloseHandle(self, path):
            locked.discard(path)

    kernel = Kernel()

    def lock(path, *, create=False, gate=False):
        if path in locked:
            raise PermissionError('locked')
        if create or gate:
            path.touch(exist_ok=gate)
            locked.add(path)
        return kernel, path

    monkeypatch.setattr(update_helper, '_lock_file', lock)
    yield locked
    update_helper._close_runtime_leases()


def test_partial_cim_uses_runtime_locks_for_new_releases_and_preserves_legacy(releases, lease_locks, monkeypatch):
    base, stage, plan = releases
    partial = update_helper.ProcessReleases(complete=False)
    monkeypatch.setattr(update_helper, 'running_releases', lambda base: partial)
    assert update_helper.prune(stage, plan) == []  # legacy без lease не считаем остановленным
    first = update_helper.acquire_runtime_lease(base / 'release-stale')
    second = update_helper.acquire_runtime_lease(base / 'release-stale')
    assert update_helper.prune(stage, plan) == []
    update_helper.release_runtime_lease(first)
    assert update_helper.prune(stage, plan) == []  # второй экземпляр всё ещё жив
    update_helper.release_runtime_lease(second)
    assert update_helper.prune(stage, plan) == [base / 'release-stale']


def test_more_than_ten_tracked_releases_are_cleaned_with_partial_cim_without_administrator(releases, lease_locks, monkeypatch):
    base, stage, plan = releases
    for index in range(12):
        folder = base / f'release-aged-{index}'
        folder.mkdir()
        (folder / '_update_result').write_text('complete', encoding='ascii')
        token = update_helper.acquire_runtime_lease(folder)
        update_helper.release_runtime_lease(token)
    monkeypatch.setattr(update_helper, 'running_releases', lambda base: update_helper.ProcessReleases(complete=False))
    assert len(update_helper.prune(stage, plan)) == 12
    assert {path.name for path in base.iterdir()} == {'release-current', 'release-previous', 'release-stale'}


def test_partial_cleanup_retries_after_runtime_markers_were_deleted(releases, lease_locks, monkeypatch):
    base, stage, plan = releases
    stale = base / 'release-stale'
    token = update_helper.acquire_runtime_lease(stale)
    update_helper.release_runtime_lease(token)
    monkeypatch.setattr(update_helper, 'running_releases', lambda base: update_helper.ProcessReleases(complete=False))
    remove = update_helper.shutil.rmtree
    calls = []

    def partial(path):
        calls.append(path)
        if len(calls) == 1:
            remove(path / '_runtime_leases')
            (path / '_runtime_lease_version').unlink()
            (path / '_update_result').unlink()
            raise PermissionError('fixture')
        remove(path)

    monkeypatch.setattr(update_helper.shutil, 'rmtree', partial)
    assert update_helper.prune(stage, plan) == []
    assert update_helper.prune(stage, plan) == [stale]
    assert not list(base.glob('.prune-*'))


def test_runtime_registration_gate_prevents_cleanup_even_before_the_lease_is_created(releases, lease_locks, monkeypatch):
    base, stage, plan = releases
    lock = update_helper._lock_file
    cleanups = []

    def interleave(path, *, create=False, gate=False):
        if create:
            cleanups.append(update_helper.prune(stage, plan))
        return lock(path, create=create, gate=gate)

    monkeypatch.setattr(update_helper, '_lock_file', interleave)
    token = update_helper.acquire_runtime_lease(base / 'release-stale')
    assert cleanups == [[]] and (base / 'release-stale').is_dir()
    # Даже полный, но уже устаревший CIM не может переопределить живой OS-lease.
    assert update_helper.prune(stage, plan) == []
    update_helper.release_runtime_lease(token)
    assert update_helper.prune(stage, plan) == [base / 'release-stale']


def test_cleanup_gate_refuses_new_runtime_until_the_release_is_removed(releases, lease_locks, monkeypatch):
    base, stage, plan = releases
    stale = base / 'release-stale'
    token = update_helper.acquire_runtime_lease(stale)
    update_helper.release_runtime_lease(token)
    remove = update_helper.shutil.rmtree
    refused = []

    def interleave(path):
        with pytest.raises(OSError):
            update_helper.acquire_runtime_lease(path)
        refused.append(True)
        return remove(path)

    monkeypatch.setattr(update_helper.shutil, 'rmtree', interleave)
    assert update_helper.prune(stage, plan) == [stale]
    assert refused == [True]
    with pytest.raises(OSError, match='managed release unavailable'):
        update_helper.acquire_runtime_lease(stale)


@pytest.mark.integration
@pytest.mark.timeout(30)
def test_real_runtime_lock_survives_an_unreadable_cim_snapshot_until_process_exit(releases):
    base, stage, plan = releases
    stale = base / 'release-stale'
    source = str(Path(update_helper.__file__).resolve().parents[1])
    code = ('import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); '
            'from studio_agent import update_helper; update_helper.acquire_runtime_lease(Path(sys.argv[2])); '
            'print("ready", flush=True); sys.stdin.readline()')
    child = subprocess.Popen([sys.executable, '-c', code, source, str(stale)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    partial = lambda base: update_helper.ProcessReleases(complete=False)
    try:
        assert child.stdout.readline().strip() == b'ready'
        assert update_helper.prune(stage, plan, processes=partial) == []
        assert stale.is_dir()
        child.stdin.close()
        child.wait(timeout=5)
        assert update_helper.prune(stage, plan, processes=partial) == [stale]
    finally:
        if not child.stdin.closed:
            child.stdin.close()
        child.wait(timeout=5)
        child.stdout.close()


@pytest.mark.integration
@pytest.mark.timeout(30)
def test_real_cleanup_gate_refuses_concurrent_runtime_registration(releases):
    base, _, _ = releases
    stale = base / 'release-stale'
    gate = update_helper._gate_lock(base, stale, update_helper._identity(stale))
    source = str(Path(update_helper.__file__).resolve().parents[1])
    code = ('import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); '
            'from studio_agent import update_helper; update_helper.acquire_runtime_lease(Path(sys.argv[2]))')
    try:
        result = subprocess.run([sys.executable, '-c', code, source, str(stale)], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        assert result.returncode != 0
        assert not (stale / '_runtime_leases').exists()
    finally:
        update_helper.release_runtime_lease(gate)
