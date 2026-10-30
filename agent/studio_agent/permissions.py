"""Именованные режимы доступа; меняются только явным выбором владельца в студии."""

CLAUDE = ("default", "auto", "acceptEdits", "plan", "bypassPermissions", "dontAsk")
CODEX = ("read-only", "workspace-write", "danger-full-access")


def valid(engine, value):
    return value is None or isinstance(value, str) and value in (CODEX if engine == "codex" else CLAUDE)


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


def context(used, limit):
    if type(used) is not int or type(limit) is not int or used < 0 or limit <= 0:
        return None
    return {"used": used, "limit": limit, "percent": min(100, round(used * 100 / limit, 1))}
