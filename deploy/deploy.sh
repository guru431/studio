#!/bin/bash
# Выкладка студии на debian с workstation (Git Bash): закоммиченный HEAD (git archive, не рабочее
# дерево) → /opt/services/studio/.incoming → deploy/install.sh от root (среда, код, юнит, перезапуск,
# проверка сокета) → проверка снаружи через Apache.
#   bash deploy/deploy.sh                 обычная выкладка
#   bash deploy/deploy.sh --new-secret    заново положить STUDIO_SESSION_SECRET из vault
#                                         (после смены в vault; гасит все сессии студии)
# Секрет при первой установке тоже едет из vault потоком: значение не печатается и не ложится
# на диск workstation. Откат — выложить прежний коммит. Блок Apache (deploy/apache-location.conf)
# подключён в vhost llm.example.com строкой IncludeOptional — его правка требует reload Apache.
set -euo pipefail
D=$(cd "$(dirname "$0")/.." && pwd)
HOST=${STUDIO_SSH:-deploy@10.77.1.10}
PORT=${STUDIO_SSH_PORT:-22222}
KEY=${STUDIO_SSH_KEY:-$HOME/.ssh/studio_deploy_key}
VAULT=${STUDIO_VAULT:-/c/work/projects/local-config/secrets/vault.env}
URL=${STUDIO_URL:-https://llm.example.com/studio}
R=/opt/services/studio
PARTS=(app deploy requirements.txt)
SSH=(ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=20 -p "$PORT" "$HOST")

die() { echo "deploy: $*" >&2; exit 1; }
case "${1:-}" in
    "") NEW_SECRET=0 ;;
    --new-secret) NEW_SECRET=1 ;;
    *) die "неизвестный аргумент: $1 (есть только --new-secret)" ;;
esac
[ -z "$(git -C "$D" status --porcelain -- "${PARTS[@]}")" ] \
    || die "незакоммиченные изменения в ${PARTS[*]} — выкладывается только закоммиченный HEAD"

echo "==> код $(git -C "$D" rev-parse --short HEAD) → $HOST:$R"
"${SSH[@]}" "sudo install -d -m 0755 $R && sudo rm -rf $R/.incoming && sudo install -d -o root -g root -m 0700 $R/.incoming"
git -C "$D" -c core.autocrlf=false archive --format=tar HEAD "${PARTS[@]}" \
    | "${SSH[@]}" "sudo tar -xf - -C $R/.incoming"

if [ "$NEW_SECRET" = 1 ] || ! "${SSH[@]}" "sudo test -s /etc/studio/studio.env"; then
    echo "==> секрет сессии из vault (STUDIO_SESSION_SECRET)"
    grep -m1 '^STUDIO_SESSION_SECRET=' "$VAULT" | tr -d '\r' \
        | "${SSH[@]}" "sudo bash $R/.incoming/deploy/install.sh --secret-stdin" \
        || die "установка не прошла (нет ли STUDIO_SESSION_SECRET в $VAULT?)"
else
    "${SSH[@]}" "sudo bash $R/.incoming/deploy/install.sh"
fi

echo "==> снаружи через Apache: $URL/health"
code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$URL/health" || true)
[ "$code" = 200 ] || die "$URL/health ответил ${code:-нет ответа}: блок Apache не подключён или выкладка не из LAN"
echo "==> готово: $URL/"
