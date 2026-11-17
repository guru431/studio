"""Локальные сведения macOS без вывода путей и персональных данных в журнал."""
import platform
import re
import shutil
import subprocess


def _read(*args):
    return subprocess.run(args, capture_output=True, stdin=subprocess.DEVNULL, timeout=5, check=True).stdout.decode('utf-8', 'replace')


def memory_status():
    try:
        total = int(_read('/usr/sbin/sysctl', '-n', 'hw.memsize'))
        vm = _read('/usr/bin/vm_stat')
        page = int(re.search(r'page size of (\d+) bytes', vm)[1])
        available = sum(int(value) for value in re.findall(r'Pages (?:free|inactive|speculative):\s+(\d+)\.', vm)) * page
        return {'total': total, 'available': min(total, available)}
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return None


def local_disks():
    """df исключает сетевые ФС; системные APFS-тома не дублируют пользовательские диски."""
    disks = {}
    try:
        for line in _read('/bin/df', '-k', '-l', '-P').splitlines()[1:]:
            fields = line.split(None, 5)
            if len(fields) != 6:
                continue
            _, total, _, free, _, path = fields
            if path != '/' and not path.startswith('/Volumes/'):
                continue
            if total.isdecimal() and free.isdecimal() and 0 <= int(free) <= int(total):
                disks[path] = {'path': path, 'total': int(total) * 1024, 'free': int(free) * 1024}
    except (OSError, subprocess.SubprocessError):
        pass
    if '/' not in disks:
        disk = shutil.disk_usage('/')
        disks['/'] = {'path': '/', 'total': disk.total, 'free': disk.free}
    return sorted(disks.values(), key=lambda disk: (disk['path'] != '/', disk['path']))


def system_data():
    memory = memory_status()
    try:
        ips = re.findall(r'\binet6?\s+([^\s%]+)', _read('/sbin/ifconfig'))
    except (OSError, subprocess.SubprocessError):
        ips = []
    return {'os': 'macOS', 'version': platform.mac_ver()[0], 'internal_ips': ips,
            'memory_total': memory['total'] // 1024 if memory else None,
            'memory_free': memory['available'] // 1024 if memory else None,
            'disks': local_disks()}
