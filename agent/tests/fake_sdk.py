"""Подставной SDK для хода: клиент по сценарию из настоящих сообщений SDK и указаний."""

import asyncio
import itertools

from claude_agent_sdk import (AssistantMessage, ResultMessage, StreamEvent, TextBlock, ToolPermissionContext,
                              ToolResultBlock, ToolUseBlock, UserMessage)

_uuids = itertools.count(1)


def _uuid() -> str:
    return f"00000000-0000-4000-8000-{next(_uuids):012d}"


def delta(text: str, parent: str | None = None) -> StreamEvent:
    return StreamEvent(uuid=_uuid(), session_id="s", parent_tool_use_id=parent,
                       event={"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}})


def block_start(kind: str = "text") -> StreamEvent:
    block = {"type": kind, "text": ""} if kind == "text" else {"type": kind}
    return StreamEvent(uuid=_uuid(), session_id="s", event={"type": "content_block_start", "index": 0,
                                                            "content_block": block})


def assistant(*blocks, parent: str | None = None) -> AssistantMessage:
    return AssistantMessage(content=list(blocks), model="claude-opus-5-5", parent_tool_use_id=parent)


def text(t: str) -> TextBlock:
    return TextBlock(text=t)


def tool_use(tid: str, name: str, data: dict) -> ToolUseBlock:
    return ToolUseBlock(id=tid, name=name, input=data)


def user(*blocks, parent: str | None = None) -> UserMessage:
    return UserMessage(content=list(blocks), parent_tool_use_id=parent)


def tool_result(tid: str, content, error: bool = False) -> ToolResultBlock:
    return ToolResultBlock(tool_use_id=tid, content=content, is_error=error)


def result(subtype: str = "success", is_error: bool = False, **kw) -> ResultMessage:
    return ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error, num_turns=1,
                         session_id="s", **kw)


def result_ok() -> ResultMessage:
    return result(result="готово")


HOLD = ("hold",)  # ждать interrupt


class FakeClient:
    """`script` — сообщения SDK; `("ask", tool, data, suggestions[, tool_use_id])` — зовёт `can_use_tool` и ждёт
    итога (итог — в `decisions`); `HOLD` — ждёт `interrupt`; исключение — бросается."""

    def __init__(self, options, script, fail=None):
        self.options = options
        self.script = list(script)
        self._messages = iter(self.script)
        self.fail = fail
        self.decisions = []
        self.queries = []
        self.connected = self.interrupted = self.disconnected = False
        self._wake = asyncio.Event()
        self._asks = itertools.count(1)

    async def connect(self):
        if self.fail is not None:
            raise self.fail
        self.connected = True

    async def query(self, prompt):
        self.queries.append(prompt if isinstance(prompt, str) else [m async for m in prompt])

    async def receive_response(self):
        for item in self._messages:
            await asyncio.sleep(0)
            if self.interrupted:
                break
            if isinstance(item, BaseException):
                raise item
            if item == HOLD:
                await self._wake.wait()
                break
            if isinstance(item, tuple) and item[0] == "ask":
                tool, data, suggestions = item[1:4]
                tid = item[4] if len(item) > 4 else f"toolu_{next(self._asks)}"
                ctx = ToolPermissionContext(suggestions=list(suggestions), tool_use_id=tid)
                self.decisions.append(await self.options.can_use_tool(tool, data, ctx))
                continue
            yield item
            if isinstance(item, ResultMessage):
                return
        if self.interrupted:
            yield result("error_during_execution", is_error=True)

    async def interrupt(self):
        self.interrupted = True
        self._wake.set()

    async def disconnect(self):
        self.disconnected = True


class FakeFactory:
    """Фабрика клиентов: помнит выданные опции и клиентов."""

    def __init__(self, script, fail=None):
        self.script = script
        self.fail = fail
        self.options = []
        self.clients = []

    def __call__(self, options):
        self.options.append(options)
        self.clients.append(FakeClient(options, self.script, self.fail))
        return self.clients[-1]
