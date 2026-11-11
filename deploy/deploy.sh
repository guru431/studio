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
# EXE установки и ZIP обновления моста — agent/ того же HEAD студии.
# EXE собирается на Windows до первого SSH; оба файла откатываются вместе с кодом.
set -euo pipefail
D=$(cd "$(dirname "$0")/.." && pwd)
PROFILE=${STUDIO_DEPLOY_PROFILE:-$D/deploy/local-profile.sh}
if [ -f "$PROFILE" ]; then
    set -a
    # shellcheck source=/dev/null
    . "$PROFILE"
    set +a
fi
# Keep Debian paths intact when Git Bash starts native Windows helpers. Local
# file arguments still need normal MSYS conversion; preserve caller exclusions.
export MSYS2_ENV_CONV_EXCL="${MSYS2_ENV_CONV_EXCL:+$MSYS2_ENV_CONV_EXCL;}STUDIO_REMOTE_ROOT;STUDIO_MEDIA_DIR;STUDIO_CERT_DIR;STUDIO_PROXY_VHOST"
HOST=${STUDIO_SSH:-deploy@10.77.1.10}
PORT=${STUDIO_SSH_PORT:-22222}
KEY=${STUDIO_SSH_KEY:-$HOME/.ssh/studio_deploy_key}
VAULT=${STUDIO_VAULT:-/c/work/projects/local-config/secrets/vault.env}
URL=${STUDIO_URL:-https://studio.example.com}
R=${STUDIO_REMOTE_ROOT:-/opt/services/studio}
PARTS=(app deploy requirements.txt)
RUNTIME=${STUDIO_RUNTIME:-systemd}
case "$RUNTIME" in systemd|docker) ;; *) echo 'deploy: invalid STUDIO_RUNTIME' >&2; exit 1 ;; esac
if [ "$RUNTIME" = docker ]; then PARTS+=(.dockerignore); fi
SSH=(ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=20 -p "$PORT" "$HOST")

die() { echo "deploy: $*" >&2; exit 1; }
NEW_SECRET=0
CREATE_ADMIN=0
PREPARE=0
for arg in "$@"; do
    case "$arg" in
        --new-secret) NEW_SECRET=1 ;;
        --create-admin) CREATE_ADMIN=1 ;;
        --prepare-only) PREPARE=1 ;;
        *) die "неизвестный аргумент: $arg (есть --new-secret и --create-admin)" ;;
    esac
done
if [ "$PREPARE" = 1 ] && [ "$RUNTIME" != docker ]; then die '--prepare-only requires Docker'; fi
if [ "$RUNTIME" = docker ]; then
    [ -n "${STUDIO_BACKEND_URL:-}" ] || die 'Docker requires STUDIO_BACKEND_URL'
    [ -n "${STUDIO_PROXY_SSH:-}" ] || die 'Docker requires STUDIO_PROXY_SSH'
    [ -n "${STUDIO_PROXY_VHOST:-}" ] || die 'Docker requires STUDIO_PROXY_VHOST'
fi

git -C "$D" cat-file -e HEAD:agent/install.cmd 2>/dev/null \
    || die "нет agent/install.cmd в HEAD студии — архив моста едет вместе со студией"
[ -z "$(git -C "$D" status --porcelain -- "${PARTS[@]}" agent)" ] \
    || die "незакоммиченные изменения в ${PARTS[*]} agent — выкладывается только закоммиченный HEAD"

PYTHON=${STUDIO_PYTHON:-$D/.venv/bin/python}
if [ ! -x "$PYTHON" ]; then PYTHON=$D/.venv/Scripts/python.exe; fi
[ -x "$PYTHON" ] || die "нет Python среды проекта; задайте STUDIO_PYTHON"
# Validate the profile before building an installer or contacting the host.
"$PYTHON" "$D/deploy/render-profile.py" --check
MD2PDF_SOURCE=${STUDIO_MD2PDF_SOURCE:-$HOME/.claude/bin/md2pdf.py}
[ -s "$MD2PDF_SOURCE" ] || die "нет общего md2pdf.py; задайте STUDIO_MD2PDF_SOURCE"
md2pdf_expected=$(git -C "$D" show HEAD:deploy/md2pdf.sha256 | tr -d '\r\n')
md2pdf_actual=$(sha256sum "$MD2PDF_SOURCE" | cut -d' ' -f1)
[ "$md2pdf_actual" = "$md2pdf_expected" ] || die "версия md2pdf.py не совпала с deploy/md2pdf.sha256"

package=$(mktemp -d)
trap 'rm -rf -- "$package"' EXIT
git -C "$D" -c core.autocrlf=false archive --format=zip --prefix=studio-agent/ HEAD:agent > "$package/studio-agent.zip"
"$PYTHON" "$D/deploy/render-profile.py" --agent "$package/studio-agent.zip"
git -C "$D" show HEAD:deploy/build-agent-installer.ps1 > "$package/build-agent-installer.ps1"
git -C "$D" show HEAD:deploy/agent-installer.cs > "$package/agent-installer.cs"
echo "==> сборка установщика Windows"
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass \
    -File "$(cygpath -w "$package/build-agent-installer.ps1")" \
    -Archive "$(cygpath -w "$package/studio-agent.zip")" \
    -Output "$(cygpath -w "$package/studio-agent-setup.exe")"

echo "==> код $(git -C "$D" rev-parse --short HEAD) → $HOST:$R"
"${SSH[@]}" "sudo install -d -m 0755 $R && sudo rm -rf $R/.incoming && sudo install -d -o root -g root -m 0700 $R/.incoming"
git -C "$D" -c core.autocrlf=false archive --format=tar HEAD "${PARTS[@]}" \
    | "$PYTHON" "$D/deploy/render-profile.py" \
    | "${SSH[@]}" "sudo tar -xf - -C $R/.incoming"
# Shared converter is delivered byte-for-byte, like the generated bridge artifacts.
"${SSH[@]}" "sudo sh -c 'cat > $R/.incoming/deploy/md2pdf.py'" < "$MD2PDF_SOURCE"
echo "==> мост $(git -C "$D" rev-parse --short HEAD) → ZIP и установщик EXE"
"${SSH[@]}" "sudo install -d -m 0755 $R/.incoming/agent"
for file in studio-agent.zip studio-agent-setup.exe; do
    "${SSH[@]}" "sudo sh -c 'cat > $R/.incoming/agent/$file'" < "$package/$file"
done

if [ "$NEW_SECRET" = 0 ] && ! "${SSH[@]}" "sudo grep -qs '^STUDIO_DATA_KEY=' /etc/studio/studio.env"; then
    NEW_SECRET=1        # первая установка или studio.env прежней студии (подпись сессии)
fi
args=()
installer=install.sh
if [ "$RUNTIME" = docker ]; then
    installer=install-docker.sh
    args+=("--revision=$(git -C "$D" rev-parse HEAD)")
    if [ "$PREPARE" = 1 ]; then args+=(--prepare-only); fi
fi
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
} | tr -d '\r' | "${SSH[@]}" "sudo bash $R/.incoming/deploy/$installer ${args[*]:-}" \
    || die "установка не прошла (вывод install.sh — выше)"

if [ "$PREPARE" = 1 ]; then exit 0; fi
if [ "$RUNTIME" = docker ]; then
    PROXY_SSH=(ssh -T -i "$KEY" -o BatchMode=yes -o ConnectTimeout=20
               -p "${STUDIO_PROXY_SSH_PORT:-$PORT}" "$STUDIO_PROXY_SSH")
    "${PROXY_SSH[@]}" 'sudo install -d -m 0700 /etc/studio'
    git -C "$D" show HEAD:deploy/install-proxy.sh \
        | "${PROXY_SSH[@]}" "sudo sh -c 'umask 077; cat > /etc/studio/install-proxy.sh'"
    git -C "$D" -c core.autocrlf=false archive --format=tar HEAD deploy/apache-vhost.conf \
        | "$PYTHON" "$D/deploy/render-profile.py" \
        | tar -xOf - deploy/apache-vhost.conf \
        | "${PROXY_SSH[@]}" "sudo bash /etc/studio/install-proxy.sh $STUDIO_PROXY_VHOST"
fi

echo "==> снаружи через Apache: $URL/health"
code=$(curl -s -o /dev/null -m 15 -w '%{http_code}' "$URL/health" || true)
[ "$code" = 200 ] || die "$URL/health ответил ${code:-нет ответа}: vhost не подключён в apache2.conf или нет reload Apache"
echo "==> готово: $URL/"
