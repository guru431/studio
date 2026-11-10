"""Порядок текста и инструментов; текст хранится один раз, сегменты ссылаются на его позиции."""

MAX_PARTS = 2000


def width(text):
    return len(text.encode("utf-16-le", errors="surrogatepass")) // 2


def text_part(parts, start, end, *, block=None, phase=None):
    if end <= start:
        return
    previous = parts[-1] if parts else {}
    if previous.get("kind") == "text" and previous.get("end") == start and previous.get("block") == block:
        previous["end"] = end
        return
    part = {"kind": "text", "start": start, "end": end}
    if isinstance(block, str):
        part["block"] = block[:200]
    if phase in ("commentary", "partial_answer", "final_answer"):
        part["phase"] = phase
    parts.append(part)


def append_text(message, text, *, block=None, phase=None):
    parts = message.setdefault("timeline", [])
    start = next((part["end"] for part in reversed(parts) if part.get("kind") == "text"), 0)
    message["text"] += text
    text_part(parts, start, start + width(text), block=block, phase=phase)


def append_step(message, step):
    message["steps"].append(step)
    message.setdefault("timeline", []).append({"kind": "step", "id": step["id"]})


def tail(parts, steps, *, cut=0, prefix=""):
    """После сжатия оставить ссылки на уцелевшие шаги и пересчитать UTF-16 позиции текста."""
    kept = {step["id"] for step in steps}
    offset = width(prefix)
    out = []
    text_part(out, 0, offset)
    for part in parts:
        if part.get("kind") == "step":
            if part["id"] in kept:
                out.append(dict(part))
        elif part.get("kind") == "text" and part["end"] > cut:
            text_part(out, max(part["start"], cut) - cut + offset, part["end"] - cut + offset,
                      block=part.get("block"), phase=part.get("phase"))
    return out


def compact(message):
    """Обычный ответ без чередования не нуждается в дополнительном поле протокола."""
    parts = message.get("timeline", [])
    if len(parts) > MAX_PARTS or not any(p.get("kind") == "text" for p in parts) or len(parts) < 2:
        message.pop("timeline", None)
    return message
