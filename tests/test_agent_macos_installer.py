"""Границы автономной сборки PKG: недоверенные ZIP и платформенные зависимости."""
import importlib.util
from pathlib import Path
import zipfile
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('macos_builder', ROOT / 'deploy/build-agent-macos.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


@pytest.mark.parametrize('name', ['studio-agent/../../escape', '/absolute',
                                  'studio-agent/dir\\escape', 'another/install_macos.py'])
def test_source_archive_rejects_escape_before_writing(tmp_path, name):
    archive = tmp_path / 'source.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('studio-agent/good.py', 'safe')
        member = zipfile.ZipInfo(name)
        member.filename = name  # ZipInfo на Windows иначе заранее нормализует обратный слеш.
        output.writestr(member, 'bad')
    target = tmp_path / 'source'
    with pytest.raises(ValueError):
        builder.unpack_source(archive, target)
    assert not target.exists()


def test_source_archive_rejects_symlinks_and_case_collisions(tmp_path):
    archive = tmp_path / 'source.zip'
    link = zipfile.ZipInfo('studio-agent/link')
    link.external_attr = 0o120777 << 16
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr(link, 'outside')
    with pytest.raises(ValueError):
        builder.unpack_source(archive, tmp_path / 'source')
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('studio-agent/File', 'one')
        output.writestr('studio-agent/file', 'two')
    with pytest.raises(ValueError):
        builder.unpack_source(archive, tmp_path / 'source')


@pytest.mark.parametrize('arch,crypto', [('x86_64', '48.0.1'), ('arm64', '50.0.2')])
def test_cross_architecture_constraints_use_the_target_markers(tmp_path, arch, crypto):
    target = tmp_path / 'requirements.txt'
    builder.filter_requirements(ROOT / 'agent/requirements.txt', target, arch)
    lines = target.read_text(encoding='utf-8').splitlines()
    assert 'cryptography==' + crypto in lines
    assert len([line for line in lines if line.startswith('cryptography==')]) == 1
    assert not any(';' in line for line in lines)


def test_remote_build_has_time_for_both_architectures_without_extending_transfer_timeouts(tmp_path, monkeypatch):
    work = tmp_path / 'work'
    work.mkdir()
    calls = []

    def run(argv, *, timeout, **kwargs):
        calls.append((argv, timeout))
        if argv[0] == 'ssh' and '--archive' in argv[-1]:
            if timeout < 3600:
                raise builder.subprocess.TimeoutExpired(argv, timeout)
        if argv[0] == 'scp' and argv[-1] == str(work / 'studio-agent-setup.pkg'):
            Path(argv[-1]).write_bytes(b'pkg')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(builder.subprocess, 'run', run)
    args = SimpleNamespace(remote_root='/Users/builder/projects/studio', key=None, port='22',
                           host='builder@macos.example.net', archive=tmp_path / 'source.zip',
                           lock=tmp_path / 'lock.json', output=tmp_path / 'result.pkg')
    builder.remote(args, work)
    assert args.output.read_bytes() == b'pkg'
    builds = [(argv, timeout) for argv, timeout in calls if argv[0] == 'ssh' and '--archive' in argv[-1]]
    assert len(builds) == 1 and builds[0][1] == 3600
    assert all(timeout == 900 for argv, timeout in calls if not (argv[0] == 'ssh' and '--archive' in argv[-1]))
