import pytest

from studio_agent import host, linux_terminal as terminal
from test_agent import make, opened, req


async def test_terminal_settings_are_local_and_session_scoped(home, tmp_path, monkeypatch):
    monkeypatch.setattr(host, 'platform_id', lambda: 'linux')
    agent = make(home, tmp_path)
    assert agent.hello()['terminal'] and 'ssh_uri' not in agent.hello()
    assert (await agent.handle(req('terminal_info')))[0]['code'] == 'closed'
    await opened(agent)
    assert (await agent.handle(req('terminal_info')))[0]['data'] == {'ssh_uri': None, 'web_url': None}
    terminal.save(home, dict(host='2001:db8::1', port=2200, user='operator', web_url=''))
    assert (await agent.handle(req('terminal_info')))[0]['data']['ssh_uri'] == 'ssh://operator@[2001:db8::1]:2200'
    assert (await agent.handle(req('terminal_info', shared_projects=['a' * 16])))[0]['code'] == 'closed'
    with pytest.raises(ValueError):
        terminal.save(home, dict(host='-oProxyCommand', port=22, user='', web_url=''))
    assert terminal.load(home)['port'] == 2200
    (home / 'terminal.json').write_text('{"bad": true}', encoding='utf-8')
    assert (await agent.handle(req('terminal_info')))[0]['code'] == 'invalid'
