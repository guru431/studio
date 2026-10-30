"""Соединение SQLite и схема базы (версия — VERSION).

Новая база получает SCHEMA сразу последней версии; прежняя — шаги STEPS от своей версии.
Шаг только добавляет: код предыдущей выкладки (откат install.sh) с новой базой работает."""
import sqlite3
from pathlib import Path

VERSION = 23

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
#: `scan_pages` с n от 1 (свой user_id — media.referenced). Поиск — триггерами, как у расшифровки; текст скана,
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
""" + INDEXES_9 + PASSPORT_10 + EDITS_11 + PHOTO_12 + SEARCH_13 + INTERNET_14 + THINKING_15 + OCR_16 + PIN_17 + TRANSLATE_18 + CODE_19 + CODE_20 + CODE_21 + CODE_22 + CODE_23

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
