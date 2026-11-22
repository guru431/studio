"""Баланс DeepSeek: ключ уходит только на официальный адрес, ответ — только числа и валюты."""

import json
import logging
import urllib.error
import urllib.request

import pytest

from studio_agent import deepseek_usage, limits

OFFICIAL = '["deepseek-official","deepseek-v4-flash"]'
BODY = {"is_available": True, "balance_infos": [
    {"currency": "CNY", "total_balance": "0.00", "granted_balance": "0.00", "topped_up_balance": "0.00"}]}


class Response:
    def __init__(self, raw):
        self.raw = raw

    def read(self, limit=-1):
        return self.raw if limit < 0 else self.raw[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Opener:
    """Подставной `OpenerDirector`: запоминает запросы, отвечает телом или бросает исключение."""

    def __init__(self, body=None, error=None):
        self.requests, self.body, self.error = [], body, error

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        if self.error is not None:
            raise self.error
        raw = self.body if isinstance(self.body, bytes) else json.dumps(self.body).encode()
        return Response(raw)


def read(home, opener, *, model=OFFICIAL, environ=None):
    env = {"DEEPSEEK_API_KEY": "sk-test"} if environ is None else environ
    return deepseek_usage.read(model=model, home=home, environ=env, opener=opener, now=lambda: 1000)


def test_balance_parsed_from_official_response(tmp_path):
    opener = Opener(BODY)
    data = read(tmp_path, opener)
    assert data["balance"] == {"available": True, "items": [
        {"currency": "CNY", "total": 0.0, "granted": 0.0, "topped_up": 0.0}]}
    request, timeout = opener.requests[0]
    assert request.full_url == "https://api.deepseek.com/user/balance" == deepseek_usage.URL
    assert request.get_header("Authorization") == "Bearer sk-test" and timeout == 12
    assert data["engine"] == "deepseek" and data["available"] and data["source"] == limits.HOSTS["deepseek"]
    assert data["checked_at"] == 1000 and data["cache_ttl"] == 60 and data["plan"] is None
    assert [w["id"] for w in data["windows"]] == ["five_hour", "seven_day", "monthly"]
    assert all(not w["available"] and w["used_percent"] is None for w in data["windows"])
    assert data["statistics"] == {"available": False, "source": "", "summary": {}, "daily": []}


def test_amounts_are_numbers_and_unavailable_flag_is_kept(tmp_path):
    body = {"is_available": False, "balance_infos": [
        {"currency": "USD", "total_balance": "110.50", "granted_balance": "10.50", "topped_up_balance": "100.00"},
        {"currency": "CNY", "total_balance": "1", "granted_balance": "0", "topped_up_balance": "1"}]}
    balance = read(tmp_path, Opener(body))["balance"]
    assert balance["available"] is False and "reason" not in balance
    assert balance["items"][0] == {"currency": "USD", "total": 110.5, "granted": 10.5, "topped_up": 100.0}
    assert [item["currency"] for item in balance["items"]] == ["USD", "CNY"]


def test_no_request_for_third_party_provider(tmp_path):
    for model in ('["srvllm","stub-tools"]', "deepseek-official", "[", '["deepseek-official"]', 7):
        opener = Opener(BODY)
        data = read(tmp_path, opener, model=model)
        assert not opener.requests and not data["available"]
        assert data["balance"] == {"available": False, "items": [],
                                   "reason": "Баланс показывается только для официального провайдера DeepSeek"}


def test_missing_model_means_catalog_default_provider(tmp_path):
    opener = Opener(BODY)
    data = deepseek_usage.read(model=None, default=OFFICIAL, home=tmp_path, environ={"DEEPSEEK_API_KEY": "sk-test"},
                               opener=opener, now=lambda: 1)
    assert data["available"] and len(opener.requests) == 1
    # умолчание каталога — сторонний провайдер (или каталога нет): ключ не уходит
    for default in ('["srvllm","stub-tools"]', None):
        opener = Opener(BODY)
        data = deepseek_usage.read(model=None, default=default, home=tmp_path,
                                   environ={"DEEPSEEK_API_KEY": "sk-test"}, opener=opener, now=lambda: 1)
        assert not opener.requests and "официального провайдера" in data["balance"]["reason"]


def test_custom_base_url_blocks_key(tmp_path):
    custom = "Задан свой адрес DeepSeek — баланс не запрашивается"
    for base in ("https://proxy.example", "http://api.deepseek.com", "https://api.deepseek.com.evil.example",
                 "https://user:pw@api.deepseek.com", "https://api.deepseek.com:8443"):
        opener = Opener(BODY)
        data = read(tmp_path, opener, environ={"DEEPSEEK_API_KEY": "sk-test", "DEEPSEEK_BASE_URL": base})
        assert not opener.requests
        assert data["balance"]["reason"] == custom
    for base in ("", "https://api.deepseek.com", "https://api.deepseek.com/", "https://api.deepseek.com/v1",
                 "https://api.deepseek.com/anthropic"):
        opener = Opener(BODY)
        read(tmp_path, opener, environ={"DEEPSEEK_API_KEY": "sk-test", "DEEPSEEK_BASE_URL": base})
        assert len(opener.requests) == 1


def test_base_url_from_every_dsh_source_and_profile_blocks_key(tmp_path):
    """Minor-3: `DEEPSEEK_BASE_URL` из `.credentials.yaml` и `$DSH_HOME/.env`, явный `baseURL` строки
    `llm-deepseek` профиля — ключ прокси не уходит на `api.deepseek.com`."""
    sources = [(".credentials.yaml", "refs:\n  DEEPSEEK_BASE_URL: https://proxy.example\n"),
               (".env", "export DEEPSEEK_BASE_URL='https://proxy.example'\n")]
    for name, text in sources:
        home = tmp_path / name.strip(".")
        home.mkdir()
        (home / name).write_text(text, encoding="utf-8")
        opener = Opener(BODY)
        assert "свой адрес" in read(home, opener)["balance"]["reason"] and not opener.requests
    # N2: профиль не разобран (`bases=None`) — провайдер не достоверен, ключ не уходит
    opener = Opener(BODY)
    data = deepseek_usage.read(model=OFFICIAL, bases=None, home=tmp_path, environ={"DEEPSEEK_API_KEY": "sk-test"},
                               opener=opener, now=lambda: 1)
    assert "официального провайдера" in data["balance"]["reason"] and not opener.requests
    opener = Opener(BODY)
    data = deepseek_usage.read(model=OFFICIAL, bases=["https://proxy.example/anthropic"], home=tmp_path,
                               environ={"DEEPSEEK_API_KEY": "sk-test"}, opener=opener, now=lambda: 1)
    assert "свой адрес" in data["balance"]["reason"] and not opener.requests


def test_no_key_gives_reason(tmp_path):
    opener = Opener(BODY)
    data = read(tmp_path, opener, environ={})
    assert not opener.requests and not data["available"]
    assert data["balance"]["reason"] == "На компьютере нет ключа DeepSeek"


def test_key_from_credentials_file(tmp_path):
    (tmp_path / ".credentials.yaml").write_text(
        "version: 1\n\nrefs:\n  SRVLLM_API_KEY: sk-other\n  DEEPSEEK_API_KEY: 'sk-file'  # ключ\n"
        "records:\n  a/b: {}\n", encoding="utf-8")
    assert deepseek_usage.key(tmp_path, {}) == "sk-file"
    assert deepseek_usage.key(tmp_path, {"DEEPSEEK_API_KEY": "sk-env"}) == "sk-file"   # как у DSH: refs сильнее
    assert deepseek_usage.key(tmp_path / "absent", {"DEEPSEEK_API_KEY": "sk-env"}) == "sk-env"
    (tmp_path / "dotenv").mkdir()
    (tmp_path / "dotenv" / ".env").write_text("DEEPSEEK_API_KEY=sk-dotenv\n", encoding="utf-8")
    assert deepseek_usage.key(tmp_path / "dotenv", {}) == "sk-dotenv"
    opener = Opener(BODY)
    deepseek_usage.read(model=OFFICIAL, home=tmp_path, environ={}, opener=opener, now=lambda: 1)
    assert opener.requests[0][0].get_header("Authorization") == "Bearer sk-file"


@pytest.mark.parametrize("text", [
    "refs:\n  OTHER: sk-x\n", "refs:\n  DEEPSEEK_API_KEY:\n", "refs:\n  DEEPSEEK_API_KEY: |\n    sk-x\n",
    "records:\n  DEEPSEEK_API_KEY: sk-x\n", "  DEEPSEEK_API_KEY: sk-x\n", "refs:\n  DEEPSEEK_API_KEY: sk x\n",
    "refs:\n  DEEPSEEK_API_KEY: [sk-x, y]\n"])
def test_credentials_without_a_plain_key_give_none(tmp_path, text):
    (tmp_path / ".credentials.yaml").write_text(text, encoding="utf-8")
    assert deepseek_usage.key(tmp_path, {}) is None


def test_unreadable_or_huge_credentials_give_none(tmp_path):
    assert deepseek_usage.key(tmp_path / "absent", {}) is None
    (tmp_path / ".credentials.yaml").write_bytes(b"refs:\n  DEEPSEEK_API_KEY: sk-x\n" + b"#" * 70000)
    assert deepseek_usage.key(tmp_path, {}) is None


def test_default_opener_refuses_redirects():
    """Ключ не уходит на другой адрес: перенаправление — ошибка, а не переход."""
    class Redirect:
        code, msg = 302, "Found"

        def info(self):
            return {"Location": "https://evil.example/"}

        def read(self, *args):
            return b""

        def close(self):
            pass

    class Fake(urllib.request.HTTPSHandler):
        calls = []

        def https_open(self, request):
            Fake.calls.append(request.full_url)
            return Redirect()

    handler = Fake()
    opener = deepseek_usage._opener(handler)
    with pytest.raises(urllib.error.HTTPError) as caught:
        opener.open(urllib.request.Request(deepseek_usage.URL, headers={"Authorization": "Bearer sk-test"}))
    assert caught.value.code == 302 and Fake.calls == [deepseek_usage.URL]


def test_redirect_error_and_oversize_body_give_failure_reason(tmp_path):
    words = "DeepSeek не ответил на запрос баланса"
    for opener in (Opener(error=urllib.error.HTTPError(deepseek_usage.URL, 302, "x", {}, None)),
                   Opener(error=urllib.error.HTTPError(deepseek_usage.URL, 401, "x", {}, None)),
                   Opener(error=urllib.error.URLError("down")), Opener(error=TimeoutError()),
                   Opener(b"x" * (64 * 1024 + 1)), Opener(b"not json"), Opener([]), Opener({"balance_infos": {}}),
                   Opener({"is_available": True, "balance_infos": [{"currency": "CNY", "total_balance": "x"}]}),
                   Opener({"is_available": True, "balance_infos": [
                       {"currency": "CNY", "total_balance": "nan", "granted_balance": "0", "topped_up_balance": "0"}]})):
        data = read(tmp_path, opener)
        assert data["balance"] == {"available": False, "items": [], "reason": words}
        assert not data["available"]


def test_body_at_the_limit_is_accepted(tmp_path):
    body = json.dumps(BODY).encode()
    assert read(tmp_path, Opener(body + b" " * (64 * 1024 - len(body))))["balance"]["available"]


def test_key_never_in_result_or_log(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    results = [read(tmp_path, Opener(BODY)),
               read(tmp_path, Opener(error=urllib.error.HTTPError(deepseek_usage.URL, 401, "sk-test", {}, None))),
               read(tmp_path, Opener(b"sk-test")),
               read(tmp_path, Opener(BODY), environ={"DEEPSEEK_API_KEY": "sk-test", "DEEPSEEK_BASE_URL": "https://p.example"}),
               read(tmp_path, Opener(BODY), model='["srvllm","m"]')]
    assert "sk-test" not in json.dumps(results) and "sk-test" not in caplog.text


def test_default_home_follows_dsh_home(tmp_path):
    (tmp_path / ".credentials.yaml").write_text("refs:\n  DEEPSEEK_API_KEY: sk-home\n", encoding="utf-8")
    opener = Opener(BODY)
    deepseek_usage.read(model=OFFICIAL, environ={"DSH_HOME": str(tmp_path)}, opener=opener, now=lambda: 1)
    assert opener.requests[0][0].get_header("Authorization") == "Bearer sk-home"
