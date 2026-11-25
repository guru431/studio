"""Навыки проекта (протокол §26, мост 0.48.0): файлы `<папка>/<имя>/SKILL.md` в `.agents/skills` и `.claude/skills`.

Список — имя и описание из начала файла, без текста; текст — по одному навыку. Пути — как у чтения файлов
(`files.resolve`): ссылка внутри проекта читается, выход за проект — запись пропущена или `outside`."""

import json
import os
import re
import stat
from pathlib import Path

from . import events, files, limits
from .errors import Refused

FOLDERS = (".agents/skills", ".claude/skills")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
DESCRIPTION_MAX = 200
HEAD = 4096  # байт начала файла для описания в списке
FOLDED = ("", ">", "|", ">-", "|-", ">+", "|+")  # описание — следующими строками с отступом


def _value(raw: str) -> str:
    """Значение строки заголовка: в двойных кавычках — JSON-строка, в одинарных — YAML-строка, иначе как есть."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        try:
            value = json.loads(raw)
        except ValueError:
            return raw[1:-1]
        return value if isinstance(value, str) else raw
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    return raw


def head(text: str) -> tuple[str, str]:
    """(описание, текст без заголовка). Заголовок — между первой строкой `---` и следующей `---`; описание — его
    `description:`; без заголовка — первая непустая строка без ведущих `#`. Описание — до `DESCRIPTION_MAX`."""
    text = text.lstrip("﻿").replace("\r", "")
    lines = text.split("\n")
    close = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None) \
        if lines[0].strip() == "---" else None
    description = ""
    if close is not None:
        header, body = lines[1:close], "\n".join(lines[close + 1:])
        for i, line in enumerate(header):
            if not line.startswith("description:"):
                continue
            raw = line[len("description:"):].strip()
            if raw in FOLDED:
                folded = []
                for more in header[i + 1:]:
                    if more[:1] not in (" ", "\t"):
                        break
                    folded.append(more.strip())
                description = " ".join(part for part in folded if part)
            else:
                description = _value(raw)
            break
    else:
        body = text
        for line in lines:
            line = line.strip()
            if line and line != "---":
                description = line.lstrip("#").strip()
                break
    return events.clip(" ".join(description.split()), DESCRIPTION_MAX), body


def _head_text(data: bytes) -> str | None:
    """Начало файла текстом; NUL или не UTF-8 — `None` (мультибайтный знак, разрезанный `HEAD`, отбрасывается)."""
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        if len(data) == HEAD and e.start >= len(data) - 3:
            return data[:e.start].decode("utf-8")
        return None


def _entry(project: Path, folder: str, name: str) -> dict | None:
    try:
        path = files.resolve(project, f"{folder}/{name}/SKILL.md")
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limits.SKILL_MAX:
            return None
        with open(path, "rb") as f:
            text = _head_text(f.read(HEAD))
    except (Refused, OSError):
        return None
    if text is None:
        return None
    return {"name": name, "description": head(text)[0], "folder": folder}


def list_skills(project: Path) -> dict:
    """Навыки проекта без текста: папки по порядку `FOLDERS`, внутри — по имени без регистра; не больше
    `limits.SKILLS_MAX`, остальное — `truncated`. Неподходящие имена, папки без `SKILL.md`, большие, двоичные и
    ведущие за проект файлы пропускаются."""
    items = []
    for folder in FOLDERS:
        try:
            base = files.resolve(project, folder)
            with os.scandir(base) as found:
                names = sorted((e.name for e in found), key=lambda n: (n.casefold(), n))
        except (Refused, OSError):
            continue
        for name in names:
            if not NAME.fullmatch(name):
                continue
            entry = _entry(project, folder, name)
            if entry is None:
                continue
            if len(items) >= limits.SKILLS_MAX:
                return {"items": items, "truncated": True}
            items.append(entry)
    return {"items": items, "truncated": False}


def read_skill(project: Path, name: str, folder: str) -> dict:
    """Текст навыка без заголовка: папка не из `FOLDERS` или имя мимо `NAME` — `invalid`; нет файла —
    `not_found`; больше `limits.SKILL_MAX` — `too_large`; не UTF-8 или с NUL — `binary`; за проектом — `outside`."""
    if folder not in FOLDERS or not isinstance(name, str) or not NAME.fullmatch(name):
        raise Refused("invalid")
    try:
        _, data = files._read(project, f"{folder}/{name}/SKILL.md", limits.SKILL_MAX, "навык больше 64 КиБ")
    except Refused as e:
        if e.code == "not_found":
            raise Refused("not_found", "нет такого навыка") from None
        raise
    text = head(files._text(data))[1].strip()
    if not text:
        raise Refused("invalid", "навык пуст")
    return {"text": text}
