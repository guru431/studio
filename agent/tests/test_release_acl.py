"""Права каталога выпуска Windows: ACL наследуется от каталога установки (находка «Windows-автообновление может
лишить обычный сеанс прав запуска»).

Сценарий находки: мост работал с повышением, самообновление создало выпуск `tempfile.mkdtemp` — защищённый DACL
`SYSTEM, Administrators, OWNER RIGHTS`, владелец — группа Administrators; после входа обычный сеанс того же
пользователя запускает выпуск из HKCU Run и получает «Отказано в доступе». Повышение и второй сеанс в тесте не
воспроизводятся (UAC); проверяется условие, от которого доступ обычного сеанса больше не зависит от владельца:
DACL выпуска и вложенных — не защищён, все ACE унаследованы от каталога установки, ACE `OWNER RIGHTS` нет. Права
пользователя тогда те же, что у каталога установки в его профиле, кто бы ни стал владельцем."""
import hashlib
import io
import os
import subprocess
import tempfile
from unittest.mock import Mock

import pytest

pytest.importorskip('winreg')

from studio_agent import store, updater, winacl

from test_updater import FUTURE, bundle


@pytest.fixture
def local(tmp_path, monkeypatch):
    """`LOCALAPPDATA` с DACL, как у папки профиля: SYSTEM, Administrators и сам пользователь, без наследования.
    Временная папка pytest создана с режимом 0o700 и сама несёт `OWNER RIGHTS` — от неё выпуск унаследовал бы его."""
    folder = tmp_path / 'local'
    folder.mkdir()
    subprocess.run([os.path.join(os.environ['SystemRoot'], 'System32', 'icacls.exe'), str(folder),
                    '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F',
                    os.environ['USERNAME'] + ':(OI)(CI)F'], capture_output=True, check=True, timeout=10)
    monkeypatch.setenv('LOCALAPPDATA', str(folder))
    return folder


def assert_inherited(path, local):
    """Не защищён, все ACE унаследованы и это ровно права папки профиля (с пользователем), без `OWNER RIGHTS`."""
    protected, entries = winacl.describe(path)
    assert not protected, path
    assert entries and all(inherited for _, inherited in entries), (path, entries)
    sids = {sid for sid, _ in entries}
    assert winacl.OWNER_RIGHTS not in sids and sids == {sid for sid, _ in winacl.describe(local)[1]}, (path, entries)


def test_update_release_and_its_build_folder_inherit_the_install_folder_acl(home, tmp_path, local, monkeypatch):
    archive = bundle(tmp_path / 'package.zip').read_bytes()
    store.save_config(home, store.Config(url='https://studio.example'))
    monkeypatch.setattr(store, 'load_key', lambda h: 'device-key')
    opener = Mock()
    opener.open.side_effect = lambda *a, **kw: io.BytesIO(archive)
    monkeypatch.setattr(updater.urllib.request, 'build_opener', lambda *a: opener)
    seen = []

    def run(command, **kwargs):
        seen.append(kwargs['env']['TMP'])
        assert_inherited(kwargs['env']['TMP'], local)  # временная папка pip внутри выпуска

    monkeypatch.setattr(updater.subprocess, 'run', run)
    stage = updater.prepare(home, {**FUTURE, 'size': len(archive), 'sha256': hashlib.sha256(archive).hexdigest()})
    try:
        assert updater.update_helper.RELEASE_NAME.fullmatch(stage.name)
        assert stage.parent == local / 'Programs/studio-agent/releases'
        for path in (stage, stage / 'studio_agent', stage / 'studio_agent.pyw', stage / '_update_helper.py'):
            assert_inherited(path, local)
        assert len(seen) == 4 and not any(map(lambda p: updater.Path(p).exists(), seen))
    finally:
        updater.discard(stage)


def test_start_repairs_a_release_left_protected_by_an_earlier_version(local):
    """Выпуск прежней версии (`mkdtemp`) чинит запуск установленной копии — если сеанс вправе менять его DACL
    (тест работает от владельца выпуска; выпуск чужого владельца-Administrators обычный сеанс не исправит)."""
    base = local / 'Programs/studio-agent/releases'
    base.mkdir(parents=True)
    old = updater.Path(tempfile.mkdtemp(prefix='release-', dir=base))
    (old / 'studio_agent').mkdir()
    (old / 'studio_agent' / 'app.py').write_text('', encoding='ascii')
    current = base / 'release-current'
    current.mkdir()
    assert winacl.describe(old)[0]  # так выпуск создавали версии до 0.43.0
    assert updater.repair_acl(current) == 1
    for path in (old, old / 'studio_agent', old / 'studio_agent' / 'app.py'):
        assert_inherited(path, local)
    assert updater.repair_acl(current) == 0  # уже наследует — повтор ничего не меняет


def test_repair_touches_only_an_installed_copy(tmp_path, local):
    """Разработческая копия (git) и тесты не трогают папку установки пользователя."""
    base = local / 'Programs/studio-agent/releases'
    base.mkdir(parents=True)
    old = updater.Path(tempfile.mkdtemp(prefix='release-', dir=base))
    assert updater.repair_acl(tmp_path / 'checkout') == 0
    assert winacl.describe(old)[0]
