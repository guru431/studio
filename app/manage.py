"""Команды хоста. `python manage.py create-admin` — первый администратор (логин и пароль — из vault);
`python manage.py backup` — копия базы на шару (таймер deploy/studio-backup.timer, раз в сутки).

Значения — только из stdin строками KEY=VALUE: аргументы видны в списке процессов и в истории
оболочки. Обязательны STUDIO_ADMIN_LOGIN и STUDIO_ADMIN_PASSWORD, необязателен
GATEWAY_KEY_OWNER_IMAGES. Повтор с тем же паролем ничего не ломает и сессий не гасит; новый
пароль заменяет хеш и закрывает все сессии администратора. Пароль в вывод не попадает.

С GATEWAY_KEY_OWNER_IMAGES у администратора появляется подключение «srvllm» (шлюз srvllm, все
разделы); при создании оно становится подключением чата по умолчанию с моделью DEFAULT_MODEL.
Повтор обновляет ключ того же подключения, второго не заводит. Ключ в вывод не попадает.

backup — согласованная копия SQLite (`Connection.backup`, служба при этом работает) сначала во
временный файл рядом с базой (локальный диск: SQLite на CIFS не работает), затем копией байтов
на шару под временным именем и `os.replace` в `$STUDIO_MEDIA_DIR/backup/studio-YYYYMMDD.db`
(дата — UTC). Хранятся BACKUP_KEEP последних копий; временный файл на шаре при отказе убирается,
забытые прежними сбоями — вместе со старыми копиями.
"""
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

import auth
import clock
import config
import connections
import db
import sections
import security
import state

USAGE = "использование: manage.py create-admin < файл со строками KEY=VALUE | manage.py backup"
GATEWAY_NAME = "srvllm"
GATEWAY_URL = "http://10.77.1.15:4000"
DEFAULT_MODEL = "local-gemma-abl"
BACKUP_KEEP = 7
BACKUP_GLOB = "studio-????????.db"          # копии в $STUDIO_MEDIA_DIR/backup; их возраст видит панель (admin.py)


def read_pairs(text: str) -> dict[str, str]:
    pairs = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        pairs[key.strip()] = value
    return pairs


def create_admin(values: dict[str, str]) -> int:
    login = values.get("STUDIO_ADMIN_LOGIN", "").strip().lower()
    password = values.get("STUDIO_ADMIN_PASSWORD", "")
    if not login or not password:
        print("нужны STUDIO_ADMIN_LOGIN и STUDIO_ADMIN_PASSWORD", file=sys.stderr)
        return 2
    if not re.fullmatch(auth.LOGIN_RE, login):
        print(f"логин должен подходить под {auth.LOGIN_RE}", file=sys.stderr)
        return 2
    if len(password) < security.PASSWORD_MIN:
        print(f"пароль короче {security.PASSWORD_MIN} символов", file=sys.stderr)
        return 2
    gateway_key = values.get("GATEWAY_KEY_OWNER_IMAGES", "").strip()
    if gateway_key and not re.fullmatch(connections.TOKEN_RE, gateway_key):
        print("GATEWAY_KEY_OWNER_IMAGES — 1–512 печатных знаков ASCII", file=sys.stderr)
        return 2
    try:
        cfg = config.load()
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 2
    state.CFG = cfg                  # security.encrypt берёт ключ данных отсюда
    conn = db.connect(cfg.db_path)
    try:
        db.migrate(conn)
        code = _ensure_admin(conn, login, password)
        if gateway_key:
            _ensure_gateway(conn, login, gateway_key)
        return code
    finally:
        conn.close()


def _ensure_gateway(conn, login: str, key: str) -> None:
    """Подключение «srvllm» во всех разделах (чат — с DEFAULT_MODEL); связи ставит sections.py на
    этом же соединении (state.DB — на время вызова)."""
    uid = conn.execute("SELECT id FROM users WHERE login = ?", (login,)).fetchone()["id"]
    row = conn.execute("SELECT id FROM connections WHERE user_id = ? AND kind = 'srvllm' AND name = ?",
                       (uid, GATEWAY_NAME)).fetchone()
    before, state.DB = state.DB, conn
    try:
        with sections.transaction():
            if row is None:
                cid = conn.execute(
                    "INSERT INTO connections(user_id, name, kind, base_url, token_enc, sections, created, server_only)"
                    " VALUES (?, ?, 'srvllm', ?, ?, '[]', ?, 1)",
                    (uid, GATEWAY_NAME, GATEWAY_URL, security.encrypt(key), clock.NOW())).lastrowid
                sections.set_sections(uid, cid, list(sections.SECTIONS))
            else:
                conn.execute("UPDATE connections SET token_enc = ?, server_only = 1 WHERE id = ?",
                             (security.encrypt(key), row["id"]))
    finally:
        state.DB = before
    print(f"подключение {GATEWAY_NAME}: " + ("создано" if row is None else "ключ обновлён"))


def _ensure_admin(conn, login: str, password: str) -> int:
    row = conn.execute("SELECT id, pw_hash FROM users WHERE login = ?", (login,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO users(login, name, pw_hash, is_admin, created) VALUES (?, ?, ?, 1, ?)",
                     (login, login, security.hash_password(password), clock.NOW()))
        print(f"администратор {login}: создан")
        return 0
    changed = not security.verify_password(password, row["pw_hash"])
    conn.execute("BEGIN")
    try:
        conn.execute("UPDATE users SET is_admin = 1, disabled = 0 WHERE id = ?", (row["id"],))
        if changed:
            conn.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (security.hash_password(password), row["id"]))
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    print(f"администратор {login}: " + ("пароль сменён, сессии закрыты" if changed else "уже есть, пароль прежний"))
    return 0


def backup() -> int:
    try:
        cfg = config.load()
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 2
    if not cfg.db_path.is_file():   # sqlite3.connect завёл бы пустую базу
        print(f"нет базы {cfg.db_path}", file=sys.stderr)
        return 2
    day = datetime.fromtimestamp(clock.NOW(), timezone.utc)
    folder = cfg.media_dir / "backup"
    target = folder / f"studio-{day:%Y%m%d}.db"
    local = cfg.db_path.with_name(f".{target.name}.part")
    shared = folder / f".{target.name}.part"
    try:
        local.unlink(missing_ok=True)
        src, dst = sqlite3.connect(cfg.db_path), sqlite3.connect(local)
        try:
            src.backup(dst)
            # копия — один файл без -wal/-shm: открывать её с шары и переносить проще
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
            src.close()
        folder.mkdir(exist_ok=True)
        shutil.copyfile(local, shared)
        os.replace(shared, target)
    except (OSError, sqlite3.Error) as e:     # шара недоступна: копии нет, база не тронута
        try:
            shared.unlink(missing_ok=True)    # недописанная копия на шаре не остаётся
        except OSError:
            pass
        print(f"копия базы не сделана: {e}", file=sys.stderr)
        return 1
    finally:
        local.unlink(missing_ok=True)
    print(f"копия базы: {target}")
    try:
        # и .part суток, что упали, пока его уборки не было (своя копия к этому месту уже на месте)
        stale = sorted(folder.glob(BACKUP_GLOB))[:-BACKUP_KEEP] + list(folder.glob(".studio-????????.db.part"))
        for old in stale:
            old.unlink()
    except OSError as e:
        print(f"старые копии не убраны: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str]) -> int:
    if argv == ["create-admin"]:
        return create_admin(read_pairs(sys.stdin.read()))
    if argv == ["backup"]:
        return backup()
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
