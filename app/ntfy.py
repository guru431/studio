"""Уведомления ntfy на устройство: подписка сессии, адрес из allowlist, токен только шифром.

Язык уведомлений устройства (`ntfy_devices.lang`, `push_subs.lang`) — язык интерфейса его страницы; помощники
языка общие для обоих каналов: push импортирует этот модуль, а не наоборот."""
import asyncio
import hashlib
import logging
from types import SimpleNamespace
from typing import Literal

import httpx
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, StrictBool

import clock
import security
import state

DEFAULT_SERVER = "https://psh.example.net"
#: языки уведомлений — языки интерфейса (static/i18n.js); первый — умолчание строк до шага 51 и прежнего кода
LANGS = ("ru", "en")
Lang = Literal["ru", "en"]
#: заголовок служебных уведомлений и заданий разделов
STUDIO = {"ru": "Студия", "en": "Studio"}
TEST_NOTICE = {"ru": "Проверка уведомлений Studio: ntfy подключён", "en": "Studio notification test: ntfy connected"}
log = logging.getLogger("studio")
router = APIRouter(prefix="/api/me/ntfy")


class Settings(BaseModel):
    server: str = Field(min_length=1, max_length=512)
    topic: str = Field(pattern=r"^[-_A-Za-z0-9]{1,64}$")
    enabled: StrictBool
    token: str | None = Field(None, max_length=512, pattern=r"^[!-~]*$")
    lang: Lang | None = None             # язык интерфейса устройства; нет — прежний язык сессии (`session_lang`)


def pick(text: str | dict[str, str], lang: str) -> str:
    """Текст уведомления на языке устройства: строка — одна на все языки, словарь — по языку (нет — русский).
    Пользовательские имена (компьютер, проект) входят в текст как есть, без перевода."""
    return text if isinstance(text, str) else text.get(lang, text[LANGS[0]])


def session_lang(digest: str) -> str:
    """Язык, когда страница его не прислала (служебный работник, вкладка прежней выкладки): уже сохранённый у ntfy
    или push этой сессии, иначе русский."""
    row = state.DB.execute("SELECT lang FROM ntfy_devices WHERE digest = ? UNION ALL"
                           " SELECT lang FROM push_subs WHERE digest = ? LIMIT 1", (digest, digest)).fetchone()
    return row["lang"] if row else LANGS[0]


def session_langs(digest: str) -> list[str]:
    """Языки уведомлений сессии (push и ntfy) для сверки страницей при запуске (`/api/me.notify_langs`)."""
    return sorted({row["lang"] for row in state.DB.execute(
        "SELECT lang FROM ntfy_devices WHERE digest = ? UNION SELECT lang FROM push_subs WHERE digest = ?",
        (digest, digest))})


def set_session_lang(digest: str, lang: str) -> None:
    """Язык интерфейса устройства сменился — уведомления сессии на нём (`PATCH /api/me` `notify_lang`)."""
    state.DB.execute("UPDATE ntfy_devices SET lang = ? WHERE digest = ?", (lang, digest))
    state.DB.execute("UPDATE push_subs SET lang = ? WHERE digest = ?", (lang, digest))


def check_server(value: str) -> str:
    # push загружается раньше connections: upstream нужен только при обращении, не при импорте.
    import upstream

    try:
        url = httpx.URL(value.strip().rstrip("/"))
        valid = (url.scheme == "https" and url.host and not url.userinfo and not url.query
                 and not url.fragment and url.path == "/" and upstream.is_allowed(url))
    except (httpx.InvalidURL, UnicodeError, ValueError):
        valid = False
    if not valid:
        raise HTTPException(422, {"code": "ntfy_server", "message": "укажите HTTPS-сервер ntfy из списка администратора, без пути"})
    return str(url).rstrip("/")


def settings(user_id: int, digest: str) -> dict:
    row = state.DB.execute("SELECT server, topic, enabled, token_enc IS NOT NULL AS has_token"
                           " FROM ntfy_devices WHERE user_id = ? AND digest = ?", (user_id, digest)).fetchone()
    if row:
        return {**dict(row), "enabled": bool(row["enabled"]), "has_token": bool(row["has_token"]), "configured": True}
    old = state.DB.execute("SELECT server, topic, token_enc IS NOT NULL AS has_token FROM ntfy_settings WHERE user_id = ?",
                           (user_id,)).fetchone()
    # Прежние параметры — только подсказка. Общая тема никогда не становится темой нового устройства.
    suffix = hashlib.sha256(digest.encode()).hexdigest()[:32]
    return {"server": old["server"] if old else DEFAULT_SERVER,
            "topic": old["topic"][:31] + "-" + suffix if old else "",
            "enabled": False, "has_token": bool(old and old["has_token"]), "configured": False}


def save(user_id: int, digest: str, body: Settings) -> dict:
    if state.DB.execute("SELECT 1 FROM sessions WHERE digest = ? AND user_id = ? AND expires > ?",
                        (digest, user_id, clock.NOW())).fetchone() is None:
        raise HTTPException(401, {"code": "login", "message": "сессия завершена"})
    server = check_server(body.server)
    old = state.DB.execute("SELECT server, token_enc FROM ntfy_devices WHERE user_id = ? AND digest = ?",
                           (user_id, digest)).fetchone()
    if old is None:
        old = state.DB.execute("SELECT server, token_enc FROM ntfy_settings WHERE user_id = ?", (user_id,)).fetchone()
    # Прежний токен можно сохранить только для прежнего сервера. Пустая строка явно удаляет его.
    token = old["token_enc"] if old and old["server"] == server else None
    if body.token is not None:
        token = security.encrypt(body.token) if body.token else None
    state.DB.execute("INSERT INTO ntfy_devices(digest, user_id, server, topic, enabled, token_enc, lang)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)"
                     " ON CONFLICT(digest) DO UPDATE SET server=excluded.server, topic=excluded.topic,"
                     " enabled=excluded.enabled, token_enc=excluded.token_enc, lang=excluded.lang",
                     (digest, user_id, server, body.topic, int(body.enabled), token, body.lang or session_lang(digest)))
    log.info("пользователь %d: настройки ntfy сохранены", user_id)
    return settings(user_id, digest)


async def _send(row, body: str | dict[str, str], href: str, title: str | dict[str, str]) -> None:
    import upstream

    headers = {}
    try:
        if row["token_enc"] is not None:
            headers["Authorization"] = f"Bearer {security.decrypt(row['token_enc'])}"
        response = await upstream.request(SimpleNamespace(id=row["user_id"]), "POST", row["server"] + "/", keyless=True,
            headers=headers, timeout=10,
            json={"topic": row["topic"], "title": pick(title, row["lang"]), "message": pick(body, row["lang"]),
                  "click": state.CFG.origin.rstrip("/") + "/#/" + href, "priority": 4})
    except InvalidToken:
        raise HTTPException(409, {"code": "ntfy_token", "message": "токен ntfy не удалось расшифровать — введите его заново"}) from None
    except upstream.UpstreamRefused:
        raise HTTPException(422, {"code": "ntfy_server", "message": "сервер ntfy недоступен по списку разрешённых адресов"}) from None
    except httpx.HTTPError:
        raise HTTPException(502, {"code": "ntfy_network", "message": "не удалось связаться с ntfy"}) from None
    if not 200 <= response.status_code < 300:
        log.warning("пользователь %d: ntfy ответ %d", row["user_id"], response.status_code)
        words = "ntfy не разрешил отправку — проверьте тему и токен" if response.status_code in (401, 403) else "ntfy отклонил уведомление"
        raise HTTPException(502, {"code": "ntfy_delivery", "message": words})


async def notify(user_id: int, body: str | dict[str, str], href: str, *, title: str | dict[str, str], test: bool = False,
                 digest: str | None = None) -> None:
    """`body` и `title` — строка или {язык: текст}: каждому устройству — на его `lang`."""
    sql = ("SELECT n.* FROM ntfy_devices n JOIN sessions s ON s.digest = n.digest AND s.user_id = n.user_id"
           " JOIN users u ON u.id = n.user_id WHERE n.user_id = ? AND u.disabled = 0 AND s.expires > ?")
    params = [user_id, clock.NOW()]
    if test:
        sql += " AND n.digest = ?"
        params.append(digest)  # только текущая сессия, в том числе при выключенной автоматической доставке
    else:
        sql += " AND n.enabled = 1"
    rows = state.DB.execute(sql, params).fetchall()
    if test and not rows:
        raise HTTPException(409, {"code": "ntfy_settings", "message": "сначала сохраните настройки ntfy на этом устройстве"})
    targets = {(row["server"], row["topic"]): row for row in rows}
    results = await asyncio.gather(*(_send(row, body, href, title) for row in targets.values()), return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            if test:
                raise result
            log.warning("пользователь %d: ntfy сбой (%s)", user_id, type(result).__name__)


@router.get("")
async def get_settings(request: Request):
    return settings(request.state.user.id, request.state.digest)


@router.put("")
async def put_settings(body: Settings, request: Request):
    return save(request.state.user.id, request.state.digest, body)


@router.delete("", status_code=204)
async def delete_settings(request: Request):
    state.DB.execute("DELETE FROM ntfy_devices WHERE user_id = ? AND digest = ?", (request.state.user.id, request.state.digest))
    return Response(status_code=204)


@router.post("/test", status_code=204)
async def test_settings(request: Request):
    await notify(request.state.user.id, TEST_NOTICE, "settings/notifications",
                 title=STUDIO, test=True, digest=request.state.digest)
    return Response(status_code=204)
