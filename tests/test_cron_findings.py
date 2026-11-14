"""Отказ провайдера и удаление учётки в ходе потоковой выгрузки."""
import io
import json
import zipfile

import pytest

import state
import takeout
import web
from conftest import make_user, signin
from test_web import KEY, run
from test_checks import CLOUD_ROLE, add_connection, gateway_as_openai, put


def test_server_probe_does_not_retry_an_unrelated_token_validation_error(studio):
    cid = gateway_as_openai(studio)
    studio.gw.chat_error = (400, {'error': {'message': 'max_tokens must be at least 1', 'code': 'invalid_parameter'}})
    response = studio.c.post(f'/api/connections/{cid}/probe', json={'model': CLOUD_ROLE})
    assert response.status_code == 200 and not response.json()['probe']['ok']
    assert len(studio.gw.chat_requests) == 1


def test_models_only_browser_check_is_not_logged_as_a_probe(studio, caplog):
    cid = add_connection(studio)
    assert put(studio, cid, {'models': [{'id': 'local-gemma-abl'}]}).status_code == 200
    assert 'проверка браузером модели' in caplog.text and 'пробный-ответ' not in caplog.text


@pytest.mark.parametrize('statuses', [[False], 'bad', {'status': 'error'}])
def test_page_rejects_malformed_status_metadata_with_words(studio, statuses):
    studio.gw.exa_replies['/contents'] = (200, {'statuses': statuses, 'results': [{'title': 'T', 'text': 'text'}]})
    with pytest.raises(web.ToolError):
        run(studio, web.page, KEY, 'https://example.test/')


def test_export_keeps_initial_identity_when_account_is_removed_mid_stream(studio):
    uid = make_user(studio, 'export-owner')
    async def export():
        mark = object()
        takeout.RUNNING[uid] = mark
        stream = takeout._stream(uid, ('photo',), [], mark)
        chunks = [await anext(stream) for _ in range(4)]  # README и settings уже отправлены.
        state.DB.execute('DELETE FROM users WHERE id = ?', (uid,))
        chunks += [part async for part in stream]
        return b''.join(chunks)
    archive = studio.c.portal.call(export)
    with zipfile.ZipFile(io.BytesIO(archive)) as result:
        assert json.loads(result.read('manifest.json'))['user'] == {'login': 'export-owner', 'name': 'export-owner'}
    assert uid not in takeout.RUNNING


def test_revoked_export_session_cannot_continue_with_a_reused_user_id(studio):
    uid = make_user(studio, 'export-revoked')
    signin(studio, 'export-revoked')
    async def export():
        digest = state.DB.execute('SELECT digest FROM sessions WHERE user_id = ?', (uid,)).fetchone()[0]
        mark = object()
        takeout.RUNNING[uid] = mark
        stream = takeout._stream(uid, ('photo',), [], mark, session_digest=digest)
        await anext(stream)
        state.DB.execute('DELETE FROM users WHERE id = ?', (uid,))
        return [part async for part in stream]
    assert studio.c.portal.call(export) == []
    assert uid not in takeout.RUNNING
