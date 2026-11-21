"""Выкладка на debian (deploy/): юнит, vhost Apache, копия базы и скрипты договорены друг с другом."""
import hashlib
import os
from pathlib import Path
import re
import runpy
import shutil
import shlex
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import zipfile

import pytest

import bridge
import chat
import chat_exports
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
import transcripts
import videos
from conftest import DEPLOY, ROOT

UNIT = (DEPLOY / "studio-web.service").read_text(encoding="utf-8")
VHOST = (DEPLOY / "apache-vhost.conf").read_text(encoding="utf-8")
BACKUP = (DEPLOY / "studio-backup.service").read_text(encoding="utf-8")
TIMER = (DEPLOY / "studio-backup.timer").read_text(encoding="utf-8")
INSTALL = (DEPLOY / "install.sh").read_text(encoding="utf-8")
DEPLOY_SH = (DEPLOY / "deploy.sh").read_text(encoding="utf-8")
INSTALL_DOCKER = (DEPLOY / "install-docker.sh").read_text(encoding="utf-8")
DOCKERFILE = (DEPLOY / "docker" / "Dockerfile").read_text(encoding="utf-8")
NGINX = (DEPLOY / "docker" / "nginx.conf").read_text(encoding="utf-8")
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
    assert directives(UNIT, "RequiresMountsFor") == ["/mnt/storage/SharedData/studio"]


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


def test_apache_bounds_the_time_a_request_body_may_take():
    """auth.recheck решает после тела; Apache не держит тело дольше верхнего предела, а запись
    для расшифровки (transcripts.UPLOAD_MAX) на 1 Мбит/с в него укладывается."""
    first, most, rate = map(int, re.search(r"^\s*RequestReadTimeout body=(\d+)-(\d+),MinRate=(\d+)$",
                                           uncommented(VHOST), re.M).groups())
    assert first <= 20 and rate >= 500 and most <= 3600
    assert most * 125_000 >= transcripts.UPLOAD_MAX


def test_apache_accepts_agent_turn_bodies_including_followups():
    locations = re.findall(r'<LocationMatch "([^"]+)">(.*?)</LocationMatch>', VHOST, re.S)
    for op in ("turn", "steer"):
        path = f"/api/code/agents/1/projects/{'a' * 16}/chats/11111111-1111-4111-8111-111111111111/{op}"
        [body] = [body for pattern, body in locations if re.search(pattern, path)]
        assert int(re.search(r"LimitRequestBody (\d+)", body).group(1)) >= claude_code.TURN_BODY_MAX
    assert claude_code.TURN_BODY_MAX < bridge.FRAME_MAX


def test_apache_route_outlasts_every_upstream_timeout_of_the_studio():
    """B2-M8: при равных пределах 502 Apache закрывал бы 504 службы словами «шлюз не ответил»."""
    timeout = int(re.search(r"^\s*ProxyPass / \S+ timeout=(\d+)", uncommented(VHOST), re.M).group(1))
    release = (len(images.RELEASE_MODELS) + 1) * images.SHORT_TIMEOUT_SEC  # один release на слот
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
    assert _route_timeout("/api/code/agents/1/projects/a/chats/b/turn") == 600 > bridge.SEND_SEC + bridge.CALL_SEC
    upgrades = [s.strip() for s in uncommented(VHOST).splitlines() if "upgrade=websocket" in s]
    assert len(upgrades) == 1 and upgrades[0].startswith("ProxyPass /api/agent/link ")


def test_apache_route_outlasts_the_whole_synchronous_speech_operation():
    """Голосовой ввод, образец и проба голоса отвечают в том же запросе: вся операция службы (а не один
    вызов шлюза — у WhisperX там 2460 с) уложена в бюджет меньше таймаута маршрута, иначе Apache
    оборвал бы запрос раньше службы и текст диктовки пропал бы."""
    import dictation
    import speeches
    import voices
    assert dictation.BUDGET_SEC < _route_timeout("/api/dictation")
    assert speeches.SAY_BUDGET_SEC < _route_timeout("/api/speech/say")
    assert voices.BUDGET_SEC < _route_timeout("/api/voices")
    assert voices.TRANSCRIBE_BUDGET_SEC + voices.DRAFT_SAVE_SEC < _route_timeout("/api/voices/transcribe")
    assert voices.BUDGET_SEC + media.SAVE_TIMEOUT_SEC < _route_timeout("/api/voices/prepare-sample")
    assert voices.PROBE_BUDGET_SEC < _route_timeout(f"/api/voices/{'0' * 32}/probe")


def test_apache_route_outlasts_the_folder_zip():
    """B2-I2: «ZIP папки» отвечает, когда сервер MCP соберёт архив (до my_files.ZIP_SEC); общий маршрут 150 с
    обрывал большие папки 502 Apache, пока служба ещё ждала сервер."""
    import my_files
    assert my_files.ZIP_SEC < _route_timeout("/api/my-files/1/zip")


def test_voice_editor_uploads_fit_through_the_proxy():
    import voices
    voice, sample = "0" * 32, "1" * 32
    # редактор шлёт transcribe и prepare-sample; прямые маршруты API принимают тот же voices.AUDIO_MAX
    paths = ["/api/voices/transcribe", "/api/voices/prepare-sample", "/api/voices",
             f"/api/voices/{voice}/samples", f"/api/voices/{voice}/samples/{sample}"]
    limits = re.findall(r'<LocationMatch "([^"]+)">\s*LimitRequestBody (\d+)', uncommented(VHOST))
    for path in paths:
        assert any(re.fullmatch(pattern, path) and int(limit) >= voices.AUDIO_MAX + 64 * 1024 for pattern, limit in limits)
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
    manage_py = "/opt/services/studio/.venv/bin/python /opt/services/studio/app/manage.py"
    assert directives(BACKUP, "ExecStart") == [f"{manage_py} backup", f"{manage_py} verify-backup"]
    docker = [s.rsplit("/manage.py ", 1)[1]
              for s in directives((DEPLOY / "docker" / "studio-backup.service").read_text(encoding="utf-8"), "ExecStart")]
    assert docker == ["backup", "verify-backup"]           # ночная проверка — сразу после удачной копии
    assert directives(TIMER, "OnCalendar") == ["*-*-* 04:30"] and directives(TIMER, "Persistent") == ["true"]
    assert "WantedBy=timers.target" in TIMER
    assert "systemctl enable --now --quiet studio-backup.timer" in INSTALL
    assert all(f"$R/deploy/{u}" in INSTALL for u in ("studio-backup.service", "studio-backup.timer"))


def test_secret_value_is_never_an_argument():
    """Ключ данных едет из vault потоком: в argv он виден в ps и попал бы в журнал sudo."""
    assert "grep -m1 '^STUDIO_DATA_KEY=' \"$VAULT\"" in DEPLOY_SH
    assert "args+=(--secret-stdin)" in DEPLOY_SH and '$installer ${args[*]:-}' in DEPLOY_SH
    assert "installer=install.sh" in DEPLOY_SH and "installer=install-docker.sh" in DEPLOY_SH
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
    assert parts == {"app deploy requirements.txt constraints.txt docs/guide.ru.md"}
    assert "URL=${STUDIO_URL:-https://studio.example.com}" in DEPLOY_SH and '"$URL/health"' in DEPLOY_SH


def test_help_guide_ships_with_the_app():
    """«Справка» читает docs/guide.ru.md рядом с app/ (`help.GUIDE`): часть выкладки, образ и обмен частей с
    подкаталогом — на прежнее место, и прежняя копия — в .previous по тому же пути."""
    import help as help_module
    assert help_module.GUIDE == ROOT / "docs" / "guide.ru.md" and help_module.GUIDE.is_file()
    assert "COPY docs/guide.ru.md ./docs/guide.ru.md" in DOCKERFILE                # /opt/studio/docs рядом с app
    assert {"!docs/", "!docs/guide.ru.md"} <= set((ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines())
    assert 'mkdir -p "$R/$(dirname "$p")" "$OLD/$(dirname "$p")"' in INSTALL
    assert 'if [ -e "$R/$p" ]; then mv "$R/$p" "$OLD/$p"; fi' in INSTALL


def test_unit_lets_a_frame_just_over_the_limit_reach_the_app():
    """Кадр больше FRAME_MAX — 4400 протокола, а не 1009 самого uvicorn: предел uvicorn чуть выше, и такой кадр
    доходит до `bridge._frame`. Пинги, которые протокол §2 называет, — явными флагами, а не умолчаниями."""
    exec_start = directives(UNIT, "ExecStart")[0]
    assert int(re.search(r"--ws-max-size (\d+)", exec_start).group(1)) == bridge.FRAME_MAX + 1024
    assert "--ws-ping-interval 20 --ws-ping-timeout 20" in exec_start


def _uvicorn_flags(argv: list) -> list:
    """Флаги uvicorn без интерпретатора и путей (каталог приложения у юнита и образа разный)."""
    argv = argv[argv.index("uvicorn") + 1:]
    assert argv[0] == "main:app" and argv[1:3][0] == "--app-dir"
    return argv[3:]


def test_docker_command_runs_uvicorn_with_the_unit_flags():
    """Пределы `--ws-max-size`, `--timeout-graceful-shutdown` и пинги сверялись только у юнита systemd: образ
    мог разойтись с ним молча."""
    import json
    cmd = json.loads(re.search(r"^CMD (\[.*\])$", DOCKERFILE, re.M).group(1))
    unit = shlex.split(directives(UNIT, "ExecStart")[0])
    assert cmd[:3] == ["python", "-m", "uvicorn"] and unit[1:3] == ["-m", "uvicorn"]
    assert _uvicorn_flags(cmd) == _uvicorn_flags(unit)          # и сокет: nginx ходит в /run/studio/web.sock
    assert "upstream studio { server unix:/run/studio/web.sock; }" in NGINX


def _nginx(name: str) -> str:
    values = re.findall(rf"^\s*{name} (\S+);", uncommented(NGINX), re.M)
    assert len(values) == 1, (name, values)
    return values[0]


def test_nginx_passes_every_body_and_wait_that_apache_passes():
    """nginx контейнера стоит за Apache: меньший предел тела или ожидания отбивал бы запрос раньше Apache."""
    body = _nginx("client_max_body_size")
    size = int(body[:-1]) * {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[body[-1].lower()]
    assert size >= max(int(n) for n in re.findall(r"LimitRequestBody (\d+)", uncommented(VHOST)))
    longest = max(int(t) for t in re.findall(r"^\s*ProxyPass \S+ \S+ (?:\S+=\S+ )*timeout=(\d+)", uncommented(VHOST), re.M))
    for name in ("proxy_read_timeout", "proxy_send_timeout"):
        value = _nginx(name)
        assert value.endswith("s") and int(value[:-1]) >= longest, (name, value, longest)


COMPOSE = (DEPLOY / "docker" / "compose.yaml").read_text(encoding="utf-8")


def _compose_block(start: str, end: "str | None") -> str:
    """Текст блока compose.yaml между двумя строками-заголовками, без комментариев."""
    text = uncommented(COMPOSE)
    begin = text.index(start + "\n")
    return text[begin:text.index(end + "\n", begin) if end else len(text)]


def _keys(block: str, indent: int) -> dict:
    """Ключи заданного отступа и их значения в той же строке."""
    return dict(re.findall(rf"^ {{{indent}}}([A-Za-z_<]+):[ ]?(.*)$", block, re.M))


def test_compose_keeps_the_container_restrictions_of_both_services():
    """uid 10001, корень только для чтения, без capabilities и новых привилегий: ослабление якоря `common`
    или переопределение в сервисе прошло бы зелёный набор."""
    common = _keys(_compose_block("x-common: &common", "services:"), 2)
    assert common["user"] == '"10001:10001"' and common["read_only"] == "true"
    assert common["cap_drop"] == "[ALL]" and common["security_opt"] == "[no-new-privileges:true]"
    for name, end in (("  web:", "  ingress:"), ("  ingress:", None)):
        block = _compose_block(name, end)
        keys = _keys(block, 4)
        assert keys["<<"] == "*common", name
        assert not {"user", "read_only", "cap_drop", "privileged", "pid", "ipc", "userns_mode"} & set(keys), name
        if "security_opt" in keys:              # список сервиса заменяет список якоря целиком
            options = re.search(r"^    security_opt:\n((?:      - .*\n)+)", block, re.M).group(1)
            assert "      - no-new-privileges:true\n" in options, name
    web = _compose_block("  web:", "  ingress:")
    assert "ports" not in _keys(web, 4) and "expose" not in _keys(web, 4)     # TCP-входа в приложение нет
    assert "      - seccomp:./chromium-seccomp.json\n" in web
    assert _keys(web, 4)["cap_add"] == "[SYS_CHROOT]"
    ingress = _compose_block("  ingress:", None)
    ports = re.search(r"^    ports:\n((?:      - .*\n)+)", ingress, re.M).group(1)
    assert ports == '      - "${STUDIO_BIND_IP:?Set STUDIO_BIND_IP}:${STUDIO_BACKEND_PORT:-8151}:8080"\n'
    assert "network_mode" not in _keys(ingress, 4) and "cap_add" not in _keys(ingress, 4)


def test_deploy_builds_the_bridge_archive_from_the_studio_commit():
    """Мост и сервер собираются из одного HEAD; проверка всего компонента — до первого ssh."""
    code = uncommented(DEPLOY_SH)
    bundle = (DEPLOY / "build-agent-bundle.py").read_text(encoding="utf-8")
    assert 'git(repo, "archive", "--format=zip", "--prefix=studio-agent/", f"{revision}:agent")' in bundle
    assert "for file in studio-agent.zip studio-agent-setup.exe studio-agent-setup.pkg studio-agent-setup.sh" in code
    assert "$IN/agent/$file" in code
    first_ssh = code.index('"${SSH[@]}" "sudo install -d')
    assert code.index('status --porcelain -- "${PARTS[@]}" agent') < first_ssh
    assert code.index('cat-file -e "$REV:agent/install.cmd"') < first_ssh
    checkout = code.index('show "$REV:deploy/build-agent-bundle.py"')
    build = code.index('"$PYTHON" "$package/build-agent-bundle.py" "$D" "$REV" "$package"')
    assert checkout < build < first_ssh
    assert 'git_file(repo, revision, f"deploy/{name}", work / name)' in bundle
    assert 'render-profile.py" --agent' not in code


def test_bundle_cache_rebuilds_after_an_artifact_changes(tmp_path, monkeypatch):
    bundle = runpy.run_path(str(DEPLOY / "build-agent-bundle.py"))
    prepare = bundle["prepare"]
    calls = []

    def build(repo, revision, work):
        calls.append(revision)
        for name in bundle["ARTIFACTS"]:
            (work / name).write_bytes(f"{name}-{len(calls)}".encode())

    for name in bundle["SIGNING"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setitem(prepare.__globals__, "cache_key", lambda *_: "fingerprint")
    monkeypatch.setitem(prepare.__globals__, "build", build)
    for revision in ("first", "same-bridge", "corrupt"):
        work = tmp_path / revision
        work.mkdir()
        if revision == "corrupt":
            (tmp_path / ".cache/studio-agent-bundle/studio-agent-setup.pkg").write_bytes(b"bad")
        prepare(tmp_path, revision, work)
    assert calls == ["first", "corrupt"]
    assert (tmp_path / "same-bridge/studio-agent-setup.pkg").read_bytes() == b"studio-agent-setup.pkg-1"
    assert (tmp_path / "corrupt/studio-agent-setup.pkg").read_bytes() == b"studio-agent-setup.pkg-2"


def test_deploy_resolves_the_revision_once_and_uploads_into_its_own_folder():
    """HEAD разрешался заново около десяти раз: коммит параллельной сессии смешивал мост, код и тег образа
    из разных ревизий. Общую .incoming одна выкладка очищала посреди загрузки другой."""
    code = uncommented(DEPLOY_SH)
    gits = re.findall(r'git -C "\$D" .*', code)
    assert [g for g in gits if "HEAD" in g] == ["git -C \"$D\" rev-parse --verify 'HEAD^{commit}')"]
    assert all('"$REV' in g for g in gits if re.search(r" (show|archive|cat-file) ", g)), gits
    assert len([g for g in gits if " archive " in g]) == 2 and 'args+=("--revision=$REV")' in code
    assert "IN=$R/.incoming-${REV:0:12}-$$-$RANDOM" in code and "sudo mkdir -m 0700 $IN" in code
    assert "$R/.incoming" not in code.replace("$R/.incoming-${REV", "") and "rm -rf $R/.incoming" not in code
    assert '"${SSH[@]}" "sudo bash $IN/deploy/$installer ${args[*]:-}"' in code
    assert 'if [ "$uploaded" = 1 ]; then "${SSH[@]}" "sudo rm -rf $IN" || true; fi' in code     # отказ, --prepare-only
    docker = uncommented(INSTALL_DOCKER)
    check = docker.index('case "${NEW##*/}" in .incoming-*) ;;')
    assert check < docker.index("flock -n 9") < docker.index("docker build")
    native = uncommented(INSTALL)
    assert 'NEW=$(readlink -f "$(dirname "$(readlink -f "$0")")/..")' in native
    check = native.index('case "$NEW" in "$R"/.incoming-*) ;;')
    assert check < native.index('exec 9>"$R/.deploy.lock"') < native.index("flock -n 9") < native.index("getent passwd")


@pytest.mark.integration                   # настоящий bash и операции с каталогами
@pytest.mark.parametrize("outcome", ["success", "upload-failed", "mkdir-failed"])
def test_deploy_cleanup_removes_only_the_folder_it_created(tmp_path, outcome):
    bash = _bash()
    if not bash:
        pytest.skip("нет bash")
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory:
        root = Path(directory) / "remote"
        root.mkdir()
        for name in (".incoming-old", ".incoming-active", ".incoming-collision"):
            folder = root / name
            folder.mkdir()
            (folder / "payload").write_bytes(b"another deployment")
        os.utime(root / ".incoming-old", (1, 1))
        package = Path(directory) / "package"
        package.mkdir()
        setup = DEPLOY_SH[DEPLOY_SH.index("IN=$R/"):
                          DEPLOY_SH.index('git -C "$D" show "$REV:deploy/build-agent-bundle.py"')]
        create = DEPLOY_SH[DEPLOY_SH.index('echo "==> код '):
                           DEPLOY_SH.index('git -C "$D" -c core.autocrlf=false archive --format=tar')]
        script = ("set -euo pipefail\n"
                  f"R={shlex.quote(root.as_posix())}\npackage={shlex.quote(package.as_posix())}\n"
                  f"REV={'a' * 40}\nHOST=fixture\nSSH=(ssh)\n"
                  # Функции сильнее PATH: никакого настоящего SSH или sudo.
                  # POSIX-права на Windows не моделируем, создание и удаление — настоящие.
                  'sudo() { case "$1" in install) mkdir -p -- "${*: -1}" ;; '
                  'mkdir) mkdir -- "${*: -1}" ;; *) "$@" ;; esac; }\n'
                  'ssh() { eval "${*: -1}"; }\n' + setup)
        if outcome == "mkdir-failed":
            script += 'IN=$R/.incoming-collision\n'
        script += create + ("exit 7\n" if outcome == "upload-failed" else "exit 0\n")
        result = subprocess.run([bash, "-c", script], capture_output=True, text=True,
                                encoding="utf-8", timeout=20)
        for name in (".incoming-old", ".incoming-active", ".incoming-collision"):
            assert (root / name / "payload").read_bytes() == b"another deployment", result.stderr
        assert {p.name for p in root.iterdir()} == {".incoming-old", ".incoming-active", ".incoming-collision"}, result.stderr
        assert not package.exists()
        assert result.returncode == {"success": 0, "upload-failed": 7, "mkdir-failed": 1}[outcome], result.stderr


@pytest.mark.integration
def test_docker_deploy_reuses_only_bridge_files_with_matching_hash(tmp_path):
    bash = _bash()
    if not bash:
        pytest.skip("нет bash")
    root = tmp_path / "server"
    incoming = root / "incoming" / "agent"
    incoming.mkdir(parents=True)
    current = root / "current" / "agent"
    current.mkdir(parents=True)
    package = tmp_path / "package"
    package.mkdir()
    for name in ("studio-agent.zip", "studio-agent-setup.exe", "studio-agent-setup.pkg", "studio-agent-setup.sh"):
        (package / name).write_bytes(name.encode())
        (current / name).write_bytes(name.encode() if name.endswith(".pkg") else b"old")
    start = DEPLOY_SH.index("for file in studio-agent.zip")
    loop = DEPLOY_SH[start:DEPLOY_SH.index('\nif [ "$NEW_SECRET"', start)]
    result = subprocess.run(
        [bash, "-c", 'set -euo pipefail\nR=$1; IN=$R/incoming; package=$2; RUNTIME=docker; SSH=(ssh)\n'
         'ssh() { eval "$1"; }\nsudo() { if [ "$1" = -n ]; then shift; fi; "$@"; }\n' + loop,
         "fixture", root.as_posix(), package.as_posix()], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["==> studio-agent-setup.pkg: использован файл на сервере"]
    assert {p.name: p.read_bytes() for p in incoming.iterdir()} == {p.name: p.read_bytes() for p in package.iterdir()}


@pytest.mark.parametrize("path", [".tmp/deploy-x/studio-agent.zip", ".tmp/browser-mcp-1/shot.png",
                                  ".cache/studio-agent-bundle/studio-agent.zip"])
def test_local_build_folder_is_gitignored(path):
    """Рабочие файлы выкладки, кэш пакетов и снимки MCP исключены из Git."""
    git = shutil.which("git")
    if not git:
        pytest.skip("нет git — проверка .gitignore пропущена")
    r = subprocess.run([git, "-C", str(ROOT), "check-ignore", "-q", path], capture_output=True, timeout=20)
    assert r.returncode == 0, f"{path} не покрыт .gitignore"


def test_install_swaps_the_bridge_archive_with_the_code():
    """`agent` переезжает вместе с кодом и откатывается с ним; без архива — отказ до подмены кода."""
    code = uncommented(INSTALL)
    assert 'SWAP=("${PARTS[@]}" agent)' in code
    check = code.index('[ -s "$NEW/agent/studio-agent.zip" ]')
    assert check < code.index('for p in "${SWAP[@]}"; do') and check < code.index('chown -R root:root "$NEW"')
    setup = code.index('[ -s "$NEW/agent/studio-agent-setup.exe" ]')
    assert setup < code.index('for p in "${SWAP[@]}"; do') and setup < code.index('chown -R root:root "$NEW"')
    macos = code.index('[ -s "$NEW/agent/studio-agent-setup.pkg" ]')
    assert macos < code.index('for p in "${SWAP[@]}"; do')
    assert code.index('[ -s "$NEW/agent/studio-agent-setup.sh" ]') < code.index('for p in "${SWAP[@]}"; do')


def test_requirements_are_exact_pins():
    lines = [line for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
             if line and not line.startswith("#")]
    assert lines and all(re.fullmatch(r"[A-Za-z0-9._-]+(\[[a-z,]+\])?==\S+", line) for line in lines), lines


def _requirements(path: Path) -> list:
    from packaging.requirements import Requirement
    requirements = [Requirement(line) for line in path.read_text(encoding="utf-8").splitlines()
                    if line and not line.startswith(("#", "-"))]
    return [req for req in requirements if req.marker is None or req.marker.evaluate()]


def _constraints(path: Path) -> dict:
    """Каждая действующая в этой среде строка — точная версия, имя — один раз."""
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    pins = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            req = Requirement(line)
            assert re.fullmatch(r"==[A-Za-z0-9.+!]+", str(req.specifier)), line
            if req.marker is not None and not req.marker.evaluate():
                continue
            assert canonicalize_name(req.name) not in pins, line
            pins[canonicalize_name(req.name)] = str(req.specifier)[2:]
    return pins


def unpinned_dependencies(roots, pins: dict, environments: list) -> set:
    """Имена замыкания `roots`, которых нет в `pins`. Зависимости — из метаданных пакетов, установленных
    в среде тестов, маркеры — целевых сред (любой из них достаточно); пакет, которого здесь нет (uvloop
    под Windows), проверяется только сам."""
    import importlib.metadata
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    todo, seen, missing = list(roots), set(), set()
    while todo:
        req = todo.pop()
        name = canonicalize_name(req.name)
        if name not in pins:
            missing.add(name)
            continue
        if (name, frozenset(req.extras)) in seen:
            continue
        seen.add((name, frozenset(req.extras)))
        try:
            requires = importlib.metadata.requires(req.name) or []
        except importlib.metadata.PackageNotFoundError:
            continue
        for spec in map(Requirement, requires):
            if spec.marker is None or any(spec.marker.evaluate({**env, "extra": extra})
                                          for env in environments for extra in {"", *req.extras}):
                todo.append(spec)
    return missing


LINUX_313 = {"sys_platform": "linux", "platform_system": "Linux", "os_name": "posix", "platform_machine": "x86_64",
             "python_version": "3.13", "python_full_version": "3.13.0", "implementation_name": "cpython",
             "platform_python_implementation": "CPython"}


def test_constraints_pin_the_whole_dependency_closure():
    """Закреплены были только прямые пакеты: starlette, pydantic, anyio, websockets сервера и mcp, anyio,
    jsonschema моста брались свежими в момент сборки слоя pip или обновления моста на каждой машине."""
    from packaging.utils import canonicalize_name
    for folder in (ROOT, ROOT / "agent"):
        pins = _constraints(folder / "constraints.txt")
        roots = _requirements(folder / "requirements.txt")
        for req in roots:                                   # прямые — те же версии, что в requirements.txt
            assert pins.get(canonicalize_name(req.name)) == str(req.specifier)[2:], req
    # Замыкание сервера — по метаданным этой среды (requirements-dev ставит requirements.txt); моста —
    # в наборе agent/ (agent/tests/test_constraints.py), его пакетов в среде сервера нет.
    assert unpinned_dependencies(_requirements(ROOT / "requirements.txt"), _constraints(ROOT / "constraints.txt"),
                                 [LINUX_313]) == set()


def test_dockerfile_pins_yt_dlp_default_and_deno():
    """YouTube требует JS-рантайм (спека 2026-11-21-studio-source-capabilities-design, п. 22): yt-dlp с extra
    `default` (yt-dlp-ejs) и deno — закреплёнными версиями; версия yt-dlp — одна, в Dockerfile, замыкание и deno —
    в constraints.txt (wheel deno manylinux_2_27_x86_64 ставится в trixie); регламент подъёма — в deploy-docker.md."""
    line = next(s for s in DOCKERFILE.splitlines() if "pip install" in s)
    assert re.search(r'"yt-dlp\[default\]==\d{4}\.\d{1,2}\.\d{1,2}" deno$', line), line
    pins = _constraints(ROOT / "constraints.txt")
    assert re.fullmatch(r"\d+\.\d+\.\d+", pins.get("deno", "")), pins.get("deno")
    for name in ("yt-dlp-ejs", "requests", "urllib3", "charset-normalizer", "certifi", "idna", "mutagen",
                 "pycryptodomex", "brotli", "websockets"):
        assert name in pins, name
    assert "yt-dlp" not in pins
    doc = (ROOT / "docs" / "architecture" / "deploy-docker.md").read_text(encoding="utf-8")
    section = doc.split("## Поднять yt-dlp", 1)[1].split("\n## ", 1)[0]
    for needle in ("yt-dlp --version", "deno --version", "VIDEO=", "deploy.sh"):
        assert needle in section, needle


def test_every_pip_install_uses_the_constraints():
    assert "pip install --no-cache-dir -r requirements.txt -c constraints.txt " in DOCKERFILE
    assert "COPY requirements.txt constraints.txt ./" in DOCKERFILE
    assert "!constraints.txt" in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert '-r "$NEW/requirements.txt" -c "$NEW/constraints.txt"' in INSTALL
    assert 'cat "$NEW/requirements.txt" "$NEW/constraints.txt" | sha256sum' in INSTALL      # штамп среды
    updater = (ROOT / "agent" / "studio_agent" / "updater.py").read_text(encoding="utf-8")
    assert "'-r', str(stage / 'requirements.txt'), '-c', str(stage / 'constraints.txt')" in updater
    install = (ROOT / "agent" / "install.ps1").read_text(encoding="utf-8-sig")
    assert "'-r', (Join-Path $repo 'requirements.txt'), '-c', (Join-Path $repo 'constraints.txt')" in install
    assert "'constraints.txt'" in (DEPLOY / "build-agent-installer.ps1").read_text(encoding="utf-8-sig")
    attributes = subprocess.run(["git", "-C", str(ROOT), "check-attr", "export-ignore", "agent/constraints.txt"],
                                capture_output=True, text=True, timeout=20).stdout
    assert attributes.endswith(": unspecified\n"), attributes          # едет в ZIP моста


def test_deploy_files_have_lf_endings():
    """\r в юните systemd принял бы за часть пути, bash — за часть команды."""
    tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", "deploy/"],
                             check=True, capture_output=True, timeout=20).stdout.decode("utf-8")
    for name in filter(None, tracked.split("\0")):
        assert b"\r\n" not in (ROOT / name).read_bytes(), name


def test_docker_build_refreshes_the_base_and_the_packages_weekly():
    """Без --pull и ARG REFRESH база и слой apt (Chromium, ffmpeg) брались из кэша первой сборки:
    исправления Debian и Python в прод не попадали, пока кэш не очистят вручную."""
    code = uncommented(INSTALL_DOCKER)
    build = re.search(r"^build=\((.*)\)$", code, re.M).group(1)
    assert build.startswith("docker build --pull ") and '--build-arg "REFRESH=$(date -u +%G-W%V)"' in build
    assert 'if [ "$REFRESH_BASE" = 1 ]; then build+=(--no-cache); fi' in code
    assert len(re.findall(r"docker build\b", code)) == 1 and '"${build[@]}" -t "$image"' in code
    assert "--refresh-base) REFRESH_BASE=1 ;;" in code
    assert "args+=(--refresh-base)" in DEPLOY_SH and "--refresh-base requires Docker" in DEPLOY_SH
    lines = uncommented(DOCKERFILE).splitlines()
    apt = next(i for i, s in enumerate(lines) if "apt-get install" in s or "apt-get update" in s)
    assert 0 < lines.index("ARG REFRESH=none") < apt         # кэш промахивается на слое apt и дальше


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


@pytest.mark.parametrize("configured,mounted,success", [
    ("", "cifs", True), ("", "ext4", False),
    ("ext4", "ext4", True), ("ext4", "cifs", False),
])
def test_docker_entrypoint_requires_the_configured_media_filesystem(configured, mounted, success):
    bash = _bash()
    if not bash:
        pytest.skip("нет bash")
    script = ('findmnt() { if [ "$5" = /media ]; then printf "%s\\n" "$MOUNTED"; else echo ext4; fi; }; '
              'entrypoint=$1; set -- true; . "$entrypoint"')
    env = {**os.environ, "STUDIO_MEDIA_DIR": "/media", "STUDIO_MEDIA_FSTYPE": configured, "MOUNTED": mounted}
    path = (DEPLOY / "docker/entrypoint.sh").as_posix()
    result = subprocess.run([bash, "-c", script, "entrypoint-test", path], env=env, capture_output=True, timeout=20)
    assert (result.returncode == 0) == success, result.stderr


@pytest.mark.integration  # Launches Git and Bash to check the deployed wrapper.
def test_pdf_chromium_wrapper_reaches_the_image_and_adds_the_offline_flags(tmp_path):
    """chat_exports.OFFLINE_BROWSER: in the image, executable, LF, and every md2pdf argument passed through."""
    wrapper = DEPLOY / "chromium-offline.sh"
    assert chat_exports.OFFLINE_BROWSER == wrapper.resolve()
    assert "COPY deploy/md2pdf.py deploy/md2pdf.sha256 deploy/chromium-offline.sh ./deploy/" in DOCKERFILE
    assert "!deploy/chromium-offline.sh" in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert b"\r" not in wrapper.read_bytes()
    staged = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-s", "deploy/chromium-offline.sh"],
                            capture_output=True, text=True, timeout=20).stdout
    assert staged.startswith("100755 "), staged              # md2pdf запускает его напрямую
    bash = _bash()
    if not bash:
        pytest.skip("нет bash")
    (tmp_path / "chromium").write_text('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done\n', newline="\n")
    (tmp_path / "chromium").chmod(0o755)
    url = "file:///tmp/chat-export-x/a b.html"
    r = subprocess.run([bash, "-c", f'PATH="$PWD:$PATH" exec sh "$1" --headless "{url}"', "-", wrapper.as_posix()],
                       cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ["--proxy-server=socks5://255.255.255.255:9", "--proxy-bypass-list=<-loopback>",
                                     "--host-resolver-rules=MAP * ~NOTFOUND", "--headless", url]


@pytest.mark.parametrize("script", ["install.sh", "deploy.sh", "install-docker.sh", "install-proxy.sh",
                                    "docker/entrypoint.sh", "chromium-offline.sh"])
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


@pytest.mark.integration  # Bash с файловыми процессами под xdist: > 1 с на Windows
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


@pytest.mark.integration  # настоящий bash и несколько файловых процессов: > 1 с на Windows
def test_data_key_rotation_preserves_the_public_cleaner_ca(tmp_path):
    from cryptography.fernet import Fernet
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    ca = "STUDIO_CLEANER_CA=/usr/local/share/studio/cleaner-cert.pem\n"
    result, path = _install_environment(tmp_path, f"STUDIO_DATA_KEY={old}\n" + ca, f"STUDIO_DATA_KEY={new}\n")
    assert result.returncode == 0, result.stderr
    assert path.read_text() == f"STUDIO_DATA_KEY={new}\n" + ca
    assert old not in result.stdout + result.stderr and new not in result.stdout + result.stderr


@pytest.mark.integration  # настоящий Bash: > 1 с на Windows под нагрузкой набора
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


@pytest.mark.integration  # запуск Bash под нагрузкой набора: > 1 с на Windows
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


@pytest.mark.integration  # поведенческий сценарий настоящего Bash, как остальные ветки отказа
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


@pytest.mark.integration  # настоящий Bash, rm и mv под нагрузкой набора: > 1 с на Windows
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


@pytest.mark.integration  # настоящий Bash: > 1 с на Windows под нагрузкой набора
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
        (base / "constraints.txt").write_text("b==2\n", newline="\n")
    fake, log = tmp_path / "python", tmp_path / "log"
    fake.write_text(FAKE_PY, newline="\n")
    if old_venv is not None:
        (root / ".venv" / "bin").mkdir(parents=True)
        shutil.copy(fake, root / ".venv" / "bin" / "python")
        (root / ".venv" / ".studio-requirements").write_text(old_venv + "\n", newline="\n")
    start = INSTALL.index("# Среда Python")
    block = INSTALL[start:INSTALL.index('rm -rf "$NEW"\n', start) + len('rm -rf "$NEW"\n')]
    script = (f"set -euo pipefail\nR={root.as_posix()}; NEW=$R/.incoming; OLD=$R/.previous\n"
              "PARTS=(app deploy requirements.txt constraints.txt)\n"
              "python3() { case \"$1\" in -V) echo 'Python 3.13.5' ;; -m) mkdir -p \"$3/bin\"; cp \"$FAKE\" \"$3/bin/python\" ;; esac; }\n"
              "chown() { :; }; chmod() { :; }; install() { mkdir -p \"${@: -1}\"; }\n"
              + _function("restore") + block + then)
    env = {**os.environ, "LOG": log.as_posix(), "FAKE": fake.as_posix(), **({"FAIL_PIP": "1"} if fail_pip else {})}
    r = subprocess.run([bash, "-c", script], capture_output=True, text=True, encoding="utf-8", timeout=20, env=env)
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return r, root, calls


def _stamp(venv) -> str:
    return (venv / ".studio-requirements").read_text().strip()


SAME = hashlib.sha256(b"a==1\nb==2\n").hexdigest() + " Python 3.13.5"


@pytest.mark.integration                   # bash с заглушками: 1–3 с на Windows (каждый mv — процесс)
@pytest.mark.parametrize("first", [False, True], ids=["update", "first-install"])
def test_install_swaps_a_fresh_environment_and_restore_brings_the_old_one_back(tmp_path, first):
    r, root, calls = _run_env(tmp_path, old_venv=None if first else "old", then="echo swapped; restore\n")
    assert r.returncode == 0 and "swapped" in r.stdout, r.stderr
    new_py = root.as_posix() + "/.incoming/.venv/bin/python"
    assert calls == [f"{new_py} -m pip install --quiet --disable-pip-version-check --root-user-action=ignore "
                     f"-r {root.as_posix()}/.incoming/requirements.txt -c {root.as_posix()}/.incoming/constraints.txt",
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


@pytest.fixture
def component_workspace(tmp_path):
    with tempfile.TemporaryDirectory(dir=tmp_path) as directory:
        yield Path(directory)


@pytest.mark.integration                   # git и bash на Windows — секунды
@pytest.mark.timeout(120)
@pytest.mark.parametrize("signed_pkg", [False, True])
def test_deploy_ships_the_committed_component_and_refuses_missing_or_dirty_sources(component_workspace, signed_pkg):
    tmp_path = component_workspace
    bash = _bash()
    if not bash:
        pytest.skip("нет bash")
    repo = tmp_path / "studio"
    (repo / "deploy").mkdir(parents=True)
    (repo / "deploy" / "deploy.sh").write_text(DEPLOY_SH, encoding="utf-8", newline="\n")
    for name in ("build-agent-installer.ps1", "agent-installer.cs", "build-agent-linux.py", "render-profile.py",
                 "build-agent-bundle.py"):
        shutil.copyfile(DEPLOY / name, repo / "deploy" / name)
    # Здесь проверяется доставка одного HEAD; тяжёлая сборка PKG имеет собственную приёмку на Mac.
    (repo / 'deploy/build-agent-macos.py').write_text(
        'import pathlib,sys; pathlib.Path(sys.argv[sys.argv.index("--output")+1]).write_bytes(b"xar!fixture"); '
        'pathlib.Path(sys.argv[sys.argv.index("--launcher-output")+1]).write_bytes(b"\\xca\\xfe\\xba\\xbe fixture")\n')
    shutil.copyfile(DEPLOY / 'macos-runtime.json', repo / 'deploy/macos-runtime.json')
    (repo / "app").mkdir()
    (repo / "app" / "main.py").write_text("# server\n")
    (repo / "requirements.txt").write_text("# requirements\n")
    (repo / "constraints.txt").write_text("# constraints\n")
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.ru.md").write_text("# guide\n")
    converter_bytes = b'# Shared converter fixture\n'
    (repo / 'deploy' / 'md2pdf.py').write_bytes(converter_bytes)
    (repo / 'deploy' / 'md2pdf.sha256').write_text(hashlib.sha256(converter_bytes).hexdigest() + '\n', encoding='utf-8')
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
    env.pop('STUDIO_MD2PDF_SOURCE', None)
    env.pop('STUDIO_TLS_CERT', None)
    env['STUDIO_MACOS_SIGNING_IDENTITY'] = '0' * 40 if signed_pkg else ''
    wrapper = ('ssh() { printf "called\\n" >> "$PROBE/ssh-calls"; '
               'case "${*: -1}" in *"cat > "*"studio-agent.zip"*) cat > "$PROBE/bridge.zip" ;; '
               '*"cat > "*"studio-agent-setup.exe"*) cat > "$PROBE/setup.exe" ;; '
               '*"cat > "*"studio-agent-setup.pkg"*) cat > "$PROBE/setup.pkg" ;; '
               '*"cat > "*"studio-agent-setup.sh"*) cat > "$PROBE/setup.sh" ;; '
               '*"tar -xf - -C "*) cat > "$PROBE/release.tar" ;; '
               '*) cat >/dev/null ;; esac; }; '
               'curl() { printf "%s\\n" "$@" > "$PROBE/curl-args"; printf 200; }; source "$0"')

    def deploy():
        # errors="replace": powershell.exe сборки EXE пишет в кодовой странице консоли, не UTF-8
        return subprocess.run([bash, "-c", wrapper, (repo / "deploy" / "deploy.sh").as_posix()], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60, env=env)

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
    if signed_pkg:
        certificate = tmp_path / "private CA.pem"
        env["STUDIO_TLS_CERT"] = certificate.as_posix()
        r = deploy()
        assert r.returncode == 1 and "STUDIO_TLS_CERT" in r.stderr and not (tmp_path / "ssh-calls").exists()
        certificate.write_text("CA fixture", encoding="ascii")
    r = deploy()
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "ssh-calls").is_file()
    curl_args = (tmp_path / "curl-args").read_text(encoding="utf-8").splitlines()
    expected_tls_args = ["--cacert", env["STUDIO_TLS_CERT"]] if signed_pkg else []
    assert curl_args == [*expected_tls_args, "-sS", "-o", "/dev/null", "-m", "15", "-w", "%{http_code}",
                         "https://studio.example.net/health"]
    assert (tmp_path / "setup.exe").read_bytes().startswith(b"MZ")
    assert (tmp_path / "setup.pkg").read_bytes() == b"xar!fixture"
    assert (tmp_path / "setup.sh").read_bytes().startswith(b"#!/")
    with tarfile.open(tmp_path / 'release.tar') as release_archive:
        assert release_archive.extractfile('deploy/md2pdf.py').read() == converter_bytes
        assert release_archive.extractfile('deploy/md2pdf.sha256').read().decode().strip() == hashlib.sha256(converter_bytes).hexdigest()
    with zipfile.ZipFile(tmp_path / "bridge.zip") as archive:
        names = {n for n in archive.namelist() if not n.endswith("/")}
        assert "studio-agent/install.cmd" in names and "studio-agent/studio_agent/app.py" in names
        assert archive.read('studio-agent/macos/studio-agent').startswith(b'\xca\xfe\xba\xbe')
        descriptor = 'studio-agent/macos/signed-package.json'
        if signed_pkg:
            import json
            assert json.loads(archive.read(descriptor)) == {
                'sha256': hashlib.sha256(b'xar!fixture').hexdigest(), 'size': len(b'xar!fixture')}
        else:
            assert descriptor not in names
        assert b'"https://studio.example.com"' in archive.read("studio-agent/studio_agent/store.py")
        assert b'"https://studio.example.net"' not in archive.read("studio-agent/studio_agent/store.py")
        assert not any(n.startswith("studio-agent/tests/") or n.endswith("CLAUDE.md") for n in names)
        assert all(n.startswith("studio-agent/") for n in names)
        cmd = archive.read("studio-agent/install.cmd")
        assert cmd.count(b"\r\n") == cmd.count(b"\n") > 0
    if not signed_pkg:
        files = ("bridge.zip", "setup.exe", "setup.pkg", "setup.sh")
        before = {name: (tmp_path / name).read_bytes() for name in files}
        env["STUDIO_URL"] = "https://another.example.net:8443"
        r = deploy()
        assert r.returncode == 0, r.stderr
        assert "общий комплект моста из кэша" in r.stdout
        assert {name: (tmp_path / name).read_bytes() for name in files} == before
        args = (tmp_path / "curl-args").read_text(encoding="utf-8").splitlines()
        assert args[-1] == "https://another.example.net:8443/health"
    calls = (tmp_path / 'ssh-calls').read_bytes()
    override = tmp_path / 'md2pdf.py'
    override.write_bytes(b'# unexpected converter\n')
    env['STUDIO_MD2PDF_SOURCE'] = override.as_posix()
    r = deploy()
    assert r.returncode == 1 and 'версия STUDIO_MD2PDF_SOURCE не совпала' in r.stderr
    assert (tmp_path / 'ssh-calls').read_bytes() == calls
    del env['STUDIO_MD2PDF_SOURCE']
    (repo / 'deploy' / 'md2pdf.py').write_bytes(override.read_bytes())
    subprocess.run([*git, 'commit', '-qam', 'corrupt pin'], check=True, timeout=20)
    r = deploy()
    assert r.returncode == 1 and 'версия md2pdf.py не совпала' in r.stderr
    assert (tmp_path / 'ssh-calls').read_bytes() == calls


def _symlink_bash(tmp_path):
    """Git Bash создаёт настоящие ссылки только с MSYS=winsymlinks:nativestrict (иначе — копию)."""
    bash = _bash()
    if not bash:
        pytest.skip("нет bash")
    env = {**os.environ, "MSYS": "winsymlinks:nativestrict"}
    (tmp_path / "probe-target").mkdir()
    probe = subprocess.run([bash, "-c", 'ln -s "$1/probe-target" "$1/probe" && [ -L "$1/probe" ]', "x",
                            tmp_path.as_posix()], env=env, capture_output=True, timeout=20)
    if probe.returncode:
        pytest.skip("нет прав на символические ссылки")
    return bash, env


@pytest.mark.integration                   # bash со ссылками: под нагрузкой набора — больше 1 с
@pytest.mark.parametrize("first", [True, False], ids=["first-install", "update"])
def test_failed_docker_start_never_links_current_to_itself(tmp_path, first):
    """Без `current` `readlink -f` возвращал сам путь с кодом 0: неудачный первый запуск делал `current`
    ссылкой на себя. Неудачное обновление возвращает прежний релиз."""
    bash, env = _symlink_bash(tmp_path)
    root = tmp_path / "r"
    (root / "releases").mkdir(parents=True)
    (root / ".incoming-x").mkdir()
    if not first:
        (root / "releases" / "old").mkdir()
        subprocess.run([bash, "-c", 'ln -s "$1/releases/old" "$1/current"', "x", root.as_posix()], env=env, check=True,
                       timeout=20)
    start = INSTALL_DOCKER.index('release="$R/releases/')
    block = INSTALL_DOCKER[start:INSTALL_DOCKER.index("    exit 1\nfi\n", start) + len("    exit 1\nfi\n")]
    # Первый `up` (новый релиз) не проходит проверку здоровья, повторный (прежний релиз) — проходит.
    stubs = (f"set -euo pipefail\nR={root.as_posix()}; NEW=$R/.incoming-x; REVISION={'a' * 40}\n"
             'docker() { echo "docker $*" >> "$R/calls"; if [ "$6" = up ]; then'
             ' [ -e "$R/failed" ] && return 0; touch "$R/failed"; return 1; fi; }\n')
    r = subprocess.run([bash, "-c", stubs + block], env=env, capture_output=True, text=True, timeout=20)
    assert r.returncode == 1 and "new release failed health checks" in r.stderr, r
    release = next(p for p in (root / "releases").iterdir() if p.name != "old")
    target = release if first else root / "releases" / "old"
    same = subprocess.run([bash, "-c", '[ -d "$1/current" ] && [ "$1/current" -ef "$2" ]',
                           "x", root.as_posix(), target.as_posix()], env=env, timeout=20)
    assert same.returncode == 0, "current → не тот релиз"
    calls = (root / "calls").read_text().splitlines()
    assert ("previous image restored" in r.stderr) is not first
    assert calls[-1].endswith("stop --timeout 20") is first


def _posix(path: Path) -> str:
    """Путь для проверок `^/…` скрипта: в Git Bash — /d/…, а не D:/…."""
    text = path.as_posix()
    return "/" + text[0].lower() + text[2:] if os.name == "nt" and text[1:2] == ":" else text


@pytest.mark.integration                   # настоящий Bash и файловые процессы: > 1 с на Windows
@pytest.mark.parametrize("outcome", ["installed", "unchanged", "empty", "configtest-failed", "reload-failed"])
def test_install_proxy_restores_the_previous_vhost_on_refusal(tmp_path, outcome):
    """Отказ configtest или reload Apache возвращает прежний vhost; успех сохраняет прежний для ручного отката."""
    bash = _bash()
    if not bash:
        pytest.skip("нет bash")
    etc, target, log = tmp_path / "etc", tmp_path / "sites" / "studio.conf", tmp_path / "log"
    etc.mkdir()
    target.parent.mkdir()
    target.write_text("old vhost\n", newline="\n")
    incoming = {"unchanged": "old vhost\n", "empty": ""}.get(outcome, "new vhost\n")
    proxy = (DEPLOY / "install-proxy.sh").read_text(encoding="utf-8").replace("/etc/studio", _posix(etc))
    # Функции сильнее PATH: Apache и systemd не нужны; flock в Git Bash нет. Каждый вызов пишет, какой vhost видел.
    stubs = ('flock() { :; }\n'
             'apache2ctl() { echo "apache2ctl $* $(cat "$target")" >> "$LOG"; [ "$OUTCOME" != configtest-failed ]; }\n'
             'systemctl() { echo "systemctl $* $(cat "$target")" >> "$LOG";'
             ' if [ "$OUTCOME" = reload-failed ] && [ ! -e "$LOG.once" ]; then touch "$LOG.once"; return 1; fi; }\n')
    script = proxy.replace("set -euo pipefail\n", "set -euo pipefail\n" + stubs, 1)
    env = {**os.environ, "LOG": log.as_posix(), "OUTCOME": outcome}
    r = subprocess.run([bash, "-c", script, "install-proxy.sh", _posix(target)], input=incoming.encode(), env=env,
                       capture_output=True, timeout=20)
    stderr = r.stderr.decode("utf-8")
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    previous = etc / "apache-vhost.previous.conf"
    assert r.returncode == (0 if outcome in ("installed", "unchanged") else 1), stderr
    assert target.read_text() == ("new vhost\n" if outcome == "installed" else "old vhost\n")
    assert previous.exists() is (outcome == "installed")
    assert [p.name for p in etc.iterdir() if p.name.startswith("proxy.")] == []      # рабочая папка убрана
    expected = {"installed": ["apache2ctl -t new vhost", "systemctl reload apache2 new vhost"],
                "unchanged": [], "empty": [], "configtest-failed": ["apache2ctl -t new vhost"],
                "reload-failed": ["apache2ctl -t new vhost", "systemctl reload apache2 new vhost",
                                  "systemctl reload apache2 old vhost"]}[outcome]
    assert calls == expected, calls
    if outcome == "installed":
        assert previous.read_text() == "old vhost\n"
    if outcome == "configtest-failed":
        assert "proxy config rejected; previous file restored" in stderr
    if outcome == "reload-failed":
        assert "proxy reload failed; previous file restored" in stderr


@pytest.mark.integration                   # настоящий Bash: > 1 с на Windows под нагрузкой набора
@pytest.mark.parametrize("empty", ["studio-agent.zip", "studio-agent-setup.exe", "studio-agent-setup.pkg",
                                   "studio-agent-setup.sh"])
def test_docker_install_refuses_an_empty_bridge_artifact(tmp_path, empty):
    """`[ -s zip ] && [ -s exe ]` под set -e: отказ левой части списка скрипт не завершает — пустой ZIP
    проходил, а мосты переставали получать обновления."""
    bash = _bash()
    if not bash:
        pytest.skip("нет bash")
    agent = tmp_path / "agent"
    agent.mkdir()
    for name in ("studio-agent.zip", "studio-agent-setup.exe", "studio-agent-setup.pkg", "studio-agent-setup.sh"):
        (agent / name).write_bytes(b"" if name == empty else b"x")
    start = INSTALL_DOCKER.index('rm "$probe"\n') + len('rm "$probe"\n')
    block = INSTALL_DOCKER[start:INSTALL_DOCKER.index("version=$(sed", start)]
    script = f"set -euo pipefail\nNEW={tmp_path.as_posix()}\n" + block + "echo unreachable\n"
    r = subprocess.run([bash, "-c", script], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert r.returncode == 1 and "unreachable" not in r.stdout, r
    assert empty in r.stderr


@pytest.mark.integration                   # настоящий Bash: > 1 с на Windows под нагрузкой набора
@pytest.mark.parametrize("current,running,database,fail,backup,code", [
    (True, True, "47", "", True, 0),                # шаг схемы впереди — копия до переключения
    (True, True, "48", "", False, 0),               # схема та же — копии нет
    (True, True, "47", "1", True, 1),               # копия не вышла — выкладка прервана
    (True, True, "unknown", "", False, 1),          # версию базы не прочесть — отказ, а не выкладка вслепую
    (True, False, "47", "", False, 0),              # прежний контейнер не работает — копировать нечем
    (False, False, "47", "", False, 0),             # первая установка
], ids=["schema-step", "same-schema", "backup-failed", "unreadable", "stopped", "first-install"])
def test_docker_install_backs_up_the_database_before_a_schema_step(tmp_path, current, running, database, fail,
                                                                   backup, code):
    """Шаг схемы применяется первым стартом нового образа: испорченные им данные возвращала только ночная копия."""
    bash = _bash()
    if not bash:
        pytest.skip("нет bash")
    root = tmp_path / "r"
    (root / ".incoming-x" / "app").mkdir(parents=True)
    (root / ".incoming-x" / "app" / "db.py").write_text('"""db"""\nVERSION = 48\n', newline="\n")
    if current:
        (root / "current" / "deploy" / "docker").mkdir(parents=True)
        (root / "current" / "deploy" / "docker" / "compose.yaml").write_text("name: studio\n", newline="\n")
    start = INSTALL_DOCKER.index("# A schema step")
    block = INSTALL_DOCKER[start:INSTALL_DOCKER.index('release="$R/releases/', start)]
    stubs = (f"set -euo pipefail\nR={root.as_posix()}; NEW=$R/.incoming-x\n"
             'docker() { echo "$*" >> "$R/calls"; case "$*" in'
             ' *" ps "*) if [ "$RUNNING" = 1 ]; then echo 0123456789ab; fi ;;'
             ' *"PRAGMA user_version"*) echo "$DATABASE" ;;'
             ' *"manage.py backup"*) [ -z "$FAIL" ] ;; esac; }\n')
    env = {**os.environ, "RUNNING": str(int(running)), "DATABASE": database, "FAIL": fail}
    r = subprocess.run([bash, "-c", stubs + block + "echo switched\n"], env=env, capture_output=True, text=True,
                       encoding="utf-8", timeout=20)
    assert r.returncode == code and ("switched" in r.stdout) is (code == 0), r
    calls = (root / "calls").read_text().splitlines() if (root / "calls").exists() else []
    assert any("manage.py backup" in c for c in calls) is backup, calls
    assert all(c.startswith(f"compose --env-file {root.as_posix()}/current/deploy/docker/host.conf ") for c in calls)
    if fail:
        assert "backup before the schema change failed" in r.stderr


@pytest.mark.integration                   # bash со ссылками: под нагрузкой набора — больше 1 с
def test_docker_rollback_runs_the_previous_release_with_its_own_host_conf(tmp_path):
    """`set -a` у host.conf экспортировал значения нового релиза, а окружение у Compose сильнее `--env-file`:
    откат поднимал прежний образ с новыми STUDIO_BIND_IP и STUDIO_URL. Скрипту нужен только STUDIO_MEDIA_DIR."""
    bash, env = _symlink_bash(tmp_path)
    root = tmp_path / "r"
    new, old = root / ".incoming-x", root / "releases" / "old"
    for folder, ip in ((new, "192.0.2.99"), (old, "192.0.2.15")):
        (folder / "deploy" / "docker").mkdir(parents=True)
        (folder / "deploy" / "docker" / "host.conf").write_text(
            f"STUDIO_URL=https://{ip}\nSTUDIO_MEDIA_DIR=/mnt/media/studio\nSTUDIO_BIND_IP={ip}\n", newline="\n")
    subprocess.run([bash, "-c", 'ln -s "$1/releases/old" "$1/current"', "x", root.as_posix()], env=env, check=True,
                   timeout=20)
    start = INSTALL_DOCKER.index("# This file contains")
    read = INSTALL_DOCKER[start:INSTALL_DOCKER.index('[ "$(findmnt', start)]
    start = INSTALL_DOCKER.index('release="$R/releases/')
    block = INSTALL_DOCKER[start:INSTALL_DOCKER.index("    exit 1\nfi\n", start) + len("    exit 1\nfi\n")]
    # Compose видит только окружение процесса: заглушка пишет его, а не переменные оболочки.
    stubs = (f"set -euo pipefail\nR={root.as_posix()}; NEW=$R/.incoming-x; REVISION={'a' * 40}\n"
             'docker() { echo "$(printenv STUDIO_BIND_IP || echo -) $(printenv STUDIO_URL || echo -) $*" >> "$R/calls";'
             ' if [ "$6" = up ]; then [ -e "$R/failed" ] && return 0; touch "$R/failed"; return 1; fi; }\n')
    script = stubs + read + 'echo "media=$STUDIO_MEDIA_DIR"\n' + block
    r = subprocess.run([bash, "-c", script], env=env, capture_output=True, text=True, timeout=20)
    assert r.returncode == 1 and "previous image restored" in r.stderr, r
    assert "media=/mnt/media/studio" in r.stdout
    calls = (root / "calls").read_text().splitlines()
    assert len(calls) == 2 and all(c.startswith("- - compose --env-file ") for c in calls), calls
    assert calls[1].split()[4] == root.as_posix() + "/current/deploy/docker/host.conf"


@pytest.fixture
def docker_release_root(tmp_path):
    with tempfile.TemporaryDirectory(dir=tmp_path) as folder:
        yield Path(folder)


@pytest.mark.integration                   # bash со ссылками: под нагрузкой набора — больше 1 с
@pytest.mark.parametrize('cleanup_failure', ['', 'builder prune', 'image rm'])
def test_docker_install_keeps_current_previous_and_cleans_orphan_images(docker_release_root, cleanup_failure):
    """Сохраняем теги из current/previous, в том числе legacy; старые и бесхозные образы удаляются.
    Ошибка уборки не отменяет здоровую выкладку, остальные приложения не затрагиваются."""
    bash = _bash()
    if not bash:
        pytest.skip('нет bash')
    root = docker_release_root
    revs = [f"{n:x}" * 40 for n in range(1, 9)]
    names = [f"{rev}-2026110{n}T120000" for n, rev in enumerate(revs, start=1)]
    images = [f'studio:{rev}' for rev in revs]
    images[-1] = f'arcane.local/studio:0.18.3-{revs[-1]}'
    for name, image in zip(names, images):
        config = root / 'releases' / name / 'deploy/docker/host.conf'
        config.parent.mkdir(parents=True)
        config.write_text(f'STUDIO_IMAGE={image}\n')
    orphan = 'arcane.local/studio:0.18.2-' + 'f' * 40  # нет каталога релиза: prepare-only или неудачная выкладка
    legacy = ['studio:development', 'studio:migration-preflight']
    inventory = '\n'.join([*images, orphan, *legacy, 'other:latest'])
    (root / 'images').write_text(inventory, newline='\n')
    (root / 'releases' / 'unmanaged').mkdir()
    for target, name in ((names[7], "current"), (names[0], "previous")):
        if os.name == 'nt':
            import _winapi
            _winapi.CreateJunction(str(root / 'releases' / target), str(root / name))
        else:
            (root / name).symlink_to(root / 'releases' / target, target_is_directory=True)
    start = INSTALL_DOCKER.index("prune_releases() {")
    block = INSTALL_DOCKER[start:INSTALL_DOCKER.index("\nprintf 'studio: Docker release", start) + 1]
    stubs = (f"set -euo pipefail\nR={root.as_posix()}\n"
             'docker() { echo "$*" >> "$R/calls"; if [ "$1 $2" = "image ls" ]; then cat "$R/images"; fi; '
             '[ "$1 $2" != "$FAIL" ]; }\n')
    r = subprocess.run([bash, "-c", stubs + block + "echo deployed\n"], env={**os.environ, 'FAIL': cleanup_failure},
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 0 and "deployed" in r.stdout and "removed 6 old releases" in r.stdout, r
    assert sorted(p.name for p in (root / "releases").iterdir()) == sorted([names[0], names[-1], 'unmanaged'])
    assert (root / "calls").read_text().splitlines() == [
        "image ls --filter reference=arcane.local/studio:* --filter reference=studio:* --format {{.Repository}}:{{.Tag}}",
        *[f'image rm {image}' for image in [*images[1:-1], orphan, *legacy]],
        "image prune -f --filter label=org.opencontainers.image.title=Studio --filter until=168h",
        "builder prune -f --filter until=168h"]
    assert ("cleanup of old releases or images failed" in r.stderr) is bool(cleanup_failure)


@pytest.mark.integration
@pytest.mark.parametrize('invalid', ['missing', 'studio:invalid'])
def test_docker_cleanup_preserves_everything_if_rollback_image_is_unknown(docker_release_root, invalid):
    bash = _bash()
    if not bash:
        pytest.skip('нет bash')
    root = docker_release_root
    config = root / 'current/deploy/docker/host.conf'
    config.parent.mkdir(parents=True)
    if invalid != 'missing':
        config.write_text(f'STUDIO_IMAGE={invalid}\n')
    old = root / 'releases' / ('a' * 40 + '-20261101T120000')
    old.mkdir(parents=True)
    start = INSTALL_DOCKER.index('prune_releases() {')
    block = INSTALL_DOCKER[start:INSTALL_DOCKER.index("\nprintf 'studio: Docker release", start)]
    stubs = f'R={root.as_posix()}\ndocker() {{ echo unsafe; }}\n'
    result = subprocess.run([bash, '-c', stubs + block], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0 and 'cleanup of old releases or images failed' in result.stderr
    assert 'unsafe' not in result.stdout and old.is_dir()


@pytest.mark.integration
def test_docker_build_uses_application_version_in_tag_and_metadata(tmp_path):
    bash = _bash()
    if not bash:
        pytest.skip('нет bash')
    app = tmp_path / 'app'
    app.mkdir()
    version_file = app / 'release.py'
    start = INSTALL_DOCKER.index('version=$(sed')
    block = INSTALL_DOCKER[start:INSTALL_DOCKER.index('\ndocker compose', start)]
    stubs = f'NEW={tmp_path.as_posix()}; REVISION={"a" * 40}; REFRESH_BASE=0\ndocker() {{ printf "%s\\n" "$*"; }}\n'
    (tmp_path / 'deploy/docker').mkdir(parents=True)
    version_file.write_text('VERSION = "0.18.3"\n')
    result = subprocess.run([bash, '-c', stubs + block], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    image = 'arcane.local/studio:0.18.3-' + 'a' * 40
    assert '--build-arg STUDIO_VERSION=0.18.3' in result.stdout and f'-t {image}' in result.stdout
    assert (tmp_path / 'deploy/docker/host.conf').read_text().strip() == f'STUDIO_IMAGE={image}'
    version_file.write_text('VERSION = "invalid"\n')
    result = subprocess.run([bash, '-c', stubs + block], capture_output=True, text=True, timeout=20)
    assert result.returncode == 1 and 'invalid application version' in result.stderr
    assert 'build --pull' not in result.stdout


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


def test_apache_lets_dialog_media_through_only_on_its_route():
    """Звук и видео диалога для MCP (`POST /api/chats/<id>/media`) — до mcp_files.MEDIA_MAX телом запроса: свой
    LocationMatch, общий предел меньше; nginx контейнера пропускает не меньше."""
    import mcp_files
    v = uncommented(VHOST)
    block = re.search(r'<LocationMatch "\^/api/chats/\[\^/\]\+/media\$">(.*?)</LocationMatch>', v, re.S)
    assert block, "нет LocationMatch медиа диалога"
    limit = int(re.search(r"LimitRequestBody (\d+)", block.group(1)).group(1))
    assert mcp_files.MEDIA_MAX <= limit <= mcp_files.MEDIA_MAX + (1 << 20)
    assert re.search(r"^/api/chats/[^/]+/media$", "/api/chats/" + "a" * 32 + "/media")
    assert not re.search(r"^/api/chats/[^/]+/media$", "/api/chats/" + "a" * 32 + "/messages")
    general = int(re.search(r"^\s*LimitRequestBody (\d+)", v, re.M).group(1))
    assert general < mcp_files.MEDIA_MAX


def test_apache_lets_my_files_chunks_through_only_on_the_upload_route():
    """Кусок «Моих файлов» (`PUT /api/my-files/<sid>/upload`) — до my_files.CHUNK телом запроса: свой LocationMatch
    на 33 МиБ + 1 МиБ, общий предел меньше; nginx контейнера пропускает не меньше (общий тест выше)."""
    import my_files
    v = uncommented(VHOST)
    block = re.search(r'<LocationMatch "\^/api/my-files/\[\^/\]\+/upload\$">(.*?)</LocationMatch>', v, re.S)
    assert block, "нет LocationMatch загрузки «Моих файлов»"
    limit = int(re.search(r"LimitRequestBody (\d+)", block.group(1)).group(1))
    assert limit == (33 << 20) + (1 << 20) and limit > my_files.CHUNK
    assert re.search(r"^/api/my-files/[^/]+/upload$", "/api/my-files/12/upload")
    assert not re.search(r"^/api/my-files/[^/]+/upload$", "/api/my-files/12/list")
    general = int(re.search(r"^\s*LimitRequestBody (\d+)", v, re.M).group(1))
    assert general < my_files.CHUNK
