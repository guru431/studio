"""DeepSeek Harness в мосте: `dsh --profile acp` поверх общей основы ACP (`acp.AcpRunner`).

Процесс получает наложение настроек профиля Web/Desktop человека (`deepseek_settings.build`); файл наложения живёт,
пока жив процесс. Продолжение чата — `session/resume`, история — из файлов сессий (`deepseek_history`): у ACP DSH нет
ни `session/load`, ни повтора событий. Пробные сессии каталога и предпросмотра настроек идут с хранилищами во
временном каталоге моста (проба 2026-11-22, D4): в `session/list` и `$DSH_HOME` они не попадают.

Права (D3): `DSH_PERMISSION_MODE` действует только на создаваемую сессию — у чата с сессией права те, что при
создании. Файлы DSH человека мост только читает; в журнал — только id, коды и имена типов исключений."""

import asyncio
import contextlib
import contextvars
import json
import os
import re
import uuid
from pathlib import Path

from . import (acp, deepseek_history, deepseek_rpc, deepseek_settings, history_images, host, limits, mcp, permissions,
               store)
from .errors import Refused

SESSION_ID = deepseek_history.SESSION_ID
MODEL_ID = re.compile(r'\["[^"\\\x00-\x1f]{1,150}","[^"\\\x00-\x1f]{1,150}"\]')  # `["<провайдер>","<модель>"]`
NAMESPACE = uuid.UUID("5d0f7c2e-3b8a-4f61-9c47-2e8d1a6b9f30")
FAILED = "DeepSeek Harness не ответил — проверьте настройки провайдера в DeepSeek Harness на компьютере"
UNAVAILABLE = "Выбранные настройки недоступны для этой модели DeepSeek Harness"
MISSING = "DeepSeek Harness не найден на компьютере"
NODE = "Для DeepSeek Harness нужен Node.js новее установленного"
WRITER = "Чат открыт в DeepSeek Harness на компьютере — закройте DeepSeek Harness и повторите"
FIXED = "Права этого чата DeepSeek Harness заданы при создании"
IMAGES = "Выбранная модель DeepSeek Harness не принимает картинки"
GONE = "нет такого чата"
SCRATCH = "overlay-scratch-"  # временные хранилища пробных сессий; остатки после падения стирает `deepseek_settings.sweep`

# Вид процесса DSH: ход — с наложением; пробный (каталог, предпросмотр) — хранилища сессий во временном каталоге,
# без MCP; список чатов — без наложения вовсе (`session/list` читает хранилище по умолчанию, MCP и провайдеры ему
# не нужны). Переменные контекста — у своей задачи: ходы, идущие одновременно, получают процесс хода.
_SCRATCH = contextvars.ContextVar("deepseek_scratch", default=None)
_LIST = contextvars.ContextVar("deepseek_list", default=False)


def _remove(path):
    with contextlib.suppress(OSError):
        Path(path).unlink()


def _model(pair):
    """`(провайдер, модель)` файла сессии → значение ACP `["<провайдер>","<модель>"]` (как `JSON.stringify` DSH)."""
    if pair is None:
        return None
    value = json.dumps(list(pair), ensure_ascii=False, separators=(",", ":"))
    return value if MODEL_ID.fullmatch(value) else None


def _plain(values):
    """Усилие `""` («Provider default» DSH) — как отсутствие выбора: в каталоге усилий его нет."""
    return {**values, "effort": values.get("effort") or None}


class _Process:
    """Процесс `dsh --profile acp` и его наложение: файл удаляется после `disconnect`."""

    def __init__(self, rpc, install, overlay, summary):
        self._rpc, self.install, self.overlay, self.summary = rpc, install, overlay, summary

    def __getattr__(self, name):
        return getattr(self._rpc, name)

    async def disconnect(self):
        try:
            await self._rpc.disconnect()
        finally:
            if self.overlay is not None:
                await asyncio.to_thread(_remove, self.overlay)


class DeepSeekRunner(acp.AcpRunner):
    ENGINE, LABEL = "deepseek", "DeepSeek Harness"
    SESSION_ID = SESSION_ID
    MODEL_ID = MODEL_ID
    EFFORT_KEY = "reasoning_effort"
    ALIASES_FILE, CHOICES_DIR = "deepseek-chats.json", "deepseek"
    NAMESPACE = NAMESPACE
    FAILED, UNAVAILABLE = FAILED, UNAVAILABLE

    def __init__(self, emit, *, home, choices=None, find_installs=deepseek_rpc.find_installs,
                 factory=deepseek_rpc.Client, environ=None):
        super().__init__(emit, home=home, choices=choices, find_cli=None, factory=factory)
        self.home = Path(home)
        self.find_installs = find_installs
        self.environ = environ
        self._overview_cache = {}  # путь файла сессии → (ревизия, `deepseek_history.overview`)
        self.unavailable = None  # причина последнего отказа запуска: `node` | `missing` | None

    @property
    def unavailable_message(self):
        """Слова причины для отказов студии и карточки компьютера: DSH стоит, но Node старше нужного."""
        return NODE if self.unavailable == "node" else None

    def _dsh_home(self):
        return deepseek_settings.dsh_home(self.environ)

    def _choose(self):
        installs = self.find_installs()
        home = self._dsh_home()
        return home, deepseek_rpc.choose(installs, deepseek_settings.source(home)), installs

    async def _connect(self, project, *, access=None):
        scratch = _SCRATCH.get()
        home, install, installs = await asyncio.to_thread(self._choose)
        if install is None:
            self.unavailable = "node" if any(i.reason == "node" for i in installs) else "missing"
            raise Refused("unsupported", NODE if self.unavailable == "node" else MISSING)
        self.unavailable = None
        if _LIST.get() and scratch is None:
            overlay, summary = None, None
        else:
            overlay, summary = await asyncio.to_thread(
                deepseek_settings.build, home, self.environ if self.environ is not None else os.environ,
                self.home, install, scratch=scratch["root"] if scratch else None, access=access)
            if scratch is not None and overlay is None:
                raise Refused("unsupported", FAILED)  # пробная сессия без своего хранилища попала бы к человеку
        process = _Process(self.rpc_factory(install, Path(project), access=access, overlay=overlay),
                           install, overlay, summary)
        try:
            await process._rpc.connect()
        except BaseException:
            with contextlib.suppress(Exception):
                await process.disconnect()
            raise
        if scratch is not None:
            scratch["process"] = process
        return process

    @contextlib.asynccontextmanager
    async def _scratch(self):
        folder = await asyncio.to_thread(host.temporary_dir, self.home / self.CHOICES_DIR, SCRATCH)
        holder = {"root": Path(folder.name), "process": None}
        token = _SCRATCH.set(holder)
        try:
            yield holder
        finally:
            _SCRATCH.reset(token)
            with contextlib.suppress(OSError):
                await asyncio.to_thread(folder.cleanup)

    async def _probe(self, root):
        async with self._scratch() as holder:
            ready = await super()._probe(root)
        if ready:
            process = holder["process"]
            self.version = await asyncio.to_thread(deepseek_rpc.version, process.install)
            self.catalog = {**self.catalog, "version": self.version, "settings": process.summary,
                            "defaults": _plain(self.catalog["defaults"])}
        return ready

    async def chats(self, project):
        token = _LIST.set(True)  # процесс списка запускается здесь и живёт до `close()` — без наложения
        try:
            items = await super().chats(project)
        finally:
            _LIST.reset(token)
        # `session/list` DSH даёт только id и cwd: название и время — из файлов сессий (только чтение)
        if items:
            saved = await asyncio.to_thread(self.aliases.load)
            targets = {item["chat"]: (saved.get(item["chat"]) or {}).get("session") for item in items}
            found = await asyncio.to_thread(self._overviews, project, targets)
            for item in items:
                info = found.get(item["chat"]) or {}
                item["title"] = item["title"] or info.get("title", "")[:200]
                item["created"] = item["created"] or info.get("created")
                item["modified"] = item["modified"] or info.get("modified")
        return items

    def _overviews(self, project, targets):
        """Название, создание и изменение сессий по файлам; разобранное живёт до смены ревизии файла."""
        home, out = self._dsh_home(), {}
        if len(self._overview_cache) > 4 * limits.CHATS_MAX:
            self._overview_cache.clear()
        for chat, target in targets.items():
            try:
                path = deepseek_history.locate(home, project, target)
                if path is None:
                    continue
                revision = deepseek_history.revision(path)
                cached = self._overview_cache.get(path)
                if cached is None or cached[0] != revision:
                    cached = self._overview_cache[path] = (revision, deepseek_history.overview(path))
                out[chat] = {**cached[1], "modified": revision[0] / 1e9}
            except (deepseek_history.Unsupported, OSError):
                continue
        return out

    async def _mcp(self, rpc, project):
        return await asyncio.to_thread(lambda: mcp.deepseek(mcp.load(store.home(), project, "deepseek"), rpc.info))

    async def _open(self, rpc, project, target, directories=None):
        try:
            return await rpc.request("session/resume", {"sessionId": target, "cwd": str(project),
                                                        "mcpServers": await self._mcp(rpc, project)})
        except acp.RpcError as e:
            if e.owned:
                raise Refused("writer_active", WRITER) from None
            if e.not_found:
                raise Refused("not_found", GONE) from None
            raise

    async def _discard(self, rpc, target):
        # Только пробная сессия этого вызова (во временном хранилище); сессии человека не закрываются отсюда.
        await rpc.request("session/close", {"sessionId": target})

    # --- история: файлы сессий ---

    async def _row(self, project, target):
        path = await asyncio.to_thread(deepseek_history.locate, self._dsh_home(), project, target)
        if path is None:
            return None
        try:
            return {"path": path, "revision": await asyncio.to_thread(deepseek_history.revision, path)}
        except OSError:
            return None

    @staticmethod
    def _revision(row):
        return row.get("revision") if row is not None else None

    async def _history_items(self, project, target, row, *, image_blocks=None):
        path = row["path"] if row is not None else await asyncio.to_thread(
            deepseek_history.locate, self._dsh_home(), project, target)
        if path is None:
            raise Refused("not_found", GONE)
        try:
            return await asyncio.to_thread(deepseek_history.read, path, image_blocks=image_blocks)
        except OSError:
            raise Refused("not_found", GONE) from None

    async def history(self, project, chat, before, limit):
        try:
            return await super().history(project, chat, before, limit)
        except deepseek_history.Unsupported as e:
            if e.code == "too_large":
                raise Refused("too_large") from None
            return {"items": [], "start": 0, "total": 0, "engine": self.ENGINE, "notice": "unsupported_format"}

    async def history_image(self, project, chat, message_id, index):
        target = await asyncio.to_thread(self.aliases.target, project, chat)
        if target is None:
            raise Refused("not_found", GONE)
        blocks = {}
        try:
            await self._history_items(project, target, None, image_blocks=blocks)
        except deepseek_history.Unsupported as e:
            if e.code == "too_large":
                raise Refused("too_large") from None
            raise Refused("not_found", "Изображение отсутствует в сообщении") from None
        return await asyncio.to_thread(history_images.payload, blocks.get(message_id), index, self.ENGINE, project,
                                       root=self._dsh_home())

    # --- настройки и права ---

    def _settings(self, t, data):
        # Ход идёт в сессии: после него права чата закреплены (D3).
        return {**_plain(super()._settings(t, data)), "overlay": t.client.rpc.summary, "access_fixed": True}

    async def _recorded(self, project, target):
        """Настройки чата из файла его сессии (`deepseek_history.recorded`); нет файла, формат незнаком или файл
        повреждён — `{}` (тогда действуют умолчания каталога)."""
        try:
            path = await asyncio.to_thread(deepseek_history.locate, self._dsh_home(), project, target)
            return await asyncio.to_thread(deepseek_history.recorded, path) if path is not None else {}
        except (deepseek_history.Unsupported, OSError):
            return {}

    @staticmethod
    def _fixed(saved, recorded):
        """Права чата с сессией: выбранные студией при создании, иначе `permission/preset` файла (D3)."""
        for value in (saved.get("access"), recorded.get("preset")):
            if value in deepseek_rpc.ACCESS:
                return value
        return "default"

    async def state(self, project, chat, model=None, effort=None, access=None, *, agent_mode=None,
                    additional_directories=None):
        """Настройки без генерации. Чат с сессией не возобновляется: модель, усилие, контекст и права — из файла
        сессии (права — как при создании, D3), варианты — из каталога пробы. Выбор модели и усилия — предпросмотром
        в пробной сессии временного хранилища: сессия человека не меняется."""
        saved = await asyncio.to_thread(self.choices.details, chat)
        target = await asyncio.to_thread(self.aliases.target, project, chat)
        model, effort = model or saved.get("model"), effort or saved.get("effort")
        context, recorded = saved.get("context"), {}
        if target is not None:
            recorded = await self._recorded(project, target)
            access = self._fixed(saved, recorded)
            if found := permissions.context(recorded.get("used"), recorded.get("limit")):
                context = {**(context or {}), **found}
        if target is not None and not (model or effort):
            defaults = (self.catalog or {}).get("defaults") or {}
            chosen = _model(recorded.get("model"))
            settings = {"model": chosen or defaults.get("model"),
                        "effort": recorded.get("effort") if chosen else defaults.get("effort"), "access": access}
            overlay = (self.catalog or {}).get("settings")
        else:
            async with self._scratch():
                rpc, temporary = await self._connect(project, access=access if target is None else None), None
                try:
                    data = await self._new(rpc, project)
                    temporary = data["sessionId"]
                    data = await self._configure(rpc, temporary, data, model, effort)
                finally:
                    try:
                        if temporary is not None:
                            await self._discard(rpc, temporary)
                    finally:
                        await rpc.disconnect()
            settings, overlay = acp.settings(data, access, effort_key=self.EFFORT_KEY), rpc.summary
        return {"settings": {**_plain(settings), "commands": [], "additional_directories": [], "overlay": overlay,
                             **({"access_fixed": True} if target is not None else {})},
                "context": context}

    async def _run(self, t, text, model, effort, images):
        try:
            if t.access is not None:
                target = await asyncio.to_thread(self.aliases.target, t.project, t.chat)
                saved = await asyncio.to_thread(self.choices.details, t.chat)
                # страница шлёт права, которые ответил `state` (из файла сессии); прежний ответ — выбор студии
                if (target is not None and t.access != (saved.get("access") or "default")
                        and t.access != self._fixed(saved, await self._recorded(t.project, target))):
                    raise Refused("invalid", FIXED)
        except asyncio.CancelledError:
            await self._finish(t, False, None)
            raise
        except Refused as e:
            await self._finish(t, False, e.message)
            return
        except Exception:
            pass  # чтение соответствий повторит общий ход и назовёт сбой сам
        await super()._run(t, text, model, effort, images)

    def _failure(self, t, error):
        if isinstance(error, Refused) and error.code == "writer_active":
            t.writer_active = True
        if isinstance(error, acp.RpcError) and error.image:
            return IMAGES
        return super()._failure(t, error)

    def _done(self, t, failure):
        result = super()._done(t, failure)
        if failure is not None and getattr(t, "writer_active", False):
            result["code"] = "writer_active"
        return result

    async def _request_card(self, t, frame):
        # Запрос разрешения DSH несёт только `toolCall.toolCallId`: имя и аргументы — из шага с тем же id.
        params = frame.get("params")
        tool = params.get("toolCall") if isinstance(params, dict) else None
        if frame.get("method") == "session/request_permission" and isinstance(tool, dict):
            step = next((s for s in t.feed.steps if s["id"] == tool.get("toolCallId")), None)
            if step is not None:
                frame = {**frame, "params": {**params, "toolCall": {
                    "title": step["tool"], "rawInput": step["input"], **tool}}}
        await super()._request_card(t, frame)
