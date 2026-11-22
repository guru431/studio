"""OpenCode в мосте: ACP-каталог, локальные сессии, поток и карточки студии (общее — `acp.AcpRunner`)."""

import asyncio
import uuid
from pathlib import Path

from . import acp, limits, mcp, opencode_events, opencode_rpc, sessions, store
from .acp import (EFFORT_ID, MODEL_ID, Aliases, AcpRunner, normalize, options, settings, step_of, text_of,  # noqa: F401
                  timestamp, values)
from .codex import same_project
from .errors import Refused

SESSION_ID = opencode_events.SESSION_ID
NAMESPACE = uuid.UUID("9043b00c-91a9-485c-a679-91fa3607114e")
FAILED = "OpenCode не ответил — проверьте вход и журнал OpenCode на компьютере"
UNAVAILABLE = "Выбранные настройки недоступны для этой модели OpenCode"


class OpenCodeRunner(AcpRunner):
    ENGINE, LABEL = "opencode", "OpenCode"
    SESSION_ID = SESSION_ID
    ALIASES_FILE, CHOICES_DIR = "opencode-chats.json", "opencode"
    NAMESPACE = NAMESPACE
    FAILED, UNAVAILABLE = FAILED, UNAVAILABLE

    def start(self, project, pid, chat, text, model, effort, *, agent_mode=None, additional_directories=None, **kwargs):
        result = super().start(project, pid, chat, text, model, effort, **kwargs)
        self._turns[chat].opencode_options = {'agent_mode': agent_mode, 'additional_directories': additional_directories}
        return result

    def __init__(self, emit, *, home, choices=None, find_cli=None, factory=opencode_rpc.Client):
        super().__init__(emit, home=home, choices=choices, find_cli=find_cli or opencode_rpc.find_cli, factory=factory)
        self.find_opencode = self.find_acp

    async def _connect(self, project, *, access=None):
        path = await asyncio.to_thread(self.find_opencode)
        if path is None:
            raise Refused("unsupported", "OpenCode не найден на компьютере")
        rpc = self.rpc_factory(path, Path(project))
        await rpc.connect()
        return rpc

    async def _mcp(self, rpc, project):
        return await asyncio.to_thread(lambda: mcp.opencode(mcp.load(store.home(), project, "opencode"), rpc.info))

    async def _discard(self, rpc, target):
        await self._delete(rpc, target)

    async def _delete(self, rpc, target):
        # Только пустая сессия, которую создал этот вызов; пользовательские сессии не удаляются.
        await rpc.request("session/delete", {"sessionId": target})

    @staticmethod
    def _directories(row):
        directories = row.get('additionalDirectories', [])
        if not isinstance(directories, list) or not all(isinstance(p, str) and Path(p).is_absolute() for p in directories):
            raise Refused('invalid')
        return directories

    async def _open(self, rpc, project, target, directories=None):
        try:
            if directories is None:
                cursor, seen = None, set()
                for _ in range(100):
                    page = await rpc.request('session/list', {'cwd': str(project), **({'cursor': cursor} if cursor else {})})
                    row = next((row for row in page.get('sessions', []) if row.get('sessionId') == target
                                and same_project(row.get('cwd'), project)), None)
                    if row is not None:
                        directories = self._directories(row)
                        break
                    cursor = page.get('nextCursor')
                    if not cursor or cursor in seen:
                        break
                    seen.add(cursor)
                else:
                    raise Refused('too_large')
            data = await rpc.request("session/load", {"sessionId": target, "cwd": str(project), "mcpServers": await self._mcp(rpc, project),
                **({'additionalDirectories': directories} if directories is not None else {})})
            return {**data, '_studio_directories': directories}
        except acp.RpcError as e:
            if e.not_found:
                raise Refused("not_found", "нет такого чата") from None
            raise

    @staticmethod
    def _updates(rpc, target, *, children=None):
        return acp.drain(rpc, target, opencode_events.child, children)

    def _child_of(self, update, target, known):
        return opencode_events.child(update, target, known)

    async def _history_items(self, project, target, row, *, image_blocks=None):
        # Replay всей сессии `session/load` — отдельный процесс `opencode acp`.
        rpc = await self._connect(project)
        try:
            await self._open(rpc, project, target, self._directories(row) if row is not None else None)
            return await asyncio.to_thread(normalize, self._updates(rpc, target), image_blocks=image_blocks)
        finally:
            await rpc.disconnect()

    def _notification(self, t, update):
        super()._notification(t, update)
        if (update.get("sessionUpdate") in ("tool_call", "tool_call_update") and isinstance(update.get("toolCallId"), str)
                and self._child_of(update, t.client.session, t.opencode_children) is None):
            self._adopt(t, update, next(s for s in t.feed.steps if s["id"] == update["toolCallId"]))

    def _progress(self, t, update):
        self._status(t, opencode_events.status(update))

    def _child(self, t, child):
        if t.opencode_children.get(child['id']) == child:
            return
        t.opencode_children[child['id']] = child
        t.subtasks[child['id']] = child['title']
        t.progress['agents'] = [{'id': row['id'], 'title': row['title'], 'status': 'running'}
                                for row in t.opencode_children.values()]
        self._send(t, {'k': 'progress', **t.progress})

    async def fork_chat(self, project, chat, message_id=None):
        target = await asyncio.to_thread(self.aliases.target, project, chat)
        if target is None:
            raise Refused('not_found')
        draft = ''
        if message_id is not None:
            from .opencode_native import fork_at

            rpc = await self._connect(project)
            try:
                await self._open(rpc, project, target)
                items = await asyncio.to_thread(normalize, self._updates(rpc, target))
            finally:
                await rpc.disconnect()
            point = next((row for row in items if row['id'] == message_id and row['role'] == 'user'), None)
            if point is None:
                raise Refused('invalid', 'Точка ветвления отсутствует в истории')
            draft = point['text'][:100_000]
            target_new = await fork_at(await asyncio.to_thread(self.find_opencode), project, target, message_id)
        else:
            rpc = await self._connect(project)
            try:
                loaded = await self._open(rpc, project, target)
                directories = loaded.get('_studio_directories')
                data = await rpc.request('session/fork', {'sessionId': target, 'cwd': str(project), 'mcpServers': await self._mcp(rpc, project),
                    **({'additionalDirectories': directories} if directories is not None else {})})
                target_new = data.get('sessionId')
            finally:
                await rpc.disconnect()
        if not isinstance(target_new, str) or not SESSION_ID.fullmatch(target_new) or target_new == target:
            raise Refused('invalid', 'OpenCode не создал независимый чат')
        new_chat = str(uuid.uuid4())
        await asyncio.to_thread(self.aliases.put, project, new_chat, target_new)
        return {'chat': new_chat, 'draft': draft}

    async def subagents(self, project, chat):
        target = await asyncio.to_thread(self.aliases.target, project, chat)
        if target is None:
            return {'items': [], 'complete': True}
        live = self._turns.get(chat)
        from .opencode_native import children_of

        listing = await children_of(await asyncio.to_thread(self.find_opencode), project, target)
        if live is not None and live.project == project:
            for row in listing['items']:
                if row['id'] in live.opencode_children and row['status'] == 'unknown':
                    row['status'] = 'running'
        return listing

    async def subagent_history(self, project, chat, request_id):
        listing = await self.subagents(project, chat)
        if request_id not in {row['id'] for row in listing['items']}:
            raise Refused('not_found')
        rpc = await self._connect(project)
        try:
            await self._open(rpc, project, request_id)
            items = await asyncio.to_thread(normalize, self._updates(rpc, request_id))
            page = await asyncio.to_thread(sessions.fit, items, budget=limits.HISTORY_BUDGET)
            return {'items': page, 'complete': True}
        finally:
            await rpc.disconnect()

    @staticmethod
    def _adopt(t, update, step):
        """Дочерняя сессия инструмента `task` этого хода: OpenCode пишет её в metadata шага
        (`sessionId`, `parentSessionId`). Только такие сессии получают карточку «Подзадача»."""
        output = update.get("rawOutput")
        for source in (output.get("metadata"), output) if isinstance(output, dict) else ():
            if not isinstance(source, dict):
                continue
            child = source.get("sessionId") or source.get("sessionID")
            parent = source.get("parentSessionId") or source.get("parentSessionID")
            if (isinstance(child, str) and SESSION_ID.fullmatch(child) and child != t.client.session
                    and parent == t.client.session and (child in t.subtasks or len(t.subtasks) < 50)):
                data = step.get("input") if isinstance(step.get("input"), dict) else {}
                name = next((v.strip()[:200] for v in (step.get("tool"), data.get("description"),
                             data.get("subagent_type")) if isinstance(v, str) and v.strip() and v != "Tool"), None)
                t.subtasks[child] = name or t.subtasks.get(child)
                return
