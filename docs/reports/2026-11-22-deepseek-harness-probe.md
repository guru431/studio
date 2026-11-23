# Проба DeepSeek Harness (шаг 0 плана «DeepSeek Harness — четвёртый помощник «Агентов»»)

Дата: 2026-11-22. Машина разработки: Windows Server 2022, Node 26.7.0, npm 11.19.0, Python 3.14.
DSH: npm `@deepseek-ai/dsh@latest` = `0.2.2-rc.5` (`engines.node >=24.9.0`, подходит), поставлен
`npm install --prefix` во временный каталог, `DSH_HOME` — временный. Спека:
[2026-11-22-studio-deepseek-harness-design](../superpowers/specs/2026-11-22-studio-deepseek-harness-design.md).
Фикстуры — `agent/tests/fixtures/deepseek/` (без ключей, путей и адресов; пути — `<PROJECT>`, `<HOME>`).

**Итог ворот: D1 работает** — наложение `--patch` поверх `acp` подхватывает провайдера, модель и MCP
из патча профиля `web`, но не дословно: нужен перевод трёх видов строк (раздел «Строки наложения»).

## Как пробовали и чем это ограничено

- **Провайдер — заглушка, не шлюз srvllm.** Vault на этой машине нет ни по пути из задания, ни по пути
  `STUDIO_VAULT` из `deploy/local-profile.sh`; других ключей шлюза в окружении нет. Вместо шлюза — локальная
  OpenAI-совместимая заглушка на loopback (`openai-completions`, потоковые ответы, `reasoning_content`, вызовы
  инструментов по сценарию). Проверено настоящее: адаптер pi-ai DSH, ключ через `.credentials.yaml`
  (заглушка видела `Authorization: Bearer sk-test`), каталог моделей, ход, инструменты, MCP, карточки,
  отмена, файлы сессий. **Не проверено:** совместимость самого шлюза srvllm с DSH (роль `developer`,
  `max_tokens`, вызов инструментов его моделями) — это `manual`-тест `agent/tests/test_deepseek_live.py`.
- **DSH Desktop не установлен.** `https://download.deepseek.com/desktop/dsh-latest-windows-x64.exe`
  (официальный адрес из `apps/desktop/publishing/README.md`) из этой сети не скачивается: TCP есть, TLS
  обрывается (curl/schannel, PowerShell, Node fetch). На GitHub Releases файлов нет. D6 — по исходникам
  `apps/desktop` (master) и документации пакета.
- Баланс DeepSeek не запрашивался (ключа с балансом нет).

## D1 Наложение

Итог: **работает с переводом строк.**

- Патч `profiles/web/cordis.patch.yml` дословно через `--patch` поверх `acp`: процесс стартует
  (`--dump-config` — rc 0), но строки, чьих записей нет в `acp`, пропускаются с предупреждением
  `patch: entry "<id>" not found` — запуск они не ломают. Пропускаются: `mcp-manager` (MCP из Web) и все
  строки web-app (`locale`, `ui-*`, `webserver` и т. п.).
- Строка `agent-default-model` в `acp` есть, но сессии ACP её не используют: модель новой сессии берётся из
  `acp.config.provider/model` (по умолчанию `deepseek-official` / `deepseek-v4-flash`). Без строки `acp`
  провайдер из Web есть в каталоге, но текущей остаётся DeepSeek.
- Работающий перевод (фикстура `acp_patch.yml`): `llm-pi-ai` и `agent-default-model` — как есть;
  `acp` — `provider`/`model` из `agent-default-model`; `mcp-manager` — вставкой целой записи
  (`insert` с `name: '@deepseek-ai/dsh-mcp-manager'`, `inject: [credentials]`, `config.servers` из Web).
  С ним: модель провайдера в `configOptions` (`["srvllm","stub-tools"]`), инструмент MCP
  `mcp__probe__echo` вызывается (`tool_call` → `tool_call_update` `completed`).
- Патч заменяет `config` записи целиком. Строка Web `permission: {defaultPreset: …}` поверх `acp` стирает
  таблицу `presets` (остаются умолчания схемы, без `read-only`): переносить только `defaultPreset`, дописывая
  `presets` из состава `acp`.
- Наложение в JSON (`--patch file.json`) DSH принимает так же, как YAML.
- Память: отдельного плагина памяти в Web нет; «память» — это MCP-серверы (руководство
  `docs/user/guide/mcp-memory.md`) — строки `mcp-manager` или вставки `@deepseek-ai/dsh-mcp-client`.
  Привязки к профилю Web нет.
- Формат `--dump-config`: YAML-массив записей с тегами `!!js` и комментариями `# == <слой>` (какой бандл или
  файл патча правил запись). JSON-вывода нет; `--dump-config-schema` — JSON Schema, не значения.
- Ключ провайдера: `apiKeyEnv: <ИМЯ>` — ссылка, значение ищется в `$DSH_HOME/.credentials.yaml`
  (`refs.<ИМЯ>`), затем в окружении запуска, `<cwd>/.env`, `$DSH_HOME/.env` (README `dsh-credentials-local`).

## D2 Чтение истории

Итог: **официального способа нет — читать файлы.**

- `dsh --help`: подкоманд экспорта нет. ACP: нет `session/load` и replay (README `dsh-acp`). SDK
  (`--profile sdk`): `session/resume` «historical events remain in the stored log», метода чтения нет.
  Экспорт есть только в Web (`dsh-session-log-export`: «Copy Session as Markdown», ZIP) — по
  аутентифицированному маршруту Web.
- Файл: `$DSH_HOME/sessions/--<ключ cwd>--/<id>/session.v4.jsonl.zstd` (по умолчанию) или
  `session.v4.jsonl` (`compression: none`). **Текущая версия формата — 4**, не 3: в `0.2.2-rc.5` уже V4.
  Брать файл с наибольшим `vN`; старшие версии — миграцией DSH при записи. Подробно — «Формат сессий».

## D3 Права на возобновлённой сессии

Итог: **нет, переменная на возобновлённую сессию не действует.**

Сессия создана при `workspace-write`; процесс с `DSH_PERMISSION_MODE=read-only` сделал `session/resume`
и запись в папке проекта прошла без отказа и без карточки. Контроль: новая сессия в том же процессе — запись отказана
(`[sandbox: file access denied under read-only mode]`), повтор с `sandbox_permissions` дал карточку.
Пресет закрепляется событием `permission/preset` при создании.

## D4 След пробной сессии

Итог: **след остаётся.** `session/new` + `session/close` без prompt (и без close: разрыв stdin, kill) —
сессия есть в `session/list` и на диске (`session.v4.jsonl.zstd` с заголовком и событиями прав,
`storages/session_projcache/sessions/<id>.json`): ACP материализует сессию сразу.

Обход, проверен: пробный процесс каталога получает в наложении `session-persistence-jsonl.config.root`
и `storage-json.config.root` во временном каталоге — `configOptions` те же, в `$DSH_HOME` новых файлов
нет. Любой запуск `dsh --profile <имя>` (и `--dump-config`) сам создаёт заготовку
`$DSH_HOME/profiles/<имя>/` (если её нет) и `$DSH_HOME/.anonymous-user-id`.

## D5 Занятая сессия

Итог: **отказ JSON-RPC `-32603`, Web держит блокировку до выхода из приложения.**

- Второй процесс `acp`, сессия открыта первым (только `session/resume`, без prompt): ответ
  `{"code": -32603, "message": "Internal error", "data": {"details": "session \"<id>\" is already owned by
  an active write handle"}}` (фикстура `resume_busy_error.json`). Распознавать по `data.details`.
- После `session/close` у держателя — `session/resume` во втором процессе проходит: `acp` отпускает
  блокировку по `session/close`.
- DSH Web (`dsh --profile web --no-open`, страница в headless Edge, отправлен prompt): resume из `acp` —
  тот же отказ; вкладка закрыта, через 15 с — отказ снова; после выхода процесса Web — resume проходит.
  Кода выгрузки сессий по простою в `dsh-api-session-controller` нет. Значит, слова отказа — «закройте
  DeepSeek Harness».
- `session/list` в том же соединении не показывает сессии, открытые в нём самом; из другого процесса
  открытая сессия видна.
- Блокировка на Windows — именованный семафор ядра на сеанс входа (README
  `session-persistence-jsonl`): процесс в другом сеансе входа блокировку не увидит.

## D6 Desktop

Итог: **установка не выполнена (сеть), пути — по исходникам.** `~/.dsh` на машине не появился,
следов Desktop в `%APPDATA%`/`%LOCALAPPDATA%` нет.

- Windows: установщик NSIS только для текущего пользователя (`perMachine: false`,
  `allowElevation: false`); каталог установки записывается в `InstallLocation` записи удаления в
  `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\…` (installer.nsh) — так мосту и искать
  (по умолчанию electron-builder ставит per-user в `%LOCALAPPDATA%\Programs\…`, точное имя каталога
  без установки не проверено). Exe — `DeepSeek Harness.exe` (`productName`).
- CLI в ресурсах: `<install>\resources\runtime\cli\bin\dsh.cmd` (macOS — `…/Contents/Resources/runtime/cli/bin/dsh`).
  Точная команда RunAsNode (из `apps/desktop/cli/dsh.cmd`):
  `ELECTRON_RUN_AS_NODE=1 "<install>\DeepSeek Harness.exe" --expose-internals
  "<install>\resources\app.asar\dsh\node_modules\@deepseek-ai\dsh-desktop-host\lib\cli.js" <аргументы>`.
  Вход CLI Desktop — `dsh-desktop-host/lib/cli.js`, не `@deepseek-ai/dsh/lib/bin.js`. Рядом есть и
  обычный Node: `<install>\resources\runtime\primary-runtime\dependencies\node\bin\node.exe`
  (им Desktop запускает менеджер команды).
- `DSH_HOME` Desktop — тот же, что у CLI (`resolveDshHome`: `$DSH_HOME`, иначе `~/.dsh`); профиль
  `profiles/desktop`, им владеет Electron, CLI npm его не загружает.
- «Manage dsh Command… → Install»: Windows — каталог `…\resources\runtime\cli\bin` в PATH пользователя
  (HKCU `Environment\Path`, владение отмечено отдельно, `command-path.ps1`); macOS — `/usr/local/bin/dsh`
  (ссылка на `…/runtime/cli/bin/dsh`, с правами администратора при необходимости).
- macOS: `/Applications/DeepSeek Harness.app` (dmg/zip), RunAsNode — `Contents/MacOS/DeepSeek Harness`.
- Linux: сборка только `AppImage` (`linux.target`), устойчивого каталога установки нет; команду
  «Manage dsh Command…» исходники обрабатывают для darwin и win32. Находить Desktop на Linux нечем —
  только npm/Python-CLI.
- Не проверено живьём: есть ли `js-yaml` в `app.asar` (ниже «Зависимости моста»), работает ли RunAsNode
  из службы моста.

## Прочие значения

- `DSH_HOME` по умолчанию на Windows — `%USERPROFILE%\.dsh` (`os.homedir()`; проверено подменой
  `USERPROFILE`; руководство удаления Desktop называет тот же путь).
- `usage_update`: `{"sessionUpdate": "usage_update", "used": <токены контекста>, "size": <окно>}` —
  после каждого шага; итог `session/prompt` — только `{"stopReason": "end_turn" | "cancelled"}`, поля
  `usage` нет. Накопленные токены — в файле (`assistant/message.usage`, `projcache.tokenUsage`).
- Ход: `agent_thought_chunk` и `agent_message_chunk` приходят **целым сообщением** (committed), не дельтами;
  у них `messageId`. Инструмент: `tool_call` (`title` = имя инструмента, `kind: "other"`, `status:
  "in_progress"`, `rawInput`) → `tool_call_update` (`completed` | `failed`, `content` текстом).
  `config_option_update` и `session_info_update` не приходят; `session/set_config_option` возвращает всё
  состояние.
- Карточка: `session/request_permission` несёт только `toolCall.toolCallId` и варианты
  `allow-once`/`reject-once` (`kind` `allow_once`/`reject_once`) — имя и аргументы брать из предыдущего
  `tool_call` с тем же id. Карточка возникает не на каждую запись: в папке проекта при `workspace-write`
  запись идёт без вопроса; вне папки — отказ песочницы, затем повтор модели с `sandbox_permissions` и
  карточка. Отказ — результат инструмента «the user rejected escalating…».
- Отмена: `session/cancel` → итог `cancelled`, частичный ответ приходит одним `agent_message_chunk`.
- Картинка: `promptCapabilities.image` в `initialize` — по модели **по умолчанию** (`acp.config`). Если
  `false` — prompt с картинкой: `-32602 "Invalid params: inline image prompts were not advertised by this
  connection"`; если `true`, но текущая модель без картинок — `-32602 "Invalid params: model \"<id>\" does
  not declare image input"`. Ни то, ни другое не идёт к провайдеру.
- Значение модели в `configOptions` — непрозрачная строка JSON-массива `["<провайдер>","<модель>"]`;
  `set_config_option` принимает только её дословно (`["srvllm", "stub-tools"]` с пробелами — `unknown model
  option`). Усилие — `reasoning_effort`, варианты из `reasoningEfforts` модели плюс `""` («Provider default»);
  у DeepSeek — `off`/`low`/`high`/`max`. Модель без `reasoningEfforts` опции усилия не имеет вовсе.
- Без ключа DeepSeek `deepseek-official` в каталоге всё равно есть (одна текущая модель).
- `.credentials.yaml`: `version: 1`, `refs: {<ИМЯ_ПЕРЕМЕННОЙ>: <ключ>}`, `records: {"<владелец>/<id>": …}`
  (фикстура `credentials.example.yml`).
- Телеметрия base-профиля: `session-telemetry-otel`, режим `DSH_TELEMETRY_MODE` или `FEEDBACK_ONLY`,
  адрес `https://dsh-otel-collector.deepseeksvc.com/v1/logs` — это процесс DSH, не мост.
- DSH подмешивает `AGENTS.md`/`CLAUDE.md` корня проекта (корень — ближайший `.git` вверх от cwd).
- npm 11 не запустил install-скрипты пяти пакетов (`koffi`, `node-pty`, `dsh-subprocess-local`, …:
  `allowScripts`); ACP, файлы, MCP и сессии на Windows работают и так. Инструмент `pwsh`/`bash` и POSIX
  (`node-addon-system` для `flock`) не проверялись — на macOS/Linux проверить в `manual`.

## Строки наложения

Из патча профиля-источника (`profiles/web` или `profiles/desktop`) в наложение для `acp`:

| Строка источника | Что делать |
|---|---|
| `llm-pi-ai` | как есть (провайдеры, модели, `apiKeyEnv`, `compat`, `reasoningEfforts`) |
| `llm-deepseek`, `llm-deepseek-account` | как есть (есть в `acp`) |
| `agent-default-model` | как есть **и** `- id: acp` с `config: {provider, model}` из неё |
| `mcp-manager` | `insert` целой записи: `id: mcp-manager`, `name: '@deepseek-ai/dsh-mcp-manager'`, `inject: [credentials]`, `config` из источника |
| `insert` с `name: '@deepseek-ai/dsh-mcp-client'` (память и MCP вручную) | как есть |
| `permission` | только `defaultPreset`, `presets` — из состава `acp` |
| строки web-app (`ui-*`, `locale`, `webserver`, `web-*`, …), `hmr`, всё незнакомое | не переносить → «не применено» |
| строки со значениями `!!js` | не переносить → «не применено» (безопасно и без вычислений) |

Пробному процессу каталога — дополнительно `session-persistence-jsonl.config.root` и
`storage-json.config.root` во временный каталог (D4) и без `mcp-manager`.

## Формат сессий

- Путь: `<root>/--<ключ cwd>--/<id>/session.v<N>.jsonl[.zstd]`, `root` = `$DSH_HOME/sessions`. Ключ cwd
  (`projectKey`, `format.ts`): `/`, `\`, `:` — один `-` на серию, `[A-Za-z0-9._-]` как есть, прочее —
  `~XXXX` (UTF-16, верхний регистр), ведущие `-` срезаны, до 251 символа, обрамление `--`. Ключ потерян
  (разные cwd совпадают) — сверять `cwd` в заголовке. Каталог сессии — `encodeSegment(id)` (тот же `~XXXX`).
- zstd: склейка независимых кадров с контрольной суммой — кадр заголовка, потом кадр на пачку записей;
  `compression.zstd.decompress` читает склейку целиком. Оборванный последний кадр при записи DSH
  восстанавливает сам; читатель должен терпеть хвост.
- Строка 1 — заголовок: `{"type":"session","version":4,"id","createdAt","cwd","isSeeded","delegationDepth"}`
  (+ `agentPreset` у сессий Web). Дальше события `{"type","seq","time","data"}`.
- Переписка:
  - `user/message`: `data.content[]` (`{type:"text",text}`, `{type:"image",attachment:{attachmentId:"sha256:…",
    mediaType,width,height,bytes}}`), `data.source.kind == "user"` — реплика человека; другие `kind`
    (`agent-instructions`, `runtime-context`, `skill-catalog`) — подмешанный контекст, не показывать.
  - `assistant/message`: `data.message.content[]` — `{type:"reasoning",text}` (мысли), `{type:"text",text}`,
    `{type:"tool-call",id,name,arguments}` (`arguments` — строка JSON); `data.message.source` —
    `provider`, `model`; `data.usage` — `inputTokens`, `outputTokens`, `totalTokens`, `cacheReadTokens`;
    `interrupted: true` у оборванного отменой; `stream` — служебная запись потока, не показывать.
  - `tool/call` (`callId`, `name`, `arguments`) и `tool/result` (`data.message.toolCallId`, `content[]`,
    `isError`; `data.meta` у файловых — `operation`, `path`, `diffs`).
  - `turn/start`/`turn/end` (`reason.kind`: `completed` | `aborted`), `step/start`/`step/end`.
  - Название — последнее `session/title` (`data.title`; `source.kind` `fallback` у acp, у Web потом
    `provider`). Модель — `model/selection`. Карточки — `approval/asked` (`toolName`, `callId`, `reason`) и
    `approval/decided` (`outcome`: `allowed-once` | …). Права — `permission/preset`, `sandbox/mode`,
    `approval/policy`. `session/end-seed` — граница возобновления.
  - Служебное, не показывать: `system/message`, `developer/message`, `request/header`, `request/context`,
    `agent/inbox/spliced`, `session/title-llm-request`.
- Картинки: `$DSH_HOME/attachments/v1/objects/<первые 2 hex>/<sha256 hex>`.
- Кэш проекций `$DSH_HOME/storages/session_projcache/sessions/<id>.json` (`version: 7`): `rows.title`,
  `tokenUsage`, `modelSelection` — дешёвый источник для списка, но не истина (пишется раз в 200 событий/5 с).

## Идентификатор сессии

- Созданные по ACP — UUID v4 без префикса (`85219569-668b-433d-b4bf-853d2acbfa59`).
- Созданные в Web — `session-<uuid>` (`session-3aa7bb24-…`). Мост принимает оба вида.
- `session/list`: элементы только `{sessionId, cwd}` — без `title` и `updatedAt`; новые первыми;
  `cwd` — фильтр по точному каталогу.

## Зависимости моста

- **PyYAML не нужен.** Патч человека переводится в JSON YAML-парсером самого DSH: `js-yaml` из его
  установки (`createRequire(<bin.js>)`, тип `!!js` → маркер), наложение пишется JSON-файлом (DSH его
  принимает). Проверено на npm-DSH; для Desktop — `app.asar/dsh/node_modules` не проверен.
- **zstd:** Python 3.14 — `compression.zstd`; мост поддерживает 3.11–3.13 — там нужна зависимость
  `zstandard` (маркер `python_version < "3.14"`, колёса для Windows x64, macOS, Linux) либо распаковка
  через Node DSH (`zlib.zstdDecompressSync`, без зависимости, но процесс на каждое чтение).

## Решения

- **D1 → наложение (решение А) реализуемо.** Источник — патч профиля `web`/`desktop`, перевод — по таблице
  «Строки наложения»; YAML — через `js-yaml` установки DSH, наложение — JSON.
- **Путь истории → файлы сессий** формата v4 (`.jsonl` и `.jsonl.zstd`), поля — «Формат сессий»;
  незнакомая версия — пояснение вместо истории (как в спеке).
- **Зависимости моста:** без PyYAML; `zstandard` только для Python < 3.14 (или распаковка через Node DSH —
  выбирает Task истории).
- **D3 → ветвь «только новый чат»:** выбор прав доступен новому чату (`DSH_PERMISSION_MODE` процессу,
  создающему сессию); у существующего — «как при создании» (`permission/preset` из файла).
- **D4 → каталог моделей и усилий** — пробной сессией с перенаправленными `root` хранилищ сессий и
  проекций во временный каталог моста; в `$DSH_HOME` пробная сессия не попадает.
- **D5 → слова отказа:** «Чат открыт в DeepSeek Harness на компьютере — закройте DeepSeek Harness и
  повторите» (Web держит чат до выхода из приложения); признак — `-32603` с `data.details`, содержащим
  `already owned by an active write handle`.
- **D6 →** Windows — поиск по `InstallLocation` в HKCU Uninstall и запуск RunAsNode командой выше; macOS —
  `/Applications/DeepSeek Harness.app` и ссылка `/usr/local/bin/dsh`; Linux — Desktop не поддерживается
  (AppImage). До кода Task поиска установок нужна живая установка Desktop на машине с доступом к
  `download.deepseek.com` (здесь CDN недоступен).
- **Провайдер srvllm** — проверить `manual`-тестом до выпуска: настоящая модель шлюза с инструментами.

## Приёмка 2026-11-23

`agent/tests/test_deepseek_live.py` провёл ход через ACP и `DeepSeekRunner` с временными проектом и профилем. Во всех
успешных прогонах проверены каталог моделей, наложение профиля, окончание хода, история, список чатов и состояние.

| Среда | DeepSeek Harness | Провайдер и результат |
| --- | --- | --- |
| Windows x64 | Desktop `0.2.2-rc.3` | srvllm, роль `local-gemma-abl`, профиль `desktop`. Тест прошёл за 14,25 с. |
| macOS 15.6.1 x64 | npm `0.2.2-rc.5`, Node `26.12.1` | Та же модель srvllm, профиль `web`, реальный вызов `bash`. Тест прошёл за 53,95 с. |
| macOS 15.6.1 x64 | Desktop `0.2.1-rc.1` из подписанного DMG | Та же модель srvllm, профиль `desktop`, запуск RunAsNode и чтение наложения из `app.asar`. Тест прошёл за 37,69 с. |
| Debian 13 x64 (WSL2) | npm `0.2.2-rc.5`, Node `26.12.1` | Детерминированный OpenAI-совместимый провайдер на loopback, профиль `web`. Он получил результат реального вызова `bash`. Тест прошёл за 42,35 с. |

В WSL2 попытка со шлюзом srvllm дошла до первого шага ACP и завершилась по лимиту хода 180 с. В это время был загружен
слот ComfyUI, а модель роли `local-gemma-abl` была выгружена. При повторной проверке перед завершением работы модель
оставалась выгруженной, поэтому для Linux использован локальный провайдер пробы.

Интерфейс раздела «Агенты» проверяется отдельными браузерными сценариями с подставным мостом в `tests/test_ui_deepseek.py`.
