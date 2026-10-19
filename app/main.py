"""Студия на https://studio.example.com (спека docs/superpowers/specs/2026-10-19-studio-pwa-design.md).

Служба слушает только unix-сокет за Apache. Анонимны `/health`, `/login` (страница и вход) и
`/logout`; `/` без живой сессии уводит на вход, всё `/api/*` стережёт `auth.guard`.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

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


app = FastAPI(title="studio", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.middleware("http")(auth.guard)
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
