"""Краткое описание браузера сессии. Полный User-Agent не сохраняется и в журнал не попадает.

Это подсказки для владельца, не удостоверение устройства: браузер может скрыть модель или версию ОС.
Имя сессии задаётся отдельно владельцем; сведения не участвуют в проверке доступа.
"""
import ipaddress
import re

import state


def _version(value: str) -> str:
    return re.sub(r"(?:\.0)+$", "", value.replace("_", "."))


def describe(user_agent: str | None) -> tuple[str | None, str | None, str | None]:
    """Браузер, ОС, доступная модель. Неизвестное — None, исходная строка не возвращается."""
    ua = (user_agent or "")[:2048]
    browser = os = model = None
    # Производные Chromium и iOS-браузеры — раньше Chrome/Safari, чьи токены тоже есть в их UA.
    for name, pattern in (
        ("Edge", r"(?:Edg|EdgA|EdgiOS)/([\d.]+)"),
        ("Opera", r"(?:OPR|OPiOS)/([\d.]+)"),
        ("Яндекс Браузер", r"YaBrowser/([\d.]+)"),
        ("Samsung Internet", r"SamsungBrowser/([\d.]+)"),
        ("Firefox", r"(?:Firefox|FxiOS)/([\d.]+)"),
        ("Chrome", r"(?:Chrome|CriOS)/([\d.]+)"),
        ("Safari", r"Version/([\d.]+).*Safari/"),
    ):
        match = re.search(pattern, ua)
        if match:
            browser = f"{name} {_version(match[1])[:32]}"
            break
    if "iPhone" in ua or "iPad" in ua or "iPod" in ua or ("Macintosh" in ua and "Mobile/" in ua):
        model = "iPad" if "iPad" in ua or "Macintosh" in ua else "iPod" if "iPod" in ua else "iPhone"
        match = re.search(r"(?:CPU(?: iPhone)? OS|iPhone OS) ([\d_]+)", ua)
        os = ("iPadOS" if model == "iPad" else "iOS") + (f" {_version(match[1])[:32]}" if match else "")
    elif "Android" in ua:
        match = re.search(r"Android[ /]([\d.]+)", ua)
        # Chromium с сокращённым UA сообщает фиксированные Android 10 и модель K.
        reduced = bool(re.search(r"Android 10; K\)", ua))
        os = "Android" + (f" {_version(match[1])[:32]}" if match and not reduced else "")
        match = re.search(r"Android[^;)]*;([^)]*)\)", ua)
        if match:
            parts = [p.strip().split(" Build/", 1)[0] for p in match[1].split(";")]
            parts = [p for p in parts if p and p not in {"K", "wv", "U", "Mobile"}
                     and not re.fullmatch(r"[a-z]{2}(?:[-_][A-Z]{2})?", p)]
            if parts:
                model = "".join(c for c in parts[-1] if c.isprintable())[:100] or None
    elif "Windows" in ua:
        match = re.search(r"Windows NT ([\d.]+)", ua)
        version = {"10.0": "10/11", "6.3": "8.1", "6.2": "8", "6.1": "7"}.get(match[1]) if match else None
        os = "Windows" + (f" {version}" if version else "")
    elif "CrOS" in ua:
        os = "ChromeOS"
    elif "Macintosh" in ua or "Mac OS X" in ua:
        match = re.search(r"Mac OS X ([\d_]+)", ua)
        os = "macOS" + (f" {_version(match[1])[:32]}" if match else "")
    elif "Linux" in ua:
        os = "Linux"
    return browser, os, model


def remember(digest: str, user_agent: str | None, ip: str, device: str | None = None) -> None:
    """Вход и heartbeat обновляют только собственную строку. Неизменившаяся строка не переписывается."""
    browser, os, model = describe(user_agent)
    try:
        address = str(ipaddress.ip_address(ip))
    except ValueError:
        address = None
    # Пустые заголовки старого клиента не стирают уже известные сведения.
    state.DB.execute(
        "UPDATE sessions SET browser = COALESCE(?, browser), os = COALESCE(?, os),"
        " model = COALESCE(?, model), ip = COALESCE(?, ip), device = COALESCE(?, device) WHERE digest = ? AND"
        " (browser IS NOT COALESCE(?, browser) OR os IS NOT COALESCE(?, os)"
        " OR model IS NOT COALESCE(?, model) OR ip IS NOT COALESCE(?, ip) OR device IS NOT COALESCE(?, device))",
        (browser, os, model, address, device, digest, browser, os, model, address, device))
