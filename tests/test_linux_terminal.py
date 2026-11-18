import logging

import pytest
import state
import terminal_links
from conftest import make_user, signin
from test_code import setup


def test_terminal_is_owner_only_and_requires_an_open_linux_session(studio, caplog):
    aid, machine, link = setup(studio, pin='2468', opened=False)
    machine._op_terminal_info = lambda *_: {'ssh_uri': 'ssh://operator@[2001:db8::1]:2200',
                                            'web_url': 'https://console.example.net:9090/system/terminal'}
    route = f'/api/code/agents/{aid}/terminal'
    assert studio.c.get(route).json()['detail']['code'] == 'unsupported'
    link.hello.update(terminal=True, platform='linux', desktop=False)
    assert studio.c.get('/api/agents').json()['items'][0]['terminal'] is True
    assert studio.c.get(route).json()['detail']['code'] == 'closed'
    assert studio.c.post(f'/api/code/agents/{aid}/open', json={'pin': '2468'}).status_code == 204
    with caplog.at_level(logging.INFO):
        answer = studio.c.get(route)
    assert answer.json() == {**machine._op_terminal_info(), 'ssh_command': 'ssh -p 2200 operator@2001:db8::1'}
    assert answer.headers['cache-control'] == 'no-store'
    assert 'operator@' not in caplog.text and 'console.example.net' not in caplog.text
    member = make_user(studio, 'member')
    state.DB.execute('INSERT INTO project_members(agent_id,project,user_id,created) VALUES(?,?,?,0)',
                     (aid, 'a' * 16, member))
    signin(studio, 'member')
    count = len(machine.requests)
    assert not studio.c.get('/api/agents').json()['items'][0]['terminal']
    assert studio.c.get(route).status_code == 404
    assert len(machine.requests) == count


@pytest.mark.parametrize('key,bad', [
    ('ssh_uri', 'ssh://-oProxyCommand=x:22'), ('ssh_uri', 'ssh://alice:x@server:22'),
    ('ssh_uri', 'ssh://server:22/x'), ('ssh_uri', 'ssh://0x7f000001:22'),
    ('web_url', 'https://evil.example\\@good.example'), ('web_url', 'http://console.example.net'),
    ('web_url', 'https://u:p@host'), ('web_url', 'https://host/?access_token=x'),
    ('web_url', 'https://host/#token'), ('web_url', 'https://host:65536/'),
])
def test_untrusted_bridge_cannot_supply_shell_syntax_or_credentials(key, bad):
    with pytest.raises(ValueError):
        terminal_links.clean({key: bad})


def test_web_only_endpoint_never_invents_an_ssh_login_or_returns_extra_fields():
    assert terminal_links.clean({'web_url': 'https://Console.Example.net', 'password': 'ignored'}) == {
        'ssh_command': None, 'ssh_uri': None, 'web_url': 'https://console.example.net/'}
