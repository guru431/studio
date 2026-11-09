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


def test_chrome_override_keeps_paths_outside_bmp_and_controls_valid_toml(tmp_path):
    """Эмодзи в пути — не пара суррогатов `\\ud83d…` (TOML её запрещает); управляющие и кавычки — экранированы."""
    from studio_agent import chrome
    import tomllib
    project = tmp_path / "проект 😀 \"q\" \\ \x7f"
    value = {"command": "node", "args": [f"--filesystemRoot={project}", "tab\there"], "enabled": False, "n": 90}
    assert tomllib.loads("value=" + chrome._toml(value))["value"] == value
    npm = tmp_path / "node_modules/npm/bin"
    npm.mkdir(parents=True)
    (npm / "npx-cli.js").write_text("", "utf-8")
    config = chrome.config(project, which=lambda name: str(tmp_path / name))
    assert tomllib.loads("value=" + chrome._toml(config))["value"] == config


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

    rows = [
        {"type": "turn_context", "payload": {"model": "gpt-6.1-sol"}},
        {"type": "event_msg", "timestamp": "2026-01-01T00:00:00Z", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"total_tokens": 20000}, "model_context_window": 100000}}},
        {"type": "event_msg", "timestamp": "2026-01-01T00:01:00Z", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"total_tokens": 25000}, "model_context_window": 100000}}},
        {"type": "turn_context", "payload": {"model": "gpt-6-astra"}},
        {"type": "event_msg", "timestamp": "2026-01-01T00:02:00Z", "payload": {"type": "token_count", "info": {
            "last_token_usage": {"total_tokens": 25000}, "model_context_window": 100000}}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    assert last_context(str(path), folder)["cache"] == {"updated_at": 1767225660, "model": "gpt-6.1-sol"}
    rows[2]["timestamp"] = "invalid"
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    assert "cache" not in last_context(str(path), folder)


def test_claude_cache_uses_reported_ttl_and_does_not_refresh_for_duplicate_blocks(home):
    from claude_agent_sdk import StreamEvent
    from studio_agent.turns import _Turn
    r = Runner(lambda _: None, choices=store.Choices(home), cli_path=None, now=lambda: 1800000000)
    t = _Turn(home, PID, CHAT, "turn", context={"used": 2500, "limit": 100000})
    def event(mid, creation=None, cached=0, parent=None):
        return StreamEvent(uuid="event", session_id=CHAT, parent_tool_use_id=parent, event={
            "type": "message_start", "message": {"id": mid, "model": "claude-test", "usage": {
                "cache_read_input_tokens": cached, "cache_creation": creation or {}}}})
    for mid, creation, ttl in [
        ("five", {"ephemeral_5m_input_tokens": 100}, 300),
        ("hour", {"ephemeral_1h_input_tokens": 100}, 3600),
        ("mixed", {"ephemeral_1h_input_tokens": 100, "ephemeral_5m_input_tokens": 10}, 300),
    ]:
        r._cache_usage(t, event(mid, creation))
        assert t.context["cache"]["ttl_seconds"] == ttl
    r.now = lambda: 1800000060
    r._cache_usage(t, event("mixed", {"ephemeral_5m_input_tokens": 10}))
    assert t.context["cache"]["updated_at"] == 1800000000
    r._cache_usage(t, event("child", {"ephemeral_1h_input_tokens": 10}, parent="tool"))
    assert t.context["cache"]["message_id"] == "mixed"
    r._cache_usage(t, event("read", cached=100))
    assert t.context["cache"]["ttl_seconds"] == 300
    assert t.context["cache"]["updated_at"] == 1800000060
    r._cache_usage(t, event("unknown"))
    assert t.context["cache"]["ttl_seconds"] is None
    r.now = lambda: 1800000090
    r._cache_usage(t, event("unknown", {"ephemeral_1h_input_tokens": 100}))
    assert t.context["cache"]["ttl_seconds"] == 3600
    assert t.context["cache"]["updated_at"] == 1800000060


async def test_claude_cache_survives_state_reads_but_external_replies_invalidate_its_timestamp(home, project, monkeypatch):
    from claude_agent_sdk import SessionMessage, StreamEvent
    from studio_agent import sessions
    usage = {"cache_creation": {"ephemeral_1h_input_tokens": 100}}
    reply = SessionMessage(type="assistant", uuid="answer", session_id=CHAT,
        message={"id": "api-answer", "model": "claude-test", "usage": usage})
    monkeypatch.setattr(sessions, "get_session_messages", lambda *a, **kw: [reply])
    event = StreamEvent(uuid="event", session_id=CHAT, event={"type": "message_start", "message": reply.message})
    r = Runner(lambda _: None, choices=store.Choices(home), cli_path=None, now=lambda: 1800000000,
               factory=lambda options: StateClient(options, [event, result_ok()]))
    r.start(project, PID, CHAT, "Задание", None, None)
    await settle(r)
    assert r.choices.details(CHAT)["context"]["cache"]["updated_at"] == 1800000000
    r.now = lambda: 1800000060
    cache = (await r.state(project, CHAT))["context"]["cache"]
    assert cache["updated_at"] == 1800000000 and cache["ttl_seconds"] == 3600
    reply.message["id"] = "external-answer"
    assert "updated_at" not in (await r.state(project, CHAT))["context"]["cache"]


def test_usage_distinguishes_cached_input_from_anthropic_cache_and_rejects_bad_counters():
    assert permissions.token_usage({"inputTokens": 100, "outputTokens": 20, "cachedInputTokens": 60,
                                    "reasoningOutputTokens": 10}) == {
        "input": 100, "output": 20, "cached": 60, "reasoning": 10, "total": 120}
    assert permissions.token_usage({"input_tokens": 100, "output_tokens": 20,
                                    "cache_creation_input_tokens": 30})["total"] == 150
    assert permissions.token_usage({"input_tokens": True, "outputTokens": -1, "total_tokens": "100"}) is None
