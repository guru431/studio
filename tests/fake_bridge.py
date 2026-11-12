"""Подставной мост Claude Code (протокол docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md):
привязка к учётке студии, машина по протоколу (§3–§7) и два способа подключить её к студии.

- `FakeMachine` — сам компьютер: проекты, чаты, файлы, git, PIN и открытые сессии; `handle` отвечает на
  просьбу кадром `res` и кадрами `ev` после него. Без службы — её сверяет `test_fake_machine_follows_the_protocol`.
- `InProcessBridge` — канал в процессе службы (быстрые тесты): кадры студии пишутся в `sent`, ответы машины
  приходят через `bridge.deliver` на цикле службы.
- `SocketBridge` — настоящий WebSocket к живой студии (браузерные тесты), в своём потоке.
"""
import asyncio
import base64
import hashlib
import json
import threading
import uuid

import bridge
import state

PIN_TRIES = 5                    # неверных PIN подряд — дальше pin_locked (§3)
OPEN_MAX = 2 * 1024 * 1024       # текст до 2 МиБ (§6)
RAW_MAX = 10 * 1024 * 1024
T0 = 1_800_000_000               # часы машины: свои, не настоящие


def enroll(s, login="owner", name="Рабочий ПК", password="password-123", ip="10.77.48.5") -> tuple[int, str]:
    """Привязка, как её делает мост: JSON без Origin, адрес — последним в X-Forwarded-For. (agent_id, device_key)."""
    r = s.c.post("/api/agent/enroll", json={"login": login, "password": password, "name": name},
                 headers={"X-Forwarded-For": ip})
    assert r.status_code == 201, r.text
    body = r.json()
    return body["agent_id"], body["device_key"]


def pid(name: str) -> str:
    """id проекта — первые 16 знаков hex sha256 имени папки (§5)."""
    return hashlib.sha256(name.encode()).hexdigest()[:16]


def _version(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Refused(Exception):
    def __init__(self, code: str, message: str = "", **extra):
        super().__init__(code)
        self.code, self.message, self.extra = code, message, extra


class FakeMachine:
    """Компьютер с мостом. `projects` — имя → {"files": {путь: bytes}, "chats": {chat: {"title", "created",
    "modified", "tag", "messages": [...]}}, "git": dict | None}; `script` — события следующего хода (без `t`,
    `project`, `chat`), `requests` — все просьбы по порядку."""

    def __init__(self, projects: dict | None = None, pin: str | None = None):
        self.projects = projects if projects is not None else {}
        self.pin = pin
        self.pin_fails = 0
        self.read_only: set[str] = set()         # пути файлов «только для чтения»: `save` — отказ read_only
        self.open_sessions: set[str] = set()
        self.requests: list[dict] = []
        self.script: list[dict] = []
        self.running: dict[str, dict] = {}       # chat → идущий ход
        self.now = T0
        self.turns = 0
        self.codex = None
        self.opencode = None
        self.images = True
        self.history_images = False
        self.chat_state = True
        self.chat_control = True
        self.attachments = False
        self.project_activity = False
        self.project_summary = False
        self.computer_info = False
        self.visualizations = False
        self.passive_requests = False            # фоновые просьбы (`passive: true`) срок PIN не продлевают (§3)
        self.computer_data = {}
        self.desktop = False
        self.desktop_enabled = False
        self.desktop_active = None
        self.desktop_owner = None
        self.desktop_clipboard = ''
        self.file_stats = False
        self.file_operations = False
        self.file_suggestions = False
        self.chat_search = False
        self.queue_edit = False
        self.chat_fork = False
        self.subagents = False
        self.worktrees = False
        self.subscription_usage = False
        self.usage_data = None
        self.agent_version = None
        self.agent_update = False
        self.update_state = 'idle'
        self.update_version = None
        self.update_holds = None                 # что держит ожидание обновления (мост 0.20.0)
        self.agent_update_cancel = False         # «Отменить обновление» (мост 0.20.0)
        self.context = None
        self.project_sharing = True
        self.completions = False                 # кадр досланных завершений (§18)
        self.visualization_scope = False         # участнику — только визуализации проекта (§17)

    @property
    def hello(self) -> dict:
        return {"t": "hello", "v": 1, "cc_version": "2.3.1", "projects_root": "C:\\work\\projects",
                "has_pin": self.pin is not None,
                "models": [{"id": "opus", "label": "Opus"}, {"id": "sonnet", "label": "Sonnet"}],
                "efforts": ["low", "high"], "images": self.images, "chat_state": self.chat_state,
                "chat_control": self.chat_control,
                **({'agent_version': self.agent_version} if self.agent_version else {}),
                **({'agent_update': True} if self.agent_update else {}),
                **{name: True for name in ("attachments", "project_activity", "subscription_usage", "project_summary", "computer_info", "visualizations", "desktop", "file_stats", "file_operations", "passive_requests",
                                           "file_suggestions", "chat_search", "queue_edit", "chat_fork", "subagents", "worktrees", "history_images", "project_sharing",
                                           "completions", "visualization_scope", "agent_update_cancel")
                   if getattr(self, name)},
                "defaults": {"model": "opus", "effort": "high", "access": "auto"},
                **{name: catalog for name, catalog in (("codex", self.codex), ("opencode", self.opencode))
                   if catalog is not None}}

    # ---- просьбы ----------------------------------------------------------------

    def handle(self, req: dict) -> tuple[dict, list[dict]]:
        self.requests.append(req)
        events: list[dict] = []
        try:
            op = req.get("op")
            if op == "open":
                data = self._open(req)
            else:
                scope = req.get("shared_projects")
                if scope is not None and op not in ("projects", "activity") and req.get("project") not in scope:
                    raise Refused("closed")
                if scope is None and req.get("session") not in self.open_sessions:
                    raise Refused("closed", "компьютер закрыт")
                handler = getattr(self, f"_op_{op}", None) if isinstance(op, str) else None
                if handler is None:
                    raise Refused("invalid", "неизвестная просьба")
                data = handler(req, events)
                if scope is not None and op in ("projects", "activity"):
                    key = "pid" if op == "projects" else "project"
                    data["items"] = [i for i in data.get("items", []) if i.get(key) in scope]
        except Refused as e:
            return {"t": "res", "id": req.get("id"), "ok": False, "code": e.code, "message": e.message,
                    **e.extra}, []
        return {"t": "res", "id": req.get("id"), "ok": True, "data": data}, events

    def _open(self, req):
        if self.pin is None:
            self.open_sessions.add(req["session"])
            return {}
        if req.get("pin") is None and req.get("session") in self.open_sessions:
            return {}                            # уже открыта — продление без PIN (протокол §3)
        if self.pin_fails >= PIN_TRIES:
            raise Refused("pin_locked", "PIN заблокирован")
        if req.get("pin") is None:
            raise Refused("pin_needed", "нужен PIN")
        if req["pin"] == self.pin:
            self.pin_fails = 0
            self.open_sessions.add(req["session"])
            return {}
        self.pin_fails += 1
        if self.pin_fails >= PIN_TRIES:
            raise Refused("pin_locked", "PIN заблокирован")
        raise Refused("pin", "неверный PIN", left=PIN_TRIES - self.pin_fails)

    def _op_agent_update(self, req, events):
        self.update_state = 'preparing'
        self.update_version = req['version']
        return self._op_agent_update_status(req, events)

    def _op_computer_info(self, req, events):
        return self.computer_data

    def _op_desktop_status(self, req, events):
        return {'enabled': self.desktop_enabled, 'active': self.desktop_active is not None}

    def _op_desktop_start(self, req, events):
        if not self.desktop_enabled:
            raise Refused('desktop_disabled', 'Рабочий стол выключен. Включите его в агенте на компьютере')
        if self.desktop_active:
            raise Refused('busy', 'Рабочий стол уже открыт')
        self.desktop_active = uuid.uuid4().hex
        self.desktop_owner = req['session']
        return {'control': self.desktop_active, 'monitors': [{'id': 0, 'width': 160, 'height': 90, 'primary': True}],
                'monitor': 0}

    def _desktop_check(self, req):
        if not self.desktop_enabled:
            raise Refused('desktop_disabled', 'Рабочий стол выключен. Включите его в агенте на компьютере')
        if not self.desktop_active or req.get('control') != self.desktop_active or req['session'] != self.desktop_owner:
            raise Refused('owned', 'Сеанс принадлежит другому окну')

    def _op_desktop_frame(self, req, events):
        import io
        from PIL import Image
        self._desktop_check(req)
        with io.BytesIO() as buf:
            Image.new('RGB', (160, 90), '#366e93').save(buf, 'JPEG')
            image = base64.b64encode(buf.getvalue()).decode('ascii')
        return {'image': image, 'width': 160, 'height': 90, 'screen_width': 160, 'screen_height': 90, 'monitor': 0}

    def _op_desktop_input(self, req, events):
        self._desktop_check(req)
        return {}

    def _op_desktop_clipboard(self, req, events):
        self._desktop_check(req)
        if req.get('text') is not None:
            self.desktop_clipboard = req['text']
        return {'text': self.desktop_clipboard}

    def _op_desktop_stop(self, req, events):
        self._desktop_check(req)
        self.desktop_active = self.desktop_owner = None
        return {}

    def _op_agent_update_status(self, req, events):
        status = {'state': self.update_state, 'version': self.update_version}
        if self.update_state == 'waiting' and self.update_holds is not None:
            status['holds'] = self.update_holds
        return status

    def _op_agent_update_cancel(self, req, events):
        if self.update_state in ('preparing', 'waiting'):
            self.update_state, self.update_version = 'idle', None
        return self._op_agent_update_status(req, events)

    def _project(self, req) -> tuple[str, dict]:
        for name, project in self.projects.items():
            if pid(name) == req.get("project"):
                return name, project
        raise Refused("not_found", "нет такого проекта")

    def _chat(self, req) -> dict:
        _, project = self._project(req)
        chat = project.setdefault("chats", {}).get(req.get("chat"))
        if chat is None or chat.get("engine", "claude") != req.get("engine", "claude"):
            raise Refused("not_found", "нет такого чата")
        return chat

    def _op_projects(self, req, events):
        return {"items": [{"pid": pid(name), "name": name} for name in sorted(self.projects)]}

    def _op_project_summary(self, req, events):
        _, project = self._project(req)
        return project.get("summary", {"findings": 0, "ideas": 0, "changed": 0, "unpushed": 0,
                                       "modified": self.now, "checked_at": self.now, "git": "ok"})

    def _op_activity(self, req, events):
        return {"items": [{"project": turn["project"], "chat": chat, "running": True,
                           "asking": bool(turn.get("asks")), "engine": turn.get("engine", "claude")}
                          for chat, turn in self.running.items()]}

    def _op_usage(self, req, events):
        return self.usage_data

    def _op_chats(self, req, events):
        """Сессия с `"listed": False` в список не попадает (как старше 200 новейших на настоящем мосте), а история и
        ход у неё работают."""
        _, project = self._project(req)
        items = [{"chat": chat, "title": c.get("title"), "created": c.get("created"), "modified": c.get("modified"),
                  "tag": c.get("tag"), "engine": c.get("engine", "claude"), "running": chat in self.running,
                  "asking": chat in self.running and bool(self.running[chat]["asks"])}
                 for chat, c in project.get("chats", {}).items() if c.get("listed", True)]
        return {"items": sorted(items, key=lambda i: i["modified"] or 0, reverse=True)}

    def _op_file_suggestions(self, req, events):
        _, project = self._project(req)
        return {"items": [path for path in sorted(project.get("files", {})) if req["query"].casefold() in path.casefold()][:50],
                "complete": True, "more": False}

    def _op_chat_fork(self, req, events):
        _, project = self._project(req)
        source = self._chat(req)
        if req["chat"] in self.running:
            raise Refused("running")
        messages, draft = source["messages"], ""
        if req.get("message_id"):
            index = next(i for i, message in enumerate(messages) if message["id"] == req["message_id"])
            draft = messages[index]["text"]
            messages = messages[:index]
        child = str(uuid.uuid4())
        project["chats"][child] = {**source, "title": "Ветка чата", "messages": json.loads(json.dumps(messages)), "tag": "studio"}
        return {"chat": child, "engine": req.get("engine", "claude"), "draft": draft}

    def _op_subagents(self, req, events):
        row = self._chat(req)
        return {"items": [{"id": key, "title": value.get("title", key), "status": value.get("status", "completed"),
                           **{field: value[field] for field in ("started_at", "ended_at", "duration_ms", "total_tokens") if field in value}}
                          for key, value in row.get("children", {}).items()]}

    def _op_subagent_history(self, req, events):
        row = self._chat(req)
        child = row.get("children", {}).get(req["request_id"])
        if child is None:
            raise Refused("not_found")
        return {"items": child["messages"], "complete": True}

    def _op_worktrees(self, req, events):
        name, _ = self._project(req)
        return {"items": [{"pid": pid(name), "name": name, "branch": "main", "current": True}]}

    def _op_worktree_create(self, req, events):
        name, _ = self._project(req)
        folder = name + "--" + req["query"]
        self.projects[folder] = {"chats": {}, "files": {}, "git": {"branch": "studio/" + req["query"], "files": []}}
        return {"pid": pid(folder), "name": folder, "branch": "studio/" + req["query"]}

    def _op_chat_search(self, req, events):
        _, project = self._project(req)
        items = []
        query = req["query"].casefold()
        for chat, row in project.get("chats", {}).items():
            snippets = [{"text": message["text"][:200], "role": message["role"]}
                        for message in row.get("messages", []) if query in message.get("text", "").casefold()]
            if snippets or query in (row.get("title") or "").casefold():
                items.append({"chat": chat, "title": row.get("title") or "", "engine": row.get("engine", "claude"),
                              "snippets": snippets[:3]})
        return {"items": items, "complete": True}

    def _op_state(self, req, events):
        _, project = self._project(req)
        engine = req.get("engine", "claude")
        catalog = self.hello if engine == "claude" else getattr(self, engine) or {}
        saved = project.get("chats", {}).get(req["chat"], {}).get("settings", {})
        model = req.get("model") or (catalog.get("models") or [{"id": "codex-test"}])[0]["id"]
        return {"settings": {"model": req.get("model") or saved.get("model") or model,
                "effort": req.get("effort") or saved.get("effort") or "high",
                "access": req.get("access") or saved.get("access") or ("workspace-write" if engine == "codex" else "auto")},
                "context": self.context}

    def _op_history(self, req, events):
        messages = self._chat(req).get("messages", [])
        before, limit = req.get("before"), req.get("limit")
        if not (type(limit) is int and 1 <= limit <= 100) or not (before is None or type(before) is int):
            raise Refused("invalid", "неверные поля")
        end = len(messages) if before is None else max(0, min(before, len(messages)))
        start = max(0, end - limit)
        return {"items": messages[start:end], "start": start, "total": len(messages)}

    def _op_history_image(self, req, events):
        if not self.history_images:
            raise Refused("unsupported")
        data = self._chat(req).get("image_data", {}).get((req.get("message_id"), req.get("index")))
        if data is None:
            raise Refused("not_found")
        return data

    def _op_turn(self, req, events):
        _, project = self._project(req)
        chat_id = req.get("chat")
        if chat_id in self.running:
            raise Refused("running", "в чате уже идёт ход")
        chats = project.setdefault("chats", {})
        self.now += 1
        chat = chats.get(chat_id)
        if chat is None:             # новая сессия с session_id = chat и меткой studio
            chat = chats[chat_id] = {"title": req.get("text", "")[:120], "created": self.now, "modified": self.now,
                                     "tag": "studio", "messages": [], "engine": req.get("engine", "claude")}
        chat["messages"].append({"id": f"m-{len(chat['messages'])}", "role": "user", "text": req.get("text", ""),
                                 "steps": [], "time": self.now})
        chat["modified"] = self.now
        self.turns += 1
        turn = f"t-{self.turns}"
        self.running[chat_id] = {"turn": turn, "project": req["project"], "rest": list(self.script),
                                 "text": "", "steps": [], "asks": {}, "owner": req.get("window"), "followups": [],
                                 "timeline": [], "progress": {}}
        self.script = []
        if req.get("window"):
            events.append(self._ev(chat_id, {"k": "input_accepted"}))
        self._play(chat_id, events)
        return {"turn": turn}

    def _op_steer(self, req, events):
        self._chat(req)
        run = self.running.get(req["chat"])
        if run is None:
            raise Refused("idle", "хода нет")
        if run.get("owner") and run["owner"] != req.get("window"):
            raise Refused("owned", "задание выполняется в другом окне")
        mode = 'queue' if req.get('engine') == 'opencode' or req.get('mode') == 'queue' else 'steer'
        identifier = f"{len(run['followups']):016x}" if self.queue_edit else f"followup-{len(run['followups'])}"
        item = {"id": identifier, "text": req["text"], "mode": mode,
                "position": {"text": len(run.get("text", "").encode("utf-16-le", errors="surrogatepass")) // 2,
                             "steps": len(run.get("steps", []))},
                **({"version": 1, "input_text": req["text"]} if self.queue_edit and mode == 'queue' else {})}
        if req.get("images"):
            message = f"followup:{identifier}"
            item.update(image_message_id=message, images=[
                {"index": index, "name": f"Изображение {index + 1}"} for index, _ in enumerate(req["images"])])
            self._chat(req).setdefault("image_data", {}).update(
                {(message, index): image for index, image in enumerate(req["images"])})
        run["followups"].append(item)
        events.append(self._ev(req["chat"], {"k": "followup", **item}))
        return {"mode": mode, "id": item['id']}

    def _op_queue_edit(self, req, events):
        self._chat(req)
        run = self.running.get(req["chat"])
        if run is None:
            raise Refused("gone")
        if run.get("owner") and run["owner"] != req.get("window"):
            raise Refused("owned")
        item = next((item for item in run["followups"] if item["id"] == req["request_id"] and not item.get("sent")), None)
        if item is None:
            raise Refused("gone")
        if item["version"] != req["version"]:
            raise Refused("conflict")
        if req.get("text") is None:
            run["followups"].remove(item)
            events.append(self._ev(req["chat"], {"k": "followup_removed", "id": item["id"]}))
        else:
            item.update(text=req["text"], input_text=req["text"], version=item["version"] + 1)
        result = {"items": [item for item in run["followups"] if item["mode"] == "queue" and not item.get("sent")],
                  "paused": run.get("queue_paused", False)}
        events.append(self._ev(req["chat"], {"k": "queue", **result}))
        return result

    def _op_queue_pause(self, req, events):
        self._chat(req)
        run = self.running.get(req["chat"])
        if run is None:
            raise Refused("gone")
        if run.get("owner") and run["owner"] != req.get("window"):
            raise Refused("owned")
        run["queue_paused"] = req["paused"]
        result = {"items": [item for item in run["followups"] if item["mode"] == "queue" and not item.get("sent")],
                  "paused": req["paused"]}
        events.append(self._ev(req["chat"], {"k": "queue", **result}))
        return result

    def _op_claim(self, req, events):
        self._chat(req)
        run = self.running.get(req["chat"])
        if run is not None:
            run["owner"] = req["window"]
            events.append(self._ev(req["chat"], {"k": "ownership", "owner": req["window"]}))
        return {"available": True}

    def _op_watch(self, req, events):
        chat_id = req.get("chat")
        self._chat(req)
        run = self.running.get(chat_id)
        if run is not None:
            asks = [{k: v for k, v in a.items() if k != "k"} for a in run["asks"].values()]
            events.append(self._ev(chat_id, {"k": "snapshot", "text": run["text"],
                                             "steps": [dict(s) for s in run["steps"]], "asks": asks,
                                             **({"owner": run["owner"], "accepted": True} if run.get("owner") else {}),
                                             **({"followups": run["followups"]} if run.get("followups") else {}),
                                             **({"queue": [item for item in run["followups"] if item["mode"] == "queue" and not item.get("sent")]}
                                                if self.queue_edit else {}),
                                             **({"queue_paused": True} if run.get("queue_paused") else {}),
                                             **({"activity": run["activity"]} if run.get("activity") else {})}))
            events[-1]["e"].update({key: run[key] for key in ("timeline", "progress")
                                    if run.get(key) and (key != "timeline" or len(run[key]) > 1)})
        return {"running": run is not None}

    def _op_answer(self, req, events):
        chat_id = req.get("chat")
        self._chat(req)
        run = self.running.get(chat_id)
        if run is None or req.get("request_id") not in run["asks"]:
            raise Refused("gone", "запроса уже нет")
        if req.get("decision") not in ("allow", "always", "deny"):
            raise Refused("invalid", "неверные поля")
        del run["asks"][req["request_id"]]
        events.append(self._ev(chat_id, {"k": "asked", "request_id": req["request_id"]}))
        self._play(chat_id, events)
        return {}

    def _op_stop(self, req, events):
        chat_id = req.get("chat")
        self._chat(req)
        run = self.running.get(chat_id)
        if run is None:
            raise Refused("idle", "хода нет")
        for request_id in list(run["asks"]):
            events.append(self._ev(chat_id, {"k": "asked", "request_id": request_id}))
        unsent = [{"id": item['id'], "text": item['text']} for item in run.get('followups', []) if item['mode'] == 'queue']
        self._finish(chat_id, events, {"k": "done", "status": "stopped", **({'unsent': unsent} if unsent else {})})
        return {}

    def _files(self, req) -> dict:
        _, project = self._project(req)
        path = req.get("path")
        if not isinstance(path, str):
            raise Refused("invalid", "неверные поля")
        if "\\" in path or any(p in ("", ".", "..") for p in (path.split("/") if path else [])):
            raise Refused("outside", "путь вне проекта")
        return project.setdefault("files", {})

    def _op_files(self, req, events):
        files, path = self._files(req), req["path"]
        _, project = self._project(req)
        empty_dirs = project.setdefault("dirs", set())
        prefix = path + "/" if path else ""
        if path and path not in empty_dirs and not any(f.startswith(prefix) for f in files):
            raise Refused("not_found", "нет такой папки")
        dirs, plain = {}, []
        for f, data in files.items():
            if not f.startswith(prefix):
                continue
            head, _, rest = f[len(prefix):].partition("/")
            if rest:
                dirs[head] = {"name": head, "dir": True, "size": 0, "created": self.now, "modified": self.now}
            else:
                plain.append({"name": head, "dir": False, "size": len(data), "created": self.now, "modified": self.now})
        for folder in empty_dirs:
            if folder.startswith(prefix) and folder != path:
                head = folder[len(prefix):].split("/")[0]
                dirs[head] = {"name": head, "dir": True, "size": 0, "created": self.now, "modified": self.now}
        return {"items": sorted(dirs.values(), key=lambda i: i["name"]) + sorted(plain, key=lambda i: i["name"])}

    def _op_file_operation(self, req, events):
        files = self._files(req)
        _, project = self._project(req)
        dirs = project.setdefault("dirs", set())
        for filename in list(files):
            parts = filename.split("/")
            dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
        path, operation, destination = req["path"], req["operation"], req.get("destination")
        if not path or operation == "move" and not destination:
            raise Refused("invalid", "неверные поля")
        if operation.startswith("create_"):
            if path in files or path in dirs:
                raise Refused("exists", "файл или папка с таким именем уже существует")
            if path.rpartition("/")[0] not in dirs | {""}:
                raise Refused("not_found", "нет такой папки")
            if operation == "create_file":
                files[path] = b""
            else:
                dirs.add(path)
        else:
            if path not in files and path not in dirs:
                raise Refused("not_found", "файл или папка не найдены")
            if path in self.read_only:
                raise Refused("read_only", "файл только для чтения")
            if operation == "move":
                if destination in files or destination in dirs:
                    raise Refused("exists", "файл или папка с таким именем уже существует")
                if destination.startswith(path + "/"):
                    raise Refused("invalid", "нельзя переместить папку внутрь самой себя")
                if destination.rpartition("/")[0] not in dirs | {""}:
                    raise Refused("not_found", "нет такой папки")
            affected = lambda value: value == path or value.startswith(path + "/")
            for filename in [value for value in files if affected(value)]:
                content = files.pop(filename)
                if operation == "move":
                    files[destination + filename[len(path):]] = content
            for folder in [value for value in dirs if affected(value)]:
                dirs.remove(folder)
                if operation == "move":
                    dirs.add(destination + folder[len(path):])
        return {}

    def _op_file_stats(self, req, events):
        _, project = self._project(req)
        if "file_statistics" in project:
            return project["file_statistics"]
        path = req["path"]
        prefix = path + "/" if path else ""
        rows = {path: {"path": path, "size": 0, "files": 0, "folders": 0, "complete": True}}
        for filename, data in self._files(req).items():
            if not filename.startswith(prefix):
                continue
            parents = [path]
            tail = filename[len(prefix):].split("/")[:-1]
            for i in range(len(tail)):
                folder = prefix + "/".join(tail[:i + 1])
                rows.setdefault(folder, {"path": folder, "size": 0, "files": 0, "folders": 0, "complete": True})
                parents.append(folder)
            for parent in parents:
                rows[parent]["size"] += len(data)
                rows[parent]["files"] += 1
        for parent, row in rows.items():
            row["folders"] = sum(folder != parent and (not parent or folder.startswith(parent + "/")) for folder in rows)
        return {"items": list(rows.values()), "complete": True, "skipped_links": 0, "checked_at": self.now}

    def _file(self, req) -> bytes:
        data = self._files(req).get(req["path"])
        if data is None:
            raise Refused("not_found", "нет такого файла")
        return data

    @staticmethod
    def _text(data: bytes) -> str:
        if len(data) > OPEN_MAX:
            raise Refused("too_large", "файл больше 2 МиБ")
        try:
            if b"\0" in data:
                raise UnicodeDecodeError("utf-8", data, 0, 1, "NUL")
            return data.decode("utf-8")
        except UnicodeDecodeError:
            raise Refused("binary", "двоичный файл") from None

    def _op_read(self, req, events):
        data = self._file(req)
        return {"text": self._text(data), "version": _version(data)}

    def _op_visualization(self, req, events):
        return self._op_read({**req, 'path': req['source']}, events)

    def _op_raw(self, req, events):
        data = self._file(req)
        if len(data) > RAW_MAX:
            raise Refused("too_large", "файл больше 10 МиБ")
        return {"data": base64.b64encode(data).decode(), "size": len(data), "version": _version(data)}

    def _op_save(self, req, events):
        data = self._file(req)
        self._text(data)
        if req.get("version") != _version(data):
            raise Refused("conflict", "файл изменился", version=_version(data))
        text = req.get("text")
        if not isinstance(text, str):
            raise Refused("invalid", "неверные поля")
        new = text.encode()
        if len(new) > OPEN_MAX:
            raise Refused("too_large", "файл больше 2 МиБ")
        if req["path"] in self.read_only:
            raise Refused("read_only", "файл только для чтения")
        self._files(req)[req["path"]] = new
        return {"version": _version(new)}

    def _op_git(self, req, events):
        _, project = self._project(req)
        if project.get("git") is None:
            raise Refused("not_git", "не репозиторий git")
        return dict(project["git"])

    # ---- ход ---------------------------------------------------------------------

    def _ev(self, chat_id: str, e: dict) -> dict:
        run = self.running.get(chat_id) or {}
        return {"t": "ev", "project": run.get("project"), "chat": chat_id, "turn": run.get("turn"), "e": e}

    def _play(self, chat_id: str, events: list) -> None:
        """События хода до первого `ask` включительно или до `done`."""
        run = self.running[chat_id]
        run.setdefault("timeline", [])
        run.setdefault("progress", {})
        run.setdefault("rest", [])      # ход, собранный тестом вручную: без него KeyError тихо съедал `asked`
        while run["rest"]:
            e = run["rest"].pop(0)
            if e["k"] == "done":
                self._finish(chat_id, events, e)
                return
            events.append(self._ev(chat_id, e))
            if e["k"] == "text":
                start = len(run["text"].encode("utf-16-le", errors="surrogatepass")) // 2
                run["text"] += e["text"]
                run["timeline"].append({"kind": "text", "start": start,
                                        "end": len(run["text"].encode("utf-16-le", errors="surrogatepass")) // 2,
                                        **({"block": e["block"]} if e.get("block") else {})})
            elif e["k"] == "context":
                self.context = {k: v for k, v in e.items() if k != "k"}
            elif e["k"] == "activity":
                run["activity"] = {k: v for k, v in e.items() if k != "k"}
            elif e["k"] == "settings":
                for project in self.projects.values():
                    if chat_id in project.get("chats", {}):
                        project["chats"][chat_id]["settings"] = {k: v for k, v in e.items() if k != "k"}
            elif e["k"] == "step":
                run["steps"].append({**{k: v for k, v in e.items() if k != "k"}, "text": "", "error": False})
                run["timeline"].append({"kind": "step", "id": e["id"]})
            elif e["k"] in ("result", "step_update"):
                for step in run["steps"]:
                    if step["id"] == e["id"]:
                        step.update({k: v for k, v in e.items() if k != "k"})
            elif e["k"] == "progress":
                run["progress"].update({k: v for k, v in e.items() if k != "k"})
            elif e["k"] == "ask":
                run["asks"][e["request_id"]] = e
                return

    def _finish(self, chat_id: str, events: list, done: dict) -> None:
        run = self.running[chat_id]
        events.append(self._ev(chat_id, done))
        del self.running[chat_id]
        for project in self.projects.values():
            chat = project.get("chats", {}).get(chat_id)
            if chat is not None:
                self.now += 1
                chat["messages"].append({"id": f"m-{len(chat['messages'])}", "role": "assistant",
                                         "text": run["text"], "steps": run["steps"], "time": self.now,
                                         **{key: run[key] for key in ("timeline", "progress") if run.get(key)}})
                chat["modified"] = self.now


# ---- канал в процессе службы ---------------------------------------------------------

class InProcessBridge(bridge.Link):
    """Канал без сокета: кадры студии — в `sent`, ответ и события машины — через `bridge.deliver` следующими
    шагами цикла службы, по порядку. После `close` отправка падает, как у закрытого сокета."""

    def __init__(self, agent_id: int, user_id: int, name: str, machine: FakeMachine, ip: str = "10.77.48.5"):
        super().__init__(agent_id, user_id, name, ip)
        self.hello = bridge.parse_hello(machine.hello)
        self.machine = machine
        self.sent: list[dict] = []
        self.closed_with: int | None = None

    async def send(self, frame: dict) -> None:
        if self.closed_with is not None:
            raise ConnectionError("канал закрыт")
        self.sent.append(frame)
        if frame["t"] == "req":
            res, evs = self.machine.handle(frame)
            loop = asyncio.get_running_loop()
            for f in (res, *evs):
                loop.call_soon(bridge.deliver, self, f)
        elif frame["t"] == "close":
            self.machine.open_sessions.discard(frame["session"])
            if self.machine.desktop_owner == frame["session"]:
                self.machine.desktop_active = self.machine.desktop_owner = None
        elif frame["t"] == "desktop_close":
            if self.machine.desktop_owner == frame["session"]:
                self.machine.desktop_active = self.machine.desktop_owner = None

    async def close(self, code: int) -> None:
        self.closed_with = code


def connect(s, agent_id: int, machine: FakeMachine) -> InProcessBridge:
    """Мост на связи: новый канал — открытые сессии машины пропадают (§3)."""
    row = state.DB.execute("SELECT user_id, name FROM agents WHERE id = ?", (agent_id,)).fetchone()
    link = InProcessBridge(agent_id, row["user_id"], row["name"], machine)
    machine.open_sessions.clear()
    s.c.portal.call(bridge.attach, link)
    return link


def emit(s, link: InProcessBridge, project: str, chat: str, *events: dict) -> None:
    """Мост сам шлёт события хода (на цикле службы — очереди наблюдателей asyncio)."""
    def deliver():
        for e in events:
            bridge.deliver(link, {"t": "ev", "project": project, "chat": chat, "turn": "t-0", "e": e})
    s.c.portal.call(deliver)


def drop(s, link: InProcessBridge) -> None:
    """Канал оборвался со стороны моста."""
    link.closed_with = 1006
    s.c.portal.call(bridge.detach, link)


def settle(s) -> None:
    """Дождаться фоновых отправок службы (`bridge.TASKS`: уведомления `close`)."""
    async def wait():
        while bridge.TASKS:
            await asyncio.gather(*list(bridge.TASKS), return_exceptions=True)
            await asyncio.sleep(0)  # gather(done) can finish before the TASKS.discard callback runs.
    s.c.portal.call(wait)


# ---- настоящий WebSocket -----------------------------------------------------------

class SocketBridge:
    """Мост через сеть: `start()` подключается ключом устройства, шлёт `hello`, ждёт `welcome` и дальше в
    своём потоке отвечает машиной; `close_code` — код закрытия сервером (после конца потока)."""

    def __init__(self, url: str, key: str, machine: FakeMachine):
        self.ws_url = "ws" + url[len("http"):] + "/api/agent/link"
        self.key, self.machine = key, machine
        self.welcome: dict | None = None
        self.close_code: int | None = None
        self._ws = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> "SocketBridge":
        from websockets.sync.client import connect as ws_connect
        self._ws = ws_connect(self.ws_url, additional_headers={"Authorization": f"Bearer {self.key}"},
                              open_timeout=5, legacy=True)
        self.machine.open_sessions.clear()
        self._send(self.machine.hello)
        self.welcome = json.loads(self._ws.recv(timeout=5))
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _send(self, frame: dict) -> None:
        with self._lock:
            self._ws.send(json.dumps(frame, ensure_ascii=False))

    def _run(self) -> None:
        from websockets.exceptions import ConnectionClosed
        try:
            for message in self._ws:
                frame = json.loads(message)
                if frame.get("t") == "req":
                    res, evs = self.machine.handle(frame)
                    for f in (res, *evs):
                        self._send(f)
                elif frame.get("t") == "close":
                    self.machine.open_sessions.discard(frame.get("session"))
                    if self.machine.desktop_owner == frame.get("session"):
                        self.machine.desktop_active = self.machine.desktop_owner = None
                elif frame.get("t") == "desktop_close":
                    if self.machine.desktop_owner == frame.get("session"):
                        self.machine.desktop_active = self.machine.desktop_owner = None
        except ConnectionClosed:
            pass
        finally:
            self.close_code = self._ws.close_code

    def send_events(self, project: str, chat: str, *events: dict) -> None:
        for e in events:
            self._send({"t": "ev", "project": project, "chat": chat, "turn": "t-0", "e": e})

    def stop(self) -> None:
        if self._ws is not None:
            self._ws.close()
        if self._thread is not None:
            self._thread.join(5)
