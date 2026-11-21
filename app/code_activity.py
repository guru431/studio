"""Компактные статусы всех проектов открытого компьютера, без истории и текстов."""
import asyncio

from fastapi import APIRouter, HTTPException, Request

import bridge
import code_attention
import state
import project_access
from claude_code import ask, open_without_pin

router = APIRouter(prefix="/api/code")
SUMMARY_TIMEOUT = 5


async def session_activity(request: Request, aid: int, data: dict, *, passive: bool = False) -> list:
    """Названия — из каталога моста; непрочитанные отметки живут до просмотра этой учёткой, без часового лимита
    (участнику — завершения в выданных проектах после выдачи)."""
    agent = project_access.agent(request.state.user.id, aid, shared=True)
    allowed = None if agent["user_id"] == request.state.user.id else project_access.projects(request.state.user.id, aid)
    items = {}
    for item in data.get("items", []):
        if (isinstance(item, dict) and isinstance(item.get("project"), str)
                and bridge.PID.fullmatch(item["project"]) and isinstance(item.get("chat"), str)
                and bridge.UUID.fullmatch(item["chat"]) and (item.get("running") or item.get("asking"))):
            clean = {k: v for k, v in item.items() if k != "queue_paused" or v is True}
            items[item["chat"]] = {**clean, "running": True, "title": None}
    since = None if allowed is None else {r[0]: r[1] for r in state.DB.execute(
        "SELECT project, created FROM project_members WHERE agent_id = ? AND user_id = ?",
        (aid, request.state.user.id)) if r[0] in allowed}
    for row in code_attention.unread(aid, request.state.user.id, since):
        item = items.setdefault(row["chat"], {"project": row["project"], "chat": row["chat"],
                                             "running": False, "asking": False, "engine": "claude", "title": None})
        item.update(unread=True, attention=row["revision"], finished=row["finished"])
    limit = asyncio.Semaphore(4)

    async def titles(project):
        try:
            async with limit:
                found = await ask(request, aid, "chats", quiet=True, passive=passive, project=project)
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
    if allowed is not None:
        project_access.agent(request.state.user.id, aid, shared=True)
        allowed = project_access.projects(request.state.user.id, aid)
    return [i for i in items.values() if allowed is None or i["project"] in allowed]


@router.get("/activity")
async def all_activity(request: Request):
    """Сводка для значка раздела: только свои доступные компьютеры, PIN не открываем (компьютер без PIN
    открывается со строкой `open` в истории подключений). Значок опрашивает её из любого раздела — все просьбы
    фоновые и срок PIN компьютера не продлевают."""
    agents = [dict(row) for row in state.DB.execute("SELECT id, name, user_id, has_pin FROM agents WHERE user_id = ? OR id IN"
        " (SELECT m.agent_id FROM project_members m JOIN agents a ON a.id = m.agent_id"
        " JOIN users u ON u.id = a.user_id WHERE m.user_id = ? AND u.disabled = 0) ORDER BY id",
        (request.state.user.id, request.state.user.id))
              if (link := bridge.LINKS.get(row["id"])) is not None and link.hello.get("project_activity")]

    async def read(agent):
        aid = agent["id"]
        try:
            async with asyncio.timeout(SUMMARY_TIMEOUT):
                data = await open_without_pin(
                    request, agent, lambda: ask(request, aid, "activity", quiet=True, passive=True))
                items = await session_activity(request, aid, data, passive=True)
            return {"aid": aid, "items": items}
        except (HTTPException, TimeoutError):
            return {"aid": aid, "items": []}

    return {"items": await asyncio.gather(*(read(agent) for agent in agents))}
