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

verify-backup [ПУТЬ] — поднимется ли студия из копии (без пути — из новейшей): verify_backup на
временной копии в tmp_dir, рабочая база не открывается. Итог — JSON в stdout; код 0 — поднимается,
1 — нет, 2 — копии нет или неверный вызов. Та же функция — у панели (admin.py, «Проверить копию»).
"""
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

import auth
import clock
import config
import connections
import db
import sections
import security
import state

USAGE = ("использование: manage.py create-admin < файл со строками KEY=VALUE | manage.py backup"
         " | manage.py verify-backup [ПУТЬ]")
GATEWAY_NAME = "srvllm"
GATEWAY_URL = "http://10.77.1.15:4000"
DEFAULT_MODEL = "local-gemma-abl"
BACKUP_KEEP = 7
BACKUP_GLOB = "studio-????????.db"          # копии в $STUDIO_MEDIA_DIR/backup; их возраст видит панель (admin.py)
VERIFY_FILES_SEC = 600                      # verify_backup: дольше файлы не проверяются — «проверено N из M»
#: verify_backup: свободного места в tmp_dir (диск рабочей базы) — копия плюс столько (или 10 % копии)
VERIFY_RESERVE = 256 << 20


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


def _newest_backup(folder: Path) -> "Path | None":
    names = sorted(p.name for p in folder.glob(BACKUP_GLOB))
    return folder / names[-1] if names else None


def _check_copy(conn: sqlite3.Connection, cfg: config.Config, out: dict) -> None:
    """Шаги 2–5 verify_backup на временной копии; итог — в out, провалы — словами в out["problems"]."""
    problems = out["problems"]
    try:
        verdict = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    except sqlite3.DatabaseError:
        verdict = []
    out["integrity"] = "ok" if verdict == ["ok"] else "broken"
    if verdict != ["ok"]:
        problems.append("копия повреждена или это не база SQLite")
        return
    out["schema_from"] = conn.execute("PRAGMA user_version").fetchone()[0]
    if out["schema_from"] < 1:                  # migrate налил бы в неё свежую схему и «прошёл»
        problems.append("копия пуста: схемы студии в ней нет")
        return
    if out["schema_from"] > db.VERSION:
        problems.append(f"схема копии ({out['schema_from']}) новее кода ({db.VERSION})")
        return
    try:
        db.migrate(conn)
    except sqlite3.Error:
        problems.append("шаги схемы к копии не применились")
        return
    out["schema_to"] = conn.execute("PRAGMA user_version").fetchone()[0]
    out["users"] = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if not out["users"]:
        problems.append("в копии нет ни одной учётки")
    key, tokens = Fernet(cfg.data_key), out["tokens"]
    for (blob,) in conn.execute("SELECT token_enc FROM connections"):
        try:
            key.decrypt(blob)                    # значение не держится и не печатается
            tokens["ok"] += 1
        except InvalidToken:
            tokens["failed"] += 1
    if tokens["failed"]:
        problems.append(f"токены подключений не расшифрованы: {tokens['failed']} из "
                        f"{tokens['ok'] + tokens['failed']} — STUDIO_DATA_KEY не тот, что у копии")
    files = out["files"]
    rels = [row[0] for row in conn.execute("SELECT rel_path FROM media")]
    files["total"] = len(rels)
    try:
        cfg.media_dir.stat()
    except OSError:
        files["state"] = "down"
        problems.append("шара недоступна: файлы не проверены")
        return
    start = clock.NOW()                          # срок — на файлы: копия, integrity_check и migrate — не в счёт
    for rel in rels:
        if clock.NOW() - start > VERIFY_FILES_SEC:
            files["state"] = "partial"
            return
        try:
            (cfg.media_dir / rel).stat()
            files["present"] += 1
        except FileNotFoundError:
            files["missing"] += 1
        except OSError:
            files["state"] = "down"
            problems.append("шара перестала отвечать: файлы проверены не все")
            return
        files["checked"] += 1
    files["state"] = "ok"


def verify_backup(path: "str | Path | None" = None) -> dict:
    """Поднимется ли студия из копии базы: без пути — новейшая `backup/<BACKUP_GLOB>` на шаре. Копия
    копируется файлом `verify-<случайное>.db` в tmp_dir (локальный диск; файл, а не каталог: каталог
    уронил бы audio.clean_tmp при старте; свободно меньше копии с запасом VERIFY_RESERVE или 10 % — не
    копируется) и там проверяется: целостность, схема (1…db.VERSION, шаги db.STEPS проходят), учётки,
    токены текущим STUDIO_DATA_KEY, файлы на шаре (`stat`, не дольше VERIFY_FILES_SEC от начала их
    обхода). Рабочую базу не открывает, шару только читает; временная копия удаляется.
    Итог — только числа и слова, без путей, логинов и имён: его видит панель (state.CFG — из службы
    или из main)."""
    cfg, start = state.CFG, clock.NOW()
    out = {"backup": None, "at": None, "size": None, "integrity": None, "schema_from": None, "schema_to": None,
           "users": None, "tokens": {"ok": 0, "failed": 0},
           "files": {"state": "skipped", "present": 0, "missing": 0, "checked": 0, "total": 0},
           "ok": False, "took": 0, "problems": []}
    try:
        source = Path(path) if path else _newest_backup(cfg.media_dir / "backup")
        found = source is not None and source.is_file()
    except OSError:
        found = False
    if not found:
        out["problems"].append("копии базы нет" if path else "копий базы нет (или шара недоступна)")
        return out
    info = source.stat()
    out.update(backup=source.name, at=info.st_mtime, size=info.st_size)
    cfg.tmp_dir.mkdir(parents=True, exist_ok=True)
    # tmp_dir — на диске рабочей базы: копия, забившая его до ENOSPC, уронила бы запись студии до уборки
    if shutil.disk_usage(cfg.tmp_dir).free < info.st_size + max(VERIFY_RESERVE, info.st_size // 10):
        out["problems"].append("мало места на диске базы: копию проверить негде")
        out["took"] = round(clock.NOW() - start, 1)
        return out
    tmp = cfg.tmp_dir / f"verify-{secrets.token_hex(8)}.db"
    try:
        shutil.copyfile(source, tmp)
        conn = sqlite3.connect(tmp, isolation_level=None)
        try:
            _check_copy(conn, cfg, out)
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as e:
        out["problems"].append(f"копию не удалось прочитать ({type(e).__name__})")
    finally:
        for extra in ("", "-journal", "-wal", "-shm"):
            Path(f"{tmp}{extra}").unlink(missing_ok=True)
    out["ok"] = not out["problems"]
    out["took"] = round(clock.NOW() - start, 1)
    return out


def verify_command(argv: list[str]) -> int:
    """verify-backup [ПУТЬ]: итог JSON в stdout; 0 — поднимается, 1 — нет, 2 — копии нет."""
    try:
        state.CFG = config.load()
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 2
    out = verify_backup(argv[0] if argv else None)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out["ok"] else 2 if out["backup"] is None else 1


def main(argv: list[str]) -> int:
    if argv == ["create-admin"]:
        return create_admin(read_pairs(sys.stdin.read()))
    if argv == ["backup"]:
        return backup()
    if argv[:1] == ["verify-backup"] and len(argv) <= 2:
        return verify_command(argv[1:])
    print(USAGE, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
