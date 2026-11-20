from contextlib import nullcontext
import hashlib
import io
import json
from pathlib import Path
import plistlib
import shutil
from unittest.mock import Mock
import zipfile

import pytest

from studio_agent import macos_signed as signed, updater


def fake_app(app, version='1.2.3', team='EXAMPLE123', capture=False):
    contents = app / 'Contents'
    contents.mkdir(parents=True)
    (contents / 'Info.plist').write_bytes(plistlib.dumps({
        'CFBundleIdentifier': signed.ID, 'StudioSigningTeam': team, 'StudioPersistentCapture': capture,
        'CFBundleShortVersionString': version}))
    return app


def test_signed_update_uses_current_publisher_and_denies_entitlement_downgrade(tmp_path, monkeypatch):
    app = fake_app(tmp_path / signed.NAME)
    run = Mock()
    monkeypatch.setattr(signed.subprocess, 'run', run)
    for team, version, capture in [('OTHER12345', '1.2.3', False), ('EXAMPLE123', '1.2.4', False),
                                   ('EXAMPLE123', '1.2.3', True)]:
        with pytest.raises(ValueError):
            signed.verify(app, team, version, capture=capture)
    run.assert_not_called()
    signed.verify(app, 'EXAMPLE123', '1.2.3')
    args = run.call_args.args[0]
    requirement = args[args.index('-R') + 1]
    assert requirement.startswith('=identifier') and 'anchor apple generic' in requirement
    assert 'EXAMPLE123' in requirement and signed.ID in requirement
    assert '1.2.840.113635.100.6.1.13' in requirement


def test_signed_update_checks_pkg_digest_before_extraction(tmp_path, monkeypatch):
    archive = tmp_path / 'release.zip'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('studio-agent/macos/signed-package.json', json.dumps({'size': 5, 'sha256': '0' * 64}))
    opener = Mock()
    opener.open.return_value = io.BytesIO(b'other')
    monkeypatch.setattr(updater.urllib.request, 'build_opener', lambda *args: opener)
    monkeypatch.setattr(updater.subprocess, 'run', lambda *a, **kw: pytest.fail('invalid PKG was extracted'))
    with pytest.raises(ValueError, match='checksum'):
        updater._prepare_signed(Path('/Applications') / signed.NAME, tmp_path, archive,
                               'https://studio.example/api/agent/package', 'fixture', {})
    request = opener.open.call_args.args[0]
    assert request.full_url.endswith('?platform=macos') and request.get_header('Authorization') == 'Bearer fixture'


def test_signed_update_prepares_only_the_verified_app(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    current = fake_app(tmp_path / 'Applications' / signed.NAME, capture=True)
    stage = tmp_path / 'incoming'
    stage.mkdir()
    archive = stage / 'release.zip'
    pkg = b'xar!fixture'
    with zipfile.ZipFile(archive, 'w') as z:
        z.writestr('studio-agent/macos/signed-package.json', json.dumps(
            {'size': len(pkg), 'sha256': hashlib.sha256(pkg).hexdigest()}))
        z.writestr('studio-agent/_macos_signed.py', 'untrusted helper')
    opener = Mock()
    opener.open.return_value = io.BytesIO(pkg)
    monkeypatch.setattr(updater.urllib.request, 'build_opener', lambda *args: opener)

    def expand(args, **kwargs):
        assert args[:2] == ['/usr/sbin/pkgutil', '--expand-full']
        assert Path(args[2]).read_bytes() == pkg
        fake_app(signed.candidate(stage), version='2.0', capture=True)

    monkeypatch.setattr(updater.subprocess, 'run', expand)
    verify = Mock()
    monkeypatch.setattr(signed, 'verify', verify)
    updater._prepare_signed(current, stage, archive, 'https://studio.example/api/agent/package', 'key', {'version': '2.0'})
    verify.assert_called_once_with(signed.candidate(stage), 'EXAMPLE123', '2.0', capture=True)
    assert list(stage.iterdir()) == [stage / 'expanded']
    assert not archive.exists() and not (stage / 'agent.pkg').exists()


def test_sealed_app_and_writable_readiness_marker_are_distinct(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    bundle = fake_app(tmp_path / signed.NAME)
    repo = bundle / 'Contents/Resources/agent'
    stage = tmp_path / 'Library/Application Support/Studio Agent/app/releases/release-test'
    stage.mkdir(parents=True)
    assert signed.sealed_app(repo) == bundle and signed.sealed_app(stage) is None
    assert signed.ready_marker(str(stage / '_update_ready'))
    assert not signed.ready_marker(str(repo / '_update_ready'))


def test_signed_handoff_runs_the_helper_from_the_installed_bundle(install_root, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: install_root)
    app = fake_app(install_root / 'Applications' / signed.NAME)
    repo = app / 'Contents/Resources/agent'
    stage = install_root / 'Library/Application Support/Studio Agent/app/releases/release-qa'
    new = fake_app(signed.candidate(stage), version='2.0')
    monkeypatch.setattr(updater.host, 'MACOS', True)
    monkeypatch.setattr(updater, 'REPO', repo)
    monkeypatch.setattr(signed, 'verify', Mock())
    start = Mock()
    monkeypatch.setattr(updater.subprocess, 'Popen', start)
    updater.launch(stage, install_root)
    args = start.call_args.args[0]
    assert args[:5] == [str(signed.interpreter(new)), '-I', '-B', str(repo / 'studio_agent/macos_signed.py'), '--update']
    assert args[5] == str(stage) and args[-1] == '2.0'
    # Ручной вызов сторонним Python не может удалить даже правильно названный каталог выпуска.
    executable = signed.interpreter(new)
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(signed.sys, 'argv', args[3:])
    with pytest.raises(ValueError, match='Хелпер'):
        signed.main()
    assert stage.is_dir()


@pytest.mark.parametrize('error, result', [(signed.RolledBack('exit'), 'rolled_back'), (ValueError('team'), 'failed'),
                                           (None, 'old_running'), (signed.NotReady('slow'), None)])
def test_failed_signed_update_leaves_its_result_for_the_running_copy(install_root, monkeypatch, error, result):
    """Подписанный переход macOS: провал доходит до студии (`update_helper.failed_update`), как у хелпера ZIP."""
    from studio_agent import update_helper

    monkeypatch.setattr(Path, 'home', lambda: install_root)
    app = fake_app(install_root / 'Applications' / signed.NAME)
    stage = install_root / 'Library/Application Support/Studio Agent/app/releases/release-qa'
    new = fake_app(signed.candidate(stage), version='2.0')
    executable = signed.interpreter(new)
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(signed.sys, 'executable', str(executable))
    monkeypatch.setattr(signed.sys, 'argv', ['macos_signed.py', '--update', str(stage), '1', '2.0'])
    monkeypatch.setattr(signed, 'sealed_app', lambda repo: app)
    monkeypatch.setattr(signed.runpy, 'run_path', lambda path: {'wait_exit': lambda pid: error is not None})
    reopen = Mock()
    monkeypatch.setattr(signed.subprocess, 'run', reopen)

    def replace(*args, **kwargs):
        raise error

    monkeypatch.setattr(signed, 'replace_app', replace)
    with pytest.raises(BlockingIOError if error is None else type(error)):
        signed.main()
    monkeypatch.setattr(update_helper, 'MACOS', True)
    monkeypatch.setattr(update_helper, 'LINUX', False)
    if result is None:  # новая копия на месте и может подняться — неудачей не считаем
        assert not stage.exists()
        assert update_helper.failed_update(app / 'Contents/Resources/agent') is None
        return
    assert [path.name for path in stage.iterdir()] == ['_update_result']
    assert update_helper.failed_update(app / 'Contents/Resources/agent') == result
    assert update_helper.failed_update(install_root / 'elsewhere.app/Contents/Resources/agent') is None


@pytest.mark.parametrize('outcome', ['ready', 'exit', 'copy-error', 'wrong-team'])
def test_swap_waits_for_readiness_or_preserves_the_installed_app(tmp_path, monkeypatch, outcome):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    stage = tmp_path / 'Library/Application Support/Studio Agent/app/releases/release-signed'
    source = fake_app(stage / signed.NAME, version='2.0')
    target = fake_app(tmp_path / 'Applications' / signed.NAME, version='1.0')
    monkeypatch.setattr(signed, '_lock', lambda path: nullcontext())

    def copy(argv, **kwargs):
        assert argv[0] == '/usr/bin/ditto'
        if outcome == 'copy-error':
            raise OSError('disk full')
        shutil.copytree(argv[1], argv[2])

    def start(argv):
        assert signed.info(target)['CFBundleShortVersionString'] == '2.0'
        assert str(target) == argv[-1]
        if outcome == 'ready':
            (stage / '_update_ready').touch()
        return Mock(poll=lambda: 1)

    monkeypatch.setattr(signed.subprocess, 'run', copy)
    monkeypatch.setattr(signed.subprocess, 'Popen', start)
    monkeypatch.setattr(signed, 'verify', lambda app, team, version, **kwargs:
                        None if team == 'EXAMPLE123' else pytest.fail('untrusted publisher'))
    if outcome == 'ready':
        signed.replace_app(source, target, 'EXAMPLE123', '2.0', wait_ready=stage)
        assert signed.info(target)['CFBundleShortVersionString'] == '2.0'
    else:
        with pytest.raises((ValueError, RuntimeError, OSError)):
            signed.replace_app(source, target, 'OTHER12345' if outcome == 'wrong-team' else 'EXAMPLE123',
                               '2.0', wait_ready=stage)
        assert signed.info(target)['CFBundleShortVersionString'] == '1.0'
    assert list(target.parent.iterdir()) == [target]
