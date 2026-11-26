"""Render deployment templates from a local environment without changing Git."""
import ipaddress
import io
import json
import os
from pathlib import PurePosixPath
import re
import sys
import tarfile
from urllib.parse import SplitResult, urlsplit


TEMPLATES = {"deploy/install.sh", "deploy/studio-web.service",
             "deploy/studio-backup.service", "deploy/apache-vhost.conf",
             "deploy/docker/host.conf", "deploy/docker/nginx.conf",
             "deploy/docker/studio-docker.service", "deploy/docker/studio-backup.service"}
HTTPS_POLICY = "deploy/managed-https/policy.json"


def https_origin(value: str, name: str) -> SplitResult:
    parsed = urlsplit(value)
    base = f"https://{parsed.netloc}"
    if (value not in {base, base + "/"} or not parsed.hostname
            or not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?", parsed.netloc)
            or parsed.port == 0):
        raise ValueError(f"Invalid {name}")
    return parsed


def managed_https(environ):
    enabled = environ.get("STUDIO_MANAGED_HTTPS", "0")
    if enabled not in {"0", "1"}:
        raise ValueError("Invalid STUDIO_MANAGED_HTTPS")
    if enabled == "0":
        return None
    proxy = environ.get("STUDIO_PROXY_SSH")
    port = int(environ.get("STUDIO_APACHE_PORT", "8443"))
    primary = https_origin(environ.get("STUDIO_URL", ""), "STUDIO_URL")
    ip = str(ipaddress.IPv4Address(primary.hostname))
    if (environ.get("STUDIO_RUNTIME") != "docker" or not proxy or proxy != environ.get("STUDIO_SSH")
            or environ.get("STUDIO_PROXY_SSH_PORT", environ.get("STUDIO_SSH_PORT")) != environ.get("STUDIO_SSH_PORT")
            or (primary.port or 443) != port):
        raise ValueError("Managed HTTPS requires a local Apache and an IP-based STUDIO_URL on its HTTPS port")
    hosts = [ip]
    if alternate := environ.get("STUDIO_ALTERNATE_ORIGIN"):
        hosts.append(https_origin(alternate, "STUDIO_ALTERNATE_ORIGIN").hostname)
    return {"probe_ip": ip, "port": port, "existing_hosts": hosts}


def settings(environ):
    replacements = []
    media_fstype = environ.get("STUDIO_MEDIA_FSTYPE", "cifs")
    if media_fstype not in {"cifs", "ext4", "xfs", "btrfs"}:
        raise ValueError("Invalid STUDIO_MEDIA_FSTYPE")
    if media_fstype != "cifs" and environ.get("STUDIO_RUNTIME") != "docker":
        raise ValueError("Local media requires Docker")
    replacements.append(("STUDIO_MEDIA_FSTYPE=cifs", "STUDIO_MEDIA_FSTYPE=" + media_fstype))
    build_network = environ.get("STUDIO_BUILD_NETWORK", "default")
    if build_network not in {"default", "host"}:
        raise ValueError("Invalid STUDIO_BUILD_NETWORK")
    replacements.append(("STUDIO_BUILD_NETWORK=default", "STUDIO_BUILD_NETWORK=" + build_network))
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
    parsed = https_origin(origin, "STUDIO_URL")
    replacements.append(("https://studio.example.com", origin.rstrip("/")))
    replacements.append(("studio.example.com", parsed.hostname))
    alternate = environ.get("STUDIO_ALTERNATE_ORIGIN")
    if alternate:
        secondary = https_origin(alternate, "STUDIO_ALTERNATE_ORIGIN")
        if (environ.get("STUDIO_RUNTIME") != "docker"
                or (secondary.hostname, secondary.port or 443) == (parsed.hostname, parsed.port or 443)):
            raise ValueError("STUDIO_ALTERNATE_ORIGIN needs Docker and a different HTTPS origin")
        port = f":{secondary.port}" if secondary.port not in {None, 443} else ""
        replacements.append(("STUDIO_ALTERNATE_ORIGIN=", f"STUDIO_ALTERNATE_ORIGIN=https://{secondary.hostname}{port}"))
        replacements.append(("# Preserve Host when a second origin is configured.",
                             "ServerAlias " + secondary.hostname + "\n    ProxyPreserveHost On"))
    spark_chat = environ.get("STUDIO_SPARK_CHAT_URL", "").removesuffix("/")
    if spark_chat:
        url_match = re.fullmatch(r"https?://([0-9.]+|\[[0-9a-fA-F:]+\])(?::([0-9]{1,5}))?/v1", spark_chat)
        if url_match is None:
            raise ValueError("Invalid STUDIO_SPARK_CHAT_URL: use an internal IP and the /v1 path")
        ip, port = url_match.groups()
        if ipaddress.ip_address(ip.strip("[]")).is_global or (port is not None and not 1 <= int(port) <= 65535):
            raise ValueError("Invalid STUDIO_SPARK_CHAT_URL: use an internal IP and a valid port")
        replacements.append(("STUDIO_SPARK_CHAT_URL=", "STUDIO_SPARK_CHAT_URL=" + spark_chat))
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
    return replacements, {"STUDIO_HAIRPIN_IP": hairpin, "STUDIO_SPARK_CHAT_URL": spark_chat}


def substitute(text, replacements):
    """One pass: a substituted value is never matched again by another example.

    The longest example wins at a position; examples that begin or end with a digit match only whole
    addresses and ports."""
    values = dict(replacements)
    pattern = "|".join(
        ("(?<![0-9.])" if old[0].isdigit() else "") + re.escape(old) + ("(?![0-9])" if old[-1].isdigit() else "")
        for old in sorted(values, key=len, reverse=True))
    return re.sub(pattern, lambda match: values[match[0]], text)


def add_managed_vhost(base):
    # Copy Studio's proxy rules so transport, body limits and streaming stay in sync.
    vhost = base[base.index("<VirtualHost "):]
    vhost = re.sub(r"(?m)^    (?:ServerAlias|ProxyPreserveHost|SSLCertificateChainFile) .+\n", "", vhost)
    vhost = re.sub(r"(?m)^    ServerName .+$", "    ServerName ${STUDIO_HTTPS_DOMAIN}\n    ProxyPreserveHost On", vhost)
    vhost = re.sub(r"(?m)^    SSLCertificateFile .+$", "    SSLCertificateFile ${STUDIO_HTTPS_SLOT}/fullchain.pem", vhost)
    vhost = re.sub(r"(?m)^    SSLCertificateKeyFile .+$", "    SSLCertificateKeyFile ${STUDIO_HTTPS_SLOT}/key.pem", vhost)
    return (base + "\nIncludeOptional /etc/studio/managed-https/site.conf\n"
            "<IfDefine STUDIO_HTTPS_DOMAIN>\n" + vhost + "</IfDefine>\n")


def render(source, destination, environ):
    replacements, service_env = settings(environ)
    https_policy = managed_https(environ)
    if https_policy:
        replacements.append(("STUDIO_MANAGED_HTTPS=0", "STUDIO_MANAGED_HTTPS=1"))
    environment = "".join(f"\nEnvironment={name}={value}" for name, value in service_env.items() if value)
    with tarfile.open(fileobj=source, mode="r|") as incoming:
        with tarfile.open(fileobj=destination, mode="w|") as outgoing:
            for member in incoming:
                if member.isfile():
                    with incoming.extractfile(member) as contents:
                        body = contents.read()
                    if member.name in TEMPLATES:
                        text = substitute(body.decode("utf-8"), replacements)
                        if environment and member.name.endswith(".service"):
                            text = re.sub(r"(?m)^(Environment=STUDIO_ORIGIN=.*)$",
                                          lambda m: m[0] + environment, text)
                        if https_policy and member.name == "deploy/apache-vhost.conf":
                            text = add_managed_vhost(text)
                        body = text.encode("utf-8")
                    elif https_policy and member.name == HTTPS_POLICY:
                        body = (json.dumps(https_policy) + "\n").encode("ascii")
                    member.size = len(body)
                    outgoing.addfile(member, io.BytesIO(body))
                else:
                    outgoing.addfile(member)


if __name__ == "__main__":
    try:
        if sys.argv[1:] == ["--check"]:
            settings(os.environ)
            managed_https(os.environ)
        elif not sys.argv[1:]:
            render(sys.stdin.buffer, sys.stdout.buffer, os.environ)
        else:
            raise ValueError("Usage: render-profile.py [--check]")
    except (ValueError, tarfile.TarError) as error:
        raise SystemExit(type(error).__name__ + ": invalid deployment profile or archive") from None
