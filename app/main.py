"""Студия на https://studio.example.com (спека docs/superpowers/specs/2026-10-19-studio-pwa-design.md).

Служба слушает только unix-сокет за Apache. Анонимны `/health`, `/login` (страница и вход),
`/logout` и оболочка PWA: `/static/*`, `/manifest.webmanifest`, `/sw.js`, `/offline.html`;
`/` без живой сессии уводит на вход, всё `/api/*` стережёт `auth.guard`.

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
import auth
import chat
import config
import connections
import db
import images
import media
import profile
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


@asynccontextmanager
async def lifespan(_app):
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    log.setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    state.CFG = config.load()
    state.DB = db.connect(state.CFG.db_path)
    db.migrate(state.DB)
    images.recover()
    chat.recover()
    state.HTTP = state.make_http()
    watcher = videos.start_watcher() if state.WATCHER else None
    try:
        yield
    finally:
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
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
app.include_router(admin.router)
app.include_router(media.router)
app.include_router(images.router)
app.include_router(videos.router)
app.include_router(chat.router)


@app.exception_handler(RequestValidationError)
async def invalid_request(_request: Request, _exc: RequestValidationError):
    """Отказ в общем формате и без разбора полей: разбор FastAPI повторяет присланное, в том числе пароль."""
    return JSONResponse({"detail": {"code": "invalid", "message": "неверные данные запроса"}}, status_code=422)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def index(request: Request):
    if auth.session_user(request.cookies.get(auth.COOKIE)) is None:
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(auth.page("index.html"), headers={"Cache-Control": "no-store"})


@app.get("/sw.js")
async def service_worker():
    """Из корня: область работника — весь сайт (`Service-Worker-Allowed: /`)."""
    return Response(SW_BODY, media_type="text/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(auth.STATIC / "manifest.webmanifest", media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


@app.get("/offline.html")
async def offline():
    return HTMLResponse(auth.page("offline.html"), headers={"Cache-Control": "no-cache"})
