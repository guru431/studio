"""Пакет, права владельца и PIN при удалённом обновлении агента."""
import hashlib
import logging
import zipfile

import pytest

import agent_release
import agents
import bridge
import state
from fake_bridge import FakeMachine, connect, enroll
from conftest import make_user, signin


@pytest.fixture(params=['', 'studio-agent/'])
def package(tmp_path, monkeypatch, request):
    path = tmp_path / 'studio-agent.zip'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr(request.param + 'studio_agent/__init__.py', 'VERSION = "0.10.1"\n')
        archive.writestr(request.param + 'studio_agent.pyw', '')
        archive.writestr(request.param + 'requirements.txt', '')
    monkeypatch.setattr(agents, 'AGENT_ZIP', path)
    return path


def test_package_requires_a_live_device_key_and_never_exposes_it(studio, package, caplog):
    caplog.set_level(logging.DEBUG)
    uid = make_user(studio, 'owner')
    aid, key = enroll(studio)
    url = '/api/agent/package'
    assert studio.c.get(url).status_code == 401
    signin(studio, 'owner')
    assert studio.c.get(url).status_code == 401  # сессия браузера не заменяет ключ устройства
    headers = {'Authorization': 'Bearer ' + key}
    response = studio.c.get(url, headers=headers)
    assert response.status_code == 200 and response.content == package.read_bytes()
    assert response.headers['cache-control'] == 'no-store'
    assert studio.c.get(url, headers={**headers, 'Origin': 'https://evil.example'}).status_code == 403
    state.DB.execute('UPDATE users SET disabled = 1 WHERE id = ?', (uid,))
    assert studio.c.get(url, headers=headers).status_code == 401
    state.DB.execute('UPDATE users SET disabled = 0 WHERE id = ?', (uid,))
    state.DB.execute('DELETE FROM agents WHERE id = ?', (aid,))
    assert studio.c.get(url, headers=headers).status_code == 401
    assert key not in caplog.text


def test_version_and_checksum_come_from_the_published_zip(package):
    result = agent_release.release(package)
    assert result == {'version': '0.10.1', 'size': package.stat().st_size,
                      'sha256': hashlib.sha256(package.read_bytes()).hexdigest()}
    with zipfile.ZipFile(package, 'w') as archive:
        archive.writestr('studio_agent/__init__.py', 'VERSION = "0.11.0"\n')
    assert agent_release.release(package)['version'] == '0.11.0'
    package.write_bytes(b'bad zip')
    assert agent_release.release(package) is None


def test_update_is_owned_csrf_protected_pin_protected_and_uses_server_release(studio, package):
    make_user(studio, 'owner')
    make_user(studio, 'friend')
    aid, _ = enroll(studio)
    machine = FakeMachine(pin='2468')
    machine.agent_version, machine.agent_update = '0.10.0', True
    connect(studio, aid, machine)
    url = f'/api/code/agents/{aid}/update'
    signin(studio, 'friend')
    assert studio.c.post(url).status_code == 404
    assert studio.c.get(url).status_code == 404
    assert not machine.requests
    signin(studio, 'owner')
    saved = studio.c.headers.pop('x-studio-csrf')
    assert studio.c.post(url).status_code == 403
    studio.c.headers['x-studio-csrf'] = saved
    assert studio.c.post(url).json()['detail']['code'] == 'closed'
    assert studio.c.post(f'/api/code/agents/{aid}/open', json={'pin': '2468'}).status_code == 204
    item = studio.c.get('/api/agents').json()['items'][0]
    assert (item['agent_version'], item['latest_version'], item['agent_update']) == ('0.10.0', '0.10.1', True)
    response = studio.c.post(url, json={'version': 'evil', 'url': 'https://evil.example'})
    assert response.json() == {'state': 'preparing', 'version': '0.10.1'}
    request = machine.requests[-1]
    assert request['version'] == '0.10.1' and 'url' not in request
    assert request['sha256'] == hashlib.sha256(package.read_bytes()).hexdigest()
    assert studio.c.get(url).json() == response.json()
    machine.update_state = 'waiting'
    assert studio.c.get(url).json()['state'] == 'waiting'


def test_old_offline_and_missing_package_have_explicit_errors(studio, package):
    make_user(studio, 'owner')
    aid, _ = enroll(studio)
    machine = FakeMachine()
    link = connect(studio, aid, machine)
    signin(studio, 'owner')
    url = f'/api/code/agents/{aid}/update'
    assert studio.c.post(url).json()['detail']['code'] == 'unsupported'
    assert not machine.requests
    link.hello['agent_update'] = True
    package.unlink()
    assert studio.c.post(url).json()['detail']['code'] == 'no_agent'
    bridge.LINKS.pop(aid)
    assert studio.c.get(url).json()['detail']['code'] == 'offline'


@pytest.mark.parametrize('value', ['../file', '1.2', '1.2.3\n', 123, None])
def test_invalid_running_version_is_rejected(value):
    with pytest.raises(bridge.ProtocolError):
        bridge.parse_hello({**FakeMachine().hello, 'agent_version': value})
