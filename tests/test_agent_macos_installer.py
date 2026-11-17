"""Границы автономной сборки PKG: недоверенные ZIP и платформенные зависимости."""
import importlib.util
from pathlib import Path
import zipfile

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
