"""Роли диалога (спека 2026-11-21-studio-source-foundation-design, §3): именованный текст, который чат ставит в
системное сообщение («Роль в этом диалоге — …»).

Два уровня. Умолчания (`role_defaults`) ведёт администратор в панели; первый запуск службы копирует в них
встроенный набор `BUILTIN` (флаг `settings` `role_defaults_seeded`), дальше его правки и удаления не
возвращаются. Библиотека учётки (`chat_roles`) засевается умолчаниями языка страницы при первом чтении
(`users.roles_seeded`) — одной транзакцией и один раз: опустошённая библиотека снова не засевается, вернуть
умолчания — «Добавить умолчания» (`add_defaults`, недостающие по `origin`). Правка умолчаний меняет только
посев и «Добавить умолчания» — уже засеянные библиотеки остаются как были.

Роли — настройки, а не история: «Удалить всю историю» их не трогает, удаление учётки уносит каскадом, выгрузка
(`takeout`) их содержит. Только свои: чужой id — 404. Журнал — только id и числа: имена и тексты ролей туда не
пишутся.
"""
import logging
import re
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import AfterValidator, BaseModel, ConfigDict, StringConstraints, ValidationError

import clock
import forms
import sections
import state

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/roles")

MAX_ROLES = 50
NAME_MAX = 60
TEXT_MAX = 8000
LANGS = ("ru", "en")
SEEDED = "role_defaults_seeded"
# текст — до 8000 знаков, а знак — до 12 байт в JSON с `\uXXXX` (суррогатная пара)
BODY_MAX = 128 * 1024
INPUT = {"code": "input",
         "message": f"имя роли — от 1 до {NAME_MAX} знаков одной строкой, текст — от 1 до {TEXT_MAX} знаков"}
FULL = {"code": "roles", "message": f"не больше {MAX_ROLES} ролей — удалите ненужные"}
GONE = {"code": "gone", "message": "такой роли нет"}
# NUL и одиночные суррогаты: SQLite и JSON страницы их не перенесут
BAD = re.compile("[\x00\ud800-\udfff]")

Lang = Literal["ru", "en"]

#: встроенный набор: ключ (`role_defaults.builtin`), имя, текст — по языкам; порядок — порядок в библиотеке
BUILTIN = {
    "ru": (
        ("lawyer", "Юрист",
         "Ты помогаешь разобраться в правовых вопросах: объясняешь нормы простыми словами, называешь, от чего зависит"
         " ответ (страна, даты, условия договора), и предлагаешь, какие документы собрать и что уточнить. Ссылайся на"
         " законы и практику, только если уверен в них, и прямо говори, когда не уверен. Ты не адвокат и не заменяешь"
         " его: для решений, сроков и споров советуй обратиться к юристу, который знает обстоятельства дела."),
        ("medic", "Медик",
         "Ты помогаешь понять вопросы здоровья: объясняешь термины, анализы и назначения понятным языком, описываешь,"
         " какие бывают причины симптомов и что обычно спрашивает врач. Диагнозов не ставишь и лечение не назначаешь;"
         " для решений о лечении и лекарствах советуй обратиться к врачу. При признаках неотложного состояния"
         " (сильная боль в груди, затруднённое дыхание, потеря сознания) прямо советуй вызвать скорую помощь."),
        ("translator", "Переводчик",
         "Ты переводишь тексты точно и естественно для языка перевода: сохраняешь смысл, тон, форматирование,"
         " имена и числа, не добавляешь своего. Если язык перевода не назван, переводи с русского на английский, а с"
         " любого другого языка — на русский. Неоднозначные места и термины без устоявшегося перевода отмечай коротко"
         " после перевода."),
        ("editor", "Редактор",
         "Ты редактируешь тексты: исправляешь орфографию, пунктуацию и грамматику, убираешь повторы, канцелярит и"
         " лишние слова, выстраиваешь логику абзацев, сохраняя голос и смысл автора. Сначала давай исправленный"
         " текст, затем коротко — главные правки и почему. Не переписывай текст целиком, если об этом не просили."),
        ("programmer", "Программист",
         "Ты опытный разработчик: пишешь понятный, рабочий код без лишних абстракций, объясняешь решение и его"
         " ограничения. Уточняй язык, версии и окружение, если от них зависит ответ. Предупреждай об ошибках,"
         " уязвимостях и необратимых действиях (удаление данных, миграции, force-push). Код — блоками с указанием"
         " языка; при правке — только изменённые места с контекстом."),
        ("analyst", "Аналитик",
         "Ты аналитик: раскладываешь задачу на части, отделяешь факты от предположений, считаешь и проверяешь"
         " расчёты, называешь допущения и источники данных. Выводы формулируй коротко и с уровнем уверенности, по"
         " возможности — таблицей или списком. Если данных не хватает, скажи, каких именно, а не додумывай."),
    ),
    "en": (
        ("lawyer", "Lawyer",
         "You help with legal questions: explain rules in plain language, say what the answer depends on (country,"
         " dates, contract terms), and suggest which documents to gather and what to clarify. Cite laws and case law"
         " only when you are confident, and say plainly when you are not. You are not an attorney and do not replace"
         " one: for decisions, deadlines and disputes, recommend consulting a lawyer who knows the facts of the case."),
        ("medic", "Medic",
         "You help people understand health questions: explain terms, test results and prescriptions in plain"
         " language, describe common causes of symptoms and what a doctor usually asks. You do not diagnose or"
         " prescribe; for treatment and medication decisions, recommend consulting a doctor. At signs of an emergency"
         " (severe chest pain, difficulty breathing, loss of consciousness), plainly advise calling emergency services."),
        ("translator", "Translator",
         "You translate texts accurately and naturally for the target language: keep the meaning, tone, formatting,"
         " names and numbers, and add nothing of your own. If no target language is given, translate English into"
         " Russian and any other language into English. Briefly note ambiguous passages and terms without an"
         " established translation after the translation."),
        ("editor", "Editor",
         "You edit texts: fix spelling, punctuation and grammar, remove repetition, jargon and filler, and tighten the"
         " logic of paragraphs while keeping the author's voice and meaning. Give the edited text first, then briefly"
         " list the main changes and why. Do not rewrite the whole text unless asked."),
        ("programmer", "Programmer",
         "You are an experienced developer: write clear, working code without needless abstractions and explain the"
         " solution and its limits. Ask about language, versions and environment when the answer depends on them."
         " Warn about bugs, vulnerabilities and irreversible actions (deleting data, migrations, force-push). Put code"
         " in blocks with the language named; when changing code, show only the changed parts with context."),
        ("analyst", "Analyst",
         "You are an analyst: break the problem into parts, separate facts from assumptions, calculate and check the"
         " numbers, and name your assumptions and data sources. State conclusions briefly with a confidence level,"
         " as a table or list where it helps. If data is missing, say exactly what is missing instead of guessing."),
    ),
}


def _clean(text: str) -> str:
    if BAD.search(text):
        raise ValueError("bad character")
    return text


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX, pattern=r"^[^\r\n]*$"),
                 AfterValidator(_clean)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TEXT_MAX),
                 AfterValidator(_clean)]


class RoleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name
    text: Text


class RolePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: "Name | None" = None
    text: "Text | None" = None


class DefaultIn(RoleIn):
    lang: Lang


async def read(request: Request, model: type[BaseModel]):
    """Тело с отказом словами раздела: разбор FastAPI ответил бы общим «неверные данные запроса»."""
    try:
        return model.model_validate_json(await forms.read_body(request, BODY_MAX))
    except ValidationError:
        raise HTTPException(422, INPUT) from None


def _out(row) -> dict:
    return {"id": row["id"], "name": row["name"], "text": row["text"], "position": row["position"],
            "origin": row["origin"], "created": row["created"], "updated": row["updated"]}


def _default_out(row) -> dict:
    return {"id": row["id"], "lang": row["lang"], "name": row["name"], "text": row["text"],
            "position": row["position"], "builtin": row["builtin"], "created": row["created"],
            "updated": row["updated"]}


def _rows(uid: int) -> list:
    return state.DB.execute("SELECT * FROM chat_roles WHERE user_id = ? ORDER BY position, created, id",
                            (uid,)).fetchall()


def _count(uid: int) -> int:
    return state.DB.execute("SELECT COUNT(*) FROM chat_roles WHERE user_id = ?", (uid,)).fetchone()[0]


def _next(uid: int) -> int:
    return state.DB.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM chat_roles WHERE user_id = ?",
                            (uid,)).fetchone()[0]


# ---- умолчания -------------------------------------------------------------------

def seed_defaults() -> None:
    """Один раз на базу (при старте службы): встроенный набор — в умолчания администратора."""
    with sections.transaction():
        if state.DB.execute("SELECT 1 FROM settings WHERE key = ?", (SEEDED,)).fetchone() is not None:
            return
        now = clock.NOW()
        for lang, items in BUILTIN.items():
            for position, (key, name, text) in enumerate(items):
                state.DB.execute("INSERT INTO role_defaults(lang, name, text, position, builtin, created, updated)"
                                 " VALUES (?, ?, ?, ?, ?, ?, ?)", (lang, name, text, position, key, now, now))
        state.DB.execute("INSERT INTO settings(key, value) VALUES (?, '1')", (SEEDED,))
    log.info("роли: умолчания засеяны, %d", sum(len(items) for items in BUILTIN.values()))


def defaults(lang: "str | None" = None) -> list[dict]:
    rows = state.DB.execute("SELECT * FROM role_defaults WHERE ? IS NULL OR lang = ? ORDER BY lang, position, id",
                            (lang, lang)).fetchall()
    return [_default_out(r) for r in rows]


def _default(did: int):
    row = state.DB.execute("SELECT * FROM role_defaults WHERE id = ?", (did,)).fetchone()
    if row is None:
        raise HTTPException(404, GONE)
    return row


def create_default(lang: str, name: str, text: str) -> dict:
    with sections.transaction():
        count, position = state.DB.execute("SELECT COUNT(*), COALESCE(MAX(position), -1) + 1 FROM role_defaults"
                                           " WHERE lang = ?", (lang,)).fetchone()
        if count >= MAX_ROLES:
            raise HTTPException(422, FULL)
        now = clock.NOW()
        did = state.DB.execute("INSERT INTO role_defaults(lang, name, text, position, created, updated)"
                               " VALUES (?, ?, ?, ?, ?, ?)", (lang, name, text, position, now, now)).lastrowid
    log.info("умолчание роли %d создано", did)
    return _default_out(_default(did))


def update_default(did: int, name: "str | None", text: "str | None") -> dict:
    row = _default(did)
    state.DB.execute("UPDATE role_defaults SET name = ?, text = ?, updated = ? WHERE id = ?",
                     (name or row["name"], text or row["text"], clock.NOW(), did))
    log.info("умолчание роли %d изменено", did)
    return _default_out(_default(did))


def delete_default(did: int) -> None:
    if not state.DB.execute("DELETE FROM role_defaults WHERE id = ?", (did,)).rowcount:
        raise HTTPException(404, GONE)
    log.info("умолчание роли %d удалено", did)


# ---- библиотека учётки -----------------------------------------------------------

def _copy(uid: int, lang: str, skip: set) -> int:
    """Умолчания языка, которых нет в `skip` (по id умолчания), — в конец библиотеки до `MAX_ROLES`."""
    room, position, now, added = MAX_ROLES - _count(uid), _next(uid), clock.NOW(), 0
    for row in state.DB.execute("SELECT id, name, text FROM role_defaults WHERE lang = ? ORDER BY position, id",
                                (lang,)).fetchall():
        if row["id"] in skip or added >= room:
            continue
        state.DB.execute("INSERT INTO chat_roles(id, user_id, name, text, position, origin, created, updated)"
                         " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (uuid.uuid4().hex, uid, row["name"], row["text"], position + added, row["id"], now, now))
        added += 1
    state.DB.execute("UPDATE users SET roles_seeded = ? WHERE id = ? AND roles_seeded IS NULL", (now, uid))
    return added


def list_for(uid: int, lang: str) -> list[dict]:
    """Библиотека учётки; первое чтение засевает её умолчаниями языка `lang` — один раз."""
    with sections.transaction():
        row = state.DB.execute("SELECT roles_seeded FROM users WHERE id = ?", (uid,)).fetchone()
        if row is not None and row["roles_seeded"] is None:
            added = _copy(uid, lang, set())
            log.info("роли учётки %d: засеяно %d", uid, added)
    return [_out(r) for r in _rows(uid)]


def get(uid: int, role_id: str) -> "dict | None":
    row = state.DB.execute("SELECT * FROM chat_roles WHERE id = ? AND user_id = ?", (role_id, uid)).fetchone()
    return _out(row) if row is not None else None


def create(uid: int, name: str, text: str) -> dict:
    # без await от проверки до записи: предел не гоняется с другой вкладкой
    if _count(uid) >= MAX_ROLES:
        raise HTTPException(422, FULL)
    role_id, now = uuid.uuid4().hex, clock.NOW()
    state.DB.execute("INSERT INTO chat_roles(id, user_id, name, text, position, created, updated)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)", (role_id, uid, name, text, _next(uid), now, now))
    log.info("роль %s создана", role_id[:8])
    return get(uid, role_id)


def update(uid: int, role_id: str, name: "str | None", text: "str | None") -> dict:
    role = get(uid, role_id)
    if role is None:
        raise HTTPException(404, GONE)
    state.DB.execute("UPDATE chat_roles SET name = ?, text = ?, updated = ? WHERE id = ?",
                     (name or role["name"], text or role["text"], clock.NOW(), role_id))
    log.info("роль %s изменена", role_id[:8])
    return get(uid, role_id)


def delete(uid: int, role_id: str) -> None:
    """Диалоги с этой ролью остаются без роли (триггер `chat_roles_delete`)."""
    if not state.DB.execute("DELETE FROM chat_roles WHERE id = ? AND user_id = ?", (role_id, uid)).rowcount:
        raise HTTPException(404, GONE)
    log.info("роль %s удалена", role_id[:8])


def add_defaults(uid: int, lang: str) -> list[dict]:
    """Недостающие умолчания языка — по `origin`: удалённые из библиотеки возвращаются, правленые не дублируются."""
    with sections.transaction():
        have = {r[0] for r in state.DB.execute("SELECT origin FROM chat_roles WHERE user_id = ? AND origin IS NOT NULL",
                                               (uid,))}
        added = _copy(uid, lang, have)
    log.info("роли учётки %d: добавлено умолчаний %d", uid, added)
    return [_out(r) for r in _rows(uid)]


# ---- API учётки ------------------------------------------------------------------

@router.get("")
async def list_roles(request: Request, lang: Lang = Query("ru")):
    return {"items": list_for(request.state.user.id, lang)}


@router.post("", status_code=201)
async def create_role(request: Request):
    body = await read(request, RoleIn)
    return create(request.state.user.id, body.name, body.text)


@router.post("/defaults")
async def add_default_roles(request: Request, lang: Lang = Query("ru")):
    return {"items": add_defaults(request.state.user.id, lang)}


@router.patch("/{role_id}")
async def patch_role(role_id: str, request: Request):
    body = await read(request, RolePatch)
    return update(request.state.user.id, role_id, body.name, body.text)


@router.delete("/{role_id}", status_code=204)
async def delete_role(role_id: str, request: Request):
    delete(request.state.user.id, role_id)
    return Response(status_code=204)
