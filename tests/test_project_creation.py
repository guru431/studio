"""Создание проекта доступно владельцу через совместимый открытый мост."""
import pytest

from conftest import make_user, signin
from fake_bridge import FakeMachine, Refused, connect, enroll, pid
from test_code import url


def creation_machine(count=2, pin=None):
    machine = FakeMachine({'alpha': {}}, pin=pin)
    machine.project_create = True
    machine.roots = [{'id': f'{index + 1:016x}', 'path': f'C:\\Projects-{index + 1}'} for index in range(count)]
    machine._op_project_roots = lambda req, events: {'items': machine.roots}

    def create(req, events):
        if req['root'] not in {item['id'] for item in machine.roots}:
            raise Refused('not_found', 'папка проектов больше не разрешена — обновите список')
        if req['name'] in machine.projects:
            raise Refused('exists', 'проект или файл с таким именем уже существует')
        machine.projects[req['name']] = {'files': {}, 'chats': {}, 'git': None}
        return {'pid': pid(req['name']), 'name': req['name']}

    machine._op_project_create = create
    return machine


def setup_creation(studio, opened=True):
    make_user(studio, 'owner')
    aid, _ = enroll(studio)
    signin(studio, 'owner')
    machine = creation_machine(pin='2468')
    link = connect(studio, aid, machine)
    if opened:
        assert studio.c.post(url(aid, 'open'), json={'pin': '2468'}).status_code == 204
    return aid, machine, link


def test_owner_creates_in_selected_root_without_logging_paths_or_name(studio, caplog):
    aid, machine, link = setup_creation(studio)
    assert studio.c.get('/api/agents').json()['items'][0]['project_create'] is True
    roots = studio.c.get(url(aid, 'project-roots')).json()['items']
    assert roots == machine.roots
    response = studio.c.post(url(aid, 'projects'), json={'root': roots[1]['id'], 'name': 'secret-project'})
    assert response.status_code == 201 and response.json()['pid'] == pid('secret-project')
    assert machine.requests[-1]['op'] == 'project_create' and machine.requests[-1]['root'] == roots[1]['id']
    assert response.json() in studio.c.get(url(aid, 'projects')).json()['items']
    response = studio.c.post(url(aid, 'projects'), json={'root': roots[1]['id'], 'name': 'secret-project'})
    assert response.status_code == 409 and response.json()['detail']['code'] == 'exists'
    assert 'secret-project' not in caplog.text and roots[1]['path'] not in caplog.text


def test_closed_old_and_shared_computers_refuse_creation(studio):
    aid, machine, link = setup_creation(studio, opened=False)
    body = {'root': machine.roots[0]['id'], 'name': 'new'}
    assert studio.c.get(url(aid, 'project-roots')).json()['detail']['code'] == 'closed'
    assert studio.c.post(url(aid, 'projects'), json=body).json()['detail']['code'] == 'closed'
    link.hello['project_create'] = False
    for method, path, data in [('GET', 'project-roots', None), ('POST', 'projects', body)]:
        before = len(machine.requests)
        response = studio.c.request(method, url(aid, path), **({'json': data} if data else {}))
        assert response.status_code == 409 and response.json()['detail']['code'] == 'unsupported'
        assert len(machine.requests) == before
    link.hello['project_create'] = True
    assert studio.c.post(url(aid, 'open'), json={'pin': '2468'}).status_code == 204
    make_user(studio, 'friend')
    assert studio.c.post(url(aid, 'projects', pid('alpha'), 'members'), json={'login': 'friend'}).status_code == 201
    signin(studio, 'friend')
    assert studio.c.get('/api/agents').json()['items'][0]['project_create'] is False
    before = len(machine.requests)
    assert studio.c.get(url(aid, 'project-roots')).status_code == 404
    assert studio.c.post(url(aid, 'projects'), json=body).status_code == 404
    assert len(machine.requests) == before


@pytest.mark.parametrize('body', [{'root': 'C:/outside', 'name': 'new'}, {'root': '0' * 16, 'name': ''},
    {'root': '0' * 16, 'name': 'x' * 121}, {'root': '0' * 16, 'name': '\ud800'}])
def test_invalid_create_fields_are_refused_before_bridge(studio, body):
    aid, machine, _ = setup_creation(studio)
    before = len(machine.requests)
    # ASCII-JSON для одиночного суррогата, как у настоящего канала.
    import json
    response = studio.c.post(url(aid, 'projects'), content=json.dumps(body), headers={'Content-Type': 'application/json'})
    assert response.status_code == 422 and len(machine.requests) == before
