"""Границы автономной сборки PKG: недоверенные ZIP и платформенные зависимости."""
from contextlib import nullcontext
import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile
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
    assert '--cache-dir /Users/builder/projects/studio/.cache/studio-agent-macos' in builds[0][0][-1]
    assert all(timeout == 900 for argv, timeout in calls if not (argv[0] == 'ssh' and '--archive' in argv[-1]))


def test_runtime_cache_rechecks_pinned_digest_before_each_extraction(tmp_path, monkeypatch):
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode='w:gz') as archive:
        member = tarfile.TarInfo('python/marker')
        member.size = 3
        archive.addfile(member, io.BytesIO(b'sdk'))
    compressed = payload.getvalue()
    lock = {'version': '3.13.18', 'release': '20261110', 'arm64': hashlib.sha256(compressed).hexdigest()}
    cache = tmp_path / 'cache'
    cache.mkdir()
    fetched = []

    def urlopen(url, *, timeout):
        fetched.append(url)
        return io.BytesIO(compressed)

    monkeypatch.setattr(builder.urllib.request, 'urlopen', urlopen)
    monkeypatch.setattr(builder, 'cache_lock', lambda *_: nullcontext())
    for index in range(3):
        work = tmp_path / f'run-{index}'
        work.mkdir()
        builder.download_runtime(lock, 'arm64', work / 'python', work, cache)
        assert (work / 'python/marker').read_bytes() == b'sdk'
        assert len(fetched) == (1 if index < 2 else 2)
        if index == 1:
            (cache / 'runtime' / (lock['arm64'] + '.tar.gz')).write_bytes(b'corrupted')
    assert list((cache / 'runtime').iterdir()) == [cache / 'runtime' / (lock['arm64'] + '.tar.gz')]


def test_failed_runtime_download_never_reaches_shared_cache(tmp_path, monkeypatch):
    lock = {'version': '3.13.18', 'release': '20261110', 'arm64': 'a' * 64}
    cache = tmp_path / 'cache'
    cache.mkdir()
    monkeypatch.setattr(builder.urllib.request, 'urlopen', lambda *args, **kwargs: io.BytesIO(b'impostor'))
    monkeypatch.setattr(builder, 'cache_lock', lambda *_: nullcontext())
    with pytest.raises(ValueError, match='SHA-256'):
        builder.download_runtime(lock, 'arm64', tmp_path / 'python', tmp_path, cache)
    assert not (tmp_path / 'python').exists()
    assert list((cache / 'runtime').iterdir()) == []


def test_wheel_cache_skips_resolving_and_download_until_inputs_change_or_cache_breaks(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    requirements = source / 'requirements.txt'
    requirements.write_text('example==1.0\n', encoding='utf-8')
    constraints = source / 'constraints.txt'
    constraints.write_text('example==1.0\n', encoding='utf-8')
    cache = tmp_path / 'cache'
    lock = {'version': '3.13.18', 'arm64': 'a' * 64, 'x86_64': 'b' * 64}
    name = 'example-1.0-py3-none-any.whl'
    downloads = []
    monkeypatch.setattr(builder, 'cache_lock', lambda *_: nullcontext())

    def run(argv, *, env):
        if '--filter-requirements' in argv:
            builder.filter_requirements(*argv[-3:])
        else:
            downloads.append(argv)
            assert 'macosx_13_0_arm64' in argv
            assert env['PIP_CACHE_DIR'] == str(cache / 'pip')
            (argv[-1] / name).write_bytes(b'wheels')

    monkeypatch.setattr(builder, 'run', run)
    before = builder.wheelhouse_path(cache, lock, 'arm64', source)
    assert before != builder.wheelhouse_path(cache, lock, 'x86_64', source)
    for index, expected in enumerate((1, 1, 2, 3)):
        if index == 2:
            before.write_bytes(b'incomplete')
        if index == 3:
            constraints.write_text('example==1.1\n', encoding='utf-8')
            assert before != builder.wheelhouse_path(cache, lock, 'arm64', source)
        work = tmp_path / f'run-{index}'
        wheels = work / 'wheels' / 'arm64'
        builder.download_wheels(Path('python'), lock, 'arm64', source, wheels, work,
                                {'PIP_CACHE_DIR': str(cache / 'pip')}, cache)
        assert (wheels / name).read_bytes() == b'wheels'
        assert len(downloads) == expected


def test_cached_wheels_cannot_write_outside_the_staging_directory(tmp_path):
    archive = tmp_path / 'wheels.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        output.writestr('../escape.whl', b'outside')
    wheels = tmp_path / 'build/wheels'
    wheels.parent.mkdir()
    assert not builder.restore_wheelhouse(archive, wheels)
    assert list(wheels.parent.iterdir()) == []
