"""Ярлык «Studio Agent» в меню «Пуск» (Windows): команда автозапуска, настоящий `.lnk` во временной `APPDATA`
(фикстура `start_menu`), перевод на новый выпуск при самообновлении — пакетом и автономным хелпером."""
import json
import runpy
import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest

winreg = pytest.importorskip('winreg')

from studio_agent import autostart, shortcut, update_helper


@pytest.fixture
def releases(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'local'))
    monkeypatch.setattr(update_helper, 'running_releases', lambda base: set())
    monkeypatch.setattr(update_helper, '_autostart', lambda: '')
    base = tmp_path / 'local/Programs/studio-agent/releases'
    for name in ('release-old', 'release-new'):
        (base / name).mkdir(parents=True)
    stage = base / 'release-new'
    plan = {'pid': 1, 'home': str(tmp_path / 'home'), 'old_repo': str(base / 'release-old'), 'old_python': 'py'}
    (stage / '_update_plan.json').write_text(json.dumps(plan), encoding='utf-8')
    return stage, plan


def test_shortcut_runs_the_same_command_as_autostart(start_menu):
    program, arguments, folder, icon = shortcut.target(autostart.REPO)
    assert f'"{program}" {arguments}' == autostart.command()  # pythonw.exe — без окна консоли
    assert folder == str(autostart.REPO)
    assert icon.endswith('.ico') and Path(icon).is_file()      # значок студии в копии, не значок Python
    assert shortcut.path() == start_menu / 'Studio Agent.lnk'


def test_shortcut_of_an_older_version_gets_the_studio_icon(tmp_path, start_menu, monkeypatch):
    """Ярлык до 0.44.2 — без значка: `ensure` (новая копия после `complete`) переписывает его."""
    repo = tmp_path / 'release-abc'
    full = shortcut.target
    monkeypatch.setattr(shortcut, 'target', lambda r: full(r)[:3] + ('',))
    shortcut.write(repo)
    monkeypatch.setattr(shortcut, 'target', full)
    assert shortcut.read() == full(repo)[:3] + ('',)
    assert shortcut.ensure(repo) is True and shortcut.read() == shortcut.target(repo)


def test_real_shortcut_is_written_read_and_moved_to_another_copy(tmp_path, start_menu):
    repo = tmp_path / 'Programs' / 'studio agent' / 'releases' / 'release-abc'  # пробел в пути — в кавычках
    assert shortcut.read() is None
    assert shortcut.ensure(repo) is True
    assert shortcut.read() == shortcut.target(repo)
    assert shortcut.ensure(repo) is False                      # уже указывает сюда — не переписывается
    other = tmp_path / 'other'
    assert shortcut.ensure(other) is True and shortcut.read() == shortcut.target(other)
    shortcut.path().write_bytes(b'not a shell link')
    assert shortcut.ensure(repo) is True and shortcut.read() == shortcut.target(repo)


def test_confirmed_start_of_a_release_points_the_shortcut_to_it(releases, start_menu):
    """И переход, который вёл хелпер прежней версии без ярлыка: новая копия ставит его сама после `complete`."""
    stage, plan = releases
    (stage / '_update_result').write_text('complete', encoding='ascii')
    assert update_helper.cleanup_started(stage) is True
    assert shortcut.read() == shortcut.target(stage)


@pytest.mark.parametrize('result', ['rolled_back', 'failed'])
def test_unconfirmed_release_leaves_the_shortcut_alone(releases, start_menu, result):
    stage, plan = releases
    (stage / '_update_result').write_text(result, encoding='ascii')
    assert update_helper.cleanup_started(stage) is True
    assert shortcut.read() is None


def test_helper_moves_the_shortcut_with_autostart_after_the_new_copy_is_ready(releases, start_menu, monkeypatch):
    stage, plan = releases
    shortcut.write(plan['old_repo'])
    monkeypatch.setattr(update_helper, '_switch_windows', Mock(return_value=''))
    (stage / '_update_ready').touch()
    assert update_helper.activate(stage, plan, wait=lambda pid: True, start=Mock()) == 'complete'
    assert shortcut.read() == shortcut.target(stage)


def test_copy_outside_the_install_folder_never_writes_the_shortcut(tmp_path, start_menu, monkeypatch):
    """Разработческая копия или тест: ни ярлыка, ни перезаписи ярлыка установленной копии."""
    monkeypatch.setattr(update_helper, '_switch_windows', Mock(return_value=''))
    plan = {'pid': 1, 'home': str(tmp_path), 'old_repo': str(tmp_path / 'old'), 'old_python': 'py'}
    (tmp_path / '_update_ready').touch()
    assert update_helper.activate(tmp_path, plan, wait=lambda pid: True, start=Mock()) == 'complete'
    assert shortcut.read() is None


def test_standalone_helper_uses_its_trusted_shortcut_copy(releases, start_menu):
    """Хелпер в выпуске — `_update_helper.py` вне пакета: ярлык пишет копия `_shortcut.py` (`updater.prepare`)."""
    stage, plan = releases
    shutil.copyfile(update_helper.__file__, stage / '_update_helper.py')
    helper = runpy.run_path(str(stage / '_update_helper.py'))
    helper['_shortcut'](stage)
    assert shortcut.read() is None                             # копии ярлыка нет — тихо без ярлыка
    shutil.copyfile(shortcut.__file__, stage / '_shortcut.py')
    helper['_shortcut'](stage)
    assert shortcut.read() == shortcut.target(stage)
