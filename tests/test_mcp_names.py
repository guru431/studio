"""Имена и пределы инструментов MCP хода (спека 2026-11-25-studio-skills-status-local-models, п. 32): имя — из
сервера и инструмента, одно и то же от хода к ходу; локальной модели — не больше 32 инструментов с описанием до
1000 знаков, пока профиль подключения не задал своё."""
import mcp_client
from conftest import add_gateway
from test_chat import messages, new_chat, sse, turn
from test_mcp_dialog import HOST, server, setup, tool


def mcp_tools(request):
    """Описания функций MCP запроса к модели (имя с приставкой `mcp_`)."""
    return [t["function"] for t in request.get("tools", []) if t["function"]["name"].startswith("mcp_")]


def test_tool_name_is_built_from_the_server_and_the_tool():
    assert mcp_client.tool_name("Shared tools", "lookup") == "mcp_shared_tools__lookup"
    assert mcp_client.tool_name("Почта отдела", "mail.send:v2") == "mcp_pochta_otdela__mail_send_v2"
    assert mcp_client.tool_name("Почта отдела", "Скачать.файл") == "mcp_pochta_otdela__Skachat_fayl"
    assert mcp_client.tool_name("🙂", "getItem") == "mcp_server__getItem"
    long = mcp_client.tool_name("S" * 80, "t" * 128)
    assert len(long) == mcp_client.CALL_MAX == 64 and long.startswith("mcp_" + "s" * 24 + "__t")
    assert mcp_client.tool_name("A b", "t", {"mcp_a_b__t"}) == "mcp_a_b__t_2"


def test_tool_name_numbers_repeats_without_growing_past_the_limit():
    taken = {mcp_client.tool_name("S" * 80, "t" * 128)}
    second = mcp_client.tool_name("S" * 80, "t" * 128, taken)
    assert len(second) == 64 and second.endswith("_2") and second not in taken
    taken.add(second)
    assert mcp_client.tool_name("S" * 80, "t" * 128, taken).endswith("_3")


def test_names_stay_when_another_tool_is_switched_off(studio):
    sid, _, _ = setup(studio, tools=[tool("a"), tool("b"), tool("c")])
    cid = new_chat(studio)
    sse(turn(studio, cid))
    before = [t["name"] for t in mcp_tools(studio.gw.chat_requests[-1])]
    assert before == ["mcp_shared_tools__a", "mcp_shared_tools__b", "mcp_shared_tools__c"]
    assert studio.c.patch(f"/api/chats/{cid}", json={"mcp_tools_off": {str(sid): ["a"]}}).status_code == 200
    sse(turn(studio, cid, "Ещё"))
    assert [t["name"] for t in mcp_tools(studio.gw.chat_requests[-1])] == before[1:]     # было: mcp_<sid>_0, _1


def test_local_model_gets_32_tools_with_1000_char_descriptions_unless_the_profile_says_more(studio):
    gw = add_gateway(studio)
    cid = new_chat(studio)                                              # local-gemma-abl — локальная
    server(studio, "/mine", tools=[tool(f"t{n}", description="д" * 3000) for n in range(40)])
    mine = studio.c.post("/api/mcp", json={"name": "Mine", "url": HOST + "/mine"}).json()["id"]
    assert studio.c.patch(f"/api/chats/{cid}", json={"mcp_ids": [mine]}).status_code == 200
    sse(turn(studio, cid))
    sent = mcp_tools(studio.gw.chat_requests[-1])
    assert len(sent) == mcp_client.LOCAL_MAX_TOOLS == 32
    assert {len(t["description"]) for t in sent} == {mcp_client.LOCAL_DESCRIPTION_MAX} == {1000}
    assert messages(studio, cid)[1]["tools"] == [{"kind": "mcp", "server": "Mine", "note": mcp_client.TRIMMED.format(32)}]
    assert studio.c.put(f"/api/connections/{gw}/profile", json={"params": {"max_tools": 64}}).status_code == 200
    sse(turn(studio, cid, "Ещё"))
    assert len(mcp_tools(studio.gw.chat_requests[-1])) == 40            # `max_tools` профиля сильнее
    assert studio.c.put(f"/api/connections/{gw}/profile", json={}).status_code == 200
    studio.c.put(f"/api/sections/chat/links/{gw}", json={"models": ["local-gemma-abl", "gemma4-abl"]})
    assert studio.c.patch(f"/api/chats/{cid}", json={"model": "gemma4-abl"}).status_code == 200
    sse(turn(studio, cid, "И ещё"))
    cloud = mcp_tools(studio.gw.chat_requests[-1])                      # облачной роли — как было
    assert studio.gw.chat_requests[-1]["model"] == "gemma4-abl"
    assert len(cloud) == 40 and all(3000 < len(t["description"]) <= mcp_client.DESCRIPTION_MAX for t in cloud)
