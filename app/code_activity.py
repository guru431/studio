"""Компактные статусы всех проектов открытого компьютера, без истории и текстов."""
import asyncio

from fastapi import APIRouter, HTTPException, Request

import bridge
import state
from claude_code import _refuse, ask, own_agent

router = APIRouter(prefix="/api/code")
SUMMARY_TIMEOUT = 5


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
            return {"aid": aid, "items": data.get("items", [])}
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
