# CLAUDE.md — studio

Студия `https://studio.example.com` — многопользовательское приложение (PWA) с учётками, историей и девятью
разделами (Чат, Фото, Видео, Озвучка, Расшифровка, Сканы, Перевод, Open WebUI, Агенты), плюс пакетные скрипты для Windows и Linux —
клиенты API картинок и видео LLM-шлюза srvllm. Описание — [README.md](README.md) и [README.ru.md](README.ru.md),
подробное руководство — [docs/guide.ru.md](docs/guide.ru.md); главная спека —
[2026-10-19-studio-pwa-design](docs/superpowers/specs/2026-10-19-studio-pwa-design.md); как устроено по областям —
[docs/architecture/](docs/architecture/README.md); названия интерфейса — [docs/glossary.md](docs/glossary.md).
`origin` (`git.example.net/xadmin/studio`) приватный, Gitea — серверное pull-зеркало, но **основная ветка после
очистки публикуется на GitHub**: в отслеживаемые файлы — только примеры адресов, имён и путей (`studio.example.com`,
`10.77.1.x`), реальные — в игнорируемом `deploy/local-profile.sh` ([docs/publication.md](docs/publication.md)).
Решение владельца 2026-11-04: `cld` он обновляет вручную; агент не проверяет, не синхронизирует и не пушит `cld`.
Журналы `FINDINGS.md`/`IDEAS.md` вне Git (`.gitignore`): след закрытия записи — сообщение коммита с её заголовком.

## Версии выпусков

Версия самой Studio — `app/release.py::VERSION`, начинается с 0.1.0; версия моста Windows/macOS/Linux независима.
При выпуске изменений сервера или интерфейса повышать версию Studio: patch для исправлений,
minor для возможностей, major для несовместимых изменений. `/health` и `/api/me` сообщают версию
работающего процесса, версия видна в меню пользователя и в настройках под навигацией. При знакомстве канал передаёт
`welcome.studio_version`: мост показывает её только для текущего соединения, без записи в конфиг.

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
  `/manifest.webmanifest` (и английский `/manifest.en.webmanifest`), `/sw.js`, `/offline.html`, значки из корня `/apple-touch-icon.png`,
  `/apple-touch-icon-precomposed.png`, `/favicon.ico`) и четыре класса входов **со своей защитой**:
  - ссылка «Поделиться»: `/share` (оболочка) и `/share/unlock|check|file` (`chat_shares.py`): секреты — только телом
    JSON до 4096 байт (`chat_shares.public_guard`), чужой `Origin` — 403, PIN ссылки с паузами (5 на ссылку, 30 на IP
    за 15 мин) и общим пределом `chat_shares.FAILS_MAX`, read-токен в памяти страницы без прав API учётки, всё —
    `no-store`, `no-referrer`, `noindex`;
  - личная ссылка на файл «Моих файлов»: `/f` (оболочка) и `/f/check|open|file` (`my_files.py`): токен — только во
    фрагменте адреса и телом JSON до 4096 байт (`chat_shares.public_guard`), в базе — sha256, путь и имя — шифром,
    срок `my_files.LINK_SEC` (86400); чужой `Origin` — 403; неизвестный, истёкший, отозванный токен, сменённый ключ
    сервера, снятый доступ владельца, исчезнувший файл — одинаковый 404; неудачи — пауза по адресу (`my_files.FAILS`
    (30) за `my_files.FAIL_SEC` (900), потом 429 `file_locked`); файл — по cookie `studio_file` (`my_files.OPEN_SEC`
    (600), только `/f/file`, значение — шифром `STUDIO_DATA_KEY` id ссылки и время выдачи), каждый запрос проверяет ссылку заново; всё — `no-store`,
    `no-referrer`, `noindex`;
  - почта учётки `/account/*` (`email_accounts.py`): страницы и JSON-действия приглашения, восстановления и
    кодов; свой Origin, предел тела, одноразовые хешированные токены, сроки и паузы — [email-accounts](docs/architecture/email-accounts.md);
  - `auth.OPEN_API`: `/api/agent/enroll` (`POST` — вход логином/email и паролем, при включении ещё кодом, со счётом и паузами `/login`; `DELETE` —
    отвязка ключом устройства в `Authorization`; чужой `Origin` — 403) и `/api/agent/package` (ZIP обновления — только
    `Authorization: Bearer` действующего устройства активной учётки) и `/api/agent/linux-installer`
    (только Linux-установщик по временному Bearer-пропуску действующей сессии).
  `/` без сессии уводит на вход; остальное `/api/*` — через `auth.guard` (сессия + CSRF у изменяющих). `auth.guard` —
  HTTP-middleware: WebSocket `/api/agent/link` проверяет ключ сам, и новый WebSocket-маршрут без своей проверки был бы
  анонимным. Страницы отвечают и на HEAD.
- **Пользователь видит своё и явно выданные проекты «Агентов»**: история остальных разделов, файлы и подключения
  фильтруются по `user_id` сессии; панель администратора чужую историю не показывает — как и чужие корзину
  (`trash`), «Мои файлы» и ссылки на файлы (`my_files`), память (`memory`) и личные базы знаний (`knowledge`). Совместные проекты —
  `project_access.py`, [project-collaboration](docs/architecture/project-collaboration.md): права проверяются на
  сервере и мосту, PIN компьютера участнику не передаётся.
- **Список разрешённых адресов** (панель администратора): только к ним ходит сервер (с учётом перенаправлений и
  разрешения имён; имя — только https и только публичные адреса). Исключения — только адреса, зашитые в `web.py`
  (`api.exa.ai`, YouTube через yt-dlp — правила в [chat.md](docs/architecture/chat.md))
  и в `push.py` (`push.HOSTS`). Ключ Exa — свой у учётки, шифром. Почта учётки — на сервер SMTP, заданный
  администратором в панели или переменными окружения (в том числе частный адрес `connect_ip`, [email-accounts](docs/architecture/email-accounts.md)).
  Подключение через Cleaner (`cleaner_route.routes`: переключатель «Через Cleaner» или принудительный режим) — не
  исключение: сервер ходит только к Cleaner (его адрес — в списке), а данные доходят до провайдера, которого в списке
  нет, через Cleaner и его проверки (только публичные https-адреса, соединение с проверенным адресом). Частные
  IP-адреса списка — внутренние (`cleaner_route.is_external`, и для шлюзов s06 и srvllm: флажок и вид внутренним не
  делают): шлюзы в облачные модели в список не добавлять.
- **Защищённый источник MCP** (`mcp_servers.protected`, только общий сервер): после его успешного вызова диалог
  навсегда без «Интернета» (`chats.protected_used`), веб-инструменты MCP снимаются в том же ходе, вызов с адресом
  в аргументах (`mcp_client.has_url`: и без схемы — `evil.com/x`, IPv4, `xn--`) — отказ без запроса. Мимо хода тоже
  ничего: ссылка «Поделиться», экспорт и архив такого диалога — 409 `protected` (`chat.unprotected`), его прежняя
  ссылка недоступна навсегда, выгрузка истории — без его сообщений; ссылка «Моих файлов» на файл защищённого сервера —
  409, прежняя — одинаковый 404. Не ослаблять: иначе данные источника уходят в сеть
  ([chat](docs/architecture/chat.md)).
- **Мост ходит сам** (кроме студии) только на фиксированные HTTPS-адреса `agent/studio_agent/limits.py::HOSTS` (сверяет
  `tests/test_docs.py`): `api64.ipify.org` (внешний IP, без данных); `registry.npmjs.org`, `marketplace.visualstudio.com`,
  `formulae.brew.sh`, `downloads.claude.ai` (версии программ, `software.py`: имя пакета); `api.anthropic.com` (лимиты
  подписки Claude — OAuth-токен Claude Code); `opencode.ai` (лимиты OpenCode Go — ключ OpenCode); `api.deepseek.com` (баланс DeepSeek — ключ DeepSeek, только при официальном провайдере и без своего `DEEPSEEK_BASE_URL`); `github.com`
  (анонимная проверка публичной копии: путь `owner/repo` из `origin`/`github`, без учётных данных). Ещё — шлюз
  OpenCode из конфига пользователя (https или `http://` частной сети) с его ключом; обновление CLI — штатные установщики.
- **Путь к службе один — Apache debian**: приложение в Docker слушает только unix-сокет `/run/studio/web.sock`.
  nginx контейнера принимает запросы только от адреса Apache и передаёт его `X-Forwarded-For` без добавления своего
  адреса. Прямого TCP-входа в приложение нет: счёт попыток входа нельзя обходить поддельным заголовком.
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
  (локальные `ICONS` и `<svg` вне него запрещены тестом); меню — `menu.js`; тема и акцент — `appearance.js`; цвета тем — один
  `light-dark()` при `color-scheme` (`[data-theme]` меняет только `color-scheme`); `:hover` — только внутри
  `@media (hover: hover)`. Сторожат `tests/test_css_tokens.py`, `tests/test_sections_meta.py`.

## Карта модулей (`app/`)

| Область | Модули | Подробно |
|---|---|---|
| Ядро | `main` (lifespan: `db.migrate`, `chat_shares.seal`, `sections.adopt`, `search.compose_once`, фоновые задачи), `db`, `config` (`config.REQUIRED`), `state`, `clock` (часы — `clock.NOW`, паузы — `clock.SLEEP`), `operations` (барьер создания истории), `security` (Fernet `STUDIO_DATA_KEY`), `static_assets`, `release` (версия Studio), `i18n_words` (слова сервера по-английски для выгрузок, как `StudioI18n.message`), `manage` (`create-admin`, `backup`, `verify-backup`) | [shell](docs/architecture/shell.md) |
| Вход и защита | `auth`, `totp` (приложение-аутентификатор и резервные коды), `account_profile`, `screenlock`, `lock_policy`, `account`, `session_devices`, `security_log`, `admin` | [security-lock](docs/architecture/security-lock.md) |
| Почта учётки | `email_accounts`, `account_mail`: обязательный email, приглашения, восстановление, коды входа | [email-accounts](docs/architecture/email-accounts.md) |
| Подключения | `connections`, `sections`, `upstream`, `checks`, `capabilities`, `access` (группы доступа к общему), `cleaner_route` (внешние модели через шлюз Cleaner: настройки администратора, служебный ключ шифром), `profiles` (профиль подключения: системный текст, параметры запроса, подсказки) | [connections](docs/architecture/connections.md) |
| Чат | `chat`, `chat_pending` (пауза хода: подтверждение инструмента, вопрос `ask_user`), `chat_tools` (встроенные инструменты хода), `chat_system` (системное сообщение: дата, профиль, роль, правила MCP), `roles` (роли диалога и их умолчания), `web`, `documents`, `cleaner_client`, `mcp_client` (подключения MCP, инструменты хода), `mcp_sources` (кэш каталогов MCP, пометки и политика инструментов, MCP диалога по умолчанию), `mcp_sections` (инструменты MCP разделов), `mcp_files` (файлы диалога на MCP с согласием, медиа диалога, файлы результатов), `mcp_contracts` (контракты Studio для любых MCP: возможности из `experimental`, файлы пользователя, состояние источника), `help` («Справка»: руководство Studio и руководства MCP), `memory` (личная память, шифром), `knowledge` (большие вложения и базы знаний: куски, BM25, эмбеддинги, переранжирование) | [chat](docs/architecture/chat.md), [mcp-contracts](docs/architecture/mcp-contracts.md) |
| Ссылки и экспорт | `chat_shares`, `chat_exports`, `chat_export_selection`, `my_files` («Мои файлы» на серверах MCP, личная ссылка на файл на сутки и анонимный `/f`) | [sharing-export](docs/architecture/sharing-export.md), [mcp-contracts](docs/architecture/mcp-contracts.md) |
| Фото, видео, файлы | `images`, `image_upload`, `videos`, `forms`, `chains`, `presets`, `media`, `takeout`, `history`, `trash` (корзина истории) | [media](docs/architecture/media.md) |
| Речь (раздел «Озвучка», «Расшифровка», голосовой ввод) | `speech`, `audio`, `dictation`, `transcripts`, `voices`, `speeches`, `exports` | [speech](docs/architecture/speech.md) |
| Сканы и перевод | `ocr`, `ocr_render`, `ocr_pdf` («PDF со слоем»), `translate`, `translate_text`, `translate_spot` | [ocr-translate](docs/architecture/ocr-translate.md) |
| Поиск, задания, уведомления | `search`, `jobs`, `card` (очередь карты шлюза), `push`, `ntfy` | [search](docs/architecture/search.md), [notifications](docs/architecture/notifications.md) |
| «Агенты» | `agents`, `agent_release`, `bridge`, `claude_code`, `workspace`, `code_activity`, `code_attention`, `code_usage`, `code_visualizations`, `code_actions`, `computer_info`, `desktop`, `terminal_links`, `project_access` (участники), `project_mirrors` (зеркала), `mirror_groups` (группы копий зеркал) | [agents](docs/architecture/agents.md), [agents-workspace](docs/architecture/agents-workspace.md), [project-collaboration](docs/architecture/project-collaboration.md) |
| Open WebUI | `owui` (учётки Open WebUI), `owui_tokens` (постоянная сессия: долгий токен шифром, рабочий в памяти, обновление с ротацией), `owui_view` (ответы Open WebUI → вид Студии: ветка чата, элементы, пауза, события сокета), `owui_link` (соединение socket.io на учётку: события → SSE, отказ обратным вызовам, переподключение, закрытие по простою); страница — `owui.js`, `owui-panel.js` (учётки и дерево), `owui-chat.js` (лента и поле), `owui-pending.js` (карточка паузы) | [open-webui](docs/architecture/open-webui.md) |
| Мост `agent/` | `studio_agent/*`, правила — [agent/CLAUDE.md](agent/CLAUDE.md) | [agent-bridge](docs/architecture/agent-bridge.md) |
| Страница: общее | `app/static/js/`: `app.js` (роутер, рельс `buildRail`, `announce`), `sections-meta.js` (реестр разделов), `icons.js` (значки), `menu.js` (меню и панели), `appearance.js` (тема, акцент, плотность, шрифт, масштаб), `settings.js`, `account-security.js` (сессии, аварийный выход, журнал), `mobile-navigation.js`, `trash.js` («Корзина»), `my-files.js` («Мои файлы»), `help.js` («Справка»), `recorder.js` («Записать» встречу), `speech-read.js` («Прочитать»), `share-target.js` (принятое листом «Поделиться»); `file-link.js` — страница `/f`; `theme.js` — до модулей | [shell](docs/architecture/shell.md), [security-lock](docs/architecture/security-lock.md) |
| Страница: «Чат» | `chat.js`, `chat-pending.js` (карточка паузы хода), `chat-mcp.js`, `chat-files.js`, `chat-suggest.js` (подсказки), `chat-context.js` (кольцо контекста), `chat-voice.js` («Разговор»), `knowledge.js` (чип «Знания»), `chat-share.js`; `share-view.js` — страница `/share` | [chat](docs/architecture/chat.md), [sharing-export](docs/architecture/sharing-export.md) |
| Страница: «Агенты» | `code.js`, `code-panel.js`, `code-tabs.js`, `code-chat.js`, `code-workspace.js`, `code-files.js`, `code-git.js`, `code-opencode.js` и `code-deepseek.js` (настройки OpenCode и DeepSeek Harness в чате) и др.; общий выбор-кнопка разделов — `picker.js` | [agents-workspace](docs/architecture/agents-workspace.md) |

Фронтенд без сборки — `app/static/` (модули ES, библиотеки копией в `static/vendor/`, без CDN; importmap с SHA-CSP,
снимок статики в памяти — после правки статики служба и тестовый процесс требуют перезапуска). Протокол моста —
[2026-10-28-studio-code-bridge-protocol](docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md).
Хранение: база SQLite `/var/lib/studio/studio.db` (локальный диск Docker-хоста), файлы — шара
(`/media` внутри контейнера); токены подключений и ключи — шифром `STUDIO_DATA_KEY`, пароли и PIN — scrypt.

Языки интерфейса — русский и английский: `static/en.js` (словарь), `static/i18n.js` (ранний запуск),
`static/js/i18n.js` (`tr`, `message`, `locale`). Новые подписи — через `tr`, HTML — `data-i18n`;
пользовательский текст не переводить. Выбор языка — на устройстве (`studio-language`), подробнее — [shell](docs/architecture/shell.md#язык-интерфейса).

## Хост и выкладка

По умолчанию один и тот же коммит выкладывается на оба экземпляра Studio с проверкой каждого по его HTTPS-адресу.
Имена локальных профилей указаны в `deploy/local-profile.sh`. Для второго запуска передай его профиль через
`STUDIO_DEPLOY_PROFILE`. По явному указанию владельца можно ограничить выкладку одним экземпляром.

Основной запуск — Docker Compose на хосте `10.77.1.15`: контейнеры `web` и `ingress`, Python 3.13, Chromium,
ffmpeg и yt-dlp внутри образа. `web` работает от uid/gid 10001 с корнем только для чтения и пределом 2 ГБ;
база и медиа — внешние bind mounts, секрет — `/etc/studio/studio.env` (0600 root). `studio-docker.service`
поднимает Compose после монтирования шары, `studio-backup.timer` в 04:30 запускает согласованную копию базы
из контейнера и её проверку `verify-backup` (итог — в `settings`, провал — push администраторам); сохраняются
7 последних копий на шаре. Apache на прежнем хосте завершает HTTPS и проксирует
на ограниченный nginx контейнера. Профиль установки — `deploy/local-profile.sh`, `STUDIO_RUNTIME=docker`.
Подробно — [deploy-docker](docs/architecture/deploy-docker.md); прежняя нативная установка и подробности
совместимости схемы — [deploy-host](docs/architecture/deploy-host.md).

    bash deploy/deploy.sh                    # HEAD → Docker, install-docker.sh, проверка через Apache
    bash deploy/deploy.sh --new-secret       # заново положить STUDIO_DATA_KEY из vault
    bash deploy/deploy.sh --create-admin     # первый администратор и его подключение «srvllm» из vault
    bash deploy/deploy.sh --refresh-base     # Docker: образ без кэша — свежие база и пакеты Debian сразу

Выкладывается закоммиченный HEAD (`git archive` частей `app deploy requirements.txt constraints.txt docs/guide.ru.md` (руководство — для «Справки») и `.dockerignore` для Docker).
Мост — ZIP из `HEAD:agent` (плюс собранные запускатели macOS и Windows `Studio Agent.exe`) и три установщика. `deploy/build-agent-bundle.py` сверяет общий комплект
по хэшам артефактов и переиспользует его при совпадении дерева моста и скриптов сборки;
грязное дерево выкладываемых частей и `agent/` — отказ. `install-docker.sh`
собирает образ до смены `current`, перед новым шагом схемы делает копию базы в работающем `web`
(`manage.py backup`, отказ — выкладка прервана), проверяет тип хранилища медиа по профилю и здоровье обоих контейнеров; неудачный образ возвращает
предыдущий, сохраняя актуальную базу. `install-proxy.sh` проверяет конфигурацию Apache и перезагружает только
прокси. `--prepare-only` собирает образ без запуска контейнеров и переключения прокси. Сборка — `--pull`, слой apt
пересобирается первой выкладкой каждой ISO-недели (`ARG REFRESH`), таймера пересборки нет. Смоук —
`https://studio.example.com/health`. Windows-мост после
выкладки сервера не перезапускать; активный мост не завершать (Stop-Process/kill) из выполняемого им задания.

## Откат по шагам схемы

Прежний код (`.previous`, в Docker — `previous`) на новой базе. SQL — при остановленной службе, команды общие для обеих
установок: Docker — функция `sql` из [deploy-docker](docs/architecture/deploy-docker.md#восстановление-и-sql-обходы)
(Python образа от 10001:10001, остановить и таймер копии), нативная — `sudo -u studio sqlite3 /var/lib/studio/studio.db`;
с `PRAGMA foreign_keys=ON;` для каскадов (`sql` включает сама); «стёршим» — тем, кто стирал историю прежним кодом, после возврата вперёд
(или стереть историю снова). Развёрнуто — [deploy-host](docs/architecture/deploy-host.md#откат-по-шагам-схемы--подробности).
Ночная проверка копии на прежнем коде видит схему новее своей: код с `skipped` в `manage.verify_backup` — пропуск
без тревоги, более старый — провал с push каждую ночь до возврата вперёд ([deploy-docker](docs/architecture/deploy-docker.md#откат)).

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
| 37 | браузер, ОС, модель, имя и IP сессии | столбцы не читает и не меняет; вход и завершение сессий работают | — |
| 38 | `project_members`, `code_member_tabs` | совместные проекты не показывает, собственные проекты и вкладки работают | стёршим — `DELETE FROM code_member_tabs WHERE user_id IN (…)` |
| 39 | `agent_events.kind` шире: `member_add`, `member_remove` | история подключений показывает их кодом; выдачу не журналирует; аварийный выход выдачи не снимает; участник снова может создать ссылку на чат владельца — при возврате вперёд её удалит `chat_shares.disown()` | после аварийного выхода прежним кодом — `DELETE FROM project_members WHERE agent_id IN (SELECT id FROM agents WHERE user_id = …)` |
| 40 | Email, коды, приглашения, почтовые токены и паузы | Не требует email/кодов, возвращает прежнее создание учёток; почтовая защита при откате не действует | Не нужен; данные сохраняются |
| 41 | `code_chats.origin` | столбец не читает; происхождение определяет по метке моста; новые строки получают `other` | — |
| 42 | `voice_samples` | читает выбранный образец из прежних полей `voices`; новые голоса получают образец триггером; экспорт дополнительных образцов недоступен, чистка сирот при незнакомом столбце отключена | Не нужен; дополнительные образцы сохраняются до возврата вперёд |
| 43 | `mcp_servers`, `chats.mcp_ids` | подключения MCP и выбор инструментов не читает; чаты и остальные подключения работают | Не нужен; настройки сохраняются до возврата вперёд |
| 44 | `videos.model`, по умолчанию `wan` | новые ролики H3 показывает как Wan; новые модели до возврата вперёд не запускать и не повторять | Не нужен; история сохраняется |
| 45 | `speeches.chat_id`, `chat_created`, `voice_name` — продолжения озвучки | показывает отдельные записи; триггер заполняет новые поля при вставке прежним кодом | Не нужен; записи и группы сохраняются до возврата вперёд |
| 46 | `agents.platform` — последняя известная ОС для установщика отключённого компьютера | прежний код пропускает новое поле | Не нужен |
| 47 | `f5-tts` в связях «Озвучки» с отмеченным Qwen3-TTS | прежняя Studio озвучит выбор F5 через Qwen3-TTS | Перед откатом снять отметку F5-TTS в разделе «Озвучка» |
| 48 | `project_mirrors` — зеркала проектов (адрес и база шифром) | не читает: зеркал не показывает; отзыв компьютера уносит их каскадом | Не нужен; зеркала сохраняются до возврата вперёд |
| 49 | `connections.id` — `AUTOINCREMENT` (пересборка с сохранением связей разделов) | работает как прежде, id тоже не повторяются; удаление подключения не обнуляет `connection_id` диалогов — их ход отвечает 404 вместо просьбы выбрать подключение | Не нужен |
| 50 | модель ролика в поиске — из `videos.model` (триггеры `search_videos_*`, прежние строки) | поиск и фильтр по модели видят H3 как H3; выгрузка истории — без модели ролика | Не нужен |
| 51 | `push_subs.lang`, `ntfy_devices.lang` — язык уведомлений устройства | столбец не читает: push и ntfy по-русски на всех устройствах; новые подписки и настройки ntfy получают `ru`, правка прежней язык не меняет | Не нужен; после возврата вперёд страница снова сверит язык при запуске |
| 52 | `speeches.queued`, `previous`, `previous_media_id`; поиск озвучки — и по смене модели | столбцы не читает: очередь — по `created`, «Озвучить заново» сразу стирает прежний mp3; повторную озвучку, начатую новым кодом, при отказе оставит без звука, а прежний mp3 держит `previous_media_id` до возврата вперёд (чистка сирот прежнего кода с незнакомым столбцом не идёт) | Не нужен; после возврата вперёд прежний mp3 уйдёт чисткой сирот, когда запись озвучат заново или удалят |
| 53 | `chat_shares.label` — название чата «Агентов» у ссылки (шифром) | столбец не читает: в «Доступах и ссылках» чаты «Агентов» — «компьютер · проект» без названия; новые ссылки — без названия и после возврата вперёд | Не нужен |
| 54 | `code_attention_seen` — «непрочитано» «Агентов» по учётке | таблицу не читает: отметка снова одна на всех (общий `code_attention.seen`, новый код его тоже ставит); стирание истории владельца уносит строки каскадом | Не нужен; после возврата вперёд просмотры, сделанные прежним кодом, учётке не засчитаны — их завершения снова «непрочитаны» |
| 55 | Общие API моделей и MCP (`user_id IS NULL`) | видит личные подключения; диалог с общей моделью нужно переключить на личную. Если личным умолчанием была выбрана общая модель, заново выбрать умолчание в прежней версии | Не нужен; общие интеграции сохраняются до возврата вперёд |
| 56 | Группы доступа: `access_groups`, `access_group_members`, `access_rules`, `access_rule_groups` | таблицы не читает: закрытые группами общие API, MCP и инструменты MCP открываются всем учёткам; снятия моделей мало — повтор старого скана берёт подключение из записи скана | Перед откатом, пока `current` — 0.39.0+: `sudo docker run --rm --network none --entrypoint python "$IMAGE" -c 'import sys; sys.path.insert(0, "/opt/studio/app"); import access; print(access.ROLLBACK_56)' \| sql` — ключ общего API с правилом сохраняется в `access_rollback` и портится (прежний код: «токен не расшифровался»), общие MCP с правилами выключаются; после возврата вперёд — `access.FORWARD_56` выполняется сам при старте службы (`access.forward`; ключ, введённый заново прежним кодом, и включённый им MCP не затираются) |
| 57 | `connection_profiles`, `role_defaults`, `chat_roles`, `users.roles_seeded`, `chats.role_id`, `mcp_off`, `mcp_tools_off`, `mcp_uploads`, `chats.mcp_file_consent`, `mcp_tool_policy`, `chat_media`, `mcp_media_uploads` | не читает: системное сообщение только с датой, параметры не шлёт, ролей нет; общие MCP снова выключены по умолчанию (действуют только `mcp_ids`), выключенные инструменты снова предлагаются модели; вложения на MCP не грузит; файлы результатов под ответом не показывает; согласия на передачу файлов, политику инструментов и медиа диалогов не читает (медиа диалогов не видны; чистка сирот с незнакомым `chat_media.media_id` не идёт) | Не нужен; данные сохраняются до возврата вперёд |
| 58 | `chat_pending`, `messages.todo`, корзина (`deleted`), `file_links`, `mcp_servers.protected`, `chats.protected_used`, память (`user_memory`, `users.memory_on`), знания (`knowledge_bases`, `kb_*`, `chat_kbs`), язык расшифровки и озвучки | ждущие подтверждения ответы выглядят законченными; плана нет; записи из корзины видны как обычные; ссылок на файлы, памяти и знаний нет; прежний код допускает «Интернет» в диалогах с защищёнными данными; выбранный язык речи не применяется; стирание истории оставляет память и ссылки на файлы | при остановленных службе и таймере копии выполнить `trash.ROLLBACK_58` через `sql`, как строку 56. При ошибке `no_protected_chats` прервать откат: есть живые диалоги с защищёнными данными. Успех означает окончательное удаление корзины и отключение защищённых MCP. Оставить их выключенными в старом выпуске. При возврате в выпуск с этой проверкой `trash.forward` восстановит ранее включённые. Стершим историю в старом выпуске — `DELETE FROM user_memory WHERE user_id IN (…); DELETE FROM file_links WHERE user_id IN (…)` |
| 59 | `code_place.pinned_projects` — закреплённые проекты учётки | старый код пропускает поле и оставляет сохранённые закрепления до возврата | Не нужен |
| 60 | `transcripts.listen_media_id` — MP3 для плеера и повтора | старый код берёт `source_media_id`, после удаления исходника там тот же MP3 | Не нужен. Старая чистка сирот пропускается до возврата вперёд |
| 61 | `voice_sample_trash` и вид `voice_sample_items` — корзина отдельных образцов голоса (отметка — своей таблицей: прежний код вставляет образец без списка столбцов) | отметок не читает: образцы из корзины снова видны и годятся для озвучки; удаляет образец сразу (отметка уходит каскадом). Скан, ушедший в корзину вместе со своим переводом, показывает отдельной строкой корзины; «Вернуть» перевода его не возвращает | Перед откатом выполнить `trash.ROLLBACK_61` через `sql`, как строку 56: образцы из корзины удаляются окончательно, их файлы уберёт чистка сирот прежнего кода |
| 62 | `mcp_servers.dialog_default` — общий MCP «В диалоге по умолчанию» | столбец не читает: общие серверы без отметки снова действуют во всех диалогах (и уже включённые вручную остаются в `mcp_ids`); редактор администратора флажка не показывает и при сохранении его не меняет | Не нужен. Если инструменты такого сервера не должны предлагаться по умолчанию — перед откатом выключить сервер в панели |
| 63 | `mirror_groups`, `mirror_copies`, `mirror_pairs`, `mirror_legacy` — зеркала группой копий; строки `project_mirrors` переносит `mirror_groups.adopt` при старте | новые таблицы не читает: видит односторонние зеркала `project_mirrors` на момент обновления с прежними базами сравнения; копий, групп и синхронизаций нового кода не видит; после возврата вперёд его синхронизации не учтены (больше «локальных изменений»), отвязки и правки исключений тоже (отвязанное им зеркало снова видно — отвязать заново), а его новые зеркала `adopt` перенесёт | Не нужен |
| 64 | `code_chats.engine` (deepseek) — пересборка с CHECK четырёх помощников, столбцы и `origin` сохраняются | движка нет в `claude_code.ACCESS`: чаты DSH, присланные мостом, из списка проекта исчезают целиком; без названия видны только открытые отметки без сессии на машине (новый чат до первого хода); история, ход и «Поделиться» при мосте на связи — 500 (словарь имён `require_engine` без `deepseek`, KeyError), без связи — 409 `offline`; `state` и ход с непустыми правами — 500 и без связи (`ACCESS[engine]`); закрыть и открыть отметку можно. Поднятый в «Приоритете агентов» DeepSeek Harness (`users.code_defaults`, не шаг схемы) — прежний код отвечает 422 `priority` на любое сохранение умолчаний, после возврата вперёд — то же («от 1 до 4») | `DELETE FROM code_chats WHERE engine = 'deepseek';` — сессии остаются на компьютерах; «Приоритет агентов» — пересохранить |
| 65 | `owui_accounts` — учётки Open WebUI (адрес, кто подключён и постоянная сессия — шифром) | таблицу не читает: раздела нет; учётки сохраняются до возврата вперёд; удаление учётки Студии уносит их строки каскадом, но сессии «Studio» в Open WebUI не отзывает | Не нужен; оставшиеся сессии «Studio» можно отозвать в самом Open WebUI |
| 66 | `connections.via_cleaner`, `messages.cleaner_request`, `cleaner_rollback` — подключения через Cleaner; `cleaner_route_contexts` и `cleaner_deletions.route` — контексты шлюза Cleaner (служебный ключ — заголовком `X-Cleaner-Key`) | прежний код не знает столбца: ход такого подключения ушёл бы провайдеру напрямую; принудительный режим не соблюдается; удаление контекстов шлюза прежний код шлёт ключом в `Authorization` — Cleaner отвечает 401, и очередь откладывает весь адрес Cleaner на час (и удаления прежнего вида на нём); строки ждут возврата вперёд | перед откатом выполнить `cleaner_route.ROLLBACK_66` через `sql`, как строку 56: ключи таких подключений сохраняются и портятся (прежний код: «токен не расшифрован»); администратор решает, отключать ли внешние подключения; вперёд — `cleaner_route.FORWARD_66` выполняется сам при старте службы (`cleaner_route.forward`; ключ, введённый заново прежним кодом, не затирается) |
| 67 | `translations.cleaner_used` — признак работы через Cleaner, переживает удаление подключения и контекста | прежний код игнорирует признак и может передать перевод внешней модели речи | при откате использовать внутренние модели речи для переводов через Cleaner |
| 68 | `owui_file_drafts` — файлы, которые Studio загрузила в Open WebUI и ещё может удалить | прежний код таблицу не читает: удаление снятых вложений не поддерживает | Перед откатом очистить только отметки черновиков через `DELETE FROM owui_file_drafts;`, чтобы прежний код мог отправлять файлы без сохранения права их удалить |
| 69 | `chat_shares.item_kind`, `item_id` — ссылки «Поделиться» на расшифровку, перевод, скан, картинку и ролик; триггеры удаления записи | столбцы не читает: по такой ссылке после PIN получатель видит название записи и заглушку `chat_shares.PLACEHOLDER` («создала более новая версия Студии»), запись в корзине ссылку не закрывает (видна та же заглушка), файлы снимка отдаются только запросом с read-токеном (кнопок нет); в «Доступах и ссылках» — строка «Чат» без «Открыть», отключить можно; окончательное удаление записи уносит ссылку триггером | Не обязателен; чтобы получатели не видели заглушку — `DELETE FROM chat_shares WHERE item_kind IS NOT NULL;` (после возврата вперёд ссылки создать заново) |
| 70 | `users.totp_secret`, `totp_step`, `login_notice`, `totp_backup` — приложение-аутентификатор, резервные коды, письмо о входе с нового устройства | столбцы и таблицу не читает: учётки с приложением входят и привязывают мосты одним паролем (коды из письма — как были включены); письма о новом входе нет. Граница безопасности отката, как у шага 40 | Не нужен; приложение и резервные коды действуют снова после возврата вперёд. Тем, кто подключил приложение, перед откатом включить коды из письма |
| 71 | `messages.unchosen` — невыбранные варианты ответа чата (бывают только у последнего ответа диалога) | столбец не читает: варианты последнего ответа показывает подряд обычными ответами, отдаёт модели вместе с выбранным, находит поиском и кладёт в «Поделиться», экспорт и выгрузку; «Ответить заново» удаляет только самый новый вариант | Перед откатом, если варианты не нужны, — `DELETE FROM messages WHERE unchosen = 1;` (остаётся выбранный) |
| 72 | `scan_pages.recognized` — распознанный текст правленой страницы «Сканов» (правленый — в `text`) | столбец не читает: правленый текст для него — распознанный (выгрузки, «В чат», перевод, поиск — с правкой), без отметки «правлено» и «Вернуть распознанное»; «Распознать заново» другим движком переносит правку страниц слоя. Документы диапазоном PDF (номера страниц не с 1) прежний код распознаёт, показывает и выгружает с теми же номерами; листалка — «страница 2 из 3» по месту, состояние идущего — «стр. 51 из 3»; PDF длиннее 50 страниц — снова отказ «разделите документ» | Не нужен; вернуть распознанное — сначала текст документа для поиска (`scans.text`, триггер `search_scans_au`), потом страницы: `UPDATE scans SET text = (SELECT group_concat(t, char(10,10)) FROM (SELECT coalesce(recognized, text) AS t FROM scan_pages p WHERE p.scan_id = scans.id AND p.state IN ('done','layer') AND t <> '' ORDER BY p.n)) WHERE id IN (SELECT scan_id FROM scan_pages WHERE recognized IS NOT NULL); UPDATE scan_pages SET text = recognized, recognized = NULL WHERE recognized IS NOT NULL;` |
| 73 | `translate_glossary`, `users.translate_tone` — глоссарий «термин → перевод» и тон перевода учётки | не читает: переводит без глоссария и тона; удаление учётки уносит пары каскадом | Не нужен; глоссарий и тон действуют снова после возврата вперёд |
| 74 | `scan_pages.boxes` — рамки строк страницы скана, `scans.layer_prior` — документ в очереди за рамками для «PDF со слоем» и его прежний итог | столбцы не читает: «PDF со слоем» нет (выгрузка pdf — 422); документ, ждущий рамок, прежний код распознаёт как документ без ждущих страниц — «готово» с уведомлением, и документ с ошибкой или отменой тоже; идущий поиск рамок остановка службы уже вернула к прежнему итогу | Не нужен; рамки сохраняются до возврата вперёд, устаревший `layer_prior` кончившегося документа новый код не читает (`ocr.retry` и `ocr.find_lines` его переписывают) |
| 75 | `notify_prefs` — «О чём уведомлять» устройства: снятые виды, тихие часы, интервал напоминания о ждущей карточке | таблицу не читает: push и ntfy — все виды и в любое время, напоминаний о карточках нет; строки уходят каскадом с сессией | Не нужен; выбор устройств действует снова после возврата вперёд |
| 76 | `project_members.role` (`editor`/`viewer`) и `expires` — наблюдатель совместного проекта и срок выдачи | столбцы не читает: **наблюдатель снова соавтор** (ход, правка файлов, ответы на карточки), срочная выдача бессрочна, истёкшая, ещё не снятая, снова действует; новые выдачи — соавторы без срока | Перед откатом снять такие выдачи: `DELETE FROM code_member_tabs WHERE (agent_id, project, user_id) IN (SELECT agent_id, project, user_id FROM project_members WHERE role = 'viewer' OR expires IS NOT NULL); DELETE FROM project_members WHERE role = 'viewer' OR expires IS NOT NULL;` — владелец выдаст их заново после возврата вперёд |
| 77 | `mirror_copies.auto` — «синхронизировать после хода» копии зеркала | столбец не читает: после ходов не синхронизирует, флажок сохраняется до возврата вперёд | Не нужен |

Новый код строки незнакомых разделов не видит (`connections._from_row`, `search.KNOWN`) — откат со следующего нового
раздела не даст 422 правки подключения и 500 поиска.

## Тесты

Контракт — `local-config/cron/project-registry.json::studio.tests`: наборы `main` (корень, `.venv`) и `agent`
(`agent/`, своя `.venv`, [agent/CLAUDE.md](agent/CLAUDE.md#тесты)); быстрый и полный уровни запускают оба.

    .venv/Scripts/python -m pytest -q                       # быстрый набор: 10 процессов xdist, бюджет 60 с
    .venv/Scripts/python -m pytest -q -n0 <файл>            # один тест/файл без параллели
    .venv/Scripts/python -m pytest -q -m integration        # браузер (Edge/Chrome + node 22, Firefox, WebKit), PowerShell
    .venv/Scripts/python -m pytest -q -m manual             # живая приёмка после выкладки (команда — testing.md)

- Точечно: s06 — `tests/test_s06.py` и соседи; Cleaner — `tests/test_cleaner.py` и `tests/test_cleaner_route*.py`,
  `tests/test_cleaner_forced.py`, сквозной — `-m integration tests/test_cleaner_contract.py tests/test_ui_cleaner.py
  tests/test_ui_cleaner_route.py` (нужен `cleaner/.venv` или `CLEANER_TEST_REPO`). Полные
  команды — [testing](docs/architecture/testing.md).
- Документация: `tests/test_docs.py` (быстрый) сверяет `` `модуль.символ` `` в CLAUDE.md и `docs/architecture/*.md`
  с кодом (AST, без импорта), пары «`модуль.КОНСТАНТА` (значение)» и относительные ссылки. Значения констант в текст
  пишутся только с модулем — тест их сверяет; без модуля — только имя.
- Без реального времени: часы — `clock.NOW`, паузы — `clock.SLEEP`; шлюз — `httpx.MockTransport` (`tests/fakes.py`);
  мост — `tests/fake_bridge.py` (`FakeMachine`, `InProcessBridge`, `SocketBridge`). Тест дольше 1 с — `integration`;
  `manual` — группой `serial`, одним процессом; файл `integration` — группой `lane:<файл>`, целиком в одном
  процессе, такие файлы — не больше чем в трёх процессах сразу (`LANES` в `tests/conftest.py`). Обычные тесты
  назначаются процессу целыми файлами; явные группы имеют приоритет. Срок теста `timeout = 30` (метод `thread`),
  `faulthandler_timeout = 20`; `--durations=0 --durations-min=1` в `addopts` — каждый прогон называет тесты дольше 1 с.
- Браузер — 320 и 1280 px; WebKit — `tests/test_ui_webkit.py` (`playwright install webkit`; с `page.route` —
  `service_workers="block"`).
- `STUDIO_ADMIN_PASSWORD` в vault — пароль восстановления, не рабочий: вход им на проде — 401, и из LAN пятая неудача
  за 15 минут запрёт владельцу вход из дома. Для живой приёмки — спросить владельца.
- Решение владельца 2026-11-03: подписка Claude остановлена — живые пробы Claude только после сообщения о
  восстановлении доступа.

## Ловушки

Полные формулировки — в «Ловушках» файлов docs/architecture.

- Apache: `ProxyPass` — на имя `studio-backend`, не `localhost`; `/api/chats/`, `/api/code/` и `/api/owui/` (600 с, `flushpackets=on`,
  `no-gzip`), `/api/videos` (330 с), `/api/my-files/` (330 с, ZIP папки — `my_files.ZIP_SEC`), `/api/speeches/chats/`
  (930 с, один mp3 чата — `audio.TIMEOUT_SEC`) и `/api/agent/link` (`upgrade=websocket timeout=300`) — раньше общего `ProxyPass /`;
  `LimitRequestBody` не меньше пределов службы; ожидание nginx контейнера — не меньше самого долгого маршрута. Сверяет `tests/test_deploy.py`.
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
  «недоступна»; токены подключений — ввести заново; настройки SMTP из панели — снова переменные окружения,
  ввести заново (`account_mail.settings`, признак `reenter`); зеркала проектов — «настройка недоступна», отвязать и
  привязать заново (`project_mirrors._item`, признак `reenter`), исключения группы — сохранить заново (`exclude_reenter`),
  состояния пар и накопленное «держал» нечитаемы — первые сравнения осторожнее (больше «локальных изменений»); идентификаторы `X-Studio-User` (`access.studio_user`)
  — новые: внешний MCP (например, мост с папками по людям) видит новых пользователей, их папки, задания и файлы потеряны;
  личная память чата — `memory.UNREADABLE` вместо текста, удалить и записать заново; учётки Open WebUI — `relogin`
  (`owui._out`, признак `reenter`), «Войти заново» с email и паролем; приложение-аутентификатор — секрет нечитаем,
  вход и привязка моста идут без него (коды из письма — если включены), «Защита» просит подключить заново
  (`totp.status`, признак `reenter`), прежние резервные коды не действуют; настройки Cleaner администратора —
  ввести заново (`cleaner_route.settings`, признак `reenter`; до этого принудительный режим выключен).
- Подключение через Cleaner: `Connection.routed` ⇒ `proxied` (`connections._from_row`) — инструменты, «Интернет»,
  знания и перевод идут прежними серверными путями, но запрос такого подключения — только через `cleaner_route`
  (`stream`, `request`, `models`, `context`); прямой `upstream.request` с ним — отказ `cleaner` предохранителем
  `upstream._cleaner_only`. Служебный ключ — только `X-Cleaner-Key`: `Authorization` — ключ провайдера. Контекст шлюза
  отмечен отдельной таблицей `cleaner_route_contexts` (прежний код вставляет в `cleaner_contexts` без списка столбцов).
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
