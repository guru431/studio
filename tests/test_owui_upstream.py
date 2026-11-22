"""`upstream` для учёток Open WebUI: явный `Authorization`, доверенный сертификат, сертификат рукопожатием,
цель WebSocket — те же правила списка и имени, что у REST."""
import asyncio
import datetime
import json
import ssl
from types import SimpleNamespace

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

import clock
import connections
import state
import upstream

SOCKET = "/ws/socket.io/?EIO=4&transport=websocket"


def allow(*entries):
    state.DB.execute("UPDATE settings SET value = ? WHERE key = 'allowlist'", (json.dumps(list(entries)),))


def conn(cert_pem=None):
    """Учётка Open WebUI — не строка `connections`: ключа подключения у неё нет."""
    return SimpleNamespace(id=7, cleaner=False, cert_pem=cert_pem)


def self_signed(name: str, strict: bool = True) -> tuple[str, str]:
    """Самоподписанный сертификат как у `openssl req -x509` по умолчанию (PEM сертификата, PEM ключа); `strict=False` —
    `keyCertSign` без `CA:TRUE`, как у иных генераторов: строгую проверку RFC 5280 (OpenSSL 3.5) он не проходит."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(datetime.datetime(2020, 1, 1)).not_valid_after(datetime.datetime(2099, 1, 1)))
    if strict:
        builder = (builder.add_extension(ski, critical=False)
                   .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ski), critical=False)
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True))
    else:
        builder = builder.add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                                      key_encipherment=False, data_encipherment=False,
                                                      key_agreement=False, key_cert_sign=True, crl_sign=False,
                                                      encipher_only=False, decipher_only=False), critical=True)
    cert = builder.sign(key, hashes.SHA256())
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
    return cert.public_bytes(serialization.Encoding.PEM).decode(), key_pem.decode()


@pytest.fixture
def public(monkeypatch):
    async def resolve(host, port): return ["93.184.216.34"]
    monkeypatch.setattr(upstream, "RESOLVE", resolve)


def test_explicit_auth_is_sent_and_connection_token_is_not_read(studio, monkeypatch):
    allow("10.77.1.20:8080")
    def no_token(c): raise AssertionError("connections.token зван")
    monkeypatch.setattr(connections, "token", no_token)
    seen = []
    studio.gw.reply = lambda q: (seen.append(q), httpx.Response(200, json={}))[1]
    asyncio.run(upstream.request(conn(), "GET", "http://10.77.1.20:8080/api/config", auth="Bearer s-token"))

    async def streamed():
        async with upstream.stream(conn(), "GET", "http://10.77.1.20:8080/api/x", auth="Bearer s-token") as r:
            return r.status_code
    assert asyncio.run(streamed()) == 200
    assert [q.headers["authorization"] for q in seen] == ["Bearer s-token", "Bearer s-token"]


def test_pinned_certificate_selects_its_own_client(studio, monkeypatch):
    allow("10.77.1.21:3000")
    pem, _ = self_signed("10.77.1.21")
    made, seen = [], []

    def fake_http(verify=True):
        made.append(verify)
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda q: (seen.append(q), httpx.Response(204))[1]))
    monkeypatch.setattr(state, "_http", fake_http)
    for _ in range(2):
        r = asyncio.run(upstream.request(conn(cert_pem=pem), "GET", "https://10.77.1.21:3000/api/config",
                                         auth="Bearer s-token"))
        assert r.status_code == 204
    assert len(seen) == 2 and studio.gw.requests == []          # мимо общего клиента
    assert len(made) == 1 and isinstance(made[0], ssl.SSLContext) and not made[0].check_hostname
    assert state.pinned_http(pem) is state.pinned_http(pem)


def test_evicted_pinned_client_closes_after_grace(studio, monkeypatch):
    """Ревью M6: вытесненный из `state.PINNED` клиент может быть в работе у запроса другой учётки — закрывается через
    `state.PINNED_GRACE_SEC`, а не сразу."""
    monkeypatch.setattr(state, "PINNED_MAX", 1)

    async def scenario():
        release, waited = asyncio.Event(), []

        async def sleep(seconds):
            waited.append(seconds)
            await release.wait()

        monkeypatch.setattr(clock, "SLEEP", sleep)
        first = state.pinned_http(self_signed("10.77.1.21")[0])
        state.pinned_http(self_signed("10.77.1.22")[0])
        await asyncio.sleep(0)
        assert waited == [state.PINNED_GRACE_SEC] and not first.is_closed
        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
        assert first.is_closed and not state.RETIRED
        await state.close_pinned()

    asyncio.run(scenario())


def test_ws_target_refuses_unlisted_and_private_names(studio, monkeypatch):
    allow("owui.example.com:443", "10.77.1.20:8080")
    for url in ["https://other.example.com" + SOCKET, "http://owui.example.com:443" + SOCKET,
                "http://10.77.1.20:8081" + SOCKET]:
        with pytest.raises(upstream.UpstreamRefused) as e:
            asyncio.run(upstream.ws_target(conn(), url))
        assert e.value.reason == "unlisted", url

    async def inside(host, port): return ["10.0.0.5"]
    monkeypatch.setattr(upstream, "RESOLVE", inside)
    with pytest.raises(upstream.UpstreamRefused) as e:
        asyncio.run(upstream.ws_target(conn(), "https://owui.example.com" + SOCKET))
    assert e.value.reason == "internal"


def test_ws_target_pins_public_name(studio, public):
    allow("owui.example.com:443", "10.77.1.20:8080")
    t = asyncio.run(upstream.ws_target(conn(), "https://owui.example.com" + SOCKET))
    assert (t.host, t.port, t.path, t.server_hostname, t.host_header) == (
        "93.184.216.34", 443, SOCKET, "owui.example.com", "owui.example.com")
    assert isinstance(t.ssl, ssl.SSLContext) and t.ssl.check_hostname        # системные корни и имя
    pem, _ = self_signed("owui.example.com")
    pinned = asyncio.run(upstream.ws_target(conn(cert_pem=pem), "https://owui.example.com" + SOCKET)).ssl
    assert not pinned.check_hostname and pinned.verify_mode == ssl.CERT_REQUIRED
    t = asyncio.run(upstream.ws_target(conn(), "http://10.77.1.20:8080" + SOCKET))
    assert (t.host, t.port, t.path, t.host_header) == ("10.77.1.20", 8080, SOCKET, "10.77.1.20:8080")
    assert t.ssl is None and t.server_hostname is None


@pytest.mark.parametrize("strict", [True, False])
def test_pinned_context_accepts_only_its_certificate(studio, tmp_path, strict):
    """Ревью M2: и сертификат, не проходящий строгую проверку RFC 5280, принимается, если он — ровно закреплённый."""
    pem, key = self_signed("10.77.1.21", strict)
    other, _ = self_signed("10.77.1.21", strict)
    (tmp_path / "c.pem").write_text(pem)
    (tmp_path / "k.pem").write_text(key)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")

    async def served(client):
        async def answer(reader, writer):
            writer.close()
        server = await asyncio.start_server(answer, "127.0.0.1", 0, ssl=server_ctx)
        port = server.sockets[0].getsockname()[1]
        try:
            return port, await client(port)
        finally:
            server.close()

    async def handshake(context, port):
        _, writer = await asyncio.open_connection("127.0.0.1", port, ssl=context, server_hostname="owui")
        writer.close()
        return True

    assert asyncio.run(served(lambda port: handshake(upstream.pinned_context(pem), port)))[1]
    with pytest.raises(ssl.SSLCertVerificationError):
        asyncio.run(served(lambda port: handshake(upstream.pinned_context(other), port)))

    async def fetched(port):
        allow(f"127.0.0.1:{port}")
        return await upstream.fetch_certificate(conn(), f"https://127.0.0.1:{port}/")
    der = asyncio.run(served(fetched))[1]
    assert der == x509.load_pem_x509_certificate(pem.encode()).public_bytes(serialization.Encoding.DER)
    allow()
    with pytest.raises(upstream.UpstreamRefused):
        asyncio.run(upstream.fetch_certificate(conn(), "https://127.0.0.1:1/"))
