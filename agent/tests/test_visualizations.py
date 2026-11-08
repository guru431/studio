"""Visualization roots, size/encoding limits and PIN checks use real temporary files."""
import pytest
import _winapi

from studio_agent import limits, projects, visualizations
from studio_agent.errors import Refused
from test_agent import make, req, SESSION


def test_read_project_and_codex_visualizations(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    codex = tmp_path / 'custom-codex'
    monkeypatch.setenv('CODEX_HOME', str(codex))
    output = codex / 'visualizations' / '2026' / '11' / '08' / 'thread' / 'схема.html'
    output.parent.mkdir(parents=True)
    output.write_text('<button>Готово</button>', encoding='utf-8')
    local = project / 'view.html'
    local.write_text('<p>Project</p>', encoding='utf-8')
    assert visualizations.read(project, str(output))['text'] == '<button>Готово</button>'
    assert visualizations.read(project, 'view.html')['text'] == '<p>Project</p>'
    assert visualizations.read(project, str(local))['text'] == '<p>Project</p>'


def test_reject_outside_traversal_and_non_html(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex'))
    project = tmp_path / 'project'
    project.mkdir()
    private = tmp_path / 'private.html'
    private.write_text('private', encoding='utf-8')
    for source in (str(private), '../private.html', str(project / '..' / 'private.html'),
                   'view.txt', 'view.html\x00', '//server/share/view.html'):
        with pytest.raises(Refused):
            visualizations.read(project, source)


def test_read_is_bounded_and_utf8(tmp_path, monkeypatch):
    path = tmp_path / 'view.html'
    path.write_bytes(b'\xff')
    with pytest.raises(Refused) as caught:
        visualizations.read(tmp_path, 'view.html')
    assert caught.value.code == 'binary'
    monkeypatch.setattr(limits, 'TEXT_MAX', 4)
    path.write_bytes(b'12345')
    with pytest.raises(Refused) as caught:
        visualizations.read(tmp_path, str(path))
    assert caught.value.code == 'too_large'


@pytest.mark.parametrize('root_link', [False, True])
def test_codex_visualizations_cannot_follow_junctions(tmp_path, monkeypatch, root_link):
    project = tmp_path / 'project'
    project.mkdir()
    codex = tmp_path / 'codex'
    codex.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(codex))
    private = tmp_path / 'private'
    private.mkdir()
    (private / 'secret.html').write_text('private', encoding='utf-8')
    root = codex / 'visualizations'
    if root_link:
        _winapi.CreateJunction(str(private), str(root))
        source = root / 'secret.html'
    else:
        root.mkdir()
        _winapi.CreateJunction(str(private), str(root / 'linked'))
        source = root / 'linked' / 'secret.html'
    with pytest.raises(Refused) as caught:
        visualizations.read(project, str(source))
    assert caught.value.code == 'outside'


async def test_bridge_requires_pin_and_rechecks_it_after_read(home, tmp_path, monkeypatch):
    root = tmp_path / 'projects'
    project = root / 'demo'
    project.mkdir(parents=True)
    (project / 'view.html').write_text('<p>private</p>', encoding='utf-8')
    agent = make(home, root)
    frame = req('visualization', project=projects.pid('demo'), source='view.html')
    assert (await agent.handle(frame))[0]['code'] == 'closed'
    await agent.handle(req('open', pin=None))
    assert (await agent.handle(frame))[0]['data']['text'] == '<p>private</p>'
    original = visualizations.read

    def revoke(*args):
        result = original(*args)
        agent.open.close(SESSION)
        return result

    monkeypatch.setattr(visualizations, 'read', revoke)
    assert (await agent.handle(frame))[0]['code'] == 'closed'
