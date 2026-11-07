"""Лимиты аккаунтов без генерации; секреты остаются на машине, кэш — только в памяти."""

import asyncio
import copy
import json
import math
import os
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

TTL = 60
WINDOWS = (("five_hour", "5 часов", 300), ("seven_day", "Неделя", 10080),
           ("monthly", "Месяц", None))
LINKS = {
    "claude": [{"label": "Использование Claude", "url": "https://claude.ai/settings/usage"}],
    "codex": [{"label": "Использование Codex", "url": "https://chatgpt.com/codex/settings/usage"}],
    "opencode": [{"label": "Провайдеры OpenCode", "url": "https://opencode.ai/docs/providers/"}],
}


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def timestamp(value):
    if number(value) is not None:
        return value
    if isinstance(value, str):
        try:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return date.timestamp() if date.tzinfo is not None else None
        except (ValueError, OverflowError, OSError):
            pass
    return None


def window(wid, label, minutes=None, used=None, reset=None, reason=None):
    used = number(used)
    if used is not None:
        used = min(100, used)
    value = {"id": wid, "label": label, "available": used is not None, "used_percent": used,
             "remaining_percent": None if used is None else round(100 - used, 2),
             "window_minutes": minutes, "resets_at": timestamp(reset)}
    if reason:
        value["reason"] = reason
    return value


def empty(engine, now, reason):
    return {"engine": engine, "available": False, "source": engine, "checked_at": now, "cache_ttl": TTL,
            "plan": None, "windows": [window(*w, reason=reason) for w in WINDOWS],
            "statistics": {"available": False, "source": engine, "summary": {}, "daily": [], "reason": reason},
            "links": copy.deepcopy(LINKS[engine]), "message": reason}


def _complete(data, windows):
    """Обязательные периоды остаются неизвестными, если провайдер их не прислал."""
    known = {w["id"]: w for w in windows}
    data["windows"] = [known.pop(wid, window(wid, label, mins, reason="Провайдер не сообщает этот лимит"))
                       for wid, label, mins in WINDOWS] + list(known.values())
    data["available"] = any(w["available"] for w in windows)
    if data["available"]:
        data.pop("message", None)
    return data


def codex_usage(raw, now):
    data = empty("codex", now, "Лимиты недоступны: проверьте вход в Codex и версию CLI")
    data["source"] = "Codex app-server"
    account = raw.get("account", {}).get("account")
    if isinstance(account, dict) and isinstance(account.get("planType"), str):
        data["plan"] = account["planType"][:64]
    limits = raw.get("limits", {})
    buckets = limits.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        buckets = {"codex": limits.get("rateLimits")}
    windows = []
    for bid, bucket in list(buckets.items())[:20]:
        if not isinstance(bucket, dict):
            continue
        if data["plan"] is None and isinstance(bucket.get("planType"), str):
            data["plan"] = bucket["planType"][:64]
        for key in ("primary", "secondary"):
            item = bucket.get(key)
            if not isinstance(item, dict):
                continue
            mins = number(item.get("windowDurationMins"))
            wid = "five_hour" if mins == 300 else "seven_day" if mins == 10080 else "monthly" if (
                mins is not None and 40320 <= mins <= 44640) else f"{str(bid)[:64]}_{key}"
            label = next((label for ident, label, _ in WINDOWS if ident == wid),
                         f"{str(bucket.get('limitName') or bid)[:80]} · {mins or '?'} мин")
            if any(w["id"] == wid for w in windows):
                wid, label = f"{str(bid)[:64]}_{key}", f"{str(bucket.get('limitName') or bid)[:80]} · {label}"
            windows.append(window(wid, label, mins, item.get("usedPercent"), item.get("resetsAt")))
    usage = raw.get("usage", {})
    summary = usage.get("summary")
    names = {"lifetimeTokens": "lifetime_tokens", "peakDailyTokens": "peak_daily_tokens",
             "longestRunningTurnSec": "longest_running_turn_sec", "currentStreakDays": "current_streak_days",
             "longestStreakDays": "longest_streak_days"}
    stats = {names[k]: number(v) for k, v in summary.items() if k in names and number(v) is not None} if (
        isinstance(summary, dict)) else {}
    credits = limits.get("rateLimitResetCredits")
    count = credits.get("availableCount") if isinstance(credits, dict) else None
    if type(count) is int and count >= 0:
        stats["available_resets"] = count
    rows = usage.get("dailyUsageBuckets")
    daily = [{"date": r["startDate"], "tokens": r["tokens"]} for r in (rows if isinstance(rows, list) else [])[-366:]
             if isinstance(r, dict) and isinstance(r.get("startDate"), str) and len(r["startDate"]) == 10
             and number(r.get("tokens")) is not None]
    if stats or daily:
        data["statistics"] = {"available": True, "source": "Аккаунт Codex", "summary": stats, "daily": daily}
    else:
        data["statistics"]["reason"] = "Эта версия Codex или тип аккаунта не сообщает статистику подписки"
    return _complete(data, windows)


def _read_json(path, limit=512 * 1024):
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError()
    value = json.loads(raw)
    return value if isinstance(value, dict) else {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # OAuth-заголовок не уходит на другой адрес.


def _claude_request(token):
    request = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
        "Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json", "User-Agent": "studio-agent"})
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(request, timeout=8) as response:
        raw = response.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise ValueError()
    value = json.loads(raw)
    return value if isinstance(value, dict) else {}


def _claude_stats(config):
    """Только агрегаты CLI; это не все расходы подписки в вебе и других клиентах."""
    try:
        raw = _read_json(config / "stats-cache.json", 4 * 1024 * 1024)
    except (OSError, ValueError):
        return None
    summary = {target: value for key, target in (("totalSessions", "total_sessions"),
                ("totalMessages", "total_messages")) if (value := number(raw.get(key))) is not None}
    daily = []
    rows = raw.get("dailyModelTokens")
    for row in (rows if isinstance(rows, list) else [])[-366:]:
        if not isinstance(row, dict) or not isinstance(row.get("date"), str) or len(row["date"]) != 10:
            continue
        tokens = row.get("tokensByModel")
        if isinstance(tokens, dict):
            daily.append({"date": row["date"], "tokens": sum(number(n) or 0 for n in tokens.values())})
    if not summary and not daily:
        return None
    return {"available": True, "source": "Локальная история Claude Code (не вся подписка)",
            "summary": summary, "daily": daily}


def claude_usage(now, *, config=None, request=_claude_request, environ=None):
    env = os.environ if environ is None else environ
    config = Path(config) if config is not None else Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    data = empty("claude", now, "Войдите в Claude Code на компьютере для просмотра лимитов")
    data["source"] = "Claude OAuth usage (недокументированный API)"
    if stats := _claude_stats(config):
        data["statistics"] = stats
    oauth, token = {}, env.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not token:
        try:
            value = _read_json(config / ".credentials.json").get("claudeAiOauth")
            oauth = value if isinstance(value, dict) else {}
            token = oauth.get("accessToken")
        except (OSError, ValueError, AttributeError):
            oauth = {}
    if isinstance(oauth.get("subscriptionType"), str):
        data["plan"] = oauth["subscriptionType"][:64]
    if not isinstance(token, str) or not token or len(token) > 16384:
        return data
    expires = number(oauth.get("expiresAt"))
    if expires is not None and expires / 1000 <= now:
        data["message"] = "Вход Claude Code истёк: откройте CLI для обновления входа"
        return data
    try:
        raw = request(token)
    except urllib.error.HTTPError as error:
        data["message"] = ("Вход Claude Code истёк или не разрешает чтение лимитов" if error.code in (401, 403)
                           else "Сервис Claude временно не сообщает лимиты; повторите позже")
        return data
    except (OSError, ValueError):
        data["message"] = "Не удалось прочитать лимиты Claude; повторите позже"
        return data
    windows = []
    for wid, label, mins in WINDOWS:
        item = raw.get(wid)
        if isinstance(item, dict):
            windows.append(window(wid, label, mins, item.get("utilization"), item.get("resets_at")))
    for key, item in list(raw.items())[:40]:
        if key.startswith("seven_day_") and isinstance(item, dict) and number(item.get("utilization")) is not None:
            windows.append(window(key[:100], "Неделя · " + key[10:][:80], 10080,
                                  item.get("utilization"), item.get("resets_at")))
    extra = raw.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled") is True:
        windows.append(window("extra_monthly", "Месяц · дополнительное использование", None,
                              extra.get("utilization")))
    return _complete(data, windows)


class UsageCache:
    """Кэш одного моста; одновременные запросы даже с refresh ждут одну проверку."""

    def __init__(self, *, now=time.monotonic, wall=time.time):
        self.now, self.wall = now, wall
        self.cached, self.pending = {}, {}

    async def read(self, engine, root, backend=None, *, refresh=False):
        cached = self.cached.get(engine)
        if not refresh and cached is not None and self.now() - cached[0] < TTL:
            return copy.deepcopy(cached[1])
        task = self.pending.get(engine)
        if task is None:
            task = self.pending[engine] = asyncio.create_task(self._load(engine, root, backend))
        try:
            value = await asyncio.shield(task)
            if self.pending.get(engine) is task:
                self.cached[engine] = self.now(), value
            return copy.deepcopy(value)
        finally:
            if task.done() and self.pending.get(engine) is task:
                self.pending.pop(engine, None)

    async def _load(self, engine, root, backend):
        now = self.wall()
        if engine == "claude":
            return await asyncio.to_thread(claude_usage, now)
        if engine == "codex":
            raw = await backend.subscription_usage(root)
            return codex_usage(raw, now)
        return empty("opencode", now, "OpenCode не предоставляет общего API лимитов подписок; откройте кабинет провайдера")
