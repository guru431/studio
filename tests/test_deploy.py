"""Выкладка на debian (deploy/): юнит, блок Apache и скрипты договорены друг с другом."""
import re
import shutil
import subprocess

import pytest

from conftest import APP, DEPLOY, ROOT

UNIT = (DEPLOY / "studio-web.service").read_text(encoding="utf-8")
APACHE = (DEPLOY / "apache-location.conf").read_text(encoding="utf-8")
INSTALL = (DEPLOY / "install.sh").read_text(encoding="utf-8")
DEPLOY_SH = (DEPLOY / "deploy.sh").read_text(encoding="utf-8")
SOCK = "/run/studio/web.sock"


def test_service_listens_only_on_the_socket_apache_proxies_to():
    """Порта TCP нет: X-Forwarded-For честен, пока путь к службе один — Apache этого хоста."""
    exec_start = re.search(r"^ExecStart=(.*)$", UNIT, re.M).group(1)
    assert f"--uds {SOCK}" in exec_start and "--port" not in exec_start and "--host" not in exec_start
    assert "--no-access-log" in exec_start          # промпты и ключи — не в журнал
    assert "RuntimeDirectory=studio" in UNIT and "RuntimeDirectoryMode=0750" in UNIT
    assert "Group=www-data" in UNIT and "User=studio" in UNIT
    assert f"unix:{SOCK}|http://studio-backend/studio/" in APACHE
    assert "localhost" not in re.sub(r"^#.*$", "", APACHE, flags=re.M)
    assert f"--unix-socket \"$SOCK\"" in INSTALL and f"SOCK={SOCK}" in INSTALL


def test_service_gets_every_variable_the_server_requires():
    server = (APP / "server.py").read_text(encoding="utf-8")
    required = re.search(r"^REQUIRED = \((.*)\)$", server, re.M).group(1)
    assert set(re.findall(r'"([A-Z_]+)"', required)) == {"STUDIO_SESSION_SECRET", "STUDIO_GATEWAY_URL"}
    assert "EnvironmentFile=/etc/studio/studio.env" in UNIT      # STUDIO_SESSION_SECRET
    assert re.search(r"^Environment=STUDIO_GATEWAY_URL=http://[0-9.]+:4000/slot$", UNIT, re.M)
    assert "ENV_FILE=/etc/studio/studio.env" in INSTALL


def test_apache_is_lan_and_tunnels_only():
    block = re.search(r"<Location /studio/>(.*?)</Location>", APACHE, re.S).group(1)
    assert "Require ip 10.77.1.0/24 example0/24 10.10.49.0/24 10.10.50.0/24" in block
    assert "Require not ip 10.77.1.1" in block and "Require all granted" not in APACHE


def test_install_and_deploy_ship_the_same_parts():
    parts = {re.search(r"^PARTS=\((.*)\)$", text, re.M).group(1) for text in (INSTALL, DEPLOY_SH)}
    assert parts == {"app deploy requirements.txt"}
    assert "ExecStart=/opt/services/studio/.venv/bin/uvicorn server:app --app-dir /opt/services/studio/app" in UNIT


def test_requirements_are_exact_pins():
    lines = [line for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
             if line and not line.startswith("#")]
    assert lines and all(re.fullmatch(r"[A-Za-z0-9._-]+(\[[a-z,]+\])?==\S+", line) for line in lines), lines


def test_secret_value_is_never_an_argument():
    """Секрет едет из vault потоком: в argv он виден в ps и попал бы в журнал sudo."""
    assert "grep -m1 '^STUDIO_SESSION_SECRET=' \"$VAULT\"" in DEPLOY_SH
    assert "install.sh --secret-stdin" in DEPLOY_SH and "IFS= read -r line" in INSTALL


def test_deploy_files_have_lf_endings():
    """\r в юните systemd принял бы за часть пути, bash — за часть команды."""
    for path in DEPLOY.iterdir():
        assert b"\r\n" not in path.read_bytes(), path.name


def _bash():
    for candidate in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash")):
        if candidate and shutil.which(candidate):
            return candidate
    return None


@pytest.mark.parametrize("script", ["install.sh", "deploy.sh"])
def test_scripts_parse(script):
    bash = _bash()
    if not bash:
        pytest.skip("нет bash")
    r = subprocess.run([bash, "-n", (DEPLOY / script).as_posix()], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
