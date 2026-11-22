"""Real Studio → real Cleaner HTTP contract, with a synthetic provider and no external traffic."""
import json
import os
import socket
import ssl
import subprocess
import time
from pathlib import Path
from dataclasses import replace

import httpx
import pytest

import cleaner_client
import sections
import state
from conftest import add_connection, make_user, signin
from test_chat import messages, new_chat, sse, turn
from test_chat_web import EXA_KEY
from test_mcp import TOKEN as MCP_KEY, URL as MCP_URL

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]


@pytest.fixture
def cleaner_server(tmp_path, request):
    """Настоящий Cleaner на петле. Параметр: False — http, True — https своим сертификатом, "client" — http и ключ
    `client` с заглушкой провайдера (`--client`: строки `CLIENT_KEY=` и `CLIENT_UPSTREAM=` — в `cleaner.log`)."""
    root = Path(os.environ.get("CLEANER_TEST_REPO", Path(__file__).resolve().parents[2] / "cleaner"))
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    script = root / "tests/studio_server.py"
    if not python.is_file() or not script.is_file():
        pytest.skip("нужны соседний cleaner и его .venv (CLEANER_TEST_REPO)")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    mode = getattr(request, "param", False)
    tls = mode is True
    url = f'{"https" if tls else "http"}://127.0.0.1:{port}'
    extra, ca, verify = [], None, True
    if tls:
        import ipaddress
        from datetime import datetime, timedelta, timezone
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Cleaner fixture")])
        # Certificate validity is test data, not a decision based on the real date.
        beginning = datetime(2020, 1, 1, tzinfo=timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(1).not_valid_before(beginning).not_valid_after(beginning + timedelta(days=20000))
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), True).sign(key, hashes.SHA256()))
        ca, private = tmp_path / "cleaner-cert.pem", tmp_path / "cleaner-key.pem"
        ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        extra = [str(ca), str(private)]
        verify = ssl.create_default_context(cafile=ca)
    log = (tmp_path / "cleaner.log").open("wb")
    if mode == "client":
        extra = ["--client"]
    process = subprocess.Popen([str(python), str(script), str(tmp_path), str(port), *extra], cwd=root,
                               stdout=log, stderr=log)
    real = httpx.Client(trust_env=False, timeout=10, verify=verify)
    try:
        for _ in range(200):
            if process.poll() is not None:
                raise AssertionError((tmp_path / "cleaner.log").read_text(encoding="utf-8"))
            try:
                if real.get(url + "/login").status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            time.sleep(0.05)
        else:
            raise AssertionError("Cleaner fixture did not start")
        yield url, real, ca
    finally:
        real.close()
        process.terminate()
        process.wait(timeout=10)
        log.close()


@pytest.fixture
def cleaner_ca_client(studio, cleaner_server, monkeypatch):
    ca = cleaner_server[2]
    if ca is None:
        yield
        return
    monkeypatch.setattr(state, "CFG", replace(state.CFG, cleaner_ca=ca))
    native = state.make_cleaner_http()
    monkeypatch.setattr(state, "CLEANER_HTTP", native)
    yield
    studio.c.portal.call(native.aclose)


@pytest.mark.parametrize("cleaner_server", [False, True], indirect=True)
def test_real_cleaner_preview_send_restore_and_delete(studio, cleaner_server, cleaner_ca_client, monkeypatch):
    url, real, _ = cleaner_server
    port = httpx.URL(url).port
    original = studio.gw.handler

    def forward(request):
        if request.url.port != port:
            return original(request)
        response = real.request(request.method, str(request.url), headers=request.headers, content=request.content)
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    monkeypatch.setattr(state.HTTP._transport, "handler", forward)
    make_user(studio, "owner", is_admin=True)
    signin(studio, "owner")
    state.DB.execute("UPDATE settings SET value = ? WHERE key = 'allowlist'", (f'["127.0.0.1:{port}"]',))
    conn = studio.c.post("/api/connections", json={"name": "Cleaner", "kind": "openai", "cleaner": True,
        "base_url": url, "token": "ck_" + "A" * 43, "sections": ["chat"]})
    assert conn.status_code == 201, conn.text
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]
    sections.set_models(uid, "chat", conn.json()["id"], ["protected-model"])
    cid = new_chat(studio)
    text = "Почта ivan@contoso.ru"
    shown = studio.c.post(f"/api/chats/{cid}/cleaner-preview", json={"text": text})
    assert shown.status_code == 200, shown.text
    assert "ivan@contoso.ru" not in shown.text and shown.json()["counts"]["EMAIL"] == 1
    result = sse(turn(studio, cid, text, cleaner_ticket=shown.json()["ticket"]))
    assert result[-1] == ("done", {"status": "done"}), result
    answer = messages(studio, cid)[-1]
    assert answer["content"].startswith("Ответ ivan@contoso.ru")
    assert "https://external.example/?v=ivan@contoso.ru" not in answer["content"]
    assert "<EMAIL_1>" in answer["cleaner_raw"]
    assert studio.c.delete(f"/api/chats/{cid}").status_code == 204
    assert studio.c.request("DELETE", f"/api/trash/chats/{cid}", json={"password": "password-123"}).status_code == 204   # контекст — при окончательном удалении
    studio.c.portal.call(cleaner_client.purge)
    assert state.DB.execute("SELECT COUNT(*) FROM cleaner_deletions").fetchone()[0] == 0
    assert real.get(url + "/api/v1/chats", headers={"X-Cleaner-Key": "ck_" + "A" * 43}).json() == []


CANARY = "ivan@contoso.ru"         # заглушка провайдера Cleaner отвечает 500, если увидит её неочищенной


def echo_mcp(studio) -> list:
    """Общий MCP с одним инструментом `q`: возвращает свой аргумент; аргументы вызовов — в списке."""
    calls = []

    async def handler(request):
        payload = json.loads(request.content) if request.method == "POST" else {}
        method, params = payload.get("method"), payload.get("params", {})
        if request.method == "DELETE" or method == "notifications/initialized":
            return httpx.Response(204 if request.method == "DELETE" else 202)
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "echo", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "find", "description": "Найти запись",
                                 "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}}]}
        else:
            calls.append(params.get("arguments"))
            result = {"content": [{"type": "text", "text": "Найдено: " + params["arguments"]["q"]}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    studio.gw.on["/mcp"] = handler
    return calls


@pytest.mark.parametrize("cleaner_server", ["client"], indirect=True)
def test_real_cleaner_client_key_turn_tools_web_sent_and_delete(studio, cleaner_server, tmp_path, monkeypatch):
    """Подключение «Через Cleaner» с настоящим Cleaner (ключ `client`) и его заглушкой провайдера: модель видит
    замены, MCP-инструмент получает оригинал, его результат уходит провайдеру очищенным; поиск «Интернета» — с
    заменами; «Что получила модель» — запрос с заменами; удаление диалога удаляет контекст в Cleaner."""
    url, real, _ = cleaner_server
    printed = dict(line.split("=", 1) for line in (tmp_path / "cleaner.log").read_text(encoding="utf-8").split()
                   if line.startswith(("CLIENT_KEY=", "CLIENT_UPSTREAM=")))
    key, provider = printed["CLIENT_KEY"], printed["CLIENT_UPSTREAM"]
    port = httpx.URL(url).port
    original = studio.gw.handler

    def forward(request):
        if request.url.port != port:
            return original(request)
        response = real.request(request.method, str(request.url), headers=request.headers, content=request.content)
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    monkeypatch.setattr(state.HTTP._transport, "handler", forward)
    cid = add_connection(studio, base_url=provider, token="sk-provider-fixture", sections=["chat"],
                         chat_models=("stub-model",))
    state.DB.execute("UPDATE settings SET value = ? WHERE key = 'allowlist'",
                     (json.dumps(["10.77.1.15:4000", f"127.0.0.1:{port}"]),))
    saved = studio.c.put("/api/admin/cleaner", json={"base_url": url, "key": key, "forced": False})
    assert saved.status_code == 200, saved.text
    assert studio.c.post("/api/admin/cleaner/check").json()["ok"] is True
    assert studio.c.patch(f"/api/connections/{cid}", json={"via_cleaner": True}).json()["routed"] is True
    calls = echo_mcp(studio)
    sid = studio.c.post("/api/admin/shared/mcp", json={"name": "Echo", "url": MCP_URL, "token": MCP_KEY}).json()["id"]
    tool = f"mcp_{sid}_0"

    # MCP: заглушка вызывает инструмент, чьё имя есть в вопросе, с текстом вопроса в аргументе
    cid_chat = new_chat(studio, connection_id=cid, model="stub-model")
    assert sse(turn(studio, cid_chat, f"{tool}: почта {CANARY}"))[-1] == ("done", {"status": "done"})
    assert calls == [{"q": f"{tool}: почта {CANARY}"}]                      # инструмент — оригинал
    answer = messages(studio, cid_chat)[-1]
    assert answer["content"] == f"Инструмент вернул: Найдено: {tool}: почта {CANARY}", answer
    shown = studio.c.get(f"/api/chats/{cid_chat}/messages/{answer['id']}/cleaner-sent")
    assert shown.status_code == 200, shown.text
    body = shown.json()
    assert CANARY not in shown.text and body["counts"].get("EMAIL", 0) >= 1 and body["tools"] >= 1
    roles = [m["role"] for m in body["messages"]]
    assert "user" in roles and roles[-1] == "tool", roles                    # результат инструмента — с заменами
    label = next(m["content"] for m in body["messages"] if m["role"] == "user").rsplit("почта ", 1)[1]
    assert label and label != CANARY and label in body["messages"][-1]["content"]

    # «Интернет»: аргументы веб-инструмента Cleaner отдаёт с заменами — в Exa уходит метка, а не почта
    web_chat = new_chat(studio, connection_id=cid, model="stub-model")
    assert studio.c.put("/api/me/exa-key", json={"key": EXA_KEY}).status_code == 204
    assert studio.c.patch(f"/api/chats/{web_chat}", json={"web": True}).json()["web"] is True
    assert sse(turn(studio, web_chat, f"web_search почта {CANARY}"))[-1] == ("done", {"status": "done"})
    query = studio.gw.exa_requests[-1][1]["query"]
    assert CANARY not in query and query.startswith("web_search почта ") and query != f"web_search почта {CANARY}"

    # удаление диалога: контекст уходит из Cleaner (его «Что ушло» — 404 `chat`, а не журнал)
    remote, rid = state.DB.execute("SELECT cleaner_request FROM messages WHERE id = ?",
                                   (answer["id"],)).fetchone()[0].split("/")
    path = f"{url}/agent/openai/v1/cleaner/chats/{remote}/requests/{rid}"
    assert real.get(path, headers={"X-Cleaner-Key": key}).status_code == 200
    assert studio.c.delete(f"/api/chats/{cid_chat}").status_code == 204
    assert studio.c.request("DELETE", f"/api/trash/chats/{cid_chat}", json={"password": "password-123"}).status_code == 204
    studio.c.portal.call(cleaner_client.purge)
    assert state.DB.execute("SELECT COUNT(*) FROM cleaner_deletions").fetchone()[0] == 0
    gone = real.get(path, headers={"X-Cleaner-Key": key})
    assert gone.status_code == 404 and gone.json()["error"]["code"] == "chat", gone.text
