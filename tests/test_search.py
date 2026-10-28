"""Поиск по истории (app/search.py, шаг схемы 13): индекс держат триггеры таблиц истории, ответ чата —
после «готово», расшифровки — по тексту (`transcripts.text`), запрос — телом POST, только своё."""
import logging
import pathlib
import time
from datetime import datetime

import auth
import chat
import db
import search as search_api
import state
import transcripts
from conftest import T0, add_gateway, make_user, signin
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
SCAN = ("INSERT INTO scans(id, user_id, model, name, origin, pages, state, detail, created, text)"
        " VALUES (?, ?, 'hunyuan-ocr', ?, 'section', 1, 'done', 'готово', ?, ?)")
TRANSLATION = ("INSERT INTO translations(id, user_id, kind, model, source_lang, target_lang, name, source, state,"
               " detail, created) VALUES (?, ?, 'text', 'local-gemma-abl', 'auto', 'ru', ?, ?, 'queued', 'в очереди', ?)")
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


def test_a_chat_title_row_follows_the_model_of_the_chat(studio):
    """Строка названия несёт модель диалога: сменили модель — фильтр по модели находит по новой."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    state.DB.execute(CHAT, ("c1", uid, "Планы", "old-model", T0, T0))
    state.DB.execute("UPDATE chats SET model = 'new-model' WHERE id = 'c1'")
    assert hits(search(studio, model="new-model")) == [("chat", "c1")]
    assert hits(search(studio, model="old-model")) == []


def test_large_documents_and_transcripts_are_indexed_by_their_start(studio):
    """Документ на 2 млн знаков и длинная расшифровка — в индексе первые INDEX_CHARS знаков: триггеры
    токенизируют в транзакции хода (цикл событий), и удаление — тот же текст."""
    uid = make_user(studio, "owner")
    big = "начало " + "слово " * (db.INDEX_CHARS // 6) + "хвост " + "слово " * 300_000
    state.DB.execute(CHAT, ("c1", uid, "Отчёт", "m", 1, 1))
    state.DB.execute(MESSAGE, ("m1", "c1", "user", "вот", "m", 2))
    state.DB.execute("INSERT INTO attachments(id, user_id, message_id, kind, name, media_id, text, created)"
                     " VALUES ('a1', ?, NULL, 'document', 'отчёт.txt', 'x', ?, 3)", (uid, big))
    state.DB.execute("UPDATE attachments SET message_id = 'm1' WHERE id = 'a1'")
    state.DB.execute(TRANSCRIPT, ("t1", uid, "Звонок.m4a", 4, big))
    assert found("начал") == [("chat", "c1", "a1"), ("transcribe", "t1", "")]
    assert found("хвост") == []
    assert state.DB.execute("SELECT MAX(length(body)) FROM search_docs").fetchone()[0] <= db.INDEX_CHARS + 20
    state.DB.execute("DELETE FROM chats WHERE id = 'c1'")
    state.DB.execute("DELETE FROM transcripts WHERE id = 't1'")
    state.DB.execute("INSERT INTO search(search) VALUES ('integrity-check')")    # удаление — тем же текстом
    assert found("начал") == [] and found("слово") == []


def test_scan_text_is_found_and_removed_with_the_scan(studio):
    """Распознавание (шаг 16): документ — имя и текст (первые INDEX_CHARS); текст пишется по готовности одним
    обновлением — индекс по новому; удаление документа и стирание истории уносят его из выдачи."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    state.DB.execute(SCAN, ("o1", uid, "Договор.pdf", T0, "арендатор обязуется"))
    r = search(studio, q="арендатор")
    assert hits(r) == [("ocr", "o1")]
    item = r.json()["items"][0]
    assert (item["title"], item["href"], item["model"], item["part"]) == ("Договор.pdf", "ocr?id=o1", "hunyuan-ocr", "")
    assert hits(search(studio, q="договор")) == hits(search(studio, section="ocr")) == [("ocr", "o1")]
    assert studio.c.get("/api/search/models").json() == {"models": {"ocr": ["hunyuan-ocr"]}}
    state.DB.execute("UPDATE scans SET text = 'арендодатель вправе', name = 'Акт.pdf' WHERE id = 'o1'")
    assert hits(search(studio, q="обязуется")) == [] and hits(search(studio, q="договор")) == []
    assert hits(search(studio, q="арендодатель акт")) == [("ocr", "o1")]
    state.DB.execute("DELETE FROM scans WHERE id = 'o1'")
    assert hits(search(studio, q="арендодатель")) == []
    state.DB.execute(SCAN, ("o2", uid, "Скан.pdf", T0, None))                 # ещё не распознан — по имени
    assert found("скан") == [("ocr", "o2", "")]
    state.DB.execute("UPDATE scans SET text = ? WHERE id = 'o2'", ("начало " + "слово " * (db.INDEX_CHARS // 6) + "хвост",))
    assert found("начал") == [("ocr", "o2", "")] and found("хвост") == []
    assert studio.c.delete("/api/history").status_code == 200
    assert found("скан") == [] and state.DB.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0] == 0


def test_search_finds_a_translation_by_source_and_result(studio):
    """Перевод (шаг 18): запись — имя, исходник и перевод (пишется в конце задания одним обновлением);
    фильтр раздела, модели, удаление записи уносит её из выдачи."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    state.DB.execute(TRANSLATION, ("p1", uid, "Письмо партнёру", "Dear partner, the contract", T0))
    r = search(studio, q="contract")
    assert hits(r) == [("translate", "p1")]
    item = r.json()["items"][0]
    assert (item["title"], item["href"], item["model"], item["part"]) == (
        "Письмо партнёру", "translate?id=p1", "local-gemma-abl", "")
    assert hits(search(studio, q="партнер")) == hits(search(studio, section="translate")) == [("translate", "p1")]
    assert studio.c.get("/api/search/models").json() == {"models": {"translate": ["local-gemma-abl"]}}
    assert hits(search(studio, q="договор")) == []
    state.DB.execute("UPDATE translations SET text = 'Уважаемый партнёр, договор' WHERE id = 'p1'")
    assert hits(search(studio, q="договор")) == [("translate", "p1")]
    state.DB.execute("DELETE FROM translations WHERE id = 'p1'")
    assert hits(search(studio, q="contract")) == [] and found("договор") == []


def test_search_survives_a_section_this_code_does_not_know(studio):
    """Строки раздела новее кода (откат install.sh после следующего шага схемы) — не в выдаче, не в
    моделях и не 500: прежний _title искал бы их заголовок по TITLES (ревью I2)."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    seed(uid)
    state.DB.execute("INSERT INTO search_docs(section, item, part, user_id, model, created, body)"
                     " VALUES ('future', 'f1', '', ?, 'm-next', ?, 'Кошка из будущего')", (uid, T0 + 99))
    assert hits(search(studio, q="кошк")) == [("chat", "c"), ("speech", "s"), ("transcribe", "t"), ("photo", "i")]
    assert hits(search(studio, q="будущ")) == [] and hits(search(studio, model="m-next")) == []
    page = search(studio, q="кошк", limit=1)
    cursor = {k: page.json()["items"][0][k] for k in ("created", "part")} | {"item": page.json()["items"][0]["id"]}
    assert hits(search(studio, q="кошк", limit=1, before=cursor)) == [("speech", "s")]
    assert "future" not in studio.c.get("/api/search/models").json()["models"]


def test_reindex_repairs_the_index_and_keeps_yo_where_rebuild_loses_it(studio):
    """Ремонт индекса: штатный 'rebuild' FTS5 берёт тело search_docs как есть и теряет «ё» = «е»;
    search.reindex — 'delete-all' и вставка с той же заменой, что у триггеров."""
    uid = make_user(studio, "owner")
    seed(uid)
    state.DB.execute("INSERT INTO search(search) VALUES ('rebuild')")
    assert found("елк") == []                                 # ловушка, ради которой reindex
    search_api.reindex(state.DB)
    assert found("елк") == [("speech", "s", ""), ("video", "v", "")]
    state.DB.execute("INSERT INTO search(search) VALUES ('integrity-check')")
    state.DB.execute("DELETE FROM videos WHERE id = 'v'")
    assert found("елк") == [("speech", "s", "")] and len(found("кош")) == 4


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
    assert found("алло") == []
    reads, real = [], pathlib.Path.read_bytes

    def gone(self):
        reads.append(self)
        raise OSError("share is gone")
    monkeypatch.setattr(pathlib.Path, "read_bytes", gone)
    assert studio.c.portal.call(transcripts.backfill_text) == 0
    assert len(reads) == 1                                          # шары нет — дальше не читает
    monkeypatch.setattr(pathlib.Path, "read_bytes", real)
    assert studio.c.portal.call(transcripts.backfill_text) == 2                 # следующий старт
    texts = dict(state.DB.execute("SELECT id, text FROM transcripts").fetchall())
    assert texts == {tids[0]: "Алло, слышно? Добрый день.", tids[1]: None, tids[2]: "Здравствуйте. Добрый день."}
    assert found("алло") == [("transcribe", tids[0], "")]


def test_reset_with_the_share_gone_keeps_the_edits_and_the_text_together(studio, fake_audio, monkeypatch):
    """«Сбросить правки» читает файл итога до записи: шары нет — 503, а правки и текст для поиска прежние
    (иначе правки сняты, а текст с ними навсегда: фон заполняет только пустой)."""
    add_gateway(studio)
    tid = upload(studio).json()["id"]
    wait_tr(studio, tid, {"done"})
    assert studio.c.patch(f"/api/transcripts/{tid}", json={"texts": {"1": "Добрый вечер."}}).status_code == 200
    real = pathlib.Path.read_bytes

    def gone(self):
        raise OSError("share is gone")
    monkeypatch.setattr(pathlib.Path, "read_bytes", gone)
    assert studio.c.delete(f"/api/transcripts/{tid}/edits").status_code == 503
    monkeypatch.setattr(pathlib.Path, "read_bytes", real)
    edits, text = state.DB.execute("SELECT edits, text FROM transcripts").fetchone()
    assert edits is not None and text == "Здравствуйте. Добрый вечер." and found("вечер") == [("transcribe", tid, "")]
    assert studio.c.delete(f"/api/transcripts/{tid}/edits").status_code == 200
    assert found("вечер") == [] and found("день") == [("transcribe", tid, "")]


def test_backfill_refreshes_the_text_of_transcripts_edited_by_the_old_code(studio, fake_audio):
    """Откат install.sh: прежний код правит edits, а text не трогает — новый старт пересчитывает текст
    правленых (их мало), неизменный — не переписывает."""
    add_gateway(studio)
    tid = upload(studio).json()["id"]
    wait_tr(studio, tid, {"done"})
    state.DB.execute("UPDATE transcripts SET edits = ? WHERE id = ?",
                     ('{"texts": {"1": "Добрый вечер."}, "names": {}}', tid))
    assert found("вечер") == []
    assert studio.c.portal.call(transcripts.backfill_text) == 1
    assert found("вечер") == [("transcribe", tid, "")] and found("день") == []
    assert studio.c.portal.call(transcripts.backfill_text) == 0


def test_transcript_rows_for_the_feed_leave_the_search_text_behind(studio, fake_audio, monkeypatch):
    """Лента, опрос раз в 3 с и строка записи текст для поиска не тянут — он нужен только индексу."""
    add_gateway(studio)
    tid = upload(studio).json()["id"]
    wait_tr(studio, tid, {"done"})
    uid = state.DB.execute("SELECT user_id FROM transcripts").fetchone()[0]
    assert "text" not in transcripts._row(uid, tid).keys()
    listed, real = [], db.page

    def spy(*args, **kwargs):
        rows = real(*args, **kwargs).fetchall()
        listed.extend(rows)
        return rows
    monkeypatch.setattr(db, "page", spy)
    assert studio.c.get("/api/transcripts").status_code == 200
    assert listed and all("text" not in r.keys() for r in listed)


# ---- POST /api/search ---------------------------------------------------------

def search(s, **body):
    return s.c.post("/api/search", json=body)


def hits(r) -> list:
    assert r.status_code == 200, r.text
    return [(i["section"], i["id"]) for i in r.json()["items"]]


def seed(uid: int, tag: str = "", at: float = T0) -> None:
    """По записи каждого раздела со словами «кошка» и «ёлка»; id — с меткой учётки."""
    state.DB.execute(IMAGE, (f"i{tag}", uid, "lustify", "Кошка на крыше", at + 1))
    state.DB.execute(VIDEO, (f"v{tag}", uid, "Ёлка в снегу", at + 2))
    state.DB.execute(TRANSCRIPT, (f"t{tag}", uid, "Звонок.m4a", at + 3, "Кошкин дом сгорел"))
    state.DB.execute(SPEECH, (f"s{tag}", uid, "КОШКА мурлычет у ёлки", at + 4))
    state.DB.execute(CHAT, (f"c{tag}", uid, "Про зверей", "local-gemma-abl", at + 5, at + 5))
    state.DB.execute(MESSAGE, (f"m{tag}", f"c{tag}", "assistant", "Две кошки спят", "local-gemma-abl", at + 6))


def test_each_section_is_found_by_the_start_of_a_word_in_any_case_and_yo(studio):
    """«кошк» — фото, расшифровка (текст), озвучка (заглавными), диалог (ответ модели); «елка» — «Ёлка»."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    seed(uid)
    r = search(studio, q="кошк")
    assert hits(r) == [("chat", "c"), ("speech", "s"), ("transcribe", "t"), ("photo", "i")]
    by = {i["section"]: i for i in r.json()["items"]}
    assert by["photo"] == {"section": "photo", "id": "i", "part": "", "title": "Кошка на крыше",
                           "snippet": "\u0002Кошка\u0003 на крыше", "model": "lustify", "created": T0 + 1,
                           "href": "photo?id=i"}
    assert by["chat"]["title"] == "Про зверей" and by["chat"]["href"] == "chat/c" and by["chat"]["part"] == "m"
    assert by["chat"]["snippet"] == "Две \u0002кошки\u0003 спят"
    assert by["transcribe"]["title"] == "Звонок.m4a" and by["transcribe"]["href"] == "transcribe?id=t"
    assert by["speech"]["href"] == "speech?id=s" and r.json()["more"] is False
    assert hits(search(studio, q="ЕЛК")) == [("speech", "s"), ("video", "v")]
    assert search(studio, q="елка").json()["items"][0]["snippet"] == "\u0002Ёлка\u0003 в снегу"
    assert hits(search(studio, q="кошка ёлки")) == [("speech", "s")]          # все слова
    assert hits(search(studio, q="кот")) == []


def test_another_users_history_is_never_found(studio):
    owner, other = make_user(studio, "owner"), make_user(studio, "other")
    seed(owner); seed(other, "x")
    state.DB.execute(IMAGE, ("ix2", other, "flux", "Чужая кошка", T0 + 9))
    signin(studio, "owner")
    assert {i for _, i in hits(search(studio, q="кошк"))} == {"c", "s", "t", "i"}
    assert {i for _, i in hits(search(studio, section="photo"))} == {"i"}
    assert studio.c.get("/api/search/models").json() == {"models": {
        "chat": ["local-gemma-abl"], "photo": ["lustify"], "speech": ["qwen3-tts"], "transcribe": ["whisperx"],
        "video": ["wan"]}}


def test_filters_narrow_and_alone_make_a_list(studio):
    """Раздел, модель, дни (по часам хоста, «по» — включительно); без слов — тоже список, сниппета нет."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    day = datetime(2027, 1, 11).timestamp()
    for n, at in enumerate((day - 3600, day + 12 * 3600, day + 86400 - 1, day + 86400 + 60)):
        state.DB.execute(IMAGE, (f"i{n}", uid, "flux" if n == 2 else "lustify", f"Кошка {n}", at))
    state.DB.execute(VIDEO, ("v", uid, "Кошка бежит", day + 3600))
    assert hits(search(studio, q="кошк", section="photo", model="lustify")) == [("photo", "i3"), ("photo", "i1"),
                                                                                ("photo", "i0")]
    assert hits(search(studio, q="кошк", since="2027-01-11", until="2027-01-11")) == [
        ("photo", "i2"), ("photo", "i1"), ("video", "v")]
    listed = search(studio, section="video").json()["items"]
    assert [(i["id"], i["snippet"], i["title"]) for i in listed] == [("v", "", "Кошка бежит")]
    assert hits(search(studio, since="2027-01-12")) == [("photo", "i3")]
    assert hits(search(studio, model="flux")) == [("photo", "i2")]


def test_pages_follow_the_cursor_without_losing_rows_of_the_same_time(studio):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    for n in range(5):
        state.DB.execute(IMAGE, (f"i{n}", uid, "flux", "Кошка", T0))
    state.DB.execute(IMAGE, ("old", uid, "flux", "Кошка", T0 - 1))
    seen, before = [], None
    while True:
        body = search(studio, q="кошк", limit=2, **({"before": before} if before else {})).json()
        seen += [i["id"] for i in body["items"]]
        if not body["more"]:
            break
        last = body["items"][-1]
        before = {"created": last["created"], "item": last["id"], "part": last["part"]}
    assert seen == ["i4", "i3", "i2", "i1", "i0", "old"]


def test_a_chat_comes_once_by_its_latest_match(studio):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    state.DB.execute(CHAT, ("c1", uid, "Кошки", "m", T0, T0))
    for n in range(3):
        state.DB.execute(MESSAGE, (f"m{n}", "c1", "user", f"кошка номер {n}", "m", T0 + n))
    state.DB.execute(MESSAGE, ("m9", "c1", "assistant", "про собак", "m", T0 + 9))
    items = search(studio, q="кошк").json()["items"]
    assert [(i["id"], i["part"], i["created"]) for i in items] == [("c1", "m2", T0 + 2)]
    assert [i["part"] for i in search(studio, section="chat").json()["items"]] == ["m9"]
    # страницы по одной: картинки между сообщениями диалога — диалог не возвращается следующей страницей
    for n, at in ((1, T0 + 1.5), (2, T0 + 0.5)):
        state.DB.execute(IMAGE, (f"i{n}", uid, "flux", "Кошка", at))
    seen, before = [], None
    while True:
        body = search(studio, q="кошк", limit=1, **({"before": before} if before else {})).json()
        seen += [i["id"] for i in body["items"]]
        if not body["more"]:
            break
        last = body["items"][-1]
        before = {"created": last["created"], "item": last["id"], "part": last["part"]}
    assert seen == ["c1", "i1", "i2"]


def test_a_question_and_its_answer_of_one_time_show_the_answer(studio):
    """Ход пишет вопрос и ответ с одним created: «последнее совпадение» диалога — ответ (он позже в
    индексе), а не случайный из двух по id."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    state.DB.execute(CHAT, ("c1", uid, None, "m", T0, T0))
    state.DB.execute(MESSAGE, ("mz", "c1", "user", "где кошка", "m", T0 + 1))      # id вопроса — больше
    state.DB.execute(MESSAGE, ("ma", "c1", "assistant", "кошка на крыше", "m", T0 + 1))
    assert [i["part"] for i in search(studio, q="кошк").json()["items"]] == ["ma"]


def test_deleted_records_chats_and_the_wiped_history_are_gone_at_once(studio):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    seed(uid)
    assert studio.c.delete("/api/images/i").status_code == 204
    assert studio.c.delete("/api/chats/c").status_code == 204
    assert hits(search(studio, q="кошк")) == [("speech", "s"), ("transcribe", "t")]
    assert studio.c.delete("/api/history").status_code == 200
    assert hits(search(studio, q="кошк")) == [] and hits(search(studio, section="video")) == []


def test_fts_syntax_in_the_query_is_just_words(studio):
    """Кавычки, звёздочки, скобки, NEAR, OR и «-» — разделители и слова, а не синтаксис FTS5."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    seed(uid)
    state.DB.execute(IMAGE, ("near", uid, "flux", "NEAR OR кошка", T0 + 20))
    assert hits(search(studio, q='"кош*" NEAR OR')) == [("photo", "near")]
    assert hits(search(studio, q='кошк -крыш (')) == [("photo", "i")]           # «-» — не «кроме»
    assert search(studio, q='"*() -').status_code == 400
    assert hits(search(studio, q='"*', section="photo")) == [("photo", "near"), ("photo", "i")]


def test_refusals_need_words_or_a_filter_a_session_and_csrf(studio):
    uid = make_user(studio, "owner")
    assert search(studio, q="кошк").status_code == 401
    signin(studio, "owner"); seed(uid)
    r = search(studio, q="  ")
    assert r.status_code == 400 and r.json()["detail"]["code"] == "input"
    assert search(studio, q="к" * 201).status_code == 422
    assert search(studio, section="voices").status_code == 422
    assert search(studio, since="2027-02-30").status_code == 422
    for days in ({"until": "9999-12-31"}, {"since": "9999-12-31"}, {"since": "0001-01-01"}, {"until": "1999-12-31"}):
        r = search(studio, **days)                   # иначе OverflowError (Linux) / OSError (Windows) — 500
        assert r.status_code == 422 and r.json()["detail"]["message"] == search_api.DAYS_WORDS, days
    assert search(studio, since="2000-01-01", until="2100-12-31").status_code == 200
    assert search(studio, q="кошк", limit=51).status_code == 422
    csrf = studio.c.headers.pop(auth.CSRF_HEADER)
    assert search(studio, q="кошк").status_code == 403
    studio.c.headers[auth.CSRF_HEADER] = csrf


def test_the_log_gets_the_count_not_the_words(studio, caplog):
    caplog.set_level(logging.DEBUG)
    uid = make_user(studio, "owner"); signin(studio, "owner")
    seed(uid)
    assert len(hits(search(studio, q="кошк", model="lustify", since="2027-01-01"))) == 1
    assert "search: 1" in caplog.text
    assert "кошк" not in caplog.text.lower() and "lustify" not in caplog.text and "2027-01-01" not in caplog.text


def test_search_of_twenty_thousand_rows_is_quick(studio):
    """Анти-цель «поиск тормозит службу»: 20 000 строк учётки — ответ быстрее 0,2 с, и общим словом
    (совпали все), и одними фильтрами."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    state.DB.execute(                                # одним запросом: строки из Python — секунды
        "WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i + 1 FROM n WHERE i < 19999)"
        " INSERT INTO images(id, user_id, kind, model, prompt, size, seed, state, detail, created)"
        " SELECT printf('i%05d', i), ?, 'generation', 'flux', 'Кошка номер ' || i || ' на крыше', '1024x1024', 1,"
        " 'done', 'готово', ? + i FROM n", (uid, T0))
    for body in ({"q": "крыш кошк"}, {"section": "photo"}):
        spent = []
        for _ in range(2):                           # лучшее из двух: соседние процессы набора делят ядра
            started = time.perf_counter()
            assert search(studio, **body).status_code == 200
            spent.append(time.perf_counter() - started)
        assert min(spent) < 0.2, (body, spent)
    assert hits(search(studio, q="номер 19999")) == [("photo", "i19999")]


def test_next_pages_of_chats_are_as_quick_as_the_first(studio):
    """Со словами запросы идут от FTS5 и с курсором, и с фильтром: от индекса search_docs MATCH шёл заново на
    каждую строку истории — вторая страница диалогов на 20 000 сообщений длилась секунды, а первая — десятую."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    state.DB.execute(                                # 500 диалогов по 10 сообщений со словами запроса
        "WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i + 1 FROM n WHERE i < 499)"
        " INSERT INTO chats(id, user_id, title, model, created, updated)"
        " SELECT printf('c%05d', i), ?, 'диалог', 'm', ? + i * 10, ? + i * 10 FROM n", (uid, T0, T0))
    state.DB.execute(
        "WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i + 1 FROM n WHERE i < 4999)"
        " INSERT INTO messages(id, chat_id, role, content, status, via, model, created)"
        " SELECT printf('m%05d', i), printf('c%05d', i / 10), 'user', 'кошка на крыше', 'done', 'server', 'm', ? + i"
        " FROM n", (T0,))
    before, best, chats = None, [], []
    for _ in range(3):
        spent = []
        for _ in range(2):                           # лучшее из двух: соседние процессы набора делят ядра
            started = time.perf_counter()
            r = search(studio, q="крыш кошк", section="chat", before=before)
            spent.append(time.perf_counter() - started)
        best.append(min(spent))
        items = r.json()["items"]
        chats += [i["id"] for i in items]
        before = {k: items[-1][k] for k in ("created", "id", "part")}
        before["item"] = before.pop("id")
    assert chats == [f"c{i:05d}" for i in range(499, 409, -1)]   # диалог — один раз, по порядку
    assert max(best[1:]) < 2 * best[0] + 0.05, best


def test_one_letter_over_a_large_vocabulary_is_quick(studio):
    """Короткое начало слова: snippet() по «к»* без индекса префиксов сливал списки позиций всех слов
    на «к» на каждую строку выдачи (150 000 разных слов — 1,4 с в цикле событий); с prefix='1 2 3' —
    миллисекунды."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    state.DB.execute(                                # 3000 промптов по 50 разных слов на «к»
        "WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i + 1 FROM n WHERE i < 2999),"
        " w(j) AS (SELECT 0 UNION ALL SELECT j + 1 FROM w WHERE j < 49)"
        " INSERT INTO images(id, user_id, kind, model, prompt, size, seed, state, detail, created)"
        " SELECT printf('i%05d', i), ?, 'generation', 'flux',"
        " (SELECT group_concat('к' || lower(hex(randomblob(3))), ' ') FROM w WHERE i >= 0),"
        " '1024x1024', 1, 'done', 'готово', ? + i FROM n", (uid, T0))
    spent = []
    for _ in range(2):                               # лучшее из двух: соседние процессы набора делят ядра
        started = time.perf_counter()
        r = search(studio, q="к")
        spent.append(time.perf_counter() - started)
        assert r.status_code == 200 and len(r.json()["items"]) == 30 and "\u0002" in r.json()["items"][0]["snippet"]
    assert min(spent) < 0.2, spent
