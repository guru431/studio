"""Real Studio → real Cleaner HTTP contract, with a synthetic provider and no external traffic."""
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
from conftest import make_user, signin
from test_chat import messages, new_chat, sse, turn

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]


@pytest.fixture
def cleaner_server(tmp_path, request):
    root = Path(os.environ.get("CLEANER_TEST_REPO", Path(__file__).resolve().parents[2] / "cleaner"))
    python = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    script = root / "tests/studio_server.py"
    if not python.is_file() or not script.is_file():
        pytest.skip("нужны соседний cleaner и его .venv (CLEANER_TEST_REPO)")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    tls = bool(getattr(request, "param", False))
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
    studio.c.portal.call(cleaner_client.purge)
    assert state.DB.execute("SELECT COUNT(*) FROM cleaner_deletions").fetchone()[0] == 0
    assert real.get(url + "/api/v1/chats", headers={"X-Cleaner-Key": "ck_" + "A" * 43}).json() == []
