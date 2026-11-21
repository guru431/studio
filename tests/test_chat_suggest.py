"""Пустой экран и поле ввода чата (спека 2026-11-21-studio-source-capabilities-design, п. 2 и 33): подсказки —
из профиля подключения, затем `prompts` MCP диалога (не больше `chat.SUGGESTIONS_MAX`); оценка заполнения
контекста — системное сообщение, история и вложения будущего хода."""
import math

import chat
import chat_system
import connections
import profiles
import state
from conftest import add_gateway, make_user, signin
from test_chat import attach, new_chat, png, sse, turn
from test_mcp_dialog import server, view
from test_access_groups import shared_tools
from test_shared_integrations import ADMIN

PROMPTS = [{"name": "brief", "title": "Обзор темы", "description": "Короткий обзор",
            "arguments": [{"name": "topic", "description": "Тема", "required": True}]},
           {"name": "plan"}, {"name": "draft"}, {"name": "extra"}]


def setup(studio, suggestions=6):
    """Администратор, участник, общий API моделей с подсказками профиля и общий MCP с `prompts`; вход — участником."""
    make_user(studio, "admin", is_admin=True)
    make_user(studio, "member")
    sid = shared_tools(studio)
    conn_id = state.DB.execute("SELECT id FROM connections WHERE user_id IS NULL").fetchone()[0]
    items = [{"title": f"Подсказка {n}", "text": f"Текст {n}"} for n in range(suggestions)]
    r = studio.c.put(f"{ADMIN}/connections/{conn_id}/profile", json={"suggestions": items})
    assert r.status_code == 200, r.text
    calls = server(studio, prompts=PROMPTS)
    signin(studio, "member")
    return sid, calls


def test_suggestions_merge_profile_and_mcp_prompts_capped_at_8(studio):
    sid, calls = setup(studio)
    cid = new_chat(studio)
    conn_id = view(studio, cid)["connection_id"]
    assert chat.SUGGESTIONS_MAX == 8

    got = studio.c.get("/api/chats/suggestions", params={"chat_id": cid})
    assert got.status_code == 200, got.text
    items = got.json()
    assert len(items) == 8
    assert items[:6] == [{"title": f"Подсказка {n}", "text": f"Текст {n}", "source": "profile"} for n in range(6)]
    assert items[6] == {"title": "Обзор темы", "text": "Короткий обзор", "source": "mcp", "server": "Shared tools",
                        "prompt": {"sid": sid, "name": "brief",
                                   "arguments": [{"name": "topic", "description": "Тема", "required": True}]}}
    assert items[7]["title"] == "plan" and items[7]["prompt"] == {"sid": sid, "name": "plan", "arguments": []}

    # новый диалог — MCP по умолчанию (общий сервер), каталог — из кэша
    fresh = studio.c.get("/api/chats/suggestions", params={"connection_id": conn_id, "model": "test-chat"}).json()
    assert fresh == items
    assert [m for m, _ in calls].count("prompts/list") == 1

    # сервер выключен в диалоге — только профиль
    assert studio.c.patch(f"/api/chats/{cid}", json={"mcp_off": [sid]}).status_code == 200
    assert [i["source"] for i in studio.c.get("/api/chats/suggestions", params={"chat_id": cid}).json()] == ["profile"] * 6

    # без подключения и без диалога — пусто; чужой диалог — 404
    assert studio.c.get("/api/chats/suggestions").json() == []
    assert studio.c.get("/api/chats/suggestions", params={"chat_id": "nope"}).status_code == 404


def test_estimate_counts_system_history_and_attachments(studio):
    conn_id = add_gateway(studio)
    cid = new_chat(studio)
    url = f"/api/chats/{cid}/estimate"

    def estimate(text="", ids=()):
        r = studio.c.post(url, json={"text": text, "attachment_ids": list(ids)})
        assert r.status_code == 200, r.text
        return r.json()

    uid = state.DB.execute("SELECT user_id FROM chats WHERE id = ?", (cid,)).fetchone()[0]
    conn = connections.get(uid, conn_id)
    empty = estimate()
    system_only = chat.estimate_tokens(chat_system.wrap(chat_system.base(uid, conn, None), [{"role": "user", "content": ""}]))
    assert empty == {"tokens": system_only, "limit": chat.CONTEXT["local-gemma-abl"]}

    # системный текст профиля — в оценке
    assert studio.c.put(f"/api/connections/{conn_id}/profile", json={"system_text": "с" * 3000}).status_code == 200
    with_profile = estimate()["tokens"]
    assert with_profile - empty["tokens"] in (1000, 1001)
    assert profiles.text_of(conn) == "с" * 3000

    # текст хода и вложения: фото — 1000, документ — по знакам
    assert estimate("x" * 300)["tokens"] - with_profile in (100, 101)
    photo = attach(studio, "p.png", png())
    doc = attach(studio, "a.txt", ("d" * 3000).encode())
    both = estimate("", [photo["id"], doc["id"]])["tokens"]
    assert both - with_profile >= 2000
    assert estimate("", [photo["id"]])["tokens"] - with_profile in (1000, 1001)

    # история диалога — в оценке
    studio.gw.chat_script = [["о" * 600]]
    sse(turn(studio, cid, "в" * 600))
    assert estimate()["tokens"] - with_profile in range(399, 403)

    # отправленное вложение — 404, как у хода
    sse(turn(studio, cid, "ещё", attachment_ids=[photo["id"]]))
    assert studio.c.post(url, json={"text": "", "attachment_ids": [photo["id"]]}).status_code == 404
    assert math.isfinite(estimate()["tokens"])
