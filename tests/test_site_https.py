"""Certificate application and its effect on account links."""
import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
from subprocess import CalledProcessError

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID

import site_https
import state
from conftest import make_user, signin


SPEC = importlib.util.spec_from_file_location("https_host", Path(__file__).parents[1] / "deploy/managed-https/apply.py")
HOST = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(HOST)
NOW = datetime(2026, 11, 26, tzinfo=timezone.utc)
DOMAIN = "studio.example.net"


def issue(name, issuer=None, signer=None):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer.subject if issuer else subject)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(NOW - timedelta(days=1)).not_valid_after(NOW + timedelta(days=1)))
    builder = builder.add_extension(x509.BasicConstraints(ca=issuer is None, path_length=None), critical=True)
    if issuer:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
    return builder.sign(signer or key, hashes.SHA256()), key


def encoded(data):
    return base64.b64encode(data).decode("ascii")


@pytest.fixture
def files():
    ca, ca_key = issue("Root CA")
    leaf, key = issue(DOMAIN, ca, ca_key)
    bundle = pkcs7.serialize_certificates([ca, leaf], serialization.Encoding.DER)
    return {"domain": DOMAIN, "certificate": encoded(leaf.public_bytes(serialization.Encoding.DER)),
            "chain": encoded(bundle), "key": encoded(key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))}, leaf, ca


def test_der_certificate_accepts_pkcs7_chain_and_checks_hostname_and_expiry(files):
    payload, leaf, ca = files
    certs, _, expires = HOST.certificate_bundle(payload, DOMAIN, NOW)
    assert certs == [leaf, ca]
    assert expires == NOW + timedelta(days=1)
    with pytest.raises(HOST.Rejected, match="hostname"):
        HOST.certificate_bundle(payload, "other.example.net", NOW)
    with pytest.raises(HOST.Rejected, match="expired"):
        HOST.certificate_bundle(payload, DOMAIN, NOW + timedelta(days=2))
    with pytest.raises(HOST.Rejected, match="domain"):
        HOST.domain_name("example.net\nInclude /etc/hosts")


def test_apache_health_failure_restores_previous_vhost_and_mail_origin(tmp_path, monkeypatch, files):
    payload, leaf, ca = files
    monkeypatch.setattr(HOST, "ROOT", tmp_path)
    published = tmp_path / "status.json"
    monkeypatch.setattr(HOST, "PUBLISHED", published)
    monkeypatch.setattr(HOST, "certificate_bundle", lambda body, domain, now: ([leaf, ca],
        HOST.private_key(HOST.decode(body["key"], "key")), NOW + timedelta(days=1)))
    commands = []
    monkeypatch.setattr(HOST, "command", lambda *args: commands.append(args))
    monkeypatch.setattr(HOST, "healthy", lambda *args: True)
    policy = {"probe_ip": "192.0.2.14", "port": 8443, "existing_hosts": ["192.0.2.14"]}
    HOST.activate(payload, policy)
    assert json.loads(published.read_bytes())["origin"] == f"https://{DOMAIN}:8443"
    config = tmp_path / "site.conf"
    previous_config, previous_status = config.read_bytes(), published.read_bytes()

    monkeypatch.setattr(HOST, "healthy", lambda *args: False)
    with pytest.raises(HOST.Rejected, match="apply"):
        HOST.activate({**payload, "domain": "replacement.example.net"}, policy)
    assert config.read_bytes() == previous_config and published.read_bytes() == previous_status
    assert [args[1] for args in commands] == ["-t", "reload", "-t", "reload", "reload"]

    def invalid_config(*args):
        assert args[1] == "-t"
        raise CalledProcessError(1, args)

    monkeypatch.setattr(HOST, "command", invalid_config)
    with pytest.raises(HOST.Rejected, match="apply"):
        HOST.activate(payload, policy)
    assert config.read_bytes() == previous_config and published.read_bytes() == previous_status


def test_upload_requires_admin_csrf_password_and_origin(studio, monkeypatch):
    monkeypatch.setattr(state, "CFG", replace(state.CFG, managed_https=True))
    submitted = []

    async def apply(payload):
        submitted.append(payload)
        return {"ok": True}

    monkeypatch.setattr(site_https, "_apply", apply)
    path = "/api/admin/https"
    body = {"domain": DOMAIN, "certificate": "YQ==", "key": "Yg==", "password": "password-123"}
    assert studio.c.put(path, json=body).status_code == 401
    make_user(studio, "member")
    signin(studio, "member")
    assert studio.c.put(path, json=body).status_code == 403
    make_user(studio, "owner", is_admin=True)
    signin(studio, "owner")
    assert studio.c.put(path, json=body, headers={"x-studio-csrf": "wrong"}).status_code == 403
    assert studio.c.put(path, json=body, headers={"Origin": "https://other.example.net"}).status_code == 403
    assert studio.c.put(path, json={**body, "password": "wrong"}).status_code == 403
    assert studio.c.put(path, json={**body, "certificate": "A" * site_https.LIMIT}).status_code == 413
    assert not submitted
    response = studio.c.put(path, json=body)
    assert response.status_code == 200 and response.json() == {"port": 443}
    assert submitted == [{"domain": DOMAIN, "certificate": "YQ==", "key": "Yg==", "chain": ""}]
