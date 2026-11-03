"""Основа студии: конфиг, схема базы, пароли, шифрование токенов."""
import dataclasses
import json

import pytest
from cryptography.fernet import Fernet, InvalidToken

import clock  # noqa: F401  — модуль времени есть и импортируется
import config
import db
import security
import state


def test_config_names_missing_variables_without_values(monkeypatch):
    for k in config.REQUIRED: monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError) as e: config.load()
    assert all(k in str(e.value) for k in config.REQUIRED)


def test_config_refuses_a_bad_data_key_without_showing_it(env, monkeypatch):
    """Неверный ключ — отказ при старте, а не 500 на первом токене; значение в текст не попадает."""
    for bad in ["not-a-fernet-key-but-a-secret", Fernet.generate_key().decode()[:-4]]:
        monkeypatch.setenv("STUDIO_DATA_KEY", bad)
        with pytest.raises(RuntimeError) as e:
            config.load()
        assert "STUDIO_DATA_KEY" in str(e.value) and bad not in str(e.value)
        assert e.value.__cause__ is None and e.value.__suppress_context__


DROP_V4 = "DROP TABLE section_links; DROP TABLE section_defaults;"
DROP_V5 = "DROP TABLE transcripts; DROP TABLE voices; DROP TABLE speeches;"
DROP_V6 = "ALTER TABLE users DROP COLUMN views;"
DROP_V7 = ("ALTER TABLE users DROP COLUMN screen_lock; ALTER TABLE sessions DROP COLUMN mobile;"
           " ALTER TABLE sessions DROP COLUMN seen; DROP TABLE passkeys;")
DROP_V8 = "DROP TABLE purge;"
DROP_V9 = "DROP INDEX media_user; DROP INDEX messages_streaming;"
DROP_V10 = "ALTER TABLE connections DROP COLUMN passport;"
DROP_V11 = "ALTER TABLE transcripts DROP COLUMN edits; ALTER TABLE attachments DROP COLUMN source_transcript_id;"
DROP_V12 = "DROP TABLE presets; DROP INDEX images_user_source;"
#: шаг 13 — триггеры раньше столбца: DROP COLUMN перечитывает схему, а триггер ссылался бы на снятую
#: search_docs; снимать первой в цепочках — DROP_V5 сносит transcripts
SEARCH_TRIGGERS = ("search_images_ai", "search_images_ad", "search_videos_ai", "search_videos_au", "search_videos_ad",
                   "search_chats_ai", "search_chats_au", "search_chats_ad", "search_messages_ai", "search_messages_au",
                   "search_messages_ad", "search_attachments_ai", "search_attachments_au", "search_attachments_ad",
                   "search_transcripts_ai", "search_transcripts_au", "search_transcripts_ad", "search_speeches_ai",
                   "search_speeches_ad", "search_users_ad")
DROP_V13 = ("".join(f"DROP TRIGGER {t}; " for t in SEARCH_TRIGGERS)
            + "DROP TABLE search; DROP TABLE search_docs; ALTER TABLE transcripts DROP COLUMN text;")
DROP_V14 = ("ALTER TABLE chats DROP COLUMN web; ALTER TABLE chats DROP COLUMN web_consent;"
            " ALTER TABLE users DROP COLUMN exa_key; ALTER TABLE messages DROP COLUMN tools;")
DROP_V15 = ("ALTER TABLE chats DROP COLUMN thinking; ALTER TABLE messages DROP COLUMN reasoning;"
            " ALTER TABLE messages DROP COLUMN reasoning_sec;")
#: шаг 16 (распознавание): таблицы уносят свои индексы и триггеры поиска (связей ocr у новой базы нет —
#: подключений нет); снимать первой — триггеры ссылаются на search_docs, а DROP_V13 её сносит; триггер текста
#: вложений висит на attachments — его отдельно
DROP_V16 = "DROP TABLE scan_pages; DROP TABLE scans; DROP TRIGGER search_attachments_text;"
DROP_V17 = "ALTER TABLE users DROP COLUMN pin_hash; ALTER TABLE users DROP COLUMN pin_fails;"
#: шаг 18 (перевод): таблицы уносят свои индексы и триггеры поиска; снимать первой — триггеры ссылаются на
#: search_docs, а DROP_V13 её сносит (связи translate ставит код при старте — sections.adopt, не шаг)
DROP_V18 = "DROP TABLE translation_parts; DROP TABLE translations;"
#: шаг 19 (Claude Code): таблицы уносят свои индексы; снимать первой — code_chats и push_subs ссылаются на
#: agents и sessions
DROP_V19 = "DROP TABLE push_subs; DROP TABLE code_chats; DROP TABLE agent_events; DROP TABLE agents;"
#: шаг 20 (рабочее место Claude Code): снимать раньше шага 19 — таблицы ссылаются на agents
# Все старые фикстуры снимают и добавленное после рабочего места поле умолчаний.
DROP_V28 = "DROP TABLE ntfy_settings; DROP TABLE code_attention;"
DROP_V27 = DROP_V28 + "ALTER TABLE users DROP COLUMN timezone;"
DROP_V26 = DROP_V27 + ("DROP TRIGGER cleaner_context_delete; DROP TRIGGER cleaner_chat_delete;"
            " DROP TRIGGER cleaner_translation_delete; DROP TRIGGER cleaner_user_delete;"
            " DROP TRIGGER cleaner_connection_delete; DROP TABLE cleaner_contexts; DROP TABLE cleaner_deletions;"
            " ALTER TABLE connections DROP COLUMN cleaner; ALTER TABLE messages DROP COLUMN cleaner_raw;")
DROP_V25 = DROP_V26 + "ALTER TABLE users DROP COLUMN code_quick_actions;"
DROP_V24 = DROP_V25 + ("ALTER TABLE users DROP COLUMN screen_lock_sections; ALTER TABLE users DROP COLUMN screen_lock_timeout;"
            " ALTER TABLE users DROP COLUMN computer_pin_timeout;")
DROP_V20 = DROP_V24 + "ALTER TABLE users DROP COLUMN code_defaults; DROP TABLE code_place; DROP TABLE code_tabs;"


def test_migrate_creates_schema_once_and_seeds_the_gateway(env):
    conn = db.connect(env.db_path); db.migrate(conn); db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION == 29
    assert json.loads(conn.execute("SELECT value FROM settings WHERE key='allowlist'").fetchone()[0]) == ["10.77.1.15:4000"]
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_step_25_keeps_profile_and_gives_existing_users_initial_quick_actions(env):
    conn = db.connect(env.db_path)
    conn.executescript(db.SCHEMA.removesuffix(db.NTFY_29).removesuffix(db.CODE_28).removesuffix(db.TIMEZONE_27).removesuffix(db.CLEANER_26).removesuffix(db.CODE_25) + "\nPRAGMA user_version = 24;")
    conn.execute("INSERT INTO users(login, name, pw_hash, created, theme, code_defaults)"
                 " VALUES ('owner', 'Owner', 'hash', 0, 'dark', ?)", ('{"codex":{"effort":"high"}}',))
    db.migrate(conn); db.migrate(conn)
    assert conn.execute("SELECT theme, code_defaults, code_quick_actions FROM users").fetchone()[:] == (
        'dark', '{"codex":{"effort":"high"}}', None)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()


def test_step_27_defaults_existing_users_to_moscow_without_changing_their_data(env):
    conn = db.connect(env.db_path)
    conn.executescript(db.SCHEMA.removesuffix(db.NTFY_29).removesuffix(db.CODE_28).removesuffix(db.TIMEZONE_27) + '\nPRAGMA user_version = 26;')
    conn.execute("INSERT INTO users(login, name, pw_hash, created, theme) VALUES ('owner', 'Owner', 'hash', 0, 'dark')")
    db.migrate(conn)
    assert conn.execute('SELECT timezone, theme FROM users').fetchone()[:] == ('Europe/Moscow', 'dark')
    conn.execute("UPDATE users SET timezone = 'Asia/Kathmandu'")
    db.migrate(conn)
    assert conn.execute('SELECT timezone FROM users').fetchone()[0] == 'Asia/Kathmandu'
    assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    conn.close()


def test_step_28_keeps_profile_and_cascades_attention_with_computer(env):
    conn = db.connect(env.db_path)
    conn.executescript(db.SCHEMA.removesuffix(db.NTFY_29).removesuffix(db.CODE_28) + '\nPRAGMA user_version = 27;')
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created, timezone) VALUES (1, 'u', 'U', 'h', 0, 'UTC')")
    conn.execute("INSERT INTO agents(id, user_id, name, key_hash, created) VALUES (1, 1, 'PC', 'k', 0)")
    db.migrate(conn)
    conn.execute("INSERT INTO code_attention VALUES (1, 'p', 'c', 'r', 1, 0)")
    db.migrate(conn)
    assert conn.execute('SELECT timezone FROM users').fetchone()[0] == 'UTC'
    assert conn.execute('SELECT seen FROM code_attention').fetchone()[0] == 0
    conn.execute('DELETE FROM agents WHERE id = 1')
    assert conn.execute('SELECT * FROM code_attention').fetchall() == []
    assert conn.execute('PRAGMA foreign_key_check').fetchall() == []
    conn.close()


def test_migrate_brings_a_version_1_base_up_keeping_its_rows(env):
    """База прода — версии 1: шаг добавляет столбцы, строки и настройки остаются."""
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript("ALTER TABLE images DROP COLUMN mask_media_id; ALTER TABLE messages DROP COLUMN alive_at;"
                       " ALTER TABLE images DROP COLUMN chain; ALTER TABLE videos DROP COLUMN chain;"
                       f" {DROP_V20} {DROP_V19} {DROP_V18} {DROP_V17} {DROP_V16} {DROP_V15} {DROP_V14} {DROP_V13} {DROP_V12} {DROP_V11} {DROP_V4} {DROP_V5} {DROP_V6} {DROP_V7} {DROP_V8} {DROP_V9} {DROP_V10} PRAGMA user_version = 1;")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    conn.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, state, detail, created)"
                 " VALUES ('i1', 1, 'edit', 'lustify', 'p', '64x48', 1, 'done', 'готово', 0)")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION
    assert conn.execute("SELECT id, mask_media_id FROM images").fetchone()[:] == ("i1", None)
    assert "alive_at" in [r[1] for r in conn.execute("PRAGMA table_info(messages)")]


def test_migrate_brings_a_version_2_base_up_keeping_its_rows(env):
    """База прода — версии 2: шаг добавляет столбцы цепочки, строки остаются без неё."""
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript("ALTER TABLE images DROP COLUMN chain; ALTER TABLE videos DROP COLUMN chain;"
                       f" {DROP_V20} {DROP_V19} {DROP_V18} {DROP_V17} {DROP_V16} {DROP_V15} {DROP_V14} {DROP_V13} {DROP_V12} {DROP_V11} {DROP_V4} {DROP_V5} {DROP_V6} {DROP_V7} {DROP_V8} {DROP_V9} {DROP_V10} PRAGMA user_version = 2;")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    conn.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, state, detail, created)"
                 " VALUES ('i1', 1, 'edit', 'lustify', 'p', '64x48', 1, 'done', 'готово', 0)")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION
    assert conn.execute("SELECT id, chain IS NULL FROM images").fetchone()[:] == ("i1", 1)
    assert "chain" in [r[1] for r in conn.execute("PRAGMA table_info(videos)")]


def test_migrate_links_a_version_3_base_to_every_section(env):
    """Шаг 4: srvllm — во всех шести разделах с перечнем, чат — с моделью пользователя; openai — в чате.
    Шаг 16 добавляет srvllm в «Распознавание»."""
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript(f"{DROP_V20} {DROP_V19} {DROP_V18} {DROP_V17} {DROP_V16} {DROP_V15} {DROP_V14} {DROP_V13} {DROP_V12} {DROP_V11} {DROP_V4} {DROP_V5} {DROP_V6} {DROP_V7} {DROP_V8} {DROP_V9} {DROP_V10} PRAGMA user_version = 3;")
    ins = ("INSERT INTO connections(id, user_id, name, kind, base_url, token_enc, sections, created)"
           " VALUES (?, 1, 'c', ?, ?, x'00', ?, 0)")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    conn.execute(ins, (7, "srvllm", "http://10.77.1.15:4000", '["chat","photo","video"]'))
    conn.execute(ins, (8, "openai", "https://cloud.test/v1", '["chat"]'))
    conn.execute("UPDATE users SET default_connection_id = 7, default_model = 'gemma4-abl'")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION
    links = {(r[0], r[1]): json.loads(r[2]) for r in conn.execute("SELECT connection_id, section, models FROM section_links")}
    assert links == {(7, "chat"): ["gemma4-abl"], (7, "photo"): ["lustify", "flux"], (7, "video"): ["wan"],
                     (7, "transcribe"): ["whisperx"], (7, "speech"): ["qwen3-tts"], (7, "dictation"): ["gigaam", "whisperx"],
                     (7, "ocr"): ["hunyuan-ocr"], (8, "chat"): []}
    defaults = dict(((r[0]), (r[1], r[2])) for r in conn.execute("SELECT section, connection_id, model FROM section_defaults"))
    assert defaults == {"chat": (7, "gemma4-abl"), "photo": (7, "lustify"), "video": (7, "wan"),
                        "transcribe": (7, "whisperx"), "speech": (7, "qwen3-tts"), "dictation": (7, "gigaam"),
                        "ocr": (7, "hunyuan-ocr")}


def test_deleting_a_user_cascades(env):
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    conn.execute("INSERT INTO sessions(digest, user_id, csrf, expires, renewed) VALUES ('d', 1, 'c', 1, 0)")
    conn.execute("INSERT INTO chats(id, user_id, created, updated) VALUES ('c1', 1, 0, 0)")
    conn.execute("INSERT INTO messages(id, chat_id, role, content, status, via, created)"
                 " VALUES ('m1', 'c1', 'user', 'hi', 'done', 'server', 0)")
    conn.execute("DELETE FROM users WHERE id = 1")
    for table in ("users", "sessions", "chats", "messages"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0, table


def test_password_hash_roundtrip_and_salt():
    a, b = security.hash_password("correct horse"), security.hash_password("correct horse")
    assert a != b and a.startswith("scrypt$16384$8$1$")
    assert security.verify_password("correct horse", a) and not security.verify_password("wrong", a)
    assert not security.verify_password("x", security.DUMMY_HASH)


def test_token_encryption_needs_the_same_key(env, monkeypatch):
    cfg = config.load()
    monkeypatch.setattr(state, "CFG", cfg)
    blob = security.encrypt("sk-secret")
    assert isinstance(blob, bytes) and b"sk-secret" not in blob
    assert security.decrypt(blob) == "sk-secret"
    monkeypatch.setattr(state, "CFG", dataclasses.replace(cfg, data_key=Fernet.generate_key().decode()))
    with pytest.raises(InvalidToken):
        security.decrypt(blob)


def test_migrate_adds_the_speech_tables_to_a_version_4_base(env):
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript(f"{DROP_V20} {DROP_V19} {DROP_V18} {DROP_V17} {DROP_V16} {DROP_V15} {DROP_V14} {DROP_V13} {DROP_V12} {DROP_V11} {DROP_V5} {DROP_V6} {DROP_V7} {DROP_V8} {DROP_V9} {DROP_V10} PRAGMA user_version = 4;")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"transcripts", "voices", "speeches"} <= tables
    assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1


def test_migrate_gives_a_version_5_base_an_empty_view_choice(env):
    """Шаг 6: вид ленты — в учётке; у прежних учёток выбора нет — разделы берут свой вид по умолчанию."""
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript(f"{DROP_V20} {DROP_V19} {DROP_V18} {DROP_V17} {DROP_V16} {DROP_V15} {DROP_V14} {DROP_V13} {DROP_V12} {DROP_V11} {DROP_V6} {DROP_V7} {DROP_V8} {DROP_V9} {DROP_V10} PRAGMA user_version = 5;")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION
    assert conn.execute("SELECT views FROM users").fetchone()[0] == "{}"


def test_migrate_adds_the_screen_lock_to_a_version_6_base(env):
    """Шаг 7: блокировка на телефоне выключена у прежних учёток, прежние сессии — без отметок."""
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript(f"{DROP_V20} {DROP_V19} {DROP_V18} {DROP_V17} {DROP_V16} {DROP_V15} {DROP_V14} {DROP_V13} {DROP_V12} {DROP_V11} {DROP_V7} {DROP_V8} {DROP_V9} {DROP_V10} PRAGMA user_version = 6;")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    conn.execute("INSERT INTO sessions(digest, user_id, csrf, expires, renewed) VALUES ('d', 1, 'c', 9e12, 0)")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION
    assert conn.execute("SELECT screen_lock FROM users").fetchone()[0] == 0
    assert conn.execute("SELECT mobile, seen IS NULL FROM sessions").fetchone()[:] == (0, 1)
    assert conn.execute("SELECT count(*) FROM passkeys").fetchone()[0] == 0


def test_migrate_adds_the_purge_queue_to_a_version_7_base(env):
    """Шаг 8: очередь удаления файлов — пустая и без ссылки на учётку: удаление учётки путь не уносит."""
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript(f"{DROP_V20} {DROP_V19} {DROP_V18} {DROP_V17} {DROP_V16} {DROP_V15} {DROP_V14} {DROP_V13} {DROP_V12} {DROP_V11} {DROP_V8} {DROP_V9} {DROP_V10} PRAGMA user_version = 7;")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION
    conn.execute("INSERT INTO purge(media_id, user_id, rel_path, queued) VALUES ('m', 1, 'u1/2027/01/m.png', 0)")
    conn.execute("DELETE FROM users WHERE id = 1")
    assert conn.execute("SELECT media_id, user_id, tries FROM purge").fetchone()[:] == ("m", 1, 0)


def test_migrate_indexes_files_and_streaming_answers_in_a_version_8_base(env):
    """Шаг 9: файлы учётки и идущие ответы — по индексу, а не полным просмотром media («Хранилище»,
    стирание истории) и всей переписки (предел ответов на каждом ходу)."""
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript(f"{DROP_V20} {DROP_V19} {DROP_V18} {DROP_V17} {DROP_V16} {DROP_V15} {DROP_V14} {DROP_V13} {DROP_V12} {DROP_V11} {DROP_V9} {DROP_V10} PRAGMA user_version = 8;")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION

    def plan(sql):
        return " ".join(r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + sql))
    assert "media_user" in plan("SELECT size FROM media WHERE user_id = 1")
    assert "messages_streaming" in plan("SELECT COUNT(*) FROM messages m JOIN chats c ON c.id = m.chat_id"
                                        " WHERE c.user_id = 1 AND m.via = 'server' AND m.status = 'streaming'")


def test_migrate_adds_the_passport_to_a_version_9_base(env):
    """Шаг 10: паспорт подключения (checks.py) — у прежних подключений пуст: «не проверено»."""
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript(f"{DROP_V20} {DROP_V19} {DROP_V18} {DROP_V17} {DROP_V16} {DROP_V15} {DROP_V14} {DROP_V13} {DROP_V12} {DROP_V11} {DROP_V10} PRAGMA user_version = 9;")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    conn.execute("INSERT INTO connections(user_id, name, kind, base_url, token_enc, sections, created)"
                 " VALUES (1, 'c', 'openai', 'https://cloud.test/v1', x'00', '[]', 0)")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION
    assert conn.execute("SELECT passport FROM connections").fetchone()[0] is None


def test_migrate_adds_transcript_edits_to_a_version_10_base(env):
    """Шаг 11: правки расшифровки и ссылка вложения на расшифровку; прежние строки — без них."""
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript(f"{DROP_V20} {DROP_V19} {DROP_V18} {DROP_V17} {DROP_V16} {DROP_V15} {DROP_V14} {DROP_V13} {DROP_V12} {DROP_V11} PRAGMA user_version = 10;")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    conn.execute("INSERT INTO transcripts(id, user_id, model, name, source_media_id, state, detail, created)"
                 " VALUES ('t1', 1, 'whisperx', 'a.m4a', 'm', 'done', 'готово', 0)")
    conn.execute("INSERT INTO attachments(id, user_id, kind, name, media_id, created) VALUES ('a1', 1, 'document', 'a.txt', 'm2', 0)")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION
    assert conn.execute("SELECT edits FROM transcripts").fetchone()[0] is None
    assert conn.execute("SELECT source_transcript_id FROM attachments").fetchone()[0] is None


def test_migrate_adds_presets_and_the_source_index_to_a_version_11_base(env):
    """Шаг 12 (фото): пресеты учётки (каскад с ней) и индекс вариантов — картинки по исходнику."""
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript(f"{DROP_V20} {DROP_V19} {DROP_V18} {DROP_V17} {DROP_V16} {DROP_V15} {DROP_V14} {DROP_V13} {DROP_V12} PRAGMA user_version = 11;")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION
    conn.execute("INSERT INTO presets(id, user_id, section, name, params, created) VALUES ('p', 1, 'photo', 'n', '{}', 0)")

    def plan(sql):
        return " ".join(r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + sql))
    assert "presets_user" in plan("SELECT * FROM presets WHERE user_id = 1 AND section = 'photo'")
    assert "images_user_source" in plan("SELECT * FROM images WHERE user_id = 1 AND source_media_id = 'm'")
    conn.execute("DELETE FROM users WHERE id = 1")
    assert conn.execute("SELECT COUNT(*) FROM presets").fetchone()[0] == 0


def test_migrate_adds_the_search_index_to_a_version_12_base(env):
    """Шаг 13: индекс поиска заполняется из таблиц истории — промпты, названия и сообщения диалогов,
    отправленные документы, расшифровки по имени, озвучки; идущий ответ и неотправленный документ — нет.
    Текст расшифровки (`transcripts.text`) пуст: его заполнит фон по файлу итога."""
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript(f"{DROP_V20} {DROP_V19} {DROP_V18} {DROP_V17} {DROP_V16} {DROP_V15} {DROP_V14} {DROP_V13} PRAGMA user_version = 12;")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    conn.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, state, detail, created)"
                 " VALUES ('i1', 1, 'generation', 'lustify', 'Кошка на крыше', '64x48', 1, 'done', 'готово', 1)")
    conn.execute(FEED_ROWS["videos"].replace("'p'", "'Ёлка горит'"), ("v1", 1, 2))
    conn.execute(FEED_ROWS["transcripts"].replace("'a.m4a'", "'Звонок Ивану.m4a'"), ("t1", 1, 3))
    conn.execute(FEED_ROWS["speeches"].replace("'t'", "'Доброе утро'"), ("s1", 1, 4))
    conn.execute("INSERT INTO chats(id, user_id, title, model, created, updated) VALUES ('c1', 1, 'Про отпуск', 'm', 5, 5)")
    for mid, content, status in (("m1", "Где море теплее", "done"), ("m2", "Пишется ответ", "streaming")):
        conn.execute("INSERT INTO messages(id, chat_id, role, content, status, via, model, created)"
                     " VALUES (?, 'c1', 'user', ?, ?, 'server', 'm', 6)", (mid, content, status))
    for aid, message in (("a1", "m1"), ("a2", None)):
        conn.execute("INSERT INTO attachments(id, user_id, message_id, kind, name, media_id, text, created)"
                     " VALUES (?, 1, ?, 'document', ?, 'x', 'Договор аренды', 7)", (aid, message, f"{aid}.txt"))
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION

    def found(word):
        return sorted(tuple(r) for r in conn.execute(
            "SELECT d.section, d.item, d.part FROM search JOIN search_docs d ON d.id = search.rowid"
            " WHERE search MATCH ?", (f'"{word}"*',)))
    assert found("кошк") == [("photo", "i1", "")]
    assert found("елка") == [("video", "v1", "")]
    assert found("иван") == [("transcribe", "t1", "")]
    assert found("утро") == [("speech", "s1", "")]
    assert found("отпуск") == [("chat", "c1", "title")]
    assert found("море") == [("chat", "c1", "m1")]
    assert found("пишется") == []
    assert found("договор") == [("chat", "c1", "a1")]
    assert conn.execute("SELECT text FROM transcripts").fetchone()[0] is None


def test_migrate_adds_the_internet_columns_to_a_version_13_base(env):
    """Шаг 14 (интернет в чате): у прежних диалогов «Интернет» выключен и подтверждения нет, у учёток
    нет ключа Exa, у сообщений — шагов инструментов; строки целы."""
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript(f"{DROP_V20} {DROP_V19} {DROP_V18} {DROP_V17} {DROP_V16} {DROP_V15} {DROP_V14} PRAGMA user_version = 13;")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    conn.execute("INSERT INTO chats(id, user_id, title, model, created, updated) VALUES ('c1', 1, 'Про отпуск', 'm', 5, 5)")
    conn.execute("INSERT INTO messages(id, chat_id, role, content, status, via, model, created)"
                 " VALUES ('m1', 'c1', 'assistant', 'Ответ', 'done', 'server', 'm', 6)")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION
    assert conn.execute("SELECT id, title, web, web_consent FROM chats").fetchone()[:] == ("c1", "Про отпуск", 0, None)
    assert conn.execute("SELECT login, exa_key FROM users").fetchone()[:] == ("u", None)
    assert conn.execute("SELECT content, tools FROM messages").fetchone()[:] == ("Ответ", None)


@pytest.mark.parametrize("stepped", [False, True], ids=["new", "stepped"])
def test_previous_code_still_works_on_a_version_14_base(env, stepped):
    """Откат install.sh на код схемы 13 (спека интернета, раздел 9): прежний код пишет строки без новых
    столбцов и читает `SELECT *` — у новой базы (SCHEMA) и у поднятой шагом 14."""
    conn = db.connect(env.db_path); db.migrate(conn)
    if stepped:
        conn.executescript(f"{DROP_V20} {DROP_V19} {DROP_V18} {DROP_V17} {DROP_V16} {DROP_V15} {DROP_V14} PRAGMA user_version = 13;")
        db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    conn.execute("INSERT INTO chats(id, user_id, title, connection_id, model, created, updated)"
                 " VALUES ('c1', 1, NULL, NULL, 'm', 5, 5)")
    conn.execute("INSERT INTO messages(id, chat_id, role, content, status, via, model, created, alive_at)"
                 " VALUES ('m1', 'c1', 'assistant', '', 'streaming', 'server', 'm', 6, 6)")
    conn.execute("UPDATE messages SET content = 'Ответ', status = 'done' WHERE id = 'm1'")
    chat = dict(conn.execute("SELECT * FROM chats").fetchone())
    assert (chat["id"], chat["model"], chat["web"], chat["web_consent"]) == ("c1", "m", 0, None)
    message = dict(conn.execute("SELECT * FROM messages").fetchone())
    assert (message["content"], message["status"], message["tools"]) == ("Ответ", "done", None)
    assert dict(conn.execute("SELECT * FROM users").fetchone())["exa_key"] is None
    conn.execute("DELETE FROM chats WHERE id = 'c1'")                       # каскад прежний
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_migrate_adds_the_reasoning_columns_to_a_version_14_base(env):
    """Шаг 15 (кнопка «Рассуждение»): у прежних диалогов кнопка выключена, у сообщений нет рассуждения и его
    длительности; строки целы."""
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript(f"{DROP_V20} {DROP_V19} {DROP_V18} {DROP_V17} {DROP_V16} {DROP_V15} PRAGMA user_version = 14;")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    conn.execute("INSERT INTO chats(id, user_id, title, model, web, created, updated)"
                 " VALUES ('c1', 1, 'Про отпуск', 'm', 1, 5, 5)")
    conn.execute("INSERT INTO messages(id, chat_id, role, content, status, via, model, created, tools)"
                 " VALUES ('m1', 'c1', 'assistant', 'Ответ', 'done', 'server', 'm', 6, '[]')")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION
    assert conn.execute("SELECT id, title, web, thinking FROM chats").fetchone()[:] == ("c1", "Про отпуск", 1, 0)
    assert conn.execute("SELECT content, tools, reasoning, reasoning_sec FROM messages").fetchone()[:] == (
        "Ответ", "[]", None, None)


@pytest.mark.parametrize("stepped", [False, True], ids=["new", "stepped"])
def test_previous_code_still_works_on_a_version_15_base(env, stepped):
    """Откат install.sh на код схемы 14: прежний код пишет строки без новых столбцов и читает `SELECT *` — у
    новой базы (SCHEMA) и у поднятой шагом 15."""
    conn = db.connect(env.db_path); db.migrate(conn)
    if stepped:
        conn.executescript(f"{DROP_V20} {DROP_V19} {DROP_V18} {DROP_V17} {DROP_V16} {DROP_V15} PRAGMA user_version = 14;")
        db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    conn.execute("INSERT INTO chats(id, user_id, title, connection_id, model, created, updated)"
                 " VALUES ('c1', 1, NULL, NULL, 'm', 5, 5)")
    conn.execute("UPDATE chats SET web = 1 WHERE id = 'c1'")
    conn.execute("INSERT INTO messages(id, chat_id, role, content, status, via, model, created, alive_at)"
                 " VALUES ('m1', 'c1', 'assistant', '', 'streaming', 'server', 'm', 6, 6)")
    conn.execute("UPDATE messages SET content = 'Ответ', status = 'done', tools = '[]' WHERE id = 'm1'")
    chat = dict(conn.execute("SELECT * FROM chats").fetchone())
    assert (chat["id"], chat["web"], chat["thinking"]) == ("c1", 1, 0)
    message = dict(conn.execute("SELECT * FROM messages").fetchone())
    assert (message["content"], message["tools"], message["reasoning"], message["reasoning_sec"]) == (
        "Ответ", "[]", None, None)
    conn.execute("DELETE FROM chats WHERE id = 'c1'")                       # каскад прежний
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_step_16_links_every_srvllm_connection_to_ocr(env):
    """Шаг 16 (распознавание): каждое подключение srvllm — в разделе «Распознавание» с hunyuan-ocr, умолчание —
    наименьшее из них у пользователя; openai — нет. Прежние связи целы."""
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript(f"{DROP_V20} {DROP_V19} {DROP_V18} {DROP_V17} {DROP_V16} PRAGMA user_version = 15;")
    ins = ("INSERT INTO connections(id, user_id, name, kind, base_url, token_enc, sections, created)"
           " VALUES (?, ?, 'c', ?, ?, x'00', '[]', ?)")
    for uid in (1, 2):
        conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (?, ?, 'U', 'h', 0)", (uid, f"u{uid}"))
    for cid, uid, kind in ((3, 1, "srvllm"), (4, 1, "openai"), (5, 1, "srvllm"), (6, 2, "openai"), (9, 2, "srvllm")):
        conn.execute(ins, (cid, uid, kind, "https://cloud.test/v1" if kind == "openai" else "http://10.77.1.15:4000",
                           cid * 10))
    conn.execute("INSERT INTO section_links(user_id, section, connection_id, models, created)"
                 " VALUES (1, 'photo', 3, '[\"flux\"]', 1)")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION
    links = [(r[0], r[1], json.loads(r[2]), r[3]) for r in conn.execute(
        "SELECT user_id, connection_id, models, created FROM section_links WHERE section = 'ocr' ORDER BY connection_id")]
    assert links == [(1, 3, ["hunyuan-ocr"], 30), (1, 5, ["hunyuan-ocr"], 50), (2, 9, ["hunyuan-ocr"], 90)]
    defaults = [tuple(r) for r in conn.execute(
        "SELECT user_id, connection_id, model FROM section_defaults WHERE section = 'ocr' ORDER BY user_id")]
    assert defaults == [(1, 3, "hunyuan-ocr"), (2, 9, "hunyuan-ocr")]
    assert [tuple(r) for r in conn.execute("SELECT section, models FROM section_links WHERE section != 'ocr'")] == [
        ("photo", '["flux"]')]


def test_step_17_adds_the_pin_to_users(env):
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript(f"{DROP_V20} {DROP_V19} {DROP_V18} {DROP_V17} PRAGMA user_version = 16;")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created, screen_lock) VALUES (1, 'u', 'U', 'h', 0, 1)")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION
    assert tuple(conn.execute("SELECT login, screen_lock, pin_hash, pin_fails FROM users").fetchone()) == ("u", 1, None, 0)


def test_step_18_adds_translations_with_search_and_keeps_rows(env):
    """Шаг 18 (перевод): записи и куски, поиск триггерами — по исходнику и переводу (с «ё» = «е»); запись и
    куски уходят с учёткой, строка поиска — с ними."""
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript(f"{DROP_V20} {DROP_V19} {DROP_V18}PRAGMA user_version = 17;")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION == 29
    conn.execute("INSERT INTO translations(id, user_id, kind, model, source_lang, target_lang, name, source,"
                 " state, detail, created) VALUES ('t1', 1, 'text', 'm', 'auto', 'ru', 'Hello', 'Hello world',"
                 " 'queued', 'в очереди', 1)")
    conn.execute("UPDATE translations SET text = 'Привет, ёжик' WHERE id = 't1'")
    found = lambda w: [tuple(r) for r in conn.execute("SELECT d.section, d.item FROM search JOIN search_docs d"   # noqa: E731
                                                      " ON d.id = search.rowid WHERE search MATCH ?", (f'"{w}"*',))]
    assert found("world") == found("ежик") == found("hello") == [("translate", "t1")]
    conn.execute("UPDATE translations SET source = 'Goodbye' WHERE id = 't1'")
    assert found("world") == [] and found("goodbye") == [("translate", "t1")]
    conn.execute("INSERT INTO translation_parts(translation_id, n, source, state) VALUES ('t1', 1, 'Hello', 'waiting')")
    assert conn.execute("SELECT sep, box, text FROM translation_parts").fetchone()[:] == ("", None, None)

    def plan(sql):
        return " ".join(r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + sql))
    assert "translations_user_created" in plan("SELECT * FROM translations WHERE user_id = 1 ORDER BY created DESC")
    assert "translations_state" in plan("SELECT * FROM translations WHERE state = 'working'")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO translations(id, user_id, kind, model, source_lang, target_lang, name,"
                     " state, detail, created) VALUES ('t2', 1, 'mail', 'm', 'auto', 'ru', 'n', 'queued', '', 1)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO translation_parts(translation_id, n, source, state) VALUES ('t1', 2, 'x', 'ready')")
    conn.execute("DELETE FROM users WHERE id = 1")                       # запись и куски — каскадом
    assert conn.execute("SELECT COUNT(*) FROM translation_parts").fetchone()[0] == 0
    assert found("goodbye") == [] and conn.execute("SELECT COUNT(*) FROM search_docs").fetchone()[0] == 0


def test_step_19_adds_code_tables_and_cascades(env):
    """Шаг 19 (Claude Code): компьютеры, история подключений, чаты, подписки push. Подписка уходит с сессией,
    чаты — с компьютером (история подключений остаётся), всё — с учёткой."""
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript(f"{DROP_V20} {DROP_V19} PRAGMA user_version = 18;")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    conn.execute("INSERT INTO sessions(digest, user_id, csrf, expires, renewed) VALUES ('d1', 1, 'c', 9, 0)")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION == 29
    conn.execute("INSERT INTO agents(id, user_id, name, key_hash, created) VALUES (5, 1, 'ПК', 'h5', 1)")
    assert conn.execute("SELECT has_pin, projects_root, cc_version, last_seen FROM agents").fetchone()[:] == (
        0, None, None, None)
    conn.execute("INSERT INTO agent_events(user_id, agent_id, agent_name, created, kind) VALUES (1, 5, 'ПК', 1, 'enroll')")
    conn.execute("INSERT INTO code_chats(user_id, agent_id, project, session_id, state, created, updated)"
                 " VALUES (1, 5, 'p', 's1', 'open', 1, 1)")
    conn.execute("INSERT INTO push_subs(endpoint, user_id, digest, p256dh, auth, created) VALUES ('e1', 1, 'd1', 'k', 'a', 1)")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO agents(user_id, name, key_hash, created) VALUES (1, 'ПК2', 'h5', 1)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO agent_events(user_id, agent_id, agent_name, created, kind) VALUES (1, 5, 'ПК', 1, 'x')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO code_chats(user_id, agent_id, project, session_id, state, created, updated)"
                     " VALUES (1, 5, 'p', 's1', 'open', 1, 1)")

    def count(table):
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    conn.execute("DELETE FROM sessions WHERE digest = 'd1'")               # подписка — с сессией
    assert count("push_subs") == 0 and count("agents") == 1
    conn.execute("DELETE FROM agents WHERE id = 5")                        # чаты — с компьютером, история — нет
    assert count("code_chats") == 0 and count("agent_events") == 1
    conn.execute("INSERT INTO sessions(digest, user_id, csrf, expires, renewed) VALUES ('d2', 1, 'c', 9, 0)")
    # новый компьютер — id больше любого бывшего (AUTOINCREMENT): без него — наибольший живой + 1, здесь 1
    assert conn.execute("INSERT INTO agents(user_id, name, key_hash, created) VALUES (1, 'ПК', 'h6', 1)"
                        " RETURNING id").fetchone()[0] == 6
    conn.execute("INSERT INTO code_chats(user_id, agent_id, project, session_id, state, created, updated)"
                 " VALUES (1, 6, 'p', 's2', 'closed', 1, 1)")
    conn.execute("INSERT INTO push_subs(endpoint, user_id, digest, p256dh, auth, created) VALUES ('e2', 1, 'd2', 'k', 'a', 1)")
    conn.execute("DELETE FROM users WHERE id = 1")                         # всё — с учёткой
    assert [count(t) for t in ("agents", "agent_events", "code_chats", "push_subs")] == [0, 0, 0, 0]


def test_step_20_adds_the_code_workspace_and_cascades(env):
    """Шаг 20 (рабочее место Claude Code): вкладки уходят с компьютером, место и панель с ним пустеют; всё — с
    учёткой; вид — только из перечня, у проекта компьютера — одна строка."""
    import sqlite3
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript(f"ALTER TABLE code_chats DROP COLUMN engine; {DROP_V20} PRAGMA user_version = 19;")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    conn.execute("INSERT INTO agents(id, user_id, name, key_hash, created) VALUES (5, 1, 'ПК', 'h5', 1)")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION == 29
    conn.execute("INSERT INTO code_tabs(user_id, agent_id, project, touched) VALUES (1, 5, 'p', 1)")
    assert conn.execute("SELECT tabs, active FROM code_tabs").fetchone()[:] == ("[]", None)
    conn.execute("INSERT INTO code_place(user_id, panel_id, agent_id, project) VALUES (1, 5, 5, 'p')")
    assert conn.execute("SELECT view FROM code_place").fetchone()[0] == "chat"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO code_tabs(user_id, agent_id, project, touched) VALUES (1, 5, 'p', 2)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE code_place SET view = 'x'")

    def count(table):
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    conn.execute("DELETE FROM agents WHERE id = 5")
    assert count("code_tabs") == 0
    assert conn.execute("SELECT panel_id, agent_id, project FROM code_place").fetchone()[:] == (None, None, "p")
    conn.execute("DELETE FROM users WHERE id = 1")
    assert count("code_place") == 0


def test_code_without_the_translate_section_ignores_its_rows(studio, monkeypatch):
    """Откат на код схемы 17 — так, как его видит прежний код: раздела translate он не знает. Подключение
    со связью translate правится без `sections` (не 422), а поиск со строкой перевода — 200 без неё (не 500)."""
    import connections
    import search
    import sections
    from conftest import add_gateway
    cid = add_gateway(studio)
    uid = state.DB.execute("SELECT user_id FROM connections WHERE id = ?", (cid,)).fetchone()[0]
    assert "translate" in connections.get(uid, cid).sections
    state.DB.execute("INSERT INTO translations(id, user_id, kind, model, source_lang, target_lang, name, source,"
                     " state, detail, created) VALUES ('t1', ?, 'text', 'm', 'auto', 'ru', 'Hello', 'Hello world',"
                     " 'done', 'готово', 1)", (uid,))
    old = tuple(s for s in sections.SECTIONS if s != "translate")
    titles = {k: v for k, v in search.TITLES.items() if k != "translate"}
    monkeypatch.setattr(sections, "SECTIONS", old)
    monkeypatch.setattr(search, "TITLES", titles)
    monkeypatch.setattr(search, "KNOWN", "+d.section IN ({})".format(", ".join(f"'{s}'" for s in titles)))
    assert "translate" not in connections.get(uid, cid).sections
    r = studio.c.patch(f"/api/connections/{cid}", json={"name": "x"})
    assert r.status_code == 200 and "translate" not in r.json()["sections"]
    r = studio.c.post("/api/search", json={"q": "world"})
    assert r.status_code == 200 and r.json()["items"] == []


@pytest.mark.parametrize("stepped", [False, True], ids=["new", "stepped"])
def test_scans_and_their_pages_follow_the_user_and_the_constraints(env, stepped):
    """Документы распознавания (шаг 16): индексы ленты, вложения и состояния; страницы уходят с документом,
    документ — с учёткой; происхождение и состояние страницы — только из перечня."""
    import sqlite3
    conn = db.connect(env.db_path); db.migrate(conn)
    if stepped:
        conn.executescript(f"{DROP_V20} {DROP_V19} {DROP_V18} {DROP_V17} {DROP_V16} PRAGMA user_version = 15;")
        db.migrate(conn)
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    conn.execute("INSERT INTO scans(id, user_id, model, name, origin, attachment_id, pages, state, detail, created)"
                 " VALUES ('s1', 1, 'hunyuan-ocr', 'Скан.pdf', 'chat', 'a1', 2, 'queued', 'в очереди', 1)")
    for n, page_state in ((1, "layer"), (2, "waiting")):
        conn.execute("INSERT INTO scan_pages(scan_id, user_id, n, state) VALUES ('s1', 1, ?, ?)", (n, page_state))
    assert conn.execute("SELECT done_pages, text, finished FROM scans").fetchone()[:] == (0, None, None)

    def plan(sql):
        return " ".join(r[-1] for r in conn.execute("EXPLAIN QUERY PLAN " + sql))
    assert "scans_user_created" in plan("SELECT * FROM scans WHERE user_id = 1 ORDER BY created DESC")
    assert "scans_attachment" in plan("SELECT * FROM scans WHERE attachment_id = 'a1'")
    assert "scans_state" in plan("SELECT * FROM scans WHERE state = 'working'")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO scans(id, user_id, model, name, origin, pages, state, detail, created)"
                     " VALUES ('s2', 1, 'm', 'n', 'mail', 1, 'queued', '', 1)")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO scan_pages(scan_id, user_id, n, state) VALUES ('s1', 1, 3, 'ready')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO scan_pages(scan_id, user_id, n, state) VALUES ('s1', 1, 1, 'done')")
    conn.execute("DELETE FROM scans WHERE id = 's1'")
    assert conn.execute("SELECT COUNT(*) FROM scan_pages").fetchone()[0] == 0
    conn.execute("INSERT INTO scans(id, user_id, model, name, origin, pages, state, detail, created)"
                 " VALUES ('s3', 1, 'hunyuan-ocr', 'n', 'section', 1, 'done', 'готово', 2)")
    conn.execute("INSERT INTO scan_pages(scan_id, user_id, n, state) VALUES ('s3', 1, 1, 'done')")
    conn.execute("DELETE FROM users WHERE id = 1")
    assert conn.execute("SELECT (SELECT COUNT(*) FROM scans) + (SELECT COUNT(*) FROM scan_pages)").fetchone()[0] == 0


#: строка истории каждой ленты: id, user_id, created
FEED_ROWS = {
    "images": "INSERT INTO images(id, user_id, kind, model, prompt, size, seed, state, detail, created)"
              " VALUES (?, ?, 'generation', 'flux', 'p', '1024x1024', 1, 'done', 'готово', ?)",
    "videos": "INSERT INTO videos(id, user_id, connection_id, source_media_id, prompt, seconds, quality, smooth,"
              " state, detail, created) VALUES (?, ?, 1, 'm', 'p', 1, 'fast', 0, 'completed', 'готово', ?)",
    "transcripts": "INSERT INTO transcripts(id, user_id, model, name, source_media_id, state, detail, created)"
                   " VALUES (?, ?, 'whisperx', 'a.m4a', 'm', 'done', 'готово', ?)",
    "speeches": "INSERT INTO speeches(id, user_id, model, voice, text, state, detail, created)"
                " VALUES (?, ?, 'qwen3-tts', 'v', 't', 'done', 'готово', ?)",
}


@pytest.mark.parametrize("table", FEED_ROWS)
def test_feed_page_keeps_rows_of_the_same_time_at_its_edge(studio, table):
    """Курсор ленты — created и id последней записи: строки с тем же временем на границе страницы не
    теряются. Один created (вкладка до выкладки) по-прежнему отвечает — строки того же времени пропускает."""
    from conftest import T0, make_user, signin
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    a, b, c, old = "a" * 32, "b" * 32, "c" * 32, "0" * 32
    for row_id, created in ((a, T0), (b, T0), (c, T0), (old, T0 - 1)):
        state.DB.execute(FEED_ROWS[table], (row_id, uid, created))
    url = f"/api/{table}?limit=2"
    page = studio.c.get(url).json()["items"]
    assert [x["id"] for x in page] == [c, b]
    last = page[-1]
    rest = studio.c.get(f"{url}&before={last['created']}&before_id={last['id']}").json()["items"]
    assert [x["id"] for x in rest] == [a, old]
    assert [x["id"] for x in studio.c.get(f"{url}&before={last['created']}").json()["items"]] == [old]
