"""HTTP/мост: владение, PIN, CSRF, неизменяемое разрешение и ограниченные данные."""
import logging

import bridge
import state
from conftest import make_user, signin
from test_code import setup


def ready(studio):
    aid, machine, link = setup(studio)
    machine.desktop = machine.desktop_enabled = True
    link.hello['desktop'] = True
    return aid, machine, link, f'/api/code/agents/{aid}/desktop'


def test_capability_pin_and_ownership_are_checked_before_desktop_access(studio):
    aid, machine, link = setup(studio, pin='2468', opened=False)
    path = f'/api/code/agents/{aid}/desktop'
    assert studio.c.get(path).json()['detail']['code'] == 'unsupported'
    link.hello['desktop'] = machine.desktop = True
    assert studio.c.get('/api/agents').json()['items'][0]['desktop'] is True
    assert studio.c.get(path).json()['detail']['code'] == 'closed'
    assert studio.c.post(f'/api/code/agents/{aid}/open', json={'pin': '2468'}).status_code == 204
    response = studio.c.get(path)
    assert response.json() == {'enabled': False, 'active': False}
    assert response.headers['cache-control'] == 'no-store'
    assert studio.c.post(path + '/start', json={}).json()['detail']['code'] == 'desktop_disabled'
    make_user(studio, 'other')
    signin(studio, 'other')
    before = len(machine.requests)
    assert studio.c.get(path).status_code == 404
    assert len(machine.requests) == before


def test_permission_has_no_http_write_path_and_unknown_fields_never_reach_agent(studio):
    aid, machine, link, path = ready(studio)
    machine.desktop_enabled = False
    before = len(machine.requests)
    assert studio.c.post(path, json={'enabled': True}).status_code == 405
    assert studio.c.patch(path, json={'enabled': True}).status_code == 405
    assert studio.c.post(path + '/start', json={'enabled': True}).status_code == 422
    assert len(machine.requests) == before and not machine.desktop_enabled


def test_desktop_roundtrip_and_single_control_do_not_log_or_store_content(studio, caplog):
    aid, machine, link, path = ready(studio)
    with caplog.at_level(logging.INFO):
        started = studio.c.post(path + '/start', json={})
        assert started.status_code == 200, started.text
        control = started.json()['control']
        assert studio.c.post(path + '/start', json={}).json()['detail']['code'] == 'busy'
        body = {'control': control}
        frame = studio.c.post(path + '/frame', json={**body, 'monitor': 0})
        assert frame.status_code == 200 and frame.json()['width'] == 160
        assert frame.headers['cache-control'] == 'no-store'
        assert studio.c.post(path + '/input', json={**body, 'events': [
            {'type': 'text', 'text': 'private-keyboard-text'}]}).status_code == 204
        assert studio.c.post(path + '/clipboard', json={**body, 'text': 'private-clipboard-text'}).json() == {
            'text': 'private-clipboard-text'}
        assert studio.c.post(path + '/clipboard', json=body).json()['text'] == 'private-clipboard-text'
        assert studio.c.post(path + '/stop', json=body).status_code == 204
    assert all(value not in caplog.text for value in (control, 'private-keyboard-text', 'private-clipboard-text'))
    assert studio.c.post(path + '/frame', json={**body, 'monitor': 0}).json()['detail']['code'] == 'owned'
    # Схема Студии не получает таблиц/полей с экранами, буфером или токенами управления.
    assert not any('desktop' in row[0] for row in state.DB.execute("SELECT name FROM sqlite_master WHERE type='table'"))


def test_invalid_events_and_malformed_frames_are_bounded(studio):
    aid, machine, link, path = ready(studio)
    control = studio.c.post(path + '/start', json={}).json()['control']
    body = {'control': control}
    before = len(machine.requests)
    for event in ({'type': 'key', 'code': 'KeyA', 'down': 1},
                  {'type': 'move', 'monitor': 0, 'x': 2, 'y': 0},
                  {'type': 'text', 'text': 'x' * 4097},
                  {'type': 'enable', 'enabled': True}):
        assert studio.c.post(path + '/input', json={**body, 'events': [event]}).status_code == 422
    assert len(machine.requests) == before
    machine._op_desktop_frame = lambda req, events: {'image': '/9j/xxxxx', 'width': 1, 'height': 1}
    assert studio.c.post(path + '/frame', json={**body, 'monitor': 0}).status_code == 502


def test_locked_or_expired_session_discards_inflight_frame(studio):
    aid, machine, link, path = ready(studio)
    control = studio.c.post(path + '/start', json={}).json()['control']
    original = machine._op_desktop_frame

    def expire(req, events):
        result = original(req, events)
        state.DB.execute('DELETE FROM sessions')
        return result

    machine._op_desktop_frame = expire
    response = studio.c.post(path + '/frame', json={'control': control, 'monitor': 0})
    assert response.status_code == 409 and response.json()['detail']['code'] == 'closed'


def test_screen_lock_closes_desktop_without_resetting_computer_pin(studio):
    from fake_bridge import settle

    aid, machine, link, path = ready(studio)
    studio.c.post(path + '/start', json={})
    before = set(machine.open_sessions)
    digest = state.DB.execute('SELECT digest FROM sessions').fetchone()[0]

    async def lock():
        bridge.forget(link.user_id, digest, close_computers=False)

    studio.c.portal.call(lock)
    settle(studio)
    assert machine.desktop_active is None
    assert machine.open_sessions == before
