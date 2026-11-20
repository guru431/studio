#!/bin/sh
set -eu
umask 0007
# Refuse an unexpected filesystem, including a local directory under a missing CIFS mount.
[ "$(findmnt -n -o FSTYPE -T "$STUDIO_MEDIA_DIR")" = "${STUDIO_MEDIA_FSTYPE:-cifs}" ] || {
    echo 'studio: media filesystem does not match the deployment profile' >&2
    exit 1
}
case "$(findmnt -n -o FSTYPE -T /var/lib/studio)" in
    cifs|nfs|nfs4) echo 'studio: SQLite must be on local storage' >&2; exit 1 ;;
esac
exec "$@"
