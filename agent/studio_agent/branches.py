"""Штатное ветвление Claude SDK; оригинальную сессию и файлы проекта не меняем."""

import uuid

import claude_agent_sdk

from . import sessions
from .errors import Refused


def claude(project, chat, before=None):
    messages = [message for message in sessions.get_session_messages(chat, directory=str(project))
                if message.parent_tool_use_id is None]
    if not messages:
        raise Refused("not_found", "У чата ещё нет истории для ветвления")
    cutoff, draft = None, ""
    if before is not None:
        index = next((i for i, message in enumerate(messages) if message.uuid == before
                      and message.type == "user" and sessions.text_of(message.message.get("content"))), None)
        if index is None:
            raise Refused("invalid", "Точка ветвления отсутствует в истории")
        draft = sessions.text_of(messages[index].message.get("content"))[:100_000]
        if index == 0:
            return {"chat": str(uuid.uuid4()), "draft": draft}
        cutoff = messages[index - 1].uuid
    try:
        result = claude_agent_sdk.fork_session(chat, directory=str(project), up_to_message_id=cutoff)
    except FileNotFoundError:
        raise Refused("not_found", "Сессия не найдена на компьютере") from None
    except (ValueError, AttributeError):
        raise Refused("unsupported", "Обновите Claude Code и SDK для ветвления этой сессии") from None
    return {"chat": result.session_id, "draft": draft}
