"""Числовые сведения подзадачи; тексты, пути и ответы не сохраняются."""

import math
from datetime import datetime

FIELDS = ("started_at", "ended_at", "duration_ms", "total_tokens")
ACTIVE = ("running", "inProgress", "pending", "waiting")
TERMINAL = ("completed", "failed", "errored", "stopped", "shutdown", "interrupted")


def number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def timestamp(value):
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return number(parsed.timestamp()) if parsed.tzinfo is not None else None
        except (ValueError, OverflowError, OSError):
            return None
    value = number(value)
    return value / 1000 if value is not None and value > 100_000_000_000 else value


def numeric(row):
    return {key: row[key] for key in FIELDS if number(row.get(key)) is not None}


def update(row, *, now, started=False, ended=False, patch=None, usage=None):
    patch = patch if isinstance(patch, dict) else {}
    usage = usage if isinstance(usage, dict) else {}
    start = timestamp(patch.get("start_time"))
    end = timestamp(patch.get("end_time"))
    if start is not None:
        row["started_at"] = start
    elif started and row.get("started_at") is None:
        row["started_at"] = now
    if started:
        row.pop("ended_at", None)
    if end is not None:
        row["ended_at"] = end
    elif ended and row.get("ended_at") is None:
        row["ended_at"] = now
    for key in ("total_tokens", "duration_ms"):
        value = number(usage.get(key))
        if value is not None:
            row[key] = max(row.get(key, 0), value)
    if ended and "duration_ms" not in usage and row.get("started_at") is not None:
        row["duration_ms"] = max(0, (row["ended_at"] - row["started_at"]) * 1000)
    return row
