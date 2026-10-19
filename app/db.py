"""Соединение SQLite и схема базы (версия 1)."""
import sqlite3
from pathlib import Path

VERSION = 1

SCHEMA = """
CREATE TABLE users(id INTEGER PRIMARY KEY, login TEXT NOT NULL UNIQUE, name TEXT NOT NULL, pw_hash TEXT NOT NULL,
      is_admin INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0,
      theme TEXT NOT NULL DEFAULT 'auto', default_connection_id INTEGER, default_model TEXT,
      created REAL NOT NULL);
CREATE TABLE sessions(digest TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
         csrf TEXT NOT NULL, expires REAL NOT NULL, renewed REAL NOT NULL);
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE connections(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
            name TEXT NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('openai','srvllm')),
            base_url TEXT NOT NULL, token_enc BLOB NOT NULL, sections TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE media(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
      rel_path TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, has_thumb INTEGER NOT NULL,
      download_name TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE chats(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE, title TEXT,
      connection_id INTEGER, model TEXT, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE messages(id TEXT PRIMARY KEY, chat_id TEXT NOT NULL REFERENCES chats ON DELETE CASCADE,
         role TEXT NOT NULL CHECK (role IN ('user','assistant')), content TEXT NOT NULL,
         status TEXT NOT NULL CHECK (status IN ('done','streaming','stopped','error')),
         via TEXT NOT NULL CHECK (via IN ('server','browser')), model TEXT, created REAL NOT NULL);
CREATE TABLE attachments(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
            message_id TEXT REFERENCES messages ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK (kind IN ('document','image')), name TEXT NOT NULL,
            media_id TEXT NOT NULL, text TEXT, chars INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE TABLE images(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       kind TEXT NOT NULL CHECK (kind IN ('generation','edit')), model TEXT NOT NULL, prompt TEXT NOT NULL,
       size TEXT NOT NULL, seed INTEGER NOT NULL, strength REAL, source_media_id TEXT, media_id TEXT,
       state TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL, finished REAL);
CREATE TABLE videos(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       connection_id INTEGER NOT NULL, gateway_id TEXT, source_media_id TEXT NOT NULL, media_id TEXT,
       prompt TEXT NOT NULL, prompt_source TEXT, seconds INTEGER NOT NULL, quality TEXT NOT NULL,
       smooth INTEGER NOT NULL, seed INTEGER, state TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
       detail TEXT NOT NULL, created REAL NOT NULL, finished REAL);
CREATE INDEX images_user_created ON images(user_id, created);
CREATE INDEX videos_user_created ON videos(user_id, created);
CREATE INDEX chats_user_updated ON chats(user_id, updated);
CREATE INDEX messages_chat_created ON messages(chat_id, created);
INSERT INTO settings(key, value) VALUES ('allowlist', '["10.77.1.15:4000"]');
"""


def connect(path: Path) -> sqlite3.Connection:
    """Автокоммит (isolation_level=None): транзакции — явным BEGIN."""
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA user_version").fetchone()[0] >= VERSION:
        return
    # одной транзакцией: обрыв посреди не оставит полсхемы с user_version = 0
    conn.executescript(f"BEGIN;\n{SCHEMA}\nPRAGMA user_version = {VERSION};\nCOMMIT;")
