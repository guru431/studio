"""Разрешённые поля снимка компьютера: без произвольных данных из ответа моста."""

import ipaddress
import math


def _text(value, maximum=512):
    return value if isinstance(value, str) and 0 < len(value) <= maximum and not any(
        ord(c) < 32 or ord(c) == 127 for c in value) else None


def _number(value):
    return value if type(value) is int and 0 <= value <= 2**63 - 1 else None


def _ip(value, *, external=False):
    if not isinstance(value, str) or len(value) > 80:
        return None
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if external and not address.is_global:
        return None
    return str(address)


def clean(data, *, connection_ip=None):
    def rows(key, limit):
        value = data.get(key)
        return value[:limit] if isinstance(value, list) else []

    memory = data.get("memory")
    if isinstance(memory, dict):
        total, available, used = (_number(memory.get(k)) for k in ("total", "available", "used"))
        memory = {"total": total, "available": available, "used": used} if (
            total is not None and total > 0 and available is not None and used is not None
            and used + available == total) else None
    else:
        memory = None
    disks = []
    for disk in rows("disks", 64):
        if not isinstance(disk, dict) or not (path := _text(disk.get("path"), 4096)):
            continue
        total, free = _number(disk.get("total")), _number(disk.get("free"))
        disks.append({"path": path, "total": total,
                      "free": free if total is not None and free is not None and free <= total else None})
    stamp = data.get("checked_at")
    return {"hostname": _text(data.get("hostname")), "username": _text(data.get("username")),
            "os": _text(data.get("os")), "external_ip": (_ip(data.get("external_ip"), external=True)
                                                          or _ip(connection_ip, external=True)),
            "internal_ips": list(dict.fromkeys(ip for value in rows("internal_ips", 128) if (ip := _ip(value)))),
            "memory": memory, "disks": disks,
            "projects_roots": list(dict.fromkeys(path for value in rows("projects_roots", 64)
                                                if (path := _text(value, 4096)))),
            "checked_at": stamp if type(stamp) in (int, float) and math.isfinite(stamp) and stamp >= 0 else None}
