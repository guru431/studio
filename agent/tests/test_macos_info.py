"""Локальные тома macOS: съёмные диски, APFS и ограничение внешней команды."""
import subprocess
from types import SimpleNamespace

from studio_agent import macos_info


def test_local_volumes_keep_spaces_and_hide_system_apfs_mounts(monkeypatch):
    def read(*args):
        assert args == ('/bin/df', '-k', '-l', '-P')
        return '''Filesystem 1024-blocks Used Available Capacity Mounted on
/dev/disk1 100 60 40 60% /
/dev/disk2 200 50 150 25% /Volumes/Backup Disk
/dev/disk3 100 60 40 60% /System/Volumes/Data
invalid row
/dev/disk4 nan 10 20 50% /Volumes/Bad
'''
    monkeypatch.setattr(macos_info, '_read', read)
    assert macos_info.local_disks() == [
        {'path': '/', 'total': 102400, 'free': 40960},
        {'path': '/Volumes/Backup Disk', 'total': 204800, 'free': 153600}]


def test_failed_volume_probe_preserves_root_disk(monkeypatch):
    def failed(*args):
        raise subprocess.TimeoutExpired(args, 5)
    monkeypatch.setattr(macos_info, '_read', failed)
    monkeypatch.setattr(macos_info.shutil, 'disk_usage', lambda path: SimpleNamespace(total=100, free=30))
    assert macos_info.local_disks() == [{'path': '/', 'total': 100, 'free': 30}]
