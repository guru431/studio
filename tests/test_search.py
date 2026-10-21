"""Поиск по истории (app/search.py, шаг схемы 13): индекс держат триггеры таблиц истории, ответ чата —
после «готово», расшифровки — по тексту (`transcripts.text`), запрос — телом POST, только своё."""
import pathlib

import chat
import state
import transcripts
from conftest import add_gateway, make_user
from test_chat import events_of, new_chat, wait
from test_transcripts import result_file, upload, wait_tr

IMAGE = ("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, state, detail, created)"
         " VALUES (?, ?, 'generation', ?, ?, '1024x1024', 1, 'done', 'готово', ?)")
VIDEO = ("INSERT INTO videos(id, user_id, connection_id, source_media_id, prompt, seconds, quality, smooth, state, detail,"
         " created) VALUES (?, ?, 1, 'm', ?, 1, 'fast', 0, 'completed', 'готово', ?)")
TRANSCRIPT = ("INSERT INTO transcripts(id, user_id, model, name, source_media_id, state, detail, created, text)"
              " VALUES (?, ?, 'whisperx', ?, 'm', 'done', 'готово', ?, ?)")
SPEECH = ("INSERT INTO speeches(id, user_id, model, voice, text, state, detail, created)"
          " VALUES (?, ?, 'qwen3-tts', 'v', ?, 'done', 'готово', ?)")
CHAT = "INSERT INTO chats(id, user_id, title, model, created, updated) VALUES (?, ?, ?, ?, ?, ?)"
MESSAGE = ("INSERT INTO messages(id, chat_id, role, content, status, via, model, created)"
           " VALUES (?, ?, ?, ?, 'done', 'server', ?, ?)")


def found(word: str) -> list:
    """Документы индекса по началу слова — прямо из базы (как их держат триггеры)."""
    return sorted(tuple(r) for r in state.DB.execute(
        "SELECT d.section, d.item, d.part FROM search JOIN search_docs d ON d.id = search.rowid WHERE search MATCH ?",
        (f'"{word}"*',)))


def test_deleting_a_chat_or_a_user_takes_their_rows_out_of_the_index(studio):
    """Каскады FK дёргают триггеры дочерних таблиц: удаление диалога уносит его название, сообщения и
    документы; удаление учётки — всё её."""
    uid = make_user(studio, "owner")
    state.DB.execute(CHAT, ("c1", uid, "Отпуск", "m", 1, 1))
    state.DB.execute(MESSAGE, ("m1", "c1", "user", "Отпуск у моря", "m", 2))
    state.DB.execute("INSERT INTO attachments(id, user_id, message_id, kind, name, media_id, text, created)"
                     " VALUES ('a1', ?, NULL, 'document', 'отпуск.txt', 'x', 'Отпускные', 3)", (uid,))
    assert found("отпуск") == [("chat", "c1", "m1"), ("chat", "c1", "title")]   # неотправленный документ — нет
    state.DB.execute("UPDATE attachments SET message_id = 'm1' WHERE id = 'a1'")
    assert ("chat", "c1", "a1") in found("отпуск")
    state.DB.execute("DELETE FROM chats WHERE id = 'c1'")
    assert found("отпуск") == [] and state.DB.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0] == 0

    state.DB.execute(IMAGE, ("i1", uid, "flux", "Кошка", 1))
    state.DB.execute(VIDEO, ("v1", uid, "Кошка бежит", 2))
    state.DB.execute(TRANSCRIPT, ("t1", uid, "Кошка.m4a", 3, None))
    state.DB.execute(SPEECH, ("s1", uid, "Кошка спит", 4))
    state.DB.execute(CHAT, ("c2", uid, "Кошки", "m", 5, 5))
    state.DB.execute(MESSAGE, ("m2", "c2", "user", "кошачий корм", "m", 6))
    assert len(found("кош")) == 6
    state.DB.execute("DELETE FROM users WHERE id = ?", (uid,))
    assert found("кош") == [] and state.DB.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0] == 0


def test_index_follows_changed_prompts_titles_and_texts(studio):
    """Ролик: промпт придумал шлюз — индекс по новому; название диалога и текст расшифровки — так же."""
    uid = make_user(studio, "owner")
    state.DB.execute(VIDEO, ("v1", uid, "", 1))
    state.DB.execute("UPDATE videos SET prompt = 'Медленный рассвет', prompt_source = 'gateway' WHERE id = 'v1'")
    assert found("рассвет") == [("video", "v1", "")]
    state.DB.execute(CHAT, ("c1", uid, None, "m", 1, 1))
    state.DB.execute("UPDATE chats SET title = 'Планы', updated = 2 WHERE id = 'c1'")
    state.DB.execute("UPDATE chats SET title = 'Итоги', updated = 3 WHERE id = 'c1'")
    assert found("план") == [] and found("итог") == [("chat", "c1", "title")]
    state.DB.execute(TRANSCRIPT, ("t1", uid, "Звонок.m4a", 1, None))
    state.DB.execute("UPDATE transcripts SET text = 'Встреча в среду' WHERE id = 't1'")
    assert found("сред") == found("звонок") == [("transcribe", "t1", "")]


def test_answer_is_indexed_once_it_is_done_not_while_it_streams(studio):
    """Ход пишет текст ответа каждые SAVE_SEC со статусом streaming — индекс только по готовому."""
    add_gateway(studio); cid = new_chat(studio)
    studio.gw.chat_hold = True                       # шлюз отдал «Здрав» и ждёт
    try:
        with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": "Привет"}) as r:
            events = events_of(r.iter_lines())
            _, ids = next(events)
            assert next(d for e, d in events if e == "delta") == {"text": "Здрав"}
            stream = chat.STREAMS[ids["assistant_message_id"]]
            state.DB.execute("UPDATE messages SET content = 'Здрав' WHERE id = ?", (ids["assistant_message_id"],))
            assert found("здрав") == []
            assert set(found("привет")) == {("chat", cid, "title"), ("chat", cid, ids["user_message_id"])}
    finally:
        studio.gw.chat_hold = False
    wait(studio, stream)
    assert found("здравствуйте") == [("chat", cid, ids["assistant_message_id"])]


# ---- текст расшифровок ------------------------------------------------------

def test_transcript_is_found_by_its_text_and_follows_the_edits(studio, fake_audio):
    """Готовая — по словам реплик; правка меняет найденное, «Сбросить правки» — возвращает."""
    add_gateway(studio)
    tid = upload(studio).json()["id"]
    wait_tr(studio, tid, {"done"})
    assert state.DB.execute("SELECT text FROM transcripts").fetchone()[0] == "Здравствуйте. Добрый день."
    assert found("здравств") == found("иван") == [("transcribe", tid, "")]   # имя записи — тоже
    assert studio.c.patch(f"/api/transcripts/{tid}", json={"texts": {"1": "Добрый вечер."}}).status_code == 200
    assert found("вечер") == [("transcribe", tid, "")] and found("день") == []
    assert studio.c.delete(f"/api/transcripts/{tid}/edits").status_code == 200
    assert found("вечер") == [] and found("день") == [("transcribe", tid, "")]


def test_backfill_fills_the_text_of_old_transcripts_from_their_files(studio, fake_audio, monkeypatch):
    """Расшифровка старого кода (без text): фон читает файл итога и пишет текст с правками; файла нет —
    пропуск; шара недоступна — фон кончается, текст ждёт следующего старта."""
    add_gateway(studio)
    tids = []
    for name in ("Первая.m4a", "Вторая.m4a", "Третья.m4a"):
        tids.append(upload(studio, name=name).json()["id"])
        wait_tr(studio, tids[-1], {"done"})
    state.DB.execute("UPDATE transcripts SET text = NULL, edits = ? WHERE id = ?",
                     ('{"texts": {"0": "Алло, слышно?"}, "names": {}}', tids[0]))
    state.DB.execute("UPDATE transcripts SET text = NULL WHERE id != ?", (tids[0],))
    result_file(tids[1]).unlink()
    broken_file = result_file(tids[2])
    assert found("алло") == []
    real = pathlib.Path.read_bytes

    def broken(self):
        if self == broken_file:
            raise OSError("share is gone")
        return real(self)
    monkeypatch.setattr(pathlib.Path, "read_bytes", broken)
    assert studio.c.portal.call(transcripts.backfill_text) == 1
    texts = dict(state.DB.execute("SELECT id, text FROM transcripts").fetchall())
    assert texts == {tids[0]: "Алло, слышно? Добрый день.", tids[1]: None, tids[2]: None}
    assert found("алло") == [("transcribe", tids[0], "")]
    monkeypatch.setattr(pathlib.Path, "read_bytes", real)
    assert studio.c.portal.call(transcripts.backfill_text) == 1                 # следующий старт
    assert state.DB.execute("SELECT text FROM transcripts WHERE id = ?", (tids[2],)).fetchone()[0] is not None
