"""Кнопка «Интернет» диалога (`chats.web`) и описания инструментов (спека 2026-10-21-studio-chat-internet,
6.3 и 6.8)."""
import web
from conftest import add_connection, add_gateway, make_user, signin
from test_chat import new_chat

EXA_KEY = "exa-test-key"


def chat_of(s, cid) -> dict:
    r = s.c.get(f"/api/chats/{cid}")
    assert r.status_code == 200, r.text
    return r.json()["chat"]


def patch(s, cid, **body) -> dict:
    r = s.c.patch(f"/api/chats/{cid}", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_switch_is_stored_and_tools_follow_the_exa_key(studio):
    add_gateway(studio); cid = new_chat(studio)
    chat = chat_of(studio, cid)
    assert chat["web"] is False and chat["web_tools"] == ["youtube"]      # кнопке есть что включать
    assert patch(studio, cid, web=True)["web"] is True
    chat = chat_of(studio, cid)
    assert chat["web"] is True and chat["web_tools"] == ["youtube"]
    assert studio.c.put("/api/me/exa-key", json={"key": EXA_KEY}).status_code == 204
    assert chat_of(studio, cid)["web_tools"] == ["search", "page", "youtube"]
    before = chat_of(studio, cid)
    assert patch(studio, cid, web=None)["web"] is True                  # null — не трогает
    assert patch(studio, cid, title="Новости")["web"] is True           # другое поле — не трогает
    after = chat_of(studio, cid)
    assert (after["connection_id"], after["model"]) == (before["connection_id"], before["model"])
    assert patch(studio, cid, web=False)["web"] is False
    assert chat_of(studio, cid)["web"] is False


def test_switch_of_someone_elses_chat_is_gone(studio):
    add_gateway(studio); cid = new_chat(studio)
    make_user(studio, "bob"); signin(studio, "bob")
    r = studio.c.patch(f"/api/chats/{cid}", json={"web": True})
    assert r.status_code == 404 and r.json()["detail"]["code"] == "gone"
    signin(studio, "owner")
    assert chat_of(studio, cid)["web"] is False


def test_direct_connection_keeps_the_switch_but_offers_no_tools(studio):
    gateway = add_gateway(studio); direct = add_connection(studio)
    assert studio.c.put("/api/me/exa-key", json={"key": EXA_KEY}).status_code == 204
    cid = new_chat(studio, connection_id=direct)
    chat = patch(studio, cid, web=True)
    assert chat["web"] is True and chat["web_tools"] == []
    chat = patch(studio, cid, connection_id=gateway)                     # туда и обратно — web остаётся
    assert chat["web"] is True and chat["web_tools"] == ["search", "page", "youtube"]
    chat = patch(studio, cid, connection_id=direct)
    assert chat["web"] is True and chat["web_tools"] == []


def test_tools_for_decides_by_the_path_and_the_key(studio):
    import connections
    gateway = add_gateway(studio); direct = add_connection(studio)
    uid = studio.c.get("/api/me").json()["id"]
    proxied, browser = connections.get(uid, gateway), connections.get(uid, direct)
    assert web.tools_for(None, True) == []
    assert web.tools_for(browser, True) == [] and web.tools_for(browser, False) == []
    assert web.tools_for(proxied, False) == ["youtube"]
    assert web.tools_for(proxied, True) == list(web.KINDS) == ["search", "page", "youtube"]


def test_specs_are_openai_functions_with_one_required_string():
    only = web.specs(["youtube"])
    assert [t["function"]["name"] for t in only] == ["youtube_subtitles"]
    tools = web.specs(list(web.KINDS))
    assert {t["function"]["name"]: web.NAMES[t["function"]["name"]] for t in tools} == {
        "web_search": "search", "read_page": "page", "youtube_subtitles": "youtube"}
    params = {"web_search": "query", "read_page": "url", "youtube_subtitles": "url"}
    for t in tools:
        f = t["function"]
        assert t["type"] == "function" and f["description"].strip()
        p, schema = params[f["name"]], f["parameters"]
        assert schema["type"] == "object" and list(schema["properties"]) == [p] and schema["required"] == [p]
        assert schema["properties"][p]["type"] == "string" and schema["properties"][p]["description"].strip()
    assert web.specs([]) == []
