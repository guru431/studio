"""Отметки завершения и просмотра сессий: завершение — одно на чат, просмотр — по учётке. Только идентификаторы,
без названий и переписки."""
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


def forget(aid: int, chat: str) -> None:
    """Удалённая сессия: её отметка «непрочитано» и просмотры (`code_attention_seen` — каскадом)."""
    state.DB.execute("DELETE FROM code_attention WHERE agent_id = ? AND chat = ?", (aid, chat))


def revision(aid: int, project: str, chat: str) -> str | None:
    row = state.DB.execute("SELECT revision FROM code_attention WHERE agent_id = ? AND project = ? AND chat = ?",
                           (aid, project, chat)).fetchone()
    return row["revision"] if row is not None else None


def seen(aid: int, project: str, chat: str, value: str, user_id: int) -> None:
    """Просмотр — у каждой учётки свой (`code_attention_seen`): участник не снимает отметку владельца и наоборот.
    Общий столбец `seen` пишется по-прежнему — его читает прежний код после отката."""
    # Запоздалый просмотр предыдущего ответа не снимает отметку нового завершения.
    state.DB.execute("INSERT INTO code_attention_seen(agent_id, chat, user_id, revision)"
                     " SELECT agent_id, chat, ?, revision FROM code_attention WHERE agent_id = ? AND project = ?"
                     " AND chat = ? AND revision = ? ON CONFLICT(agent_id, chat, user_id)"
                     " DO UPDATE SET revision = excluded.revision", (user_id, aid, project, chat, value))
    state.DB.execute("UPDATE code_attention SET seen = 1 WHERE agent_id = ? AND project = ? AND chat = ?"
                     " AND revision = ?", (aid, project, chat, value))


def unread(aid: int, user_id: int, since: dict[str, float] | None = None) -> list:
    """Завершения компьютера, не просмотренные учёткой, новые сверху. `since` — участнику: проект → время выдачи,
    более ранние завершения и чужие проекты не показываются."""
    rows = state.DB.execute(
        "SELECT c.project, c.chat, c.revision, c.finished FROM code_attention c LEFT JOIN code_attention_seen s"
        " ON s.agent_id = c.agent_id AND s.chat = c.chat AND s.user_id = ? AND s.revision = c.revision"
        " WHERE c.agent_id = ? AND s.user_id IS NULL ORDER BY c.finished DESC", (user_id, aid)).fetchall()
    return [r for r in rows if since is None or (r["project"] in since and r["finished"] >= since[r["project"]])]
