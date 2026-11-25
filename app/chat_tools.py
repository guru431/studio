"""Встроенные инструменты чата, не из «Интернета» (`web.py`): их предлагает сервер в каждом ходе через сервер, кроме
Cleaner (`chat.StreamTask._read_model`). Спека 2026-11-21-studio-source-capabilities-design:

- `ask_user` (п. 17): вопрос пользователю кнопками; ход встаёт на паузу (`chat_pending.pause`), ответ продолжает
  тот же ответ;
- `todo_write` (п. 18): видимый план многошаговой работы — `messages.todo` ответа и событие SSE `todo`;
- `memory_save` / `memory_forget` (п. 18): личная память (`memory`), только при `users.memory_on`;
- `knowledge_search` (п. 15): поиск по базам знаний и большим вложениям диалога (`knowledge`);
- `run_part` (спека 2026-11-25-studio-skills-status-local-models, п. 7): часть задачи отдельным запросом — исполняет
  `chat_parts.run` (сюда — только описание, счёт и отказы: `chat_parts` импортирует этот модуль).

Новый инструмент: спецификация, строка в `KIND_OF`, ветка в `call`. В журнал — ничего: вопрос, ответ, план и
запрос пишет только шаг в `messages.tools` и `messages.todo`, текст памяти — только `user_memory` шифром."""
import json

from fastapi import HTTPException

import chat_pending
import knowledge
import memory
import state
import web

QUESTION_MAX = 500
OPTIONS_MAX = 6
OPTION_MAX = 80
NO_ANSWER = "пользователь не ответил"
BAD_ARGS = "неверные аргументы ask_user — нужен вопрос до 500 знаков и не больше 6 вариантов до 80 знаков"

ASK_USER = {"type": "function", "function": {
    "name": "ask_user",
    "description": ("Спросить пользователя и дождаться ответа. Вызывай, когда нужен выбор или сведения, которые "
                    "знает только пользователь; если ответ — один из немногих, дай короткие варианты."),
    "parameters": {"type": "object", "properties": {
        "question": {"type": "string", "maxLength": QUESTION_MAX, "description": "вопрос на языке пользователя"},
        "options": {"type": "array", "maxItems": OPTIONS_MAX, "items": {"type": "string", "maxLength": OPTION_MAX},
                    "description": "варианты ответа — кнопки"},
        "allow_text": {"type": "boolean", "default": True, "description": "можно ли ответить своим текстом"}},
        "required": ["question"]}}}

TODO_MAX = 20
TODO_TEXT_MAX = 200
TODO_STATUSES = ("pending", "in_progress", "done")
TODO_BAD = (f"неверные аргументы todo_write — нужен список items до {TODO_MAX} пунктов: text до {TODO_TEXT_MAX} знаков "
            "и status pending, in_progress или done")
TODO_SAVED = "План сохранён: пунктов — {0}. Пользователь видит его над ответом."

TODO_WRITE = {"type": "function", "function": {
    "name": "todo_write",
    "description": ("Показать пользователю план многошаговой работы и отмечать ход: вызывай в начале такой работы и "
                    "после каждого шага, каждый раз со всем списком. Простым вопросам план не нужен."),
    "parameters": {"type": "object", "properties": {
        "items": {"type": "array", "maxItems": TODO_MAX, "items": {"type": "object", "properties": {
            "text": {"type": "string", "maxLength": TODO_TEXT_MAX, "description": "шаг на языке пользователя"},
            "status": {"type": "string", "enum": list(TODO_STATUSES)}},
            "required": ["text", "status"]}}},
        "required": ["items"]}}}

MEMORY_OFF = "память выключена пользователем"
MEMORY_GONE = "такой записи памяти нет — номер из блока «Что известно о пользователе»"
MEMORY_SAVED = "Запомнено."
MEMORY_FORGOTTEN = "Запись удалена из памяти."
#: память идёт в системное сообщение всех диалогов (и с «Интернетом», и прямых облачных): после защищённого
#: источника (`task.protected`, chats.protected_used) `memory_save` не пишет
MEMORY_PROTECTED = "после защищённого источника память не пишется"

MEMORY_SAVE = {"type": "function", "function": {
    "name": "memory_save",
    "description": ("Запомнить о пользователе устойчивый факт или предпочтение на будущие диалоги (имя, язык, стиль "
                    "ответов, постоянные обстоятельства). Не записывай разовое, секреты, пароли и чужие данные."),
    "parameters": {"type": "object", "properties": {
        "text": {"type": "string", "maxLength": memory.ITEM_MAX, "description": "одна короткая запись"}},
        "required": ["text"]}}}
MEMORY_FORGET = {"type": "function", "function": {
    "name": "memory_forget",
    "description": "Удалить запись из памяти о пользователе — устаревшую или по его просьбе.",
    "parameters": {"type": "object", "properties": {
        "id": {"type": "string", "description": "номер записи в квадратных скобках из «Что известно о пользователе»"}},
        "required": ["id"]}}}

#: поиск по базам знаний и большим вложениям диалога (knowledge.py, п. 15): предлагается, когда у учётки есть базы
#: или у диалога — большие вложения (`knowledge.offered`); без `kb_ids` — базы диалога (не выбраны — все видимые) и
#: большие вложения диалога. Шаг — `{kind: knowledge, query, found, sources}`, найденное — модели в `text`
KNOWLEDGE_SEARCH = {"type": "function", "function": {
    "name": "knowledge_search",
    "description": ("Найти куски документов в базах знаний пользователя и в больших документах этого диалога. "
                    "Вызывай, когда ответ может быть в документах; запрос — своими словами о сути."),
    "parameters": {"type": "object", "properties": {
        "query": {"type": "string", "maxLength": knowledge.QUERY_MAX, "description": "что искать"},
        "kb_ids": {"type": "array", "maxItems": 16, "items": {"type": "string"},
                   "description": "id баз знаний; без него — базы диалога"}},
        "required": ["query"]}}}
KNOWLEDGE_ARGS = "неверные аргументы knowledge_search — нужен запрос до 500 знаков и список id баз"

#: часть большой задачи отдельным запросом с чистым контекстом (спека 2026-11-25-studio-skills-status-local-models,
#: п. 7, решение 15; исполняет `chat_parts.run`): предлагается, пока у последнего навыка диалога есть «по частям»
PARTS_MAX = 10                   # частей на ответ
PART_RESULT_MAX = 4000           # знаков итога части — модели и в шаг
PART_ROUNDS = 8                  # кругов инструментов внутри части, затем запрос без них
PART_TITLE_MAX = 200
PART_TASK_MAX = 20_000
#: внутри части не предлагаются и отказываются словами NOT_IN_PART: вложенные части, план, вопрос, запись в память
PART_EXCLUDED = {"run_part", "todo_write", "ask_user", "memory_save"}
NOT_IN_PART = "этот инструмент недоступен внутри части"
#: шаг части без итога и ошибки (ответ прервался, пока часть шла) — модели в следующих ходах
PART_CUT = "часть не закончилась — итога нет"

RUN_PART = {"type": "function", "function": {
    "name": "run_part",
    "description": ("Выполнить одну часть большой задачи отдельным запросом с чистым контекстом: он видит только "
                    "задание части и итоги прежних частей этого ответа, но не переписку. Пиши задание полностью и "
                    "самодостаточно; вызывай по одной части за раз и жди её итога."),
    "parameters": {"type": "object", "properties": {
        "title": {"type": "string", "maxLength": PART_TITLE_MAX, "description": "короткое название части"},
        "task": {"type": "string", "maxLength": PART_TASK_MAX,
                 "description": "задание части целиком: что сделать и что вернуть итогом"}},
        "required": ["title", "task"]}}}


#: вид шага `messages.tools` каждого встроенного инструмента
KIND_OF: dict[str, str] = {"ask_user": "ask", "todo_write": "todo", "memory_save": "memory", "memory_forget": "memory",
                           "knowledge_search": "knowledge"}
KIND_OF["run_part"] = "part"
NAMES: set[str] = set(KIND_OF)
KINDS: set[str] = set(KIND_OF.values())          # виды их шагов в `messages.tools`


def for_model(step: dict) -> str:
    """Результат шага встроенного инструмента для модели; ошибка — без приписки «Интернета» (web.NOTHING). Шаг части
    без итога (прерван, пока шёл) — PART_CUT."""
    if "error" not in step and step["kind"] == "part" and not step.get("text"):
        return PART_CUT
    return f"Ошибка инструмента: {step['error']}" if "error" in step else step["text"]


def specs(task) -> list[dict]:
    """Встроенные инструменты хода `task` (`chat.StreamTask`): вопрос и план — всегда, память — только при
    `users.memory_on`, поиск по знаниям — когда есть базы или большие вложения (`knowledge.offered`), `run_part` —
    последним, пока последний навык диалога «по частям» (`parts_offered`)."""
    return ([ASK_USER, TODO_WRITE] + ([MEMORY_SAVE, MEMORY_FORGET] if memory.enabled(task.user_id) else [])
            + ([_knowledge_spec(task)] if knowledge.offered(task.user_id, task.chat_id) else [])
            + ([RUN_PART] if parts_offered(task.chat_id) else []))


def parts_offered(chat_id: str) -> bool:
    """`run_part` предлагается (решение 14): последнее по порядку выбранное сообщение диалога с навыком
    (`messages.skill`) несёт отметку «по частям»."""
    row = state.DB.execute("SELECT skill FROM messages WHERE chat_id = ? AND unchosen = 0 AND skill IS NOT NULL"
                           " ORDER BY created DESC, rowid DESC LIMIT 1", (chat_id,)).fetchone()
    if row is None:
        return False
    try:
        mark = json.loads(row[0])
    except ValueError:
        return False
    return isinstance(mark, dict) and mark.get("parts") is True


def part_specs(task) -> list[dict]:
    """Встроенные инструменты внутри части: те же, что у хода, кроме PART_EXCLUDED."""
    return [spec for spec in specs(task) if spec["function"]["name"] not in PART_EXCLUDED]


def part_refusal(call, words: str) -> dict:
    """Шаг отказа `run_part` до запуска (без номера и длительности): название — из аргументов, если оно строка."""
    title = (_args(call.arguments) or {}).get("title")
    return {"kind": "part", "title": title.strip()[:PART_TITLE_MAX] if isinstance(title, str) else "",
            "state": "error", "error": words}


def _args(arguments: str) -> "dict | None":
    try:
        args = json.loads(arguments) if arguments.strip() else {}
    except ValueError:
        return None
    return args if isinstance(args, dict) else None


def todo_items(arguments: str) -> "list[dict] | None":
    """План из аргументов `todo_write` (`[{text, status}]`, текст без пробелов по краям) или None — неверные."""
    args = _args(arguments)
    items = args.get("items") if args is not None else None
    if not isinstance(items, list) or len(items) > TODO_MAX:
        return None
    out = []
    for item in items:
        text = item.get("text") if isinstance(item, dict) else None
        if (not isinstance(text, str) or not text.strip() or len(text) > TODO_TEXT_MAX
                or item.get("status") not in TODO_STATUSES):
            return None
        out.append({"text": text.strip(), "status": item["status"]})
    return out


def _todo(task, call) -> dict:
    """План ответа: в `messages.todo` идущего ответа и событием SSE `todo {items}` (блок плана над ответом)."""
    items = todo_items(call.arguments)
    if items is None:
        return {"kind": "todo", "error": TODO_BAD}
    state.DB.execute("UPDATE messages SET todo = ? WHERE id = ? AND status = 'streaming'",
                     (json.dumps(items, ensure_ascii=False), task.message_id))
    task.fresh = True
    task.put("todo", {"items": items})
    return {"kind": "todo", "text": TODO_SAVED.format(len(items))}


async def _memory(task, call) -> dict:
    """`memory_save` / `memory_forget`: только при `users.memory_on`; `memory_save` после защищённого источника —
    MEMORY_PROTECTED. Шаг текста записи не хранит (память — шифром),
    `memory_forget` принимает номер из блока — первые знаки id своей записи."""
    action = "save" if call.name == "memory_save" else "forget"
    if not memory.enabled(task.user_id):
        return {"kind": "memory", "action": action, "error": MEMORY_OFF}
    args = _args(call.arguments) or {}
    if action == "save":
        if task.protected:
            return {"kind": "memory", "action": action, "error": MEMORY_PROTECTED}
        text = args.get("text")
        if not isinstance(text, str) or not 0 < len(text.strip()) <= memory.ITEM_MAX:
            return {"kind": "memory", "action": action, "error": memory.INVALID["message"]}
        if call is not task.confirmed:
            await chat_pending.pause(task, "confirm", call, {"memory_text": text.strip()})
            return {}
        try:
            memory.save(task.user_id, text)
        except HTTPException as exc:
            return {"kind": "memory", "action": action, "error": exc.detail["message"]}
        return {"kind": "memory", "action": action, "text": MEMORY_SAVED}
    handle = args.get("id")
    found = [item["id"] for item in memory.list_for(task.user_id)
             if isinstance(handle, str) and len(handle.strip()) >= 6 and item["id"].startswith(handle.strip().lower())]
    if len(found) != 1 or not memory.forget(task.user_id, found[0]):
        return {"kind": "memory", "action": action, "error": MEMORY_GONE}
    return {"kind": "memory", "action": action, "text": MEMORY_FORGOTTEN}


def _knowledge_spec(task) -> dict:
    """`knowledge_search` с перечнем видимых баз (id и имя) в описании: иначе модели неоткуда взять `kb_ids`."""
    listed = "; ".join(f'{b["id"]} — {b["name"]}' for b in knowledge.bases(task.user_id))
    if not listed:
        return KNOWLEDGE_SEARCH
    spec = json.loads(json.dumps(KNOWLEDGE_SEARCH))
    spec["function"]["description"] += f" Базы: {listed}."
    return spec


async def _knowledge(task, call) -> dict:
    try:
        args = json.loads(call.arguments) if call.arguments.strip() else {}
    except ValueError:
        args = None
    query = args.get("query") if isinstance(args, dict) else None
    ids = args.get("kb_ids") if isinstance(args, dict) else None
    if (not isinstance(query, str) or not query.strip() or len(query) > knowledge.QUERY_MAX
            or (ids is not None and (not isinstance(ids, list) or len(ids) > 16
                                     or any(not isinstance(i, str) for i in ids)))):
        return {"kind": "knowledge", "error": KNOWLEDGE_ARGS}
    if ids:
        scope = {"kb_ids": ids}
    else:
        chosen = knowledge.chat_kbs(task.user_id, task.chat_id)
        scope = {"kb_ids": chosen or [b["id"] for b in knowledge.bases(task.user_id)],
                 "attachment_ids": list(knowledge.big_attachments(task.chat_id))}
    hits = await knowledge.search(task.user_id, query.strip(), **scope, cache=task.knowledge_cache,
                                  vectors=knowledge.vectors_for(task.conn))
    return {"kind": "knowledge", "query": query.strip(), "found": len(hits),
            "sources": list(dict.fromkeys(h.doc_name for h in hits)),
            "text": knowledge.render(hits) if hits else knowledge.NOTHING}


def _ask_view(arguments: str) -> "dict | None":
    """Вид карточки вопроса из аргументов модели или None — аргументы неверные."""
    try:
        args = json.loads(arguments) if arguments.strip() else {}
    except ValueError:
        return None
    if not isinstance(args, dict):
        return None
    question, options, allow = args.get("question"), args.get("options", []), args.get("allow_text", True)
    if (not isinstance(question, str) or not question.strip() or len(question) > QUESTION_MAX
            or not isinstance(options, list) or len(options) > OPTIONS_MAX
            or any(not isinstance(o, str) or not o.strip() or len(o) > OPTION_MAX for o in options)
            or not isinstance(allow, bool)):
        return None
    options = [o.strip() for o in options]
    return {"server": None, "tool": "ask_user", "arguments": [], "question": question.strip(), "options": options,
            "allow_text": allow or not options}


async def call(task, call) -> dict:
    """Вызов встроенного инструмента: шаг ответа. `ask_user` с верными аргументами ставит ход на паузу
    (`task.paused`): шаг даст ответ пользователя (`answer_step`). Внутри части (`task.part`) имена PART_EXCLUDED —
    отказ NOT_IN_PART; `run_part`, дошедший сюда, модели не предлагали — web.NOT_OFFERED."""
    if getattr(task, "part", None) is not None and call.name in PART_EXCLUDED:
        return ({"kind": KIND_OF[call.name], "error": NOT_IN_PART}
                | ({"action": "save"} if call.name == "memory_save" else {}))
    if call.name == "run_part":
        return part_refusal(call, web.NOT_OFFERED)
    if call.name == "todo_write":
        return _todo(task, call)
    if call.name in ("memory_save", "memory_forget"):
        return await _memory(task, call)
    if call.name == "knowledge_search":
        return await _knowledge(task, call)
    view = _ask_view(call.arguments)
    if view is None:
        return {"kind": "ask", "error": BAD_ARGS}
    await chat_pending.pause(task, "ask", call, view)
    return {"kind": "ask", "question": view["question"]}


def answer_step(view: dict, action: str, answer: "str | None") -> dict:
    """Шаг ответа на вопрос: модели — JSON `{"question", "answer": текст}`, отказ — `{"question", "answer": null,
    "note": NO_ANSWER}`: вопрос — в результате, следующие ходы видят его в истории (chat.compose)."""
    result = {"question": view["question"]} | (
        {"answer": answer} if action == "answer" else {"answer": None, "note": NO_ANSWER})
    return {"kind": "ask", "question": view["question"], "text": json.dumps(result, ensure_ascii=False)}
