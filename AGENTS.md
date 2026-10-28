# AGENTS.md — studio-agent

Windows-мост: отдаёт студии `studio.example.com` локальный Claude Code владельца по протоколу v1. Полные правила —
[CLAUDE.md](CLAUDE.md).

## Ключевые файлы

- Протокол (контракт): `C:/work/projects/studio/docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md`
- Спека: `C:/work/projects/studio/docs/superpowers/specs/2026-10-28-studio-claude-code-design.md`
- План: `C:/work/projects/studio/docs/superpowers/plans/2026-10-28-studio-agent-bridge.md`
- Эталон машины для сервера: `C:/work/projects/studio/tests/fake_bridge.py` (`FakeMachine`)
- Пределы — `studio_agent/limits.py`, слова отказов — `studio_agent/errors.py`

## Ловушки

- Только Windows: DPAPI (`studio_agent/dpapi.py`) — через ctypes, область пользователя; блоб другой учётки не читается.
- Журнал `agent.log` — только id, коды, числа, имена типов исключений. Ни ключа, ни PIN, ни путей, ни текстов.
- Счёт неверных PIN пишется на диск **до** scrypt — иначе обрыв посреди проверки даёт лишнюю попытку.
- Открытые сессии — только в памяти; любой новый канал их сбрасывает.
- Кадры — `json.dumps(..., ensure_ascii=True, allow_nan=False)`: имена файлов Windows бывают с одинокими суррогатами.
- Блокирующее — `asyncio.to_thread`, иначе встают пинги канала.
- Тесты без реального времени: часы и паузы — параметрами (`now`, `sleep`).
- `.ps1` — UTF-8 с BOM и CRLF; `.py` — UTF-8 без BOM и LF.
- Пароль студии — не в argv и не на диске.
- Push — только по слову владельца.
