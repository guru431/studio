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


def test_migrate_creates_schema_once_and_seeds_the_gateway(env):
    conn = db.connect(env.db_path); db.migrate(conn); db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert json.loads(conn.execute("SELECT value FROM settings WHERE key='allowlist'").fetchone()[0]) == ["10.77.1.15:4000"]
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


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
