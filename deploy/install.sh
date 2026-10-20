#!/bin/bash
# Установка выкладки на debian — её запускает deploy/deploy.sh:
#   sudo bash /opt/services/studio/.incoming/deploy/install.sh [--secret-stdin] [--admin-stdin]
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
NEW=$R/.incoming
OLD=$R/.previous
ENV_FILE=/etc/studio/studio.env
UNIT=studio-web.service
SOCK=/run/studio/web.sock
MEDIA=/mnt/storage/SharedData/studio
PARTS=(app deploy requirements.txt)

SECRET=0
ADMIN=0
for arg in "$@"; do
    case "$arg" in
        --secret-stdin) SECRET=1 ;;
        --admin-stdin) ADMIN=1 ;;
        *) echo "ОШИБКА: неизвестный аргумент $arg (есть --secret-stdin и --admin-stdin)" >&2; exit 1 ;;
    esac
done

getent passwd studio >/dev/null \
    || useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin studio
install -d -o root -g root -m 0700 /etc/studio
if [ "$SECRET" = 1 ]; then
    IFS= read -r line || true
    if ! [[ $line =~ ^STUDIO_DATA_KEY=[A-Za-z0-9_-]{43}=$ ]]; then
        echo "ОШИБКА: первая строка stdin — не STUDIO_DATA_KEY=<ключ Fernet, 44 знака base64url> — $ENV_FILE не тронут" >&2
        exit 1
    fi
    (umask 077; printf '%s\n' "$line" > "$ENV_FILE.part")
    mv -f "$ENV_FILE.part" "$ENV_FILE"
    unset line
fi
if [ "$ADMIN" = 1 ]; then
    # Остаток stdin — сразу в память: дальше pip, sudo и systemctl его уже не прочтут
    admin_lines=$(cat)
    [ -n "$admin_lines" ] || { echo "ОШИБКА: --admin-stdin без строк на stdin" >&2; exit 1; }
fi
# studio.env целиком — окружение службы: только ключ данных
if ! grep -qE '^STUDIO_DATA_KEY=.+$' "$ENV_FILE" 2>/dev/null || grep -qvE '^STUDIO_DATA_KEY=' "$ENV_FILE"; then
    echo "ОШИБКА: в $ENV_FILE должна быть одна строка STUDIO_DATA_KEY=… — deploy.sh --new-secret кладёт её из vault" >&2
    exit 1
fi
# Файлы истории — на шаре storage. Не смонтирована или нет прав — до подмены кода, прежний работает.
if ! sudo -u studio test -w "$MEDIA"; then
    echo "ОШИБКА: учётке studio не писать в $MEDIA — смонтирована ли /mnt/storage" \
         "(//10.77.1.5/dd\$ в /etc/fstab: mount /mnt/storage) и есть ли каталог SharedData/studio?" >&2
    exit 1
fi

# Среда — до подмены кода: упади pip, работать останется прежний код
[ -x "$R/.venv/bin/python" ] || python3 -m venv "$R/.venv"
"$R/.venv/bin/pip" install --quiet --disable-pip-version-check --root-user-action=ignore -r "$NEW/requirements.txt"
"$R/.venv/bin/python" -m compileall -q "$NEW/app"     # __pycache__ служба сама не запишет
chown -R root:root "$NEW"
chmod -R u=rwX,go=rX "$NEW"
rm -rf "$OLD"
install -d -o root -g root -m 0700 "$OLD"
for p in "${PARTS[@]}"; do      # убранные из репозитория файлы уходят вместе с прежним каталогом
    if [ -e "$R/$p" ]; then mv "$R/$p" "$OLD/"; fi
    mv "$NEW/$p" "$R/$p"
done
rm -rf "$NEW"

install -m 0644 "$R/deploy/$UNIT" "/etc/systemd/system/$UNIT"
install -m 0644 "$R/deploy/studio-backup.service" "$R/deploy/studio-backup.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null 2>&1
systemctl restart "$UNIT"
# Type=simple: restart возвращается раньше, чем uvicorn поднял приложение — ждём ответа.
# От www-data — так к сокету ходит Apache; /health открыт без входа.
code=000
for _ in $(seq 1 40); do
    code=$(sudo -u www-data curl -s -o /dev/null -w '%{http_code}' --unix-socket "$SOCK" \
        http://studio-backend/health 2>/dev/null || true)
    [ "$code" = 200 ] && break
    sleep 0.5
done
if [ "$code" != 200 ]; then
    echo "ОШИБКА: $UNIT не ответил 200 на /health за 20 с (от www-data, последний код $code)" >&2
    systemctl status "$UNIT" --no-pager 2>&1 | tail -n 15 >&2
    journalctl -u "$UNIT" -n 20 --no-pager >&2
    echo "Прежний код — $OLD: for p in ${PARTS[*]}; do rm -rf $R/\$p; mv $OLD/\$p $R/; done; systemctl restart $UNIT" >&2
    exit 1
fi
rm -rf "$OLD"
echo "OK: $UNIT отвечает на $SOCK"
systemctl enable --now studio-backup.timer >/dev/null 2>&1
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
