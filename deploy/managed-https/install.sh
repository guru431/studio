#!/bin/bash
set -euo pipefail
source_dir=$(dirname "$(readlink -f "$0")")
target=/etc/studio/managed-https
exec 9>/etc/studio/.proxy.lock
flock -n 9 || { echo 'studio: proxy configuration in progress' >&2; exit 1; }
/usr/bin/python3 -I -c 'from cryptography import x509; assert hasattr(x509.Certificate, "verify_directly_issued_by")'
install -d -m 0700 "$target"
install -m 0600 "$source_dir/apply.py" "$source_dir/policy.json" "$target/"
install -m 0644 "$source_dir/studio-https.socket" "$source_dir/studio-https@.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now studio-https.socket
