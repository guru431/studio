# CLAUDE.md — компонент agent проекта studio

Windows-мост к студии `https://studio.example.com`: держит исходящий канал WebSocket к студии и отдаёт ей локальные
Claude Code, Codex и OpenCode владельца — проекты, чаты, ход с карточками разрешений, файлы, git — строго по протоколу
версии 1. Как ставить и пользоваться — [README.md](README.md); устройство подробно, решения плана и таблица
capability → версия моста — [docs/architecture/agent-bridge.md](../docs/architecture/agent-bridge.md).

- Спека: [2026-10-28-studio-claude-code-design](../docs/superpowers/specs/2026-10-28-studio-claude-code-design.md)
  (§2, §4, §6.3, §6.6, §6.10, §7, §8); окно, трей, отвязка и установка из архива студии —
  [2026-10-28-studio-agent-download-and-window-design](../docs/superpowers/specs/2026-10-28-studio-agent-download-and-window-design.md) (§5).
- Протокол (контракт с сервером): [2026-10-28-studio-code-bridge-protocol](../docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md).
- План: [2026-10-28-studio-agent-bridge](../docs/superpowers/plans/2026-10-28-studio-agent-bridge.md).
- Сервер — `app/bridge.py`, `app/claude_code.py`; эталон поведения машины — `tests/fake_bridge.py` (`FakeMachine`).

Исходники и Git-история — часть репозитория `studio`, каталог `agent/` (с 2026-10-30): `origin` приватный, но основная
ветка после очистки публикуется на GitHub — в отслеживаемые файлы только примеры. Общие правила —
[../CLAUDE.md](../CLAUDE.md); сервер и мост меняются одним коммитом, ZIP собирается из `HEAD:agent`. Браузеру
отдаётся один `studio-agent-setup.exe`, содержащий этот ZIP: сборка — `../deploy/build-agent-installer.ps1`, оболочка —
`../deploy/agent-installer.cs` (временная распаковка, `install.ps1`, уборка только своей папки). Права администратора
не нужны; Python 3.11+ и интернет для pip нужны. Обновления моста получают ZIP. Находки и идеи — в корневых
`FINDINGS.md` и `IDEAS.md` (вне Git: след закрытия — сообщение коммита с заголовком записи). Отдельного проекта и
remote у компонента нет. Версия — `studio_agent/__init__.py::VERSION`.

## Карта пакета `studio_agent`

| Область | Модули |
|---|---|
| Канал и просьбы | `link` (WebSocket, знакомство, переподключение), `agent` (просьбы, проверка полей, открытость сессий), `events` (урезка событий), `errors` (`Refused`), `limits` (все числа протокола и моста) |
| Безопасность на машине | `dpapi`, `pin` (`PinStore`, `OpenSessions`), `store` (`config.json`, `key.bin`, `choices.json`), `journal` (`agent.log`), `single` (мьютекс), `enroll`, `setup`, `autostart` |
| Claude Code | `cli` (`claude.exe`, версия, модели, окружение), `claude_sdk` (клиент SDK без окон консоли), `sessions` (чаты, история, `fit`), `turns` (`Runner`: ход, карточки, остановка), `branches`, `subagents` |
| Codex, OpenCode | `codex`, `codex_rpc`, `codex_progress`, `opencode`, `opencode_rpc`, `chrome` (Chrome DevTools MCP), `permissions`, `jobs` (Job Object) |
| Ход: ввод и вывод | `timeline`, `image_input`, `attachments`, `diffs`, `discovery` |
| Проекты и файлы | `projects` (`Catalog`, `pid`), `files`, `file_stats`, `worktrees`, `gitinfo`, `github_status`, `remote_status`, `project_summary`, `metadata` (кэш), `subscription` (лимиты), `visualizations` |
| Компьютер | `computer_info`, `desktop`, `desktop_native` |
| Обновление | `updater`, `update_helper` (stdlib-хелпер), `install.ps1`, `install.cmd` |
| Оболочка | `app` (`Controller` без Tk), `window`, `tray`, `theme`, `__main__` (консольные команды), `studio_agent.pyw` |

## Правила

- Закрытие/фон/блокировка Студии снимают наблюдателя, но не останавливают ход. Выкладка сервера не требует перезапуска
  Windows-моста. Не применять Stop-Process/kill к активному мосту, особенно из выполняемого им задания: это уничтожает
  исполнителя и его CLI. Ручная установка — после завершения заданий и выхода в трее; автоматическое обновление через
  `updater.py` само дожидается простоя и просит штатный выход GUI.
- Только Windows. Ключ устройства и PIN — DPAPI области пользователя; пароль студии на машине не остаётся и в argv не
  передаётся (только окно или `getpass`).
- Протокол v1, значения дословно: кадр ≤ 16 МиБ; первый кадр — `hello` с `"v": 1`; `welcome` ждать ≤ 10 с; закрытия
  4401 (отозван — не переподключаться), 4409 (заменён — не переподключаться), 4400 (пауза 60 с и снова),
  1012/1001/обрыв/отказ рукопожатия — переподключение 1, 2, 4, 8, 16, 32, 60 с; `id` просьбы — 16 hex, `session` —
  32 hex, `project` — 16 hex, `chat` — UUID строчными 8-4-4-4-12; `message` отказа ≤ 300 знаков; студия ждёт ответа 30 с.
- PIN — 4–8 цифр ASCII, scrypt (`n=2**14, r=8, p=1`, соль 16 байт, `dklen=32`); 5 неверных подряд — `pin_locked` до
  снятия блока на машине; счёт — на диске, попытка считается до scrypt. Открытые сессии — только в памяти, срок простоя
  по `req.idle_sec` (60–604800 с), по умолчанию `limits.OPEN_IDLE_SEC` (10800) с; `hello.pin_timeout: true`; проверка
  таймера — и на `open`, и на каждой просьбе до исполнения. Продлевают срок только принятые просьбы (после проверки `op` и
  полей) без `req.passive: true` (`hello.passive_requests`, протокол §16); `open` с `pin: null` не продлевает. Сессии
  пропадают при `close`, перезапуске моста и явной настройке (`restart_link`); переподключение и обновление каталога
  моделей сохраняют отметки без продления. Запирание студии сохраняет PIN компьютера; сервер закрывает потоки и кэши и
  блокирует запросы до отпирания.
- Пределы: текст файла 2 МиБ, `raw` 10 МиБ, путь ≤ 1024 знака, `history.limit` 1–100, строки событий и шагов — до
  20 000 знаков с `…` в конце, страница истории — ≤ 512 КиБ, снимок — ≤ `limits.HISTORY_BUDGET` (8 МиБ), файлов в
  списке ≤ 5000, чатов ≤ 200, файлов git ≤ 2000, одновременных ходов — `Config.max_turns` (по умолчанию 8).
- `pid` = первые 16 знаков hex sha256 имени папки в исходном корне; в дополнительных корнях — полного нормализованного
  настоящего пути корня + NUL + имени. Исходный корень хранится отдельно для стабильности id; корни хранятся
  `realpath`-ом.
- Кадры наружу — `json.dumps(frame, ensure_ascii=True, allow_nan=False)` (имена файлов Windows бывают с одинокими
  суррогатами).
- **Журнал** `%LOCALAPPDATA%\studio-agent\agent.log` — только id, коды, числа, имена типов исключений: ни ключа, ни PIN,
  ни пароля, ни имён проектов, файлов, путей, ни текстов, ни stderr Claude Code.
- Блокирующее (файлы, scrypt, сессии SDK, git) — `asyncio.to_thread`: цикл держит пинги канала.
- Git проекта — недоверенный: `gitinfo.SAFE`, фильтры репозитория выключены (`gitinfo._filters`), хуки не запускаются;
  исключение — `worktree add` по действию владельца.
- Автозапуск — значение `StudioAgent` в `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`; Task Scheduler не трогать.
- Кодировки: `.py` — UTF-8 без BOM, LF; `.ps1` — UTF-8 **с BOM**, CRLF (`.gitattributes`).
- Комментарии, докстринги и тексты — по-русски, как в студии; слова отказов и интерфейса — дословно из «Тексты» плана.
- Коммит — сообщение через файл (`git commit -F <файл>`), в конце `Co-Authored-By: Claude Opus 5.5
  <noreply@anthropic.com>`.

## Тесты

Контракт — `local-config/cron/project-registry.json::studio.tests` (набор `agent`, `cwd: agent` относительно корня
studio). Команды ниже — из `agent/`, с отдельной средой для Windows-моста; среда сервера в корне не меняется.

    "/c/Program Files/Python314/python" -m venv .venv
    .venv/Scripts/python -m pip install -r requirements-dev.txt
    .venv/Scripts/python -m pytest -q                       # быстрый набор, ≤ 60 с (замер 14–18 с)
    .venv/Scripts/python -m pytest -q -m integration        # настоящий git, реестр, PowerShell, установка
    .venv/Scripts/python -m pytest -q -m manual             # настоящий Claude Code, тратит квоту

`tests/test_codex_live.py` — отдельная ручная проверка настоящего Codex (два коротких ответа квоты): временный проект
только для чтения, ход → чтение состояния → продолжение того же чата. Проверяет реальную блокировку writer app-server;
созданный диагностический thread затем архивируется.

`tests/test_opencode_live.py` — один короткий ответ установленного OpenCode, затем история и состояние:
`.venv/Scripts/python -m pytest tests/test_opencode_live.py -q -m manual --durations=5`. Временный проект, без
инструментов; после проверки удаляется только созданная ею сессия. `tests/test_opencode.py` и `test_opencode_rpc.py`
проверяют ACP без CLI и квоты.

`timeout = 30`, `asyncio_mode = auto`; pytest-xdist нет — набор идёт одним процессом.
Временный корень — `%TEMP%/studio-agent-pytest`, успешные временные каталоги удаляются; явные `--basetemp`
и `PYTEST_DEBUG_TEMPROOT` сохраняются. Изоляция и два одновременных прогона — `tests/test_tmp_isolation.py`.
Хук `pytest_asyncio_loop_factories` (pytest-asyncio ≥ 1.4) выбирает общий тестовый Proactor из
`../tools/pytest_support.py`: при WinError 995 действующего внутреннего чтения IOCP чтение возобновляется.
Отмена, остальные ошибки и общий таймаут сохраняются; регрессия и условие удаления обхода —
[testing](../docs/architecture/testing.md).
`verbosity_test_cases = 1` сохраняет имя теста перед сбоем, `faulthandler_timeout = 20` выводит стеки напрямую в stderr
до завершения процесса по таймауту, в том числе при закрытии asyncio Runner. Регрессия — `tests/test_suite_timeout.py`
(`integration`, отдельный процесс с намеренно незавершаемой задачей).
Реального времени в быстрых тестах нет: часы — параметр `now`, паузы — параметр `sleep`.
Каталог моста в тестах — фикстура `home` (`tests/conftest.py`,
`STUDIO_AGENT_HOME` во временной папке). Студия в быстрых тестах — подставная (`tests/fake_studio.py`, `websockets` на
`127.0.0.1:0`), SDK — `tests/fake_sdk.py`. Окно и трей — без Tk: чистые функции `window`, `tray`, `theme`
(`test_window_logic.py`, `test_tray.py`, `test_theme.py`); снимки вида — вручную. `test_pin.py` проверяет счёт и
сохранение с уменьшенным N scrypt; отдельный `integration`-сценарий сверяет рабочие параметры и настоящий PIN после
повторного чтения. Tk-сценарии используют один корень на модуль, как приложение, очищают окна и восстанавливают DPI
после каждого случая: повторное создание интерпретаторов в одном процессе давало перемежающиеся ошибки чтения Tcl.
Геометрия окна при 100/150/200% DPI и смене темы — `tests/test_window_integration.py` (`integration`).
Установка — `tests/test_install.py` (`integration`): «архив» — рабочее дерево без `export-ignore` в папке без `.git`,
`LOCALAPPDATA` временная, `-NoVenv -NoStart` (сети нет), `-RunKey` — тестовый ключ (настоящий `Run` тесты не трогают);
работающий мост подменяет `python.exe` с путём копии. Недоверенный конфиг Git — `tests/test_git_untrusted_config.py`
(`integration`), Job Object — `tests/test_jobs.py`.

Сквозная проверка — `tests/test_live_studio.py` (`manual`, **четыре коротких хода квоты** — не гонять по кругу):
`tests/live_studio_server.py` поднимает живую студию её средой (`<STUDIO_REPO>/.venv`, по умолчанию корень studio над
`agent/`; `start_live_app`, `READY <адрес>`, гаснет по закрытию stdin), мост — настоящие `Agent`, `Runner` (SDK и
`claude.exe`) и `Link` в цикле теста, браузер — `urllib` в потоке. Сценарий: привязка, «на связи», `open`, проекты,
новый чат, ход «Ответь одним словом: готово», ход с Bash — карточка `permission`, `allow`, итог шага; чаты (`origin:
"studio"`), история из 4 сообщений, файлы, чтение, git; ход с Bash — карточка, «Остановить» (настоящий `interrupt()`) —
`done` `stopped`; следующий ход того же чата (не `running`) — вопрос `AskUserQuestion`, `answers`, в ответе выбранный
вариант; PIN на машине и новый канал — `pin_needed`, с PIN — 204. Окружение моста — как у `app.prepare_env` (тест идёт
и из Claude Code). Файл сессии пробы тест убирает из `~/.claude/projects`. Перед ним — отладить всё без хода.

## Ловушки

- **Ключ устройства — только заголовком** `Authorization: Bearer` (рукопожатие WebSocket и привязка — телом POST):
  строку запроса пишет журнал Apache. Логгер `websockets` на DEBUG пишет заголовки — с ключом; `journal.setup` держит
  его не ниже INFO.
- **PIN проверяет мост, не студия**: хеш и счёт — в `pin.bin` машины; студия знает только `has_pin` из `hello`. Значит,
  `set-pin`/`clear-pin` из консоли при работающем мосте действуют на новые `open` сразу, уже открытые сессии — до
  нового канала, а `has_pin` студии — до перезапуска моста или `restart_link` (переподключение самого `Link` берёт
  `has_pin` из памяти; окно — `Controller.set_pin` — делает `restart_link` само).
- **Кадры — ASCII-JSON** (`ensure_ascii=True, allow_nan=False`): имя файла Windows с одиноким суррогатом в UTF-8 не
  кодируется — канал упал бы на первом таком списке.
- **`setting_sources` задавать явно**: без него SDK не грузит настройки — ни правил разрешений проекта, ни CLAUDE.md,
  ни `env` `claude-switch`. Режим разрешений — из настроек: у владельца в `~/.claude/settings.json` `defaultMode:
  "auto"` и `allow: ["Bash", …]`, поэтому карточка приходит только на то, что Claude Code спросил бы и в VS Code
  (живой тест кладёт проекту `defaultMode: "default"` и `ask` — проектная настройка сильнее пользовательской).
- **Клиент на ход, не на чат**: держать клиентов открытыми — процесс `claude.exe` на каждый открытый чат. `can_use_tool`
  требует потокового ввода и холостого хука `PreToolUse` (`turns._keep_alive`) — без него SDK закрывает ввод, пока
  карточка ждёт ответа.
- stderr `claude.exe` отбрасывается (`cli._drop`): в нём пути и тексты.
- Сессии Claude Code пишутся в настоящий `~/.claude/projects` (по пути проекта) — у тестов с настоящим CLI тоже.
- `claude.exe` не в Job Object (`jobs`): его фоновые задачи переживают ход; `codex app-server` и `opencode acp` — в нём.
