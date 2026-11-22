"""Лимиты без CLI/сети: периоды, деградация, секреты, кэш и объединение запросов."""

import asyncio
import json
import subprocess
import urllib.error
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from studio_agent import codex, codex_rpc, subscription
from studio_agent.errors import Refused


async def test_codex_reset_reuses_rpc_key_and_filters_response(home):
    rpc = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock(),
        request=AsyncMock(return_value={"outcome": "alreadyRedeemed", "secret": "private"}))
    runner = codex.CodexRunner(lambda f: None, home=home, find_cli=lambda: home / "codex.exe",
                              factory=lambda *_: rpc)
    key = "8ae96ff3-3425-4f4c-8772-b6fd61502868"
    try:
        assert await runner.reset_usage(home, key) == {"outcome": "alreadyRedeemed"}
        rpc.request.assert_awaited_once_with("account/rateLimitResetCredit/consume", {"idempotencyKey": key})
        rpc.request.side_effect = codex_rpc.RpcError(transport=True)
        with pytest.raises(Refused, match="error"):
            await runner.reset_usage(home, key)
        assert runner._usage_meta is None
        rpc.request.side_effect = codex_rpc.RpcError(code=-32601)
        with pytest.raises(Refused, match="unsupported"):
            await runner.reset_usage(home, key)
    finally:
        if runner._usage_meta is not None:
            await runner._usage_meta.disconnect()


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


@pytest.mark.parametrize("count", [0, 2])
def test_codex_available_resets_are_independent_of_token_statistics(count):
    raw = {"limits": {"rateLimitResetCredits": {"availableCount": count,
        "credits": [{"id": "private-credit-id"}]}}}
    data = subscription.codex_usage(raw, 10)
    assert not data["available"]  # No quota windows were reported.
    assert data["statistics"]["available"]
    assert data["statistics"]["summary"] == {"available_resets": count}
    assert "private-credit-id" not in json.dumps(data)


@pytest.mark.parametrize("credits", [None, {}, {"availableCount": None}, {"availableCount": True},
    {"availableCount": -1}, {"availableCount": 1.5}, {"availableCount": "2"},
    {"availableCount": float("nan")}, {"credits": [{"id": "not-a-count"}]}])
def test_codex_missing_or_invalid_reset_count_stays_unknown(credits):
    data = subscription.codex_usage({"limits": {"rateLimitResetCredits": credits}}, 10)
    assert "available_resets" not in data["statistics"]["summary"]
    assert not data["statistics"]["available"]


def test_codex_reset_expiry_preserves_individual_dates_without_private_credit_fields():
    raw = {"limits": {"rateLimitResetCredits": {"availableCount": 4, "credits": [
        {"id": "private-credit", "resetType": "codexRateLimits", "status": "available", "expiresAt": 2000,
         "title": "private-title", "description": "private-description"},
        {"resetType": "codexRateLimits", "status": "available", "expiresAt": 1000},
        {"resetType": "codexRateLimits", "status": "available", "expiresAt": None},
        {"resetType": "codexRateLimits", "status": "redeemed", "expiresAt": 3000},
        {"resetType": "other", "status": "available", "expiresAt": 4000}, None]}}}
    stats = subscription.codex_usage(raw, 10)["statistics"]
    assert stats["summary"]["available_resets"] == 4  # The detail list can be incomplete.
    assert stats["reset_credits"] == [{"expires_at": 1000}, {"expires_at": 2000}, {"expires_at": None}]
    assert "private" not in json.dumps(stats)


@pytest.mark.parametrize("details", [None, []])
def test_codex_reset_expiry_keeps_unknown_details_distinct_from_empty_list(details):
    raw = {"limits": {"rateLimitResetCredits": {"availableCount": 2, "credits": details}}}
    stats = subscription.codex_usage(raw, 10)["statistics"]
    assert stats.get("reset_credits") == details


@pytest.mark.parametrize("expires", [-1, True, "2000", float("inf"), 253402300800])
def test_codex_invalid_reset_expiry_stays_unknown(expires):
    raw = {"limits": {"rateLimitResetCredits": {"availableCount": 1, "credits": [
        {"resetType": "codexRateLimits", "status": "available", "expiresAt": expires}]}}}
    assert subscription.codex_usage(raw, 10)["statistics"]["reset_credits"] == [{"expires_at": None}]


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


def test_macos_reads_claude_login_from_keychain_when_there_is_no_file(tmp_path, monkeypatch):
    """На macOS Claude Code хранит вход в Keychain: файла нет — читается запись `security`, секрет не в argv."""
    calls = []
    stored = json.dumps({"claudeAiOauth": {"accessToken": "keychain-secret", "expiresAt": 2000000,
                                           "subscriptionType": "pro", "refreshToken": "never"}}).encode()

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stored + b"\n", b"")

    monkeypatch.setattr(subscription.host, "MACOS", True)
    monkeypatch.setattr(subscription.subprocess, "run", run)
    request = Mock(return_value={"five_hour": {"utilization": 40}})
    data = subscription.claude_usage(1000, config=tmp_path, request=request, environ={})
    request.assert_called_once_with("keychain-secret")
    assert data["available"] and data["plan"] == "pro"
    assert calls == [["/usr/bin/security", "find-generic-password", "-s", "Claude Code-credentials", "-w"]]
    assert "secret" not in json.dumps(data) and "never" not in json.dumps(data)


@pytest.mark.parametrize("code,words", [(44, "Войдите в Claude Code на компьютере для просмотра лимитов"),
                                        (51, subscription.KEYCHAIN_FAILED), ("timeout", subscription.KEYCHAIN_FAILED),
                                        ("junk", subscription.KEYCHAIN_FAILED)])
def test_macos_keychain_absence_and_refusal_have_their_own_words(tmp_path, monkeypatch, code, words):
    def run(argv, **kw):
        if code == "timeout":
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        return subprocess.CompletedProcess(argv, 0 if code == "junk" else code, b"not json", b"")

    monkeypatch.setattr(subscription.host, "MACOS", True)
    monkeypatch.setattr(subscription.subprocess, "run", run)
    request = Mock(side_effect=AssertionError("network"))
    data = subscription.claude_usage(1000, config=tmp_path, request=request, environ={})
    assert not data["available"] and data["message"] == words and not request.called


def test_windows_and_linux_never_ask_the_keychain(tmp_path, monkeypatch):
    monkeypatch.setattr(subscription.host, "MACOS", False)
    monkeypatch.setattr(subscription.subprocess, "run", Mock(side_effect=AssertionError("security")))
    data = subscription.claude_usage(1000, config=tmp_path, request=Mock(), environ={})
    assert data["message"] == "Войдите в Claude Code на компьютере для просмотра лимитов"


def test_oauth_requests_never_follow_redirects():
    assert subscription._NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test") is None


async def test_cache_ttl_refresh_and_simultaneous_reads_share_one_request(monkeypatch):
    from studio_agent import opencode_usage
    monkeypatch.setattr(opencode_usage, "read", lambda now, **kw: subscription.empty("opencode", now, "unknown"))
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
    assert runner._usage_meta.calls == ["account/read", "account/rateLimits/read", "account/usage/read"]
    assert subscription.codex_usage(raw, 1000)["available"]


async def test_deepseek_usage_is_cached_per_model(monkeypatch):
    from studio_agent import deepseek_usage
    calls = []

    def fake(*, model=None, now=None, **kwargs):
        calls.append(model)
        return {"engine": "deepseek", "checked_at": now(), "model": model}

    monkeypatch.setattr(deepseek_usage, "read", fake)
    cache = subscription.UsageCache(now=lambda: 0, wall=lambda: 1000)
    first, second = '["deepseek-official","deepseek-v4-flash"]', '["deepseek-official","deepseek-v4-pro"]'
    assert (await cache.read("deepseek", "root", model=first))["checked_at"] == 1000
    await cache.read("deepseek", "root", model=first)
    await cache.read("deepseek", "root", model=second)
    assert calls == [first, second]
    await cache.read("deepseek", "root", model=first, refresh=True)
    assert calls == [first, second, first]
