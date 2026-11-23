"""Web Push раздела «Код» и готовых фоновых заданий.

- Ключ VAPID (P-256) заводит служба при первом обращении и хранит в `settings` (`vapid`) шифром
  `STUDIO_DATA_KEY`, как токены подключений. Ключ данных сменили — прежний не расшифровать: новый ключ VAPID,
  подписки стираются (их браузеры подписывались на прежний открытый ключ), push включают заново.
- Подписка — устройства, на сессию студии (`push_subs.digest`, каскад от `sessions`): выход или конец сессии
  уносит её. Адрес присылает браузер, а запрос на него шлёт сервер — поэтому только https-адреса служб push из
  `HOSTS` (исключение из правила списка разрешённых адресов, CLAUDE.md «Правила»); иной адрес — 422.
- Полезная нагрузка — RFC 8291 (aes128gcm, RFC 8188) одной записью, подпись — VAPID (RFC 8292, ES256); всё на
  `cryptography`. Текст — только повод (у «Кода» ещё имя компьютера и проекта), подробности — в студии.
- Язык текста — язык подписки (`push_subs.lang`, язык интерфейса страницы): повод и заголовок — {язык: текст},
  имена компьютера и проекта — как есть.
- Журнал — без адреса подписки и текста: «push: ответ <код>», «push: сбой сети», «пользователь N: …».
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx
from cryptography.fernet import InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator

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
TITLE = "Агенты"                      # имя раздела (sections-meta.js); заголовок push и ntfy «Агентов»
#: заголовок «Агентов» на языках подписки (английский — перевод имени раздела в en.js)
TITLES = {"ru": TITLE, "en": "Agents"}
STUDIO = ntfy.STUDIO                   # заголовок заданий разделов и служебных уведомлений
#: раздел, «готово» и «не получилось» на языках подписки (те же слова, что у jobs.js и en.js)
JOB_NOTICES = {
    "images": ("photo", {"ru": "картинка готова", "en": "image ready"},
               {"ru": "картинка не получилась", "en": "image failed"}),
    "videos": ("video", {"ru": "ролик готов", "en": "video ready"}, {"ru": "ролик не получился", "en": "video failed"}),
    "transcripts": ("transcribe", {"ru": "расшифровка готова", "en": "transcript ready"},
                    {"ru": "расшифровка не получилась", "en": "transcription failed"}),
    "scans": ("ocr", {"ru": "распознавание готово", "en": "recognition ready"},
              {"ru": "распознавание не получилось", "en": "recognition failed"}),
    "translations": ("translate", {"ru": "перевод готов", "en": "translation ready"},
                     {"ru": "перевод не получился", "en": "translation failed"}),
    "speeches": ("speech", {"ru": "озвучка готова", "en": "speech ready"},
                 {"ru": "озвучка не получилась", "en": "speech failed"}),
}

#: ответ чата через сервер (chat.StreamTask): «готово» и «не получилось» на языках подписки (те же слова — jobs.js)
CHAT_NOTICES = ({"ru": "ответ готов", "en": "answer ready"}, {"ru": "ответ не получился", "en": "answer failed"})
#: ответ Open WebUI на сообщение из Студии ждёт решения (вопрос модели, подтверждение инструмента) — без вопроса модели
DECISION_NOTICE = {"ru": "нужно ваше решение", "en": "your decision is needed"}
#: заголовок push раздела «Open WebUI» (имя продукта, на всех языках одно)
OWUI_TITLE = "Open WebUI"

#: виды уведомлений — галочки «О чём уведомлять» устройства (`notify_prefs.off`): `ask` — карточки «Агентов»
#: (подтверждение, вопрос), очередь ждёт, решение Open WebUI и напоминания о карточке; `done` — конец хода «Агентов»,
#: ответ чата и Open WebUI; `jobs` — задания разделов; `service` — копия базы, отключённая ссылка «Поделиться»
KINDS = ("ask", "done", "jobs", "service")
#: напоминание о ждущей карточке «Агентов» (`bridge.remind`), минуты; 0 — не напоминать
REMINDS = (0, 10, 30, 60)
REMIND_DEFAULT = 30
TIME = r"^([01]\d|2[0-3]):[0-5]\d$"

#: сессии, чья страница студии сейчас на экране: digest → время, когда опрос `/api/jobs?visible=1` это сказал
#: (`screen`). Готовность заданий (`quiet`) им не шлётся ни push, ни ntfy — она видна в самой студии, как у «Агентов»
#: `bridge.watching`. Видимая страница с идущим заданием опрашивает раз в 5 с (jobs.js JOBS_POLL_MS), скрываясь —
#: шлёт `visible=0`; отметка без подтверждения старше SCREEN_SEC не действует (заснувший телефон)
ON_SCREEN: dict[str, float] = {}
SCREEN_SEC = 15

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

def screen(digest: str, visible: bool) -> None:
    """Страница сессии на экране (`visible`) или ушла с него; заодно — прочь отметки старше SCREEN_SEC."""
    now = clock.NOW()
    for key in [key for key, seen in ON_SCREEN.items() if now - seen >= SCREEN_SEC]:
        del ON_SCREEN[key]
    if visible:
        ON_SCREEN[digest] = now
    else:
        ON_SCREEN.pop(digest, None)


def on_screen() -> frozenset[str]:
    """Сессии, чья страница сейчас на экране."""
    now = clock.NOW()
    return frozenset(key for key, seen in ON_SCREEN.items() if now - seen < SCREEN_SEC)


async def notify(user_id: int, body: str | dict[str, str], href: str, tag: str, *,
                 title: str | dict[str, str] = TITLES, skip: frozenset[str] = frozenset()) -> None:
    """Уведомление живым сессиям активной учётки; 404 и 410 — подписки больше нет. `body` и `title` — строка или
    {язык: текст}: каждой подписке — на её `lang`. `skip` — сессии, которым не слать (на экране)."""
    rows = state.DB.execute(
        "SELECT p.endpoint, p.p256dh, p.auth, p.digest, p.lang FROM push_subs p"
        " JOIN sessions s ON s.digest = p.digest AND s.user_id = p.user_id"
        " JOIN users u ON u.id = p.user_id"
        " WHERE p.user_id = ? AND s.expires > ? AND u.disabled = 0", (user_id, clock.NOW())).fetchall()
    for row in rows:
        if row["digest"] in skip:
            continue
        endpoint = row["endpoint"]
        payload = json.dumps({"title": ntfy.pick(title, row["lang"]), "body": ntfy.pick(body, row["lang"]),
                              "href": href, "tag": tag}, ensure_ascii=False).encode()
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


def _quiet(row, now: float) -> bool:
    """Тихие часы строки `notify_prefs` идут сейчас по поясу учётки; через полночь — «с 22:00 по 08:00»."""
    start, end = row["quiet_from"], row["quiet_to"]
    if start is None or end is None:
        return False
    moment = datetime.fromtimestamp(now, ZoneInfo(row["timezone"]))
    minute = moment.hour * 60 + moment.minute
    return start <= minute < end if start < end else minute >= start or minute < end


def muted(user_id: int, kind: str) -> frozenset[str]:
    """Сессии учётки, которым вид `kind` сейчас не слать: снята его галочка или идут тихие часы устройства."""
    now = clock.NOW()
    rows = state.DB.execute("SELECT n.digest, n.off, n.quiet_from, n.quiet_to, u.timezone FROM notify_prefs n"
                            " JOIN users u ON u.id = n.user_id WHERE n.user_id = ?", (user_id,)).fetchall()
    return frozenset(row["digest"] for row in rows if kind in json.loads(row["off"]) or _quiet(row, now))


def reminders(user_id: int) -> dict[str, int]:
    """Живые сессии учётки → интервал напоминания о ждущей карточке, с (0 — не напоминать; нет строки — умолчание)."""
    return {row[0]: row[1] * 60 for row in state.DB.execute(
        "SELECT s.digest, COALESCE(n.remind, ?) FROM sessions s LEFT JOIN notify_prefs n ON n.digest = s.digest"
        " WHERE s.user_id = ? AND s.expires > ?", (REMIND_DEFAULT, user_id, clock.NOW()))}


def notify_later(user_id: int, body: str | dict[str, str], href: str, tag: str, *,
                 title: str | dict[str, str] = TITLES, quiet: bool = False, kind: str = "service",
                 skip: frozenset[str] = frozenset()) -> None:
    """`notify` фоном: завершение задания и событие моста не ждут службу push. `quiet` — готовность задания: сессиям,
    чья страница на экране (`on_screen`), не слать. `kind` — вид из `KINDS`: устройствам, где он снят или идут тихие
    часы (`muted`), не слать. `skip` — ещё сессии, которым не слать (напоминание — только тем, кому оно пора)."""
    skip = skip | muted(user_id, kind) | (on_screen() if quiet else frozenset())

    async def run():
        results = await asyncio.gather(notify(user_id, body, href, tag, title=title, skip=skip),
                                       ntfy.notify(user_id, body, href, title=title, skip=skip), return_exceptions=True)
        for channel, result in zip(("push", "ntfy"), results):
            if isinstance(result, Exception):  # сбой одного канала не мешает другому, без текста/адреса в журнале
                log.warning("%s: сбой (%s)", channel, type(result).__name__)
    task = asyncio.get_running_loop().create_task(run())
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)


def unread_scans(scan_ids) -> set:
    """Из документов «Сканов» — те, где не прочитана ни одна страница (есть `failed`, нет `done`/`layer`, как пусто у
    `ocr._readable`): push, ntfy и «Готово за час» (`jobs._ocr`) называют такой «готовый» документ неудачей, а не
    готовностью. Один запрос на любое число документов — «В работе» опрашивается часто."""
    ids = list(scan_ids)
    if not ids:
        return set()
    rows = state.DB.execute(
        f"SELECT scan_id FROM scan_pages WHERE scan_id IN ({', '.join('?' * len(ids))}) GROUP BY scan_id"
        " HAVING SUM(state = 'failed') > 0 AND SUM(state IN ('done', 'layer')) = 0", ids).fetchall()
    return {row[0] for row in rows}


def job_finished(table: str, item_id: str, *, tag: str | None = None, failed: bool = False) -> None:
    """После первого сохранения итога: только готовое/ошибка, только подписки владельца, без содержимого. `tag` —
    метка уведомления (нет — id записи), `failed` — «не получилось» и при готовой записи (чат озвучки, где не
    получилась другая часть)."""
    section, done, error = JOB_NOTICES[table]  # имя таблицы — только из кода
    if table == "scans" and state.DB.execute("SELECT 1 FROM scans WHERE id = ? AND origin = 'chat'",
                                             (item_id,)).fetchone():
        return                                 # скан вложения: о нём скажет ответ чата (`chat_answered`)
    row = state.DB.execute(f"SELECT j.user_id, j.state FROM {table} j WHERE j.id = ? AND j.deleted IS NULL"
                            " AND j.finished IS NOT NULL AND j.state IN ('done', 'error', 'failed')"
                            " AND EXISTS (SELECT 1 FROM users u WHERE u.id = j.user_id AND u.disabled = 0)"
                            " AND (EXISTS (SELECT 1 FROM push_subs p JOIN sessions s ON s.digest = p.digest"
                            " WHERE p.user_id = j.user_id AND s.expires > ?)"
                            " OR EXISTS (SELECT 1 FROM ntfy_devices n JOIN sessions s ON s.digest = n.digest"
                            " WHERE n.user_id = j.user_id AND n.enabled = 1 AND s.expires > ?))",
                            (item_id, clock.NOW(), clock.NOW())).fetchone()
    if row is not None:
        ok = row["state"] == "done" and not failed and not (table == "scans" and unread_scans([item_id]))
        notify_later(row["user_id"], done if ok else error,
                     f"{section}?id={item_id}", tag or item_id, title=STUDIO, quiet=True, kind="jobs")


def chat_answered(user_id: int, chat_id: str, message_id: str, ok: bool) -> None:
    """Ответ чата через сервер кончился (готов или ошибка): как задание раздела — без содержимого, в диалог.
    Диалог в корзине — молча."""
    if state.DB.execute("SELECT 1 FROM chats WHERE id = ? AND deleted IS NULL", (chat_id,)).fetchone() is None:
        return
    notify_later(user_id, CHAT_NOTICES[0 if ok else 1], f"chat/{chat_id}", message_id, title=STUDIO, quiet=True,
                 kind="done")


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
    lang: ntfy.Lang | None = None        # язык интерфейса; служебный работник его не знает — `ntfy.session_lang`


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
    lang = body.lang or ntfy.session_lang(request.state.digest)     # до удаления прежней подписки сессии
    # у сессии (браузера) подписка одна: новая заменяет прежнюю, а не копится рядом мёртвым адресом
    state.DB.execute("DELETE FROM push_subs WHERE digest = ? AND endpoint != ?", (request.state.digest, body.endpoint))
    state.DB.execute(
        "INSERT INTO push_subs(endpoint, user_id, digest, p256dh, auth, created, lang) VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(endpoint) DO UPDATE SET user_id = excluded.user_id, digest = excluded.digest,"
        " p256dh = excluded.p256dh, auth = excluded.auth, created = excluded.created, lang = excluded.lang",
        (body.endpoint, uid, request.state.digest, b64u(keys[0]), b64u(keys[1]), clock.NOW(), lang))
    log.info("пользователь %d: подписка push", uid)
    return Response(status_code=204)


@router.delete("/me/push", status_code=204)
async def unsubscribe(request: Request):
    n = state.DB.execute("DELETE FROM push_subs WHERE digest = ?", (request.state.digest,)).rowcount
    log.info("пользователь %d: подписка push снята (%d)", request.state.user.id, n)
    return Response(status_code=204)


# ---- о чём уведомлять устройство -------------------------------------------------

class Prefs(BaseModel):
    """«О чём уведомлять» этой сессии — для push и ntfy сразу: снятые виды, тихие часы «ЧЧ:ММ» (оба или ни одного,
    не равные) по поясу учётки, напоминание о ждущей карточке, мин."""
    off: list[Literal[KINDS]] = Field(max_length=len(KINDS))
    quiet_from: str | None = Field(None, pattern=TIME)
    quiet_to: str | None = Field(None, pattern=TIME)
    remind: Literal[REMINDS]

    @model_validator(mode="after")
    def _window(self):
        if (self.quiet_from is None) != (self.quiet_to is None) or (
                self.quiet_from is not None and self.quiet_from == self.quiet_to):
            raise ValueError("quiet hours need a start and a different end")
        return self


def _minute(text: str | None) -> int | None:
    return None if text is None else int(text[:2]) * 60 + int(text[3:])


def _clock(minute: int | None) -> str | None:
    return None if minute is None else f"{minute // 60:02d}:{minute % 60:02d}"


def prefs(user_id: int, digest: str) -> dict:
    row = state.DB.execute("SELECT off, quiet_from, quiet_to, remind FROM notify_prefs WHERE digest = ?",
                           (digest,)).fetchone()
    zone = state.DB.execute("SELECT timezone FROM users WHERE id = ?", (user_id,)).fetchone()[0]
    if row is None:
        return {"off": [], "quiet_from": None, "quiet_to": None, "remind": REMIND_DEFAULT, "timezone": zone}
    return {"off": [k for k in KINDS if k in json.loads(row["off"])], "quiet_from": _clock(row["quiet_from"]),
            "quiet_to": _clock(row["quiet_to"]), "remind": row["remind"], "timezone": zone}


@router.get("/me/notify")
async def get_prefs(request: Request):
    return prefs(request.state.user.id, request.state.digest)


@router.put("/me/notify")
async def put_prefs(body: Prefs, request: Request):
    uid = request.state.user.id
    state.DB.execute(
        "INSERT INTO notify_prefs(digest, user_id, off, quiet_from, quiet_to, remind) VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(digest) DO UPDATE SET off = excluded.off, quiet_from = excluded.quiet_from,"
        " quiet_to = excluded.quiet_to, remind = excluded.remind",
        (request.state.digest, uid, json.dumps(sorted(set(body.off))), _minute(body.quiet_from),
         _minute(body.quiet_to), body.remind))
    log.info("пользователь %d: настройка уведомлений устройства сохранена", uid)
    return prefs(uid, request.state.digest)
