"""Ретрансляция уточнений и управления сохраняет авторизацию и единственный ход."""
import pytest

from test_code import A, ALPHA, chat_url, detail, setup


@pytest.mark.parametrize('engine', ['claude', 'codex', 'opencode'])
def test_followup_reuses_running_task_and_claim_moves_ownership(studio, engine):
    aid, machine, link = setup(studio)
    if engine != 'claude':
        link.hello[engine] = {'version': '1', 'models': [], 'efforts': []}
        machine.projects['alpha']['chats'][A]['engine'] = engine
    suffix = '' if engine == 'claude' else f'?engine={engine}'
    machine.running[A] = {'turn': 't1', 'project': ALPHA, 'text': '', 'steps': [], 'asks': {},
                           'owner': 'a' * 32, 'followups': []}
    endpoint = chat_url(aid, 'steer') + suffix
    r = studio.c.post(endpoint, json={'text': 'Уточнение'}, headers={'x-studio-window': 'b' * 32})
    assert r.status_code == 409 and detail(r)['code'] == 'owned'
    r = studio.c.post(chat_url(aid, 'claim') + suffix, json={}, headers={'x-studio-window': 'b' * 32})
    assert r.status_code == 200
    r = studio.c.post(endpoint, json={'text': 'Уточнение'}, headers={'x-studio-window': 'b' * 32})
    assert r.status_code == 200 and r.json()['mode'] == ('queue' if engine == 'opencode' else 'steer')
    assert machine.running[A]['turn'] == 't1'
    request = machine.requests[-1]
    assert request['op'] == 'steer' and request.get('engine', 'claude') == engine
    assert request['window'] == 'b' * 32
    assert not any(r['op'] == 'turn' for r in machine.requests)


def test_old_bridge_invalid_content_and_window_are_rejected_before_relay(studio):
    aid, machine, link = setup(studio)
    link.hello.pop('chat_control')
    assert studio.c.post(chat_url(aid, 'steer'), json={'text': 'Текст'}).status_code == 409
    assert studio.c.post(chat_url(aid, 'claim'), json={}).status_code == 409
    link.hello['chat_control'] = True
    for body in ({'text': ''}, {'text': 'x', 'images': [{'mime': 'image/png', 'data': '???'}]}):
        assert studio.c.post(chat_url(aid, 'steer'), json=body).status_code == 422
    assert studio.c.post(chat_url(aid, 'steer'), json={'text': 'Текст'},
                         headers={'x-studio-window': 'invalid'}).status_code == 422
    assert not any(r['op'] in ('steer', 'claim') for r in machine.requests)
