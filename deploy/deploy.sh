#!/bin/bash
# Выкладка студии на debian с workstation (Git Bash): закоммиченный HEAD (git archive, не рабочее
# дерево) → /opt/services/studio/.incoming → deploy/install.sh от root (среда, код, юниты, перезапуск,
# проверка сокета, таймер копии базы) → проверка снаружи через Apache.
#   bash deploy/deploy.sh                   обычная выкладка
#   bash deploy/deploy.sh --new-secret      заново положить STUDIO_DATA_KEY из vault (после смены
#                                           в vault; прежние токены подключений не расшифровать)
#   bash deploy/deploy.sh --create-admin    завести или обновить первого администратора:
#                                           STUDIO_ADMIN_LOGIN, STUDIO_ADMIN_PASSWORD и
#                                           GATEWAY_KEY_OWNER_IMAGES (подключение «srvllm») из vault
# Ключ данных при первой установке (в studio.env нет строки STUDIO_DATA_KEY=) тоже едет из vault.
# Значения идут потоком grep | ssh: не печатаются, не ложатся на диск workstation и не попадают в
# argv. Откат — выложить прежний коммит. Vhost (deploy/apache-vhost.conf) подключён в
# /opt/services/apache/apache2.conf строкой IncludeOptional: install.sh проверяет его apache2ctl -t
# (отказ — прежний код на место) и после ответа службы всегда сам делает reload Apache.
set -euo pipefail
D=$(cd "$(dirname "$0")/.." && pwd)
HOST=${STUDIO_SSH:-deploy@10.77.1.10}
PORT=${STUDIO_SSH_PORT:-22222}
KEY=${STUDIO_SSH_KEY:-$HOME/.ssh/studio_deploy_key}
VAULT=${STUDIO_VAULT:-/c/work/projects/local-config/secrets/vault.env}
URL=${STUDIO_URL:-https://studio.example.com}
R=/opt/services/studio
PARTS=(app deploy requirements.txt)
SSH=(ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=20 -p "$PORT" "$HOST")

die() { echo "deploy: $*" >&2; exit 1; }
NEW_SECRET=0
CREATE_ADMIN=0
for arg in "$@"; do
    case "$arg" in
        --new-secret) NEW_SECRET=1 ;;
        --create-admin) CREATE_ADMIN=1 ;;
        *) die "неизвестный аргумент: $arg (есть --new-secret и --create-admin)" ;;
    esac
done
[ -z "$(git -C "$D" status --porcelain -- "${PARTS[@]}")" ] \
    || die "незакоммиченные изменения в ${PARTS[*]} — выкладывается только закоммиченный HEAD"

echo "==> код $(git -C "$D" rev-parse --short HEAD) → $HOST:$R"
"${SSH[@]}" "sudo install -d -m 0755 $R && sudo rm -rf $R/.incoming && sudo install -d -o root -g root -m 0700 $R/.incoming"
git -C "$D" -c core.autocrlf=false archive --format=tar HEAD "${PARTS[@]}" \
    | "${SSH[@]}" "sudo tar -xf - -C $R/.incoming"

if [ "$NEW_SECRET" = 0 ] && ! "${SSH[@]}" "sudo grep -qs '^STUDIO_DATA_KEY=' /etc/studio/studio.env"; then
    NEW_SECRET=1        # первая установка или studio.env прежней студии (подпись сессии)
fi
args=()
if [ "$NEW_SECRET" = 1 ]; then
    args+=(--secret-stdin)
    echo "==> ключ данных из vault (STUDIO_DATA_KEY)"
fi
if [ "$CREATE_ADMIN" = 1 ]; then
    args+=(--admin-stdin)
    echo "==> первый администратор из vault (STUDIO_ADMIN_LOGIN, GATEWAY_KEY_OWNER_IMAGES)"
fi
# Порядок строк — порядок флагов install.sh: первая — ключ данных, остальные — администратору.
{
    if [ "$NEW_SECRET" = 1 ]; then
        grep -m1 '^STUDIO_DATA_KEY=' "$VAULT" || die "нет STUDIO_DATA_KEY в $VAULT"
    fi
    if [ "$CREATE_ADMIN" = 1 ]; then
        grep -E '^(STUDIO_ADMIN_LOGIN|STUDIO_ADMIN_PASSWORD|GATEWAY_KEY_OWNER_IMAGES)=' "$VAULT" \
            || die "нет STUDIO_ADMIN_LOGIN / STUDIO_ADMIN_PASSWORD в $VAULT"
    fi
} | tr -d '\r' | "${SSH[@]}" "sudo bash $R/.incoming/deploy/install.sh ${args[*]:-}" \
    || die "установка не прошла (вывод install.sh — выше)"

echo "==> снаружи через Apache: $URL/health"
code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$URL/health" || true)
[ "$code" = 200 ] || die "$URL/health ответил ${code:-нет ответа}: vhost не подключён в apache2.conf или нет reload Apache"
echo "==> готово: $URL/"
