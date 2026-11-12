"""Просмотр штатных дочерних сессий Claude без управления их исполнением."""

import functools
import json

import claude_agent_sdk
from claude_agent_sdk._internal.sessions import _collect_agent_files, _resolve_subagents_dir

from . import limits, permissions, sessions, task_metrics
from .errors import Refused

TAIL_BYTES = 32 * 1024


@functools.lru_cache(maxsize=256)
def _metrics(path, stamp, size):
    """Читаем числовые поля, расход одного ответа учитываем один раз даже при нескольких блоках."""
    rows, first, last, complete = {}, None, None, False
    try:
        with path.open("rb") as stream:
            data = stream.read(limits.HISTORY_BUDGET + 1)
        complete = len(data) <= limits.HISTORY_BUDGET
        for line in data[:limits.HISTORY_BUDGET].splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict) or record.get("type") not in ("user", "assistant"):
                continue
            moment = task_metrics.timestamp(record.get("timestamp"))
            if moment is not None:
                first = moment if first is None else min(first, moment)
                last = moment if last is None else max(last, moment)
            message = record.get("message")
            if record["type"] != "assistant" or not isinstance(message, dict):
                continue
            usage = permissions.token_usage(message.get("usage")) or {}
            key = message.get("id") or record.get("uuid")
            if isinstance(key, str) and "total" in usage:
                rows[key] = max(rows.get(key, 0), usage["total"])
    except OSError:
        return {}
    result = {"started_at": first} if first is not None else {}
    if first is not None and last is not None and complete and _tail_status(path, stamp, size) == "completed":
        result.update(ended_at=last, duration_ms=max(0, (last - first) * 1000))
    if rows and complete:
        result["total_tokens"] = sum(rows.values())
    return result


@functools.lru_cache(maxsize=512)
def _tail_status(path, stamp, size):
    """Последняя законченная реплика агента — подтверждение конца; неполный хвост не угадываем."""
    try:
        with path.open("rb") as stream:
            start = max(0, size - TAIL_BYTES)
            stream.seek(start)
            lines = stream.read(TAIL_BYTES).splitlines()
        if start:
            lines = lines[1:]  # первая строка могла начаться до прочитанного хвоста
        for line in reversed(lines):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                return "unknown"
            if row.get("type") not in ("user", "assistant"):
                continue
            message = row.get("message")
            return ("completed" if row["type"] == "assistant" and isinstance(message, dict)
                    and message.get("stop_reason") == "end_turn" else "unknown")
    except (OSError, ValueError):
        pass
    return "unknown"


def _transcript_statuses(project, chat, members):
    """Только файлы уже проверенных SDK детей, внутри их каталога; ключ кэша — mtime и размер."""
    if not members:
        return {}
    base = _resolve_subagents_dir(chat, str(project))
    if base is None:
        return {}
    result = {}
    try:
        root = base.resolve()
        for child, path in _collect_agent_files(base):
            if child not in members or not path.resolve().is_relative_to(root):
                continue
            try:
                stamp = path.stat()
                result[child] = _tail_status(path, stamp.st_mtime_ns, stamp.st_size)
            except OSError:
                continue
    except OSError:
        pass
    return result


def list_children(project, chat, *, choices=None, live=()):
    if not live and not sessions.exists(project, chat):
        raise Refused("not_found", "Сессия не найдена")
    details = choices.details(chat) if choices is not None else {}
    saved = details.get("subagent_statuses")
    saved = saved if isinstance(saved, dict) else {}
    rows = {aid: {"id": aid, "title": f"Агент {aid}", "status": saved.get(aid, "unknown")}
            for aid in claude_agent_sdk.list_subagents(chat, directory=str(project))}
    metrics = details.get("subagent_metrics") or {}
    for aid, row in rows.items():
        if isinstance(metrics, dict) and isinstance(metrics.get(aid), dict):
            row.update(task_metrics.numeric(metrics[aid]))
    for row in live:
        rows[row["id"]] = dict(row)
    unfinished = {aid for aid, row in rows.items() if row["status"] != "running"}
    for aid, status in _transcript_statuses(project, chat, unfinished).items():
        if status == "completed" or rows[aid]["status"] not in ("failed", "stopped"):
            rows[aid]["status"] = status
    base = _resolve_subagents_dir(chat, str(project)) if rows else None
    if base is not None:
        try:
            root = base.resolve()
            for aid, path in _collect_agent_files(base):
                try:
                    if aid not in rows or not path.resolve().is_relative_to(root):
                        continue
                    stamp = path.stat()
                    observed = _metrics(path, stamp.st_mtime_ns, stamp.st_size)
                except OSError:
                    continue
                # Lifecycle знает длительность точнее, чем интервал между первой и последней записью.
                for key, value in observed.items():
                    if key == "total_tokens":
                        rows[aid][key] = max(rows[aid].get(key, 0), value)
                    else:
                        rows[aid].setdefault(key, value)
        except OSError:
            pass
    return {"items": list(rows.values())}


def history(project, chat, child):
    if child not in {row["id"] for row in list_children(project, chat)["items"]}:
        raise Refused("not_found", "Подзадача не принадлежит этому чату")
    raw = claude_agent_sdk.get_subagent_messages(chat, child, directory=str(project), limit=200)
    normalized = sessions.normalize(raw, include_subagents=True)
    page = sessions.fit(normalized)
    return {"items": page, "complete": len(raw) < 200 and len(page) == len(normalized)}
