"""Студия на https://studio.example.com (спека docs/superpowers/specs/2026-10-19-studio-pwa-design.md).

Служба слушает только unix-сокет за Apache. Анонимны `/health`, `/login` (страница и вход),
`/logout`, `/robots.txt` (роботам — ничего), PIN-вход `/share` и оболочка PWA: `/static/*`, `/manifest.webmanifest`,
`/sw.js`, `/offline.html`, значки из корня (`/apple-touch-icon.png`, `/apple-touch-icon-precomposed.png`,
`/favicon.ico`); `/` без живой сессии уводит на вход, всё `/api/*` стережёт `auth.guard`.
Публичные `/share/unlock`, `/share/check`, `/share/file` проверяют отдельные PIN/read-токены;
доступа к API учётки они не дают, секреты несут только телом запроса, ответы не кэшируются.
Страницы отвечают и на HEAD: им проверяют адрес и строят превью ссылок.

У HTML-ответов — CSP с хешем единственной доверенной importmap (`CSP`); у всех — nosniff и Referrer-Policy.
Версия `STATIC_VERSION` фиксирует шаблоны и снимок `/static/v/<hash>/` на время процесса.
Importmap сохраняет один экземпляр API-модуля; неизменяемая статика хранится в браузере год.
Обычные ссылки `/static/*` без версии по-прежнему проверяются через сеть.
"""
import asyncio
import html
import json
import logging
import mimetypes
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

import account
import admin
import agents
import audio
import auth
import bridge
import chat
import chat_exports
import chat_shares
import cleaner_client as cleaner
import checks
import claude_code
import code_usage
import code_activity
import config
import connections
import db
import desktop
import dictation
import images
import jobs
import media
import ocr
import ntfy
import presets
import profile
import project_access
import release
import push
import screenlock
import security
import search
import sections
import speeches
import state
import static_assets
import takeout
import transcripts
import translate
import videos
import voices
import workspace

log = logging.getLogger("studio")

CSP = (f"default-src 'self'; script-src 'self' 'sha256-{auth.MODULE_IMPORTS_HASH}'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; "
       "media-src 'self' blob:; connect-src 'self' https: http://localhost:* http://127.0.0.1:*; "
       "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
VERSIONED = "public, max-age=31536000, immutable"
STATIC_VERSION = auth.STATIC_VERSION
# реестр Windows отдаёт .js как text/plain, и браузер модуль не исполнит
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("application/manifest+json", ".webmanifest")
SW_BODY = f'const VERSION = "{STATIC_VERSION}";\n' + (auth.STATIC / "sw.js").read_text(encoding="utf-8")
ROBOTS = "User-agent: *\nDisallow: /\n"
PAGE = ["GET", "HEAD"]


@asynccontextmanager
async def lifespan(_app):
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    log.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    state.CFG = config.load()
    state.DB = db.connect(state.CFG.db_path)
    db.migrate(state.DB)
    chat_shares.seal()                           # снимки ссылок открытым текстом (до шага 37, откат) — шифром
    chat_shares.disown()                         # ссылки участников на чужие компьютеры (прежний код, откат) — прочь
    sections.adopt("translate", "chat")          # раз на базу: модели чата через сервер — в переводчик
    search.compose_once(state.DB)                # раз на базу: прежние тела индекса — в NFC
    chat_exports.clean_tmp()
    audio.clean_tmp()
    images.recover()
    chat.recover()
    transcripts.recover()
    ocr.recover()
    translate.recover()
    speeches.recover()
    videos.recover()
    state.HTTP = state.make_http()
    state.CLEANER_HTTP = state.make_cleaner_http()
    transcripts.resume()                         # ждущие в очереди — дальше (после recover и HTTP)
    speeches.resume()
    loops =([videos.start_watcher(), chat.start_sweeper(), media.start_purger(), transcripts.start_backfill(),
              asyncio.create_task(cleaner.sweep())]
             if state.WATCHER else [])
    try:
        yield
    finally:
        for loop in loops:
            loop.cancel()
        await asyncio.gather(*loops, return_exceptions=True)
        await images.stop()
        await transcripts.stop()
        await translate.stop()                   # раньше ocr: перевод, ждущий скан, не начнёт следующее
        await ocr.stop()
        await speeches.stop()
        await chat.stop_all()
        await bridge.stop()
        await push.stop()
        cleaner.TICKETS.clear()
        cleaner.LOCKS.clear()
        if state.CLEANER_HTTP is not state.HTTP:
            await state.CLEANER_HTTP.aclose()
        await state.HTTP.aclose()
        state.DB.close()


async def headers(request: Request, call_next):
    response = await chat_shares.public_guard(request, call_next)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.url.path == "/share" or request.url.path.startswith("/share/"):
        response.headers.update(chat_shares.PUBLIC_HEADERS)
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers.setdefault("Content-Security-Policy", CSP)
    if request.url.path.startswith("/static/"):
        versioned = "v" in request.query_params or request.url.path.startswith(f"/static/v/{STATIC_VERSION}/")
        response.headers["Cache-Control"] = (VERSIONED if response.status_code in (200, 304) else 'no-store') if versioned else "no-cache"
    return response


app = FastAPI(title="studio", version=release.VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.middleware("http")(auth.guard)
app.middleware("http")(headers)
app.mount(f"/static/v/{STATIC_VERSION}", static_assets.Snapshot(auth.STATIC), name="static-versioned")
app.mount("/static", StaticFiles(directory=auth.STATIC), name="static")
app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(account.router)
app.include_router(screenlock.router)
app.include_router(connections.router)
app.include_router(checks.router)
app.include_router(sections.router)
app.include_router(admin.router)
app.include_router(media.router)
app.include_router(presets.router)          # раньше images: /api/images/presets — не id картинки
app.include_router(images.router)
app.include_router(videos.router)
app.include_router(chat.router)
app.include_router(chat_exports.router)
app.include_router(chat_shares.router)
app.include_router(dictation.router)
app.include_router(transcripts.router)
app.include_router(ocr.router)
app.include_router(translate.router)
app.include_router(voices.router)
app.include_router(speeches.router)
app.include_router(jobs.router)
app.include_router(search.router)
app.include_router(takeout.router)
app.include_router(agents.router)
app.include_router(bridge.router)
app.include_router(workspace.router)
app.include_router(claude_code.router)
app.include_router(project_access.router)
app.include_router(desktop.router)
app.include_router(code_usage.router)
app.include_router(code_activity.router)
app.include_router(push.router)
app.include_router(ntfy.router)


@app.exception_handler(RequestValidationError)
async def invalid_request(_request: Request, _exc: RequestValidationError):
    """Отказ в общем формате и без разбора полей: разбор FastAPI повторяет присланное, в том числе пароль."""
    return JSONResponse({"detail": {"code": "invalid", "message": "неверные данные запроса"}}, status_code=422)


@app.api_route("/health", methods=PAGE)
async def health():
    return {"status": "ok", "version": release.VERSION}


@app.api_route("/robots.txt", methods=PAGE)
async def robots():
    return Response(ROBOTS, media_type="text/plain", headers={"Cache-Control": "no-cache"})


@app.api_route("/", methods=PAGE)
async def index(request: Request):
    # renew=False: страница cookie заново не ставит — продлит первый запрос /api/* вместе с cookie
    session = auth.session_user(request.cookies.get(auth.COOKIE), renew=False)
    if session is None:
        return RedirectResponse("/login", status_code=303)
    user, csrf = session
    # Hash-маршрут виден только браузеру: security даёт истечение общей сессии,
    # а lock.js применяет область защиты к открытому разделу до показа панели.
    lock = screenlock.snapshot(user.id, security.digest(request.cookies[auth.COOKIE]), csrf, "security")
    page = auth.page("index.html").replace("{{lock_state}}", html.escape(json.dumps(lock)))
    return HTMLResponse(page, headers={"Cache-Control": "no-store"})


@app.api_route("/sw.js", methods=PAGE)
async def service_worker():
    """Из корня: область работника — весь сайт (`Service-Worker-Allowed: /`)."""
    return Response(SW_BODY, media_type="text/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.api_route("/manifest.webmanifest", methods=PAGE)
async def manifest():
    return FileResponse(auth.STATIC / "manifest.webmanifest", media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


@app.api_route("/apple-touch-icon.png", methods=PAGE)
@app.api_route("/apple-touch-icon-precomposed.png", methods=PAGE)
async def apple_touch_icon():
    """iOS спрашивает значок из корня сам, если не нашёл подходящий в странице."""
    return FileResponse(auth.STATIC / "icons" / "apple-touch-icon.png", media_type="image/png",
                        headers={"Cache-Control": "no-cache"})


@app.api_route("/favicon.ico", methods=PAGE)
async def favicon():
    return FileResponse(auth.STATIC / "icons" / "favicon.ico", media_type="image/x-icon",
                        headers={"Cache-Control": "no-cache"})


@app.api_route("/offline.html", methods=PAGE)
async def offline():
    return HTMLResponse(auth.page("offline.html"), headers={"Cache-Control": "no-cache"})
