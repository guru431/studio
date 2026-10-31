"""Реальные значения настройки, разрешения и статистика контекста — контракт SDK/RPC и моста."""
from fake_sdk import FakeClient, result_ok
from studio_agent import permissions, store
from studio_agent.turns import Runner
from test_turns import CHAT, PID, marked, project, settle


class StateClient(FakeClient):
    async def get_server_info(self):
        return {"current_permission_mode": self.options.permission_mode or "auto", "models": [
            {"value": "default", "resolvedModel": "claude-test"}]}

    async def get_context_usage(self):
        return {"model": self.options.model or "claude-test", "totalTokens": 2500, "maxTokens": 100000}


async def test_state_reads_sdk_without_a_prompt_and_keeps_access(home, project):
    made = []
    def factory(options):
        client = StateClient(options, [result_ok()])
        made.append(client)
        return client
    r = Runner(lambda _: None, choices=store.Choices(home), cli_path=None, factory=factory)
    r.choices.update(CHAT, access="plan", effort="high")
    state = await r.state(project, CHAT)
    assert state == {"settings": {"model": "claude-test", "effort": "high", "access": "plan"},
                     "context": {"used": 2500, "limit": 100000, "percent": 2.5}}
    assert made[0].queries == [] and made[0].disconnected
    assert made[0].options.permission_mode == "plan"
    assert (await r.state(project, CHAT, access="acceptEdits"))["settings"]["access"] == "acceptEdits"
    assert r.choices.details(CHAT)["access"] == "plan"   # чтение состояния не меняет выбор


async def test_turn_applies_access_and_sends_context_and_snapshot(home, project):
    sent = []
    r = Runner(sent.append, choices=store.Choices(home), cli_path=None,
               factory=lambda options: StateClient(options, [result_ok()]))
    r.start(project, PID, CHAT, "Вопрос", None, None, access="plan")
    await settle(r)
    context = [f["e"] for f in sent if f["e"]["k"] == "context"]
    assert context[-1] == {"k": "context", "used": 2500, "limit": 100000, "percent": 2.5}
    assert r.choices.details(CHAT)["access"] == "plan"
    assert r.choices.details(CHAT)["context"]["used"] == 2500
    assert all(f["e"].get("access") == "plan" for f in sent if f["e"]["k"] == "settings")


def test_access_presets_are_explicit_and_context_is_not_cumulative():
    assert permissions.codex_turn(None) == permissions.codex_start(None) == {}
    assert permissions.codex_turn("workspace-write") == {
        "sandboxPolicy": {"type": "workspaceWrite", "networkAccess": False}, "approvalPolicy": "on-request"}
    assert permissions.codex_start("danger-full-access") == {"sandbox": "danger-full-access", "approvalPolicy": "never"}
    assert not permissions.valid("codex", "bypassPermissions") and not permissions.valid("claude", "danger-full-access")
    assert permissions.context(True, 100) is None and permissions.context(1, 0) is None


def test_chrome_uses_current_profile_and_preserves_desktop_authorization(tmp_path):
    from studio_agent import chrome
    import tomllib
    npm = tmp_path / "node_modules/npm/bin"
    npm.mkdir(parents=True)
    (npm / "npx-cli.js").write_text("", "utf-8")
    config = chrome.config(tmp_path, which=lambda name: str(tmp_path / name))
    assert "--autoConnect" in config["args"] and "--isolated" not in config["args"]
    assert "--no-usage-statistics" in config["args"] and "--no-performance-crux" in config["args"]
    assert tomllib.loads("value=" + chrome._toml(config))["value"] == config
    assert chrome.config(tmp_path, which=lambda _: None) is None


def test_existing_codex_context_reads_only_counters_under_the_profile(tmp_path):
    import json
    from studio_agent.codex import last_context
    folder = tmp_path / "profile"
    folder.mkdir()
    path = folder / "session.jsonl"
    path.write_text(json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
        "total_token_usage": {"total_tokens": 800000}, "last_token_usage": {"total_tokens": 25000},
        "model_context_window": 100000}}}) + '\n{"type":"message","text":"private"}\n', "utf-8")
    assert last_context(str(path), folder) == {"used": 25000, "limit": 100000, "percent": 25,
                                             "usage": {"total": 800000}}
    assert last_context(str(path), tmp_path / "other") is None


def test_usage_distinguishes_cached_input_from_anthropic_cache_and_rejects_bad_counters():
    assert permissions.token_usage({"inputTokens": 100, "outputTokens": 20, "cachedInputTokens": 60,
                                    "reasoningOutputTokens": 10}) == {
        "input": 100, "output": 20, "cached": 60, "reasoning": 10, "total": 120}
    assert permissions.token_usage({"input_tokens": 100, "output_tokens": 20,
                                    "cache_creation_input_tokens": 30})["total"] == 150
    assert permissions.token_usage({"input_tokens": True, "outputTokens": -1, "total_tokens": "100"}) is None
