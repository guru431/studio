"""Владение, PIN, совместимость и фильтрация сведений компьютера."""
import logging

import bridge
import auth
from conftest import make_user, signin
from test_code import setup


def test_computer_info_requires_capability_pin_ownership_and_never_logs_details(studio, caplog):
    aid, machine, link = setup(studio, pin='2468', opened=False)
    path = f'/api/code/agents/{aid}/info'
    assert studio.c.get(path).json()['detail']['code'] == 'unsupported'
    link.hello['computer_info'] = True
    assert studio.c.get('/api/agents').json()['items'][0]['computer_info'] is True
    assert studio.c.get(path).json()['detail']['code'] == 'closed'
    assert studio.c.post(f'/api/code/agents/{aid}/open', json={'pin': '2468'}).status_code == 204
    machine.computer_data = {'hostname': 'private-machine', 'username': 'private-user',
                             'os': 'Windows 11', 'projects_roots': ['C:\\private-projects'],
                             'internal_ips': ['10.77.1.4'], 'external_ip': '8.8.8.8',
                             'memory': {'total': 100, 'used': 60, 'available': 40},
                             'disks': [{'path': 'C:\\', 'total': 100, 'free': 0}], 'checked_at': 200}
    with caplog.at_level(logging.INFO):
        response = studio.c.get(path)
    assert response.json() == machine.computer_data
    assert response.headers['cache-control'] == 'no-store'
    assert machine.requests[-1]['op'] == 'computer_info'
    assert all(value not in caplog.text for value in ('private-machine', 'private-user', 'private-projects'))
    make_user(studio, 'other')
    signin(studio, 'other')
    before = len(machine.requests)
    assert studio.c.get(path).status_code == 404
    assert len(machine.requests) == before
    signin(studio, 'owner')
    bridge.LINKS.pop(aid)
    assert studio.c.get(path).json()['detail']['code'] == 'offline'


def test_computer_info_filters_unknown_fields_invalid_metrics_and_addresses(studio):
    aid, machine, link = setup(studio)
    link.hello['computer_info'] = True
    machine.computer_data = {'hostname': ['invalid'], 'username': 'bad\nname', 'os': 'Windows',
        'external_ip': '10.77.1.1', 'internal_ips': ['10.77.1.4', '10.77.1.4', '<script>', 10],
        'memory': {'total': 100, 'used': True, 'available': 10}, 'checked_at': True,
        'disks': [{'path': 'C:\\', 'total': 100, 'free': 101, 'secret': 'hidden'},
                  {'path': 'D:\\', 'total': True, 'free': -1}, {'path': '\n'}, None],
        'projects_roots': ['D:\\Проекты\ud800', 'D:\\Проекты\ud800', 3], 'secret': 'hidden'}
    assert studio.c.get(f'/api/code/agents/{aid}/info').json() == {
        'hostname': None, 'username': None, 'os': 'Windows', 'external_ip': None,
        'internal_ips': ['10.77.1.4'], 'memory': None, 'checked_at': None,
        'disks': [{'path': 'C:\\', 'total': 100, 'free': None}, {'path': 'D:\\', 'total': None, 'free': None}],
        'projects_roots': ['D:\\Проекты\ud800']}


def test_unavailable_external_lookup_uses_only_a_public_bridge_connection_ip(studio):
    aid, machine, link = setup(studio)
    link.hello['computer_info'] = True
    link.ip = '8.8.4.4'
    path = f'/api/code/agents/{aid}/info'
    assert studio.c.get(path).json()['external_ip'] == '8.8.4.4'
    link.ip = '10.77.1.1'
    assert studio.c.get(path).json()['external_ip'] is None


def test_software_keeps_the_old_node_reason_of_deepseek():
    """DSH стоит, но Node.js старше нужного: причина `node` доходит до карточки компьютера, чужие коды — нет."""
    import computer_info
    row = {'id': 'deepseek', 'installed': True, 'version': '0.2.2-rc.5', 'source': 'npm', 'reason': 'node'}
    data = computer_info.software({'items': [row, {**row, 'id': 'codex', 'reason': 'other'}]})
    assert [item['reason'] for item in data['items']] == ['node', None]


def test_software_routes_require_capability_pin_ownership_and_csrf(studio, caplog):
    aid, machine, link = setup(studio, pin='2468', opened=False)
    path = f'/api/code/agents/{aid}/software'
    assert studio.c.get(path).json()['detail']['code'] == 'unsupported'
    link.hello['software_versions'] = True
    assert studio.c.get('/api/agents').json()['items'][0]['software_versions'] is True
    assert studio.c.get(path).json()['detail']['code'] == 'closed'
    studio.c.post(f'/api/code/agents/{aid}/open', json={'pin': '2468'})
    machine.software_data = {'items': [{'id': 'codex', 'installed': True, 'version': '1.2.3',
        'latest': '1.2.10', 'cli_version': '1.2.3', 'source': 'npm', 'update_available': True,
        'can_update': True, 'reason': None}], 'update': {'tool': None, 'state': 'idle'}}
    with caplog.at_level(logging.INFO):
        response = studio.c.get(path + '?refresh=true')
        assert response.json() == machine.software_data
        assert response.headers['cache-control'] == 'no-store'
        assert machine.requests[-1]['refresh'] is True
        assert studio.c.post(path + '/codex/update').json()['update'] == {'tool': 'codex', 'state': 'waiting'}
    assert studio.c.post(path + '/cancel').json()['update']['state'] == 'cancelled'
    assert studio.c.post(path + '/arbitrary/update').status_code == 422
    before = len(machine.requests)
    studio.c.headers.pop(auth.CSRF_HEADER, None)
    assert studio.c.post(path + '/codex/update').status_code == 403
    assert len(machine.requests) == before
    make_user(studio, 'other-software')
    signin(studio, 'other-software')
    for method, suffix in [('GET', ''), ('POST', '/codex/update'), ('POST', '/cancel')]:
        assert studio.c.request(method, path + suffix).status_code == 404
    assert len(machine.requests) == before


def test_software_response_drops_paths_output_and_invalid_versions():
    from computer_info import software

    data = software({'items': [{'id': 'codex', 'installed': True, 'version': '1.2.3 & command',
        'latest': '1.2.4', 'source': 'arbitrary', 'path': 'private-machine-path', 'stderr': 'private-text'},
        {'id': 'codex'}, {'id': 'unknown'}], 'update': {'tool': 'unknown', 'state': 'private-output'}})
    assert len(data['items']) == 1
    assert data['items'][0]['version'] is None and data['items'][0]['source'] is None
    assert data['items'][0]['latest'] == '1.2.4'
    assert data['update'] == {'tool': None, 'state': 'error'}
    assert 'private' not in str(data)


def test_self_check_and_log_require_capability_pin_ownership_and_never_log(studio, caplog):
    """Мост 0.47.0: самопроверка и хвост журнала — владельцу после PIN; ретрансляция без журнала сервера."""
    aid, machine, link = setup(studio, pin='2468', opened=False)
    for suffix in ('/self-check', '/log'):
        assert studio.c.get(f'/api/code/agents/{aid}{suffix}').json()['detail']['code'] == 'unsupported'
    link.hello.update(self_check=True, agent_log=True)
    item = studio.c.get('/api/agents').json()['items'][0]
    assert item['self_check'] is True and item['agent_log'] is True
    assert studio.c.get(f'/api/code/agents/{aid}/log').json()['detail']['code'] == 'closed'
    studio.c.post(f'/api/code/agents/{aid}/open', json={'pin': '2468'})
    machine.self_check_data = {'items': [{'id': 'git', 'state': 'ok', 'version': '2.47.1'},
                                         {'id': 'disk', 'state': 'warn', 'code': 'low', 'free': 5}], 'checked_at': 9}
    machine.log_lines = ['2026-11-23 INFO канал: на связи', 'private-line\x1b']
    with caplog.at_level(logging.DEBUG):
        check = studio.c.get(f'/api/code/agents/{aid}/self-check')
        journal = studio.c.get(f'/api/code/agents/{aid}/log')
    assert check.headers['cache-control'] == journal.headers['cache-control'] == 'no-store'
    assert check.json()['items'][1] == {'id': 'disk', 'state': 'warn', 'code': 'low', 'version': None, 'login': None,
                                        'count': None, 'total': None, 'free': 5}
    assert journal.json() == {'lines': ['2026-11-23 INFO канал: на связи', 'private-line']}
    assert 'private-line' not in caplog.text
    make_user(studio, 'other-log')
    signin(studio, 'other-log')
    before = len(machine.requests)
    assert studio.c.get(f'/api/code/agents/{aid}/log').status_code == 404
    assert len(machine.requests) == before


def test_self_check_drops_unknown_items_paths_and_texts():
    from computer_info import log_lines, self_check

    data = self_check({'items': [{'id': 'git', 'state': 'odd', 'code': 'C:\\private', 'version': '2.4 private',
                                  'path': 'private', 'login': 'maybe', 'count': -1},
                                 {'id': 'git', 'state': 'ok'}, {'id': 'unknown', 'state': 'ok'}, None],
                       'checked_at': 'now'})
    assert data == {'items': [{'id': 'git', 'state': 'fail', 'code': None, 'version': None, 'login': None,
                               'count': None, 'total': None, 'free': None}], 'checked_at': None}
    assert log_lines({'lines': [*map(str, range(250)), 5]})['lines'][0] == '51'
    assert log_lines({'lines': 'text'}) == {'lines': []}
