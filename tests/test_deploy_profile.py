"""A local deployment profile must preserve code and reject unsafe SSH paths."""
import importlib.util
import io
from pathlib import Path
import tarfile
import zipfile

import pytest
import config


SPEC = importlib.util.spec_from_file_location(
    "deployment_profile", Path(__file__).parents[1] / "deploy/render-profile.py")
PROFILE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PROFILE)


def test_profile_renders_only_deployment_templates():
    source, destination = io.BytesIO(), io.BytesIO()
    files = {
        "deploy/studio-web.service": b"WorkingDirectory=/opt/services/studio\nEnvironment=STUDIO_ORIGIN=https://studio.example.com\n",
        "deploy/install.sh": b"MEDIA=/mnt/storage/SharedData/studio\nR=/opt/services/studio\n",
        "app/example.py": b'origin = "https://studio.example.com"\n',
        "deploy/apache-vhost.conf": b"<VirtualHost *:8443>\nSSLCertificateFile /etc/letsencrypt/live/example.net/cert.pem\nCustomLog /var/log/studio.example.com(8443)-access.log combined\n",
    }
    with tarfile.open(fileobj=source, mode="w") as archive:
        for name, body in files.items():
            member = tarfile.TarInfo(name)
            member.mode = 0o755 if name.endswith(".sh") else 0o644
            member.size = len(body)
            archive.addfile(member, io.BytesIO(body))
    source.seek(0)
    PROFILE.render(source, destination, {
        "STUDIO_REMOTE_ROOT": "/srv/studio", "STUDIO_MEDIA_DIR": "/mnt/media/studio",
        "STUDIO_URL": "https://studio.example.net", "STUDIO_HAIRPIN_IP": "198.51.100.1",
        "STUDIO_CERT_DIR": "/etc/certs/studio", "STUDIO_APACHE_PORT": "9443",
    })
    destination.seek(0)
    with tarfile.open(fileobj=destination) as archive:
        unit = archive.extractfile("deploy/studio-web.service").read()
        assert b"WorkingDirectory=/srv/studio" in unit
        assert b"STUDIO_ORIGIN=https://studio.example.net" in unit
        assert b"STUDIO_HAIRPIN_IP=198.51.100.1" in unit
        assert archive.extractfile("app/example.py").read() == files["app/example.py"]
        assert archive.getmember("deploy/install.sh").mode == 0o755
        apache = archive.extractfile("deploy/apache-vhost.conf").read()
        assert b"/etc/certs/studio/cert.pem" in apache
        assert b"studio.example.net(9443)-access.log" in apache
        assert b"<VirtualHost *:9443>" in apache


@pytest.mark.parametrize("name,value", [
    ("STUDIO_REMOTE_ROOT", "/"), ("STUDIO_REMOTE_ROOT", "/opt"),
    ("STUDIO_REMOTE_ROOT", "/srv/studio/../other"),
    ("STUDIO_REMOTE_ROOT", "/srv/studio;echo unsafe"),
    ("STUDIO_MEDIA_DIR", "relative"), ("STUDIO_URL", "https://studio.example.net/\nunsafe"),
    ("STUDIO_URL", "https://studio.\nexample.net"),
    ("STUDIO_HAIRPIN_IP", "invalid"),
    ("STUDIO_APACHE_PORT", "9443\nunsafe"),
])
def test_profile_rejects_values_unsafe_for_commands_or_configuration(name, value):
    with pytest.raises(ValueError):
        PROFILE.settings({name: value})


def test_hairpin_address_can_be_supplied_by_the_host(env, monkeypatch):
    monkeypatch.setenv("STUDIO_HAIRPIN_IP", "198.51.100.1")
    assert config.load().hairpin_ip == "198.51.100.1"


def test_agent_package_receives_the_installation_server_and_retains_other_files(tmp_path):
    archive_path = tmp_path / "agent.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("studio-agent/studio_agent/store.py", 'url = "https://studio.example.com"\n')
        archive.writestr("studio-agent/install.cmd", b"@echo off\r\n")
    PROFILE.render_agent(archive_path, {"STUDIO_URL": "https://studio.example.net"})
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.read("studio-agent/studio_agent/store.py") == b'url = "https://studio.example.net"\n'
        assert archive.read("studio-agent/install.cmd") == b"@echo off\r\n"
    assert list(tmp_path.iterdir()) == [archive_path]
