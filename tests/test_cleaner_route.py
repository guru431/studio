"""Настройки шлюза Cleaner для внешних моделей: администратор, служебный ключ шифром, шаг схемы 66 и откат."""
import asyncio
import json
import logging
from dataclasses import replace

import httpx
import pytest
from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException

import auth
import cleaner_route
import connections
import sections
import security
import state
import upstream
from conftest import add_connection, make_user, set_passport, signin, stored_passport

ADDRESS = "https://10.77.1.20:5464"
KEY = "ck_" + "a" * 43


def allow(studio, *entries):
    """Список разрешённых адресов администратора: к умолчанию добавляются записи теста."""
    state.DB.execute("UPDATE settings SET value = ? WHERE key = 'allowlist'",
                     (json.dumps(["10.77.1.15:4000", *entries]),))


def admin(studio):
    make_user(studio, "boss", is_admin=True)
    signin(studio, "boss")


def save(studio, **body):
    return studio.c.put("/api/admin/cleaner", json={"base_url": ADDRESS, "key": KEY, "forced": False} | body)


def test_admin_saves_cleaner_settings_and_key_never_returns(studio):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    response = save(studio)
    assert response.status_code == 200
    assert response.json() == {"base_url": ADDRESS, "key_set": True, "forced": False, "web": False, "reenter": False}
    assert KEY not in response.text + studio.c.get("/api/admin/cleaner").text
    raw = state.DB.execute("SELECT value FROM settings WHERE key = ?", (cleaner_route.SETTINGS_KEY,)).fetchone()[0]
    assert KEY.encode() not in raw and KEY in security.decrypt(raw)
    assert cleaner_route.configured() and not cleaner_route.forced()


def test_empty_settings_before_first_save(studio):
    admin(studio)
    assert studio.c.get("/api/admin/cleaner").json() == {"base_url": "", "key_set": False, "forced": False,
                                                          "web": False, "reenter": False}
    assert not cleaner_route.configured() and not cleaner_route.forced()


@pytest.mark.parametrize("address", ["https://10.77.1.99:5464", "https://cleaner.example.com"])
def test_unlisted_cleaner_address_is_refused(studio, address):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    response = save(studio, base_url=address)
    assert response.status_code == 422 and response.json()["detail"]["code"] == "server"
    assert "в списке" in response.json()["detail"]["message"]
    assert studio.c.get("/api/admin/cleaner").json()["base_url"] == ""


@pytest.mark.parametrize("address", [ADDRESS + "/api", "ftp://10.77.1.20:5464", "https://user:pw@10.77.1.20:5464",
                                     ADDRESS + "?a=1"])
def test_cleaner_address_is_a_bare_origin(studio, address):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    response = save(studio, base_url=address)
    assert response.status_code == 422 and response.json()["detail"]["code"] == "url"


def test_trailing_slash_is_dropped(studio):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    assert save(studio, base_url=ADDRESS + "/").json()["base_url"] == ADDRESS


def test_forced_needs_address_and_key(studio):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    for body in ({"base_url": "", "key": KEY}, {"key": None}, {"key": ""}):
        response = save(studio, forced=True, **body)
        assert response.status_code == 422 and response.json()["detail"]["code"] == "cleaner", body
    assert not cleaner_route.forced()
    assert save(studio, forced=True).json()["forced"] is True and cleaner_route.forced()


def test_bad_key_is_refused_without_echo(studio):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    response = save(studio, key="ключ с пробелом")
    assert response.status_code == 422 and response.json()["detail"]["code"] == "key"
    assert "ключ с пробелом" not in response.text


def test_empty_key_keeps_the_previous(studio):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    save(studio)
    response = save(studio, key=None, forced=True)
    assert response.json() == {"base_url": ADDRESS, "key_set": True, "forced": True, "web": False, "reenter": False}
    assert cleaner_route.settings()["key"] == KEY
    assert save(studio, key="").json()["key_set"] is True and cleaner_route.settings()["key"] == KEY


@pytest.mark.parametrize(("forced", "web", "legacy"), [(True, False, False), (False, True, False), (False, False, True)])
def test_new_data_key_asks_to_reenter(studio, monkeypatch, forced, web, legacy):
    """Смена STUDIO_DATA_KEY сохраняет известную политику; без её снимка действуют оба запрета."""
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    save(studio, forced=forced, web=web)
    if legacy:
        state.DB.execute("DELETE FROM settings WHERE key = ?", (cleaner_route.POLICY_KEY,))
    monkeypatch.setattr(state, "CFG", replace(state.CFG, data_key=Fernet.generate_key()))
    assert studio.c.get("/api/admin/cleaner").json() == {"base_url": "", "key_set": False, "forced": forced or legacy,
                                                          "web": web or legacy, "reenter": True}
    assert not cleaner_route.configured() and cleaner_route.forced() is (forced or legacy)
    assert cleaner_route.web_only() is (web or legacy)
    assert studio.c.post("/api/admin/cleaner/check").status_code == 503
    assert save(studio, key=None, forced=True).status_code == 422          # прежнего ключа нет
    response = save(studio)
    assert response.status_code == 200 and response.json()["reenter"] is False and response.json()["key_set"] is True


def test_settings_are_admin_only(studio):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    make_user(studio, "member")
    signin(studio, "member")
    for method, path, body in [("GET", "/api/admin/cleaner", None),
                               ("PUT", "/api/admin/cleaner", {"base_url": ADDRESS, "key": KEY}),
                               ("POST", "/api/admin/cleaner/check", None)]:
        assert studio.c.request(method, path, json=body).status_code == 403, path
    assert not cleaner_route.configured()


def test_settings_change_is_logged_without_values(studio, caplog):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    caplog.set_level(logging.INFO, logger="studio")
    save(studio, forced=True)
    assert "cleaner: настройки сохранены" in caplog.text
    assert KEY not in caplog.text and "10.77.1.20" not in caplog.text


def test_changing_address_or_key_queues_contexts_of_the_new_kind(studio):
    """Контексты нового вида (отметка `cleaner_route_contexts`) уходят в очередь со снимком прежнего адреса и ключа и
    признаком `route`; контекст прежнего вида Cleaner (личный ключ, без отметки) остаётся."""
    uid = make_user(studio, "owner")
    admin(studio)
    allow(studio, "10.77.1.20:5464", "10.77.1.21:5464")
    save(studio)
    add = ("INSERT INTO connections(user_id, name, kind, base_url, token_enc, sections, created, server_only, cleaner)"
           " VALUES (?, ?, 'openai', 'https://api.cloud.test/v1', ?, '[]', 0, 1, ?)")
    plain = state.DB.execute(add, (uid, "plain", security.encrypt("provider"), 0)).lastrowid
    legacy = state.DB.execute(add, (uid, "legacy", security.encrypt("personal"), 1)).lastrowid
    context = "INSERT INTO cleaner_contexts VALUES (?, ?, 'chat', ?, ?, ?, ?, ?, 0)"
    state.DB.execute(context, ("ctx-new", uid, "c1", plain, "remote-new", ADDRESS, security.encrypt(KEY)))
    state.DB.execute("INSERT INTO cleaner_route_contexts(id) VALUES ('ctx-new')")
    state.DB.execute(context, ("ctx-old", uid, "c2", legacy, "remote-old", "https://x.test", security.encrypt("personal")))

    save(studio, forced=True)                                             # адрес и ключ прежние — ничего не уходит
    assert state.DB.execute("SELECT count(*) FROM cleaner_deletions").fetchone()[0] == 0
    save(studio, base_url="https://10.77.1.21:5464")
    rows = state.DB.execute("SELECT id, remote_id, base_url, token_enc, route FROM cleaner_deletions").fetchall()
    assert [(r["id"], r["remote_id"], r["base_url"], r["route"]) for r in rows] == [("ctx-new", "remote-new", ADDRESS, 1)]
    assert security.decrypt(rows[0]["token_enc"]) == KEY
    assert [r[0] for r in state.DB.execute("SELECT id FROM cleaner_contexts")] == ["ctx-old"]
    assert state.DB.execute("SELECT count(*) FROM cleaner_route_contexts").fetchone()[0] == 0

    state.DB.execute(context, ("ctx-new2", uid, "c3", plain, "remote-new2", "https://10.77.1.21:5464",
                               security.encrypt(KEY)))
    state.DB.execute("INSERT INTO cleaner_route_contexts(id) VALUES ('ctx-new2')")
    save(studio, base_url="https://10.77.1.21:5464", key="ck_" + "b" * 43)           # только ключ
    assert {r[0] for r in state.DB.execute("SELECT id FROM cleaner_deletions")} == {"ctx-new", "ctx-new2"}
    assert [r[0] for r in state.DB.execute("SELECT id FROM cleaner_contexts")] == ["ctx-old"]
    state.DB.execute("DELETE FROM cleaner_contexts WHERE id = 'ctx-old'")             # прежний вид — без признака
    assert state.DB.execute("SELECT route FROM cleaner_deletions WHERE id = 'ctx-old'").fetchone()[0] == 0


# ---- самопроверка ------------------------------------------------------------------

def check_handler(studio, status=200, body=None):
    seen = []

    async def handle(request):
        seen.append(request)
        return httpx.Response(status, json=body if body is not None else {"ok": True, "checks": [
            {"name": "key", "ok": True}, {"name": "layers", "ok": True}]})

    studio.gw.on["/agent/openai/v1/cleaner/check"] = handle
    return seen


def test_check_sends_only_the_service_key_header(studio):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    save(studio)
    seen = check_handler(studio)
    response = studio.c.post("/api/admin/cleaner/check")
    assert response.status_code == 200 and response.json()["ok"] is True and len(response.json()["checks"]) == 2
    assert seen[0].headers["x-cleaner-key"] == KEY and "authorization" not in seen[0].headers
    assert str(seen[0].url) == ADDRESS + "/agent/openai/v1/cleaner/check"
    assert KEY not in response.text


def test_check_needs_settings(studio):
    admin(studio)
    response = studio.c.post("/api/admin/cleaner/check")
    assert response.status_code == 503 and response.json()["detail"]["code"] == "cleaner_unset"


@pytest.mark.parametrize("status", [401, 403])
def test_check_reports_a_refused_key_in_words(studio, status):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    save(studio)
    check_handler(studio, status, {"error": {"message": "Cleaner: bad key " + KEY}})
    response = studio.c.post("/api/admin/cleaner/check")
    assert response.status_code == 502 and "служебный ключ" in response.json()["detail"]["message"]
    assert KEY not in response.text


def test_check_when_cleaner_is_down_or_unlisted(studio, caplog):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    save(studio)
    caplog.set_level(logging.INFO, logger="studio")
    studio.gw.down = True
    response = studio.c.post("/api/admin/cleaner/check")
    assert response.status_code == 502 and response.json()["detail"]["message"] == "Cleaner недоступен"
    allow(studio)                                                       # адрес убран из списка
    response = studio.c.post("/api/admin/cleaner/check")
    assert response.status_code == 502 and "списка" in response.json()["detail"]["message"]
    assert KEY not in caplog.text and "10.77.1.20" not in caplog.text


# ---- шаг схемы 66 и откат ------------------------------------------------------------

def test_rollback_64_spoils_routed_tokens_and_forward_restores(studio, caplog):
    uid = make_user(studio, "owner")
    add = ("INSERT INTO connections(user_id, name, kind, base_url, token_enc, sections, created, server_only, via_cleaner)"
           " VALUES (?, ?, 'openai', 'https://api.cloud.test/v1', ?, '[]', 0, 1, ?)")
    routed = state.DB.execute(add, (uid, "routed", security.encrypt("provider-key"), 1)).lastrowid
    plain = state.DB.execute(add, (uid, "plain", security.encrypt("plain-key"), 0)).lastrowid

    def token(cid):
        return state.DB.execute("SELECT token_enc FROM connections WHERE id = ?", (cid,)).fetchone()[0]

    state.DB.executescript(cleaner_route.ROLLBACK_66)
    with pytest.raises(InvalidToken):
        security.decrypt(token(routed))                                 # прежний код: «токен не расшифрован»
    assert security.decrypt(token(plain)) == "plain-key"
    state.DB.executescript(cleaner_route.ROLLBACK_66)                   # повтор не затирает сохранённый ключ
    state.DB.executescript(cleaner_route.FORWARD_66)
    assert security.decrypt(token(routed)) == "provider-key" and security.decrypt(token(plain)) == "plain-key"
    assert state.DB.execute("SELECT count(*) FROM cleaner_rollback").fetchone()[0] == 0
    state.DB.executescript(cleaner_route.FORWARD_66)                    # повтор без строк ничего не меняет
    assert security.decrypt(token(routed)) == "provider-key"

    state.DB.executescript(cleaner_route.ROLLBACK_66)
    state.DB.execute("UPDATE connections SET token_enc = ? WHERE id = ?", (security.encrypt("typed-again"), routed))
    state.DB.executescript(cleaner_route.FORWARD_66)                    # ключ, введённый заново прежним кодом, остаётся
    assert security.decrypt(token(routed)) == "typed-again"

    caplog.set_level(logging.INFO, logger="studio")
    cleaner_route.forward()                                             # строк нет — ничего не делает и не пишет
    assert "после отката" not in caplog.text
    state.DB.executescript(cleaner_route.ROLLBACK_66)
    cleaner_route.forward()
    assert security.decrypt(token(routed)) == "typed-again"
    assert "после отката возвращено ключей 1" in caplog.text
    assert state.DB.execute("SELECT count(*) FROM cleaner_rollback").fetchone()[0] == 0


def test_rollback_64_skips_a_token_already_spoiled_by_the_access_rollback(studio):
    """Ключ, уже испорченный откатом шага 56, не сохраняется как «настоящий»: вперёд вернётся только он."""
    uid = make_user(studio, "owner")
    cid = state.DB.execute(
        "INSERT INTO connections(user_id, name, kind, base_url, token_enc, sections, created, server_only, via_cleaner)"
        " VALUES (?, 'routed', 'openai', 'https://api.cloud.test/v1', x'00', '[]', 0, 1, 1)", (uid,)).lastrowid
    state.DB.executescript(cleaner_route.ROLLBACK_66)
    assert state.DB.execute("SELECT count(*) FROM cleaner_rollback WHERE connection_id = ?", (cid,)).fetchone()[0] == 0


# ---- внутреннее и внешнее, переключатель, действующий маршрут ----------------------------

PROVIDER = "https://api.cloud.test/v1"


def entry(url: str) -> str:
    parsed = httpx.URL(url)
    host = f"[{parsed.host}]" if ":" in parsed.host else parsed.host
    return f"{host}:{parsed.port or upstream.DEFAULT_PORTS[parsed.scheme]}"


def conn_of(cid: int) -> connections.Connection:
    uid = state.DB.execute("SELECT user_id FROM connections WHERE id = ?", (cid,)).fetchone()[0]
    return connections.get(uid, cid)


def configure(studio, forced=False):
    """Cleaner настроен: адрес — в списке разрешённых, служебный ключ задан."""
    if auth.CSRF_HEADER not in studio.c.headers:
        admin(studio)
    allow(studio, entry(ADDRESS))
    assert save(studio, forced=forced).status_code == 200


def provider_listed(monkeypatch):
    """Имя провайдера — в списке разрешённых, разрешается в публичный адрес."""
    allow(None, entry(ADDRESS), "api.cloud.test:443", "other.cloud.test:443")

    async def resolve(host, port):
        return ["93.184.216.34"]
    monkeypatch.setattr(upstream, "RESOLVE", resolve)


@pytest.mark.parametrize("kind, base_url, listed, flags, external", [
    ("openai", "https://10.77.1.30:8000/v1", True, {}, False),      # частный IP из списка
    ("openai", "https://10.77.1.30:8000/v1", False, {}, True),      # частный IP не из списка (прямое)
    ("openai", "http://localhost:1234/v1", False, {}, True),
    ("openai", "https://api.cloud.test/v1", True, {}, True),        # имя — внешнее и из списка
    ("openai", "https://8.8.8.8/v1", True, {}, True),
    ("openai", "https://[2001:4860:4860::8888]/v1", True, {}, True),
    ("openai", "https://[fd00::1]/v1", True, {}, False),
    ("openai", "https://10.77.1.15:4000", True, {"s06": True}, False),
    ("openai", "https://10.77.1.20:5464", True, {"cleaner": True}, False),
    ("srvllm", "https://10.77.1.15:4000", True, {}, False),
    # флажок s06 и вид srvllm выбирает пользователь: внутренним их делает только частный IP из списка
    ("openai", "https://api.cloud.test", True, {"s06": True}, True),
    ("openai", "https://8.8.8.8", True, {"s06": True}, True),
    ("openai", "http://10.77.1.16:4000", False, {"s06": True}, True),
    ("srvllm", "https://api.cloud.test", True, {}, True),
    ("srvllm", "https://8.8.8.8", True, {}, True),
    ("srvllm", "http://10.77.1.16:4000", False, {}, True),
])
def test_internal_and_external(studio, kind, base_url, listed, flags, external):
    if listed:
        allow(studio, entry(base_url))
    conn = connections.Connection(1, 1, "c", kind, base_url, frozenset(), False, **flags)
    assert cleaner_route.is_external(conn) is external


def test_via_cleaner_makes_a_direct_connection_server_side(studio):
    cid = add_connection(studio, base_url=PROVIDER, sections=["chat"])         # не в списке → прямое
    item = studio.c.get("/api/connections").json()["items"][0]
    assert not item["proxied"] and not item["routed"] and item["external"] and not item["cleaner_locked"]
    assert studio.c.get(f"/api/connections/{cid}/token").status_code == 200
    response = studio.c.patch(f"/api/connections/{cid}", json={"via_cleaner": True})
    assert response.status_code == 200, response.text
    item = response.json()
    assert item["via_cleaner"] and item["routed"] and item["proxied"] and item["external"]
    assert studio.c.get(f"/api/connections/{cid}/token").status_code == 403
    assert conn_of(cid).routed and conn_of(cid).proxied and not upstream.is_allowed(PROVIDER)


@pytest.mark.parametrize("fields", [
    {"kind": "srvllm", "base_url": "http://10.77.1.15:4000"},
    {"s06": True, "base_url": "http://10.77.1.15:4000"},
    {"cleaner": True, "base_url": ADDRESS},
    {"base_url": "https://10.77.1.30:8000/v1"},                          # частный из списка
])
def test_via_cleaner_only_for_external_openai(studio, fields):
    make_user(studio, "owner", is_admin=True)
    signin(studio, "owner")
    allow(studio, "10.77.1.30:8000", entry(ADDRESS))
    body = {"name": "c", "kind": "openai", "base_url": PROVIDER, "token": "cloud-key", "sections": ["chat"],
            "via_cleaner": True} | fields
    response = studio.c.post("/api/connections", json=body)
    assert response.status_code == 422 and response.json()["detail"]["code"] == "via_cleaner", response.text
    plain = studio.c.post("/api/connections", json={**body, "via_cleaner": False})        # без переключателя — можно
    assert plain.status_code == 201, plain.text
    response = studio.c.patch(f"/api/connections/{plain.json()['id']}", json={"via_cleaner": True})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "via_cleaner", response.text
    assert not conn_of(plain.json()["id"]).via_cleaner


def test_via_cleaner_links_only_chat_and_translate(studio, monkeypatch):
    provider_listed(monkeypatch)
    cid = add_connection(studio, base_url=PROVIDER, sections=["chat"])
    studio.c.patch(f"/api/connections/{cid}", json={"via_cleaner": True})
    conn = conn_of(cid)
    assert sections.accepts(conn) == ["chat", "translate"]
    with pytest.raises(HTTPException) as e:
        sections.check_link(conn, "photo")
    assert e.value.status_code == 422 and e.value.detail["code"] == "kind"
    sections.check_link(conn, "translate")                                # прямому адресу «Перевод» закрыт, через Cleaner — нет
    assert studio.c.patch(f"/api/connections/{cid}", json={"sections": ["chat", "translate"]}).status_code == 200
    response = studio.c.patch(f"/api/connections/{cid}", json={"sections": ["chat", "photo"]})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "via_cleaner"
    # включить переключатель у подключения с «Фото» — отказ, связь не теряется молча
    other = add_connection(studio, base_url="https://other.cloud.test/v1", sections=["chat", "photo"])
    response = studio.c.patch(f"/api/connections/{other}", json={"via_cleaner": True})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "via_cleaner"
    assert not conn_of(other).routed
    # прямому адресу без переключателя «Перевод» закрыт
    direct = add_connection(studio, base_url="https://direct.cloud.test/v1")
    response = studio.c.patch(f"/api/connections/{direct}", json={"sections": ["chat", "translate"]})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "server"
    assert studio.c.patch(f"/api/connections/{direct}", json={"via_cleaner": True,
                                                              "sections": ["chat", "translate"]}).status_code == 200


def test_toggle_keeps_the_token_and_resets_the_passport(studio):
    cid = add_connection(studio, base_url=PROVIDER, token="provider-key-1")
    before = state.DB.execute("SELECT token_enc, server_only FROM connections WHERE id = ?", (cid,)).fetchone()
    set_passport(cid, {"checked": 1, "models": []})
    assert studio.c.patch(f"/api/connections/{cid}", json={"via_cleaner": True}).status_code == 200
    assert stored_passport(cid) is None
    after = state.DB.execute("SELECT token_enc, server_only FROM connections WHERE id = ?", (cid,)).fetchone()
    assert tuple(after) == tuple(before) and security.decrypt(after["token_enc"]) == "provider-key-1"
    set_passport(cid, {"checked": 2, "models": []})
    assert studio.c.patch(f"/api/connections/{cid}", json={"name": "renamed"}).status_code == 200
    assert stored_passport(cid) is not None                                          # другая правка паспорт не трогает
    assert studio.c.patch(f"/api/connections/{cid}", json={"via_cleaner": False}).status_code == 200
    assert stored_passport(cid) is None and not conn_of(cid).routed
    assert studio.c.get(f"/api/connections/{cid}/token").json()["token"] == "provider-key-1"


def test_forced_routes_external_and_locks_the_toggle(studio):
    cid = add_connection(studio, base_url=PROVIDER)
    internal = add_connection(studio, kind="srvllm", base_url="http://10.77.1.15:4000", sections=["chat"])
    assert not conn_of(cid).routed
    configure(studio, forced=True)
    item = {i["id"]: i for i in studio.c.get("/api/connections").json()["items"]}
    assert item[cid]["routed"] and item[cid]["proxied"] and item[cid]["cleaner_locked"] and not item[cid]["via_cleaner"]
    assert not item[internal]["routed"] and not item[internal]["cleaner_locked"] and not item[internal]["external"]
    for value in (False, True):
        response = studio.c.patch(f"/api/connections/{cid}", json={"via_cleaner": value})
        assert response.status_code == 422 and response.json()["detail"]["code"] == "via_cleaner"
    assert studio.c.patch(f"/api/connections/{cid}", json={"name": "renamed"}).status_code == 200
    assert studio.c.get(f"/api/connections/{cid}/token").status_code == 403
    assert sections.accepts(conn_of(cid)) == ["chat", "translate"]
    save(studio, forced=False)                                                       # флажок снят — прежнее значение
    assert not conn_of(cid).routed and studio.c.get(f"/api/connections/{cid}/token").status_code == 200


def test_forced_does_not_expose_a_reentered_provider_key(studio, monkeypatch):
    cid = add_connection(studio, base_url=PROVIDER)
    configure(studio, forced=True)
    assert conn_of(cid).routed
    monkeypatch.setattr(state, "CFG", replace(state.CFG, data_key=Fernet.generate_key()))
    assert studio.c.patch(f"/api/connections/{cid}", json={"token": "new-provider-key"}).status_code == 200
    studio.gw.requests.clear()
    assert conn_of(cid).routed
    assert studio.c.get(f"/api/connections/{cid}/token").status_code == 403
    response = studio.c.get(f"/api/connections/{cid}/models")
    assert response.status_code == 503 and response.json()["detail"]["code"] == "cleaner_unset"
    assert studio.gw.requests == []


def test_routed_connection_never_reaches_the_provider(studio, monkeypatch):
    provider_listed(monkeypatch)
    cid = add_connection(studio, base_url=PROVIDER, token="provider-key-1")        # имя из списка: сервер ходит сам
    configure(studio)
    assert studio.c.patch(f"/api/connections/{cid}", json={"via_cleaner": True}).status_code == 200
    conn = conn_of(cid)
    assert conn.routed
    studio.gw.requests.clear()

    async def stream():
        async with upstream.stream(conn, "GET", PROVIDER + "/models") as response:
            return response.status_code
    for call in (lambda: upstream.request(conn, "GET", PROVIDER + "/models"),
                 lambda: upstream.request(conn, "POST", PROVIDER + "/chat/completions", json={}),
                 lambda: upstream.request(conn, "GET", "https://10.77.1.15:4000/v1/models"),
                 stream,
                 lambda: upstream.fetch_certificate(conn, PROVIDER),
                 lambda: upstream.ws_target(conn, PROVIDER)):
        with pytest.raises(upstream.UpstreamRefused) as e:
            asyncio.run(call())
        assert e.value.reason == "cleaner"
    assert studio.gw.requests == []
    assert "Cleaner" in upstream.refused_words(e.value, conn)
    # адрес Cleaner из настроек подключению открыт
    async def models(request):
        return httpx.Response(200, json={"data": []})
    studio.gw.on["/v1/models"] = models
    assert asyncio.run(upstream.request(conn, "GET", ADDRESS + "/v1/models")).status_code == 200


def test_routed_connection_is_refused_while_cleaner_is_not_configured(studio, monkeypatch):
    provider_listed(monkeypatch)
    cid = add_connection(studio, base_url=PROVIDER)
    assert studio.c.patch(f"/api/connections/{cid}", json={"via_cleaner": True}).status_code == 200     # fail closed
    with pytest.raises(upstream.UpstreamRefused) as e:
        asyncio.run(upstream.request(conn_of(cid), "GET", PROVIDER + "/models"))
    assert e.value.reason == "cleaner"


def test_token_is_not_given_to_a_snapshot_taken_before_the_toggle(studio):
    cid = add_connection(studio, base_url=PROVIDER)
    old = conn_of(cid)
    assert connections.token(old) == "cloud-key"
    studio.c.patch(f"/api/connections/{cid}", json={"via_cleaner": True})
    with pytest.raises(upstream.ConnectionChanged):
        connections.token(old)
