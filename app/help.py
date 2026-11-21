"""«Справка» (#/help): руководство Studio (docs/guide.ru.md) и руководства MCP-серверов учётки — ресурсы MCP
`text/markdown` с `annotations.audience`, где есть `user` (docs/architecture/mcp-contracts.md#руководство).

Текст отдаётся Markdown: страница рисует его тем же безопасным рендером, что ответы чата (HTML — текстом).
Относительные ссылки репозитория и якоря становятся простым текстом: на сайте их адресов нет, а якорь сменил бы
раздел страницы. Ресурс читается, только если сервер сам перечислил его как руководство для пользователя.
"""

import asyncio
from functools import lru_cache
import logging
from pathlib import Path
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import mcp_client as mcp
import mcp_sources

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/help")

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "guide.ru.md"
MIME = "text/markdown"
NAME_MAX = 160
#: ссылка или картинка Markdown, чей адрес — не http(s) и не почта: относительный путь репозитория или якорь.
#: Подпись без `[`, адрес без скобок и пробелов — разбор линейный: длинная строка из `[` руководства MCP не держит
#: цикл событий (ресурс до 1 МиБ)
LOCAL_LINK = re.compile(r"!?\[([^\[\]\n]*)\]\((?!https?://|mailto:)[^()\s]*\)")
FENCE = re.compile(r"^\s*(```|~~~)")
NOT_FOUND = "Руководство не найдено"


def plain_links(text: str) -> str:
    return LOCAL_LINK.sub(r"\1", text)


def studio_text(text: str) -> str:
    """Руководство без вступления до первого заголовка `## `, локальные ссылки — простым текстом."""
    first = re.search(r"^## ", text, re.M)
    return plain_links(text[first.start():] if first else "")


def headings(text: str) -> list[str]:
    """Заголовки `## ` по порядку, кроме строк внутри блоков кода."""
    found, fenced = [], False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced and line.startswith("## "):
            found.append(line[3:].strip())
    return found


@lru_cache(maxsize=1)
def _guide(_stamp) -> str:
    return studio_text(GUIDE.read_text(encoding="utf-8"))


def guide() -> str:
    """Текст руководства Studio (кэш до смены файла); нет файла — 404."""
    try:
        return _guide(GUIDE.stat().st_mtime_ns)
    except OSError:
        raise mcp.refused(NOT_FOUND, "not_found", 404) from None


def _for_user(item) -> bool:
    annotations = item.get("annotations")
    audience = annotations.get("audience") if isinstance(annotations, dict) else None
    return item.get("mimeType") == MIME and isinstance(audience, list) and "user" in audience


def _name(item) -> str:
    for key in ("title", "name"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return "".join(c for c in value if c.isprintable()).strip()[:NAME_MAX]
    return item["uri"][:NAME_MAX]


async def guides(session) -> list[dict]:
    """Руководства сервера для пользователя: [{uri, name}]; сервер без возможности `resources` — пусто."""
    async with asyncio.timeout(mcp_sources.LIST_SEC):
        found = await mcp_sources.catalog(session, mcp_sources.LIST_SEC)
        if not isinstance(found.capabilities.get("resources"), dict):
            return []
        return [{"uri": item["uri"], "name": _name(item)} for item in await session.resources() if _for_user(item)]


async def _server(item, uid):
    session = mcp.Session(item, uid=uid)
    try:
        resources = await guides(session)
    except Exception as exc:                     # сбой одного сервера не роняет «Справку»; текст исключения — никуда
        log.info("mcp %d: руководства не прочитаны (%s)", item["id"], type(exc).__name__)
        return None
    finally:
        await session.close()
    return {"sid": item["id"], "name": item["name"], "resources": resources} if resources else None


@router.get("")
async def index(request: Request):
    """Разделы руководства Studio и руководства включённых серверов MCP учётки."""
    uid = request.state.user.id
    try:
        titles = headings(guide())
    except HTTPException:
        titles = []
    servers = await asyncio.gather(*(_server(item, uid) for item in mcp.servers(uid, enabled_only=True)))
    return {"studio": {"sections": [{"id": f"s{n}", "title": title} for n, title in enumerate(titles, 1)]},
            "servers": [server for server in servers if server is not None]}


@router.get("/studio")
async def studio_guide():
    return {"markdown": guide()}


class ServerGuideIn(BaseModel):
    sid: int
    uri: str = Field(min_length=1, max_length=mcp.RESOURCE_URI_MAX)


@router.post("/server")
async def server_guide(body: ServerGuideIn, request: Request):
    """Текст руководства сервера: только ресурс, который сервер перечислил как руководство для пользователя."""
    uid = request.state.user.id
    session = mcp.Session(mcp.row(uid, body.sid), uid=uid)
    try:
        try:
            listed = await guides(session)
        except TimeoutError:
            raise mcp.refused("MCP не ответил вовремя") from None
        if body.uri not in {item["uri"] for item in listed}:
            raise mcp.refused(NOT_FOUND, "not_found", 404)
        return {"markdown": plain_links(await session.read_resource(body.uri))}
    finally:
        await session.close()
