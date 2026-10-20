"""Вход по логину и паролю, сессии на сервере, пауза после неудач, защита от CSRF
(спека 2026-10-19-studio-pwa, раздел 7.2).

- Сессия — cookie `__Host-studio_sid` со случайным токеном (HttpOnly, Secure, SameSite=Lax,
  30 дней). Префикс `__Host-`: браузер примет её только с Secure, Path=/ и без Domain — соседний
  хост `*.example.com` не подсунет свою cookie на весь домен.
  В базе лежит только sha256 токена: копия базы входа не даёт. Срок скользит — запрос позже суток
  после прошлого продления отодвигает его, а ответ API ставит cookie заново. Отключённая учётка
  и истёкший срок стирают сессию: включение учётки старую сессию не оживляет.
- CSRF: каждый не-GET запрос `/api/*` несёт заголовок `x-studio-csrf` с токеном сессии (страница
  берёт его из `GET /api/me`); вход и выход сверяют Origin, если он есть, с CFG.origin.
- Пауза: 5 неудач за 15 минут на пару «логин + адрес» или на адрес целиком — 15 минут отказа
  и с верным паролем. Адрес петли из дома (CFG.hairpin_ip) считается только парой. Логин
  целиком не запирается: после пятой неудачи за 15 минут со всех адресов ответ на неверный
  пароль ждёт 2, 4, 8 … 30 с. Адрес — последний элемент X-Forwarded-For: путь к службе один,
  Apache (unix-сокет), начало заголовка мог прислать сам клиент.
- Блокировка на телефоне (спека 2026-10-21-studio-screen-lock, 6.2): сессия заперта, когда у учётки
  включена блокировка, сессия мобильная и отметки «на экране» (screenlock.py) не было дольше STALE_SEC.
  Запертой — 423 `screen_locked` на всё /api/*, кроме SCREEN_LOCK_PATHS. Не путать с паузой входа
  (LOCK_SEC, locked, 429 `locked`).
- Журнал — только адрес и итог: логин, пароль, cookie и токены туда не пишутся.
"""
import asyncio
import hashlib
import hmac
import html
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

import clock
import security
import state

COOKIE = "__Host-studio_sid"
SESSION_SEC = 30 * 86400
RENEW_SEC = 86400
LOCK_FAILS = 5
LOCK_SEC = 900
LOGIN_BODY_MAX = 4096
CSRF_HEADER = "x-studio-csrf"
LOGIN_RE = r"[a-z0-9._-]{2,32}"
DELAY_MAX = 30
STALE_SEC = 75
#: запросы самой блокировки: их запертая сессия делает (состояние, отметка, «заперто», отпирание)
SCREEN_LOCK_PATHS = frozenset({"/api/lock", "/api/lock/alive", "/api/lock/now", "/api/lock/challenge",
                               "/api/lock/open", "/api/lock/password"})

STATIC = Path(__file__).resolve().parent / "static"
log = logging.getLogger("studio")


def _static_version(root: Path) -> str:
    """Первые 12 hex sha256 всех файлов каталога (путь и содержимое): новая выкладка — новые ссылки."""
    digest = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file():
            digest.update(p.relative_to(root).as_posix().encode() + b"\0" + p.read_bytes())
    return digest.hexdigest()[:12]


#: версия статики при старте — `?v=` в ссылках страниц (`{{v}}`) и VERSION служебного работника
STATIC_VERSION = _static_version(STATIC)

#: ("pair", логин, адрес) и ("ip", адрес) → моменты неудач за последние LOCK_SEC.
FAILS: dict[tuple, list[float]] = {}
router = APIRouter()


@dataclass
class User:
    id: int
    login: str
    name: str
    is_admin: bool
    theme: str
    default_connection_id: int | None
    default_model: str | None


def client_ip(request: Request) -> str:
    """Последний элемент X-Forwarded-For — его дописал Apache; начало мог прислать сам клиент."""
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
    return forwarded or (request.client.host if request.client else "?")


# ---- сессии ----------------------------------------------------------------

def _set_cookie(response: Response, value: str) -> None:
    response.set_cookie(COOKIE, value, max_age=SESSION_SEC, path="/", secure=True, httponly=True, samesite="lax")


def create_session(user_id: int) -> tuple[str, str]:
    """(значение cookie, csrf). Токены — token_urlsafe, без `=`: Starlette не берёт их в кавычки."""
    now = clock.NOW()
    value, csrf = security.new_token(), security.new_token()
    state.DB.execute("DELETE FROM sessions WHERE expires <= ?", (now,))      # заодно забыть истёкшие
    # seen = now: только что вошедший паролем не заперт
    state.DB.execute("INSERT INTO sessions(digest, user_id, csrf, expires, renewed, seen) VALUES (?, ?, ?, ?, ?, ?)",
                     (security.digest(value), user_id, csrf, now + SESSION_SEC, now, now))
    return value, csrf


def screen_locked(enabled: int, mobile: int, seen: float | None, now: float) -> bool:
    """Заперта ли сессия: блокировка учётки включена, сессия мобильная, свежей отметки нет."""
    return bool(enabled) and bool(mobile) and (seen is None or now - seen > STALE_SEC)


def _live(value: str | None, renew: bool = True) -> tuple[User, str, bool, bool] | None:
    """(пользователь, csrf, продлена ли сейчас, заперта ли) живой сессии или None. renew=False — для
    ответов, которые cookie заново не ставят (страницы): продлит следующий запрос /api/* вместе с cookie."""
    if not value:
        return None
    key = security.digest(value)
    row = state.DB.execute(
        "SELECT u.id, u.login, u.name, u.is_admin, u.theme, u.default_connection_id, u.default_model,"
        " u.disabled, u.screen_lock, s.csrf, s.expires, s.renewed, s.mobile, s.seen"
        " FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.digest = ?", (key,)).fetchone()
    if row is None:
        return None
    now = clock.NOW()
    if row["disabled"] or row["expires"] <= now:
        state.DB.execute("DELETE FROM sessions WHERE digest = ?", (key,))
        return None
    renewed = renew and now - row["renewed"] > RENEW_SEC
    if renewed:
        state.DB.execute("UPDATE sessions SET expires = ?, renewed = ? WHERE digest = ?",
                         (now + SESSION_SEC, now, key))
    user = User(id=row["id"], login=row["login"], name=row["name"], is_admin=bool(row["is_admin"]),
                theme=row["theme"], default_connection_id=row["default_connection_id"],
                default_model=row["default_model"])
    return user, row["csrf"], renewed, screen_locked(row["screen_lock"], row["mobile"], row["seen"], now)


def session_user(value: str | None, renew: bool = True) -> tuple[User, str] | None:
    live = _live(value, renew)
    return live[:2] if live else None


def end_sessions(user_id: int, keep_digest: str | None = None) -> None:
    if keep_digest is None:
        state.DB.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    else:
        state.DB.execute("DELETE FROM sessions WHERE user_id = ? AND digest != ?", (user_id, keep_digest))


def require_admin(request: Request) -> User:
    user = getattr(request.state, "user", None)
    if user is None or not user.is_admin:
        raise HTTPException(403, {"code": "admin", "message": "только для администратора"})
    return user


async def guard(request: Request, call_next):
    """Все /api/* — только с живой сессией; не-GET — ещё и с заголовком CSRF этой сессии; запертой
    сессии — только запросы блокировки. Порядок: сессия → CSRF → блокировка."""
    path = request.url.path
    if path != "/api" and not path.startswith("/api/"):
        return await call_next(request)
    live = _live(request.cookies.get(COOKIE))
    if live is None:
        return JSONResponse({"detail": {"code": "login", "message": "нужен вход"}}, status_code=401)
    user, csrf, renewed, screen = live
    if request.method != "GET":
        sent = request.headers.get(CSRF_HEADER, "")
        # байты: compare_digest со строкой не-ASCII падает TypeError
        if not hmac.compare_digest(sent.encode("utf-8"), csrf.encode("utf-8")):
            return JSONResponse({"detail": {"code": "csrf", "message": "обновите страницу"}}, status_code=403)
    if screen and path not in SCREEN_LOCK_PATHS:
        response = JSONResponse({"detail": {"code": "screen_locked", "message": "студия заперта"}}, status_code=423,
                                headers={"Cache-Control": "no-store"})
    else:
        request.state.user, request.state.csrf = user, csrf
        request.state.locked, request.state.digest = screen, security.digest(request.cookies[COOKIE])
        response = await call_next(request)
    if renewed:                      # срок в браузере догоняет продлённый на сервере
        _set_cookie(response, request.cookies[COOKIE])
    # ответы API (токен прямого подключения, csrf в /api/me, чаты) — не в дисковый кеш браузера;
    # свой заголовок (media: private, max-age) остаётся
    response.headers.setdefault("Cache-Control", "no-store")
    return response


# ---- пауза после неудач ----------------------------------------------------

def _keys(login: str, ip: str) -> list[tuple]:
    keys = [("pair", login, ip)]
    if ip != state.CFG.hairpin_ip:   # петля из дома: весь дом — один адрес, его целиком не запираем
        keys.append(("ip", ip))
    return keys


def locked(login: str, ip: str) -> bool:
    return any(locked_key(key) for key in _keys(login, ip))


def locked_key(key: tuple) -> bool:
    """LOCK_FAILS неудач по ключу за LOCK_SEC. Заодно забывает неудачи старше LOCK_SEC: иначе
    каждый, кто хоть раз ошибся, оставался бы в FAILS навсегда."""
    now = clock.NOW()
    for k in list(FAILS):
        FAILS[k] = [t for t in FAILS[k] if now - t < LOCK_SEC]
        if not FAILS[k]:
            del FAILS[k]
    return len(FAILS.get(key, [])) >= LOCK_FAILS


def _login_fails(login: str) -> int:
    return sum(len(times) for key, times in FAILS.items() if key[0] == "pair" and key[1] == login)


def _forget(key: tuple, attempt: float) -> None:
    times = FAILS.get(key)
    if times and attempt in times:
        times.remove(attempt)
        if not times:
            del FAILS[key]


# ---- страницы --------------------------------------------------------------

def page(name: str, error: str = "") -> str:
    return ((STATIC / name).read_text(encoding="utf-8")
            .replace("{{error}}", html.escape(error)).replace("{{v}}", STATIC_VERSION))


def _login_page(error: str = "", status: int = 200) -> HTMLResponse:
    return HTMLResponse(page("login.html", error), status_code=status, headers={"Cache-Control": "no-store"})


def _foreign(request: Request) -> bool:
    origin = request.headers.get("origin")
    return origin is not None and origin != state.CFG.origin.rstrip("/")


async def small_body(request: Request, limit: int) -> bytes | None:
    """Тело не длиннее `limit` или None. Читается потоком: вход открыт без сессии, и ни
    Content-Length, ни chunked не заставят держать в памяти больше предела."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return body


@router.api_route("/login", methods=["GET", "HEAD"])
async def login_page(request: Request):
    if session_user(request.cookies.get(COOKIE), renew=False):
        return RedirectResponse("/", status_code=303)
    return _login_page()


@router.post("/login")
async def login(request: Request):
    ip = client_ip(request)
    if _foreign(request):
        log.warning("вход с %s: чужой Origin", ip)
        return _login_page("Запрос пришёл с чужого сайта", 403)
    body = await small_body(request, LOGIN_BODY_MAX)
    if body is None:
        log.warning("вход с %s: тело длиннее %d байт", ip, LOGIN_BODY_MAX)
        return _login_page("Слишком длинный запрос", 413)
    form = parse_qs(body.decode("utf-8", "replace"))
    name = (form.get("login") or [""])[0].strip().lower()
    password = (form.get("password") or [""])[0]
    if locked(name, ip):
        log.warning("вход с %s: пауза", ip)
        return _login_page("Слишком много попыток — подождите 15 минут", 429)
    # неудача записывается ДО проверки пароля и снимается при успехе: пачка параллельных попыток
    # не проходит проверку паузы разом, пока считается хеш
    attempt = clock.NOW()
    for key in _keys(name, ip):
        FAILS.setdefault(key, []).append(attempt)
    row = None
    if re.fullmatch(LOGIN_RE, name):
        row = state.DB.execute("SELECT id, pw_hash FROM users WHERE login = ? AND disabled = 0", (name,)).fetchone()
    # неизвестный логин проверяется против DUMMY_HASH: по времени ответа его не отличить
    ok = await asyncio.to_thread(security.verify_password, password,
                                 row["pw_hash"] if row else security.DUMMY_HASH)
    if ok and row:
        FAILS.pop(("pair", name, ip), None)
        _forget(("ip", ip), attempt)  # только эту попытку: свой вход не обнуляет счёт адреса
        replaced = request.cookies.get(COOKIE)
        if replaced:                  # прежняя сессия этого браузера иначе лежала бы до срока
            state.DB.execute("DELETE FROM sessions WHERE digest = ?", (security.digest(replaced),))
        value, _ = create_session(row["id"])
        log.info("вход с %s: успешно", ip)
        response = RedirectResponse("/", status_code=303)
        _set_cookie(response, value)
        return response
    fails = _login_fails(name)
    if fails > LOCK_FAILS:
        await clock.SLEEP(min(2 ** (fails - LOCK_FAILS), DELAY_MAX))
    log.warning("вход с %s: неверно", ip)
    return _login_page("Неверный логин или пароль", 401)


@router.post("/logout")
async def logout(request: Request):
    if _foreign(request):
        return _login_page("Запрос пришёл с чужого сайта", 403)
    value = request.cookies.get(COOKIE)
    if value:
        state.DB.execute("DELETE FROM sessions WHERE digest = ?", (security.digest(value),))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    return response
