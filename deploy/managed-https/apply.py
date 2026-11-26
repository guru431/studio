"""Socket-activated host service for Studio's additional HTTPS vhost.

The app can send certificate material and a DNS name. Only root owns the policy,
Apache configuration and key slots. This service never uses caller-supplied paths.
"""
import base64
import binascii
from datetime import datetime, timezone
import http.client
import ipaddress
import json
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
from tempfile import NamedTemporaryFile
import time

from cryptography import x509
from cryptography.exceptions import InvalidSignature, UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs7


ROOT = Path("/etc/studio/managed-https")
PUBLISHED = Path("/var/lib/studio-https/status.json")
LOCK = Path("/etc/studio/.proxy.lock")
LIMIT = 256 * 1024
LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
PEM = serialization.Encoding.PEM


class Rejected(ValueError):
    pass


def domain_name(value):
    try:
        name = value.strip().removesuffix(".").encode("idna").decode("ascii").lower()
    except (AttributeError, UnicodeError):
        raise Rejected("domain") from None
    labels = name.split(".")
    if (len(name) > 253 or len(labels) < 2 or labels[-1].isdigit()
            or not all(LABEL.fullmatch(label) for label in labels)):
        raise Rejected("domain")
    return name


def certificates(data):
    for load in (x509.load_pem_x509_certificates, pkcs7.load_pem_pkcs7_certificates,
                 pkcs7.load_der_pkcs7_certificates):
        try:
            if parsed := load(data):
                return parsed
        except (ValueError, UnsupportedAlgorithm):
            pass
    try:
        return [x509.load_der_x509_certificate(data)]
    except (ValueError, UnsupportedAlgorithm):
        raise Rejected("certificate") from None


def decode(encoded, error):
    try:
        return base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError, TypeError):
        raise Rejected(error) from None


def private_key(data):
    load = serialization.load_pem_private_key if data.startswith(b"-----BEGIN ") else serialization.load_der_private_key
    try:
        return load(data, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise Rejected("key") from None


def signature_matches(cert, issuer):
    if cert.issuer != issuer.subject:
        return False
    try:
        cert.verify_directly_issued_by(issuer)
        return True
    except (ValueError, TypeError, InvalidSignature, UnsupportedAlgorithm):
        return False


def validity(cert):
    # Ubuntu's cryptography 41 exposes naive UTC datetimes.
    if hasattr(cert, "not_valid_after_utc"):
        return cert.not_valid_before_utc, cert.not_valid_after_utc
    return (cert.not_valid_before.replace(tzinfo=timezone.utc),
            cert.not_valid_after.replace(tzinfo=timezone.utc))


def certificate_bundle(payload, domain, now):
    certs = certificates(decode(payload.get("certificate"), "certificate"))
    key = private_key(decode(payload.get("key"), "key"))
    key_public = key.public_key().public_bytes(PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    leaf = next((cert for cert in certs if cert.public_key().public_bytes(
        PEM, serialization.PublicFormat.SubjectPublicKeyInfo) == key_public), None)
    if leaf is None:
        raise Rejected("key")
    try:
        names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        names = []
    wildcard = "*." + domain.split(".", 1)[1]
    if not any(name.lower() in {domain, wildcard} for name in names):
        raise Rejected("hostname")
    if payload.get("chain"):
        certs.extend(certificates(decode(payload["chain"], "certificate")))
    remaining = list(dict.fromkeys(cert for cert in certs if cert != leaf))
    chain = [leaf]
    while not signature_matches(chain[-1], chain[-1]):
        issuer = next((cert for cert in remaining if signature_matches(chain[-1], cert)), None)
        if issuer is None:
            break
        try:
            if not issuer.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
                raise Rejected("chain")
        except x509.ExtensionNotFound:
            raise Rejected("chain") from None
        chain.append(issuer)
        remaining.remove(issuer)
    if remaining and len(chain) == 1:
        raise Rejected("chain")
    periods = [validity(cert) for cert in chain]
    if any(not start <= now < end for start, end in periods):
        raise Rejected("expired")
    return chain, key, min(end for _, end in periods)


def atomic_write(path, content, mode=0o600):
    temporary = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        temporary.chmod(mode)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def command(*args):
    subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   check=True, timeout=8)


def healthy(policy, domain, authority, chain):
    """Probe the installed route by SNI on Apache's configured local listener."""
    context = ssl.create_default_context()
    if len(chain) > 1 or signature_matches(chain[0], chain[0]):
        context.load_verify_locations(cadata=chain[-1].public_bytes(PEM).decode("ascii"))
        context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
    fingerprint = chain[0].fingerprint(hashes.SHA256())
    endpoint = (str(ipaddress.IPv4Address(policy["probe_ip"])), policy["port"])
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        try:
            with (socket.create_connection(endpoint, timeout=1) as tcp,
                  context.wrap_socket(tcp, server_hostname=domain) as tls):
                leaf = x509.load_der_x509_certificate(tls.getpeercert(binary_form=True))
                if leaf.fingerprint(hashes.SHA256()) == fingerprint:
                    tls.sendall(f"GET /health HTTP/1.1\r\nHost: {authority}\r\nConnection: close\r\n\r\n".encode("ascii"))
                    response = http.client.HTTPResponse(tls)
                    with response:
                        response.begin()
                        result = json.loads(response.read(1024)) if response.status == 200 else None
                        if isinstance(result, dict) and result.get("status") == "ok":
                            return True
        except (OSError, http.client.HTTPException, ValueError):
            pass
        time.sleep(0.2)
    return False


def activate(payload, policy):
    domain = domain_name(payload.get("domain"))
    if domain in policy["existing_hosts"]:
        raise Rejected("domain")
    chain, key, expires = certificate_bundle(payload, domain, datetime.now(timezone.utc))
    authority = domain if policy["port"] == 443 else f"{domain}:{policy['port']}"
    config = ROOT / "site.conf"
    previous = config.read_bytes() if config.exists() else b""
    slot = ROOT / ("slot-b" if b"/slot-a\n" in previous else "slot-a")
    slot.mkdir(mode=0o700, exist_ok=True)
    atomic_write(slot / "fullchain.pem", b"".join(cert.public_bytes(PEM) for cert in chain))
    atomic_write(slot / "key.pem", key.private_bytes(PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    atomic_write(config, f"Define STUDIO_HTTPS_DOMAIN {domain}\nDefine STUDIO_HTTPS_SLOT {slot}\n".encode("ascii"))
    tested = False
    try:
        command("/usr/sbin/apache2ctl", "-t")
        tested = True
        command("/usr/bin/systemctl", "reload", "apache2.service")
        if not healthy(policy, domain, authority, chain):
            raise Rejected("apply")
        public = {"domain": domain, "origin": "https://" + authority, "expires": expires.isoformat()}
        atomic_write(PUBLISHED, json.dumps(public).encode(), mode=0o644)
    except (OSError, ValueError, subprocess.SubprocessError):
        if previous:
            atomic_write(config, previous)
        else:
            config.unlink(missing_ok=True)
        if tested:
            command("/usr/bin/systemctl", "reload", "apache2.service")
        raise Rejected("apply") from None


def run(stream):
    import fcntl

    raw = stream.readline(LIMIT + 2)
    if len(raw) > LIMIT + 1 or not raw.endswith(b"\n"):
        raise Rejected("certificate")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise Rejected("certificate")
    with LOCK.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Rejected("busy") from None
        activate(payload, json.loads((ROOT / "policy.json").read_bytes()))
    return {"ok": True}


if __name__ == "__main__":
    try:
        result = run(sys.stdin.buffer)
    except Rejected as error:
        result = {"error": str(error)}
    except Exception as error:
        # Journal only the exception type, which cannot contain uploaded material.
        print(f"studio HTTPS: {type(error).__name__}", file=sys.stderr)
        result = {"error": "apply"}
    print(json.dumps(result), flush=True)
