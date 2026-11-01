"""Владение, PIN, совместимость и фильтрация сведений компьютера."""
import logging

import bridge
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
