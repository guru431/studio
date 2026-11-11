"""A local deployment profile must preserve code and reject unsafe SSH paths."""
import importlib.util
import io
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import zipfile

import pytest
import config
from test_deploy import _bash


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
    ("STUDIO_PROXY_IP", "10.77.1.10;allow all"),
    ("STUDIO_BIND_IP", "0.0.0.0\nunsafe"),
    ("STUDIO_PROXY_IP", "::1"),
    ("STUDIO_BACKEND_URL", "http://10.77.1.15:8151/unsafe"),
    ("STUDIO_BACKEND_URL", "http://10.77.1.15:8151;unsafe"),
    ("STUDIO_BACKEND_URL", "http://10.77.1.14:8151"),
    ("STUDIO_PROXY_VHOST", "/srv/studio/../other.conf"),
    ("STUDIO_APACHE_PORT", "9443\nunsafe"),
])
def test_profile_rejects_values_unsafe_for_commands_or_configuration(name, value):
    with pytest.raises(ValueError):
        PROFILE.settings({name: value})


def test_docker_profile_preserves_trusted_proxy_and_storage_boundaries():
    source, destination = io.BytesIO(), io.BytesIO()
    root = Path(__file__).parents[1]
    names = ["deploy/docker/host.conf", "deploy/docker/nginx.conf",
             "deploy/docker/studio-docker.service", "deploy/docker/studio-backup.service"]
    with tarfile.open(fileobj=source, mode="w") as archive:
        for name in names:
            archive.add(root / name, arcname=name)
    source.seek(0)
    PROFILE.render(source, destination, {
        "STUDIO_PROXY_IP": "198.51.100.10", "STUDIO_BIND_IP": "198.51.100.15",
        "STUDIO_MEDIA_DIR": "/mnt/media/studio", "STUDIO_REMOTE_ROOT": "/srv/studio",
        "STUDIO_URL": "https://studio.example.net", "STUDIO_HAIRPIN_IP": "198.51.100.1",
    })
    destination.seek(0)
    with tarfile.open(fileobj=destination) as archive:
        proxy = archive.extractfile(names[1]).read().decode()
        assert "allow 198.51.100.10;" in proxy and "deny all;" in proxy
        assert "proxy_set_header X-Forwarded-For $http_x_forwarded_for;" in proxy
        host = archive.extractfile(names[0]).read().decode()
        assert "STUDIO_BIND_IP=198.51.100.15" in host
        assert "STUDIO_HAIRPIN_IP=198.51.100.1" in host
        assert "STUDIO_MEDIA_DIR=/mnt/media/studio" in host
        unit = archive.extractfile(names[2]).read().decode()
        assert "RequiresMountsFor=/mnt/media/studio /var/lib/studio" in unit
        assert "WorkingDirectory=/srv/studio" in unit


def test_remote_backend_changes_only_the_proxy_destination_and_retains_route_options():
    source, destination = io.BytesIO(), io.BytesIO()
    root = Path(__file__).parents[1]
    with tarfile.open(fileobj=source, mode="w") as archive:
        for name in ("deploy/apache-vhost.conf", "deploy/docker/host.conf"):
            archive.add(root / name, arcname=name)
    source.seek(0)
    PROFILE.render(source, destination, {"STUDIO_BACKEND_URL": "http://10.77.1.15:9123"})
    destination.seek(0)
    with tarfile.open(fileobj=destination) as archive:
        proxy = archive.extractfile("deploy/apache-vhost.conf").read().decode()
        assert "unix:/run/studio/web.sock" not in proxy
        assert "http://10.77.1.15:9123/api/agent/link upgrade=websocket timeout=300" in proxy
        assert "http://10.77.1.15:9123/api/code/ timeout=600 flushpackets=on" in proxy
        assert "http://10.77.1.15:9123/api/videos timeout=330" in proxy
        assert "LimitRequestBody 220200960" in proxy
        assert "STUDIO_BACKEND_PORT=9123" in archive.extractfile("deploy/docker/host.conf").read().decode()


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


@pytest.mark.integration
@pytest.mark.skipif(os.name != 'nt', reason='Git Bash native environment conversion is Windows-specific')
def test_git_bash_preserves_remote_profile_paths_for_native_python(tmp_path):
    bash = _bash()
    if not bash:
        pytest.skip('Git Bash is unavailable')
    root = Path(__file__).parents[1]
    profile = tmp_path / 'local-profile.sh'
    profile.write_text('STUDIO_REMOTE_ROOT=/srv/studio\nSTUDIO_MEDIA_DIR=/mnt/media/studio\n'
                       'STUDIO_CERT_DIR=/etc/certs/studio\n', encoding='utf-8', newline='\n')
    probe = tmp_path / 'check.py'
    probe.write_text('import os,runpy,sys\n'
                     'assert os.environ["STUDIO_TEST_PATH"] == "/srv/keep"\n'
                     'sys.argv = [sys.argv[1], "--check"]\n'
                     'runpy.run_path(sys.argv[0], run_name="__main__")\n', encoding='utf-8')
    script = root / 'deploy/deploy.sh'
    prefix = script.read_text(encoding='utf-8').split('HOST=${STUDIO_SSH', 1)[0]
    command = prefix + '\nexec ' + shlex.join([Path(sys.executable).as_posix(), probe.as_posix(),
                                              (root/'deploy/render-profile.py').as_posix()])
    env = {key: value for key, value in os.environ.items()
           if key not in ('STUDIO_REMOTE_ROOT', 'STUDIO_MEDIA_DIR', 'STUDIO_CERT_DIR')}
    env.update(STUDIO_DEPLOY_PROFILE=profile.as_posix(), STUDIO_TEST_PATH='/srv/keep',
               MSYS2_ENV_CONV_EXCL='STUDIO_TEST_PATH')
    result = subprocess.run([bash, '-c', command, script.as_posix()], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
