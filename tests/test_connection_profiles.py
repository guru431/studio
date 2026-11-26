"""Профиль API-подключения (app/profiles.py): CRUD своего и общего, видимость, параметры запроса, проба,
Cleaner, удаление вместе с подключением, журнал."""
import logging

import httpx

import connections
import profiles
import state
from conftest import add_connection, add_gateway, make_user, signin
from test_chat import new_chat, sse, turn
from test_checks import CLOUD_ROLE, gateway_as_openai
from test_shared_integrations import ADMIN, add_models

TEXT = "Отвечай кратко и по делу: тайный-текст-профиля"
FULL = {"system_text": TEXT,
        "params": {"temperature": 0.3, "top_p": 0.9, "top_k": 40, "presence_penalty": -0.5, "max_tokens": 500},
        "suggestions": [{"title": "Сводка", "text": "Сделай сводку тайная-подсказка"}]}


def code(r) -> str:
    return r.json()["detail"]["code"]


def own(studio, cid) -> dict:
    return next(c for c in studio.c.get("/api/connections").json()["items"] if c["id"] == cid)


def test_own_profile_crud_and_limits(studio):
    cid = add_connection(studio)
    assert studio.c.get(f"/api/connections/{cid}/profile").json() == {
        "system_text": "", "params": {}, "suggestions": [], "model_params": {}, "provider": "compatible"}
    assert own(studio, cid)["profile_set"] is False and own(studio, cid)["suggestions"] == []

    r = studio.c.put(f"/api/connections/{cid}/profile", json=FULL)
    assert r.status_code == 200, r.text
    assert r.json() == {**FULL, "model_params": {}, "provider": "compatible"}
    assert studio.c.get(f"/api/connections/{cid}/profile").json() == r.json()
    item = own(studio, cid)
    assert item["profile_set"] is True and item["suggestions"] == FULL["suggestions"]

    partial = {"system_text": "", "params": {"temperature": None, "top_p": 0.5}}
    assert studio.c.put(f"/api/connections/{cid}/profile", json=partial).json()["params"] == {"top_p": 0.5}
    assert studio.c.put(f"/api/connections/{cid}/profile", json={}).json()["params"] == {}
    assert own(studio, cid)["profile_set"] is False

    lone = studio.c.put(f"/api/connections/{cid}/profile", content='{"system_text": "\\ud800"}',
                        headers={"Content-Type": "application/json"})      # одиночный суррогат — экранированием JSON
    assert lone.status_code == 422
    bad = [{"system_text": "x" * 8001}, {"system_text": "a\x00b"},
           {"params": {"temperature": 2.1}}, {"params": {"top_p": 0}}, {"params": {"top_k": 1.5}},
           {"params": {"top_k": True}}, {"params": {"top_k": 1001}}, {"params": {"presence_penalty": -3}},
           {"params": {"max_tokens": 0}}, {"params": {"max_tokens": 1_000_001}}, {"params": {"seed": 1}},
           {"params": {"max_tools": 0}}, {"params": {"max_tools": 257}}, {"params": {"max_tools": "200"}},
           {"params": {"max_tools": 200.0}}, {"params": {"max_tools": True}},
           {"suggestions": [{"title": "t", "text": "x"}] * 13}, {"suggestions": [{"title": "", "text": "x"}]},
           {"suggestions": [{"title": "t" * 81, "text": "x"}]}, {"suggestions": [{"title": "t", "text": "x" * 2001}]}]
    for body in bad:
        r = studio.c.put(f"/api/connections/{cid}/profile", json=body)
        assert r.status_code == 422, body

    make_user(studio, "other")
    signin(studio, "other")
    assert studio.c.get(f"/api/connections/{cid}/profile").status_code == 404
    assert studio.c.put(f"/api/connections/{cid}/profile", json=FULL).status_code == 404


def test_shared_profile_is_admin_only_and_its_text_hidden_but_suggestions_visible(studio):
    make_user(studio, "admin", is_admin=True)
    make_user(studio, "member")
    signin(studio, "admin")
    cid = add_models(studio)
    r = studio.c.put(f"{ADMIN}/connections/{cid}/profile", json=FULL)
    assert r.status_code == 200 and r.json()["system_text"] == TEXT
    assert studio.c.get(f"{ADMIN}/connections/{cid}/profile").json()["params"] == FULL["params"]
    listed = next(c for c in studio.c.get(f"{ADMIN}/connections").json()["items"] if c["id"] == cid)
    assert listed["profile_set"] is True

    signin(studio, "member")
    assert studio.c.get(f"{ADMIN}/connections/{cid}/profile").status_code == 403
    assert studio.c.put(f"{ADMIN}/connections/{cid}/profile", json={}).status_code == 403
    assert studio.c.get(f"/api/connections/{cid}/profile").status_code == 404
    assert studio.c.put(f"/api/connections/{cid}/profile", json={}).status_code == 404
    item = own(studio, cid)
    assert item["suggestions"] == FULL["suggestions"] and "profile_set" not in item
    for path in ("/api/connections", "/api/sections"):
        body = studio.c.get(path).text
        assert "тайный-текст-профиля" not in body and "presence_penalty" not in body
    assert state.DB.execute("SELECT system_text FROM connection_profiles WHERE connection_id = ?",
                            (cid,)).fetchone()[0] == TEXT          # профиль цел — просто не виден


def test_request_params_by_provider(studio):
    cloud = add_connection(studio, base_url="https://api.openai.com/v1", name="openai")
    other = gateway_as_openai(studio)
    gateway = add_gateway(studio)
    for cid in (cloud, other, gateway):
        assert studio.c.put(f"/api/connections/{cid}/profile", json=FULL).status_code == 200
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]
    get = lambda cid: connections.get(uid, cid)

    assert studio.c.get(f"/api/connections/{cloud}/profile").json()["provider"] == "openai"
    assert profiles.request_params(get(cloud)) == {"temperature": 0.3, "top_p": 0.9, "presence_penalty": -0.5,
                                                   "max_completion_tokens": 500}      # top_k у openai — нет
    expected = {"temperature": 0.3, "top_p": 0.9, "presence_penalty": -0.5, "max_tokens": 500, "top_k": 40}
    assert profiles.request_params(get(other)) == expected
    assert profiles.request_params(get(gateway)) == expected
    studio.c.put(f"/api/connections/{other}/profile", json={"params": {"temperature": 1}})
    assert profiles.request_params(get(other)) == {"temperature": 1}                  # top_k — только явно заданный
    # предел инструментов MCP хода — не параметр запроса; без него — mcp_client.MAX_TOOLS
    assert profiles.max_tools(get(other)) == 128
    for value in (1, 256):
        assert studio.c.put(f"/api/connections/{other}/profile", json={"params": {"max_tools": value}}).status_code == 200
        assert profiles.max_tools(get(other)) == value and profiles.request_params(get(other)) == {}


def test_model_params_override_the_connection_params(studio):
    cid = add_gateway(studio); url = f"/api/connections/{cid}/profile"
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]
    body = {"params": {"temperature": 0.3, "max_tools": 64},
            "model_params": {"tiny": {"temperature": 0.9, "context_window": 16384, "max_images": 2}, "empty": {}}}
    r = studio.c.put(url, json=body)
    assert r.status_code == 200 and r.json()["model_params"] == {"tiny": body["model_params"]["tiny"]}
    conn = connections.get(uid, cid)
    assert profiles.model_params(conn, "tiny") == {"temperature": 0.9, "max_tools": 64, "context_window": 16384,
                                                   "max_images": 2}
    assert profiles.model_params(conn, "other") == profiles.model_params(conn, None) == body["params"]
    assert profiles.request_params(conn, "tiny") == {"temperature": 0.9}
    assert profiles.request_params(conn) == {"temperature": 0.3}
    assert (profiles.max_images(conn, "tiny"), profiles.max_images(conn), profiles.max_tools(conn, "tiny")) == (2, 4, 64)
    studio.c.put(f"/api/sections/chat/links/{cid}", json={"models": ["local-gemma-abl", "tiny"]})
    sse(turn(studio, new_chat(studio, connection_id=cid, model="tiny")))
    sent = studio.gw.chat_requests[-1]
    assert sent["temperature"] == 0.9 and not {"context_window", "max_images", "max_tools"} & set(sent)
    assert studio.c.put(url, json={"model_params": {"tiny": {"top_p": 0.5}}}).json() == {
        "system_text": "", "params": {}, "suggestions": [], "model_params": {"tiny": {"top_p": 0.5}}, "provider": "srvllm"}
    assert own(studio, cid)["profile_set"] is True
    for bad in ({"": {}}, {"m" * 201: {}}, {"m": {"context_window": 8191}}, {"m": {"context_window": 4_000_001}},
                {"m": {"context_window": "32768"}}, {"m": {"seed": 1}}, {f"m{n}": {"top_p": 0.5} for n in range(201)}):
        assert studio.c.put(url, json={"model_params": bad}).status_code == 422, bad


def test_cleaner_has_no_profile(studio):
    add_connection(studio)                                         # вход владельцем
    r = studio.c.post("/api/connections", json={"name": "Cleaner", "kind": "openai", "cleaner": True,
                                                "base_url": "http://10.77.1.15:4000", "token": "ck_synthetic",
                                                "sections": ["chat"]})
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    for response in (studio.c.get(f"/api/connections/{cid}/profile"),
                     studio.c.put(f"/api/connections/{cid}/profile", json=FULL)):
        assert response.status_code == 422 and code(response) == "cleaner"
    item = own(studio, cid)
    assert item["suggestions"] == [] and item["profile_set"] is False
    uid = state.DB.execute("SELECT user_id FROM connections WHERE id = ?", (cid,)).fetchone()[0]
    state.DB.execute("INSERT INTO connection_profiles(connection_id, params, updated) VALUES (?, ?, 0)",
                     (cid, '{"temperature": 1}'))                  # строка в базе не превращает Cleaner в профиль
    assert profiles.request_params(connections.get(uid, cid)) == {} and own(studio, cid)["profile_set"] is False


def test_profile_goes_with_its_connection(studio):
    cid = add_connection(studio)
    assert studio.c.put(f"/api/connections/{cid}/profile", json=FULL).status_code == 200
    assert studio.c.delete(f"/api/connections/{cid}").status_code == 204
    assert state.DB.execute("SELECT COUNT(*) FROM connection_profiles").fetchone()[0] == 0


def test_profile_text_is_not_logged(studio, caplog):
    caplog.set_level(logging.DEBUG)
    cid = add_connection(studio)
    assert studio.c.put(f"/api/connections/{cid}/profile", json=FULL).status_code == 200
    assert f"подключение {cid}: профиль сохранён" in caplog.text
    assert "тайный" not in caplog.text and "тайная" not in caplog.text and "Сводка" not in caplog.text


def test_probe_sends_profile_params_and_tells_a_refused_one(studio):
    cid = gateway_as_openai(studio)
    assert studio.c.put(f"/api/connections/{cid}/profile", json=FULL).status_code == 200
    assert studio.c.post(f"/api/connections/{cid}/probe", json={"model": CLOUD_ROLE}).json()["probe"]["ok"]
    body, = studio.gw.chat_requests
    assert body["temperature"] == 0.3 and body["top_k"] == 40 and body["presence_penalty"] == -0.5
    assert body["max_tokens"] == 16 and "system" not in str(body["messages"])     # проба короткая, без текста

    async def picky(request):
        return httpx.Response(400, json={"error": {"message": "Unknown parameter: top_k"}})
    studio.gw.on["/v1/chat/completions"] = picky
    probe = studio.c.post(f"/api/connections/{cid}/probe", json={"model": CLOUD_ROLE}).json()["probe"]
    assert not probe["ok"] and probe["words"] == profiles.REFUSED.format("top_k")


SENT = {"temperature": 0.3, "top_k": 40, "max_tokens": 5000}


def test_refusal_needs_a_rejection_marker_next_to_the_name():
    # vLLM: переполнение контекста называет max_tokens, но параметр принят — это не отказ параметра
    too_large = ("'max_tokens' or 'max_completion_tokens' is too large: 5000. This model's maximum context length "
                 "is 8192 tokens and your request has 4000 input tokens (5000 > 8192 - 4000).")
    context = ("This model's maximum context length is 8192 tokens. However, you requested 9000 tokens "
               "(4000 in the messages, 5000 in the completion). Please reduce the length of the messages or "
               "completion (max_tokens).")
    assert profiles.refusal(too_large, SENT) is None
    assert profiles.refusal(context, SENT) is None
    openai = '{"error": {"message": "Unrecognized request argument supplied: top_k", "type": "invalid_request_error"}}'
    assert profiles.refusal(openai, SENT) == profiles.REFUSED.format("top_k")
    extra = '{"detail": [{"type": "extra_forbidden", "loc": ["body", "top_k"], "msg": "Extra inputs are not permitted"}]}'
    assert profiles.refusal(extra, SENT) == profiles.REFUSED.format("top_k")
    assert profiles.refusal("Unrecognized request argument supplied: top_k", {"temperature": 0.3}) is None


def test_direct_answer_markers_and_limits_match_the_server():
    """Прямой ход и запрос usage используют общие признаки отказа; поля PUT ограничены теми же числами."""
    import re
    from pathlib import Path
    import chat

    js = Path(__file__).resolve().parents[1] / "app/static/js"
    source = (js / "chat-usage.js").read_text(encoding="utf-8")
    block = re.search(r"const REJECTED = \[(.*?)\];", source, re.S)
    assert block, "нет REJECTED в chat-usage.js"
    assert tuple(re.findall(r"'([^']*)'", block.group(1))) == profiles.REJECTED
    for name in ("USAGE_MAX", "MS_MAX"):
        value = re.search(rf"const {name} = (\d+);", source)
        assert value and int(value.group(1)) == getattr(chat, name)
