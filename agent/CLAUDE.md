# CLAUDE.md — компонент agent проекта studio

Мост Windows/macOS/Linux к студии `https://studio.example.com`: держит исходящий канал WebSocket к студии и отдаёт ей локальные
Claude Code, Codex, OpenCode и DeepSeek Harness владельца — проекты, чаты, ход с карточками разрешений, файлы, git — строго по протоколу
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
отдаётся `studio-agent-setup.exe` для Windows: сборка — `../deploy/build-agent-installer.ps1`, оболочка —
`../deploy/agent-installer.cs` (временная распаковка ZIP, `install.ps1`, уборка только своей папки); запускатель
`Studio Agent.exe` (с 0.47.0) — `../deploy/agent-launcher.cs`, сборка `../deploy/build-agent-launcher.ps1`, в ZIP его
кладёт `../deploy/build-agent-bundle.py` (в git-копии его нет). Windows требует
Python 3.11+ и интернет для pip; macOS получает автономный `studio-agent-setup.pkg` со встроенными Python/Tk и wheels
(`../deploy/build-agent-macos.py`). Права администратора не нужны. Обновления моста получают ZIP. Находки и идеи — в корневых
`FINDINGS.md` и `IDEAS.md` (вне Git: след закрытия — сообщение коммита с заголовком записи). Отдельного проекта и
remote у компонента нет. Версия — `studio_agent/__init__.py::VERSION`.

## Карта пакета `studio_agent`

| Область | Модули |
|---|---|
| Канал и просьбы | `link` (WebSocket, знакомство, переподключение), `agent` (просьбы, проверка полей, открытость сессий), `events` (урезка событий), `errors` (`Refused`), `limits` (все числа протокола и моста) |
| Безопасность на машине | `dpapi`, `pin` (`PinStore`, `OpenSessions`), `store` (`config.json`, `key.bin`, `choices.json`), `journal` (`agent.log`), `single` (мьютекс), `enroll`, `setup`, `autostart` |
| Claude Code | `cli` (`claude.exe`, версия, модели, окружение), `claude_sdk` (клиент SDK без окон консоли), `sessions` (чаты, история, `fit`), `turns` (`Runner`: ход, карточки, остановка), `branches`, `subagents` |
| Codex, OpenCode | `codex`, `codex_rpc`, `codex_progress`, `acp` (общая основа помощников ACP: процесс, сессии, ход, карточки), `opencode`, `opencode_rpc`, `opencode_events` (метки детей и статусы), `opencode_native` (ветки перед сообщением и дети через защищённый loopback API), `chrome` (Chrome DevTools MCP), `permissions`, `jobs` (Job Object) |
| DeepSeek Harness | `deepseek` (`DeepSeekRunner` поверх `acp`: `session/resume`, права только у нового чата, пробные сессии во временном хранилище), `deepseek_rpc` (установки npm, Python-пакета и Desktop, запуск `--profile acp`), `deepseek_settings` (наложение профиля Web/Desktop `--patch`, `sweep`), `deepseek_history` (история из файлов сессий v4, `.jsonl[.zstd]`), `deepseek_usage` (баланс DeepSeek) |
| Ход: ввод и вывод | `timeline`, `image_input`, `attachments`, `diffs`, `discovery` |
| Проекты и файлы | `projects` (`Catalog`, `pid`), `files`, `skills` (навыки проекта `SKILL.md`), `file_stats`, `worktrees`, `gitinfo`, `github_status`, `remote_status`, `project_summary`, `metadata` (кэш), `subscription` и `opencode_usage` (лимиты), `visualizations` |
| Компьютер | `computer_info`, `selfcheck` (самопроверка моста), `desktop`, `desktop_native` (Windows), `desktop_macos` (macOS) |
| Обновление | `updater`, `update_helper` (stdlib-хелпер), `winacl` (ACL каталогов Windows), `shortcut` (ярлык меню «Пуск»), `install.ps1`, `install.cmd` |
| Оболочка | `app` (`Controller` без Tk), `window`, `tray`, `theme`, `i18n` (язык окна, трея и консоли), `__main__` (консольные команды), `transfer` (перенос на новый компьютер: `export-chats`/`import-chats`), `studio_agent.pyw` |

## Правила

- Закрытие/фон/блокировка Студии снимают наблюдателя, но не останавливают ход. Выкладка сервера не требует перезапуска
  Windows-моста. Не применять Stop-Process/kill к активному мосту, особенно из выполняемого им задания: это уничтожает
  исполнителя и его CLI. Ручная установка — после завершения заданий и выхода в трее; автоматическое обновление через
  `updater.py` само дожидается простоя и просит штатный выход GUI.
- Windows: ключ устройства и PIN защищены DPAPI пользователя. macOS: Fernet с ключом в Keychain пользователя;
  пароль студии на машине не остаётся и в argv не передаётся (только окно или `getpass`).
- macOS Intel/Apple Silicon: один `studio-agent-setup.pkg` со встроенным Python/Tk и wheels → `install_macos.py`,
  сборка `../deploy/build-agent-macos.py`; `install.command` оставлен для разработчиков. Приложение `/Applications/Studio Agent.app`
  или `~/Applications/Studio Agent.app`, если общая папка недоступна для записи,
  настройки `~/Library/Application Support/Studio Agent`, автозапуск LaunchAgent. [Контракт и проверки](../docs/architecture/agent-macos.md).
- Протокол v1, значения дословно: кадр ≤ 48 МиБ; первый кадр — `hello` с `"v": 1`; `welcome` ждать ≤ 10 с; закрытия
  4401 (отозван — не переподключаться), 4409 (заменён — не переподключаться), 4400 (пауза 60 с и снова),
  4403 (учётка отключена, с 0.42.0 — ходы и открытые сессии прочь, переподключение с паузами `BACKOFF`),
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
- Пределы: текст редактора 2 МиБ, HTML-визуализация 10 МиБ, `raw` 10 МиБ, вложений хода — `limits.IMAGE_COUNT` (12)
  изображений и `limits.ATTACHMENT_COUNT` (12) всего (объявляет `hello.limits`; мост до 0.48.0 — 4 и 8), файл навыка
  проекта — `limits.SKILL_MAX` (65536) байт, навыков в списке — `limits.SKILLS_MAX` (100), путь ≤ 1024 знака, `history.limit` 1–100, строки событий и шагов — до
  20 000 знаков с `…` в конце, страница истории — ≤ 512 КиБ, снимок — ≤ `limits.HISTORY_BUDGET` (8 МиБ), файлов в
  списке ≤ 5000, чатов ≤ 200, файлов git ≤ 2000, одновременных ходов — `Config.max_turns` (по умолчанию 12).
  С 0.26.1 запуск однократно переводит прежние лимиты на 12 (`store.migrate_config`, `Config.max_turns_revision`);
  последующие ручные настройки сохраняются.
- `pid` = первые 16 знаков hex sha256 имени папки в исходном корне; в дополнительных корнях — полного нормализованного
  настоящего пути корня + NUL + имени. Исходный корень хранится отдельно для стабильности id; корни хранятся
  `realpath`-ом.
- Кадры наружу — `json.dumps(frame, ensure_ascii=True, allow_nan=False)` (имена файлов Windows бывают с одинокими
  суррогатами).
- **Журнал** `%LOCALAPPDATA%\studio-agent\agent.log` — только id, коды, числа, имена типов исключений: ни ключа, ни PIN,
  ни пароля, ни имён проектов, файлов, путей, ни текстов, ни stderr Claude Code.
- **Внешние адреса** — кроме студии, только `limits.HOSTS` (без перенаправлений на другие адреса): `api64.ipify.org`
  (внешний IP, без данных); `registry.npmjs.org`, `marketplace.visualstudio.com`, `formulae.brew.sh`,
  `downloads.claude.ai` (версии программ: имя пакета); `api.anthropic.com` (лимиты Claude: OAuth-токен Claude Code);
  `https://opencode.ai` (лимиты OpenCode Go: ключ OpenCode); `api.deepseek.com` (баланс DeepSeek: ключ DeepSeek, только при официальном провайдере и без своего `DEEPSEEK_BASE_URL`); `github.com` (публичная копия: `owner/repo`, без учётных данных).
  Плюс шлюз OpenCode из конфига пользователя (`opencode_usage.connection`) и штатные установщики CLI. Новый адрес — в
  константу и в перечни обоих CLAUDE.md: их и литералы `https://` кода сверяет `tests/test_docs.py`.
- Блокирующее (файлы, scrypt, сессии SDK, git) — `asyncio.to_thread`: цикл держит пинги канала.
- Git проекта — недоверенный: `gitinfo.SAFE`, фильтры репозитория выключены (`gitinfo._filters`), хуки не запускаются,
  частичный клон не дозагружает объекты (`gitinfo.OFFLINE`); исключение — `worktree add` по действию владельца.
- Автозапуск Windows — значение `StudioAgent` в `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`; Task Scheduler не трогать.
  Ярлык «Studio Agent» меню «Пуск» (`%APPDATA%`) — та же команда; тесты пишут его во временную `APPDATA` (conftest).
  Команда — `Studio Agent.exe` копии только после пробы (`update_helper.launcher`), иначе `pythonw.exe`; откат на
  `pythonw.exe` при пропаже exe — `autostart.repair` (поток `autostart.watch`; у моста от exe ещё
  `autostart.follow_launcher` — exe без Job Object, мост переживает его завершение антивирусом). Тесты реестра — тестовый ключ или
  подставной `winreg`, настоящий `Run` не трогают.
  macOS — `~/Library/LaunchAgents/com.studio.agent.plist`, без принудительного перезапуска.
- Каталоги, которые читает обычный сеанс, — `host.private_dir`, `host.new_dir`, `host.temporary_dir`, не `mode=0o700` и
  не `tempfile.mkdtemp`/`TemporaryDirectory`: на Windows режим 0o700 даёт DACL «по владельцу», и после запуска с
  повышением обычный сеанс теряет доступ (`winacl`; сторож — `tests/test_private_acl.py`). Файлы так не страдают.
- Кодировки: `.py` — UTF-8 без BOM, LF; `.ps1` — UTF-8 **с BOM**, CRLF (`.gitattributes`).
- Комментарии, докстринги и тексты — по-русски, как в студии; слова отказов и интерфейса — дословно из «Тексты» плана.
  Подписи окна, трея, меню и консоли показываются через `i18n.tr` с английским переводом в `i18n.EN` (язык системы,
  `STUDIO_AGENT_LANG`; сторож — `tests/test_i18n.py`); отказы `Refused` и журнал не переводятся.
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

`tests/test_deepseek_live.py` — один короткий ответ настоящего DeepSeek Harness, затем история из файлов сессий, чаты и
состояние: `.venv/Scripts/python -m pytest tests/test_deepseek_live.py -q -m manual --durations=5`. Временные проект и
`DSH_HOME`; провайдер — сторонний OpenAI-совместимый из окружения теста: `DSH_LIVE_BASE_URL`, `DSH_LIVE_MODEL`,
`DSH_LIVE_API_KEY` (ключ только окружением, DSH читает его по `apiKeyEnv`). `DSH_LIVE_PROFILE` выбирает установку
`web` или `desktop`, по умолчанию проверяется профиль найденной установки. `DSH_LIVE_SHELL=1` дополнительно проверяет
вызов `bash` на macOS/Linux без записи файлов. `DSH_TEST_PACKAGE` — каталог пакета
`@deepseek-ai/dsh`, если DSH не установлен глобально. Без настроек провайдера — пропуск. Node DSH не принимает
самоподписанный сертификат без имени адреса — такому шлюзу нужен его HTTP-порт частной сети.

`timeout = 30`, `asyncio_mode = auto`; pytest-xdist нет — набор идёт одним процессом.
Временный корень — `.tmp/.pytest_cache/agent` внутри корня Studio (вне синхронизации), успешные временные каталоги удаляются; явные `--basetemp`
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
  `set-pin`/`clear-pin` из консоли Windows/macOS пишут `pin.bin` мимо работающего моста: на новые `open` действуют
  сразу, а уже открытые сессии живут до простоя (`idle_sec`, до 7 суток), `close`, `restart_link` или перезапуска
  моста — переподключение (`Agent.new_link(reconnect=True)`) отметки сохраняет; `has_pin` студии — тоже до
  перезапуска или `restart_link` (переподключение самого `Link` берёт `has_pin` из памяти). Окно и консоль Linux
  (`linux_menu.invoke`) идут через `Controller.set_pin`: он сам делает `restart_link` и сбрасывает открытые сессии.
- **Кадры — ASCII-JSON** (`ensure_ascii=True, allow_nan=False`): имя файла Windows с одиноким суррогатом в UTF-8 не
  кодируется — канал упал бы на первом таком списке.
- **`setting_sources` задавать явно**: без него SDK не грузит настройки — ни правил разрешений проекта, ни CLAUDE.md,
  ни `env` `claude-switch`. Режим разрешений — из настроек: у владельца в `~/.claude/settings.json` `defaultMode:
  "auto"` и `allow: ["Bash", …]`, поэтому карточка приходит только на то, что Claude Code спросил бы и в VS Code
  (живой тест кладёт проекту `defaultMode: "default"` и `ask` — проектная настройка сильнее пользовательской).
  Фоновое чтение состояния (`Runner.state`) идёт с `turns.NO_HOOKS` (`--settings {"disableAllHooks": true}`): по
  документации Claude Code SessionStart исполняется при начале или продолжении сессии, а не при первом запросе, а
  `--settings` сильнее project/local — хуки скопированной папки не запускаются, модель и режим читаются как прежде.
  Ход хуки сохраняет. Управляемые (managed) хуки так не гасятся; живьём не проверено (подписка остановлена).
  Там же `strict_mcp_config` (`--strict-mcp-config`): по документации Claude Code (MCP, «project-scoped servers»)
  в Agent SDK и `-p` серверы `.mcp.json` проекта поднимаются без одобрения, а stdio-сервер — это запуск команды
  папки при подключении. Чтение берёт только конфиг владельца (`mcp.claude_config` → `--mcp-config`); счёт контекста
  поэтому без инструментов `.mcp.json`. Ход их по-прежнему поднимает — в SDK без одобрения, в отличие от VS Code:
  решение владельца 2026-11-19 — ход, как и хуки, остаётся действием владельца в своём проекте, не ограничивать.
- **Клиент на ход, не на чат**: держать клиентов открытыми — процесс `claude.exe` на каждый открытый чат. `can_use_tool`
  требует потокового ввода и холостого хука `PreToolUse` (`turns._keep_alive`) — без него SDK закрывает ввод, пока
  карточка ждёт ответа.
- stderr `claude.exe` отбрасывается (`cli._drop`): в нём пути и тексты.
- Сессии Claude Code пишутся в настоящий `~/.claude/projects` (по пути проекта) — у тестов с настоящим CLI тоже.
- `claude.exe` не в Job Object (`jobs`): его фоновые задачи переживают ход; `codex app-server`, `opencode acp` и `dsh --profile acp` — в нём.
- Файлы DeepSeek Harness (`$DSH_HOME/profiles`, `cordis.patch.yml`, `.credentials.yaml`, `sessions/`) мост только
  читает; каталог моделей — пробной сессией с хранилищами во временном каталоге моста (у ACP DSH сессия
  материализуется сразу; строки хранилищ — при любом исходе разбора профиля, без них проба не запускается);
  наложение — временный JSON `<мост>/deepseek/overlay-*`, остатки стирает `deepseek_settings.sweep` при старте ядра;
  процесс списка чатов (`session/list`) — без наложения. Сам DSH при запуске создаёт `profiles/acp/` и `.anonymous-user-id` в `DSH_HOME`.
