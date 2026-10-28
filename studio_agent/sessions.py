"""Сессии Claude Code проекта: список чатов и история (протокол §4, `chats`, `history`).

Функции SDK — атрибуты модуля: тесты их подменяют. Всё здесь блокирующее — звать через `asyncio.to_thread`."""

import contextlib
import json
import logging
from pathlib import Path

from claude_agent_sdk import get_session_info, get_session_messages, list_sessions, tag_session

from . import limits
from .errors import Refused
from .events import clip, text_of

log = logging.getLogger(__name__)

TITLE_MAX = 200
PROMPT_TITLE = 120  # начало first_prompt в названии
STEP_MAX = 2000  # строки шагов одиночного огромного элемента
STEPS_CUT = "… первые шаги не показаны"
HEAD_CUT = "… начало ответа не показано"
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


def normalize(messages: list) -> list[dict]:
    """Записи сессии → сообщения `{"id", "role", "text", "steps", "time"}` главного агента.

    Подряд идущие ответы ассистента до следующего вопроса — одно сообщение; итоги инструментов — к его шагам."""
    items, texts = [], {}  # texts: id(элемента ответа) → тексты блоков
    answer, steps = None, {}  # текущий ответ и его шаги по tool_use_id
    for m in messages:
        if m.parent_tool_use_id is not None:
            continue
        content = m.message.get("content") if isinstance(m.message, dict) else None
        if m.type == "user":
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    step = steps.get(block.get("tool_use_id"))
                    if step is not None:
                        step["text"] = clip(text_of(block.get("content")))
                        step["error"] = bool(block.get("is_error"))
            text = text_of(content)
            if text:
                items.append({"id": m.uuid, "role": "user", "text": text, "steps": [], "time": None})
                answer = None
        elif m.type == "assistant":
            if answer is None:
                answer, steps = {"id": m.uuid, "role": "assistant", "text": "", "steps": [], "time": None}, {}
                items.append(answer)
                texts[id(answer)] = []
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str) and block["text"]:
                    texts[id(answer)].append(block["text"])
                elif block.get("type") == "tool_use":
                    step = {"id": block.get("id"), "tool": block.get("name"), "input": clip(block.get("input")),
                            "text": "", "error": False}
                    answer["steps"].append(step)
                    steps[step["id"]] = step
    for item in items:
        if item["role"] == "assistant":
            item["text"] = "\n\n".join(texts[id(item)])
    return items


def _size(value) -> int:
    return len(json.dumps(value, ensure_ascii=True))


def _shrink(item: dict, budget: int) -> dict:
    """Одиночный элемент больше бюджета: строки шагов — до 2000 знаков, затем шаги с начала прочь, затем начало текста."""
    steps = [clip(s, STEP_MAX) for s in item["steps"]]
    item = dict(item, steps=steps)
    if _size(item) <= budget:
        return item
    head = STEPS_CUT + "\n" if steps else ""
    bare = dict(item, text=head + item["text"], steps=[])
    sizes = [_size(s) + 2 for s in steps]  # с разделителем «, »
    base, rest, drop = _size(bare), sum(sizes), 0
    while drop < len(steps) and base + rest > budget:
        rest -= sizes[drop]
        drop += 1
    if base + rest <= budget:
        return dict(bare, steps=steps[drop:])
    # Шагов не осталось, а текст всё больше бюджета — его хвост, что влезает.
    head += HEAD_CUT + "\n"
    room = budget - _size(dict(bare, text=head))
    text = item["text"]
    n = max(0, min(len(text), room))
    over = _size(text[len(text) - n:]) - 2 - room
    if over > 0:  # каждый знак — не меньше байта: убрать over знаков хватит
        n = max(0, n - over)
    return dict(bare, text=head + text[len(text) - n:] if n else head)


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


def history(project: Path, chat: str, before: int | None, limit: int) -> dict:
    """Последние `limit` сообщений перед номером `before` (`None` — до конца), урезанные под бюджет кадра."""
    messages = get_session_messages(chat, directory=str(project))
    if not messages:
        raise Refused("not_found", "нет такого чата")
    items = normalize(messages)
    total = len(items)
    end = total if before is None else max(0, min(before, total))
    page = fit(items[max(0, end - limit):end])
    return {"items": page, "start": end - len(page), "total": total}


def exists(project: Path, chat: str) -> bool:
    return get_session_info(chat, directory=str(project)) is not None


def last_model(project: Path, chat: str) -> str | None:
    """Модель последнего ответа главного агента (без `<synthetic>` и не по MODEL_ID)."""
    for m in reversed(get_session_messages(chat, directory=str(project))):
        if m.type != "assistant" or m.parent_tool_use_id is not None or not isinstance(m.message, dict):
            continue
        model = m.message.get("model")
        if isinstance(model, str) and model != "<synthetic>" and limits.MODEL_ID.fullmatch(model):
            return model
    return None


def mark_studio(project: Path, chat: str) -> None:
    """Метка `studio` сессии, начатой студией; сбой — только в журнал."""
    try:
        tag_session(chat, TAG, directory=str(project))
    except Exception as e:
        log.warning("метка сессии: %s", type(e).__name__)
