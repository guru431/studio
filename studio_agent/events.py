"""Строки событий и шагов: урезка до `CLIP` и текст блоков содержимого."""

from . import limits


def clip(value, limit: int = limits.CLIP):
    """Копия, где каждая строка длиннее `limit` (в словарях и списках на любой глубине) — `limit - 1` знаков + «…»."""
    if isinstance(value, str):
        return value if len(value) <= limit else value[:limit - 1] + "…"
    if isinstance(value, dict):
        return {k: clip(v, limit) for k, v in value.items()}
    if isinstance(value, list):
        return [clip(v, limit) for v in value]
    return value


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
