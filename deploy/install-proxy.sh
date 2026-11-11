#!/bin/bash
# Install just Studio's vhost. Invalid configuration restores the previous file before exit.
set -euo pipefail
target=${1:-}
[[ $target =~ ^/[A-Za-z0-9_./-]+$ ]] && [[ $target != *'/../'* ]] && [ -f "$target" ] || {
    echo 'studio: existing proxy vhost required' >&2; exit 1;
}
exec 9>/etc/studio/.proxy.lock
flock -n 9 || { echo 'studio: proxy deployment already running' >&2; exit 1; }
work=$(mktemp -d /etc/studio/proxy.XXXXXXXX)
trap 'rm -rf -- "$work"' EXIT
cat > "$work/incoming"
[ -s "$work/incoming" ] || { echo 'studio: empty vhost' >&2; exit 1; }
if cmp -s "$work/incoming" "$target"; then exit 0; fi
cp -p "$target" "$work/previous"
install -m 0644 "$work/incoming" "$target.next"
mv -f "$target.next" "$target"
if ! apache2ctl -t; then
    cp -p "$work/previous" "$target"
    echo 'studio: proxy config rejected; previous file restored' >&2
    exit 1
fi
if ! systemctl reload apache2; then
    cp -p "$work/previous" "$target"
    systemctl reload apache2
    echo 'studio: proxy reload failed; previous file restored' >&2
    exit 1
fi
cp -p "$work/previous" /etc/studio/apache-vhost.previous.conf
