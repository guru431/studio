"""Соединение SQLite и схема базы (версия — VERSION).

Новая база получает SCHEMA сразу последней версии; прежняя — шаги STEPS от своей версии.
Шаг только добавляет: код предыдущей выкладки (откат install.sh) с новой базой работает."""
import sqlite3
from pathlib import Path

VERSION = 78
DEFAULT_THEME = "dark"
MEDIA_44 = "ALTER TABLE videos ADD COLUMN model TEXT NOT NULL DEFAULT 'wan';"

MCP_43 = """CREATE TABLE mcp_servers(id INTEGER PRIMARY KEY AUTOINCREMENT,
       user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       name TEXT NOT NULL, url TEXT NOT NULL, secret_enc TEXT NOT NULL,
       enabled INTEGER NOT NULL DEFAULT 1);
CREATE INDEX mcp_servers_user ON mcp_servers(user_id);
ALTER TABLE chats ADD COLUMN mcp_ids TEXT NOT NULL DEFAULT '[]';
"""

# Each voice keeps its selected reference in the old columns for rollback compatibility.
VOICE_42 = """CREATE TABLE voice_samples(id TEXT PRIMARY KEY,
       voice_id TEXT NOT NULL REFERENCES voices ON DELETE CASCADE,
       user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       ref_text TEXT NOT NULL, media_id TEXT NOT NULL, duration REAL NOT NULL, created REAL NOT NULL);
CREATE INDEX voice_samples_voice ON voice_samples(voice_id, created, id);
CREATE INDEX voice_samples_user ON voice_samples(user_id);
INSERT INTO voice_samples SELECT id, id, user_id, ref_text, media_id, duration, created FROM voices;
CREATE TRIGGER voices_sample_ai AFTER INSERT ON voices BEGIN
  INSERT INTO voice_samples VALUES(NEW.id, NEW.id, NEW.user_id, NEW.ref_text, NEW.media_id, NEW.duration, NEW.created);
END;
"""

# Происхождение нового чата известно до первой метки моста. Прежние строки используют метку сессии.
CODE_41 = """ALTER TABLE code_chats ADD COLUMN origin TEXT NOT NULL DEFAULT 'other'
    CHECK (origin IN ('studio', 'other'));
"""

# NULL — только у прежних учёток до обязательного подтверждения почты.
EMAIL_40 = """ALTER TABLE users ADD COLUMN email TEXT COLLATE NOCASE;
ALTER TABLE users ADD COLUMN email_codes INTEGER NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX users_email ON users(email) WHERE email IS NOT NULL;
CREATE TABLE invitations(id TEXT PRIMARY KEY, email TEXT NOT NULL COLLATE NOCASE UNIQUE,
       comment TEXT NOT NULL DEFAULT '', is_admin INTEGER NOT NULL DEFAULT 0,
       invited_by INTEGER REFERENCES users ON DELETE SET NULL, created REAL NOT NULL,
       expires REAL NOT NULL, token_hash TEXT UNIQUE, accepted REAL, revoked REAL,
       user_id INTEGER REFERENCES users ON DELETE SET NULL, sent INTEGER NOT NULL DEFAULT 0);
CREATE TABLE email_tokens(digest TEXT PRIMARY KEY, kind TEXT NOT NULL,
       user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE, email TEXT NOT NULL,
       password_stamp TEXT NOT NULL, secret_hash TEXT, next TEXT NOT NULL DEFAULT '',
       created REAL NOT NULL, expires REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0);
CREATE INDEX email_tokens_user ON email_tokens(user_id, kind);
CREATE TABLE email_limits(key TEXT PRIMARY KEY, started REAL NOT NULL, count INTEGER NOT NULL);
"""

#: выдача и отзыв доступа к проекту — в истории подключений компьютера (project_access): CHECK видов шире, как
#: CODE_23 у code_chats. Входящих FK у agent_events нет; id, строки и индекс сохраняются. Прежний код пишет свои
#: виды, а новые показывает как есть (`member_add`, `member_remove`)
AGENT_EVENTS_39 = """CREATE TABLE agent_events_39(id INTEGER PRIMARY KEY,
       user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       agent_id INTEGER NOT NULL, agent_name TEXT NOT NULL, created REAL NOT NULL,
       kind TEXT NOT NULL CHECK (kind IN ('enroll','revoke','online','offline','open','pin_ok','pin_fail','session_start',
                                          'member_add','member_remove')),
       device TEXT, ip TEXT);
INSERT INTO agent_events_39 SELECT id, user_id, agent_id, agent_name, created, kind, device, ip FROM agent_events;
DROP TABLE agent_events;
ALTER TABLE agent_events_39 RENAME TO agent_events;
CREATE INDEX agent_events_user ON agent_events(user_id, id);
"""

PROJECTS_38 = """CREATE TABLE project_members(
       agent_id INTEGER NOT NULL REFERENCES agents ON DELETE CASCADE, project TEXT NOT NULL,
       user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE, created REAL NOT NULL,
       PRIMARY KEY(agent_id, project, user_id));
CREATE INDEX project_members_user ON project_members(user_id, agent_id);
CREATE TABLE code_member_tabs(user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       agent_id INTEGER NOT NULL REFERENCES agents ON DELETE CASCADE, project TEXT NOT NULL,
       tabs TEXT NOT NULL DEFAULT '[]', active TEXT, touched REAL NOT NULL,
       PRIMARY KEY(user_id, agent_id, project));
"""

#: Краткие сведения сессии без исходного User-Agent; имя задаёт владелец, ip — последний адрес клиента.
DEVICES_37 = """ALTER TABLE sessions ADD COLUMN browser TEXT;
ALTER TABLE sessions ADD COLUMN os TEXT;
ALTER TABLE sessions ADD COLUMN model TEXT;
ALTER TABLE sessions ADD COLUMN label TEXT;
ALTER TABLE sessions ADD COLUMN ip TEXT;
"""

#: ссылки «Поделиться» и хвосты безопасности учётки (chat_shares.py, security_log.py, account.py):
#: - `chat_shares.sealed` — снимок шифром STUDIO_DATA_KEY (security.encrypt). В прежнем столбце `snapshot` новый код
#:   держит заглушку `chat_shares.PLACEHOLDER` (JSON без переписки): прежний код (откат) покажет её вместо снимка.
#:   Снимки, записанные открытым текстом (до шага или прежним кодом после отката), шифрует `chat_shares.seal()` при
#:   старте службы. `expires` — срок ссылки (NULL — бессрочно), `views`/`viewed` — открытия по верному PIN,
#:   `fails` — все неверные PIN ссылки (без окна): `chat_shares.FAILS_MAX` отзывает ссылку;
#: - `security_events.count`/`first` — свёрнутые повторы неудач (security_log.NOISY): одна строка на вид, адрес и
#:   подробность с числом и временем первой; прежний код пишет строки без них (count = 1);
#: - `users.password_reset` — когда пароль сбросил администратор: студия предлагает владельцу аварийный выход.
SHARES_36 = """ALTER TABLE chat_shares ADD COLUMN sealed BLOB;
ALTER TABLE chat_shares ADD COLUMN expires REAL;
ALTER TABLE chat_shares ADD COLUMN views INTEGER NOT NULL DEFAULT 0;
ALTER TABLE chat_shares ADD COLUMN viewed REAL;
ALTER TABLE chat_shares ADD COLUMN fails INTEGER NOT NULL DEFAULT 0;
ALTER TABLE security_events ADD COLUMN count INTEGER NOT NULL DEFAULT 1;
ALTER TABLE security_events ADD COLUMN first REAL;
ALTER TABLE users ADD COLUMN password_reset REAL;
"""

#: сессии учётки и журнал безопасности (account.py, security_log.py): у сессии — когда создана, грубый вид
#: устройства по User-Agent при входе ('phone' | 'computer', строку UA не храним) и последняя отметка
#: активности (seen обнуляет запирание, а список сессий показывает и запертые). Прежний код этих столбцов не
#: пишет — у его сессий NULL. Журнал только добавляется (security_log.record), без текстов; kind без CHECK:
#: новый вид события не требует шага схемы. Уходит с учёткой; «Удалить всю историю» его не трогает
SECURITY_34 = """ALTER TABLE sessions ADD COLUMN created REAL;
ALTER TABLE sessions ADD COLUMN device TEXT;
ALTER TABLE sessions ADD COLUMN active REAL;
CREATE TABLE security_events(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       created REAL NOT NULL, kind TEXT NOT NULL, detail TEXT, device TEXT, session TEXT, ip TEXT);
CREATE INDEX security_events_user ON security_events(user_id, id);
"""

#: причина отказа ответа чата словами (chat._finish): после перечитывания видно, почему ответа нет; прежний код
#: столбец не читает (выбирает по именам или `*` со словарём строк)
CHAT_35 = "ALTER TABLE messages ADD COLUMN error TEXT;"

SHARES_33 ="""CREATE TABLE chat_shares(
       id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       chat_id TEXT REFERENCES chats ON DELETE CASCADE,
       agent_id INTEGER REFERENCES agents ON DELETE CASCADE, project TEXT, session_id TEXT, engine TEXT,
       token_hash TEXT NOT NULL UNIQUE, token_enc BLOB NOT NULL, pin_hash TEXT NOT NULL,
       snapshot TEXT NOT NULL, created REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
       attempt_until REAL NOT NULL DEFAULT 0);
CREATE UNIQUE INDEX chat_shares_chat ON chat_shares(chat_id);
CREATE UNIQUE INDEX chat_shares_agent ON chat_shares(agent_id, project, session_id, engine);
CREATE TABLE chat_share_visitors(
       digest TEXT PRIMARY KEY, share_id TEXT NOT NULL REFERENCES chat_shares ON DELETE CASCADE,
       expires REAL NOT NULL);
CREATE INDEX chat_share_visitors_share ON chat_share_visitors(share_id);
CREATE TABLE chat_share_files(
       share_id TEXT NOT NULL REFERENCES chat_shares ON DELETE CASCADE,
       media_id TEXT NOT NULL REFERENCES media ON DELETE CASCADE,
       PRIMARY KEY(share_id, media_id));
CREATE TABLE chat_share_attempts(ip_hash TEXT PRIMARY KEY, attempts INTEGER NOT NULL, until REAL NOT NULL);
"""

S06_32 = "ALTER TABLE connections ADD COLUMN s06 INTEGER NOT NULL DEFAULT 0;"

# Message ids deliberately remain as tombstones after regeneration removes an answer.
# The owning chat (including history cleanup) removes the acknowledgement itself.
CHAT_31 = """CREATE TABLE chat_turn_requests(
       chat_id TEXT NOT NULL REFERENCES chats ON DELETE CASCADE, request_id TEXT NOT NULL,
       fingerprint TEXT NOT NULL, user_message_id TEXT NOT NULL, assistant_message_id TEXT NOT NULL,
       PRIMARY KEY(chat_id, request_id));"""

NTFY_30 = """CREATE TABLE ntfy_devices(
       digest TEXT PRIMARY KEY REFERENCES sessions ON DELETE CASCADE,
       user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       server TEXT NOT NULL, topic TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0, token_enc BLOB);
CREATE INDEX ntfy_devices_user ON ntfy_devices(user_id);
INSERT INTO ntfy_devices(digest, user_id, server, topic, enabled, token_enc)
SELECT s.digest, s.user_id, n.server, substr(n.topic, 1, 31) || '-' || lower(hex(randomblob(16))), 0, n.token_enc
FROM sessions s JOIN ntfy_settings n ON n.user_id = s.user_id;
UPDATE ntfy_settings SET enabled = 0;"""

NTFY_29 = """CREATE TABLE ntfy_settings(
       user_id INTEGER PRIMARY KEY REFERENCES users ON DELETE CASCADE,
       server TEXT NOT NULL, topic TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0, token_enc BLOB);"""

CODE_28 = """CREATE TABLE code_attention(
       agent_id INTEGER NOT NULL REFERENCES agents ON DELETE CASCADE,
       project TEXT NOT NULL, chat TEXT NOT NULL, revision TEXT NOT NULL, finished REAL NOT NULL,
       seen INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(agent_id, chat));"""

TIMEZONE_27 = "ALTER TABLE users ADD COLUMN timezone TEXT NOT NULL DEFAULT 'Europe/Moscow';"

#: Cleaner contexts survive restarts. Deletion queues retain only an encrypted credential snapshot
#: bound to the original address, so deleting a user or connection cannot strand remote originals.
CLEANER_26 = """
ALTER TABLE connections ADD COLUMN cleaner INTEGER NOT NULL DEFAULT 0;
ALTER TABLE messages ADD COLUMN cleaner_raw TEXT;
CREATE TABLE cleaner_contexts(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, resource TEXT NOT NULL,
       item_id TEXT NOT NULL, connection_id INTEGER NOT NULL, remote_id TEXT NOT NULL,
       base_url TEXT NOT NULL, token_enc BLOB NOT NULL, created REAL NOT NULL,
       UNIQUE(resource, item_id, connection_id));
CREATE TABLE cleaner_deletions(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, remote_id TEXT NOT NULL,
       base_url TEXT NOT NULL, token_enc BLOB NOT NULL, queued REAL NOT NULL, retry_at REAL NOT NULL DEFAULT 0);
CREATE TRIGGER cleaner_context_delete BEFORE DELETE ON cleaner_contexts BEGIN
  INSERT OR IGNORE INTO cleaner_deletions(id, user_id, remote_id, base_url, token_enc, queued)
     VALUES(old.id, old.user_id, old.remote_id, old.base_url, old.token_enc, old.created);
END;
CREATE TRIGGER cleaner_chat_delete AFTER DELETE ON chats BEGIN
  DELETE FROM cleaner_contexts WHERE resource = 'chat' AND item_id = old.id;
END;
CREATE TRIGGER cleaner_translation_delete AFTER DELETE ON translations BEGIN
  DELETE FROM cleaner_contexts WHERE resource = 'translate' AND item_id = old.id;
END;
CREATE TRIGGER cleaner_user_delete AFTER DELETE ON users BEGIN
  DELETE FROM cleaner_contexts WHERE user_id = old.id;
END;
CREATE TRIGGER cleaner_connection_delete AFTER DELETE ON connections BEGIN
  DELETE FROM cleaner_contexts WHERE connection_id = old.id;
END;
"""

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

#: интернет в чате (web.py, спека 2026-10-21-studio-chat-internet, 6.1): кнопка «Интернет» диалога, время
#: подтверждения «расшифровка и Exa» (NULL — не было), ключ Exa учётки шифром security.encrypt (NULL — нет;
#: настройка, а не история), шаги инструментов ответа ассистента JSON (NULL — без инструментов)
INTERNET_14 = """ALTER TABLE chats ADD COLUMN web INTEGER NOT NULL DEFAULT 0;
ALTER TABLE chats ADD COLUMN web_consent REAL;
ALTER TABLE users ADD COLUMN exa_key BLOB;
ALTER TABLE messages ADD COLUMN tools TEXT;
"""

#: кнопка «Рассуждение» (chat.py, спека 2026-10-22-studio-chat-tools-badge-reasoning, 4.2): включена ли у диалога,
#: рассуждение ответа ассистента текстом и его длительность в секундах (NULL — не было или не показывалось)
THINKING_15 = """ALTER TABLE chats ADD COLUMN thinking INTEGER NOT NULL DEFAULT 0;
ALTER TABLE messages ADD COLUMN reasoning TEXT;
ALTER TABLE messages ADD COLUMN reasoning_sec REAL;
"""

#: распознавание (ocr.py, спека 2026-10-27-studio-ocr-design, 6.3 и 6.7): документ — `scans` (state — queued,
#: working, cancelling, done, error, cancelled; origin — раздел или вложение чата, attachment_id без ссылки на
#: attachments: вложение удаляют, документ остаётся; text — весь текст по готовности, для поиска), страницы —
#: `scan_pages`, n — номер страницы исходника с 1 (PDF диапазоном — с первой выбранной; свой user_id —
#: media.referenced). Поиск — триггерами, как у расшифровки; текст скана,
#: который раннер дописал во вложение уже после хода, — в поиск чата `search_attachments_text` (строка — заново:
#: у прямого подключения ход уходит с готовым текстом, и строка уже есть). Каждое
#: подключение srvllm — в разделе ocr с hunyuan-ocr, умолчание — наименьшее (как шаг 4): скан в чате работает
#: сразу после выкладки. У новой базы (SCHEMA) подключений нет — вставки пусты
_SCAN = _TRANSCRIPT                             # имя и начало текста — как у расшифровки
OCR_16 = f"""CREATE TABLE scans(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
      connection_id INTEGER, model TEXT NOT NULL, name TEXT NOT NULL, source_media_id TEXT,
      origin TEXT NOT NULL CHECK (origin IN ('section','chat')), attachment_id TEXT,
      pages INTEGER NOT NULL, done_pages INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL, detail TEXT NOT NULL,
      text TEXT, created REAL NOT NULL, finished REAL);
CREATE TABLE scan_pages(scan_id TEXT NOT NULL REFERENCES scans ON DELETE CASCADE,
      user_id INTEGER NOT NULL, n INTEGER NOT NULL, media_id TEXT,
      state TEXT NOT NULL CHECK (state IN ('waiting','done','failed','layer','blank')),
      text TEXT, detail TEXT, PRIMARY KEY (scan_id, n));
CREATE INDEX scans_user_created ON scans(user_id, created);
CREATE INDEX scans_attachment ON scans(attachment_id);
CREATE INDEX scans_state ON scans(state);
CREATE TRIGGER search_scans_ai AFTER INSERT ON scans BEGIN
  {_DOC} ('ocr', new.id, '', new.user_id, new.model, new.created, {_SCAN}); END;
CREATE TRIGGER search_scans_au AFTER UPDATE OF name, text ON scans
  WHEN old.name IS NOT new.name OR old.text IS NOT new.text BEGIN
  DELETE FROM search_docs WHERE section = 'ocr' AND item = old.id;
  {_DOC} ('ocr', new.id, '', new.user_id, new.model, new.created, {_SCAN}); END;
CREATE TRIGGER search_scans_ad AFTER DELETE ON scans BEGIN
  DELETE FROM search_docs WHERE section = 'ocr' AND item = old.id; END;
CREATE TRIGGER search_attachments_text AFTER UPDATE OF text ON attachments
  WHEN new.message_id IS NOT NULL AND new.kind = 'document' AND old.text IS NOT new.text BEGIN
  DELETE FROM search_docs WHERE part = old.id AND section = 'chat';
  {_CHAT_DOCUMENT} AND new.text IS NOT NULL; END;
INSERT INTO section_links(user_id, section, connection_id, models, created)
SELECT user_id, 'ocr', id, '["hunyuan-ocr"]', created FROM connections WHERE kind = 'srvllm';
INSERT INTO section_defaults(user_id, section, connection_id, model)
SELECT user_id, 'ocr', MIN(connection_id), 'hunyuan-ocr' FROM section_links WHERE section = 'ocr' GROUP BY user_id;
"""

#: PIN блокировки на телефоне (screenlock.py, спека 2026-10-28-studio-lock-pin, 6.1): хеш PIN учётки
#: (security.hash_password; NULL — не задан) и неверные PIN подряд во всех её сессиях
PIN_17 = """ALTER TABLE users ADD COLUMN pin_hash TEXT;
ALTER TABLE users ADD COLUMN pin_fails INTEGER NOT NULL DEFAULT 0;
"""

#: перевод (translate.py, спека 2026-10-28-studio-translator-design, 6.7 и 6.8): запись — `translations` (kind —
#: текст, файл, снимок; name — у текста начало первой строки исходника, у файла и снимка — имя файла; scan_id —
#: документ «Сканов» без ссылки; source — NULL, пока исходник не известен; text — склейка готовых кусков, пишется
#: в конце задания), куски — `translation_parts` с n от 1 (у снимка — блок, box — JSON [x1, y1, x2, y2] в долях
#: 0…1000). Поиск — триггерами: имя, исходник и перевод (каждый — первые INDEX_CHARS). Связи раздела translate
#: ставит код при старте (sections.adopt): список разрешённых адресов в SQL не виден
_TRANSLATION = (f"new.name || ' ' || substr(COALESCE(new.source, ''), 1, {INDEX_CHARS})"
                f" || ' ' || substr(COALESCE(new.text, ''), 1, {INDEX_CHARS})")
TRANSLATE_18 = f"""CREATE TABLE translations(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
      kind TEXT NOT NULL CHECK (kind IN ('text','file','photo')), connection_id INTEGER, model TEXT NOT NULL,
      source_lang TEXT NOT NULL, target_lang TEXT NOT NULL, detected_lang TEXT, result_lang TEXT,
      name TEXT NOT NULL, source_media_id TEXT, scan_id TEXT, source TEXT, text TEXT,
      parts INTEGER NOT NULL DEFAULT 0, done_parts INTEGER NOT NULL DEFAULT 0,
      state TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL, finished REAL);
CREATE TABLE translation_parts(translation_id TEXT NOT NULL REFERENCES translations ON DELETE CASCADE,
      n INTEGER NOT NULL, source TEXT NOT NULL, sep TEXT NOT NULL DEFAULT '', box TEXT, text TEXT,
      state TEXT NOT NULL CHECK (state IN ('waiting','done','failed')), detail TEXT, PRIMARY KEY (translation_id, n));
CREATE INDEX translations_user_created ON translations(user_id, created);
CREATE INDEX translations_state ON translations(state);
CREATE TRIGGER search_translations_ai AFTER INSERT ON translations BEGIN
  {_DOC} ('translate', new.id, '', new.user_id, new.model, new.created, {_TRANSLATION}); END;
CREATE TRIGGER search_translations_au AFTER UPDATE OF name, source, text ON translations
  WHEN old.name IS NOT new.name OR old.source IS NOT new.source OR old.text IS NOT new.text BEGIN
  DELETE FROM search_docs WHERE section = 'translate' AND item = old.id;
  {_DOC} ('translate', new.id, '', new.user_id, new.model, new.created, {_TRANSLATION}); END;
CREATE TRIGGER search_translations_ad AFTER DELETE ON translations BEGIN
  DELETE FROM search_docs WHERE section = 'translate' AND item = old.id; END;
"""

#: Claude Code (agents.py, bridge.py, claude_code.py, push.py; спека 2026-10-28-studio-claude-code-design, 6.1 и 6.8):
#: агент — компьютер с мостом (key_hash — security.digest ключа устройства, сам ключ не хранится; has_pin,
#: projects_root, cc_version — что сказал мост при входе). События подключений — только добавляются (agent_id и
#: agent_name без ссылки: компьютер отзывают, история остаётся), «Удалить всю историю» их не трогает. code_chats —
#: слой студии поверх сессий машины (сессия — на компьютере, здесь только отметка и состояние). Подписка push живёт,
#: пока жива сессия студии, что её завела. id компьютера — AUTOINCREMENT: id отозванного не достаётся новому
#: (им ключуются agent_events без ссылки, память службы и адреса push `code/<id>/…`)
CODE_19 = """CREATE TABLE agents(id INTEGER PRIMARY KEY AUTOINCREMENT,
       user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE, name TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE,
       has_pin INTEGER NOT NULL DEFAULT 0, projects_root TEXT, cc_version TEXT, created REAL NOT NULL, last_seen REAL);
CREATE INDEX agents_user ON agents(user_id);
CREATE TABLE agent_events(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       agent_id INTEGER NOT NULL, agent_name TEXT NOT NULL, created REAL NOT NULL,
       kind TEXT NOT NULL CHECK (kind IN ('enroll','revoke','online','offline','open','pin_ok','pin_fail','session_start')),
       device TEXT, ip TEXT);
CREATE INDEX agent_events_user ON agent_events(user_id, id);
CREATE TABLE code_chats(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       agent_id INTEGER NOT NULL REFERENCES agents ON DELETE CASCADE, project TEXT NOT NULL, session_id TEXT NOT NULL,
       state TEXT NOT NULL CHECK (state IN ('open','closed')), created REAL NOT NULL, updated REAL NOT NULL,
       UNIQUE (agent_id, session_id));
CREATE INDEX code_chats_user ON code_chats(user_id, created);
CREATE TABLE push_subs(endpoint TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       digest TEXT NOT NULL REFERENCES sessions ON DELETE CASCADE, p256dh TEXT NOT NULL, auth TEXT NOT NULL,
       created REAL NOT NULL);
CREATE INDEX push_subs_user ON push_subs(user_id);
CREATE INDEX push_subs_digest ON push_subs(digest);
"""

#: рабочее место раздела Claude Code (workspace.py; спека 2026-10-29-studio-code-workspace-design, 6.8): вкладки
#: каждого проекта (tabs — JSON-список UUID сессий по порядку, active — одна из них; touched — когда проект открывали:
#: сверх предела уходит самый давний) и место учётки (компьютер панели, открытый проект и вид). Только id — ни имён
#: компьютеров и проектов, ни названий сессий. Отзыв компьютера уносит его вкладки, а место и панель с ним пустеют
CODE_20 = """CREATE TABLE code_tabs(user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       agent_id INTEGER NOT NULL REFERENCES agents ON DELETE CASCADE, project TEXT NOT NULL,
       tabs TEXT NOT NULL DEFAULT '[]', active TEXT, touched REAL NOT NULL, PRIMARY KEY (agent_id, project));
CREATE INDEX code_tabs_user ON code_tabs(user_id, touched);
CREATE TABLE code_place(user_id INTEGER PRIMARY KEY REFERENCES users ON DELETE CASCADE,
       panel_id INTEGER REFERENCES agents ON DELETE SET NULL, agent_id INTEGER REFERENCES agents ON DELETE SET NULL,
       project TEXT, view TEXT NOT NULL DEFAULT 'chat' CHECK (view IN ('chat','files','git')));
"""

CODE_21 = """ALTER TABLE code_chats ADD COLUMN engine TEXT NOT NULL DEFAULT 'claude'
CHECK (engine IN ('claude', 'codex'));
"""

CODE_22 = "ALTER TABLE users ADD COLUMN code_defaults TEXT NOT NULL DEFAULT '{}';"

CODE_25 = "ALTER TABLE users ADD COLUMN code_quick_actions TEXT;"

LOCK_24 = """ALTER TABLE users ADD COLUMN screen_lock_sections TEXT NOT NULL DEFAULT '["*"]';
ALTER TABLE users ADD COLUMN screen_lock_timeout INTEGER NOT NULL DEFAULT 60;
ALTER TABLE users ADD COLUMN computer_pin_timeout INTEGER NOT NULL DEFAULT 10800;
"""

#: Расширение CHECK помощников. На code_chats нет входящих FK; идентификаторы, отметки, индекс и
#: каскад удаления сохраняются. Старый код продолжает читать прежние столбцы после отката.
CODE_23 = """CREATE TABLE code_chats_23(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       agent_id INTEGER NOT NULL REFERENCES agents ON DELETE CASCADE, project TEXT NOT NULL, session_id TEXT NOT NULL,
       state TEXT NOT NULL CHECK (state IN ('open','closed')), created REAL NOT NULL, updated REAL NOT NULL,
       engine TEXT NOT NULL DEFAULT 'claude' CHECK (engine IN ('claude', 'codex', 'opencode')),
       UNIQUE (agent_id, session_id));
INSERT INTO code_chats_23 SELECT id, user_id, agent_id, project, session_id, state, created, updated, engine FROM code_chats;
DROP TABLE code_chats;
ALTER TABLE code_chats_23 RENAME TO code_chats;
CREATE INDEX code_chats_user ON code_chats(user_id, created);
"""

SCHEMA = SECTION_TABLES + SPEECH_TABLES + PASSKEY_TABLES + PURGE_TABLES + f"""
CREATE TABLE users(id INTEGER PRIMARY KEY, login TEXT NOT NULL UNIQUE, name TEXT NOT NULL, pw_hash TEXT NOT NULL,
      is_admin INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0,
      theme TEXT NOT NULL DEFAULT '{DEFAULT_THEME}', default_connection_id INTEGER, default_model TEXT,
      created REAL NOT NULL, views TEXT NOT NULL DEFAULT '{{}}', screen_lock INTEGER NOT NULL DEFAULT 0);
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
""" + INDEXES_9 + PASSPORT_10 + EDITS_11 + PHOTO_12 + SEARCH_13 + INTERNET_14 + THINKING_15 + OCR_16 + PIN_17 + TRANSLATE_18 + CODE_19 + CODE_20 + CODE_21 + CODE_22 + CODE_23 + LOCK_24 + CODE_25 + CLEANER_26 + TIMEZONE_27 + CODE_28 + NTFY_29 + NTFY_30 + CHAT_31 + S06_32 + SHARES_33 + SECURITY_34 + CHAT_35 + SHARES_36 + DEVICES_37 + PROJECTS_38 + AGENT_EVENTS_39 + EMAIL_40 + CODE_41

SCHEMA += VOICE_42
SCHEMA += MCP_43
SCHEMA += MEDIA_44

#: Старые озвучки — отдельные чаты; время начала копируется в продолжения и переживает удаление первой записи.
SPEECH_45 = """
ALTER TABLE speeches ADD COLUMN chat_id TEXT;
ALTER TABLE speeches ADD COLUMN chat_created REAL;
ALTER TABLE speeches ADD COLUMN voice_name TEXT NOT NULL DEFAULT '';
UPDATE speeches SET chat_id = id, chat_created = created,
  voice_name = COALESCE((SELECT name FROM voices WHERE voices.id = speeches.voice
                        AND voices.user_id = speeches.user_id), voice);
CREATE INDEX speeches_user_chat ON speeches(user_id, chat_id, created, id);
CREATE INDEX speeches_user_chat_created ON speeches(user_id, chat_created, chat_id);
CREATE TRIGGER speeches_chat_ai AFTER INSERT ON speeches WHEN new.chat_id IS NULL BEGIN
  UPDATE speeches SET chat_id = new.id, chat_created = new.created,
    voice_name = COALESCE((SELECT name FROM voices WHERE id = new.voice AND user_id = new.user_id), new.voice)
    WHERE id = new.id;
END;
CREATE TRIGGER search_speeches_au AFTER UPDATE OF text ON speeches WHEN old.text IS NOT new.text BEGIN
  DELETE FROM search_docs WHERE section = 'speech' AND item = old.id;
  """ + _DOC + """ ('speech', new.id, '', new.user_id, new.model, new.created, new.text);
END;
"""
SCHEMA += SPEECH_45
AGENT_PLATFORM_46 = "ALTER TABLE agents ADD COLUMN platform TEXT CHECK (platform IN ('win32','darwin','linux'));\n"
SCHEMA += AGENT_PLATFORM_46
F5_TTS_47 = """
UPDATE section_links SET models = json_insert(models, '$[#]', 'f5-tts')
WHERE section = 'speech'
  AND connection_id IN (SELECT id FROM connections WHERE kind = 'srvllm')
  AND EXISTS (SELECT 1 FROM json_each(models) WHERE value = 'qwen3-tts')
  AND NOT EXISTS (SELECT 1 FROM json_each(models) WHERE value = 'f5-tts');
"""
SCHEMA += F5_TTS_47
PROJECT_MIRRORS_48 = """CREATE TABLE project_mirrors(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source INTEGER NOT NULL REFERENCES agents ON DELETE CASCADE,
    project TEXT NOT NULL,
    destination INTEGER NOT NULL REFERENCES agents ON DELETE CASCADE,
    root TEXT NOT NULL,
    name_key TEXT NOT NULL,
    location BLOB NOT NULL,
    baseline BLOB,
    synced REAL,
    UNIQUE(destination, root, name_key)
);
CREATE INDEX project_mirrors_source ON project_mirrors(source, project);
"""
SCHEMA += PROJECT_MIRRORS_48

#: id подключения — AUTOINCREMENT, как у `agents`: id удалённого не достаётся новому, и `connection_id` истории
#: (без FK) не начинает указывать на чужой адрес. Пересборка: DROP при foreign_keys=ON каскадом удаляет
#: `section_links`/`section_defaults` — их строки переживают шаг во временных копиях; триггер Cleaner — заново.
CONNECTIONS_49 = """CREATE TABLE connections_49(id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
            name TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('openai','srvllm')),
            base_url TEXT NOT NULL, token_enc BLOB NOT NULL, sections TEXT NOT NULL, created REAL NOT NULL,
            server_only INTEGER NOT NULL DEFAULT 0, passport TEXT, cleaner INTEGER NOT NULL DEFAULT 0,
            s06 INTEGER NOT NULL DEFAULT 0);
INSERT INTO connections_49(id, user_id, name, kind, base_url, token_enc, sections, created, server_only, passport,
                           cleaner, s06)
SELECT id, user_id, name, kind, base_url, token_enc, sections, created, server_only, passport, cleaner, s06
  FROM connections;
CREATE TEMP TABLE links_49 AS SELECT * FROM section_links;
CREATE TEMP TABLE defaults_49 AS SELECT * FROM section_defaults;
DROP TABLE connections;
ALTER TABLE connections_49 RENAME TO connections;
INSERT INTO section_links SELECT * FROM temp.links_49;
INSERT INTO section_defaults SELECT * FROM temp.defaults_49;
DROP TABLE temp.links_49;
DROP TABLE temp.defaults_49;
CREATE TRIGGER cleaner_connection_delete AFTER DELETE ON connections BEGIN
  DELETE FROM cleaner_contexts WHERE connection_id = old.id;
END;
"""
SCHEMA += CONNECTIONS_49

#: модель ролика в поиске — из `videos.model` (шаг 44 добавил столбец, а триггеры шага 13 писали литерал 'wan')
SEARCH_VIDEOS_50 = f"""DROP TRIGGER search_videos_ai;
DROP TRIGGER search_videos_au;
CREATE TRIGGER search_videos_ai AFTER INSERT ON videos BEGIN
  {_DOC} ('video', new.id, '', new.user_id, new.model, new.created, new.prompt); END;
CREATE TRIGGER search_videos_au AFTER UPDATE OF prompt, model ON videos
  WHEN old.prompt IS NOT new.prompt OR old.model IS NOT new.model BEGIN
  DELETE FROM search_docs WHERE section = 'video' AND item = old.id;
  {_DOC} ('video', new.id, '', new.user_id, new.model, new.created, new.prompt); END;
UPDATE search_docs SET model = (SELECT v.model FROM videos v WHERE v.id = search_docs.item)
 WHERE section = 'video' AND EXISTS (SELECT 1 FROM videos v WHERE v.id = search_docs.item);
"""
SCHEMA += SEARCH_VIDEOS_50

#: язык уведомлений устройства — язык интерфейса его страницы (push.py, ntfy.py): прежний код вставляет без столбца,
#: строки до шага и его вставки — 'ru'
NOTIFY_LANG_51 = """ALTER TABLE push_subs ADD COLUMN lang TEXT NOT NULL DEFAULT 'ru' CHECK (lang IN ('ru', 'en'));
ALTER TABLE ntfy_devices ADD COLUMN lang TEXT NOT NULL DEFAULT 'ru' CHECK (lang IN ('ru', 'en'));
"""
SCHEMA += NOTIFY_LANG_51

#: озвучка (speeches.py): время постановки в очередь — очередь и «В работе» по нему, место в чате — по `created`;
#: прежние поля и звук записи на время повторной озвучки — отказ и отмена их возвращают; поиск видит смену модели
SPEECH_52 = f"""ALTER TABLE speeches ADD COLUMN queued REAL;
ALTER TABLE speeches ADD COLUMN previous TEXT;
ALTER TABLE speeches ADD COLUMN previous_media_id TEXT;
DROP TRIGGER search_speeches_au;
CREATE TRIGGER search_speeches_au AFTER UPDATE OF text, model ON speeches
  WHEN old.text IS NOT new.text OR old.model IS NOT new.model BEGIN
  DELETE FROM search_docs WHERE section = 'speech' AND item = old.id;
  {_DOC} ('speech', new.id, '', new.user_id, new.model, new.created, new.text); END;
UPDATE search_docs SET model = (SELECT s.model FROM speeches s WHERE s.id = search_docs.item)
 WHERE section = 'speech' AND EXISTS (SELECT 1 FROM speeches s WHERE s.id = search_docs.item);
"""
SCHEMA += SPEECH_52

#: название чата «Агентов» у ссылки «Поделиться» — шифром STUDIO_DATA_KEY, для списка «Доступы и ссылки» владельца
#: (account.access): название обычного чата берётся из `chats`, а чат «Агентов» в базе студии не хранится.
#: Прежний код столбец не читает и не пишет; ссылки до шага — без названия
SHARES_53 = "ALTER TABLE chat_shares ADD COLUMN label BLOB;\n"
SCHEMA += SHARES_53
#: «непрочитано» «Агентов» — у каждой учётки своё (code_attention.py): просмотренная ревизия завершения по
#: пользователю; общий `code_attention.seen` переходит владельцу и участникам проекта и дальше пишется для отката
CODE_54 = """CREATE TABLE code_attention_seen(
       agent_id INTEGER NOT NULL, chat TEXT NOT NULL,
       user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       revision TEXT NOT NULL, PRIMARY KEY(agent_id, chat, user_id),
       FOREIGN KEY(agent_id, chat) REFERENCES code_attention(agent_id, chat) ON DELETE CASCADE);
INSERT INTO code_attention_seen(agent_id, chat, user_id, revision)
SELECT c.agent_id, c.chat, a.user_id, c.revision FROM code_attention c JOIN agents a ON a.id = c.agent_id
 WHERE c.seen = 1
UNION SELECT c.agent_id, c.chat, m.user_id, c.revision FROM code_attention c
  JOIN project_members m ON m.agent_id = c.agent_id AND m.project = c.project WHERE c.seen = 1;
"""
SCHEMA += CODE_54

# NULL owner denotes a Studio-wide integration. Keep ids and their high-water marks across the rebuild.
SHARED_55 = """CREATE TEMP TABLE integration_sequences_55 AS
  SELECT name, seq FROM sqlite_sequence WHERE name IN ('connections', 'mcp_servers');
CREATE TABLE connections_55(id INTEGER PRIMARY KEY AUTOINCREMENT,
       user_id INTEGER REFERENCES users ON DELETE CASCADE,
       name TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('openai','srvllm')),
       base_url TEXT NOT NULL, token_enc BLOB NOT NULL, sections TEXT NOT NULL, created REAL NOT NULL,
       server_only INTEGER NOT NULL DEFAULT 0, passport TEXT, cleaner INTEGER NOT NULL DEFAULT 0,
       s06 INTEGER NOT NULL DEFAULT 0, CHECK (user_id IS NOT NULL OR server_only = 1));
INSERT INTO connections_55 SELECT * FROM connections;
CREATE TABLE section_links_55(user_id INTEGER REFERENCES users ON DELETE CASCADE, section TEXT NOT NULL,
       connection_id INTEGER NOT NULL REFERENCES connections_55 ON DELETE CASCADE, models TEXT NOT NULL,
       created REAL NOT NULL, PRIMARY KEY (user_id, section, connection_id));
INSERT INTO section_links_55 SELECT * FROM section_links ORDER BY rowid;
CREATE TEMP TABLE defaults_55 AS SELECT * FROM section_defaults;
DROP TABLE section_links;
DROP TABLE connections;
ALTER TABLE connections_55 RENAME TO connections;
ALTER TABLE section_links_55 RENAME TO section_links;
CREATE UNIQUE INDEX section_links_shared ON section_links(section, connection_id) WHERE user_id IS NULL;
INSERT INTO section_defaults SELECT * FROM temp.defaults_55;
DROP TABLE temp.defaults_55;
CREATE TRIGGER cleaner_connection_delete AFTER DELETE ON connections BEGIN
  DELETE FROM cleaner_contexts WHERE connection_id = old.id;
END;
CREATE TABLE mcp_servers_55(id INTEGER PRIMARY KEY AUTOINCREMENT,
       user_id INTEGER REFERENCES users ON DELETE CASCADE,
       name TEXT NOT NULL, url TEXT NOT NULL, secret_enc TEXT NOT NULL,
       enabled INTEGER NOT NULL DEFAULT 1);
INSERT INTO mcp_servers_55 SELECT * FROM mcp_servers;
DROP TABLE mcp_servers;
ALTER TABLE mcp_servers_55 RENAME TO mcp_servers;
CREATE INDEX mcp_servers_user ON mcp_servers(user_id);
DELETE FROM sqlite_sequence WHERE name IN ('connections', 'mcp_servers');
INSERT INTO sqlite_sequence SELECT * FROM temp.integration_sequences_55;
DROP TABLE temp.integration_sequences_55;
"""
SCHEMA += SHARED_55

#: группы доступа к общему (access.py): правило на общее подключение или MCP (`tool = ''`) и на инструмент MCP;
#: правило без групп — только администраторам. Правила уходят вместе с подключением или сервером (id не повторяются).
#: Прежний код таблицы не читает: закрытое становится общим для всех — перед откатом `access.ROLLBACK_56` прячет
#: закрытое в `access_rollback` (в обычной работе пуста), после возврата вперёд `access.FORWARD_56` возвращает
#: `name_key` — `name.strip().casefold()`: NOCASE SQLite складывает только латиницу, «Медиа» и «медиа» — нет.
ACCESS_56 = """CREATE TABLE access_groups(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
       name_key TEXT NOT NULL UNIQUE, created REAL NOT NULL);
CREATE TABLE access_group_members(group_id INTEGER NOT NULL REFERENCES access_groups ON DELETE CASCADE,
       user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE, PRIMARY KEY (group_id, user_id));
CREATE INDEX access_group_members_user ON access_group_members(user_id);
CREATE TABLE access_rules(id INTEGER PRIMARY KEY AUTOINCREMENT,
       kind TEXT NOT NULL CHECK (kind IN ('connection', 'mcp')), item_id INTEGER NOT NULL,
       tool TEXT NOT NULL DEFAULT '', CHECK (kind = 'mcp' OR tool = ''), UNIQUE (kind, item_id, tool));
CREATE TABLE access_rule_groups(rule_id INTEGER NOT NULL REFERENCES access_rules ON DELETE CASCADE,
       group_id INTEGER NOT NULL REFERENCES access_groups ON DELETE CASCADE, PRIMARY KEY (rule_id, group_id));
CREATE TRIGGER access_rules_connection_delete AFTER DELETE ON connections BEGIN
  DELETE FROM access_rules WHERE kind = 'connection' AND item_id = old.id;
END;
CREATE TRIGGER access_rules_mcp_delete AFTER DELETE ON mcp_servers BEGIN
  DELETE FROM access_rules WHERE kind = 'mcp' AND item_id = old.id;
END;
CREATE TABLE access_rollback(kind TEXT NOT NULL CHECK (kind IN ('connection', 'mcp')), item_id INTEGER NOT NULL,
       token_enc BLOB, enabled INTEGER, PRIMARY KEY (kind, item_id));
"""
SCHEMA += ACCESS_56

#: возможности источников: профиль подключения (системный текст, параметры, подсказки), роли диалога (умолчания
#: администратора и библиотека учётки), состояние MCP диалога (выключенные общие и инструменты, согласие на передачу
#: файлов `{sid: stamp}`), загрузки вложений и медиа диалога (`chat_media`: звук и видео) на MCP (`stamp` —
#: `security.digest(secret_enc)`), политика инструментов общего MCP (`open_world`, `confirm`; NULL — как у
#: сервера). К `connections` и `mcp_servers` — только триггеры: таблицы
#: пересобирались на шаге 55. Прежний код новые таблицы и столбцы не читает; его вставки получают умолчания.
CAPABILITIES_57 = """CREATE TABLE connection_profiles(connection_id INTEGER PRIMARY KEY,
       system_text TEXT NOT NULL DEFAULT '', params TEXT NOT NULL DEFAULT '{}',
       suggestions TEXT NOT NULL DEFAULT '[]', updated REAL NOT NULL);
CREATE TRIGGER connection_profiles_delete AFTER DELETE ON connections BEGIN
  DELETE FROM connection_profiles WHERE connection_id = old.id;
END;
CREATE TABLE role_defaults(id INTEGER PRIMARY KEY AUTOINCREMENT,
       lang TEXT NOT NULL DEFAULT 'ru' CHECK (lang IN ('ru','en')), name TEXT NOT NULL, text TEXT NOT NULL,
       position INTEGER NOT NULL, builtin TEXT, created REAL, updated REAL);
CREATE TABLE chat_roles(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       name TEXT NOT NULL, text TEXT NOT NULL, position INTEGER NOT NULL, origin INTEGER, created REAL, updated REAL);
CREATE INDEX chat_roles_user ON chat_roles(user_id, position);
ALTER TABLE users ADD COLUMN roles_seeded REAL;
ALTER TABLE chats ADD COLUMN role_id TEXT;
ALTER TABLE chats ADD COLUMN mcp_off TEXT NOT NULL DEFAULT '[]';
ALTER TABLE chats ADD COLUMN mcp_tools_off TEXT NOT NULL DEFAULT '{}';
CREATE TRIGGER chat_roles_delete AFTER DELETE ON chat_roles BEGIN
  UPDATE chats SET role_id = NULL WHERE role_id = old.id;
END;
CREATE TABLE mcp_uploads(attachment_id TEXT NOT NULL REFERENCES attachments ON DELETE CASCADE,
       server_id INTEGER NOT NULL, artifact_id TEXT NOT NULL, path TEXT NOT NULL, stamp TEXT NOT NULL,
       created REAL NOT NULL, PRIMARY KEY (attachment_id, server_id));
CREATE TRIGGER mcp_uploads_server_delete AFTER DELETE ON mcp_servers BEGIN
  DELETE FROM mcp_uploads WHERE server_id = old.id;
END;
ALTER TABLE chats ADD COLUMN mcp_file_consent TEXT NOT NULL DEFAULT '{}';
CREATE TABLE mcp_tool_policy(server_id INTEGER NOT NULL, tool TEXT NOT NULL,
       open_world INTEGER CHECK (open_world IN (0,1)), confirm TEXT, PRIMARY KEY (server_id, tool));
CREATE TRIGGER mcp_tool_policy_server_delete AFTER DELETE ON mcp_servers BEGIN
  DELETE FROM mcp_tool_policy WHERE server_id = old.id;
END;
CREATE TABLE chat_media(id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chats ON DELETE CASCADE,
       user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE, name TEXT NOT NULL, mime TEXT NOT NULL,
       size INTEGER NOT NULL, media_id TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX chat_media_chat ON chat_media(chat_id);
CREATE TABLE mcp_media_uploads(chat_media_id TEXT NOT NULL REFERENCES chat_media ON DELETE CASCADE,
       server_id INTEGER NOT NULL, artifact_id TEXT NOT NULL, path TEXT NOT NULL, stamp TEXT NOT NULL,
       created REAL NOT NULL, PRIMARY KEY (chat_media_id, server_id));
CREATE TRIGGER mcp_media_uploads_server_delete AFTER DELETE ON mcp_servers BEGIN
  DELETE FROM mcp_media_uploads WHERE server_id = old.id;
END;
"""
SCHEMA += CAPABILITIES_57

#: таблицы с корзиной (`deleted` — время переноса, NULL — запись на месте); список — `trash.TABLES`
_TRASH_58 = ("chats", "images", "videos", "speeches", "transcripts", "scans", "translations", "voices",
             "chat_media", "knowledge_bases", "kb_documents")
#: раздел поиска записи корзины: запись в корзине уходит из поиска (у чата — с сообщениями и вложениями: их `item` —
#: id чата)
_TRASH_SEARCH_58 = {"chats": "chat", "images": "photo", "videos": "video", "speeches": "speech",
                    "transcripts": "transcribe", "scans": "ocr", "translations": "translate"}
#: ждущий ответ хода (`chat_pending`: подтверждение инструмента или вопрос модели; `view` — вид карточки отдельным
#: шифром, выдача сообщений не расшифровывает payload) и список дел ответа
#: (`messages.todo`, JSON); память учётки (`user_memory.text_enc` — шифром) и её выключатель; корзина;
#: личные ссылки на файлы MCP (`file_links`: хеш токена, путь и имя — шифром, `stamp` — подпись секрета сервера,
#: размер и mtime файла при создании — ссылка ведёт к файлу, а не к пути);
#: «защищённый источник» MCP и отметка диалога о его срабатывании; базы знаний (`user_id NULL` — общая, закрытая
#: группами `kb_rules`), документы базы или большого вложения диалога, куски с FTS5 и вектором; язык расшифровки и
#: озвучки (NULL — авто, как раньше). FTS кусков — с внешним содержимым и заменой «ё» на «е», как индекс поиска
#: (`YO`): запрос к `kb_fts` сворачивается так же. К `mcp_servers` — только столбец: таблица пересобиралась на шаге 55.
#: Прежний код новые таблицы и столбцы не читает, его вставки получают умолчания; записи корзины он показывает как
#: обычные — перед откатом `trash.ROLLBACK_58`.
REMAINING_58 = f"""CREATE TABLE chat_pending(id TEXT PRIMARY KEY,
       chat_id TEXT NOT NULL REFERENCES chats ON DELETE CASCADE,
       message_id TEXT NOT NULL REFERENCES messages ON DELETE CASCADE,
       kind TEXT NOT NULL CHECK (kind IN ('confirm','ask')), payload TEXT NOT NULL, view TEXT NOT NULL DEFAULT '',
       created REAL NOT NULL,
       resolved REAL, outcome TEXT CHECK (outcome IN ('run','refuse','answer','superseded','expired')));
CREATE INDEX chat_pending_open ON chat_pending(chat_id) WHERE resolved IS NULL;
ALTER TABLE messages ADD COLUMN todo TEXT;
ALTER TABLE users ADD COLUMN memory_on INTEGER NOT NULL DEFAULT 0;
CREATE TABLE user_memory(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       text_enc TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX user_memory_user ON user_memory(user_id);
CREATE TABLE file_links(id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
       user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       server_id INTEGER NOT NULL REFERENCES mcp_servers ON DELETE CASCADE, stamp TEXT NOT NULL,
       path_enc TEXT NOT NULL, name_enc TEXT NOT NULL, size INTEGER, mtime REAL, created REAL NOT NULL,
       expires REAL NOT NULL, revoked REAL);
CREATE INDEX file_links_user ON file_links(user_id);
ALTER TABLE mcp_servers ADD COLUMN protected INTEGER NOT NULL DEFAULT 0;
ALTER TABLE chats ADD COLUMN protected_used INTEGER NOT NULL DEFAULT 0;
CREATE TABLE knowledge_bases(id TEXT PRIMARY KEY, user_id INTEGER REFERENCES users ON DELETE CASCADE,
       name TEXT NOT NULL, created REAL NOT NULL, deleted REAL);
CREATE TABLE kb_documents(id TEXT PRIMARY KEY, kb_id TEXT REFERENCES knowledge_bases ON DELETE CASCADE,
       attachment_id TEXT REFERENCES attachments ON DELETE CASCADE,
       user_id INTEGER REFERENCES users ON DELETE CASCADE, name TEXT NOT NULL, chars INTEGER NOT NULL DEFAULT 0,
       state TEXT NOT NULL CHECK (state IN ('queued','ready','error')), created REAL NOT NULL, deleted REAL,
       CHECK ((kb_id IS NULL) <> (attachment_id IS NULL)));
CREATE INDEX kb_documents_kb ON kb_documents(kb_id);
CREATE INDEX kb_documents_attachment ON kb_documents(attachment_id);
CREATE TABLE kb_chunks(id INTEGER PRIMARY KEY, doc_id TEXT NOT NULL REFERENCES kb_documents ON DELETE CASCADE,
       n INTEGER NOT NULL, text TEXT NOT NULL, vector BLOB);
CREATE INDEX kb_chunks_doc ON kb_chunks(doc_id, n);
CREATE VIRTUAL TABLE kb_fts USING fts5(text, content='kb_chunks', content_rowid='id',
       tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER kb_chunks_ai AFTER INSERT ON kb_chunks BEGIN
  INSERT INTO kb_fts(rowid, text) VALUES (new.id, {YO.format('new.text')}); END;
CREATE TRIGGER kb_chunks_ad AFTER DELETE ON kb_chunks BEGIN
  INSERT INTO kb_fts(kb_fts, rowid, text) VALUES ('delete', old.id, {YO.format('old.text')}); END;
CREATE TABLE kb_rules(kb_id TEXT NOT NULL REFERENCES knowledge_bases ON DELETE CASCADE,
       group_id INTEGER NOT NULL REFERENCES access_groups ON DELETE CASCADE, PRIMARY KEY (kb_id, group_id));
CREATE TABLE chat_kbs(chat_id TEXT NOT NULL REFERENCES chats ON DELETE CASCADE,
       kb_id TEXT NOT NULL REFERENCES knowledge_bases ON DELETE CASCADE, PRIMARY KEY (chat_id, kb_id));
ALTER TABLE transcripts ADD COLUMN language TEXT;
ALTER TABLE speeches ADD COLUMN language TEXT;
""" + "".join(
    (f"ALTER TABLE {t} ADD COLUMN deleted REAL;\n" if t not in ("knowledge_bases", "kb_documents") else "")
    + f"CREATE INDEX {t}_trash ON {t}(user_id, deleted) WHERE deleted IS NOT NULL;\n" for t in _TRASH_58) + "".join(
    f"CREATE TRIGGER search_{t}_hide AFTER UPDATE OF deleted ON {t} WHEN NEW.deleted IS NOT NULL BEGIN\n"
    f"  DELETE FROM search_docs WHERE section = '{s}' AND item = old.id; END;\n" for t, s in _TRASH_SEARCH_58.items())
SCHEMA += REMAINING_58

CODE_PINS_59 = "ALTER TABLE code_place ADD COLUMN pinned_projects TEXT NOT NULL DEFAULT '[]';"
SCHEMA += CODE_PINS_59

TRANSCRIPT_PLAYBACK_60 = "ALTER TABLE transcripts ADD COLUMN listen_media_id TEXT;"
SCHEMA += TRANSCRIPT_PLAYBACK_60

#: корзина отдельных образцов голоса (trash.py): отметка — своей таблицей, не столбцом `voice_samples`: прежний код
#: вставляет образец без списка столбцов, и лишний столбец уронил бы его вставку. Вид `voice_sample_items` — образец
#: с `deleted`, как строка таблицы корзины; «Вернуть» (UPDATE вида `deleted = NULL`) снимает отметку триггером.
#: Образец, удалённый прежним кодом, уносит отметку каскадом; прежний код отметок не читает — перед откатом
#: `trash.ROLLBACK_61`.
VOICE_TRASH_61 = """CREATE TABLE voice_sample_trash(id TEXT PRIMARY KEY REFERENCES voice_samples ON DELETE CASCADE,
       deleted REAL NOT NULL);
CREATE INDEX voice_sample_trash_deleted ON voice_sample_trash(deleted);
CREATE VIEW voice_sample_items AS SELECT s.id, s.voice_id, s.user_id, s.ref_text, s.media_id, s.duration, s.created,
       t.deleted FROM voice_samples s LEFT JOIN voice_sample_trash t ON t.id = s.id;
CREATE TRIGGER voice_sample_items_restore INSTEAD OF UPDATE OF deleted ON voice_sample_items
       WHEN NEW.deleted IS NULL BEGIN
  DELETE FROM voice_sample_trash WHERE id = OLD.id;
END;
"""
SCHEMA += VOICE_TRASH_61

#: общий MCP «В диалоге по умолчанию» (mcp_sources.dialog_servers): 0 — в диалог только вручную; прежний код столбец
#: не читает — такие серверы снова действуют во всех диалогах
MCP_DEFAULT_62 = "ALTER TABLE mcp_servers ADD COLUMN dialog_default INTEGER NOT NULL DEFAULT 1;"
SCHEMA += MCP_DEFAULT_62

#: зеркала группой равноправных копий (mirror_groups.py): группа — исключения шифром, копия — папка компьютера
#: (`project` — id проекта, `root` + `name_key` + `location` шифром — адрес; неизвестное — NULL; `held` шифром —
#: накопленные записи копии на её синхронизациях), пара — состояния двух копий на их последнюю синхронизацию
#: (шифром). Строки `project_mirrors` переносит `mirror_groups.adopt` при
#: старте, перенесённые — в `mirror_legacy`; сам `project_mirrors` не меняется. Прежний код новые таблицы не читает:
#: видит свои односторонние зеркала с прежними базами, его новые строки `adopt` перенесёт после возврата вперёд
MIRROR_GROUPS_63 = """CREATE TABLE mirror_groups(id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE, exclude BLOB);
CREATE TABLE mirror_copies(id INTEGER PRIMARY KEY AUTOINCREMENT,
    grp INTEGER NOT NULL REFERENCES mirror_groups ON DELETE CASCADE,
    agent INTEGER NOT NULL REFERENCES agents ON DELETE CASCADE,
    project TEXT, root TEXT, name_key TEXT, location BLOB, held BLOB, UNIQUE(agent, root, name_key));
CREATE INDEX mirror_copies_project ON mirror_copies(agent, project);
CREATE INDEX mirror_copies_grp ON mirror_copies(grp);
CREATE TABLE mirror_pairs(a INTEGER NOT NULL REFERENCES mirror_copies ON DELETE CASCADE,
    b INTEGER NOT NULL REFERENCES mirror_copies ON DELETE CASCADE,
    state_a BLOB, state_b BLOB, synced REAL, PRIMARY KEY(a, b), CHECK(a < b));
CREATE INDEX mirror_pairs_b ON mirror_pairs(b);
CREATE TABLE mirror_legacy(id INTEGER PRIMARY KEY);
"""
SCHEMA += MIRROR_GROUPS_63

#: четвёртый помощник — DeepSeek Harness: CHECK `engine` шире, как CODE_23. На code_chats нет входящих FK, триггеров
#: и видов; id, все столбцы (и `origin` шага 41), индекс и каскады сохраняются. Прежний код пишет свои движки
CODE_64 = """CREATE TABLE code_chats_64(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       agent_id INTEGER NOT NULL REFERENCES agents ON DELETE CASCADE, project TEXT NOT NULL, session_id TEXT NOT NULL,
       state TEXT NOT NULL CHECK (state IN ('open','closed')), created REAL NOT NULL, updated REAL NOT NULL,
       engine TEXT NOT NULL DEFAULT 'claude' CHECK (engine IN ('claude', 'codex', 'opencode', 'deepseek')),
       origin TEXT NOT NULL DEFAULT 'other' CHECK (origin IN ('studio', 'other')),
       UNIQUE (agent_id, session_id));
INSERT INTO code_chats_64(id, user_id, agent_id, project, session_id, state, created, updated, engine, origin)
SELECT id, user_id, agent_id, project, session_id, state, created, updated, engine, origin FROM code_chats;
DROP TABLE code_chats;
ALTER TABLE code_chats_64 RENAME TO code_chats;
CREATE INDEX code_chats_user ON code_chats(user_id, created);
"""
SCHEMA += CODE_64

#: учётки Open WebUI внутри учётки Студии (owui.py): кто подключён (`who_enc`) и постоянная сессия (`secret_enc`) —
#: шифром `STUDIO_DATA_KEY`; пароль не хранится нигде. Прежний код таблицу не читает; учётка уходит с пользователем
OWUI_65 = """CREATE TABLE owui_accounts(id INTEGER PRIMARY KEY AUTOINCREMENT,
       user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
       label TEXT NOT NULL, base_url TEXT NOT NULL, who_enc BLOB NOT NULL, secret_enc BLOB NOT NULL, cert_pem TEXT,
       state TEXT NOT NULL DEFAULT 'ok' CHECK (state IN ('ok', 'relogin')), version TEXT,
       created REAL NOT NULL, used REAL);
CREATE INDEX owui_accounts_user ON owui_accounts(user_id);
"""
SCHEMA += OWUI_65

#: внешние модели через шлюз Cleaner (cleaner_route.py): `connections.via_cleaner` — переключатель «Через Cleaner»,
#: `messages.cleaner_request` — «<id чата Cleaner>/<id запроса>» последнего запроса хода («Что получила модель»).
#: Прежний код столбцов не знает и отправил бы ход такого подключения провайдеру напрямую — перед откатом
#: `cleaner_route.ROLLBACK_66` прячет ключи этих подключений в `cleaner_rollback` (в обычной работе пуста), после
#: возврата вперёд `cleaner_route.FORWARD_66` возвращает.
#: Контекст нового вида (чат Cleaner диалога через шлюз, служебный ключ — заголовком `X-Cleaner-Key`) отмечен строкой
#: `cleaner_route_contexts` — своей таблицей: прежний код вставляет в `cleaner_contexts` без списка столбцов.
#: Триггер удаления переносит отметку в `cleaner_deletions.route`, по ней очередь шлёт ключ нужным заголовком.
CLEANER_ROUTE_66 = """ALTER TABLE connections ADD COLUMN via_cleaner INTEGER NOT NULL DEFAULT 0;
ALTER TABLE messages ADD COLUMN cleaner_request TEXT;
CREATE TABLE cleaner_rollback(connection_id INTEGER PRIMARY KEY, token_enc BLOB NOT NULL);
CREATE TABLE cleaner_route_contexts(id TEXT PRIMARY KEY);
ALTER TABLE cleaner_deletions ADD COLUMN route INTEGER NOT NULL DEFAULT 0;
DROP TRIGGER IF EXISTS cleaner_context_delete;
CREATE TRIGGER cleaner_context_delete BEFORE DELETE ON cleaner_contexts BEGIN
  INSERT OR IGNORE INTO cleaner_deletions(id, user_id, remote_id, base_url, token_enc, queued, route)
     VALUES(old.id, old.user_id, old.remote_id, old.base_url, old.token_enc, old.created,
            EXISTS(SELECT 1 FROM cleaner_route_contexts WHERE id = old.id));
  DELETE FROM cleaner_route_contexts WHERE id = old.id;
END;
"""
SCHEMA += CLEANER_ROUTE_66

#: Признак сохраняется вместе с переводом, даже если контекст Cleaner или подключение удалят.
TRANSLATE_CLEANER_67 = """ALTER TABLE translations ADD COLUMN cleaner_used INTEGER;
UPDATE translations SET cleaner_used = 1 WHERE EXISTS (
    SELECT 1 FROM cleaner_contexts c WHERE c.resource = 'translate' AND c.item_id = translations.id
    AND c.user_id = translations.user_id);
"""
SCHEMA += TRANSLATE_CLEANER_67

#: Загруженные Студией файлы до отправки. Обычные файлы и вложения отправленного сообщения удалить через Студию нельзя.
OWUI_DRAFTS_68 = """CREATE TABLE owui_file_drafts(
    account_id INTEGER NOT NULL REFERENCES owui_accounts(id) ON DELETE CASCADE,
    file_id TEXT NOT NULL, PRIMARY KEY(account_id, file_id));
"""
SCHEMA += OWUI_DRAFTS_68

#: ссылка «Поделиться» на запись другого раздела (chat_shares.ITEMS): таблица и id записи. Окончательное удаление
#: записи уносит ссылку триггером (и прежним кодом); корзину и защиту учитывает `chat_shares.LIVE`. Снимок такой
#: ссылки несёт `messages` заглушки `chat_shares.PLACEHOLDER`: прежний код (откат) покажет её вместо записи
SHARES_69 = "ALTER TABLE chat_shares ADD COLUMN item_kind TEXT;\nALTER TABLE chat_shares ADD COLUMN item_id TEXT;\n" \
    "CREATE UNIQUE INDEX chat_shares_item ON chat_shares(item_kind, item_id);\n" + "".join(
        f"CREATE TRIGGER chat_shares_{table}_gone AFTER DELETE ON {table} BEGIN\n"
        f"  DELETE FROM chat_shares WHERE item_kind = '{table}' AND item_id = old.id; END;\n"
        for table in ("transcripts", "translations", "scans", "images", "videos"))
SCHEMA += SHARES_69
#: второй фактор приложением-аутентификатором (totp.py): секрет — шифром `STUDIO_DATA_KEY`, `totp_step` — последний
#: принятый шаг 30 с (тот же код второй раз не принимается), резервные коды — sha256, каждый одноразовый;
#: `users.login_notice` — письмо о входе с нового устройства (auth.finish_login). Прежний код столбцов и таблицы не
#: читает: вход без второго фактора приложения (коды из письма — как были), письма о новом входе нет
TOTP_70 = """ALTER TABLE users ADD COLUMN totp_secret BLOB;
ALTER TABLE users ADD COLUMN totp_step INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN login_notice INTEGER NOT NULL DEFAULT 1;
CREATE TABLE totp_backup(user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE, digest TEXT NOT NULL,
       PRIMARY KEY(user_id, digest));
"""
SCHEMA += TOTP_70
#: Варианты ответа чата: «Ответить заново» не удаляет прежний ответ — он остаётся невыбранным (`unchosen` = 1);
#: варианты — ответы ассистента после одного сообщения пользователя, выбран один. Невыбранные бывают только у
#: последнего ответа диалога: новый ход их удаляет (chat.py). Прежний код столбца не знает и видит их подряд.
VARIANTS_71 = """ALTER TABLE messages ADD COLUMN unchosen INTEGER NOT NULL DEFAULT 0;
CREATE INDEX messages_unchosen ON messages(chat_id) WHERE unchosen = 1;
"""
SCHEMA += VARIANTS_71

#: правка распознанного текста страницы «Сканов» (ocr.edit_page): `text` — правленый (его берут выгрузки, «В чат»,
#: перевод, ссылка «Поделиться» и поиск), `recognized` — распознанный до первой правки, NULL — страницу не правили.
#: Прежний код столбец не читает: правленый текст для него — распознанный
SCAN_EDITS_72 = "ALTER TABLE scan_pages ADD COLUMN recognized TEXT;\n"
SCHEMA += SCAN_EDITS_72

#: глоссарий и тон перевода учётки (translate.preferences): пары «термин → перевод» по порядку `n`, тон — ключ
#: `translate_text.TONES` или пусто. Прежний код их не читает: перевод без глоссария и тона
GLOSSARY_73 = """CREATE TABLE translate_glossary(user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       n INTEGER NOT NULL, term TEXT NOT NULL, translation TEXT NOT NULL, PRIMARY KEY(user_id, n));
ALTER TABLE users ADD COLUMN translate_tone TEXT NOT NULL DEFAULT '';
"""
SCHEMA += GLOSSARY_73

#: «PDF со слоем» скана (ocr.find_lines, ocr_pdf): `scan_pages.boxes` — строки страницы с рамками по ответу
#: `translate_spot.PROMPT` (JSON `[[текст, x1, y1, x2, y2], …]`, доли 0…1000 картинки страницы; `[]` — строк с
#: координатами нет), NULL — не искали; `scans.layer_prior` — документ снова в очереди ради рамок строк, JSON его
#: прежнего итога `{state, detail, finished}` (вернётся после поиска); NULL — обычный документ. Прежний код столбцы
#: не читает: ждущий поиск строк для него — распознавание, которому нечего распознавать, идущий — «прервано»
SCAN_LAYER_74 = "ALTER TABLE scan_pages ADD COLUMN boxes TEXT;\nALTER TABLE scans ADD COLUMN layer_prior TEXT;\n"
SCHEMA += SCAN_LAYER_74
#: о чём уведомлять устройство (push.muted, push.reminders): у сессии — снятые виды (`off`, JSON-список из
#: `push.KINDS`), тихие часы (минуты от полуночи по поясу учётки, оба NULL — без них) и интервал напоминания о ждущей
#: карточке «Агентов», мин (0 — не напоминать). Одна строка на оба канала (push и ntfy) этой сессии; прежний код
#: таблицу не читает — шлёт все виды и в любое время, строки уходят каскадом с сессией
NOTIFY_PREFS_75 = """CREATE TABLE notify_prefs(
       digest TEXT PRIMARY KEY REFERENCES sessions ON DELETE CASCADE,
       user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       off TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(off)),
       quiet_from INTEGER CHECK (quiet_from BETWEEN 0 AND 1439), quiet_to INTEGER CHECK (quiet_to BETWEEN 0 AND 1439),
       remind INTEGER NOT NULL DEFAULT 30);
CREATE INDEX notify_prefs_user ON notify_prefs(user_id);
"""
SCHEMA += NOTIFY_PREFS_75
#: роль и срок выдачи доступа к проекту (project_access): `viewer` — только читающие просьбы, `expires` — время
#: истечения или NULL (бессрочно). Прежний код столбцов не читает: наблюдатель — снова соавтор, срок не действует
PROJECT_ROLES_76 = """ALTER TABLE project_members ADD COLUMN role TEXT NOT NULL DEFAULT 'editor'
       CHECK(role IN ('editor', 'viewer'));
ALTER TABLE project_members ADD COLUMN expires REAL;
"""
SCHEMA += PROJECT_ROLES_76
#: «Синхронизировать после хода» копии зеркала (project_mirrors.after_turn): после успешного хода в проекте этой
#: копии сервер сам сравнивает её с остальными копиями группы и отправляет изменения без конфликтов и удалений.
#: Прежний код столбец не читает: после ходов не синхронизирует, флажок сохраняется до возврата вперёд
MIRROR_AUTO_77 = "ALTER TABLE mirror_copies ADD COLUMN auto INTEGER NOT NULL DEFAULT 0;\n"
SCHEMA += MIRROR_AUTO_77
#: ссылку приглашения показал администратор (email_accounts.show_link), а не отправило письмо: принятие пишет в
#: журнал новой учётки «почту вписал администратор». Прежний код столбец не читает: ссылка действует, отметки нет
INVITE_LINK_78 = "ALTER TABLE invitations ADD COLUMN manual INTEGER NOT NULL DEFAULT 0;\n"
SCHEMA += INVITE_LINK_78

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
    # вид ленты разделов (account_profile.py) — в учётке: JSON {раздел: вид}
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
    # интернет в чате: кнопка и подтверждение диалога, ключ Exa учётки, шаги инструментов ответа
    14: INTERNET_14,
    # кнопка «Рассуждение» диалога, рассуждение ответа и его длительность
    15: THINKING_15,
    # распознавание: документы и страницы, поиск по ним; srvllm — в разделе ocr
    16: OCR_16,
    # PIN блокировки на телефоне: хеш PIN учётки и счёт неверных
    17: PIN_17,
    # перевод: записи и куски, поиск по ним (связи раздела translate — sections.adopt при старте)
    18: TRANSLATE_18,
    # Claude Code: компьютеры с мостом, история подключений, чаты студии, подписки push
    19: CODE_19,
    # Claude Code: рабочее место учётки — вкладки проектов, компьютер панели, открытый проект и вид
    20: CODE_20,
    21: CODE_21,
    22: CODE_22,
    23: CODE_23,
    24: LOCK_24,
    25: CODE_25,
    26: CLEANER_26,
    27: TIMEZONE_27,
    28: CODE_28,
    29: NTFY_29,
    30: NTFY_30,
    31: CHAT_31,
    32: S06_32,
    33: SHARES_33,
    # сессии учётки (создана, вид устройства, активность) и журнал безопасности
    34: SECURITY_34,
    # причина отказа ответа чата
    35: CHAT_35,
    # ссылки «Поделиться»: снимок шифром, срок, счётчики; свёрнутые неудачи журнала; сброс пароля администратором
    36: SHARES_36,
    37: DEVICES_37,
    38: PROJECTS_38,
    # история подключений: выдача и отзыв доступа к проекту
    39: AGENT_EVENTS_39,
    40: EMAIL_40,
    41: CODE_41,
    42: VOICE_42,
    43: MCP_43,
    44: MEDIA_44,
    45: SPEECH_45,
    46: AGENT_PLATFORM_46,
    47: F5_TTS_47,
    48: PROJECT_MIRRORS_48,
    # id подключения не переходит к новому после удаления
    49: CONNECTIONS_49,
    # модель ролика в поиске — из videos.model
    50: SEARCH_VIDEOS_50,
    # язык уведомлений push и ntfy устройства
    51: NOTIFY_LANG_51,
    # озвучка: время постановки в очередь, прежний звук на время повторной озвучки, модель в поиске
    52: SPEECH_52,
    # название чата «Агентов» у ссылки «Поделиться» (шифром)
    53: SHARES_53,
    # «непрочитано» «Агентов» — у каждой учётки своё
    54: CODE_54,
    55: SHARED_55,
    # группы доступа к общим API моделей, MCP и инструментам MCP
    56: ACCESS_56,
    # профили подключений, роли диалога, состояние MCP диалога, загрузки вложений на MCP
    57: CAPABILITIES_57,
    # ждущие ответы и список дел, корзина, ссылки на файлы, защищённые MCP, память, базы знаний, язык речи
    58: REMAINING_58,
    59: CODE_PINS_59,
    60: TRANSCRIPT_PLAYBACK_60,
    # корзина отдельных образцов голоса
    61: VOICE_TRASH_61,
    # общий MCP: в диалоге по умолчанию или только вручную
    62: MCP_DEFAULT_62,
    # зеркала группой копий: группы, копии, пары, перенесённые строки project_mirrors
    63: MIRROR_GROUPS_63,
    # четвёртый помощник «Агентов» — DeepSeek Harness
    64: CODE_64,
    # учётки Open WebUI
    65: OWUI_65,
    # внешние модели через шлюз Cleaner
    66: CLEANER_ROUTE_66,
    67: TRANSLATE_CLEANER_67,
    68: OWUI_DRAFTS_68,
    # ссылка «Поделиться» на расшифровку, перевод, скан, картинку и ролик
    69: SHARES_69,
    # приложение-аутентификатор и резервные коды; письмо о входе с нового устройства
    70: TOTP_70,
    # варианты ответа чата: невыбранный вариант последнего ответа
    71: VARIANTS_71,
    # правленый текст страницы скана и распознанный рядом
    72: SCAN_EDITS_72,
    # глоссарий и тон перевода учётки
    73: GLOSSARY_73,
    # рамки строк страниц скана для «PDF со слоем»
    74: SCAN_LAYER_74,
    # о чём уведомлять устройство: виды, тихие часы, напоминание о ждущей карточке
    75: NOTIFY_PREFS_75,
    # роль «Наблюдатель» и срок выдачи доступа к проекту
    76: PROJECT_ROLES_76,
    # «синхронизировать после хода» у копии зеркала
    77: MIRROR_AUTO_77,
    # ссылка приглашения, показанная администратором
    78: INVITE_LINK_78,
}


def connect(path: Path) -> sqlite3.Connection:
    """Автокоммит (isolation_level=None): транзакции — явным BEGIN."""
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def page(conn: sqlite3.Connection, table: str, user_id: int, before: "float | None", before_id: "str | None",
         limit: int, columns: str = "*", extra: str = "", extra_args: tuple = (), *,
         after: "tuple[float, str] | None" = None) -> sqlite3.Cursor:
    """Страница ленты истории (images, videos, transcripts, speeches): новые сверху — created, затем id по
    убыванию. Курсор — created и id последней записи прежней страницы: у записей бывает одно время.
    Без before_id (вкладка до выкладки составного курсора) — только created, записи того же времени
    пропадают. after — (created, id) записи: limit ближайших более новых, в том же порядке ленты
    (просмотр листает от записи вне загруженной ленты). table, columns и extra (« AND …» с extra_args) —
    из кода, не из запроса."""
    if after is not None:
        return conn.execute(f"SELECT * FROM (SELECT {columns} FROM {table} WHERE user_id = ?{extra}"
                            " AND (created > ? OR (created = ? AND id > ?)) ORDER BY created, id LIMIT ?)"
                            " ORDER BY created DESC, id DESC",
                            (user_id, *extra_args, after[0], after[0], after[1], limit))
    if before is None:
        where, args = "", ()
    elif before_id is None:
        where, args = " AND created < ?", (before,)
    else:
        where, args = " AND (created < ? OR (created = ? AND id < ?))", (before, before, before_id)
    return conn.execute(f"SELECT {columns} FROM {table} WHERE user_id = ?{extra}{where}"
                        " ORDER BY created DESC, id DESC LIMIT ?", (user_id, *extra_args, *args, limit))


#: наибольший id удалённой учётки (settings): `users.id` без AUTOINCREMENT, а SQLite выдал бы max(id) + 1 — и новой
#: учётке достались бы хвосты прежней (очереди удаления `purge`, `cleaner_deletions`, паузы почты, каталог `u<id>/`)
USER_SEQ = "user_seq"


def next_user_id(conn: sqlite3.Connection) -> int:
    """id новой учётки — после всех бывших: живых, удалённых (`USER_SEQ`) и тех, чьи хвосты остались в очередях
    удаления с прежних удалений до учёта. Без шага схемы: прежний код настройку не читает и хуже не делает."""
    return conn.execute(
        "SELECT max(coalesce((SELECT max(id) FROM users), 0),"
        " coalesce((SELECT CAST(value AS INTEGER) FROM settings WHERE key = ?), 0),"
        " coalesce((SELECT max(user_id) FROM purge), 0),"
        " coalesce((SELECT max(user_id) FROM cleaner_deletions), 0)) + 1", (USER_SEQ,)).fetchone()[0]


def retire_user_id(conn: sqlite3.Connection, uid: int) -> None:
    """Перед удалением учётки: её id больше не выдаётся (`next_user_id`)."""
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key)"
                 " DO UPDATE SET value = max(CAST(value AS INTEGER), CAST(excluded.value AS INTEGER))",
                 (USER_SEQ, str(uid)))


def migrate(conn: sqlite3.Connection) -> None:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current >= VERSION:
        return
    body = SCHEMA if current == 0 else "\n".join(STEPS[v] for v in range(current + 1, VERSION + 1))
    # одной транзакцией: обрыв посреди не оставит полсхемы со старой user_version
    conn.executescript(f"BEGIN;\n{body}\nPRAGMA user_version = {VERSION};\nCOMMIT;")
