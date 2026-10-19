#!/bin/bash
# Установка выкладки на debian — её запускает deploy/deploy.sh:
#   sudo bash /opt/services/studio/.incoming/deploy/install.sh [--secret-stdin]
# Код и .venv — root: служба (пользователь studio) их только читает и исполняет. Секрет —
# /etc/studio/studio.env, 0600 root: его читает systemd при старте, а не служба.
# --secret-stdin — первая строка stdin `STUDIO_SESSION_SECRET=…` (из vault) ложится в studio.env:
# первая установка или смена секрета (смена гасит все сессии студии).
set -euo pipefail
R=/opt/services/studio
NEW=$R/.incoming
OLD=$R/.previous
ENV_FILE=/etc/studio/studio.env
UNIT=studio-web.service
SOCK=/run/studio/web.sock
PARTS=(app deploy requirements.txt)

getent passwd studio >/dev/null \
    || useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin studio
install -d -o root -g root -m 0700 /etc/studio
if [ "${1:-}" = --secret-stdin ]; then
    IFS= read -r line || true
    if ! [[ $line =~ ^STUDIO_SESSION_SECRET=[^[:space:]]{32,}$ ]]; then
        echo "ОШИБКА: на stdin нет строки STUDIO_SESSION_SECRET=… (не короче 32 знаков) — $ENV_FILE не тронут" >&2
        exit 1
    fi
    (umask 077; printf '%s\n' "$line" > "$ENV_FILE.part")
    mv -f "$ENV_FILE.part" "$ENV_FILE"
fi
# studio.env целиком — окружение службы: только подпись сессии
if ! grep -qE '^STUDIO_SESSION_SECRET=.+$' "$ENV_FILE" 2>/dev/null || grep -qvE '^STUDIO_SESSION_SECRET=' "$ENV_FILE"; then
    echo "ОШИБКА: в $ENV_FILE должна быть одна строка STUDIO_SESSION_SECRET=… — deploy.sh кладёт её из vault" >&2
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
systemctl daemon-reload
systemctl enable "$UNIT" >/dev/null 2>&1
systemctl restart "$UNIT"
# Type=simple: restart возвращается раньше, чем uvicorn поднял приложение — ждём ответа.
# От www-data — так к сокету ходит Apache; /studio/health открыт без входа.
code=000
for _ in $(seq 1 40); do
    code=$(sudo -u www-data curl -s -o /dev/null -w '%{http_code}' --unix-socket "$SOCK" \
        http://studio-backend/studio/health 2>/dev/null || true)
    if [ "$code" = 200 ]; then
        rm -rf "$OLD"
        echo "OK: $UNIT отвечает на $SOCK"
        exit 0
    fi
    sleep 0.5
done
echo "ОШИБКА: $UNIT не ответил 200 на /studio/health за 20 с (от www-data, последний код $code)" >&2
systemctl status "$UNIT" --no-pager 2>&1 | tail -n 15 >&2
journalctl -u "$UNIT" -n 20 --no-pager >&2
echo "Прежний код — $OLD: for p in ${PARTS[*]}; do rm -rf $R/\$p; mv $OLD/\$p $R/; done; systemctl restart $UNIT" >&2
exit 1
