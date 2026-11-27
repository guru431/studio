"""Лимиты помощника: только владелец компьютера и открытая PIN-сессия моста."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field, ValidationError

import bridge
import claude_code

router = APIRouter(prefix="/api/code")


class UsageWindow(BaseModel):
    id: str = Field(max_length=150)
    label: str = Field(max_length=200)
    available: bool
    used_percent: float | None = Field(None, ge=0, le=100, allow_inf_nan=False)
    remaining_percent: float | None = Field(None, ge=0, le=100, allow_inf_nan=False)
    window_minutes: float | None = Field(None, ge=0, allow_inf_nan=False)
    resets_at: float | None = Field(None, ge=0, allow_inf_nan=False)
    reason: str | None = Field(None, max_length=300)


class UsageDay(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    tokens: float = Field(ge=0, allow_inf_nan=False)


class UsageSummary(BaseModel):
    available_resets: int | None = Field(None, ge=0, strict=True)
    lifetime_tokens: float | None = Field(None, ge=0, allow_inf_nan=False)
    peak_daily_tokens: float | None = Field(None, ge=0, allow_inf_nan=False)
    longest_running_turn_sec: float | None = Field(None, ge=0, allow_inf_nan=False)
    current_streak_days: float | None = Field(None, ge=0, allow_inf_nan=False)
    longest_streak_days: float | None = Field(None, ge=0, allow_inf_nan=False)
    total_sessions: float | None = Field(None, ge=0, allow_inf_nan=False)
    total_messages: float | None = Field(None, ge=0, allow_inf_nan=False)


class ResetCredit(BaseModel):
    expires_at: float | None = Field(None, ge=0, le=253402300799, allow_inf_nan=False, strict=True)


class ClaudeResetGrant(ResetCredit):
    kind: Literal["session", "weekly", "full", "fable", "other"]
    count: int = Field(ge=1, le=100, strict=True)
    reset_key: UUID | None = None


class UsageStats(BaseModel):
    available: bool
    source: str = Field(max_length=200)
    summary: UsageSummary
    daily: list[UsageDay] = Field(max_length=366)
    reset_credits: list[ResetCredit] | None = Field(None, max_length=100)
    reason: str | None = Field(None, max_length=300)


class BalanceItem(BaseModel):
    currency: str = Field(min_length=1, max_length=8)
    total: float | None = Field(None, ge=0, allow_inf_nan=False)
    granted: float | None = Field(None, ge=0, allow_inf_nan=False)
    topped_up: float | None = Field(None, ge=0, allow_inf_nan=False)


class UsageBalance(BaseModel):
    """Баланс DeepSeek по валютам (у DSH нет окон лимита); `reason` — почему его не узнать."""
    available: bool
    items: list[BalanceItem] = Field(max_length=10)
    reason: str | None = Field(None, max_length=300)


class Usage(BaseModel):
    engine: Literal["claude", "codex", "opencode", "deepseek"]
    available: bool
    source: str = Field(max_length=200)
    checked_at: float = Field(ge=0, allow_inf_nan=False)
    cache_ttl: int = Field(ge=0, le=3600)
    plan: str | None = Field(None, max_length=64)
    windows: list[UsageWindow] = Field(max_length=50)
    statistics: UsageStats
    message: str | None = Field(None, max_length=300)
    balance: UsageBalance | None = None
    claude_resets: list[ClaudeResetGrant] | None = Field(None, max_length=50)


LINKS = {
    "claude": [{"label": "Открыть лимиты и сбросы в Claude", "url": "https://claude.ai/settings/usage"}],
    "codex": [{"label": "Использование Codex", "url": "https://chatgpt.com/codex/settings/usage"}],
    "opencode": [{"label": "Провайдеры OpenCode", "url": "https://opencode.ai/docs/providers/"}],
    "deepseek": [{"label": "Баланс DeepSeek", "url": "https://platform.deepseek.com/usage"},
                 {"label": "Пополнить DeepSeek", "url": "https://platform.deepseek.com/top_up"}],
}
#: помощник → id модели, для которой мост считает лимиты, и признак такого моста в `hello`
MODELS = {"opencode": (bridge.OPENCODE_MODEL_ID, "opencode_usage"), "deepseek": (bridge.DEEPSEEK_MODEL_ID, "deepseek_usage")}


class ResetRequest(BaseModel):
    idempotency_key: UUID
    engine: Literal["claude", "codex"] = "codex"


class ResetResult(BaseModel):
    outcome: Literal["reset", "alreadyRedeemed", "nothingToReset", "noCredit", "stale", "blocked"]


@router.post("/agents/{aid}/usage/reset")
async def reset_usage(aid: int, body: ResetRequest, request: Request):
    claude_code.own_agent(request, aid)
    if body.engine == "codex":
        claude_code.require_engine(aid, "codex")
    link = bridge.LINKS.get(aid)
    capability = "claude_usage_reset" if body.engine == "claude" else "usage_reset"
    if link is not None and not link.hello.get(capability):
        raise claude_code._refuse(409, "unsupported", "обновите мост для сброса лимита")
    raw = await claude_code.ask(request, aid, "usage_reset", engine=body.engine, idempotency_key=str(body.idempotency_key))
    try:
        return ResetResult.model_validate(raw).model_dump()
    except ValidationError:
        raise claude_code._refuse(502, "bridge", "мост ответил непонятно") from None


@router.get("/agents/{aid}/usage")
async def usage(aid: int, request: Request, engine: str = "claude", refresh: bool = False, model: str | None = None):
    claude_code.own_agent(request, aid)
    if engine not in LINKS:
        raise claude_code._refuse(422, "engine", "неверный помощник")
    if model is not None and (engine not in MODELS or not MODELS[engine][0].fullmatch(model)):
        raise claude_code._refuse(422, "model", "неверная модель")
    link = bridge.LINKS.get(aid)
    if link is not None and not link.hello.get("subscription_usage"):
        raise claude_code._refuse(409, "unsupported", "обновите мост для просмотра лимитов")
    if engine != "claude":
        claude_code.require_engine(aid, engine)
    extra = {"model": model} if model and link is not None and link.hello.get(MODELS[engine][1]) else {}
    raw = await claude_code.ask(request, aid, "usage", engine=engine, refresh=refresh, **extra)
    try:
        value = Usage.model_validate(raw)
        if value.engine != engine:
            raise ValueError()
    except (ValidationError, ValueError):
        raise claude_code._refuse(502, "bridge", "мост ответил непонятно") from None
    return {**value.model_dump(mode="json", exclude_unset=True,
                               exclude={"claude_resets"} if engine != "claude" else None), "links": LINKS[engine]}
