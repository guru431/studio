"""Уровень рассуждения диалога (спека 2026-11-25 п. 40): `chats.effort` выбирается только у подключений, чей профиль
провайдера его принимает (`profiles.EFFORT_PROVIDERS`), и уходит в запрос хода только им — прямому подключению
параметром для страницы, через сервер — телом запроса, и в продолжении после паузы."""
import capabilities
import chat
import profiles
import state
from conftest import add_connection, add_gateway
from test_chat import new_chat, sse, turn
from test_chat_pending import answer, pending_of
from test_chat_web import call
from test_checks import CLOUD_ROLE, gateway_as_openai
from test_cleaner_route_chat import cleaner_gw  # noqa: F401  (фикстура)
from test_mcp_dialog import view
from test_protected_source import code_of


def test_effort_follows_the_provider_profile_and_reaches_the_request(studio, monkeypatch):
    cloud = add_connection(studio); cid = new_chat(studio, connection_id=cloud, model="local-gemma-abl")   # «compatible»
    patch = lambda body: studio.c.patch(f"/api/chats/{cid}", json=body)
    assert view(studio, cid)["effort_ok"] is False and code_of(patch({"effort": "high"})) == "no_effort"
    assert view(studio, cid)["effort"] is None
    monkeypatch.setitem(capabilities.PROFILES, "cloud.test", "openai")
    assert view(studio, cid)["effort_ok"] is True and patch({"effort": "high"}).json()["effort"] == "high"
    assert patch({"effort": "max"}).status_code == 422 and patch({"title": "Имя"}).json()["effort"] == "high"
    listed = {c["id"]: c for c in studio.c.get("/api/connections").json()["items"]}
    assert listed[cloud]["effort"] is True
    assert turn(studio, cid).json()["params"] == {"reasoning_effort": "high"}        # прямое: параметр — странице
    monkeypatch.setitem(capabilities.PROFILES, "cloud.test", "openrouter")
    other = new_chat(studio, connection_id=cloud, model="local-gemma-abl")
    assert studio.c.patch(f"/api/chats/{other}", json={"effort": "medium"}).json()["effort"] == "medium"
    assert turn(studio, other).json()["params"] == {"reasoning": {"effort": "medium"}}
    assert patch({"effort": None}).json()["effort"] is None                           # «как у модели»
    third = new_chat(studio, connection_id=cloud, model="local-gemma-abl")
    assert turn(studio, third).json()["params"] == {}

    gw = add_gateway(studio); local = new_chat(studio, connection_id=gw, model="local-gemma-abl")
    assert view(studio, local)["effort_ok"] is False
    assert code_of(studio.c.patch(f"/api/chats/{local}", json={"effort": "low"})) == "no_effort"
    assert {c["id"]: c for c in studio.c.get("/api/connections").json()["items"]}[gw]["effort"] is False
    state.DB.execute("UPDATE chats SET effort = 'low' WHERE id = ?", (local,)); sse(turn(studio, local))
    assert not {"reasoning_effort", "reasoning"} & set(studio.gw.chat_requests[-1])   # непринимающему не уходит
    assert profiles.refusal("Unrecognized request argument supplied: reasoning_effort",
                            {"reasoning_effort": "high"}) == profiles.EFFORT_REFUSED
    assert profiles.refusal("extra inputs are not permitted: reasoning", {"reasoning": {"effort": "low"}}) == profiles.EFFORT_REFUSED
    assert profiles.refusal("unsupported value: temperature", {"temperature": 1}) == profiles.REFUSED.format("temperature")


def test_effort_params_by_provider(studio, monkeypatch):
    cloud = add_connection(studio)
    import connections
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]
    conn = connections.get(uid, cloud)
    assert profiles.EFFORTS == ("low", "medium", "high") and profiles.EFFORT_PROVIDERS == ("openai", "openrouter")
    assert profiles.effort_params(conn, "high") == {}                                 # «compatible» — не принимает
    monkeypatch.setitem(capabilities.PROFILES, "cloud.test", "openai")
    assert profiles.effort_params(conn, "low") == {"reasoning_effort": "low"} and profiles.effort_params(conn, None) == {}
    monkeypatch.setitem(capabilities.PROFILES, "cloud.test", "openrouter")
    assert profiles.effort_params(conn, "high") == {"reasoning": {"effort": "high"}}


def test_effort_reaches_a_server_request_and_its_continuation(studio, monkeypatch):
    """`openai` через сервер: тело запроса несёт `reasoning_effort`, продолжение после паузы — его же; уровень —
    настройка диалога, а не профиля подключения: смена модели его не сбрасывает."""
    cid_conn = gateway_as_openai(studio)
    monkeypatch.setitem(capabilities.PROFILES, "10.77.1.15", "openai")
    cid = new_chat(studio, connection_id=cid_conn, model=CLOUD_ROLE)
    assert view(studio, cid)["effort_ok"] is True
    assert studio.c.patch(f"/api/chats/{cid}", json={"effort": "low"}).json()["effort"] == "low"
    studio.gw.chat_script = [{"tool_calls": [call("ask_user", question="Какой цвет?", options=["Красный"])]},
                             ["Выбран красный."]]
    shown = pending_of(sse(turn(studio, cid, "Нарисуй")))
    assert studio.gw.chat_requests[-1]["reasoning_effort"] == "low"
    assert sse(answer(studio, cid, shown["id"], "answer", "Красный"))[-1] == ("done", {"status": "done"})
    assert studio.gw.chat_requests[-1]["reasoning_effort"] == "low"                   # продолжение — тот же уровень
    monkeypatch.delitem(capabilities.PROFILES, "10.77.1.15")                          # провайдер перестал принимать
    sse(turn(studio, cid, "Ещё"))
    assert "reasoning_effort" not in studio.gw.chat_requests[-1] and view(studio, cid)["effort"] == "low"


def test_effort_is_not_offered_through_cleaner(cleaner_gw, monkeypatch):  # noqa: F811
    studio, gw = cleaner_gw
    monkeypatch.setitem(capabilities.PROFILES, "api.cloud.test", "openai")
    cid = new_chat(studio, connection_id=gw.conn_id, model="gpt-x")
    assert view(studio, cid)["effort_ok"] is False
    assert code_of(studio.c.patch(f"/api/chats/{cid}", json={"effort": "high"})) == "no_effort"
    assert {c["id"]: c for c in studio.c.get("/api/connections").json()["items"]}[gw.conn_id]["effort"] is False
    assert chat.NO_EFFORT == "у этого подключения уровень рассуждения не выбирается"
