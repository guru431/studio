"""Окна Go напрямую или через шлюз; только доверенная конфигурация пользователя."""

import ipaddress
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import subscription

JSONC = re.compile(r'("(?:\\.|[^"\\])*")|//[^\r\n]*|/\*[\s\S]*?\*/|,(?=\s*[}\]])')
REFERENCE = re.compile(r"\{(env|file):([^{}]+)\}")
GATEWAY_PATH = "/subscriptions/opencode-go/usage"
OFFICIAL = "https://opencode.ai/zen/go/v1/usage"


def _json(path):
    with path.open("rb") as stream:
        raw = stream.read(512 * 1024 + 1)
    if len(raw) > 512 * 1024:
        raise ValueError()
    text = raw.decode("utf-8-sig")
    # Строки защищены первой альтернативой: URL и содержимое ключа не меняются.
    text = JSONC.sub(lambda match: match.group(1) or "", text)
    # После удаления комментария запятая может оказаться перед закрывающей скобкой.
    text = JSONC.sub(lambda match: match.group(1) or "", text)
    value = json.loads(text)
    return value if isinstance(value, dict) else {}


def _expand(value, parent, env):
    if not isinstance(value, str):
        return ""
    match = REFERENCE.fullmatch(value)
    if match:
        kind, name = match.groups()
        if kind == "env":
            value = env.get(name, "")
        else:
            path = Path(name).expanduser()
            with (path if path.is_absolute() else parent / path).open("rb") as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError()
            value = raw.decode("utf-8-sig").strip()
    if len(value) > 16384 or any(ord(c) < 32 for c in value):
        raise ValueError()
    return value


def connection(*, model=None, home=None, environ=None):
    """Не читает настройки репозитория: они не могут перенаправить ключ пользователя."""
    home = Path.home() if home is None else Path(home)
    env = os.environ if environ is None else environ
    folder = Path(env.get("OPENCODE_CONFIG_DIR") or Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "opencode")
    paths = [folder / "opencode.json", folder / "opencode.jsonc"]
    if env.get("OPENCODE_CONFIG"):
        paths.append(Path(env["OPENCODE_CONFIG"]).expanduser())
    config, providers = {}, {}
    for path in paths:
        if not path.is_file():
            continue
        raw = _json(path)
        config.update({k: v for k, v in raw.items() if k not in ("provider", "providers")})
        for key in ("provider", "providers"):
            for name, provider in raw.get(key, {}).items():
                if isinstance(provider, dict):
                    providers[name] = (provider, path.parent)
    model = model or config.get("model")
    if not isinstance(model, str) or "/" not in model:
        return None
    provider_id = model.split("/", 1)[0]
    provider, parent = providers.get(provider_id, ({}, folder))
    settings = provider.get("settings") or provider.get("options") or {}
    base = _expand(settings.get("baseURL"), parent, env).rstrip("/")
    key = _expand(settings.get("apiKey"), parent, env)
    if provider_id == "gateway" and base:
        parsed = urllib.parse.urlsplit(base)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError()
        if parsed.scheme != "https":
            # Существующие LAN-шлюзы могут использовать HTTP; публичным адресам ключ не отправляем.
            try:
                address = ipaddress.ip_address(parsed.hostname or "")
            except ValueError:
                raise ValueError() from None
            if parsed.scheme != "http" or not (address.is_private or address.is_loopback):
                raise ValueError()
        if not parsed.hostname or not key:
            return None
        prefix = parsed.path.removesuffix("/v1").rstrip("/")
        url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, prefix + GATEWAY_PATH, "", ""))
        return url, key, "OpenCode Go · общая подписка шлюза"
    official = provider_id in ("opencode-go", "opencode") and (not base or base in (
        "https://opencode.ai/zen/go/v1", "https://opencode.ai/inference/go/openai/v1",
        "https://opencode.ai/inference/go/anthropic", "https://opencode.ai/inference/go/google/v1beta"))
    if not official:
        return None
    if not key:
        data = Path(env.get("XDG_DATA_HOME") or home / ".local/share") / "opencode/auth.json"
        raw = _json(data).get(provider_id, {}) if data.is_file() else {}
        key = raw.get("key", "") if raw.get("type") == "api" else ""
        key = _expand(key, folder, env)
    return (OFFICIAL, key, "OpenCode Go") if key else None


def _request(url, key):
    request = urllib.request.Request(url, headers={"Authorization": "Bearer " + key,
        "Accept": "application/json", "User-Agent": "studio-opencode-usage/1.0"})
    with urllib.request.build_opener(subscription._NoRedirect()).open(request, timeout=12) as response:
        raw = response.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise ValueError()
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get("usage"), dict):
        raise ValueError()
    return value


def read(now, *, model=None, home=None, environ=None, request=_request):
    data = subscription.empty("opencode", now, "Для выбранного провайдера OpenCode чтение лимитов не настроено")
    try:
        target = connection(model=model, home=home, environ=environ)
        if target is None:
            return data
        url, key, source = target
        data["source"] = source
        raw = request(url, key)
        usage = raw.get("usage")
        if not isinstance(usage, dict):
            raise ValueError()
        windows = []
        for name, (wid, label, minutes) in zip(("rolling", "weekly", "monthly"), subscription.WINDOWS):
            item = usage.get(name)
            if isinstance(item, dict):
                windows.append(subscription.window(wid, label, minutes, item.get("percent"), item.get("resetsAt")))
        data["statistics"]["reason"] = "API Go сообщает окна подписки; статистика токенов подписки недоступна"
        return subscription._complete(data, windows)
    except urllib.error.HTTPError as error:
        data["message"] = ("Ключ или права чтения лимитов OpenCode недействительны" if error.code in (401, 403)
                           else "Сервис OpenCode временно не сообщает лимиты; повторите позже")
    except (OSError, ValueError, TypeError, AttributeError):
        data["message"] = "Не удалось прочитать лимиты OpenCode; проверьте конфигурацию и соединение"
    for window in data["windows"]:
        window["reason"] = data["message"]
    return data
