#!/bin/bash
# Выкладка студии на debian с workstation (Git Bash): закоммиченный HEAD (git archive, не рабочее
# дерево) → /opt/services/studio/.incoming-<ревизия>-… → deploy/install.sh от root (среда, код, юниты, перезапуск,
# проверка сокета, таймер копии базы) → проверка снаружи через Apache.
#   bash deploy/deploy.sh                   обычная выкладка
#   bash deploy/deploy.sh --new-secret      заново положить STUDIO_DATA_KEY из vault (после смены
#                                           в vault; прежние токены подключений не расшифровать)
#   bash deploy/deploy.sh --create-admin    завести или обновить первого администратора:
#                                           STUDIO_ADMIN_LOGIN, STUDIO_ADMIN_PASSWORD и
#                                           GATEWAY_KEY_OWNER_IMAGES (подключение «srvllm») из vault
#   bash deploy/deploy.sh --refresh-base    Docker: собрать образ заново без кэша (свежие база и
#                                           пакеты Debian сразу, не дожидаясь новой недели)
# Ключ данных при первой установке (в studio.env нет строки STUDIO_DATA_KEY=) тоже едет из vault.
# Значения идут потоком grep | ssh: не печатаются, не ложатся на диск workstation и не попадают в
# argv. Откат — выложить прежний коммит. Vhost (deploy/apache-vhost.conf) подключён в
# /opt/services/apache/apache2.conf строкой IncludeOptional: install.sh проверяет его apache2ctl -t
# (отказ — прежний код на место) и после ответа службы всегда сам делает reload Apache.
# EXE установки и ZIP обновления моста — agent/ того же HEAD студии.
# EXE собирается на Windows, PKG — на Mac из локального профиля; файлы откатываются вместе с кодом.
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
export MSYS2_ENV_CONV_EXCL="${MSYS2_ENV_CONV_EXCL:+$MSYS2_ENV_CONV_EXCL;}STUDIO_REMOTE_ROOT;STUDIO_MEDIA_DIR;STUDIO_CERT_DIR;STUDIO_PROXY_VHOST;STUDIO_MACOS_BUILD_ROOT;STUDIO_MACOS_CAPTURE_PROFILE"
HOST=${STUDIO_SSH:-deploy@10.77.1.10}
PORT=${STUDIO_SSH_PORT:-22222}
KEY=${STUDIO_SSH_KEY:-$HOME/.ssh/studio_deploy_key}
VAULT=${STUDIO_VAULT:-/c/work/projects/local-config/secrets/vault.env}
URL=${STUDIO_URL:-https://studio.example.com}
R=${STUDIO_REMOTE_ROOT:-/opt/services/studio}
# docs/guide.ru.md — руководство Studio для «Справки» (help.py)
PARTS=(app deploy requirements.txt constraints.txt docs/guide.ru.md)
RUNTIME=${STUDIO_RUNTIME:-systemd}
case "$RUNTIME" in systemd|docker) ;; *) echo 'deploy: invalid STUDIO_RUNTIME' >&2; exit 1 ;; esac
if [ "$RUNTIME" = docker ]; then PARTS+=(.dockerignore); fi
SSH=(ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=20 -p "$PORT" "$HOST")

die() { echo "deploy: $*" >&2; exit 1; }
curl_tls=()
if [ -n "${STUDIO_TLS_CERT:-}" ]; then
    [ -f "$STUDIO_TLS_CERT" ] && [ -r "$STUDIO_TLS_CERT" ] && [ -s "$STUDIO_TLS_CERT" ] \
        || die 'STUDIO_TLS_CERT must point to a readable CA file'
    curl_tls+=(--cacert "$STUDIO_TLS_CERT")
fi
NEW_SECRET=0
CREATE_ADMIN=0
PREPARE=0
REFRESH_BASE=0
for arg in "$@"; do
    case "$arg" in
        --new-secret) NEW_SECRET=1 ;;
        --create-admin) CREATE_ADMIN=1 ;;
        --prepare-only) PREPARE=1 ;;
        --refresh-base) REFRESH_BASE=1 ;;
        *) die "неизвестный аргумент: $arg (есть --new-secret, --create-admin, --prepare-only, --refresh-base)" ;;
    esac
done
if [ "$PREPARE" = 1 ] && [ "$RUNTIME" != docker ]; then die '--prepare-only requires Docker'; fi
if [ "$REFRESH_BASE" = 1 ] && [ "$RUNTIME" != docker ]; then die '--refresh-base requires Docker'; fi
if [ "$RUNTIME" = docker ]; then
    [ -n "${STUDIO_BACKEND_URL:-}" ] || die 'Docker requires STUDIO_BACKEND_URL'
    [ -n "${STUDIO_PROXY_SSH:-}" ] || die 'Docker requires STUDIO_PROXY_SSH'
    [ -n "${STUDIO_PROXY_VHOST:-}" ] || die 'Docker requires STUDIO_PROXY_VHOST'
fi

# Ревизия разрешается один раз: коммит параллельной сессии во время выкладки не смешает в ней мост,
# код и тег образа из разных ревизий.
REV=$(git -C "$D" rev-parse --verify 'HEAD^{commit}')
git -C "$D" cat-file -e "$REV:agent/install.cmd" 2>/dev/null \
    || die "нет agent/install.cmd в HEAD студии — архив моста едет вместе со студией"
[ -z "$(git -C "$D" status --porcelain -- "${PARTS[@]}" agent)" ] \
    || die "незакоммиченные изменения в ${PARTS[*]} agent — выкладывается только закоммиченный HEAD"

PYTHON=${STUDIO_PYTHON:-$D/.venv/bin/python}
if [ ! -x "$PYTHON" ]; then PYTHON=$D/.venv/Scripts/python.exe; fi
[ -x "$PYTHON" ] || die "нет Python среды проекта; задайте STUDIO_PYTHON"
# Validate the profile before building an installer or contacting the host.
"$PYTHON" "$D/deploy/render-profile.py" --check
md2pdf_expected=$(git -C "$D" show "$REV:deploy/md2pdf.sha256" | tr -d '\r\n')
md2pdf_actual=$(git -C "$D" show "$REV:deploy/md2pdf.py" | sha256sum | cut -d' ' -f1) \
    || die "нет deploy/md2pdf.py в HEAD студии"
[ "$md2pdf_actual" = "$md2pdf_expected" ] || die "версия md2pdf.py не совпала с deploy/md2pdf.sha256"
if [ -n "${STUDIO_MD2PDF_SOURCE:-}" ]; then
    [ -s "$STUDIO_MD2PDF_SOURCE" ] || die "нет файла STUDIO_MD2PDF_SOURCE"
    md2pdf_override=$(sha256sum "$STUDIO_MD2PDF_SOURCE" | cut -d' ' -f1)
    [ "$md2pdf_override" = "$md2pdf_expected" ] || die "версия STUDIO_MD2PDF_SOURCE не совпала с закреплённым md2pdf.py"
fi

mkdir -p "$D/.tmp"
package=$(mktemp -d "$D/.tmp/deploy-XXXXXXXX")
# Своя папка загрузки на хосте: параллельная выкладка не очистит и не перемешает её содержимое.
# Установщик её переносит или удаляет; что осталось (отказ, --prepare-only) — убирает выход.
IN=$R/.incoming-${REV:0:12}-$$-$RANDOM
uploaded=0
cleanup() {
    rm -rf -- "$package"
    if [ "$uploaded" = 1 ]; then "${SSH[@]}" "sudo rm -rf $IN" || true; fi
}
trap cleanup EXIT
git -C "$D" show "$REV:deploy/build-agent-bundle.py" > "$package/build-agent-bundle.py"
"$PYTHON" "$package/build-agent-bundle.py" "$D" "$REV" "$package"

echo "==> код ${REV:0:7} → $HOST:$R"
# Чужие загрузки не трогаем независимо от возраста. Выход очищает только успешно созданную свою.
"${SSH[@]}" "sudo install -d -m 0755 $R && sudo mkdir -m 0700 $IN"
uploaded=1
git -C "$D" -c core.autocrlf=false archive --format=tar "$REV" "${PARTS[@]}" \
    | "$PYTHON" "$D/deploy/render-profile.py" \
    | "${SSH[@]}" "sudo tar -xf - -C $IN"
echo "==> мост ${REV:0:7} → ZIP и установщик EXE"
"${SSH[@]}" "sudo install -d -m 0755 $IN/agent"
for file in studio-agent.zip studio-agent-setup.exe studio-agent-setup.pkg studio-agent-setup.sh; do
    if [ "$RUNTIME" = docker ]; then
        expected=$(sha256sum "$package/$file" | cut -d' ' -f1)
        printf -v reuse 'sudo -n bash -c %q -- %q %q %q' '
            source=$1 target=$2 expected=$3
            if [ -f "$source" ] && sha256sum -- "$source" | grep -q "^$expected " &&
               cp --reflink=auto -- "$source" "$target" &&
               sha256sum -- "$target" | grep -q "^$expected "; then printf reused; fi
        ' "$R/current/agent/$file" "$IN/agent/$file" "$expected"
        if [ "$("${SSH[@]}" "$reuse")" = reused ]; then
            echo "==> $file: использован файл на сервере"
            continue
        fi
    fi
    "${SSH[@]}" "sudo sh -c 'cat > $IN/agent/$file'" < "$package/$file"
done

if [ "$NEW_SECRET" = 0 ] && ! "${SSH[@]}" "sudo grep -qs '^STUDIO_DATA_KEY=' /etc/studio/studio.env"; then
    NEW_SECRET=1        # первая установка или studio.env прежней студии (подпись сессии)
fi
args=()
installer=install.sh
if [ "$RUNTIME" = docker ]; then
    installer=install-docker.sh
    args+=("--revision=$REV")
    if [ "$PREPARE" = 1 ]; then args+=(--prepare-only); fi
    if [ "$REFRESH_BASE" = 1 ]; then args+=(--refresh-base); fi
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
        if [ -n "${STUDIO_ADMIN_EMAIL:-}" ]; then
            printf 'STUDIO_ADMIN_EMAIL=%s\n' "$STUDIO_ADMIN_EMAIL"
        else
            grep -m1 '^STUDIO_ADMIN_EMAIL=' "$VAULT" || true
        fi
    fi
} | tr -d '\r' | "${SSH[@]}" "sudo bash $IN/deploy/$installer ${args[*]:-}" \
    || die "установка не прошла (вывод install.sh — выше)"

if [ "$PREPARE" = 1 ]; then exit 0; fi
if [ "$RUNTIME" = docker ]; then
    PROXY_SSH=(ssh -T -i "$KEY" -o BatchMode=yes -o ConnectTimeout=20
               -p "${STUDIO_PROXY_SSH_PORT:-$PORT}" "$STUDIO_PROXY_SSH")
    "${PROXY_SSH[@]}" 'sudo install -d -m 0700 /etc/studio'
    git -C "$D" show "$REV:deploy/install-proxy.sh" \
        | "${PROXY_SSH[@]}" "sudo sh -c 'umask 077; cat > /etc/studio/install-proxy.sh'"
    git -C "$D" -c core.autocrlf=false archive --format=tar "$REV" deploy/apache-vhost.conf \
        | "$PYTHON" "$D/deploy/render-profile.py" \
        | tar -xOf - deploy/apache-vhost.conf \
        | "${PROXY_SSH[@]}" "sudo bash /etc/studio/install-proxy.sh $STUDIO_PROXY_VHOST"
    if [ "${STUDIO_MANAGED_HTTPS:-0}" = 1 ]; then
        "${PROXY_SSH[@]}" "sudo bash $R/current/deploy/managed-https/install.sh"
    fi
fi

echo "==> снаружи через Apache: $URL/health"
code=$(curl "${curl_tls[@]}" -sS -o /dev/null -m 15 -w '%{http_code}' "$URL/health") \
    || die "не удалось проверить $URL/health"
[ "$code" = 200 ] || die "$URL/health ответил ${code:-нет ответа}: vhost не подключён в apache2.conf или нет reload Apache"
echo "==> готово: $URL/"
