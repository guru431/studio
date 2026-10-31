"""Компактные статусы всех проектов открытого компьютера, без истории и текстов."""
from fastapi import APIRouter, Request

import bridge
from claude_code import _refuse, ask, own_agent

router = APIRouter(prefix="/api/code")


@router.get("/agents/{aid}/activity")
async def activity(request: Request, aid: int):
    own_agent(request, aid)
    link = bridge.LINKS.get(aid)
    if link is not None and not link.hello.get("project_activity"):
        raise _refuse(409, "unsupported", "обновите мост для статусов проектов")
    return await ask(request, aid, "activity")
