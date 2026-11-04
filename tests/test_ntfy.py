"""Прямая доставка ntfy: личные настройки, секреты, граница адресов и общие события push."""
import json
import logging
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

import auth
import db
import ntfy
import push
import security
import state
import upstream
from conftest import make_user, signin
from fake_bridge import FakeMachine, connect, emit, enroll
from test_push import A, ALPHA, Device, settle

SERVER = "https://psh.example.net"
OTHER = "https://other-ntfy.example.com"
SETTINGS = {"server": SERVER, "topic": "private-topic", "enabled": True}


def test_ntfy_import_does_not_start_the_connections_cycle():
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'app')}
    result = subprocess.run([sys.executable, '-c', "import ntfy,sys; assert 'upstream' not in sys.modules"],
                            env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_settings_and_test_delivery_require_session_csrf_and_unlocked_phone(studio):
    assert studio.c.get('/api/me/ntfy').status_code == 401
    uid = make_user(studio, 'owner')
    csrf = signin(studio, 'owner')
    studio.c.headers.pop(auth.CSRF_HEADER)
    assert studio.c.put('/api/me/ntfy', json=SETTINGS).status_code == 403
    assert studio.c.post('/api/me/ntfy/test').status_code == 403
    studio.c.headers[auth.CSRF_HEADER] = csrf
    state.DB.execute('UPDATE users SET screen_lock = 1 WHERE id = ?', (uid,))
    state.DB.execute('UPDATE sessions SET mobile = 1, seen = 0 WHERE user_id = ?', (uid,))
    assert studio.c.get('/api/me/ntfy').status_code == 423
    assert studio.c.post('/api/me/ntfy/test').status_code == 423


@pytest.fixture
def ntfy_ready(studio, monkeypatch):
    state.DB.execute("UPDATE settings SET value = ? WHERE key = 'allowlist'", (json.dumps(["psh.example.net:443", "other-ntfy.example.com:443"]),))
    async def resolve(host, port):
        return ["93.184.216.34"]
    monkeypatch.setattr(upstream, "RESOLVE", resolve)
    return studio


def test_personal_settings_encrypt_tokens_keep_server_binding_and_cascade(ntfy_ready, caplog):
    s = ntfy_ready
    uid = make_user(s, "owner")
    make_user(s, "other")
    signin(s, "owner")
    caplog.set_level(logging.INFO, logger="studio")
    assert s.c.get('/api/me/ntfy').json() == {"server": SERVER, "topic": "", "enabled": False, "has_token": False}
    r = s.c.put('/api/me/ntfy', json={**SETTINGS, "token": "tk-secret-token"})
    assert r.status_code == 200 and r.json()["has_token"]
    stored = state.DB.execute('SELECT token_enc FROM ntfy_settings WHERE user_id = ?', (uid,)).fetchone()[0]
    assert b'tk-secret-token' not in stored and security.decrypt(stored) == 'tk-secret-token'
    assert 'tk-secret-token' not in r.text + s.c.get('/api/me/ntfy').text + caplog.text
    assert s.c.put('/api/me/ntfy', json={**SETTINGS, "topic": "changed"}).json()["has_token"]
    assert s.c.put('/api/me/ntfy', json={**SETTINGS, "server": OTHER}).json()["has_token"] == 0
    s.c.cookies.clear()
    signin(s, "other")
    assert s.c.get('/api/me/ntfy').json()["topic"] == ''
    assert s.c.post('/api/me/ntfy/test').status_code == 409
    assert s.c.delete('/api/me/ntfy').status_code == 204
    assert ntfy.settings(uid)["topic"] == SETTINGS["topic"]
    state.DB.execute('DELETE FROM users WHERE id = ?', (uid,))
    assert state.DB.execute('SELECT * FROM ntfy_settings').fetchall() == []


def test_delivery_is_json_with_deep_link_and_errors_do_not_expose_secrets(ntfy_ready, caplog):
    s = ntfy_ready
    uid = make_user(s, "owner")
    signin(s, "owner")
    assert s.c.put('/api/me/ntfy', json={**SETTINGS, "token": "tk-private"}).status_code == 200
    assert s.c.post('/api/me/ntfy/test').status_code == 204
    [request] = s.gw.ntfy_requests
    assert str(request.url) == 'https://93.184.216.34/'
    assert request.headers['host'] == 'psh.example.net' and request.headers['authorization'] == 'Bearer tk-private'
    assert request.extensions['sni_hostname'] == 'psh.example.net'
    assert json.loads(request.content) == {"topic": "private-topic", "title": "Студия", "message": "Проверка уведомлений Studio: ntfy подключён",
                                          "click": "https://testserver/#/settings/other", "priority": 4}
    for code in [302, 401, 403, 500]:
        s.gw.ntfy_status = code
        r = s.c.post('/api/me/ntfy/test')
        assert r.status_code == 502 and 'private-server-response' not in r.text
    assert len(s.gw.ntfy_requests) == 5  # 302 не порождает второй запрос
    s.gw.ntfy_raise = httpx.ConnectError
    r = s.c.post('/api/me/ntfy/test')
    assert r.status_code == 502 and 'private-server-error' not in r.text
    assert all(secret not in caplog.text for secret in ['tk-private', 'private-topic', 'private-server'])
    state.DB.execute('UPDATE users SET disabled = 1 WHERE id = ?', (uid,))
    before = len(s.gw.ntfy_requests)
    async def notify():
        await ntfy.notify(uid, 'event', 'code', title='Код')
    s.c.portal.call(notify)
    assert len(s.gw.ntfy_requests) == before


def test_only_allowlisted_https_roots_and_public_dns_receive_requests(ntfy_ready, monkeypatch):
    s = ntfy_ready
    make_user(s, 'owner')
    signin(s, 'owner')
    for server in ['http://psh.example.net', 'https://psh.example.net.evil.test', 'https://user@psh.example.net',
                   SERVER + '/path', SERVER + '?x=1', SERVER + '#fragment', 'https://127.0.0.1', SERVER + ':444']:
        assert s.c.put('/api/me/ntfy', json={**SETTINGS, 'server': server}).status_code == 422
    assert s.gw.ntfy_requests == []
    assert s.c.put('/api/me/ntfy', json=SETTINGS).status_code == 200
    async def private(host, port):
        return ['93.184.216.34', '127.0.0.1']
    monkeypatch.setattr(upstream, 'RESOLVE', private)
    assert s.c.post('/api/me/ntfy/test').status_code == 422
    assert s.gw.ntfy_requests == []
    for invalid in [{'topic': '../private'}, {'topic': ''}, {'topic': 'a' * 65}, {'token': 'a\nb'}, {'enabled': 'yes'}]:
        assert s.c.put('/api/me/ntfy', json={**SETTINGS, **invalid}).status_code == 422


def test_ntfy_receives_agent_and_job_events_without_webpush_and_channels_are_independent(ntfy_ready, caplog):
    s = ntfy_ready
    uid = make_user(s, 'owner')
    aid, _ = enroll(s)
    signin(s, 'owner')
    assert s.c.put('/api/me/ntfy', json=SETTINGS).status_code == 200
    assert s.c.get('/api/jobs').json()['push']
    link = connect(s, aid, FakeMachine({'alpha': {}}))
    emit(s, link, ALPHA, A, {'k': 'ask', 'kind': 'question', 'questions': ['private-question']},
         {'k': 'done', 'status': 'done', 'message': 'private-answer'})
    settle(s)
    payloads = [json.loads(r.content) for r in s.gw.ntfy_requests]
    assert [p['message'] for p in payloads] == ['Рабочий ПК: вопрос', 'Рабочий ПК: ход окончен']
    assert all(p['click'] == f'https://testserver/#/code/{aid}/{ALPHA}/{A}' for p in payloads)
    assert 'private-question' not in json.dumps(payloads) and 'private-answer' not in json.dumps(payloads)
    state.DB.execute("INSERT INTO images(id,user_id,kind,model,prompt,size,seed,state,detail,created,finished)"
                     " VALUES ('job', ?, 'generation','m','private-prompt','1x1',1,'done','private-detail',1,2)", (uid,))
    s.c.portal.call(push.job_finished, 'images', 'job')
    settle(s)
    assert json.loads(s.gw.ntfy_requests[-1].content)['click'] == 'https://testserver/#/photo?id=job'
    device = Device()
    assert s.c.post('/api/me/push', json=device.body()).status_code == 204
    s.gw.ntfy_status = 503
    s.c.portal.call(push.notify_later, uid, 'event', 'code', 't')
    settle(s)
    assert device.decrypt(s.gw.push_requests[-1].content)['body'] == 'event'
    s.gw.ntfy_status, s.gw.push_raise = 200, httpx.ConnectError
    s.c.portal.call(push.notify_later, uid, 'event2', 'code', 't')
    settle(s)
    assert json.loads(s.gw.ntfy_requests[-1].content)['message'] == 'event2'
    assert s.c.put('/api/me/ntfy', json={**SETTINGS, 'enabled': False}).status_code == 200
    count = len(s.gw.ntfy_requests)
    s.c.portal.call(push.notify_later, uid, 'muted', 'code', 't')
    settle(s)
    assert len(s.gw.ntfy_requests) == count
    assert s.c.post('/api/me/ntfy/test').status_code == 204  # явная проверка сохранённого выключенного канала
    assert s.c.delete('/api/me/ntfy').status_code == 204
    assert not s.c.get('/api/me/ntfy').json()['has_token']


def test_schema_29_adds_settings_without_enabling_or_changing_existing_accounts(env):
    conn = db.connect(env.db_path)
    conn.executescript(db.SCHEMA.removesuffix(db.NTFY_29) + '\nPRAGMA user_version = 28;')
    conn.execute("INSERT INTO users(id,login,name,pw_hash,created) VALUES (1,'u','U','h',0)")
    db.migrate(conn)
    db.migrate(conn)
    assert conn.execute('SELECT * FROM ntfy_settings').fetchall() == []
    assert conn.execute('SELECT login FROM users').fetchone()[0] == 'u'
    assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    conn.close()
