"""Навык в ходе «Чата» (спека 2026-11-25-studio-skills-status-local-models, п. 2, решения 2, 14 и 18).

Навык библиотеки учётки (`skills.py`, область `chat` или `both`) вызывает человек: ход несёт `skill_id`, и сообщение
пользователя — `текст навыка + "\\n\\n" + текст человека` (без текста человека — только текст навыка). Отметка
`messages.skill` — JSON `{"name", "chars", "parts"}`: имя, длина текста навыка в начале сообщения, «по частям». Правка
такого сообщения с `skill_keep` оставляет прежний текст навыка из самого сообщения: навык в библиотеке могли
изменить или удалить. Ссылка «Поделиться» и экспорт получают имя навыка и `human` — то, что дописал человек;
выгрузка своих данных — сообщение целиком. В журнал — ничего из навыка."""
import json

from fastapi import HTTPException

import skills

#: навык только для «Агентов» в ходе «Чата»
AGENTS_ONLY = {"code": "skill", "message": "этот навык — только для «Агентов»"}
#: `skill_keep` у сообщения без навыка
NO_SKILL = {"code": "invalid", "message": "у этого сообщения нет навыка"}


def _join(head: str, text: str) -> str:
    return head + "\n\n" + text if text.strip() else head


def _mark(raw) -> "dict | None":
    """`messages.skill` строки: {name, chars, parts} или None (нет или испорчен)."""
    try:
        mark = json.loads(raw) if raw else None
    except ValueError:
        return None
    if (not isinstance(mark, dict) or not isinstance(mark.get("name"), str)
            or not isinstance(mark.get("chars"), int) or mark["chars"] < 0):
        return None
    return {"name": mark["name"], "chars": mark["chars"], "parts": bool(mark.get("parts"))}


def apply(uid: int, body, edit) -> tuple[str, "str | None"]:
    """Текст сообщения хода `body` (`chat.TurnIn`) и JSON для `messages.skill`. `skill_id` — навык учётки (чужой или
    несуществующий — 404 `gone`, только «Агенты» — 422 `skill`); `skill_keep` — прежний текст навыка сообщения `edit`
    (без навыка — 422 `invalid`); иначе — `(body.text, None)`. Сочетания полей проверяет `chat.TurnIn`."""
    if body.skill_id is not None:
        found = skills.get(uid, body.skill_id)
        if found["scope"] not in skills.SCOPES["chat"]:
            raise HTTPException(422, AGENTS_ONLY)
        mark = {"name": found["name"], "chars": len(found["text"]), "parts": found["parts"]}
        return _join(found["text"], body.text), json.dumps(mark, ensure_ascii=False)
    if body.skill_keep:
        mark = _mark(edit["skill"]) if edit is not None else None
        if mark is None:
            raise HTTPException(422, NO_SKILL)
        return _join(edit["content"][:mark["chars"]], body.text), json.dumps(mark, ensure_ascii=False)
    return body.text, None


def view(row) -> "dict | None":
    """Навык сообщения для страницы: {name, chars, parts} или None."""
    return _mark(row["skill"])


def human(row) -> str:
    """То, что дописал человек: текст сообщения без текста навыка (`content[chars:]` без ведущей пустой строки)."""
    mark = _mark(row["skill"])
    if mark is None:
        return row["content"]
    rest = row["content"][mark["chars"]:]
    return rest[2:] if rest.startswith("\n\n") else rest


def title(mark: "str | None") -> "str | None":
    """Название диалога из отметки навыка (JSON `messages.skill`), когда человек ничего не дописал."""
    found = _mark(mark)
    return found["name"] if found is not None else None


def preview(uid: int, text: str, skill_id: "str | None") -> str:
    """Текст сообщения для оценки заполнения контекста: с текстом навыка `skill_id`; незнакомый навык — `text`."""
    if skill_id is None:
        return text
    try:
        found = skills.get(uid, skill_id)
    except HTTPException:
        return text
    return _join(found["text"], text) if found["scope"] in skills.SCOPES["chat"] else text
