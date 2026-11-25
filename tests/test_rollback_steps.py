"""Сторож отката шага 82 (CLAUDE.md, таблица отката): прежний код на новой базе. Прежний `compose` берёт у каждого
шага ответа `text` (или `error`) и пропускает только шаги с `note`: шаг без них дал бы 500 на каждом ходе диалога.
Новые шаги выпуска 0.55.0 — часть `run_part` и шаги внутри неё, автоматический блок знаний, источники
`knowledge_search`, картинки результата MCP, заметка урезанного списка MCP — должны это соблюдать."""
import base64
import json

import mcp_client
import state
from conftest import add_gateway
from test_chat import messages, new_chat, png, sse, turn
from test_chat_parts import SKILL, part
from test_chat_web import EXA_KEY, call
from test_knowledge import base, big_text, upload
from test_mcp_dialog import HOST, server, tool

PIC = base64.b64encode(png("red")).decode()
PICTURE = {"content": [{"type": "text", "text": "график готов"}, {"type": "image", "data": PIC, "mimeType": "image/png"}]}


def assistant_rows(cid: str) -> list:
    return state.DB.execute("SELECT id, tools FROM messages WHERE chat_id = ? AND role = 'assistant' ORDER BY created",
                            (cid,)).fetchall()


def test_every_stored_step_is_readable_by_the_previous_compose(studio):
    add_gateway(studio)                                                 # local-gemma-abl — локальная: MCP урезан до 32
    assert studio.c.put("/api/me/exa-key", json={"key": EXA_KEY}).status_code == 204
    cid = new_chat(studio)
    assert studio.c.patch(f"/api/chats/{cid}", json={"web": True}).json()["web"] is True
    kb = base(studio)
    upload(studio, kb, "справка.txt", big_text("Пароль от сейфа — 4711."))
    assert studio.c.put(f"/api/chats/{cid}/knowledge", json={"kb_ids": [kb]}).status_code == 200
    server(studio, "/mine", tools=[tool("lookup")] + [tool(f"t{n}") for n in range(40)], results={"lookup": PICTURE})
    mine = studio.c.post("/api/mcp", json={"name": "Mine", "url": HOST + "/mine"}).json()["id"]
    assert studio.c.patch(f"/api/chats/{cid}", json={"mcp_ids": [mine]}).status_code == 200
    skill = studio.c.post("/api/skills", json=SKILL)
    assert skill.status_code == 201, skill.text
    lookup = mcp_client.tool_name("Mine", "lookup")
    studio.gw.chat_script = [{"tool_calls": [part()]}, {"tool_calls": [call("web_search", query="новости")]},
                             ["Итог первой части"], {"tool_calls": [call("knowledge_search", query="пароль от сейфа")]},
                             {"tool_calls": [call(lookup, q="1")]}, ["Готово"]]
    sse(turn(studio, cid, "Какой пароль от сейфа?", skill_id=skill.json()["id"]))
    answer = messages(studio, cid)[1]
    assert answer["status"] == "done" and answer["content"] == "Готово"
    stored = [step for row in assistant_rows(cid) for step in json.loads(row["tools"] or "[]")]
    kinds = {(s["kind"], "part" in s, bool(s.get("auto")), "note" in s) for s in stored}
    assert {("part", False, False, False), ("search", True, False, False), ("knowledge", False, True, False),
            ("knowledge", False, False, False), ("mcp", False, False, True), ("mcp", False, False, False)} <= kinds
    assert next(s for s in stored if s["kind"] == "mcp" and "note" not in s)["pictures"] == 1
    assert [a["kind"] for a in answer["attachments"]] == ["image"]       # картинка результата — вложением ответа
    for step in stored:
        assert "note" in step or "error" in step or isinstance(step.get("text"), str), step["kind"]
    old = lambda s: f"Ошибка инструмента: {s['error']}" if "error" in s else s["text"]        # прежний for_model
    blocks = [old(s) for row in assistant_rows(cid) for s in json.loads(row["tools"] or "[]") if "note" not in s]
    assert all(isinstance(b, str) for b in blocks)
    # новый код следующему ходу не отдаёт ни шагов внутри части, ни автоматического блока знаний
    studio.gw.chat_script = [["Ещё ответ"]]
    sse(turn(studio, cid, "Ещё"))
    history = "".join(str(m["content"]) for m in studio.gw.chat_requests[-1]["messages"][:-1])
    assert '<web kind="search">' not in history and history.count('<web kind="knowledge">') == 1
    assert '<web kind="part">' in history and "Итог первой части" in history
