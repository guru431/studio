"""Ответы Open WebUI 0.13 → вид Студии (спека 2026-11-22-studio-open-webui-design, «Вид чата для страницы»).

Чистые функции, без ввода-вывода и без журнала: изменения формата Open WebUI правятся здесь, страница их не видит.
Формы — по исходнику Open WebUI v0.13.4: `routers/chats.py` (`ChatTitleIdResponse`, `ChatResponse`), `routers/folders.py`
(`FolderNameIdResponse`), `main.py` (`GET /api/models`), `utils/middleware.py` (элементы `output`: `reasoning`, `message`,
`function_call`, `function_call_output`), `utils/ask_user.py` (вопросы модели).

- `chat_item` — строка списка чатов; `folder_item`, `model_item` — папка и модель.
- `branch` — текущая ветка чата: от `currentId` по `parentId`, не больше числа сообщений чата; цикл и пропавший
  родитель — конец ветки, не исключение. Ответ — `{id, title, models, folder_id, messages}`.
- `chat_files` — прежние документы чата для `files` продолжения (Open WebUI перезаписывает ими `chat.files`).
- Неожиданная форма (нет ключа, список вместо объекта, ветка без текущего сообщения) — `Shape`: маршрут превращает
  её в 502 `owui_shape`. Текст `Shape` — только слова кода, ничего из ответа Open WebUI.
- Неизвестный элемент `output` — не ошибка: его тип попадает в `other`, страница показывает «элемент <тип>».
- `normalize` — событие `events` сокета Open WebUI → короткий набор событий Студии для SSE; неизвестное — `[]`.
"""
import json
import re

#: предел текста результата инструмента в `tools[].result`, знаков (с многоточием)
RESULT_MAX = 2000
#: предел аргументов инструмента в `pending.arguments`, знаков (с многоточием)
ARGS_MAX = 2000
#: предел длины типа неизвестного элемента, знаков
TYPE_MAX = 64
OPTION_LABEL_MAX = 200
OPTION_DESCRIPTION_MAX = 2000
OPTION_INDEX_MAX = 16
#: статусы вызова, в которых ответ ждёт решения (`pause_for_tool_approval` ставит `pending`, прежние версии — `requires_approval`)
PAUSED = ("pending", "requires_approval")
KNOWN = ("reasoning", "message", "function_call", "function_call_output")
#: файл Open WebUI по адресу `/api/v1/files/<id>/content` (картинки результатов инструментов)
FILE_URL = re.compile(r"(?:^|/)api/v1/files/([A-Za-z0-9_.:-]{1,128})/content(?:\?.*)?$")


class Shape(Exception):
    """Open WebUI ответил не той формой, что ждёт Студия."""


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _str(value) -> str | None:
    return value if isinstance(value, str) else None


def _num(value) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _dict(value, what: str) -> dict:
    if not isinstance(value, dict):
        raise Shape(what)
    return value


def _text(parts, sep: str) -> str:
    """Текст частей `[{type, text}]` (или строки)."""
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, list):
        return ""
    return sep.join(part["text"] for part in parts if isinstance(part, dict) and isinstance(part.get("text"), str))


# ---- списки ---------------------------------------------------------------------------------------------------------

def chat_item(raw: dict) -> dict:
    """Строка списка чатов (`ChatTitleIdResponse`): `{id, title, updated, folder_id, active, pending}`."""
    _dict(raw, "chat")
    chat_id = raw.get("id")
    if not isinstance(chat_id, str) or not chat_id:
        raise Shape("chat id")
    return {"id": chat_id, "title": _str(raw.get("title")) or "", "updated": int(_num(raw.get("updated_at")) or 0),
            "folder_id": _str(raw.get("folder_id")), "active": raw.get("active") is True,
            "pending": raw.get("pending_approval") is True}


def folder_item(raw: dict) -> dict:
    """Папка (`FolderNameIdResponse`): `{id, name, parent_id}`."""
    _dict(raw, "folder")
    folder_id = raw.get("id")
    if not isinstance(folder_id, str) or not folder_id:
        raise Shape("folder id")
    return {"id": folder_id, "name": _str(raw.get("name")) or "", "parent_id": _str(raw.get("parent_id"))}


def model_item(raw: dict) -> dict | None:
    """Модель из `GET /api/models`: `{id, name, vision, file_upload}`. Агенты терминалов (`agent:*`) и скрытые модели —
    `None`. Возможность, которой нет в `info.meta.capabilities`, считается включённой, как на странице Open WebUI."""
    _dict(raw, "model")
    model_id = raw.get("id")
    if not isinstance(model_id, str) or not model_id:
        raise Shape("model id")
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    meta = info.get("meta") if isinstance(info.get("meta"), dict) else {}
    if model_id.startswith("agent:") or meta.get("hidden") is True:
        return None
    caps = meta.get("capabilities") if isinstance(meta.get("capabilities"), dict) else {}
    return {"id": model_id, "name": _str(raw.get("name")) or model_id, "vision": caps.get("vision") is not False,
            "file_upload": caps.get("file_upload") is not False}


# ---- чат ------------------------------------------------------------------------------------------------------------

def _file(raw) -> dict | None:
    """Вложение или файл результата: `{id, name, kind}`; без id (встроенная картинка `data:`) — `None`."""
    if not isinstance(raw, dict):
        return None
    inner = raw.get("file") if isinstance(raw.get("file"), dict) else {}
    meta = inner.get("meta") if isinstance(inner.get("meta"), dict) else {}
    file_id = _str(raw.get("id")) or _str(inner.get("id"))
    if not file_id:
        found = FILE_URL.search(_str(raw.get("url")) or _str(raw.get("image_url")) or "")
        file_id = found[1] if found else None
    if not file_id:
        return None
    name = _str(raw.get("name")) or _str(meta.get("name")) or _str(inner.get("filename")) or ""
    mime = _str(meta.get("content_type")) or _str(raw.get("content_type")) or ""
    image = raw.get("type") in ("image", "input_image") or mime.startswith("image/")
    return {"id": file_id, "name": name, "kind": "image" if image else "file"}


def _seconds(item: dict) -> int | None:
    duration = _num(item.get("duration"))
    if duration is not None:
        return int(duration)
    started, ended = _num(item.get("started_at")), _num(item.get("ended_at"))
    return int(max(0, ended - started)) if started is not None and ended is not None else None


def _questions(arguments, *, display: bool = True) -> dict | None:
    """Вопросы `ask_user` (`utils/ask_user.py::normalize_ask_user_request`); не разобралось — `None`."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except ValueError:
            return None
    if not isinstance(arguments, dict) or not isinstance(arguments.get("questions"), list) or not arguments["questions"]:
        return None
    allow_other = arguments.get("allow_other") is not False
    questions = []
    for raw in arguments["questions"]:
        if not isinstance(raw, dict) or not isinstance(raw.get("options"), list):
            return None
        if not isinstance(raw.get("id"), str) or not isinstance(raw.get("question"), str):
            return None
        options = [{"label": _str(o.get("label")) or "", "description": _str(o.get("description")) or ""}
                   for o in raw["options"] if isinstance(o, dict)]
        if display:
            options = [{"label": _cut(o["label"], OPTION_LABEL_MAX),
                        "description": _cut(o["description"], OPTION_DESCRIPTION_MAX)}
                       for o in options[:OPTION_INDEX_MAX + 1]]
        questions.append({"id": raw["id"], "header": _str(raw.get("header")) or "", "question": raw["question"],
                          "options": options, "allow_other": raw.get("allow_other", allow_other) is not False})
    return {"questions": questions, "allow_other": allow_other}


def _pending(item: dict) -> dict | None:
    """Пауза по вызову: вопрос модели (`question`) или подтверждение инструмента (`approval`); вопрос, который не
    разобрался, — `None`."""
    call_id = _str(item.get("call_id")) or _str(item.get("id")) or ""
    name = _str(item.get("name")) or ""
    arguments = item.get("arguments")
    if name == "ask_user":
        found = _questions(arguments)
        return {"call_id": call_id, "kind": "question", **found} if found else None
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False) if arguments is not None else ""
    return {"call_id": call_id, "kind": "approval", "name": name, "arguments": _cut(arguments, ARGS_MAX)}


def _error(value) -> str | None:
    """Ошибка ответа: `None` — нет; строка — слова Open WebUI (пустая, если их нет)."""
    if not value:
        return None
    if isinstance(value, dict):
        return _cut(_str(value.get("content")) or _str(value.get("message")) or "", RESULT_MAX)
    return _cut(value, RESULT_MAX) if isinstance(value, str) else ""


def _message(key: str, raw: dict) -> dict:
    output = raw.get("output")
    items = [item for item in output if isinstance(item, dict)] if isinstance(output, list) else []
    texts, thoughts, spans, calls, results, other, files = [], [], [], [], {}, [], []

    def add_file(found) -> None:
        if found and all(found["id"] != known["id"] for known in files):
            files.append(found)

    for item in items:
        kind = item.get("type")
        if kind == "message":
            texts.append(_text(item.get("content"), ""))
        elif kind == "reasoning":
            thoughts.append(_text(item.get("content"), "") or _text(item.get("summary"), "\n"))
            spans.append(_seconds(item))
        elif kind == "function_call":
            if item.get("hidden") is not True:
                calls.append(item)
        elif kind == "function_call_output":
            call_id = _str(item.get("call_id"))
            if call_id:
                results[call_id] = item
            for found in (item.get("files") if isinstance(item.get("files"), list) else []):
                add_file(_file(found))
            for part in (item.get("output") if isinstance(item.get("output"), list) else []):
                if isinstance(part, dict) and part.get("type") == "input_image":
                    add_file(_file({"image_url": part.get("image_url"), "type": "image"}))
        else:
            label = _cut(kind, TYPE_MAX) if isinstance(kind, str) and kind else "unknown"
            if label not in other:
                other.append(label)
    for found in (raw.get("files") if isinstance(raw.get("files"), list) else []):
        add_file(_file(found))

    content = raw.get("content")
    text = "\n\n".join(t for t in texts if t) if any(texts) else (content if isinstance(content, str) else "")
    thought = "\n\n".join(t for t in thoughts if t)
    known = [s for s in spans if s is not None]
    reasoning = {"text": thought, "seconds": sum(known) if known else None} if thought else None

    done = raw.get("done") is not False
    waiting = None
    if not done:
        waiting = next((c for c in calls if (_str(c.get("call_id")) or _str(c.get("id"))) not in results
                        and c.get("status") in PAUSED), None)
    pending = _pending(waiting) if waiting else None
    if waiting and pending is None:
        label = _cut(_str(waiting.get("name")) or "unknown", TYPE_MAX)
        if label not in other:
            other.append(label)
    tools = []
    for call in calls:
        if pending and call is waiting:
            continue
        call_id = _str(call.get("call_id")) or _str(call.get("id")) or ""
        result = results.get(call_id)
        status = _str(call.get("status")) or ("completed" if result else "in_progress")
        if result and result.get("status") == "failed":
            status = "failed"
        tools.append({"call_id": call_id, "name": _str(call.get("name")) or "", "status": status,
                      "result": _cut(_text(result.get("output"), "\n"), RESULT_MAX) if result else None})
    stamp = _num(raw.get("timestamp"))
    return {"id": _str(raw.get("id")) or key, "role": _str(raw.get("role")) or "", "model": _str(raw.get("model")),
            "timestamp": int(stamp) if stamp is not None else None, "text": text, "reasoning": reasoning,
            "tools": tools, "files": files, "other": other, "done": done, "error": _error(raw.get("error")),
            "pending": pending}


def branch(raw_chat: dict) -> dict:
    """Текущая ветка чата (`ChatResponse` Open WebUI): `{id, title, models, folder_id, messages}`.

    Идёт от `chat.history.currentId` (нет — от `current_message_id` чата) по `parentId`; шагов не больше, чем сообщений
    в чате. Цикл, пропавший родитель и сообщение не-объект — конец ветки. Порядок — от первого сообщения к последнему."""
    _dict(raw_chat, "chat")
    chat_id = raw_chat.get("id")
    if not isinstance(chat_id, str) or not chat_id:
        raise Shape("chat id")
    chat = _dict(raw_chat.get("chat"), "chat body")
    history = _dict(chat.get("history"), "history")
    messages = _dict(history.get("messages"), "messages")
    current = _str(history.get("currentId")) or _str(raw_chat.get("current_message_id"))
    if messages and not current:
        raise Shape("current message")
    found, seen = [], set()
    for _ in range(len(messages)):
        raw = messages.get(current) if current is not None else None
        if not isinstance(raw, dict) or current in seen:
            break
        seen.add(current)
        found.append(_message(current, raw))
        current = _str(raw.get("parentId"))
    found.reverse()
    models = chat.get("models")
    return {"id": chat_id, "title": _str(raw_chat.get("title")) or _str(chat.get("title")) or "",
            "updated": int(_num(raw_chat.get("updated_at")) or 0), "pinned": raw_chat.get("pinned") is True,
            "models": [m for m in models if isinstance(m, str)] if isinstance(models, list) else [],
            "folder_id": _str(raw_chat.get("folder_id")), "messages": found}


def call_questions(raw_chat: dict, message_id: str, call_id: str) -> list[dict]:
    """Вопросы указанного вызова из сохранённого ответа, независимо от текущей ветки чата."""
    chat = _dict(_dict(raw_chat, "chat").get("chat"), "chat body")
    messages = _dict(_dict(chat.get("history"), "history").get("messages"), "messages")
    message = messages.get(message_id)
    output = message.get("output") if isinstance(message, dict) else None
    for call in output if isinstance(output, list) else ():
        if (isinstance(call, dict) and call.get("type") == "function_call" and call.get("name") == "ask_user"
                and (call.get("call_id") or call.get("id")) == call_id):
            found = _questions(call.get("arguments"), display=False)
            return found["questions"] if found else []
    return []


def chat_files(raw_chat: dict, parent: str) -> list[dict]:
    """Документы чата (`chat.files` в `ChatResponse`), которые ещё есть в сообщениях ветки от `parent` к началу, — как
    их отбирает страница Open WebUI 0.13.4 (`Chat.svelte`: `chatFiles` по `chatMessageFiles`). Элементы — как хранит
    Open WebUI, без повторов по `id`. Нет `files` или они не списком — `[]`."""
    chat = _dict(_dict(raw_chat, "chat").get("chat"), "chat body")
    messages = _dict(_dict(chat.get("history"), "history").get("messages"), "messages")
    used, seen, current = set(), set(), parent
    for _ in range(len(messages)):
        raw = messages.get(current) if current is not None else None
        if not isinstance(raw, dict) or current in seen:
            break
        seen.add(current)
        files = raw.get("files")
        if isinstance(files, list):
            used.update(f["id"] for f in files if isinstance(f, dict) and isinstance(f.get("id"), str))
        current = _str(raw.get("parentId"))
    stored = chat.get("files")
    found: dict[str, dict] = {}
    for item in stored if isinstance(stored, list) else []:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"] in used:
            found.setdefault(item["id"], item)
    return list(found.values())


# ---- события сокета -------------------------------------------------------------------------------------------------

DELTAS = {"response.output_text.delta": "text", "response.reasoning_text.delta": "reasoning"}


def _paused(output) -> bool:
    """В `output` есть вызов, ждущий решения: статус паузы и нет результата."""
    items = [item for item in output if isinstance(item, dict)] if isinstance(output, list) else []
    answered = {item.get("call_id") for item in items if item.get("type") == "function_call_output"}
    return any(item.get("type") == "function_call" and item.get("status") in PAUSED
               and (_str(item.get("call_id")) or _str(item.get("id"))) not in answered for item in items)


def _response_event(chat_id: str, message_id: str | None, data: dict) -> list[dict]:
    """Событие Responses API из потока ответа: кусок текста или рассуждения, вызов инструмента."""
    kind = data.get("type")
    if kind in DELTAS and isinstance(data.get("delta"), str):
        return [{"type": "delta", "chat_id": chat_id, "message_id": message_id, "kind": DELTAS[kind],
                 "text": data["delta"]}]
    item = data.get("item")
    if kind in ("response.output_item.added", "response.output_item.done") and isinstance(item, dict) \
            and item.get("type") == "function_call" and item.get("hidden") is not True:
        return [{"type": "item", "chat_id": chat_id, "message_id": message_id,
                 "tool": {"call_id": _str(item.get("call_id")) or _str(item.get("id")) or "",
                          "name": _str(item.get("name")) or "", "status": _str(item.get("status")) or ""}}]
    return []


def normalize(payload: dict) -> list[dict]:
    """Событие `events` сокета Open WebUI (`{chat_id, message_id, data: {type, data}}`) → события Студии:
    `list`, `title`, `active`, `delta` (`kind`: `text` | `reasoning`), `item`, `done` (`error` — `None` или слова),
    `pending`. Неизвестное и не той формы — `[]`.

    Пауза Open WebUI 0.13.4 (`utils/middleware.py::pause_for_tool_approval`) приходит как `chat:active {active: true}`
    и снимок `chat:completion` без `done` с вызовом в статусе паузы; `pending_approval: true` в `chat:active` (так
    видела проба) — тоже пауза. `queued` сокет не шлёт: его знает ответ на отправку."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return []
    chat_id, message_id = _str(payload.get("chat_id")), _str(payload.get("message_id"))
    kind, data = payload["data"].get("type"), payload["data"].get("data")
    if isinstance(data, dict) and kind in ("response:completion", "chat:completion") \
            and str(data.get("type", "")).startswith("response."):
        return _response_event(chat_id, message_id, data) if chat_id else []
    if kind == "chat:list":
        data = data if isinstance(data, dict) else {}
        chat_id = _str(data.get("chat_id")) or chat_id
        return [{"type": "list", "chat_id": chat_id, "folder_id": _str(data.get("folder_id"))}]
    if not chat_id:
        return []
    if kind == "chat:title":
        title = data if isinstance(data, str) else _str(data.get("title")) if isinstance(data, dict) else None
        return [{"type": "title", "chat_id": chat_id, "title": title}] if title is not None else []
    if not isinstance(data, dict):
        return []
    if kind == "chat:active":
        found = [{"type": "active", "chat_id": chat_id, "active": data.get("active") is True}]
        if data.get("pending_approval") is True:
            found.append({"type": "pending", "chat_id": chat_id, "message_id": message_id})
        return found
    if kind == "chat:message:error":
        return [{"type": "done", "chat_id": chat_id, "message_id": message_id, "error": _error(data.get("error")) or ""}]
    if kind == "chat:completion":
        if data.get("error"):
            return [{"type": "done", "chat_id": chat_id, "message_id": message_id, "error": _error(data["error"])}]
        if data.get("done") is True:
            return [{"type": "done", "chat_id": chat_id, "message_id": message_id, "error": None}]
        if _paused(data.get("output")):
            return [{"type": "pending", "chat_id": chat_id, "message_id": message_id}]
    return []
