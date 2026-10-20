"""Студия на https://studio.example.com (спека docs/superpowers/specs/2026-10-19-studio-pwa-design.md).

Служба слушает только unix-сокет за Apache. Анонимны `/health`, `/login` (страница и вход),
`/logout`, `/robots.txt` (роботам — ничего) и оболочка PWA: `/static/*`, `/manifest.webmanifest`,
`/sw.js`, `/offline.html`, значки из корня (`/apple-touch-icon.png`, `/apple-touch-icon-precomposed.png`,
`/favicon.ico`); `/` без живой сессии уводит на вход, всё `/api/*` стережёт `auth.guard`.
Страницы отвечают и на HEAD: им проверяют адрес и строят превью ссылок.

У HTML-ответов — CSP без inline-скриптов (`CSP`); у всех — nosniff и Referrer-Policy. Статика
по ссылке с `?v=` (версия — `STATIC_VERSION`) хранится в браузере год, без неё — сверяется
каждый раз: модули `js/*` грузят друг друга без версии.
"""
import asyncio
import logging
import mimetypes
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

import admin
import audio
import auth
import chat
import config
import connections
import db
import dictation
import images
import media
import profile
import sections
import state
import videos

log = logging.getLogger("studio")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; "
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
    audio.clean_tmp()
    images.recover()
    chat.recover()
    state.HTTP = state.make_http()
    loops = [videos.start_watcher(), chat.start_sweeper()] if state.WATCHER else []
    try:
        yield
    finally:
        for loop in loops:
            loop.cancel()
        await asyncio.gather(*loops, return_exceptions=True)
        await images.stop()
        await chat.stop_all()
        await state.HTTP.aclose()
        state.DB.close()


async def headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    if response.headers.get("content-type", "").startswith("text/html"):
        response.headers["Content-Security-Policy"] = CSP
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = VERSIONED if "v" in request.query_params else "no-cache"
    return response


app = FastAPI(title="studio", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.middleware("http")(auth.guard)
app.middleware("http")(headers)
app.mount("/static", StaticFiles(directory=auth.STATIC), name="static")
app.include_router(auth.router)
app.include_router(profile.router)
app.include_router(connections.router)
app.include_router(sections.router)
app.include_router(admin.router)
app.include_router(media.router)
app.include_router(images.router)
app.include_router(videos.router)
app.include_router(chat.router)
app.include_router(dictation.router)


@app.exception_handler(RequestValidationError)
async def invalid_request(_request: Request, _exc: RequestValidationError):
    """Отказ в общем формате и без разбора полей: разбор FastAPI повторяет присланное, в том числе пароль."""
    return JSONResponse({"detail": {"code": "invalid", "message": "неверные данные запроса"}}, status_code=422)


@app.api_route("/health", methods=PAGE)
async def health():
    return {"status": "ok"}


@app.api_route("/robots.txt", methods=PAGE)
async def robots():
    return Response(ROBOTS, media_type="text/plain", headers={"Cache-Control": "no-cache"})


@app.api_route("/", methods=PAGE)
async def index(request: Request):
    # renew=False: страница cookie заново не ставит — продлит первый запрос /api/* вместе с cookie
    if auth.session_user(request.cookies.get(auth.COOKIE), renew=False) is None:
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(auth.page("index.html"), headers={"Cache-Control": "no-store"})


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
