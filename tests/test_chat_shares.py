"""Chat links grant read access to one snapshot after a fresh, rate-limited PIN check."""
import asyncio
from urllib.parse import urlsplit

import httpx
import pytest

import auth
import chat_shares
import db
import main
import media
import operations
import security
import state
from conftest import make_user, signin
from test_code import A, ALPHA, setup


def regular(s, chat_id="chat", *, user=True):
    uid = make_user(s, "owner") if user else state.DB.execute("SELECT id FROM users WHERE login='owner'").fetchone()[0]
    if user:
        signin(s, "owner")
    state.DB.execute("INSERT INTO chats(id,user_id,title,created,updated) VALUES(?,?,?,0,0)",
                     (chat_id, uid, "Private title"))
    state.DB.execute("INSERT INTO messages(id,chat_id,role,content,status,via,created)"
                     " VALUES(?,?,'user','Private question','done','server',0)", (chat_id + '-u', chat_id))
    state.DB.execute("INSERT INTO messages(id,chat_id,role,content,status,via,created)"
                     " VALUES(?,?,'assistant','**Private answer**','done','server',1)", (chat_id + '-a', chat_id))
    return f"/api/chats/{chat_id}/share", uid


def create(s, endpoint, pin="0123"):
    response = s.c.post(endpoint, json={"pin": pin})
    assert response.status_code == 201, response.text
    return response.json(), urlsplit(response.json()["url"]).fragment


def unlock(s, token, pin="0123", **headers):
    return s.c.post('/share/unlock', json={"token": token, "pin": pin}, headers=headers)


def test_new_link_is_unique_per_creation_and_chat_and_pin_is_required_every_open(studio):
    endpoint, _ = regular(studio)
    out, token = create(studio, endpoint)
    other, _ = regular(studio, "other", user=False)
    _, second = create(studio, other, "9876")
    assert token != second
    row = state.DB.execute("SELECT * FROM chat_shares WHERE chat_id='chat'").fetchone()
    assert token not in row['token_enc'].decode() and row['token_hash'] == security.digest(token)
    assert row['pin_hash'] != '0123' and security.verify_password('0123', row['pin_hash'])
    assert studio.c.get(endpoint).json() == out
    studio.c.cookies.clear()
    page = studio.c.get('/share')
    assert page.status_code == 200 and 'Private' not in page.text and 'share-pin' in page.text
    assert page.headers['cache-control'] == 'no-store' and page.headers['referrer-policy'] == 'no-referrer'
    assert studio.c.head('/share').status_code == 200
    response = unlock(studio, token)
    assert response.status_code == 200 and not response.cookies
    assert response.json()['messages'][1]['text'] == '**Private answer**'
    assert unlock(studio, token, '9876').status_code == 403
    assert unlock(studio, second, '0123').status_code == 403
    assert studio.c.get('/share/check').status_code == 405
    access = response.json()['access']
    assert studio.c.post('/share/check', json={'access': access}).status_code == 204
    assert studio.c.get('/api/chats/chat', headers={'Authorization': 'Bearer ' + access}).status_code == 401
    assert studio.c.post('/api/chats/chat/turns', json={'text': 'write'},
                         headers={'Authorization': 'Bearer ' + access}).status_code == 401


@pytest.mark.parametrize('pin', [None, '', '123', '12345', 1234, 'abcd', '１２３４', '1234\n'])
def test_pin_must_be_exactly_four_ascii_digits(studio, pin):
    endpoint, _ = regular(studio)
    assert studio.c.post(endpoint, json={'pin': pin}).status_code == 422
    assert state.DB.execute('SELECT count(*) FROM chat_shares').fetchone()[0] == 0


def test_rotation_and_revocation_invalidate_previous_readers_and_snapshot_does_not_grow(studio):
    endpoint, _ = regular(studio)
    _, token = create(studio, endpoint)
    visitor = unlock(studio, token).json()['access']
    state.DB.execute("UPDATE messages SET content='Changed' WHERE id='chat-a'")
    assert unlock(studio, token).json()['messages'][1]['text'] == '**Private answer**'
    _, replacement = create(studio, endpoint, '5678')
    assert replacement != token and unlock(studio, token).status_code == 404
    assert studio.c.post('/share/check', json={'access': visitor}).status_code == 404
    assert unlock(studio, replacement).status_code == 403
    assert unlock(studio, replacement, '5678').json()['messages'][1]['text'] == 'Changed'
    assert studio.c.delete(endpoint).status_code == 204
    assert studio.c.get(endpoint).json() == {'enabled': False}
    assert unlock(studio, replacement, '5678').status_code == 404


def test_ownership_csrf_screen_lock_and_history_cleanup(studio):
    endpoint, uid = regular(studio)
    _, token = create(studio, endpoint)
    assert studio.c.post(endpoint, json={'pin': '0123'}, headers={auth.CSRF_HEADER: 'wrong'}).status_code == 403
    state.DB.execute("UPDATE users SET screen_lock=1, screen_lock_sections='[\"chat\"]' WHERE id=?", (uid,))
    state.DB.execute('UPDATE sessions SET seen=0')
    assert studio.c.post(endpoint, json={'pin': '0123'}).status_code == 423
    state.DB.execute('UPDATE users SET screen_lock=0 WHERE id=?', (uid,))
    make_user(studio, 'friend')
    signin(studio, 'friend')
    for method in ('GET', 'POST', 'DELETE'):
        assert studio.c.request(method, endpoint, json={'pin': '0123'}).status_code == 404
    signin(studio, 'owner')
    assert studio.c.delete('/api/history').status_code == 200
    assert unlock(studio, token).status_code == 404
    assert state.DB.execute('PRAGMA foreign_key_check').fetchall() == []


def test_pin_limit_is_shared_across_addresses_persistent_and_expires(studio):
    endpoint, _ = regular(studio)
    _, token = create(studio, endpoint)
    for n in range(chat_shares.PIN_FAILS):
        assert unlock(studio, token, '9999', **{'X-Forwarded-For': f'198.51.100.{n}'}).status_code == 403
    auth.FAILS.clear()
    assert unlock(studio, token).status_code == 429
    studio.clock[0] += chat_shares.LOCK_SEC + 1
    assert unlock(studio, token).status_code == 200


def test_concurrent_attempts_are_reserved_before_hashing(studio, monkeypatch):
    endpoint, _ = regular(studio)
    _, token = create(studio, endpoint)
    calls = []

    def fail(pin, stored):
        calls.append(pin)
        return False

    monkeypatch.setattr(security, 'verify_password', fail)

    async def batch():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='https://testserver') as client:
            return await asyncio.gather(*[client.post('/share/unlock', json={'token': token, 'pin': '9999'})
                                          for _ in range(12)])

    responses = studio.c.portal.call(batch)
    assert sum(r.status_code == 403 for r in responses) == chat_shares.PIN_FAILS
    assert len(calls) == chat_shares.PIN_FAILS
    assert all(r.status_code in (403, 429) for r in responses)


def test_wrong_link_ip_limit_and_cross_origin_requests_do_not_hash(studio, monkeypatch):
    endpoint, _ = regular(studio)
    _, token = create(studio, endpoint)
    assert unlock(studio, token, Origin='https://foreign.invalid').status_code == 403
    monkeypatch.setattr(chat_shares, 'IP_ATTEMPTS', 2)
    assert unlock(studio, 'x' * 43).status_code == 404
    assert unlock(studio, 'y' * 43).status_code == 404
    monkeypatch.setattr(security, 'verify_password', lambda *args: pytest.fail('rate-limited request hashed'))
    assert unlock(studio, token).status_code == 429


def test_anonymous_requests_are_bounded_even_without_content_length(studio):
    response = studio.c.post('/share/unlock', content=iter([b'{"token":"', b'x' * 5000, b'"}']),
                              headers={'content-type': 'application/json'})
    assert response.status_code == 413
    assert response.headers['cache-control'] == 'no-store'
    assert studio.c.post('/share/unlock', json={}).status_code == 422
    assert state.DB.execute('SELECT count(*) FROM chat_share_attempts').fetchone()[0] == 0


@pytest.mark.parametrize('action', ['revoke', 'disable', 'delete'])
def test_recheck_after_pin_hash_refuses_revoked_or_disabled_source(studio, monkeypatch, action):
    endpoint, uid = regular(studio)
    _, token = create(studio, endpoint)

    async def invalidate():
        if action == 'disable':
            state.DB.execute('UPDATE users SET disabled=1 WHERE id=?', (uid,))
        elif action == 'delete':
            state.DB.execute('DELETE FROM users WHERE id=?', (uid,))
        else:
            state.DB.execute('DELETE FROM chat_shares')

    def verify(*args):
        studio.c.portal.call(invalidate)
        return True

    monkeypatch.setattr(security, 'verify_password', verify)
    assert unlock(studio, token).status_code == 404
    assert state.DB.execute('SELECT count(*) FROM chat_share_visitors').fetchone()[0] == 0


def test_file_download_is_scoped_to_snapshot_and_revoked_with_it(studio):
    endpoint, uid = regular(studio)
    mid = media.save(uid, b'private document', 'text/plain', 'document.txt')
    hidden = media.save(uid, b'unshared document', 'text/plain', 'other.txt')
    state.DB.execute("INSERT INTO attachments(id,user_id,message_id,kind,name,media_id,created)"
                     " VALUES('attachment',?,'chat-u','document','document.txt',?,0)", (uid, mid))
    _, token = create(studio, endpoint)
    opened = unlock(studio, token).json()
    assert opened['messages'][0]['attachments'][0]['file'] == mid
    studio.c.cookies.clear()
    body = {'access': opened['access'], 'file': mid}
    response = studio.c.post('/share/file', json=body)
    assert response.status_code == 200 and response.content == b'private document'
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['content-disposition'].startswith('attachment;')
    assert studio.c.post('/share/file', json=body | {'file': hidden}).status_code == 404
    state.DB.execute('DELETE FROM chats')
    assert studio.c.post('/share/file', json=body).status_code == 404


def test_visitor_expires_and_disabling_account_blocks_existing_viewer(studio):
    endpoint, uid = regular(studio)
    _, token = create(studio, endpoint)
    access = unlock(studio, token).json()['access']
    state.DB.execute('UPDATE users SET disabled=1 WHERE id=?', (uid,))
    assert studio.c.post('/share/check', json={'access': access}).status_code == 404
    state.DB.execute('UPDATE users SET disabled=0 WHERE id=?', (uid,))
    studio.clock[0] += chat_shares.VIEW_SEC + 1
    assert studio.c.post('/share/check', json={'access': access}).status_code == 404


@pytest.mark.parametrize('action', ['history', 'logout', 'lock'])
def test_creation_rechecks_source_and_session_after_hashing(studio, monkeypatch, action):
    endpoint, uid = regular(studio)
    original = security.hash_password

    async def invalidate():
        if action == 'history':
            with operations.barrier(uid):
                state.DB.execute('DELETE FROM chats')
        elif action == 'logout':
            state.DB.execute('DELETE FROM sessions')
        else:
            state.DB.execute("UPDATE users SET screen_lock=1, screen_lock_sections='[\"chat\"]'")
            state.DB.execute('UPDATE sessions SET seen=0')

    def hash_and_invalidate(pin):
        hashed = original(pin)
        studio.c.portal.call(invalidate)
        return hashed

    monkeypatch.setattr(security, 'hash_password', hash_and_invalidate)
    assert studio.c.post(endpoint, json={'pin': '0123'}).status_code == 409
    assert state.DB.execute('SELECT count(*) FROM chat_shares').fetchone()[0] == 0


def test_oversized_snapshot_does_not_replace_existing_link(studio, monkeypatch):
    endpoint, _ = regular(studio)
    _, token = create(studio, endpoint)
    monkeypatch.setattr(chat_shares, 'MAX_MESSAGES', 1)
    assert studio.c.post(endpoint, json={'pin': '4567'}).status_code == 413
    assert unlock(studio, token).status_code == 200


def test_agent_snapshot_fetches_all_pages_and_is_readable_without_bridge(studio):
    aid, machine, _ = setup(studio, pin='1357')
    endpoint = f'/api/code/agents/{aid}/projects/{ALPHA}/chats/{A}/share'
    _, token = create(studio, endpoint)
    data = unlock(studio, token).json()
    assert len(data['messages']) == 250
    assert [data['messages'][n]['text'] for n in (0, 99, 249)] == ['0', '99', '249']
    assert not {'agent_id', 'project', 'session_id'} & data.keys()
    machine.open_sessions.clear()
    assert unlock(studio, token).status_code == 200
    assert studio.c.post(endpoint, json={'pin': '1234'}).status_code == 409
    assert studio.c.delete(endpoint).status_code == 204
    assert unlock(studio, token).status_code == 404


def test_share_schema_migration_keeps_original_chats_and_is_idempotent(env):
    conn = db.connect(env.db_path)
    conn.executescript(db.SCHEMA.removesuffix(db.SECURITY_34).removesuffix(db.SHARES_33) + '\nPRAGMA user_version=32;')
    conn.execute("INSERT INTO users(id,login,name,pw_hash,created) VALUES(1,'u','U','h',0)")
    conn.execute("INSERT INTO chats(id,user_id,title,created,updated) VALUES('c',1,'Keep',0,0)")
    db.migrate(conn)
    db.migrate(conn)
    assert conn.execute('SELECT title FROM chats').fetchone()[0] == 'Keep'
    assert conn.execute('SELECT count(*) FROM chat_shares').fetchone()[0] == 0
    assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    conn.close()
