"""Рабочее место раздела «Claude Code» (спека 2026-10-29-studio-code-workspace-design, 6.8): компьютер панели,
открытый проект и вид, вкладки каждого проекта — в учётке, все устройства видят одно.

- Только id: компьютер, `pid` (16 hex), UUID сессий, вид `chat|files|git`. Ни имён, ни названий, ни путей; журнала нет.
- Запись — действиями, не документом: каждое применяется одной транзакцией к тому, что лежит в базе, и меняет только
  своё. Телефон со вчерашним документом не сотрёт вкладки, открытые сегодня на компьютере, и после «Удалить всю
  историю» не вернёт стёртые номера.
- К мосту не ходит: проверяет только, что компьютер свой (`claude_code.own_agent`), и вид полей.
- Пределы: вкладок в проекте — `TABS_MAX` (лишняя — самая левая неактивная), проектов с вкладками — `PROJECTS_MAX`
  (лишний — открытый раньше всех).
"""
import json
from typing import Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, StrictInt, model_validator

import claude_code
import clock
import state

router = APIRouter(prefix="/api/code")

TABS_MAX = 20
PROJECTS_MAX = 100
#: действие → поля, без которых его нет
NEEDS = {"open": ("pid", "chat"), "close": ("pid", "chat"), "place": ("pid", "view"), "panel": ()}


class Action(BaseModel):
    op: Literal["open", "close", "place", "panel"]
    aid: StrictInt
    pid: str | None = None
    chat: str | None = None
    view: Literal["chat", "files", "git"] | None = None

    @model_validator(mode="after")
    def _needs(self):
        if any(getattr(self, name) is None for name in NEEDS[self.op]):
            raise ValueError("fields")
        return self


def doc(user_id: int) -> dict:
    """Документ рабочего места: `panel`, `place` (компьютер отозван — `null`), проекты по убыванию `touched`."""
    place = state.DB.execute("SELECT panel_id, agent_id, project, view FROM code_place WHERE user_id = ?",
                             (user_id,)).fetchone()
    rows = state.DB.execute("SELECT agent_id, project, tabs, active FROM code_tabs WHERE user_id = ?"
                            " ORDER BY touched DESC, rowid DESC", (user_id,))
    return {"panel": place and place["panel_id"],
            "place": ({"aid": place["agent_id"], "pid": place["project"], "view": place["view"]}
                      if place and place["agent_id"] is not None else None),
            "projects": [{"aid": r["agent_id"], "pid": r["project"], "tabs": json.loads(r["tabs"]),
                          "active": r["active"]} for r in rows]}


def _tabs(aid: int, pid: str) -> "tuple[list[str], str | None] | None":
    row = state.DB.execute("SELECT tabs, active FROM code_tabs WHERE agent_id = ? AND project = ?",
                           (aid, pid)).fetchone()
    return None if row is None else (json.loads(row["tabs"]), row["active"])


def _open(uid: int, aid: int, pid: str, chat: str) -> None:
    """Вкладка — в конец ряда, если её нет, и активна; проект — открытый (вид «Чат»)."""
    tabs, _ = _tabs(aid, pid) or ([], None)
    if chat not in tabs:
        tabs.append(chat)
        del tabs[:max(0, len(tabs) - TABS_MAX)]         # новая — в конце и активна: уходят самые левые
    state.DB.execute("INSERT INTO code_tabs(user_id, agent_id, project, tabs, active, touched) VALUES (?, ?, ?, ?, ?, ?)"
                     " ON CONFLICT(agent_id, project) DO UPDATE SET tabs = excluded.tabs, active = excluded.active,"
                     " touched = excluded.touched", (uid, aid, pid, json.dumps(tabs), chat, clock.NOW()))
    state.DB.execute("DELETE FROM code_tabs WHERE user_id = ? AND rowid NOT IN (SELECT rowid FROM code_tabs"
                     " WHERE user_id = ? ORDER BY touched DESC, rowid DESC LIMIT ?)", (uid, uid, PROJECTS_MAX))
    _place(uid, aid, pid, "chat")


def _close(aid: int, pid: str, chat: str) -> None:
    """Вкладка уходит; активная — соседняя справа, иначе слева; последняя — проект без вкладок. Чужая — ничего."""
    tabs, active = _tabs(aid, pid) or ([], None)
    if chat not in tabs:
        return
    i = tabs.index(chat)
    del tabs[i]
    if not tabs:
        state.DB.execute("DELETE FROM code_tabs WHERE agent_id = ? AND project = ?", (aid, pid))
        return
    if active == chat:
        active = tabs[min(i, len(tabs) - 1)]
    state.DB.execute("UPDATE code_tabs SET tabs = ?, active = ? WHERE agent_id = ? AND project = ?",
                     (json.dumps(tabs), active, aid, pid))


def _place(uid: int, aid: int, pid: str, view: str) -> None:
    state.DB.execute("INSERT INTO code_place(user_id, agent_id, project, view) VALUES (?, ?, ?, ?)"
                     " ON CONFLICT(user_id) DO UPDATE SET agent_id = excluded.agent_id, project = excluded.project,"
                     " view = excluded.view", (uid, aid, pid, view))
    state.DB.execute("UPDATE code_tabs SET touched = ? WHERE agent_id = ? AND project = ?", (clock.NOW(), aid, pid))


def _panel(uid: int, aid: int) -> None:
    state.DB.execute("INSERT INTO code_place(user_id, panel_id) VALUES (?, ?)"
                     " ON CONFLICT(user_id) DO UPDATE SET panel_id = excluded.panel_id", (uid, aid))


@router.get("/workspace")
async def get_workspace(request: Request):
    return doc(request.state.user.id)


@router.patch("/workspace")
async def patch_workspace(body: Action, request: Request):
    uid = request.state.user.id
    claude_code.own_agent(request, body.aid)
    if body.pid is not None:
        claude_code.check_pid(body.pid)
    if body.chat is not None:
        claude_code.check_chat(body.chat)
    state.DB.execute("BEGIN IMMEDIATE")
    try:
        if body.op == "open":
            _open(uid, body.aid, body.pid, body.chat)
        elif body.op == "close":
            _close(body.aid, body.pid, body.chat)
        elif body.op == "place":
            _place(uid, body.aid, body.pid, body.view)
        else:
            _panel(uid, body.aid)
        state.DB.execute("COMMIT")
    except BaseException:
        state.DB.execute("ROLLBACK")
        raise
    return doc(uid)
