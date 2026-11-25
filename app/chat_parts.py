"""Ответ по частям в «Чате» (спека 2026-11-25-studio-skills-status-local-models, п. 7, решения 14 и 15): встроенный
инструмент `run_part` (описание и счёт — `chat_tools`) делает отдельный запрос той же модели тем же путём, что ход
(`chat.StreamTask._round`, в том числе через `cleaner_route`), с чистым контекстом: системное сообщение хода, записка
NOTE, задание части и выжимки (`chat.history_excerpt`) итогов прежних частей этого ответа; истории диалога в запросе
нет. Внутри части — инструменты хода без `chat_tools.PART_EXCLUDED` и без MCP с подтверждением; вызовы проходят те же
проверки, что у хода (`StreamTask._tools`: защищённый источник, «Интернет», повторы, CALLS_PER_ROUND), не больше
`chat_tools.PART_ROUNDS` кругов, затем запрос без инструментов. Частей — не больше `chat_tools.PARTS_MAX` на ответ и
одна за круг; круг, где шли только части, в счёт кругов хода не входит. Ответ с частями живёт до `chat.PARTS_TOTAL_SEC`,
часть — до срока обычного ответа (`chat.STREAM_TOTAL_SEC`); ожидание карты в сроки не входит.

Шаг ответа — `{"kind": "part", "n", "title", "state": "running" | "done" | "error", "ms", "text"}` (у ошибки — `error`;
отказ до запуска — без `n` и `ms`); шаги инструментов внутри части — обычные шаги с `"part": n` и пустым `text`
(`StreamTask._step`): следующим ходам итог части идёт через её шаг. События SSE — `tool {kind: "part", label}` и
`part {n, title, state[, ms]}`. В журнал — номер части, состояние и миллисекунды: ни названия, ни задания, ни итога."""
import asyncio
import html
import logging
import math

import chat
import chat_tools
import clock
import sections
import web

BAD_ARGS = "неверные аргументы run_part — нужны название и задание части"
ONE_PER_ROUND = "за один круг выполняется одна часть — вызови следующую после итога этой"
LIMIT = f"не больше {chat_tools.PARTS_MAX} частей в одном ответе — собери ответ из готовых итогов"
TIME = "часть шла дольше срока ответа — остановлена"
EMPTY = "модель не вернула итог части"
STOPPED = "часть остановлена"
NEEDS_CONFIRM = "инструмент с подтверждением недоступен внутри части"
#: записка модели части — перед заданием в единственном сообщении пользователя
NOTE = ("You are carrying out one part of a larger task in a separate request. Do only the task inside <part>; the "
        "results of earlier parts, if any, are in <done-part> blocks. Use tools only when the part needs them. Reply "
        "with the result of this part only: it goes back to the main conversation.")

log = logging.getLogger("studio")


def _args(call) -> "tuple[str, str] | None":
    """(название, задание) из аргументов `run_part` или None — неверные."""
    args = web.args_of(call.arguments or "{}")
    title, task = (args or {}).get("title"), (args or {}).get("task")
    if (not isinstance(title, str) or not title.strip() or len(title) > chat_tools.PART_TITLE_MAX
            or not isinstance(task, str) or not task.strip() or len(task) > chat_tools.PART_TASK_MAX):
        return None
    return title.strip(), task.strip()


def extend(task) -> None:
    """Ответ стал ответом по частям (раз на ответ): срок — PARTS_TOTAL_SEC вместо STREAM_TOTAL_SEC, пометка обрезки
    по сроку — `chat.CUT_TIME_PARTS`."""
    if task.long:
        return
    task.long = True
    if math.isfinite(task.deadline):
        task.deadline += chat.PARTS_TOTAL_SEC - chat.STREAM_TOTAL_SEC


def _request(task, title: str, job: str) -> list[dict]:
    """Сообщения запроса части: системное сообщение хода и одно сообщение пользователя — записка, выжимки итогов
    прежних частей этого ответа и задание."""
    first = task.messages[0] if task.messages and task.messages[0]["role"] == "system" else None
    done = "".join(f'<done-part n="{s["n"]}" title="{html.escape(s["title"])}">\n{chat.history_excerpt(s["text"])}\n'
                   f"</done-part>\n\n" for s in task.steps
                   if s.get("kind") == "part" and s.get("state") == "done" and "part" not in s and s.get("text"))
    asked = {"role": "user", "content": f'{NOTE}\n\n{done}<part title="{html.escape(title)}">\n{job}\n</part>'}
    return [first, asked] if first is not None else [asked]


def _specs(task) -> list[dict]:
    """Инструменты части: «Интернет» хода, MCP без пометки подтверждения, встроенные без PART_EXCLUDED."""
    mcp = [spec for spec in (task.mcp.specs if task.mcp else [])
           if ((task.mcp.confirm_hint(spec["function"]["name"]) or {}).get("confirm")) is None]
    builtin = [] if task.builtin_off else chat_tools.part_specs(task)
    return web.specs(task.tools) + mcp + builtin


async def _rounds(task) -> "tuple[str | None, str]":
    """Круги части до текста без вызовов; итог — (слова ошибки или None, текст последнего круга)."""
    rounds = 0
    while True:
        if task.tools and not task._web_on():         # выключили посреди ответа — дальше без «Интернета»
            task.tools = []
        specs = _specs(task) if rounds < chat_tools.PART_ROUNDS else []
        task.spec_size = chat.spec_tokens(specs, task.ratio)
        payload = {**task.params, "model": task.model, "messages": task._request(bool(specs)), "stream": True}
        if specs:
            payload |= {"tools": specs, "tool_choice": "auto"}
        if task.thinking is not None:
            payload["chat_template_kwargs"] = {"enable_thinking": task.thinking}
        error, ending, calls, text = await task._round(payload, False, part=True)
        if error is not None:
            return error, text
        if ending is chat.BUSY:                        # 503: тот же круг заново, как у хода
            if not task.freed and task.conn.kind == "srvllm" and sections.is_local(task.conn, task.model):
                task.freed = True
                await task._free_images()
                continue
            if (error := await task._wait_card()) is not None:
                return error, text
            continue
        if ending is chat.TOO_LONG:                    # отказ по длине — словами хода, без сжатия
            return chat.TOO_LONG_WORDS, text
        # структурный вызов — при любом признаке конца, кроме обрыва; вызов текстом в части не разбирается.
        # Паузы внутри части нет: вопрос и подтверждение отказаны словами (chat_tools.call, NEEDS_CONFIRM)
        if specs and calls and ending not in (None, "length"):
            rounds += 1
            if (error := await task._tools(calls, text)) is not None:
                return error, text
            continue
        if ending is None:
            return chat.CUT_OFF, text           # те же слова, что у хода
        return None, text


async def run(task, call) -> dict:
    """`run_part` хода `task` (`chat.StreamTask`, `task.part` — None): шаг части. Неверные аргументы — BAD_ARGS, вторая
    часть круга — ONE_PER_ROUND, сверх PARTS_MAX — LIMIT (шаг без запуска). Иначе шаг `running` (событие и запись
    сразу), запрос части с подменой `messages`, `seen`, `part`, `part_deadline`, `spec_size` хода на её время, итог —
    `text.strip()[:PART_RESULT_MAX]`. Отмена хода — шаг STOPPED и отмена дальше."""
    parsed = _args(call)
    if parsed is None:
        return chat_tools.part_refusal(call, BAD_ARGS)
    if task.part_round:
        return chat_tools.part_refusal(call, ONE_PER_ROUND)
    if task.parts >= chat_tools.PARTS_MAX:
        return chat_tools.part_refusal(call, LIMIT)
    title, job = parsed
    task.parts += 1
    task.part_round = True
    extend(task)
    n = task.parts
    step = {"kind": "part", "n": n, "title": title, "state": "running", "ms": 0, "text": ""}
    task._step(step)                                  # до подмены: шаг ответа, не шаг внутри части
    task.fresh = True
    task.put("tool", {"kind": "part", "label": title})
    task.put("part", {"n": n, "title": title, "state": "running"})
    started = clock.NOW()
    saved = (task.messages, task.seen, task.spec_size, task.part_deadline)
    try:
        try:
            task.messages = _request(task, title, job)
            task.seen, task.part = set(), n
            task.part_deadline = clock.NOW() + chat.STREAM_TOTAL_SEC
            error, text = await _rounds(task)
        finally:
            task.messages, task.seen, task.spec_size, task.part_deadline = saved
            task.part = None
    except asyncio.CancelledError:
        step |= {"state": "error", "error": STOPPED, "ms": round((clock.NOW() - started) * 1000)}
        task._step(step)
        task.put("part", {"n": n, "title": title, "state": "error", "ms": step["ms"]})
        log.info("чат %s: ответ %s — часть %d: остановлена, %d мс", task.chat_id[:8], task.message_id[:8], n, step["ms"])
        raise
    result = text.strip()[:chat_tools.PART_RESULT_MAX]
    if error is None and not result:
        error = EMPTY
    step["ms"] = round((clock.NOW() - started) * 1000)
    if error is None:
        step |= {"state": "done", "text": result}
    else:
        step |= {"state": "error", "error": error}
    task.fresh = True
    task.put("part", {"n": n, "title": title, "state": step["state"], "ms": step["ms"]})
    log.info("чат %s: ответ %s — часть %d: %s, %d мс", task.chat_id[:8], task.message_id[:8], n, step["state"],
             step["ms"])
    return step
