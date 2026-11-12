#!/bin/bash
# Staged, health-checked image deployment. The database and media are outside images.
set -euo pipefail
NEW=$(dirname "$(readlink -f "$0")")/..
NEW=$(readlink -f "$NEW")
R=$(dirname "$NEW")
REVISION=
CREATE_ADMIN=0
SECRET=0
PREPARE=0
REFRESH_BASE=0
for arg in "$@"; do
    case "$arg" in
        --revision=*) REVISION=${arg#*=} ;;
        --secret-stdin) SECRET=1 ;;
        --admin-stdin) CREATE_ADMIN=1 ;;
        --prepare-only) PREPARE=1 ;;
        --refresh-base) REFRESH_BASE=1 ;;
        *) echo 'studio: unsupported Docker install argument' >&2; exit 1 ;;
    esac
done
[[ $REVISION =~ ^[a-f0-9]{40}$ ]] || { echo 'studio: invalid revision' >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo 'studio: root required' >&2; exit 1; }
# deploy.sh uploads every deployment into its own $R/.incoming-<revision>-<id>.
case "${NEW##*/}" in .incoming-*) ;; *) echo 'studio: install must use an .incoming-* upload' >&2; exit 1 ;; esac
exec 9>"$R/.deploy.lock"
flock -n 9 || { echo 'studio: deployment already running' >&2; exit 1; }

# This file contains validated nonsecret values rendered by render-profile.py.
set -a
. "$NEW/deploy/docker/host.conf"
set +a
[ "$(findmnt -n -o FSTYPE -T "$STUDIO_MEDIA_DIR")" = cifs ] || {
    echo 'studio: media is not mounted from CIFS' >&2; exit 1;
}
case "$(findmnt -n -o FSTYPE -T /var/lib)" in
    cifs|nfs|nfs4) echo 'studio: SQLite requires local storage' >&2; exit 1 ;;
esac
install -d -m 0700 /etc/studio
if [ "$SECRET" = 1 ]; then
    IFS= read -r line
    [[ $line =~ ^STUDIO_DATA_KEY=[A-Za-z0-9_-]{43}=$ ]] || {
        echo 'studio: invalid data key' >&2; exit 1;
    }
    # Explicit --new-secret is the only path that rotates an existing key.
    (umask 077; printf '%s\n' "$line" > /etc/studio/studio.env.next)
    if [ -f /etc/studio/studio.env ]; then
        grep '^STUDIO_CLEANER_CA=' /etc/studio/studio.env >> /etc/studio/studio.env.next || true
    fi
    mv /etc/studio/studio.env.next /etc/studio/studio.env
fi
grep -qE '^STUDIO_DATA_KEY=[A-Za-z0-9_-]{43}=$' /etc/studio/studio.env
install -d -o 10001 -g 10001 -m 0700 /var/lib/studio
install -d -o 10001 -g 10001 -m 0750 /run/studio-docker
install -d -m 0755 /usr/local/share/studio "$R/releases"
# Test actual writes before altering the current release.
probe="$STUDIO_MEDIA_DIR/.studio-deploy-$$"
setpriv --reuid=10001 --regid=10001 --clear-groups touch "$probe"
setpriv --reuid=10001 --regid=10001 --clear-groups rm "$probe"
[ -s "$NEW/agent/studio-agent.zip" ] && [ -s "$NEW/agent/studio-agent-setup.exe" ]
image="studio:$REVISION"
# --pull: a newer base image (Debian and Python patches) replaces the cached one. REFRESH: the
# first build of each ISO week reinstalls apt packages. --refresh-base: rebuild every layer now.
build=(docker build --pull --build-arg "STUDIO_REVISION=$REVISION" --build-arg "REFRESH=$(date -u +%G-W%V)")
if [ "$REFRESH_BASE" = 1 ]; then build+=(--no-cache); fi
"${build[@]}" -t "$image" -f "$NEW/deploy/docker/Dockerfile" "$NEW"
printf '\nSTUDIO_IMAGE=%s\n' "$image" >> "$NEW/deploy/docker/host.conf"
docker compose --env-file "$NEW/deploy/docker/host.conf" -f "$NEW/deploy/docker/compose.yaml" config --quiet
if [ "$PREPARE" = 1 ]; then
    printf 'studio: image %s prepared; application was not started\n' "$REVISION"
    exit 0
fi

release="$R/releases/$REVISION-$(date -u +%Y%m%dT%H%M%S)"
previous=$(readlink -f "$R/current" || true)
mv "$NEW" "$release"
ln -s "$release" "$R/.current-next"
mv -Tf "$R/.current-next" "$R/current"
compose=(docker compose --env-file "$R/current/deploy/docker/host.conf" -f "$R/current/deploy/docker/compose.yaml")
if ! "${compose[@]}" up -d --no-build --wait --wait-timeout 90; then
    echo 'studio: new release failed health checks' >&2
    if [ -n "$previous" ]; then
        ln -s "$previous" "$R/.current-next"
        mv -Tf "$R/.current-next" "$R/current"
        "${compose[@]}" up -d --no-build --wait --wait-timeout 90
        echo 'studio: previous image restored; database retained' >&2
    else
        "${compose[@]}" stop --timeout 20
    fi
    exit 1
fi
if [ -n "$previous" ]; then
    ln -sfn "$previous" "$R/previous"
fi
install -m 0644 "$release/deploy/docker/studio-docker.service" /etc/systemd/system/studio-docker.service
install -m 0644 "$release/deploy/docker/studio-docker-start.service" /etc/systemd/system/studio-docker-start.service
install -m 0644 "$release/deploy/docker/studio-backup.service" /etc/systemd/system/studio-backup.service
install -m 0644 "$release/deploy/studio-backup.timer" /etc/systemd/system/studio-backup.timer
systemctl enable --no-reload studio-docker-start.service studio-backup.timer
# Remove the legacy boot link only after the retrying starter is enabled.
if [ "$(systemctl is-enabled studio-docker.service || true)" = enabled ]; then
    systemctl disable --no-reload studio-docker.service
fi
systemctl daemon-reload
systemctl start studio-docker-start.service studio-docker.service studio-backup.timer
if [ "$CREATE_ADMIN" = 1 ]; then
    "${compose[@]}" exec -T web python /opt/studio/app/manage.py create-admin
fi
printf 'studio: Docker release %s is healthy\n' "$REVISION"
