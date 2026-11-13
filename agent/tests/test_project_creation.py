"""Создание проекта: выбранный корень, имена Windows, отсутствие перезаписи и PIN."""
import asyncio
import _winapi

import pytest

from studio_agent import projects, store
from studio_agent.errors import Refused
from test_agent import agent, root, req


def test_create_in_selected_root_and_refresh_cached_catalog(tmp_path):
    first, second = tmp_path / 'work', tmp_path / 'personal'
    first.mkdir()
    second.mkdir()
    cfg = store.Config(projects_roots=[str(first), str(second)])
    catalog = projects.Catalog(now=lambda: 0)
    assert catalog.list(cfg) == []
    options = projects.list_roots(cfg)
    assert [item['path'] for item in options] == [str(first), str(second)]
    for option, root_path in zip(options, (first, second)):
        row = catalog.create(cfg, option['id'], 'Новый проект')
        assert catalog.find(cfg, row['pid']) == root_path / 'Новый проект'
        assert list((root_path / 'Новый проект').iterdir()) == []
    assert len(catalog.list(cfg)) == 2
    assert len({row['pid'] for row in catalog.list(cfg)}) == 2
    cfg.set_project_roots([str(second), str(first)])
    assert projects.list_roots(cfg) == options[::-1]
    assert len(catalog.list(cfg)) == 2


@pytest.mark.parametrize('name', ['', '.', '..', '.hidden', 'a/b', 'a\\b', 'C:escape', 'x.', 'x ',
    'CON', 'nul.txt', 'COM¹', 'NUL .txt', 'x\x00y', 'x\ny', 'x\ud800y', 'x' * 121])
def test_invalid_project_names_never_create_a_directory(tmp_path, name):
    option = projects.list_roots(tmp_path)[0]
    with pytest.raises(Refused, match='invalid'):
        projects.create_project(tmp_path, option['id'], name)
    assert list(tmp_path.iterdir()) == []


def test_existing_files_directories_and_junctions_are_preserved(tmp_path):
    root, outside = tmp_path / 'projects', tmp_path / 'outside'
    root.mkdir()
    outside.mkdir()
    option = projects.list_roots(root)[0]
    (root / 'demo').mkdir()
    (root / 'file').write_text('keep', encoding='utf-8')
    _winapi.CreateJunction(str(outside), str(root / 'linked'))
    for name, code in [('demo', 'exists'), ('file', 'exists'), ('linked', 'outside')]:
        with pytest.raises(Refused, match=code):
            projects.create_project(root, option['id'], name)
    assert (root / 'file').read_text(encoding='utf-8') == 'keep'
    assert list(outside.iterdir()) == []


def test_stale_unknown_and_missing_roots_do_not_create_parents(tmp_path):
    root = tmp_path / 'missing'
    option = projects.list_roots(root)[0]
    with pytest.raises(Refused, match='not_found'):
        projects.create_project(root, option['id'], 'demo')
    assert not root.exists()
    root.mkdir()
    with pytest.raises(Refused, match='not_found'):
        projects.create_project(root, '0' * 16, 'demo')
    cfg = store.Config(projects_roots=[str(root)])
    cfg.set_project_roots([str(tmp_path)])
    with pytest.raises(Refused, match='not_found'):
        projects.create_project(cfg, option['id'], 'demo')
    assert list(root.iterdir()) == []


async def test_creation_requires_open_owner_session_and_rejects_shared_scope(agent, root):
    assert agent.hello()['project_create'] is True
    option = projects.list_roots(agent.cfg)[0]
    fields = {'root': option['id'], 'name': 'новый'}
    assert (await agent.handle(req('project_roots')))[0]['code'] == 'closed'
    assert (await agent.handle(req('project_create', **fields)))[0]['code'] == 'closed'
    await agent.handle(req('open', pin=None))
    for op, params in [('project_roots', {}), ('project_create', fields)]:
        response = (await agent.handle(req(op, shared_projects=[projects.pid('demo')], **params)))[0]
        assert response['code'] == 'closed'
    assert (await agent.handle(req('project_roots')))[0]['data']['items'] == [option]
    responses = await asyncio.gather(*(agent.handle(req('project_create', **fields)) for _ in range(2)))
    assert sum(response[0]['ok'] for response in responses) == 1
    assert [response[0].get('code') for response in responses].count('exists') == 1
    row = next(response[0]['data'] for response in responses if response[0]['ok'])
    assert (root / 'новый').is_dir()
    assert row in (await agent.handle(req('projects')))[0]['data']['items']


@pytest.mark.parametrize('state', ['updating', 'exiting'])
async def test_creation_is_blocked_while_agent_is_updating_or_exiting(agent, root, state):
    from types import SimpleNamespace
    await agent.handle(req('open', pin=None))
    if state == 'updating':
        agent.updater = SimpleNamespace(busy=True)
    else:
        agent.local_exit_pending = True
    response = (await agent.handle(req('project_create', root=projects.list_roots(agent.cfg)[0]['id'], name='new')))[0]
    assert response['code'] == 'busy' and not (root / 'new').exists()
