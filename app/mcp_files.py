"""Файлы между чатом и MCP (спека 2026-11-21-studio-source-foundation-design, §5 и §7a п. 1, 3, 4).

1. Вложения и медиа диалога получают только серверы действующего набора диалога, у чьего `artifact_upload` есть
   пометка `_meta["studio/chat-files"] = {"max_bytes": N}` (`mcp_sources.chat_files`), и только с согласием
   пользователя на пару «диалог, сервер»: `chats.mcp_file_consent` `{sid: stamp}` (`mcp_sources.stamp` шифра
   ключа). Новый сервер, сменённый адрес или ключ требуют нового согласия; «Не передавать» хранит `REFUSED` +
   stamp — запрос больше не повторяется, пока сервер тот же. Без согласия `sync` ничего не грузит: модель
   получает имена файлов «есть, но не переданы», лента — шаг с `consent` (кнопки на странице).
2. `sync` в `chat.StreamTask._read` после `discover`: PER_TURN новейших вложений (кроме отклонённых моделью
   ходов — `chat._refused_turns`) и PER_TURN новейших медиа диалога; свежая загрузка с тем же stamp моложе
   UPLOAD_TTL_SEC (`mcp_uploads`, `mcp_media_uploads`) не повторяется; на ход — не больше SYNC_BYTES и SYNC_SEC,
   остальное — заметка модели. Перед каждой загрузкой — заново доступ (группы), включённость, stamp, набор
   диалога (`mcp_off`), `mcp_tools_off` и согласие. Сбой — шаг-ошибка, ход идёт.
3. Звук и видео — `POST /api/chats/{chat_id}/media` (таблица `chat_media`, файл — медиа учётки): до приёма байтов
   нужен сервер с пометкой и согласием, размер — не больше наибольшего его `max_bytes`; серверу уходит файл,
   который вмещает его предел. Модели — только имя.
4. Файлы результатов (`result_files`) — в шаге `{sid, stamp, files: [{aid, mime, name}]}`; странице — только имя
   и тип (`web.public`). Скачивание сверяет диалог, шаг, доступ к серверу и stamp: адрес или ключ сменились — 404.

Журнал — только id и числа: ни имён файлов, ни путей на сервере.
"""
import asyncio
import json
import logging
import uuid
from urllib.parse import unquote

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import auth
import chat
import chat_system
import clock
import documents
import forms
import mcp_client
import mcp_sections
import mcp_sources
import media
import operations
import state

log = logging.getLogger("studio")
router = APIRouter(prefix="/api")

UPLOAD_TTL_SEC = 72000                       # загрузка на сервер считается живой столько
PER_TURN = 8                                 # новейших вложений (и отдельно медиа) диалога на ход
SYNC_BYTES = 256 << 20                       # байтов загрузок на ход, все серверы вместе
SYNC_SEC = 120                               # загрузки хода начинаются не позже стольких секунд от первой
RESULT_FILES = 16                            # файлов результата на шаг
NAME_MAX = 255
REFUSED = "refused:"                         # «Не передавать»: `chats.mcp_file_consent[sid]` = REFUSED + stamp
UPLOAD = "artifact_upload"

NO_CONSENT = "Файлы диалога не переданы серверу «{0}» без согласия пользователя"
NOT_SENT = "Файлы диалога есть, но не переданы серверу «{0}» без согласия пользователя: {1}."
ON_SERVER = "Файлы диалога на сервере «{0}»:"
TOO_BIG = "файл «{0}» не передан: больше предела сервера ({1} МиБ)"
LEFT = "файл «{0}» не передан: предел хода"
CHANGED = "файл «{0}» не передан: доступ к серверу или настройки диалога изменились"
FAILED = "файл «{0}» не передан: сбой загрузки"
MISSING = "файл «{0}» не передан: его нет в хранилище"
NO_SERVER = ("Звук и видео можно приложить, когда в диалоге включён MCP, принимающий файлы чата, и вы разрешили "
             "передать ему файлы")
MEDIA_BIG = "Файл больше {0} МиБ — предела MCP этого диалога"
NOT_MEDIA = "Приложите звук или видео"
GONE = mcp_sections.GONE_FILE


def _mib(size: int) -> int:
    return max(size >> 20, 1)


def _consent(row) -> dict:
    data = json.loads(row["mcp_file_consent"] or "{}")
    return data if isinstance(data, dict) else {}


def consent_state(uid: int, row) -> dict:
    """Согласие диалога для страницы: {sid: "allowed" | "refused"} — только для серверов учётки с тем же stamp
    (сменился адрес или ключ — согласия нет)."""
    data = _consent(row)
    if not data:
        return {}
    out = {}
    for item in mcp_client.servers(uid):
        value, current = data.get(str(item["id"])), mcp_sources.stamp(item["secret_enc"])
        if value == current:
            out[str(item["id"])] = "allowed"
        elif value == REFUSED + current:
            out[str(item["id"])] = "refused"
    return out


def media_of(chat_id: str) -> list[dict]:
    """Звук и видео диалога для страницы (без id файла учётки)."""
    return [{"id": r["id"], "name": r["name"], "mime": r["mime"], "size": r["size"], "created": r["created"]}
            for r in state.DB.execute("SELECT id, name, mime, size, created FROM chat_media WHERE chat_id = ?"
                                      " ORDER BY created, rowid", (chat_id,))]


def _candidates(task) -> list[dict]:
    """Файлы диалога для серверов: PER_TURN новейших вложений (кроме ходов, отклонённых моделью хода) и PER_TURN
    новейших медиа диалога; строки без файла учётки пропускаются."""
    refused = chat._refused_turns(chat._messages(task.chat_id), task.model) - {task.user_message_id}
    files = []
    for r in state.DB.execute(
            "SELECT a.id, a.name, a.media_id, a.message_id, m.mime, m.size FROM attachments a"
            " JOIN messages g ON g.id = a.message_id JOIN media m ON m.id = a.media_id AND m.user_id = a.user_id"
            " WHERE g.chat_id = ? AND a.user_id = ? ORDER BY a.created DESC, a.rowid DESC",
            (task.chat_id, task.user_id)):
        if r["message_id"] in refused:
            continue
        files.append({"table": "mcp_uploads", "key": "attachment_id", "id": r["id"], "name": r["name"],
                      "media_id": r["media_id"], "mime": r["mime"], "size": r["size"]})
        if len(files) >= PER_TURN:
            break
    for r in state.DB.execute(
            "SELECT c.id, c.name, c.media_id, c.mime, c.size FROM chat_media c JOIN media m ON m.id = c.media_id"
            " AND m.user_id = c.user_id WHERE c.chat_id = ? AND c.user_id = ? ORDER BY c.created DESC, c.rowid DESC"
            " LIMIT ?", (task.chat_id, task.user_id, PER_TURN)):
        files.append({"table": "mcp_media_uploads", "key": "chat_media_id", "id": r["id"], "name": r["name"],
                      "media_id": r["media_id"], "mime": r["mime"], "size": r["size"]})
    return files


def _fresh(f: dict, sid: int, stamp: str):
    """Живая загрузка файла `f` на сервер `sid` с тем же stamp (моложе UPLOAD_TTL_SEC) или None."""
    return state.DB.execute(  # имена таблицы и столбца — из _candidates, не из запроса
        f"SELECT artifact_id, path FROM {f['table']} WHERE {f['key']} = ? AND server_id = ? AND stamp = ?"
        " AND created > ?", (f["id"], sid, stamp, clock.NOW() - UPLOAD_TTL_SEC)).fetchone()


def _allowed(task, sid: int, stamp: str) -> bool:
    """Перед загрузкой — заново: сервер открыт учётке и включён, его адрес и ключ прежние, он в действующем наборе
    диалога, `artifact_upload` не выключен в диалоге, согласие на тот же stamp."""
    try:
        item = mcp_client.row(task.user_id, sid)
    except HTTPException:
        return False
    row = state.DB.execute("SELECT * FROM chats WHERE id = ? AND user_id = ?", (task.chat_id, task.user_id)).fetchone()
    if row is None or not item["enabled"] or mcp_sources.stamp(item["secret_enc"]) != stamp:
        return False
    off, _ = mcp_sources.tool_state(row["mcp_tools_off"])
    return (sid in mcp_sources.dialog_servers(task.user_id, row, task.conn) and UPLOAD not in off.get(sid, ())
            and _consent(row).get(str(sid)) == stamp)


def _line(name: str, path: str, aid: str) -> str:
    return f"- {chat_system.clean(name, 200)} — path {path or '—'}, artifact {aid}"


def _read(path) -> bytes:
    return path.read_bytes()


async def sync(task, servers) -> tuple[list[str], list[dict]]:
    """Файлы диалога на серверы хода `task` (`chat.StreamTask`); `servers` — (строка, сессия, каталог) ответивших
    серверов (`mcp_client.Tools.found`). Итог — строки для модели (кусок 5, `chat_system.mcp_block`) и шаги ленты
    (ошибки загрузки; запрос согласия — шаг с `consent: {server_id, files}`)."""
    targets = [(item, session, found, limit) for item, session, found in servers
               if (limit := mcp_sources.chat_files(found)) is not None]
    if not targets:
        return [], []
    row = state.DB.execute("SELECT * FROM chats WHERE id = ? AND user_id = ?", (task.chat_id, task.user_id)).fetchone()
    files = _candidates(task) if row is not None else []
    if not files:
        return [], []
    consent, notes, steps = _consent(row), [], []
    started, sent = clock.NOW(), 0
    for item, session, found, limit in targets:
        sid, server, stamp = item["id"], item["name"], mcp_sources.stamp(item["secret_enc"])
        names = [f["name"] for f in files]
        if consent.get(str(sid)) != stamp:
            notes.append(NOT_SENT.format(chat_system.clean(server, 200),
                                         ", ".join(chat_system.clean(n, 200) for n in names)))
            if consent.get(str(sid)) != REFUSED + stamp:
                steps.append({"kind": "mcp", "server": server, "tool": UPLOAD, "error": NO_CONSENT.format(server),
                              "consent": {"server_id": sid, "files": names}})
            continue
        # имя файла — только серверу, чья схема `artifact_upload` его принимает
        schema = next(t for t in found.tools if t.get("name") == UPLOAD).get("inputSchema")
        named = isinstance(schema, dict) and isinstance(schema.get("properties"), dict) and "name" in schema["properties"]
        lines, broken, given = [], False, 0
        for f in files:
            if f["size"] > limit:
                lines.append("- " + TOO_BIG.format(chat_system.clean(f["name"], 200), _mib(limit)))
                continue
            kept = _fresh(f, sid, stamp)
            if kept is not None:
                lines.append(_line(f["name"], kept["path"], kept["artifact_id"]))
                given += 1
                continue
            if broken:
                lines.append("- " + FAILED.format(chat_system.clean(f["name"], 200)))
                continue
            if sent + f["size"] > SYNC_BYTES or clock.NOW() - started > SYNC_SEC:
                lines.append("- " + LEFT.format(chat_system.clean(f["name"], 200)))
                continue
            if not _allowed(task, sid, stamp):
                lines.append("- " + CHANGED.format(chat_system.clean(f["name"], 200)))
                continue
            try:
                data = await asyncio.to_thread(_read, media.path(task.user_id, f["media_id"]))
            except (HTTPException, FileNotFoundError):
                lines.append("- " + MISSING.format(chat_system.clean(f["name"], 200)))
                continue
            except OSError:
                steps.append({"kind": "mcp", "server": server, "tool": UPLOAD, "error": chat.STORAGE})
                broken = True
                lines.append("- " + FAILED.format(chat_system.clean(f["name"], 200)))
                continue
            try:
                await session.ensure()
                done = await mcp_sections.upload_artifact(session, data, f["mime"], f["name"] if named else None)
            except HTTPException as exc:
                steps.append({"kind": "mcp", "server": server, "tool": UPLOAD, "error": exc.detail["message"]})
                broken = True
                lines.append("- " + FAILED.format(chat_system.clean(f["name"], 200)))
                continue
            sent += len(data)
            state.DB.execute(f"INSERT OR REPLACE INTO {f['table']}({f['key']}, server_id, artifact_id, path, stamp,"
                             " created) VALUES (?, ?, ?, ?, ?, ?)",
                             (f["id"], sid, done["id"], done["path"], stamp, clock.NOW()))
            lines.append(_line(f["name"], done["path"], done["id"]))
            given += 1
        notes.append("\n".join([ON_SERVER.format(chat_system.clean(server, 200)), *lines]))
        log.info("чат %s: MCP %d — файлов %d", task.chat_id[:8], sid, given)
    return notes, steps


def result_files(result: dict) -> list[dict]:
    """Файлы результата инструмента: `structuredContent.artifacts` или `structuredContent.result.artifacts` по
    проверкам `mcp_sections.present` (id `art_…`, путь передачи по контракту), не больше RESULT_FILES."""
    data = result.get("structuredContent")
    if not isinstance(data, dict):
        return []
    artifacts = data.get("artifacts")
    if not isinstance(artifacts, list) and isinstance(data.get("result"), dict):
        artifacts = data["result"].get("artifacts")
    if not isinstance(artifacts, list):
        return []
    out = []
    for artifact in artifacts[:128]:
        if not isinstance(artifact, dict):
            continue
        aid, mime = artifact.get("id"), artifact.get("mime_type")
        if (not isinstance(aid, str) or not mcp_sections.ARTIFACT_ID.fullmatch(aid) or not isinstance(mime, str)
                or artifact.get("download_path") != f"/artifacts/{aid}/content"):
            continue
        out.append({"aid": aid, "mime": mime if mime in mcp_sections.TICKET_MIME else "application/octet-stream",
                    "name": mcp_sections.clean_text(artifact.get("name"), 120)})
        if len(out) >= RESULT_FILES:
            break
    return out


# ---- API -------------------------------------------------------------------------------------------------

class ConsentIn(BaseModel):
    server_id: int
    allow: bool


@router.post("/chats/{chat_id}/mcp-consent")
async def set_consent(chat_id: str, body: ConsentIn, request: Request):
    """«Передать» / «Не передавать» файлы диалога серверу: согласие — на текущий stamp сервера. Чужой, закрытый
    группами или выключенный сервер — 404."""
    uid = request.state.user.id
    row = chat._chat_row(uid, chat_id)
    item = mcp_client.row(uid, body.server_id)
    if not item["enabled"]:
        raise mcp_client.refused("Подключение MCP не найдено", "not_found", 404)
    stamp = mcp_sources.stamp(item["secret_enc"])
    data = _consent(row)
    data[str(body.server_id)] = stamp if body.allow else REFUSED + stamp
    state.DB.execute("UPDATE chats SET mcp_file_consent = ? WHERE id = ?", (json.dumps(data, sort_keys=True), chat_id))
    log.info("чат %s: MCP %d — файлы диалога %s", chat_id[:8], body.server_id, "разрешены" if body.allow else "нет")
    return {"mcp_file_consent": consent_state(uid, chat._chat_row(uid, chat_id))}


async def media_limit(uid: int, row) -> "int | None":
    """Наибольший `max_bytes` серверов действующего набора диалога с пометкой `studio/chat-files` и согласием
    (каталог — из кэша `mcp_sources`, не ответивший сервер пропускается); таких нет — None."""
    conn = chat._chat_connection(uid, row["connection_id"])
    if conn is None or not conn.proxied or conn.cleaner:
        return None
    consent, (off, _) = _consent(row), mcp_sources.tool_state(row["mcp_tools_off"])
    items = []
    for sid in mcp_sources.dialog_servers(uid, row, conn):
        try:
            item = mcp_client.row(uid, sid)
        except HTTPException:
            continue
        if (item["enabled"] and consent.get(str(sid)) == mcp_sources.stamp(item["secret_enc"])
                and UPLOAD not in off.get(sid, ())):
            items.append(item)

    async def one(item):
        session = mcp_client.Session(item, uid=uid)
        try:
            return mcp_sources.chat_files(await mcp_sources.catalog(session, mcp_sources.LIST_SEC))
        except (HTTPException, TimeoutError):
            return None
        finally:
            await session.close()

    limits = [limit for limit in await asyncio.gather(*(one(item) for item in items)) if limit is not None]
    return max(limits, default=None)


@router.post("/chats/{chat_id}/media", status_code=201)
async def upload_media(chat_id: str, request: Request):
    """Звук или видео диалога для MCP: тело — сам файл (`Content-Type` — его тип, имя — `X-Studio-Name`). До приёма
    байтов — сервер с пометкой и согласием (иначе 409) и размер не больше его предела (иначе 413)."""
    uid = request.state.user.id
    row = chat._chat_row(uid, chat_id)
    name = documents.nfc(unquote(request.headers.get("x-studio-name", ""))).strip().replace("\x00", "")[:NAME_MAX]
    name = name or "файл"
    mime = forms.audio_type(request.headers.get("content-type", ""), name)
    if mime is None:
        raise HTTPException(415, {"code": "format", "message": NOT_MEDIA})
    limit = await media_limit(uid, row)
    if limit is None:
        raise HTTPException(409, {"code": "mcp_files", "message": NO_SERVER})
    too_big = {"code": "too_big", "message": MEDIA_BIG.format(_mib(limit))}
    length = request.headers.get("content-length", "").strip()
    if length.isdigit() and int(length) > limit:
        raise HTTPException(413, too_big)
    operations.check(uid)
    try:
        media_id = await media.save_stream(uid, request.stream(), mime, name, limit)
    except media.TooBig:
        raise HTTPException(413, too_big) from None
    except media.StorageError as e:
        raise HTTPException(503, {"code": "storage", "message": str(e)}) from None
    try:
        auth.recheck(request)                        # сессию закрыли, пока шла загрузка
        operations.check(uid)
        chat._chat_row(uid, chat_id)                 # диалог удалили, пока шла загрузка
    except HTTPException:
        await media.delete_async(uid, media_id)
        raise
    size = state.DB.execute("SELECT size FROM media WHERE id = ?", (media_id,)).fetchone()[0]
    item_id, now = uuid.uuid4().hex, clock.NOW()
    state.DB.execute("INSERT INTO chat_media(id, chat_id, user_id, name, mime, size, media_id, created)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (item_id, chat_id, uid, name, mime, size, media_id, now))
    log.info("чат %s: медиа диалога %s, %d байт", chat_id[:8], item_id[:8], size)
    return {"id": item_id, "name": name, "mime": mime, "size": size, "created": now}


@router.get("/chats/{chat_id}/messages/{message_id}/files/{step}/{n}")
async def result_file(chat_id: str, message_id: str, step: int, n: int, request: Request):
    """Файл результата MCP под ответом: диалог учётки, шаг с файлами, сервер открыт учётке и включён, его адрес и
    ключ — те же, что при результате (`stamp`); иначе 404."""
    uid = request.state.user.id
    chat._chat_row(uid, chat_id)
    found = state.DB.execute("SELECT tools FROM messages WHERE id = ? AND chat_id = ?", (message_id, chat_id)).fetchone()
    steps = json.loads(found["tools"] or "[]") if found is not None else []
    item = steps[step] if 0 <= step < len(steps) and isinstance(steps[step], dict) else {}
    files = item.get("files")
    if not isinstance(files, list) or not 0 <= n < len(files) or type(item.get("sid")) is not int:
        raise HTTPException(404, {"code": "gone", "message": GONE})
    server = mcp_client.row(uid, item["sid"])
    entry = files[n] if isinstance(files[n], dict) else {}
    aid, mime = entry.get("aid"), entry.get("mime")
    if (not server["enabled"] or mcp_sources.stamp(server["secret_enc"]) != item.get("stamp")
            or not isinstance(aid, str) or not mcp_sections.ARTIFACT_ID.fullmatch(aid)):
        raise HTTPException(404, {"code": "gone", "message": GONE})
    mime = mime if mime in mcp_sections.TICKET_MIME else "application/octet-stream"
    return await mcp_sections.relay(mcp_client.Session(server, uid=uid), aid, mime, request)
