"""Баланс DeepSeek для панели лимитов «DeepSeek Harness»: ключ уходит только на `api.deepseek.com`."""

import json
import math
import os
import re
import time
import urllib.request
from pathlib import Path

from . import deepseek_settings, limits, subscription

URL = f"https://{limits.HOSTS['deepseek']}/user/balance"
OFFICIAL_BASE = "https://api.deepseek.com"
OFFICIAL_PROVIDER = "deepseek-official"
KEY_NAME = "DEEPSEEK_API_KEY"
BODY_LIMIT = 64 * 1024
TIMEOUT = 12
ITEMS = 20

REASONS = {
    "provider": "Баланс показывается только для официального провайдера DeepSeek",
    "base_url": "Задан свой адрес DeepSeek — баланс не запрашивается",
    "key": "На компьютере нет ключа DeepSeek",
    "failed": "DeepSeek не ответил на запрос баланса",
}
NO_WINDOWS = "У DeepSeek нет окон лимита — расходуется баланс"

KEY = re.compile(r"[\x21-\x7e]{1,512}")
REFS = re.compile(r"refs\s*:\s*(?:#.*)?")
ENTRY = re.compile(r"[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*:[ \t]*(.*)")


def _refs_key(text: str) -> str | None:
    """`refs.DEEPSEEK_API_KEY` из `.credentials.yaml`: файл DSH — `refs:` с парами `ИМЯ: ключ` на одну строку
    (проба, фикстура `credentials.example.yml`); блоки, списки и якоря YAML не разбираются — тогда ключа «нет»."""
    inside = False
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            inside = REFS.fullmatch(line.rstrip()) is not None
            continue
        match = ENTRY.fullmatch(line.rstrip()) if inside else None
        if match is None or match.group(1) != KEY_NAME:
            continue
        value = match.group(2).strip()
        if value[:1] in ("\"", "'"):
            end = value.find(value[0], 1)
            value = value[1:end] if end > 0 else ""
        else:
            value = re.split(r"\s#", value, maxsplit=1)[0].strip()
        return value if KEY.fullmatch(value) and value[0] not in "|>[{&*!%@`" else None
    return None


def key(home: Path, environ=None) -> str | None:
    """Ключ DeepSeek: `DEEPSEEK_API_KEY` окружения моста, иначе `.credentials.yaml` из `DSH_HOME`."""
    env = os.environ if environ is None else environ
    value = (env.get(KEY_NAME) or "").strip()
    if value:
        return value if KEY.fullmatch(value) else None
    try:
        with (Path(home) / deepseek_settings.CREDENTIALS).open("rb") as stream:
            raw = stream.read(BODY_LIMIT + 1)
        if len(raw) > BODY_LIMIT:
            return None
        return _refs_key(raw.decode("utf-8-sig"))
    except (OSError, ValueError):
        return None


def _provider(model) -> str | None:
    """Провайдер из значения модели DSH `["<провайдер>","<модель>"]`; без модели — умолчание DSH."""
    if model is None:
        return OFFICIAL_PROVIDER
    try:
        value = json.loads(model) if isinstance(model, str) else None
    except ValueError:
        return None
    return value[0] if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) else None


def eligible(model, environ=None) -> str | None:
    """`None` — спрашивать можно; иначе причина (`provider`, `base_url`): ключ не уходит чужому сервису."""
    env = os.environ if environ is None else environ
    if _provider(model) != OFFICIAL_PROVIDER:
        return "provider"
    base = (env.get("DEEPSEEK_BASE_URL") or "").strip().rstrip("/")
    return None if base in ("", OFFICIAL_BASE) else "base_url"


def _opener(*handlers):
    return urllib.request.build_opener(subscription._NoRedirect(), *handlers)


def _request(opener, token):
    request = urllib.request.Request(URL, headers={"Authorization": "Bearer " + token,
        "Accept": "application/json", "User-Agent": "studio-deepseek-usage/1.0"})
    with opener.open(request, timeout=TIMEOUT) as response:
        raw = response.read(BODY_LIMIT + 1)
    if len(raw) > BODY_LIMIT:
        raise ValueError()
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get("balance_infos"), list):
        raise ValueError()
    return value


def _amount(value) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError()
    return number


def _items(raw) -> list:
    items = []
    for row in raw["balance_infos"][:ITEMS]:
        if not isinstance(row, dict) or not isinstance(row.get("currency"), str) or not 0 < len(row["currency"]) <= 8:
            raise ValueError()
        items.append({"currency": row["currency"], "total": _amount(row.get("total_balance")),
                      "granted": _amount(row.get("granted_balance")), "topped_up": _amount(row.get("topped_up_balance"))})
    return items


def read(*, model=None, home=None, environ=None, opener=None, now=time.time) -> dict:
    """Ответ `usage` для `deepseek`: окон нет, баланс — по валютам; сбой — причина в `balance.reason`."""
    env = os.environ if environ is None else environ
    home = deepseek_settings.dsh_home(env) if home is None else Path(home)
    balance = {"available": False, "items": []}
    reason = eligible(model, env)
    token = None
    if reason is None:
        token = key(home, env)
        reason = None if token else "key"
    if reason is None:
        try:
            raw = _request(_opener() if opener is None else opener, token)
            balance = {"available": raw.get("is_available") is True, "items": _items(raw)}
        except (OSError, ValueError, TypeError, AttributeError):
            reason = "failed"
    if reason is not None:
        balance["reason"] = REASONS[reason]
    data = subscription.empty("deepseek", now(), NO_WINDOWS)
    data.update(source=limits.HOSTS["deepseek"], available="reason" not in balance, balance=balance)
    data["statistics"] = {"available": False, "source": "", "summary": {}, "daily": []}
    data.pop("message")
    return data
