"""Storage failures must stop the container before it can open a database or write media."""
import os
from pathlib import Path
import subprocess

from test_deploy import _bash
import pytest


def test_container_entrypoint_checks_storage_before_executing_the_application():
    bash = _bash()
    if not bash:
        pytest.skip('Bash unavailable')
    entry = Path(__file__).parents[1] / 'deploy/docker/entrypoint.sh'
    script = ('findmnt() { case "$*" in\n'
              '*"/var/lib/studio") printf "%s\\n" "$TEST_STATE_FS" ;;\n'
              '*) printf "%s\\n" "$TEST_MEDIA_FS" ;;\nesac; }\n. "$TEST_ENTRY"')
    env = {**os.environ, 'TEST_ENTRY': entry.as_posix(),
           'STUDIO_MEDIA_DIR': '/media'}
    for media_fs, state_fs, success in [('ext4', 'ext4', False), ('cifs', 'nfs4', False),
                                       ('cifs', 'ext4', True)]:
        result = subprocess.run([bash, '-c', script, 'entrypoint', 'printf', '%s', 'app started'],
                                env={**env, 'TEST_STATE_FS': state_fs, 'TEST_MEDIA_FS': media_fs},
                                capture_output=True, timeout=10)
        assert (result.returncode == 0) is success, result.stderr
        assert (b'app started' in result.stdout) is success
