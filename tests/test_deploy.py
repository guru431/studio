"""Выкладка на debian (deploy/): юнит, vhost Apache, копия базы и скрипты договорены друг с другом."""
import re
import shutil
import sqlite3
import subprocess

import pytest

import chat
import clock
import config
import db
import documents
import image_upload
import images
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
    assert "MEDIA=/mnt/storage/SharedData/studio" in INSTALL


def test_install_checks_the_share_by_a_real_write():
    """B2-M2: у cifs с noperm `test -w` верен при любых правах, а локальный каталог с тем же путём
    прошёл бы его без шары: путь — на cifs, и studio там правда создаёт и удаляет файл."""
    check = INSTALL[INSTALL.index('probe="$MEDIA/'):INSTALL.index("# Среда — до подмены кода")]
    assert 'findmnt -n -o FSTYPE -T "$MEDIA"' in check and "grep -qx cifs" in check
    assert 'sudo -u studio touch "$probe"' in check and 'sudo -u studio rm -f "$probe"' in check
    assert "test -w" not in uncommented(INSTALL)


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
    assert f"ProxyPass / unix:{SOCK}|http://studio-backend/ timeout=150" in v
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


def test_apache_route_outlasts_every_upstream_timeout_of_the_studio():
    """B2-M8: при равных пределах 502 Apache закрывал бы 504 службы словами «шлюз не ответил»."""
    timeout = int(re.search(r"^\s*ProxyPass / \S+ timeout=(\d+)", uncommented(VHOST), re.M).group(1))
    release = (len(images.MODELS) + 1) * images.SHORT_TIMEOUT_SEC         # «Освободить карту»: три release
    assert timeout > max(videos.VIDEO_TIMEOUT_SEC, chat.MODELS_TIMEOUT_SEC, release)


def test_backup_unit_is_bounded_and_never_owns_the_socket_directory():
    """B2-M4: копия, повисшая на CIFS, не держит юнит сутками (таймер бы его не перезапустил).
    B2-M10: RuntimeDirectory у oneshot удалял бы /run/studio с сокетом службы при каждой остановке."""
    assert directives(BACKUP, "TimeoutStartSec") == ["30min"]
    assert "RuntimeDirectory" not in uncommented(BACKUP)


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


def test_backup_failing_on_the_share_leaves_no_part_file_there(env, monkeypatch, capsys):
    """B2-M5: копия упала уже на шаре (os.replace, место) — временный .part оттуда убран."""
    conn = db.connect(env.db_path)
    db.migrate(conn)
    conn.close()
    monkeypatch.setattr(clock, "NOW", lambda: 1_792_454_400.0)   # 2026-10-20 00:00 UTC

    def refuse(src, dst):
        raise OSError("шара занята")

    monkeypatch.setattr(manage.os, "replace", refuse)
    assert manage.main(["backup"]) == 1
    assert "копия базы не сделана" in capsys.readouterr().err
    folder = env.media_dir / "backup"
    assert sorted(p.name for p in folder.iterdir()) == [] and list(env.db_path.parent.glob("*.part")) == []


def test_backup_retention_removes_stale_part_files(env, monkeypatch):
    """B2-M5: .part прежних неудачных суток (до исправления) уходит вместе со старыми копиями."""
    conn = db.connect(env.db_path)
    db.migrate(conn)
    conn.close()
    folder = env.media_dir / "backup"
    folder.mkdir()
    (folder / ".studio-20261012.db.part").write_bytes(b"half")
    monkeypatch.setattr(clock, "NOW", lambda: 1_792_454_400.0)
    assert manage.main(["backup"]) == 0
    assert sorted(p.name for p in folder.iterdir()) == ["studio-20261020.db"]


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


def _function(name: str) -> str:
    """Тело функции install.sh — для запуска в bash с подменёнными командами хоста."""
    return re.search(rf"^{name}\(\) \{{[^\n]*\n.*?^\}}\n", INSTALL, re.S | re.M).group(0)


def test_install_failure_branch_prints_the_journal_and_the_rollback_hint():
    """B2-M1: `systemctl status` упавшей службы отвечает 3 — под pipefail скрипт умирал на нём, и
    журнал (traceback) с подсказкой отката не печатались; подсказка забывала прежний юнит."""
    bash = _bash()
    if not bash:
        pytest.skip("нет bash")
    stubs = ("set -euo pipefail\n"
             "systemctl() { echo 'studio-web.service: activating (auto-restart)'; return 3; }\n"
             "journalctl() { echo 'Traceback: boom'; }\n"
             "R=/r; OLD=/r/.previous; UNIT=studio-web.service; ENV_FILE=/etc/studio/studio.env\n"
             "PARTS=(app deploy requirements.txt); code=000\n")
    r = subprocess.run([bash, "-c", stubs + _function("health_failed") + "health_failed\necho unreachable\n"],
                       capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert r.returncode == 1 and "unreachable" not in r.stdout, r
    assert "auto-restart" in r.stderr and "Traceback: boom" in r.stderr
    assert "install -m 0644 /r/deploy/studio-web.service /etc/systemd/system/studio-web.service" in r.stderr
    assert "systemctl daemon-reload" in r.stderr and "чинить вперёд" not in r.stderr


def test_install_restore_puts_the_previous_tree_back(tmp_path):
    """Откат после отказа configtest: новое дерево уходит, прежнее — на место; без прежнего (первая
    установка) — просто нет нового."""
    bash = _bash()
    if not bash:
        pytest.skip("нет bash")
    root, old = tmp_path / "r", tmp_path / "r" / ".previous"
    for part, text in (("app", "new"), ("deploy", "new")):
        (root / part).mkdir(parents=True)
        (root / part / "f").write_text(text)
    (root / "requirements.txt").write_text("new")
    (old / "app").mkdir(parents=True)
    (old / "app" / "f").write_text("old")
    (old / "requirements.txt").write_text("old")
    script = (f"set -euo pipefail\nR={root.as_posix()}; OLD={old.as_posix()}; PARTS=(app deploy requirements.txt)\n"
              + _function("restore") + "restore\n")
    r = subprocess.run([bash, "-c", script], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    assert (root / "app" / "f").read_text() == "old" and (root / "requirements.txt").read_text() == "old"
    assert not (root / "deploy").exists()


def test_install_checks_apache_before_the_restart_and_always_reloads_after_health():
    """B2-M3: vhost Apache включает прямо из выложенного дерева — сломанный положил бы все vhost
    хоста при следующем старте Apache. configtest — после подмены и до перезапуска, с откатом.
    N1: reload — после каждого ответа /health, а не по сравнению с прежним деревом: повтор
    выкладки после неудачной видел бы «без изменений», и Apache остался бы на старом vhost."""
    test = INSTALL.index("apache2ctl -t")
    assert INSTALL.index('mv "$NEW/$p" "$R/$p"') < test < INSTALL.index('systemctl restart "$UNIT"')
    failed = INSTALL[test:INSTALL.index("fi\n", test)]
    assert "restore" in failed and "exit 1" in failed
    assert "vhost_changed" not in INSTALL and "cmp -s" not in INSTALL
    reload = INSTALL.index("systemctl reload apache2")
    assert INSTALL.count("systemctl reload apache2") == 1
    assert INSTALL.index("|| health_failed") < reload < INSTALL.index('rm -rf "$OLD"\necho "OK')
    line = INSTALL[INSTALL.rindex("\n", 0, reload) + 1:INSTALL.index("\n", reload)]
    assert line == "systemctl reload apache2"           # без условия вокруг


def test_install_configtest_refusal_shows_the_output_and_blames_no_file():
    """N3: configtest падает и из-за чужого vhost хоста — сообщение не называет файл студии,
    а вывод apache2ctl показан целиком."""
    bash = _bash()
    if not bash:
        pytest.skip("нет bash")
    start = INSTALL.index("if ! apache_test=$(apache2ctl -t 2>&1); then")
    block = INSTALL[start:INSTALL.index("fi\n", start) + 3]
    stubs = ("set -euo pipefail\nUNIT=studio-web.service\nrestore() { echo restored; }\n"
             "apache2ctl() { for i in 1 2 3 4 5 6 7; do echo \"line $i\"; done;"
             " echo 'AH00526: Syntax error on line 3 of /etc/apache2/sites-enabled/other.conf'; return 1; }\n")
    r = subprocess.run([bash, "-c", stubs + block + "echo unreachable\n"],
                       capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert r.returncode == 1 and "unreachable" not in r.stdout and "restored" in r.stdout, r
    assert "line 1" in r.stderr and "other.conf" in r.stderr     # вывод целиком, не хвост
    assert "Apache не проходит configtest (вывод выше" in r.stderr
    assert "apache-vhost.conf" not in r.stderr
