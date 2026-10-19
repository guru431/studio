"""Студия на https://studio.example.com (спека docs/superpowers/specs/2026-10-19-studio-pwa-design.md).

Служба слушает только unix-сокет за Apache. Анонимны `/health`, `/login` (страница и вход) и
`/logout`; `/` без живой сессии уводит на вход, всё `/api/*` стережёт `auth.guard`.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse

import auth
import config
import db
import state

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
    state.HTTP = state.make_http()
    try:
        yield
    finally:
        await state.HTTP.aclose()
        state.DB.close()


app = FastAPI(title="studio", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.middleware("http")(auth.guard)
app.include_router(auth.router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def index(request: Request):
    if auth.session_user(request.cookies.get(auth.COOKIE)) is None:
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(auth.page("index.html"), headers={"Cache-Control": "no-store"})
