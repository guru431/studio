"""Навыки (спека 2026-11-25-studio-skills-status-local-models, п. 1, 4–6): именованный текст, который человек
вызывает через «/», а Studio подставляет в сообщение. Строение — как у ролей (`roles.py`).

Два уровня. Умолчания (`skill_defaults`) ведёт администратор в панели; первый запуск службы копирует в них
встроенный набор `BUILTIN` (флаг `settings` `skill_defaults_seeded`), дальше его правки и удаления не
возвращаются. Библиотека учётки (`skills`) засевается умолчаниями языка страницы при первом чтении
(`users.skills_seeded`) — одной транзакцией и один раз: опустошённая библиотека снова не засевается, вернуть
умолчания — «Добавить умолчания» (`add_defaults`, недостающие по `origin`).

Быстрые действия «Агентов» стали навыками с отметкой `quick` («кнопкой в пустом чате»). Свой список учётки
(`users.code_quick_actions` не NULL) посев переносит в библиотеку по порядку, а быстрые умолчания пропускает —
иначе вернулись бы удалённые кнопки. Сам `users.code_quick_actions` не меняется: его читает прежний код после
отката.

Навыки — настройки, а не история: «Удалить всю историю» их не трогает, удаление учётки уносит каскадом, выгрузка
(`takeout`) их содержит. Только свои: чужой id — 404; администратор видит только умолчания. Журнал — только id и
числа: имена, описания и тексты навыков туда не пишутся.
"""
import logging
import re
import uuid
from collections import namedtuple
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictStr, StringConstraints, ValidationError

import clock
import code_actions
import forms
import sections
import state

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/skills")

NAME_MAX = 80
DESCRIPTION_MAX = 200
TEXT_MAX = 100_000
MAX_SKILLS = 100
LANGS = ("ru", "en")
SEEDED = "skill_defaults_seeded"
# текст — до 100 000 знаков, а знак — до 12 байт в JSON с `\uXXXX` (суррогатная пара)
BODY_MAX = 1536 * 1024
#: ключи встроенных быстрых навыков — по порядку `code_actions.DEFAULTS`
QUICK_KEYS = ("close-journals", "audit", "critics")
#: ключ встроенного навыка — имя папки и поле `name` файла `SKILL.md` в проекте («Агенты»)
KEY = re.compile(r"[a-z][a-z0-9-]{0,39}")
INPUT = {"code": "input",
         "message": f"имя навыка — от 1 до {NAME_MAX} знаков одной строкой, описание — до {DESCRIPTION_MAX},"
                    f" текст — от 1 до {TEXT_MAX} знаков"}
FULL = {"code": "skills", "message": f"не больше {MAX_SKILLS} навыков — удалите ненужные"}
GONE = {"code": "gone", "message": "такого навыка нет"}
ORDER = {"code": "order", "message": "список навыков изменился — обновите страницу"}
# NUL и одиночные суррогаты: SQLite и JSON страницы их не перенесут
BAD = re.compile("[\x00\ud800-\udfff]")

Lang = Literal["ru", "en"]
Scope = Literal["chat", "agents", "both"]
#: фильтр списка: навыки области и навыки «Чат и Агенты»
SCOPES = {"chat": ("chat", "both"), "agents": ("agents", "both")}

Builtin = namedtuple("Builtin", "key name description text scope quick parts")

#: тексты встроенных навыков (содержимое для модели, а не слова страницы): (язык, ключ) → текст
_TEXTS = {
    ("ru", "spec"): """Подготовь техническое задание (ТЗ) по моей просьбе ниже. Код пока не пиши.

1. Изучи проект: структуру, документацию, соглашения, тесты и то, как сейчас устроено то, что предстоит менять.
2. Запиши задачу файлом (или несколькими файлами) в папке `tasks/todo/`: что сделать и зачем, где это в коде, ограничения и как проверить результат.
3. Спрашивай только то, что нельзя решить самому по коду и документации. Каждый вопрос — с вариантами ответа и твоей рекомендацией: какой вариант выбрал бы ты и почему. Вопросы задавай одним списком.
4. Проверь «руками» доступы, которые понадобятся для проверки результата: команды тестов и сборки, адреса, учётные записи, ключи, данные. Чего не хватает — назови прямо.
5. Веди очередь в файле `tasks/index.md` с разделами `Todo`, `NeedInfo`, `Errors`, `Done`; строка — ссылка на файл задачи и одна фраза о ней. Новые задачи ставь в раздел `NeedInfo` и держи там, пока я не скажу «финализируй». После этого перенеси их в раздел `Todo`.""",

    ("en", "spec"): """Prepare a specification for my request below. Do not write code yet.

1. Study the project: its structure, documentation, conventions, tests, and how the parts you are going to change work today.
2. Write the task as a file (or several files) in the `tasks/todo/` folder: what to do and why, where it lives in the code, the constraints, and how to check the result.
3. Ask only what you cannot decide yourself from the code and documentation. Give each question answer options and your recommendation: which option you would choose and why. Ask all questions in one list.
4. Check by hand the access you will need to verify the result: test and build commands, addresses, accounts, keys, data. Name plainly whatever is missing.
5. Keep the queue in `tasks/index.md` with the sections `Todo`, `NeedInfo`, `Errors`, `Done`; each line is a link to the task file and one sentence about it. Put new tasks in `NeedInfo` and keep them there until I say "finalize". Then move them to `Todo`.""",

    ("ru", "split"): """Раздели ТЗ на задачи, которые можно выполнять по одной с чистым контекстом. Код не пиши.

Каждая задача — отдельный файл в `tasks/todo/` с разделами:
- **Цель** — что должно получиться, одной-двумя фразами.
- **Владение** — каталоги и файлы, которые задача меняет. Владение разных задач не пересекается: два исполнителя не правят один файл.
- **Зависимости** — какие задачи должны быть сделаны раньше.
- **Критерии приёмки** — проверяемые признаки готовности.
- **Команда проверки** — команда, которая подтверждает критерии: тесты, сборка, линтер.
- **Ограничения** — чего не делать: не трогать чужие файлы, не менять договорённые интерфейсы, не добавлять зависимостей без нужды.

Затем запиши порядок выполнения и отметь, какие задачи можно делать параллельно. Последняя задача — приёмка: сверить сделанное с критериями всех задач и прогнать проверки.

Строки задач добавь в `tasks/index.md` (ссылка на файл задачи и одна фраза): в раздел `Todo`, если ТЗ уже финализировано, иначе — в `NeedInfo`.""",

    ("en", "split"): """Split the specification into tasks that can be done one at a time with a clean context. Do not write code.

Each task is a separate file in `tasks/todo/` with these sections:
- **Goal** — what the result should be, in a sentence or two.
- **Ownership** — the folders and files the task changes. Ownership of different tasks does not overlap: two workers never edit the same file.
- **Dependencies** — which tasks must be done first.
- **Acceptance criteria** — checkable signs that the task is done.
- **Check command** — the command that confirms the criteria: tests, build, linter.
- **Constraints** — what not to do: do not touch other tasks' files, do not change agreed interfaces, do not add dependencies without need.

Then write down the order of work and mark which tasks can run in parallel. The last task is acceptance: compare the result with the criteria of every task and run the checks.

Add a line for each task to `tasks/index.md` (a link to the task file and one sentence): to `Todo` if the specification is already finalized, otherwise to `NeedInfo`.""",

    ("ru", "queue"): """Выполни очередь задач из `tasks/index.md` (разделы `Todo`, `NeedInfo`, `Errors`, `Done`; строка — ссылка на файл задачи и одна фраза).

Ты — головной чат: сам код не пишешь, а ведёшь очередь.
1. Возьми первую задачу из раздела `Todo` и отдай её подзадаче (субагенту) с чистым контекстом: путь к файлу задачи и просьба выполнить ровно её, не трогая чужих файлов.
2. Когда подзадача закончит, проверь результат: прогони команду проверки из задачи и сверь критерии приёмки.
3. Получилось — один коммит на задачу: `git add` только файлов этой задачи (не `git add .` и не `git add -A`) и понятное сообщение. Перенеси файл задачи в `tasks/done/`, а её строку — в раздел `Done`.
4. Неясно, как делать, — откати изменения кода этой задачи, перенеси её строку в `NeedInfo` и допиши в файл задачи вопросы с вариантами ответа.
5. Не вышло — откати изменения кода этой задачи и перенеси её строку в `Errors` с причиной одной фразой.
6. Переходи к следующей задаче из `Todo`, пока раздел не опустеет. В конце — короткий итог: что в `Done`, `NeedInfo` и `Errors`.

Если подзадач у тебя нет, выполни сам ровно одну задачу по тем же шагам и после неё попроси меня начать новый чат, чтобы следующая задача шла с чистым контекстом.""",

    ("en", "queue"): """Run the task queue from `tasks/index.md` (sections `Todo`, `NeedInfo`, `Errors`, `Done`; each line is a link to a task file and one sentence).

You are the lead chat: you do not write code yourself, you run the queue.
1. Take the first task from `Todo` and hand it to a subtask (subagent) with a clean context: the path to the task file and a request to do exactly that task without touching other files.
2. When the subtask finishes, check the result: run the task's check command and compare against its acceptance criteria.
3. If it worked, make one commit per task: `git add` only this task's files (not `git add .` or `git add -A`) with a clear message. Move the task file to `tasks/done/` and its line to `Done`.
4. If it is unclear how to do it, revert this task's code changes, move its line to `NeedInfo`, and add questions with answer options to the task file.
5. If it failed, revert this task's code changes and move its line to `Errors` with the reason in one sentence.
6. Go on to the next task in `Todo` until the section is empty. At the end, give a short summary of what is in `Done`, `NeedInfo` and `Errors`.

If you cannot start subtasks, do exactly one task yourself with the same steps, then ask me to start a new chat so the next task runs with a clean context.""",

    ("ru", "acceptance"): """Проведи приёмку сделанной работы. Код не меняй.

1. Прочитай все задачи — в `tasks/done/` и `tasks/todo/` — и их критерии приёмки.
2. По каждому критерию проверь, выполнен ли он: прогони команды проверки задач и общий набор тестов проекта, посмотри изменения в коде.
3. Дай отчёт таблицей: задача, критерий, итог — «прошло», «не прошло» или «неприменимо» — и коротко, на чём основан вывод.
4. Для «не прошло» — что именно не так и что нужно сделать; такие задачи предложи вернуть в раздел `Todo` файла `tasks/index.md`.""",

    ("en", "acceptance"): """Run acceptance of the finished work. Do not change code.

1. Read all tasks in `tasks/done/` and `tasks/todo/` and their acceptance criteria.
2. Check every criterion: run the tasks' check commands and the project's test suite, and review the code changes.
3. Report as a table: task, criterion, result — "passed", "failed" or "not applicable" — and briefly what the verdict is based on.
4. For "failed", say exactly what is wrong and what needs to be done, and suggest moving those tasks back to `Todo` in `tasks/index.md`.""",

    ("ru", "parts"): """Реши эту задачу по частям.

1. Составь план не больше чем из 7 частей и запиши его инструментом `todo_write`: каждая часть — отдельный пункт.
2. Покажи план и спроси подтверждение инструментом `ask_user`. Если я поправлю план — исправь его.
3. Выполняй части по одной: каждую — вызовом инструмента `run_part` с названием части и полным заданием для неё. Часть видит только своё задание и итоги прежних частей, но не наш диалог, поэтому пиши задание так, чтобы его хватало без диалога. После каждой части отмечай её в плане.
4. Когда все части готовы, собери из их итогов общий ответ.

Если инструмента `run_part` нет, выполни части по очереди здесь же.""",

    ("en", "parts"): """Solve this task in parts.

1. Make a plan of at most 7 parts and record it with the `todo_write` tool, one item per part.
2. Show the plan and ask for confirmation with the `ask_user` tool. If I correct the plan, update it.
3. Do the parts one at a time, each with a call to the `run_part` tool giving the part's title and its full assignment. A part sees only its assignment and the results of earlier parts, not our conversation, so write the assignment so that it stands on its own. Mark each part done in the plan.
4. When all parts are done, combine their results into one answer.

If the `run_part` tool is not available, do the parts one after another right here.""",

    ("ru", "minutes"): """Составь протокол встречи по расшифровке из этого диалога. Пиши только то, что есть в расшифровке, ничего не додумывай.

В начале — тема встречи и участники, если они понятны из расшифровки. Дальше три раздела:
- **Решения** — что решили, списком.
- **Поручения** — что сделать, кто отвечает и к какому сроку. Если ответственный или срок не названы, так и напиши: «не назван».
- **Открытые вопросы** — что осталось нерешённым или требует уточнения.""",

    ("en", "minutes"): """Write the meeting minutes from the transcript in this conversation. Use only what is in the transcript and do not invent anything.

Start with the meeting topic and the participants if the transcript makes them clear. Then three sections:
- **Decisions** — what was decided, as a list.
- **Action items** — what to do, who is responsible and by when. If the owner or the deadline was not named, say "not named".
- **Open questions** — what remains unresolved or needs clarification.""",
}


_EN_QUICK = (
    ("Resolve issues and ideas",
     "Resolve all issues and tasks in FINDINGS.md and IDEAS.md.\n"
     "Remove all resolved issues, implemented ideas, and rejected ones too. "
     "The files should contain minimal information — only the header."),
    ("Deep project audit",
     "Analyze the project in depth for code bugs and logic errors, and suggest functional improvements\n"
     "Analyze the conclusions in all documents. Find inconsistencies in logic or conclusions, or things you disagree with.\n"
     "Suggest functional improvements or new ideas and capabilities.\n"
     "Record everything you find in the corresponding files — FINDINGS.md and IDEAS.md\n"
     "Also pay special attention to the theme design — styling, features, the set of menus and buttons. Think about how it can be improved"),
    ("Independent critics",
     "Run this project through an independent adversarial orchestration: several critics with different lenses.\n"
     "Record everything you find in the corresponding files — FINDINGS.md and IDEAS.md"),
)

#: встроенный набор по языкам: порядок — порядок в библиотеке; ключ — `skill_defaults.builtin` и `origin` навыка
BUILTIN = {
    "ru": (
        Builtin("spec", "Подготовить ТЗ", "Изучить проект, задать нужные вопросы и записать задачи в tasks/todo/.",
                _TEXTS["ru", "spec"], "agents", False, False),
        Builtin("split", "Разбить на задачи", "Разделить ТЗ на задачи без пересечений по файлам, с критериями и проверкой.",
                _TEXTS["ru", "split"], "agents", False, False),
        Builtin("queue", "Выполнить очередь", "Выполнять задачи из Todo по одной в подзадачах, с проверкой и коммитом.",
                _TEXTS["ru", "queue"], "agents", False, False),
        Builtin("acceptance", "Приёмка", "Сверить сделанное с критериями всех задач и дать отчёт.",
                _TEXTS["ru", "acceptance"], "agents", False, False),
        *(Builtin(key, action["title"], "", action["text"], "agents", True, False)
          for key, action in zip(QUICK_KEYS, code_actions.DEFAULTS)),
        Builtin("parts", "Разобрать по частям", "Составить план до 7 частей и выполнить каждую отдельным запросом.",
                _TEXTS["ru", "parts"], "chat", False, True),
        Builtin("minutes", "Протокол встречи", "Решения, поручения и открытые вопросы по расшифровке в диалоге.",
                _TEXTS["ru", "minutes"], "chat", False, False),
    ),
    "en": (
        Builtin("spec", "Prepare the spec", "Study the project, ask what is needed and write tasks to tasks/todo/.",
                _TEXTS["en", "spec"], "agents", False, False),
        Builtin("split", "Split into tasks", "Split the spec into tasks with separate files, criteria and a check.",
                _TEXTS["en", "split"], "agents", False, False),
        Builtin("queue", "Run the queue", "Run Todo tasks one at a time in subtasks, with a check and a commit each.",
                _TEXTS["en", "queue"], "agents", False, False),
        Builtin("acceptance", "Acceptance", "Compare the work with the criteria of all tasks and report.",
                _TEXTS["en", "acceptance"], "agents", False, False),
        *(Builtin(key, name, "", text, "agents", True, False) for key, (name, text) in zip(QUICK_KEYS, _EN_QUICK)),
        Builtin("parts", "Work in parts", "Plan up to 7 parts and run each one as a separate request.",
                _TEXTS["en", "parts"], "chat", False, True),
        Builtin("minutes", "Meeting minutes", "Decisions, action items and open questions from a transcript in the chat.",
                _TEXTS["en", "minutes"], "chat", False, False),
    ),
}


def _clean(text: str) -> str:
    if BAD.search(text):
        raise ValueError("bad character")
    return text


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=NAME_MAX, pattern=r"^[^\r\n]*$"),
                 AfterValidator(_clean)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=DESCRIPTION_MAX, pattern=r"^[^\r\n]*$"),
                        AfterValidator(_clean)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=TEXT_MAX),
                 AfterValidator(_clean)]


class SkillIn(BaseModel):
    """Навык целиком: PUT заменяет все поля, неприсланные — умолчаниями модели."""
    model_config = ConfigDict(extra="forbid")
    name: Name
    description: Description = ""
    text: Text
    scope: Scope
    quick: bool = False
    parts: bool = False


class DefaultIn(SkillIn):
    lang: Lang


class OrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ids: list[StrictStr] = Field(max_length=MAX_SKILLS)


async def read(request: Request, model: type[BaseModel]):
    """Тело с отказом словами раздела: разбор FastAPI ответил бы общим «неверные данные запроса»."""
    try:
        return model.model_validate_json(await forms.read_body(request, BODY_MAX))
    except ValidationError:
        raise HTTPException(422, INPUT) from None


def _fields(body: SkillIn) -> tuple:
    return body.name, body.description, body.text, body.scope, int(body.quick), int(body.parts)


def _out(row, text: bool = True) -> dict:
    item = {"id": row["id"], "name": row["name"], "description": row["description"], "scope": row["scope"],
            "quick": bool(row["quick"]), "parts": bool(row["parts"]), "origin": row["origin"]}
    if text:
        item["text"] = row["text"]
    return item


def _default_out(row) -> dict:
    return {"id": row["id"], "lang": row["lang"], "builtin": row["builtin"], "name": row["name"],
            "description": row["description"], "text": row["text"], "scope": row["scope"],
            "quick": bool(row["quick"]), "parts": bool(row["parts"]), "position": row["position"]}


def _rows(uid: int, scope: "str | None" = None) -> list:
    shown = SCOPES.get(scope, ("chat", "agents", "both"))
    marks = ", ".join("?" * len(shown))
    return state.DB.execute(f"SELECT * FROM skills WHERE user_id = ? AND scope IN ({marks}) ORDER BY position, id",
                            (uid, *shown)).fetchall()


def _count(uid: int) -> int:
    return state.DB.execute("SELECT COUNT(*) FROM skills WHERE user_id = ?", (uid,)).fetchone()[0]


def _next(uid: int) -> int:
    return state.DB.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM skills WHERE user_id = ?",
                            (uid,)).fetchone()[0]


def _seeded(uid: int) -> bool:
    row = state.DB.execute("SELECT skills_seeded FROM users WHERE id = ?", (uid,)).fetchone()
    return row is None or row["skills_seeded"] is not None


# ---- умолчания -------------------------------------------------------------------

def seed_defaults() -> None:
    """Один раз на базу (при старте службы): встроенный набор — в умолчания администратора."""
    with sections.transaction():
        if state.DB.execute("SELECT 1 FROM settings WHERE key = ?", (SEEDED,)).fetchone() is not None:
            return
        for lang, items in BUILTIN.items():
            for position, b in enumerate(items):
                state.DB.execute("INSERT INTO skill_defaults(builtin, lang, name, description, text, scope, quick, parts,"
                                 " position) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 (b.key, lang, b.name, b.description, b.text, b.scope, int(b.quick), int(b.parts),
                                  position))
        state.DB.execute("INSERT INTO settings(key, value) VALUES (?, '1')", (SEEDED,))
    log.info("навыки: умолчания засеяны, %d", sum(len(items) for items in BUILTIN.values()))


def defaults(lang: "str | None" = None) -> list[dict]:
    rows = state.DB.execute("SELECT * FROM skill_defaults WHERE ? IS NULL OR lang = ? ORDER BY lang, position, id",
                            (lang, lang)).fetchall()
    return [_default_out(r) for r in rows]


def _default(did: int):
    row = state.DB.execute("SELECT * FROM skill_defaults WHERE id = ?", (did,)).fetchone()
    if row is None:
        raise HTTPException(404, GONE)
    return row


def create_default(body: DefaultIn) -> dict:
    with sections.transaction():
        count, position = state.DB.execute("SELECT COUNT(*), COALESCE(MAX(position), -1) + 1 FROM skill_defaults"
                                           " WHERE lang = ?", (body.lang,)).fetchone()
        if count >= MAX_SKILLS:
            raise HTTPException(422, FULL)
        did = state.DB.execute("INSERT INTO skill_defaults(lang, name, description, text, scope, quick, parts, position)"
                               " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (body.lang, *_fields(body), position)).lastrowid
    log.info("умолчание навыка %d создано", did)
    return _default_out(_default(did))


def update_default(did: int, body: SkillIn) -> dict:
    _default(did)
    state.DB.execute("UPDATE skill_defaults SET name = ?, description = ?, text = ?, scope = ?, quick = ?, parts = ?"
                     " WHERE id = ?", (*_fields(body), did))
    log.info("умолчание навыка %d изменено", did)
    return _default_out(_default(did))


def delete_default(did: int) -> None:
    if not state.DB.execute("DELETE FROM skill_defaults WHERE id = ?", (did,)).rowcount:
        raise HTTPException(404, GONE)
    log.info("умолчание навыка %d удалено", did)


# ---- библиотека учётки -----------------------------------------------------------

def _insert(uid: int, rows: list[tuple]) -> int:
    """Строки (origin, name, description, text, scope, quick, parts) — в конец библиотеки до `MAX_SKILLS`."""
    rows = rows[:max(0, MAX_SKILLS - _count(uid))]
    position, now = _next(uid), clock.NOW()
    for n, (origin, *fields) in enumerate(rows):
        state.DB.execute("INSERT INTO skills(id, user_id, origin, name, description, text, scope, quick, parts,"
                         " position, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (uuid.uuid4().hex, uid, origin, *fields, position + n, now))
    return len(rows)


def _from_defaults(lang: str, skip, quick: bool = True) -> list[tuple]:
    """Умолчания языка по порядку, кроме `skip` (по `origin`); без быстрых — при `quick=False`."""
    rows = []
    for row in state.DB.execute("SELECT * FROM skill_defaults WHERE lang = ? ORDER BY position, id", (lang,)):
        origin = row["builtin"] or f"default-{row['id']}"
        if origin not in skip and (quick or not row["quick"]):
            rows.append((origin, row["name"], row["description"], row["text"], row["scope"], row["quick"], row["parts"]))
    return rows


def _seed(uid: int, lang: str) -> None:
    """Посев (внутри транзакции): свои быстрые действия, затем умолчания языка; отметка `users.skills_seeded`."""
    saved = state.DB.execute("SELECT code_quick_actions FROM users WHERE id = ?", (uid,)).fetchone()[0]
    rows = []
    if saved is not None:
        for action in code_actions.for_account(saved):
            origin = next((key for key, known in zip(QUICK_KEYS, code_actions.DEFAULTS)
                           if known["text"] == action["text"]), None)
            rows.append((origin, action["title"], "", action["text"], "agents", 1, 0))
    moved = len(rows)
    added = _insert(uid, rows + _from_defaults(lang, set(), quick=saved is None))
    state.DB.execute("UPDATE users SET skills_seeded = ? WHERE id = ?", (clock.NOW(), uid))
    log.info("навыки учётки %d: засеяно %d, из них быстрых действий %d", uid, added, min(moved, added))


def list_for(uid: int, lang: str, scope: "str | None" = None) -> list[dict]:
    """Библиотека учётки без текстов; первое чтение засевает её умолчаниями языка `lang` — один раз."""
    with sections.transaction():
        if not _seeded(uid):
            _seed(uid, lang)
    return [_out(r, text=False) for r in _rows(uid, scope)]


def get(uid: int, skill_id: str) -> dict:
    row = state.DB.execute("SELECT * FROM skills WHERE id = ? AND user_id = ?", (skill_id, uid)).fetchone()
    if row is None:
        raise HTTPException(404, GONE)
    return _out(row)


def quick_actions(uid: int) -> "list[dict] | None":
    """Кнопки пустого чата «Агентов» из навыков; None — библиотека не засеяна (действуют прежние быстрые действия)."""
    if not _seeded(uid):
        return None
    return [{"title": r["name"], "text": r["text"]} for r in _rows(uid, "agents") if r["quick"]]


def create(uid: int, body: SkillIn) -> dict:
    # без await от проверки до записи: предел не гоняется с другой вкладкой
    if _count(uid) >= MAX_SKILLS:
        raise HTTPException(422, FULL)
    skill_id = uuid.uuid4().hex
    state.DB.execute("INSERT INTO skills(id, user_id, name, description, text, scope, quick, parts, position, updated)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (skill_id, uid, *_fields(body), _next(uid), clock.NOW()))
    log.info("навык %s создан", skill_id[:8])
    return get(uid, skill_id)


def replace(uid: int, skill_id: str, body: SkillIn) -> dict:
    get(uid, skill_id)
    state.DB.execute("UPDATE skills SET name = ?, description = ?, text = ?, scope = ?, quick = ?, parts = ?,"
                     " updated = ? WHERE id = ? AND user_id = ?", (*_fields(body), clock.NOW(), skill_id, uid))
    log.info("навык %s изменён", skill_id[:8])
    return get(uid, skill_id)


def reorder(uid: int, ids: list[str]) -> list[dict]:
    """Новый порядок — перестановка всех своих id; иначе (список изменился в другой вкладке, чужой id) — 409."""
    with sections.transaction():
        own = [r[0] for r in state.DB.execute("SELECT id FROM skills WHERE user_id = ?", (uid,))]
        if len(ids) != len(own) or set(ids) != set(own):
            raise HTTPException(409, ORDER)
        for position, skill_id in enumerate(ids):
            state.DB.execute("UPDATE skills SET position = ? WHERE id = ? AND user_id = ?", (position, skill_id, uid))
    log.info("навыки учётки %d: порядок изменён, %d", uid, len(ids))
    return [_out(r, text=False) for r in _rows(uid)]


def delete(uid: int, skill_id: str) -> None:
    if not state.DB.execute("DELETE FROM skills WHERE id = ? AND user_id = ?", (skill_id, uid)).rowcount:
        raise HTTPException(404, GONE)
    log.info("навык %s удалён", skill_id[:8])


def add_defaults(uid: int, lang: str) -> list[dict]:
    """Недостающие умолчания языка (и быстрые) — по `origin`: удалённые возвращаются, правленые не дублируются.
    Незасеянная библиотека засевается, как при первом чтении."""
    with sections.transaction():
        if not _seeded(uid):
            _seed(uid, lang)
        else:
            have = {r[0] for r in state.DB.execute("SELECT origin FROM skills WHERE user_id = ? AND origin IS NOT NULL",
                                                   (uid,))}
            added = _insert(uid, _from_defaults(lang, have))
            log.info("навыки учётки %d: добавлено умолчаний %d", uid, added)
    return [_out(r, text=False) for r in _rows(uid)]


# ---- API учётки ------------------------------------------------------------------

@router.get("")
async def list_skills(request: Request, scope: "Literal['chat', 'agents'] | None" = Query(None),
                      lang: Lang = Query("ru")):
    return {"items": list_for(request.state.user.id, lang, scope)}


@router.post("", status_code=201)
async def create_skill(request: Request):
    return create(request.state.user.id, await read(request, SkillIn))


@router.put("/order")
async def order_skills(request: Request):
    body = await read(request, OrderIn)
    return {"items": reorder(request.state.user.id, body.ids)}


@router.post("/defaults")
async def add_default_skills(request: Request, lang: Lang = Query("ru")):
    return {"items": add_defaults(request.state.user.id, lang)}


@router.get("/{skill_id}")
async def get_skill(skill_id: str, request: Request):
    return get(request.state.user.id, skill_id)


@router.put("/{skill_id}")
async def put_skill(skill_id: str, request: Request):
    body = await read(request, SkillIn)
    return replace(request.state.user.id, skill_id, body)


@router.delete("/{skill_id}", status_code=204)
async def delete_skill(skill_id: str, request: Request):
    delete(request.state.user.id, skill_id)
    return Response(status_code=204)
