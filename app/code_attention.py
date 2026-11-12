"""Отметки завершения и просмотра сессий. Только идентификаторы, без названий и переписки."""
import secrets

import clock
import state


def completed(aid: int, project: str, chat: str, finished: float | None = None) -> str:
    """`finished` — время конца хода, если он кончился раньше вести о нём (досланное завершение)."""
    revision = secrets.token_hex(16)
    state.DB.execute(
        "INSERT INTO code_attention(agent_id, project, chat, revision, finished, seen) VALUES (?, ?, ?, ?, ?, 0)"
        " ON CONFLICT(agent_id, chat) DO UPDATE SET project = excluded.project, revision = excluded.revision,"
        " finished = excluded.finished, seen = 0",
        (aid, project, chat, revision, clock.NOW() if finished is None else finished))
    return revision


def revision(aid: int, project: str, chat: str) -> str | None:
    row = state.DB.execute("SELECT revision FROM code_attention WHERE agent_id = ? AND project = ? AND chat = ?",
                           (aid, project, chat)).fetchone()
    return row["revision"] if row is not None else None


def seen(aid: int, project: str, chat: str, value: str) -> None:
    # Запоздалый просмотр предыдущего ответа не снимает отметку нового завершения.
    state.DB.execute("UPDATE code_attention SET seen = 1 WHERE agent_id = ? AND project = ? AND chat = ?"
                     " AND revision = ?", (aid, project, chat, value))
