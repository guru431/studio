"""Проверка подключения и паспорт (app/checks.py, app/capabilities.py): пробы только чтением, возможности с
источником, паспорт браузера, пробный ответ."""
import json
import logging

import httpx
import pytest

import capabilities
import connections
import state
import upstream
from conftest import add_connection, add_gateway, make_user, signin
from fakes import FakeGateway

GW = "http://10.77.1.15:4000"


def code(r) -> str:
    return r.json()["detail"]["code"]


def check(s, cid):
    return s.c.post(f"/api/connections/{cid}/check")


def nothing_spent(gw: FakeGateway) -> bool:
    """Проверка не рисует, не говорит, не освобождает карту и не читает список роликов."""
    return not (gw.generations or gw.releases or gw.video_calls or gw.descriptions or gw.chat_requests
                or gw.asr_requests or gw.tts_requests)


def by_id(passport) -> dict:
    return {m["id"]: m for m in passport["models"]}


def caps(model) -> set:
    return {(c["name"], c["source"]) for c in model["caps"]}


def allow(*entries):
    state.DB.execute("UPDATE settings SET value = ? WHERE key = 'allowlist'", (json.dumps(list(entries)),))


@pytest.fixture
def public(monkeypatch):
    """Имена из списка разрешаются в публичный адрес."""
    async def resolve(host, port): return ["93.184.216.34"]
    monkeypatch.setattr(upstream, "RESOLVE", resolve)


# ---- через сервер: srvllm ----------------------------------------------------------

def test_gateway_check_reads_two_lists_and_spends_nothing(studio):
    cid = add_gateway(studio)
    studio.gw.slot_models = [{"id": "lustify-v10-krea2-turbo"}, {"id": "wan2.2-remix-i2v-v3"}, {"id": "other-x"}]
    r = check(studio, cid)
    assert r.status_code == 200
    p = r.json()["passport"]
    assert [q.url.path for q in studio.gw.requests] == ["/v1/models", "/slot/models"]
    assert all(q.method == "GET" for q in studio.gw.requests) and nothing_spent(studio.gw)
    assert p["source"] == "server" and p["profile"] == "srvllm" and p["checked"] == pytest.approx(studio.clock[0])
    chat, slot = p["routes"]
    assert chat["path"] == "/v1/models" and slot["path"] == "/slot/models"
    assert chat["reach"] == {"ok": True, "words": "отвечает"} and chat["status"] == 200 and chat["count"] == 2
    assert chat["key"]["state"] == "accepted" and slot["key"]["state"] == "accepted" and slot["count"] == 3
    models = by_id(p)
    assert caps(models["lustify"]) == {(c, "catalog") for c in ("generate", "edit", "mask", "strength", "seed")}
    assert caps(models["flux"]) == {(c, "catalog") for c in ("generate", "edit", "mask", "seed")}
    assert models["flux"]["absent"] is True and "absent" not in models["lustify"]
    assert caps(models["wan"]) == {("video", "catalog")}
    assert caps(models["whisperx"]) == {("diarize", "catalog"), ("dictation", "catalog")}
    assert caps(models["gigaam"]) == {("dictation_ru", "catalog")} and caps(models["qwen3-tts"]) == {("voices", "catalog")}
    assert {("describe", "catalog"), ("translate", "catalog")} <= caps(models["gemma4-abl"])
    assert models["local-gemma-abl"]["context_length"] == 131072 and "other-x" in models
    # найденные — короткие имена перечня, которые шлюз назвал; чат — из /v1/models
    found = r.json()["found"]
    assert found["photo"] == ["lustify"] and found["video"] == ["wan"]
    assert found["chat"] == ["local-gemma-abl", "gemma4-abl"] and "transcribe" not in found
    # паспорт — в подключении и в разделах, токена в нём нет
    assert studio.c.get("/api/connections").json()["items"][0]["passport"] == p
    sect = studio.c.get("/api/sections").json()["sections"][1]["links"][0]["connection"]
    assert sect["passport"] == p and FakeGateway.TOKEN not in json.dumps(p)


def test_gateway_slot_closed_to_the_key_is_limited(studio):
    cid = add_gateway(studio)

    async def closed(request):
        return httpx.Response(403, json={"error": {"message": "key not allowed"}})
    studio.gw.on["/slot/models"] = closed
    p = check(studio, cid).json()["passport"]
    chat, slot = p["routes"]
    assert chat["key"]["state"] == "accepted"
    assert slot["key"] == {"state": "limited", "code": 403, "words": "принят, маршрут закрыт (403)"}
    models = by_id(p)
    assert "absent" not in models["flux"]                              # список не прочитан — не судим
    assert "gemma4-abl" in models and ("describe", "catalog") not in caps(models["gemma4-abl"])


def test_wrong_key_is_refused_and_a_dead_address_does_not_answer(studio):
    cid = add_gateway(studio)
    studio.c.patch(f"/api/connections/{cid}", json={"token": "wrong-gateway-key"})
    p = check(studio, cid).json()["passport"]
    assert {r["key"]["state"] for r in p["routes"]} == {"refused"}
    assert p["routes"][0]["key"] == {"state": "refused", "code": 401, "words": "не принят (401): введите токен заново"}
    studio.gw.down = True
    p = check(studio, cid).json()["passport"]
    assert all(not r["reach"]["ok"] and r["reach"]["words"].startswith("не отвечает: ") for r in p["routes"])
    assert all(r["key"] is None and r["ms"] is None for r in p["routes"])


def test_dropped_address_is_told_without_a_request(studio):
    cid = add_gateway(studio)
    allow()
    r = check(studio, cid)
    assert r.status_code == 200 and studio.gw.requests == []
    p = r.json()["passport"]
    assert [x["path"] for x in p["routes"]] == ["/v1/models", "/slot/models"]
    assert {x["reach"]["words"] for x in p["routes"]} == {"адрес подключения «srvllm» убран из списка администратора"}
    assert r.json()["found"] == {}


def test_name_inside_is_refused_before_the_request(studio, monkeypatch):
    allow("api.cloud.test:443")
    async def resolve(host, port): return ["10.0.0.7"]
    monkeypatch.setattr(upstream, "RESOLVE", resolve)
    cid = add_connection(studio, base_url="https://api.cloud.test/v1", token="cloud-key-1")
    p = check(studio, cid).json()["passport"]
    assert studio.gw.requests == [] and "внутренний адрес" in p["routes"][0]["reach"]["words"]


def test_direct_and_foreign_connections(studio):
    add_gateway(studio)
    direct = add_connection(studio)
    r = check(studio, direct)
    assert r.status_code == 409 and code(r) == "direct" and studio.gw.requests == []
    make_user(studio, "bob"); signin(studio, "bob")
    r = check(studio, direct)
    assert r.status_code == 404 and code(r) == "gone"


# ---- через сервер: openai ----------------------------------------------------------

def test_openai_list_closed_without_a_key_proves_the_key(studio):
    cid = add_connection(studio, base_url=GW + "/v1", token=FakeGateway.TOKEN)
    p = check(studio, cid).json()["passport"]
    auths = [q.headers.get("authorization") for q in studio.gw.requests]
    assert [q.url.path for q in studio.gw.requests] == ["/v1/models", "/v1/models"]
    assert auths == [f"Bearer {FakeGateway.TOKEN}", None]                # второй — без ключа
    assert p["profile"] == "compatible" and p["routes"][0]["key"]["state"] == "accepted"


def test_openai_open_list_leaves_the_key_unverified(studio):
    cid = add_connection(studio, base_url=GW + "/v1", token="any-key-123")

    async def open_list(request):
        return httpx.Response(200, json={"data": [{"id": "m1"}]})
    studio.gw.on["/v1/models"] = open_list
    r = check(studio, cid).json()
    key = r["passport"]["routes"][0]["key"]
    assert key["state"] == "unverified" and "без ключа" in key["words"]
    assert r["found"] == {"chat": ["m1"]}


def test_openai_metadata_and_name_guesses(studio):
    cid = add_connection(studio, base_url=GW + "/v1", token=FakeGateway.TOKEN)

    async def listed(request):
        return httpx.Response(200 if request.headers.get("authorization") else 401, json={"data": [
            {"id": "acme/painter", "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["image"]}},
            {"id": "acme/texter", "context_length": 32000,
             "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
            {"id": "openai/gpt-4o", "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}},
            {"id": "gpt-image-1"}, {"id": "whisper-1"}, {"id": "tts-1"}, {"id": "plain"}]})
    studio.gw.on["/v1/models"] = listed
    models = by_id(check(studio, cid).json()["passport"])
    assert caps(models["acme/painter"]) == {("vision", "metadata"), ("generate", "metadata")}
    assert caps(models["acme/texter"]) == set() and models["acme/texter"]["context_length"] == 32000
    assert caps(models["openai/gpt-4o"]) == set()                       # метаданные важнее догадки
    assert caps(models["gpt-image-1"]) == {("generate", "name"), ("edit", "name")}
    assert caps(models["whisper-1"]) == {("transcribe", "name"), ("dictation", "name")}
    assert caps(models["tts-1"]) == {("tts", "name")} and caps(models["plain"]) == set()


@pytest.mark.parametrize("model,want", [
    ("gpt-image-1", ("generate", "edit")), ("dall-e-3", ("generate", "edit")),
    ("whisper-1", ("transcribe", "dictation")), ("gpt-4o-transcribe", ("transcribe", "dictation")),
    ("tts-1-hd", ("tts",)), ("gpt-4o-mini-tts", ("tts",)),
    ("gpt-4o-mini", ("vision",)), ("gpt-4.1", ("vision",)), ("gpt-5", ("vision",)), ("o3-mini", ("vision",)),
    ("o4-mini", ("vision",)), ("anthropic/claude-sonnet-4", ("vision",)), ("google/Gemini-2.5-pro", ("vision",)),
    ("llama-3-70b", ()), ("local-gemma-abl", ())])
def test_name_rules(model, want):
    assert capabilities.by_name(model) == want


def test_every_name_rule_is_tested():
    tested = {"gpt-image-*", "dall-e-*", "whisper-*", "*-transcribe*", "tts-*", "*-tts*", "gpt-4o*", "gpt-4.1*",
              "gpt-5*", "o3*", "o4*", "claude-*", "gemini-*"}
    assert {p for patterns, _ in capabilities.NAME_RULES for p in patterns} == tested


def test_openrouter_key_is_checked_by_its_key_route(studio, public):
    allow("openrouter.ai:443")
    cid = add_connection(studio, base_url="https://openrouter.ai/api/v1", token="or-key-1234")

    async def open_list(request):
        return httpx.Response(200, json={"data": [{"id": "openai/gpt-4o"}]})

    async def key(request):
        ok = request.headers.get("authorization") == "Bearer or-key-1234"
        return httpx.Response(200 if ok else 401, json={"data": {"label": "x"}})
    studio.gw.on["/api/v1/models"] = open_list
    studio.gw.on["/api/v1/key"] = key
    p = check(studio, cid).json()["passport"]
    assert p["profile"] == "openrouter"
    assert [r["path"] for r in p["routes"]] == ["/api/v1/models", "/api/v1/key"]
    assert [r["key"]["state"] for r in p["routes"]] == ["unverified", "accepted"]
    assert all(q.method == "GET" for q in studio.gw.requests)


def test_profiles():
    def c(kind, base):
        return connections.Connection(1, 1, "c", kind, base, frozenset({"chat"}), True)
    assert capabilities.profile(c("srvllm", GW)) == "srvllm"
    assert capabilities.profile(c("openai", "https://api.openai.com/v1")) == "openai"
    assert capabilities.profile(c("openai", "https://openrouter.ai/api/v1")) == "openrouter"
    assert capabilities.profile(c("openai", GW + "/v1")) == "compatible"


def test_check_logs_only_the_id_and_the_outcome(studio, caplog):
    caplog.set_level(logging.DEBUG)
    cid = add_gateway(studio)
    check(studio, cid)
    assert f"подключение {cid} — проверка ok" in caplog.text
    assert "192.168" not in caplog.text and "gemma" not in caplog.text and "slot" not in caplog.text
