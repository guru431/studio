"""MCP: изоляция учёток, SSRF, шифрование и настоящий цикл инструментов чата через HTTP transport."""
import json
import subprocess
import sys

import httpx
import pytest

import db
import mcp_client
import security
import state
from conftest import APP, add_gateway, make_user, signin
from test_chat import messages, new_chat, sse, turn
from test_chat_web import call, names

URL = "http://10.77.1.15:4000/mcp"
TOKEN = "private-mcp-token"
LOOKUP = mcp_client.tool_name("Tools", "lookup")      # имя функции `lookup` сервера «Tools» для модели


@pytest.mark.integration
def test_mcp_module_can_be_loaded_by_the_service_console_before_main():
    result = subprocess.run([sys.executable, '-c', 'import mcp_client'], cwd=APP,
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode(errors='replace')


def add(studio):
    add_gateway(studio)
    response = studio.c.post('/api/mcp', json={'name': 'Tools', 'url': URL, 'token': TOKEN})
    assert response.status_code == 201, response.text
    return response.json()['id']


def mock(studio, *, streaming=False, fail=False):
    calls = []

    async def handler(request):
        assert request.headers['authorization'] == f'Bearer {TOKEN}'
        if request.method == 'DELETE':
            calls.append(('close', {}))
            return httpx.Response(204)
        payload = json.loads(request.content)
        method = payload['method']
        calls.append((method, payload.get('params', {})))
        if method == 'notifications/initialized':
            assert request.headers['mcp-session-id'] == 'private-session'
            assert request.headers['mcp-protocol-version'] == '2025-06-18'
            return httpx.Response(202)
        result = {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'fake', 'version': '1'}}
        if method == 'tools/list':
            result = {'tools': [{'name': 'lookup', 'description': 'Read data', 'inputSchema': {'type': 'object'}}]}
        if method == 'tools/call':
            if fail:
                raise httpx.ReadError('private service error')
            result = {'content': [{'type': 'text', 'text': 'result-private'}]}
        response = {'jsonrpc': '2.0', 'id': payload['id'], 'result': result}
        if streaming:
            return httpx.Response(200, content=('event: message\r\ndata: ' + json.dumps(response) + '\r\n\r\n').encode(),
                                  headers={'content-type': 'text/event-stream', 'mcp-session-id': 'private-session'})
        return httpx.Response(200, json=response, headers={'mcp-session-id': 'private-session'})

    studio.gw.on['/mcp'] = handler
    return calls


@pytest.mark.parametrize('streaming', [False, True])
def test_mcp_discovery_tool_round_and_secret_storage(studio, caplog, streaming):
    sid = add(studio)
    calls = mock(studio, streaming=streaming)
    cid = new_chat(studio)
    assert studio.c.patch(f'/api/chats/{cid}', json={'mcp_ids': [sid]}).status_code == 200
    studio.gw.chat_script = [{'tool_calls': [call(LOOKUP, query='value')]}, ['Готово']]
    events = sse(turn(studio, cid, 'Use tool'))
    assert events[-1] == ('done', {'status': 'done'})
    # каталог — проверкой окна хода (`chat._turn_specs`, холодный кэш), вызов — ходом из кэша
    assert [method for method, _ in calls] == ['initialize', 'notifications/initialized', 'tools/list', 'close',
                                              'initialize', 'notifications/initialized', 'tools/call', 'close']
    assert calls[6][1] == {'name': 'lookup', 'arguments': {'query': 'value'}}
    assert studio.gw.chat_requests[1]['messages'][-1]['content'] == 'result-private'
    assert messages(studio, cid)[1]['tools'] == [{'kind': 'mcp', 'server': 'Tools', 'tool': 'lookup'}]
    stored = state.DB.execute('SELECT secret_enc FROM mcp_servers WHERE id=?', (sid,)).fetchone()[0]
    assert TOKEN.encode() not in stored
    assert TOKEN not in caplog.text + studio.c.get('/api/mcp').text
    assert 'result-private' not in caplog.text


def test_mcp_unselected_is_not_contacted_and_calls_are_not_retried(studio):
    sid = add(studio); calls = mock(studio, fail=True)
    cid = new_chat(studio)
    sse(turn(studio, cid, 'Hello'))
    assert calls == [] and names(studio.gw.chat_requests[-1]) == []
    studio.c.patch(f'/api/chats/{cid}', json={'mcp_ids': [sid]})
    studio.gw.chat_script = [{'tool_calls': [call(LOOKUP)]}, ['Failed']]
    sse(turn(studio, cid, 'Use tool'))
    assert sum(method == 'tools/call' for method, _ in calls) == 1
    assert messages(studio, cid)[-1]['tools'][0]['error']


def test_unreachable_mcp_server_is_skipped_and_named_not_the_whole_turn(studio):
    """Отмеченный, но лежащий сервер — шаг-ошибка с его именем; модель зовётся с инструментами остальных."""
    sid = add(studio); mock(studio)
    down = studio.c.post('/api/mcp', json={'name': 'Down', 'url': 'http://10.77.1.15:4000/down', 'token': TOKEN}).json()['id']

    async def unreachable(request):
        raise httpx.ConnectError('private failure')

    studio.gw.on['/down'] = unreachable
    cid = new_chat(studio)
    assert studio.c.patch(f'/api/chats/{cid}', json={'mcp_ids': [down, sid]}).status_code == 200
    studio.gw.chat_script = [['Готово']]
    events = sse(turn(studio, cid, 'Use tool'))
    assert events[-1] == ('done', {'status': 'done'})
    assert ('tool', {'kind': 'mcp', 'label': 'Down', 'skipped': True}) in events
    assert names(studio.gw.chat_requests[-1]) == [LOOKUP]
    assert messages(studio, cid)[1]['tools'] == [{'kind': 'mcp', 'server': 'Down',
                                                 'error': 'MCP недоступен или вернул неверный ответ'}]


def test_mcp_account_isolation_allowlist_and_token_preservation(studio):
    sid = add(studio); cid = new_chat(studio)
    assert studio.c.put(f'/api/mcp/{sid}', json={'name': 'Edited', 'url': URL}).status_code == 200
    calls = mock(studio)
    assert studio.c.post(f'/api/mcp/{sid}/check').status_code == 200
    assert calls
    assert studio.c.post('/api/mcp', json={'name': 'Forbidden', 'url': 'http://169.254.169.254/mcp'}).status_code == 422
    make_user(studio, 'other'); signin(studio, 'other')
    assert studio.c.get('/api/mcp').json() == {'items': []}
    for method, path, body in [('GET', f'/api/mcp/{sid}', None), ('POST', f'/api/mcp/{sid}/check', None),
                               ('PUT', f'/api/mcp/{sid}', {'name': 'x', 'url': URL}), ('DELETE', f'/api/mcp/{sid}', None)]:
        assert studio.c.request(method, path, json=body).status_code == 404
    assert studio.c.patch(f'/api/chats/{cid}', json={'mcp_ids': [sid]}).status_code == 404


def test_mcp_header_values_are_write_only_and_kept_by_null(studio):
    """Как и токен, значения заголовков только записываются: `detail` отдаёт имена с null, null в PUT — прежнее значение."""
    add_gateway(studio)
    sid = studio.c.post('/api/mcp', json={'name': 'Tools', 'url': URL,
                                          'headers': {'X-Api-Key': 'private-key', 'X-Team': 'red'}}).json()['id']
    shown = studio.c.get(f'/api/mcp/{sid}')
    assert shown.json()['headers'] == {'X-Api-Key': None, 'X-Team': None} and 'private-key' not in shown.text
    body = {'name': 'Tools', 'url': URL, 'headers': {'X-Api-Key': None, 'X-Team': 'blue', 'X-New': 'n'}}
    assert studio.c.put(f'/api/mcp/{sid}', json=body).status_code == 200
    stored = json.loads(security.decrypt(state.DB.execute('SELECT secret_enc FROM mcp_servers WHERE id=?', (sid,)).fetchone()[0]))
    assert stored['headers'] == {'X-Api-Key': 'private-key', 'X-Team': 'blue', 'X-New': 'n'}
    # Прежнего значения нет (новое имя, создание) — сохранять нечего: 422, а не пустой заголовок.
    for method, path in (('PUT', f'/api/mcp/{sid}'), ('POST', '/api/mcp')):
        response = studio.c.request(method, path, json={'name': 'Tools', 'url': URL, 'headers': {'X-Other': None}})
        assert response.status_code == 422, (method, response.text)


def test_mcp_only_chat_names_a_made_up_tool_and_calls_a_tool_without_arguments(studio):
    """Только MCP, без «Интернета»: чужое имя — «инструмента нет», а не «Интернет выключен»; пустая строка
    аргументов — вызов с {}."""
    sid = add(studio); calls = mock(studio)
    cid = new_chat(studio)
    assert studio.c.patch(f'/api/chats/{cid}', json={'mcp_ids': [sid]}).status_code == 200
    studio.gw.chat_script = [{'tool_calls': [('made_up_tool', '{}'), (LOOKUP, '')]}, ['Готово']]
    assert sse(turn(studio, cid, 'Use tool'))[-1] == ('done', {'status': 'done'})
    assert ('tools/call', {'name': 'lookup', 'arguments': {}}) in calls
    assert messages(studio, cid)[1]['tools'] == [{'kind': 'unknown', 'error': 'модель вызвала инструмент, которого у неё нет'},
                                                {'kind': 'mcp', 'server': 'Tools', 'tool': 'lookup'}]


def test_disabled_mcp_server_can_be_checked_but_not_used(studio):
    sid = add(studio); calls = mock(studio)
    assert studio.c.put(f'/api/mcp/{sid}', json={'name': 'Tools', 'url': URL, 'enabled': False}).status_code == 200
    response = studio.c.post(f'/api/mcp/{sid}/check')
    assert response.status_code == 200 and response.json() == {'tools': ['lookup']}
    calls.clear()
    cid = new_chat(studio)
    state.DB.execute('UPDATE chats SET mcp_ids=? WHERE id=?', (json.dumps([sid]), cid))
    sse(turn(studio, cid, 'Hello'))
    assert calls == [] and names(studio.gw.chat_requests[-1]) == []


def test_new_mcp_address_requires_secrets_again(studio):
    """Сохранённые токен и значения заголовков — только прежнему адресу: на новый их надо ввести заново."""
    add_gateway(studio)
    sid = studio.c.post('/api/mcp', json={'name': 'Tools', 'url': URL, 'token': TOKEN,
                                          'headers': {'X-Api-Key': 'private-key'}}).json()['id']
    other = 'http://10.77.1.15:4000/other'
    for body in ({'name': 'Tools', 'url': other},
                 {'name': 'Tools', 'url': other, 'token': 'new-token'},
                 {'name': 'Tools', 'url': other, 'token': 'new-token', 'headers': {'X-Api-Key': None}}):
        response = studio.c.put(f'/api/mcp/{sid}', json=body)
        assert response.status_code == 422 and 'заново' in response.json()['detail']['message'], body
    body = {'name': 'Tools', 'url': other, 'token': 'new-token', 'headers': {'X-Api-Key': 'new-key'}}
    assert studio.c.put(f'/api/mcp/{sid}', json=body).status_code == 200
    stored = json.loads(security.decrypt(state.DB.execute('SELECT secret_enc FROM mcp_servers WHERE id=?', (sid,)).fetchone()[0]))
    assert stored == {'token': 'new-token', 'headers': {'X-Api-Key': 'new-key'}}
    # Без сохранённых секретов переносить нечего — новый адрес принимается без токена.
    plain = studio.c.post('/api/mcp', json={'name': 'Plain', 'url': URL}).json()['id']
    assert studio.c.put(f'/api/mcp/{plain}', json={'name': 'Plain', 'url': other}).status_code == 200


def test_deleted_mcp_server_is_no_longer_selected_in_chats(studio):
    """Удалённый в настройках сервер не числится в диалоге и не мешает перейти на прямое подключение."""
    from conftest import add_connection
    sid = add(studio)
    cid = new_chat(studio)
    assert studio.c.patch(f'/api/chats/{cid}', json={'mcp_ids': [sid]}).json()['mcp_ids'] == [sid]
    assert studio.c.delete(f'/api/mcp/{sid}').status_code == 204
    assert studio.c.get(f'/api/chats/{cid}').json()['chat']['mcp_ids'] == []
    cloud = add_connection(studio, name='cloud')
    moved = studio.c.patch(f'/api/chats/{cid}', json={'connection_id': cloud, 'model': 'local-gemma-abl'})
    assert moved.status_code == 200, moved.text


def test_model_without_tools_explains_how_to_disable_mcp(studio):
    sid = add(studio); mock(studio)
    cid = new_chat(studio)
    assert studio.c.patch(f'/api/chats/{cid}', json={'mcp_ids':[sid]}).status_code == 200

    async def refused(request):
        return httpx.Response(400, json={'error':{'message':'tools not supported'}})

    studio.gw.on['/v1/chat/completions'] = refused
    sse(turn(studio, cid, 'Use tool'))
    assert 'MCP' in messages(studio, cid)[1]['error']


def test_mcp_step_43_keeps_previous_chat_writer_compatible(env):
    from contextlib import closing
    with closing(db.connect(env.db_path)) as conn:
        conn.executescript(db.SCHEMA.split(db.SPEECH_45, 1)[0].removesuffix(db.MEDIA_44).removesuffix(db.MCP_43) + '\nPRAGMA user_version=42;')
        conn.execute("INSERT INTO users(id,login,name,pw_hash,created) VALUES(1,'owner','Owner','hash',0)")
        conn.execute("INSERT INTO chats(id,user_id,created,updated) VALUES('before',1,0,0)")
        db.migrate(conn)
        conn.execute("INSERT INTO chats(id,user_id,created,updated) VALUES('after',1,0,0)")
        assert [r[0] for r in conn.execute('SELECT mcp_ids FROM chats')] == ['[]', '[]']
