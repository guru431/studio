# AGENTS.md — компонент agent проекта studio

Windows-мост: отдаёт студии `studio.example.com` локальные Claude Code, Codex и OpenCode по протоколу v1. Часть репозитория studio.
Общие правила — [../CLAUDE.md](../CLAUDE.md), устройство моста — [CLAUDE.md](CLAUDE.md), установка — [README.md](README.md).

## Ключевые файлы

- Протокол (контракт): `../docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md`
- Спека: `../docs/superpowers/specs/2026-10-28-studio-claude-code-design.md`
- Эталон машины для сервера: `../tests/fake_bridge.py` (`FakeMachine`)
- Ядро: `studio_agent/link.py` (канал), `agent.py` (просьбы, PIN), `turns.py` (ход через Agent SDK), `app.py` (`Controller`)
- Codex: `codex.py` (каталог, чаты, ходы), `codex_rpc.py` (локальный app-server по stdio); соответствие UUID — `codex-chats.json`.
- OpenCode: `opencode.py`, `opencode_rpc.py` (ACP по stdio); соответствие UUID — `opencode-chats.json`, настройки — `opencode/choices.json`.
- Пределы — `studio_agent/limits.py`, слова отказов — `studio_agent/errors.py`
- Сквозная проверка с живой студией — `tests/test_live_studio.py` (`manual`, тратит квоту)

## Ловушки

- Только Windows: DPAPI (`studio_agent/dpapi.py`) — через ctypes, область пользователя; блоб другой учётки не читается.
- Ключ устройства — только заголовком `Authorization`; логгер `websockets` не ниже INFO (на DEBUG пишет ключ).
- PIN проверяет мост; счёт неверных пишется на диск **до** scrypt. Открытые сессии — в памяти, переподключение сохраняет срок простоя 3 часа; явная настройка сбрасывает доступ.
- Журнал `agent.log` — только id, коды, числа, имена типов исключений. Ни ключа, ни PIN, ни путей, ни текстов.
- Кадры — `json.dumps(..., ensure_ascii=True, allow_nan=False)`: имена файлов Windows бывают с одинокими суррогатами.
- Ход: клиент SDK на ход, `setting_sources=["user", "project", "local"]`; `permission_mode` и sandbox меняются только явным выбором прав в студии.
- Блокирующее — `asyncio.to_thread`, иначе встают пинги канала.
- Закрытие/фон/блокировка Студии снимают только наблюдателя, задания продолжаются. Выкладка сервера не требует перезапуска моста.
- Не завершать процесс работающего моста из его задания (в том числе Stop-Process/kill): это убивает исполнителя. Обновлять мост после завершения заданий и явного выхода в трее.
- Тесты без реального времени: часы и паузы — параметрами (`now`, `sleep`).
- `.ps1` — UTF-8 с BOM и CRLF; `.py` — UTF-8 без BOM и LF.
- Пароль студии — не в argv и не на диске. Находки — в `../FINDINGS.md`, идеи — в `../IDEAS.md`.
