"""Запускатель Windows «Studio Agent.exe» (0.47.0): проба, выбор команды автозапуска и ярлыка, откат на `pythonw.exe`
(быстрые: подставной реестр, ярлык во временной `APPDATA`) и настоящая сборка csc (`integration`)."""
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

winreg = pytest.importorskip('winreg')

from studio_agent import app, autostart, shortcut, update_helper  # noqa: E402

REPO = autostart.REPO
EXE, PYTHONW = update_helper.startup_commands(REPO)
DEPLOY = REPO.parent / 'deploy'


class Registry:
    """Подставной `winreg` для `autostart`: одно значение `StudioAgent` под любым ключом."""
    HKEY_CURRENT_USER, KEY_SET_VALUE, KEY_QUERY_VALUE, REG_SZ = object(), 2, 1, 1

    def __init__(self, value=None):
        self.value, self.writes = value, []

    def OpenKey(self, *args):
        return self

    CreateKeyEx = OpenKey

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def QueryValueEx(self, key, name):
        if self.value is None:
            raise FileNotFoundError(name)
        return self.value, self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.value = value
        self.writes.append(value)


@pytest.fixture
def ready(monkeypatch):
    """Итог пробы запускателя этой копии: `ready(True)` — прошёл, `ready(False)` — нет файла или отказ."""
    def set_ready(on):
        monkeypatch.setattr(update_helper, 'launcher', lambda repo, run=None: Path(repo) / update_helper.LAUNCHER
                            if on else None)
    return set_ready


def test_one_name_and_app_id_for_launcher_shortcut_and_process():
    assert shortcut.LAUNCHER == update_helper.LAUNCHER == 'Studio Agent.exe'
    assert shortcut.APP_ID == app.APP_ID
    assert f'"studio-agent/{update_helper.LAUNCHER}"' in (DEPLOY / 'build-agent-bundle.py').read_text(encoding='utf-8')


def test_probe_runs_the_launcher_and_accepts_only_exit_code_zero(tmp_path):
    run = Mock(return_value=SimpleNamespace(returncode=0))
    assert update_helper.launcher(tmp_path, run=run) is None and not run.called   # нет файла — не запускаем
    exe = tmp_path / update_helper.LAUNCHER
    exe.write_bytes(b'MZ')
    assert update_helper.launcher(tmp_path, run=run) == exe
    assert run.call_args.args[0] == [str(exe), '--probe'] and run.call_args.kwargs['timeout'] == update_helper.PROBE_SEC
    run.return_value = SimpleNamespace(returncode=78)                            # нет среды рядом
    assert update_helper.launcher(tmp_path, run=run) is None
    for error in (OSError('blocked'), subprocess.TimeoutExpired('exe', 10)):    # антивирус запретил, завис
        assert update_helper.launcher(tmp_path, run=Mock(side_effect=error)) is None


def test_autostart_writes_the_launcher_only_after_a_passed_probe(monkeypatch, ready):
    registry = Registry()
    monkeypatch.setattr(autostart, 'winreg', registry)
    ready(False)
    autostart.set_enabled(True)
    assert registry.value == PYTHONW and autostart.enabled()
    ready(True)
    autostart.set_enabled(True)
    assert registry.value == EXE and autostart.enabled()                         # галочка окна — обе команды копии
    registry.value = EXE.upper()
    assert autostart.enabled()


def test_start_moves_own_autostart_to_the_launcher_and_back_when_it_is_gone(monkeypatch, ready):
    registry = Registry(PYTHONW)
    monkeypatch.setattr(autostart, 'winreg', registry)
    ready(True)
    autostart.repair()
    assert registry.value == EXE
    autostart.repair()
    assert registry.writes == [EXE]                                              # уже на запускателе — не пишем
    ready(False)                                                                 # антивирус убрал exe
    autostart.repair()
    assert registry.value == PYTHONW                                             # мост всё равно стартует при входе


def test_running_bridge_keeps_checking_so_a_removed_exe_never_breaks_the_next_login(monkeypatch):
    calls = []
    monkeypatch.setattr(autostart, 'repair', lambda: calls.append('repair') or (len(calls) > 1 and _raise()))
    pauses = []

    def sleep(seconds):
        pauses.append(seconds)
        if len(pauses) == 3:
            raise StopIteration

    with pytest.raises(StopIteration):
        autostart.watch(sleep=sleep)
    assert len(calls) == 3 and pauses == [autostart.REPAIR_SEC] * 3              # сбой реестра поток не гасит


def _raise():
    raise PermissionError('fixture')


@pytest.mark.parametrize('value', [None, r'"C:\other\studio-agent\Studio Agent.exe"',
                                   r'"C:\other\studio-agent\.venv\Scripts\pythonw.exe" "C:\other\studio-agent\studio_agent.pyw"'])
def test_start_leaves_a_missing_or_foreign_autostart_alone(monkeypatch, ready, value):
    registry = Registry(value)
    monkeypatch.setattr(autostart, 'winreg', registry)
    ready(True)
    autostart.repair()
    assert registry.writes == [] and registry.value == value


def test_start_refreshes_only_a_shortcut_of_this_copy(monkeypatch, ready, tmp_path):
    """Ярлык `install.ps1` (без AppUserModelID) и ярлык прежней версии мост приводит к своей цели; чужой и
    отсутствующий ярлык не трогает."""
    monkeypatch.setattr(autostart, 'winreg', Registry())
    ready(True)
    autostart.repair()
    assert shortcut.read() is None                                               # не было — и не появился
    full = shortcut.target
    monkeypatch.setattr(shortcut, 'target', lambda repo, launcher=False: full(repo, launcher)[:4] + ('',))
    shortcut.write(REPO)
    monkeypatch.setattr(shortcut, 'target', full)
    assert shortcut.read()[4] == ''
    autostart.repair()
    assert shortcut.read() == shortcut.target(REPO, True)                        # запускатель и AppUserModelID
    ready(False)
    autostart.repair()
    assert shortcut.read() == shortcut.target(REPO, False)
    other = tmp_path / 'other'
    shortcut.write(other)
    ready(True)
    autostart.repair()
    assert shortcut.read() == shortcut.target(other)


def test_helper_starts_and_switches_to_the_new_launcher_after_its_probe(tmp_path, monkeypatch, ready):
    old, stage = tmp_path / 'old', tmp_path / 'new'
    stage.mkdir()
    plan = {'pid': 1, 'home': str(tmp_path / 'home'), 'old_repo': str(old), 'old_python': 'py'}
    registry = Mock()
    registry.__enter__ = Mock(return_value=registry)
    registry.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(winreg, 'OpenKey', Mock(return_value=registry))
    set_value = Mock()
    monkeypatch.setattr(winreg, 'SetValueEx', set_value)
    (stage / '_update_ready').touch()
    for before in update_helper.startup_commands(old):                           # прежний — exe или pythonw
        for on in (True, False):
            ready(on)
            monkeypatch.setattr(winreg, 'QueryValueEx', Mock(return_value=(before, winreg.REG_SZ)))
            start = Mock()
            assert update_helper.activate(stage, plan, wait=lambda pid: True, start=start) == 'complete'
            exe, pythonw = update_helper.startup_commands(stage)
            assert set_value.call_args.args[-1] == (exe if on else pythonw)
            assert start.call_args.args[0] == ([str(stage / update_helper.LAUNCHER)] if on else
                                               [str(stage / '.venv/Scripts/pythonw.exe'), str(stage / 'studio_agent.pyw')])


def test_helper_shortcut_follows_the_probe(tmp_path, monkeypatch, ready):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    stage = tmp_path / 'local/Programs/studio-agent/releases/release-new'
    stage.mkdir(parents=True)
    ready(True)
    update_helper._shortcut(stage)
    assert shortcut.read() == shortcut.target(stage, True)
    ready(False)
    update_helper._shortcut(stage)
    assert shortcut.read() == shortcut.target(stage, False)


# --- настоящая сборка и запуск (integration) ---------------------------------------------------------------------

def _powershell(script, *args):
    return subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
                           str(script), *args], capture_output=True, timeout=60)


@pytest.fixture(scope='module')
def built(tmp_path_factory):
    """`Studio Agent.exe` из пакета с версией и значком этой копии — сборщиком выкладки (csc .NET Framework)."""
    folder = tmp_path_factory.mktemp('launcher')
    package = folder / 'studio-agent.zip'
    with zipfile.ZipFile(package, 'w') as z:
        for name in ('studio_agent/__init__.py', shortcut.ICON):
            z.write(REPO / name, 'studio-agent/' + name)
    exe = folder / 'build' / update_helper.LAUNCHER
    result = _powershell(DEPLOY / 'build-agent-launcher.ps1', '-Archive', str(package), '-Output', str(exe))
    assert result.returncode == 0, (result.stdout + result.stderr).decode('utf-8', 'replace')
    return exe


def _copy(built, tmp_path, *, venv=True):
    """Копия моста: запускатель, сценарий `studio_agent.pyw` и (по желанию) настоящий venv-лаунчер `pythonw.exe`."""
    repo = tmp_path / 'studio agent'
    repo.mkdir()
    shutil.copyfile(built, repo / update_helper.LAUNCHER)
    (repo / 'studio_agent.pyw').write_text(
        'import json, os, subprocess, sys, time\n'
        'from pathlib import Path\n'
        'child = None\n'
        'if os.environ["MODE"] == "hold":\n'
        '    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],\n'
        '        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP).pid\n'
        'Path(__file__).with_name("out.json").write_text(json.dumps({"pid": os.getpid(), "cwd": os.getcwd(),\n'
        '    "exe": sys.executable, "child": child, "mark": os.environ.get("MARK")}))\n'
        'if os.environ["MODE"] == "hold":\n'
        '    time.sleep(60)\n'
        'sys.exit(7)\n', encoding='utf-8')
    if venv:
        (repo / '.venv/Scripts').mkdir(parents=True)
        shutil.copyfile(Path(sys.executable).with_name('pythonw.exe'), repo / '.venv/Scripts/pythonw.exe')
        (repo / '.venv/pyvenv.cfg').write_text(f'home = {Path(sys._base_executable).parent}\n', encoding='utf-8')
    return repo


@pytest.mark.integration
def test_built_launcher_has_the_studio_name_version_and_icon(built):
    from studio_agent import VERSION
    script = ("$i = (Get-Item -LiteralPath $env:STUDIO_LAUNCHER).VersionInfo; "
              "[Console]::Write($i.FileDescription + '|' + $i.ProductName + '|' + $i.ProductVersion)")
    r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', script],
                       env={**os.environ, 'STUDIO_LAUNCHER': str(built)}, capture_output=True, text=True, timeout=60)
    assert r.stdout == f'Studio Agent|Studio Agent|{VERSION}', r.stderr
    assert b'\x00\x00\x01\x00' in built.read_bytes()                               # ресурс значка (GRPICONDIR)


@pytest.mark.integration
def test_probe_of_the_built_launcher(built, tmp_path):
    repo = _copy(built, tmp_path, venv=False)
    exe = repo / update_helper.LAUNCHER
    assert subprocess.run([str(exe), '--probe'], timeout=30).returncode == 78   # среды нет — не готов
    assert update_helper.launcher(repo) is None
    assert subprocess.run([str(exe), '--other'], timeout=30).returncode == 64
    (repo / '.venv/Scripts').mkdir(parents=True)
    (repo / '.venv/Scripts/pythonw.exe').write_bytes(b'')
    assert update_helper.launcher(repo) == exe


@pytest.mark.integration
def test_launcher_runs_venv_pythonw_as_its_child_and_returns_its_code(built, tmp_path):
    repo = _copy(built, tmp_path)
    r = subprocess.run([str(repo / update_helper.LAUNCHER)], env={**os.environ, 'MODE': 'exit', 'MARK': 'm'},
                       cwd=tmp_path, timeout=30)
    assert r.returncode == 7
    out = json.loads((repo / 'out.json').read_text(encoding='utf-8'))
    assert out['cwd'] == str(repo) and out['mark'] == 'm'
    assert os.path.normcase(out['exe']) == os.path.normcase(str(repo / '.venv/Scripts/pythonw.exe'))  # sys.executable


@pytest.mark.integration
def test_bridge_dies_with_the_launcher_but_its_detached_helper_survives(built, tmp_path):
    """KILL_ON_JOB_CLOSE — мост не переживает запускатель; SILENT_BREAKAWAY_OK — хелпер обновления (как
    `updater.launch`) не входит в задание и переживает выход старого моста."""
    repo = _copy(built, tmp_path)
    out = repo / 'out.json'
    launcher = subprocess.Popen([str(repo / update_helper.LAUNCHER)], env={**os.environ, 'MODE': 'hold'})
    info = None
    try:
        for _ in range(200):
            if out.is_file() and out.stat().st_size:
                info = json.loads(out.read_text(encoding='utf-8'))
                break
            time.sleep(0.05)
        assert info is not None
        launcher.kill()                                                            # только свой тестовый процесс
        launcher.wait(timeout=10)
        assert update_helper.wait_old(info['pid'], 10000)
        assert not update_helper.wait_old(info['child'], 300)
    finally:
        if launcher.poll() is None:
            launcher.kill()
        if info is not None and not update_helper.wait_old(info['child'], 0):
            os.kill(info['child'], 9)
