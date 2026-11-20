"""Чаты озвучки: продолжение, снимок голоса и повторная генерация выбранной записи."""
import db
import speeches
import state
from conftest import make_user, signin
from fakes import WAV_FAKE
from test_speeches import add_gateway, busy_speech, voice, wait_sp


def test_continue_chat_and_replace_a_record(studio, fake_audio):
    add_gateway(studio)
    vid = voice(studio, fake_audio)
    first = studio.c.post('/api/speeches', json={'text': 'Первая.', 'voice': vid}).json()
    first = wait_sp(studio, first['id'], {'done'})
    second = studio.c.post('/api/speeches', json={
        'text': 'Вторая.', 'voice': vid, 'chat_id': first['id']}).json()
    second = wait_sp(studio, second['id'], {'done'})
    chats = studio.c.get('/api/speeches/chats').json()['items']
    assert len(chats) == 1 and chats[0]['id'] == first['id']
    assert chats[0]['created'] == first['created']
    assert studio.c.get(f"/api/speeches/chats/{first['id']}").json()['items'] == [first, second]
    assert studio.c.patch(f'/api/voices/{vid}', json={'name': 'Новое имя'}).status_code == 200
    assert studio.c.get(f"/api/speeches/{first['id']}").json()['voice_name'] == 'Мой'
    old_media = first['media_id']
    replacement = studio.c.patch(f"/api/speeches/{first['id']}", json={
        'text': 'Исправленная.', 'voice': vid}).json()
    assert replacement['id'] == first['id'] and replacement['chat_id'] == first['id']
    edited = wait_sp(studio, first['id'], {'done'})
    assert edited['text'] == 'Исправленная.' and edited['voice_name'] == 'Новое имя'
    assert edited['media_id'] != old_media and edited['created'] == first['created']
    assert len(studio.c.get('/api/speeches').json()['items']) == 2
    assert studio.c.get(f'/api/media/{old_media}').status_code == 404
    found = state.DB.execute("SELECT body FROM search_docs WHERE section='speech' AND item=?",
                             (first['id'],)).fetchone()
    assert found[0] == 'Исправленная.'
    assert studio.c.delete(f"/api/speeches/{first['id']}").status_code == 204
    chat = studio.c.get(f"/api/speeches/chats/{first['id']}").json()
    assert chat['created'] == first['created'] and [i['id'] for i in chat['items']] == [second['id']]


def test_chat_ownership_and_running_edit_are_checked(studio, fake_audio, monkeypatch):
    add_gateway(studio)
    sid = busy_speech(studio, fake_audio, monkeypatch)
    current = wait_sp(studio, sid, {'working'})
    body = {'text': 'Правка.', 'voice': current['voice']}
    assert studio.c.patch(f'/api/speeches/{sid}', json=body).status_code == 409
    make_user(studio, 'other')
    signin(studio, 'other')
    add_gateway(studio)
    body['voice'] = voice(studio, fake_audio)
    assert studio.c.get(f'/api/speeches/chats/{sid}').status_code == 404
    assert studio.c.patch(f'/api/speeches/{sid}', json=body).status_code == 404
    assert studio.c.post('/api/speeches', json=body | {'chat_id': sid}).status_code == 404
    assert studio.c.get('/api/speeches/chats').json()['items'] == []


def test_chat_pages_keep_equal_start_times_and_the_first_record_order(studio):
    uid = make_user(studio, 'owner')
    signin(studio, 'owner')
    for sid, cid, text in [('a', 'a', 'Первый'), ('b', 'b', 'Другой'), ('c', 'c', 'Третий'), ('0', 'a', 'Продолжение')]:
        state.DB.execute("INSERT INTO speeches(id,user_id,model,voice,text,state,detail,created,chat_id,chat_created)"
                         " VALUES(?,?,'qwen3-tts','voice',?,'done','готово',100,?,100)", (sid, uid, text, cid))
    first = studio.c.get('/api/speeches/chats?limit=2').json()['items']
    assert [i['id'] for i in first] == ['c', 'b']
    rest = studio.c.get('/api/speeches/chats?limit=2&before=100&before_id=b').json()['items']
    assert [i['id'] for i in rest] == ['a']
    chat = studio.c.get('/api/speeches/chats/a').json()
    assert [i['text'] for i in chat['items']] == ['Первый', 'Продолжение']


def test_failed_edit_preserves_audio_and_shared_audio_survives_replacement(studio, fake_audio):
    add_gateway(studio)
    vid = voice(studio, fake_audio)
    created = studio.c.post('/api/speeches', json={'text': 'Оригинал.', 'voice': vid}).json()
    old = wait_sp(studio, created['id'], {'done'})
    assert studio.c.patch(f"/api/speeches/{old['id']}", json={'text': ' ', 'voice': vid}).status_code == 422
    assert studio.c.get(f"/api/speeches/{old['id']}").json() == old
    state.DB.execute("INSERT INTO speeches(id,user_id,model,voice,text,media_id,state,detail,created)"
                     " SELECT 'shared-audio',user_id,model,voice,'Копия',media_id,state,detail,created"
                     " FROM speeches WHERE id=?", (old['id'],))
    assert studio.c.patch(f"/api/speeches/{old['id']}", json={'text': 'Правка.', 'voice': vid}).status_code == 202
    assert wait_sp(studio, old['id'], {'done'})['media_id'] != old['media_id']
    assert studio.c.get(f"/api/media/{old['media_id']}").status_code == 200


def search_doc(sid):
    return state.DB.execute("SELECT body, model FROM search_docs WHERE section='speech' AND item=?", (sid,)).fetchone()[:]


def test_failed_revoice_keeps_the_previous_text_and_audio(studio, fake_audio):
    """Шлюз отказал новой озвучке: прежние текст и mp3 на месте, ошибка — словами (находка 04:F4)."""
    add_gateway(studio)
    vid = voice(studio, fake_audio)
    old = wait_sp(studio, studio.c.post('/api/speeches', json={'text': 'Оригинал.', 'voice': vid}).json()['id'], {'done'})
    studio.gw.tts_replies = [(500, {'detail': 'сбой'})]
    assert studio.c.patch(f"/api/speeches/{old['id']}", json={'text': 'Правка.', 'voice': vid}).status_code == 202
    failed = wait_sp(studio, old['id'], {'done', 'error'})
    assert failed['state'] == 'error' and failed['detail'].endswith(speeches.KEPT_NOTE)
    assert (failed['text'], failed['media_id'], failed['duration']) == (old['text'], old['media_id'], old['duration'])
    assert studio.c.get(f"/api/media/{old['media_id']}").status_code == 200
    assert search_doc(old['id']) == ('Оригинал.', 'qwen3-tts')
    assert state.DB.execute("SELECT previous, previous_media_id FROM speeches").fetchone()[:] == (None, None)
    studio.gw.tts_replies = [(200, WAV_FAKE)]
    assert studio.c.patch(f"/api/speeches/{old['id']}", json={'text': 'Правка.', 'voice': vid}).status_code == 202
    done = wait_sp(studio, old['id'], {'done', 'error'})
    assert done['state'] == 'done' and done['media_id'] != old['media_id']
    assert studio.c.get(f"/api/media/{old['media_id']}").status_code == 404


def test_cancelled_revoice_restores_the_record_and_waits_its_turn(studio, fake_audio, monkeypatch):
    """«Отменить» повторной озвучки — запись как была; ждёт она после поставленных раньше, «В работе» считает
    время с постановки, поиск видит новую модель; перезапуск — прежний звук с ошибкой (04:F4, 04:F10)."""
    add_gateway(studio)
    vid = voice(studio, fake_audio)
    old = wait_sp(studio, studio.c.post('/api/speeches', json={'text': 'Оригинал.', 'voice': vid}).json()['id'], {'done'})
    wait_sp(studio, busy_speech(studio, fake_audio, monkeypatch), {'working'})
    studio.clock[0] += 100
    waiting = studio.c.post('/api/speeches', json={'text': 'Ждёт.', 'voice': vid}).json()
    studio.clock[0] += 100
    body = {'text': 'Правка.', 'voice': vid, 'model': 'f5-tts'}
    edited = studio.c.patch(f"/api/speeches/{old['id']}", json=body).json()
    assert (waiting['detail'], edited['detail']) == ('в очереди · 1', 'в очереди · 2')
    started = {i['id']: i['started'] for i in studio.c.get('/api/jobs').json()['running']}
    assert started[old['id']] - started[waiting['id']] >= 100          # часы двигает и ждущая карту озвучка
    assert search_doc(old['id']) == ('Правка.', 'f5-tts')
    assert studio.c.post(f"/api/speeches/{old['id']}/cancel").json() == {'state': 'done'}
    assert studio.c.get(f"/api/speeches/{old['id']}").json() == old
    assert search_doc(old['id']) == ('Оригинал.', 'qwen3-tts')
    assert studio.c.get(f"/api/media/{old['media_id']}").status_code == 200
    studio.c.patch(f"/api/speeches/{old['id']}", json=body)
    state.DB.execute("UPDATE speeches SET state = 'working' WHERE id = ?", (old['id'],))
    speeches.recover()
    interrupted = studio.c.get(f"/api/speeches/{old['id']}").json()
    assert interrupted['state'] == 'error' and interrupted['detail'] == f'{speeches.INTERRUPTED} — {speeches.KEPT_NOTE}'
    assert (interrupted['text'], interrupted['media_id']) == (old['text'], old['media_id'])


def test_whole_chat_is_deleted_at_once_but_not_while_a_record_runs(studio, fake_audio, monkeypatch):
    """«Удалить чат» — все записи и их mp3 одним запросом; идущая запись — 409, чужой чат — 404 (04:F11)."""
    add_gateway(studio)
    vid = voice(studio, fake_audio)
    first = wait_sp(studio, studio.c.post('/api/speeches', json={'text': 'Первая.', 'voice': vid}).json()['id'], {'done'})
    second = studio.c.post('/api/speeches', json={'text': 'Вторая.', 'voice': vid, 'chat_id': first['id']}).json()
    second = wait_sp(studio, second['id'], {'done'})
    other = wait_sp(studio, studio.c.post('/api/speeches', json={'text': 'Другой чат.', 'voice': vid}).json()['id'], {'done'})
    monkeypatch.setattr(speeches, 'WAIT_MAX_SEC', 10 ** 9)
    studio.gw.tts_replies = [(503, {'detail': 'busy'})]
    running = studio.c.post('/api/speeches', json={'text': 'Идёт.', 'voice': vid, 'chat_id': first['id']}).json()
    wait_sp(studio, running['id'], {'working'})
    r = studio.c.delete(f"/api/speeches/chats/{first['id']}")
    assert r.status_code == 409 and r.json()['detail']['code'] == 'running'
    assert studio.c.post(f"/api/speeches/{running['id']}/cancel").json() == {'state': 'cancelled'}
    make_user(studio, 'other')
    signin(studio, 'other')
    assert studio.c.delete(f"/api/speeches/chats/{first['id']}").status_code == 404
    signin(studio, 'owner')
    assert studio.c.delete(f"/api/speeches/chats/{first['id']}").status_code == 204
    assert [c['id'] for c in studio.c.get('/api/speeches/chats').json()['items']] == [other['id']]
    assert [studio.c.get(f"/api/media/{i['media_id']}").status_code for i in (first, second, other)] == [404, 404, 200]


def test_schema_52_refreshes_the_search_model_and_keeps_old_rows_queueable(env):
    from contextlib import closing
    with closing(db.connect(env.db_path)) as conn:
        conn.executescript(db.SCHEMA.split(db.SPEECH_52, 1)[0] + '\nPRAGMA user_version=51;')
        conn.execute("INSERT INTO users(id,login,name,pw_hash,created) VALUES(1,'u','U','h',0)")
        conn.execute("INSERT INTO speeches(id,user_id,model,voice,text,state,detail,created)"
                     " VALUES('s',1,'qwen3-tts','v','Привет','done','готово',123)")
        conn.execute("UPDATE speeches SET model = 'f5-tts'")              # прежний триггер смену модели не видел
        db.migrate(conn)
        assert conn.execute("SELECT model FROM search_docs WHERE item='s'").fetchone()[0] == 'f5-tts'
        assert conn.execute("SELECT queued, previous, previous_media_id FROM speeches").fetchone()[:] == (None, None, None)
        conn.execute("UPDATE speeches SET model = 'qwen3-tts'")           # прежний код после отката — тоже
        assert conn.execute("SELECT model FROM search_docs WHERE item='s'").fetchone()[0] == 'qwen3-tts'


def test_migration_preserves_old_speeches_and_previous_writer(env):
    from contextlib import closing
    with closing(db.connect(env.db_path)) as conn:
        conn.executescript(db.SCHEMA.split(db.SPEECH_45, 1)[0] + '\nPRAGMA user_version=44;')
        conn.execute("INSERT INTO users(id,login,name,pw_hash,created) VALUES(1,'u','U','h',0)")
        conn.execute("INSERT INTO voices VALUES('v',1,'Старый голос','образец','ref',8,0)")
        insert = ("INSERT INTO speeches(id,user_id,model,voice,text,state,detail,created)"
                  " VALUES(?,1,'qwen3-tts','v','Привет','done','готово',123)")
        conn.execute(insert, ('old',))
        db.migrate(conn)
        db.migrate(conn)
        assert conn.execute('SELECT chat_id,chat_created,voice_name FROM speeches').fetchone()[:] == (
            'old', 123, 'Старый голос')
        conn.execute(insert, ('previous-writer',))
        assert conn.execute("SELECT chat_id,chat_created FROM speeches WHERE id='previous-writer'").fetchone()[:] == (
            'previous-writer', 123)
