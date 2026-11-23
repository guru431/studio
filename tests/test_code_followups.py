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


def test_answer_from_another_window_takes_over_only_when_the_owner_window_is_not_watched(studio):
    """08:I4: ответ на карточку с другого устройства — без «Продолжить здесь», если окно-владельца хода никто не
    смотрит: студия передаёт мосту 0.47.0 окна наблюдателей (`watched`), он принимает ответ и передаёт управление.
    Окно-владелец смотрит — прежний `owned`; прежнему мосту окна не уходят."""
    import bridge
    aid, machine, link = setup(studio)
    machine.answer_takeover = link.hello['answer_takeover'] = True
    owner, other = 'a' * 32, 'b' * 32
    machine.running[A] = {'turn': 't1', 'project': ALPHA, 'text': '', 'steps': [], 'owner': owner, 'followups': [],
                          'asks': {f'r{i}': {'request_id': f'r{i}', 'kind': 'permission'} for i in (1, 2)}}
    q = studio.c.portal.call(bridge.watch, aid, A, None, link.user_id, owner)

    def answer(rid):
        return studio.c.post(chat_url(aid, 'answer'), json={'request_id': rid, 'decision': 'allow'},
                             headers={'x-studio-window': other})

    r = answer('r1')
    assert r.status_code == 409 and detail(r)['code'] == 'owned'
    assert machine.requests[-1]['watched'] == [owner]
    studio.c.portal.call(bridge.unwatch, aid, A, q)
    assert answer('r1').status_code == 204
    assert machine.requests[-1]['watched'] == [] and machine.running[A]['owner'] == other
    link.hello.pop('answer_takeover')
    assert answer('r2').status_code == 204 and 'watched' not in machine.requests[-1]


def test_window_reading_its_turn_stream_counts_as_watching(studio):
    """Окно, начавшее ход, читает поток ответа `POST …/turn` (на `/live` не переходит): оно смотрит чат, и ответ
    на карточку из другого окна ход не перехватывает — `owned`, мосту уходит `watched` с окном-владельцем."""
    from test_code import ASK
    aid, machine, link = setup(studio)
    machine.answer_takeover = link.hello['answer_takeover'] = True
    owner, other = 'a' * 32, 'b' * 32
    machine.script = [ASK]
    answered = None
    with studio.c.stream('POST', chat_url(aid, 'turn'), json={'text': 'x'},
                         headers={'x-studio-window': owner}) as response:
        assert response.status_code == 200
        for line in response.iter_lines():
            if line.startswith('event: ask'):            # поток ещё читается: выход из цикла его закроет
                answered = studio.c.post(chat_url(aid, 'answer'), json={'request_id': 'r1', 'decision': 'allow'},
                                         headers={'x-studio-window': other})
                break
    assert answered is not None and answered.status_code == 409 and detail(answered)['code'] == 'owned', answered
    assert machine.requests[-1]['watched'] == [owner]
    assert machine.running[A]['owner'] == owner
