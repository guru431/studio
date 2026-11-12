#!/bin/bash
# Установка выкладки на debian — её запускает deploy/deploy.sh:
#   sudo bash /opt/services/studio/.incoming-<ревизия>-…/deploy/install.sh [--secret-stdin] [--admin-stdin]
# Код и .venv — root: служба (пользователь studio) их только читает и исполняет. Секрет —
# /etc/studio/studio.env, 0600 root: его читает systemd при старте, а не служба.
# --secret-stdin — первая строка stdin `STUDIO_DATA_KEY=…` (ключ Fernet из vault) ложится в
#   studio.env: первая установка или смена ключа (со сменой прежние токены подключений в базе
#   не расшифровать — их придётся ввести заново).
# --admin-stdin — остаток stdin (строки STUDIO_ADMIN_LOGIN=, STUDIO_ADMIN_PASSWORD=,
#   GATEWAY_KEY_OWNER_IMAGES=) уходит в `manage.py create-admin` от studio с окружением службы:
#   первый администратор и его подключение «srvllm». Повтор ничего не ломает.
# Значения в argv не попадают и не печатаются: только поток stdin.
set -euo pipefail
R=/opt/services/studio
NEW=$(readlink -f "$(dirname "$(readlink -f "$0")")/..")
OLD=$R/.previous
ENV_FILE=/etc/studio/studio.env
UNIT=studio-web.service
SOCK=/run/studio/web.sock
MEDIA=/mnt/storage/SharedData/studio
PARTS=(app deploy requirements.txt constraints.txt)

SECRET=0
ADMIN=0
for arg in "$@"; do
    case "$arg" in
        --secret-stdin) SECRET=1 ;;
        --admin-stdin) ADMIN=1 ;;
        *) echo "ОШИБКА: неизвестный аргумент $arg (есть --secret-stdin и --admin-stdin)" >&2; exit 1 ;;
    esac
done
# Своя папка загрузки каждой выкладки (deploy.sh) и одна установка за раз: параллельные выкладки не
# подменяют код друг другу посреди переезда.
case "$NEW" in "$R"/.incoming-*) ;; *) echo "ОШИБКА: install.sh запускают из $R/.incoming-* (deploy.sh)" >&2; exit 1 ;; esac
exec 9>"$R/.deploy.lock"
flock -n 9 || { echo "ОШИБКА: уже идёт другая выкладка" >&2; exit 1; }

getent passwd studio >/dev/null \
    || useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin studio
install -d -o root -g root -m 0700 /etc/studio
if [ "$SECRET" = 1 ]; then
    IFS= read -r line || true
    if ! [[ $line =~ ^STUDIO_DATA_KEY=[A-Za-z0-9_-]{43}=$ ]]; then
        echo "ОШИБКА: первая строка stdin — не STUDIO_DATA_KEY=<ключ Fernet, 44 знака base64url> — $ENV_FILE не тронут" >&2
        exit 1
    fi
    (umask 077
     printf '%s\n' "$line" > "$ENV_FILE.part"
     if [ -f "$ENV_FILE" ]; then
         grep -m1 '^STUDIO_CLEANER_CA=' "$ENV_FILE" >> "$ENV_FILE.part" || true
     fi)
    mv -f "$ENV_FILE.part" "$ENV_FILE"
    unset line
fi
if [ "$ADMIN" = 1 ]; then
    # Остаток stdin — сразу в память: дальше pip, sudo и systemctl его уже не прочтут
    admin_lines=$(cat)
    [ -n "$admin_lines" ] || { echo "ОШИБКА: --admin-stdin без строк на stdin" >&2; exit 1; }
fi
# Only the data key and an optional absolute path to the public Cleaner certificate are accepted.
# The CA file lives outside /etc/studio: that secrets directory is intentionally root-only.
if ! grep -qE '^STUDIO_DATA_KEY=[A-Za-z0-9_-]{43}=$' "$ENV_FILE" 2>/dev/null \
        || [ "$(grep -c '^STUDIO_DATA_KEY=' "$ENV_FILE")" -ne 1 ] \
        || [ "$(grep -c '^STUDIO_CLEANER_CA=' "$ENV_FILE" || true)" -gt 1 ] \
        || grep -qvE '^STUDIO_DATA_KEY=[A-Za-z0-9_-]{43}=$|^STUDIO_CLEANER_CA=/[A-Za-z0-9_./-]+$' "$ENV_FILE"; then
    echo "ОШИБКА: $ENV_FILE — одна STUDIO_DATA_KEY=… и необязательная STUDIO_CLEANER_CA=/путь/к/сертификату" >&2
    exit 1
fi
# Файлы истории — на шаре storage. Не смонтирована или нет прав — до подмены кода, прежний работает.
# Проверка — настоящей записью: у cifs с noperm `test -w` (access(2)) верен при любых правах на
# сервере, а каталог с тем же путём на диске debian прошёл бы его и без шары.
probe="$MEDIA/.install-probe-$$"
if ! findmnt -n -o FSTYPE -T "$MEDIA" 2>/dev/null | grep -qx cifs \
        || ! sudo -u studio touch "$probe" || ! sudo -u studio rm -f "$probe"; then
    echo "ОШИБКА: учётке studio не писать на шару в $MEDIA — смонтирована ли /mnt/storage" \
         "(//10.77.1.5/dd\$ в /etc/fstab: mount /mnt/storage) и есть ли каталог SharedData/studio?" >&2
    exit 1
fi

# Звук студии (расшифровка, озвучка) — ffmpeg debian; до подмены кода: не встал — работает прежний
command -v ffmpeg >/dev/null     || DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ffmpeg >/dev/null
# The shared Markdown-to-PDF CLI uses Chromium; install before swapping the running app.
command -v chromium >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends chromium fonts-dejavu-core >/dev/null
[ -s "$NEW/deploy/md2pdf.py" ] || {
    echo "ОШИБКА: нет общего md2pdf.py — deploy.sh должен доставить конвертер" >&2
    exit 1
}
[ "$(sha256sum "$NEW/deploy/md2pdf.py" | cut -d' ' -f1)" = "$(tr -d '\r\n' < "$NEW/deploy/md2pdf.sha256")" ] || {
    echo "ОШИБКА: md2pdf.py не совпал с закреплённой версией — прежний код не тронут" >&2
    exit 1
}
# Субтитры YouTube в чате — общий yt-dlp debian (root обновляет его ночью из /etc/crontab): своей копии нет,
# поэтому без него только предупреждение, выкладка идёт дальше
command -v yt-dlp >/dev/null     || echo "ВНИМАНИЕ: нет yt-dlp — чтение YouTube в чате недоступно" >&2
# ZIP обновления моста и установщик EXE (GET /api/agent/download) кладёт deploy.sh; без них подмена agent/ оборвалась бы
# посреди переезда кода — проверка до неё
[ -s "$NEW/agent/studio-agent.zip" ] || {
    echo "ОШИБКА: нет $NEW/agent/studio-agent.zip — deploy.sh собирает его из agent/ студии" >&2
    exit 1
}
[ -s "$NEW/agent/studio-agent-setup.exe" ] || {
    echo "ОШИБКА: нет $NEW/agent/studio-agent-setup.exe — deploy.sh собирает его на Windows" >&2
    exit 1
}

# Среда Python — своя у новой версии, до подмены кода: pip не меняет пакеты работающей службы.
# Собирается в $NEW/.venv, только если сменились requirements.txt, constraints.txt или python3 (штамп в самой
# среде; не сменились — действующая остаётся, её никто не трогал), и переезжает на место вместе с
# кодом; прежняя уходит в $OLD, откат возвращает и её. Упал pip — недособранная среда убрана,
# прежние код и среда не тронуты. Переезд ломает абсолютный shebang скриптов bin/ — поэтому служба,
# копия базы и manage.py запускаются интерпретатором среды (`.venv/bin/python -m …`), pip — тоже.
STAMP=.studio-requirements
want="$(cat "$NEW/requirements.txt" "$NEW/constraints.txt" | sha256sum | cut -d' ' -f1) $(python3 -V 2>&1)"
SWAP=("${PARTS[@]}" agent)      # архив моста (agent/) переезжает и откатывается вместе с кодом
PY=$R/.venv/bin/python
if ! [ -x "$PY" ] || [ "$(cat "$R/.venv/$STAMP" 2>/dev/null)" != "$want" ]; then
    rm -rf "$NEW/.venv"
    python3 -m venv "$NEW/.venv"
    if ! "$NEW/.venv/bin/python" -m pip install --quiet --disable-pip-version-check --root-user-action=ignore \
            -r "$NEW/requirements.txt" -c "$NEW/constraints.txt"; then
        rm -rf "$NEW/.venv"
        echo "ОШИБКА: pip не поставил зависимости — прежние код и среда не тронуты" >&2
        exit 1
    fi
    printf '%s\n' "$want" > "$NEW/.venv/$STAMP"
    SWAP+=(.venv)
    PY=$NEW/.venv/bin/python
fi
"$PY" -m compileall -q "$NEW/app"     # __pycache__ служба сама не запишет
chown -R root:root "$NEW"
chmod -R u=rwX,go=rX "$NEW"
rm -rf "$OLD"
install -d -o root -g root -m 0700 "$OLD"
for p in "${SWAP[@]}"; do       # убранные из репозитория файлы уходят вместе с прежним каталогом
    if [ -e "$R/$p" ]; then mv "$R/$p" "$OLD/"; fi
    mv "$NEW/$p" "$R/$p"
done
rm -rf "$NEW"

restore() {     # прежние дерево и среда — на место новых (первая установка: прежних нет)
    for p in "${SWAP[@]}"; do
        rm -rf "${R:?}/$p"
        if [ -e "$OLD/$p" ]; then mv "$OLD/$p" "$R/$p"; fi
    done
}
# Vhost Apache включает прямо из этого дерева: сломанный отказал бы не сейчас, а при следующем
# старте Apache (перезагрузка debian) — и вместе со всеми vhost хоста. Проверка — до перезапуска.
if ! apache_test=$(apache2ctl -t 2>&1); then
    printf '%s\n' "$apache_test" >&2
    restore
    echo "ОШИБКА: Apache не проходит configtest (вывод выше; сломан может быть и чужой vhost) —" \
         "прежний код возвращён, $UNIT не перезапускалась" >&2
    exit 1
fi

install -m 0644 "$R/deploy/$UNIT" "/etc/systemd/system/$UNIT"
install -m 0644 "$R/deploy/studio-backup.service" "$R/deploy/studio-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --quiet "$UNIT"      # --quiet убирает «Created symlink», ошибки остаются видны
service_failed() {      # $1 — что не прошло; прежняя служба уже остановлена, новый код на месте
    # status упавшей службы (failed, auto-restart) возвращает 3: под pipefail диагностика не должна
    # ронять скрипт раньше журнала и подсказки
    echo "ОШИБКА: $1" >&2
    systemctl status "$UNIT" --no-pager 2>&1 | tail -n 15 >&2 || true
    journalctl -u "$UNIT" -n 20 --no-pager >&2 || true
    echo "Откат: прежние код, юнит и (если менялась) среда Python — в $OLD:" >&2
    echo "  for p in ${SWAP[*]}; do rm -rf $R/\$p; [ ! -e $OLD/\$p ] || mv $OLD/\$p $R/; done" >&2
    echo "  install -m 0644 $R/deploy/$UNIT /etc/systemd/system/$UNIT; systemctl daemon-reload; systemctl restart $UNIT" >&2
    if [ -f "$OLD/deploy/$UNIT" ] && grep -q ' server:app' "$OLD/deploy/$UNIT"; then
        echo "Но это переход со старой студии (server:app): в $ENV_FILE уже только STUDIO_DATA_KEY," \
             "прежняя служба с ним не поднимется — откат здесь значит чинить вперёд." >&2
    fi
    exit 1
}
systemctl restart "$UNIT" || service_failed "systemctl restart $UNIT не прошёл (ошибка выше)"
# Type=simple: restart возвращается раньше, чем uvicorn поднял приложение — ждём ответа.
# От www-data — так к сокету ходит Apache; /health открыт без входа.
code=000
for _ in $(seq 1 40); do
    code=$(sudo -u www-data curl -s -o /dev/null -w '%{http_code}' --unix-socket "$SOCK" \
        http://studio-backend/health 2>/dev/null || true)
    [ "$code" = 200 ] && break
    sleep 0.5
done
[ "$code" = 200 ] || service_failed "$UNIT не ответил 200 на /health за 20 с (от www-data, последний код $code)"
# reload — всегда после проверки службы (configtest пройден выше): сравнение с прежним деревом
# пропустило бы vhost, выложенный неудачной выкладкой того же коммита
systemctl reload apache2
echo "OK: Apache перечитал конфигурацию"
rm -rf "$OLD"
echo "OK: $UNIT отвечает на $SOCK"
if ! systemctl enable --now --quiet studio-backup.timer; then
    echo "ОШИБКА: studio-backup.timer не включён (ошибка выше) — $UNIT обновлена и отвечает," \
         "но копии базы по расписанию не будет; включить: systemctl enable --now studio-backup.timer" >&2
    exit 1
fi
echo "OK: studio-backup.timer — $(systemctl show -p NextElapseUSecRealtime --value studio-backup.timer)"

if [ "$ADMIN" = 1 ]; then
    # Окружение — из юнита службы (одно место правды), ключ данных — из studio.env самим systemd.
    # --pipe: строки администратора — stdin manage.py (printf — встроенная команда, мимо argv).
    printf '%s\n' "$admin_lines" | systemd-run --quiet --collect --pipe --wait --service-type=exec \
        -p User=studio -p Group=www-data -p UMask=0007 \
        -p EnvironmentFile=$ENV_FILE \
        -p "Environment=$(systemctl show -p Environment --value "$UNIT")" \
        "$R/.venv/bin/python" "$R/app/manage.py" create-admin
fi
