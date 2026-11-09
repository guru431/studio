# CLAUDE.md — studio

Студия `https://studio.example.com` — многопользовательское приложение (PWA) с учётками, историей и восемью
разделами (Чат, Агенты, Фото, Видео, Разговоры, Озвучка, Сканы, Перевод), плюс пакетные скрипты для Windows и Linux —
клиенты API картинок и видео LLM-шлюза srvllm. Что умеет и как пользоваться — [README.md](README.md); главная спека —
[2026-10-19-studio-pwa-design](docs/superpowers/specs/2026-10-19-studio-pwa-design.md); как устроено по областям —
[docs/architecture/](docs/architecture/README.md); названия интерфейса — [docs/glossary.md](docs/glossary.md).
`origin` (`git.example.net/xadmin/studio`) приватный, Gitea — серверное pull-зеркало, но **основная ветка после
очистки публикуется на GitHub**: в отслеживаемые файлы — только примеры адресов, имён и путей (`studio.example.com`,
`10.77.1.x`), реальные — в игнорируемом `deploy/local-profile.sh` ([docs/publication.md](docs/publication.md)).
Решение владельца 2026-11-04: `cld` он обновляет вручную; агент не проверяет, не синхронизирует и не пушит `cld`.
Журналы `FINDINGS.md`/`IDEAS.md` вне Git (`.gitignore`): след закрытия записи — сообщение коммита с её заголовком.

## Граница с llm_routers

- Выделен из `llm_routers` 2026-10-19: страница — бывший `srvllm/deploy/services/studio/` (служба
  `srvllm-studio-web` на srvllm, :8151), скрипты — `srvllm/client/`, тесты — `srvllm/tests/test_studio*.py`; история
  до выделения и спеки (`docs/superpowers/specs/2026-10-16-srvllm-studio-design.md`, `2026-10-19-images-api-lustify-design.md`,
  `2026-10-19-srvllm-studio-video-design.md`) — там же; вики — `local-config/wiki/projects/llm_routers/architecture-srvllm-studio.md`.
- 2026-10-20 страница `llm.example.com/studio/` (`app/server.py`, вход ключом шлюза, история в памяти) заменена
  приложением на `studio.example.com`; старая — в `git log`; перенаправление `/studio/` — vhost `llm_routers`.
- Здесь только клиенты. ComfyUI, переводчик `srvllm-lustify-api`, слот `comfy`, маршруты шлюза `/slot/images/*`,
  `/slot/videos`, `/pages/ocr`, `/pages/ocr-release` (слот `ocr`), потребитель `owner-images` — в `llm_routers`. Смена
  контракта API (поля, модели, размеры, пределы) — сначала там, потом здесь: имена моделей и размеры сверяют тесты скриптов.
- `app/image_upload.py` — копия модуля переводчика на 2026-10-19: правка пределов не выходит за пределы переводчика.
  В обоих `warnings.catch_warnings` не потокобезопасна (оставляет фильтр `error` на процесс): студия защищена на
  местах чтения (`images._picture`, `claude_code._image_bytes`); оригинал в `llm_routers` стоит поправить там.

## Инварианты

- **Приватность — контракт. История — в базе и на шаре, в журнал — ничего.** Промпты, ключи, пароли, тексты
  сообщений и документов, картинки, ролики, имена файлов, пути машин в журнал не пишутся (uvicorn `--no-access-log`,
  журнал — события с id, кодами и числами). Сторожат тесты журналов разделов. Тексты агентских чатов в базе бывают
  только в снимке ссылки «Поделиться» и только шифром `STUDIO_DATA_KEY` ([sharing-export](docs/architecture/sharing-export.md)).
- **Анонимны только**: `/health`, `/login` (страница и вход), `/logout`, `/robots.txt`, оболочка PWA (`/static/*`,
  `/manifest.webmanifest`, `/sw.js`, `/offline.html`, значки из корня `/apple-touch-icon.png`,
  `/apple-touch-icon-precomposed.png`, `/favicon.ico`) и два класса входов **со своей защитой**:
  - ссылка «Поделиться»: `/share` (оболочка) и `/share/unlock|check|file` (`chat_shares.py`): секреты — только телом
    JSON до 4096 байт (`chat_shares.public_guard`), чужой `Origin` — 403, PIN ссылки с паузами (5 на ссылку, 30 на IP
    за 15 мин) и общим пределом `chat_shares.FAILS_MAX`, read-токен в памяти страницы без прав API учётки, всё —
    `no-store`, `no-referrer`, `noindex`;
  - `auth.OPEN_API`: `/api/agent/enroll` (`POST` — вход логином и паролем со счётом и паузами `/login`; `DELETE` —
    отвязка ключом устройства в `Authorization`; чужой `Origin` — 403) и `/api/agent/package` (ZIP обновления — только
    `Authorization: Bearer` действующего устройства активной учётки).
  `/` без сессии уводит на вход; остальное `/api/*` — через `auth.guard` (сессия + CSRF у изменяющих). `auth.guard` —
  HTTP-middleware: WebSocket `/api/agent/link` проверяет ключ сам, и новый WebSocket-маршрут без своей проверки был бы
  анонимным. Страницы отвечают и на HEAD.
- **Пользователь видит только своё**: каждый запрос к истории, файлам и подключениям — с `user_id` сессии; панель
  администратора чужую историю не показывает.
- **Список разрешённых адресов** (панель администратора): только к ним ходит сервер (с учётом перенаправлений и
  разрешения имён; имя — только https и только публичные адреса). Исключения — только адреса, зашитые в `web.py`
  (`api.exa.ai`, YouTube через yt-dlp — правила в [chat.md](docs/architecture/chat.md))
  и в `push.py` (`push.HOSTS`), и фиксированный HTTPS `api64.ipify.org` моста. Ключ Exa — свой у учётки, шифром.
- **Путь к службе один — Apache debian**: служба слушает только unix-сокет `/run/studio/web.sock` и верит последнему
  `X-Forwarded-For`; порт TCP не открывать — счёт попыток входа подделывался бы заголовком.
- **Секреты** — vault `local-config/secrets/vault.env`, в Git — никогда; на хост — только потоком stdin (`grep … | ssh`),
  не в argv и не в вывод. Скрипты в репозитории без ключа (`$EmbeddedKey = ''`, `EMBEDDED_KEY = ""`, сторожат тесты),
  копии с ключом — `*.local.ps1`, `*.local.py` (в `.gitignore`).
- **Схема базы** — `db.VERSION`; новая база получает `db.SCHEMA` сразу, база прода — шаги `db.STEPS`, **только
  добавлять**: откат `install.sh` на прежний код (`.previous`) обязан работать с новой базой (таблица ниже).
- **Кодировки**: `client/*.ps1` — UTF-8 с BOM и CRLF (PowerShell 5.1 без BOM читает CP1251); `client/*.py` — UTF-8
  без BOM, LF, Python 3.8+ без сторонних пакетов; правка поведения — в обе версии сразу (`test_studio_{batch,video}_client.py`
  и `tests/test_client_parity.py` гоняют обе; `-PromptsFile` = `--prompts-file`); `deploy/*` — LF (`.gitattributes`).
- Защита входа от перебора — паузы на пару «логин + адрес» и на адрес; растущая задержка ответа параллельные
  попытки не тормозит — на неё не полагаться.
- **Единственные источники на странице**: имена, группы, подсказки и порядок разделов — только `sections-meta.js`
  (сервер `sections.NAMES`, `push.TITLE` сверяет `tests/test_sections_meta.py`); SVG-значки — только `icons.js`
  (локальные `ICONS` запрещены тестом); меню — `menu.js`; тема и акцент — `appearance.js`; цвета тем — один
  `light-dark()` при `color-scheme` (`[data-theme]` меняет только `color-scheme`); `:hover` — только внутри
  `@media (hover: hover)`. Сторожат `tests/test_css_tokens.py`, `tests/test_sections_meta.py`.

## Карта модулей (`app/`)

| Область | Модули | Подробно |
|---|---|---|
| Ядро | `main` (lifespan: `db.migrate`, `chat_shares.seal`, `sections.adopt`, `search.compose_once`, фоновые задачи), `db`, `config` (`config.REQUIRED`), `state`, `clock` (часы — `clock.NOW`, паузы — `clock.SLEEP`), `operations` (барьер создания истории), `security` (Fernet `STUDIO_DATA_KEY`), `static_assets`, `manage` (`create-admin`, `backup`, `verify-backup`) | [shell](docs/architecture/shell.md) |
| Вход и защита | `auth`, `profile`, `screenlock`, `lock_policy`, `account`, `security_log`, `admin` | [security-lock](docs/architecture/security-lock.md) |
| Подключения | `connections`, `sections`, `upstream`, `checks`, `capabilities` | [connections](docs/architecture/connections.md) |
| Чат | `chat`, `web`, `documents`, `cleaner_client` | [chat](docs/architecture/chat.md) |
| Ссылки и экспорт | `chat_shares`, `chat_exports` | [sharing-export](docs/architecture/sharing-export.md) |
| Фото, видео, файлы | `images`, `image_upload`, `videos`, `forms`, `chains`, `presets`, `media`, `takeout`, `history` | [media](docs/architecture/media.md) |
| Речь (раздел «Озвучка», «Разговоры», голосовой ввод) | `speech`, `audio`, `dictation`, `transcripts`, `voices`, `speeches`, `exports` | [speech](docs/architecture/speech.md) |
| Сканы и перевод | `ocr`, `ocr_render`, `translate`, `translate_text`, `translate_spot` | [ocr-translate](docs/architecture/ocr-translate.md) |
| Поиск, задания, уведомления | `search`, `jobs`, `push`, `ntfy` | [search](docs/architecture/search.md), [notifications](docs/architecture/notifications.md) |
| «Агенты» | `agents`, `agent_release`, `bridge`, `claude_code`, `workspace`, `code_activity`, `code_attention`, `code_usage`, `code_visualizations`, `code_actions`, `computer_info`, `desktop` | [agents](docs/architecture/agents.md), [agents-workspace](docs/architecture/agents-workspace.md) |
| Мост `agent/` | `studio_agent/*`, правила — [agent/CLAUDE.md](agent/CLAUDE.md) | [agent-bridge](docs/architecture/agent-bridge.md) |
| Страница: общее | `app/static/js/`: `app.js` (роутер, рельс `buildRail`, `announce`), `sections-meta.js` (реестр разделов), `icons.js` (значки), `menu.js` (меню и панели), `appearance.js` (тема, акцент, плотность, шрифт, масштаб), `settings.js`, `account-security.js` (сессии, аварийный выход, журнал), `mobile-navigation.js`; `theme.js` — до модулей | [shell](docs/architecture/shell.md), [security-lock](docs/architecture/security-lock.md) |
| Страница: «Агенты» | `code.js`, `code-panel.js`, `code-tabs.js`, `code-chat.js`, `code-inbox.js` (входящие), `code-workspace.js`, `code-files.js`, `code-git.js`, `code-picker.js` и др. | [agents-workspace](docs/architecture/agents-workspace.md) |

Фронтенд без сборки — `app/static/` (модули ES, библиотеки копией в `static/vendor/`, без CDN; importmap с SHA-CSP,
снимок статики в памяти — после правки статики служба и тестовый процесс требуют перезапуска). Протокол моста —
[2026-10-28-studio-code-bridge-protocol](docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md).
Хранение: база SQLite `/var/lib/studio/studio.db` (локальный диск), файлы — шара (на debian
`/mnt/storage/SharedData/studio`); токены подключений и ключи — шифром `STUDIO_DATA_KEY`, пароли и PIN — scrypt.

## Хост и выкладка

Хост debian `10.77.1.10`: `ssh -i ~/.ssh/studio_deploy_key -p 22222 deploy@10.77.1.10`, passwordless sudo. Код и
`.venv` (Python 3.13) — `/opt/services/studio`; служба `studio-web.service` от `studio` (`ProtectSystem=strict`, запись
— только `/var/lib/studio` и шара, `MemoryMax=2G`); секрет — `/etc/studio/studio.env` (0600 root: `STUDIO_DATA_KEY=` и
необязательная `STUDIO_CLEANER_CA=`); копия базы — `studio-backup.timer` 04:30, 7 последних на шаре; журнал —
`sudo journalctl -u studio-web`, `-u studio-backup`. Подробно (шара `soft`, ffmpeg, yt-dlp, маршрут api.exa.ai,
verify-backup и восстановление, Apache) — [deploy-host](docs/architecture/deploy-host.md).

    bash deploy/deploy.sh                    # HEAD → debian, install.sh, проверка через Apache
    bash deploy/deploy.sh --new-secret       # заново положить STUDIO_DATA_KEY из vault
    bash deploy/deploy.sh --create-admin     # первый администратор и его подключение «srvllm» из vault

Выкладывается закоммиченный HEAD (`git archive` частей `app deploy requirements.txt`, мост — ZIP `HEAD:agent` и EXE
`deploy/build-agent-installer.ps1`); грязное дерево выкладываемых частей и `agent/` — отказ. `install.sh` собирает
среду рядом, проверяет шару, md2pdf и `apache2ctl -t`, держит прежние код и среду в `.previous` до ответа `/health`;
отказ — `service_failed` без автоматического отката. Смоук — `https://studio.example.com/health`. Windows-мост после
выкладки сервера не перезапускать; активный мост не завершать (Stop-Process/kill) из выполняемого им задания.

## Откат по шагам схемы

Прежний код (`.previous`) на новой базе. SQL — служба остановлена, `sudo -u studio sqlite3 /var/lib/studio/studio.db`,
с `PRAGMA foreign_keys=ON;` для каскадов; «стёршим» — тем, кто стирал историю прежним кодом, после возврата вперёд
(или стереть историю снова). Развёрнуто — [deploy-host](docs/architecture/deploy-host.md#откат-по-шагам-схемы--подробности).

| Шаг | Что добавил | Что делает прежний код | SQL-обход |
|---|---|---|---|
| 16 | `scans`, раздел `ocr` | `PATCH` подключения srvllm без `sections` — 422; поиск с документом ocr — 500; стирание не уносит сканы; вложение со сканом — «None» | `DELETE FROM section_links WHERE section='ocr'; DELETE FROM section_defaults WHERE section='ocr'; DELETE FROM search_docs WHERE section='ocr';` вперёд — вернуть индекс (deploy-host); стёршим — `DELETE FROM scans WHERE user_id IN (…)` |
| 17 | PIN учётки | не читает: открывают пароль и passkey | — |
| 18 | `translations` | раздела нет; стирание не уносит переводы; удаление скана уносит файл перевода | стёршим — `DELETE FROM translations WHERE user_id IN (…)` |
| 19 | `agents`, `code_chats`, `push_subs` | раздела нет; мост — отказ рукопожатия, переподключается; отозвать нельзя; стирание не уносит `code_chats` | стёршим — `DELETE FROM code_chats WHERE user_id IN (…)`; срочный отзыв — `DELETE FROM agents WHERE id = …` |
| 20 | `code_tabs`, `code_place` | рабочего места нет, стирание их не уносит | стёршим — `DELETE FROM code_tabs …; DELETE FROM code_place …` по `user_id` |
| 21, 23 | `code_chats.engine` (codex, opencode) | чаты Codex/OpenCode — как Claude, без истории | — |
| 22, 25 | `users.code_defaults`, `code_quick_actions` | не читает: умолчания CLI, без быстрых действий | — |
| 24 | область и таймеры блокировки | прежняя блокировка «на телефоне», всё приложение, прежние сроки PIN | — |
| 26 | Cleaner | подключение Cleaner — обычное `openai` без предпросмотра (не проверялось — не выбирать его модели); очередь удаления копится триггерами и доудаляется после возврата | — |
| 27 | `users.timezone` | дата по Москве (UTC+3) для всех | — |
| 28 | `code_attention` | не читает; стирание не уносит | стёршим — `DELETE FROM code_attention WHERE agent_id IN (SELECT id FROM agents WHERE user_id IN (…))` |
| 29, 30 | ntfy | шаг 29: ntfy нет; шаг 30: общая `ntfy_settings` выключена миграцией, включение снова шлёт на общую тему | — |
| 31 | `chat_turn_requests` | повтор POST хода не распознаётся — возможен второй вызов модели | — |
| 32 | `connections.s06` | подключение s06 — обычное `openai`; речь и сканы s06 не работают (не проверялось — не пользоваться) | — |
| 33 | `chat_shares` | ссылок нет; стирание удаляет `chats` — ссылки обычных чатов каскадом, **агентские (`chat_id NULL`) переживают** и снова открываются после возврата | стёршим — `DELETE FROM chat_shares WHERE user_id IN (…)` |
| 34 | сессии, `security_events` | сессии без `created/device/active`; журнал не пишется | — |
| 35 | `messages.error` | не читает; слова скана/хранилища — в `content`, как раньше | — |
| 36 | `chat_shares.sealed`, срок, счётчики, `password_reset` | получатель видит заглушку `chat_shares.PLACEHOLDER`; срок и общий предел PIN не проверяются, содержимого нет; новые ссылки по возвращении шифрует `seal()` (бессрочные) | — |

Новый код строки незнакомых разделов не видит (`connections._from_row`, `search.KNOWN`) — откат со следующего нового
раздела не даст 422 правки подключения и 500 поиска.

## Тесты

Контракт — `local-config/cron/project-registry.json::studio.tests`: наборы `main` (корень, `.venv`) и `agent`
(`agent/`, своя `.venv`, [agent/CLAUDE.md](agent/CLAUDE.md#тесты)); быстрый и полный уровни запускают оба.

    .venv/Scripts/python -m pytest -q                       # быстрый набор: 4 процесса xdist, бюджет 60 с (без нагрузки ~35–50 с)
    .venv/Scripts/python -m pytest -q -n0 <файл>            # один тест/файл без параллели
    .venv/Scripts/python -m pytest -q -m integration        # браузер (Edge/Chrome + node 22, Firefox, WebKit), PowerShell
    .venv/Scripts/python -m pytest -q -m manual             # живая приёмка после выкладки (команда — testing.md)

- Точечно: s06 — `tests/test_s06.py` и соседи; Cleaner — `tests/test_cleaner.py`, сквозной — `-m integration
  tests/test_cleaner_contract.py tests/test_ui_cleaner.py` (нужен `cleaner/.venv` или `CLEANER_TEST_REPO`). Полные
  команды — [testing](docs/architecture/testing.md).
- Документация: `tests/test_docs.py` (быстрый) сверяет `` `модуль.символ` `` в CLAUDE.md и `docs/architecture/*.md`
  с кодом (AST, без импорта), пары «`модуль.КОНСТАНТА` (значение)» и относительные ссылки. Значения констант в текст
  пишутся только с модулем — тест их сверяет; без модуля — только имя.
- Без реального времени: часы — `clock.NOW`, паузы — `clock.SLEEP`; шлюз — `httpx.MockTransport` (`tests/fakes.py`);
  мост — `tests/fake_bridge.py` (`FakeMachine`, `InProcessBridge`, `SocketBridge`). Тест дольше 1 с — `integration`;
  `integration`/`manual` — группой `serial`, одним процессом. Срок теста `timeout = 30` (метод `thread`),
  `faulthandler_timeout = 20`; `--durations=0 --durations-min=1` в `addopts` — каждый прогон называет тесты дольше 1 с.
- Браузер — 320 и 1280 px; WebKit — `tests/test_ui_webkit.py` (`playwright install webkit`; с `page.route` —
  `service_workers="block"`).
- `STUDIO_ADMIN_PASSWORD` в vault — пароль восстановления, не рабочий: вход им на проде — 401, и из LAN пятая неудача
  за 15 минут запрёт владельцу вход из дома. Для живой приёмки — спросить владельца.
- Решение владельца 2026-11-03: подписка Claude остановлена — живые пробы Claude только после сообщения о
  восстановлении доступа.

## Ловушки

Полные формулировки — в «Ловушках» файлов docs/architecture.

- Apache: `ProxyPass` — на имя `studio-backend`, не `localhost`; `/api/chats/` и `/api/code/` (600 с, `flushpackets=on`,
  `no-gzip`), `/api/videos` (330 с) и `/api/agent/link` (`upgrade=websocket timeout=300`) — раньше общего `ProxyPass /`;
  `LimitRequestBody` не меньше пределов службы. Сверяет `tests/test_deploy.py`.
- SQLite — только локальный диск (на CIFS портится); копия на шару — готовым файлом. `studio-backup.service` — те же
  `User`/`Group`/`StateDirectory`, что у службы.
- Карта одна: отмена задания запрос к шлюзу не обрывает (`release_after_cancel`); аренда модели картинок 600 с — перед
  запросом release другой модели; release после отмены — токеном учётки, удаление учётки ждёт `images.settle_user`.
  `asr`/`tts-qwen` без release — `speech.free_card` и повтор на 503. Слот `ocr` (6656 МиБ) — `ocr._settle` после
  документа, модель чата и перевода — только после `ocr.idle` ([ocr-translate](docs/architecture/ocr-translate.md#ловушки)).
- Список роликов шлюза общий для всех ключей: читать только для ролика с seed студии (`videos._adopt`); seed
  пользователя ничего не доказывает — `LOST_MANUAL`. Не ослаблять.
- HTTP-клиент службы (`state.make_http`) один на всех и cookie не хранит; наблюдатель роликов — задача на подключение
  (`videos.WATCHING`).
- Имя файла — заголовком `X-Studio-Name`, поисковый запрос — телом POST, ключи — только `Authorization`: строку
  запроса пишет журнал Apache. id ключа passkey в API — хеш credential id.
- «EN» промпта — маршрут описания `/slot/images/descriptions`, не чат-роль: у него scoped release gemma-abl.
- Блокировка — 423 `screen_locked`, пауза входа — 429 `locked`: не путать. Новый путь запертой сессии — только в
  `auth.SCREEN_LOCK_PATHS`. «Агенты» 423 не отдают: «компьютер закрыт» — 409 `closed`.
- `seen` сессии — только по `POST /api/lock/alive`. Варианты passkey готовятся заранее, `navigator.credentials.*`
  — первым в обработчике нажатия (Safari). Шторка и экран блокировки — `<dialog>` через `showModal()`.
- Запись голоса одна на студию (`mic.js`): смена раздела, шторка и блокировка зовут `cancelRecording()`.
- Ключ VAPID — в базе шифром: смена `STUDIO_DATA_KEY` — новый VAPID, все `push_subs` стёрты; ссылки «Поделиться» —
  «недоступна»; токены подключений — ввести заново.
- Модуль раздела — `claude_code.py`, не `code.py` (заслонил бы стандартный `code`, pytest — INTERNALERROR).
- `ntfy` не импортирует `upstream` при загрузке (цикл main → bridge → push → ntfy); после правки цепочки импорта —
  `import main` на Python хоста (3.13).
- Роутер ставит метку `{entry: n}`: раздел меняет адрес через `history.replaceState(history.state, …)`, не `null`.
- Плашка пути «Агентов» — `#code-place`; `#code-path` — путь папки «Файлов».
- Меню — popover (`menu.js`): в тестах открытое — `:popover-open`, а не `hidden`; Escape закрывает только верхнее меню
  и до обработчиков раздела не доходит. Меню аватара — панель без `role="menu"`.
- Переименование раздела — только в реестре (`sections-meta.js`, `sections.NAMES`): id и адрес остаются (`#/speech` —
  «Озвучка»); старые адреса настроек — через `settings.js` `OLD_IDS` (`other` → `security`, `dictation` → `input`).
- На хосте разработки Chromium сообщает `prefers-reduced-motion: reduce`: сценарии анимаций это учитывают.
- PDF экспорта: дочернему Chromium `HOME`/`XDG_*` — во временный каталог (у службы HOME `/nonexistent`).
- Python 3.14: `settle` тестов после `gather` уступает цикл `asyncio.sleep(0)`, иначе `while TASKS` зацикливается.
- Мобильная `.code-panel` — `align-self: stretch` (WebKit иначе обрезает проекты).
