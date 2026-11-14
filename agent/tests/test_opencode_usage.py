"""Настоящие окна Go, выбранный провайдер, секреты и отказ без сетевых проб."""

import json
import urllib.error
from unittest.mock import Mock

import pytest

from studio_agent import opencode_usage, subscription


@pytest.fixture
def config(tmp_path):
    folder = tmp_path / ".config/opencode"
    folder.mkdir(parents=True)
    (folder / "gateway.key").write_text("private-gateway-key\n", encoding="utf-8")
    path = folder / "opencode.jsonc"
    path.write_text('''{
      // Настройки OpenCode v2, ссылка относительно файла конфигурации.
      "model": "gateway/review-full",
      "providers": {"gateway": {"settings": {
        "baseURL": "http://10.77.1.2:4000/v1",
        "apiKey": "{file:gateway.key}", /* комментарий после запятой */
      }}},
    }''', encoding="utf-8")
    return tmp_path, path


def test_go_windows_are_read_from_configured_gateway_without_exposing_key(config):
    home, path = config
    original = path.read_bytes()
    request = Mock(return_value={"usage": {
        "rolling": {"percent": 12, "resetsAt": "2026-11-14T12:00:00Z"},
        "weekly": {"percent": 40, "resetsAt": 2000000000},
        "monthly": {"percent": 5}, "secret": "upstream-private"}})
    result = opencode_usage.read(1000, home=home, environ={}, request=request)
    request.assert_called_once_with("http://10.77.1.2:4000/subscriptions/opencode-go/usage", "private-gateway-key")
    assert result["available"]
    assert [w["remaining_percent"] for w in result["windows"]] == [88, 60, 95]
    assert result["windows"][1]["resets_at"] == 2000000000
    assert not result["statistics"]["available"]
    assert "private" not in json.dumps(result) and path.read_bytes() == original


def test_selected_provider_never_falls_back_to_gateway_subscription(config):
    home, _ = config
    request = Mock()
    result = opencode_usage.read(1000, model="another/model", home=home, environ={}, request=request)
    assert not result["available"] and not request.called


def test_direct_go_uses_local_cli_credentials_and_env_reference(config):
    home, path = config
    path.write_text(json.dumps({"model": "opencode-go/model", "provider": {
        "opencode-go": {"options": {"apiKey": "{env:GO_KEY}"}}}}), encoding="utf-8")
    target = opencode_usage.connection(home=home, environ={"GO_KEY": "go-key"})
    assert target[:2] == (opencode_usage.OFFICIAL, "go-key")
    path.write_text('{"model":"opencode-go/model"}', encoding="utf-8")
    auth = home / ".local/share/opencode/auth.json"
    auth.parent.mkdir(parents=True)
    auth.write_text('{"opencode-go":{"type":"api","key":"cli-key"}}', encoding="utf-8")
    assert opencode_usage.connection(home=home, environ={})[:2] == (opencode_usage.OFFICIAL, "cli-key")


@pytest.mark.parametrize("base", ["http://example.com/v1", "http://10.77.1.2/v1?key=secret",
    "https://user:password@example.com/v1", "file:///somewhere", "http://10.77.1.2/v1#secret"])
def test_bad_gateway_urls_are_rejected_before_any_request(config, base):
    home, path = config
    path.write_text(json.dumps({"model": "gateway/model", "providers": {"gateway": {
        "settings": {"baseURL": base, "apiKey": "private-key"}}}}), encoding="utf-8")
    request = Mock()
    result = opencode_usage.read(1000, home=home, environ={}, request=request)
    assert not result["available"] and not request.called
    assert "private-key" not in json.dumps(result)


@pytest.mark.parametrize("failure", [urllib.error.HTTPError("https://private", 403, "secret", {}, None),
    OSError("secret"), ValueError("secret")])
def test_failures_return_no_secrets_and_keep_unknown_windows(config, failure):
    home, _ = config
    result = opencode_usage.read(1000, home=home, environ={}, request=Mock(side_effect=failure))
    assert not result["available"] and "secret" not in json.dumps(result)
    assert all(w["remaining_percent"] is None and w["reason"] == result["message"] for w in result["windows"])


def test_missing_invalid_and_partial_windows_are_unknown_instead_of_zero(config):
    home, _ = config
    result = opencode_usage.read(1000, home=home, environ={}, request=lambda *a: {
        "usage": {"rolling": {"percent": True}, "weekly": {"percent": 0}}})
    assert [w["remaining_percent"] for w in result["windows"]] == [None, 100, None]


async def test_usage_cache_separates_selected_models_and_shares_refresh(monkeypatch):
    calls = []
    def read(now, **kw):
        calls.append(kw["model"])
        return subscription.empty("opencode", now, "unknown")
    monkeypatch.setattr(opencode_usage, "read", read)
    cache = subscription.UsageCache(now=lambda: 10, wall=lambda: 1000)
    await cache.read("opencode", ".", model="gateway/model")
    await cache.read("opencode", ".", model="gateway/model")
    await cache.read("opencode", ".", model="other/model")
    assert calls == ["gateway/model", "other/model"]
