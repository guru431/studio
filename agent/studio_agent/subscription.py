"""Лимиты аккаунтов без генерации; секреты остаются на машине, кэш — только в памяти."""

import asyncio
import copy
import hmac
import json
import math
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from . import host, limits

TTL = 60
KEYCHAIN_SERVICE = "Claude Code-credentials"
KEYCHAIN_SEC = 5
KEYCHAIN_NOT_FOUND = 44  # `security`: «The specified item could not be found in the keychain»
KEYCHAIN_FAILED = "Лимиты Claude недоступны: мост не смог прочитать вход Claude Code из Keychain macOS"
CLAUDE_LOGIN = "Войдите в Claude Code на компьютере для просмотра лимитов"
CLAUDE_USAGE_PATH = "/api/oauth/usage"
CLAUDE_GRANTS_PATH = CLAUDE_USAGE_PATH + "?cedar_ember=1"
CLAUDE_FABLE_KEYS = ("seven_day_fable", "seven_day_overage_included")
WINDOWS = (("five_hour", "5 часов", 300), ("seven_day", "Неделя", 10080),
           ("monthly", "Месяц", None))
LINKS = {
    "claude": [{"label": "Использование Claude", "url": "https://claude.ai/settings/usage"}],
    "codex": [{"label": "Использование Codex", "url": "https://chatgpt.com/codex/settings/usage"}],
    "opencode": [{"label": "Провайдеры OpenCode", "url": "https://opencode.ai/docs/providers/"}],
    "deepseek": [],  # ссылки для страницы ставит студия (`code_usage.LINKS`)
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
    reset_credits = None
    details = credits.get("credits") if isinstance(credits, dict) else None
    if isinstance(details, list):
        reset_credits = []
        for item in details[:100]:
            if not isinstance(item, dict) or item.get("status") != "available" or item.get("resetType") != "codexRateLimits":
                continue
            expires = number(item.get("expiresAt"))
            reset_credits.append({"expires_at": expires if expires is not None and expires <= 253402300799 else None})
        reset_credits.sort(key=lambda item: (item["expires_at"] is None, item["expires_at"] or 0))
    rows = usage.get("dailyUsageBuckets")
    daily = [{"date": r["startDate"], "tokens": r["tokens"]} for r in (rows if isinstance(rows, list) else [])[-366:]
             if isinstance(r, dict) and isinstance(r.get("startDate"), str) and len(r["startDate"]) == 10
             and number(r.get("tokens")) is not None]
    if stats or daily or reset_credits:
        data["statistics"] = {"available": True, "source": "Аккаунт Codex", "summary": stats, "daily": daily}
    else:
        data["statistics"]["reason"] = "Эта версия Codex или тип аккаунта не сообщает статистику подписки"
    if reset_credits is not None:
        data["statistics"]["reset_credits"] = reset_credits
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


def _claude_json(token, path, user_agent, *, body=None):
    request = urllib.request.Request(f"https://{limits.HOSTS['anthropic']}{path}", headers={
        "Authorization": "Bearer " + token, "anthropic-beta": "oauth-2025-04-20",
        "Accept": "application/json", "User-Agent": user_agent},
        data=None if body is None else json.dumps(body).encode())
    if body is not None:
        request.add_header("Content-Type", "application/json")
    opener = urllib.request.build_opener(_NoRedirect())
    with opener.open(request, timeout=8) as response:
        raw = response.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise ValueError()
    value = json.loads(raw)
    return value if isinstance(value, dict) else {}


def _claude_user_agent(cli_version):
    if re.fullmatch(r"[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}", cli_version):
        return f"claude-cli/{cli_version} (external, cli)"
    return "studio-agent"


def _claude_request(token, cli_version, *, skip_spend=False):
    user_agent = _claude_user_agent(cli_version)
    if user_agent != "studio-agent":
        path = CLAUDE_GRANTS_PATH + ("&skip_spend=1" if skip_spend else "")
        try:
            return _claude_json(token, path, user_agent)
        except urllib.error.HTTPError as error:
            if skip_spend or error.code not in (400, 403):
                raise
            error.close()
    return _claude_json(token, CLAUDE_USAGE_PATH, "studio-agent")


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


def _keychain_oauth():
    """macOS: Claude Code хранит вход не в файле, а в Keychain — запись `KEYCHAIN_SERVICE` с тем же JSON, что
    `.credentials.json`. Читает штатный `/usr/bin/security`: им Claude Code запись и создаёт, поэтому доступ не
    спрашивается. Секрет — только в stdout дочернего процесса, не в argv. Записи нет — `LookupError`, иной
    отказ (запрет доступа, заблокированная связка, срок) — `OSError`."""
    try:
        result = subprocess.run(["/usr/bin/security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                                capture_output=True, timeout=KEYCHAIN_SEC)
    except subprocess.TimeoutExpired:
        raise OSError("keychain timeout") from None
    if result.returncode == KEYCHAIN_NOT_FOUND:
        raise LookupError()
    if result.returncode != 0 or len(result.stdout) > 512 * 1024:
        raise OSError("keychain refused")
    try:
        value = json.loads(result.stdout)
    except ValueError:
        raise OSError("keychain value") from None
    oauth = value.get("claudeAiOauth") if isinstance(value, dict) else None
    if not isinstance(oauth, dict):
        raise OSError("keychain value")
    return oauth


def _claude_config(config, env):
    return Path(config) if config is not None else Path(env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def _claude_login(now, config, env):
    oauth, token = {}, env.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not token:
        try:
            value = _read_json(config / ".credentials.json").get("claudeAiOauth")
            oauth = value if isinstance(value, dict) else {}
            token = oauth.get("accessToken")
        except (OSError, ValueError, AttributeError):
            oauth = {}
    if not token and host.MACOS:
        try:
            oauth = _keychain_oauth()
            token = oauth.get("accessToken")
        except LookupError:
            pass
        except OSError:
            return None, None, KEYCHAIN_FAILED
    plan = oauth.get("subscriptionType")
    plan = plan[:64] if isinstance(plan, str) else None
    if not isinstance(token, str) or not token or len(token) > 16384:
        return None, plan, CLAUDE_LOGIN
    expires = number(oauth.get("expiresAt"))
    if expires is not None and expires / 1000 <= now:
        return None, plan, "Вход Claude Code истёк: откройте CLI для обновления входа"
    return token, plan, None


def _claude_fable(raw):
    buckets = raw.get("limits")
    for item in buckets[:50] if isinstance(buckets, list) else []:
        if not isinstance(item, dict) or item.get("kind") != "weekly_scoped":
            continue
        scope = item.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        name = model.get("display_name") if isinstance(model, dict) else None
        if (isinstance(name, str) and re.match(r"^(?:claude )?fable\b", name, re.IGNORECASE)
                and number(item.get("percent")) is not None):
            return window("seven_day_fable", "Неделя · Fable", 10080, item["percent"], item.get("resets_at"))
    for key in CLAUDE_FABLE_KEYS:
        item = raw.get(key)
        if isinstance(item, dict) and number(item.get("utilization")) is not None:
            return window("seven_day_fable", "Неделя · Fable", 10080, item["utilization"], item.get("resets_at"))
    return None


def _claude_reset_grants(raw, token, now):
    """Инвентарь и выбранное Anthropic предложение. Идентификатор провайдера остаётся на мосте."""
    program = raw.get("cedar_ember")
    if not isinstance(program, dict) or program.get("eligible") is not True or not isinstance(program.get("grants"), list):
        return None, None
    rows, selected = [], None
    cooldown = program.get("cooldown_until")
    cooldown_end = timestamp(cooldown)
    can_use = cooldown is None or (cooldown_end is not None and cooldown_end <= now)
    for grant in program["grants"][:50]:
        if not isinstance(grant, dict) or grant.get("paused") is not False:
            continue
        gid, count = grant.get("id"), grant.get("resets_left")
        if (not isinstance(gid, str) or not re.fullmatch(r"[a-z0-9_-]{1,40}", gid)
                or type(count) is not int or not 1 <= count <= 100):
            continue
        starts, ends = grant.get("starts_at"), grant.get("ends_at")
        start, expiry = timestamp(starts), timestamp(ends)
        if (starts is not None and (start is None or start > now)
                or ends is not None and (expiry is None or not now < expiry <= 253402300799)):
            continue
        clears = grant.get("clears")
        windows = set(clears) if isinstance(clears, list) and all(isinstance(item, str) for item in clears) else set()
        # Не обещаем эффект, если в списке появились неизвестные окна.
        kind = "other"
        if windows <= {"five_hour", "seven_day", *CLAUDE_FABLE_KEYS}:
            if grant.get("gives_back_weekly") is True:
                if "seven_day" not in windows and windows.intersection(CLAUDE_FABLE_KEYS):
                    kind = "fable"
            elif "seven_day" in windows:
                kind = "full" if "five_hour" in windows else "weekly"
            elif windows == {"five_hour"}:
                kind = "session"
        row = {"kind": kind, "count": count, "expires_at": expiry}
        if (selected is None and can_use and kind != "other" and gid == program.get("next_grant_id")
                and grant.get("usable_now") is True):
            # Фиксируем конкретное предложение, остаток и вход на момент просмотра.
            target = json.dumps([gid, count, start, expiry, kind, sorted(windows)])
            proof = hmac.digest(token.encode(), target.encode(), "sha256")
            row["reset_key"] = str(UUID(bytes=proof[:16], version=4))
            selected = (row["reset_key"], gid, target)
        rows.append(row)
    rows.sort(key=lambda row: (row["expires_at"] is None, row["expires_at"] or 0))
    return rows, selected


def claude_reset(key, cli_version, *, now=time.time, config=None, environ=None):
    env = os.environ if environ is None else environ
    token, _, error = _claude_login(now(), _claude_config(config, env), env)
    if error:
        return {"outcome": "stale"}
    raw = _claude_request(token, cli_version, skip_spend=True)
    _, selected = _claude_reset_grants(raw, token, now())
    if selected is None or not hmac.compare_digest(key, selected[0]):
        return {"outcome": "stale"}
    user_agent = _claude_user_agent(cli_version)
    organization = _claude_json(token, "/api/oauth/profile", user_agent).get("organization")
    org_id = organization.get("uuid") if isinstance(organization, dict) else None
    if not isinstance(org_id, str):
        return {"outcome": "blocked"}
    try:
        org_id = UUID(org_id)
    except ValueError:
        return {"outcome": "blocked"}
    _, grant_id, target = selected
    # Один и тот же остаток даёт один request_id даже при повторе на другом мосте.
    request_id = str(uuid5(NAMESPACE_URL, f"studio/claude-reset/{org_id}/{target}"))
    result = _claude_json(token, f"/api/organizations/{org_id}/reset_rate_limits", user_agent, body={
        "program": "cedar_ember", "grant_id": grant_id, "request_id": request_id}).get("result")
    outcomes = {"reset": "reset", "already_used": "alreadyRedeemed", "not_limited": "nothingToReset",
                "ineligible": "noCredit", "unavailable": "noCredit", "cooldown": "blocked"}
    if not isinstance(result, str) or result not in outcomes:
        raise ValueError("unexpected Claude reset response")
    return {"outcome": outcomes[result]}


def claude_usage(now, *, config=None, request=None, environ=None, cli_version=""):
    env = os.environ if environ is None else environ
    config = _claude_config(config, env)
    data = empty("claude", now, CLAUDE_LOGIN)
    data["source"] = "Claude OAuth usage (недокументированный API)"
    if stats := _claude_stats(config):
        data["statistics"] = stats
    token, data["plan"], error = _claude_login(now, config, env)
    if error:
        data["message"] = error
        return data
    try:
        raw = request(token) if request is not None else _claude_request(token, cli_version)
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
    if fable := _claude_fable(raw):
        windows.append(fable)
    for key, item in list(raw.items())[:40]:
        if (key.startswith("seven_day_") and key not in CLAUDE_FABLE_KEYS and isinstance(item, dict)
                and number(item.get("utilization")) is not None):
            windows.append(window(key[:100], "Неделя · " + key[10:][:80], 10080,
                                  item.get("utilization"), item.get("resets_at")))
    extra = raw.get("extra_usage")
    if isinstance(extra, dict) and extra.get("is_enabled") is True:
        windows.append(window("extra_monthly", "Месяц · дополнительное использование", None,
                              extra.get("utilization")))
    grants, _ = _claude_reset_grants(raw, token, now)
    if grants is not None:
        data["claude_resets"] = grants
    return _complete(data, windows)


class UsageCache:
    """Кэш одного моста; одновременные запросы даже с refresh ждут одну проверку."""

    def __init__(self, *, now=time.monotonic, wall=time.time):
        self.now, self.wall = now, wall
        self.cached, self.pending = {}, {}

    async def read(self, engine, root, backend=None, *, refresh=False, model=None, cli_version=""):
        key = (engine, model if engine in ("opencode", "deepseek") else None)
        cached = self.cached.get(key)
        if not refresh and cached is not None and self.now() - cached[0] < TTL:
            return copy.deepcopy(cached[1])
        task = self.pending.get(key)
        if task is None:
            task = self.pending[key] = asyncio.create_task(self._load(engine, root, backend, model, cli_version))
        try:
            value = await asyncio.shield(task)
            if self.pending.get(key) is task:
                self.cached[key] = self.now(), value
                if len(self.cached) > 64:
                    self.cached.pop(next(iter(self.cached)))
            return copy.deepcopy(value)
        finally:
            if task.done() and self.pending.get(key) is task:
                self.pending.pop(key, None)

    async def _load(self, engine, root, backend, model, cli_version):
        now = self.wall()
        if engine == "claude":
            return await asyncio.to_thread(claude_usage, now, cli_version=cli_version)
        if engine == "codex":
            raw = await backend.subscription_usage(root)
            return codex_usage(raw, now)
        if engine == "deepseek":
            from . import deepseek_usage
            catalog, bases = getattr(backend, "catalog", None), getattr(backend, "base_urls", None)
            defaults = catalog.get("defaults") if isinstance(catalog, dict) else None
            default = defaults.get("model") if isinstance(defaults, dict) else None
            bases = list(bases) if isinstance(bases, list) else None  # None — профиль не разобран
            return await asyncio.to_thread(lambda: deepseek_usage.read(model=model, default=default, bases=bases,
                                                                       now=lambda: now))
        from . import opencode_usage
        return await asyncio.to_thread(opencode_usage.read, now, model=model)
