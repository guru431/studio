"""Cleaner client. All traffic uses Studio's allowlist, credential snapshots and server session.

Preview tickets are bounded, short-lived process memory. Remote contexts and deletion retries are
durable; deleting local history also schedules removal of its encrypted replacement table.
"""
import asyncio
import json
import uuid
from dataclasses import dataclass, field
from cryptography.fernet import InvalidToken

import httpx
from fastapi import HTTPException

import clock
import connections
import security
import state
import upstream

TICKET_SECONDS = 540
TICKETS: dict[str, "Preview"] = {}
LOCKS: dict[tuple, asyncio.Lock] = {}


@dataclass
class Preview:
    user_id: int
    item_id: str
    conn: connections.Connection
    snapshot: tuple = field(repr=False)
    model: str = ""
    messages: list = field(default_factory=list, repr=False)
    remote_id: str = ""
    receipt: str = field(default="", repr=False)
    expires: float = 0


def refused(message: str, status=409):
    return HTTPException(status, {"code": "cleaner", "message": message})


async def call(conn, method, path, **kwargs):
    connections.check_reachable(conn)
    try:
        response = await upstream.request(conn, method, conn.base_url + "/api/v1" + path,
                                          timeout=kwargs.pop("timeout", httpx.Timeout(180, connect=15)), **kwargs)
    except upstream.UpstreamRefused as exc:
        raise connections.refused(conn, exc) from None
    except httpx.HTTPError:
        raise refused("Cleaner недоступен: данные не отправлены облачной модели", 502) from None
    if not response.is_success:
        # Authentication errors use our words; no credential or provider response is echoed.
        message = upstream.gateway_error(response, conn.name) if response.status_code in (401, 403) else upstream.message(response)
        raise refused(message or "Cleaner отказал: запрос не отправлен", 502 if response.status_code >= 500 else 409)
    return upstream.json_body(response) if response.status_code != 204 else {}


async def context(conn, resource: str, item_id: str) -> str:
    key = (resource, item_id, conn.id)
    lock = LOCKS.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            token = connections.token(conn)
            row = state.DB.execute("SELECT * FROM cleaner_contexts WHERE resource = ? AND item_id = ?"
                                   " AND connection_id = ?", key).fetchone()
            if row is not None:
                if row["base_url"] == conn.base_url and security.decrypt(row["token_enc"]) == token:
                    return row["remote_id"]
                state.DB.execute("DELETE FROM cleaner_contexts WHERE id = ?", (row["id"],))
            snap = connections.snapshot(conn)
            created = await call(conn, "POST", "/chats", json={"title": "Studio"})
            remote_id = created.get("id")
            if not isinstance(remote_id, str) or not remote_id.isalnum():
                raise refused("Cleaner вернул неверный идентификатор контекста", 502)
            identifier = uuid.uuid4().hex
            table = {"chat": "chats", "translate": "translations"}.get(resource)
            parent = state.DB.execute(f"SELECT 1 FROM {table} WHERE id = ? AND user_id = ?",
                                      (item_id, conn.user_id)).fetchone() if table else None
            if connections.snapshot(conn) != snap or parent is None:
                state.DB.execute("INSERT INTO cleaner_deletions(id, user_id, remote_id, base_url, token_enc, queued)"
                                 " VALUES (?, ?, ?, ?, ?, ?)",
                                 (identifier, conn.user_id, remote_id, conn.base_url, security.encrypt(token), clock.NOW()))
                raise refused("Запись или подключение изменились: запустите заново")
            state.DB.execute("INSERT INTO cleaner_contexts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (identifier, conn.user_id, resource, item_id, conn.id, remote_id,
                              conn.base_url, security.encrypt(token), clock.NOW()))
            return remote_id
    finally:
        # Do not remove a lock while another caller is waiting on it.
        if not lock.locked() and not getattr(lock, "_waiters", None):
            LOCKS.pop(key, None)


async def prepare(conn, resource, item_id, model, messages, rules=()):
    if not conn.cleaner or any(not isinstance(m.get("content"), str) for m in messages):
        raise refused("Через Cleaner можно отправить текст и распознанные документы; изображения не передаются")
    remote = await context(conn, resource, item_id)
    snap = connections.snapshot(conn)
    shown = await call(conn, "POST", f"/chats/{remote}/requests",
                       json={"model": model, "messages": messages, "rules": list(rules)})
    if connections.snapshot(conn) != snap:
        raise refused("Подключение изменилось: проверьте запрос ещё раз")
    if not isinstance(shown.get("receipt"), str) or not isinstance(shown.get("messages"), list):
        raise refused("Cleaner вернул неверный предпросмотр", 502)
    preview = Preview(conn.user_id, item_id, conn, snap, model, messages, remote, shown["receipt"],
                      clock.NOW() + TICKET_SECONDS)
    return preview, {k: shown.get(k) for k in ("messages", "counts", "deliverable", "blocked", "leaks", "replacements")}


def remember(preview):
    for key, old in list(TICKETS.items()):
        if old.expires <= clock.NOW() or (old.user_id, old.item_id) == (preview.user_id, preview.item_id):
            TICKETS.pop(key, None)
    if len(TICKETS) >= 64:
        raise refused("Слишком много открытых предпросмотров: повторите позже", 429)
    ticket = uuid.uuid4().hex
    TICKETS[ticket] = preview
    return ticket


def forget(user_id, item_id=None):
    for ticket, preview in list(TICKETS.items()):
        if preview.user_id == user_id and (item_id is None or preview.item_id == item_id):
            TICKETS.pop(ticket, None)


def take(ticket, conn, item_id, model, messages):
    preview = TICKETS.get(ticket)
    if preview is not None and preview.expires <= clock.NOW():
        TICKETS.pop(ticket, None)
        preview = None
    if (preview is None or preview.user_id != conn.user_id or preview.item_id != item_id
            or preview.conn.id != conn.id or preview.model != model or preview.messages != messages
            or preview.expires <= clock.NOW() or preview.snapshot != connections.snapshot(conn)):
        raise refused("Запрос изменился или предпросмотр истёк: проверьте данные ещё раз")
    TICKETS.pop(ticket)
    return preview


async def events(preview):
    conn, remote = preview.conn, preview.remote_id
    if connections.snapshot(conn) != preview.snapshot:
        raise refused("Подключение изменилось: проверьте запрос ещё раз")
    answer = await call(conn, "POST", f"/chats/{remote}/requests/send", json={"receipt": preview.receipt})
    answer_id = answer.get("item_id")
    if not isinstance(answer_id, str) or not answer_id.isalnum():
        raise refused("Cleaner вернул неверный ответ", 502)
    done = False
    try:
        async with upstream.stream(conn, "GET", f"{conn.base_url}/api/v1/chats/{remote}/answers/{answer_id}/events",
                                   timeout=httpx.Timeout(1900, connect=15)) as response:
            if not response.is_success:
                raise refused("Не удалось прочитать ответ Cleaner", 502)
            kind = ""
            async for line in response.aiter_lines():
                if len(line) > 24_000_000:
                    raise refused("Ответ Cleaner слишком длинный", 502)
                if line.startswith("event: "):
                    kind = line[7:]
                elif line.startswith("data: "):
                    try:
                        data = json.loads(line[6:])
                    except ValueError:
                        raise refused("Cleaner оборвал ответ", 502) from None
                    if not isinstance(data, dict):
                        raise refused("Cleaner вернул неверный ответ", 502)
                    if kind == "done":
                        done = True
                    yield kind, data
            if not done:
                raise refused("Cleaner оборвал ответ: сохранена полученная часть", 502)
    finally:
        if not done:
            try:
                async with asyncio.timeout(3):
                    await call(conn, "POST", f"/chats/{remote}/answers/{answer_id}/stop", timeout=3)
            except (HTTPException, httpx.HTTPError, upstream.UpstreamRefused, TimeoutError):
                pass


async def translate(conn, item_id, model, system, text, on_text):
    preview, shown = await prepare(conn, "translate", item_id, model,
                                   [{"role": "system", "content": system}, {"role": "user", "content": text}])
    if not shown["deliverable"]:
        raise refused("Cleaner заблокировал перевод по политике данных")
    answer = ""
    async for kind, data in events(preview):
        if isinstance(data.get("text"), str):
            answer = data["text"]
            on_text(answer)
        if kind == "done" and data.get("status") != "done":
            raise refused(data.get("error") or "Перевод оборвался")
    return answer


async def redact(conn, resource, item_id, text):
    remote = await context(conn, resource, item_id)
    result = await call(conn, "POST", f"/chats/{remote}/texts", json={"text": text, "wait": 60})
    deadline = clock.NOW() + 180
    while result.get("status") in ("queued", "running"):
        if clock.NOW() >= deadline:
            raise refused("Cleaner ещё обрабатывает текст: повторите позже")
        await clock.SLEEP(0.5)
        result = await call(conn, "GET", f'/chats/{remote}/items/{result["item_id"]}')
    if not result.get("deliverable") or not isinstance(result.get("sanitized"), str):
        raise refused("Cleaner не разрешил выдачу текста: проверьте политики и распознавание")
    return result["sanitized"]


async def purge():
    rows = state.DB.execute("SELECT * FROM cleaner_deletions WHERE retry_at <= ? ORDER BY queued LIMIT 10",
                            (clock.NOW(),)).fetchall()
    for row in rows:
        conn = connections.Connection(0, row["user_id"], "Cleaner", "openai", row["base_url"], frozenset(), True,
                                      server_only=True, cleaner=True)
        try:
            response = await upstream.request(
                conn, "DELETE", f'{row["base_url"]}/api/v1/chats/{row["remote_id"]}', keyless=True,
                headers={"Authorization": f'Bearer {security.decrypt(row["token_enc"])}'},
                timeout=httpx.Timeout(10),
            )
            if response.status_code in (204, 404):
                state.DB.execute("DELETE FROM cleaner_deletions WHERE id = ?", (row["id"],))
            elif response.status_code in (401, 403, 429):
                # Cleaner throttles bad keys by source address. Do not lock out active Studio users
                # by repeatedly retrying revoked cleanup credentials on the same host.
                state.DB.execute("UPDATE cleaner_deletions SET retry_at = ? WHERE base_url = ?",
                                 (clock.NOW() + 3600, row["base_url"]))
                break
        except (httpx.HTTPError, upstream.UpstreamRefused, ValueError, InvalidToken):
            continue


async def sweep():
    while True:
        for ticket, preview in list(TICKETS.items()):
            if preview.expires <= clock.NOW():
                TICKETS.pop(ticket, None)
        await purge()
        await clock.SLEEP(60)
