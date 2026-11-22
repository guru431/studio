"""Панель администратора (спека, раздел 7.10): пользователи, их подключения и разделы, список разрешённых
адресов, хранилище (шара, копия базы, очередь удаления числом) и проверка копии базы (manage.verify_backup:
поднимется ли из неё студия — только числа).

Чужую историю панель не отдаёт: ответы о пользователях — только поля учётки и число подключений,
о хранилище — без пользователей, путей и объёмов чужих файлов.
Журнал — только id: логины, имена, пароли и пути туда не пишутся.
"""
import asyncio
import concurrent.futures
import fnmatch
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
from contextlib import nullcontext
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

import access
import account
import auth
import account_mail
import bridge
import chat
import cleaner_route
import clock
import connections
import email_accounts
import history
import knowledge
import manage
import mcp_client
import mcp_sources
import media
import operations
import owui
import account_profile as profile
import profiles
import push
import roles
import sections
import security
import security_log
import state
import upstream

NAME_MAX = 64

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/admin", dependencies=[Depends(auth.require_admin)])


class UserPatch(BaseModel):
    login: str | None = Field(None, max_length=64)
    name: str | None = Field(None, max_length=NAME_MAX)
    is_admin: bool | None = None
    disabled: bool | None = None
    password: str | None = Field(None, max_length=profile.PASSWORD_MAX)


class Allowlist(BaseModel):
    entries: list[str]


@router.get("/smtp")
async def smtp_settings():
    return account_mail.public_settings()


@router.get("/smtp/status")
async def smtp_status():
    return account_mail.delivery_status(account_mail.settings())


@router.put("/smtp")
async def save_smtp_settings(body: account_mail.Settings):
    return account_mail.save_settings(body)


@router.post("/smtp/check")
async def check_smtp_settings():
    return await account_mail.check()


@router.get("/cleaner")
async def cleaner_settings():
    return cleaner_route.public_settings()


@router.put("/cleaner")
async def save_cleaner_settings(body: cleaner_route.SettingsIn):
    return cleaner_route.save_settings(body)


@router.post("/cleaner/check")
async def check_cleaner_settings():
    return await cleaner_route.check()


USERS_SQL = ("SELECT u.id, u.login, u.name, u.is_admin, u.disabled, u.created, u.email,"
             " (SELECT COUNT(*) FROM connections c WHERE c.user_id = u.id) AS connections,"
             " (SELECT json_group_array(json_array(id, name)) FROM (SELECT g.id, g.name FROM access_group_members m"
             "   JOIN access_groups g ON g.id = m.group_id WHERE m.user_id = u.id ORDER BY g.id))"
             " AS access_groups FROM users u")


def _user_out(uid: int) -> dict:
    row = state.DB.execute(USERS_SQL + ' WHERE u.id = ?', (uid,)).fetchone()
    if row is None:
        raise HTTPException(404, {"code": "gone", "message": "такого пользователя нет"})
    return _user_doc(row)


def _user_doc(row) -> dict:
    groups = json.loads(row["access_groups"])
    return {"id": row["id"], "login": row["login"], "name": row["name"], "is_admin": bool(row["is_admin"]),
            "email": row["email"],
            "disabled": bool(row["disabled"]), "created": row["created"], "connections": row["connections"],
            "groups": [gid for gid, _ in groups], "group_names": [name for _, name in groups]}


def _login(text: str) -> str:
    login = text.strip().lower()
    if not re.fullmatch(auth.LOGIN_RE, login):
        raise HTTPException(422, {"code": "login", "message": "логин — 2–32 знака: латиница, цифры, . _ -"})
    return login


def _taken() -> HTTPException:
    return HTTPException(409, {"code": "taken", "message": "такой логин уже есть"})


def _name(text: str) -> str:
    name = text.strip()
    if not name:
        raise HTTPException(422, {"code": "name", "message": "нужно имя"})
    return name


def _self(admin: auth.User, uid: int) -> None:
    if admin.id == uid:
        raise HTTPException(409, {"code": "self", "message": "себя отключить, разжаловать и удалить нельзя"})


@router.get("/users")
async def list_users():
    return {"items": [_user_doc(row) for row in state.DB.execute(USERS_SQL + ' ORDER BY u.id')]}


@router.post("/users", status_code=201)
async def create_user():
    raise HTTPException(410, {"code": "invite", "message": "Создавайте пользователей приглашением на email"})


@router.post("/users/{uid}/password-link", status_code=204)
async def send_password_link(uid: int, request: Request):
    user = _user_out(uid)
    if uid == request.state.user.id:
        raise HTTPException(409, {"code": "self", "message": "свой пароль меняют в «Настройках» → «Защита»"})
    if user["disabled"] or not user["email"]:
        raise HTTPException(409, {"code": "email", "message": "Нужна активная учётка с подтверждённой почтой."})
    if not account_mail.configured(account_mail.settings()):
        raise account_mail.unset()
    email_accounts.throttle("forgot-user", str(uid), 5, 900)
    token = email_accounts.new_token(uid, "reset", user["email"], email_accounts.RESET_SEC)
    try:
        await email_accounts.recovery_mail(token, user["email"], auth.request_origin(request), admin=True)
    except HTTPException:
        email_accounts.refund("forgot-user", str(uid))
        raise
    log.info("пользователь %d: отправлена ссылка для нового пароля", uid)
    return Response(status_code=204)


@router.patch("/users/{uid}")
async def patch_user(uid: int, body: UserPatch, admin: auth.User = Depends(auth.require_admin)):
    _user_out(uid)                                            # нет такого — 404 до всяких проверок
    if body.disabled or body.is_admin is False:
        _self(admin, uid)
    if body.password is not None and uid == admin.id:
        # свой — только с текущим (POST /api/me/password): иначе получивший телефон открытым на минуту
        # сменил бы пароль и потом открывал бы запертую студию сам (спека screen-lock, раздел 4)
        raise HTTPException(409, {"code": "self", "message": "свой пароль меняют в «Настройках» → «Защита»"})
    changes = {}
    if body.login is not None:
        changes["login"] = _login(body.login)               # сессии — по id: переименование их не гасит
    if body.name is not None:
        changes["name"] = _name(body.name)
    if body.is_admin is not None:
        changes["is_admin"] = int(body.is_admin)
    if body.disabled is not None:
        changes["disabled"] = int(body.disabled)
    if body.password is not None:
        profile.check_password(body.password)
        changes["pw_hash"] = await asyncio.to_thread(security.hash_password, body.password)
        # владелец после входа увидит совет проверить сессии и сделать аварийный выход (account.py)
        changes["password_reset"] = clock.NOW()
    with operations.barrier(uid) if body.disabled else nullcontext():
        return await _apply_user_patch(uid, body, admin, changes)


async def _apply_user_patch(uid: int, body: UserPatch, admin: auth.User, changes: dict):
    state.DB.execute("BEGIN")
    try:
        was = state.DB.execute("SELECT is_admin FROM users WHERE id = ?", (uid,)).fetchone()
        if body.disabled or body.is_admin is False:
            account.keep_active_admin(uid)
        if changes:
            cols = ", ".join(f"{k} = ?" for k in changes)    # имена — из кода выше, не из запроса
            state.DB.execute(f"UPDATE users SET {cols} WHERE id = ?", (*changes.values(), uid))
        if was is not None and body.is_admin is not None and bool(was["is_admin"]) != body.is_admin:
            security_log.record(uid, "admin_grant" if body.is_admin else "admin_revoke")   # без адреса администратора
        if body.disabled or body.password is not None:
            auth.end_sessions(uid)               # себя отключить и сменить себе пароль отсюда нельзя
        if body.disabled or body.is_admin is False:
            email_accounts.revoke_invited_by(uid)
        state.DB.execute("COMMIT")
    except sqlite3.IntegrityError:
        state.DB.execute("ROLLBACK")
        raise _taken() from None
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    if body.password is not None:      # в журнал владельца: без адреса и устройства администратора
        security_log.record(uid, "password_reset")
    log.info("администратор %d: пользователь %d изменён (%s)", admin.id, uid,
             ", ".join(sorted(k if k != "pw_hash" else "password" for k in changes if k != "password_reset")) or "ничего")
    if body.disabled:
        # мосты — 4403 (вернутся после «Включить»; прежние — 4401); смена пароля их не рвёт: ключ устройства
        # самостоятелен (спека 6.2)
        await bridge.drop_user(uid, disabled=True)
        await owui.drop_user(uid, revoke=False)    # токены Open WebUI не обновлять; сессии — до «Включить»
        await history.stop_user(uid)          # отключённая учётка не держит карту и очередь шлюза
    return _user_out(uid)


@router.delete("/users/{uid}")
async def delete_user(uid: int, admin: auth.User = Depends(auth.require_admin)):
    _self(admin, uid)
    _user_out(uid)
    pending = await account.delete_user_data(uid)
    log.info("администратор %d: пользователь %d удалён вместе с историей, файлов в очереди удаления — %d",
             admin.id, uid, pending)
    return {"pending_files": pending}


#: проба шары — не дольше; ответ панели — словами
PROBE_SEC = 5
#: идущая проба (concurrent.futures.Future): пока висит, новая не начинается — зависшая CIFS держит
#: один поток, а не по потоку на каждое «Обновить»
PROBE: "concurrent.futures.Future | None" = None


def _probe(media_dir: Path) -> dict:
    """В потоке: место на шаре и копии базы (manage.backup). Каталога медиа нет (шара не
    смонтирована) — OSError наружу; каталога копий нет — копий нет; список не прочитался — неизвестно."""
    disk = shutil.disk_usage(media_dir)
    share = {"state": "ok", "free": disk.free, "total": disk.total}
    try:
        names = sorted(e.name for e in os.scandir(media_dir / "backup")
                       if fnmatch.fnmatchcase(e.name, manage.BACKUP_GLOB))
        newest = (media_dir / "backup" / names[-1]).stat().st_mtime if names else None
    except FileNotFoundError:
        names, newest = [], None
    except OSError as e:
        log.warning("хранилище: список копий базы не прочитан (%s)", type(e).__name__)
        return {"share": share, "backup": {"state": "unknown"}}
    return {"share": share, "backup": {"state": "ok", "copies": len(names), "at": newest} if names else {"state": "none"}}


def _daemon(name: str, fn, *args) -> concurrent.futures.Future:
    """Свой поток-демон, а не пул asyncio.to_thread: зависший не занимает пул службы и не держит её остановку."""
    fut = concurrent.futures.Future()

    def run():
        try:
            fut.set_result(fn(*args))
        except BaseException as e:            # noqa: BLE001 — итог (и отказ) забирает запрос
            fut.set_exception(e)

    threading.Thread(target=run, name=name, daemon=True).start()
    return fut


def _start_probe(media_dir: Path) -> concurrent.futures.Future:
    return _daemon("studio-probe", _probe, media_dir)


@router.get("/storage")
async def get_storage():
    """Хранилище без чужой истории: доступность шары и свободное место, возраст последней копии базы,
    очередь удаления — только числом (без пользователей и путей), итог последней чистки «сирот» (media.orphans:
    время и число, null — прохода не было). Шара не ответила за PROBE_SEC —
    «не отвечает», а не ожидание до таймаута Apache."""
    global PROBE
    if PROBE is None or PROBE.done():
        PROBE = _start_probe(state.CFG.media_dir)
    files, since = media.waiting()
    copy = manage.last_copy(state.DB)
    out = {"pending": {"files": files, "since": since}, "orphans": media.orphans(), "check": _last_check(),
           "copy": {"ok": copy["ok"], "at": copy["at"]} if copy else None, "stale_sec": COPY_STALE_SEC}
    probe = asyncio.wrap_future(PROBE)
    done, _ = await asyncio.wait({probe}, timeout=PROBE_SEC)
    if not done:
        # отказ, пришедший позже, забрать здесь: иначе asyncio запишет в журнал его текст — с путём
        probe.add_done_callback(lambda f: f.cancelled() or f.exception())
        log.warning("хранилище: проба не ответила за %s с", PROBE_SEC)
        return out | {"share": {"state": "stalled", "message": "хранилище не отвечает"}, "backup": {"state": "unknown"}}
    try:
        found = probe.result()
    except OSError as e:
        log.warning("хранилище: недоступно (%s)", type(e).__name__)
        return out | {"share": {"state": "down", "message": "хранилище недоступно"}, "backup": {"state": "unknown"}}
    backup = found["backup"]                  # итог пробы общий для запросов, ждавших её: не править
    if backup.get("at") is not None:
        backup = backup | {"age": clock.NOW() - backup["at"]}
    return out | {"share": found["share"], "backup": backup}


#: идущая или последняя проверка копии базы (manage.verify_backup) — одна за раз; итог живёт в памяти
#: службы до следующей проверки
CHECK: "concurrent.futures.Future | None" = None
CHECK_FAILED = {"ok": False, "problems": ["проверка копии не удалась — повторите на хосте: manage.py verify-backup"]}


def _check_backup() -> dict:
    """В потоке: проверка новейшей копии; журнал — только ok или fail, без чисел и путей."""
    try:
        result = manage.verify_backup()
    except Exception as e:                    # noqa: BLE001 — текст отказа может нести путь
        log.warning("backup-check: fail (%s)", type(e).__name__)
        return CHECK_FAILED
    log.info("backup-check: %s", "ok" if result["ok"] else "fail")
    return result


@router.post("/backup-check", status_code=202)
async def start_backup_check(admin: auth.User = Depends(auth.require_admin)):
    """Проверка копии базы потоком-демоном: долгая (копия на локальный диск, stat каждого файла шары)."""
    global CHECK
    if CHECK is not None and not CHECK.done():
        raise HTTPException(409, {"code": "running", "message": "проверка копии уже идёт"})
    loop = asyncio.get_running_loop()
    CHECK = _daemon("studio-backup-check", _check_backup)
    # итог — и в settings, как у ночной проверки (с цикла событий: state.DB — его соединение)
    CHECK.add_done_callback(lambda done: loop.call_soon_threadsafe(_record_panel_check, done))
    log.info("администратор %d: проверка копии базы", admin.id)
    return {"state": "running"}


def _record_panel_check(done: concurrent.futures.Future) -> None:
    """Итог проверки из панели — последним итогом; администратор видит его сам, уведомлять не о чем.
    Отказ самой проверки (CHECK_FAILED) — без чисел, не записывается."""
    result = None if done.cancelled() or done.exception() else done.result()
    if result is not None and result is not CHECK_FAILED:
        manage.record_check(state.DB, result, notified=True)


def _last_check() -> "dict | None":
    """Последний записанный итог проверки новейшей копии (ночной или из панели) с возрастом; None — не было."""
    last = manage.last_check(state.DB)
    return None if last is None else {k: last[k] for k in ("ok", "problems", "at", "backup_at")} | {
        "skipped": bool(last.get("skipped")), "age": clock.NOW() - last["at"]}


#: как часто служба смотрит итоги копирования и проверки, записанные другим процессом
CHECK_WATCH_SEC = 900
#: копия — раз в сутки в 04:30: последний итог копирования старше полутора суток — копия пропущена (и панель —
#: красным: значение уходит ей в `/api/admin/storage`)
COPY_STALE_SEC = 36 * 3600
#: push и ntfy администраторам на языках подписки (английские — как в en.js)
CHECK_NOTICE = {"ru": "Копия базы не поднимается: проблем — {}. Подробности — «Администрирование» → «Хранилище»",
                "en": "Database backup cannot be restored: {} issues. See Administration → Storage"}
COPY_NOTICE = {"ru": "Не удалось выполнить резервное копирование базы. Подробности — «Администрирование» → «Хранилище»",
               "en": "Database backup failed. See Administration → Storage"}
STALE_NOTICE = {"ru": f"Резервная копия базы не запускалась больше {COPY_STALE_SEC // 3600} ч."
                      " Подробности — «Администрирование» → «Хранилище»",
                "en": f"Database backup has not run for more than {COPY_STALE_SEC // 3600} h. See Administration → Storage"}


def notify_failed_check() -> int:
    """Провальный итог, о котором ещё не сообщали, — push и ntfy активным администраторам, один раз:
    отметка `notified` ставится сравнением с прочитанным значением, новый итог между чтением и отметкой
    достанется следующему проходу. Возвращает число уведомлённых."""
    notified = set()
    for key, tag in ((manage.COPY_KEY, "backup-copy"), (manage.CHECK_KEY, "backup-check")):
        row = state.DB.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        last = json.loads(row["value"]) if row else None
        if last is None or last["notified"]:
            continue
        marked = json.dumps(last | {"notified": True})
        if state.DB.execute("UPDATE settings SET value = ? WHERE key = ? AND value = ?",
                            (marked, key, row["value"])).rowcount != 1:
            continue
        notice = COPY_NOTICE if key == manage.COPY_KEY else {
            lang: text.format(last["problems"]) for lang, text in CHECK_NOTICE.items()}
        admins = [r["id"] for r in state.DB.execute("SELECT id FROM users WHERE is_admin = 1 AND disabled = 0")]
        for uid in admins:
            push.notify_later(uid, notice, "admin", tag, title=push.STUDIO)
        notified.update(admins)
        log.warning("%s: уведомлены администраторы (%d)", tag, len(admins))
    return len(notified)


def notify_stale_copy() -> int:
    """Копия не запускалась (таймер выключен, юнит упал до manage.py, блокировка не взята): итог копирования
    старше COPY_STALE_SEC — push и ntfy активным администраторам один раз, отметкой `stale` в той же записи;
    новый итог manage.py её сбрасывает. Записи нет вовсе (копий ещё не было) — молчим."""
    row = state.DB.execute("SELECT value FROM settings WHERE key = ?", (manage.COPY_KEY,)).fetchone()
    last = json.loads(row["value"]) if row else None
    if last is None or last.get("stale") or clock.NOW() - last["at"] <= COPY_STALE_SEC:
        return 0
    if state.DB.execute("UPDATE settings SET value = ? WHERE key = ? AND value = ?",
                        (json.dumps(last | {"stale": True}), manage.COPY_KEY, row["value"])).rowcount != 1:
        return 0
    admins = [r["id"] for r in state.DB.execute("SELECT id FROM users WHERE is_admin = 1 AND disabled = 0")]
    for uid in admins:
        push.notify_later(uid, STALE_NOTICE, "admin", "backup-stale", title=push.STUDIO)
    log.warning("backup-stale: уведомлены администраторы (%d)", len(admins))
    return len(admins)


async def _watch_checks() -> None:
    while True:
        try:
            notify_failed_check()
            notify_stale_copy()
        except Exception as e:                    # noqa: BLE001 — наблюдение не должно вставать
            log.warning("backup-check: сбой наблюдения (%s)", type(e).__name__)
        await clock.SLEEP(CHECK_WATCH_SEC)


def start_check_watch() -> asyncio.Task:
    """Первый проход — сразу: провал, записанный, пока служба не работала, сообщается после старта."""
    return asyncio.create_task(_watch_checks())


@router.get("/backup-check")
async def get_backup_check():
    """{state: idle|running|done, result?}: итог — только числа и слова, без путей, логинов и имён."""
    if CHECK is None:
        return {"state": "idle"}
    if not CHECK.done():
        return {"state": "running"}
    return {"state": "done", "result": CHECK.result()}


class ChatSettingsIn(BaseModel):
    tool_rounds: int = Field(ge=chat.ROUNDS_MIN, le=chat.ROUNDS_MAX)


@router.get("/chat")
async def get_chat_settings():
    """Настройки чата для всех учёток: кругов с инструментами на ответ (`chat.tool_rounds`)."""
    return {"tool_rounds": chat.tool_rounds()}


@router.put("/chat")
async def put_chat_settings(body: ChatSettingsIn, admin: auth.User = Depends(auth.require_admin)):
    chat.set_tool_rounds(body.tool_rounds)
    log.info("администратор %d: кругов инструментов в чате — %d", admin.id, body.tool_rounds)
    return {"tool_rounds": chat.tool_rounds()}


# ---- знания: источник эмбеддингов и общие базы (knowledge.py) ----------------------

class KnowledgeAccessIn(BaseModel):
    groups: "list[int] | None" = Field(None, max_length=200)     # None — всем


@router.get("/knowledge")
async def get_knowledge():
    """Источник эмбеддингов (`settings.knowledge_source`; null — только BM25), признак «источник внешний и не
    используется: Cleaner обязателен» (`knowledge.source_off`) и общие базы с группами."""
    return {"source": knowledge.source(), "source_off": knowledge.source_off(), "bases": knowledge.shared_bases()}


@router.put("/knowledge")
async def put_knowledge(body: knowledge.SourceIn, admin: auth.User = Depends(auth.require_admin)):
    knowledge.set_source(body)
    log.info("администратор %d: источник знаний %s", admin.id, "задан" if body.connection_id else "снят")
    return await get_knowledge()


@router.post("/knowledge/bases", status_code=201)
async def create_shared_base(body: knowledge.BaseIn):
    return {**knowledge.create_base(None, body.name), "groups": []}


@router.delete("/knowledge/bases/{kb_id}", status_code=204)
async def delete_shared_base(kb_id: str):
    knowledge.delete_shared(kb_id)
    return Response(status_code=204)


@router.put("/knowledge/bases/{kb_id}/access")
async def put_shared_base_access(kb_id: str, body: KnowledgeAccessIn):
    return {"groups": knowledge.set_access(kb_id, body.groups)}


@router.post("/knowledge/bases/{kb_id}/documents", status_code=201)
async def post_shared_document(kb_id: str, request: Request):
    return await knowledge.add_document(None, kb_id, request)


@router.delete("/knowledge/bases/{kb_id}/documents/{doc_id}", status_code=204)
async def delete_shared_document(kb_id: str, doc_id: str):
    knowledge.delete_shared_document(kb_id, doc_id)
    return Response(status_code=204)


@router.get("/allowlist")
async def get_allowlist():
    return {"entries": upstream.allowlist()}


@router.put("/allowlist")
async def put_allowlist(body: Allowlist, admin: auth.User = Depends(auth.require_admin)):
    entries = []
    for text in body.entries:
        try:
            entry = upstream.parse_entry(text)
        except ValueError:
            raise HTTPException(422, {"code": "entry", "message": f"«{text[:100]}» — не IP:порт и не имя:порт"
                                                                  " (имя — только для https)"}) from None
        if entry not in entries:
            entries.append(entry)
    state.DB.execute("INSERT INTO settings(key, value) VALUES ('allowlist', ?)"
                     " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (json.dumps(entries),))
    log.info("администратор %d: список разрешённых адресов — %d записей", admin.id, len(entries))
    return {"entries": upstream.allowlist()}


# ---- подключения пользователя: те же действия, что в профиле, но для `uid` ------
# паспорт (проверка подключения, checks.py) панель не показывает и проверять не даёт

def _bare(conn: dict) -> dict:
    return {k: v for k, v in conn.items() if k != "passport"}


@router.get("/users/{uid}/connections")
async def list_user_connections(uid: int):
    _user_out(uid)
    return {"items": [connections.out(c, passport=False) for c in connections.list_for(uid)]}


@router.post("/users/{uid}/connections", status_code=201)
async def create_user_connection(uid: int, body: connections.ConnIn, admin: auth.User = Depends(auth.require_admin)):
    _user_out(uid)
    conn = _bare(connections.create(uid, body))
    log.info("администратор %d: пользователю %d добавлено подключение %d", admin.id, uid, conn["id"])
    return conn


@router.patch("/users/{uid}/connections/{conn_id}")
async def patch_user_connection(uid: int, conn_id: int, body: connections.ConnPatch,
                                admin: auth.User = Depends(auth.require_admin)):
    _user_out(uid)
    conn = _bare(connections.patch(uid, conn_id, body))
    log.info("администратор %d: у пользователя %d изменено подключение %d", admin.id, uid, conn_id)
    return conn


@router.delete("/users/{uid}/connections/{conn_id}", status_code=204)
async def delete_user_connection(uid: int, conn_id: int, admin: auth.User = Depends(auth.require_admin)):
    _user_out(uid)
    connections.delete(uid, conn_id)
    log.info("администратор %d: у пользователя %d удалено подключение %d", admin.id, uid, conn_id)
    return Response(status_code=204)


# ---- разделы пользователя: те же действия, что в настройках, но для `uid` ---------

@router.get("/users/{uid}/sections")
async def get_user_sections(uid: int):
    _user_out(uid)
    return sections.all_out(uid, passport=False)


@router.post("/users/{uid}/sections/{section}/links")
async def add_user_link(uid: int, section: str, body: sections.LinkIn, admin: auth.User = Depends(auth.require_admin)):
    _user_out(uid)
    sections.link(uid, section, body.connection_id)
    log.info("администратор %d: у пользователя %d изменены разделы", admin.id, uid)
    return sections.section_out(uid, section, passport=False)


@router.delete("/users/{uid}/sections/{section}/links/{conn_id}", status_code=204)
async def remove_user_link(uid: int, section: str, conn_id: int, admin: auth.User = Depends(auth.require_admin)):
    _user_out(uid)
    sections.unlink(uid, section, conn_id)
    log.info("администратор %d: у пользователя %d изменены разделы", admin.id, uid)
    return Response(status_code=204)


@router.put("/users/{uid}/sections/{section}/links/{conn_id}")
async def put_user_models(uid: int, section: str, conn_id: int, body: sections.ModelsIn,
                          admin: auth.User = Depends(auth.require_admin)):
    _user_out(uid)
    sections.set_models(uid, section, conn_id, body.models)
    log.info("администратор %d: у пользователя %d изменены разделы", admin.id, uid)
    return sections.section_out(uid, section, passport=False)


@router.put("/users/{uid}/sections/{section}/default")
async def put_user_default(uid: int, section: str, body: sections.DefaultIn, admin: auth.User = Depends(auth.require_admin)):
    _user_out(uid)
    sections.set_default(uid, section, body.connection_id, body.model)
    log.info("администратор %d: у пользователя %d изменены разделы", admin.id, uid)
    return sections.section_out(uid, section, passport=False)


@router.get("/users/{uid}/sections/{section}/links/{conn_id}/found")
async def get_user_found(uid: int, section: str, conn_id: int):
    _user_out(uid)
    return await sections.found(uid, section, conn_id)


# NULL owner is the shared scope. All routes inherit the admin dependency above.
@router.get("/shared/connections")
async def list_shared_connections():
    return {"items": [connections.out(conn, passport=False) for conn in connections.list_for(None)]}


@router.post("/shared/connections", status_code=201)
async def create_shared_connection(body: connections.ConnIn):
    return _bare(connections.create(None, body))


@router.patch("/shared/connections/{conn_id}")
async def patch_shared_connection(conn_id: int, body: connections.ConnPatch):
    return _bare(connections.patch(None, conn_id, body))


@router.delete("/shared/connections/{conn_id}", status_code=204)
async def delete_shared_connection(conn_id: int):
    connections.delete(None, conn_id)
    return Response(status_code=204)


@router.get("/shared/sections")
async def shared_sections():
    return sections.all_out(None, passport=False)


@router.post("/shared/sections/{section}/links")
async def add_shared_link(section: str, body: sections.LinkIn):
    sections.link(None, section, body.connection_id)
    return sections.section_out(None, section, passport=False)


@router.delete("/shared/sections/{section}/links/{conn_id}", status_code=204)
async def remove_shared_link(section: str, conn_id: int):
    sections.unlink(None, section, conn_id)
    return Response(status_code=204)


@router.put("/shared/sections/{section}/links/{conn_id}")
async def put_shared_models(section: str, conn_id: int, body: sections.ModelsIn):
    sections.set_models(None, section, conn_id, body.models)
    return sections.section_out(None, section, passport=False)


@router.get("/shared/sections/{section}/links/{conn_id}/found")
async def get_shared_found(section: str, conn_id: int):
    return await sections.found(None, section, conn_id)


@router.get("/shared/mcp")
async def list_shared_mcp():
    return mcp_client.list_for(None)


@router.post("/shared/mcp", status_code=201)
async def create_shared_mcp(body: mcp_client.ServerIn):
    return mcp_client.create_for(None, body)


@router.get("/shared/mcp/{sid}")
async def get_shared_mcp(sid: int):
    return mcp_client.detail_for(None, sid)


@router.put("/shared/mcp/{sid}")
async def put_shared_mcp(sid: int, body: mcp_client.ServerIn):
    return mcp_client.update_for(None, sid, body)


@router.delete("/shared/mcp/{sid}", status_code=204)
async def delete_shared_mcp(sid: int):
    mcp_client.delete_for(None, sid)
    return Response(status_code=204)


@router.post("/shared/mcp/{sid}/check")
async def check_shared_mcp(sid: int, admin: auth.User = Depends(auth.require_admin)):
    return await mcp_client.check_for(None, sid, actor=admin.id)


# ---- группы доступа к общему (access.py): журнал — только id и числа --------------------

class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)


class MembersIn(BaseModel):
    user_ids: list[int] = Field(max_length=1000)


class AccessIn(BaseModel):
    groups: list[int] | None = Field(max_length=1000)      # null — все пользователи; [] — только администраторам


class McpAccessIn(AccessIn):
    tools: dict[str, list[int]] = Field(max_length=256)    # полный набор правил инструментов


@router.get("/groups")
async def list_groups():
    return {"items": access.groups()}


@router.post("/groups", status_code=201)
async def create_group(body: GroupIn):
    return access.create_group(body.name)


@router.patch("/groups/{gid}")
async def rename_group(gid: int, body: GroupIn):
    return access.rename_group(gid, body.name)


@router.delete("/groups/{gid}", status_code=204)
async def delete_group(gid: int):
    access.delete_group(gid)
    return Response(status_code=204)


@router.put("/groups/{gid}/members")
async def put_group_members(gid: int, body: MembersIn):
    return access.set_members(gid, body.user_ids)


def _connection_access(cid: int) -> dict:
    return {"groups": access.rule("connection", cid)}


def _mcp_access(sid: int) -> dict:
    return {"groups": access.rule("mcp", sid), "tools": access.tool_rules(sid)}


@router.get("/shared/connections/{cid}/access")
async def get_connection_access(cid: int):
    connections.get(None, cid, own=True)
    return _connection_access(cid)


@router.put("/shared/connections/{cid}/access")
async def put_connection_access(cid: int, body: AccessIn):
    access.set_rule("connection", cid, body.groups)
    return _connection_access(cid)


@router.get("/shared/mcp/{sid}/access")
async def get_mcp_access(sid: int):
    mcp_client.row(None, sid, own=True)
    return _mcp_access(sid)


@router.put("/shared/mcp/{sid}/access")
async def put_mcp_access(sid: int, body: McpAccessIn):
    # имя инструмента — как у MCP; неизвестное серверу сейчас допускается: инструмент мог временно пропасть
    if any(not mcp_client.NAME.fullmatch(tool) for tool in body.tools):
        raise HTTPException(422, {"code": "tool", "message": "неверное имя инструмента MCP"})
    with sections.transaction():                                   # доступ к серверу и инструментам — вместе
        access.set_rule("mcp", sid, body.groups)
        access.set_tool_rules(sid, body.tools)
    return _mcp_access(sid)


# ---- профиль общего подключения (profiles.py): текст и параметры видит только администратор ----

@router.get("/shared/connections/{cid}/profile")
async def get_shared_profile(cid: int):
    return profiles.get(None, cid)


@router.put("/shared/connections/{cid}/profile")
async def put_shared_profile(cid: int, body: profiles.ProfileIn):
    return profiles.put(None, cid, body)


# ---- умолчания ролей диалога (roles.py): меняют только посев новых учёток и «Добавить умолчания» ----------

@router.get("/roles")
async def list_role_defaults(lang: "roles.Lang | None" = Query(None)):
    return {"items": roles.defaults(lang)}


@router.post("/roles", status_code=201)
async def create_role_default(request: Request):
    body = await roles.read(request, roles.DefaultIn)
    return roles.create_default(body.lang, body.name, body.text)


@router.patch("/roles/{did}")
async def patch_role_default(did: int, request: Request):
    body = await roles.read(request, roles.RolePatch)
    return roles.update_default(did, body.name, body.text)


@router.delete("/roles/{did}", status_code=204)
async def delete_role_default(did: int):
    roles.delete_default(did)
    return Response(status_code=204)


# ---- политика инструментов общего MCP (mcp_tool_policy): сильнее подсказок сервера ----------------------

class ToolPolicyIn(BaseModel):
    open_world: bool | None = None                         # «интернет»: null — как у сервера
    confirm: Literal["never", "always"] | None = None      # «подтверждение»: null — как у сервера


class McpPolicyIn(BaseModel):
    tools: dict[str, ToolPolicyIn] = Field(max_length=256)  # полный набор


def _mcp_policy(sid: int) -> dict:
    return {"tools": {tool: {"open_world": None if r["open_world"] is None else bool(r["open_world"]),
                             "confirm": None if r["confirm"] is None else "never" if r["confirm"] == "never"
                             else "always"}
                      for tool, r in sorted(mcp_sources.policy(sid).items())}}


@router.get("/shared/mcp/{sid}/policy")
async def get_mcp_policy(sid: int):
    mcp_client.row(None, sid, own=True)
    return _mcp_policy(sid)


@router.put("/shared/mcp/{sid}/policy")
async def put_mcp_policy(sid: int, body: McpPolicyIn):
    mcp_client.row(None, sid, own=True)
    if any(not mcp_client.NAME.fullmatch(tool) for tool in body.tools):
        raise HTTPException(422, {"code": "tool", "message": "неверное имя инструмента MCP"})
    rows = [(sid, tool, None if rule.open_world is None else int(rule.open_world),
             {"never": "never", "always": "true"}.get(rule.confirm))
            for tool, rule in body.tools.items() if rule.open_world is not None or rule.confirm is not None]
    with sections.transaction():
        state.DB.execute("DELETE FROM mcp_tool_policy WHERE server_id = ?", (sid,))
        state.DB.executemany("INSERT INTO mcp_tool_policy(server_id, tool, open_world, confirm) VALUES (?, ?, ?, ?)",
                             rows)
    log.info("mcp %d: политика инструментов, правил %d", sid, len(rows))
    return _mcp_policy(sid)
