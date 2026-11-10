"""Сессии Claude Code проекта: список чатов и история (протокол §4, `chats`, `history`).

Функции SDK — атрибуты модуля: тесты их подменяют. Всё здесь блокирующее — звать через `asyncio.to_thread`."""

import contextlib
import json
import logging
from pathlib import Path

from claude_agent_sdk import get_session_info, get_session_messages, list_sessions, tag_session

from . import diffs, history_images, limits, permissions, timeline
from .errors import Refused
from .events import clip, text_of

log = logging.getLogger(__name__)

TITLE_MAX = 200
PROMPT_TITLE = 120  # начало first_prompt в названии
STEP_MAX = 2000  # строки шагов одиночного огромного элемента
STEPS_CUT = "… первые шаги не показаны"
HEAD_CUT = "… начало не показано"
TAG = "studio"


def chats(project: Path, live: dict[str, bool]) -> list[dict]:
    """Чаты проекта, новые сверху; `live` — чат идущего хода → ждёт ли ход ответа."""
    items = []
    for s in list_sessions(directory=str(project), limit=limits.CHATS_MAX, include_worktrees=False):
        if not limits.CHAT_ID.fullmatch(s.session_id):
            continue
        title = s.custom_title or s.summary or (s.first_prompt or "")[:PROMPT_TITLE]
        modified = int(s.last_modified) // 1000  # SDK — мс Unix
        items.append({"chat": s.session_id, "title": title[:TITLE_MAX],
                      "created": int(s.created_at) // 1000 if s.created_at is not None else modified,
                      "modified": modified, "tag": s.tag,
                      "running": s.session_id in live, "asking": bool(live.get(s.session_id))})
    items.sort(key=lambda c: c["modified"], reverse=True)
    return items


def normalize(messages: list, *, include_subagents=False) -> list[dict]:
    """Записи сессии → сообщения `{"id", "role", "text", "steps", "time"}` главного агента.

    Подряд идущие ответы ассистента до следующего вопроса — одно сообщение; итоги инструментов — к его шагам."""
    items = []
    answer, steps = None, {}  # текущий ответ и его шаги по tool_use_id
    for m in messages:
        if m.parent_tool_use_id is not None and not include_subagents:
            continue
        content = m.message.get("content") if isinstance(m.message, dict) else None
        if m.type == "user":
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    step = steps.get(block.get("tool_use_id"))
                    if step is not None:
                        step["text"] = clip(text_of(block.get("content")))
                        step["error"] = bool(block.get("is_error"))
                        changes = diffs.for_tool(step["tool"], step["input"]) if not step["error"] else []
                        if changes:
                            step["changes"] = changes
            images = history_images.references(content)
            text = text_of([block for block in content if not isinstance(block, dict) or block.get("type") != "image"]
                           if images and isinstance(content, list) else content)
            if text or images:
                items.append({"id": m.uuid, "role": "user", "text": text, "steps": [], "time": None,
                              **({"images": images} if images else {})})
                answer = None
        elif m.type == "assistant":
            if answer is None:
                answer, steps = {"id": m.uuid, "role": "assistant", "text": "", "steps": [], "time": None}, {}
                items.append(answer)
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"]:
                    timeline.append_text(answer, ("\n\n" if answer["text"] else "") + block["text"], block=m.uuid)
                elif block.get("type") == "tool_use":
                    step = {"id": block.get("id"), "tool": block.get("name"), "input": clip(block.get("input")),
                            "text": "", "error": False}
                    timeline.append_step(answer, step)
                    steps[step["id"]] = step
    return [timeline.compact(item) for item in items]


def _size(value) -> int:
    return len(json.dumps(value, ensure_ascii=True))


def _shrink(item: dict, budget: int) -> dict:
    """Одиночный элемент больше бюджета: строки шагов — до 2000 знаков, затем шаги с начала прочь, затем начало текста."""
    item = dict(item)
    # Сводки уступают место ответу, но порядок текста и команд сохраняется и в большой истории.
    item.pop("progress", None)
    steps = [clip(s, STEP_MAX) for s in item["steps"]]
    item = dict(item, steps=steps)
    if _size(item) <= budget:
        return item

    def shortened(text, kept, *, cut=0, prefix=""):
        result = dict(item, text=prefix + text, steps=kept)
        if "timeline" in item:
            result["timeline"] = timeline.tail(item["timeline"], kept, cut=cut, prefix=prefix)
        return result

    head = STEPS_CUT + "\n" if steps else ""
    bare = shortened(item["text"], [], prefix=head)
    references = {part["id"]: _size(part) + 2 for part in item.get("timeline", []) if part.get("kind") == "step"}
    sizes = [_size(s) + 2 + references.get(s["id"], 0) for s in steps]  # с разделителями массивов
    base, rest, drop = _size(bare), sum(sizes), 0
    while drop < len(steps) and base + rest > budget:
        rest -= sizes[drop]
        drop += 1
    if base + rest <= budget:
        return shortened(item["text"], steps[drop:], prefix=head)
    # Шагов не осталось, а текст всё больше бюджета — его хвост, что влезает.
    head += HEAD_CUT + "\n"
    text = item["text"]
    left, right = 0, min(len(text), budget)
    while left < right:
        n = (left + right + 1) // 2
        candidate = shortened(text[-n:], [], cut=timeline.width(text[:-n]), prefix=head)
        if _size(candidate) <= budget:
            left = n
        else:
            right = n - 1
    return shortened(text[-left:] if left else "", [],
                     cut=timeline.width(text[:-left] if left else text), prefix=head)


def fit(items: list[dict], budget: int = limits.HISTORY_BUDGET) -> list[dict]:
    """Хвост `items`, что влезает в `budget` байтов JSON (ensure_ascii), хотя бы один элемент."""
    out, total = [], 2  # «[]»
    for item in reversed(items):
        add = _size(item) + (2 if out else 0)
        if out and total + add > budget:
            break
        out.append(item)
        total += add
    out.reverse()
    if len(out) == 1 and total > budget:
        out = [_shrink(out[0], budget - 2)]
    return out


def history(project: Path, chat: str, before: int | None, limit: int, *, index=None) -> dict:
    """Последние `limit` сообщений перед номером `before` (`None` — до конца), урезанные под бюджет кадра."""
    def revision():
        info = get_session_info(chat, directory=str(project))
        size = getattr(info, 'file_size', None)
        return (info.last_modified, size) if info and size is not None else None

    stamp = revision() if index is not None else None
    key = (str(project.resolve()), chat)
    items = index.get(key, stamp) if index is not None else None
    if items is None:
        messages = get_session_messages(chat, directory=str(project))
        if not messages:
            raise Refused("not_found", "нет такого чата")
        items = normalize(messages)
        if index is not None:
            index.put(key, stamp, revision(), items)
    total = len(items)
    end = total if before is None else max(0, min(before, total))
    page = fit(items[max(0, end - limit):end], budget=512 * 1024)
    return {"items": page, "start": end - len(page), "total": total}


def history_image(project, chat, message_id, index):
    """Изображение только сообщения главного агента в выбранной сессии проекта."""
    for message in get_session_messages(chat, directory=str(project)):
        if message.uuid == message_id and message.type == "user" and message.parent_tool_use_id is None:
            content = message.message.get("content") if isinstance(message.message, dict) else None
            return history_images.payload(content, index, "claude", project)
    raise Refused("not_found", "Сообщение отсутствует в этом чате")


def exists(project: Path, chat: str) -> bool:
    """Есть ли сессия: у сессии без извлекаемого названия SDK отдаёт `None` — тогда по первой записи."""
    if get_session_info(chat, directory=str(project)) is not None:
        return True
    return bool(get_session_messages(chat, directory=str(project), limit=1))


def last_model(project: Path, chat: str) -> str | None:
    """Модель последнего ответа главного агента (без `<synthetic>` и не по MODEL_ID)."""
    for m in reversed(get_session_messages(chat, directory=str(project))):
        if m.type != "assistant" or m.parent_tool_use_id is not None or not isinstance(m.message, dict):
            continue
        model = m.message.get("model")
        if isinstance(model, str) and model != "<synthetic>" and limits.MODEL_ID.fullmatch(model):
            return model
    return None


def token_usage(project: Path, chat: str) -> dict | None:
    """Расход главной сессии из истории CLI; блоки одного ответа не удваивают счётчик."""
    return _token_usage(get_session_messages(chat, directory=str(project)))


def _token_usage(history):
    messages = {}
    for m in history:
        if m.type != "assistant" or m.parent_tool_use_id is not None or not isinstance(m.message, dict):
            continue
        if m.message.get("model") == "<synthetic>":
            continue
        usage = permissions.token_usage(m.message.get("usage"))
        if usage:
            key = m.message.get("id") or m.uuid
            previous = messages.setdefault(key, {})
            for name, value in usage.items():
                previous[name] = max(previous.get(name, 0), value)
    if not messages:
        return None
    return {name: sum(usage.get(name, 0) for usage in messages.values())
            for name in set().union(*(usage.keys() for usage in messages.values()))}


def context_stats(project: Path, chat: str, previous=None):
    """Расход и кэш из одного снимка истории SDK, без повторного чтения файла сессии."""
    history = get_session_messages(chat, directory=str(project))
    return _token_usage(history), _cache_status(history, previous)


def _cache_status(history, previous):
    """SDK не отдаёт время истории: сохранённая отметка годится лишь для того же API-ответа."""
    for m in reversed(history):
        if m.type != "assistant" or m.parent_tool_use_id is not None or not isinstance(m.message, dict):
            continue
        model, message_id = m.message.get("model"), m.message.get("id")
        if model == "<synthetic>":
            continue
        if not isinstance(model, str) or not isinstance(message_id, str):
            return None
        if isinstance(previous, dict) and previous.get("message_id") == message_id and previous.get("model") == model:
            return previous
        return {"model": model, "message_id": message_id,
                "ttl_seconds": permissions.claude_cache_ttl(m.message.get("usage"))}
    return None


def mark_studio(project: Path, chat: str) -> None:
    """Метка `studio` сессии, начатой студией; сбой — только в журнал."""
    try:
        tag_session(chat, TAG, directory=str(project))
    except Exception as e:
        log.warning("метка сессии: %s", type(e).__name__)
