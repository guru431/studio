"""Чтение локальной памяти, дисков и адресов Linux для карточки компьютера."""
import platform
import re
import shutil
import subprocess
from pathlib import Path


def memory_status():
    try:
        fields = dict(re.findall(r"^(MemTotal|MemAvailable):\s+(\d+) kB$", Path("/proc/meminfo").read_text("ascii"), re.M))
        total, available = (int(fields[name]) * 1024 for name in ("MemTotal", "MemAvailable"))
        if 0 <= available <= total and total > 0:
            return {"total": total, "available": available}
    except (KeyError, OSError, ValueError):
        pass
    return None


def local_disks():
    paths = ["/"]
    try:
        # mountinfo escapes whitespace/backslashes with octal sequences. Skip network and virtual mounts.
        table = Path("/proc/self/mountinfo").read_text(errors="surrogateescape")
        for line in table.splitlines():
            before, _, after = line.partition(" - ")
            mount, device = before.split(), after.split()
            if len(mount) >= 6 and len(device) >= 2 and device[1].startswith("/dev/") and mount[3] == "/":
                paths.append(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), mount[4]))
    except OSError:
        pass
    disks = []
    for path in dict.fromkeys(paths):
        try:
            usage = shutil.disk_usage(path)
            disks.append({"path": path, "total": usage.total, "free": usage.free})
        except OSError:
            pass
    return disks


def system_data():
    try:
        distro = platform.freedesktop_os_release().get("PRETTY_NAME", "Linux")
    except OSError:
        distro = "Linux"
    try:
        result = subprocess.run(["hostname", "-I"], capture_output=True, stdin=subprocess.DEVNULL,
                                check=True, timeout=4)
        ips = result.stdout.decode("ascii").split()
    except (OSError, ValueError, subprocess.SubprocessError):
        ips = []
    memory = memory_status()
    return {"os": distro, "version": platform.release(), "internal_ips": ips,
            "memory_total": memory["total"] // 1024 if memory else None,
            "memory_free": memory["available"] // 1024 if memory else None, "disks": local_disks()}
