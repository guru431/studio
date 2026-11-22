"""Ход чата подключения через Cleaner: контекст Cleaner на диалог, запрос в шлюз агентов Cleaner с ключом провайдера,
все инструменты, удержание веб-инструментов, id запроса Cleaner у ответа, отказы словами без обхода, картинки,
смена адреса Cleaner, удаление диалога, журнал без данных."""
import json
import logging

import pytest

import chat
import cleaner_client
import security
import state
from conftest import add_connection, add_gateway, make_user, signin
from fakes import FakeCleanerGateway
from test_chat import attach, messages, new_chat, png, sse, turn
from test_chat_shares import create as create_share, unlock as unlock_share
from test_chat_pending import answer
from test_chat_web import EXA_KEY, call
from test_knowledge import big_text
from test_mcp import TOKEN as MCP_KEY, URL as MCP_URL
from test_mcp_dialog import server, tool

ADDRESS = "https://10.77.1.20:5464"
SECOND = "https://10.77.1.21:5464"
KEY = "ck_" + "a" * 43
PROVIDER = "https://api.cloud.test/v1"


def allow(*entries):
    state.DB.execute("UPDATE settings SET value = ? WHERE key = 'allowlist'",
                     (json.dumps(["10.77.1.15:4000", *entries]),))


def settings(s, **body):
    response = s.c.put("/api/admin/cleaner", json={"base_url": ADDRESS, "key": KEY, "forced": False} | body)
    assert response.status_code == 200, response.text


@pytest.fixture
def cleaner_gw(studio):
    """Cleaner настроен (адрес в списке, служебный ключ), подключение https://api.cloud.test/v1 с переключателем
    «Через Cleaner»; вход — владельцем-администратором. Узел провайдера — AssertionError."""
    cid = add_connection(studio, base_url=PROVIDER, sections=["chat"], chat_models=("gpt-x",))
    gw = FakeCleanerGateway(KEY)
    gw.conn_id, gw.service_key = cid, KEY
    for address in ("10.77.1.20:5464", "10.77.1.21:5464"):
        studio.gw.hosts[address] = gw.handler
    studio.gw.hosts["api.cloud.test:443"] = gw.provider
    allow("10.77.1.20:5464", "10.77.1.21:5464")
    settings(studio)
    assert studio.c.patch(f"/api/connections/{cid}", json={"via_cleaner": True}).status_code == 200
    yield studio, gw
    assert studio.gw.chat_requests == []                 # шлюз srvllm и прочее тоже не получали ход


def routed_chat(s, gw) -> str:
    return new_chat(s, connection_id=gw.conn_id, model="gpt-x")


def stored_request(answer_id: str) -> "str | None":
    return state.DB.execute("SELECT cleaner_request FROM messages WHERE id = ?", (answer_id,)).fetchone()[0]


def shared_mcp(s, tools) -> int:
    server(s, tools=tools)
    response = s.c.post("/api/admin/shared/mcp", json={"name": "Shared tools", "url": MCP_URL, "token": MCP_KEY})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def web_on(s, chat_id):
    assert s.c.put("/api/me/exa-key", json={"key": EXA_KEY}).status_code == 204
    assert s.c.patch(f"/api/chats/{chat_id}", json={"web": True}).json()["web"] is True


def tool_names(body) -> list[str]:
    return [t["function"]["name"] for t in body.get("tools", [])]


def test_turn_goes_to_cleaner_with_provider_key_and_context(cleaner_gw):
    s, gw = cleaner_gw
    chat_id = routed_chat(s, gw)
    assert s.c.get(f"/api/chats/{chat_id}").json()["chat"]["routed"] is True
    events = sse(turn(s, chat_id, "Привет"))
    assert events[-1] == ("done", {"status": "done"})
    sent = gw.turns[0]
    assert str(sent.url) == ADDRESS + "/agent/openai/v1/chat/completions"
    assert sent.headers["x-cleaner-key"] == gw.service_key and sent.headers["authorization"] == "Bearer cloud-key"
    assert sent.headers["x-cleaner-upstream"] == PROVIDER and sent.headers["x-cleaner-chat"] == gw.chats[0]
    assert "x-cleaner-hold-tools" not in sent.headers
    assert gw.bodies[0]["model"] == "gpt-x" and gw.bodies[0]["stream"] is True
    assert gw.bodies[0]["messages"][-1] == {"role": "user", "content": "Привет"}
    created = gw.requests[0]
    assert created.url.path == "/api/v1/chats" and created.headers["x-cleaner-key"] == KEY
    assert "authorization" not in created.headers and json.loads(created.content) == {"title": "Studio"}
    answer = messages(s, chat_id)[-1]
    assert answer["via"] == "server" and answer["cleaner_sent"] and answer["content"] == "Привет из облака"
    assert stored_request(answer["id"]) == f"{gw.chats[0]}/{1:032x}"
    assert not messages(s, chat_id)[0]["cleaner_sent"]
    row = state.DB.execute("SELECT c.base_url, c.token_enc, c.user_id FROM cleaner_contexts c"
                           " JOIN cleaner_route_contexts r ON r.id = c.id WHERE c.item_id = ?", (chat_id,)).fetchone()
    assert row["base_url"] == ADDRESS and security.decrypt(row["token_enc"]) == KEY


def test_tools_web_and_knowledge_stay_on(cleaner_gw):
    s, gw = cleaner_gw
    sid = shared_mcp(s, [tool("lookup")])
    chat_id = routed_chat(s, gw)
    web_on(s, chat_id)
    book = attach(s, "отчёт.txt", big_text("Пароль от сейфа — 4711.").encode(), "text/plain")
    assert sse(turn(s, chat_id, "Какой пароль от сейфа?", attachment_ids=[book["id"]]))[-1][0] == "done"
    body = gw.bodies[0]
    names = tool_names(body)
    assert {"web_search", "read_page", "youtube_subtitles", f"mcp_{sid}_0"} <= set(names)
    assert body["messages"][0]["role"] == "system"
    asked = body["messages"][-1]["content"]
    assert '<document name="отчёт.txt" excerpts="true">' in asked and "4711" in asked   # знания: выдержки по вопросу


def test_web_and_open_world_tools_are_held(cleaner_gw):
    s, gw = cleaner_gw
    sid = shared_mcp(s, [tool("lookup"), tool("search", annotations={"openWorldHint": True})])
    chat_id = routed_chat(s, gw)
    web_on(s, chat_id)
    sse(turn(s, chat_id, "Найди"))
    assert gw.turns[0].headers["x-cleaner-hold-tools"] == f"web_search,read_page,youtube_subtitles,mcp_{sid}_1"
    assert f"mcp_{sid}_0" in tool_names(gw.bodies[0])                      # обычный MCP получает оригиналы


def test_tool_round_reuses_the_context_and_stores_the_last_request(cleaner_gw):
    s, gw = cleaner_gw
    sid = shared_mcp(s, [tool("lookup")])
    chat_id = routed_chat(s, gw)
    gw.script = [{"tool_calls": [call(f"mcp_{sid}_0", q="Иван Петров")]}, ["Готово"]]
    assert sse(turn(s, chat_id, "Найди Ивана"))[-1] == ("done", {"status": "done"})
    assert len(gw.turns) == 2 and len(gw.chats) == 1
    assert {r.headers["x-cleaner-chat"] for r in gw.turns} == {gw.chats[0]}
    assert gw.bodies[1]["messages"][-1]["role"] == "tool"
    answer = messages(s, chat_id)[-1]
    assert answer["content"] == "Готово" and stored_request(answer["id"]) == f"{gw.chats[0]}/{2:032x}"
    sse(turn(s, chat_id, "Ещё"))                                            # следующий ход — тот же контекст
    assert len(gw.chats) == 1 and gw.turns[-1].headers["x-cleaner-chat"] == gw.chats[0]


def test_resume_after_a_pause_goes_through_the_same_context(cleaner_gw):
    """Продолжение после паузы (chat_pending) контекста не знает: первый круг берёт тот же чат Cleaner диалога."""
    s, gw = cleaner_gw
    sid = shared_mcp(s, [tool("translate", _meta={"studio/confirm": True})])
    chat_id = routed_chat(s, gw)
    gw.script = [{"text": ["Сейчас."], "tool_calls": [call(f"mcp_{sid}_0", phrase="hello")]}, ["Готово."]]
    events = sse(turn(s, chat_id, "Переведи"))
    shown = next(data for name, data in events if name == "pending")
    assert sse(answer(s, chat_id, shown["id"], "refuse"))[-1] == ("done", {"status": "done"})
    assert len(gw.turns) == 2 and len(gw.chats) == 1
    assert gw.turns[1].headers["x-cleaner-chat"] == gw.chats[0] and gw.bodies[1]["messages"][-1]["role"] == "tool"
    assert messages(s, chat_id)[-1]["content"] == "Сейчас.\n\nГотово."


def test_cleaner_down_refuses_without_touching_the_provider(cleaner_gw):
    s, gw = cleaner_gw
    chat_id = routed_chat(s, gw)
    gw.down = True
    response = turn(s, chat_id, "Привет")
    assert response.status_code == 502
    assert response.json()["detail"]["message"] == "Cleaner недоступен: данные не отправлены"
    assert messages(s, chat_id) == []                                       # ход не записан
    gw.down = False
    sse(turn(s, chat_id, "Привет"))                                         # контекст есть
    gw.down = True
    sse(turn(s, chat_id, "Ещё"))
    assert messages(s, chat_id)[-1]["error"] == "Cleaner недоступен: данные не отправлены"


def test_unconfigured_cleaner_refuses_in_words(cleaner_gw):
    s, gw = cleaner_gw
    chat_id = routed_chat(s, gw)
    settings(s, base_url="", key=None)
    response = turn(s, chat_id, "Привет")
    assert response.status_code == 503 and "данные не отправлены" in response.json()["detail"]["message"]
    assert gw.requests == []


@pytest.mark.parametrize("status, code, words", [(400, "upstream", "публичные https"), (429, None, "Cleaner занят"),
                                                 (401, "client_login", "ключ"), (503, None, "очистк")])
def test_cleaner_refusals_in_words(cleaner_gw, status, code, words):
    s, gw = cleaner_gw
    chat_id = routed_chat(s, gw)
    gw.refuse = (status, code)
    events = sse(turn(s, chat_id, "Привет"))
    assert events[-1][0] == "error" and words in events[-1][1]["message"]
    answer = messages(s, chat_id)[-1]
    assert words in answer["error"] and answer["cleaner_sent"]              # id запроса есть и у отказа


def test_local_provider_is_refused_by_cleaner_in_words(cleaner_gw):
    """Подключение через Cleaner на localhost: Cleaner отвечает 400 `upstream`, слова понятны, провайдеру — ничего."""
    s, gw = cleaner_gw
    local = add_connection(s, base_url="http://localhost:1234/v1", sections=["chat"], chat_models=("local-x",),
                           name="local")
    assert s.c.patch(f"/api/connections/{local}", json={"via_cleaner": True}).status_code == 200
    chat_id = new_chat(s, connection_id=local, model="local-x")
    sse(turn(s, chat_id, "Привет"))
    assert "публичные https" in messages(s, chat_id)[-1]["error"]


def test_vanished_context_is_recreated_on_the_next_turn(cleaner_gw):
    s, gw = cleaner_gw
    chat_id = routed_chat(s, gw)
    sse(turn(s, chat_id, "Привет"))
    gw.chats.clear()                                                        # чат удалили в интерфейсе Cleaner
    sse(turn(s, chat_id, "Ещё"))
    assert "контекст" in messages(s, chat_id)[-1]["error"]
    assert sse(turn(s, chat_id, "Снова"))[-1] == ("done", {"status": "done"})
    assert len(gw.chats) == 1


def test_new_image_attachment_is_refused(cleaner_gw):
    s, gw = cleaner_gw
    chat_id = routed_chat(s, gw)
    photo = attach(s, "photo.png", png(), "image/png")
    response = turn(s, chat_id, "Что на фото?", attachment_ids=[photo["id"]])
    assert response.status_code == 409 and response.json()["detail"]["code"] == "cleaner_images"
    assert gw.requests == [] and messages(s, chat_id) == []


def test_history_images_are_dropped(cleaner_gw):
    s, gw = cleaner_gw
    gateway = add_gateway(s)
    chat_id = new_chat(s, connection_id=gateway, model="local-gemma-abl")
    photo = attach(s, "photo.png", png(), "image/png")
    sse(turn(s, chat_id, "Что на фото?", attachment_ids=[photo["id"]]))
    assert any(isinstance(m["content"], list) for m in s.gw.chat_requests[0]["messages"])
    s.gw.chat_requests.clear()
    assert s.c.patch(f"/api/chats/{chat_id}", json={"connection_id": gw.conn_id, "model": "gpt-x"}).status_code == 200
    assert sse(turn(s, chat_id, "А теперь?"))[-1] == ("done", {"status": "done"})
    raw = json.dumps(gw.bodies[0], ensure_ascii=False)
    assert "image_url" not in raw and "data:image" not in raw and chat.DROPPED.format(0) in raw


def test_forced_direct_connection_turn_is_server_side(cleaner_gw):
    """Флажок администратора включён, а страница держит старое «прямое» состояние: ход идёт через сервер и Cleaner."""
    s, gw = cleaner_gw
    assert s.c.patch(f"/api/connections/{gw.conn_id}", json={"via_cleaner": False}).status_code == 200
    assert turn(s, routed_chat(s, gw), "Привет").json()["url"]               # прямое: браузер пошёл бы сам
    chat_id = routed_chat(s, gw)
    settings(s, forced=True)
    response = turn(s, chat_id, "Привет")
    events = sse(response)                                                   # поток сервера, а не JSON с `url`
    assert events[-1] == ("done", {"status": "done"}) and len(gw.turns) == 1
    assert messages(s, chat_id)[-1]["via"] == "server"


def queued() -> list:
    return state.DB.execute("SELECT remote_id, base_url, token_enc, route FROM cleaner_deletions ORDER BY queued").fetchall()


def test_new_cleaner_address_makes_a_new_context(cleaner_gw):
    s, gw = cleaner_gw
    chat_id = routed_chat(s, gw)
    sse(turn(s, chat_id, "Привет"))
    settings(s, base_url=SECOND)
    assert [(r["remote_id"], r["base_url"], r["route"]) for r in queued()] == [(gw.chats[0], ADDRESS, 1)]
    assert security.decrypt(queued()[0]["token_enc"]) == KEY
    sse(turn(s, chat_id, "Ещё"))
    assert len(gw.chats) == 2 and str(gw.turns[-1].url).startswith(SECOND)
    assert gw.turns[-1].headers["x-cleaner-chat"] == gw.chats[1]
    # только ключ: прежний контекст — со снимком прежнего ключа
    settings(s, base_url=SECOND, key="ck_" + "b" * 43)
    rows = {r["remote_id"]: r for r in queued()}
    assert set(rows) == set(gw.chats) and rows[gw.chats[1]]["base_url"] == SECOND
    assert security.decrypt(rows[gw.chats[1]]["token_enc"]) == KEY
    gw.key = "ck_" + "b" * 43
    sse(turn(s, chat_id, "Снова"))
    assert len(gw.chats) == 3
    # очередь удаляет служебным ключом снимка — заголовком X-Cleaner-Key, без Authorization
    gw.key = KEY
    s.c.portal.call(cleaner_client.purge)
    assert [d[0] for d in gw.deleted] == gw.chats[:2] and queued() == []
    assert all(d[1]["x-cleaner-key"] == KEY and "authorization" not in d[1] for d in gw.deleted)


def test_dialog_delete_queues_the_context(cleaner_gw):
    s, gw = cleaner_gw
    chat_id = routed_chat(s, gw)
    sse(turn(s, chat_id, "Привет"))
    assert s.c.delete(f"/api/chats/{chat_id}").status_code == 204             # корзина: контекст ещё жив
    assert queued() == []
    uid = state.DB.execute("SELECT user_id FROM chats WHERE id = ?", (chat_id,)).fetchone()[0]
    s.c.portal.call(chat.purge, uid, chat_id)
    assert [(r["remote_id"], r["route"]) for r in queued()] == [(gw.chats[0], 1)]
    assert state.DB.execute("SELECT count(*) FROM cleaner_route_contexts").fetchone()[0] == 0
    s.c.portal.call(cleaner_client.purge)
    assert [d[0] for d in gw.deleted] == [gw.chats[0]] and queued() == []


def test_toggle_is_offered_to_external_connections_while_cleaner_is_configured(cleaner_gw):
    """Страница показывает переключатель по `cleaner_offer`: внешний `openai` при настроенном Cleaner; включённый —
    и без адреса, чтобы его можно было выключить; внутреннему — никогда."""
    s, gw = cleaner_gw
    gateway = add_gateway(s)
    other = add_connection(s, base_url="https://api.other.test/v1", sections=["chat"], name="other")

    def offers():
        return {c["id"]: c["cleaner_offer"] for c in s.c.get("/api/connections").json()["items"]}

    assert offers() == {gw.conn_id: True, gateway: False, other: True}
    settings(s, base_url="")
    assert offers() == {gw.conn_id: True, gateway: False, other: False}


def sent_of(s, chat_id, message_id):
    return s.c.get(f"/api/chats/{chat_id}/messages/{message_id}/cleaner-sent")


def masked_turn(s, gw) -> tuple[str, str]:
    """Ход «Позвони Ивану» через Cleaner, который заменил имя на «<PERSON_1>»: (диалог, ответ)."""
    gw.masks = {"Ивану": "<PERSON_1>"}
    chat_id = routed_chat(s, gw)
    assert sse(turn(s, chat_id, "Позвони Ивану"))[-1] == ("done", {"status": "done"})
    return chat_id, messages(s, chat_id)[-1]["id"]


def test_sent_shows_the_cleaned_request_to_the_owner(cleaner_gw):
    s, gw = cleaner_gw
    chat_id, answer_id = masked_turn(s, gw)
    response = sent_of(s, chat_id, answer_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["messages"][-1] == {"role": "user", "content": "Позвони <PERSON_1>"}
    assert body["counts"] == {"PERSON": 1} and isinstance(body["tools"], int)
    assert response.headers["cache-control"] == "no-store"
    read = gw.sent_reads[-1]
    assert read.url.path == f"/agent/openai/v1/cleaner/chats/{gw.chats[0]}/requests/{1:032x}"
    assert read.headers["x-cleaner-key"] == KEY                              # только служебный ключ
    assert "authorization" not in read.headers and "x-cleaner-upstream" not in read.headers
    user_id = messages(s, chat_id)[0]["id"]                                  # у вопроса запроса Cleaner нет
    assert sent_of(s, chat_id, user_id).status_code == 404
    assert sent_of(s, chat_id, "nope").status_code == 404


def test_sent_of_another_users_dialog_is_404(cleaner_gw):
    s, gw = cleaner_gw
    chat_id, answer_id = masked_turn(s, gw)
    reads = len(gw.sent_reads)
    make_user(s, "friend")
    signin(s, "friend")
    response = sent_of(s, chat_id, answer_id)
    assert response.status_code == 404 and response.json()["detail"]["code"] == "gone"
    assert len(gw.sent_reads) == reads                                       # к Cleaner за чужим не ходили


def test_sent_after_journal_gone_is_410(cleaner_gw):
    s, gw = cleaner_gw
    chat_id, answer_id = masked_turn(s, gw)
    gw.journal.clear()                                                       # Cleaner удалил журнал за давностью
    response = sent_of(s, chat_id, answer_id)
    assert response.status_code == 410 and response.json()["detail"]["code"] == "journal_gone"
    assert response.json()["detail"]["message"]
    gw.chats.clear()                                                         # контекст удалили в Cleaner — тоже
    assert sent_of(s, chat_id, answer_id).status_code == 410
    gw.down = True
    response = sent_of(s, chat_id, answer_id)
    assert response.status_code == 502 and response.json()["detail"]["message"] == "Cleaner недоступен"


def test_sent_is_not_in_share_or_export(cleaner_gw):
    s, gw = cleaner_gw
    chat_id, _ = masked_turn(s, gw)
    reads = len(gw.sent_reads)
    _, token = create_share(s, f"/api/chats/{chat_id}/share")
    shown = unlock_share(s, token)
    assert shown.status_code == 200
    assert "<PERSON_1>" not in shown.text and "cleaner" not in shown.text.lower()
    exported = s.c.post(f"/api/chats/{chat_id}/export", json={"format": "md"})
    assert exported.status_code == 200 and "Позвони Ивану" in exported.text
    assert "<PERSON_1>" not in exported.text and "cleaner" not in exported.text.lower()
    assert len(gw.sent_reads) == reads                                       # ни ссылка, ни экспорт Cleaner не спрашивают


def test_cleaner_turn_never_reaches_the_log(cleaner_gw, caplog):
    s, gw = cleaner_gw
    caplog.set_level(logging.INFO, logger="studio")
    chat_id = routed_chat(s, gw)
    sse(turn(s, chat_id, "Секретный вопрос про Ивана"))
    gw.refuse = (429, None)
    sse(turn(s, chat_id, "Ещё секрет"))
    assert "ход через Cleaner: код 429" in caplog.text
    for secret in ("Секретный", "секрет", "Привет из облака", "api.cloud.test", KEY, "cloud-key", "10.77.1.20",
                   gw.chats[0]):
        assert secret not in caplog.text, secret
