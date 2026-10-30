"""Codex в мосте: каталог app-server, история и ходы с теми же карточками и снимками, что у Claude Code."""

import asyncio
import json
import logging
import secrets
import threading
from pathlib import Path

from . import codex_rpc, events, limits, sessions, store
from .errors import Refused
from .turns import Runner, _Ask

log = logging.getLogger(__name__)
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")


def same_project(left, right) -> bool:
    return isinstance(left, str) and str(Path(left).resolve()).casefold() == str(Path(right).resolve()).casefold()


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


def step_of(item):
    kind = item.get("type")
    if kind == "commandExecution":
        tool, data, text = "Shell", {"command": item.get("command"), "cwd": item.get("cwd")}, item.get("aggregatedOutput", "")
    elif kind == "fileChange":
        tool, data, text = "Edit", {"changes": item.get("changes", [])}, ""
    elif kind in ("mcpToolCall", "dynamicToolCall"):
        tool, data = item.get("tool", kind), item.get("arguments", {})
        text = json.dumps(item.get("result") or item.get("contentItems") or item.get("error") or "", ensure_ascii=False)
    elif kind == "webSearch":
        tool, data, text = "WebSearch", {"query": item.get("query", "")}, ""
    elif kind == "collabAgentToolCall":
        tool, data, text = item.get("tool", "Agent"), {"prompt": item.get("prompt", "")}, ""
    else:
        return None
    return events.bound({"id": item["id"], "tool": tool, "input": events.clip(data),
                         "text": events.clip(text), "error": item.get("status") in ("failed", "declined")})


def normalize(turns):
    out = []
    for turn in turns:
        answer = None
        for item in turn.get("items", []):
            if item.get("type") == "userMessage":
                text = "\n".join(c.get("text", "[изображение]") for c in item.get("content", [])
                                 if isinstance(c, dict) and c.get("type") in ("text", "image", "localImage"))
                out.append({"id": item["id"], "role": "user", "text": text, "steps": [], "time": None})
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
                answer["steps"].append(step)
            if text:
                answer["text"] += ("\n\n" if answer["text"] else "") + text
    return out


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
    def __init__(self, emit, *, home, choices=None, find_cli=None, factory=codex_rpc.Client):
        self.find_codex = find_cli or codex_rpc.find_cli
        super().__init__(emit, choices=choices or store.Choices(home), cli_path=None)
        self.aliases = Aliases(home)
        self.rpc_factory = factory
        self.catalog = None
        self.models, self.efforts, self.version = [], [], ""
        self._meta = None
        self._meta_lock = asyncio.Lock()

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
            except BaseException:
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
            self.efforts = [e for e in EFFORTS if e in efforts]
            self.catalog = {"version": self.version, "models": self.models, "efforts": self.efforts}
            return bool(self.models)
        except Exception as e:
            log.warning("Codex: каталог: %s", type(e).__name__)
            return False

    async def chats(self, project):
        if self.catalog is None:
            return []
        data = await self._metadata(project, "thread/list", {"cwd": str(project), "limit": limits.CHATS_MAX,
                                                           "sortKey": "updated_at", "sourceKinds":
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
        total = len(items)
        end = min(before, total) if before is not None else total
        page = await asyncio.to_thread(sessions.fit, items[max(0, end - limit):end])
        return {"items": page, "start": end - len(page), "total": total, "engine": "codex"}

    async def _run(self, t, text, model, effort, images):
        failure, cancelled, requests = None, False, set()
        try:
            path = await asyncio.to_thread(self.find_codex)
            if path is None:
                raise Refused("unsupported", "Codex не найден на компьютере")
            target = await asyncio.to_thread(self.aliases.target, t.project, t.chat)
            saved_model, saved_effort = await asyncio.to_thread(self.choices.get, t.chat)
            model, effort = model or saved_model, effort or saved_effort
            rpc = self.rpc_factory(path, t.project)
            client = t.client = _TurnClient(rpc)
            await rpc.connect()
            if t.stopped or t.closing:
                return
            params = {"cwd": str(t.project)}
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
            if not known:
                await asyncio.to_thread(self.aliases.put, t.project, t.chat, target)
            if t.stopped or t.closing:
                return
            await asyncio.to_thread(self.choices.put, t.chat, model, effort)
            content = [{"type": "image", "url": f"data:{i['mime']};base64,{i['data']}"} for i in images]
            if text.strip():
                content.append({"type": "text", "text": text})
            params = {"threadId": target, "input": content}
            if model is not None:
                params["model"] = model
            if effort is not None:
                params["effort"] = effort
            data = await rpc.request("turn/start", params)
            client.turn = data["turn"]["id"]
            if t.stopped or t.closing:
                await client.interrupt()
            while True:
                frame = await rpc.inbox.get()
                if frame is None:
                    raise codex_rpc.RpcError()
                p = frame.get("params", {})
                if "id" in frame:
                    task = asyncio.create_task(self._request(t, frame))
                    requests.add(task)
                    task.add_done_callback(requests.discard)
                    continue
                if p.get("threadId") != target:
                    continue
                if frame.get("method") == "turn/completed":
                    result = p.get("turn", {})
                    if result.get("status") == "failed":
                        failure = "Codex: " + events.clip((result.get("error") or {}).get("message", "ошибка хода"), 280)
                    break
                self._notification(t, frame["method"], p)
        except asyncio.CancelledError:
            cancelled = True
        except Refused as e:
            failure = e.message
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
        if method == "item/agentMessage/delta":
            delta = p.get("delta", "")
            t.feed.text += delta
            self._send(t, {"k": "text", "text": delta})
        elif method in ("item/started", "item/completed"):
            item = p.get("item", {})
            if item.get("type") == "agentMessage":
                if method == "item/started" and t.feed.text and not t.feed.text.endswith("\n"):
                    t.feed.text += "\n\n"
                    self._send(t, {"k": "text", "text": "\n\n"})
                return
            step = step_of(item)
            if step is None:
                return
            old = next((s for s in t.feed.steps if s["id"] == step["id"]), None)
            if old is None:
                t.feed.steps.append(step)
                self._send(t, {"k": "step", **{k: v for k, v in step.items() if k not in ("text", "error")}})
            else:
                old.update(step)
            if method == "item/completed":
                self._send(t, {"k": "result", "id": step["id"], "text": step["text"], "error": step["error"]})
        elif method == "serverRequest/resolved":
            for rid, ask in list(t.asks.items()):
                if ask.data["frame"]["id"] == p.get("requestId"):
                    self._settle(t, rid, None)

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
        if p.get("threadId") != t.client.thread:
            await t.client.rpc.send({"id": frame["id"], "error": {"code": -32601, "message": "Unsupported request"}})
            return
        rid = secrets.token_hex(8)
        if method == "item/tool/requestUserInput":
            questions = [{"question": q["question"], "header": q.get("header"), "multi": False,
                          "options": q.get("options") or []} for q in p.get("questions", [])]
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
        ask = _Ask(event, asyncio.get_running_loop().create_future(), {"frame": frame}, [])
        t.asks[rid] = ask
        self._send(t, event)
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
            if decision != "allow" or not isinstance(answers, dict):
                raise Refused("invalid")
            result = {}
            for q in frame["params"].get("questions", []):
                value = answers.get(q["question"])
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
        async with self._meta_lock:
            if self._meta is not None:
                await self._meta.disconnect()
                self._meta = None
