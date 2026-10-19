"""Команды хоста. `python manage.py create-admin` — первый администратор (логин и пароль — из vault).

Значения — только из stdin строками KEY=VALUE: аргументы видны в списке процессов и в истории
оболочки. Обязательны STUDIO_ADMIN_LOGIN и STUDIO_ADMIN_PASSWORD, необязателен
GATEWAY_KEY_OWNER_IMAGES. Повтор с тем же паролем ничего не ломает и сессий не гасит; новый
пароль заменяет хеш и закрывает все сессии администратора. Пароль в вывод не попадает.
"""
import re
import sys

import auth
import clock
import config
import db
import security

USAGE = "использование: manage.py create-admin < файл со строками KEY=VALUE"


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
    try:
        cfg = config.load()
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 2
    conn = db.connect(cfg.db_path)
    try:
        db.migrate(conn)
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
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    if argv != ["create-admin"]:
        print(USAGE, file=sys.stderr)
        return 2
    return create_admin(read_pairs(sys.stdin.read()))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
