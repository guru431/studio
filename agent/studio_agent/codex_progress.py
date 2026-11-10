"""Ограниченные сводки штатных событий Codex. Без чтения файлов и записи текстов на диск."""

import ast
import re

from . import limits

DIFF_MAX = 120_000
FILES_MAX = 200
AGENTS_MAX = 50


def patch_summary(patch):
    files = {}
    row = None
    old = None
    partial = False
    in_hunk = False

    def path_of(value, prefix=""):
        if value.startswith('"'):
            try:
                value = ast.literal_eval(value)
                if not isinstance(value, str):
                    return ""
                try:
                    value = value.encode("latin1").decode("utf-8")
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
            except (SyntaxError, ValueError):
                return ""
        return value.removeprefix(prefix) if prefix else value

    def save():
        nonlocal partial
        if not row or not row["path"]:
            return
        key = row["path"]
        if key not in files:
            if len(files) >= FILES_MAX:
                partial = True
                return
            files[key] = {"path": key[:1024], "added": 0, "removed": 0}
        files[key]["added"] += row["added"]
        files[key]["removed"] += row["removed"]

    for line in patch.splitlines():
        if line.startswith("diff --git "):
            save()
            match = re.search(r' ("b/.*"|b/.*)$', line)
            row = {"path": path_of(match[1], "b/") if match else "", "added": 0, "removed": 0}
            old = None
            in_hunk = False
        elif line.startswith("@@ "):
            in_hunk = True
        elif line.startswith("--- ") and not in_hunk:
            old = path_of(line[4:], "a/")
        elif line.startswith("+++ ") and not in_hunk:
            path = path_of(line[4:], "b/")
            if path == "/dev/null":
                path = old
            if row is None:
                row = {"path": path, "added": 0, "removed": 0}
            else:
                row["path"] = path
        elif line.startswith("rename to ") and not in_hunk and row is not None:
            row["path"] = path_of(line[10:])
        elif row is not None:
            if line.startswith("+"):
                row["added"] += 1
            elif line.startswith("-"):
                row["removed"] += 1
    save()
    return {"diff": patch[:DIFF_MAX], "files": list(files.values()),
            "truncated": len(patch) > DIFF_MAX or partial}


def children(items, previous=(), *, limit=AGENTS_MAX):
    rows = {row["id"]: dict(row) for row in previous}
    for item in items:
        if item.get("type") == "collabAgentToolCall":
            for child in item.get("receiverThreadIds", []):
                if not isinstance(child, str) or not limits.CHAT_ID.fullmatch(child):
                    continue
                state = (item.get("agentsStates") or {}).get(child) or {}
                row = rows.setdefault(child, {"id": child, "title": f"Агент {child[:8]}"})
                status = state.get("status")
                if isinstance(status, str):
                    row["status"] = status[:40]
                elif "status" not in row:
                    row["status"] = "running" if item.get("tool") == "spawnAgent" else "unknown"
                if isinstance(item.get("prompt"), str) and item["prompt"]:
                    row["task"] = item["prompt"][:300]
        elif item.get("type") == "subAgentActivity":
            child = item.get("agentThreadId")
            if not isinstance(child, str) or not limits.CHAT_ID.fullmatch(child):
                continue
            row = rows.setdefault(child, {"id": child, "title": f"Агент {child[:8]}"})
            if isinstance(item.get("agentPath"), str):
                row["title"] = item["agentPath"][:200]
            row["status"] = {"started": "running", "interacted": "running", "interrupted": "shutdown",
                             "completed": "completed"}.get(item.get("kind"), "unknown")
    result = list(rows.values())
    if limit is None or len(result) <= limit:
        return result
    active = {"running", "pending", "waiting", "inProgress"}
    return ([row for row in result if row.get("status") in active]
            + [row for row in result if row.get("status") not in active])[:limit]


def plan(value):
    return [{"step": row["step"][:500], "status": row.get("status", "pending")}
            for row in value[:100] if isinstance(row, dict) and isinstance(row.get("step"), str)
            and row.get("status", "pending") in ("pending", "inProgress", "completed")]
