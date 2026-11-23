"""Флажок администратора «Веб и внешние MCP — только через Cleaner» (`cleaner_route.web_only`): «Интернет» и
веб-инструменты MCP (пометка «интернет») — только ходу через Cleaner; в диалоге шлюза srvllm сервер их не предлагает,
вызов модели — шаг-ошибка без Exa, PATCH {web: true} — 409; инструменты разделов MCP с пометкой не показываются;
флажок выключен — как раньше."""
import pytest

import cleaner_route
import mcp_files
import mcp_sections
import state
from conftest import add_connection, add_gateway
from fakes import FakeCleanerGateway
from test_chat import attach, messages, new_chat, sse, turn
from test_chat_web import EXA_KEY, call, chat_of
from test_cleaner_route_chat import ADDRESS, KEY, PROVIDER, allow, settings, shared_mcp, tool, tool_names
from test_mcp import TOKEN as MCP_KEY, URL as MCP_URL
from test_mcp_chat_files import consent, post_media, server as files_server, uploads


@pytest.fixture
def flagged(studio):
    """Cleaner настроен, флажок «Веб только через Cleaner» включён. Подключение через Cleaner (`gw.conn_id`), шлюз
    srvllm (`studio.srvllm`), ключ Exa у учётки, общий MCP с обычным `lookup` и веб-инструментом `search`
    (`studio.mcp`); вход — владельцем-администратором."""
    cid = add_connection(studio, base_url=PROVIDER, sections=["chat"], chat_models=("gpt-x",))
    gw = FakeCleanerGateway(KEY)
    gw.conn_id = cid
    studio.gw.hosts["10.77.1.20:5464"] = gw.handler
    studio.gw.hosts["api.cloud.test:443"] = gw.provider
    allow("10.77.1.20:5464")
    assert studio.c.patch(f"/api/connections/{cid}", json={"via_cleaner": True}).status_code == 200
    studio.srvllm = add_gateway(studio)
    assert studio.c.put("/api/me/exa-key", json={"key": EXA_KEY}).status_code == 204
    studio.mcp = shared_mcp(studio, [tool("lookup"), tool("search", annotations={"openWorldHint": True})])
    settings(studio, web=True)
    assert cleaner_route.web_only()
    return studio, gw


def test_flag_needs_cleaner_and_is_shown_to_the_page(studio):
    add_gateway(studio)
    allow("10.77.1.20:5464")
    r = studio.c.put("/api/admin/cleaner", json={"base_url": "", "key": None, "forced": False, "web": True})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "cleaner"
    assert studio.c.get("/api/me").json()["cleaner_web"] is False
    settings(studio, web=True)
    assert studio.c.get("/api/admin/cleaner").json() == {"base_url": ADDRESS, "key_set": True, "forced": False,
                                                         "web": True, "reenter": False}
    assert studio.c.get("/api/me").json()["cleaner_web"] is True
    settings(studio)                     # прежний вид запроса панели (старая вкладка, без `web`) флажок не меняет
    assert studio.c.get("/api/me").json()["cleaner_web"] is True and cleaner_route.web_only()
    settings(studio, web=False)
    assert studio.c.get("/api/me").json()["cleaner_web"] is False and not cleaner_route.web_only()
    settings(studio, web=True)
    r = studio.c.put("/api/admin/cleaner", json={"base_url": "", "key": None, "forced": False})
    assert r.status_code == 200 and r.json()["web"] is False         # адрес снят — флажку не на чем держаться


def test_dialog_not_through_cleaner_gets_no_web(flagged):
    s, _ = flagged
    chat_id = new_chat(s, connection_id=s.srvllm)
    view = chat_of(s, chat_id)
    assert view["web_tools"] == [] and view["routed"] is False
    r = s.c.patch(f"/api/chats/{chat_id}", json={"web": True})
    assert r.status_code == 409 and r.json()["detail"] == {"code": "cleaner_web", "message": cleaner_route.WEB_ONLY}
    state.DB.execute("UPDATE chats SET web = 1 WHERE id = ?", (chat_id,))   # включён до флажка
    s.gw.chat_script = [{"tool_calls": [call("web_search", query="секрет"), call(f"mcp_{s.mcp}_1", q="секрет")]},
                        ["Без интернета."]]
    assert sse(turn(s, chat_id, "Найди"))[-1][0] == "done"
    names = tool_names(s.gw.chat_requests[0])
    assert f"mcp_{s.mcp}_0" in names                     # обычный MCP остаётся
    assert not {"web_search", "read_page", "youtube_subtitles", f"mcp_{s.mcp}_1"} & set(names)
    assert s.gw.exa_requests == []
    steps = messages(s, chat_id)[-1]["tools"]
    assert len(steps) == 2 and all(step.get("error") for step in steps)   # оба вызова — ошибки без запроса


def test_dialog_through_cleaner_keeps_web(flagged):
    s, gw = flagged
    chat_id = new_chat(s, connection_id=gw.conn_id, model="gpt-x")
    assert chat_of(s, chat_id)["web_tools"] == ["search", "page", "youtube"]
    assert s.c.patch(f"/api/chats/{chat_id}", json={"web": True}).json()["web"] is True
    assert sse(turn(s, chat_id, "Найди"))[-1][0] == "done"
    names = tool_names(gw.bodies[0])
    assert {"web_search", "read_page", "youtube_subtitles", f"mcp_{s.mcp}_0", f"mcp_{s.mcp}_1"} <= set(names)
    assert gw.turns[0].headers["x-cleaner-hold-tools"] == f"web_search,read_page,youtube_subtitles,mcp_{s.mcp}_1"


def test_flag_off_keeps_web_of_internal_dialogs(flagged):
    s, _ = flagged
    settings(s, web=False)
    chat_id = new_chat(s, connection_id=s.srvllm)
    assert s.c.patch(f"/api/chats/{chat_id}", json={"web": True}).json()["web"] is True
    assert sse(turn(s, chat_id, "Найди"))[-1][0] == "done"
    assert {"web_search", f"mcp_{s.mcp}_1"} <= set(tool_names(s.gw.chat_requests[0]))


def test_dialog_files_do_not_go_to_a_web_server_even_through_cleaner(flagged):
    """Файлы диалога Studio грузит на MCP сама, мимо Cleaner: при флажке веб-`artifact_upload` закрыт и в диалоге
    через Cleaner — ни вложений, ни приёма медиа; без флажка — как раньше."""
    s, gw = flagged
    calls = files_server(s, "/files")
    sid = s.c.post("/api/admin/shared/mcp", json={"name": "Files", "url": MCP_URL.replace("/mcp", "/files"),
                                                   "token": MCP_KEY}).json()["id"]
    assert s.c.put(f"/api/admin/shared/mcp/{sid}/policy",
                   json={"tools": {"artifact_upload": {"open_world": True}}}).status_code == 200
    chat_id = new_chat(s, connection_id=gw.conn_id, model="gpt-x")
    assert s.c.patch(f"/api/chats/{chat_id}", json={"web": True}).json()["web"] is True
    consent(s, chat_id, sid)
    note = attach(s, "notes.txt", b"secret notes")
    assert sse(turn(s, chat_id, "Посмотри", attachment_ids=[note["id"]]))[-1][0] == "done"
    assert uploads(calls) == []
    assert mcp_files.BLOCKED_CLEANER.format("Files") in gw.bodies[-1]["messages"][0]["content"]
    refused = post_media(s, chat_id, b"v" * 10)
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "mcp_files"
    settings(s, web=False)
    assert post_media(s, chat_id, b"v" * 10).status_code == 201
    assert sse(turn(s, chat_id, "Ещё"))[-1][0] == "done"
    assert len(uploads(calls)) == 2


def test_section_tools_with_the_internet_mark_are_hidden_by_the_flag(flagged):
    s, _ = flagged
    schema = {"type": "object", "properties": {"prompt": {"type": "string"}}}
    tools = [{"name": "image_generate", "inputSchema": schema, "annotations": {"openWorldHint": True}},
             {"name": "image_describe", "inputSchema": schema}]
    own = {"id": 1, "user_id": 5}
    assert [t["name"] for t in mcp_sections.supported("photo", tools, item=own)] == ["image_describe"]
    assert [t["name"] for t in mcp_sections.supported("photo", tools)] == ["image_generate", "image_describe"]
    settings(s, web=False)
    assert [t["name"] for t in mcp_sections.supported("photo", tools, item=own)] == ["image_generate", "image_describe"]
