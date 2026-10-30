# AGENTS.md — studio-agent

Windows-мост: отдаёт студии `studio.example.com` локальные Claude Code и Codex по протоколу v1. Полные правила —
[CLAUDE.md](CLAUDE.md), установка и пользование — [README.md](README.md).

## Ключевые файлы

- Протокол (контракт): `C:/work/projects/studio/docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md`
- Спека: `C:/work/projects/studio/docs/superpowers/specs/2026-10-28-studio-claude-code-design.md`
- Эталон машины для сервера: `C:/work/projects/studio/tests/fake_bridge.py` (`FakeMachine`)
- Ядро: `studio_agent/link.py` (канал), `agent.py` (просьбы, PIN), `turns.py` (ход через Agent SDK), `app.py` (`Controller`)
- Codex: `codex.py` (каталог, чаты, ходы), `codex_rpc.py` (локальный app-server по stdio); соответствие UUID — `codex-chats.json`.
- Пределы — `studio_agent/limits.py`, слова отказов — `studio_agent/errors.py`
- Сквозная проверка с живой студией — `tests/test_live_studio.py` (`manual`, тратит квоту)

## Ловушки

- Только Windows: DPAPI (`studio_agent/dpapi.py`) — через ctypes, область пользователя; блоб другой учётки не читается.
- Ключ устройства — только заголовком `Authorization`; логгер `websockets` не ниже INFO (на DEBUG пишет ключ).
- PIN проверяет мост; счёт неверных пишется на диск **до** scrypt. Открытые сессии — в памяти, новый канал их сбрасывает.
- Журнал `agent.log` — только id, коды, числа, имена типов исключений. Ни ключа, ни PIN, ни путей, ни текстов.
- Кадры — `json.dumps(..., ensure_ascii=True, allow_nan=False)`: имена файлов Windows бывают с одинокими суррогатами.
- Ход: клиент SDK на ход, `setting_sources=["user", "project", "local"]`, `permission_mode` не задаётся.
- Блокирующее — `asyncio.to_thread`, иначе встают пинги канала.
- Тесты без реального времени: часы и паузы — параметрами (`now`, `sleep`).
- `.ps1` — UTF-8 с BOM и CRLF; `.py` — UTF-8 без BOM и LF.
- Пароль студии — не в argv и не на диске. Push — только по слову владельца.
