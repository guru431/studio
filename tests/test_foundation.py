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


def test_migrate_creates_schema_once_and_seeds_the_gateway(env):
    conn = db.connect(env.db_path); db.migrate(conn); db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION == 6
    assert json.loads(conn.execute("SELECT value FROM settings WHERE key='allowlist'").fetchone()[0]) == ["10.77.1.15:4000"]
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_migrate_brings_a_version_1_base_up_keeping_its_rows(env):
    """База прода — версии 1: шаг добавляет столбцы, строки и настройки остаются."""
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript("ALTER TABLE images DROP COLUMN mask_media_id; ALTER TABLE messages DROP COLUMN alive_at;"
                       " ALTER TABLE images DROP COLUMN chain; ALTER TABLE videos DROP COLUMN chain;"
                       f" {DROP_V4} {DROP_V5} {DROP_V6} PRAGMA user_version = 1;")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    conn.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, state, detail, created)"
                 " VALUES ('i1', 1, 'edit', 'lustify', 'p', '64x48', 1, 'done', 'готово', 0)")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    assert conn.execute("SELECT id, mask_media_id FROM images").fetchone()[:] == ("i1", None)
    assert "alive_at" in [r[1] for r in conn.execute("PRAGMA table_info(messages)")]


def test_migrate_brings_a_version_2_base_up_keeping_its_rows(env):
    """База прода — версии 2: шаг добавляет столбцы цепочки, строки остаются без неё."""
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript("ALTER TABLE images DROP COLUMN chain; ALTER TABLE videos DROP COLUMN chain;"
                       f" {DROP_V4} {DROP_V5} {DROP_V6} PRAGMA user_version = 2;")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    conn.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, state, detail, created)"
                 " VALUES ('i1', 1, 'edit', 'lustify', 'p', '64x48', 1, 'done', 'готово', 0)")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    assert conn.execute("SELECT id, chain IS NULL FROM images").fetchone()[:] == ("i1", 1)
    assert "chain" in [r[1] for r in conn.execute("PRAGMA table_info(videos)")]


def test_migrate_links_a_version_3_base_to_every_section(env):
    """Шаг 4: srvllm — во всех шести разделах с перечнем, чат — с моделью пользователя; openai — в чате."""
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript(f"{DROP_V4} {DROP_V5} {DROP_V6} PRAGMA user_version = 3;")
    ins = ("INSERT INTO connections(id, user_id, name, kind, base_url, token_enc, sections, created)"
           " VALUES (?, 1, 'c', ?, ?, x'00', ?, 0)")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    conn.execute(ins, (7, "srvllm", "http://10.77.1.15:4000", '["chat","photo","video"]'))
    conn.execute(ins, (8, "openai", "https://cloud.test/v1", '["chat"]'))
    conn.execute("UPDATE users SET default_connection_id = 7, default_model = 'gemma4-abl'")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    links = {(r[0], r[1]): json.loads(r[2]) for r in conn.execute("SELECT connection_id, section, models FROM section_links")}
    assert links == {(7, "chat"): ["gemma4-abl"], (7, "photo"): ["lustify", "flux"], (7, "video"): ["wan"],
                     (7, "transcribe"): ["whisperx"], (7, "speech"): ["qwen3-tts"], (7, "dictation"): ["gigaam", "whisperx"],
                     (8, "chat"): []}
    defaults = dict(((r[0]), (r[1], r[2])) for r in conn.execute("SELECT section, connection_id, model FROM section_defaults"))
    assert defaults == {"chat": (7, "gemma4-abl"), "photo": (7, "lustify"), "video": (7, "wan"),
                        "transcribe": (7, "whisperx"), "speech": (7, "qwen3-tts"), "dictation": (7, "gigaam")}


def test_deleting_a_user_cascades(env):
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    conn.execute("INSERT INTO sessions VALUES ('d', 1, 'c', 1, 0)")
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
    conn.executescript(f"{DROP_V5} {DROP_V6} PRAGMA user_version = 4;")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"transcripts", "voices", "speeches"} <= tables
    assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1


def test_migrate_gives_a_version_5_base_an_empty_view_choice(env):
    """Шаг 6: вид ленты — в учётке; у прежних учёток выбора нет — разделы берут свой вид по умолчанию."""
    conn = db.connect(env.db_path); db.migrate(conn)
    conn.executescript(f"{DROP_V6} PRAGMA user_version = 5;")
    conn.execute("INSERT INTO users(id, login, name, pw_hash, created) VALUES (1, 'u', 'U', 'h', 0)")
    db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 6
    assert conn.execute("SELECT views FROM users").fetchone()[0] == "{}"
