"""Соединение SQLite и схема базы (версия 13).

Новая база получает SCHEMA сразу последней версии; прежняя — шаги STEPS от своей версии.
Шаг только добавляет: код предыдущей выкладки (откат install.sh) с новой базой работает."""
import sqlite3
from pathlib import Path

VERSION = 13

#: разделы пользователя (sections.py): подключения раздела с отмеченными моделями (JSON) и умолчание
SECTION_TABLES = """
CREATE TABLE section_links(user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE, section TEXT NOT NULL,
             connection_id INTEGER NOT NULL REFERENCES connections ON DELETE CASCADE, models TEXT NOT NULL,
             created REAL NOT NULL, PRIMARY KEY (user_id, section, connection_id));
CREATE TABLE section_defaults(user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE, section TEXT NOT NULL,
             connection_id INTEGER NOT NULL REFERENCES connections ON DELETE CASCADE, model TEXT NOT NULL,
             PRIMARY KEY (user_id, section));
"""

#: речь (transcripts.py, voices.py, speeches.py): расшифровки, образцы голосов, озвучки
SPEECH_TABLES = """
CREATE TABLE transcripts(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
            connection_id INTEGER, model TEXT NOT NULL, name TEXT NOT NULL, source_media_id TEXT NOT NULL,
            result_media_id TEXT, duration REAL, speakers INTEGER, state TEXT NOT NULL, detail TEXT NOT NULL,
            created REAL NOT NULL, finished REAL);
CREATE TABLE voices(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       name TEXT NOT NULL, ref_text TEXT NOT NULL, media_id TEXT NOT NULL, duration REAL NOT NULL,
       created REAL NOT NULL);
CREATE TABLE speeches(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
         connection_id INTEGER, model TEXT NOT NULL, voice TEXT NOT NULL, text TEXT NOT NULL, media_id TEXT,
         duration REAL, state TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL, finished REAL);
CREATE INDEX transcripts_user_created ON transcripts(user_id, created);
CREATE INDEX voices_user_created ON voices(user_id, created);
CREATE INDEX speeches_user_created ON speeches(user_id, created);
"""

#: блокировка на телефоне (screenlock.py): ключи passkey учётки; id — credential id (base64url)
PASSKEY_TABLES = """
CREATE TABLE passkeys(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
         public_key BLOB NOT NULL, sign_count INTEGER NOT NULL, transports TEXT NOT NULL, name TEXT NOT NULL,
         created REAL NOT NULL, used REAL);
CREATE INDEX passkeys_user ON passkeys(user_id);
"""

#: очередь удаления файлов (media.py): путь media, чьей строки уже (удаление) или ещё (запись) нет.
#: user_id — без ссылки на users: удаление учётки путь не уносит, файл доудалится, когда шара вернётся
PURGE_TABLES = """
CREATE TABLE purge(media_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, rel_path TEXT NOT NULL,
      queued REAL NOT NULL, tries INTEGER NOT NULL DEFAULT 0);
CREATE INDEX purge_user ON purge(user_id);
"""

#: файлы учётки — по user_id: «Хранилище», стирание истории и каскад удаления учётки шли полным
#: просмотром media; идущие ответы — частичным индексом: предел chat.USER_STREAMS считает их на
#: каждом ходу и без него читал бы всю переписку пользователя
INDEXES_9 = """CREATE INDEX media_user ON media(user_id);
CREATE INDEX messages_streaming ON messages(chat_id) WHERE status = 'streaming';
"""

#: паспорт подключения (checks.py): итог «Проверить» JSON; смена адреса, вида или токена его обнуляет
PASSPORT_10 = "ALTER TABLE connections ADD COLUMN passport TEXT;\n"

#: правки расшифровки (JSON поверх файла итога, transcripts.py) и расшифровка, из которой сделано
#: вложение чата (без ссылки на transcripts: запись удаляют, вложение остаётся в диалоге)
EDITS_11 = """ALTER TABLE transcripts ADD COLUMN edits TEXT;
ALTER TABLE attachments ADD COLUMN source_transcript_id TEXT;
"""

#: фото (presets.py, images.py): пресеты учётки — настройки, а не история (стирание истории их не
#: трогает, удаление учётки — каскадом); картинки по исходнику — «Варианты» просмотра
PHOTO_12 = """CREATE TABLE presets(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
        section TEXT NOT NULL, name TEXT NOT NULL, params TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX presets_user ON presets(user_id, section);
CREATE INDEX images_user_source ON images(user_id, source_media_id);
"""

#: «ё» = «е» в индексе: unicode61 их не сворачивает; содержимое search_docs — исходное (snippet() выделяет
#: по номерам токенов, а их замена не меняет). Поэтому FTS5 расходится с search_docs намеренно, и ремонт —
#: только search.reindex ('delete-all' и вставка с этой заменой): штатный 'rebuild' берёт тело как есть и
#: молча теряет «ё» = «е», а ('integrity-check', 1) сверяет с содержимым и пишет «malformed»
YO = "replace(replace({}, 'ё', 'е'), 'Ё', 'Е')"
#: документ чата и текст расшифровки — в индексе первые INDEX_CHARS знаков: триггеры токенизируют в
#: транзакции хода и удаления (цикл событий), документ на 2 млн знаков — 0,65 с; удаление — то же тело
INDEX_CHARS = 200_000
_CHAT_MESSAGE = ("INSERT INTO search_docs(section, item, part, user_id, model, created, body)"
                 " SELECT 'chat', new.chat_id, new.id, c.user_id, new.model, new.created, new.content"
                 " FROM chats c WHERE c.id = new.chat_id")
_CHAT_DOCUMENT = ("INSERT INTO search_docs(section, item, part, user_id, model, created, body)"
                  " SELECT 'chat', m.chat_id, new.id, new.user_id, m.model, m.created,"
                  f" new.name || ' ' || substr(new.text, 1, {INDEX_CHARS})"
                  " FROM messages m WHERE m.id = new.message_id")
_TRANSCRIPT = f"new.name || ' ' || substr(COALESCE(new.text, ''), 1, {INDEX_CHARS})"
_DOC = "INSERT INTO search_docs(section, item, part, user_id, model, created, body) VALUES"

#: поиск по истории (search.py): документ — промпт, название диалога, сообщение, отправленный документ,
#: расшифровка (имя и текст), озвучка; part — '' у записей разделов, у чата — 'title', id сообщения или
#: вложения. Индекс держат триггеры таблиц истории — верен и при прежнем коде (откат install.sh).
#: FTS5 — с внешним содержимым: удаление и фильтры идут по индексам search_docs, а не просмотром FTS.
#: prefix='1 2 3' — индексы начал слов: без них snippet() по «к»* сливает списки позиций всех слов на «к»
#: на каждую строку выдачи (150 000 разных слов — 1,4 с в цикле событий).
#: transcripts.text — реплики с правками одной строкой (transcripts.py; старым — фон backfill_text)
SEARCH_13 = f"""ALTER TABLE transcripts ADD COLUMN text TEXT;
CREATE TABLE search_docs(id INTEGER PRIMARY KEY, section TEXT NOT NULL, item TEXT NOT NULL, part TEXT NOT NULL,
            user_id INTEGER NOT NULL, model TEXT, created REAL NOT NULL, body TEXT NOT NULL);
CREATE UNIQUE INDEX search_docs_key ON search_docs(section, item, part);
CREATE INDEX search_docs_user ON search_docs(user_id, created);
CREATE INDEX search_docs_part ON search_docs(part);
CREATE VIRTUAL TABLE search USING fts5(body, content='search_docs', content_rowid='id',
            tokenize='unicode61 remove_diacritics 2', prefix='1 2 3');
CREATE TRIGGER search_docs_ai AFTER INSERT ON search_docs BEGIN
  INSERT INTO search(rowid, body) VALUES (new.id, {YO.format('new.body')}); END;
CREATE TRIGGER search_docs_ad AFTER DELETE ON search_docs BEGIN
  INSERT INTO search(search, rowid, body) VALUES ('delete', old.id, {YO.format('old.body')}); END;
INSERT INTO search_docs(section, item, part, user_id, model, created, body)
SELECT 'photo', id, '', user_id, model, created, prompt FROM images
UNION ALL SELECT 'video', id, '', user_id, 'wan', created, prompt FROM videos
UNION ALL SELECT 'chat', id, 'title', user_id, model, created, title FROM chats WHERE title IS NOT NULL
UNION ALL SELECT 'chat', m.chat_id, m.id, c.user_id, m.model, m.created, m.content
            FROM messages m JOIN chats c ON c.id = m.chat_id WHERE m.status != 'streaming'
UNION ALL SELECT 'chat', m.chat_id, a.id, a.user_id, m.model, m.created, a.name || ' ' || substr(a.text, 1, {INDEX_CHARS})
            FROM attachments a JOIN messages m ON m.id = a.message_id WHERE a.kind = 'document' AND a.text IS NOT NULL
UNION ALL SELECT 'transcribe', id, '', user_id, model, created, name || ' ' || substr(COALESCE(text, ''), 1, {INDEX_CHARS})
            FROM transcripts
UNION ALL SELECT 'speech', id, '', user_id, model, created, text FROM speeches;
CREATE TRIGGER search_images_ai AFTER INSERT ON images BEGIN
  {_DOC} ('photo', new.id, '', new.user_id, new.model, new.created, new.prompt); END;
CREATE TRIGGER search_images_ad AFTER DELETE ON images BEGIN
  DELETE FROM search_docs WHERE section = 'photo' AND item = old.id; END;
CREATE TRIGGER search_videos_ai AFTER INSERT ON videos BEGIN
  {_DOC} ('video', new.id, '', new.user_id, 'wan', new.created, new.prompt); END;
CREATE TRIGGER search_videos_au AFTER UPDATE OF prompt ON videos WHEN old.prompt IS NOT new.prompt BEGIN
  DELETE FROM search_docs WHERE section = 'video' AND item = old.id;
  {_DOC} ('video', new.id, '', new.user_id, 'wan', new.created, new.prompt); END;
CREATE TRIGGER search_videos_ad AFTER DELETE ON videos BEGIN
  DELETE FROM search_docs WHERE section = 'video' AND item = old.id; END;
CREATE TRIGGER search_chats_ai AFTER INSERT ON chats WHEN new.title IS NOT NULL BEGIN
  {_DOC} ('chat', new.id, 'title', new.user_id, new.model, new.created, new.title); END;
CREATE TRIGGER search_chats_au AFTER UPDATE OF title, model ON chats
  WHEN old.title IS NOT new.title OR old.model IS NOT new.model BEGIN
  DELETE FROM search_docs WHERE section = 'chat' AND item = old.id AND part = 'title';
  INSERT INTO search_docs(section, item, part, user_id, model, created, body)
  SELECT 'chat', new.id, 'title', new.user_id, new.model, new.created, new.title WHERE new.title IS NOT NULL; END;
CREATE TRIGGER search_chats_ad AFTER DELETE ON chats BEGIN
  DELETE FROM search_docs WHERE section = 'chat' AND item = old.id; END;
CREATE TRIGGER search_messages_ai AFTER INSERT ON messages WHEN new.status != 'streaming' BEGIN
  {_CHAT_MESSAGE}; END;
CREATE TRIGGER search_messages_au AFTER UPDATE OF content, status ON messages
  WHEN old.status != 'streaming' OR new.status != 'streaming' BEGIN
  DELETE FROM search_docs WHERE section = 'chat' AND item = old.chat_id AND part = old.id;
  {_CHAT_MESSAGE} AND new.status != 'streaming'; END;
CREATE TRIGGER search_messages_ad AFTER DELETE ON messages BEGIN
  DELETE FROM search_docs WHERE section = 'chat' AND item = old.chat_id AND part = old.id; END;
CREATE TRIGGER search_attachments_ai AFTER INSERT ON attachments
  WHEN new.message_id IS NOT NULL AND new.kind = 'document' AND new.text IS NOT NULL BEGIN
  {_CHAT_DOCUMENT}; END;
CREATE TRIGGER search_attachments_au AFTER UPDATE OF message_id ON attachments
  WHEN old.message_id IS NULL AND new.message_id IS NOT NULL AND new.kind = 'document' AND new.text IS NOT NULL BEGIN
  {_CHAT_DOCUMENT}; END;
CREATE TRIGGER search_attachments_ad AFTER DELETE ON attachments WHEN old.message_id IS NOT NULL BEGIN
  DELETE FROM search_docs WHERE part = old.id AND section = 'chat'; END;
CREATE TRIGGER search_transcripts_ai AFTER INSERT ON transcripts BEGIN
  {_DOC} ('transcribe', new.id, '', new.user_id, new.model, new.created, {_TRANSCRIPT}); END;
CREATE TRIGGER search_transcripts_au AFTER UPDATE OF name, text ON transcripts
  WHEN old.name IS NOT new.name OR old.text IS NOT new.text BEGIN
  DELETE FROM search_docs WHERE section = 'transcribe' AND item = old.id;
  {_DOC} ('transcribe', new.id, '', new.user_id, new.model, new.created, {_TRANSCRIPT}); END;
CREATE TRIGGER search_transcripts_ad AFTER DELETE ON transcripts BEGIN
  DELETE FROM search_docs WHERE section = 'transcribe' AND item = old.id; END;
CREATE TRIGGER search_speeches_ai AFTER INSERT ON speeches BEGIN
  {_DOC} ('speech', new.id, '', new.user_id, new.model, new.created, new.text); END;
CREATE TRIGGER search_speeches_ad AFTER DELETE ON speeches BEGIN
  DELETE FROM search_docs WHERE section = 'speech' AND item = old.id; END;
CREATE TRIGGER search_users_ad AFTER DELETE ON users BEGIN
  DELETE FROM search_docs WHERE user_id = old.id; END;
"""

SCHEMA = SECTION_TABLES + SPEECH_TABLES + PASSKEY_TABLES + PURGE_TABLES + """
CREATE TABLE users(id INTEGER PRIMARY KEY, login TEXT NOT NULL UNIQUE, name TEXT NOT NULL, pw_hash TEXT NOT NULL,
      is_admin INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0,
      theme TEXT NOT NULL DEFAULT 'auto', default_connection_id INTEGER, default_model TEXT,
      created REAL NOT NULL, views TEXT NOT NULL DEFAULT '{}', screen_lock INTEGER NOT NULL DEFAULT 0);
CREATE TABLE sessions(digest TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
         csrf TEXT NOT NULL, expires REAL NOT NULL, renewed REAL NOT NULL, mobile INTEGER NOT NULL DEFAULT 0,
         seen REAL);
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE connections(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
            name TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('openai','srvllm')),
            base_url TEXT NOT NULL, token_enc BLOB NOT NULL, sections TEXT NOT NULL, created REAL NOT NULL,
            server_only INTEGER NOT NULL DEFAULT 0);
CREATE TABLE media(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
      rel_path TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, has_thumb INTEGER NOT NULL,
      download_name TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE chats(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE, title TEXT,
      connection_id INTEGER, model TEXT, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE messages(id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chats ON DELETE CASCADE,
         role TEXT NOT NULL CHECK (role IN ('user','assistant')), content TEXT NOT NULL,
         status TEXT NOT NULL CHECK (status IN ('done','streaming','stopped','error')),
         via TEXT NOT NULL CHECK (via IN ('server','browser')), model TEXT, created REAL NOT NULL,
         alive_at REAL);
CREATE TABLE attachments(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
            message_id TEXT REFERENCES messages ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('document','image')), name TEXT NOT NULL,
            media_id TEXT NOT NULL, text TEXT, chars INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE TABLE images(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       kind TEXT NOT NULL CHECK (kind IN ('generation','edit')), model TEXT NOT NULL, prompt TEXT NOT NULL,
       size TEXT NOT NULL, seed INTEGER NOT NULL, strength REAL, source_media_id TEXT, media_id TEXT,
       state TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL, finished REAL, mask_media_id TEXT,
       chain TEXT);
CREATE TABLE videos(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       connection_id INTEGER NOT NULL, gateway_id TEXT, source_media_id TEXT NOT NULL, media_id TEXT,
       prompt TEXT NOT NULL, prompt_source TEXT, seconds INTEGER NOT NULL, quality TEXT NOT NULL,
       smooth INTEGER NOT NULL, seed INTEGER, state TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
       detail TEXT NOT NULL, created REAL NOT NULL, finished REAL, chain TEXT);
CREATE INDEX images_user_created ON images(user_id, created);
CREATE INDEX videos_user_created ON videos(user_id, created);
CREATE INDEX chats_user_updated ON chats(user_id, updated);
CREATE INDEX messages_chat_created ON messages(chat_id, created);
INSERT INTO settings(key, value) VALUES ('allowlist', '["10.77.1.15:4000"]');
""" + INDEXES_9 + PASSPORT_10 + EDITS_11 + PHOTO_12 + SEARCH_13

#: версия → SQL, который приводит к ней базу предыдущей версии
STEPS = {
    # маска правки кистью (повтор правки идёт с ней); признак жизни ответа, который читает браузер
    2: "ALTER TABLE images ADD COLUMN mask_media_id TEXT;\nALTER TABLE messages ADD COLUMN alive_at REAL;",
    # цепочка промптов — снимок шагов JSON (chains.py); у старых строк NULL — собирается по ссылкам
    3: "ALTER TABLE images ADD COLUMN chain TEXT;\nALTER TABLE videos ADD COLUMN chain TEXT;",
    # разделы: srvllm — во всех шести с встроенным перечнем (чат — с моделью пользователя, если это его
    # подключение по умолчанию), openai — в чате; умолчание — первая отмеченная модель подключения
    # пользователя по умолчанию (чат) или наименьшего id. Прежние столбцы остаются для отката.
    4: SECTION_TABLES + """
INSERT INTO section_links(user_id, section, connection_id, models, created)
SELECT c.user_id, s.section, c.id,
       CASE WHEN s.section = 'chat'
            THEN json_array(COALESCE(CASE WHEN u.default_connection_id = c.id THEN u.default_model END, 'local-gemma-abl'))
            ELSE s.models END, c.created
  FROM connections c JOIN users u ON u.id = c.user_id
  JOIN (SELECT 'chat' AS section, NULL AS models UNION ALL SELECT 'photo', '["lustify","flux"]'
        UNION ALL SELECT 'video', '["wan"]' UNION ALL SELECT 'transcribe', '["whisperx"]'
        UNION ALL SELECT 'speech', '["qwen3-tts"]' UNION ALL SELECT 'dictation', '["gigaam","whisperx"]') s
 WHERE c.kind = 'srvllm';
INSERT INTO section_links(user_id, section, connection_id, models, created)
SELECT c.user_id, 'chat', c.id,
       CASE WHEN u.default_connection_id = c.id AND u.default_model IS NOT NULL THEN json_array(u.default_model)
            ELSE '[]' END, c.created
  FROM connections c JOIN users u ON u.id = c.user_id
 WHERE c.kind = 'openai' AND EXISTS (SELECT 1 FROM json_each(c.sections) WHERE value = 'chat');
INSERT INTO section_defaults(user_id, section, connection_id, model)
SELECT l.user_id, l.section, l.connection_id, json_extract(l.models, '$[0]') FROM section_links l
 WHERE json_array_length(l.models) > 0
   AND l.connection_id = COALESCE(
       (SELECT u.default_connection_id FROM users u WHERE u.id = l.user_id AND l.section = 'chat'
           AND EXISTS (SELECT 1 FROM section_links d WHERE d.user_id = u.id AND d.section = 'chat'
                          AND d.connection_id = u.default_connection_id AND json_array_length(d.models) > 0)),
       (SELECT MIN(x.connection_id) FROM section_links x WHERE x.user_id = l.user_id AND x.section = l.section
           AND json_array_length(x.models) > 0));""",
    # речь: расшифровки записей, образцы голосов и озвучки
    5: SPEECH_TABLES,
    # вид ленты разделов (profile.py) — в учётке: JSON {раздел: вид}
    6: "ALTER TABLE users ADD COLUMN views TEXT NOT NULL DEFAULT '{}';",
    # блокировка на телефоне: включена ли у учётки, мобильна ли сессия, последняя отметка «на экране»
    7: """ALTER TABLE users ADD COLUMN screen_lock INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN mobile INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN seen REAL;""" + PASSKEY_TABLES,
    # очередь удаления файлов: не удалившийся или недописанный файл не теряет свой путь
    8: PURGE_TABLES,
    # индексы: файлы по учётке, идущие ответы
    9: INDEXES_9,
    # паспорт подключения: проверка адреса, ключа и возможностей моделей
    10: PASSPORT_10,
    # правки расшифровки; вложение чата из расшифровки
    11: EDITS_11,
    # фото: пресеты учётки, индекс картинок по исходнику
    12: PHOTO_12,
    # поиск по истории: документы, FTS5 и триггеры; текст расшифровок
    13: SEARCH_13,
}


def connect(path: Path) -> sqlite3.Connection:
    """Автокоммит (isolation_level=None): транзакции — явным BEGIN."""
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def page(conn: sqlite3.Connection, table: str, user_id: int, before: "float | None", before_id: "str | None",
         limit: int, columns: str = "*") -> sqlite3.Cursor:
    """Страница ленты истории (images, videos, transcripts, speeches): новые сверху — created, затем id по
    убыванию. Курсор — created и id последней записи прежней страницы: у записей бывает одно время.
    Без before_id (вкладка до выкладки составного курсора) — только created, записи того же времени
    пропадают. table и columns — из кода, не из запроса."""
    if before is None:
        where, args = "", ()
    elif before_id is None:
        where, args = " AND created < ?", (before,)
    else:
        where, args = " AND (created < ? OR (created = ? AND id < ?))", (before, before, before_id)
    return conn.execute(f"SELECT {columns} FROM {table} WHERE user_id = ?{where} ORDER BY created DESC, id DESC LIMIT ?",
                        (user_id, *args, limit))


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= VERSION:
        return
    body = SCHEMA if current == 0 else "\n".join(STEPS[v] for v in range(current + 1, VERSION + 1))
    # одной транзакцией: обрыв посреди не оставит полсхемы со старой user_version
    conn.executescript(f"BEGIN;\n{body}\nPRAGMA user_version = {VERSION};\nCOMMIT;")
