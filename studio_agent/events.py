"""События хода (протокол §7): урезка строк до `CLIP`, предел события `EVENT_MAX`, текст блоков, разбор потока SDK,
карточки."""

import json

from claude_agent_sdk import AssistantMessage, StreamEvent, TextBlock, ToolResultBlock, ToolUseBlock, UserMessage

from . import limits

HIDDEN = {"…": "ввод не показан — слишком большой"}


def clip(value, limit: int = limits.CLIP):
    """Копия, где каждая строка длиннее `limit` (в словарях и списках на любой глубине) — `limit - 1` знаков + «…»."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit - 1] + "…"
    if isinstance(value, dict):
        return {k: clip(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [clip(v, limit) for v in value]
    return value


class Ready(dict):
    """Кадр, уже закодированный в ASCII-JSON (`text`): писатель канала шлёт его как есть, второй раз не кодирует."""

    __slots__ = ("text",)


def ready(frame: dict) -> Ready:
    """Кадр и его ASCII-JSON (`allow_nan=False`: не кодируется — `ValueError`)."""
    r = Ready(frame)
    r.text = json.dumps(frame, ensure_ascii=True, allow_nan=False)
    return r


def _size(e: dict) -> int:
    return len(json.dumps(e, ensure_ascii=True))


def bound(e: dict) -> dict:
    """Событие больше `EVENT_MAX` в ASCII-JSON (кириллица — ×6): `input`, `text`, `questions` — строки до
    `CLIP_TIGHT`; всё ещё больше — `input` пометкой `HIDDEN`. Кадр так не подходит к `FRAME_MAX` и сам по себе."""
    if _size(e) <= limits.EVENT_MAX:
        return e
    e = {k: clip(v, limits.CLIP_TIGHT) if k in ("input", "text", "questions") else v for k, v in e.items()}
    if "input" in e and _size(e) > limits.EVENT_MAX:
        e["input"] = dict(HIDDEN)
    return e


def text_of(content) -> str:
    """Строка — как есть; список блоков — тексты `text` через `\\n`, `image` — «[изображение]», прочее — пропуск."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif block.get("type") == "image":
            parts.append("[изображение]")
    return "\n".join(parts)


class Feed:
    """Разбор потока одного хода: сообщение SDK → события `{"k": ...}` главного агента.

    Текст — по дельтам `StreamEvent`; блоки текста `AssistantMessage` — только если дельт за ход не было.
    `text` и `steps` (с итогами, как в `history`) — накопленное для снимка."""

    def __init__(self):
        self.text = ""
        self.steps: list[dict] = []
        self._by_id: dict[str, dict] = {}
        self._deltas = False

    def _text(self, text: str) -> list[dict]:
        self.text += text
        return [{"k": "text", "text": text}]

    def _gap(self) -> list[dict]:
        """Новый блок текста после текста не на `\\n` — сперва пустая строка."""
        return self._text("\n\n") if self.text and not self.text.endswith("\n") else []

    def events(self, message) -> list[dict]:
        if getattr(message, "parent_tool_use_id", None) is not None:
            return []  # подагент
        if isinstance(message, StreamEvent):
            ev = message.event if isinstance(message.event, dict) else {}
            if ev.get("type") == "content_block_start" and (ev.get("content_block") or {}).get("type") == "text":
                return self._gap()
            delta = ev.get("delta") or {}
            if ev.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                self._deltas = True
                text = delta.get("text")
                return self._text(text) if isinstance(text, str) and text else []
            return []
        out = []
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    step = bound({"k": "step", "id": block.id, "tool": block.name, "input": clip(block.input)})
                    self.steps.append({k: v for k, v in step.items() if k != "k"} | {"text": "", "error": False})
                    self._by_id[block.id] = self.steps[-1]
                    out.append(step)
                elif isinstance(block, TextBlock) and block.text and not self._deltas:
                    out += self._gap() + self._text(block.text)
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    e = bound({"k": "result", "id": block.tool_use_id, "text": clip(text_of(block.content)),
                               "error": bool(block.is_error)})
                    step = self._by_id.get(block.tool_use_id)
                    if step is not None:
                        step.update(text=e["text"], error=e["error"])
                    out.append(e)
        return out


def ask_event(request_id: str, tool: str, data: dict, always: bool) -> dict:
    """Карточка: `AskUserQuestion` — вопрос, прочее — разрешение (`always` — есть что запомнить)."""
    if tool == "AskUserQuestion":
        questions = []
        for q in data.get("questions") or []:
            if not isinstance(q, dict):
                continue
            options = [{"label": o.get("label"), "description": o.get("description")}
                       for o in q.get("options") or [] if isinstance(o, dict)]
            questions.append({"question": q.get("question"), "header": q.get("header"),
                              "multi": bool(q.get("multiSelect")), "options": options})
        return bound({"k": "ask", "request_id": request_id, "kind": "question", "questions": clip(questions)})
    return bound({"k": "ask", "request_id": request_id, "kind": "permission", "tool": tool, "input": clip(data),
                  "always": always})
