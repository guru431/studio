"""Локальные сведения macOS без вывода путей и персональных данных в журнал."""
import platform
import re
import shutil
import subprocess


def _read(*args):
    return subprocess.run(args, capture_output=True, stdin=subprocess.DEVNULL, timeout=5, check=True).stdout.decode()


def memory_status():
    try:
        total = int(_read('/usr/sbin/sysctl', '-n', 'hw.memsize'))
        vm = _read('/usr/bin/vm_stat')
        page = int(re.search(r'page size of (\d+) bytes', vm)[1])
        available = sum(int(value) for value in re.findall(r'Pages (?:free|inactive|speculative):\s+(\d+)\.', vm)) * page
        return {'total': total, 'available': min(total, available)}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None


def system_data():
    memory = memory_status()
    disk = shutil.disk_usage('/')
    try:
        ips = re.findall(r'\binet6?\s+([^\s%]+)', _read('/sbin/ifconfig'))
    except (OSError, subprocess.SubprocessError):
        ips = []
    return {'os': 'macOS', 'version': platform.mac_ver()[0], 'internal_ips': ips,
            'memory_total': memory['total'] // 1024 if memory else None,
            'memory_free': memory['available'] // 1024 if memory else None,
            'disks': [{'path': '/', 'total': disk.total, 'free': disk.free}]}
