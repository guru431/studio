"""Render deployment templates from a local environment without changing Git."""
import ipaddress
import io
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tarfile
import tempfile
from urllib.parse import urlsplit
import zipfile


TEMPLATES = {"deploy/install.sh", "deploy/studio-web.service",
             "deploy/studio-backup.service", "deploy/apache-vhost.conf",
             "deploy/docker/host.conf", "deploy/docker/nginx.conf",
             "deploy/docker/studio-docker.service", "deploy/docker/studio-backup.service"}


def settings(environ):
    replacements = []
    for name, default in [("STUDIO_REMOTE_ROOT", "/opt/services/studio"),
                          ("STUDIO_MEDIA_DIR", "/mnt/storage/SharedData/studio"),
                          ("STUDIO_CERT_DIR", "/etc/letsencrypt/live/example.net")]:
        value = environ.get(name, default)
        path = PurePosixPath(value)
        if (not re.fullmatch(r"/[A-Za-z0-9_./-]+", value)
                or ".." in path.parts or str(path) == "/"
                or (name == "STUDIO_REMOTE_ROOT" and len(path.parts) < 3)):
            raise ValueError(f"Invalid {name}")
        replacements.append((default, str(path)))
    origin = environ.get("STUDIO_URL", "https://studio.example.com")
    parsed = urlsplit(origin)
    parsed.port  # Validate the port before including the origin in configuration.
    if (any(character.isspace() for character in origin)
            or parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment
            or not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?", parsed.netloc)):
        raise ValueError("Invalid STUDIO_URL")
    replacements.append(("https://studio.example.com", origin.rstrip("/")))
    replacements.append(("studio.example.com", parsed.hostname))
    apache_port = environ.get("STUDIO_APACHE_PORT", "8443")
    if not apache_port.isascii() or not apache_port.isdecimal() or not 1 <= int(apache_port) <= 65535:
        raise ValueError("Invalid STUDIO_APACHE_PORT")
    replacements.append(("(8443)", "(" + apache_port + ")"))
    replacements.append((":8443>", ":" + apache_port + ">"))
    hairpin = environ.get("STUDIO_HAIRPIN_IP")
    if hairpin:
        ipaddress.ip_address(hairpin)
        replacements.append(("STUDIO_HAIRPIN_IP=10.77.1.1", "STUDIO_HAIRPIN_IP=" + hairpin))
    for name, default in [("STUDIO_PROXY_IP", "10.77.1.10"),
                          ("STUDIO_BIND_IP", "10.77.1.15")]:
        value = environ.get(name, default)
        if ipaddress.ip_address(value).version != 4:
            raise ValueError(f"Invalid {name}: use IPv4")
        replacements.append((default, value))
    backend = environ.get("STUDIO_BACKEND_URL")
    if backend:
        target = urlsplit(backend)
        if (target.scheme != "http" or not target.hostname or target.username or target.password
                or target.path or target.query or target.fragment or not target.port
                or ipaddress.ip_address(target.hostname).version != 4
                or backend != f"http://{target.hostname}:{target.port}"):
            raise ValueError("Invalid STUDIO_BACKEND_URL")
        replacements.append(("unix:/run/studio/web.sock|http://studio-backend", backend))
        if target.hostname != environ.get("STUDIO_BIND_IP", "10.77.1.15"):
            raise ValueError("STUDIO_BACKEND_URL must use STUDIO_BIND_IP")
        replacements.append(("STUDIO_BACKEND_PORT=8151", f"STUDIO_BACKEND_PORT={target.port}"))
    proxy_vhost = environ.get("STUDIO_PROXY_VHOST")
    if proxy_vhost and (not re.fullmatch(r"/[A-Za-z0-9_./-]+", proxy_vhost)
                        or ".." in PurePosixPath(proxy_vhost).parts
                        or len(PurePosixPath(proxy_vhost).parts) < 4):
        raise ValueError("Invalid STUDIO_PROXY_VHOST")
    return replacements, hairpin


def render(source, destination, environ):
    replacements, hairpin = settings(environ)
    with tarfile.open(fileobj=source, mode="r|") as incoming:
        with tarfile.open(fileobj=destination, mode="w|") as outgoing:
            for member in incoming:
                if member.isfile():
                    with incoming.extractfile(member) as contents:
                        body = contents.read()
                    if member.name in TEMPLATES:
                        text = body.decode("utf-8")
                        for old, new in replacements:
                            text = text.replace(old, new)
                        if hairpin and member.name.endswith(".service"):
                            text = re.sub(r"(?m)^(Environment=STUDIO_ORIGIN=.*)$",
                                          lambda m: m[0] + "\nEnvironment=STUDIO_HAIRPIN_IP=" + hairpin,
                                          text)
                        body = text.encode("utf-8")
                    member.size = len(body)
                    outgoing.addfile(member, io.BytesIO(body))
                else:
                    outgoing.addfile(member)


def render_agent(archive_path, environ):
    replacements, _ = settings(environ)
    origin = dict(replacements)["https://studio.example.com"]
    changed = False
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=archive_path.parent, suffix=".zip", delete=False) as temporary:
            pending = Path(temporary.name)
        with zipfile.ZipFile(archive_path) as incoming, zipfile.ZipFile(pending, "w") as outgoing:
            for entry in incoming.infolist():
                body = incoming.read(entry)
                if entry.filename == "studio-agent/studio_agent/store.py":
                    expected = b'"https://studio.example.com"'
                    if body.count(expected) != 1:
                        raise ValueError("Unexpected agent default server configuration")
                    body = body.replace(expected, ('"' + origin + '"').encode("ascii"))
                    changed = True
                outgoing.writestr(entry, body)
        if not changed:
            raise ValueError("Agent configuration is missing")
        pending.replace(archive_path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--check"]:
            settings(os.environ)
        elif len(sys.argv) == 3 and sys.argv[1] == "--agent":
            render_agent(Path(sys.argv[2]), os.environ)
        elif not sys.argv[1:]:
            render(sys.stdin.buffer, sys.stdout.buffer, os.environ)
        else:
            raise ValueError("Usage: render-profile.py [--check | --agent archive.zip]")
    except (ValueError, tarfile.TarError, zipfile.BadZipFile) as error:
        raise SystemExit(type(error).__name__ + ": invalid deployment profile or archive") from None
