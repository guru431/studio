"""Каталоги моста вне выпуска на Windows — с наследуемым ACL, как выпуск (`test_release_acl.py`): папка данных
(`config.json`, `key.bin`, `pin.bin`), вложения хода, временный конфиг MCP, зеркало проекта. Режим 0o700 давал им
защищённый DACL `SYSTEM, Administrators, OWNER RIGHTS`: после первого запуска с повышением владелец —
Administrators, и обычный сеанс того же пользователя не читал бы настройки и ключ. Проверяется то же условие, что
у выпуска (повышение и второй сеанс не воспроизводятся): не защищён, всё унаследовано от папки профиля, без
`OWNER RIGHTS`."""
import base64
import os
import subprocess

import pytest

pytest.importorskip('winreg')

from studio_agent import attachments, host, mcp, mirror, store, winacl

from test_release_acl import assert_inherited, local  # noqa: F401 — фикстура `local`: LOCALAPPDATA как профиль


@pytest.fixture
def data_home(local, monkeypatch):
    monkeypatch.delenv('STUDIO_AGENT_HOME', raising=False)
    return local / 'studio-agent'


def test_data_folder_inherits_the_profile_acl(data_home, local):
    assert store.home() == data_home
    assert_inherited(data_home, local)
    store.save_config(data_home, store.Config(url='https://studio.example'))
    assert_inherited(data_home / 'config.json', local)


def test_attachments_of_a_turn_inherit_the_profile_acl(data_home, local):
    files = attachments.Files()
    try:
        text = files.prompt('task', [{'name': 'a.png', 'mime': 'image/png',
                                      'data': base64.b64encode(b'png').decode('ascii')}])
        assert 'file-1.png' in text
        folder = next((data_home / attachments.FOLDER).iterdir())
        for path in (data_home / attachments.FOLDER, folder, folder / 'file-1.png'):
            assert_inherited(path, local)
    finally:
        files.cleanup()


def test_runtime_mcp_config_inherits_the_profile_acl(data_home, local, monkeypatch):
    monkeypatch.setattr(mcp, 'load', lambda home, project, engine: {'mcpServers': {'s': {'command': 'x'}}})
    config = mcp.ClaudeConfig(store.home(), 'project')
    try:
        folder = mcp.Path(config.path).parent
        for path in (folder, config.path):
            assert_inherited(path, local)
    finally:
        config.cleanup()


def test_new_project_mirror_inherits_the_destination_acl(local):
    destination = local / 'mirrors' / 'copy'
    destination.parent.mkdir()
    transfer = mirror.Transfer(destination, 'test', 'ticket')
    try:
        assert_inherited(transfer.temp, local)
        transfer.write([['a.txt', 0, 'bmV3']])
        transfer.apply(mirror.fingerprint(None), {'a.txt': ['file', mirror.files.version(b'new'), 3, 0]}, [])
    finally:
        transfer.close()
    for path in (destination, destination / 'a.txt'):
        assert_inherited(path, local)


def test_start_repairs_a_data_folder_left_owner_only_by_an_earlier_version(data_home, local):
    """Папка данных 0o700 прежней версии и такой же вложенный каталог: запуск ядра (`store.repair_acl`) возвращает
    наследование — если сеанс вправе менять DACL (владелец; созданное с повышением чинит только сеанс с повышением)."""
    data_home.mkdir(mode=0o700)
    (data_home / attachments.FOLDER).mkdir(mode=0o700)
    (data_home / 'key.bin').write_bytes(b'key')
    assert winacl.owner_only(data_home) and winacl.owner_only(data_home / attachments.FOLDER)
    assert store.repair_acl(data_home) == 2
    for path in (data_home, data_home / attachments.FOLDER, data_home / 'key.bin'):
        assert_inherited(path, local)
    assert store.repair_acl(data_home) == 0


def test_repair_leaves_a_deliberately_protected_folder_alone(local):
    """Защищённый DACL без `OWNER RIGHTS` (не режим 0o700 Python) — чужое решение, не трогаем."""
    folder = local / 'locked'
    folder.mkdir()
    subprocess.run([os.path.join(os.environ['SystemRoot'], 'System32', 'icacls.exe'), str(folder),
                    '/inheritance:d'], capture_output=True, check=True, timeout=10)
    assert winacl.describe(folder)[0] and not winacl.owner_only(folder)
    assert winacl.repair_tree(folder) == 0 and winacl.describe(folder)[0]


def test_windows_code_creates_folders_only_through_host_helpers():
    """Сторож правила: режим 0o700 и `mkdtemp`/`TemporaryDirectory` — только в помощниках `host` и в модулях
    macOS/Linux (там режим POSIX и нужен)."""
    import re
    from pathlib import Path
    package = Path(host.__file__).parent
    pattern = re.compile(r'mkdtemp|TemporaryDirectory|mode=0o700|mkdir\([^)]*0o7')
    allowed = {'host.py', 'winacl.py', 'updater.py'}  # updater: только комментарии о прежнем `mkdtemp`
    offenders = []
    for path in sorted(package.glob('*.py')):
        if path.name in allowed or path.name.startswith(('linux_', 'macos_')):
            continue
        code = [line for line in path.read_text(encoding='utf-8').splitlines()
                if not line.lstrip().startswith('#')]
        offenders += [path.name for line in code if pattern.search(line)]
    assert offenders == []


def test_temporary_dir_is_removed_by_cleanup(local):
    folder = host.temporary_dir(local, 'probe-')
    assert_inherited(folder.name, local)
    folder.cleanup()
    assert not host.Path(folder.name).exists() and not list(local.glob('probe-*'))
    created = host.new_dir(local, 'x-')
    assert created.name.startswith('x-')
    assert_inherited(created, local)
