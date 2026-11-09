"""Codex в мосте: каталог app-server, история и ходы с теми же карточками и снимками, что у Claude Code."""

import asyncio
import contextlib
import json
import logging
import os
import secrets
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import codex_progress, codex_rpc, diffs, events, limits, metadata, permissions, sessions, store, timeline
from .errors import Refused
from .turns import Runner, _Ask

log = logging.getLogger(__name__)
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def same_project(left, right) -> bool:
    return isinstance(left, str) and str(Path(left).resolve()).casefold() == str(Path(right).resolve()).casefold()


def last_context(path, home=None):
    """Последний счётчик CLI у сессии, открытой ещё до запуска моста; тексты не возвращаются."""
    if not isinstance(path, str):
        return None
    root = Path(home or os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
    try:
        target = Path(path).resolve()
        if not target.is_relative_to(root):
            return None
        with target.open("rb") as source:
            source.seek(0, 2)
            source.seek(max(0, source.tell() - 512 * 1024))
            lines = source.read().splitlines()
        result, updated_at, model, collecting = None, None, None, True
        for line in reversed(lines):
            try:
                row = json.loads(line)
                payload = row.get("payload", {})
                if payload.get("type") == "token_count":
                    info = payload.get("info") or {}
                    current = permissions.context((info.get("last_token_usage") or {}).get("total_tokens"),
                                                  info.get("model_context_window"),
                                                  permissions.token_usage(info.get("total_token_usage")))
                    if current and collecting:
                        if result is not None and current != result:
                            if model is not None:
                                break
                            collecting = False
                            continue
                        result = current
                        # CLI повторяет счётчики при обновлении лимитов. Они не продлевают кэш:
                        # берём первую запись последнего изменения, а не последний timestamp.
                        updated_at, model = None, None
                        stamp = row.get("timestamp")
                        if isinstance(stamp, str):
                            parsed = datetime.fromisoformat(stamp)
                            if parsed.tzinfo is not None:
                                updated_at = parsed.timestamp()
                elif row.get("type") == "turn_context" and result is not None and model is None:
                    model = payload.get("model")
                    if not collecting:
                        break
            except (ValueError, AttributeError):
                continue
        if result is not None:
            if updated_at is not None and updated_at > 0 and isinstance(model, str):
                result["cache"] = {"updated_at": updated_at, "model": model}
            return result
    except OSError:
        pass
    return None


class Aliases:
    """UUID студии → UUID Codex и настоящий проект. Здесь только идентификаторы, без текстов чатов."""

    def __init__(self, home: Path):
        self.path = home / "codex-chats.json"
        self.lock = threading.Lock()

    def load(self):
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict):
            raise ValueError("codex-chats")
        return data

    def target(self, project, chat):
        item = self.load().get(chat)
        if item is None:
            return chat
        if not isinstance(item, dict) or not same_project(item.get("project"), project):
            raise Refused("not_found")
        thread = item.get("thread")
        if not isinstance(thread, str) or not limits.CHAT_ID.fullmatch(thread):
            raise Refused("not_found")
        return thread

    def put(self, project, chat, thread):
        with self.lock:
            data = self.load()
            data[chat] = {"project": str(project.resolve()), "thread": thread}
            store.write_atomic(self.path, json.dumps(data, ensure_ascii=True).encode())


def web_search(item):
    action = item.get("action")
    action = action if isinstance(action, dict) else {}
    query = item.get("query")
    if not isinstance(query, str) or not query:
        query = action.get("query")
    fields = {"query": query, "url": action.get("url"), "pattern": action.get("pattern"), "action": action.get("type")}
    data = {key: value for key, value in fields.items() if isinstance(value, str) and value}
    if isinstance(queries := action.get("queries"), list):
        data["queries"] = [query for query in queries[:20] if isinstance(query, str)]
    if not data:
        data["query"] = ""
    results = item.get("results")
    if not isinstance(results, list):
        return data, ""
    rows = []
    for result in results[:20]:
        if not isinstance(result, dict):
            continue
        lines = [events.clip(value, size) for key, size in (("title", 300), ("url", 2048), ("snippet", 500))
                 if isinstance(value := result.get(key), str) and value]
        if lines:
            rows.append("\n".join(lines))
    return data, "\n\n".join(rows)


def step_of(item):
    kind = item.get("type")
    if kind == "commandExecution":
        tool, data = "Shell", {"command": item.get("command"), "cwd": item.get("cwd")}
        output = item.get("aggregatedOutput")
        text = output if isinstance(output, str) else ""
    elif kind == "fileChange":
        tool, data, text = "Edit", {"changes": item.get("changes", [])}, ""
    elif kind in ("mcpToolCall", "dynamicToolCall"):
        tool, data = item.get("tool", kind), item.get("arguments", {})
        text = json.dumps(item.get("result") or item.get("contentItems") or item.get("error") or "", ensure_ascii=False)
    elif kind == "webSearch":
        tool = "WebSearch"
        data, text = web_search(item)
    elif kind == "collabAgentToolCall":
        tool, data, text = item.get("tool", "Agent"), {"prompt": item.get("prompt", "")}, ""
    else:
        return None
    step = {"id": item["id"], "tool": tool, "input": events.clip(data),
            "text": events.clip(text), "error": item.get("status") in ("failed", "declined")}
    status = item.get("status")
    if status in ("inProgress", "completed", "failed", "declined"):
        step["status"] = {"inProgress": "running", "declined": "failed"}.get(status, status)
    for source, key in (("durationMs", "duration_ms"), ("exitCode", "exit_code")):
        if type(item.get(source)) is int:
            step[key] = item[source]
    if kind == "commandExecution":
        if len(text) > limits.CLIP:
            step["output_truncated"] = True
        if isinstance(item.get("processId"), str):
            step["process_id"] = item["processId"][:200]
        if item.get("exitCode") not in (None, 0):
            step.update(error=True, status="failed")
    elif kind == "collabAgentToolCall":
        step["agents"] = codex_progress.children([item])
    if (kind == "dynamicToolCall" and item.get("success") is False
            or kind == "mcpToolCall" and item.get("error") is not None):
        step.update(error=True, status="failed")
    if kind == "fileChange" and item.get("status") == "completed":
        if changes := diffs.codex(item.get("changes")):
            step["changes"] = changes
    return events.bound(step)


def spawned(thread):
    """`thread/started` субагента: (родительский поток, имя). Thread v2 несёт `parentThreadId` и
    `agentNickname`/`agentRole`; источник `subAgent.thread_spawn` — запасной путь того же смысла."""
    source = thread.get("source")
    sub = source.get("subAgent") if isinstance(source, dict) else None
    spawn = sub.get("thread_spawn") or sub.get("threadSpawn") if isinstance(sub, dict) else None
    spawn = spawn if isinstance(spawn, dict) else {}
    parent = thread.get("parentThreadId") or spawn.get("parent_thread_id") or spawn.get("parentThreadId")
    name = next((value.strip()[:200] for value in (thread.get("agentNickname"), spawn.get("agent_nickname"),
                 thread.get("agentRole"), spawn.get("agent_role"), spawn.get("agent_path"))
                 if isinstance(value, str) and value.strip()), None)
    return parent, name


def normalize(turns):
    out = []
    for turn in turns:
        answer = None
        for item_index, item in enumerate(turn.get("items", [])):
            if item.get("type") == "userMessage":
                text = "\n".join(c.get("text", "[изображение]") for c in item.get("content", [])
                                 if isinstance(c, dict) and c.get("type") in ("text", "image", "localImage"))
                out.append({"id": item["id"], "role": "user", "text": text, "steps": [], "time": None,
                            **({"forkable": False} if item_index else {})})
                answer = None
                continue
            step = step_of(item)
            text = item.get("text", "") if item.get("type") == "agentMessage" else ""
            if not step and not text:
                continue
            if answer is None:
                answer = {"id": item["id"], "role": "assistant", "text": "", "steps": [], "time": None}
                out.append(answer)
            if step:
                timeline.append_step(answer, step)
            if text:
                timeline.append_text(answer, ("\n\n" if answer["text"] else "") + text,
                                     block=item.get("id"), phase=item.get("phase"))
        if answer is not None:
            agents = codex_progress.children(turn.get("items", []))
            if agents:
                answer["progress"] = {"agents": agents}
    return [timeline.compact(message) for message in out]


class _TurnClient:
    def __init__(self, rpc):
        self.rpc = rpc
        self.thread = self.turn = None

    async def interrupt(self):
        if self.turn is not None:
            await self.rpc.request("turn/interrupt", {"threadId": self.thread, "turnId": self.turn})

    async def disconnect(self):
        await self.rpc.disconnect()


class CodexRunner(Runner):
    def __init__(self, emit, *, home, choices=None, find_cli=None, factory=codex_rpc.Client, now=time.time):
        self.find_codex = find_cli or codex_rpc.find_cli
        super().__init__(emit, choices=choices or store.Choices(home), cli_path=None)
        self.aliases = Aliases(home)
        self.rpc_factory = factory
        self.now = now
        self.catalog = None
        self.models, self.efforts, self.version = [], [], ""
        self.default_efforts = {}
        self._meta = None
        self._meta_lock = asyncio.Lock()
        self._usage_meta = None
        self._usage_lock = asyncio.Lock()
        self._histories = metadata.HistoryIndex()
        self._history_paths = {}

    async def takeover(self, project, pid, chat, owner, *, fork=False):
        if self._turn(pid, chat) is not None:
            return self.claim(pid, chat, owner)
        target = await asyncio.to_thread(self.aliases.target, project, chat)
        path = await asyncio.to_thread(self.find_codex)
        if path is None:
            raise Refused("unsupported")
        rpc, opened = self.rpc_factory(path, project), None
        try:
            await rpc.connect()
            try:
                data = await rpc.request("thread/read", {"threadId": target})
            except codex_rpc.RpcError as e:
                if e.not_found:
                    return {"available": True}
                raise
            if not same_project(data.get("thread", {}).get("cwd"), project):
                raise Refused("not_found")
            data = await rpc.request("thread/fork" if fork else "thread/resume", {"threadId": target})
            opened = data.get("thread", {}).get("id")
            if not isinstance(opened, str) or not limits.CHAT_ID.fullmatch(opened) or not same_project(
                    data.get("thread", {}).get("cwd"), project) or (fork and opened == target):
                raise Refused("invalid")
            if fork:
                await asyncio.to_thread(self.aliases.put, project, chat, opened)
            return {"available": True, "forked": fork}
        except codex_rpc.RpcError as e:
            if e.writer_active:
                return {"available": False, "external": True}
            raise Refused("conflict", "Не удалось проверить сессию Codex — повторите позже") from None
        finally:
            if opened:
                with contextlib.suppress(codex_rpc.RpcError):
                    await rpc.request("thread/unsubscribe", {"threadId": opened})
            await rpc.disconnect()

    async def _steer_input(self, t, text, images, message_id):
        content = [{"type": "image", "url": f"data:{i['mime']};base64,{i['data']}"} for i in images]
        if text.strip():
            content.append({"type": "text", "text": text})
        try:
            await t.client.rpc.request("turn/steer", {"threadId": t.client.thread,
                "expectedTurnId": t.client.turn, "input": content})
        except codex_rpc.RpcError:
            raise Refused("conflict", "Codex не принял уточнение — ход мог завершиться; текст сохранён в поле") from None
        return "steer"

    def _done(self, t, failure):
        result = super()._done(t, failure)
        if failure is not None and not t.accepted and not getattr(t, "start_attempted", False):
            result["not_started"] = True
            if getattr(t, "writer_active", False):
                result["code"] = "writer_active"
        return result

    async def subscription_usage(self, root):
        """Только account RPC: не открывает thread и не расходует квоту генерации.

        Старый app-server может знать лимиты, но не usage; отказ одного метода не стирает другой.
        Общий бюджет меньше 30 секунд ожидания сервера студии.
        """
        result = {}
        try:
            async with asyncio.timeout(24):
                async with self._usage_lock:
                    try:
                        if self._usage_meta is None:
                            path = await asyncio.to_thread(self.find_codex)
                            if path is None:
                                return result
                            self._usage_meta = self.rpc_factory(path, Path(root))
                            await self._usage_meta.connect()
                        for key, method, params in (("account", "account/read", {"refreshToken": False}),
                                ("limits", "account/rateLimits/read", {}), ("usage", "account/usage/read", {})):
                            try:
                                value = await self._usage_meta.request(method, params)
                                if isinstance(value, dict):
                                    result[key] = value
                            except codex_rpc.RpcError:
                                continue
                    except BaseException:
                        if self._usage_meta is not None:
                            await self._usage_meta.disconnect()
                            self._usage_meta = None
                        raise
        except (TimeoutError, OSError, codex_rpc.RpcError):
            pass
        return result

    async def _metadata(self, project, method, params):
        async with self._meta_lock:
            try:
                if self._meta is None:
                    path = await asyncio.to_thread(self.find_codex)
                    if path is None:
                        raise Refused("unsupported", "Codex не найден на компьютере")
                    self._meta = self.rpc_factory(path, Path(project))
                    await self._meta.connect()
                return await self._meta.request(method, params)
            except BaseException as e:
                # Ответ живого сервера (not_found нового чата и прочие RpcError) процесс не гасит:
                # перезапуск задержал бы под `_meta_lock` все чтения Codex. Закрывают обрыв, срок, отмена.
                if isinstance(e, codex_rpc.RpcError) and not e.transport:
                    raise
                if self._meta is not None:
                    await self._meta.disconnect()
                    self._meta = None
                raise

    async def probe(self, root) -> bool:
        try:
            path = await asyncio.to_thread(self.find_codex)
            if path is None:
                self.catalog = None
                return False
            self.version = await asyncio.to_thread(codex_rpc.version, path)
            data = await self._metadata(root, "model/list", {"limit": 50})
            models, efforts = [], set()
            for m in data.get("data", []):
                value = m.get("model")
                if m.get("hidden") or not isinstance(value, str) or not limits.MODEL_ID.fullmatch(value):
                    continue
                models.append({"id": value, "label": str(m.get("displayName") or value)[:100]})
                efforts.update(e.get("reasoningEffort") for e in m.get("supportedReasoningEfforts", []))
            self.models = models[:50]
            self.default_efforts = {m.get("model"): m.get("defaultReasoningEffort") for m in data.get("data", [])}
            self.efforts = [e for e in EFFORTS if e in efforts]
            self.catalog = {"version": self.version, "models": self.models, "efforts": self.efforts}
            try:
                cfg = (await self._metadata(root, "config/read", {"cwd": str(root), "includeLayers": False}))["config"]
                default = next((m for m in data.get("data", []) if m.get("isDefault")), {})
                native = await self._metadata(root, "thread/start", {"cwd": str(root), "ephemeral": True})
                self.catalog["defaults"] = {"model": native.get("model") or cfg.get("model") or default.get("model"),
                    "effort": cfg.get("model_reasoning_effort") or default.get("defaultReasoningEffort"),
                    "access": permissions.codex_value(native.get("sandbox")) or cfg.get("sandbox_mode")}
                if tid := native.get("thread", {}).get("id"):
                    with contextlib.suppress(codex_rpc.RpcError):
                        await self._metadata(root, "thread/unsubscribe", {"threadId": tid})
            except (codex_rpc.RpcError, KeyError):
                pass
            return bool(self.models)
        except Exception as e:
            log.warning("Codex: каталог: %s", type(e).__name__)
            return False

    async def chats(self, project):
        if self.catalog is None:
            return []
        # Обычный thread/list также сканирует JSONL для восстановления метаданных.
        data = await self._metadata(project, "thread/list", {"cwd": str(project), "limit": limits.CHATS_MAX,
                                                           "useStateDbOnly": True, "sortKey": "updated_at", "sourceKinds":
                                                           ["cli", "vscode", "exec", "appServer"]})
        aliases = await asyncio.to_thread(self.aliases.load)
        reverse = {v["thread"]: k for k, v in aliases.items() if isinstance(v, dict)
                   and same_project(v.get("project"), project) and isinstance(v.get("thread"), str)}
        live, items = self.live(), []
        for thread in data.get("data", []):
            tid = thread.get("id")
            if not isinstance(tid, str) or not limits.CHAT_ID.fullmatch(tid) or not same_project(thread.get("cwd"), project):
                continue
            chat = reverse.get(tid, tid)
            items.append({"chat": chat, "engine": "codex", "title": (thread.get("name") or thread.get("preview") or "")[:200],
                          "created": thread.get("createdAt"), "modified": thread.get("updatedAt"),
                          "tag": "studio" if tid in reverse else None, "running": chat in live,
                          "asking": bool(live.get(chat))})
        return items

    async def history(self, project, chat, before, limit):
        target = await asyncio.to_thread(self.aliases.target, project, chat)
        key = (str(project.resolve()), target)
        path = self._history_paths.get(key)
        def revision(path):
            if not isinstance(path, str):
                return None
            try:
                source = Path(path).resolve()
                root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
                if not source.is_relative_to(root):
                    return None
                stat = source.stat()
                return stat.st_mtime_ns, stat.st_size
            except OSError:
                return None
        previous = await asyncio.to_thread(revision, path)
        cached = self._histories.get(key, previous)
        if cached is not None:
            total = len(cached)
            end = min(before, total) if before is not None else total
            page = await asyncio.to_thread(sessions.fit, cached[max(0, end - limit):end], budget=512 * 1024)
            return {"items": page, "start": end - len(page), "total": total, "engine": "codex"}
        started = time.time_ns()
        try:
            data = await self._metadata(project, "thread/read", {"threadId": target, "includeTurns": True})
        except codex_rpc.RpcError as e:
            if e.not_found:
                raise Refused("not_found", "нет такого чата") from None
            raise
        thread = data.get("thread", {})
        if not same_project(thread.get("cwd"), project):
            raise Refused("not_found")
        items = await asyncio.to_thread(normalize, thread.get("turns", []))
        path = thread.get("path")
        current = await asyncio.to_thread(revision, path)
        self._history_paths[key] = path
        while len(self._history_paths) > 32:
            self._history_paths.pop(next(iter(self._history_paths)))
        if previous is None and current and current[0] <= started:
            previous = current
        self._histories.put(key, previous, current, items)
        total = len(items)
        end = min(before, total) if before is not None else total
        page = await asyncio.to_thread(sessions.fit, items[max(0, end - limit):end], budget=512 * 1024)
        return {"items": page, "start": end - len(page), "total": total, "engine": "codex"}

    async def fork_chat(self, project, chat, before=None):
        target = await asyncio.to_thread(self.aliases.target, project, chat)
        source = (await self._metadata(project, "thread/read", {"threadId": target, "includeTurns": True})).get("thread", {})
        if not same_project(source.get("cwd"), project):
            raise Refused("not_found")
        turns = source.get("turns", [])
        if any(turn.get("status") == "inProgress" for turn in turns):
            raise Refused("running", "Дождитесь завершения исходной сессии")
        draft, last_turn = "", None
        if before is not None:
            selected = next(((index, item_index, item) for index, turn in enumerate(turns)
                             for item_index, item in enumerate(turn.get("items", []))
                             if item.get("type") == "userMessage" and item.get("id") == before), None)
            if selected is None:
                raise Refused("invalid", "Точка ветвления отсутствует в истории")
            index, item_index, item = selected
            if item_index:
                raise Refused("unsupported", "Codex поддерживает ветвление по началу хода; выберите его первое сообщение")
            draft = "\n".join(block.get("text", "") for block in item.get("content", []) if block.get("type") == "text")[:100_000]
            if index == 0:
                return {"chat": str(uuid.uuid4()), "draft": draft}
            last_turn = turns[index - 1]["id"]
        result = await self._metadata(project, "thread/fork", {"threadId": target, "cwd": str(project),
                                      **({"lastTurnId": last_turn} if last_turn is not None else {})})
        fork = result.get("thread", {})
        tid = fork.get("id")
        try:
            if not isinstance(tid, str) or not limits.CHAT_ID.fullmatch(tid) or tid == target or not same_project(fork.get("cwd"), project):
                raise Refused("unsupported", "Codex не создал независимую ветку")
            # Старый CLI может проигнорировать неизвестный lastTurnId: проверяем реальный результат.
            if last_turn is not None:
                copied = (await self._metadata(project, "thread/read", {"threadId": tid, "includeTurns": True})).get("thread", {})
                if [turn.get("id") for turn in copied.get("turns", [])] != [turn.get("id") for turn in turns[:index]]:
                    raise Refused("unsupported", "Обновите Codex: ветвление до выбранного сообщения не поддерживается")
            chat_id = str(uuid.uuid4())
            await asyncio.to_thread(self.aliases.put, project, chat_id, tid)
            return {"chat": chat_id, "draft": draft}
        finally:
            if isinstance(tid, str) and tid != target:
                with contextlib.suppress(Exception):
                    await self._metadata(project, "thread/unsubscribe", {"threadId": tid})

    async def subagents(self, project, chat):
        target = await asyncio.to_thread(self.aliases.target, project, chat)
        source = (await self._metadata(project, "thread/read", {"threadId": target, "includeTurns": True})).get("thread", {})
        if not same_project(source.get("cwd"), project):
            raise Refused("not_found")
        children = codex_progress.children([item for turn in source.get("turns", []) for item in turn.get("items", [])])
        return {"items": [row for row in children if row["id"] != target]}

    async def subagent_history(self, project, chat, child):
        if child not in {row["id"] for row in (await self.subagents(project, chat))["items"]}:
            raise Refused("not_found", "Подзадача не принадлежит этому чату")
        data = (await self._metadata(project, "thread/read", {"threadId": child, "includeTurns": True})).get("thread", {})
        if not same_project(data.get("cwd"), project):
            raise Refused("not_found")
        items = await asyncio.to_thread(normalize, data.get("turns", []))
        page = await asyncio.to_thread(sessions.fit, items)
        return {"items": page, "complete": len(page) == len(items)}

    async def state(self, project, chat, model=None, effort=None, access=None):
        saved = await asyncio.to_thread(self.choices.details, chat)
        model, effort, access = model or saved.get("model"), effort or saved.get("effort"), access or saved.get("access")
        target = await asyncio.to_thread(self.aliases.target, project, chat)
        try:
            thread = (await self._metadata(project, "thread/read", {"threadId": target}))["thread"]
        except codex_rpc.RpcError as e:
            if not e.not_found:
                raise
            thread = None
        params = {"cwd": str(project)}
        if model:
            params["model"] = model
        params.update(permissions.codex_start(access))
        if thread is not None:
            if not same_project(thread.get("cwd"), project):
                raise Refused("not_found")
            # resume захватывает writer исходной сессии. Для чтения настроек нужен только
            # временный снимок; он работает и пока настоящий ход пишет исходную сессию.
            data = await self._metadata(project, "thread/fork", {
                "threadId": target, "ephemeral": True, "excludeTurns": True, **params})
        else:
            data = await self._metadata(project, "thread/start", {"ephemeral": True, **params})
        temporary = data.get("thread", {})
        tid = temporary.get("id")
        if temporary.get("ephemeral") is not True or not isinstance(tid, str) or tid == target:
            raise Refused("unsupported", "обновите Codex: недоступно безопасное чтение состояния чата")
        try:
            actual_model = data.get("model") or model
            default_effort = self.default_efforts.get(actual_model)
            if default_effort is None:
                default_effort = next((m.get("defaultReasoningEffort") for m in
                    (await self._metadata(project, "model/list", {"limit": 50})).get("data", [])
                    if m.get("model") == actual_model), None)
            context = await asyncio.to_thread(last_context, (thread or {}).get("path"))
            return {"settings": {"model": actual_model,
                        "effort": effort or data.get("reasoningEffort") or default_effort,
                        "access": permissions.codex_value(data.get("sandbox")) or access,
                        "approval": data.get("approvalPolicy")}, "context": context or saved.get("context")}
        finally:
            with contextlib.suppress(codex_rpc.RpcError):
                await self._metadata(project, "thread/unsubscribe", {"threadId": tid})

    async def _run(self, t, text, model, effort, images):
        self._activity(t, "thinking")
        failure, cancelled, requests = None, False, set()
        try:
            text = await self._attachment_text(t, text, t.attachments)
            t.attachments = []
            path = await asyncio.to_thread(self.find_codex)
            if path is None:
                raise Refused("unsupported", "Codex не найден на компьютере")
            target = await asyncio.to_thread(self.aliases.target, t.project, t.chat)
            saved_model, saved_effort = await asyncio.to_thread(self.choices.get, t.chat)
            saved = await asyncio.to_thread(self.choices.details, t.chat)
            t.access = t.access or saved.get("access")
            t.context = saved.get("context")
            model, effort = model or saved_model, effort or saved_effort
            rpc = self.rpc_factory(path, t.project)
            client = t.client = _TurnClient(rpc)
            await rpc.connect()
            if t.stopped or t.closing:
                return
            params = {"cwd": str(t.project), **permissions.codex_start(t.access)}
            if model is not None:
                params["model"] = model
            # Новая сессия студии не существует в Codex. Для внешней сессии ошибка чтения не создаёт новый чат.
            known = target != t.chat or t.chat in await asyncio.to_thread(self.aliases.load)
            if not known:
                try:
                    data = await rpc.request("thread/read", {"threadId": target})
                    known = True
                except codex_rpc.RpcError as e:
                    if not e.not_found:
                        raise
                    data = None
            if known:
                data = await rpc.request("thread/read", {"threadId": target})
                if not same_project(data.get("thread", {}).get("cwd"), t.project):
                    raise Refused("not_found")
                data = await rpc.request("thread/resume", {"threadId": target, **params})
            else:
                data = await rpc.request("thread/start", params)
            thread = data["thread"]
            target = thread["id"]
            if not limits.CHAT_ID.fullmatch(target) or not same_project(thread.get("cwd"), t.project):
                raise Refused("not_found")
            client.thread = target
            actual_model = data.get("model") or model
            t.settings = {"model": actual_model,
                          "effort": effort or data.get("reasoningEffort") or self.default_efforts.get(actual_model),
                          "access": permissions.codex_value(data.get("sandbox")) or t.access,
                          "approval": data.get("approvalPolicy")}
            self._send(t, {"k": "settings", **t.settings})
            if not known:
                await asyncio.to_thread(self.aliases.put, t.project, t.chat, target)
            if t.stopped or t.closing:
                return
            await asyncio.to_thread(self.choices.put, t.chat, model, effort)
            if t.access is not None:
                await asyncio.to_thread(self.choices.update, t.chat, access=t.access)
            content = [{"type": "image", "url": f"data:{i['mime']};base64,{i['data']}"} for i in images]
            if text.strip():
                content.append({"type": "text", "text": text})
            params = {"threadId": target, "input": content, **permissions.codex_turn(t.access)}
            if model is not None:
                params["model"] = model
            if effort is not None:
                params["effort"] = effort
            t.start_attempted = True
            data = await rpc.request("turn/start", params)
            client.turn = data["turn"]["id"]
            self._accepted(t)
            if t.stopped or t.closing:
                await client.interrupt()
            while True:
                frame = await rpc.inbox.get()
                if frame is None:
                    raise codex_rpc.RpcError()
                p = frame.get("params", {})
                if "id" in frame:
                    # Принадлежность потока — на момент прихода запроса, а не когда до него дойдёт задача.
                    frame["family"] = p.get("threadId") == target or p.get("threadId") in t.subtasks
                    task = asyncio.create_task(self._request(t, frame))
                    requests.add(task)
                    task.add_done_callback(requests.discard)
                    continue
                if self._family(t, frame.get("method"), p) or p.get("threadId") != target:
                    continue
                if frame.get("method") == "turn/completed":
                    if p.get("turn", {}).get("id") != client.turn:
                        continue
                    if p.get("turn", {}).get("status") == "completed":
                        await self._wait_queue(t)
                    async with t.input_lock:
                        result = p.get("turn", {})
                        status = result.get("status")
                        if status == "interrupted":
                            t.stopped = True
                        elif status == "failed":
                            failure = "Codex: " + events.clip((result.get("error") or {}).get("message", "ошибка хода"), 280)
                        elif status != "completed":
                            failure = "Codex: неизвестное состояние завершения хода"
                        if t.queued and result.get("status") == "completed" and not (t.stopped or t.closing):
                            message_id, extra_text, extra_images = t.queued[0]
                            content = [{"type": "text", "text": extra_text}] if extra_text.strip() else []
                            content += [{"type": "image", "url": f"data:{i['mime']};base64,{i['data']}"} for i in extra_images]
                            next_turn = await rpc.request("turn/start", {**params, "input": content})
                            client.turn = next_turn["turn"]["id"]
                            self._queued_sent(t, message_id)
                            continue
                        t.finishing = True
                    break
                self._notification(t, frame["method"], p)
        except asyncio.CancelledError:
            cancelled = True
        except Refused as e:
            failure = e.message
        except codex_rpc.RpcError as e:
            log.warning("ход %s: Codex RPC %s: %s", t.turn, e.method or "?", e.code)
            t.writer_active = e.writer_active
            failure = ("Этот чат занят другим процессом Codex — завершите его работу с чатом и повторите отправку"
                       if e.writer_active else "Codex не ответил — проверьте вход и журнал Codex на компьютере")
        except Exception as e:
            log.warning("ход %s: Codex: %s", t.turn, type(e).__name__)
            failure = "Codex не ответил — проверьте вход и журнал Codex на компьютере"
        finally:
            for task in list(requests):
                task.cancel()
            await asyncio.gather(*requests, return_exceptions=True)
            await self._finish(t, False, failure)
        if cancelled:
            raise asyncio.CancelledError

    def _notification(self, t, method, p):
        if method == "thread/tokenUsage/updated":
            usage = p.get("tokenUsage") or {}
            current = permissions.context((usage.get("last") or {}).get("totalTokens"), usage.get("modelContextWindow"),
                                          permissions.token_usage(usage.get("total")))
            if current:
                previous = {key: value for key, value in (t.context or {}).items() if key != "cache"}
                if current != previous:
                    current["cache"] = {"updated_at": self.now(), "model": (t.settings or {}).get("model")}
                elif t.context.get("cache"):
                    current["cache"] = t.context["cache"]
                t.context = current
                self._send(t, {"k": "context", **t.context})
        elif method == "item/agentMessage/delta":
            delta = p.get("delta", "")
            self._activity(t, "responding")
            t.feed.text += delta
            self._send(t, {"k": "text", "text": delta, **({"block": p["itemId"]} if p.get("itemId") else {}),
                           **({"phase": t.message_phase} if getattr(t, "message_phase", None) else {})})
        elif method in ("turn/diff/updated", "turn/plan/updated"):
            if method == "turn/diff/updated" and isinstance(p.get("diff"), str):
                update = {"changes": codex_progress.patch_summary(p["diff"])}
            elif method == "turn/plan/updated" and isinstance(p.get("plan"), list):
                update = {"plan": codex_progress.plan(p["plan"])}
            else:
                return
            t.progress.update(update)
            self._send(t, {"k": "progress", **update})
        elif method in ("item/commandExecution/outputDelta", "item/mcpToolCall/progress",
                        "item/commandExecution/terminalInteraction"):
            step = next((s for s in t.feed.steps if s["id"] == p.get("itemId")), None)
            if step is None:
                return
            if method.endswith("outputDelta") and isinstance(p.get("delta"), str):
                output = step["text"] + p["delta"]
                step["text"] = output[-limits.CLIP:]
                step["output_truncated"] = len(output) > limits.CLIP or step.get("output_truncated", False)
                self._send(t, {"k": "step_update", "id": step["id"], "text": step["text"],
                               "output_truncated": step["output_truncated"]})
            elif method.endswith("terminalInteraction"):
                step["process_id"] = str(p.get("processId", ""))[:200]
                self._send(t, {"k": "step_update", "id": step["id"], "process_id": step["process_id"]})
            elif isinstance(p.get("message"), str):
                step["progress_message"] = p["message"][:500]
                self._send(t, {"k": "step_update", "id": step["id"], "progress_message": step["progress_message"]})
        elif method in ("item/reasoning/summaryTextDelta", "item/reasoning/textDelta"):
            self._activity(t, "thinking")
        elif method in ("item/started", "item/completed"):
            item = p.get("item", {})
            if item.get("type") in ("collabAgentToolCall", "subAgentActivity"):
                t.progress["agents"] = codex_progress.children([item], t.progress.get("agents", []))
                self._send(t, {"k": "progress", "agents": t.progress["agents"]})
            if item.get("type") == "reasoning":
                self._activity(t, "thinking")
                return
            if item.get("type") == "agentMessage":
                if method == "item/started":
                    t.message_phase = item.get("phase")
                    self._activity(t, "responding")
                if method == "item/started" and t.feed.text and not t.feed.text.endswith("\n"):
                    t.feed.text += "\n\n"
                    self._send(t, {"k": "text", "text": "\n\n", "block": item.get("id"),
                                   **({"phase": item["phase"]} if item.get("phase") else {})})
                return
            step = step_of(item)
            if step is None:
                return
            old = next((s for s in t.feed.steps if s["id"] == step["id"]), None)
            if old is not None and item.get("type") == "commandExecution" and item.get("aggregatedOutput") is None:
                step["text"] = old["text"]
                if old.get("output_truncated"):
                    step["output_truncated"] = True
            if old is None:
                t.feed.steps.append(step)
                self._send(t, {"k": "step", **{k: v for k, v in step.items() if k not in ("text", "error")}})
            else:
                old.update(step)
            if method == "item/completed":
                if old is not None:
                    old.pop("progress_message", None)
                self._send(t, {"k": "result", **step})
                self._activity(t, "thinking")
            else:
                self._activity(t, "tool", step["tool"])
        elif method == "serverRequest/resolved":
            for rid, ask in list(t.asks.items()):
                if ask.data["frame"]["id"] == p.get("requestId"):
                    self._settle(t, rid, None)

    def _family(self, t, method, p):
        """Субагенты этого хода: потоки, порождённые им (`collabAgentToolCall.receiverThreadIds` от потока
        хода или уже известного субагента, `thread/started` с `parentThreadId` из них же). Их запросы
        получают карточку; события самих субагентов — только статусом в сводке. True — событие субагента."""
        family = {t.client.thread, *t.subtasks} - {None}
        if method == "thread/started":
            thread = p.get("thread")
            if isinstance(thread, dict):
                parent, name = spawned(thread)
                if parent in family:
                    self._adopt(t, thread.get("id"), name)
            return False
        sender = p.get("threadId")
        if sender not in family:
            return False
        item = p.get("item") if isinstance(p.get("item"), dict) else {}
        if method in ("item/started", "item/completed") and item.get("type") == "collabAgentToolCall":
            for child in item.get("receiverThreadIds") or []:
                self._adopt(t, child, None)
        if sender == t.client.thread:
            return False
        rows = t.progress.get("agents", [])
        if method in ("item/started", "item/completed") and item.get("type") == "collabAgentToolCall":
            rows = codex_progress.children([item], rows)
        elif method in ("turn/started", "turn/completed"):
            status = "running" if method == "turn/started" else {
                "completed": "completed", "interrupted": "interrupted", "failed": "errored"}.get(
                (p.get("turn") or {}).get("status"), "unknown")
            rows = [{**row, "status": status} if row["id"] == sender else row for row in rows]
        elif method == "serverRequest/resolved":
            self._notification(t, method, p)
        if rows != t.progress.get("agents", []):
            t.progress["agents"] = rows
            self._send(t, {"k": "progress", "agents": rows})
        return True

    def _adopt(self, t, child, name):
        if (not isinstance(child, str) or not limits.CHAT_ID.fullmatch(child) or child == t.client.thread
                or child not in t.subtasks and len(t.subtasks) >= codex_progress.AGENTS_MAX):
            return
        t.subtasks[child] = name or t.subtasks.get(child)
        if name:
            rows = t.progress.get("agents", [])
            if not any(row["id"] == child for row in rows):
                rows = [*rows, {"id": child, "title": name, "status": "running"}][:codex_progress.AGENTS_MAX]
            else:
                rows = [{**row, "title": name} if row["id"] == child else row for row in rows]
            if rows != t.progress.get("agents", []):
                t.progress["agents"] = rows
                self._send(t, {"k": "progress", "agents": rows})

    @staticmethod
    def _subtask_name(t, child):
        row = next((row for row in t.progress.get("agents", []) if row["id"] == child), {})
        return t.subtasks.get(child) or row.get("title") or f"Агент {child[:8]}"

    async def _request(self, t, frame):
        try:
            await self._request_card(t, frame)
        except Exception as e:
            log.warning("ход %s: запрос Codex: %s", t.turn, type(e).__name__)
            try:
                await t.client.rpc.send({"id": frame["id"],
                                         "error": {"code": -32603, "message": "Request failed"}})
            except Exception:
                pass

    async def _request_card(self, t, frame):
        method, p = frame["method"], frame.get("params", {})
        thread = p.get("threadId")
        # Чужой поток — отказ; субагент этого хода — та же карточка с его именем, ответ — тем же id запроса.
        if not frame.get("family", True) or thread != t.client.thread and (
                not isinstance(thread, str) or thread not in t.subtasks):
            await t.client.rpc.send({"id": frame["id"], "error": {"code": -32601, "message": "Unsupported request"}})
            return
        subtask = self._subtask_name(t, thread) if thread != t.client.thread else None
        rid = secrets.token_hex(8)
        if method == "item/tool/requestUserInput":
            rows = p.get("questions")
            if not isinstance(rows, list) or not rows or any(
                    not isinstance(q, dict) or not isinstance(q.get("id"), str) or not 0 < len(q["id"]) <= 200
                    or not isinstance(q.get("question"), str) or not q["question"] for q in rows) or \
                    len({q["id"] for q in rows}) != len(rows):
                await t.client.rpc.send({"id": frame["id"],
                    "error": {"code": -32602, "message": "Invalid questions"}})
                return
            questions = [{"id": q["id"], "question": q["question"], "header": q.get("header"), "multi": False,
                          "options": q.get("options") or []} for q in rows]
            event = events.bound({"k": "ask", "request_id": rid, "kind": "question", "questions": questions})
        elif method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                        "item/permissions/requestApproval"):
            data = {k: v for k, v in p.items() if k not in ("threadId", "turnId", "itemId")}
            if method == "item/fileChange/requestApproval":
                step = next((s for s in t.feed.steps if s["id"] == p.get("itemId")), None)
                if step:
                    data.update(step["input"])
            allowed = p.get("availableDecisions")
            always = method != "item/permissions/requestApproval" and (allowed is None or "acceptForSession" in allowed)
            event = events.ask_event(rid, {"item/commandExecution/requestApproval": "Shell",
                                          "item/fileChange/requestApproval": "Edit",
                                          "item/permissions/requestApproval": "Permissions"}[method], data, always)
        else:
            await t.client.rpc.send({"id": frame["id"], "error": {"code": -32601, "message": "Unsupported request"}})
            return
        if subtask:
            event["subtask"] = events.clip(subtask, 200)
        ask = _Ask(event, asyncio.get_running_loop().create_future(), {"frame": frame}, [])
        t.asks[rid] = ask
        self._activity(t, "waiting")
        self._send(t, event)
        try:
            result = await ask.future
            if result is not None:
                await t.client.rpc.send({"id": frame["id"], "result": result})
        finally:
            if t.asks.get(rid) is ask:
                del t.asks[rid]
                self._send(t, {"k": "asked", "request_id": rid})
                if not (t.stopped or t.closing or t.finishing):
                    self._activity(t, "thinking")

    def answer(self, pid, chat, request_id, decision, message, answers):
        if decision not in ("allow", "always", "deny"):
            raise Refused("invalid")
        t = self._turn(pid, chat)
        ask = t.asks.get(request_id) if t is not None else None
        if ask is None:
            raise Refused("gone")
        frame = ask.data["frame"]
        if ask.event["kind"] == "question":
            if decision != "allow" or not isinstance(answers, dict):
                raise Refused("invalid")
            result = {}
            questions = frame["params"]["questions"]
            for q in questions:
                value = answers.get(q["id"])
                if value is None and sum(row["question"] == q["question"] for row in questions) == 1:
                    value = answers.get(q["question"])  # страницы прежней версии
                if not isinstance(value, str) or not value or len(value) > 2000:
                    raise Refused("invalid")
                result[q["id"]] = {"answers": [value]}
            result = {"answers": result}
        elif answers is not None or (decision == "always" and not ask.event.get("always")):
            raise Refused("invalid")
        elif frame["method"] == "item/permissions/requestApproval":
            result = {"permissions": frame["params"]["permissions"] if decision == "allow" else {}, "scope": "turn"}
        else:
            result = {"decision": {"allow": "accept", "always": "acceptForSession", "deny": "decline"}[decision]}
        self._settle(t, request_id, result)

    def _deny_all(self, t):
        for rid, ask in list(t.asks.items()):
            method = ask.data["frame"]["method"]
            result = ({"permissions": {}, "scope": "turn"} if method == "item/permissions/requestApproval"
                      else {"answers": {}} if method == "item/tool/requestUserInput" else {"decision": "cancel"})
            self._settle(t, rid, result)

    async def close(self, final=False, hold=False):
        await super().close(final=final, hold=hold)
        self._histories.clear()
        self._history_paths.clear()
        async with self._meta_lock:
            if self._meta is not None:
                await self._meta.disconnect()
                self._meta = None
        async with self._usage_lock:
            if self._usage_meta is not None:
                await self._usage_meta.disconnect()
                self._usage_meta = None
