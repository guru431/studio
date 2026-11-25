"""Мост Claude Code — нижний слой раздела «Claude Code» (спека 2026-10-28-studio-claude-code-design, 6.2–6.4,
6.10; кадры, коды закрытия, просьбы и пределы — протокол docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md).

- Канал: мост за NAT сам держит исходящий WebSocket `/api/agent/link` с ключом устройства в заголовке
  `Authorization` (не в строке запроса — её пишет журнал Apache); по нему студия шлёт мосту просьбы, мост —
  ответы и поток событий Claude Code. «На связи» — есть ли канал (`LINKS`), в памяти, не в базе. Канал
  у компьютера один: новый заменяет прежний (4409). Отзыв, отключение и удаление учётки рвут канал (4401),
  смена пароля — нет: ключ устройства самостоятелен (спека 6.2).
- Просьба (`call`) уходит не дольше `SEND_SEC` и потом ждёт ответа с тем же `id` не дольше `CALL_SEC`; канал
  пропал — сразу `Offline`, а не по сроку. События хода раздаются очередям наблюдателей чата (`WATCHERS`); канал пропал или его заменил новый —
  им `("offline", None)`; канал появился (`attach`) — `("online", None)`: поток `live`, что ждёт компьютер,
  кончается, и страница сразу открывает новый. Кадры канала, который уже не текущий (заменён, снят), никуда не идут.
  Карточку (`ask`) или конец хода (`done`) чата, который начавший ход не смотрит, ему сообщает push
  (`push.notify_later`).
  Своего состояния хода студия не хранит: источник правды — машина (спека 6.10).
- Признак сессии студии для моста — `tag`: не дайджест сессии и тем более не токен, а производное от
  дайджеста; мост открывает компьютер PIN-ом для этого признака. Заперлась сессия или отперлась —
  `forget(close_computers=False)`: очереди наблюдателей этой сессии (`Watch.session`) получают
  `("closed", None)`, кэши уходят, срок PIN компьютеров сохраняется. Явный сброс шлёт мостам `close` (§2.3).
- История подключений — `agent_events`: только добавляется (`record`), «Удалить всю историю» её не трогает,
  уходит только с учёткой. Строка несёт id и имя компьютера на момент события (после отзыва строки `agents`
  нет), краткий признак устройства (`device`) и адрес — ни ключа, ни PIN, ни текстов.
- Журнал — «агент N: …» и адрес неизвестного ключа: ни ключа, ни имени компьютера, проектов и чатов.
"""
import asyncio
import hashlib
import json
import logging
import re
import secrets
from collections.abc import Callable

from fastapi import APIRouter, Request, WebSocket
from starlette.websockets import WebSocketDisconnect

import auth
import clock
import push
import release
import security
import state
import code_attention

log = logging.getLogger("studio")
router = APIRouter()

CLOSE_PROTOCOL = 4400
CLOSE_AUTH = 4401
CLOSE_REPLACED = 4409
CLOSE_RESTART = 1012
#: учётка отключена (не отзыв): мост с `hello.account_disabled` гасит ходы и переподключается с паузами — после
#: «Включить» компьютер вернётся сам; прежнему мосту отключение — 4401, как прежде
CLOSE_DISABLED = 4403
EVENT_KINDS = frozenset({"text", "step", "result", "ask", "asked", "snapshot", "done", "settings", "context", "activity",
                         "input_accepted", "followup", "followup_sent", "followup_removed", "queue", "ownership",
                         "step_update", "progress"})
EFFORTS = ("low", "medium", "high", "xhigh", "max")
FRAME_MAX = 48 * 1024 * 1024
#: json.loads идёт на цикле событий: кадр из мелких структур на 4 МиБ — 0,13 с, на 48 МиБ — секунды и ГиБ памяти.
#: Любой кадр — не больше VALUES_MAX запятых и скобок (2 млн значений — около 0,1 с). Кадр больше EVENT_FRAME_MAX
#: (снимок хода — событие до 8 МиБ, limits.HISTORY_BUDGET моста) — только `res` на ждущую просьбу (`t` и `id`
#: первыми, как пишет мост); без просьбы (срок вышел) — не разбирается.
EVENT_FRAME_MAX = 10 * 1024 * 1024
VALUES_MAX = 2_000_000
RES_HEAD = re.compile(r'\{\s*"t"\s*:\s*"res"\s*,\s*"id"\s*:\s*"([0-9a-f]{1,64})"')
HELLO_SEC = 10
CALL_SEC = 30
#: отправка кадра просьбы (с ожиданием замка канала): 40 МиБ вложений хода — около 1 Мбит/с; вместе с `CALL_SEC`
#: меньше предела Apache для `/api/code/` (600 с, сверяет tests/test_deploy.py)
SEND_SEC = 300
WATCH_MAX = 1000
WATCHED_MAX = 64  # окон в `answer.watched` (`limits.WATCHED_MAX` моста)
COMPLETIONS_MAX = 50  # досланных завершений в кадре (протокол §18)
COMPLETION_AGE_MAX = 7 * 24 * 3600  # давность конца хода, с: старше — время отметки по этому пределу
STOP_SEC = 2
MESSAGE_MAX = 300
CODE_MAX = 40
MODELS_MAX = 50
MODEL_ID = re.compile(r"[A-Za-z0-9._:\[\]-]{1,100}")
OPENCODE_MODEL_ID = re.compile(r"[A-Za-z0-9._:\[\]/@+-]{1,300}")
OPENCODE_EFFORT = re.compile(r"[A-Za-z0-9._-]{1,64}")
OPENCODE_MODELS_MAX = 500
#: модель DeepSeek Harness — строка JSON `["<провайдер>","<модель>"]`, как `deepseek.MODEL_ID` моста
DEEPSEEK_MODEL_ID = re.compile(r'\["[^"\\\x00-\x1f]{1,150}","[^"\\\x00-\x1f]{1,150}"\]')
#: почему мост не перенёс строку настроек DSH человека в наложение (`deepseek_settings` моста)
DEEPSEEK_SKIPPED = ("unknown", "acp_own", "env_key", "unreadable")
PID = re.compile(r"[0-9a-f]{16}")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")  # id карточки в досылке (`turns.REQUEST_ID` моста)
#: вложения хода у моста без `hello.limits` (до 0.48.0): изображений и всего
LEGACY_LIMITS = {"images": 4, "attachments": 8}
#: больше этих чисел студия не пропускает и от нового моста (`limits.IMAGE_COUNT`, `ATTACHMENT_COUNT` моста 0.48.0)
LIMITS_MAX = {"images": 12, "attachments": 12}


class ProtocolError(ValueError):
    """Мост нарушил протокол — канал закрывается 4400."""


class Offline(Exception):
    """Канала нет или он пропал, пока ждали ответа."""


class BridgeError(Exception):
    """Отказ моста (`res` с `ok: false`): код, слова для человека, `extra` — `left` и `version`."""

    def __init__(self, code: str, message: str, extra: dict):
        super().__init__(code)
        self.code, self.message, self.extra = code, message, extra


class Link:
    """Канал одного компьютера. `pending` — просьбы, ждущие ответа: id → будущий кадр `res`; `key_hash` — digest
    ключа, которым канал вошёл (`attach` сверяет его со строкой: «Заменить» переносит ключ на другую строку)."""

    def __init__(self, agent_id: int, user_id: int, name: str, ip: str | None = None, key_hash: str | None = None):
        self.agent_id, self.user_id, self.name, self.ip, self.key_hash = agent_id, user_id, name, ip, key_hash
        self.hello: dict = {}
        self.pending: dict[str, asyncio.Future] = {}

    async def send(self, frame: dict) -> None:
        raise NotImplementedError

    async def close(self, code: int) -> None:
        raise NotImplementedError


class SocketLink(Link):
    """Канал поверх WebSocket; отправки — под замком: кадры разных просьб не перемежаются."""

    def __init__(self, ws: WebSocket, agent_id: int, user_id: int, name: str, ip: str | None = None,
                 key_hash: str | None = None):
        super().__init__(agent_id, user_id, name, ip, key_hash)
        self.ws = ws
        self.lock = asyncio.Lock()

    async def send(self, frame: dict) -> None:
        async with self.lock:
            await self.ws.send_text(json.dumps(frame, ensure_ascii=False))

    async def close(self, code: int) -> None:
        async with self.lock:
            try:
                await self.ws.close(code)
            except Exception:            # уже закрыт или сокет оборвался — закрывать нечего
                pass


class Watch(asyncio.Queue):
    """Очередь наблюдателя чата: ("ev", событие) | ("offline", None) | ("online", None) | ("closed", None); `session` — `tag`
    сессии студии, что смотрит (её `forget` кладёт `closed`), `user_id` — её учётка (push хода не уходит, только пока
    смотрит его адресат, `watching`), `window` — id окна страницы (`X-Studio-Window`; ответ на карточку — `windows`).
    Событий в ней — не больше `WATCH_MAX`: застрявший поток SSE (TCP жив, страница не читает) получает `offline` и
    снимается (`_put_event`)."""

    def __init__(self, session: str | None, user_id: int | None = None, window: str | None = None):
        super().__init__()
        self.session = session
        self.user_id = user_id
        self.window = window


#: agent_id → канал на связи
LINKS: dict[int, Link] = {}
#: (agent_id, chat) → очереди наблюдателей чата
WATCHERS: dict[tuple[int, str], set[Watch]] = {}
#: (agent_id, chat) → кто начал идущий ход (`initiated`, `_Started`): участнику совместного проекта — push хода ему
INITIATORS: dict[tuple[int, str], "_Started"] = {}
INITIATORS_MAX = 10_000
#: (agent_id, chat) → запрос последнего хода владельца в чате (`owner_turn`; ход участника — отметка `_MemberTurn`):
#: после успешного `done` той же сессией идёт синхронизация зеркала (`project_mirrors.after_turn`). Очередь моста —
#: часть того же хода: решает тот, кто ход начал. Только память
TURN_REQUESTS: dict[tuple[int, str], "Request | _MemberTurn"] = {}
TURN_REQUESTS_MAX = 256
#: (agent_id, chat) → ждущая карточка для напоминаний (`_card`, `remind`): проект, `request_id`, текст push, время
#: карточки и последнего взгляда на чат, по сессиям — (повторов, время последнего). Только память: после перезапуска
#: службы карточки, возникшие без канала, снова заводит досылка моста (`welcome.pending_asks`), прочие — забыты
CARDS: dict[tuple[int, str], dict] = {}
CARDS_MAX = 1000
REMIND_TIMES = 3                  # повторов на карточку каждому устройству
REMIND_TICK_SEC = 60
#: фоновые отправки (`forget`) и закрытия каналов (`_close_later`): ссылка держит задачу до конца, `stop` ждёт
#: их не дольше `STOP_SEC` и отменяет остаток
TASKS: set[asyncio.Task] = set()
#: что ещё забыть, когда сессия заперлась или отперлась: (user_id, tag сессии) → None. Ставят разделы выше
#: (`claude_code` — память PDF), импорт отсюда к ним был бы кольцом
FORGET_HOOKS: list = []


def _background(coro) -> None:
    task = asyncio.get_running_loop().create_task(coro)
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)


def _close_later(link: "Link", code: int) -> None:
    """Закрыть сокет канала фоном: `close` ждёт замок отправки, а висящая отправка (полный буфер, мёртвая сеть
    посреди большого кадра) держит его до таймаута пинга — столько же ждали бы новый канал и отзыв."""
    _background(link.close(code))


def tag(digest: str) -> str:
    """Признак сессии студии для моста: из него не получить ни дайджест, ни токен сессии."""
    return hashlib.sha256(("studio-code\0" + digest).encode()).hexdigest()[:32]


def device(request: Request) -> str:
    """С какого устройства действие: «телефон» или «браузер» по сессии запроса и начало её признака."""
    row = state.DB.execute("SELECT mobile FROM sessions WHERE digest = ?", (request.state.digest,)).fetchone()
    kind = "телефон" if row is not None and row["mobile"] else "браузер"
    return f"{kind} {tag(request.state.digest)[:6]}"


def record(user_id: int, agent_id: int, agent_name: str, kind: str, *,
           device: str | None = None, ip: str | None = None) -> None:
    """Строка истории подключений."""
    state.DB.execute("INSERT INTO agent_events(user_id, agent_id, agent_name, created, kind, device, ip)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)", (user_id, agent_id, agent_name, clock.NOW(), kind, device, ip))


# ---- знакомство и учёт канала ----------------------------------------------------

def _string(value, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ProtocolError("hello")
    return value


def parse_hello(frame: dict) -> dict:
    """Кадр `hello` (протокол §2.1) → поля, которые студия держит; иначе `ProtocolError`."""
    if not isinstance(frame, dict) or frame.get("t") != "hello" or type(frame.get("v")) is not int \
            or frame["v"] != 1 or not isinstance(frame.get("has_pin"), bool):
        raise ProtocolError("hello")
    models, efforts = frame.get("models"), frame.get("efforts")
    if not isinstance(models, list) or len(models) > MODELS_MAX or not isinstance(efforts, list):
        raise ProtocolError("hello")
    out_models = []
    for m in models:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not MODEL_ID.fullmatch(m["id"]):
            raise ProtocolError("hello")
        out_models.append({"id": m["id"], "label": _string(m.get("label"), 100)})
    if not all(isinstance(e, str) and e in EFFORTS for e in efforts):
        raise ProtocolError("hello")
    result = {"cc_version": _string(frame.get("cc_version"), 64),
              "projects_root": _string(frame.get("projects_root"), 1024),
              "has_pin": frame["has_pin"], "models": out_models, "efforts": list(dict.fromkeys(efforts))}
    if "platform" in frame:
        if frame["platform"] not in ("win32", "darwin", "linux"):
            raise ProtocolError("hello")
        result["platform"] = frame["platform"]
    if "agent_version" in frame:
        from agent_release import VERSION
        value = frame["agent_version"]
        if not isinstance(value, str) or not VERSION.fullmatch(value):
            raise ProtocolError("hello")
        result["agent_version"] = value
    if "images" in frame:
        if type(frame["images"]) is not bool:
            raise ProtocolError("hello")
        result["images"] = frame["images"]
    for capability in ("chat_state", "chat_control", "attachments", "large_attachments", "project_activity", "subscription_usage", "usage_reset", "opencode_usage", "project_summary", "metadata_cache", "file_stats", "file_operations", "mcp",
                       "file_suggestions", "chat_search", "queue_edit", "chat_fork", "subagents", "worktrees", "project_create", "project_mirror", "project_mirror_discovery", "mirror_exclude", "mirror_groups", "pin_timeout", "agent_update", "agent_update_cancel", "computer_info", "software_versions", "software_update_all", "desktop", "terminal", "visualizations",
                       "passive_requests", "history_images", "project_sharing", "completions", "visualization_scope", "visualization_images", "opencode_features", "codex_models", "opencode_models", "account_disabled", "git_compare", "file_upload", "file_search",
                       "deepseek_models", "deepseek_usage", "self_check", "agent_log", "open_asks", "answer_takeover", "chat_manage",
                       "claude_models", "chat_search_all", "skills"):
        if capability in frame:
            if type(frame[capability]) is not bool:
                raise ProtocolError("hello")
            result[capability] = frame[capability]
    if "limits" in frame:
        limits = frame["limits"]
        images, attachments = (limits.get("images"), limits.get("attachments")) if isinstance(limits, dict) else (None, None)
        if type(images) is not int or type(attachments) is not int or not 1 <= images <= attachments <= 100:
            raise ProtocolError("hello")
        result["limits"] = {"images": images, "attachments": attachments}
    if isinstance(frame.get("defaults"), dict):
        result["defaults"] = {k: v for k, v in frame["defaults"].items() if k in ("model", "effort", "access")
                              and (v is None or isinstance(v, str) and len(v) <= 100)}
    if "codex" in frame:
        codex = frame["codex"]
        if not isinstance(codex, dict):
            raise ProtocolError("hello")
        levels = codex.get("efforts")
        if not isinstance(levels, list) or not all(isinstance(e, str) and e in
                ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra") for e in levels):
            raise ProtocolError("hello")
        parsed = parse_hello({"t": "hello", "v": 1, "has_pin": False, "projects_root": "",
                              "cc_version": codex.get("version"), "models": codex.get("models"), "efforts": []})
        result["codex"] = {"version": parsed["cc_version"], "models": parsed["models"],
                           "efforts": list(dict.fromkeys(levels))}
        if isinstance(codex.get("defaults"), dict):
            result["codex"]["defaults"] = {k: v for k, v in codex["defaults"].items()
                if k in ("model", "effort", "access") and (v is None or isinstance(v, str) and len(v) <= 100)}
    if "opencode" in frame:
        catalog = frame["opencode"]
        if not isinstance(catalog, dict):
            raise ProtocolError("hello")
        models, efforts = catalog.get("models"), catalog.get("efforts")
        if (not isinstance(models, list) or len(models) > OPENCODE_MODELS_MAX or not isinstance(efforts, list)
                or len(efforts) > 100 or not all(isinstance(e, str) and OPENCODE_EFFORT.fullmatch(e) for e in efforts)):
            raise ProtocolError("hello")
        out_models = []
        for model in models:
            if (not isinstance(model, dict) or not isinstance(model.get("id"), str)
                    or not OPENCODE_MODEL_ID.fullmatch(model["id"])):
                raise ProtocolError("hello")
            out_models.append({"id": model["id"], "label": _string(model.get("label"), 100)})
        result["opencode"] = {"version": _string(catalog.get("version"), 64), "models": out_models,
                              "efforts": list(dict.fromkeys(efforts))}
        if isinstance(catalog.get("defaults"), dict):
            result["opencode"]["defaults"] = {k: v for k, v in catalog["defaults"].items()
                if k in ("model", "effort", "access") and (v is None or isinstance(v, str) and len(v) <= 300)}
    if "deepseek" in frame:
        result["deepseek"] = _deepseek(frame["deepseek"])
    return result


def turn_limits(hello: dict) -> dict:
    """Пределы вложений хода моста: `hello.limits`, но не больше `LIMITS_MAX`; без поля — `LEGACY_LIMITS`."""
    limits = hello.get("limits") if isinstance(hello, dict) else None
    if not isinstance(limits, dict):
        return dict(LEGACY_LIMITS)
    return {key: min(limits[key], LIMITS_MAX[key]) for key in LIMITS_MAX}


def _deepseek(catalog) -> dict:
    """`hello.deepseek` — пределы `opencode`, свои id моделей и сводка настроек DSH человека (`settings`)."""
    if not isinstance(catalog, dict):
        raise ProtocolError("hello")
    models, efforts, settings = catalog.get("models"), catalog.get("efforts"), catalog.get("settings")
    if (not isinstance(models, list) or len(models) > OPENCODE_MODELS_MAX or not isinstance(efforts, list)
            or len(efforts) > 100 or not all(isinstance(e, str) and OPENCODE_EFFORT.fullmatch(e) for e in efforts)
            or not isinstance(settings, dict) or "profile" not in settings
            or settings["profile"] not in ("web", "desktop", None)):
        raise ProtocolError("hello")
    applied, skipped = settings.get("applied"), settings.get("skipped")
    if (not isinstance(applied, list) or len(applied) > 100 or not isinstance(skipped, list) or len(skipped) > 100
            or not all(isinstance(row, dict) and row.get("reason") in DEEPSEEK_SKIPPED for row in skipped)):
        raise ProtocolError("hello")
    out_models = []
    for model in models:
        if not isinstance(model, dict) or not isinstance(model.get("id"), str) or not DEEPSEEK_MODEL_ID.fullmatch(model["id"]):
            raise ProtocolError("hello")
        out_models.append({"id": model["id"], "label": _string(model.get("label"), 100)})
    result = {"version": _string(catalog.get("version"), 64), "models": out_models,
              "efforts": list(dict.fromkeys(efforts)),
              "settings": {"profile": settings["profile"], "applied": [_string(name, 100) for name in applied],
                           "skipped": [{"name": _string(row.get("name"), 100), "reason": row["reason"]}
                                       for row in skipped]}}
    if isinstance(catalog.get("defaults"), dict):
        result["defaults"] = {k: v for k, v in catalog["defaults"].items()
                              if k in ("model", "effort", "access") and (v is None or isinstance(v, str) and len(v) <= 310)}
    return result


async def attach(link: Link) -> None:
    """Канал на связи: прежний канал компьютера закрывается 4409 (фоном — `welcome` новому сразу). Компьютер
    отозвали, пока шло знакомство, — 4401 и в `LINKS` его нет; учётка отключена (и до рукопожатия) —
    `disabled_code`. Строка ищется по id и ключу канала: «Заменить» во время знакомства отдал ключ другой строке —
    1012, мост переподключится и придёт с её id (прежний ключ к строке уже не подходит — 4401). `welcome` несёт
    логин учётки компьютера — мост показывает, под кем он на связи."""
    hello = link.hello
    row = state.DB.execute(
        "UPDATE agents SET has_pin = ?, cc_version = ?, projects_root = ?, last_seen = ?, platform = COALESCE(?, platform)"
        " WHERE id = ? AND key_hash = ? AND user_id IN (SELECT id FROM users WHERE disabled = 0)"
        " RETURNING name, (SELECT login FROM users WHERE users.id = agents.user_id) AS login",
        (int(hello["has_pin"]), hello["cc_version"], hello["projects_root"], clock.NOW(),
         hello.get("platform"), link.agent_id, link.key_hash)).fetchone()
    if row is None:
        owner = state.DB.execute("SELECT a.id, u.disabled FROM agents a JOIN users u ON u.id = a.user_id"
                                 " WHERE a.key_hash = ?", (link.key_hash,)).fetchone()
        if owner is not None and not owner["disabled"]:
            log.info("агент %d: ключ перешёл к компьютеру %d до знакомства", link.agent_id, owner["id"])
            await link.close(CLOSE_RESTART)
            return
        log.warning("агент %d: %s до знакомства", link.agent_id, "учётка отключена" if owner else "отозван")
        await link.close(disabled_code(link) if owner else CLOSE_AUTH)
        return
    link.name = row["name"]
    old = LINKS.get(link.agent_id)
    LINKS[link.agent_id] = link
    record(link.user_id, link.agent_id, link.name, "online", ip=link.ip)
    log.info("агент %d: на связи", link.agent_id)
    if old is not None and old is not link:
        log.info("агент %d: прежний канал заменён", link.agent_id)
        _fail_pending(old)               # ответов по закрытому каналу уже не будет
        _offline(link.agent_id)          # потоки переподключатся к новому каналу
        _close_later(old, CLOSE_REPLACED)
    _check_cards(link)
    try:
        # `completions` — студия принимает кадр досланных завершений (протокол §18), `pending_asks` — и открытых
        # карточек в нём, `open_asks` — и всех открытых карточек ходов (`known`); старый мост поля не читает.
        await link.send({"t": "welcome", "agent_id": link.agent_id, "name": link.name, "login": row["login"],
                         "studio_version": release.VERSION, "completions": True, "pending_asks": True,
                         "open_asks": True})
    except Exception:
        pass                             # канал уже рвётся: его конец сделает detach
    if old is None and LINKS.get(link.agent_id) is link:
        _online(link.agent_id)           # после welcome: просьбы ждавших страниц идут мосту следом за ним; при
                                         # замене канала ждущих нет (live шёл к прежнему), потокам хватает offline


def _online(agent_id: int) -> None:
    """Наблюдателям чатов компьютера — канал снова есть: ждущий поток `live` кончится, страница откроет новый."""
    for (aid, _chat), queues in WATCHERS.items():
        if aid == agent_id:
            for q in queues:
                q.put_nowait(("online", None))


def _fail_pending(link: Link) -> None:
    for fut in link.pending.values():
        if not fut.done():
            fut.set_exception(Offline())
    link.pending.clear()


def _offline(agent_id: int) -> None:
    """Наблюдателям чатов компьютера — вестей по этому каналу больше не будет."""
    for (aid, _chat), queues in WATCHERS.items():
        if aid == agent_id:
            for q in queues:
                q.put_nowait(("offline", None))


async def detach(link: Link) -> None:
    """Канал кончился. Без await внутри: зовётся из `finally` обработчика, и отмена задачи (остановка
    службы) не должна оборвать его посередине."""
    _fail_pending(link)
    if LINKS.get(link.agent_id) is not link:
        return
    del LINKS[link.agent_id]
    _offline(link.agent_id)
    state.DB.execute("UPDATE agents SET last_seen = ? WHERE id = ?", (clock.NOW(), link.agent_id))
    record(link.user_id, link.agent_id, link.name, "offline", ip=link.ip)
    log.info("агент %d: связь потеряна", link.agent_id)


def _event_ok(frame: dict) -> bool:
    project, chat, e = frame.get("project"), frame.get("chat"), frame.get("e")
    return (isinstance(project, str) and PID.fullmatch(project) is not None
            and isinstance(chat, str) and UUID.fullmatch(chat) is not None
            and isinstance(e, dict) and e.get("k") in EVENT_KINDS)


def deliver(link: Link, frame: dict) -> None:
    """Кадр моста после знакомства: ответ — его просьбе, событие — наблюдателям чата."""
    t = frame.get("t")
    if t == "res":
        rid = frame.get("id")
        fut = link.pending.get(rid) if isinstance(rid, str) else None
        if fut is not None and not fut.done():
            fut.set_result(frame)
    elif t == "ev":
        if LINKS.get(link.agent_id) is not link:
            return                       # канал заменён или снят (отзыв): между detach и close сокет ещё читается
        if not _event_ok(frame):
            log.warning("агент %d: неверное событие", link.agent_id)
            return
        if frame["e"]["k"] == "done" and frame["e"].get("status") in ("done", "stopped", "error"):
            frame = {**frame, "e": {**frame["e"], "attention": code_attention.completed(
                link.agent_id, frame["project"], frame["chat"])}}
        for q in list(WATCHERS.get((link.agent_id, frame["chat"]), ())):
            _put_event(link.agent_id, frame["chat"], q, frame["e"])
        _push(link, frame)
        if frame["e"]["k"] == "done" and frame["e"].get("status") == "done":
            _after_turn(link, frame)
    elif t == "completions" and link.hello.get("completions"):
        _completions(link, frame)
    else:
        raise ProtocolError("t")


def _completions(link: Link, frame: dict) -> None:
    """Ходы, кончившиеся без канала (протокол §18): отметка «непрочитано» и push — как у живого `done`. Строки
    `code_attention` — только этого компьютера (`link.agent_id`): чужой чат кадр не тронет. Открытые карточки,
    возникшие без канала (`status` `permission|question`, `welcome.pending_asks`), — только push, как у живого
    `ask`. Карточка с `known: true` (мост с `hello.open_asks` досылает все открытые при каждом новом канале) —
    о ней push уже был: второго нет."""
    if LINKS.get(link.agent_id) is not link:
        return
    items = frame.get("items")
    if not (isinstance(items, list) and len(items) <= COMPLETIONS_MAX and all(
            isinstance(i, dict) and isinstance(i.get("status"), str) and (i["status"] in DONE_WORDS or i["status"] in ASK_WORDS)
            and type(i.get("ago")) is int and i["ago"] >= 0
            and _event_ok({"project": i.get("project"), "chat": i.get("chat"), "e": {"k": "done"}})
            and ("request_id" not in i or isinstance(i["request_id"], str) and REQUEST_ID.fullmatch(i["request_id"]))
            and type(i.get("known", False)) is bool and (not i.get("known") or i["status"] in ASK_WORDS)
            for i in items)):
        log.warning("агент %d: неверные досланные завершения", link.agent_id)
        return
    for item in items:
        if item["status"] in ASK_WORDS:
            card = {"project": item["project"], "chat": item["chat"], "project_name": item.get("project_name"),
                    "e": {"k": "ask", "kind": item["status"], "request_id": item.get("request_id")}}
            held = CARDS.get((link.agent_id, item["chat"]))
            if held is not None:
                held.pop("check", None)          # мост её прислал: открыта (другую `request_id` заменит `_push`)
            if item.get("known") is not True:
                _push(link, card)
            elif held is None or held["request_id"] not in (None, item.get("request_id")):
                _push(link, card, notify=False)   # напоминания — и после перезапуска службы; знакомая — без сброса счёта
            continue
        finished = clock.NOW() - min(item["ago"], COMPLETION_AGE_MAX)
        code_attention.completed(link.agent_id, item["project"], item["chat"], finished)
        _push(link, {"project": item["project"], "chat": item["chat"], "project_name": item.get("project_name"),
                     "e": {"k": "done", "status": item["status"]}})
    if link.hello.get("open_asks") is True and len(items) < COMPLETIONS_MAX:   # полный кадр мог урезать карточки
        for key in [key for key, card in CARDS.items() if key[0] == link.agent_id and "check" in card]:
            del CARDS[key]                       # мост её не прислал: закрыта, пока канала не было
    log.info("агент %d: досланных завершений %d", link.agent_id, len(items))


#: повод push → слова тела (тексты сервера плана)
#: повод push «Агентов» на языках подписки (те же слова, что в en.js)
ASK_WORDS = {"permission": {"ru": "ждёт подтверждения", "en": "awaiting confirmation"},
             "question": {"ru": "вопрос", "en": "question"}}
DONE_WORDS = {"done": {"ru": "ход окончен", "en": "turn completed"},
              "stopped": {"ru": "ход прерван", "en": "turn interrupted"}, "error": {"ru": "ошибка", "en": "error"}}


class _Started:
    """Запись `INITIATORS` одного хода: `user_id` участника или None (ход владельца). Своя на каждый ход — возврат
    при отказе (`initiated`) узнаёт, что место всё ещё за этим ходом."""
    __slots__ = ("user_id",)

    def __init__(self, user_id: int | None):
        self.user_id = user_id


def initiated(agent_id: int, chat: str, user_id: int | None) -> Callable[[], None]:
    """Ход чата начинает участник `user_id`: ему — push карточек и конца этого хода; `None` — ход владельца
    (push по умолчанию — ему). Ставится до просьбы `turn`: быстрый `done` приходит раньше, чем зовущий получит
    ответ. Возвращает возврат для хода, которого мост не принял: прежняя запись — только пока место за этим ходом,
    как у `owner_turn` (адресата хода, принятого тем временем из другого запроса, отказ не подменяет). Только
    память: перезапуск службы возвращает push владельцу."""
    key = (agent_id, chat)
    previous = INITIATORS.pop(key, None)
    mine = INITIATORS[key] = _Started(user_id)
    while len(INITIATORS) > INITIATORS_MAX:
        del INITIATORS[next(iter(INITIATORS))]

    def back() -> None:
        if INITIATORS.get(key) is mine:
            del INITIATORS[key]
            if previous is not None:
                INITIATORS[key] = previous

    return back


class _MemberTurn:
    """Отметка хода участника в `TURN_REQUESTS` вместо запроса владельца: синхронизации после него нет. Своя на
    каждый ход — возврат при отказе (`owner_turn`) узнаёт, что запись всё ещё его."""
    __slots__ = ()


def owner_turn(agent_id: int, chat: str, request: Request | None) -> Callable[[], None]:
    """Ход в чате: запрос хода владельца компьютера — для синхронизации зеркала после хода (`_after_turn`); ход
    участника (`None`) ставит свою отметку на место запроса владельца. Ставится до просьбы `turn`, как `initiated`;
    возвращает возврат для хода, которого мост не принял: прежняя запись — только пока место за этим ходом (запись
    хода, принятого тем временем из другого запроса, отказ не снимает и не подменяет)."""
    key = (agent_id, chat)
    previous = TURN_REQUESTS.pop(key, None)
    mine = request if request is not None else _MemberTurn()
    TURN_REQUESTS[key] = mine
    while len(TURN_REQUESTS) > TURN_REQUESTS_MAX:
        del TURN_REQUESTS[next(iter(TURN_REQUESTS))]

    def back() -> None:
        if TURN_REQUESTS.get(key) is mine:
            del TURN_REQUESTS[key]
            if previous is not None:
                TURN_REQUESTS[key] = previous

    return back


def _after_turn(link: Link, frame: dict) -> None:
    """Успешный ход, начатый владельцем из студии: зеркала проекта с «синхронизировать после хода». Ходы, кончившиеся
    без канала (`_completions`), и ходы участников зеркала не трогают."""
    request = TURN_REQUESTS.get((link.agent_id, frame["chat"]))
    if request is None or isinstance(request, _MemberTurn):
        return
    import project_mirrors               # он импортирует этот модуль: на загрузке — кольцо
    project_mirrors.after_turn(request, link.agent_id, frame["project"], _place(link, frame))


def _addressee(link: Link, frame: dict) -> int | None:
    """Кому push хода: начавшему его участнику, пока проект ему выдан (отозван — никому: владельцу о чужих ходах
    не шлём); нет записи — владельцу компьютера."""
    started = INITIATORS.get((link.agent_id, frame["chat"]))
    member = started.user_id if started is not None else None
    if member is None:
        return link.user_id
    import project_access                # он импортирует этот модуль: на загрузке — кольцо
    return member if frame["project"] in project_access.projects(member, link.agent_id) else None


def _place(link: Link, frame: dict) -> str:
    """«компьютер · проект» в тексте push. Имя — только один компонент пути; неизвестный/старый мост — без него."""
    project_name = frame.get("project_name")
    valid_name = (isinstance(project_name, str) and 0 < len(project_name) <= 255
                  and project_name not in (".", "..") and not any(c in '/\\' or ord(c) < 32
                  or 0xD800 <= ord(c) <= 0xDFFF or ord(c) == 127 for c in project_name))
    return f"{link.name} · {project_name}" if valid_name else link.name


def _push(link: Link, frame: dict, *, notify: bool = True) -> None:
    """Карточка или конец хода, которых начавший ход не смотрит, — push ему (спека 6.8); карточка ещё и ждёт
    напоминаний (`CARDS`, `remind`). `notify=False` — только напоминания: о карточке push уже был."""
    e = frame["e"]
    words, value = {"ask": (ASK_WORDS, e.get("kind")), "done": (DONE_WORDS, e.get("status"))}.get(e["k"], ({}, None))
    text = words.get(value) if isinstance(value, str) else None
    kind = "done" if e["k"] == "done" else "ask"
    if e["k"] == "queue" and e.get("paused") is True and e.get("waiting") is True:
        text = {"ru": "очередь ждёт вас", "en": "the queue is waiting for you"}
    user_id = _addressee(link, frame) if text is not None else None
    if e["k"] == "done":
        INITIATORS.pop((link.agent_id, frame["chat"]), None)   # ход кончился: следующий — со своей записью
    body = {lang: f"{_place(link, frame)}: {words}" for lang, words in text.items()} if text else None
    _card(link, frame, body)
    if not notify or text is None or user_id is None or watching(link.agent_id, frame["chat"], user_id):
        return
    push.notify_later(user_id, body, f"code/{link.agent_id}/{frame['project']}/{frame['chat']}",
                      f"code-{frame['chat']}", kind=kind)


def _card(link: Link, frame: dict, body: dict | None) -> None:
    """Ждущая карточка чата для напоминаний: `ask` её заводит (одна на чат — последняя), закрывает `asked` с её
    `request_id` (досланная мостом карточка его не знает — любой `asked`), конец хода и снимок без открытых карточек."""
    key, e = (link.agent_id, frame["chat"]), frame["e"]
    if e["k"] == "ask" and e.get("kind") in ASK_WORDS:
        CARDS.pop(key, None)
        CARDS[key] = {"project": frame["project"], "request_id": e.get("request_id"), "body": body,
                      "at": clock.NOW(), "seen": 0.0, "sent": {}}
        while len(CARDS) > CARDS_MAX:
            del CARDS[next(iter(CARDS))]
    elif key in CARDS and (e["k"] == "done" or e["k"] == "snapshot" and e.get("asks") == [] or e["k"] == "asked" and (
            CARDS[key]["request_id"] in (None, e.get("request_id")))):
        del CARDS[key]


def _check_cards(link: Link) -> None:
    """Новый канал моста с `hello.open_asks`: карточки компьютера ждут подтверждения досылкой (`_completions`) — о
    закрытой, пока канала не было (перезапуск моста), `asked` уже не придёт, и мост её не пришлёт. Не подтверждённую
    за `REMIND_TICK_SEC` (60) `remind` забывает (кадр досылки мост шлёт первым и только если есть что слать). Мост
    без поля — как раньше: карточка ждёт закрытия."""
    now, checks = clock.NOW(), link.hello.get("open_asks") is True
    for (agent_id, _chat), card in CARDS.items():
        if agent_id == link.agent_id:
            if checks:
                card["check"] = now
            else:
                card.pop("check", None)


def remind() -> None:
    """Проход напоминаний (07:I5): карточка не закрыта, компьютер на связи, адресат чат не смотрит — каждому его
    устройству, где напоминание включено и вид `ask` не заглушён (`push.muted`), повтор push с той же меткой
    (заменяет прежнее) через интервал устройства (`push.reminders`) от карточки, прошлого повтора или последнего
    взгляда на чат; не больше `REMIND_TIMES` на устройство. Тихие часы откладывают повтор, а не съедают его."""
    now = clock.NOW()
    for key, card in list(CARDS.items()):
        agent_id, chat = key
        link = LINKS.get(agent_id)
        if link is None:
            continue                     # без связи ответить нельзя; вернётся — карточка та же
        if "check" in card:              # новый канал её не подтвердил (`_check_cards`)
            if now - card["check"] >= REMIND_TICK_SEC:
                del CARDS[key]
            continue
        user_id = _addressee(link, {"chat": chat, "project": card["project"]})
        if user_id is None:
            del CARDS[key]
            continue
        if watching(agent_id, chat, user_id):
            card["seen"] = now
            continue
        intervals, muted = push.reminders(user_id), push.muted(user_id, "ask")
        due = set()
        for digest, interval in intervals.items():
            times, last = card["sent"].get(digest, (0, card["at"]))
            if interval and times < REMIND_TIMES and digest not in muted and now - max(last, card["seen"]) >= interval:
                due.add(digest)
                card["sent"][digest] = (times + 1, now)
        if due:
            push.notify_later(user_id, card["body"], f"code/{agent_id}/{card['project']}/{chat}", f"code-{chat}",
                              kind="ask", skip=frozenset(intervals) - due)


async def _reminders() -> None:
    while True:
        await clock.SLEEP(REMIND_TICK_SEC)
        try:
            remind()
        except Exception as e:           # noqa: BLE001 — напоминания не должны вставать
            log.warning("напоминания: сбой (%s)", type(e).__name__)


def start_reminders() -> asyncio.Task:
    return asyncio.create_task(_reminders())


# ---- просьбы -------------------------------------------------------------------

SURROGATE = re.compile(r"[\ud800-\udfff]")


def _refusal(frame: dict) -> BridgeError:
    """Слова и `version` отказа уходят браузеру обычным ответом FastAPI (UTF-8): одинокий суррогат (Node режет
    UTF-16 посреди эмодзи) уронил бы его 500 — в словах он заменяется «�», `version` с ним не передаётся."""
    code = frame.get("code")
    message = frame.get("message")
    extra = {}
    if type(frame.get("left")) is int:
        extra["left"] = frame["left"]
    if isinstance(frame.get("version"), str) and not SURROGATE.search(frame["version"]):
        extra["version"] = frame["version"]
    return BridgeError(code[:CODE_MAX] if isinstance(code, str) and code else "bridge",
                       SURROGATE.sub("�", message[:MESSAGE_MAX]) if isinstance(message, str) else "", extra)


async def call(agent_id: int, op: str, session: str, timeout: float | None = None, **args) -> dict:
    """Просьба мосту и его `data`. Нет канала или пропал — `Offline`; отказ — `BridgeError`; отправка дольше
    `SEND_SEC` или молчание после неё дольше `timeout` (по умолчанию `CALL_SEC`) — `TimeoutError`."""
    link = LINKS.get(agent_id)
    if link is None:
        raise Offline()
    rid = secrets.token_hex(8)
    fut = asyncio.get_running_loop().create_future()
    link.pending[rid] = fut
    try:
        # Срок ответа — от конца отправки: кадр с вложениями хода (до 40 МиБ) по медленному каналу уходит дольше
        # него, а отказ после частичной отправки не помешал бы кадру дойти и ходу начаться.
        try:
            await asyncio.wait_for(link.send({"t": "req", "id": rid, "op": op, "session": session, **args}), SEND_SEC)
        except TimeoutError:
            raise
        except Exception:
            raise Offline() from None
        frame = await asyncio.wait_for(fut, CALL_SEC if timeout is None else timeout)
    finally:
        link.pending.pop(rid, None)
    if frame.get("ok") is True:
        data = frame.get("data")
        return data if isinstance(data, dict) else {}
    raise _refusal(frame)


# ---- наблюдатели ---------------------------------------------------------------

def watch(agent_id: int, chat: str, session: str | None = None, user_id: int | None = None,
          window: str | None = None) -> Watch:
    q = Watch(session, user_id, window)
    WATCHERS.setdefault((agent_id, chat), set()).add(q)
    return q


def unwatch(agent_id: int, chat: str, q: Watch) -> None:
    queues = WATCHERS.get((agent_id, chat))
    if queues is not None:
        queues.discard(q)
        if not queues:
            del WATCHERS[(agent_id, chat)]


def watching(agent_id: int, chat: str, user_id: int | None = None) -> bool:
    """Чат кто-то смотрит; с `user_id` — именно эта учётка (чужой взгляд участника не глушит push владельцу)."""
    queues = WATCHERS.get((agent_id, chat), ())
    return any(user_id is None or q.user_id == user_id for q in queues)


def windows(agent_id: int, chat: str) -> list[str]:
    """Окна страниц, что сейчас смотрят чат (не больше `WATCHED_MAX`): ответ на карточку из другого окна мост
    примет без «Продолжить здесь», если окна-владельца хода среди них нет (`hello.answer_takeover`)."""
    return sorted({q.window for q in WATCHERS.get((agent_id, chat), ()) if q.window})[:WATCHED_MAX]


def _put_event(agent_id: int, chat: str, q: Watch, e: dict) -> None:
    """Событие наблюдателю; отстал на `WATCH_MAX` — вместо события `offline` (поток кончится, страница дочитает
    снимком `live`), и очередь снята: больше в неё ничего не идёт."""
    if q.qsize() < WATCH_MAX:
        q.put_nowait(("ev", e))
        return
    unwatch(agent_id, chat, q)
    q.put_nowait(("offline", None))
    log.warning("агент %d: наблюдатель отстал — снят", agent_id)


# ---- закрытие ------------------------------------------------------------------

def forget(user_id: int, digest: str, *, close_computers: bool = True) -> None:
    """Снять потоки и кэши сессии. Блокировка экрана сохраняет срок PIN компьютеров;
    явный сброс дополнительно шлёт мостам `close` фоном, не ожидая чужой сокет."""
    session = tag(digest)
    for hook in FORGET_HOOKS:
        hook(user_id, session)
    for (aid, chat), queues in list(WATCHERS.items()):
        for q in list(queues):
            if q.session == session:
                unwatch(aid, chat, q)
                q.put_nowait(("closed", None))
    for link in [x for x in LINKS.values() if x.user_id == user_id]:
        frame = ({"t": "close", "session": session} if close_computers else
                 {"t": "desktop_close", "session": session} if link.hello.get("desktop") else None)
        if frame is None:
            continue
        async def send(link=link, frame=frame):
            try:
                await link.send(frame)
            except Exception as e:
                log.warning("агент %d: закрытие сессии не отправлено (%s)", link.agent_id, type(e).__name__)
        _background(send())


async def drop(agent_id: int, code: int = CLOSE_AUTH) -> None:
    """Канал компьютера — закрыть: сперва снять с учёта (новые просьбы — `Offline`), потом закрыть сокет —
    фоном (`_close_later`): отзыв и удаление учётки не ждут висящую отправку."""
    link = LINKS.get(agent_id)
    if link is not None:
        await detach(link)
        _close_later(link, code)


def disabled_code(link: Link) -> int:
    """Закрытие канала отключённой учётки: 4403, если мост его понимает (`hello.account_disabled`), иначе 4401 —
    прежний мост на любом другом коде переподключался бы, не прервав ходов."""
    return CLOSE_DISABLED if link.hello.get("account_disabled") else CLOSE_AUTH


async def drop_user(user_id: int, *, disabled: bool = False) -> None:
    """Каналы учётки — закрыть: удаление — 4401; отключение (`disabled`) — `disabled_code`."""
    for link in [x for x in LINKS.values() if x.user_id == user_id]:
        await drop(link.agent_id, disabled_code(link) if disabled else CLOSE_AUTH)


async def stop() -> None:
    """Остановка службы: каналы — 1012 (мост переподключится); фоновые закрытия и отправки ждут не дольше
    `STOP_SEC`, остаток отменяется (висящий сокет закроет сам uvicorn)."""
    for agent_id in list(LINKS):
        await drop(agent_id, CLOSE_RESTART)
    if TASKS:
        await asyncio.wait(list(TASKS), timeout=STOP_SEC)
    for task in list(TASKS):
        task.cancel()
    await asyncio.gather(*list(TASKS), return_exceptions=True)


# ---- канал ---------------------------------------------------------------------

def _frame(message: dict, link: Link) -> "dict | None":
    """Кадр JSON-объектом; большой ответ на просьбу, которой уже нет (срок вышел), — None без разбора."""
    if message.get("type") == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    text = message.get("text")
    if not isinstance(text, str) or len(text) > FRAME_MAX or len(text.encode()) > FRAME_MAX:
        raise ProtocolError("frame")
    if len(text) > EVENT_FRAME_MAX:
        head = RES_HEAD.match(text)
        if head is None:
            raise ProtocolError("frame")
        fut = link.pending.get(head[1])
        if fut is None or fut.done():
            log.warning("агент %d: большой ответ без просьбы — пропущен", link.agent_id)
            return None
    if text.count(",") + text.count("[") + text.count("{") > VALUES_MAX:
        raise ProtocolError("frame")
    try:
        frame = json.loads(text)
    except (ValueError, RecursionError):          # RecursionError — вложенность глубже стека разбора
        raise ProtocolError("json") from None
    if not isinstance(frame, dict):
        raise ProtocolError("json")
    return frame


@router.websocket("/api/agent/link")
async def link_socket(ws: WebSocket):
    ip = auth.client_ip(ws)
    header = ws.headers.get("authorization", "")
    row = None
    if header.startswith("Bearer ") and len(header) > len("Bearer "):
        # и отключённой учётки: код закрытия ей выбирает `attach` по `hello` (`disabled_code`)
        row = state.DB.execute("SELECT a.id, a.user_id, a.name, a.key_hash FROM agents a WHERE a.key_hash = ?",
                               (security.digest(header[len("Bearer "):]),)).fetchone()
    await ws.accept()
    if row is None:
        log.warning("канал: неизвестный ключ с %s", ip)
        await ws.close(CLOSE_AUTH)
        return
    link = SocketLink(ws, row["id"], row["user_id"], row["name"], ip, row["key_hash"])
    try:
        try:
            link.hello = parse_hello(_frame(await asyncio.wait_for(ws.receive(), HELLO_SEC), link))
        except (TimeoutError, ProtocolError):
            log.warning("агент %d: нарушение протокола", link.agent_id)
            await link.close(CLOSE_PROTOCOL)
            return
        await attach(link)
        if LINKS.get(link.agent_id) is not link:
            return
        while True:
            frame = _frame(await ws.receive(), link)
            if frame is not None:
                deliver(link, frame)
    except ProtocolError:
        log.warning("агент %d: нарушение протокола", link.agent_id)
        await link.close(CLOSE_PROTOCOL)
    except WebSocketDisconnect:
        pass
    finally:
        await detach(link)
