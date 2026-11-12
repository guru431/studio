"""Ход Claude Code через Agent SDK: клиент на ход, карточки разрешений и вопросов, остановка (протокол §4, §7).

Ход живёт в задаче моста и от канала не зависит: канал оборвался — ход идёт, карточка ждёт ответа,
новый канал получает снимок (`watch`)."""

import asyncio
import contextlib
import copy
import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, CLINotFoundError, HookMatcher,
                              PermissionResultAllow, PermissionResultDeny, ResultError, ResultMessage, StreamEvent,
                              SystemMessage)

from . import attachments as attachment_input
from . import cli, events, history_images, image_input, limits, permissions, sessions, store, task_metrics, timeline
from .claude_sdk import ClaudeSDKClient
from .errors import Refused

log = logging.getLogger(__name__)

MAX_TURNS = limits.MAX_TURNS
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
DECISIONS = ("allow", "always", "deny")
ANSWER_MESSAGE_MAX = 2000
DISCONNECT_SEC = 10
INTERRUPT_SEC = 10
CLOSE_SEC = 15  # close(): ждать ходы, дальше — отмена задач
STOPPED = "Остановлено"
REFUSED = "Пользователь отказал"
NO_CLI = "Claude Code не найден на компьютере"
NO_START = "Claude Code не запустился"
BRIDGE_STOPPED = "ход прерван: мост остановлен"
CLOSING = "мост выходит"  # start() во время close(): ходы закрываются


async def _keep_alive(_input, _tool_use_id, _context) -> dict:
    """Холостой PreToolUse: с ним SDK держит поток открытым, пока `can_use_tool` ждёт ответа."""
    return {"continue_": True}


def options_for(project: Path, cli_path: Path | None, chat: str, new: bool, model: str | None,
                effort: str | None, can_use_tool, access: str | None = None, *, subagent_hook=None) -> ClaudeAgentOptions:
    """Опции клиента хода: новая сессия — `session_id=chat`, существующая — `resume=chat`."""
    limits = cli.subagent_limits(project)
    hooks = {"PreToolUse": [HookMatcher(matcher=None, hooks=[_keep_alive])]}
    if subagent_hook is not None:
        for name in ("SubagentStart", "SubagentStop"):
            hooks[name] = [HookMatcher(matcher=None, hooks=[subagent_hook])]
    return ClaudeAgentOptions(
        cwd=project, cli_path=cli_path, session_id=chat if new else None, resume=None if new else chat,
        model=model, effort=effort, permission_mode=access, include_partial_messages=True, can_use_tool=can_use_tool,
        hooks=hooks,
        setting_sources=list(cli.SETTINGS), system_prompt=cli.coding_prompt(limits),
        env={**limits, **cli.ENV}, stderr=cli._drop, extra_args={"chrome": None})


def _words(errors: list | None, result: str | None, subtype: str | None) -> str:
    """Слова ошибки результата: `errors[0]`, иначе `result`, иначе «Claude Code: <subtype>» — до 300 знаков."""
    first = errors[0] if errors and isinstance(errors[0], str) and errors[0] else None
    return events.clip(first or result or f"Claude Code: {subtype}", limits.MESSAGE_MAX)


@dataclass
class _Ask:
    event: dict  # событие ask
    future: asyncio.Future
    data: dict  # исходный ввод инструмента
    suggestions: list


@dataclass
class _Turn:
    project: Path
    pid: str
    chat: str
    turn: str
    access: str | None = None
    context: dict | None = None
    settings: dict | None = None
    activity: dict | None = None
    progress: dict = field(default_factory=dict)
    feed: events.Feed = field(default_factory=events.Feed)
    asks: dict[str, _Ask] = field(default_factory=dict)
    task: asyncio.Task | None = None
    client: object | None = None
    stopped: bool = False  # stop()
    closing: bool = False  # close(): мост выходит
    finishing: bool = False  # _finish() идёт: stop() задачу не отменяет
    held: list | None = None  # watch() готовит снимок: события хода — сюда, за снимок
    watch_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    owner: str | None = None
    accepted: bool = False
    start_attempted: bool = False
    input_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: int = 1
    followups: list = field(default_factory=list)
    queued: list = field(default_factory=list)
    queue_meta: dict = field(default_factory=dict)
    queue_paused: bool = False
    queue_wake: asyncio.Event = field(default_factory=asyncio.Event)
    attachments: list = field(default_factory=list)
    files: attachment_input.Files = field(default_factory=attachment_input.Files)
    subtasks: dict = field(default_factory=dict)  # id потока/сессии субагента этого хода → имя (или None)
    child_states: dict = field(default_factory=dict)  # lifecycle Claude; subtasks других движков хранит имена
    background_tasks: dict = field(default_factory=dict)  # фоновые агенты, Workflow и команды текущего клиента


class Runner:
    """Ходы машины: не больше `max_turns` сразу, в чате — один.

    `cli_path` сверяется перед каждым ходом (`cli.current`): VS Code обновляет расширение и удаляет прежнюю папку."""

    def __init__(self, emit: Callable[[dict], None], *, choices: store.Choices, cli_path: Path | None,
                 factory=ClaudeSDKClient, max_turns: int = MAX_TURNS, find_cli=cli.find_cli, now=time.time):
        self.emit = emit
        self.choices = choices
        self.cli_path = cli_path
        self.factory = factory
        self.max_turns = max_turns
        self.find_cli = find_cli
        self.now = now
        self._turns: dict[str, _Turn] = {}
        self._closing = False  # close() идёт (или прошёл с `final` или `hold`): новых ходов нет
        self._final = False  # close(final=True): выход моста — `reopen` ходы не возвращает
        self._live_images = history_images.LiveImages()

    # --- просьбы студии ---

    def start(self, project: Path, pid: str, chat: str, text: str, model: str | None, effort: str | None,
              *, images: list[dict] | None = None, attachments: list[dict] | None = None,
              access: str | None = None, owner: str | None = None) -> str:
        """Ход в чате: запись и задача; всё блокирующее — уже в задаче."""
        if chat in self._turns:
            raise Refused("running")
        if self._closing:
            raise Refused("busy", CLOSING)
        if len(self._turns) >= self.max_turns:
            raise Refused("busy")
        t = _Turn(project, pid, chat, secrets.token_hex(8), access=access, owner=owner)
        t.attachments = attachments or []
        self._turns[chat] = t
        t.task = asyncio.create_task(self._run(t, text, model, effort, images or []))
        log.info("ход %s: начат", t.turn)
        return t.turn

    def check_owner(self, pid, chat, owner):
        t = self._turn(pid, chat)
        if t is not None and t.owner is not None and t.owner != owner:
            raise Refused("owned", "Задание выполняется в другом окне — сначала перенесите управление сюда")

    def claim(self, pid, chat, owner):
        t = self._turn(pid, chat)
        if t is not None:
            t.owner = owner
            self._send(t, {"k": "ownership", "owner": owner})
        return {}

    async def steer(self, pid, chat, text, images, *, owner=None, attachments=None, mode="steer"):
        t = self._turn(pid, chat)
        if t is None:
            raise Refused("idle")
        async with t.input_lock:
            self.check_owner(pid, chat, owner)
            if t.finishing or t.stopped or t.closing:
                raise Refused("idle")
            if t.queue_paused:
                raise Refused("busy", "Сначала завершите редактирование очереди")
            if not t.accepted:
                raise Refused("busy", "Задание ещё запускается — отправьте уточнение через несколько секунд")
            if len(t.followups) >= 20:
                raise Refused("busy", "В одном ходе можно отправить до 20 уточнений")
            if len(json.dumps(t.followups, ensure_ascii=True)) + len(json.dumps(text, ensure_ascii=True)) > limits.HISTORY_BUDGET // 4:
                raise Refused("busy", "Уточнения заполнили текущий ход — дождитесь ответа")
            message_id = secrets.token_hex(8)
            prompt = await self._attachment_text(t, text, attachments or [])
            if mode not in ("steer", "queue"):
                raise Refused("invalid")
            mode = self._queue_input(t, prompt, images, message_id) if mode == "queue" else await self._steer_input(t, prompt, images, message_id)
            if mode == "queue":
                t.queue_meta[message_id] = {"suffix": prompt[len(text):], "version": 1,
                                           "text": text, "images": len(images), "attachments": len(attachments or [])}
            # В снимке нет повторных мегабайт картинок; оригиналы уже принял CLI.
            item = {"id": message_id, "text": text + (f"\n[Изображений: {len(images)}]" if images else "") +
                    (f"\n[Вложений: {len(attachments)}]" if attachments else ""),
                    "mode": mode}
            if images:
                image_message = f"followup:{message_id}"
                self._live_images.remember(project=t.project, chat=chat, message=image_message, images=images)
                item.update(image_message_id=image_message,
                            images=[history_images.reference(index) for index in range(len(images))])
            position = t.feed.position()
            if position is not None:
                item["position"] = position
            t.followups.append(item)
            self._send(t, {"k": "followup", **item,
                           **({"version": 1, "input_text": text} if mode == "queue" else {})})
            return {"mode": mode, "id": message_id}

    def _queue_input(self, t, text, images, message_id):
        size = sum(len(image["data"]) * 3 // 4 - (2 if image["data"].endswith("==") else 1 if image["data"].endswith("=") else 0)
                   for _, _, shots in [*t.queued, (message_id, text, images)] for image in shots)
        if size > limits.IMAGES_MAX:
            raise Refused("too_large", "Вложения заполнили очередь — дождитесь выполнения заданий")
        t.queued.append((message_id, text, images))
        return "queue"

    @staticmethod
    def _queue_state(t):
        pending = {row[0] for row in t.queued}
        return [{**item, "version": t.queue_meta.get(item["id"], {}).get("version", 1),
                 "input_text": t.queue_meta.get(item["id"], {}).get("text", item["text"])}
                for item in t.followups if item["id"] in pending]

    def _queued_sent(self, t, message_id):
        t.queued = [row for row in t.queued if row[0] != message_id]
        t.queue_meta.pop(message_id, None)
        for item in t.followups:
            if item["id"] == message_id:
                item["sent"] = True
        self._send(t, {"k": "followup_sent", "id": message_id})

    async def pause_queue(self, pid, chat, paused, *, owner=None):
        t = self._turn(pid, chat)
        if t is None:
            raise Refused("gone", "Очередь уже выполнена")
        async with t.input_lock:
            self.check_owner(pid, chat, owner)
            if not t.queued or t.stopped or t.closing or t.finishing:
                raise Refused("gone", "Очередь уже выполнена")
            t.queue_paused = paused
            if not paused:
                t.queue_wake.set()
            result = {"items": self._queue_state(t), "paused": paused}
            self._send(t, {"k": "queue", **result})
            return result

    async def _wait_queue(self, t):
        while t.queue_paused and t.queued and not (t.stopped or t.closing):
            t.queue_wake.clear()
            await t.queue_wake.wait()

    async def edit_queue(self, pid, chat, message_id, version, text, *, owner=None):
        t = self._turn(pid, chat)
        if t is None:
            raise Refused("gone", "Сообщение уже передано помощнику или задание завершилось")
        async with t.input_lock:
            self.check_owner(pid, chat, owner)
            queued = next((row for row in t.queued if row[0] == message_id), None)
            if queued is None or t.finishing or t.stopped or t.closing:
                raise Refused("gone", "Сообщение уже передано помощнику или задание завершилось")
            meta = t.queue_meta.get(message_id, {"version": 1, "suffix": ""})
            if version != meta["version"]:
                raise Refused("conflict", "Очередь изменена в другом окне — обновите её")
            item = next(row for row in t.followups if row["id"] == message_id)
            if text is None:
                t.queued.remove(queued)
                t.followups.remove(item)
                t.queue_meta.pop(message_id, None)
                if not t.queued:
                    t.queue_wake.set()
                self._send(t, {"k": "followup_removed", "id": message_id})
            else:
                if not text.strip() and not meta.get("images") and not meta.get("attachments"):
                    raise Refused("invalid", "Задание пустое; используйте удаление сообщения")
                if len(json.dumps(t.followups, ensure_ascii=True)) - len(json.dumps(item["text"], ensure_ascii=True)) + len(json.dumps(text, ensure_ascii=True)) > limits.HISTORY_BUDGET // 4:
                    raise Refused("too_large", "Текст заполнил очередь — сократите сообщение")
                t.queued[t.queued.index(queued)] = (message_id, text + meta["suffix"], queued[2])
                meta["version"] += 1
                meta["text"] = text
                t.queue_meta[message_id] = meta
                item["text"] = text + (f"\n[Изображений: {meta['images']}]" if meta.get("images") else "") + (
                    f"\n[Вложений: {meta['attachments']}]" if meta.get("attachments") else "")
            current = self._queue_state(t)
            self._send(t, {"k": "queue", "items": current, "paused": t.queue_paused})
            return {"items": current, "paused": t.queue_paused}

    async def _steer_input(self, t, text, images, message_id):
        await t.client.query(image_input.prompt(text, images) if images else text)
        t.pending += 1
        return "steer"

    def _accepted(self, t):
        t.accepted = True
        if t.owner is not None:
            self._send(t, {"k": "input_accepted"})

    async def state(self, project, chat, model=None, effort=None, access=None):
        """Настройки и контекст без генерации: SDK продолжает сессию только для чтения состояния."""
        saved = await asyncio.to_thread(self.choices.details, chat)
        new = not await asyncio.to_thread(sessions.exists, project, chat)
        model, effort = model or saved.get("model"), effort or saved.get("effort")
        access = access or saved.get("access")
        if not new and not model:
            model = await asyncio.to_thread(sessions.last_model, project, chat)
        path = await asyncio.to_thread(cli.current, self.cli_path, self.find_cli)

        async def deny(*_):
            return PermissionResultDeny(message="Чтение состояния")

        client = self.factory(options=options_for(project, path, chat, new,
            model, effort, deny, access))
        try:
            await asyncio.wait_for(client.connect(), cli.PROBE_SEC)
            info = await client.get_server_info() or {}
            raw = await client.get_context_usage()
            current = await asyncio.to_thread(cli.settings_from, info, project, raw, model=model, effort=effort, access=access)
            usage, cache = await asyncio.to_thread(sessions.context_stats, project, chat, (saved.get("context") or {}).get("cache"))
            context = permissions.context(raw.get("totalTokens"), raw.get("maxTokens"), usage)
            if context and cache:
                context["cache"] = cache
            return {"settings": current, "context": context}
        finally:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(client.disconnect(), DISCONNECT_SEC)

    def _turn(self, pid: str, chat: str) -> _Turn | None:
        """Ход чата — только того же проекта: `pid` просьбы не совпал — хода как будто нет."""
        t = self._turns.get(chat)
        return t if t is not None and t.pid == pid else None

    async def watch(self, pid: str, chat: str) -> tuple[bool, list[dict]]:
        """Идёт ли ход; идёт — кадр снимка (текст, шаги с итогами, открытые карточки), уже в ASCII-JSON
        (`events.Ready`), и события, что пришли, пока он готовился.

        Урезка и кодирование — в потоке: снимок бывает в мегабайты. Пока он готовится, события хода копятся в
        `held` и идут за снимком: студия до снимка события пропускает — в снимке их нет, они бы пропали."""
        t = self._turn(pid, chat)
        if t is None:
            return False, []
        async with t.watch_lock:
            held = t.held = []
            try:
                asks = [{k: v for k, v in a.event.items() if k != "k"} for a in t.asks.values()]
                details = {"timeline": copy.deepcopy(t.feed.timeline), "progress": copy.deepcopy(t.progress),
                           "followups": copy.deepcopy(t.followups)}
                snap = await asyncio.to_thread(self._snapshot, t, t.feed.text, [dict(s) for s in t.feed.steps], asks, details)
            except BaseException:
                t.held = None
                for frame in held:
                    self._emit(t, frame)
                raise
            t.held = None
            return True, [snap, *held]

    @classmethod
    def _snapshot(cls, t: _Turn, text: str, steps: list[dict], asks: list[dict], details=None) -> events.Ready:
        """В потоке: снимок в пределах `HISTORY_BUDGET` и его ASCII-JSON."""
        followups = [dict(item) for item in (details or {}).get("followups", t.followups)]
        room = limits.HISTORY_BUDGET - len(json.dumps(asks, ensure_ascii=True)) - len(json.dumps(followups, ensure_ascii=True)) - len(json.dumps(cls._queue_state(t), ensure_ascii=True))
        extra = {k: v for k, v in (details or {}).items() if k in ("timeline", "progress") and v}
        body = sessions.fit([timeline.compact({"text": text, "steps": steps, **extra})], room)[0]
        if body["text"] != text or len(body["steps"]) != len(steps) or ("timeline" in extra and "timeline" not in body):
            for item in followups:
                item.pop("position", None)
        return events.ready(cls._frame(t, {"k": "snapshot", "text": body["text"], "steps": body["steps"],
                                           "asks": asks, **({"context": t.context} if t.context else {}),
                                           **({"settings": t.settings} if t.settings else {}),
                                           **({"activity": t.activity} if t.activity else {}),
                                           **{k: body[k] for k in ("timeline", "progress") if k in body},
                                           **({"owner": t.owner, "accepted": t.accepted} if t.owner else {}),
                                           **({"queue": cls._queue_state(t)} if t.queued else {}),
                                           **({"queue_paused": True} if t.queue_paused else {}),
                                           **({"followups": followups} if followups else {})}))

    def answer(self, pid: str, chat: str, request_id: str, decision: str, message: str | None, answers) -> None:
        """Ответ на карточку; неверные поля — `invalid`, карточки нет (или ход другого проекта) — `gone`."""
        if decision not in DECISIONS or not (message is None or (isinstance(message, str)
                                                                  and len(message) <= ANSWER_MESSAGE_MAX)):
            raise Refused("invalid")
        t = self._turn(pid, chat)
        ask = t.asks.get(request_id) if t is not None else None
        if ask is None:
            raise Refused("gone")
        if ask.event["kind"] == "question":
            if decision != "allow" or not _answers_ok(answers):
                raise Refused("invalid")
            result = PermissionResultAllow(updated_input={**ask.data, "answers": answers})
        elif answers is not None:
            raise Refused("invalid")
        elif decision == "deny":
            result = PermissionResultDeny(message=message or REFUSED)
        else:
            result = PermissionResultAllow(updated_input=ask.data,
                                           updated_permissions=ask.suggestions if decision == "always" else None)
        self._settle(t, request_id, result)

    async def stop(self, pid: str, chat: str) -> None:
        """Карточки хода — отказ, затем `interrupt()`; ход кончится `done` со `stopped`. Хода нет (или он другого
        проекта) — `idle`.

        `interrupt()` упал или не успел, или `stop` того же хода повторный — задача хода отменяется: `_run` доводит
        конец, `disconnect` гасит `claude.exe`. Ход уже кончается (`_finish`) — не отменяется: голая отмена посреди
        `disconnect` пропустила бы гашение `claude.exe` и метку `studio`."""
        t = self._turn(pid, chat)
        if t is None:
            raise Refused("idle")
        if t.finishing:
            log.info("ход %s: остановка — ход уже кончается", t.turn)
            return
        if t.stopped:
            log.info("ход %s: повторная остановка — отмена", t.turn)
            t.task.cancel()
            return
        t.stopped = True
        if not await self._halt(t) and self._turns.get(chat) is t and not t.finishing:
            log.info("ход %s: interrupt не прошёл — отмена", t.turn)
            t.task.cancel()

    async def close(self, final: bool = False, hold: bool = False) -> None:
        """Мост выходит (или отозван): остановить все ходы и дождаться их задач (дальше `CLOSE_SEC` — отмена);
        пока идёт — новые ходы `busy` «мост выходит». `final` (выход моста) — и после: канал ещё досылает очередь,
        а ход, начатый в этот миг, погиб бы с циклом без `done`. `hold` (отвязка, отзыв, замена) — и после, до
        `reopen()`: просьба `turn`, что уже в пути (её `find_project` ещё идёт), ход не начнёт."""
        self._closing = True
        self._final = self._final or final
        try:
            turns = list(self._turns.values())
            for t in turns:
                t.closing = True
            await asyncio.gather(*(self._halt(t) for t in turns))
            tasks = {t.task for t in turns}
            if not tasks:
                return
            _, pending = await asyncio.wait(tasks, timeout=CLOSE_SEC)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=CLOSE_SEC)
        finally:
            if final or hold:
                self._live_images.clear()
            if not (final or hold):
                self._closing = False

    def reopen(self) -> None:
        """После `close(hold=True)` — ходы снова можно (ядро зовёт перед новым каналом); после выхода — нет."""
        self._closing = self._final

    def live(self) -> dict[str, bool]:
        """Идущие ходы: чат → ждёт ли ход ответа на карточку."""
        return {chat: bool(t.asks) for chat, t in self._turns.items()}

    def live_image(self, project, chat, message_id, index):
        return self._live_images.get(project, chat, message_id, index)

    def activity(self) -> list[dict]:
        """Только идентификаторы и статус; чтение не создаёт сессий CLI."""
        return [{"project": t.pid, "chat": t.chat, "running": True, "asking": bool(t.asks)}
                for t in self._turns.values()]

    def children(self, pid, chat):
        """Живые и уже завершённые подзадачи текущего хода, без чтения диска из цикла."""
        t = self._turn(pid, chat)
        return copy.deepcopy(list(t.child_states.values())) if t is not None else []

    # --- ход ---

    async def _attachment_text(self, t, text, items):
        if not items:
            return text
        work = asyncio.create_task(asyncio.to_thread(t.files.prompt, text, items))
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            # Запись в потоке должна закончиться до _finish/cleanup.
            with contextlib.suppress(Exception):
                await work
            raise

    async def _run(self, t: _Turn, text: str, model: str | None, effort: str | None, images: list[dict]) -> None:
        failure, cancelled, new, sent = None, False, False, False
        try:
            text = await self._attachment_text(t, text, t.attachments)
            t.attachments = []
            new, model, effort = await asyncio.to_thread(self._choose, t, model, effort)
            saved = await asyncio.to_thread(self.choices.details, t.chat)
            remembered = saved.get("subagent_statuses")
            t.child_states = {key: {"id": key, "title": f"Агент {key}", "status": status}
                              for key, status in (remembered.items() if isinstance(remembered, dict) else ())
                              if isinstance(key, str) and REQUEST_ID.fullmatch(key)
                              and status in ("running", "completed", "failed", "stopped", "unknown")}
            metrics = saved.get("subagent_metrics") or {}
            for key, row in t.child_states.items():
                if isinstance(metrics, dict) and isinstance(metrics.get(key), dict):
                    row.update(task_metrics.numeric(metrics[key]))
            t.access = t.access or saved.get("access")
            t.context = saved.get("context")
            if t.access is not None:
                await asyncio.to_thread(self.choices.update, t.chat, access=t.access)
            if not (t.stopped or t.closing):
                options = await asyncio.to_thread(options_for, t.project, self.cli_path, t.chat, new, model, effort,
                                                  lambda tool, data, ctx: self._ask(t, tool, data, ctx), t.access,
                                                  subagent_hook=lambda data, _id, _ctx: self._child_hook(t, data))
                t.client = self.factory(options=options)
                await t.client.connect()
                await self._usage(t, model, effort)
            if not (t.stopped or t.closing):
                sent = True  # дальше файл сессии может появиться
                t.start_attempted = True
                await t.client.query(image_input.prompt(text, images) if images else text)
                self._accepted(t)
                while t.pending or t.background_tasks:
                    received_result = False
                    async for message in t.client.receive_response():
                        self._subtask(t, message)
                        self._cache_usage(t, message)
                        for e in t.feed.events(message):
                            self._send(t, e)
                        if isinstance(message, ResultMessage):
                            received_result = True
                            async with t.input_lock:
                                # Завершение фоновой работы тоже будит родителя и даёт ResultMessage.
                                t.pending = max(0, t.pending - 1)
                                if message.is_error:
                                    failure = _words(message.errors, message.result, message.subtype)
                                    t.pending = 0
                            if not t.pending and not t.background_tasks and failure is None:
                                await self._wait_queue(t)
                            async with t.input_lock:
                                if not t.pending and not t.background_tasks and t.queued and failure is None and not (t.stopped or t.closing):
                                    message_id, extra_text, extra_images = t.queued[0]
                                    await self._steer_input(t, extra_text, extra_images, message_id)
                                    self._queued_sent(t, message_id)
                                if not t.pending and not t.background_tasks:
                                    t.finishing = True
                            await self._usage(t, model, effort)
                    if failure is not None or t.stopped or t.closing:
                        break
                    if not received_result:
                        if t.background_tasks and not (t.stopped or t.closing):
                            failure = "Claude Code завершил поток до окончания фоновых задач"
                        break
        except asyncio.CancelledError:
            cancelled = True
        except CLINotFoundError:
            failure = NO_CLI
        except ResultError as e:
            failure = _words(e.errors, e.result, e.subtype)
        except Exception as e:
            log.warning("ход %s: %s", t.turn, type(e).__name__)
            failure = NO_START
        await self._finish(t, new and sent, failure, claude=sent)
        if cancelled:
            raise asyncio.CancelledError

    def _children_progress(self, t):
        t.progress["agents"] = copy.deepcopy(list(t.child_states.values()))
        self._send(t, {"k": "progress", "agents": t.progress["agents"]})

    async def _child_hook(self, t, data):
        """Дети Workflow не обязаны иметь отдельное task_started; SDK сообщает их через хуки."""
        child = data.get("agent_id")
        if not isinstance(child, str) or not REQUEST_ID.fullmatch(child) or t.finishing:
            return {}
        previous = t.child_states.get(child)
        row = dict(previous or {"id": child, "title": f"Агент {child}"})
        # SubagentStop вызывается до записи финального ответа и допускает блокирующие хуки.
        # Подтверждение completed берём из lifecycle или JSONL, не из самого вызова хука.
        row["status"] = "running" if data.get("hook_event_name") == "SubagentStart" else "unknown"
        if t.stopped or t.closing:
            row["status"] = "stopped"
        if previous and previous["status"] in ("completed", "failed", "stopped") and row["status"] == "unknown":
            return {}
        task_metrics.update(row, now=self.now(), started=data.get("hook_event_name") == "SubagentStart",
                            ended=row["status"] == "stopped")
        if row != previous:
            t.child_states[child] = row
            self._children_progress(t)
        return {}

    def _subtask(self, t, message):
        """Штатные lifecycle-события SDK; фоновые Bash-задачи в счётчик агентов не попадают."""
        if not isinstance(message, SystemMessage) or message.subtype not in (
                "task_started", "task_progress", "task_notification", "task_updated"):
            return
        data = message.data if isinstance(message.data, dict) else {}
        child = getattr(message, "task_id", None) or data.get("task_id")
        if not isinstance(child, str) or not REQUEST_ID.fullmatch(child):
            return
        previous = t.child_states.get(child)
        kind = getattr(message, "task_type", None) or data.get("task_type")
        patch = getattr(message, "patch", None) or data.get("patch") or {}
        status = getattr(message, "status", None) or data.get("status") or (patch.get("status") if isinstance(patch, dict) else None)
        task_kind = t.background_tasks.get(child) or kind
        if message.subtype == "task_started" and kind in ("local_agent", "local_workflow", "local_bash"):
            t.background_tasks[child] = kind
        if status in ("completed", "failed", "stopped", "killed"):
            t.background_tasks.pop(child, None)
        if task_kind == "local_workflow":
            return  # Workflow — контейнер; счётчик и история относятся к его дочерним агентам.
        if previous is None and (message.subtype != "task_started" or kind not in ("local_agent", "remote_agent", "agent", "subagent")):
            return
        row = dict(previous or {"id": child, "title": f"Агент {child}", "status": "running"})
        description = getattr(message, "description", None) or data.get("description")
        if isinstance(description, str) and description:
            row["title"] = description[:200]
        if status in ("running", "completed", "failed", "stopped", "killed"):
            row["status"] = "stopped" if status == "killed" else status
        elif message.subtype == "task_started":
            row["status"] = "running"
        task_metrics.update(row, now=self.now(), started=message.subtype == "task_started",
                            ended=row["status"] in ("completed", "failed", "stopped"), patch=patch,
                            usage=getattr(message, "usage", None) or data.get("usage"))
        if row == previous:
            return
        t.child_states[child] = row
        self._children_progress(t)

    def _cache_usage(self, t, message):
        """Начало ответа главного агента обновляет оценку; повторные блоки того же ответа — нет."""
        if getattr(message, "parent_tool_use_id", None) is not None:
            return
        if isinstance(message, StreamEvent) and message.event.get("type") == "message_start":
            raw = message.event.get("message") or {}
            model, message_id, usage = raw.get("model"), raw.get("id"), raw.get("usage")
        elif isinstance(message, AssistantMessage):
            model, message_id, usage = message.model, message.message_id, message.usage
        else:
            return
        if not t.context or not isinstance(model, str) or model == "<synthetic>" or not isinstance(message_id, str):
            return
        previous = t.context.get("cache") or {}
        ttl = permissions.claude_cache_ttl(usage)
        if (previous.get("message_id") == message_id and previous.get("model") == model
                and previous.get("updated_at") is not None):
            if ttl is not None and previous.get("ttl_seconds") != ttl:
                t.context = {**t.context, "cache": {**previous, "ttl_seconds": ttl}}
                self._send(t, {"k": "context", **t.context})
            return
        cached = (permissions.token_usage(usage) or {}).get("cached", 0)
        if ttl is None and cached > 0 and previous.get("model") == model:
            ttl = previous.get("ttl_seconds")
        t.context = {**t.context, "cache": {"updated_at": self.now(), "model": model,
                     "message_id": message_id, "ttl_seconds": ttl}}
        self._send(t, {"k": "context", **t.context})

    async def _usage(self, t, model, effort):
        """Числа самого CLI; без подмены приблизительным подсчётом длины текста."""
        if not hasattr(t.client, "get_context_usage"):
            return
        try:
            raw = await asyncio.wait_for(t.client.get_context_usage(), 10)
            info = await t.client.get_server_info() or {}
            t.settings = await asyncio.to_thread(cli.settings_from, info, t.project, raw, model=model, effort=effort, access=t.access)
            self._send(t, {"k": "settings", **t.settings})
            usage, cache = await asyncio.to_thread(sessions.context_stats, t.project, t.chat, (t.context or {}).get("cache"))
            current = permissions.context(raw.get("totalTokens"), raw.get("maxTokens"), usage)
            if current:
                if cache:
                    current["cache"] = cache
                t.context = current
                self._send(t, {"k": "context", **t.context})
        except Exception as e:
            log.info("ход %s: контекст: %s", t.turn, type(e).__name__)

    def _choose(self, t: _Turn, model: str | None, effort: str | None) -> tuple[bool, str | None, str | None]:
        """В потоке: путь `claude.exe`, новая ли сессия, модель и усилие хода (явные запоминаются; `None` —
        запомненное)."""
        self.cli_path = cli.current(self.cli_path, self.find_cli)
        new = not sessions.exists(t.project, t.chat)
        saved_model, saved_effort = self.choices.get(t.chat)
        if model is not None or effort is not None:
            self.choices.put(t.chat, model if model is not None else saved_model,
                             effort if effort is not None else saved_effort)
        model = model if model is not None else saved_model
        effort = effort if effort is not None else saved_effort
        if model is None and not new:
            model = sessions.last_model(t.project, t.chat)  # продолжение не меняет модель сессии
        return new, model, effort

    async def _finish(self, t: _Turn, mark: bool, failure: str | None, *, claude=False) -> None:
        """Конец хода: карточки — отказ, клиент гаснет, новой сессии — метка, чат свободен, затем `done`."""
        t.finishing = True
        try:
            self._deny_all(t)
            disconnected = t.client is None
            if t.client is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(t.client.disconnect(), DISCONNECT_SEC)
                    disconnected = True
            if mark and disconnected:  # клиент погашен: файл сессии не пишут двое
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(sessions.mark_studio, t.project, t.chat)
            elif claude and disconnected:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(sessions.make_visible, t.project, t.chat)
            if t.context:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(self.choices.update, t.chat, context=t.context)
            if t.child_states:
                changed = False
                for row in t.child_states.values():
                    if row["status"] == "running":
                        row["status"] = "stopped" if t.stopped or t.closing else "unknown"
                        task_metrics.update(row, now=self.now(), ended=True)
                        changed = True
                if changed:
                    self._children_progress(t)
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(self.choices.update, t.chat,
                                           subagent_statuses={key: row["status"] if row["status"] != "running" else "unknown"
                                                              for key, row in t.child_states.items()},
                                           subagent_metrics={key: task_metrics.numeric(row) for key, row in t.child_states.items()})
        finally:  # и при отмене посреди конца: чат не остаётся занятым
            try:
                await asyncio.to_thread(t.files.cleanup)
            except OSError as e:
                log.warning("ход %s: очистка вложений: %s", t.turn, type(e).__name__)
            finally:
                if self._turns.get(t.chat) is t:
                    del self._turns[t.chat]
                self._send(t, self._done(t, failure))

    def _done(self, t: _Turn, failure: str | None) -> dict:
        if t.closing:
            done = {"k": "done", "status": "error", "message": BRIDGE_STOPPED}
        elif t.stopped:
            done = {"k": "done", "status": "stopped"}
        elif failure is not None:
            done = {"k": "done", "status": "error", "message": failure}
        else:
            done = {"k": "done", "status": "done"}
        if not t.accepted and not t.start_attempted:
            done["not_started"] = True
        if t.queued:
            originals = {item["id"]: item["text"] for item in t.followups}
            done["unsent"] = [{"id": message_id, "text": originals.get(message_id, text)}
                              for message_id, text, _ in t.queued]
        log.info("ход %s: %s", t.turn, done["status"])
        return done

    async def _halt(self, t: _Turn) -> bool:
        """Карточки — отказ, клиенту — `interrupt()`; `False` — interrupt упал или не успел за `INTERRUPT_SEC`."""
        self._deny_all(t)
        t.queue_wake.set()
        if t.client is None:
            return True  # клиента ещё нет: ход сверит `stopped` до запуска
        waiting_background = not t.pending and bool(t.background_tasks)
        try:
            if t.background_tasks and hasattr(t.client, "stop_task"):
                await asyncio.wait_for(asyncio.gather(*(t.client.stop_task(child)
                    for child in list(t.background_tasks))), INTERRUPT_SEC)
            await asyncio.wait_for(t.client.interrupt(), INTERRUPT_SEC)
            if waiting_background and t.task is not None and not t.finishing:
                t.task.cancel()  # Родитель уже ответил: interrupt может больше не дать ResultMessage.
        except Exception as e:
            log.info("ход %s: interrupt: %s", t.turn, type(e).__name__)
            return False
        return True

    # --- карточки ---

    async def _ask(self, t: _Turn, tool: str, data: dict, ctx):
        """`can_use_tool`: событие `ask` и ожидание ответа студии (канал может смениться — карточка ждёт)."""
        if t.stopped or t.closing:
            return PermissionResultDeny(message=STOPPED, interrupt=True)
        rid = ctx.tool_use_id
        if not (isinstance(rid, str) and REQUEST_ID.fullmatch(rid)) or rid in t.asks:
            rid = secrets.token_hex(8)
        suggestions = list(ctx.suggestions or [])
        ask = _Ask(events.ask_event(rid, tool, data, bool(suggestions)), asyncio.get_running_loop().create_future(),
                   data, suggestions)
        t.asks[rid] = ask
        self._send(t, ask.event)
        try:
            return await ask.future
        finally:
            if t.asks.get(rid) is ask:  # SDK снял запрос (отмена) — карточка закрыта
                del t.asks[rid]
                self._send(t, {"k": "asked", "request_id": rid})

    def _settle(self, t: _Turn, rid: str, result) -> None:
        ask = t.asks.pop(rid)
        if not ask.future.done():
            ask.future.set_result(result)
        self._send(t, {"k": "asked", "request_id": rid})
        if t.activity is not None and not (t.stopped or t.closing or t.finishing):
            self._activity(t, "thinking")

    def _deny_all(self, t: _Turn) -> None:
        for rid in list(t.asks):
            self._settle(t, rid, PermissionResultDeny(message=STOPPED, interrupt=True))

    # --- кадры ---

    def _activity(self, t: _Turn, state: str, tool: str | None = None) -> None:
        """Только стадия из событий CLI; повторные дельты не создают одинаковые события."""
        current = {"state": "waiting" if t.asks else state}
        if current["state"] == "tool" and tool:
            current["tool"] = events.clip(tool, 200)
        if current != t.activity:
            t.activity = current
            self._send(t, {"k": "activity", **current})

    @staticmethod
    def _frame(t: _Turn, e: dict) -> dict:
        frame = {"t": "ev", "project": t.pid, "chat": t.chat, "turn": t.turn, "e": e}
        if e.get("k") in ("ask", "done"):
            frame["project_name"] = t.project.name
        return frame

    def _send(self, t: _Turn, e: dict) -> None:
        """Событие в канал (готовится снимок `watch` — за него); сбой канала ход не роняет."""
        t.feed.note(e)
        if e.get("k") in ("step", "result"):
            for step in t.feed.steps:
                if step["id"] == e.get("id"):
                    step.setdefault("status", "running")
                    if e["k"] == "result":
                        step["status"] = e.get("status", "failed" if e.get("error") else "completed")
                    break
        frame = self._frame(t, e)
        if t.held is not None:
            t.held.append(frame)
        else:
            self._emit(t, frame)

    def _emit(self, t: _Turn, frame: dict) -> None:
        try:
            self.emit(frame)
        except Exception as exc:
            log.warning("ход %s: событие не ушло: %s", t.turn, type(exc).__name__)


def _answers_ok(answers) -> bool:
    """Ответы на вопрос: словарь «вопрос → строка или список строк»."""
    return isinstance(answers, dict) and all(
        isinstance(k, str) and (isinstance(v, str) or (isinstance(v, list) and all(isinstance(s, str) for s in v)))
        for k, v in answers.items())
