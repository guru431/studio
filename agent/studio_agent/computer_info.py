"""Снимок компьютера по запросу владельца; сведения и ошибки не пишутся в журнал."""

import ctypes
import getpass
import http.client
import ipaddress
import json
import platform
import socket
import subprocess
import time
import urllib.request
from ctypes import wintypes


def memory_status() -> dict | None:
    """Оперативная память без CIM, сети и дочерних процессов; подходит для окна моста."""
    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD),
                    ("total", ctypes.c_ulonglong), ("available", ctypes.c_ulonglong),
                    ("page_total", ctypes.c_ulonglong), ("page_available", ctypes.c_ulonglong),
                    ("virtual_total", ctypes.c_ulonglong), ("virtual_available", ctypes.c_ulonglong),
                    ("extended_available", ctypes.c_ulonglong)]

    data = MemoryStatus(length=ctypes.sizeof(MemoryStatus))
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(data)) or not data.total:
        return None
    return {"total": data.total, "available": data.available}


# Только локальные диски: недоступная сетевая шара не должна задерживать снимок.
SYSTEM_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$system = Get-CimInstance Win32_OperatingSystem
$disks = @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=2 OR DriveType=3' | ForEach-Object {
    @{path=$_.DeviceID+'\'; total=$_.Size; free=$_.FreeSpace}
})
$ips = @(Get-NetIPAddress | Where-Object { $_.AddressState -eq 'Preferred' } | ForEach-Object { $_.IPAddress })
@{os=$system.Caption; version=$system.Version; memory_total=$system.TotalVisibleMemorySize;
  memory_free=$system.FreePhysicalMemory; disks=$disks; internal_ips=$ips} | ConvertTo-Json -Depth 4 -Compress
"""


def _system_data() -> dict:
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", SYSTEM_SCRIPT],
                                capture_output=True, stdin=subprocess.DEVNULL, timeout=10,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        data = json.loads(result.stdout.decode("utf-8-sig")) if result.returncode == 0 else None
        return data if isinstance(data, dict) else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def _external_ip() -> str | None:
    """Публичный адрес выхода этой машины; запрос не содержит сведений компьютера или ключей."""
    try:
        # Фиксированный HTTPS-адрес; перенаправления не нужны.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        with urllib.request.build_opener(NoRedirect).open("https://api64.ipify.org", timeout=3) as response:
            address = ipaddress.ip_address(response.read(128).decode("ascii").strip())
        return str(address) if address.is_global else None
    except (OSError, ValueError, http.client.HTTPException):
        return None


def _bytes(value) -> int | None:
    return value if type(value) is int and value >= 0 else None


def read(roots: list[str], *, now=time.time) -> dict:
    """Вызывать в потоке: CIM и внешний адрес имеют отдельные сроки ожидания."""
    data = _system_data()
    total, free = _bytes(data.get("memory_total")), _bytes(data.get("memory_free"))
    memory = None
    if total is not None and total > 0 and free is not None and free <= total:
        memory = {"total": total * 1024, "available": free * 1024, "used": (total - free) * 1024}
    addresses = []
    for value in data.get("internal_ips", []):
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if not (address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast):
            if str(address) not in addresses:
                addresses.append(str(address))
    disks = []
    for disk in data.get("disks", []):
        total, free = _bytes(disk.get("total")), _bytes(disk.get("free"))
        disks.append({"path": disk["path"], "total": total,
                      "free": free if total is not None and free is not None and free <= total else None})
    try:
        username = getpass.getuser()
    except (OSError, KeyError):
        username = None
    os_name = " ".join(value for value in (data.get("os"), data.get("version")) if value)
    return {"hostname": socket.gethostname(), "username": username, "os": os_name or platform.platform(),
            "internal_ips": addresses, "external_ip": _external_ip(), "memory": memory, "disks": disks,
            "projects_roots": list(roots), "checked_at": now()}
