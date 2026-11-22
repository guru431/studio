"""Общая основа помощников ACP по stdio (OpenCode, DeepSeek Harness): процесс, сессии, поток и карточки студии.

Только локальный процесс, без TCP и журналирования содержимого. Движок задаёт атрибуты класса и точки расширения
`AcpRunner`; то, что есть только у одного движка (дети, fork, нативный API), живёт в его модуле."""

import asyncio
import contextlib
import json
import logging
import math
import re
import secrets
import threading
import uuid
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path

from . import (VERSION, diffs, events, history_images, host, jobs, limits, metadata, opencode_events, permissions,
               sessions, store, timeline)
from .codex import same_project
from .errors import Refused
from .turns import Runner, _Ask

log = logging.getLogger(__name__)
RPC_SEC = 60
MODEL_ID = re.compile(r"[A-Za-z0-9._:\[\]/@+\-]{1,300}")
EFFORT_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


class RpcError(Exception):
    """Тело ошибки может содержать пути/тексты; в исключении только метод и код.

    `transport` — канал с ACP оборвался; без него — ответ живого процесса."""

    def __init__(self, *, method=None, code=None, not_found=False, transport=False):
        super().__init__()
        self.method, self.code, self.not_found = method, code if type(code) is int else None, not_found
        self.transport = transport


class Client:
    """ACP JSON-RPC с процессом `command` (целиком: программа и аргументы); `env` — окружение процесса или наследуемое."""

    def __init__(self, command: Sequence[str], cwd: Path, *, env: Mapping[str, str] | None = None,
                 spawn=asyncio.create_subprocess_exec):
        self.command, self.cwd, self.env, self.spawn = [str(part) for part in command], cwd, env, spawn
        self.process = self.reader = self.job = None
        self.pending, self.methods = {}, {}
        self.inbox = asyncio.Queue()
        self.seq = 0
        self.info = {}

    async def connect(self):
        self.process = await self.spawn(*self.command, cwd=str(self.cwd),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            limit=limits.FRAME_MAX, creationflags=host.NO_WINDOW,
            **({"env": dict(self.env)} if self.env is not None else {}), **host.process_group())
        # MCP-серверы и шеллы CLI — в его job: disconnect не оставит их сиротами.
        self.job = jobs.attach(getattr(self.process, "pid", None))
        self.reader = asyncio.create_task(self._read())
        try:
            self.info = await self.request("initialize", {"protocolVersion": 1,
                "clientInfo": {"name": "studio-agent", "version": VERSION},
                "clientCapabilities": {"elicitation": {"form": True}}})
        except BaseException:
            await self.disconnect()
            raise

    async def send(self, frame):
        self.process.stdin.write((json.dumps({"jsonrpc": "2.0", **frame}, ensure_ascii=True,
                                             allow_nan=False) + "\n").encode())
        await self.process.stdin.drain()

    async def request(self, method, params, *, timeout=RPC_SEC):
        self.seq += 1
        rid = self.seq
        future = asyncio.get_running_loop().create_future()
        self.pending[rid], self.methods[rid] = future, method
        try:
            await self.send({"id": rid, "method": method, "params": params})
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(rid, None)
            self.methods.pop(rid, None)

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                frame = json.loads(line)
                if not isinstance(frame, dict):
                    raise RpcError()
                if "method" in frame:
                    self.inbox.put_nowait(frame)
                elif (future := self.pending.get(frame.get("id"))) is not None and not future.done():
                    if "error" in frame:
                        error = frame.get("error") or {}
                        message = str(error.get("message", "")).lower()
                        future.set_exception(RpcError(method=self.methods.get(frame.get("id")), code=error.get("code"),
                            not_found="not found" in message or "does not exist" in message))
                    else:
                        future.set_result(frame.get("result", {}))
        except (ValueError, OSError, RpcError, TypeError, AttributeError):
            pass
        finally:
            for rid, future in list(self.pending.items()):
                if not future.done():
                    future.set_exception(RpcError(method=self.methods.get(rid), transport=True))
            self.inbox.put_nowait(None)

    async def disconnect(self):
        if self.process is not None:
            if self.process.stdin is not None:
                self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
                await self.process.wait()
        if self.job is not None:
            self.job.close()  # оставшиеся потомки CLI завершаются вместе с job
            self.job = None
        if self.reader is not None:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
        self.process = self.reader = None


class Aliases:
    """UUID студии → непрозрачный ID сессии ACP, проект и происхождение; без текстов."""

    def __init__(self, home: Path, filename: str, session_id: re.Pattern):
        self.path = home / filename
        self.session_id = session_id
        self.lock = threading.Lock()

    def load(self):
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict):
            raise ValueError(self.path.stem)
        return data

    def target(self, project, chat):
        item = self.load().get(chat)
        if item is None:
            return None
        target = item.get("session") if isinstance(item, dict) else None
        if not (isinstance(target, str) and self.session_id.fullmatch(target)
                and same_project(item.get("project"), project)):
            raise Refused("not_found")
        return target

    def put(self, project, chat, target):
        self.merge({chat: {"project": str(project.resolve()), "session": target, "studio": True}})

    def merge(self, values):
        with self.lock:
            data = self.load()
            data.update(values)
            store.write_atomic(self.path, json.dumps(data, ensure_ascii=True).encode())


def options(data, key):
    return next((o for o in data.get("configOptions", []) if o.get("id") == key), {})


def values(option):
    """ACP допускает как плоский список, так и группы вариантов."""
    out = []
    for row in option.get("options", []):
        if "options" in row:
            out.extend(values(row))
        elif isinstance(row.get("value"), str):
            out.append(row)
    return out


def settings(data, access=None, *, effort_key="effort"):
    return {"model": options(data, "model").get("currentValue"),
            "effort": options(data, effort_key).get("currentValue"), "access": access or "default",
            **({"agent_mode": options(data, "mode")["currentValue"]} if options(data, "mode").get("currentValue") else {}),
            **({'modes': [{"id": row['value'], "label": str(row.get('name') or row['value'])[:100]}
                          for row in values(options(data, 'mode'))][:100]} if options(data, 'mode') else {})}


def text_of(content):
    if not isinstance(content, dict):
        return ""
    if content.get("type") == "image":
        return "[изображение]"
    return content.get("text", "") if content.get("type") == "text" else ""


def step_of(update, previous=None):
    step = dict(previous or {"id": update["toolCallId"], "tool": "Tool", "input": {}, "text": "", "error": False})
    if "title" in update:
        step["tool"] = update["title"]
    if "rawInput" in update:
        step["input"] = events.clip(update["rawInput"])
    if "content" in update:
        chunks = []
        if changes := diffs.acp(update["content"]):
            step["changes"] = changes
        for item in update.get("content", []):
            if item.get("type") == "content":
                chunks.append(text_of(item.get("content")))
            elif item.get("type") == "diff":
                chunks.append(str(item.get("newText") or ""))
        step["text"] = events.clip("\n".join(chunks))
    elif "rawOutput" in update:
        output = update["rawOutput"]
        step["text"] = events.clip(output if isinstance(output, str) else json.dumps(output, ensure_ascii=False))
    if "status" in update:
        step["error"] = update["status"] == "failed"
    return events.bound(step)


def normalize(updates, *, image_blocks=None):
    out, current, user_id = [], None, None
    for update in updates:
        kind = update.get("sessionUpdate")
        if kind == "user_message_chunk":
            mid = update.get("messageId")
            if current is None or current["role"] != "user" or mid != user_id:
                current = {"id": mid if isinstance(mid, str) and re.fullmatch(r'msg_[A-Za-z0-9_-]{1,200}', mid)
                           else str(len(out)), "role": "user", "text": "", "steps": [], "time": None}
                out.append(current)
            user_id = mid
            content = update.get("content")
            if isinstance(content, dict) and content.get("type") == "image":
                images = current.setdefault("images", [])
                if len(images) < history_images.COUNT:
                    images.append(history_images.reference(len(images)))
                    if image_blocks is not None:
                        image_blocks.setdefault(current["id"], []).append(content)
            else:
                current["text"] += text_of(content)
        elif kind in ("agent_message_chunk", "agent_thought_chunk", "tool_call", "tool_call_update"):
            if current is None or current["role"] != "assistant":
                current = {"id": str(len(out)), "role": "assistant", "text": "", "steps": [], "time": None}
                out.append(current)
            if kind == "agent_message_chunk":
                timeline.append_text(current, text_of(update.get("content")))
            elif kind == 'agent_thought_chunk':
                old = next((s for s in current['steps'] if s['id'] == 'thought'), None)
                if old is None:
                    old = {'id': 'thought', 'tool': 'Рассуждения', 'input': {}, 'text': '', 'error': False}
                    timeline.append_step(current, old)
                old['text'] = events.clip(old['text'] + text_of(update.get('content')))
            elif isinstance(update.get("toolCallId"), str):
                old = next((s for s in current["steps"] if s["id"] == update["toolCallId"]), None)
                step = step_of(update, old)
                if old is None:
                    timeline.append_step(current, step)
                else:
                    old.update(step)
    return [timeline.compact(message) for message in out]


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


def usage(raw):
    if not isinstance(raw, dict):
        return None
    return permissions.token_usage({**raw, 'reasoningOutputTokens': raw.get('thoughtTokens')})


def context(update, previous=None):
    current = permissions.context(update.get('used'), update.get('size'))
    if current is None:
        return previous
    current = {**(previous or {}), **current}
    cost = update.get('cost')
    if (isinstance(cost, dict) and type(cost.get('amount')) in (int, float)
            and math.isfinite(cost['amount']) and cost['amount'] >= 0 and cost.get('currency') == 'USD'):
        current['cost'] = {'amount': cost['amount'], 'currency': 'USD'}
    return current


def drain(rpc, target, child=None, known=None):
    """Накопленные `session/update` сессии `target` из входящей очереди. `child(update, target, known)` отделяет
    дочерние сессии: None — родитель, False — метка без доказанной цепочки (отбрасывается), словарь — ребёнок."""
    updates = []
    known = known if known is not None else {}
    while not rpc.inbox.empty():
        frame = rpc.inbox.get_nowait()
        if frame and frame.get("method") == "session/update" and frame.get("params", {}).get("sessionId") == target:
            update = frame["params"].get("update", {})
            meta = child(update, target, known) if child is not None else None
            if meta:
                known[meta['id']] = {**known.get(meta['id'], {}), **meta}
            elif meta is None:
                updates.append(update)
    return updates


class TurnClient:
    def __init__(self, rpc):
        self.rpc, self.session = rpc, None

    async def interrupt(self):
        if self.session is not None:
            await self.rpc.send({"method": "session/cancel", "params": {"sessionId": self.session}})

    async def disconnect(self):
        await self.rpc.disconnect()


class AcpRunner(Runner):
    """Ходы помощника ACP. Подкласс задаёт атрибуты класса и `_connect`, `_open`, `_discard`, `_history_items`;
    если он сообщает детей (`_child_of` не None), то и `_child(t, child)`."""

    ENGINE: str  # имя движка в протоколе студии
    LABEL: str  # имя помощника в журнале и запасной версии
    SESSION_ID: re.Pattern
    EFFORT_KEY = "effort"  # id опции усилия в `configOptions`
    ALIASES_FILE: str  # файл соответствий UUID студии → сессия в каталоге моста
    CHOICES_DIR: str  # каталог настроек чатов (`store.Choices`)
    NAMESPACE: uuid.UUID  # UUID студии для сессий, созданных вне студии
    FAILED: str  # слова сбоя хода
    UNAVAILABLE: str  # слова отказа: выбранные модель, усилие или режим недоступны

    def __init__(self, emit, *, home: Path, choices=None, find_cli=None, factory=None):
        (home / self.CHOICES_DIR).mkdir(parents=True, exist_ok=True)
        super().__init__(emit, choices=choices or store.Choices(home / self.CHOICES_DIR), cli_path=None)
        self.find_acp = find_cli
        self.rpc_factory = factory
        self.aliases = Aliases(home, self.ALIASES_FILE, self.SESSION_ID)
        self.catalog = None
        self.models, self.efforts, self.version = [], [], ""
        self._catalog_rpc = None
        self._catalog_lock = asyncio.Lock()
        self._probe_lock = asyncio.Lock()
        self._histories = metadata.HistoryIndex()  # история по ревизии строки `session/list`: страницы и поиск

    # --- точки расширения ---

    async def _connect(self, project, *, access=None):
        """Запущенный и инициализированный `Client` в папке проекта; `access` — права чата хода."""
        raise NotImplementedError

    async def _mcp(self, rpc, project):
        return []

    async def _new(self, rpc, project, directories=None):
        return await rpc.request("session/new", {"cwd": str(project), "mcpServers": await self._mcp(rpc, project),
            **({'additionalDirectories': directories} if directories is not None else {})})

    async def _open(self, rpc, project, target, directories=None):
        """Существующая сессия в этом процессе; ответ — состояние `configOptions` (и `_studio_directories`)."""
        raise NotImplementedError

    async def _discard(self, rpc, target):
        """Удалить или закрыть пустую сессию, которую создал этот вызов; пользовательские сессии не трогает."""
        raise NotImplementedError

    async def _history_items(self, project, target, row, *, image_blocks=None):
        """Сообщения сессии (как `normalize`); `row` — строка `session/list` или None."""
        raise NotImplementedError

    def _child_of(self, update, target, known):
        return None

    def _progress(self, t, update):
        pass

    # --- общее ---

    async def _steer_input(self, t, text, images, message_id):
        return self._queue_input(t, text, images, message_id)

    def _updates(self, rpc, target, *, children=None):
        return drain(rpc, target, self._child_of, children)

    async def probe(self, root):
        async with self._probe_lock:
            return await self._probe(root)

    async def _probe(self, root):
        rpc, temporary, ready, catalog = None, None, False, None
        try:
            rpc = await self._connect(root)
            self.version = str(rpc.info.get("agentInfo", {}).get("version") or self.LABEL)[:64]
            data = await self._new(rpc, root)
            temporary = data["sessionId"]
            models = [{"id": row["value"], "label": str(row.get("name") or row["value"])[:100]}
                      for row in values(options(data, "model")) if MODEL_ID.fullmatch(row["value"])][:500]
            efforts = {row["value"] for row in values(options(data, self.EFFORT_KEY)) if EFFORT_ID.fullmatch(row["value"])}
            native = settings(data, effort_key=self.EFFORT_KEY)
            for model in models:
                if model["id"] == native["model"]:
                    continue
                changed = await rpc.request("session/set_config_option", {
                    "sessionId": temporary, "configId": "model", "value": model["id"]})
                efforts.update(row["value"] for row in values(options(changed, self.EFFORT_KEY))
                               if EFFORT_ID.fullmatch(row["value"]))
            catalog = {"version": self.version, "models": models, "efforts": sorted(efforts)[:100], "defaults": native}
            ready = bool(models)
        except Exception as e:
            log.warning("%s: каталог: %s", self.LABEL, type(e).__name__)
        finally:
            if rpc is not None:
                try:
                    if temporary is not None:
                        await asyncio.wait_for(self._discard(rpc, temporary), 5)
                except Exception as e:
                    ready = False
                    log.warning("%s: закрытие пробы: %s", self.LABEL, type(e).__name__)
                finally:
                    with contextlib.suppress(Exception):
                        await rpc.disconnect()
        if ready:
            self.catalog = catalog
            self.models, self.efforts = catalog["models"], catalog["efforts"]
        return ready

    async def chats(self, project):
        if self.catalog is None:
            return []
        async with self._catalog_lock:
            try:
                if self._catalog_rpc is None:
                    self._catalog_rpc = await self._connect(project)
                rows, cursor = [], None
                while len(rows) < limits.CHATS_MAX:
                    data = await self._catalog_rpc.request("session/list", {"cwd": str(project), **({"cursor": cursor} if cursor else {})})
                    rows.extend(data.get("sessions", []))
                    next_cursor = data.get("nextCursor")
                    if not next_cursor or next_cursor == cursor:
                        break
                    cursor = next_cursor
            except BaseException as e:
                # Ответ живого ACP (RpcError без обрыва) процесс каталога не гасит — как Codex `_metadata`.
                if isinstance(e, RpcError) and not e.transport:
                    raise
                if self._catalog_rpc is not None:
                    await self._catalog_rpc.disconnect()
                    self._catalog_rpc = None
                raise
        saved = await asyncio.to_thread(self.aliases.load)
        reverse = {row.get("session"): chat for chat, row in saved.items()
                   if isinstance(row, dict) and same_project(row.get("project"), project)}
        live, items, added = self.live(), [], {}
        for row in rows[:limits.CHATS_MAX]:
            target = row.get("sessionId")
            if not (isinstance(target, str) and self.SESSION_ID.fullmatch(target) and same_project(row.get("cwd"), project)):
                continue
            chat = reverse.get(target) or str(uuid.uuid5(self.NAMESPACE, target))
            if target not in reverse:
                added[chat] = {"project": str(project.resolve()), "session": target, "studio": False}
            items.append({"chat": chat, "engine": self.ENGINE, "title": str(row.get("title") or "")[:200],
                          "created": timestamp(row.get("createdAt")), "modified": timestamp(row.get("updatedAt")),
                          "tag": "studio" if saved.get(chat, {}).get("studio") else None,
                          "running": chat in live, "asking": bool(live.get(chat))})
        if added:
            await asyncio.to_thread(self.aliases.merge, added)
        return items

    async def close(self, final=False, hold=False):
        await super().close(final=final, hold=hold)
        self._histories.clear()
        async with self._catalog_lock:
            if self._catalog_rpc is not None:
                await self._catalog_rpc.disconnect()
                self._catalog_rpc = None

    @staticmethod
    def _revision(row):
        value = row.get('updatedAt') if row is not None else None
        return value if isinstance(value, (str, int)) and not isinstance(value, bool) else None

    async def _row(self, project, target):
        """Строка `session/list` сессии через общий процесс каталога (как `chats`): ревизия `updatedAt` и каталоги
        без запуска процесса ACP на каждое чтение; не нашлась или сбой — None (чтение идёт прежним путём)."""
        async with self._catalog_lock:
            try:
                if self._catalog_rpc is None:
                    self._catalog_rpc = await self._connect(project)
                cursor, seen = None, set()
                for _ in range(100):
                    page = await self._catalog_rpc.request(
                        'session/list', {'cwd': str(project), **({'cursor': cursor} if cursor else {})})
                    row = next((row for row in page.get('sessions', []) if row.get('sessionId') == target
                                and same_project(row.get('cwd'), project)), None)
                    if row is not None:
                        return row
                    cursor = page.get('nextCursor')
                    if not cursor or cursor in seen:
                        return None
                    seen.add(cursor)
            except BaseException as e:
                if not (isinstance(e, RpcError) and not e.transport) and self._catalog_rpc is not None:
                    await self._catalog_rpc.disconnect()
                    self._catalog_rpc = None
                if not isinstance(e, Exception):
                    raise
                log.info("%s: ревизия истории: %s", self.LABEL, type(e).__name__)
        return None

    async def history(self, project, chat, before, limit):
        target = await asyncio.to_thread(self.aliases.target, project, chat)
        if target is None:
            raise Refused("not_found", "нет такого чата")
        # Чтение всей сессии — отдельный процесс; его итог живёт до смены ревизии (`metadata.HistoryIndex`), так что
        # «более ранние» страницы и поиск по чатам не запускают процесс ACP заново. Идущий здесь ход — без кэша.
        key = (str(Path(project).resolve()), target)
        row = await self._row(project, target)
        before_revision = None if chat in self.live() else self._revision(row)
        items = self._histories.get(key, before_revision)
        if items is None:
            items = await self._history_items(project, target, row)
            if before_revision is not None:
                self._histories.put(key, before_revision, self._revision(await self._row(project, target)), items)
        total = len(items)
        end = min(before, total) if before is not None else total
        page = await asyncio.to_thread(sessions.fit, items[max(0, end - limit):end], budget=512 * 1024)
        return {"items": page, "start": end - len(page), "total": total, "engine": self.ENGINE}

    async def history_image(self, project, chat, message_id, index):
        target = await asyncio.to_thread(self.aliases.target, project, chat)
        if target is None:
            raise Refused("not_found", "нет такого чата")
        blocks = {}
        await self._history_items(project, target, None, image_blocks=blocks)
        return await asyncio.to_thread(history_images.payload, blocks.get(message_id), index, self.ENGINE, project)

    async def _configure(self, rpc, target, data, model, effort, agent_mode=None):
        for key, value in (("model", model), (self.EFFORT_KEY, effort), ('mode', agent_mode)):
            if value is None or options(data, key).get("currentValue") == value:
                continue
            if value not in {row["value"] for row in values(options(data, key))}:
                raise Refused("invalid", self.UNAVAILABLE)
            data = await rpc.request("session/set_config_option", {"sessionId": target, "configId": key, "value": value})
        return data

    async def state(self, project, chat, model=None, effort=None, access=None, *, agent_mode=None, additional_directories=None):
        saved = await asyncio.to_thread(self.choices.details, chat)
        model, effort, access = model or saved.get("model"), effort or saved.get("effort"), access or saved.get("access")
        target = await asyncio.to_thread(self.aliases.target, project, chat)
        rpc, temporary = await self._connect(project), None
        current = saved.get("context")
        agent_mode = agent_mode or saved.get('agent_mode')
        directories = additional_directories if additional_directories is not None else saved.get('additional_directories')
        commands = saved.get('commands', [])
        try:
            if target is not None:
                data = await self._open(rpc, project, target)
                if directories is None:
                    directories = data.get('_studio_directories')
                for update in self._updates(rpc, target):
                    if update.get("sessionUpdate") == "usage_update":
                        current = context(update, current)
                    elif update.get('sessionUpdate') == 'available_commands_update':
                        commands = opencode_events.commands(update)
            else:
                data = await self._new(rpc, project, directories)
                temporary = data["sessionId"]
            if model or effort or agent_mode or additional_directories is not None:
                if temporary is None:
                    # Предпросмотр выбора не меняет существующую сессию CLI.
                    previous = settings(data, effort_key=self.EFFORT_KEY)
                    data = await self._new(rpc, project, directories)
                    temporary = data["sessionId"]
                    model, effort = model or previous["model"], effort or previous["effort"]
                    agent_mode = agent_mode or previous.get('agent_mode')
                data = await self._configure(rpc, temporary, data, model, effort, agent_mode)
            for update in self._updates(rpc, temporary or target):
                if update.get('sessionUpdate') == 'available_commands_update':
                    commands = opencode_events.commands(update)
            return {"settings": {**settings(data, access, effort_key=self.EFFORT_KEY), 'commands': commands,
                    'additional_directories': directories or []}, "context": current}
        finally:
            try:
                if temporary is not None:
                    await self._discard(rpc, temporary)
            finally:
                await rpc.disconnect()

    async def _run(self, t, text, model, effort, images):
        failure, cancelled, requests, prompt = None, False, set(), None
        try:
            text = await self._attachment_text(t, text, t.attachments)
            t.attachments = []
            saved = await asyncio.to_thread(self.choices.details, t.chat)
            extra = getattr(t, 'opencode_options', {})
            agent_mode = extra.get('agent_mode') or saved.get('agent_mode')
            directories = extra.get('additional_directories')
            if directories is None:
                directories = saved.get('additional_directories')
            model, effort = model or saved.get("model"), effort or saved.get("effort")
            t.access, t.context = t.access or saved.get("access"), saved.get("context")
            rpc = await self._connect(t.project, access=t.access)
            client = t.client = TurnClient(rpc)
            target = await asyncio.to_thread(self.aliases.target, t.project, t.chat)
            if t.stopped or t.closing:
                return
            if target is None:
                data = await self._new(rpc, t.project, directories)
                target = data["sessionId"]
                if not isinstance(target, str) or not self.SESSION_ID.fullmatch(target):
                    raise Refused("invalid")
                await asyncio.to_thread(self.aliases.put, t.project, t.chat, target)
            else:
                data = await self._open(rpc, t.project, target, directories)
                if directories is None:
                    directories = data.get('_studio_directories')
            client.session = target
            replay = self._updates(rpc, target)
            command_list = next((opencode_events.commands(u) for u in reversed(replay)
                                 if u.get('sessionUpdate') == 'available_commands_update'), [])
            data = await self._configure(rpc, target, data, model, effort, agent_mode)
            t.settings = {**settings(data, t.access, effort_key=self.EFFORT_KEY), 'commands': command_list}
            self._send(t, {"k": "settings", **t.settings})
            await asyncio.to_thread(self.choices.update, t.chat, model=model, effort=effort, access=t.access,
                                    agent_mode=t.settings.get('agent_mode'), additional_directories=directories,
                                    commands=command_list)
            if t.stopped or t.closing:
                return
            self._activity(t, "thinking")
            content = [{"type": "image", "mimeType": image["mime"], "data": image["data"]} for image in images]
            if text.strip():
                content.insert(0, {"type": "text", "text": text})
            t.start_attempted = True
            prompt = asyncio.create_task(rpc.request("session/prompt", {"sessionId": target, "prompt": content}, timeout=None))
            self._accepted(t)
            marker = object()
            prompt.add_done_callback(lambda _: rpc.inbox.put_nowait(marker))
            while True:
                frame = await rpc.inbox.get()
                if frame is marker:
                    result = await prompt
                    spent = usage(result.get('usage'))
                    if spent:
                        t.context = t.context or {}
                        previous = t.context.get('usage', {})
                        t.context['usage'] = {k: previous.get(k, 0) + v for k, v in spent.items()}
                        self._send(t, {'k': 'context', **t.context})
                    self._status(t, {'retry': None})
                    if result.get("stopReason") == "cancelled":
                        t.stopped = True
                    if not t.stopped:
                        await self._wait_queue(t)
                    async with t.input_lock:
                        if not t.queued or t.stopped or t.closing or t.queue_expired:
                            t.finishing = True
                            break
                        message_id, extra_text, extra_images = t.queued[0]
                        content = [{"type": "text", "text": extra_text}] if extra_text.strip() else []
                        content += [{"type": "image", "mimeType": i["mime"], "data": i["data"]} for i in extra_images]
                        prompt = asyncio.create_task(rpc.request("session/prompt", {"sessionId": target, "prompt": content}, timeout=None))
                        prompt.add_done_callback(lambda _: rpc.inbox.put_nowait(marker))
                        self._queued_sent(t, message_id)
                    continue
                if frame is None:
                    raise RpcError()
                if "id" in frame:
                    # Принадлежность сессии — на момент прихода запроса, а не когда до него дойдёт задача.
                    session = frame.get("params", {}).get("sessionId")
                    params = frame.get('params', {})
                    tool = params.get('toolCall') or {}
                    meta = self._child_of(tool, target, t.opencode_children)
                    if meta:
                        self._child(t, meta)
                        frame['child'] = meta['id']
                    if meta is False:
                        session = None
                    frame["family"] = session == target or session in t.subtasks
                    task = asyncio.create_task(self._request(t, frame))
                    requests.add(task)
                    task.add_done_callback(requests.discard)
                elif frame.get("method") == "session/update" and frame.get("params", {}).get("sessionId") == target:
                    self._notification(t, frame["params"].get("update", {}))
        except asyncio.CancelledError:
            cancelled = True
        except Refused as e:
            failure = e.message
        except Exception as e:
            log.warning("ход %s: %s: %s", t.turn, self.LABEL, type(e).__name__)
            failure = self.FAILED
        finally:
            if prompt is not None:
                if not prompt.done():
                    prompt.cancel()
                await asyncio.gather(prompt, return_exceptions=True)
            t.finishing = True
            self._deny_all(t)
            if requests:
                for task in requests:
                    task.cancel()
                await asyncio.gather(*requests, return_exceptions=True)
            await self._finish(t, False, failure)
        if cancelled:
            raise asyncio.CancelledError

    def _notification(self, t, update):
        child = self._child_of(update, t.client.session, t.opencode_children)
        if child is False:
            return
        if child:
            self._child(t, child)
            return
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            self._activity(t, "responding")
            delta = text_of(update.get("content"))
            t.feed.text += delta
            self._send(t, {"k": "text", "text": delta})
        elif kind in ("tool_call", "tool_call_update") and isinstance(update.get("toolCallId"), str):
            old = next((s for s in t.feed.steps if s["id"] == update["toolCallId"]), None)
            step = step_of(update, old)
            if old is None:
                t.feed.steps.append(step)
                self._send(t, {"k": "step", **{k: v for k, v in step.items() if k not in ("text", "error")}})
            else:
                old.update(step)
            if update.get("status") in ("completed", "failed"):
                self._send(t, {"k": "result", "id": step["id"], "text": step["text"], "error": step["error"],
                               **({"changes": step["changes"]} if step.get("changes") and not step["error"] else {})})
                self._activity(t, "thinking")
            else:
                self._activity(t, "tool", step["tool"])
        elif kind == "agent_thought_chunk":
            self._activity(t, "thinking")
            old = next((s for s in t.feed.steps if s['id'] == 'thought'), None)
            if old is None:
                old = {'id': 'thought', 'tool': 'Рассуждения', 'input': {}, 'text': '', 'error': False}
                t.feed.steps.append(old)
                self._send(t, {'k': 'step', **old})
            old['text'] = events.clip(old['text'] + text_of(update.get('content')))
            self._send(t, {'k': 'result', **old})
        elif kind == "usage_update":
            current = context(update, t.context)
            if current:
                t.context = current
                self._send(t, {"k": "context", **current})
        elif kind == "config_option_update":
            t.settings = {**(t.settings or {}), **settings(update, t.access, effort_key=self.EFFORT_KEY)}
            self._send(t, {"k": "settings", **t.settings})
        elif kind == 'available_commands_update':
            t.settings = {**(t.settings or {}), 'commands': opencode_events.commands(update)}
            self._send(t, {'k': 'settings', **t.settings})
        else:
            self._progress(t, update)

    def _status(self, t, data):
        if data:
            t.progress[self.ENGINE] = {**t.progress.get(self.ENGINE, {}), **data}
            self._send(t, {'k': 'progress', **t.progress})

    async def _request(self, t, frame):
        try:
            await self._request_card(t, frame)
        except Exception as e:
            log.warning("ход %s: запрос %s: %s", t.turn, self.LABEL, type(e).__name__)
            with contextlib.suppress(Exception):
                await t.client.rpc.send({"id": frame["id"], "error": {"code": -32603, "message": "Request failed"}})

    async def _request_card(self, t, frame):
        method, p = frame["method"], frame.get("params", {})
        session = p.get("sessionId")
        # Чужая сессия — отказ; дочерняя сессия этого хода — карточка с её именем, ответ тем же id.
        if not frame.get("family", True) or session != t.client.session and (
                not isinstance(session, str) or session not in t.subtasks):
            await t.client.rpc.send({"id": frame["id"], "error": {"code": -32601, "message": "Unsupported request"}})
            return
        child = frame.get('child') or (session if session != t.client.session else None)
        subtask = (t.subtasks.get(child) or "без имени") if child else None
        rid = secrets.token_hex(8)
        if method == "session/request_permission":
            offered = p.get("options", [])
            if t.access == "auto" and (once := next((o for o in offered if o.get("kind") == "allow_once"), None)):
                await t.client.rpc.send({"id": frame["id"], "result": {"outcome": {
                    "outcome": "selected", "optionId": once["optionId"]}}})
                return
            tool = p.get("toolCall", {})
            always = any(o.get("kind") == "allow_always" for o in offered)
            event = events.ask_event(rid, tool.get("title", self.LABEL), tool.get("rawInput") or tool, always)
        elif method == "elicitation/create" and p.get("mode") == "form":
            questions, fields = [], {}
            for key, schema in p.get("requestedSchema", {}).get("properties", {}).items():
                if key.endswith("_custom"):
                    continue
                label = schema.get("title") or key
                choices = schema.get("oneOf") or [{"const": v, "title": str(v)} for v in schema.get("enum", [])]
                multi = schema.get("type") == "array"
                if multi:
                    items = schema.get("items", {})
                    choices = items.get("oneOf") or [{"const": v, "title": str(v)} for v in items.get("enum", [])]
                fields[key] = {"label": label, "schema": schema, "choices": choices, "multi": multi}
                questions.append({"id": key, "question": label, "header": None, "multi": multi,
                    "options": [{"id": f"option-{i}", "label": str(o.get("title") or o.get("const")),
                                 "description": o.get("description", "")} for i, o in enumerate(choices)]})
            if not questions:
                await t.client.rpc.send({"id": frame["id"], "result": {"action": "cancel"}})
                return
            event = events.bound({"k": "ask", "request_id": rid, "kind": "question", "questions": questions})
            frame = {**frame, "_fields": fields}
        else:
            await t.client.rpc.send({"id": frame["id"], "error": {"code": -32601, "message": "Unsupported request"}})
            return
        if subtask:
            event["subtask"] = subtask
        ask = _Ask(event, asyncio.get_running_loop().create_future(), {"frame": frame}, [])
        t.asks[rid] = ask
        self._send(t, event)
        self._activity(t, "waiting")
        try:
            result = await ask.future
            if result is not None:
                await t.client.rpc.send({"id": frame["id"], "result": result})
        finally:
            if t.asks.get(rid) is ask:
                del t.asks[rid]
                self._send(t, {"k": "asked", "request_id": rid})

    def answer(self, pid, chat, request_id, decision, message, answers):
        if decision not in ("allow", "always", "deny"):
            raise Refused("invalid")
        t = self._turn(pid, chat)
        ask = t.asks.get(request_id) if t is not None else None
        if ask is None:
            raise Refused("gone")
        frame = ask.data["frame"]
        if ask.event["kind"] == "question":
            if decision == "deny":
                result = {"action": "decline"}
            elif decision != "allow" or not isinstance(answers, dict):
                raise Refused("invalid")
            else:
                content = {}
                for key, field in frame["_fields"].items():
                    value = answers.get(key)
                    if value is None and sum(row["label"] == field["label"] for row in frame["_fields"].values()) == 1:
                        value = answers.get(field["label"])  # страницы прежней версии
                    choices = field["choices"]
                    if isinstance(value, list):
                        by_id = {f"option-{i}": o.get("const") for i, o in enumerate(choices)}
                        if (not value or len(value) > 100 or not field["multi"] and len(value) != 1
                                or any(not isinstance(v, str) or v not in by_id for v in value)
                                or len(set(value)) != len(value)):
                            raise Refused("invalid")
                        selected = [by_id[v] for v in value]
                        content[key] = selected if field["multi"] else selected[0]
                        continue
                    if not isinstance(value, str) or not value or len(value) > 2000:
                        raise Refused("invalid")
                    # Старые страницы присылают подписи. Принимаем только однозначный выбор;
                    # массив ID всегда обозначает выбор, строка ID остаётся свободным текстом.
                    labels = {}
                    for option in choices:
                        labels.setdefault(str(option.get("title") or option.get("const")), []).append(option.get("const"))
                    if field["multi"]:
                        variants = []
                        if value in labels:
                            variants.append([value])
                        split = value.split(", ")
                        if len(split) > 1 and all(v in labels for v in split):
                            variants.append(split)
                        if len(variants) != 1 or any(len(labels[v]) != 1 for v in variants[0]):
                            raise Refused("invalid")
                        content[key] = [labels[v][0] for v in variants[0]]
                    elif value in labels:
                        if len(labels[value]) != 1:
                            raise Refused("invalid")
                        content[key] = labels[value][0]
                    elif choices:
                        custom = key + "_custom"
                        if custom not in frame["params"].get("requestedSchema", {}).get("properties", {}):
                            raise Refused("invalid")
                        content[custom] = value
                    else:
                        schema = field["schema"]
                        if not choices and schema.get("type") == "boolean" and value.lower() not in ("true", "false"):
                            raise Refused("invalid")
                        try:
                            content[key] = (int(value) if schema.get("type") == "integer" else
                                float(value) if schema.get("type") == "number" else
                                value.lower() == "true" if schema.get("type") == "boolean" else value)
                        except ValueError:
                            raise Refused("invalid") from None
                result = {"action": "accept", "content": content}
        else:
            if answers is not None:
                raise Refused("invalid")
            kind = {"allow": "allow_once", "always": "allow_always", "deny": "reject_once"}[decision]
            option = next((o for o in frame["params"].get("options", []) if o.get("kind") == kind), None)
            if option is None:
                if decision != "deny":
                    raise Refused("invalid")
                result = {"outcome": {"outcome": "cancelled"}}
            else:
                result = {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
        self._settle(t, request_id, result)

    def _deny_all(self, t):
        for rid, ask in list(t.asks.items()):
            result = {"action": "cancel"} if ask.event["kind"] == "question" else {"outcome": {"outcome": "cancelled"}}
            self._settle(t, rid, result)
