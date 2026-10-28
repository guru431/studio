"""Ход Claude Code через Agent SDK: клиент на ход, карточки разрешений и вопросов, остановка (протокол §4, §7).

Ход живёт в задаче моста и от канала не зависит: канал оборвался — ход идёт, карточка ждёт ответа,
новый канал получает снимок (`watch`)."""

import asyncio
import contextlib
import json
import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from claude_agent_sdk import (ClaudeAgentOptions, ClaudeSDKClient, CLINotFoundError, HookMatcher,
                              PermissionResultAllow, PermissionResultDeny, ResultError, ResultMessage)

from . import cli, events, limits, sessions, store
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


async def _keep_alive(_input, _tool_use_id, _context) -> dict:
    """Холостой PreToolUse: с ним SDK держит поток открытым, пока `can_use_tool` ждёт ответа."""
    return {"continue_": True}


def options_for(project: Path, cli_path: Path | None, chat: str, new: bool, model: str | None,
                effort: str | None, can_use_tool) -> ClaudeAgentOptions:
    """Опции клиента хода: новая сессия — `session_id=chat`, существующая — `resume=chat`."""
    return ClaudeAgentOptions(
        cwd=project, cli_path=cli_path, session_id=chat if new else None, resume=None if new else chat,
        model=model, effort=effort, include_partial_messages=True, can_use_tool=can_use_tool,
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[_keep_alive])]},
        setting_sources=list(cli.SETTINGS), env=dict(cli.ENV), stderr=cli._drop)


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
    feed: events.Feed = field(default_factory=events.Feed)
    asks: dict[str, _Ask] = field(default_factory=dict)
    task: asyncio.Task | None = None
    client: object | None = None
    stopped: bool = False  # stop()
    closing: bool = False  # close(): мост выходит


class Runner:
    """Ходы машины: не больше `max_turns` сразу, в чате — один."""

    def __init__(self, emit: Callable[[dict], None], *, choices: store.Choices, cli_path: Path | None,
                 factory=ClaudeSDKClient, max_turns: int = MAX_TURNS):
        self.emit = emit
        self.choices = choices
        self.cli_path = cli_path
        self.factory = factory
        self.max_turns = max_turns
        self._turns: dict[str, _Turn] = {}

    # --- просьбы студии ---

    def start(self, project: Path, pid: str, chat: str, text: str, model: str | None, effort: str | None) -> str:
        """Ход в чате: запись и задача; всё блокирующее — уже в задаче."""
        if chat in self._turns:
            raise Refused("running")
        if len(self._turns) >= self.max_turns:
            raise Refused("busy")
        t = _Turn(project, pid, chat, secrets.token_hex(8))
        self._turns[chat] = t
        t.task = asyncio.create_task(self._run(t, text, model, effort))
        log.info("ход %s: начат", t.turn)
        return t.turn

    def watch(self, chat: str) -> tuple[bool, list[dict]]:
        """Идёт ли ход; идёт — кадр снимка: текст, шаги с итогами, открытые карточки."""
        t = self._turns.get(chat)
        if t is None:
            return False, []
        asks = [{k: v for k, v in a.event.items() if k != "k"} for a in t.asks.values()]
        room = limits.HISTORY_BUDGET - len(json.dumps(asks, ensure_ascii=True))
        body = sessions.fit([{"text": t.feed.text, "steps": [dict(s) for s in t.feed.steps]}], room)[0]
        return True, [self._frame(t, {"k": "snapshot", "text": body["text"], "steps": body["steps"], "asks": asks})]

    def answer(self, chat: str, request_id: str, decision: str, message: str | None, answers) -> None:
        """Ответ на карточку; неверные поля — `invalid`, карточки нет — `gone`."""
        if decision not in DECISIONS or not (message is None or (isinstance(message, str)
                                                                  and len(message) <= ANSWER_MESSAGE_MAX)):
            raise Refused("invalid")
        t = self._turns.get(chat)
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

    async def stop(self, chat: str) -> None:
        """Карточки хода — отказ, затем `interrupt()`; ход кончится `done` со `stopped`."""
        t = self._turns.get(chat)
        if t is None:
            raise Refused("idle")
        t.stopped = True
        await self._halt(t)

    async def close(self) -> None:
        """Мост выходит: остановить все ходы и дождаться их задач (дальше `CLOSE_SEC` — отмена)."""
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

    def live(self) -> dict[str, bool]:
        """Идущие ходы: чат → ждёт ли ход ответа на карточку."""
        return {chat: bool(t.asks) for chat, t in self._turns.items()}

    # --- ход ---

    async def _run(self, t: _Turn, text: str, model: str | None, effort: str | None) -> None:
        failure, cancelled, new, sent = None, False, False, False
        try:
            new, model, effort = await asyncio.to_thread(self._choose, t, model, effort)
            if not (t.stopped or t.closing):
                t.client = self.factory(options=options_for(t.project, self.cli_path, t.chat, new, model, effort,
                                                            lambda tool, data, ctx: self._ask(t, tool, data, ctx)))
                await t.client.connect()
            if not (t.stopped or t.closing):
                sent = True  # дальше файл сессии может появиться
                await t.client.query(text)
                async for message in t.client.receive_response():
                    for e in t.feed.events(message):
                        self._send(t, e)
                    if isinstance(message, ResultMessage) and message.is_error:
                        failure = _words(message.errors, message.result, message.subtype)
        except asyncio.CancelledError:
            cancelled = True
        except CLINotFoundError:
            failure = NO_CLI
        except ResultError as e:
            failure = _words(e.errors, e.result, e.subtype)
        except Exception as e:
            log.warning("ход %s: %s", t.turn, type(e).__name__)
            failure = NO_START
        await self._finish(t, new and sent, failure)
        if cancelled:
            raise asyncio.CancelledError

    def _choose(self, t: _Turn, model: str | None, effort: str | None) -> tuple[bool, str | None, str | None]:
        """В потоке: новая ли сессия, модель и усилие хода (явные запоминаются; `None` — запомненное)."""
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

    async def _finish(self, t: _Turn, mark: bool, failure: str | None) -> None:
        """Конец хода: карточки — отказ, клиент гаснет, новой сессии — метка, чат свободен, затем `done`."""
        try:
            self._deny_all(t)
            if t.client is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(t.client.disconnect(), DISCONNECT_SEC)
            if mark:  # клиент погашен: файл сессии не пишут двое
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(sessions.mark_studio, t.project, t.chat)
        finally:  # и при отмене посреди конца: чат не остаётся занятым
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
        log.info("ход %s: %s", t.turn, done["status"])
        return done

    async def _halt(self, t: _Turn) -> None:
        self._deny_all(t)
        if t.client is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(t.client.interrupt(), INTERRUPT_SEC)

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

    def _deny_all(self, t: _Turn) -> None:
        for rid in list(t.asks):
            self._settle(t, rid, PermissionResultDeny(message=STOPPED, interrupt=True))

    # --- кадры ---

    @staticmethod
    def _frame(t: _Turn, e: dict) -> dict:
        return {"t": "ev", "project": t.pid, "chat": t.chat, "turn": t.turn, "e": e}

    def _send(self, t: _Turn, e: dict) -> None:
        """Событие в канал; сбой канала ход не роняет."""
        try:
            self.emit(self._frame(t, e))
        except Exception as exc:
            log.warning("ход %s: событие не ушло: %s", t.turn, type(exc).__name__)


def _answers_ok(answers) -> bool:
    """Ответы на вопрос: словарь «вопрос → строка или список строк»."""
    return isinstance(answers, dict) and all(
        isinstance(k, str) and (isinstance(v, str) or (isinstance(v, list) and all(isinstance(s, str) for s in v)))
        for k, v in answers.items())
