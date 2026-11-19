"""MCP: изоляция учёток, SSRF, шифрование и настоящий цикл инструментов чата через HTTP transport."""
import json
import subprocess
import sys

import httpx
import pytest

import db
import security
import state
from conftest import APP, add_gateway, make_user, signin
from test_chat import messages, new_chat, sse, turn
from test_chat_web import call

URL = "http://10.77.1.15:4000/mcp"
TOKEN = "private-mcp-token"


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
    studio.gw.chat_script = [{'tool_calls': [call(f'mcp_{sid}_0', query='value')]}, ['Готово']]
    events = sse(turn(studio, cid, 'Use tool'))
    assert events[-1] == ('done', {'status': 'done'})
    assert [method for method, _ in calls] == ['initialize', 'notifications/initialized', 'tools/list', 'tools/call', 'close']
    assert calls[3][1] == {'name': 'lookup', 'arguments': {'query': 'value'}}
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
    assert calls == [] and 'tools' not in studio.gw.chat_requests[-1]
    studio.c.patch(f'/api/chats/{cid}', json={'mcp_ids': [sid]})
    studio.gw.chat_script = [{'tool_calls': [call(f'mcp_{sid}_0')]}, ['Failed']]
    sse(turn(studio, cid, 'Use tool'))
    assert sum(method == 'tools/call' for method, _ in calls) == 1
    assert messages(studio, cid)[-1]['tools'][0]['error']


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
