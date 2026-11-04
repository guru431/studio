"""OpenCode в мосте: ACP-каталог, локальные сессии, поток и карточки студии."""

import asyncio
import contextlib
import json
import logging
import re
import secrets
import threading
import uuid
from datetime import datetime
from pathlib import Path

from . import diffs, events, limits, opencode_rpc, permissions, sessions, store
from .codex import same_project
from .errors import Refused
from .turns import Runner, _Ask

log = logging.getLogger(__name__)
SESSION_ID = re.compile(r"ses_[A-Za-z0-9_-]{1,200}")
MODEL_ID = re.compile(r"[A-Za-z0-9._:\[\]/@+\-]{1,300}")
EFFORT_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
NAMESPACE = uuid.UUID("9043b00c-91a9-485c-a679-91fa3607114e")


class Aliases:
    """UUID студии → непрозрачный ID OpenCode, проект и происхождение; без текстов."""

    def __init__(self, home):
        self.path = home / "opencode-chats.json"
        self.lock = threading.Lock()

    def load(self):
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(data, dict):
            raise ValueError("opencode-chats")
        return data

    def target(self, project, chat):
        item = self.load().get(chat)
        if item is None:
            return None
        target = item.get("session") if isinstance(item, dict) else None
        if not (isinstance(target, str) and SESSION_ID.fullmatch(target)
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


def settings(data, access=None):
    return {"model": options(data, "model").get("currentValue"),
            "effort": options(data, "effort").get("currentValue"), "access": access or "default"}


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


def normalize(updates):
    out, current, user_id = [], None, None
    for update in updates:
        kind = update.get("sessionUpdate")
        if kind == "user_message_chunk":
            mid = update.get("messageId")
            if current is None or current["role"] != "user" or mid != user_id:
                current = {"id": str(len(out)), "role": "user", "text": "", "steps": [], "time": None}
                out.append(current)
            user_id = mid
            current["text"] += text_of(update.get("content"))
        elif kind in ("agent_message_chunk", "tool_call", "tool_call_update"):
            if current is None or current["role"] != "assistant":
                current = {"id": str(len(out)), "role": "assistant", "text": "", "steps": [], "time": None}
                out.append(current)
            if kind == "agent_message_chunk":
                current["text"] += text_of(update.get("content"))
            elif isinstance(update.get("toolCallId"), str):
                old = next((s for s in current["steps"] if s["id"] == update["toolCallId"]), None)
                step = step_of(update, old)
                if old is None:
                    current["steps"].append(step)
                else:
                    old.update(step)
    return out


def timestamp(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        return None


class _TurnClient:
    def __init__(self, rpc):
        self.rpc, self.session = rpc, None

    async def interrupt(self):
        if self.session is not None:
            await self.rpc.send({"method": "session/cancel", "params": {"sessionId": self.session}})

    async def disconnect(self):
        await self.rpc.disconnect()


class OpenCodeRunner(Runner):
    async def _steer_input(self, t, text, images, message_id):
        return self._queue_input(t, text, images, message_id)

    def __init__(self, emit, *, home, choices=None, find_cli=None, factory=opencode_rpc.Client):
        (home / "opencode").mkdir(parents=True, exist_ok=True)
        super().__init__(emit, choices=choices or store.Choices(home / "opencode"), cli_path=None)
        self.find_opencode = find_cli or opencode_rpc.find_cli
        self.rpc_factory = factory
        self.aliases = Aliases(home)
        self.catalog = None
        self.models, self.efforts, self.version = [], [], ""
        self._catalog_rpc = None
        self._catalog_lock = asyncio.Lock()

    async def _connect(self, project):
        path = await asyncio.to_thread(self.find_opencode)
        if path is None:
            raise Refused("unsupported", "OpenCode не найден на компьютере")
        rpc = self.rpc_factory(path, Path(project))
        await rpc.connect()
        return rpc

    async def _temporary(self, rpc, project):
        return await rpc.request("session/new", {"cwd": str(project), "mcpServers": []})

    async def _delete(self, rpc, target):
        # Только пустая сессия, которую создал этот вызов; пользовательские сессии не удаляются.
        await rpc.request("session/delete", {"sessionId": target})

    async def probe(self, root):
        rpc, temporary, ready, catalog = None, None, False, None
        try:
            rpc = await self._connect(root)
            self.version = str(rpc.info.get("agentInfo", {}).get("version") or "OpenCode")[:64]
            data = await self._temporary(rpc, root)
            temporary = data["sessionId"]
            models = [{"id": row["value"], "label": str(row.get("name") or row["value"])[:100]}
                      for row in values(options(data, "model")) if MODEL_ID.fullmatch(row["value"])][:500]
            efforts = {row["value"] for row in values(options(data, "effort")) if EFFORT_ID.fullmatch(row["value"])}
            native = settings(data)
            for model in models:
                if model["id"] == native["model"]:
                    continue
                changed = await rpc.request("session/set_config_option", {
                    "sessionId": temporary, "configId": "model", "value": model["id"]})
                efforts.update(row["value"] for row in values(options(changed, "effort"))
                               if EFFORT_ID.fullmatch(row["value"]))
            catalog = {"version": self.version, "models": models, "efforts": sorted(efforts)[:100], "defaults": native}
            ready = bool(models)
        except Exception as e:
            log.warning("OpenCode: каталог: %s", type(e).__name__)
        finally:
            if rpc is not None:
                try:
                    if temporary is not None:
                        await asyncio.wait_for(self._delete(rpc, temporary), 5)
                except Exception as e:
                    ready = False
                    log.warning("OpenCode: закрытие пробы: %s", type(e).__name__)
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
            except BaseException:
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
            if not (isinstance(target, str) and SESSION_ID.fullmatch(target) and same_project(row.get("cwd"), project)):
                continue
            chat = reverse.get(target) or str(uuid.uuid5(NAMESPACE, target))
            if target not in reverse:
                added[chat] = {"project": str(project.resolve()), "session": target, "studio": False}
            items.append({"chat": chat, "engine": "opencode", "title": str(row.get("title") or "")[:200],
                          "created": timestamp(row.get("createdAt")), "modified": timestamp(row.get("updatedAt")),
                          "tag": "studio" if saved.get(chat, {}).get("studio") else None,
                          "running": chat in live, "asking": bool(live.get(chat))})
        if added:
            await asyncio.to_thread(self.aliases.merge, added)
        return items

    async def close(self, final=False, hold=False):
        await super().close(final=final, hold=hold)
        async with self._catalog_lock:
            if self._catalog_rpc is not None:
                await self._catalog_rpc.disconnect()
                self._catalog_rpc = None

    async def _load(self, rpc, project, target):
        try:
            return await rpc.request("session/load", {"sessionId": target, "cwd": str(project), "mcpServers": []})
        except opencode_rpc.RpcError as e:
            if e.not_found:
                raise Refused("not_found", "нет такого чата") from None
            raise

    @staticmethod
    def _updates(rpc, target):
        updates = []
        while not rpc.inbox.empty():
            frame = rpc.inbox.get_nowait()
            if frame and frame.get("method") == "session/update" and frame.get("params", {}).get("sessionId") == target:
                updates.append(frame["params"].get("update", {}))
        return updates

    async def history(self, project, chat, before, limit):
        target = await asyncio.to_thread(self.aliases.target, project, chat)
        if target is None:
            raise Refused("not_found", "нет такого чата")
        rpc = await self._connect(project)
        try:
            await self._load(rpc, project, target)
            items = await asyncio.to_thread(normalize, self._updates(rpc, target))
        finally:
            await rpc.disconnect()
        total = len(items)
        end = min(before, total) if before is not None else total
        page = await asyncio.to_thread(sessions.fit, items[max(0, end - limit):end], budget=512 * 1024)
        return {"items": page, "start": end - len(page), "total": total, "engine": "opencode"}

    async def _configure(self, rpc, target, data, model, effort):
        for key, value in (("model", model), ("effort", effort)):
            if value is None or options(data, key).get("currentValue") == value:
                continue
            if value not in {row["value"] for row in values(options(data, key))}:
                raise Refused("invalid", "Выбранные настройки недоступны для этой модели OpenCode")
            data = await rpc.request("session/set_config_option", {"sessionId": target, "configId": key, "value": value})
        return data

    async def state(self, project, chat, model=None, effort=None, access=None):
        saved = await asyncio.to_thread(self.choices.details, chat)
        model, effort, access = model or saved.get("model"), effort or saved.get("effort"), access or saved.get("access")
        target = await asyncio.to_thread(self.aliases.target, project, chat)
        rpc, temporary = await self._connect(project), None
        current = saved.get("context")
        try:
            if target is not None:
                data = await self._load(rpc, project, target)
                for update in self._updates(rpc, target):
                    if update.get("sessionUpdate") == "usage_update":
                        current = permissions.context(update.get("used"), update.get("size")) or current
            else:
                data = await self._temporary(rpc, project)
                temporary = data["sessionId"]
            if model or effort:
                if temporary is None:
                    # Предпросмотр выбора не меняет существующую сессию CLI.
                    previous = settings(data)
                    data = await self._temporary(rpc, project)
                    temporary = data["sessionId"]
                    model, effort = model or previous["model"], effort or previous["effort"]
                data = await self._configure(rpc, temporary, data, model, effort)
            return {"settings": settings(data, access), "context": current}
        finally:
            try:
                if temporary is not None:
                    await self._delete(rpc, temporary)
            finally:
                await rpc.disconnect()

    async def _run(self, t, text, model, effort, images):
        failure, cancelled, requests, prompt = None, False, set(), None
        try:
            text = await self._attachment_text(t, text, t.attachments)
            t.attachments = []
            saved = await asyncio.to_thread(self.choices.details, t.chat)
            model, effort = model or saved.get("model"), effort or saved.get("effort")
            t.access, t.context = t.access or saved.get("access"), saved.get("context")
            rpc = await self._connect(t.project)
            client = t.client = _TurnClient(rpc)
            target = await asyncio.to_thread(self.aliases.target, t.project, t.chat)
            if t.stopped or t.closing:
                return
            if target is None:
                data = await self._temporary(rpc, t.project)
                target = data["sessionId"]
                if not isinstance(target, str) or not SESSION_ID.fullmatch(target):
                    raise Refused("invalid")
                await asyncio.to_thread(self.aliases.put, t.project, t.chat, target)
            else:
                data = await self._load(rpc, t.project, target)
            client.session = target
            self._updates(rpc, target)  # replay — уже показанная история, в текущий ход не входит
            data = await self._configure(rpc, target, data, model, effort)
            t.settings = settings(data, t.access)
            self._send(t, {"k": "settings", **t.settings})
            await asyncio.to_thread(self.choices.update, t.chat, model=model, effort=effort, access=t.access)
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
                    if result.get("stopReason") == "cancelled":
                        t.stopped = True
                    if not t.stopped:
                        await self._wait_queue(t)
                    async with t.input_lock:
                        if not t.queued or t.stopped or t.closing:
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
                    raise opencode_rpc.RpcError()
                if "id" in frame:
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
            log.warning("ход %s: OpenCode: %s", t.turn, type(e).__name__)
            failure = "OpenCode не ответил — проверьте вход и журнал OpenCode на компьютере"
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
        elif kind == "usage_update":
            current = permissions.context(update.get("used"), update.get("size"))
            if current:
                t.context = current
                self._send(t, {"k": "context", **current})
        elif kind == "config_option_update":
            t.settings = settings(update, t.access)
            self._send(t, {"k": "settings", **t.settings})

    async def _request(self, t, frame):
        try:
            await self._request_card(t, frame)
        except Exception as e:
            log.warning("ход %s: запрос OpenCode: %s", t.turn, type(e).__name__)
            with contextlib.suppress(Exception):
                await t.client.rpc.send({"id": frame["id"], "error": {"code": -32603, "message": "Request failed"}})

    async def _request_card(self, t, frame):
        method, p = frame["method"], frame.get("params", {})
        if p.get("sessionId") != t.client.session:
            await t.client.rpc.send({"id": frame["id"], "error": {"code": -32601, "message": "Unsupported request"}})
            return
        rid = secrets.token_hex(8)
        if method == "session/request_permission":
            offered = p.get("options", [])
            if t.access == "auto" and (once := next((o for o in offered if o.get("kind") == "allow_once"), None)):
                await t.client.rpc.send({"id": frame["id"], "result": {"outcome": {
                    "outcome": "selected", "optionId": once["optionId"]}}})
                return
            tool = p.get("toolCall", {})
            always = any(o.get("kind") == "allow_always" for o in offered)
            event = events.ask_event(rid, tool.get("title", "OpenCode"), tool.get("rawInput") or tool, always)
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
