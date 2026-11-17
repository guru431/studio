# Мост macOS

С Studio 0.22.0 и Studio Agent 0.29.0 общий мост работает на Windows и macOS Intel/Apple Silicon.
Протокол v1, PIN, исходящий WebSocket, ограничения доступа и запрет записи личных данных в журнал
совпадают с [общим мостом](agent-bridge.md). Совместимость старых Windows-мостов сохраняется.

`GET /api/agent/download?platform=macos` отдаёт вошедшему пользователю тот же проверяемый ZIP,
который получает автообновление; в нём есть `install.command` и `install_macos.py`. Прежний URL
без параметра отдаёт Windows EXE. Секреты устройства по-прежнему передаются только в заголовке.
Необязательное поле `hello.platform` (`darwin`/`win32`) помогает выбрать установщик машины на связи.

## Установка и системные границы

- Python 3.11+ с Tk, git и отдельно настроенные CLI. LaunchAgent добавляет обычные пути Homebrew,
  `~/.local/bin`, `~/.cargo/bin`, `~/.opencode/bin`; команды запускаются списками аргументов без shell.
- Настройки: `~/Library/Application Support/Studio Agent`. Приложение:
  `~/Applications/Studio Agent.app`. Выпуски: `app/releases/release-*` внутри каталога настроек;
  приложение открывает `app/current`. Среды venv создаются сразу по окончательному пути.
- Ключ Fernet лежит в Keychain под сервисом `com.studio.agent.secrets.v1`, запись привязана к
  настоящему пути каталога настроек. `key.bin` и `pin.bin` содержат только аутентифицированный
  шифротекст. Ошибка или блокировка Keychain не создаёт замену существующего ключа. Пароль Studio
  не сохраняется. Перенос конфигурации на другую машину требует новой привязки и задания PIN.
- Один экземпляр на каталог настроек: `flock`, автоматически снимаемый ОС. Установщик использует
  тот же замок и отказывает при работающем мосте. Автозапуск — пользовательский Aqua LaunchAgent
  `~/Library/LaunchAgents/com.studio.agent.plist`, без `KeepAlive` и остановки текущего процесса.
- Tk и значок строки меню используют главный цикл macOS (`pystray.run_detached`).
- Codex/OpenCode и установщики CLI получают отдельную POSIX-группу процессов; закрытие клиента
  завершает также его дочерние процессы. Claude Code сохраняет штатный жизненный цикл SDK.
- Удалённый рабочий стол — только Windows: Mac сообщает `desktop: false`, локальный переключатель скрыт.
- Intel использует `cryptography==48.0.1` с готовым universal2-пакетом; Windows и Apple Silicon —
  основную версию из `constraints.txt`. Зависимости Cocoa/Quartz также закреплены.

Обновление проверяет размер и SHA-256, готовит отдельную среду, ждёт простоя и штатного выхода.
Хелпер stdlib ждёт прежний PID, запускает новую копию и переключает прежний LaunchAgent и `current`
лишь после подтверждения готовности. При раннем падении новой копии запускается прежняя.

## Проверки

Windows: команды наборов `main` и `agent` из корневого `CLAUDE.md` и `agent/CLAUDE.md`.
На macOS команды выполняются из `agent/`; временные каталоги и кэш задаются внутри `.tmp/<task>/`.

Быстрый переносимый набор (Python на Mac):

```sh
.venv/bin/python -m pytest tests/test_macos.py tests/test_store.py tests/test_pin.py tests/test_link.py tests/test_files.py tests/test_file_operations.py tests/test_gitinfo.py tests/test_cli.py tests/test_constraints.py tests/test_codex_rpc.py tests/test_opencode_rpc.py -q
```

Системная приёмка без LLM-квоты:

```sh
.venv/bin/python -m pytest tests/test_macos_integration.py tests/test_macos.py -q -m integration
```

Keychain проверяется отдельной временной связкой, которая удаляется в `finally`; окно и строка меню
проверяются в графическом сеансе. Отдельная проверка реальной генерации требует авторизованных CLI
и выполняется только по согласованию владельца.
