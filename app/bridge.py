"""Мост Claude Code — нижний слой раздела «Claude Code» (спека 2026-10-28-studio-claude-code-design, 6.2–6.4;
кадры, коды закрытия, просьбы и пределы — протокол docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md).

- Канал: мост за NAT сам держит исходящий WebSocket `/api/agent/link` с ключом устройства в заголовке
  `Authorization` (не в строке запроса — её пишет журнал Apache); по нему студия шлёт мосту просьбы, мост —
  ответы и поток событий Claude Code. «На связи» — есть ли канал, в памяти, не в базе.
- Признак сессии студии для моста — `tag`: не дайджест сессии и тем более не токен, а производное от
  дайджеста; мост открывает компьютер PIN-ом для этого признака.
- История подключений — `agent_events`: только добавляется (`record`), «Удалить всю историю» её не трогает,
  уходит только с учёткой. Строка несёт id и имя компьютера на момент события (после отзыва строки `agents`
  нет), краткий признак устройства (`device`) и адрес — ни ключа, ни PIN, ни текстов.
"""
import hashlib

from fastapi import Request

import clock
import state


def tag(digest: str) -> str:
    """Признак сессии студии для моста: из него не получить ни дайджест, ни токен сессии."""
    return hashlib.sha256(("studio-code\0" + digest).encode()).hexdigest()[:32]


def device(request: Request) -> str:
    """С какого устройства действие: «телефон» или «браузер» по сессии запроса и начало её признака."""
    row = state.DB.execute("SELECT mobile FROM sessions WHERE digest = ?", (request.state.digest,)).fetchone()
    kind = "телефон" if row is not None and row["mobile"] else "браузер"
    return f"{kind} {tag(request.state.digest)[:6]}"


def record(user_id: int, agent_id: int, agent_name: str, kind: str, *,
           device: str | None = None, ip: str | None = None) -> None:
    """Строка истории подключений."""
    state.DB.execute("INSERT INTO agent_events(user_id, agent_id, agent_name, created, kind, device, ip)"
                     " VALUES (?, ?, ?, ?, ?, ?, ?)", (user_id, agent_id, agent_name, clock.NOW(), kind, device, ip))
