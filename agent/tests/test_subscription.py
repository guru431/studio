"""Лимиты без CLI/сети: периоды, деградация, секреты, кэш и объединение запросов."""

import asyncio
import json
import urllib.error
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from studio_agent import codex, codex_rpc, subscription


def test_codex_limits_and_subscription_statistics_are_independent():
    data = subscription.codex_usage({"account": {"account": {"planType": "pro", "email": "private"}},
        "limits": {"rateLimits": {"primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 100},
                                  "secondary": {"usedPercent": 60, "windowDurationMins": 10080}}},
        "usage": {"summary": {"lifetimeTokens": 1234, "peakDailyTokens": None, "secret": "no"},
                  "dailyUsageBuckets": [{"startDate": "2026-10-30", "tokens": 123}]}}, 10)
    assert data["plan"] == "pro" and data["available"]
    assert [w["remaining_percent"] for w in data["windows"]] == [75, 40, None]
    assert not data["windows"][2]["available"]
    assert data["statistics"]["summary"] == {"lifetime_tokens": 1234}
    assert data["statistics"]["daily"] == [{"date": "2026-10-30", "tokens": 123}]
    assert "private" not in json.dumps(data) and "secret" not in json.dumps(data)
    del data["statistics"]
    no_usage = subscription.codex_usage({"limits": {"rateLimits": {
        "primary": {"usedPercent": 0, "windowDurationMins": 300}}}}, 10)
    assert no_usage["available"] and not no_usage["statistics"]["available"]
    assert no_usage["windows"][0]["remaining_percent"] == 100


def test_codex_distinguishes_provider_buckets_and_actual_window_lengths():
    data = subscription.codex_usage({"limits": {"rateLimitsByLimitId": {
        "codex": {"primary": {"usedPercent": 10, "windowDurationMins": 300}},
        "other": {"primary": {"usedPercent": 20, "windowDurationMins": 300},
                  "secondary": {"usedPercent": 30, "windowDurationMins": 15}}}}}, 10)
    rows = {w["id"]: w for w in data["windows"]}
    assert rows["five_hour"]["used_percent"] == 10
    assert rows["other_primary"]["used_percent"] == 20
    assert rows["other_secondary"]["window_minutes"] == 15
    assert not rows["seven_day"]["available"] and not rows["monthly"]["available"]


def credentials(config, **kwargs):
    path = config / ".credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "local-secret", "expiresAt": 2000000,
                                                 "subscriptionType": "max", **kwargs}}), encoding="utf-8")
    return path


def test_claude_credentials_stay_local_and_extra_spend_is_separate(tmp_path):
    path = credentials(tmp_path)
    original = path.read_bytes()
    request = Mock(return_value={"five_hour": {"utilization": 12.5, "resets_at": "2026-10-31T10:00:00Z"},
        "seven_day": {"utilization": 25}, "seven_day_sonnet": {"utilization": 10},
        "extra_usage": {"is_enabled": True, "utilization": 30}, "token": "upstream-secret"})
    (tmp_path / "stats-cache.json").write_text(json.dumps({"totalSessions": 2, "totalMessages": 10,
        "dailyModelTokens": [{"date": "2026-10-30", "tokensByModel": {"opus": 45, "sonnet": 55}}]}))
    data = subscription.claude_usage(1000, config=tmp_path, request=request, environ={})
    request.assert_called_once_with("local-secret")
    rows = {w["id"]: w for w in data["windows"]}
    assert rows["five_hour"]["remaining_percent"] == 87.5 and rows["five_hour"]["resets_at"] is not None
    assert not rows["monthly"]["available"] and rows["extra_monthly"]["used_percent"] == 30
    assert data["statistics"]["summary"] == {"total_sessions": 2, "total_messages": 10}
    assert data["statistics"]["daily"][0]["tokens"] == 100
    assert "локальн" in data["statistics"]["source"].lower()
    assert "secret" not in json.dumps(data) and path.read_bytes() == original


def test_expired_missing_or_bad_claude_credentials_do_not_query(tmp_path):
    request = Mock(side_effect=AssertionError("network"))
    assert not subscription.claude_usage(1000, config=tmp_path, request=request, environ={})["available"]
    credentials(tmp_path, expiresAt=999000)
    data = subscription.claude_usage(1000, config=tmp_path, request=request, environ={})
    assert "истёк" in data["message"]
    (tmp_path / ".credentials.json").write_text('{"claudeAiOauth": []}')
    assert not subscription.claude_usage(1000, config=tmp_path, request=request, environ={})["available"]
    assert not request.called


def test_claude_auth_failure_does_not_leak_body_or_refresh_token(tmp_path):
    path = credentials(tmp_path, refreshToken="never-use-this")
    original = path.read_bytes()
    request = Mock(side_effect=urllib.error.HTTPError("https://api.anthropic.com", 401, "secret", {}, None))
    data = subscription.claude_usage(1000, config=tmp_path, request=request, environ={})
    assert not data["available"] and "истёк" in data["message"]
    assert "secret" not in json.dumps(data) and path.read_bytes() == original


def test_oauth_requests_never_follow_redirects():
    assert subscription._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test") is None


async def test_cache_ttl_refresh_and_simultaneous_reads_share_one_request():
    clock = [10]
    ready, release = asyncio.Event(), asyncio.Event()

    async def fetch(root):
        ready.set()
        await release.wait()
        return {}

    backend = SimpleNamespace(subscription_usage=AsyncMock(side_effect=fetch))
    cache = subscription.UsageCache(now=lambda: clock[0], wall=lambda: 1000)
    first = asyncio.create_task(cache.read("codex", "root", backend))
    await ready.wait()
    second = asyncio.create_task(cache.read("codex", "root", backend, refresh=True))
    release.set()
    a, b = await asyncio.gather(first, second)
    assert backend.subscription_usage.await_count == 1 and a == b
    a["windows"].clear()
    assert len((await cache.read("codex", "root", backend))["windows"]) == 3
    clock[0] = 69
    await cache.read("codex", "root", backend)
    assert backend.subscription_usage.await_count == 1
    clock[0] = 70
    await cache.read("codex", "root", backend)
    await cache.read("codex", "root", backend, refresh=True)
    assert backend.subscription_usage.await_count == 3
    unavailable = await cache.read("opencode", "root")
    assert not unavailable["available"] and unavailable["links"]


async def test_codex_old_cli_keeps_limits_when_usage_rpc_is_unsupported(home):
    class Rpc:
        def __init__(self, path, root):
            self.calls = []

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def request(self, method, params):
            self.calls.append(method)
            if method == "account/read":
                return {"account": {"planType": "pro"}}
            if method == "account/rateLimits/read":
                return {"rateLimits": {"primary": {"usedPercent": 20, "windowDurationMins": 300}}}
            raise codex_rpc.RpcError(code=-32601)

    runner = codex.CodexRunner(lambda f: None, home=home, find_cli=lambda: home / "codex.exe", factory=Rpc)
    raw = await runner.subscription_usage(home)
    assert list(raw) == ["account", "limits"]
    assert runner._meta.calls == ["account/read", "account/rateLimits/read", "account/usage/read"]
    assert subscription.codex_usage(raw, 1000)["available"]
