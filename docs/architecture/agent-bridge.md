# Windows-мост `agent/` (studio-agent)

Правила и карта модулей — [agent/CLAUDE.md](../../agent/CLAUDE.md); установка и пользование —
[agent/README.md](../../agent/README.md); контракт — протокол
[2026-10-28-studio-code-bridge-protocol](../superpowers/specs/2026-10-28-studio-code-bridge-protocol.md); сервер —
[agents.md](agents.md). Ссылки `` `модуль.символ` `` здесь — на `agent/studio_agent/` (сверяет `tests/test_docs.py`).

## Capability и версии моста

Необязательные поля `hello` (и другие добавки v1): с какой версии мост их объявляет, где контракт. Без capability
сервер такие просьбы не шлёт (HTTP 409 `unsupported`), страница скрывает кнопки. Версия моста —
`agent/studio_agent/__init__.py::VERSION`.

| Мост | Capability / добавка | Протокол |
|---|---|---|
| до 0.3.0 | база v1: `open`, `projects`, `chats`, `history`, `turn`, `watch`, `answer`, `stop`, `files`, `read`, `raw`, `save`, `git`; `login` в `welcome`, отвязка `DELETE /api/agent/enroll` | §1–§7 |
| 0.3.0 | `hello.codex` — Codex, `engine: codex` | §2.2 |
| 0.3.3 | `hello.images` — картинки в `turn` | §2.2 |
| 0.4.0 | `hello.chat_state` (`state`, `turn.access`), `hello.defaults`, `hello.codex.defaults` | §2.2 |
| 0.5.0 | `hello.opencode` — OpenCode; событие `activity`, `snapshot.activity` | §2.2 |
| 0.6.0 | `hello.chat_control` — `steer`, `claim`, `window`, `followup`, `ownership` | §2.2 |
| 0.7.0 | `hello.attachments`, `hello.project_activity` (`activity`), `hello.subscription_usage` (`usage`) | §2.2, §9, §10 |
| 0.7.1 | переподключение сохраняет открытые PIN-сессии без продления (без capability) | §3 |
| 0.7.3 | `hello.project_summary` | §10.1 |
| 0.7.4 | `context.usage`; `git.unpushed` без upstream | §2.2 |
| 0.7.5 | `files.items[].created` | §4 |
| 0.7.6 | `hello.file_stats` (`file_stats`); `project_name` в `ask`/`done` | §4 |
| 0.8.0 | `hello.file_suggestions`, `chat_search`, `queue_edit`, `chat_fork`, `subagents`, `worktrees` | §11 |
| 0.8.1 | `hello.file_operations` (`file_operation`) | §4 |
| 0.9.0 | `hello.pin_timeout` (`req.idle_sec`), несколько корней проектов | §12 |
| 0.9.1 | общий предел заданий `Config.max_turns` (локально, без capability) | §2.2 |
| 0.9.2 | `summary.github` | §10.1 |
| 0.10.0 | `hello.agent_version`, `hello.agent_update` (`agent_update`, `agent_update_status`) | §2 |
| 0.10.1 | `summary.remotes` | §10.1 |
| 0.11.0 | `hello.computer_info` (`computer_info`) | §13 |
| 0.12.0 | `hello.desktop` (`desktop_status/start/frame/input/clipboard/stop`) | §14 |
| 0.13.0 | `hello.metadata_cache` | §15 |
| 0.13.1 | `desktop_status.resolutions`, `desktop_frame.max_edge` | §14 |
| 0.14.2 | `summary.available_resets` статистики Codex | §9 |
| 0.14.3 | `statistics.reset_credits[].expires_at` | §9 |
| 0.15.0 | `timeline` истории и снимка, `progress`, `step_update` | §2.2, §7 |
| 0.15.2 | `context.cache` | §2.2 |
| 0.15.3 | `followup.position` | §2.2 |
| 0.16.0 | `hello.visualizations` (`visualization`) | §4 |
| 0.17.0 | `hello.passive_requests` (`req.passive`); `ask.subtask`, `snapshot.asks[].subtask` | §16, §7 |
| 0.17.2 | `hello.history_images`; `history.items[].images`, `history_image` | §4 |
| 0.19.0 | `hello.project_sharing`; доверенное `req.shared_projects` | §17 |
| 0.19.1 | `followup.images`, `followup.image_message_id`; ожидание фоновых Claude Workflow | §2.2, §4 |
| 0.20.0 | `hello.completions` + `welcome.completions` — кадр `completions`; `hello.visualization_scope`; `hello.agent_update_cancel` (`agent_update_cancel`), `agent_update_status.holds` | §18, §17, §2 |

## 0.20.0

- Codex: stdout app-server читается кусками (`codex_rpc.lines`), без предела строки `readline`: ответ
  `thread/read` со всей историей и картинками бывает больше кадра канала. Строка длиннее `codex_rpc.LINE_MAX`
  (256 МиБ) в памяти не копится: по её началу или концу находится id ответа, этот запрос получает
  `RpcError(too_large=True)`, `codex.CodexRunner._metadata` отдаёт `too_large` «Чат Codex слишком большой для
  чтения мостом», не перезапуская общий процесс; прочие запросы и уведомления идут дальше. `thread/resume` и
  `thread/fork`, чьи ответы нужны только ради id и cwd, — с `excludeTurns: true`. Проверки — `tests/test_codex_rpc.py`.
- Codex `step_update` вывода команды — `codex.CodexRunner._step_output`: не чаще `codex.STEP_UPDATE_SEC` (0.25) на
  шаг, первая дельта сразу, остальные — одной отложенной отправкой последнего состояния (каждый кадр несёт весь
  хвост до `limits.CLIP`, а очередь канала общая с ответами просьб). `result` шага снимает отложенное и уходит
  сразу; конец хода досылает отложенное до `done` (`codex.CodexRunner._output_done`). Формат кадра прежний; у
  Claude и OpenCode `step_update` нет. Проверки — `tests/test_codex_progress.py`.
- `visualization` участника (`shared_projects`, §17): `visualizations.read` с `codex_home=False` — только проект,
  общий каталог визуализаций Codex (HTML тредов всех проектов владельца) — `outside`. Тред по пути не сверяется:
  раскладка каталога Codex не контракт. `hello.visualization_scope`; старому мосту сервер передаёт от участника
  только относительный путь. Проверки — `tests/test_visualizations.py`, `../tests/test_project_access.py`.
- Отмена обновления: `updater.Updater.cancel` в `preparing`/`waiting` сразу возвращает `idle` (новое поколение:
  ожидание прежнего запуска уходит на следующей паузе, подготовка в потоке доделывается, и её копию удаляет
  `updater.discard` — только `release-*` в `releases`); в `restarting` — поздно. Студия — op `agent_update_cancel`
  (`hello.agent_update_cancel` только при GUI-обновлении), трей — «Отменить обновление» (`app.Controller.cancel_update`,
  виден, пока `activity_status().updating`). `updater.Updater.status` в `waiting` несёт `holds` —
  `app.Controller._update_holds`: числа заданий, ждущих ответа, запросов канала и признак рабочего стола.
  Срока авто-откладывания нет. Проверки — `tests/test_updater.py`, `tests/test_agent.py`, `tests/test_tray.py`,
  серверные `../tests/test_agent_update.py`, `../tests/test_ui_agent_update.py`.
- Досланные завершения (§18): `agent.Agent.emit` — конец хода, который `link.Link.send` не принял (канала нет), —
  в `agent.Agent.missed`; писатель умершего канала возвращает туда же неотправленные кадры. После `welcome` с
  `completions` кадр `agent.Agent.completions` уходит первым. Только память, без текстов. Сервер —
  `bridge._completions`. Проверки — `tests/test_link.py`, серверный `../tests/test_bridge.py`.

## 0.17.3

- Уборка выпусков сохраняет текущий, `old_repo`, автозапуск и используемые процессами копии; цель проверяется
  строго внутри `%LOCALAPPDATA%/Programs/studio-agent/releases`, без ссылок/junction в пути или дереве.
  Только `complete` разрешает первичное удаление; неизвестные и неподтверждённые каталоги сохраняются.
- Перед удалением создаётся внешний пустой `.prune-*`, связанный с NTFS file id каталога. Частичный отказ
  Windows больше не уничтожает доказательство разрешённой уборки вместе с `_update_result`; следующая
  попытка продолжит её. Подменённый/заново созданный каталог под тем же именем этим разрешением не покрывается.
- Новые управляемые runtime до готовности держат отдельные Windows-lease (`FILE_SHARE_NONE`) до выхода процесса.
  Регистрация lease и окончательная проверка/удаление держат общий OS-gate вне каталога выпуска, связанный с его
  file id: новый процесс не проскочит между проверкой и `rmtree`. Занятый gate или исчезнувший управляемый выпуск
  запрещают запуск без lease.
  При неполном CIM закрытые lease подтверждают остановку новых копий без прав администратора; живой lease,
  неизвестный файл или отсутствие доказательства запрещают удаление. Старые выпуски без lease при недоступных
  сведениях о процессах сохраняются; повышать права пользователя для уборки приложение не просит.
- `updater.prepare` использует доверенный helper прежней копии. Поэтому после `_update_result=complete`
  новый `updater.cleanup_releases` повторяет безопасную уборку своим кодом при запуске, включая первый
  переход со старого helper. `update_helper.prune_candidates` строит тот же план без изменений файлов.
  Проверки — `agent/tests/test_update_prune.py`: 10+ выпусков, partial-delete/retry, смена file id, границы,
  junction, CIM и настоящий runtime-lock/выход процесса. Установка работающий мост не перезапускает.

## 0.17.2

- История Claude/Codex/OpenCode сохраняет `images: [{index, name}]` у сообщения пользователя, без base64 и путей;
  текст сообщения больше не заменяет изображение пометкой. `index` — номер среди изображений этого сообщения,
  от 0 до 31, `id` сообщения стабилен при повторном чтении.
- `history_image {project, chat, message_id, index}` возвращает сохранённые в CLI байты одного изображения.
  Новый HTTP POST `/api/code/agents/{aid}/projects/{pid}/chats/{chat}/image` принимает только `message_id`/`index`
  в JSON. Обычные проверки учётки, CSRF, PIN и принадлежности чату действуют до чтения и повторно после него.
  Автоматическая загрузка превью не продлевает таймер PIN. Ответ — оригинал PNG/JPEG/WebP/GIF до 5 МиБ и 16 Мп,
  `no-store`/`nosniff`; страница истории сохраняет предел 512 КиБ. URL не загружаются; `localImage` Codex доступен
  только внутри проекта. Сервер ничего не записывает; браузер запрашивает только видимые изображения.
- Проверки — `agent/tests/test_history_images.py`, `tests/test_code_history_images.py`.

## 0.17.1

- Сжатие большой истории сохраняет порядок комментариев и команд и пересчитывает границы UTF-16.
- Claude получает штатный системный промпт Claude Code и ограничения субагентов из действующих настроек,
  включая указание дождаться свободного места; файлы настроек и хуки владельца сохраняются.
- Подзадачи Claude и Codex объединяют живое состояние с историей. Завершённые остаются доступны;
  неизвестный статус явно обозначается и не считается работающим.

## 0.17.0

- `codex_rpc.RpcError` / `opencode_rpc.RpcError` — признак `transport`; `codex.CodexRunner._metadata` и
  `opencode.OpenCodeRunner.chats` закрывают общий процесс только при `transport`, сроке или отмене: ответ живого
  сервера, в том числе `not_found` нового чата, процесс не перезапускает.
- `chrome._toml` / `chrome._string` — символы вне BMP пишутся как есть; экранируются кавычка, обратная черта,
  управляющие (включая DEL).
- Субагенты: `turns._Turn.subtasks` — id и имена субагентов хода. Codex — `codex.CodexRunner._family` / `_adopt`,
  `codex.spawned` (разбор `thread/started`: `receiverThreadIds` collabAgentToolCall, `parentThreadId`, вложенные).
  OpenCode — `opencode.OpenCodeRunner._adopt` (дочерние сессии `task`: `rawOutput.metadata.sessionId`,
  `parentSessionId` == сессия хода). Запросы разрешений и вопросы этих потоков — обычные карточки с `subtask`, ответ —
  тем же id; посторонние потоки — отказ `-32601`, как раньше. Ход субагентов Codex — только статусом в
  `progress.agents`. Не проверено живьём (Codex/OpenCode не установлены): приходят ли запросы детей на подключение
  моста и передаёт ли ACP разрешения дочерних сессий.
- `codex.CodexRunner.chats(project, refresh=False)`: при `refresh` — фоновый полный обход отдельным app-server, не чаще
  `codex.SCAN_EVERY_SEC` (600) с на проект; результат только в памяти, сбрасывается при `close`; найденные вне индекса
  сессии добавляются к следующим спискам (кэш 20 с). `agent.Agent._op_chats` передаёт `refresh` только Codex.
- `jobs.attach(pid)` / `jobs.Job.close()`: `KILL_ON_JOB_CLOSE | BREAKAWAY_OK`; `codex app-server` и `opencode acp` —
  в своём Job Object, закрытие при `disconnect` или падении моста гасит MCP-серверы и шеллы. `claude.exe` намеренно
  не охвачен (job пришлось бы закрывать внутри SDK; фоновые задачи `run_in_background` Claude переживают ход).
- Фоновые просьбы: `hello.passive_requests` и необязательное `req.passive: bool` (§16) — проверка срока PIN без
  продления; продление — только после проверки `op` и полей (§3); `open` с `pin: null` тоже не продлевает.
  `pin.OpenSessions.alive` (без продления), `agent.Agent._dispatch` продлевает после валидации,
  `agent.Agent._op_visualization` перепроверяет без продления.
- Git: `gitinfo.SAFE` (`-c core.fsmonitor=false -c log.showSignature=false`) во всех `gitinfo.run` и
  `worktrees._git`; `gitinfo._filters` выключает драйверы фильтров clean/smudge/process из конфига самого репозитория
  (local/worktree, include; через `git config --show-scope`), системные и пользовательские (git-lfs) остаются;
  `status` — всегда с `--ignore-submodules=dirty` (правки внутри подмодуля не видны, сдвиг его коммита виден); без
  проверяемого конфига (git < 2.26, имя драйвера не выражается через `-c`) статус — `not_git`/`error`. Хуки не
  запускаются (`GIT_OPTIONAL_LOCKS=0`, индекс не пишется). `worktree add` — действие владельца: фильтры и хуки работают
  как у его git. Тест — `tests/test_git_untrusted_config.py` (integration).
- Корни: `projects.Catalog.find` сверяет с `realpath` корня; `store.Config.set_project_roots` хранит `realpath` (окно
  покажет настоящий путь, для сетевого диска — `\\сервер\…`); id проектов не меняются.
- Установщик: `install.ps1::Assert-BridgeStopped` находит `studio_agent.pyw` и в `releases\*`.
- Самообновление: `update_helper.prune` после подтверждённого `complete` оставляет текущий выпуск и откатный
  (`old_repo`), не трогает всё вне управляемого `releases`, ссылки, неизвестные/активные выпуски и автозапуск;
  частичная уборка повторяется по file id. `app.Controller._main` не зовёт `cli_version` до `_built`
  (метка готовности самообновления); версию задаёт
  `_refresh_cli` первого канала.
- Эталон сервера: `FakeMachine.passive_requests` в `tests/fake_bridge.py`.

## Возможности по capability

(мост ≥ 0.16.0) `hello.visualizations: true` объявляет op `visualization {project,source}` → `{text,version}`.
`visualizations.py` читает только UTF-8 `.html` ≤2 МиБ: относительный/абсолютный путь внутри
проекта или абсолютный путь под `${CODEX_HOME:-~/.codex}/visualizations`. Никаких прочих
файлов профиля; обход `..`, ADS и выход через junction запрещены, junction самого каталога
визуализаций тоже. Участнику совместного проекта (мост ≥ 0.20.0) — только путь внутри проекта. После чтения ядро повторно проверяет открытую PIN-сессию.
Файлы не копируются и не журналируются. Проверки — `tests/test_visualizations.py`.

(мост ≥ 0.15.0) `timeline.py` сохраняет порядок текстовых сегментов и инструментов. Текст единственный,
позиции UTF-16 совпадают с индексами JS. `Feed.note` отмечает события из общего `_send`, а
нормализаторы трёх движков восстанавливают порядок истории. `watch` копирует timeline/progress
до передачи кодировщику; при урезке истории `timeline.tail` сохраняет уцелевшие шаги и пересчитывает
позиции текста, включая добавленные пометки и символы вне BMP. `codex_progress.py` строит
ограниченные сводки diff/плана/субагентов из штатных событий. `step_update` передаёт текущий
вывод/PTY id; `result` — конечные duration_ms/exit_code/status. Тексты рассуждений по-прежнему
не передаются. Сводка и вывод живут только в памяти; после завершения источник истории — CLI.
Проверки — `tests/test_codex_progress.py` и браузерный `../tests/test_ui_code_progress.py`.

Claude запускается со штатным preset `claude_code`, как прямой CLI, и с теми же источниками настроек
`user/project/local`. `cli.subagent_limits` читает только ограничения параллельности и глубины из `env`
этих файлов, поверх окружения; `cli.coding_prompt` явно объясняет агенту ожидание свободного места.
Пределы не повышаются и файлы пользователя не меняются. SDK-хук поддержания ввода и пользовательские
хуки продолжают действовать. Проверки без вызова модели — `tests/test_claude_sdk.py`.

`turns.Runner._subtask` принимает штатные `task_started`, `task_progress`, `task_notification`, `task_updated`
Claude. Конечные фоновые `local_agent`, `local_workflow` и `local_bash` удерживают SDK после первого ответа родителя:
мост читает следующие ответы до итогового ResultMessage после завершения фоновой работы. Очередь уточнений
ждёт и фоновой работы; явная остановка вызывает `stop_task` и `interrupt`, в том числе при уже ответившем
родителе. Мониторы и удалённые агенты не удерживают ход бессрочно; обрыв CLI до окончания фоновой работы — ошибка.
Хуки `SubagentStart`/`SubagentStop` дополняют `progress.agents` детьми Workflow; сам Workflow — контейнер,
не отдельный агент в счётчике. Хук остановки до записи ответа не доказывает `completed`: его подтверждают
lifecycle или JSONL. После хода в `choices.json` сохраняются id, статусы и числовые метрики, без текстов заданий;
неподтверждённые получают `unknown`, остановленные вместе с ходом — `stopped`. `subagents.list_children`
объединяет их с полным списком SDK и живыми событиями; история завершённых детей остаётся доступной.
Для детей без живого lifecycle завершение также подтверждается последней репликой `assistant/end_turn`
в ограниченном хвосте их JSONL (32 КиБ; кэш по mtime/размеру). Неполная строка, незавершённая реплика
или отсутствие данных дают `unknown`, без предположения о работающем процессе.
`codex.CodexRunner.subagents` также дополняет историю текущими `progress.agents`, пока CLI ещё пишет её.
Список просмотра включает всех детей; ограниченная живая сводка Codex отдаёт приоритет активным.
Полное `thread/read` списка кэшируется на 10 с для 32 чатов с объединением одновременных чтений;
живые статусы добавляются при каждом запросе, не дожидаясь обновления архива.
Проверки — `tests/test_subagents.py`, `tests/test_codex_progress.py` и `tests/test_workflow_runtime.py`:
последний запускает настоящий CLI/SDK с двумя Workflow-детьми и фоновой Bash-командой с локальным подставным API, без квоты.

С 0.21.0 у подзадач есть необязательные `started_at`/`ended_at` (секунды Unix), `duration_ms` и `total_tokens`.
Claude получает их из lifecycle и JSONL, одинаковый id ответа в JSONL не увеличивает расход повторно.
Codex — из событий дочернего треда, его метаданных и счётчиков сохранённого rollout внутри CODEX_HOME;
чтения детей ограничены четырьмя параллельными запросами и общим кэшем списка. Отсутствующий счётчик не заменяется нулём.

Studio создаёт пользовательские Claude-сессии с `CLAUDE_CODE_ENTRYPOINT=local-agent` и сохраняет
`CLAUDE_AGENT_SDK_CLIENT_APP`: VS Code скрывает программные `sdk-py`/`sdk-ts`/`sdk-cli`, локальный мост виден.
После закрытия writer `sessions.make_visible` исправляет первый `entrypoint` старого чата с тегом `studio`.
Исходный JSONL остаётся в соседнем `<chat>.studio-backup`, дата изменения сохраняется; изменение файла во время
подготовки запрещает замену. Для ручного исправления старых чатов сначала проверить отсутствие их активных CLI
по `~/.claude/sessions/*.json` и процессам; для отката при остановленном writer вернуть JSONL из этой копии.
(мост ≥ 0.15.3) `followup.position` хранит счётчики опубликованного текста UTF-16 и начатых шагов:
переподключение восстанавливает уточнение между теми же событиями. Урезанный снимок опускает позицию.

(мост ≥ 0.14.0) окно и трей получают от `Controller.activity_status()` только счётчики заданий/ожиданий и факт
активного рабочего стола. Локальное завершение вызывает `desktop.revoke()` без изменения
`desktop_enabled`. Отменяемое «Выйти после завершения» ждёт `live()` всех Runner, lease рабочего
стола и запросов Link, затем просит штатный выход GUI; новые задания в промежутке перед выходом
получают busy. Никаких имён зрителя, экранов и текстов заданий в снимке активности нет.
Вопросы OpenCode передают `options[].id`: выбор — массив ID (также для single), свободный ответ —
строка; старые подписи принимаются только при однозначном соответствии. Codex `interrupted` —
`stopped`, неизвестное терминальное состояние — `error`; неотправленная очередь сохраняется.

(мост ≥ 0.13.0) `hello.metadata_cache: true`: `metadata.Cache` объединяет чтения в памяти, TTL локальных
сводок и списков сессий 20 с, GitHub 300 с. `refresh` принудительно перечитывает; локальные данные
возвращаются до GitHub, `remote` ждёт только его отдельную проверку. `github_checked_at` отличается
от времени локального Git. Каталоги Claude/Codex/OpenCode читаются параллельно с бюджетом 8 с
ожидания на движок; `complete: false` обозначает неполную выдачу, активность Runner всегда накладывается свежей.
(мост ≥ 0.14.1) истечение срока ответа оставляет общее чтение в фоне (до 30 с), чтобы следующий запрос
переиспользовал результат без сброса RPC. Каталог Codex запрашивает `thread/list` с `useStateDbOnly: true`:
готовый индекс вместо повторного сканирования JSONL для восстановления метаданных. На двух живых
проектах наборы ID совпали; 15–16 с сократились до 31–56 мс. Настройки и файлы самого Codex не меняются.
`projects.Catalog` переиспользует обход до 5 с и следит за корнями; перед каждым использованием
проверяет каталог, reparse points и принадлежность корню. Открытый PIN требуется до любого попадания в кэш.
Изменения/ходы инвалидируют снимок проекта, done — также индекс историй. Истории ограничены 512 КиБ
на страницу; нормализованный индекс до 12 записей/8 МиБ сверяет ревизию источника, при её отсутствии
читает обычным путём. Квоты Codex используют отдельный metadata-канал; OpenCode переиспользует ACP
только для каталога, закрывает его при ошибке и close. Ходы и безопасное ephemeral-чтение состояния
не меняются. Проверки — `tests/test_metadata.py`, существующие тесты проектов/историй/квот.

(мост ≥ 0.12.0) `hello.desktop: true` и операции §14 протокола. `store.Config.desktop_enabled=False`
сохраняется между запусками; меняется исключительно через Checkbutton локального окна →
`Controller.set_desktop_enabled`, без HTTP/CLI/команды протокола. `desktop.py` проверяет выключатель,
открытую PIN-сессию и случайный токен единственного зрителя; срок управления 10 с без запросов.
Выключение/close/desktop_close/обрыв отзывают токен и отпускают ввод. Поколение отбрасывает кадры,
захваченные до выключения, и работу старых запросов; очередь нативных действий ограничена.
`desktop_native.py` — ленивые ctypes/Pillow в одном потоке: WTSActive и Default desktop,
затем с 0.14.4 `SetThreadExecutionState(ES_DISPLAY_REQUIRED)` перед обращением к экрану/вводу.
Это будит программно погасший дисплей и обновляет его idle timer во время сеанса; без ES_CONTINUOUS,
без смены плана питания, без имитации пользовательского ввода. Проверка блокировки идёт раньше.
JPEG ≤2560 px/700 КиБ base64, SendInput с конечным перечнем клавиш и Unicode, CF_UNICODETEXT.
(мост ≥ 0.13.1) `desktop_status.resolutions` объявляет варианты 640, 800, 1024, 1280, 1600, 1920, 2560;
необязательный `desktop_frame.max_edge` задаёт длинную сторону кадра (по умолчанию 1600).
Разрешение монитора не меняется; ограничение объёма JPEG может дополнительно уменьшить кадр.
Ввод в собственные окна запрещён, отпускание наших клавиш разрешено при отзыве; UAC/экран входа
не обслуживаются. Ничего из экрана/ввода/буфера не журналировать и не писать на диск.
Проверки: `tests/test_desktop.py`, `tests/test_desktop_native.py` (синтетика),
`tests/test_window_integration.py` (Tk). Реальный ввод/буфер принимаются владельцем вручную,
автоматически включать работающему агенту разрешение для проверки нельзя.

(мост ≥ 0.11.0) `hello.computer_info: true`, op `computer_info` после обычной проверки открытой PIN-сессии.
Окно моста показывает доступную и общую оперативную память, обновляя снимок Windows API раз в пять
секунд без запуска PowerShell. В строке «Версия Studio» — версия из `welcome.studio_version` текущего
соединения; старый сервер без поля даёт «Неизвестна», потеря связи — «Нет связи».

`claude_sdk.py` скрывает оба вида процессов SDK на Windows: AnyIO транспорта Claude и синхронный
Git поиска сессий/worktree. Адаптеры меняют только ссылки внутри модулей SDK; общие `subprocess`
и AnyIO остаются штатными. Нативная регрессия проверяет все три запуска из настоящего `pythonw.exe`.

`computer_info.py` возвращает имя/пользователя Windows, ОС, память, локальные и съёмные диски,
адреса интерфейсов, все настроенные корни проектов и время снимка. CIM читается скрытым PowerShell
с пределом 10 с, публичный адрес IPv4/IPv6 — фиксированным HTTPS-запросом api64.ipify.org (3 с, без redirect,
без сведений компьютера или секретов). Всё — в `to_thread`, без журнала и записи снимка на диск.
Недоступные поля — null/пустые списки, не нули. Проверки — `tests/test_computer_info.py`.

(мост ≥ 0.10.0) `hello.agent_version` — версия моста, `hello.agent_update` — поддержка удалённого обновления
в GUI (в консольном Controller без обработчика штатного выхода capability выключена).
`updater.py` готовит отдельную версию/venv в `%LOCALAPPDATA%\Programs\studio-agent\releases`, не меняя
работающую копию; HTTPS того же сервера, без перенаправлений, размер/SHA-256 и пути ZIP проверяются.
`agent_update`/`agent_update_status` требуют открытой PIN-сессии. При обновлении новые ходы и файловые
изменения получают busy; существующие ходы и очередь заканчиваются. Перед выходом проверяются все три
Runner.live() и просьбы Link. GUI получает обычную команду quit. `update_helper.py` держит только право
ожидания старого процесса, запускает новую копию после его выхода, подтверждает готовность ядра и
переключает уже включённый Run; ранний сбой запуска возвращает прежнюю копию. Предыдущие версии сохраняются.
Никакого Stop-Process/TerminateProcess. Первая установка до 0.10.0 остаётся ручной. Тесты — `test_updater.py`.

Окно Tk размещает основной вид в Canvas с вертикальной прокруткой; после подключения карточки разделены
на вкладки «Подключение», «Работа», «Настройки» (Ctrl+Tab / Ctrl+Shift+Tab). Высота ограничена рабочей областью
монитора, поле с фокусом автоматически открывается при переходе Tab. Компактная форма входа использует
подписи рядом с полями; список папок показывает две строки и кнопки сбоку. Проверка геометрии при
100/150/200% DPI и смене темы — `tests/test_window_integration.py` (`integration`, без подключения моста).
Вопросы Codex/OpenCode передают стабильный `questions[].id`; ответы ключуются им, для прежней страницы
принимается текст только уникального вопроса. Claude сохраняет текстовый ключ Agent SDK.

(мост ≥ 0.9.1) `Config.max_turns` — общий предел заданий всех помощников на компьютере, по умолчанию 8
и для прежних `config.json`. Окно «Настройки» → «Одновременных заданий» сохраняет целое число > 0
через `Controller.set_max_turns`; ядро меняет общий предел Agent и `max_turns` трёх Runner без
переподключения, сброса PIN и остановки заданий. При понижении новые задания ждут освобождения мест.

(мост ≥ 0.8.1) `hello.file_operations: true`: op `file_operation {project,operation,path,destination}`,
`operation` — `create_file|create_dir|move|delete`. Пустые пути (корень), reparse-point в пути и перенос
папки внутрь себя запрещены. Создание через exclusive open/mkdir, Windows rename не затирает назначение
(`exists`); rename поддерживает смену регистра. Удаление папок — `shutil.rmtree`, вложенные junction
не обходятся. Операции и `save` под общим async-замком и в `to_thread`. Полные пути и системные тексты
исключений наружу/в журнал не попадают. `tests/test_file_operations.py` проверяет настоящий junction,
границы, сохранность назначения и PIN; сервер и интерфейс требуют capability старого моста.

(мост ≥ 0.8.0) необязательные `hello.file_suggestions/chat_search/queue_edit/chat_fork/subagents/worktrees`
объявляют инструменты рабочего места; контракт — §11 спецификации протокола. `discovery.py` читает
только локальные пути/историю с пределами и без постоянного индекса, поисковый текст — только в POST body.
`diffs.py` сохраняет предоставленные CLI фрагменты/patch; наличие только нового текста не означает создание файла.
Очередь всех Runner остаётся в `_Turn`: режим `queue` дожидается результата, `queue_pause` удерживает доставку,
`queue_edit` под `input_lock` проверяет владельца и версию. `_wait_queue` ждёт вне замка; Stop/close будят ожидание.
Claude ветвится через SDK `fork_session`, Codex — `thread/fork.lastTurnId` с проверкой отсечения и unsubscribe
потомка в finally; исходный writer не трогается. Подзадачи только читаются и сверяются с родителем.
`worktrees.py` создаёт worktree из HEAD в подпапке настроенного корня; не переносит правки, не удаляет папки
и не меняет ветку исходника. Реальная Git-проверка — `tests/test_worktrees_integration.py` (`integration`).
OpenCode ACP не объявляет ветвление/историю детей; общие подсказки, поиск, очередь и диффы работают.

(мост ≥ 0.7.4) `context.usage` необязательно несёт накопленные `total/input/output/cached/cache_write/reasoning`.
Codex: `tokenUsage.total` и `total_token_usage` из rollout; `used` по-прежнему из `last`, не из total.
Claude: `sessions.token_usage` читает SDK-историю главной сессии, объединяет блоки по API message.id,
учитывает чтение и создание кэша. Неизвестные числа не заменяются нулями. В choices сохраняются только числа.
`gitinfo.status` без upstream добавляет `unpushed` (rev-list HEAD --not --remotes, без fetch), null при ошибке.

(мост ≥ 0.6.0) `hello.chat_control` объявляет `steer` и `claim`. `window` (32 hex) — id страницы:
`_Turn.owner`, snapshot.owner и ownership; чужие steer/answer/stop получают `owned`, явный claim
передаёт управление без прерывания. Codex использует `turn/steer` с threadId/expectedTurnId,
Claude — query того же SDK-клиента и счёт результатов под input_lock; OpenCode ACP хранит ограниченную
очередь и выполняет её последовательными session/prompt. Неисполненная очередь возвращается в done.unsent.
Уточнения видны всем наблюдателям через followup и snapshot.followups; картинки в снимке не дублируются.
С моста 0.19.1 уточнение передаёт компактные `images` и отдельный `image_message_id`; существующий защищённый
`history_image` читает их из ограниченного LRU в памяти соответствующего движка (32 МиБ base64).
Кэш связан с проектом, чатом и сообщением, сохраняется после конца хода и очищается при выходе/отвязке;
при вытеснении старой картинки штатная история CLI после повторного открытия чата даёт её постоянную ссылку.
Байты не попадают в события, снимки, серверные хранилища или журналы.
Codex при отказе до отправки turn/start возвращает done.not_started, внешний writer — также code=writer_active.
`takeover` на простое только проверяет resume/unsubscribe без генерации, writer освобождается в finally;
явный fork копирует полную историю, сохраняет новый alias и оставляет внешний процесс работать.

(мост ≥ 0.5.0) третий помощник — OpenCode (`engine: opencode`, необязательный `hello.opencode`).
`opencode_rpc.py` находит настоящий exe, включая npm `@opencode/cli`, и запускает `opencode acp`
по JSON-RPC/stdio без TCP. Используются установленные провайдеры и настройки CLI. `opencode.py` —
каталог всех включённых моделей и их вариантов, история ACP `session/load`, поток, разрешения,
формы вопросов, остановка и контекст. UUID студии связаны с `ses_*` в `opencode-chats.json`
(только id/проект/происхождение); настройки чатов отдельно в `opencode/choices.json`.
Каталог и предпросмотр выбора создают пустую временную сессию и удаляют только её, без генерации;
чтение настроек существующего чата не переключает его модель. `access: default` сохраняет правила
OpenCode; `auto` выбирает `allow_once` только в присланном ACP запросе, запреты CLI остаются.
Контекст приходит из `usage_update`; картинки — встроенными base64-блоками, без временных файлов.
Controller проверяет три каталога, окно показывает три статуса; общий предел ходов задаётся в окне моста.
`Runner._activity` хранит только `{state, tool?}` в `_Turn`, отправляет `activity` при смене и
включает в `snapshot`. Codex — reasoning/tool/agentMessage из app-server, OpenCode — ACP updates.
Открытая карточка имеет приоритет `waiting`; после ответа стадия возобновляется. Текст рассуждений
не передаётся. Завершение по-прежнему `done`; страница показывает явный итог задачи.

(мост ≥ 0.4.1) чтение состояния существующего Codex-чата делает `thread/fork` с `ephemeral: true`
и `excludeTurns: true`, затем отписывает временный thread в finally. Оно никогда не вызывает
`thread/resume` исходного чата: resume держит его writer и блокирует рабочий процесс хода.
В `RpcError` сохраняются только метод, числовой код и признаки отказа (не тело с путями/текстом);
занятый внешним процессом writer имеет отдельное понятное сообщение владельцу.

(мост ≥ 0.4.0) `hello.chat_state: true`, `state` читает конкретные модель/усилие/права и контекст без
генерации. `permissions.py` задаёт режимы Claude и пресеты Codex; переданный `access` применяется
к ходу и запоминается локально в choices.json. Без явного выбора режимы CLI сохраняются.
Claude: `get_server_info`/`get_context_usage`; Codex: метаданные thread и события
`thread/tokenUsage/updated` (`last.totalTokens`, не накопленное total). Счётчик у старых сессий
Codex читается из хвоста локального rollout только внутри CODEX_HOME. В снимке — settings/context;
последний счётчик сохраняется в choices.json без текстов. Эфемерные метаданные отписываются.
`chrome.py` добавляет в дочерний Codex `studio_chrome`: `chrome-devtools-mcp@1.12.0 --autoConnect`,
Node/npm из PATH, без отдельных профилей, статистики MCP и CrUX. Нужны Remote Debugging и
разрешение Chrome. `cua_repl` выключается только для этого процесса, поскольку его host принадлежит
приложению Codex; Computer Use через node_repl/@oai/sky сохраняется.

(мост ≥ 0.3.4) `options_for` и проверка каталога запускают Claude Code с `extra_args={"chrome": None}`;
остальные инструменты не ограничиваются (`tools=None`, без `disallowed_tools`), `setting_sources`
по-прежнему user/project/local, правила разрешений не переопределяются. Установленные плагины и MCP
подхватывает CLI. Каталог не проверяет право на генерацию: аккаунт может требовать верификацию.
Управление рабочим столом Claude CLI на Windows не поддерживается; у Codex `@oai/sky` работает через
node_repl, браузерный плагин зависит от доступности browser access host приложения Codex.

(мост ≥ 0.3.3) `hello.images: true` объявляет вложения для обоих помощников. `image_input.validate` проверяет
до 4 `{mime, data}` (base64): PNG/JPEG/WebP/GIF, сигнатура, 5 МиБ на файл / 8 МиБ всего, в потоке до
запуска хода. `Runner.start(images=...)` передаёт их в Agent SDK потоковым user-сообщением с image/source
base64, Codex — image с data URL в `turn/start.input`; текст может быть пустым. URL и пути из просьбы
не используются, временных файлов нет. Тексты без images сохраняют прежний формат SDK.

(мост ≥ 0.3.0) мост поддерживает и Codex: `codex_rpc.py` запускает настоящий `codex.exe app-server --listen stdio://`
из PATH, npm или расширения VS Code, обменивается JSONL и отбрасывает stderr. `codex.py` — каталог моделей
`model/list`, сессии `thread/list` и `thread/read`, запуск/продолжение, поток, разрешения и вопросы, остановка.
`CodexRunner` использует общие снимки, ограничение ходов и жизненный цикл `turns.Runner`; при отзыве/выходе
Controller закрывает обоих помощников. Режим разрешений и sandbox меняются только явным выбором access в студии.
В `hello` — необязательный `codex {version, models, efforts}`; в просьбах чата `engine: codex`, без поля — Claude.
UUID нового чата задаёт студия, настоящий thread — Codex: соответствие и проект сохраняются атомарно в
`codex-chats.json` (без текстов), его нельзя терять при обновлении. Выбор модели и усилия — прежний `choices.json`.
Каталог app-server читается без генерации; тесты `test_codex.py` работают с подставным RPC, без квоты.

(мост ≥ 0.3.1) `Controller.backend_status()` хранит отдельные итоги проверки каталогов Claude Code и Codex
(`checking/ready/unavailable`, число моделей), `on_backends` передаёт изменения через очередь Tk в две строки
карточки подключения. Кэш моделей не означает успех последней проверки: `CodexRunner.probe()` возвращает bool.
Тема окна — `Config.theme` (`auto` у прежних настроек, явный выбор `light/dark`); `Controller.set_theme()` пишет
под `_cfg_lock` без перезапуска канала, `theme.dark_for` разрешает выбор. Публичные `config()` и `login` читают
под тем же замком: Windows не даёт заменить config.json, пока другой поток держит его открытым.
Переключатель `window.theme_controls` есть до и после привязки; `apply_theme` перекрашивает виджеты на месте,
не стирая введённые поля. Статус каталога не обещает доступность квоты генерации.

## Модули пакета `studio_agent`

Пакет `studio_agent`:

- `limits` — все числа протокола и моста (`FRAME_MAX`, `PIN_TRIES`, `OPEN_IDLE_SEC`, `TEXT_MAX`, `HISTORY_BUDGET`, …).
- `errors` — `Refused(code, message="", **extra)`: отказ просьбы; без слов — `WORDS[code]` («Тексты» плана); у
  `not_found` и `too_large` слова разные по месту — их передаёт вызывающий.
- `dpapi` — `protect`/`unprotect` (DPAPI области пользователя, `CRYPTPROTECT_UI_FORBIDDEN`, энтропия
  `b"studio-agent\0v1"`); чужой или битый блоб — `OSError`.
- `store` — каталог моста `home()` (`STUDIO_AGENT_HOME`, иначе `%LOCALAPPDATA%\studio-agent`), `Config` в
  `config.json` (`login` — логин учётки студии для окна и трея: из ответа привязки и `welcome`; битый — не JSON, не
  объект, поле не того вида — `Config()` и строка в журнале, файл не трогается до записи окном), ключ устройства в `key.bin` под DPAPI (не читается — `None`), `write_atomic`.
- `journal` — логгер `studio_agent` в `agent.log` (1 МиБ × 3, UTF-8).
- `pin` — `PinStore` (`pin.bin` под DPAPI: scrypt-хеш и счёт неверных; попытка пишется на диск до scrypt, проверки
  идут по одной — замок процесса; 5 подряд — `pin_locked` до `reset` на машине; нет файла — PIN не задан, блоб не
  читается — `OSError`) и `OpenSessions` (открытые сессии студии в памяти, срок простоя `OPEN_IDLE_SEC`).
- `projects` — `pid`, `list_projects` (подпапки разрешённых корней без `.`-имён, скрытых, системных и reparse-point), `find_project`
  (настоящий путь или `not_found`).
  `Config.projects_roots` хранит список; старый `projects_root` мигрирует в него и остаётся первой папкой для
  протокола и проверки каталогов CLI. `projects_legacy_root` сохраняет исходное пространство pid имени папки:
  у остальных корней pid = sha256(normalized_real_root + NUL + name)[:16]. Добавление, удаление и перестановка
  корней не меняют id оставшихся проектов. Одинаковые имена различаются подписью без полного пути.
  Worktree создаётся рядом с исходным проектом в его разрешённом корне; список видит все разрешённые корни.
- `files` — `resolve` (вид пути проверяется **до диска**: имена устройств, ADS `:`, точка или пробел в конце части —
  `invalid`; настоящий путь вне проекта — `outside`), `list_dir`, `read_text`, `read_raw` (base64), `save_text`
  (`version` — sha256 байтов, запись через `store.write_atomic`; «только чтение» — `read_only` «файл только для
  чтения» до записи, прочие атрибуты прежнего файла — новому `SetFileAttributesW`).
  С 0.7.5 `list_dir` добавляет необязательный `created`: секунды Unix из `st_birthtime`, в Python 3.11 на
  Windows — из `st_ctime` (создание); без даты создания — `null`. Рекурсивного обхода нет.
  С 0.7.6 `hello.file_stats` объявляет op `file_stats {project,path}`: один обход `file_stats.scan` в потоке,
  общая блокировка не допускает параллельных подсчётов. Результат — `items[{path,size,files,folders,complete}]`,
  общий `complete`, `skipped_links`, `checked_at`. Все счётчики рекурсивные, ссылки исключены; пределы —
  20 с / 5000 папок с корнем / около 4 МиБ ASCII-JSON. Ошибки доступа и пределы сохраняют нижние границы.
  `Runner._frame` добавляет имя папки проекта `project_name` только к `ask`/`done` для push, без пути и текста чата.
- `gitinfo` — `parse` (porcelain v2 `-z`), `status` (`git -C`, без окна и необязательных замков, 25 с;
  `GIT_CEILING_DIRECTORIES` — родитель настоящего пути проекта: проект без своего `.git` внутри чужого репозитория —
  `not_git`; код ≠ 0 или нет git — `not_git`, срок вышел — `TimeoutExpired` наружу).
- `cli` — `find_cli` (`claude.exe` новейшего расширения VS Code, версии числами; нет — CLI, встроенный в SDK),
  `current(path, find)` (прежний путь, пока файл есть, иначе `find()` заново — как `worker.py::_claude_bin`),
  `drop_session_env` (из окружения моста прочь `SESSION_ENV`, `SESSION_PREFIXES`, `TERM_PROGRAM=vscode` и
  `GIT_ASKPASS`/`GIT_EDITOR`, ведущие в папку VS Code),
  `cli_version` (первое слово `--version`, 30 с; сбой — «встроенный в SDK»), `models_from` (`get_server_info()["models"]`
  без `default` → `{"id", "label"}` и усилия в порядке `EFFORTS`), `probe` (короткий клиент в папке проектов,
  `setting_sources` user/project/local, ≤ 60 с, `disconnect` и при сбое; stderr Claude Code отбрасывается).
- `claude_sdk` — штатный `ClaudeSDKClient`; с 0.16.2 ссылка `anyio` только внутри модуля
  транспорта SDK 0.2.174 заменена адаптером, добавляющим `CREATE_NO_WINDOW` к обоим запускам
  (проверке версии и основному CLI). Сам модуль AnyIO и его `open_process` не меняются;
  окружение, потоки, отмена и очистка остаются в SDK. Все три точки создания клиентов
  (`app`, `cli`, `turns`) импортируют его отсюда. Контракт обоих запусков — `test_claude_sdk.py`,
  настоящий `pythonw.exe` с консольным CLI без вызова модели — `test_claude_sdk_integration.py`.
- `events` — `clip` (строки на любой глубине — до `CLIP` знаков с «…»), `bound` (событие `step`, `result`, `ask` больше
  `EVENT_MAX` 1 МиБ в ASCII-JSON — `input`/`text`/`questions` до `CLIP_TIGHT` 2000, всё ещё больше — `input` пометкой
  `HIDDEN`; шаг снимка хранится уже урезанным, CLI получает ввод целиком), `text_of` (текст блоков содержимого,
  картинка — «[изображение]»).
- `sessions` — функции SDK атрибутами модуля (тесты подменяют): `chats` (сессии проекта без worktree, только `chat`-UUID,
  мс → с, новые сверху, `running`/`asking` из ходов моста), `normalize` (главный агент; ответы до следующего вопроса —
  одно сообщение, итоги инструментов — к шагам), `fit` (хвост ≤ `HISTORY_BUDGET`; одиночный огромный — строки шагов до
  2000, шаги с начала прочь «… первые шаги не показаны», затем хвост текста «… начало ответа не показано»), `history`
  (нет записей — `not_found` «нет такого чата»), `exists`, `last_model` (без `<synthetic>`), `mark_studio` (метка
  `studio`, сбой — в журнал).
- `turns` — `Runner` (ходы через Agent SDK): клиент на ход (новая сессия — `session_id=chat`, есть — `resume=chat`;
  `null` модели и усилия — запомненные `store.Choices`, иначе модель последнего ответа сессии), не больше `Config.max_turns`
  сразу и один в чате (`busy`, `running`); `can_use_tool` → событие `ask` и ожидание `answer` (карточка переживает
  смену канала), `answer`/`stop`/`watch` — только хода того же `pid` (иначе как без хода), `watch` — снимок `snapshot` (урезан
  `sessions.fit` и закодирован в потоке; события хода тем временем — в `held`, за снимком), `stop` — отказ карточкам
  и `interrupt()`
  (`idle` без хода; interrupt упал или не успел, или `stop` повторный — отмена задачи хода: `_run` доводит `done`
  `stopped`, `disconnect` гасит процесс), `close` — мост выходит или отозван (`done` «ход прерван: мост остановлен»;
  пока идёт — `start` отвечает `busy` «мост выходит»; `final` — выход моста — и после; `hold` — отвязка, отзыв,
  замена — и после, до `reopen()` (ядро зовёт его в `_relink` перед новым каналом): просьба `turn`, чей `find_project`
  ещё шёл, хода не начнёт; `stop` во время `_finish` задачу не отменяет — `finishing`); путь `claude.exe` — `cli.current` в начале каждого хода (в потоке `_choose`);
  метка `studio` новой сессии — после `disconnect`.
- `link` — `ws_url` (`https://h` → `wss://h/api/agent/link`; `http` — только `localhost`, `127.0.0.1`, `[::1]`) и `Link`
  (канал к студии): `connect` с `Authorization: Bearer <ключ>`, `max_size=FRAME_MAX`, пинги 20/20 с; первым — `hello`,
  `welcome` ≤ `WELCOME_SEC` (кадр — колбэку `on_welcome` в цикле, его сбой канал не рвёт), затем `agent.new_link(reconnect=True)` и `agent.send = link.send`; `req` — `agent.handle` своей задачей,
  ответы подряд в очередь канала, по которому пришла просьба (оборвался — пропадают; задачи не отменяются); `close` —
  `agent.close_session`; не JSON и прочие `t` — пропуск. Писатель — одна задача на канал, кадры — `ensure_ascii=True,
  allow_nan=False` (`events.Ready` — готовой строкой). Закрытие 4401 — `revoked`, 4409 — `replaced` (выход из `run()`), 4400 — пауза `PROTOCOL_PAUSE_SEC`,
  прочее, отказ рукопожатия и любое неожиданное исключение канала (в журнал — имя типа) — `BACKOFF` по числу неудач
  подряд (сброс после `welcome`); `stop()` досылает очередь писателю не дольше `FLUSH_SEC` (2 с: `done` ходов при
  выходе), затем закрывает; `on_state`: `connecting`,
  `online`, `offline` (перед каждой паузой), `revoked`, `replaced`, `stopped`. Паузы — параметр `sleep`; тесты —
  подставная студия `tests/fake_studio.py` (`websockets` на `127.0.0.1:0`).
- `agent` — `Agent` (просьбы студии): `hello` (`has_pin` — в памяти, к диску не ходит: читается при создании и `refresh_pin` ядра перед каждым каналом;
  нечитаемый `pin.bin` — `true`), `handle` (кадр `req` → `[res, *после]`; поля — по `OPS` до
  исполнения, иначе `invalid`; `id` не вернуть — без ответа; `open` под замком — порядок Ruling 4: без PIN — открыть,
  `null` открытой — `{}`, блок — `pin_locked`, `null` — `pin_needed`, иначе `pins.check` в потоке; прочие — `touch`
  или `closed`; `watch` без хода — `{"running": false}` без проверки чата; не `Refused` — `error`, в журнал `op` и
  тип; ответ кодируется один раз — `events.Ready`; больше `FRAME_MAX` — `too_large`, не кодируется — `error`),
  `close_session`, `new_link` (`reconnect=True` сохраняет отметки PIN без продления срока, иначе все сессии прочь;
  новое поколение канала: `open`, чья проверка PIN пережила новый канал, —
  `closed`), `emit` (в `send`, его ставит `Link`; нет — событие пропадает). `Runner` создаётся раньше и получает `emit`
  поздним связыванием (`lambda f: agent.emit(f)`).
- `enroll` — `enroll(url, login, password, name)` → `(agent_id, device_key, login)`: `POST <адрес>/api/agent/enroll` JSON
  через `urllib`, без `Origin`, перенаправлениям не следует; адрес — по правилу `link.ws_url` (`base_url`); логин — из
  ответа, у прежней студии (поля нет) — введённый `strip().lower()`; отказы — `EnrollError(code, words)`: 401 `login`,
  429 `locked`, 422 с `detail.code == "name"` — `name`, прочее — `unavailable` «Студия недоступна (HTTP n | тип
  ошибки)»; неверный или усечённый ответ HTTP (`http.client.HTTPException`: `BadStatusLine`, `IncompleteRead`,
  `LineTooLong` — urllib их не оборачивает) — тоже `unavailable`. `unenroll(url, key, timeout=15)` → `(код HTTP,
  забыта)`: `DELETE` того же пути, ключ — `Authorization: Bearer`, без тела и `Origin`, без перенаправлений; забыта —
  204 и 401 с `detail.code == "key"`; прочий 401 (`login` от `guard` прежней студии), 403, 405, 5xx — нет (слова
  `KEPT`); сеть и адрес — `EnrollError`.
- `setup` — `validate(form, enrolled=)` (слова окна; не привязан — логин и пароль обязательны, привязан — оба или ни
  одного; имя как у студии — 1–64 без управляющих; папка есть; PIN — `pin_errors`) и `apply(h, form)`: с логином —
  привязка, `save_key`, `agent_id`, `name`, `login`; всегда — `url`, `projects_root`, `autostart`; PIN — `no_pin` убирает,
  заполненный задаёт, пустой не трогает. Пароль не сохраняется нигде.
- `single` — `acquire()`: мьютекс `Local\StudioAgent`, уже есть — `None` (второй мост выходит).
- `autostart` — `HKCU\…\Run` значение `StudioAgent` = `command()` (`.venv\Scripts\pythonw.exe` + `studio_agent.pyw`
  репозитория), `enabled` (значение есть и равно `command()` этой копии без учёта регистра; другая копия — `False`),
  `set_enabled`; `key` — параметр (тесты пишут в `Software\studio-agent-test\Run`).
- `app.Controller` — ядро без Tk и трея (его же берёт `run --console`): поток с циклом `asyncio` — `find_cli`,
  `cli_version`, `Runner` и `Agent` (поздний `emit`), `cli.probe` до первого канала и раз в `PROBE_EVERY_SEC` (пустой
  итог прежние модели не стирает; набор сменился — канал заново, новый `hello`, кроме `setup`/`revoked`/`replaced`), `Link`. Публичные методы — из любого потока:
  `restart_link()` (канал заново с перечитанными ключом, настройками и `has_pin`; состояния прежнего канала отсекает
  поколение `_gen`), `apply_setup(form)` (блокирует — не из потока Tk; «привязан» — есть ключ **для этого адреса**:
  другой адрес требует новой привязки, ключ чужой студии туда не уходит; с логином — `fresh`: первый `online` —
  уведомление трея), `unenroll(*, ask_studio=True) -> (стёрто, words)` («Отключиться», блокирует: под замком
  `_relink` — `runner.close(hold=True)` (не `final`: `done` уходит в канал; новых ходов нет до `reopen`),
  `_drop_link()`, затем `enroll.unenroll` (любой сбой — «не забыта») и всегда стирание: `key.bin` первым, затем
  `pin.bin`, `agent_id`, `login` — адрес, имя, папка, автозапуск остаются; `PermissionError` (файл читает другой поток:
  окно, трей) — повтор `ERASE_TRIES` (10) раз через `ERASE_PAUSE_SEC` (50 мс, пауза — параметр `sleep`); стёрто —
  `setup` и слова `""` или `KEPT`; не стёрто — `(False, ERASE_FAILED)`, строка журнала с типом и канал заново
  (`_relink`: ключ остался — `connecting`, ключа уже нет — `setup`; сбой отвязки не оставляет «на связи» без канала);
  «Войти заново» после отзыва — `ask_studio=False`), `set_pin(pin | None)`, `set_folder(path) -> [слова]`,
  `set_autostart(on)` (блокируют; папка и PIN — канал заново, кроме `revoked`/`replaced`: заменённый отнял бы ключ),
  `has_pin`, `pin_status()` (`none`/`set`/`locked`), `login`, `autostart_enabled()` (реестр через `autostart.enabled`,
  не `config.autostart`), `reset_pin()`, `quit()` (`runner.close(final=True)`, затем `link.stop()`); `on_pin_changed`
  — `set_pin`, `reset_pin` и блок PIN из студии (`Agent.on_pin_locked`); правки `config.json` — под `_cfg_lock`
  (`welcome` пишет `login` и `name` в потоке, только своего поколения; выход ждёт `_background` после остановки
  канала, до закрытия цикла: отвязка не вернёт стёртый логин; логин
  сменился — после записи подписчикам `on_login(callback)`), `state` + `on_state`: `setup` (нет ключа или папки — канал не поднимается), `connecting`,
  `online`, `offline`, `revoked`, `replaced` (оба — до новой настройки), слова — `WORDS`. Зависимости (Link, SDK, CLI,
  probe, привязка, автозапуск, `sleep`) — параметры: тесты `tests/test_app.py` без сети и реального времени.
  `_refresh_cli` (перед каждым каналом `_relink` и перед часовой пробой) — `cli.current` и, если путь сменился, версия
  заново в `agent.cc_version` (студия видит её в следующем `hello`). Канал кончился `revoked`/`replaced` текущего
  поколения — `runner.close(hold=True)` (ходы прерваны, карточкам отказ, `claude.exe` гаснет; новые — `busy` до
  нового канала) и `agent.open.clear()`.
  `prepare_env()` — `cli.drop_session_env` до `Controller` (зовут `app.main` и `run --console`).
- `__main__` — `python -m studio_agent [run] [--console] | enroll | set-pin | clear-pin | reset-pin | status`: пароль
  и PIN — `getpass`; `run --console` — мьютекс, журнал ещё и в stderr, выход на `setup`/`revoked`/`replaced` и Ctrl+C.
- `journal.setup` ставит логгеру `websockets` уровень не ниже INFO: на DEBUG он пишет заголовок `Authorization`.
- `app.main` — мост с треем: `single.acquire` (занят — окно «Мост уже запущен», выход), `journal.setup`; главный поток —
  скрытый корень Tk (`window.prepare`: тема и значок окон) и опрос очереди команд `root.after(100, …)` (меню трея и
  состояния ядра — только через очередь: `("state", s)` — трею, окну `window.update` и уведомлению `tray.notice`;
  `("login", l)` — подсказке трея и строке «Вход» окна `window.login_changed`; `pin` — меню трея и строке PIN окна);
  не подключён — окно сразу; «Снять блок PIN» трея — в своём потоке (DPAPI); «Выход» — трей прочь,
  `Controller.quit()`, конец `mainloop`. Tk и pystray импортируются только здесь — `Controller` и консоль без них.
- `theme` — палитры студии `PAPER` / `GRAPHITE` (токены спеки §5.3 дословно, плюс свои `warn`, `field`, `edge`),
  `palette(dark)`, `is_dark()` (`AppsUseLightTheme`), `dark_title(win, dark)` (`DwmSetWindowAttribute` 20),
  `apply(root, dark=None)` — стили ttk на `clam` (`Accent.`, `Danger.` кнопки, `Card.*`, `Muted.`, `Title.`,
  `Status.`, `Error.`, `Badge.`), шрифт Segoe UI Variable Text (нет — Segoe UI).
- `tray` — рисунок `icons/icon.svg` студии Pillow-ом в 512 px (дуги и кривые — многоугольником) → `icon(size)` (окно) и
  `image(state)` (64 px, точка `COLORS` в правом нижнем углу с белой обводкой: зелёная `online`, серая
  `connecting`/`setup`, жёлтая `offline`, красная `revoked`/`replaced`), `title(state, login)` («Studio Agent — на
  связи · ivanov», ≤ 127), `status_line` («● На связи — ivanov» — первая неактивная строка меню), `notice(state,
  fresh)`; `Tray` (pystray в своём потоке): «Открыть Studio Agent» (по умолчанию), «Снять блок PIN», «Открыть
  журнал», «Выход»; `refresh` — `update_menu` в своём потоке (pystray на Windows строит меню заранее), `notify`.
  Блок PIN проверкой из студии трей узнаёт от `Agent.on_pin_locked` → `Controller.on_pin_changed` → команда `pin`
  очереди Tk (меню и окно заново): смены состояния канала при этом нет; так же — «Снять блок» и PIN из окна.
- `window` — `open_main(root, controller)` (`open_setup` — синоним): одно окно «Studio Agent», вид по `view_of(enrolled)`
  — «Вход» (форма, «Подключить» → `apply_setup`) или «Подключение» (карточка `card_of(state)` → слова, токен цвета
  точки, приглушённая строка, кнопка `MAIN_BUTTONS`: «Отключиться» с подтверждением, «Войти заново» только при
  `revoked`, при `replaced` — «Подключить снова» = `restart_link()` и строка «Подключить снова — этот компьютер заберёт
  канал у другого моста»: ничего не стирается — второй мост той же учётки Windows в другом сеансе живёт с тем же
  `key.bin` и `pin.bin`; затем «Студия», «Вход», «Компьютер»; карточка «Настройки» — папка, автозапуск — галочка по
  `autostart_enabled()`, строка PIN `pin_row`); «привязан» — состояние ядра не `setup` (DPAPI окно не читает). Смена
  состояния — `state_action(вид, state, busy)`: вид сменился — окно заново, тот же — карточка на месте (`show_card`),
  окно занято (`busy`: операция `work` или открытый диалог; кнопки вида гаснут) — ждёт и применяет последнее после;
  строка PIN и папка тоже меняются на месте — ошибки «Настроек» остаются видны. `PinDialog` и `confirm` — модальные
  `Toplevel` той же темы. Блокирующее — `MainWindow.work` (свой поток, итог — `root.after`); пароль и PIN стираются из
  полей сразу после нажатия; слова отказа студии при отвязке — над формой входа, «не стёрто» — в карточке.
- `studio_agent.pyw` (корень `agent/`) — запуск без консоли: `.venv\Scripts\pythonw.exe studio_agent.pyw`.
- `install.ps1` (UTF-8 с BOM, CRLF) — `.git` нет ни рядом, ни над каталогом `agent/` (архив студии) — копия в `%LOCALAPPDATA%\Programs\studio-agent`:
  запущенный мост этой копии (`python.exe`/`pythonw.exe` с её `studio_agent.pyw` в командной строке) — отказ до
  копирования файлов и изменения среды, без остановки процесса. Владелец дожидается заданий и выходит в трее.
  После выхода файлы архива — поверх, кроме `.venv`, `Unblock-File` («Mark of the Web»); из git-копии — на
  месте. Затем `.venv` (`-Python`, иначе `py -3`, нет `py` — `python`; проверка ≥ 3.11 до `venv` с понятной ошибкой),
  `pip install -r requirements.txt -c constraints.txt` (транзитивные пакеты закреплены; так же ставит самообновление
  `updater.prepare`), запуск `pythonw.exe studio_agent.pyw` без ожидания; `-NoVenv`, `-NoStart`,
  `-RunKey` (ключ автозапуска, тесты — `HKCU:\Software\studio-agent-test\Run`) — для тестов; повтор безопасен;
  автозапуск ставит окно (`autostart.REPO` — та копия, что запущена), а установка в `Programs` значение `StudioAgent`,
  если оно есть, переписывает на эту копию (как `autostart.command()`), нет — не заводит.
- `install.cmd` (ASCII, CRLF) — двойной щелчок: `install.ps1` с `-ExecutionPolicy Bypass`, код не 0 — `pause`.
- Архив моста собирает выкладка студии (`git archive` `HEAD:agent`): `.gitattributes` `export-ignore` — тесты, `.claude/`,
  `CLAUDE.md`, `AGENTS.md`, `FINDINGS*`, `IDEAS*`, `pytest.ini`, `requirements-dev.txt`, `.gitignore`, `.gitattributes`;
  `*.cmd text eol=crlf` — последней строкой.

## Решения

## Решения

Сводка проектов (0.7.3): `hello.project_summary: true`, просьба `project_summary {project}` требует
открытую PIN-сессию. `project_summary.py` читает только корневые `FINDINGS.md`/`IDEAS.md` (до 2 МиБ),
считает записи `open`/`proposed` вне комментариев и блоков кода. Git — локальные команды без fetch,
по 5 с, счёт файлов без ограничения 2000; без upstream — `HEAD --not --remotes`. Дата изменения —
последний коммит и время изменённых файлов, без Git — mtime папки. Ошибка даёт `null`, отсутствие
журнала — 0; в ответе только числа, даты и `git: ok|not_git|error`, без текстов и путей.
Проверки — `tests/test_project_summary.py`, настоящий Git — его `integration`-сценарий.

(мост ≥ 0.9.2) `summary.github` (ответ `project_summary`) — необязательная сводка публичной копии текущей ветки:
`{state: current|behind|ahead|diverged|missing|unknown, behind, ahead}` или `null` (нет публичного remote).
`github_status.py` читает remote `github` либо GitHub `origin`, анонимно получает smart-HTTP advertisement
через HTTPS github.com (до 2 МиБ/5 с), без GitHub API, Git credentials и fetch. PAT из URL не передаётся.
`behind/ahead` относятся к GitHub относительно локального HEAD. Расчёт — локальный rev-list; неизвестный
объект, shallow history, detached HEAD и ошибки дают unknown. HTTP 401/404 скрывают приватный/отсутствующий
репозиторий. Нет новых ключей, записей refs или журналирования; команды Git ограничены 3 с.
Проверки — `tests/test_github_status.py`, включая настоящий Git без сетевых запросов.

(мост ≥ 0.10.1) `summary.remotes` (ответ `project_summary`) — необязательный список `{name, state, behind, ahead}` всех
настроенных Git remote, кроме `upstream`, включая `cld`. `remote_status.py` читает только имена (`git remote`),
сравнивает `refs/remotes/<name>/<branch>` с текущим HEAD через `rev-list --left-right --count`.
`behind/ahead` — отставание/опережение remote относительно HEAD, по локальным данным последнего
fetch/push; сервер мог измениться. Нет сети, GCM, fetch или изменения refs. Общий бюджет 5 с,
команда до 3 с. Ошибка, неизвестный ref, shallow/detached/пустая история сохраняют имя с unknown.
Имена и счётчики без URL/ключей; старые мосты не присылают поле. Проверки — `tests/test_remote_status.py`.

Плана (`Решения плана`) и контролёра (`progress.md` плана, «Rulings»), где они задают поведение:

- **Клиент SDK — на ход** (спека §6.6): поднимается в `Runner._run` и гаснет в `_finish`; новая сессия —
  `session_id=chat`, существующая — `resume=chat`; модель и усилие — опции запуска (`set_model` и перезапуска при смене
  усилия нет). «Новая» — `sessions.exists`: `get_session_info(...) is not None` **или** есть хоть одна запись
  `get_session_messages(limit=1)` — SDK отдаёт `None` и сессии без извлекаемого названия (Ruling 9), а `session_id`
  существующей сессии CLI не примет.
- `null` модели — `choices.json` чата, иначе модель последнего ответа сессии (без `<synthetic>`: продолжение сессии VS
  Code не меняет её модель), иначе умолчание Claude Code; `null` усилия — запомненное или умолчание.
- **`setting_sources=["user", "project", "local"]`** явно; `permission_mode` задаётся только явным выбором access: правила разрешений, CLAUDE.md
  и `env` проекта действуют, как в VS Code. Окружение `claude.exe` — окружение моста: при старте из него убраны только
  переменные сессии того, кто запустил мост (`cli.SESSION_ENV`, `CLAUDE_CODE_MESSAGING_*`, `VSCODE_*`,
  `CODEX_COMPANION_*`, `TERM_PROGRAM=vscode`, `GIT_ASKPASS`/`GIT_EDITOR` из папки VS Code; `install.ps1` запускают и из
  Claude Code), прочее (`ANTHROPIC_*`, настройки `CLAUDE_CODE_*`) —
  как есть; добавляется только `CLAUDE_AGENT_SDK_CLIENT_APP=studio-agent/<версия>` — и в ходе, и в `cli.probe` (Ruling 10).
- CLI — новейший `claude.exe` расширения VS Code, нет — встроенный в SDK; путь сверяется перед каждым ходом и пробой
  (VS Code ставит обновление в новую папку и удаляет прежнюю — без этого мост после первого обновления отвечал бы
  «Claude Code не найден на компьютере» до перезапуска). Модели — `get_server_info()["models"]`
  короткого клиента до первого канала и раз в час; новые студия видит только в следующем `hello` (обновить знакомство
  протокол v1 не умеет), поэтому сменившийся набор моделей или усилий поднимает канал заново.
- Текст хода — по `text_delta` (`include_partial_messages=True`), без дельт — блоками `AssistantMessage`; шаги и итоги —
  только главного агента, карточки подагентов — показываются. Метка `studio` — новой сессии после `disconnect` (файл
  сессии не пишут двое) и только если запрос ушёл к CLI. Время сообщений истории — `null`.
- `open` — порядок Ruling 4 (как `FakeMachine`): PIN не задан — открыть; `null` открытой сессии — `{}` и продление;
  блок — `pin_locked` (раньше `pin_needed`: иначе страница покажет панель PIN вместо «заблокирован»); `null` —
  `pin_needed`; иначе проверка.
- `Runner` получает `emit` поздним связыванием `lambda f: agent.emit(f)` (Ruling 5) — сборка в `app.Controller` и в
  фикстурах тестов; `stop` без хода — `Refused("idle")` (Ruling 6).
- `sessions.fit`: одиночный элемент больше бюджета и после отбрасывания шагов — `text` урезается до хвоста с первой
  строкой «… начало не показано» (Ruling 7, 10; элемент бывает и вопросом).
- `run --console` собирает ядро тем же `app.Controller`, что и трей (Ruling 11): сборка Agent/Runner/Link одна.
- Логгер `websockets` — не ниже INFO и в консоли (Ruling 13, `journal.setup`).
