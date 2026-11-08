"""Именованные режимы доступа; меняются только явным выбором владельца в студии."""

CLAUDE = ("default", "auto", "acceptEdits", "plan", "bypassPermissions", "dontAsk")
CODEX = ("read-only", "workspace-write", "danger-full-access")
OPENCODE = ("default", "auto")


def valid(engine, value):
    allowed = {"claude": CLAUDE, "codex": CODEX, "opencode": OPENCODE}.get(engine, ())
    return value is None or isinstance(value, str) and value in allowed


def codex_start(value):
    """Пресеты студии: никакой подмены настроек при отсутствии явного выбора."""
    if value is None:
        return {}
    return {"sandbox": value, "approvalPolicy": "never" if value == "danger-full-access" else "on-request"}


def codex_turn(value):
    if value is None:
        return {}
    policy = {"type": {"read-only": "readOnly", "workspace-write": "workspaceWrite",
                       "danger-full-access": "dangerFullAccess"}[value]}
    if value == "workspace-write":
        policy["networkAccess"] = False
    return {"sandboxPolicy": policy, "approvalPolicy": "never" if value == "danger-full-access" else "on-request"}


def codex_value(policy):
    return {"readOnly": "read-only", "workspaceWrite": "workspace-write", "dangerFullAccess": "danger-full-access"}.get(
        policy.get("type") if isinstance(policy, dict) else None)


def token_usage(raw):
    """Счётчики CLI с разным написанием полей; повторное чтение кэша входит в расход."""
    if not isinstance(raw, dict):
        return None
    names = {"total": ("total_tokens", "totalTokens"), "input": ("input_tokens", "inputTokens"),
             "output": ("output_tokens", "outputTokens"), "cached": ("cache_read_input_tokens", "cached_input_tokens", "cachedInputTokens"),
             "cache_write": ("cache_creation_input_tokens",), "reasoning": ("reasoning_output_tokens", "reasoningOutputTokens")}
    values = {}
    for name, keys in names.items():
        for key in keys:
            value = raw.get(key)
            if type(value) is int and value >= 0:
                values[name] = value
                break
    if "total" not in values and "input" in values and "output" in values:
        values["total"] = values["input"] + values["output"]
        if "cache_read_input_tokens" in raw or "cache_creation_input_tokens" in raw:
            values["total"] += values.get("cached", 0) + values.get("cache_write", 0)
    return values or None


def context(used, limit, usage=None):
    if type(used) is not int or type(limit) is not int or used < 0 or limit <= 0:
        return None
    return {"used": used, "limit": limit, "percent": min(100, round(used * 100 / limit, 1)),
            **({"usage": usage} if usage else {})}


def claude_cache_ttl(usage):
    """Срок только из отчёта о записи; при смешанном кэше — срок короткой части."""
    creation = usage.get("cache_creation") if isinstance(usage, dict) else None
    if isinstance(creation, dict):
        for key, ttl in (("ephemeral_5m_input_tokens", 300), ("ephemeral_1h_input_tokens", 3600)):
            value = creation.get(key)
            if type(value) is int and value > 0:
                return ttl
    return None
