"""Компактные статусы всех проектов открытого компьютера, без истории и текстов."""
import asyncio

from fastapi import APIRouter, HTTPException, Request

import bridge
import state
from claude_code import _refuse, ask, own_agent

router = APIRouter(prefix="/api/code")
SUMMARY_TIMEOUT = 5


async def session_activity(request: Request, aid: int, data: dict) -> list:
    """Названия — из каталога моста; непрочитанные отметки живут до просмотра, без часового лимита."""
    items = {}
    for item in data.get("items", []):
        if (isinstance(item, dict) and isinstance(item.get("project"), str)
                and bridge.PID.fullmatch(item["project"]) and isinstance(item.get("chat"), str)
                and bridge.UUID.fullmatch(item["chat"]) and (item.get("running") or item.get("asking"))):
            items[item["chat"]] = {**item, "running": True, "title": None}
    for row in state.DB.execute("SELECT project, chat, revision, finished FROM code_attention"
                                " WHERE agent_id = ? AND seen = 0 ORDER BY finished DESC", (aid,)):
        item = items.setdefault(row["chat"], {"project": row["project"], "chat": row["chat"],
                                             "running": False, "asking": False, "engine": "claude", "title": None})
        item.update(unread=True, attention=row["revision"], finished=row["finished"])
    limit = asyncio.Semaphore(4)

    async def titles(project):
        try:
            async with limit:
                found = await ask(request, aid, "chats", quiet=True, project=project)
            for session in found.get("items", []):
                item = items.get(session.get("chat")) if isinstance(session, dict) else None
                if item is None or item["project"] != project:
                    continue
                title = session.get("title")
                item["title"] = title[:500] if isinstance(title, str) else None
                if not item["running"] and session.get("engine") in ("claude", "codex", "opencode"):
                    item["engine"] = session["engine"]
        except HTTPException as error:
            if error.detail.get("code") in ("closed", "offline"):
                raise
            # Сбой каталога не должен скрывать работающую сессию.

    try:
        async with asyncio.timeout(2):
            await asyncio.gather(*(titles(project) for project in {i["project"] for i in items.values()}))
    except TimeoutError:
        pass
    return list(items.values())


@router.get("/activity")
async def all_activity(request: Request):
    """Сводка для значка раздела: только свои доступные компьютеры, PIN не открываем."""
    agents = [dict(row) for row in state.DB.execute("SELECT id, has_pin FROM agents WHERE user_id = ? ORDER BY id",
                                                   (request.state.user.id,))
              if (link := bridge.LINKS.get(row["id"])) is not None and link.hello.get("project_activity")]

    async def read(agent):
        aid = agent["id"]
        try:
            async with asyncio.timeout(SUMMARY_TIMEOUT):
                try:
                    data = await ask(request, aid, "activity", quiet=True)
                except HTTPException as error:
                    if error.detail.get("code") != "closed" or agent["has_pin"]:
                        raise
                    await ask(request, aid, "open", quiet=True, pin=None)
                    data = await ask(request, aid, "activity", quiet=True)
                items = await session_activity(request, aid, data)
            return {"aid": aid, "items": items}
        except (HTTPException, TimeoutError):
            return {"aid": aid, "items": []}

    return {"items": await asyncio.gather(*(read(agent) for agent in agents))}


@router.get("/agents/{aid}/activity")
async def activity(request: Request, aid: int):
    own_agent(request, aid)
    link = bridge.LINKS.get(aid)
    if link is not None and not link.hello.get("project_activity"):
        raise _refuse(409, "unsupported", "обновите мост для статусов проектов")
    return await ask(request, aid, "activity")
