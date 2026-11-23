"""Переименовать и удалить сессию «Агентов» из студии (07:I8, мост 0.47.0 `hello.chat_manage`)."""
import json

import code_attention
import state
from test_code import A, ALPHA, B, chat_url, detail, setup, url


def _manage(studio):
    aid, machine, link = setup(studio)
    machine.chat_manage = link.hello["chat_manage"] = True
    return aid, machine, link


def test_rename_goes_to_the_bridge_and_needs_the_capability(studio):
    aid, machine, link = _manage(studio)
    r = studio.c.put(chat_url(aid, "title"), json={"title": "  Своё название "})
    assert (r.status_code, r.json()) == (200, {"title": "Своё название"})
    assert machine.projects["alpha"]["chats"][A]["title"] == "Своё название"
    assert machine.requests[-1]["op"] == "chat_rename" and machine.requests[-1]["title"] == "Своё название"
    for bad in ({"title": "   "}, {"title": "a\nb"}, {"title": "x" * 201}):
        assert studio.c.put(chat_url(aid, "title"), json=bad).status_code == 422
    link.hello.pop("chat_manage")
    r = studio.c.put(chat_url(aid, "title"), json={"title": "Ещё"})
    assert r.status_code == 409 and detail(r)["code"] == "unsupported"


def test_delete_only_a_studio_session_and_clean_tabs_and_unread(studio):
    """Удаляется только сессия, начатая студией (`code_chats.origin` или метка моста); мост удаляет её штатно, сервер — строку,
    вкладки и отметку «непрочитано». «Новый чат» без хода (на машине сессии нет) тоже удаляется."""
    aid, machine, link = _manage(studio)
    r = studio.c.delete(url(aid, "projects", ALPHA, "chats", B))      # сессия терминала
    assert r.status_code == 409 and detail(r)["code"] == "not_studio"
    assert not any(q["op"] == "chat_delete" for q in machine.requests)
    machine.projects["alpha"]["chats"][B]["tag"] = "studio"          # метка моста без строки — как «студия» списка
    assert studio.c.delete(url(aid, "projects", ALPHA, "chats", B)).status_code == 204
    assert B not in machine.projects["alpha"]["chats"]
    fresh = studio.c.post(url(aid, "projects", ALPHA, "chats")).json()["chat"]
    uid = link.user_id
    state.DB.execute("INSERT INTO code_chats(user_id, agent_id, project, session_id, state, created, updated, engine, origin)"
                     " VALUES (?, ?, ?, ?, 'open', 1, 1, 'claude', 'studio')", (uid, aid, ALPHA, A))
    state.DB.execute("INSERT INTO code_tabs(user_id, agent_id, project, tabs, active, touched) VALUES (?, ?, ?, ?, ?, 1)",
                     (uid, aid, ALPHA, json.dumps([A, fresh]), A))
    code_attention.completed(aid, ALPHA, A)
    assert studio.c.delete(chat_url(aid)).status_code == 204
    assert A not in machine.projects["alpha"]["chats"]
    assert code_attention.revision(aid, ALPHA, A) is None
    tabs = state.DB.execute("SELECT tabs, active FROM code_tabs WHERE agent_id = ?", (aid,)).fetchone()
    assert (json.loads(tabs["tabs"]), tabs["active"]) == ([fresh], fresh)
    assert studio.c.delete(url(aid, "projects", ALPHA, "chats", fresh)).status_code == 204   # на машине его нет
    assert state.DB.execute("SELECT count(*) FROM code_chats WHERE agent_id = ?", (aid,)).fetchone()[0] == 0
    assert state.DB.execute("SELECT count(*) FROM code_tabs WHERE agent_id = ?", (aid,)).fetchone()[0] == 0


def test_delete_refused_by_the_bridge_keeps_the_record(studio):
    aid, machine, link = _manage(studio)
    state.DB.execute("INSERT INTO code_chats(user_id, agent_id, project, session_id, state, created, updated, engine, origin)"
                     " VALUES (?, ?, ?, ?, 'open', 1, 1, 'claude', 'studio')", (link.user_id, aid, ALPHA, A))
    machine.running[A] = {"turn": "t1", "project": ALPHA, "text": "", "steps": [], "asks": {}, "followups": []}
    r = studio.c.delete(chat_url(aid))
    assert r.status_code == 409 and detail(r)["code"] == "running"
    assert state.DB.execute("SELECT count(*) FROM code_chats WHERE session_id = ?", (A,)).fetchone()[0] == 1
