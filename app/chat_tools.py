"""Встроенные инструменты чата, не из «Интернета» (`web.py`): их предлагает сервер в каждом ходе через сервер, кроме
Cleaner (`chat.StreamTask._read_model`). Сейчас — `ask_user` (спека 2026-11-21-studio-source-capabilities-design,
п. 17): вопрос пользователю кнопками; ход встаёт на паузу (`chat_pending.pause`), ответ продолжает тот же ответ.
В журнал — ничего: вопрос и ответ пишет только шаг в `messages.tools`."""
import json

import chat_pending

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

NAMES: set[str] = {"ask_user"}
KINDS: set[str] = {"ask"}                         # виды их шагов в `messages.tools`


def for_model(step: dict) -> str:
    """Результат шага встроенного инструмента для модели; ошибка — без приписки «Интернета» (web.NOTHING)."""
    return f"Ошибка инструмента: {step['error']}" if "error" in step else step["text"]


def specs(task) -> list[dict]:
    """Встроенные инструменты хода `task` (`chat.StreamTask`)."""
    return [ASK_USER]


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
    (`task.paused`): шаг даст ответ пользователя (`answer_step`)."""
    view = _ask_view(call.arguments)
    if view is None:
        return {"kind": "ask", "error": BAD_ARGS}
    await chat_pending.pause(task, "ask", call, view)
    return {"kind": "ask", "question": view["question"]}


def answer_step(view: dict, action: str, answer: "str | None") -> dict:
    """Шаг ответа на вопрос: модели — JSON `{"answer": текст}`, отказ — `{"answer": null, "note": NO_ANSWER}`."""
    result = {"answer": answer} if action == "answer" else {"answer": None, "note": NO_ANSWER}
    return {"kind": "ask", "question": view["question"], "text": json.dumps(result, ensure_ascii=False)}
