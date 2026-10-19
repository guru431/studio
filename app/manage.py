"""Команды хоста. `python manage.py create-admin` — первый администратор (логин и пароль — из vault).

Значения — только из stdin строками KEY=VALUE: аргументы видны в списке процессов и в истории
оболочки. Обязательны STUDIO_ADMIN_LOGIN и STUDIO_ADMIN_PASSWORD, необязателен
GATEWAY_KEY_OWNER_IMAGES. Повтор с тем же паролем ничего не ломает и сессий не гасит; новый
пароль заменяет хеш и закрывает все сессии администратора. Пароль в вывод не попадает.

С GATEWAY_KEY_OWNER_IMAGES у администратора появляется подключение «srvllm» (шлюз srvllm, все
разделы); при создании оно становится подключением чата по умолчанию с моделью DEFAULT_MODEL.
Повтор обновляет ключ того же подключения, второго не заводит. Ключ в вывод не попадает.
"""
import json
import re
import sys

import auth
import clock
import config
import connections
import db
import security
import state

USAGE = "использование: manage.py create-admin < файл со строками KEY=VALUE"
GATEWAY_NAME = "srvllm"
GATEWAY_URL = "http://10.77.1.15:4000"
DEFAULT_MODEL = "local-gemma-abl"


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
    uid = conn.execute("SELECT id FROM users WHERE login = ?", (login,)).fetchone()["id"]
    row = conn.execute("SELECT id FROM connections WHERE user_id = ? AND kind = 'srvllm' AND name = ?",
                       (uid, GATEWAY_NAME)).fetchone()
    conn.execute("BEGIN")
    try:
        if row is None:
            cid = conn.execute(
                "INSERT INTO connections(user_id, name, kind, base_url, token_enc, sections, created, server_only)"
                " VALUES (?, ?, 'srvllm', ?, ?, ?, ?, 1)",
                (uid, GATEWAY_NAME, GATEWAY_URL, security.encrypt(key),
                 json.dumps(sorted(connections.KINDS["srvllm"])), clock.NOW())).lastrowid
            conn.execute("UPDATE users SET default_connection_id = ?, default_model = ? WHERE id = ?",
                         (cid, DEFAULT_MODEL, uid))
        else:
            conn.execute("UPDATE connections SET token_enc = ?, server_only = 1 WHERE id = ?",
                         (security.encrypt(key), row["id"]))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
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


def main(argv: list[str]) -> int:
    if argv != ["create-admin"]:
        print(USAGE, file=sys.stderr)
        return 2
    return create_admin(read_pairs(sys.stdin.read()))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
