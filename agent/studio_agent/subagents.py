"""Просмотр штатных дочерних сессий Claude без управления их исполнением."""

import claude_agent_sdk

from . import sessions
from .errors import Refused


def list_children(project, chat):
    if not sessions.exists(project, chat):
        raise Refused("not_found", "Сессия не найдена")
    return {"items": [{"id": aid, "title": f"Агент {aid}", "status": "unknown"}
                      for aid in claude_agent_sdk.list_subagents(chat, directory=str(project))[:50]]}


def history(project, chat, child):
    if child not in {row["id"] for row in list_children(project, chat)["items"]}:
        raise Refused("not_found", "Подзадача не принадлежит этому чату")
    raw = claude_agent_sdk.get_subagent_messages(chat, child, directory=str(project), limit=200)
    normalized = sessions.normalize(raw, include_subagents=True)
    page = sessions.fit(normalized)
    return {"items": page, "complete": len(raw) < 200 and len(page) == len(normalized)}
