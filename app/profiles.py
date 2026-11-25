"""Профиль API-подключения (спека 2026-11-21-studio-source-foundation-design, раздел 2): системный текст,
параметры запроса и подсказки.

- Хранится в `connection_profiles` по id подключения; удаление подключения уносит профиль триггером.
- Свой профиль правит владелец (`own=True`), общий — только администратор (admin.py). Текст и параметры общего
  профиля пользователю не отдаются; подсказки видны всем, кому видно подключение (`connections.out`).
- У Cleaner профиля нет: 422 `cleaner`.
- Параметры (`request_params`): `temperature`, `top_p`, `presence_penalty` — как есть; `max_tokens` — как
  `max_completion_tokens` у профиля `openai`; `top_k` нестандартен — у `openai` отбрасывается, у прочих — только
  явно заданный; `max_images` — предел картинок запроса (`max_images`), `max_tools` — предел инструментов MCP
  запроса (`max_tools`: API в духе OpenAI не принимают больше 128 инструментов), `context_window` — окно контекста
  модели (`chat.model_window`), все три в запрос не уходят. Отказ сервиса с именем отправленного параметра и признаком
  отказа (`REJECTED`) — слова `REFUSED` (`refusal`).
- Параметры на модель (`model_params`, спека п. 35): те же поля для одной модели подключения, поверх параметров
  подключения (`model_params()`); пустой набор модели не хранится.
- Журнал — только id: ни текста, ни параметров, ни подсказок.
"""
import json
import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

import capabilities
import clock
import connections
import sections
import state

TEXT_MAX = 8000
SUGGESTIONS_MAX = 12
TITLE_MAX = 80
SUGGESTION_TEXT_MAX = 2000
DEFAULT_MAX_IMAGES = 4          # картинок на запрос, пока профиль не задал своё (gemma-abl больше не примет)
#: слова отказа сервиса по параметру профиля: {0} — имя отправленного параметра
REFUSED = "подключение не принимает параметр «{0}» — уберите его в профиле подключения"
#: признаки отказа по параметру в ответе 400 (в нижнем регистре): OpenAI, vLLM/pydantic, LiteLLM
REJECTED = ("unknown", "unsupported", "unrecognized", "not supported", "extra fields not permitted",
            "extra inputs are not permitted", "unexpected keyword")

log = logging.getLogger("studio")
router = APIRouter(prefix="/api/connections")


def _plain(text: str) -> str:
    if "\x00" in text or any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        raise ValueError("недопустимые знаки")
    return text


class Params(BaseModel):
    """null (или нет поля) — параметр не слать."""
    model_config = ConfigDict(extra="forbid")
    temperature: float | None = Field(None, ge=0, le=2, allow_inf_nan=False)
    top_p: float | None = Field(None, gt=0, le=1, allow_inf_nan=False)
    top_k: int | None = Field(None, ge=1, le=1000, strict=True)
    presence_penalty: float | None = Field(None, ge=-2, le=2, allow_inf_nan=False)
    max_tokens: int | None = Field(None, ge=1, le=1_000_000, strict=True)
    max_images: int | None = Field(None, ge=1, le=32, strict=True)
    max_tools: int | None = Field(None, ge=1, le=256, strict=True)
    context_window: int | None = Field(None, ge=8192, le=4_000_000, strict=True)


class Suggestion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    text: str = Field(min_length=1, max_length=SUGGESTION_TEXT_MAX)
    _check = field_validator("title", "text")(classmethod(lambda cls, v: _plain(v)))


class ProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    system_text: str = Field("", max_length=TEXT_MAX)
    params: Params = Field(default_factory=Params)
    suggestions: list[Suggestion] = Field(default_factory=list, max_length=SUGGESTIONS_MAX)
    model_params: dict[str, Params] = Field(default_factory=dict)
    _check = field_validator("system_text")(classmethod(lambda cls, v: _plain(v)))

    @field_validator("model_params")
    @classmethod
    def _models(cls, value: dict) -> dict:
        if len(value) > sections.MODELS_MAX:
            raise ValueError(f"не больше {sections.MODELS_MAX} моделей")
        for name in value:
            if not 1 <= len(name) <= sections.MODEL_MAX:
                raise ValueError(f"имя модели — от 1 до {sections.MODEL_MAX} знаков")
            _plain(name)
        return value


def _no_cleaner(conn: connections.Connection) -> None:
    if conn.cleaner:
        raise HTTPException(422, {"code": "cleaner", "message": "у подключения Cleaner профиля нет"})


def _row(conn_id: int):
    return state.DB.execute("SELECT system_text, params, suggestions, model_params FROM connection_profiles"
                            " WHERE connection_id = ?", (conn_id,)).fetchone()


def _view(conn: connections.Connection) -> dict:
    row = _row(conn.id)
    return {"system_text": row["system_text"] if row else "",
            "params": json.loads(row["params"]) if row else {},
            "suggestions": json.loads(row["suggestions"]) if row else [],
            "model_params": json.loads(row["model_params"]) if row else {},
            "provider": capabilities.profile(conn)}


def get(owner_id: int | None, conn_id: int) -> dict:
    """Профиль своего подключения (`owner_id` None — общее, для администратора); чужое — 404."""
    conn = connections.get(owner_id, conn_id, own=True)
    _no_cleaner(conn)
    return _view(conn)


def put(owner_id: int | None, conn_id: int, body: ProfileIn) -> dict:
    """Профиль целиком; пустой — строка удаляется (`profile_set` снова ложь)."""
    conn = connections.get(owner_id, conn_id, own=True)
    _no_cleaner(conn)
    text = body.system_text.strip()
    params = body.params.model_dump(exclude_none=True)
    suggestions = [s.model_dump() for s in body.suggestions]
    per_model = {name: kept for name, value in body.model_params.items() if (kept := value.model_dump(exclude_none=True))}
    if not (text or params or suggestions or per_model):
        state.DB.execute("DELETE FROM connection_profiles WHERE connection_id = ?", (conn.id,))
    else:
        state.DB.execute(
            "INSERT INTO connection_profiles(connection_id, system_text, params, suggestions, model_params, updated)"
            " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(connection_id) DO UPDATE SET system_text = excluded.system_text,"
            " params = excluded.params, suggestions = excluded.suggestions, model_params = excluded.model_params,"
            " updated = excluded.updated",
            (conn.id, text, json.dumps(params), json.dumps(suggestions, ensure_ascii=False),
             json.dumps(per_model, ensure_ascii=False), clock.NOW()))
    log.info("подключение %d: профиль сохранён", conn.id)
    return _view(conn)


def is_set(conn: connections.Connection) -> bool:
    return not conn.cleaner and _row(conn.id) is not None


def model_params(conn: connections.Connection, model: "str | None") -> dict:
    """Сохранённые параметры подключения, поверх — параметры модели `model` (None — только подключения);
    у Cleaner — {}."""
    row = None if conn.cleaner else _row(conn.id)
    if row is None:
        return {}
    own = json.loads(row["model_params"]).get(model, {}) if model is not None else {}
    return {**json.loads(row["params"]), **own}


def request_params(conn: connections.Connection, model: "str | None" = None) -> dict:
    """Параметры профиля (с параметрами модели, `model_params`) для тела `chat/completions` (корень JSON);
    `context_window`, `max_images`, `max_tools` в запрос не уходят."""
    saved = model_params(conn, model)
    if not saved:
        return {}
    provider = capabilities.profile(conn)
    out = {k: saved[k] for k in ("temperature", "top_p", "presence_penalty") if k in saved}
    if "max_tokens" in saved:
        out["max_completion_tokens" if provider == "openai" else "max_tokens"] = saved["max_tokens"]
    if "top_k" in saved and provider != "openai":
        out["top_k"] = saved["top_k"]
    return out


def max_images(conn: connections.Connection, model: "str | None" = None) -> int:
    """Картинок на весь запрос к подключению: `max_images` профиля (модели, иначе подключения), иначе
    DEFAULT_MAX_IMAGES."""
    return model_params(conn, model).get("max_images", DEFAULT_MAX_IMAGES)


def max_tools(conn: connections.Connection, model: str | None = None) -> int:
    """Инструментов MCP на запрос хода к подключению (отбор — `mcp_client.Tools.discover`; встроенные и «Интернета»
    идут сверх него): `max_tools` профиля (модели, иначе подключения), иначе локальной модели хода
    (`sections.is_local`) — `mcp_client.LOCAL_MAX_TOOLS`, иначе `mcp_client.MAX_TOOLS`."""
    import mcp_client                      # здесь, не при загрузке: mcp_client сам зовёт profiles в ходе
    saved = model_params(conn, model).get("max_tools")
    if saved is not None:
        return saved
    if model is not None and sections.is_local(conn, model):
        return mcp_client.LOCAL_MAX_TOOLS
    return mcp_client.MAX_TOOLS


def text_of(conn: connections.Connection) -> str:
    """Системный текст профиля для куска 2 системного сообщения (`chat_system.base`); у Cleaner — пусто."""
    row = None if conn.cleaner else _row(conn.id)
    return row["system_text"] if row else ""


def suggestions_of(conn: connections.Connection) -> list:
    row = None if conn.cleaner else _row(conn.id)
    return json.loads(row["suggestions"]) if row else []


def refusal(text: str, params: dict) -> "str | None":
    """Слова отказа, если ответ 400 называет отправленный параметр профиля вместе с признаком отказа
    (`REJECTED`); иначе None. Одного имени мало: ошибка длины контекста vLLM называет `max_tokens`."""
    lowered = text.lower()
    if not any(marker in lowered for marker in REJECTED):
        return None
    return next((REFUSED.format(name) for name in params if name in lowered), None)


# ---- API своего профиля ---------------------------------------------------------

@router.get("/{conn_id}/profile")
async def get_profile(conn_id: int, request: Request):
    return get(request.state.user.id, conn_id)


@router.put("/{conn_id}/profile")
async def put_profile(conn_id: int, body: ProfileIn, request: Request):
    return put(request.state.user.id, conn_id, body)
