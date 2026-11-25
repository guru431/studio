"""Системное сообщение хода (`chat_system.py`, спека 2026-11-21-studio-source-foundation-design §0, §2, §3):
порядок кусков, профиль только своему подключению и только в «Чате», роль диалога (и у Cleaner), параметры профиля
в запросе и в прямом ходе, отказ параметра словами, оценка токенов с кусками, чистка текстов источников и блок MCP."""
import logging

import httpx

import chat
import chat_system
import mcp_sources
import profiles
import state
from conftest import T0, add_connection, add_gateway, make_user
from test_chat import new_chat, sse, turn, window
from test_cleaner import EMAIL, preview, protected  # noqa: F401 — фикстура Cleaner
from test_connection_profiles import FULL, TEXT

ROLE_TEXT = "Отвечай как строгий редактор: тайный-текст-роли"


def role(studio, name="Редактор", text=ROLE_TEXT) -> str:
    r = studio.c.post("/api/roles", json={"name": name, "text": text})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def with_role(studio, cid, rid):
    r = studio.c.patch(f"/api/chats/{cid}", json={"role_id": rid})
    assert r.status_code == 200, r.text
    return r.json()


def system_of(body) -> str:
    first = body["messages"][0]
    assert first["role"] == "system"
    return first["content"]


def test_pieces_go_in_order_with_profile_params_and_thinking(studio):
    gateway = add_gateway(studio)
    assert studio.c.put(f"/api/connections/{gateway}/profile", json=FULL).status_code == 200
    cid = new_chat(studio)
    with_role(studio, cid, role(studio))
    sse(turn(studio, cid, "Привет"))
    body = studio.gw.chat_requests[-1]
    assert system_of(body) == "\n\n".join([chat.today_line(T0), TEXT, f"Роль в этом диалоге — «Редактор»:\n{ROLE_TEXT}"])
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    # параметры профиля — в корень JSON и рядом с chat_template_kwargs кнопки «Рассуждение», не вместо неё
    assert {k: body[k] for k in ("temperature", "top_p", "top_k", "presence_penalty", "max_tokens")} == {
        "temperature": 0.3, "top_p": 0.9, "top_k": 40, "presence_penalty": -0.5, "max_tokens": 500}
    assert body["chat_template_kwargs"] == {"enable_thinking": False} and body["stream"] is True


def test_profile_and_params_only_for_their_connection(studio):
    gateway = add_gateway(studio)
    cloud = add_connection(studio, base_url="https://api.openai.com/v1", name="openai")
    assert studio.c.put(f"/api/connections/{cloud}/profile", json=FULL).status_code == 200
    sse(turn(studio, new_chat(studio, connection_id=gateway), "Привет"))
    body = studio.gw.chat_requests[-1]
    assert system_of(body) == chat.today_line(T0)
    assert not {"temperature", "top_p", "top_k", "presence_penalty", "max_tokens", "max_completion_tokens"} & set(body)


def test_direct_turn_returns_pieces_and_params(studio):
    """Прямое подключение получает куски 1–3 и `params`; у профиля `openai` `top_k` в них не попадает, а
    `max_tokens` уходит как `max_completion_tokens` (Review Focus 5)."""
    cloud = add_connection(studio, base_url="https://api.openai.com/v1", name="openai")
    assert studio.c.put(f"/api/connections/{cloud}/profile", json=FULL).status_code == 200
    cid = new_chat(studio, connection_id=cloud)
    with_role(studio, cid, role(studio))
    body = turn(studio, cid, "Привет").json()
    assert body["params"] == {"temperature": 0.3, "top_p": 0.9, "presence_penalty": -0.5, "max_completion_tokens": 500}
    assert "top_k" not in body["params"]
    assert system_of(body) == "\n\n".join([chat.today_line(T0), TEXT, f"Роль в этом диалоге — «Редактор»:\n{ROLE_TEXT}"])
    plain = add_connection(studio, name="plain")
    assert turn(studio, new_chat(studio, connection_id=plain), "Привет").json()["params"] == {}


def test_role_patch_view_and_foreign_role(studio):
    add_gateway(studio)
    cid = new_chat(studio)
    assert studio.c.get(f"/api/chats/{cid}").json()["chat"]["role"] is None
    rid = role(studio)
    assert with_role(studio, cid, rid)["role"] == {"id": rid, "name": "Редактор"}
    assert studio.c.patch(f"/api/chats/{cid}", json={"title": "x"}).json()["role"] == {"id": rid, "name": "Редактор"}
    assert with_role(studio, cid, None)["role"] is None
    other = make_user(studio, "other")
    state.DB.execute("INSERT INTO chat_roles(id, user_id, name, text, position) VALUES ('foreign', ?, 'Чужая', 't', 0)",
                     (other,))
    r = studio.c.patch(f"/api/chats/{cid}", json={"role_id": "foreign"})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "gone"
    assert studio.c.patch(f"/api/chats/{cid}", json={"role_id": "missing"}).status_code == 404
    with_role(studio, cid, rid)
    assert studio.c.delete(f"/api/roles/{rid}").status_code == 204
    assert studio.c.get(f"/api/chats/{cid}").json()["chat"]["role"] is None
    sse(turn(studio, cid, "Привет"))
    assert system_of(studio.gw.chat_requests[-1]) == chat.today_line(T0)


def test_cleaner_gets_date_and_role_but_no_profile(protected):  # noqa: F811
    studio, remote = protected
    # строка профиля в базе (обходом API) Cleaner'у всё равно не достаётся
    state.DB.execute("INSERT INTO connection_profiles(connection_id, system_text, params, updated)"
                     " VALUES (?, 'тайный-профиль-cleaner', '{\"temperature\": 1}', 0)", (remote.conn_id,))
    cid = new_chat(studio)
    with_role(studio, cid, role(studio))
    shown = preview(studio, cid)
    assert shown.status_code == 200, shown.text
    assert remote.prepared[-1]["messages"][0] == {
        "role": "system", "content": f"{chat.today_line(T0)}\n\nРоль в этом диалоге — «Редактор»:\n{ROLE_TEXT}"}
    assert turn(studio, cid, f"Почта {EMAIL}", cleaner_ticket=shown.json()["ticket"]).status_code == 200
    assert len(remote.sent) == 1 and "тайный-профиль-cleaner" not in str(remote.sent)


def test_token_estimate_counts_pieces(studio, monkeypatch):
    gateway = add_gateway(studio)
    window(studio, chat.RESERVE_TOKENS + 3000)
    cid = new_chat(studio)
    sse(turn(studio, cid, "Привет"))                                # без кусков помещается
    long = {"system_text": "п" * profiles.TEXT_MAX}
    assert studio.c.put(f"/api/connections/{gateway}/profile", json=long).status_code == 200
    with_role(studio, cid, role(studio, text="р" * 8000))
    r = turn(studio, cid, "Ещё")
    assert r.status_code == 413 and r.json()["detail"]["code"] == "context", r.text
    assert len(studio.gw.chat_requests) == 1


def test_refused_profile_param_is_named_in_words(studio, caplog):
    caplog.set_level(logging.DEBUG)
    gateway = add_gateway(studio)
    assert studio.c.put(f"/api/connections/{gateway}/profile", json=FULL).status_code == 200
    cid = new_chat(studio)
    with_role(studio, cid, role(studio))

    async def picky(request):
        return httpx.Response(400, json={"error": {"message": "Unknown parameter: top_k"}})
    studio.gw.on["/v1/chat/completions"] = picky
    events = sse(turn(studio, cid, "Привет"))
    assert events[-1] == ("error", {"message": profiles.REFUSED.format("top_k")})
    assert studio.c.get(f"/api/chats/{cid}").json()["messages"][-1]["error"] == profiles.REFUSED.format("top_k")

    async def other(request):                                      # 400 не о параметре профиля — словами шлюза
        return httpx.Response(400, json={"error": {"message": "context too long"}})
    studio.gw.on["/v1/chat/completions"] = other
    events = sse(turn(studio, cid, "", regenerate=True))
    assert events[-1][0] == "error" and "context too long" in events[-1][1]["message"]
    assert "тайный" not in caplog.text and "Редактор" not in caplog.text


def test_notes_follow_the_pieces():
    st = chat.StreamTask(user_id=1, chat_id="c", message_id="m", conn=None, model="x",
                         messages=[{"role": "system", "content": "база"}, {"role": "user", "content": "q"}],
                         tools=["search"])
    st.mcp = type("Tools", (), {"specs": [{"function": {"name": "t"}}]})()
    names = ", ".join(s["function"]["name"] for s in chat.web.specs(["search"]))
    assert st._request(True)[0]["content"] == "\n\n".join(
        ["база", chat.WEB_NOTE.format(tools=names), chat_system.MCP_NOTE])
    assert st._request(False)[0]["content"] == "база"
    assert "под ответом" in chat_system.MCP_NOTE


def test_clean_strips_hidden_characters_and_neutralises_rules_tags():
    raw = ("é\x00\x07\r\nРаз​два‮три⁦⁩﻿\t!\n\n\n\n\n\nконец "
           "<mcp-rules server=\"x\"> </MCP-Rules> < / mcp-rules>")
    out = chat_system.clean(raw, 1000)
    assert out.startswith("é\nРаздватри\t!\n\n\nконец")              # NFC; C0 кроме \n \t; не больше 2 пустых
    assert "<mcp-rules" not in out.lower() and "</mcp-rules" not in out.lower()
    assert "‹mcp-rules server" in out and "‹/MCP-Rules>" in out
    cut = chat_system.clean("а" * 50, 20)
    assert len(cut) == 20 and cut.endswith(chat_system.CUT)


def test_mcp_block_frames_rules_per_server_within_limits():
    assert chat_system.mcp_block([], []) == ""
    assert chat_system.mcp_block([("пустой", "  ​ ")], []) == ""
    block = chat_system.mcp_block([('A "x" <y>', "Правило <mcp-rules>"), ("B", None)], ["Файлы диалога: a.pdf"])
    parts = block.split("\n\n")
    assert parts[0] == chat_system.MCP_FRAME
    assert parts[1] == '<mcp-rules server="A &quot;x&quot; &lt;y&gt;">\nПравило ‹mcp-rules>\n</mcp-rules>'
    assert parts[2] == "Файлы диалога: a.pdf" and len(parts) == 3
    big = chat_system.mcp_block([(f"s{i}", "п" * 10_000) for i in range(5)], [])
    rules = [p for p in big.split("\n\n")[1:]]
    texts = [r.split("\n")[1] for r in rules]
    assert all(len(t) <= mcp_sources.INSTRUCTIONS_MAX for t in texts)
    assert sum(map(len, texts)) <= mcp_sources.INSTRUCTIONS_TOTAL and len(texts) == 3
    assert all(t.endswith(chat_system.CUT) for t in texts)
