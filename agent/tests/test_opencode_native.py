"""Изолированный OpenCode без генерации: настоящий API, история, дети и ветвление."""

import http.client

import pytest

from studio_agent import opencode, opencode_native, opencode_rpc, projects
from studio_agent.errors import Refused

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]


async def test_real_native_fork_children_and_acp_history_preserve_source(home, monkeypatch):
    path = opencode_rpc.find_cli()
    if path is None:
        pytest.skip('OpenCode не установлен')
    for key in ('XDG_DATA_HOME', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'XDG_CACHE_HOME', 'TEMP', 'TMP'):
        directory = home / key.lower()
        directory.mkdir()
        monkeypatch.setenv(key, str(directory))
    for key in ('OPENCODE_CONFIG', 'OPENCODE_CONFIG_DIR', 'OPENCODE_CONFIG_CONTENT'):
        monkeypatch.delenv(key, raising=False)
    project = home / 'native-project'
    project.mkdir()
    targets = []
    runner = opencode.OpenCodeRunner(lambda _: None, home=home, find_cli=lambda: path)
    chat = '11111111-2222-4333-8444-555555555555'
    try:
        async with opencode_native.Client(path, project) as client:
            original = (await client.request('POST', '/session', {'location': {'directory': str(project)}}))['data']
            targets.append(original['id'])
            sid = 'ses_studio_native_fixture'
            info = {**original, 'id': sid}
            messages = [{'id': 'msg_first', 'type': 'user', 'text': 'Первый вопрос', 'time': {'created': 1000}},
                        {'id': 'msg_second', 'type': 'user', 'text': 'Второй вопрос', 'time': {'created': 2000}}]
            await client.request('POST', '/experimental/session/import', {'info': info, 'messages': messages})
            targets.append(sid)
            child = (await client.request('POST', '/session', {'parentID': sid}))['data']
            assert child['parentID'] == sid
            runner.aliases.put(project, chat, sid)
            rpc = opencode_rpc.Client(path, project)
            await rpc.connect()
            try:
                await rpc.request('session/load', {'sessionId': sid, 'cwd': str(project), 'mcpServers': [],
                    'additionalDirectories': [str(home)]})
                await rpc.request('session/set_config_option', {'sessionId': sid, 'configId': 'mode', 'value': 'plan'})
                await runner.state(project, chat)
                listed = await rpc.request('session/list', {'cwd': str(project)})
                row = next(row for row in listed['sessions'] if row['sessionId'] == sid)
                assert row['additionalDirectories'] == [str(home)]
                assert (await runner.state(project, chat))['settings']['agent_mode'] == 'plan'
            finally:
                await rpc.disconnect()
            history = await runner.history(project, chat, None, 10)
            assert [m['id'] for m in history['items']] == ['msg_first', 'msg_second']
            branch = await runner.fork_chat(project, chat, 'msg_second')
            branch_target = runner.aliases.target(project, branch['chat'])
            targets.append(branch_target)
            assert branch['draft'] == 'Второй вопрос'
            assert [m['text'] for m in (await runner.history(project, branch['chat'], None, 10))['items']] == ['Первый вопрос']
            assert (await runner.history(project, chat, None, 10)) == history
            full = await runner.fork_chat(project, chat)
            targets.append(runner.aliases.target(project, full['chat']))
            assert [m['text'] for m in (await runner.history(project, full['chat'], None, 10))['items']] == ['Первый вопрос', 'Второй вопрос']
            full_state = await runner.state(project, full['chat'])
            assert full_state['settings']['agent_mode'] == 'plan'
            assert full_state['settings']['additional_directories'] == [str(home)]
            listing = await runner.subagents(project, chat)
            assert [row['id'] for row in listing['items']] == [child['id']]
            assert listing['complete']
            assert (await runner.subagent_history(project, chat, child['id']))['items'] == []
            with pytest.raises(Refused, match='not_found'):
                await runner.subagent_history(project, chat, branch_target)
            # Порт слушает только loopback; без временного пароля доступ закрыт.
            connection = http.client.HTTPConnection('127.0.0.1', client.port, timeout=5)
            try:
                connection.request('GET', '/api/session')
                assert connection.getresponse().status == 401
            finally:
                connection.close()
    finally:
        await runner.close(final=True)
        async with opencode_native.Client(path, project) as cleanup:
            for target in reversed(targets):
                await cleanup.request('DELETE', '/session/' + target)
