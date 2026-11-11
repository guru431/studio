#!/bin/sh
set -eu
umask 0007
# Refuse a local directory hidden under an unavailable network mount, including at boot.
[ "$(findmnt -n -o FSTYPE -T "$STUDIO_MEDIA_DIR")" = cifs ] || {
    echo 'studio: media must be mounted from CIFS' >&2
    exit 1
}
case "$(findmnt -n -o FSTYPE -T /var/lib/studio)" in
    cifs|nfs|nfs4) echo 'studio: SQLite must be on local storage' >&2; exit 1 ;;
esac
exec "$@"
