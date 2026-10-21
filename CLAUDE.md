# CLAUDE.md — studio

Студия `https://studio.example.com` — многопользовательское приложение (PWA) с учётками, историей и
разделами чат · фото · видео, плюс пакетные скрипты для Windows и Linux — клиенты API картинок и
видео LLM-шлюза srvllm. Что умеет и как пользоваться — [README.md](README.md); спека —
[docs/superpowers/specs/2026-10-19-studio-pwa-design.md](docs/superpowers/specs/2026-10-19-studio-pwa-design.md).
Репозиторий `git.example.net/xadmin/studio` — **приватный**: NSFW-назначение, хосты LAN.

## История и граница с llm_routers

- Выделен из `llm_routers` 2026-10-19: страница — бывший `srvllm/deploy/services/studio/`
  (служба `srvllm-studio-web` на srvllm, :8151), скрипты — `srvllm/client/`, тесты —
  `srvllm/tests/test_studio*.py`. История правок до выделения — `git log` llm_routers по этим
  путям; спеки там же: `docs/superpowers/specs/2026-10-16-srvllm-studio-design.md`,
  `2026-10-19-images-api-lustify-design.md`, `2026-10-19-srvllm-studio-video-design.md`. Вики —
  `local-config/wiki/projects/llm_routers/architecture-srvllm-studio.md`.
- 2026-10-20 однопользовательская страница `llm.example.com/studio/` (`app/server.py`, вход ключом
  шлюза, история только в памяти) заменена приложением на `studio.example.com`; старая — в `git log`.
  Перенаправление `/studio/` → новый адрес — в vhost `llm.example.com` проекта `llm_routers`.
- Здесь только клиенты. ComfyUI, переводчик `srvllm-lustify-api`, слот `comfy`, маршруты шлюза
  `/slot/images/*` и `/slot/videos`, потребитель шлюза `owner-images` — в `llm_routers`. Смена
  контракта API (поля, модели, размеры, пределы) — сначала там, потом здесь: имена моделей и
  размеры сверяют тесты скриптов.
- `app/image_upload.py` — копия модуля переводчика на 2026-10-19; правка пределов здесь не должна
  выходить за пределы переводчика.

## Устройство

- `app/main.py` — приложение FastAPI (`main:app`) от корня `/`; разделы — `auth` (вход, сессии,
  CSRF, пауза после неудач, признак «заперто» блокировки на телефоне), `profile`, `screenlock`
  (блокировка на телефоне: отметки «на экране», «заперто», отпирание паролем и passkey библиотекой
  `webauthn`, ключи учётки — таблица `passkeys`), `connections` (подключения — адрес с ключом), `sections`
  (разделы: подключения раздела, отмеченные модели, умолчание; `resolve` — пара запроса),
  `upstream` (список разрешённых адресов и имён, запросы к ним), речь — `speech` (WhisperX, GigaAM,
  Qwen3-TTS шлюза и внешний сервис), `audio` (ffmpeg debian: длительность, сжатие, WAV образца,
  склейка), `dictation` (голосовой ввод), `transcripts` (расшифровка в фоне), `exports` (txt, docx,
  srt), `voices` (образцы голосов), `speeches` (озвучка в фоне), `jobs` («В работе»: свои идущие и
  готовые за час задания всех разделов — только база и память модулей, к шлюзу не ходит), `search`
  (поиск по своей истории: `POST /api/search`, `GET /api/search/models`; страница — `search.js`), `admin`, `chat` (поток SSE;
  дата по UTC+3 — `chat.with_date` первым системным сообщением каждого запроса, и прямого подключения), `web`
  («Интернет» чата, спека 2026-10-21-studio-chat-internet, шаг схемы 14: поиск и страницы Exa — `EXA_URL`
  через `state.HTTP`, не `upstream`; субтитры YouTube — общий `yt-dlp` списком аргументов только по
  `https://www.youtube.com/watch?v=<id>` из `web.youtube_id`, одна дорожка `pick_track`, файлы `<uuid>.*`
  в `tmp_dir`; запуск — `web.RUN`, тесты подменяют; кнопка — `chats.web` (`PATCH` диалога), инструменты
  хода — `web.tools_for` (только `proxied` и только с ключом Exa — без ключа ни одного, YouTube тоже;
  `PATCH {web: true}` без ключа — 409 `no_exa_key`, `DELETE /api/me/exa-key` тем же запросом ставит
  `chats.web = 0` всем диалогам учётки; страница — по `proxied` диалога и `me.exa_key`: без ключа кнопка
  выключена с подсказкой, профиль перечитан — событие `studio-me`), круги — `chat.StreamTask` до
  `TOOL_ROUNDS`, вызовов за круг — до `CALLS_PER_ROUND` (лишним — шаг-ошибка со своим `role: tool`), срок
  ответа — перед каждым вызовом и кругом; `chats.web` читается в транзакции хода и снова перед каждым
  вызовом (выключили — «Интернет выключен», дальше без `tools`); событие SSE `tool`; 400 первого запроса с
  `tools`, где тело говорит о `tool`, — `NO_TOOLS`, иное — `gateway_error`; шаги — JSON
  `messages.tools` (страница — `web.public`, без текста для модели; выгрузка — целиком), в следующих ходах —
  блоками `<web>` `history_block` в сообщении пользователя, без ролей `tool`; ключ — `users.exa_key` шифром,
  `PUT/DELETE /api/me/exa-key` в `profile`, `verify-backup` считает `exa_keys`; ход, что даст модели Exa
  при расшифровке в истории новее `chats.web_consent`, — 409 `external_web` без `web_ok`; страница —
  `chat.js`, ключ — «Прочее» → «Интернет» в `settings.js`), `documents`, `images`, `videos`, `media` (файлы на
  шаре, `Range`; архив выделенного — `POST /api/media/archives` списком в теле → токен в памяти на
  `ARCHIVE_TTL_SEC`, `GET /api/media/archives/<токен>` — ZIP без сжатия на лету; CRC и размер — в
локальном заголовке первым проходом по файлу, без дескриптора данных: запись без сжатия с ним не
открывают Archive Utility macOS и потоковые читатели; учёт записей — `media.ZipWriter`, общий с
выгрузкой), `takeout` (выгрузка своей истории: `POST /api/me/export` разделами → токен на
`EXPORT_TTL_SEC`, не больше `EXPORT_KEEP`; `GET /api/me/export/<токен>` — ZIP асинхронным
генератором, одна идущая на пользователя (`RUNNING`, вторая — 409 `busy`): база — порциями на цикле
событий (диалог, страница раздела по `PAGE`), JSON, CRC и sha256 — `asyncio.to_thread` (`_pack`),
заголовок и данные записи — разными кусками; файл открывается один раз (`media.open_summed`, общий с
архивом выделенного: суммы и данные с одного дескриптора); README, диалог — своим файлом, разделы —
`images/0001.json` …, `files/<media id><расш.>`, `manifest.json` с sha256 последним; без токенов,
хешей, сессий, passkey), `history`; формы фото и роликов (тело с пределом, фото файлом или из истории) —
  `forms.py`; цепочка промптов картинок и роликов (снимок шагов в `images.chain` / `videos.chain`,
  у старых строк — по ссылкам) — `chains.py`, в ответе — только у одной записи (`GET …/<id>`), лента
  несёт число шагов `steps`; база — `db.py`, окружение — `config.py` (`REQUIRED`),
  часы — `clock.py`, команды хоста — `manage.py` (`create-admin`, `backup`, `verify-backup`; проверку
  копии зовёт и панель — `POST/GET /api/admin/backup-check`, поток-демон `admin.CHECK`, одна за раз). Фронтенд без сборки —
  `app/static/` (модули ES, сторонние библиотеки копией в `static/vendor/`, без CDN); шторка,
  экран блокировки, отметки и помощники passkey — `lock.js` (ставит `api.js` `hooks.locked`: 423
  ждёт отпирания и повторяется); значок и панель «В работе», опрос `GET /api/jobs` и уведомления
  (только `showNotification` service worker, флаг устройства `studio-notify`, без push) — `jobs.js`,
  нажатие на уведомление — `notificationclick` в `sw.js`; разделы переходят к записи по `?id=` и
  зовут `jobs.kick()` после запуска; вид ленты
  фото и видео (превью, плитка, таблица) — `views.js`, выбор, как и тема, — в учётке (`users.views`,
  `PATCH /api/me`); вернувшись на экран, оболочка перечитывает `/api/me`. Выделение нескольких
  записей ленты фото и видео (отметки, долгое нажатие, «Скачать» и «Удалить» пачкой) —
  `select.js` (действия раздела — `extraActions`: «Сравнить» у фото), групповое сохранение —
  `save.js` `saveMany`. Фото (спека 2026-10-21-studio-photo-presets-compare-series): пресеты —
  `presets.py` (таблица `presets`, роутер `/api/images/presets` подключён раньше `images`; не
  история — стирание их не трогает, учётка уносит каскадом) и `presets.js` (чип «★»); серия —
  поле `series` у `POST /api/images` (`images.run_series`: строки `queued` по одной в слоте
  пользователя, отмена любой снимает весь остаток; `stop()` и `recover()` переводят `queued` в
  `error`) и `series.js` (чип «×1» и панель в «Изменить»); варианты —
  `GET /api/images/<id>/variants` (индекс `images_user_source`); сравнение — `compare.js`
  (`#compare`); параметры по модели — `chain.js` (`strengthOf` только Lustify, `seedOf` — не у
  внешнего сервиса; в строку сила пишется только у Lustify). Иконки (PNG и
  `favicon.ico`) — `tools/make_icons.py` по геометрии `icons/icon.svg`: правка рисунка — в обоих,
  PNG коммитятся.
- Схема базы — `db.VERSION`; новая база получает `SCHEMA` сразу, база прода — шаги `db.STEPS`
  (только добавлять: откат `install.sh` на прежний код должен работать с новой базой).
- Подключения: к адресам из списка разрешённых (панель администратора, по умолчанию
  `10.77.1.15:4000`) ходит сервер, токен в браузер не уходит; к остальным — браузер (только чат).
  По адресам вне списка сервер не ходит (с учётом перенаправлений и разрешения имён). Запись
  списка — IP:порт или имя:порт; имя — только https, при каждом запросе все его адреса должны быть
  публичными (`ipaddress.is_global`), соединение — с проверенным адресом (SNI и `Host` — имя,
  `upstream.RESOLVE`). Правило путей `PROXIED_PATHS` — только для IP-записей. Задания держат
  снимок `Connection`: `connections.token` отдаёт ключ, только если владелец, вид и адрес строки
  совпали со снимком, иначе `upstream.ConnectionChanged` (слова — `upstream.refused_words`) — новый
  ключ прежнему адресу не уходит.
- Разделы (`chat`, `photo`, `video`, `transcribe`, `speech`, `dictation`) — таблицы `section_links`
  (подключение в разделе и его отмеченные модели) и `section_defaults`; встроенный перечень srvllm —
  `sections.CATALOG`. Старые `connections.sections` и `users.default_*` остаются для отката и
  держатся согласованными (`sections._sync_legacy`) — новая логика их не читает. Ход начатого
  диалога пару не перепроверяет (`chat.create_turn` — `connections.get`, план section-connections,
  Task 4): галочки — правило выбора для нового, не запрет старого (README «Подключения и разделы»).
- Проверка подключения — `checks.py` (спека 2026-10-21-studio-connection-check-design): паспорт —
  `connections.passport` (JSON: строки `routes` по запросам, модели с возможностями и источником —
  `capabilities.py`, пробы), обнуляется сменой адреса, вида или токена; зеркала панели его убирают.
  Пишется только той строке, что видела проверка (`connections.snapshot`: владелец, вид, адрес,
  ключ шифром; иначе 409 `changed`): проверка ждёт ответа секундами, за это время токен меняют, а id
  удалённого подключения SQLite отдаёт новому — чужому.
  Пробы через сервер (`POST …/check`) — только `GET` через `upstream`: srvllm — `/v1/models` и
  `/slot/models`, openai — `models_url` и он же без ключа (`upstream.request(keyless=True)`),
  OpenRouter — `…/key`; ни генерации, ни release, ни `GET /slot/videos` (сторожит `test_checks.py`).
  Прямое проверяет браузер (`passport.js`) → `PUT …/passport` (`source: browser`, пределы формы,
  возможности выводит сервер). «Пробный ответ» (`POST …/probe`) — единственная тратящая проба, у
  srvllm его нет. Адрес, убранный из списка, — `connections.dropped` (не прямое): «Проверить» и
  «Обновить модели» говорят это словами; `UpstreamRefused.reason` различает `unlisted`, `internal`,
  `changed`.
- Правка расшифровки — JSON `transcripts.edits` (`texts` по номеру реплики, `names` по метке
  «Собеседник N») поверх файла итога, который не переписывается; показываемые реплики для GET,
  выгрузок и вложения собирает одна `transcripts.view`. Файл итога новых расшифровок несёт ещё `raw` —
  сегменты до склейки с номером реплики `turn` (`speech.turns`), по ним SRT (`exports.srt`, пределы 7 с
  и 84 знака); прежний код читает только `segments`. Вложение из расшифровки
  (`POST /api/transcripts/<id>/attachments`) — обычный документ чата со своим .txt и
  `attachments.source_transcript_id` (без FK); ход и повтор, чья история (что уйдёт модели) несёт такое
  вложение, к модели, не локальной по `sections.is_local` (`local-*` подключения srvllm через сервер), —
  409 `external` без `external_ok`. След подтверждения — без новой схемы (`chat._needs_consent`): ответ
  ассистента той же `messages.model` и `via` после последнего сообщения с расшифровкой (подключение
  сообщение не хранит: та же модель другого подключения того же пути — подтверждена). Страница
  берёт признаки `local` и `external` у диалога (GET, POST, PATCH одного диалога).
- Правленый текст реплики — одной строкой (управляющие — пробелом, пробелы схлопнуты), до
  `len(исходной) + EDIT_TEXT_MAX`; SRT и DOCX ещё и сами убирают управляющие (`exports.CONTROL`).
- Поиск (спека 2026-10-21-studio-history-search-design, шаг схемы 13): документы — таблица `search_docs`
  (`section`, `item` — id записи или диалога, `part` — `''`, у чата `title`, id сообщения или вложения,
  `user_id`, `model`, `created`, `body`), над ней FTS5 `search` с внешним содержимым
  (`unicode61 remove_diacritics 2`, `prefix='1 2 3'`: без индексов начал `snippet()` по «к»* сливал
  списки позиций всех слов на «к» на каждую строку выдачи — секунды в цикле событий). Индекс держат **только триггеры** таблиц истории (`search_*` в
  `db.SEARCH_13`: картинки, ролики — и смена промпта, названия и сообщения диалогов, отправленные
  документы, расшифровки — имя и `transcripts.text`, озвучки, удаление учётки) — он верен и при прежнем
  коде после отката; код в `search_docs` не пишет. Ответ чата — после `status != 'streaming'` (ход
  пишет текст каждые `SAVE_SEC`); каскады FK дёргают триггеры дочерних таблиц. «ё»→«е» — только в
  тексте, что уходит в FTS (триггеры `search_docs`), и в словах запроса; содержимое — исходное, и
  `snippet()` выделяет верно (число токенов замена не меняет). **Ремонт индекса — только
  `search.reindex(conn)`** (`'delete-all'`, затем вставка из `search_docs` с той же заменой `db.YO`):
  штатный `INSERT INTO search(search) VALUES('rebuild')` берёт тело как есть и молча теряет «ё» = «е»,
  а `('integrity-check', 1)` сверяет с содержимым и пишет «malformed» (без `rank` — проверка самого
  индекса, ей можно). На хосте — служба остановлена, от `studio` из `/opt/services/studio/app`:
  `.venv/bin/python -c "import db, search; search.reindex(db.connect('/var/lib/studio/studio.db'))"`.
  Документ чата и текст расшифровки — в индексе первые `db.INDEX_CHARS` (200 000) знаков: триггеры
  токенизируют в транзакции хода и удаления, документ на 2 млн знаков — 0,65 с цикла событий.
  Удаление и фильтры идут по индексам
  `search_docs` (`search_docs_key`, `_user`, `_part`), а не просмотром FTS. `transcripts.text` —
  реплики с правками одной строкой: пишут «готово», правка и сброс (сброс — файл итога до записи, затем
  `edits` и `text` одним UPDATE); фон `transcripts.backfill_text` при старте (под `state.WATCHER`; шара
  недоступна — следующий старт) — пустым и правленым (прежний код при откате правит `edits`, а `text`
  не трогает). Строки ленты и опроса — `transcripts.COLUMNS`, без `text`.
  Запрос — телом `POST`: строку пишет журнал Apache; журнал службы — `search: N`. Слова — `[^\W_]+`
  (синтаксис FTS5 не проходит), до 8, `"слово"*` через AND; диалог в выдаче — один раз
  (строки по порядку без LIMIT, у диалога — первое совпадение; диалоги прежних страниц — одним запросом
  выше курсора; окно `row_number()` по всем совпадениям впятеро дольше); порядок — `created`, `item`,
  `search_docs.id` по убыванию (вопрос и ответ хода — одно `created`, ответ индексируется позже); курсор —
  `(created, item, part)`. Дни — годы `search.YEARS` (2000–2100; дальше `timestamp()` падает — 500).
  Строка названия диалога несёт модель диалога (триггер — и на смену `model`). Состояние экрана — только в
  памяти `search.js` (не в адресе, не в localStorage); «Показать ещё» дописывает, только если запрос тот
  же, что у выдачи. Выгрузка (`takeout`) берёт столбцы по списку —
  `search_docs` и `transcripts.text` в неё не идут.
- История: база SQLite — `/var/lib/studio/studio.db` (локальный диск); файлы — storage
  `\\storage\dd$\SharedData\studio`, на debian `/mnt/storage/SharedData/studio`. Токены подключений в
  базе зашифрованы `STUDIO_DATA_KEY` (Fernet), пароли — scrypt.
- Путь файла без строки `media` не теряется — очередь удаления `purge` (без FK на `users`, переживает
  удаление учётки): туда идут файлы, которые шара не дала удалить, и файлы, которые пишутся сейчас
  (`save_async`/`save_stream` ставят путь до записи, строка `media` снимает его той же транзакцией, —
  отмена и остановка службы посреди записи не оставят файл без записи; строка не встала — файлы
  убирает тот же проход по одному пути, в потоке). Запись ждут не дольше `media.SAVE_TIMEOUT_SEC`
  (45 с; у `save_stream` — шаг в потоке): зависшая шара — 503 «хранилище не отвечает», а не 502
  Apache через 150 с; поток дописывает сам, путь — в `SAVING`, пока он не кончит (сверяет
  `test_deploy`). Проходит её `media.purge()`:
  при старте и раз в `PURGE_SEC` (`start_purger`), в каждом `delete_async` и в начале стирания
  истории; пишущиеся (`media.SAVING`) пропускает. `DELETE /api/history` и `/api/admin/users/<id>`
  отвечают `{"pending_files": N}` — сколько файлов ещё ждут, страница это говорит.
- Хранилище: `GET /api/me/storage` (`profile`, «Прочее» → «Хранилище») — свои файлы по видам
  `media.KINDS` из строк `media` (`media.usage`, шару не обходит) и своя очередь (`media.waiting`:
  число и самый старый `queued`); `GET /api/admin/storage` (`admin`) — проба шары
  (`shutil.disk_usage`) и возраст новейшей копии `backup/<manage.BACKUP_GLOB>` в своём потоке-демоне
  со сроком `admin.PROBE_SEC`, одна проба за раз (`admin.PROBE`: зависшая CIFS держит один поток, а не
  пул `to_thread`), очередь — числом. Чужой истории (и объёмов по пользователям) панель не получает.

## Правила

- **Приватность — контракт, а не пожелание. История — в базе и на шаре, в журнал — ничего.**
  Промпты, ключи, пароли, тексты сообщений и документов, картинки, ролики и имена файлов в журнал
  не пишутся (uvicorn с `--no-access-log`, журнал — только события с id). Новый код это не ломает
  (сторожат тесты журналов разделов).
- **Анонимны только** `/health`, `/login` (страница и вход), `/logout`, `/robots.txt` и оболочка
  PWA: `/static/*`, `/manifest.webmanifest`, `/sw.js`, `/offline.html`, значки из корня
  (`/apple-touch-icon.png`, `/apple-touch-icon-precomposed.png`, `/favicon.ico`); `/` без сессии уводит на
  вход, всё `/api/*` — через `auth.guard` (сессия + CSRF у изменяющих запросов). Страницы отвечают
  и на HEAD.
- Защита входа от перебора — паузы (на пару «логин + адрес» и на адрес); растущая задержка ответа
  на неверный пароль параллельные попытки не тормозит — на неё не полагаться.
- **Пользователь видит только своё**: каждый запрос к истории, файлам и подключениям — с
  `user_id` сессии; панель администратора чужую историю не показывает.
- **Исключение из правила списка разрешённых адресов** («Устройство» → подключения) — только адреса,
  зашитые в `web.py`: `api.exa.ai` и YouTube через yt-dlp. Их не выбирают ни пользователь, ни модель:
  адрес страницы уходит в Exa данными, сервер по нему не ходит, и `read_page` берёт только адреса из
  сообщений и документов диалога или из `results[].url` поиска (иначе — шаг-ошибка, в Exa ничего: страница
  или выдача не уведут переписку на чужой сайт краулером Exa); yt-dlp получает только канонический
  адрес из id (у него общий извлекатель — чужой адрес, в том числе внутренний, сервер прочитал бы), с
  урезанным окружением (без `STUDIO_DATA_KEY`) и не больше двух сразу.
  Наружу — только при `chats.web = 1`: иначе в запросе к модели нет `tools`, Exa и yt-dlp не зовутся; в
  Exa — только `query` (до 500 знаков) / `url` (до 2048) из вызова модели, не переписка (сторожат
  `test_chat_web.py`, `test_web.py`).
- **Ключ Exa — свой у учётки**, `users.exa_key` шифром `security.encrypt` (как токены; смена
  `STUDIO_DATA_KEY` — вводят заново, `web.NO_KEY`). В браузер, ответы API, выгрузку и журнал не уходит:
  `GET /api/me` — только `exa_key: true|false`; при сохранении запросом к Exa не проверяется (тратил бы
  баланс). «Удалить всю историю» его не трогает — настройка, как пресеты.
- **Путь к службе один — Apache debian.** Служба слушает только unix-сокет `/run/studio/web.sock`
  (каталог `0750 studio:www-data`) и потому верит последнему элементу `X-Forwarded-For`. Порт TCP
  не открывать: счёт попыток входа подделывался бы заголовком.
- Секреты — vault `local-config/secrets/vault.env`; в репозиторий — никогда. Значения едут на хост только
  потоком stdin (`grep … | ssh`), не в argv и не в вывод. Скрипты в репозитории без ключа
  (`$EmbeddedKey = ''`, `EMBEDDED_KEY = ""`, сторожат тесты), копии с ключом — `*.local.ps1` и
  `*.local.py`, в `.gitignore`.
- `client/*.ps1` — UTF-8 **с BOM** и CRLF (PowerShell 5.1 без BOM читает CP1251).
- `client/*.py` — Linux-версии тех же скриптов: UTF-8 без BOM, LF (иначе shebang не сработает),
  Python 3.8+ без сторонних пакетов. Правка поведения — в обеих версиях сразу: общие сценарии
  `test_studio_{batch,video}_client.py` гоняют обе (`-PromptsFile` у PowerShell = `--prompts-file`
  у Python, это и сверяют).
- `deploy/*` — LF (`.gitattributes`): `\r` systemd принял бы за часть пути, bash — за часть команды.

## Хост (debian, 10.77.1.10)

- Доступ: `ssh -i ~/.ssh/studio_deploy_key -p 22222 deploy@10.77.1.10`, passwordless sudo.
- Код и `.venv` (Python 3.13) — `/opt/services/studio`, владелец root; служба `studio-web.service`
  ([deploy/studio-web.service](deploy/studio-web.service)) от пользователя `studio` (группа
  `www-data`), `ProtectSystem=strict`, запись — только `StateDirectory=studio` (`/var/lib/studio`,
  0700) и `ReadWritePaths=-/mnt/storage/SharedData/studio`; `MemoryMax=2G`, `LimitCORE=0`.
- Шара: `/mnt/storage` = `//10.77.1.5/dd$` (cifs, `noperm`, `/etc/fstab`); каталог
  `SharedData/studio`. Не смонтирована — каталога нет, и запись падает ошибкой, а не ложится на диск
  debian: `/mnt` под `ProtectSystem=strict` только для чтения (`-` у `ReadWritePaths` лишь даёт
  службе стартовать без каталога). `install.sh` до подмены кода проверяет, что путь — на cifs, и
  пробует настоящую запись от `studio` (`test -w` при `noperm` верен всегда). Монтирование `soft`,
  `echo_interval=60` (проверено 2026-10-21): у зависшей шары чтение само падает ошибкой минуты через
  две — отдача файла, архив выделенного и выгрузка истории рвутся, поток пула не висит вечно; срока
  чтения в коде нет и на этом держится — `hard` в `/etc/fstab` его отнял бы.
- Звук — `ffmpeg` debian (ставит `install.sh`, если нет): временные файлы — `/var/lib/studio/tmp`
  (`config.tmp_dir`), чистится при старте службы. Запись для расшифровки — до 200 МиБ телом запроса
  потоком на шару: `LimitRequestBody` 210 МиБ только на `^/api/transcripts$` (`LocationMatch` vhost),
  общий предел — прежний.
- YouTube — общий yt-dlp debian `/usr/local/bin/yt-dlp` (системный `python3`), служба зовёт `yt-dlp` по
  `PATH` (`web.YTDLP`), своей копии в `.venv` нет: она устаревала бы между выкладками, а YouTube ломает
  старые версии. Обновляет root каждую ночь — `/etc/crontab`: `0 1 * * * root /usr/bin/pip3 install -U
  yt-dlp --break-system-packages` (крон — не часть репозитория); `install.sh` без yt-dlp только
  предупреждает. Пишет только в `config.tmp_dir`, кэш выключен (`--no-cache-dir`).
- **api.exa.ai с debian заблокирован** (2026-10-21): Cloudflare Exa отвечает выходу debian (Финляндия)
  403 HTML-страницей — поиск и страницы «Exa недоступен с сервера студии». Решение — маршрут в проекте
  `network` (его `FINDINGS.md`, 2026-10-21), код студии под это не меняется; живая проверка поиска на
  проде — после маршрута.
- Секрет — `/etc/studio/studio.env` (0600 root, одна строка `STUDIO_DATA_KEY=`, vault
  `STUDIO_DATA_KEY`): его читает systemd, а не служба. Смена ключа делает прежние токены
  подключений нерасшифровываемыми — их вводят заново.
- Копия базы — `studio-backup.timer` (04:30, `Persistent=true`) → `studio-backup.service` →
  `manage.py backup`: `sqlite3` backup во временный файл рядом с базой, затем на шару
  `backup/studio-YYYYMMDD.db` (временное имя + `os.replace`), 7 последних успешных: старые убирает
  только успешная новая копия (`BACKUP_KEEP` — число копий, а не дней); юнит ограничен
  `TimeoutStartSec=30min`. В копиях удалённое («Удалить всю историю», удалённые учётки) живёт, пока
  его не вытеснят 7 более новых успешных копий — без календарного срока: пропуски копирования его
  продлевают; файлы, которые шара не дала удалить, — пока их не доудалит очередь `purge`.
  Восстановление — сначала проверить копию: `manage.py verify-backup [ПУТЬ]` (без пути — новейшая;
  панель — «Хранилище» → «Проверить копию») поднимает её файлом `verify-<hex>.db` в
  `/var/lib/studio/tmp` (файл, не каталог: `audio.clean_tmp` при старте удаляет файлы, каталог
  уронил бы старт; это диск рабочей базы — свободно меньше копии плюс `VERIFY_RESERVE` или 10 % — «мало
  места», не копирует), гоняет `integrity_check`, шаги `db.STEPS`, расшифровку токенов текущим ключом и
  `stat` файлов шары (`VERIFY_FILES_SEC` от начала обхода файлов), рабочую базу не открывает; JSON в stdout, код 0 —
  поднимается, 1 — нет, 2 — копии нет. На хосте — от `studio` с окружением юнита, как
  `create-admin` в `install.sh`: `sudo systemd-run --quiet --collect --pipe --wait
  --service-type=exec -p User=studio -p Group=www-data -p EnvironmentFile=/etc/studio/studio.env
  -p "Environment=$(systemctl show -p Environment --value studio-web)"
  /opt/services/studio/.venv/bin/python /opt/services/studio/app/manage.py verify-backup`. Затем —
  остановить `studio-web`, положить копию на место `/var/lib/studio/studio.db` (владелец
  `studio:www-data`, удалить `-wal`/`-shm`), запустить. Файлов, удалённых после даты копии, на шаре
  уже нет: их строки `media` отвечают 404. Запасной `STUDIO_DATA_KEY` — vault
  `local-config/secrets/vault.env` `STUDIO_DATA_KEY` (тот же, что в `/etc/studio/studio.env`): без него
  токены подключений из копии не расшифровать — их вводят заново.
- Apache: vhost [deploy/apache-vhost.conf](deploy/apache-vhost.conf) (`*:8443` за прозрачным
  `sslh`, wildcard `/etc/letsencrypt/live/example.net/`) подключён в конце `/opt/services/apache/apache2.conf`
  строкой `IncludeOptional /opt/services/studio/deploy/apache-vhost.conf`. Правка vhost — выкладка:
  `install.sh` после подмены кода гоняет `apache2ctl -t` (отказ — прежний код на место, служба не
  перезапускается) и после ответа `/health` всегда делает `reload apache2`.
- Журнал: `sudo journalctl -u studio-web`, `-u studio-backup` (промптов и имён файлов там нет).

## Выкладка

    bash deploy/deploy.sh                    # HEAD → debian, install.sh, проверка через Apache
    bash deploy/deploy.sh --new-secret       # заново положить STUDIO_DATA_KEY из vault
    bash deploy/deploy.sh --create-admin     # первый администратор и его подключение «srvllm» из vault

Выкладывается закоммиченный HEAD (`git archive` частей `app deploy requirements.txt`); грязное
дерево — отказ. Ключ данных при первой установке (в `studio.env` нет `STUDIO_DATA_KEY=`) едет сам.
`--create-admin` берёт из vault `STUDIO_ADMIN_LOGIN`, `STUDIO_ADMIN_PASSWORD`,
`GATEWAY_KEY_OWNER_IMAGES` и через `install.sh --admin-stdin` отдаёт в `manage.py create-admin`
(`systemd-run --pipe` от `studio` с окружением юнита); повтор безопасен, новый пароль гасит сессии.
`install.sh` до подмены кода собирает новую среду Python рядом (`.incoming/.venv`; только если
сменились `requirements.txt` или `python3` — штамп `.venv/.studio-requirements`), переносит её на
место вместе с кодом, держит прежние код и среду в `.previous` до ответа `/health` от www-data
через сокет, ставит юниты копии базы и включает таймер. Перенесённая среда — с чужим shebang в
`bin/`: всё запускается `.venv/bin/python` (`-m uvicorn`, `-m pip`, `manage.py`), не `bin/<скрипт>`. Перезапуск ждёт
открытые потоки чата не дольше 10 с (`--timeout-graceful-shutdown`, `TimeoutStopSec=30`);
недописанное помечают `chat.recover()` / `images.recover()` при старте. Смоук —
`https://studio.example.com/health`; живая приёмка — `tests/test_studio_upload_live.py` (ниже).

## Тесты

    .venv/Scripts/python -m pytest -q                                   # быстрый набор (~22 с, 4 процесса)
    .venv/Scripts/python -m pytest -q -m integration                    # браузер (Edge/Chrome + node 22), PowerShell
    v() { grep "^$1=" C:/work/projects/local-config/secrets/vault.env | cut -d= -f2-; }    # живая приёмка: значения не печатать
    STUDIO_ADMIN_LOGIN=$(v STUDIO_ADMIN_LOGIN) STUDIO_ADMIN_PASSWORD=$(v STUDIO_ADMIN_PASSWORD) \
      GATEWAY_KEY_OWNER_IMAGES=$(v GATEWAY_KEY_OWNER_IMAGES) .venv/Scripts/python -m pytest -q -m manual

Живая приёмка (`tests/test_studio_upload_live.py`, генерирует на карте, до ~1,5 ч) — после
выкладки и `--create-admin`: входит администратором в `STUDIO_LIVE_URL` (по умолчанию
`https://studio.example.com`), делает Lustify, правку кистью (Lustify, вне маски — пиксели исходника),
описание gemma-abl, ролик 1 с и ответ `local-gemma-abl` по PDF; удаляет только своё и освобождает
карту. Без `STUDIO_ADMIN_*` эта часть пропускается; с `GATEWAY_KEY_OWNER_IMAGES` — ещё шлюз
напрямую (`STUDIO_LIVE_GATEWAY`) и `client/studio-batch.local.ps1`.

**`STUDIO_ADMIN_PASSWORD` в vault — пароль восстановления, не рабочий:** владелец входит своим
(решение 2026-10-20, `local-config/project-knowledge-base.yaml` → `api_keys.studio_admin`), поэтому вход
на проде паролем из vault отвечает 401 — это не сбой выкладки. Не повторять: из LAN неудачи идут
парой «example-user + 10.77.1.1», и пятая за 15 минут запрёт владельцу вход из дома. Для живой приёмки
— спросить владельца: временно вернуть пароль восстановления (`--create-admin` гасит его сессии)
или завести отдельную учётку администратора для приёмки.

Среда: `"/c/Program Files/Python314/python" -m venv .venv`, затем
`.venv/Scripts/python -m pip install -r requirements-dev.txt`. Приложение в быстрых тестах —
фикстура `studio` (`tests/conftest.py`): шлюз подменён `httpx.MockTransport` (`tests/fakes.py`),
часы — `clock.NOW`, паузы — `clock.SLEEP`. Браузерные сценарии — `tests/test_ui_browser.py`
(живая служба `tests/live_app.py`, драйвер `tests/browser.py` + `cdp_driver.mjs`). Договорённость
юнитов, vhost и скриптов выкладки сверяет `tests/test_deploy.py`. Набор идёт четырьмя процессами
(`-n 4 --dist loadgroup` в `pytest.ini`, pytest-xdist): состояние заданий — модульное, процессы
его не делят; `integration` и `manual` — группой `serial` (хук в `tests/conftest.py`), одним
процессом по очереди. Один тест без параллели — `-n0`.

## Ловушки

- `ProxyPass` — на имя `studio-backend`, не `localhost`: mod_proxy подбирает воркер по префиксу
  URL, и чужой корневой `|http://localhost/` увёл бы запросы в свой сокет.
- `ProxyPass /api/chats/` (600 с, `flushpackets=on`, `no-gzip`) — раньше общего `ProxyPass /`:
  берётся первое совпавшее правило, а поток чата не должен копиться в буфере и рваться по 120 с.
  Так же `ProxyPass /api/videos` (330 с): создание ролика отвечает после шлюза — исходник на шару,
  задание, поиск потерянного ответа и снятие у шлюза подряд дольше общих 150 с.
- `LimitRequestBody` vhost не меньше пределов службы (документ и картинка ролика — 20 МиБ, фото
  с маской 2×10 МиБ) — сверяет `test_apache_lets_through_the_largest_body_the_studio_accepts`.
- SQLite — только на локальном диске: на CIFS он портится. Копия базы на шару — готовым файлом
  (`journal_mode=DELETE`), а не живой базой.
- `studio-backup.service` — с теми же `User`/`Group`/`StateDirectory`, что у службы: другой
  `Group` у того же `StateDirectory` systemd перевёл бы владельца каталога базы.
- Отмена задания запрос к шлюзу не обрывает (LiteLLM обрыв дальше не передаёт): задание ждёт
  конца, выбрасывает картинку и само освобождает карту (`release_after_cancel`).
- Аренда модели картинок держит карту 600 с простоя, и другая модель её не вытесняет: без
  release FLUX после Lustify ждал карту 300 с и получал 503 (приёмка 2026-10-20). Поэтому
  `images.generate` перед запросом отпускает другую модель; тесты считают и эти release.
- Освобождение карты после отмены идёт подключением учётки (токен читается из базы): удаление
  учётки ждёт её заданий (`images.settle_user`, до `SETTLE_SEC`), иначе каскад унёс бы токен.
- Список роликов шлюза (`GET /slot/videos`) общий для всех ключей: студия читает его только для
  ролика, ответ на создание которого потерялся, и только если seed выбрала она сама
  (`videos._adopt`: случайный 32-битный seed — единственный признак своего, плюс параметры и
  промпт). Seed пользователя (скрипты и другие учётки ставят те же 42) ничего не доказывает:
  тогда список не читается, ответ — «ролик мог начаться» (`LOST_MANUAL`). Не ослаблять: чужое
  задание студия скачала бы в ленту и сняла у шлюза.
- HTTP-клиент службы (`state.make_http`) один на всех пользователей и cookie ответов не хранит
  (`CookieJar` с политикой без доменов, передан самой банкой: `httpx.Cookies(...)` скопировал бы её
  без политики) — иначе cookie сервиса из запроса одного уходила бы с запросами других к тому же хосту.
- Наблюдатель роликов не ждёт подключения по очереди: ролики каждого подключения — своей
  задачей (`videos.WATCHING`, не больше `POLL_MAX`), не ответивший по сети шлюз — на паузе
  `PAUSE_SEC`, mp4 целиком — за `FETCH_SEC`. Остановка гасит задачи вместе с `_watch`.
- Освобождения слотов `asr` и `tts-qwen` у шлюза нет: перед расшифровкой и озвучкой студия
  освобождает модели картинок и gemma-abl (`speech.free_card`), а на 503 ждёт и повторяет
  (`RETRY_SEC`, до `WAIT_MAX_SEC`). Отмена обрывает только свой запрос — WhisperX дослушает запись.
  Голосовой ввод WhisperX — так же, но в пределах своего бюджета (`dictation.BUDGET_SEC`, затем 504).
- Имя файла записи едет заголовком `X-Studio-Name`, а не строкой запроса: строку пишет журнал Apache.
- Перевод промпта («EN», `POST /api/images/translations`) идёт маршрутом описания
  `/slot/images/descriptions` — текстом, без картинки, а не чат-ролью `local-gemma-abl`: у этого
  маршрута есть scoped release gemma-abl, и задание вида `translation` проходит путь «Описать» —
  одно задание за раз, release моделей картинок до запроса и gemma-abl после, так что следующий
  Lustify не ждёт карту под арендой gemma-abl. Обрезанный ответ (`finish_reason: length`) —
  ошибка, а не половина промпта в поле.
- Из LAN запрос на публичный адрес приходит с роутера 10.77.1.1 (петля): для него пауза входа —
  только на пару «логин + адрес» (`config.hairpin_ip`), иначе запирался бы весь дом.
- Блокировка на телефоне — 423 `screen_locked`, пауза после неудач — 429 `locked`: не путать (и
  имена: `screen_lock*` против `auth.LOCK_SEC` / `locked` / `locked_key`). Новый путь, который
  запертая сессия должна делать, — только в `auth.SCREEN_LOCK_PATHS`.
- Варианты passkey (`/api/lock/challenge`, `/api/passkeys/options`) готовятся до нажатия, а
  `navigator.credentials.get/create` зовётся первым в обработчике нажатия: Safari, если перед ним
  ждали сеть, теряет «нажатие», и Face ID на iPhone не появится (в Chrome всё работает) — сторожит
  `test_open_calls_the_passkey_without_waiting_for_the_network`. Вызов на сервере один на сессию и
  живёт 120 с: варианты старше `lock.FRESH_SEC` готовятся заново заранее (таймер, возвращение), а
  пока идёт вызов устройства, новых не готовить — новый заменил бы подписываемый (403).
- Шторка и экран блокировки — `<dialog>` через `showModal()`: иначе открытое окно просмотра
  (`#viewer`, тоже top layer) осталось бы над ними. Escape их не закрывает (cancel отменён,
  `closedby="none"`, закрытое силой окно открывается снова), а до нового `showModal()` оболочку
  прячет `<html class="shrouded">`.
- Запись голоса одна на студию (`mic.js`): смена раздела, шторка и блокировка зовут
  `cancelRecording()`, кнопка, ушедшая со страницы, отменяет свою запись сама; отменённый звук не
  уходит.
- id ключа в API — хеш credential id, а не он сам: `DELETE /api/passkeys/<id>` пишет журнал Apache.
- `seen` сессии — только по отметке `POST /api/lock/alive`, а не по любому запросу: опросы разделов
  идут на Android и при погасшем экране.
