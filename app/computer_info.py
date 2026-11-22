"""Разрешённые поля снимка компьютера: без произвольных данных из ответа моста."""

import ipaddress
import math
import re


def software(data):
    """Фиксированные программы, версии и коды; без путей и вывода установщиков."""
    def version(value):
        return value if isinstance(value, str) and re.fullmatch(
            r'[0-9]{1,6}\.[0-9]{1,6}\.[0-9]{1,6}(?:-[A-Za-z0-9][A-Za-z0-9.-]{0,48})?', value) else None

    items, seen = [], set()
    for item in data.get('items', [])[:4] if isinstance(data.get('items'), list) else []:
        if not isinstance(item, dict) or item.get('id') not in ('codex', 'claude', 'opencode', 'deepseek') or item['id'] in seen:
            continue
        seen.add(item['id'])
        items.append({'id': item['id'], 'installed': item.get('installed') is True,
            'version': version(item.get('version')), 'cli_version': version(item.get('cli_version')),
            'latest': version(item.get('latest')),
            'source': item.get('source') if item.get('source') in ('npm', 'vscode', 'native', 'sdk', 'homebrew') else None,
            'update_available': item.get('update_available') is True, 'can_update': item.get('can_update') is True,
            'reason': item.get('reason') if item.get('reason') in ('not_installed', 'bridge', 'manager', 'manual', 'check') else None})
    update = data.get('update') if isinstance(data.get('update'), dict) else {}
    results = update.get('results') if isinstance(update.get('results'), dict) else {}
    return {'items': items, 'update': {
        'tool': update.get('tool') if update.get('tool') in ('claude', 'codex', 'opencode', 'deepseek', 'all') else None,
        'state': update.get('state') if update.get('state') in (
            'idle', 'waiting', 'updating', 'done', 'error', 'cancelled') else 'error',
        **({'results': {tool: results[tool] for tool in ('claude', 'codex', 'opencode', 'deepseek')
                        if results.get(tool) in ('done', 'error')}} if update.get('tool') == 'all' else {})}}


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
