"""Соединение SQLite и схема базы (версия 7).

Новая база получает SCHEMA сразу последней версии; прежняя — шаги STEPS от своей версии.
Шаг только добавляет: код предыдущей выкладки (откат install.sh) с новой базой работает."""
import sqlite3
from pathlib import Path

VERSION = 7

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

SCHEMA = SECTION_TABLES + SPEECH_TABLES + PASSKEY_TABLES + """
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
"""

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
}


def connect(path: Path) -> sqlite3.Connection:
    """Автокоммит (isolation_level=None): транзакции — явным BEGIN."""
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= VERSION:
        return
    body = SCHEMA if current == 0 else "\n".join(STEPS[v] for v in range(current + 1, VERSION + 1))
    # одной транзакцией: обрыв посреди не оставит полсхемы со старой user_version
    conn.executescript(f"BEGIN;\n{body}\nPRAGMA user_version = {VERSION};\nCOMMIT;")
