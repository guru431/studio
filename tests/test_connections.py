"""Подключения профиля (app/connections.py) и запросы сервера только по списку (app/upstream.py)."""
import asyncio
import io
import json
import os
import sys

import httpx
import pytest
from cryptography.fernet import Fernet

import connections
import db
import manage
import state
import upstream
from conftest import add_connection, add_gateway, make_user, signin
from fakes import FakeGateway

GW = "http://10.77.1.15:4000"


def code(r) -> str:
    return r.json()["detail"]["code"]


def conn_of(cid: int) -> connections.Connection:
    uid = state.DB.execute("SELECT user_id FROM connections WHERE id = ?", (cid,)).fetchone()[0]
    return connections.get(uid, cid)


def post(s, **fields):
    body = {"name": "c", "kind": "openai", "base_url": "https://cloud.test/v1", "token": "cloud-key",
            "sections": ["chat"], **fields}
    return s.c.post("/api/connections", json=body)


async def streamed(conn, url, **kw) -> int:
    async with upstream.stream(conn, "GET", url, **kw) as r:
        return r.status_code


# ---- список администратора -----------------------------------------------------

def test_tricky_urls_are_not_allowlisted(studio):
    for url in ["http://10.77.1.15:4000@evil.test", "http://10.77.1.15:40000", "http://10.77.1.15.evil.test:4000",
                "http://evil.test/?h=10.77.1.15:4000", "ftp://10.77.1.15:4000", "http://10.77.1.15"]:
        assert not upstream.is_allowed(url), url
    assert upstream.is_allowed("http://10.77.1.15:4000/v1/models")


def test_default_ports_and_case_are_exact(studio):
    state.DB.execute("""UPDATE settings SET value = '["10.0.0.5:80", "10.0.0.6:443", "[::1]:8080"]'"""
                     " WHERE key = 'allowlist'")
    assert upstream.is_allowed("http://10.0.0.5/v1") and upstream.is_allowed("HTTPS://10.0.0.6/x")
    assert upstream.is_allowed("http://[0:0::1]:8080/")
    for url in ["https://10.0.0.5/v1", "http://10.0.0.6/x", "http://10.0.0.5:8080", "http://user@10.0.0.5/",
                "http://[::1]/", "not a url", ""]:
        assert not upstream.is_allowed(url), url


def test_allowlist_entries_are_validated(studio):
    make_user(studio, "owner", is_admin=True); signin(studio, "owner")
    for bad in ["10.77.1.15", "a b:1", "999.1.1.1:80", "[::g]:80", "host:", "user@host:80", "host:1:2"]:
        r = studio.c.put("/api/admin/allowlist", json={"entries": [bad]})
        assert r.status_code == 422 and code(r) == "entry", bad
    r = studio.c.put("/api/admin/allowlist", json={"entries": [" 10.0.0.2:080 ", "[0:0::1]:8080", "10.0.0.1:1"]})
    assert r.status_code == 200 and r.json() == {"entries": ["10.0.0.2:80", "[::1]:8080", "10.0.0.1:1"]}
    assert upstream.allowlist() == ["10.0.0.2:80", "[::1]:8080", "10.0.0.1:1"]


def test_allowlist_takes_ip_literals_only(studio):
    """Имя в записи сверялось бы с DNS на каждом запросе: граница SSRF — у того, кто правит DNS."""
    make_user(studio, "owner", is_admin=True); signin(studio, "owner")
    for name in ["llm.lan:8080", "localhost:4000", "cloud.test:443"]:
        r = studio.c.put("/api/admin/allowlist", json={"entries": [name]})
        assert r.status_code == 422 and code(r) == "entry", name
    with pytest.raises(ValueError):
        upstream.parse_entry("llm.lan:80")
    state.DB.execute("""UPDATE settings SET value = '["llm.lan:80"]' WHERE key = 'allowlist'""")   # из старой базы
    assert not upstream.is_allowed("http://llm.lan/v1")


def test_unparseable_hosts_are_refused_not_500(studio):
    make_user(studio, "owner"); signin(studio, "owner")
    for url in ["https://xn--zz.com/v1", "https://\u0000x.test"]:
        assert not upstream.is_allowed(url), url
        r = post(studio, base_url=url)
        assert r.status_code == 422 and code(r) == "url", url
        assert url not in r.text


def test_http_client_ignores_proxy_environment(monkeypatch):
    """HTTPS_PROXY в окружении юнита повёл бы ключи шлюза через чужой прокси."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.test:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.test:3128")
    client = state.make_http()
    try:
        assert client.trust_env is False
    finally:
        asyncio.run(client.aclose())


# ---- запросы сервера -----------------------------------------------------------

def test_upstream_never_follows_redirects(studio):
    conn = conn_of(add_gateway(studio))
    studio.gw.reply = lambda request: httpx.Response(302, headers={"Location": "http://10.77.1.1/"})
    r = asyncio.run(upstream.request(conn, "GET", GW + "/v1/models", follow_redirects=True))
    assert r.status_code == 302 and len(studio.gw.requests) == 1
    assert asyncio.run(streamed(conn, GW + "/v1/models", follow_redirects=True)) == 302
    assert len(studio.gw.requests) == 2
    assert all(str(q.url).startswith(GW) for q in studio.gw.requests)
    assert all(q.headers["authorization"] == f"Bearer {FakeGateway.TOKEN}" for q in studio.gw.requests)


def test_request_outside_the_list_is_refused_before_any_io(studio):
    conn = conn_of(add_gateway(studio))
    for url in ["http://10.77.1.1/", "http://10.77.1.15:4000@evil.test/", "https://cloud.test/v1/models",
                "file:///etc/passwd"]:
        with pytest.raises(upstream.UpstreamRefused):
            asyncio.run(upstream.request(conn, "POST", url, json={"x": 1}))
        with pytest.raises(upstream.UpstreamRefused):
            asyncio.run(streamed(conn, url))
    assert studio.gw.requests == []


def test_gateway_401_words_point_to_the_profile():
    text = upstream.gateway_error(httpx.Response(401), "srvllm")
    assert "srvllm" in text and "профиле" in text
    busy = httpx.Response(409, json={"detail": {"message": "идут ролики: 2"}})
    assert upstream.gateway_error(busy, "srvllm") == "идут ролики: 2"
    assert upstream.message(httpx.Response(400, json={"error": {"message": "плохо"}})) == "плохо"
    assert upstream.gateway_error(httpx.Response(418, text="не json"), "x") == "шлюз ответил 418: код 418"


# ---- подключения профиля -------------------------------------------------------

def test_proxied_token_never_leaves_the_server(studio):
    cid = add_connection(studio, kind="srvllm", base_url="http://10.77.1.15:4000", token="gw-secret-token")
    assert "gw-secret-token" not in studio.c.get("/api/connections").text
    assert studio.c.get(f"/api/connections/{cid}/token").status_code == 403


def test_connection_out_carries_only_a_hint(studio):
    cid = add_connection(studio, token="cloud-key-1234567", sections=["chat"], name="Облако")
    items = studio.c.get("/api/connections").json()["items"]
    assert items == [{"id": cid, "name": "Облако", "kind": "openai", "base_url": "https://cloud.test/v1",
                      "sections": ["chat"], "proxied": False, "token_hint": "…4567"}]
    cid = add_gateway(studio)
    gw = studio.c.get("/api/connections").json()["items"][1]
    assert gw["id"] == cid and gw["proxied"] and gw["sections"] == ["chat", "photo", "video"]


def test_direct_connection_needs_https_except_localhost(studio):
    make_user(studio, "owner"); signin(studio, "owner")
    r = post(studio, base_url="http://cloud.test/v1")
    assert r.status_code == 422 and code(r) == "https" and "списке администратора" in r.json()["detail"]["message"]
    for url in ["http://localhost:11434/v1", "http://127.0.0.1:8080", "http://[::1]:8080/v1"]:
        r = post(studio, base_url=url)
        assert r.status_code == 201 and not r.json()["proxied"], url
    r = post(studio, base_url="https://cloud.test/v1/")
    assert r.status_code == 201 and r.json()["base_url"] == "https://cloud.test/v1"
    r = post(studio, base_url=GW + "/v1")                       # внутренний адрес из списка — через сервер
    assert r.status_code == 201 and r.json()["proxied"]
    for url in ["https://u:p@cloud.test/v1", "https://cloud.test/v1?x=1", "https://cloud.test/v1#a",
                "ftp://cloud.test", "cloud.test/v1", "https://"]:
        r = post(studio, base_url=url)
        assert r.status_code == 422 and code(r) == "url", url
    for token in ["", "токен", "a\nb", "x" * 513]:
        r = post(studio, token=token)
        assert r.status_code == 422 and code(r) in ("token", "invalid"), repr(token)


def test_srvllm_kind_must_be_on_the_list(studio):
    make_user(studio, "owner"); signin(studio, "owner")
    for url in ["https://cloud.test", "http://10.77.1.15:40000", "http://localhost:4000"]:
        r = post(studio, kind="srvllm", base_url=url, sections=["photo"])
        assert r.status_code == 422 and code(r) == "srvllm", url
    for kind, sections in [("openai", ["photo"]), ("openai", []), ("srvllm", ["chat", "music"]), ("nope", ["chat"])]:
        r = post(studio, kind=kind, base_url=GW, sections=sections)
        assert r.status_code == 422 and code(r) in ("sections", "kind"), (kind, sections)
    cid = add_gateway(studio)
    r = studio.c.patch(f"/api/connections/{cid}", json={"base_url": "https://cloud.test"})
    assert r.status_code == 422 and code(r) == "srvllm"
    assert conn_of(cid).base_url == GW
    r = studio.c.patch(f"/api/connections/{cid}", json={"kind": "openai"})   # разделы фото/видео — не для openai
    assert r.status_code == 422 and code(r) == "sections"


def test_patch_changes_only_what_was_sent(studio):
    cid = add_connection(studio, token="cloud-key-1234567")
    r = studio.c.patch(f"/api/connections/{cid}", json={"name": "Новое"})
    assert r.status_code == 200 and r.json()["name"] == "Новое" and r.json()["token_hint"] == "…4567"
    r = studio.c.patch(f"/api/connections/{cid}", json={"token": "other-key-7654321"})
    assert r.json()["token_hint"] == "…4321" and r.json()["name"] == "Новое"
    assert connections.token(conn_of(cid)) == "other-key-7654321"


def test_direct_token_is_given_to_its_owner_only(studio):
    cid = add_connection(studio)
    # адреса — те, куда браузеру можно нести этот токен: не из списка подключений, который мог устареть
    assert studio.c.get(f"/api/connections/{cid}/token").json() == {
        "token": "cloud-key", "chat_url": "https://cloud.test/v1/chat/completions",
        "models_url": "https://cloud.test/v1/models"}
    make_user(studio, "bob"); signin(studio, "bob")
    assert studio.c.get("/api/connections").json() == {"items": []}
    for method, path, body in [("GET", f"/api/connections/{cid}/token", None),
                               ("GET", f"/api/connections/{cid}/models", None),
                               ("PATCH", f"/api/connections/{cid}", {"name": "моё"}),
                               ("DELETE", f"/api/connections/{cid}", None)]:
        r = studio.c.request(method, path, json=body)
        assert r.status_code == 404 and code(r) == "gone", (method, path)
    assert conn_of(cid).name == "openai"


def test_token_of_a_connection_dropped_from_the_list_stays_on_the_server(studio):
    cid = add_connection(studio, base_url=GW + "/v1", token="internal-secret")
    state.DB.execute("UPDATE settings SET value = '[]' WHERE key = 'allowlist'")
    assert studio.c.get(f"/api/connections/{cid}/token").status_code == 403


def test_https_entry_dropped_from_the_list_keeps_its_token_on_the_server(studio):
    """Запись https убрали из списка — адрес браузеру открыт, но токен вводили для сервера."""
    state.DB.execute("""UPDATE settings SET value = '["203.0.113.10:443"]' WHERE key = 'allowlist'""")
    cid = add_connection(studio, base_url="https://203.0.113.10/v1", token="internal-https-secret")
    state.DB.execute("UPDATE settings SET value = '[]' WHERE key = 'allowlist'")
    assert not conn_of(cid).proxied
    r = studio.c.get(f"/api/connections/{cid}/token")
    assert r.status_code == 403 and "internal-https-secret" not in r.text
    # новый токен, введённый для прямого подключения, — уже браузерный
    assert studio.c.patch(f"/api/connections/{cid}", json={"token": "direct-key-0001"}).status_code == 200
    assert studio.c.get(f"/api/connections/{cid}/token").json()["token"] == "direct-key-0001"


def test_proxied_connection_turned_direct_needs_a_new_token(studio):
    """Иначе: srvllm → openai на чужой адрес, и /token отдаёт ключ шлюза (A1-C1)."""
    cid = add_connection(studio, kind="srvllm", base_url=GW, token="gw-secret-token")
    evil = {"kind": "openai", "base_url": "https://evil.test", "sections": ["chat"]}
    r = studio.c.patch(f"/api/connections/{cid}", json=evil)
    assert r.status_code == 422 and r.json()["detail"] == {"code": "token", "message": "новый адрес — введите токен заново"}
    assert conn_of(cid).kind == "srvllm" and conn_of(cid).base_url == GW
    r = studio.c.get(f"/api/connections/{cid}/token")
    assert r.status_code == 403 and "gw-secret-token" not in r.text
    r = studio.c.patch(f"/api/connections/{cid}", json=evil | {"token": "evil-own-key-1"})
    assert r.status_code == 200 and not r.json()["proxied"]
    assert studio.c.get(f"/api/connections/{cid}/token").json()["token"] == "evil-own-key-1"


def test_repointing_a_proxied_connection_needs_a_new_token(studio):
    """Ключ одного внутреннего адреса не уходит другому: смена хоста, порта, схемы, пути или вида — с токеном."""
    state.DB.execute("""UPDATE settings SET value = '["10.77.1.15:4000", "10.77.1.16:4000"]'"""
                     " WHERE key = 'allowlist'")
    cid = add_connection(studio, kind="openai", base_url=GW + "/v1", token="gw-secret-token", sections=["chat"])
    for change in [{"base_url": "http://10.77.1.16:4000/v1"}, {"base_url": GW},
                   {"kind": "srvllm", "base_url": GW}]:
        r = studio.c.patch(f"/api/connections/{cid}", json=change)
        assert r.status_code == 422 and code(r) == "token", change
    assert conn_of(cid).base_url == GW + "/v1" and conn_of(cid).kind == "openai"
    for same in [{"name": "Новое"}, {"base_url": GW + "/v1/"}, {"base_url": "HTTP://10.77.1.15:4000/v1"},
                 {"sections": ["chat"]}]:
        assert studio.c.patch(f"/api/connections/{cid}", json=same).status_code == 200, same
    r = studio.c.patch(f"/api/connections/{cid}", json={"base_url": "http://10.77.1.16:4000/v1", "token": "k16-key"})
    assert r.status_code == 200 and connections.token(conn_of(cid)) == "k16-key"
    # так же — для подключений пользователя в панели администратора
    bob = make_user(studio, "bob")
    base = f"/api/admin/users/{bob}/connections"
    bid = studio.c.post(base, json={"name": "srvllm", "kind": "srvllm", "base_url": GW, "token": "bob-gateway-key",
                                    "sections": ["chat"]}).json()["id"]
    r = studio.c.patch(f"{base}/{bid}", json={"kind": "openai", "base_url": "https://evil.test"})
    assert r.status_code == 422 and code(r) == "token"
    assert connections.token(conn_of(bid)) == "bob-gateway-key" and conn_of(bid).kind == "srvllm"


def test_proxied_base_path_is_fixed(studio):
    """Через сервер ключ уходит только на известные пути: srvllm — без пути, openai — без пути или /v1."""
    make_user(studio, "owner"); signin(studio, "owner")
    for kind, url in [("srvllm", GW + "/v1"), ("srvllm", GW + "/slot"), ("openai", GW + "/admin"),
                      ("openai", GW + "/v1/x")]:
        r = post(studio, kind=kind, base_url=url, sections=["chat"])
        assert r.status_code == 422 and code(r) == "path", (kind, url)
    for kind, url in [("srvllm", GW), ("srvllm", GW + "/"), ("openai", GW), ("openai", GW + "/v1")]:
        assert post(studio, kind=kind, base_url=url, sections=["chat"]).status_code == 201, (kind, url)
    assert post(studio, base_url="https://cloud.test/any/path").status_code == 201   # прямое — любой путь


def test_token_is_encrypted_at_rest(studio):
    cid = add_connection(studio, token="cloud-key-secret")
    blob = state.DB.execute("SELECT token_enc FROM connections WHERE id = ?", (cid,)).fetchone()[0]
    assert b"cloud-key-secret" not in blob and connections.token(conn_of(cid)) == "cloud-key-secret"


def test_delete_clears_the_profile_default(studio):
    cid = add_connection(studio)
    studio.c.patch("/api/me", json={"default_connection_id": cid})
    assert studio.c.delete(f"/api/connections/{cid}").status_code == 204
    assert studio.c.get("/api/connections").json() == {"items": []}
    assert studio.c.get("/api/me").json()["default_connection_id"] is None


def test_capabilities_follow_connections(studio):
    make_user(studio, "owner"); signin(studio, "owner")
    caps = lambda: studio.c.get("/api/me").json()["capabilities"]   # noqa: E731
    assert caps() == {"chat": False, "photo": False, "video": False}
    cid = add_connection(studio, kind="srvllm", base_url=GW, sections=["chat", "photo"])
    assert caps() == {"chat": True, "photo": True, "video": False}
    uid = conn_of(cid).user_id
    assert connections.photo_connection(uid).id == cid and connections.video_connection(uid) is None
    state.DB.execute("UPDATE settings SET value = '[]' WHERE key = 'allowlist'")   # не через сервер — не фото
    assert caps() == {"chat": True, "photo": False, "video": False}


def test_urls_of_each_kind():
    def c(kind, base):
        return connections.Connection(1, 1, "c", kind, base, frozenset({"chat"}), True)
    assert connections.chat_url(c("openai", "https://cloud.test/v1")) == "https://cloud.test/v1/chat/completions"
    assert connections.models_url(c("openai", "https://cloud.test/v1")) == "https://cloud.test/v1/models"
    assert connections.chat_url(c("srvllm", GW)) == GW + "/v1/chat/completions"
    assert connections.models_url(c("srvllm", GW)) == GW + "/v1/models"
    assert connections.slot_url(c("srvllm", GW), "/images/generations") == GW + "/slot/images/generations"


def test_models_of_a_proxied_connection_come_through_the_server(studio):
    cid = add_gateway(studio)
    studio.gw.reply = lambda request: httpx.Response(200, json={"data": [
        {"id": "local-gemma-abl", "context_length": 8192}, {"id": "lustify", "owned_by": "x"}, {"no": "id"}]})
    r = studio.c.get(f"/api/connections/{cid}/models")
    assert r.status_code == 200
    assert r.json() == {"items": [{"id": "local-gemma-abl", "context_length": 8192}, {"id": "lustify"}]}
    (q,) = studio.gw.requests
    assert str(q.url) == GW + "/v1/models" and q.headers["authorization"] == f"Bearer {FakeGateway.TOKEN}"
    studio.gw.reply = lambda request: httpx.Response(401)
    r = studio.c.get(f"/api/connections/{cid}/models")
    assert r.status_code == 502 and code(r) == "upstream" and "«srvllm»" in r.json()["detail"]["message"]
    direct = add_connection(studio)
    r = studio.c.get(f"/api/connections/{direct}/models")
    assert r.status_code == 409 and code(r) == "direct" and len(studio.gw.requests) == 2


def test_admin_manages_a_users_connections(studio):
    make_user(studio, "owner", is_admin=True); bob = make_user(studio, "bob"); signin(studio, "owner")
    base = f"/api/admin/users/{bob}/connections"
    r = studio.c.post(base, json={"name": "srvllm", "kind": "srvllm", "base_url": GW, "token": "bob-gateway-key",
                                  "sections": ["photo", "chat"]})
    assert r.status_code == 201 and r.json()["proxied"] and "bob-gateway-key" not in r.text
    cid = r.json()["id"]
    assert conn_of(cid).user_id == bob
    assert [c["id"] for c in studio.c.get(base).json()["items"]] == [cid]
    assert studio.c.get("/api/connections").json() == {"items": []}           # у администратора своих нет
    users = {u["id"]: u for u in studio.c.get("/api/admin/users").json()["items"]}
    assert users[bob]["connections"] == 1
    r = studio.c.patch(f"{base}/{cid}", json={"token": "bob-new-key-9876"})
    assert r.status_code == 200 and connections.token(conn_of(cid)) == "bob-new-key-9876"
    r = studio.c.post(base, json={"name": "x", "kind": "srvllm", "base_url": "https://cloud.test", "token": "k",
                                  "sections": ["chat"]})
    assert r.status_code == 422 and code(r) == "srvllm"
    assert studio.c.get("/api/admin/users/9999/connections").status_code == 404
    assert studio.c.delete(f"/api/admin/users/9999/connections/{cid}").status_code == 404
    assert studio.c.delete(f"{base}/{cid}").status_code == 204
    assert studio.c.get(base).json() == {"items": []}
    signin(studio, "bob")
    for method, path in [("GET", base), ("POST", base)]:
        r = studio.c.request(method, path, json={"name": "x", "kind": "openai", "base_url": "https://cloud.test",
                                                 "token": "k", "sections": ["chat"]})
        assert r.status_code == 403 and code(r) == "admin", method


# ---- первый администратор ------------------------------------------------------

def test_create_admin_sets_up_the_owner_gateway(env, monkeypatch, capsys):
    monkeypatch.setattr(state, "CFG", None)                     # manage ставит CFG — тест вернёт прежний
    fernet = Fernet(os.environ["STUDIO_DATA_KEY"])
    for key in ["gateway-key-one", "gateway-key-two"]:
        stdin = f"STUDIO_ADMIN_LOGIN=owner\nSTUDIO_ADMIN_PASSWORD=very-long-pass\nGATEWAY_KEY_OWNER_IMAGES={key}\n"
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        assert manage.main(["create-admin"]) == 0
        conn = db.connect(env.db_path)
        rows = conn.execute("SELECT id, user_id, name, kind, base_url, sections, token_enc FROM connections").fetchall()
        user = conn.execute("SELECT id, default_connection_id, default_model FROM users").fetchone()
        conn.close()
        assert len(rows) == 1
        row = rows[0]
        assert (row["name"], row["kind"], row["base_url"]) == ("srvllm", "srvllm", "http://10.77.1.15:4000")
        assert sorted(json.loads(row["sections"])) == ["chat", "photo", "video"]
        assert fernet.decrypt(row["token_enc"]).decode() == key and row["user_id"] == user["id"]
        assert user["default_connection_id"] == row["id"] and user["default_model"] == "local-gemma-abl"
    out = capsys.readouterr()
    assert "gateway-key" not in out.out + out.err


def test_create_admin_without_gateway_key_adds_no_connection(env, monkeypatch):
    monkeypatch.setattr(state, "CFG", None)
    monkeypatch.setattr(sys, "stdin", io.StringIO("STUDIO_ADMIN_LOGIN=owner\nSTUDIO_ADMIN_PASSWORD=very-long-pass\n"))
    assert manage.main(["create-admin"]) == 0
    conn = db.connect(env.db_path)
    assert conn.execute("SELECT COUNT(*) FROM connections").fetchone()[0] == 0
    conn.close()
