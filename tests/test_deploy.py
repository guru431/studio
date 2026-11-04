"""Выкладка на debian (deploy/): юнит, vhost Apache, копия базы и скрипты договорены друг с другом."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import shlex
import sqlite3
import subprocess
import sys
import zipfile

import pytest

import bridge
import chat
import claude_code
import clock
import config
import db
import documents
import image_upload
import images
import manage
import media
import presets
import videos
from conftest import DEPLOY, ROOT

UNIT = (DEPLOY / "studio-web.service").read_text(encoding="utf-8")
VHOST = (DEPLOY / "apache-vhost.conf").read_text(encoding="utf-8")
BACKUP = (DEPLOY / "studio-backup.service").read_text(encoding="utf-8")
TIMER = (DEPLOY / "studio-backup.timer").read_text(encoding="utf-8")
INSTALL = (DEPLOY / "install.sh").read_text(encoding="utf-8")
DEPLOY_SH = (DEPLOY / "deploy.sh").read_text(encoding="utf-8")
SOCK = "/run/studio/web.sock"
EXEC_START = ("ExecStart=/opt/services/studio/.venv/bin/python -m uvicorn main:app --app-dir /opt/services/studio/app "
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
    check = INSTALL[INSTALL.index('probe="$MEDIA/'):INSTALL.index("# Среда Python")]
    assert 'findmnt -n -o FSTYPE -T "$MEDIA"' in check and "grep -qx cifs" in check
    assert 'sudo -u studio touch "$probe"' in check and 'sudo -u studio rm -f "$probe"' in check
    assert "test -w" not in uncommented(INSTALL)


def test_install_warns_without_yt_dlp():
    """yt-dlp общий на debian (root обновляет его ночью): нет — предупреждение, выкладка идёт дальше."""
    line = next(s for s in uncommented(INSTALL).splitlines() if "command -v yt-dlp" in s)
    assert "ВНИМАНИЕ: нет yt-dlp" in line and ">&2" in line
    assert "exit" not in line and "apt-get" not in line and "pip" not in line


def test_vhost_is_public_and_streams_chat():
    v = (DEPLOY / "apache-vhost.conf").read_text(encoding="utf-8")
    assert "ServerName studio.example.com" in v and "Require ip" not in v
    assert v.index("ProxyPass /api/chats/") < v.index("ProxyPass / ")
    assert v.index("ProxyPass /api/videos ") < v.index("ProxyPass / ")
    assert v.index("ProxyPass /api/agent/link ") < v.index("ProxyPass / ")
    assert v.index("ProxyPass /api/code/ ") < v.index("ProxyPass / ")
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
    assert (f"ProxyPass /api/agent/link unix:{SOCK}|http://studio-backend/api/agent/link upgrade=websocket"
            " timeout=300" in v)
    assert f"ProxyPassReverse /api/agent/link unix:{SOCK}|http://studio-backend/api/agent/link" in v
    assert f"ProxyPass /api/code/ unix:{SOCK}|http://studio-backend/api/code/ timeout=600 flushpackets=on" in v
    assert f"ProxyPassReverse /api/code/ unix:{SOCK}|http://studio-backend/api/code/" in v
    assert 'SetEnvIf Request_URI "^/api/code/" no-gzip=1' in v
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
    assert limit >= presets.BODY_MAX                                                  # пресет с длинным промптом
    assert limit >= claude_code.FILE_BODY_MAX                                         # правка файла Claude Code


def test_apache_route_outlasts_every_upstream_timeout_of_the_studio():
    """B2-M8: при равных пределах 502 Apache закрывал бы 504 службы словами «шлюз не ответил»."""
    timeout = int(re.search(r"^\s*ProxyPass / \S+ timeout=(\d+)", uncommented(VHOST), re.M).group(1))
    release = (len(images.MODELS) + 1) * images.SHORT_TIMEOUT_SEC         # «Освободить карту»: три release
    saves = 2 * media.SAVE_TIMEOUT_SEC                                    # правка: исходник и маска на шару
    # удаление учётки ждёт её заданий картинок
    assert timeout > max(chat.MODELS_TIMEOUT_SEC, release, images.SETTLE_SEC, saves)
    # создание ролика: исходник на шару, задание шлюзу, поиск потерянного ответа, снятие у шлюза
    # ролика, чьё фото удалили, пока шлюз его принимал, — подряд, у маршрута свой предел
    video = media.SAVE_TIMEOUT_SEC + videos.VIDEO_TIMEOUT_SEC + videos.ADOPT_TIMEOUT_SEC + videos.VIDEO_TIMEOUT_SEC
    assert _route_timeout("/api/videos") > video and _route_timeout("/api/videos/abc/cancel") > video


def _route_timeout(path: str) -> int:
    """Таймаут ProxyPass, которому достанется путь: mod_proxy берёт первое совпавшее правило."""
    for prefix, timeout in re.findall(r"^\s*ProxyPass (\S+) \S+ (?:\S+=\S+ )*timeout=(\d+)", uncommented(VHOST), re.M):
        if path.startswith(prefix):
            return int(timeout)
    raise AssertionError(f"{path}: нет ProxyPass")


def test_bridge_link_and_code_stream_routes():
    """Канал моста — апгрейд WebSocket с пределом 300 с (тишину рвут пинги службы каждые 20 с); поток хода
    Claude Code — как поток чата, дольше просьбы к мосту (спека claude-code 6.9)."""
    assert _route_timeout("/api/agent/link") == 300
    assert _route_timeout("/api/code/agents/1/projects/a/chats/b/turn") == 600 > bridge.CALL_SEC
    upgrades = [s.strip() for s in uncommented(VHOST).splitlines() if "upgrade=websocket" in s]
    assert len(upgrades) == 1 and upgrades[0].startswith("ProxyPass /api/agent/link ")


def test_apache_route_outlasts_the_whole_synchronous_speech_operation():
    """Голосовой ввод и образец голоса отвечают в том же запросе: вся операция службы (а не один
    вызов шлюза — у WhisperX там 2460 с) уложена в бюджет меньше таймаута маршрута, иначе Apache
    оборвал бы запрос раньше службы и текст диктовки пропал бы."""
    import dictation
    import voices
    assert dictation.BUDGET_SEC < _route_timeout("/api/dictation")
    assert voices.BUDGET_SEC < _route_timeout("/api/voices")
    assert _route_timeout("/api/chats/1/messages") == 600 and _route_timeout("/api/images") == 150


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
    assert "systemctl enable --now --quiet studio-backup.timer" in INSTALL
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


def test_unit_lets_a_frame_just_over_the_limit_reach_the_app():
    """Кадр больше FRAME_MAX — 4400 протокола, а не 1009 самого uvicorn: предел uvicorn чуть выше, и такой кадр
    доходит до `bridge._frame`. Пинги, которые протокол §2 называет, — явными флагами, а не умолчаниями."""
    exec_start = directives(UNIT, "ExecStart")[0]
    assert int(re.search(r"--ws-max-size (\d+)", exec_start).group(1)) == bridge.FRAME_MAX + 1024
    assert "--ws-ping-interval 20 --ws-ping-timeout 20" in exec_start


def test_deploy_builds_the_bridge_archive_from_the_studio_commit():
    """Мост и сервер собираются из одного HEAD; проверка всего компонента — до первого ssh."""
    code = uncommented(DEPLOY_SH)
    assert 'git -C "$D" -c core.autocrlf=false archive --format=zip --prefix=studio-agent/ HEAD:agent' in code
    assert "for file in studio-agent.zip studio-agent-setup.exe" in code
    assert "$R/.incoming/agent/$file" in code
    first_ssh = code.index('"${SSH[@]}"')
    assert code.index('status --porcelain -- "${PARTS[@]}" agent') < first_ssh
    assert code.index('cat-file -e HEAD:agent/install.cmd') < first_ssh
    archive = code.index("archive --format=zip")
    build = code.index('powershell.exe -NoProfile -NonInteractive')
    assert archive < build < first_ssh
    assert 'show HEAD:deploy/build-agent-installer.ps1' in code
    assert 'show HEAD:deploy/agent-installer.cs' in code


def test_install_swaps_the_bridge_archive_with_the_code():
    """`agent` переезжает вместе с кодом и откатывается с ним; без архива — отказ до подмены кода."""
    code = uncommented(INSTALL)
    assert 'SWAP=("${PARTS[@]}" agent)' in code
    check = code.index('[ -s "$NEW/agent/studio-agent.zip" ]')
    assert check < code.index('for p in "${SWAP[@]}"; do') and check < code.index('chown -R root:root "$NEW"')
    setup = code.index('[ -s "$NEW/agent/studio-agent-setup.exe" ]')
    assert setup < code.index('for p in "${SWAP[@]}"; do') and setup < code.index('chown -R root:root "$NEW"')


def test_requirements_are_exact_pins():
    lines = [line for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
             if line and not line.startswith("#")]
    assert lines and all(re.fullmatch(r"[A-Za-z0-9._-]+(\[[a-z,]+\])?==\S+", line) for line in lines), lines


def test_deploy_files_have_lf_endings():
    """\r в юните systemd принял бы за часть пути, bash — за часть команды."""
    for path in DEPLOY.iterdir():
        if not path.is_file():
            continue
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


def _install_environment(tmp_path, contents, replacement=None):
    bash = _bash()
    if not bash:
        pytest.skip("нет bash")
    path = tmp_path / "studio.env"
    path.write_text(contents, encoding="utf-8", newline="\n")
    block = INSTALL[INSTALL.index('if [ "$SECRET" = 1 ]; then'):INSTALL.index('# Файлы истории')]
    script = (f"set -euo pipefail\nENV_FILE={shlex.quote(path.as_posix())}\n"
              f"SECRET={int(replacement is not None)}\nADMIN=0\n" + block)
    # deploy.sh strips CR before SSH. Bytes keep that LF-only stdin on Windows as well.
    result = subprocess.run([bash, "-c", script], input=(replacement or "").encode("utf-8"),
                            capture_output=True, timeout=20)
    result.stdout = result.stdout.decode("utf-8")
    result.stderr = result.stderr.decode("utf-8")
    return result, path


@pytest.mark.parametrize("extra,valid", [
    ("", True), ("STUDIO_CLEANER_CA=/usr/local/share/studio/cleaner-cert.pem\n", True),
    ("STUDIO_CLEANER_CA=/one.pem\nSTUDIO_CLEANER_CA=/two.pem\n", False),
    ("UNKNOWN_SETTING=1\n", False), ("STUDIO_CLEANER_CA=$(touch unsafe)\n", False),
])
def test_install_accepts_only_the_optional_public_ca_setting(tmp_path, extra, valid):
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    result, _ = _install_environment(tmp_path, f"STUDIO_DATA_KEY={key}\n" + extra)
    assert (result.returncode == 0) == valid, result.stderr
    assert key not in result.stdout + result.stderr


def test_data_key_rotation_preserves_the_public_cleaner_ca(tmp_path):
    from cryptography.fernet import Fernet
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    ca = "STUDIO_CLEANER_CA=/usr/local/share/studio/cleaner-cert.pem\n"
    result, path = _install_environment(tmp_path, f"STUDIO_DATA_KEY={old}\n" + ca, f"STUDIO_DATA_KEY={new}\n")
    assert result.returncode == 0, result.stderr
    assert path.read_text() == f"STUDIO_DATA_KEY={new}\n" + ca
    assert old not in result.stdout + result.stderr and new not in result.stdout + result.stderr


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
             "PARTS=(app deploy requirements.txt); SWAP=(\"${PARTS[@]}\" .venv); code=000\n")
    r = subprocess.run([bash, "-c", stubs + _function("service_failed") + "service_failed 'не ответил'\necho unreachable\n"],
                       capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert r.returncode == 1 and "unreachable" not in r.stdout, r
    assert r.stderr.startswith("ОШИБКА: не ответил\n")
    assert "auto-restart" in r.stderr and "Traceback: boom" in r.stderr
    assert "for p in app deploy requirements.txt .venv; do" in r.stderr     # и прежняя среда Python
    assert "install -m 0644 /r/deploy/studio-web.service /etc/systemd/system/studio-web.service" in r.stderr
    assert "systemctl daemon-reload" in r.stderr and "чинить вперёд" not in r.stderr


def test_install_restart_refusal_takes_the_failure_branch():
    """Отказ `systemctl restart` (сломанный юнит) под set -e обрывал скрипт без журнала и подсказки
    отката, хотя прежняя служба уже остановлена, а новый код на месте. Теперь — путь отказа /health,
    но первая строка говорит, что не прошёл сам restart."""
    bash = _bash()
    if not bash:
        pytest.skip("нет bash")
    line = next(s for s in INSTALL.splitlines() if s.startswith('systemctl restart "$UNIT"'))
    stubs = ("set -euo pipefail\n"
             "systemctl() { if [ \"$1\" = restart ]; then echo 'Job failed' >&2; return 1; fi;"
             " echo 'studio-web.service: failed'; return 3; }\n"
             "journalctl() { echo 'bad unit file'; }\n"
             "R=/r; OLD=/r/.previous; UNIT=studio-web.service; ENV_FILE=/etc/studio/studio.env\n"
             "SWAP=(app deploy requirements.txt)\n")
    r = subprocess.run([bash, "-c", stubs + _function("service_failed") + line + "\necho unreachable\n"],
                       capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert r.returncode == 1 and "unreachable" not in r.stdout, r
    lines = r.stderr.splitlines()
    assert lines[:2] == ["Job failed", "ОШИБКА: systemctl restart studio-web.service не прошёл (ошибка выше)"], r.stderr
    assert "/health" not in r.stderr and "bad unit file" in r.stderr and "studio-web.service: failed" in r.stderr
    assert "for p in app deploy requirements.txt; do" in r.stderr


def test_install_systemctl_enable_does_not_hide_errors():
    """`enable … >/dev/null 2>&1` под set -e обрывал скрипт молча — deploy.sh говорил лишь «установка
    не прошла». «Created symlink» убирает --quiet, ошибки остаются видны."""
    lines = re.findall(r"^(?:if ! )?systemctl enable[^#\n]*", uncommented(INSTALL), re.M)    # команды, без комментариев
    assert len(lines) == 2 and all("--quiet" in s for s in lines), lines
    assert not any("2>" in s or "/dev/null" in s for s in lines), lines


def test_install_timer_refusal_is_a_loud_failure():
    """Таймер копии базы не включился: служба уже обновлена и отвечает, но это ошибка с кодом не 0."""
    bash = _bash()
    if not bash:
        pytest.skip("нет bash")
    start = INSTALL.index("if ! systemctl enable --now --quiet studio-backup.timer; then")
    block = INSTALL[start:INSTALL.index("fi\n", start) + 3]
    stubs = ("set -euo pipefail\nUNIT=studio-web.service\n"
             "systemctl() { echo 'Failed to enable unit' >&2; return 1; }\n")
    r = subprocess.run([bash, "-c", stubs + block + "echo unreachable\n"],
                       capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert r.returncode == 1 and "unreachable" not in r.stdout, r
    assert "Failed to enable unit" in r.stderr and "studio-backup.timer не включён" in r.stderr


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
    script = (f"set -euo pipefail\nR={root.as_posix()}; OLD={old.as_posix()}; SWAP=(app deploy requirements.txt)\n"
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
    assert INSTALL.index('|| service_failed "$UNIT не ответил 200') < reload < INSTALL.index('rm -rf "$OLD"\necho "OK')
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


def test_install_builds_the_environment_apart_and_swaps_it_with_the_code():
    """Находка аудита: pip ставил пакеты в .venv работающей службы — откат кода не возвращал
    прежние пакеты. Новая среда собирается в $NEW/.venv до подмены и переезжает вместе с кодом;
    откат и подсказка отката возвращают и её. Переезд ломает абсолютный shebang скриптов bin/ —
    поэтому всё запускается интерпретатором среды (`python -m`), а не bin/pip и bin/uvicorn."""
    code = uncommented(INSTALL)
    build = code.index('"$NEW/.venv/bin/python" -m pip install')
    assert code.index('python3 -m venv "$NEW/.venv"') < build < code.index('for p in "${SWAP[@]}"; do')
    assert "SWAP+=(.venv)" in code and "bin/pip" not in code
    assert '"$PY" -m compileall -q "$NEW/app"' in code
    assert 'for p in "${SWAP[@]}"; do' in _function("restore") and "${SWAP[*]}" in _function("service_failed")
    for unit in (UNIT, BACKUP):
        assert all(e.startswith("/opt/services/studio/.venv/bin/python ") for e in directives(unit, "ExecStart"))
    assert '"$R/.venv/bin/python" "$R/app/manage.py"' in code


FAKE_PY = '#!/bin/bash\necho "$0 $*" >> "$LOG"\n[ "$1 $2" != "-m pip" ] || [ -z "${FAIL_PIP:-}" ]\n'


def _run_env(tmp_path, *, old_venv: "str | None", fail_pip=False, then=""):
    """Блок среды install.sh (от «# Среда Python» до подмены кода) на заглушках: python3 и python
    среды — скрипт, пишущий вызовы в журнал; root, chown и install -d хоста не нужны."""
    bash = _bash()
    if not bash:
        pytest.skip("нет bash")
    root = tmp_path / "r"
    for base, text in ((root, "old"), (root / ".incoming", "new")):
        (base / "app").mkdir(parents=True)
        (base / "app" / "f").write_text(text)
        (base / "deploy").mkdir()
        (base / "agent").mkdir()
        (base / "agent" / "studio-agent.zip").write_text(text)
        (base / "requirements.txt").write_text("a==1\n", newline="\n")
    fake, log = tmp_path / "python", tmp_path / "log"
    fake.write_text(FAKE_PY, newline="\n")
    if old_venv is not None:
        (root / ".venv" / "bin").mkdir(parents=True)
        shutil.copy(fake, root / ".venv" / "bin" / "python")
        (root / ".venv" / ".studio-requirements").write_text(old_venv + "\n", newline="\n")
    start = INSTALL.index("# Среда Python")
    block = INSTALL[start:INSTALL.index('rm -rf "$NEW"\n', start) + len('rm -rf "$NEW"\n')]
    script = (f"set -euo pipefail\nR={root.as_posix()}; NEW=$R/.incoming; OLD=$R/.previous\n"
              "PARTS=(app deploy requirements.txt)\n"
              "python3() { case \"$1\" in -V) echo 'Python 3.13.5' ;; -m) mkdir -p \"$3/bin\"; cp \"$FAKE\" \"$3/bin/python\" ;; esac; }\n"
              "chown() { :; }; chmod() { :; }; install() { mkdir -p \"${@: -1}\"; }\n"
              + _function("restore") + block + then)
    env = {**os.environ, "LOG": log.as_posix(), "FAKE": fake.as_posix(), **({"FAIL_PIP": "1"} if fail_pip else {})}
    r = subprocess.run([bash, "-c", script], capture_output=True, text=True, encoding="utf-8", timeout=20, env=env)
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return r, root, calls


def _stamp(venv) -> str:
    return (venv / ".studio-requirements").read_text().strip()


SAME = hashlib.sha256(b"a==1\n").hexdigest() + " Python 3.13.5"


@pytest.mark.integration                   # bash с заглушками: 1–3 с на Windows (каждый mv — процесс)
@pytest.mark.parametrize("first", [False, True], ids=["update", "first-install"])
def test_install_swaps_a_fresh_environment_and_restore_brings_the_old_one_back(tmp_path, first):
    r, root, calls = _run_env(tmp_path, old_venv=None if first else "old", then="echo swapped; restore\n")
    assert r.returncode == 0 and "swapped" in r.stdout, r.stderr
    new_py = root.as_posix() + "/.incoming/.venv/bin/python"
    assert calls == [f"{new_py} -m pip install --quiet --disable-pip-version-check --root-user-action=ignore "
                     f"-r {root.as_posix()}/.incoming/requirements.txt",
                     f"{new_py} -m compileall -q {root.as_posix()}/.incoming/app"]
    assert (root / "app" / "f").read_text() == "old"           # откат: прежний код
    assert (root / "agent" / "studio-agent.zip").read_text() == "old"     # и прежний архив моста
    if first:
        assert not (root / ".venv").exists()
    else:
        assert _stamp(root / ".venv") == "old"                 # откат: прежняя среда


@pytest.mark.integration
def test_install_swaps_the_new_environment_in_with_the_code(tmp_path):
    r, root, _calls = _run_env(tmp_path, old_venv="old")
    assert r.returncode == 0, r.stderr
    assert _stamp(root / ".venv") == SAME and _stamp(root / ".previous" / ".venv") == "old"
    assert (root / "app" / "f").read_text() == "new" and not (root / ".incoming").exists()
    assert (root / "agent" / "studio-agent.zip").read_text() == "new"
    assert (root / ".previous" / "agent" / "studio-agent.zip").read_text() == "old"


@pytest.mark.integration
def test_install_keeps_the_environment_when_requirements_did_not_change(tmp_path):
    r, root, calls = _run_env(tmp_path, old_venv=SAME, then="restore\n")
    assert r.returncode == 0, r.stderr
    assert calls == [f"{root.as_posix()}/.venv/bin/python -m compileall -q {root.as_posix()}/.incoming/app"]
    assert _stamp(root / ".venv") == SAME and not (root / ".previous" / ".venv").exists()


@pytest.mark.integration
def test_install_failed_pip_leaves_the_running_code_and_environment(tmp_path):
    r, root, calls = _run_env(tmp_path, old_venv="old", fail_pip=True)
    assert r.returncode != 0 and "pip" in r.stderr
    assert len(calls) == 1 and "-m pip install" in calls[0]
    assert (root / "app" / "f").read_text() == "old" and _stamp(root / ".venv") == "old"
    assert not (root / ".incoming" / ".venv").exists() and not (root / ".previous").exists()


@pytest.mark.integration                   # git и bash на Windows — секунды
def test_deploy_ships_the_committed_component_and_refuses_missing_or_dirty_sources(tmp_path):
    bash = _bash()
    if not bash:
        pytest.skip("нет bash")
    repo = tmp_path / "studio"
    (repo / "deploy").mkdir(parents=True)
    (repo / "deploy" / "deploy.sh").write_text(DEPLOY_SH, encoding="utf-8", newline="\n")
    for name in ("build-agent-installer.ps1", "agent-installer.cs", "render-profile.py"):
        shutil.copyfile(DEPLOY / name, repo / "deploy" / name)
    (repo / "app").mkdir()
    (repo / "app" / "main.py").write_text("# server\n")
    (repo / "requirements.txt").write_text("# requirements\n")
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True, timeout=20)
    subprocess.run([*git, "add", "."], check=True, timeout=20)
    subprocess.run([*git, "commit", "-q", "-m", "x"], check=True, timeout=20)
    # Функции сильнее PATH: Git Bash добавляет свои /usr/bin и /mingw64/bin перед Windows PATH.
    # Даже при ошибке заглушки адрес зарезервирован для документации, ключа SSH нет.
    env = {**os.environ, "PROBE": tmp_path.as_posix(), "STUDIO_SSH": "nobody@192.0.2.1",
           "STUDIO_SSH_KEY": (tmp_path / "no-key").as_posix(),
           "STUDIO_URL": "https://studio.example.net",
           "STUDIO_PYTHON": Path(sys.executable).as_posix()}
    wrapper = ('ssh() { printf "called\\n" >> "$PROBE/ssh-calls"; '
               'case "${*: -1}" in *"cat > "*"studio-agent.zip"*) cat > "$PROBE/bridge.zip" ;; '
               '*"cat > "*"studio-agent-setup.exe"*) cat > "$PROBE/setup.exe" ;; '
               '*) cat >/dev/null ;; esac; }; '
               'curl() { printf 200; }; source "$0"')

    def deploy():
        return subprocess.run([bash, "-c", wrapper, (repo / "deploy" / "deploy.sh").as_posix()], capture_output=True, text=True,
                              encoding="utf-8", timeout=60, env=env)

    r = deploy()
    assert r.returncode == 1 and "нет agent/install.cmd в HEAD" in r.stderr, r
    assert not (tmp_path / "ssh-calls").exists()
    agent = repo / "agent"
    shutil.copytree(ROOT / "agent", agent, ignore=shutil.ignore_patterns(".venv", "__pycache__", ".pytest_cache"))
    subprocess.run([*git, "add", "."], check=True, timeout=20)
    subprocess.run([*git, "commit", "-q", "-m", "bridge"], check=True, timeout=20)
    (agent / "studio_agent" / "limits.py").write_text("# dirty\n")
    r = deploy()
    assert r.returncode == 1 and "незакоммиченные изменения" in r.stderr, r
    assert not (tmp_path / "ssh-calls").exists()
    subprocess.run([*git, "restore", "agent/studio_agent/limits.py"], check=True, timeout=20)
    r = deploy()
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "ssh-calls").is_file()
    assert (tmp_path / "setup.exe").read_bytes().startswith(b"MZ")
    with zipfile.ZipFile(tmp_path / "bridge.zip") as archive:
        names = {n for n in archive.namelist() if not n.endswith("/")}
        assert "studio-agent/install.cmd" in names and "studio-agent/studio_agent/app.py" in names
        assert b'"https://studio.example.net"' in archive.read("studio-agent/studio_agent/store.py")
        assert not any(n.startswith("studio-agent/tests/") or n.endswith("CLAUDE.md") for n in names)
        assert all(n.startswith("studio-agent/") for n in names)
        cmd = archive.read("studio-agent/install.cmd")
        assert cmd.count(b"\r\n") == cmd.count(b"\n") > 0


def test_install_brings_ffmpeg_before_the_code_swap():
    """Звук студии — ffmpeg debian: не встал — прежний код работает, а не новый без ffmpeg."""
    install = (DEPLOY / "install.sh").read_text(encoding="utf-8")
    assert "command -v ffmpeg" in install
    assert install.index("apt-get install") < install.index('mv "$NEW/$p"')
    assert "ffmpeg" in install[install.index("apt-get install"):install.index("apt-get install") + 120]


def test_apache_lets_a_recording_through_only_on_its_route():
    import transcripts
    v = uncommented(VHOST)
    block = re.search(r'<LocationMatch "\^/api/transcripts\$">(.*?)</LocationMatch>', v, re.S)
    assert block, "нет LocationMatch загрузки записи"
    limit = int(re.search(r"LimitRequestBody (\d+)", block.group(1)).group(1))
    assert limit >= transcripts.UPLOAD_MAX
    general = int(re.search(r"^\s*LimitRequestBody (\d+)", v, re.M).group(1))
    assert general < transcripts.UPLOAD_MAX
