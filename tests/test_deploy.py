"""Выкладка на debian (deploy/): юнит, vhost Apache, копия базы и скрипты договорены друг с другом."""
import re
import shutil
import sqlite3
import subprocess

import pytest

import clock
import config
import db
import documents
import image_upload
import manage
import videos
from conftest import DEPLOY, ROOT

UNIT = (DEPLOY / "studio-web.service").read_text(encoding="utf-8")
VHOST = (DEPLOY / "apache-vhost.conf").read_text(encoding="utf-8")
BACKUP = (DEPLOY / "studio-backup.service").read_text(encoding="utf-8")
TIMER = (DEPLOY / "studio-backup.timer").read_text(encoding="utf-8")
INSTALL = (DEPLOY / "install.sh").read_text(encoding="utf-8")
DEPLOY_SH = (DEPLOY / "deploy.sh").read_text(encoding="utf-8")
SOCK = "/run/studio/web.sock"
EXEC_START = ("ExecStart=/opt/services/studio/.venv/bin/uvicorn main:app --app-dir /opt/services/studio/app "
              f"--uds {SOCK} --no-access-log --no-proxy-headers")


def directives(text: str, name: str) -> list[str]:
    return re.findall(rf"^{name}=(.*)$", text, re.M)


def uncommented(text: str) -> str:
    return re.sub(r"^\s*#.*$", "", text, flags=re.M)


def test_service_listens_only_on_the_socket_apache_proxies_to():
    """Порта TCP нет: X-Forwarded-For честен, пока путь к службе один — Apache этого хоста."""
    exec_start = directives(UNIT, "ExecStart")[0]
    assert f"--uds {SOCK}" in exec_start and "--port" not in exec_start and "--host" not in exec_start
    assert "--no-access-log" in exec_start          # промпты, пароли и ключи — не в журнал
    assert "RuntimeDirectory=studio" in UNIT and "RuntimeDirectoryMode=0750" in UNIT
    assert "Group=www-data" in UNIT and "User=studio" in UNIT and "UMask=0007" in UNIT
    assert f"unix:{SOCK}|http://studio-backend/" in VHOST
    assert f"--unix-socket \"$SOCK\"" in INSTALL and f"SOCK={SOCK}" in INSTALL
    assert "http://studio-backend/health" in INSTALL


def test_unit_runs_the_new_app_with_a_bounded_graceful_stop():
    """Открытый поток чата не превращает каждую выкладку в SIGKILL: uvicorn ждёт соединения не
    дольше своего предела, а он меньше TimeoutStopSec. Недописанное помечают recover() при старте."""
    exec_start = "ExecStart=" + directives(UNIT, "ExecStart")[0]
    assert exec_start.startswith(EXEC_START), exec_start
    graceful = int(re.search(r"--timeout-graceful-shutdown (\d+)", exec_start).group(1))
    stop = int(directives(UNIT, "TimeoutStopSec")[0])
    assert 0 < graceful < stop


def test_service_gets_every_variable_the_app_requires():
    env = dict(line.split("=", 1) for line in directives(UNIT, "Environment"))
    assert env == {"STUDIO_DB": "/var/lib/studio/studio.db", "STUDIO_MEDIA_DIR": "/mnt/storage/SharedData/studio",
                   "STUDIO_ORIGIN": "https://studio.example.com"}
    assert directives(UNIT, "EnvironmentFile") == ["/etc/studio/studio.env"]      # STUDIO_DATA_KEY
    assert set(config.REQUIRED) == set(env) | {"STUDIO_DATA_KEY"}
    assert "ENV_FILE=/etc/studio/studio.env" in INSTALL and "^STUDIO_DATA_KEY=" in INSTALL
    for text in (UNIT, INSTALL, DEPLOY_SH, BACKUP):
        assert "STUDIO_SESSION_SECRET" not in text and "STUDIO_GATEWAY_URL" not in text


def test_state_and_media_paths_are_writable_in_the_sandbox():
    """ProtectSystem=strict: база пишется только в каталог состояния, файлы — только на шару."""
    for unit in (UNIT, BACKUP):
        assert "ProtectSystem=strict" in unit and "ProtectHome=yes" in unit and "PrivateTmp=yes" in unit
        assert "NoNewPrivileges=yes" in unit and "LimitCORE=0" in unit
        assert directives(unit, "StateDirectory") == ["studio"]
        assert directives(unit, "StateDirectoryMode") == ["0700"]
        assert directives(unit, "ReadWritePaths") == ["-/mnt/storage/SharedData/studio"]
    assert "MemoryMax=2G" in UNIT
    assert "sudo -u studio test -w \"$MEDIA\"" in INSTALL and "MEDIA=/mnt/storage/SharedData/studio" in INSTALL


def test_vhost_is_public_and_streams_chat():
    v = (DEPLOY / "apache-vhost.conf").read_text(encoding="utf-8")
    assert "ServerName studio.example.com" in v and "Require ip" not in v
    assert v.index("ProxyPass /api/chats/") < v.index("ProxyPass / ")
    assert "flushpackets=on" in v and "studio-backend" in v and "localhost" not in re.sub(r"^#.*$", "", v, flags=re.M)


def test_vhost_details():
    v = uncommented(VHOST)
    assert re.findall(r"^\s*ServerName (\S+)", v, re.M) == ["studio.example.com"]
    assert "ServerAlias" not in v and "<VirtualHost *:8443>" in v
    for line in ("SSLCertificateFile /etc/letsencrypt/live/example.net/cert.pem",
                 "SSLCertificateKeyFile /etc/letsencrypt/live/example.net/privkey.pem",
                 "SSLCertificateChainFile /etc/letsencrypt/live/example.net/chain.pem"):
        assert re.search(r"^\s*" + re.escape(line).replace(r"\ ", r"\s+") + r"\s*$", v, re.M), line
    assert (f"ProxyPass /api/chats/ unix:{SOCK}|http://studio-backend/api/chats/ timeout=600 flushpackets=on"
            in v)
    assert f"ProxyPass / unix:{SOCK}|http://studio-backend/ timeout=120" in v
    assert f"ProxyPassReverse / unix:{SOCK}|http://studio-backend/" in v
    assert 'RequestHeader set X-Forwarded-Proto "https"' in v
    assert 'SetEnvIf Request_URI "^/api/chats/" no-gzip=1' in v
    assert 'Header always set X-Frame-Options "DENY"' in v
    assert 'Header always set Strict-Transport-Security "max-age=31536000"' in v


def test_apache_lets_through_the_largest_body_the_studio_accepts():
    """Предел тела у Apache — не меньше пределов службы: иначе картинка для видео (до 20 МБ),
    фото с маской или документ чата отбиваются 413 ещё в Apache (до 2026-10-19 там было 65536 —
    настоящая фотография в правку не проходила)."""
    limit = int(re.search(r"^\s*LimitRequestBody (\d+)", uncommented(VHOST), re.M).group(1))
    assert limit >= documents.BODY_MAX and documents.BODY_MAX > documents.DOC_MAX     # документ чата
    assert limit >= videos.BODY_MAX and videos.BODY_MAX > image_upload.VIDEO_UPLOAD   # картинка ролика
    assert limit >= image_upload.MAX_BODY + image_upload.MAX_UPLOAD                   # правка: фото и маска


def test_backup_timer_runs_as_the_service_user():
    """Копия базы — тем же пользователем и с тем же окружением, что у службы: другой Group у
    того же StateDirectory systemd перевёл бы владельца каталога базы."""
    for name in ("User", "Group", "EnvironmentFile", "Environment"):
        assert directives(BACKUP, name) == directives(UNIT, name), name
    assert directives(BACKUP, "Type") == ["oneshot"]
    assert directives(BACKUP, "ExecStart") == ["/opt/services/studio/.venv/bin/python /opt/services/studio/app/manage.py backup"]
    assert directives(TIMER, "OnCalendar") == ["*-*-* 04:30"] and directives(TIMER, "Persistent") == ["true"]
    assert "WantedBy=timers.target" in TIMER
    assert "systemctl enable --now studio-backup.timer" in INSTALL
    assert all(f"$R/deploy/{u}" in INSTALL for u in ("studio-backup.service", "studio-backup.timer"))


def test_secret_value_is_never_an_argument():
    """Ключ данных едет из vault потоком: в argv он виден в ps и попал бы в журнал sudo."""
    assert "grep -m1 '^STUDIO_DATA_KEY=' \"$VAULT\"" in DEPLOY_SH
    assert "args+=(--secret-stdin)" in DEPLOY_SH and 'install.sh ${args[*]:-}' in DEPLOY_SH
    assert "IFS= read -r line" in INSTALL
    assert re.search(r"\^STUDIO_DATA_KEY=\[A-Za-z0-9_-\]\{43\}=\$", INSTALL)        # ключ Fernet, 44 знака


def test_admin_secrets_travel_by_stdin_only():
    """Логин, пароль и ключ шлюза первого администратора — из vault потоком в manage.py, мимо argv."""
    assert "grep -E '^(STUDIO_ADMIN_LOGIN|STUDIO_ADMIN_PASSWORD|GATEWAY_KEY_OWNER_IMAGES)=' \"$VAULT\"" in DEPLOY_SH
    assert "--admin-stdin" in DEPLOY_SH and "--create-admin" in DEPLOY_SH
    for name in ("STUDIO_ADMIN_LOGIN", "STUDIO_ADMIN_PASSWORD", "GATEWAY_KEY_OWNER_IMAGES", "STUDIO_DATA_KEY"):
        for text in (DEPLOY_SH, INSTALL):
            assert f"${name}" not in text and f"${{{name}" not in text, name     # значение — не в переменной
    run = re.search(r"systemd-run (.*?)create-admin", INSTALL, re.S).group(1)
    assert "--pipe" in run and "--wait" in run and "-p User=studio" in run
    assert "-p EnvironmentFile=$ENV_FILE" in run and '"$R/.venv/bin/python" "$R/app/manage.py"' in run


def test_install_and_deploy_ship_the_same_parts():
    parts = {re.search(r"^PARTS=\((.*)\)$", text, re.M).group(1) for text in (INSTALL, DEPLOY_SH)}
    assert parts == {"app deploy requirements.txt"}
    assert "URL=${STUDIO_URL:-https://studio.example.com}" in DEPLOY_SH and '"$URL/health"' in DEPLOY_SH


def test_requirements_are_exact_pins():
    lines = [line for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
             if line and not line.startswith("#")]
    assert lines and all(re.fullmatch(r"[A-Za-z0-9._-]+(\[[a-z,]+\])?==\S+", line) for line in lines), lines


def test_deploy_files_have_lf_endings():
    """\r в юните systemd принял бы за часть пути, bash — за часть команды."""
    for path in DEPLOY.iterdir():
        assert b"\r\n" not in path.read_bytes(), path.name


def test_backup_is_a_consistent_copy_and_keeps_the_last_seven(env, monkeypatch, capsys):
    conn = db.connect(env.db_path)
    db.migrate(conn)
    conn.execute("INSERT INTO settings(key, value) VALUES ('marker', 'kept')")
    folder = env.media_dir / "backup"
    folder.mkdir()
    for day in range(1, 9):                                     # восемь старых копий и чужой файл
        (folder / f"studio-202609{day:02d}.db").write_bytes(b"old")
    (folder / "notes.txt").write_text("x")
    monkeypatch.setattr(clock, "NOW", lambda: 1_792_454_400.0)   # 2026-10-20 00:00 UTC
    assert manage.main(["backup"]) == 0
    copy = folder / "studio-20261020.db"
    with sqlite3.connect(copy) as c:
        assert c.execute("SELECT value FROM settings WHERE key = 'marker'").fetchone() == ("kept",)
        assert c.execute("PRAGMA user_version").fetchone()[0] == db.VERSION
    names = sorted(p.name for p in folder.iterdir())
    assert names == sorted([f"studio-202609{d:02d}.db" for d in range(3, 9)] + ["notes.txt", "studio-20261020.db"])
    assert list(env.db_path.parent.glob("*.part")) == [] and list(folder.glob("*.part")) == []
    assert "studio-20261020.db" in capsys.readouterr().out
    conn.close()


def test_backup_without_the_share_fails_cleanly(env, capsys):
    """Шара не смонтирована (каталога нет, ReadWritePaths с «-»): ошибка, база и диск не тронуты."""
    conn = db.connect(env.db_path)
    db.migrate(conn)
    conn.close()
    env.media_dir.rmdir()
    assert manage.main(["backup"]) == 1
    assert "копия базы не сделана" in capsys.readouterr().err
    assert not env.media_dir.exists() and list(env.db_path.parent.glob("*.part")) == []


def test_backup_refuses_without_a_database(env):
    assert manage.main(["backup"]) != 0
    assert not env.db_path.exists() and not (env.media_dir / "backup").exists()


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
