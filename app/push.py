"""Web Push раздела «Код» и готовых фоновых заданий.

- Ключ VAPID (P-256) заводит служба при первом обращении и хранит в `settings` (`vapid`) шифром
  `STUDIO_DATA_KEY`, как токены подключений. Ключ данных сменили — прежний не расшифровать: новый ключ VAPID,
  подписки стираются (их браузеры подписывались на прежний открытый ключ), push включают заново.
- Подписка — устройства, на сессию студии (`push_subs.digest`, каскад от `sessions`): выход или конец сессии
  уносит её. Адрес присылает браузер, а запрос на него шлёт сервер — поэтому только https-адреса служб push из
  `HOSTS` (исключение из правила списка разрешённых адресов, CLAUDE.md «Правила»); иной адрес — 422.
- Полезная нагрузка — RFC 8291 (aes128gcm, RFC 8188) одной записью, подпись — VAPID (RFC 8292, ES256); всё на
  `cryptography`. Текст — только повод (у «Кода» ещё имя компьютера и проекта), подробности — в студии.
- Журнал — без адреса подписки и текста: «push: ответ <код>», «push: сбой сети», «пользователь N: …».
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

import clock
import ntfy
import security
import state

log = logging.getLogger("studio")
router = APIRouter(prefix="/api")

#: службы push: точное имя или окончание (с точкой)
HOSTS = ("fcm.googleapis.com", ".push.apple.com", "updates.push.services.mozilla.com", ".notify.windows.com")
TTL_SEC = 3600
JWT_SEC = 12 * 3600
ENDPOINT_MAX = 1024
RECORD_SIZE = 4096
TITLE = "Код"
JOB_NOTICES = {
    "images": ("photo", "картинка готова", "картинка не получилась"),
    "videos": ("video", "ролик готов", "ролик не получился"),
    "transcripts": ("transcribe", "расшифровка готова", "расшифровка не получилась"),
    "scans": ("ocr", "распознавание готово", "распознавание не получилось"),
    "translations": ("translate", "перевод готов", "перевод не получился"),
    "speeches": ("speech", "озвучка готова", "озвучка не получилась"),
}

#: ключ VAPID процесса (тесты очищают)
KEY: ec.EllipticCurvePrivateKey | None = None
#: фоновые отправки (`notify_later`): ссылка держит задачу до конца, `stop` их отменяет
TASKS: set[asyncio.Task] = set()


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def unb64u(text: str) -> bytes:
    """base64url без `=` (с ними — тоже); не base64 — ValueError."""
    t = text.rstrip("=")
    return base64.b64decode(t.replace("-", "+").replace("_", "/") + "=" * (-len(t) % 4), validate=True)


def allowed(endpoint: str) -> bool:
    """Адрес подписки — https служб `HOSTS`, без порта и пользователя, не длиннее `ENDPOINT_MAX`."""
    if not isinstance(endpoint, str) or len(endpoint) > ENDPOINT_MAX or any(c <= " " or c == "\x7f" for c in endpoint):
        return False
    try:
        parts = urlsplit(endpoint)
        port = parts.port
    except ValueError:
        return False
    host = parts.hostname or ""
    if parts.scheme != "https" or port is not None or parts.netloc.lower() != host:
        return False
    return any(host.endswith(h) and len(host) > len(h) if h.startswith(".") else host == h for h in HOSTS)


def _hmac(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


def _point(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)


def encrypt(payload: bytes, p256dh: bytes, auth: bytes, *, salt: bytes | None = None,
            private: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """Тело push: RFC 8291 + RFC 8188 (aes128gcm) одной записью."""
    as_key = private or ec.generate_private_key(ec.SECP256R1())
    as_pub = _point(as_key.public_key())
    shared = as_key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh))
    ikm = _hmac(_hmac(auth, shared), b"WebPush: info\x00" + p256dh + as_pub + b"\x01")
    salt = salt or os.urandom(16)
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    return (salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(as_pub)]) + as_pub
            + AESGCM(cek).encrypt(nonce, payload + b"\x02", None))


# ---- ключ VAPID ----------------------------------------------------------------

def _new_key() -> ec.EllipticCurvePrivateKey:
    key = ec.generate_private_key(ec.SECP256R1())
    raw = key.private_numbers().private_value.to_bytes(32, "big")
    state.DB.execute("INSERT INTO settings(key, value) VALUES ('vapid', ?)"
                     " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (security.encrypt(b64u(raw)).decode("ascii"),))
    return key


def vapid_key() -> ec.EllipticCurvePrivateKey:
    """Ключ VAPID: из `settings` (шифром), нет — новый; не расшифровать — новый и подписки стираются."""
    global KEY
    if KEY is not None:
        return KEY
    row = state.DB.execute("SELECT value FROM settings WHERE key = 'vapid'").fetchone()
    if row is None:
        KEY = _new_key()
        return KEY
    try:
        raw = unb64u(security.decrypt(row[0].encode("ascii")))
        KEY = ec.derive_private_key(int.from_bytes(raw, "big"), ec.SECP256R1())
    except (InvalidToken, ValueError):
        KEY = _new_key()
        state.DB.execute("DELETE FROM push_subs")
        log.warning("push: ключ VAPID заменён, подписки сброшены")
    return KEY


def public_key() -> str:
    return b64u(_point(vapid_key().public_key()))


def _json64(obj: dict) -> str:
    return b64u(json.dumps(obj, separators=(",", ":")).encode())


def vapid_header(endpoint: str) -> str:
    """`Authorization` запроса к службе push: JWT ES256 (RFC 8292) и открытый ключ."""
    parts = urlsplit(endpoint)
    signing = (_json64({"typ": "JWT", "alg": "ES256"}) + "."
               + _json64({"aud": f"{parts.scheme}://{parts.hostname}", "exp": int(clock.NOW()) + JWT_SEC,
                          "sub": state.CFG.origin}))
    r, s = decode_dss_signature(vapid_key().sign(signing.encode(), ec.ECDSA(hashes.SHA256())))
    return f"vapid t={signing}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}, k={public_key()}"


# ---- отправка ------------------------------------------------------------------

async def notify(user_id: int, body: str, href: str, tag: str, *, title: str = TITLE) -> None:
    """Уведомление живым сессиям активной учётки; 404 и 410 — подписки больше нет."""
    rows = state.DB.execute(
        "SELECT p.endpoint, p.p256dh, p.auth, p.digest FROM push_subs p"
        " JOIN sessions s ON s.digest = p.digest AND s.user_id = p.user_id"
        " JOIN users u ON u.id = p.user_id"
        " WHERE p.user_id = ? AND s.expires > ? AND u.disabled = 0", (user_id, clock.NOW())).fetchall()
    if not rows:
        return
    payload = json.dumps({"title": title, "body": body, "href": href, "tag": tag}, ensure_ascii=False).encode()
    for row in rows:
        endpoint = row["endpoint"]
        # A previous network send can yield long enough for another device to be revoked.
        if state.DB.execute(
            "SELECT 1 FROM push_subs p JOIN sessions s ON s.digest = p.digest AND s.user_id = p.user_id"
            " JOIN users u ON u.id = p.user_id WHERE p.endpoint = ? AND p.digest = ?"
            " AND s.expires > ? AND u.disabled = 0", (endpoint, row["digest"], clock.NOW())).fetchone() is None:
            continue
        headers = {"TTL": str(TTL_SEC), "Urgency": "high", "Content-Encoding": "aes128gcm",
                   "Content-Type": "application/octet-stream", "Authorization": vapid_header(endpoint)}
        try:
            r = await state.HTTP.post(endpoint, content=encrypt(payload, unb64u(row["p256dh"]), unb64u(row["auth"])),
                                      headers=headers)
        except httpx.HTTPError:
            log.warning("push: сбой сети")
            continue
        if r.status_code in (404, 410):
            state.DB.execute("DELETE FROM push_subs WHERE endpoint = ?", (endpoint,))
            log.info("пользователь %d: подписка push снята службой (%d)", user_id, r.status_code)
        elif not 200 <= r.status_code < 300:
            log.warning("push: ответ %d", r.status_code)


def notify_later(user_id: int, body: str, href: str, tag: str, *, title: str = TITLE) -> None:
    """`notify` фоном: завершение задания и событие моста не ждут службу push."""
    async def run():
        results = await asyncio.gather(notify(user_id, body, href, tag, title=title),
                                       ntfy.notify(user_id, body, href, title=title), return_exceptions=True)
        for channel, result in zip(("push", "ntfy"), results):
            if isinstance(result, Exception):  # сбой одного канала не мешает другому, без текста/адреса в журнале
                log.warning("%s: сбой (%s)", channel, type(result).__name__)
    task = asyncio.get_running_loop().create_task(run())
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)


def job_finished(table: str, item_id: str) -> None:
    """После первого сохранения итога: только готовое/ошибка, только подписки владельца, без содержимого."""
    section, done, error = JOB_NOTICES[table]  # имя таблицы — только из кода
    row = state.DB.execute(f"SELECT j.user_id, j.state FROM {table} j WHERE j.id = ?"
                            " AND j.finished IS NOT NULL AND j.state IN ('done', 'error', 'failed')"
                            " AND EXISTS (SELECT 1 FROM users u WHERE u.id = j.user_id AND u.disabled = 0)"
                            " AND (EXISTS (SELECT 1 FROM push_subs p JOIN sessions s ON s.digest = p.digest"
                            " WHERE p.user_id = j.user_id AND s.expires > ?)"
                            " OR EXISTS (SELECT 1 FROM ntfy_devices n JOIN sessions s ON s.digest = n.digest"
                            " WHERE n.user_id = j.user_id AND n.enabled = 1 AND s.expires > ?))",
                            (item_id, clock.NOW(), clock.NOW())).fetchone()
    if row is not None:
        notify_later(row["user_id"], done if row["state"] == "done" else error,
                     f"{section}?id={item_id}", item_id, title="Студия")


async def stop() -> None:
    for task in list(TASKS):
        task.cancel()
    await asyncio.gather(*list(TASKS), return_exceptions=True)


# ---- подписка устройства --------------------------------------------------------

class Keys(BaseModel):
    p256dh: str = Field(max_length=200)
    auth: str = Field(max_length=100)


class Subscription(BaseModel):
    endpoint: str                        # длину и вид сверяет `allowed` — ответ 422 `endpoint`
    keys: Keys


def _keys(keys: Keys) -> tuple[bytes, bytes] | None:
    try:
        p256dh, auth = unb64u(keys.p256dh), unb64u(keys.auth)
        if len(p256dh) != 65 or p256dh[0] != 4 or len(auth) != 16:
            return None
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)   # точка на кривой (RFC 8291, 7)
    except ValueError:
        return None
    return p256dh, auth


def endpoint_mark(endpoint: str) -> str:
    """Признак адреса подписки для сверки страницей (push-sync.js): не сам адрес — он и есть доступ к устройству."""
    return hashlib.sha256(endpoint.encode()).hexdigest()[:16]


@router.get("/me/push")
async def get_push(request: Request):
    """`endpoint` — признак адреса подписки этой сессии (или null): страница сверяет его с подпиской браузера и после
    отпирания отправляет новую, если служебный работник не смог (pushsubscriptionchange под блокировкой)."""
    row = state.DB.execute("SELECT endpoint FROM push_subs WHERE digest = ?", (request.state.digest,)).fetchone()
    return {"key": public_key(), "subscribed": row is not None,
            "endpoint": endpoint_mark(row["endpoint"]) if row else None}


@router.post("/me/push", status_code=204)
async def subscribe(body: Subscription, request: Request):
    if not allowed(body.endpoint):
        raise HTTPException(422, {"code": "endpoint", "message": "адрес не известной службы push"})
    keys = _keys(body.keys)
    if keys is None:
        raise HTTPException(422, {"code": "keys", "message": "неверные ключи подписки"})
    uid = request.state.user.id
    vapid_key()                          # ключ — до строки: замена ключа стёрла бы и эту подписку
    # у сессии (браузера) подписка одна: новая заменяет прежнюю, а не копится рядом мёртвым адресом
    state.DB.execute("DELETE FROM push_subs WHERE digest = ? AND endpoint != ?", (request.state.digest, body.endpoint))
    state.DB.execute(
        "INSERT INTO push_subs(endpoint, user_id, digest, p256dh, auth, created) VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(endpoint) DO UPDATE SET user_id = excluded.user_id, digest = excluded.digest,"
        " p256dh = excluded.p256dh, auth = excluded.auth, created = excluded.created",
        (body.endpoint, uid, request.state.digest, b64u(keys[0]), b64u(keys[1]), clock.NOW()))
    log.info("пользователь %d: подписка push", uid)
    return Response(status_code=204)


@router.delete("/me/push", status_code=204)
async def unsubscribe(request: Request):
    n = state.DB.execute("DELETE FROM push_subs WHERE digest = ?", (request.state.digest,)).rowcount
    log.info("пользователь %d: подписка push снята (%d)", request.state.user.id, n)
    return Response(status_code=204)
