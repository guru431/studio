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
  srt), `voices` (образцы голосов), `speeches` (озвучка в фоне), `admin`, `chat` (поток SSE), `documents`, `images`, `videos`, `media` (файлы на
  шаре, `Range`), `history`; формы фото и роликов (тело с пределом, фото файлом или из истории) —
  `forms.py`; цепочка промптов картинок и роликов (снимок шагов в `images.chain` / `videos.chain`,
  у старых строк — по ссылкам) — `chains.py`, в ответе — только у одной записи (`GET …/<id>`), лента
  несёт число шагов `steps`; база — `db.py`, окружение — `config.py` (`REQUIRED`),
  часы — `clock.py`, команды хоста — `manage.py` (`create-admin`, `backup`). Фронтенд без сборки —
  `app/static/` (модули ES, сторонние библиотеки копией в `static/vendor/`, без CDN); шторка,
  экран блокировки, отметки и помощники passkey — `lock.js` (ставит `api.js` `hooks.locked`: 423
  ждёт отпирания и повторяется); вид ленты
  фото и видео (превью, плитка, таблица) — `views.js`, выбор, как и тема, — в учётке (`users.views`,
  `PATCH /api/me`); вернувшись на экран, оболочка перечитывает `/api/me`. Иконки (PNG и
  `favicon.ico`) — `tools/make_icons.py` по геометрии `icons/icon.svg`: правка рисунка — в обоих,
  PNG коммитятся.
- Схема базы — `db.VERSION`; новая база получает `SCHEMA` сразу, база прода — шаги `db.STEPS`
  (только добавлять: откат `install.sh` на прежний код должен работать с новой базой).
- Подключения: к адресам из списка разрешённых (панель администратора, по умолчанию
  `10.77.1.15:4000`) ходит сервер, токен в браузер не уходит; к остальным — браузер (только чат).
  По адресам вне списка сервер не ходит (с учётом перенаправлений и разрешения имён). Запись
  списка — IP:порт или имя:порт; имя — только https, при каждом запросе все его адреса должны быть
  публичными (`ipaddress.is_global`), соединение — с проверенным адресом (SNI и `Host` — имя,
  `upstream.RESOLVE`). Правило путей `PROXIED_PATHS` — только для IP-записей.
- Разделы (`chat`, `photo`, `video`, `transcribe`, `speech`, `dictation`) — таблицы `section_links`
  (подключение в разделе и его отмеченные модели) и `section_defaults`; встроенный перечень srvllm —
  `sections.CATALOG`. Старые `connections.sections` и `users.default_*` остаются для отката и
  держатся согласованными (`sections._sync_legacy`) — новая логика их не читает. Ход начатого
  диалога пару не перепроверяет (`chat.create_turn` — `connections.get`, план section-connections,
  Task 4): галочки — правило выбора для нового, не запрет старого (README «Подключения и разделы»).
- История: база SQLite — `/var/lib/studio/studio.db` (локальный диск); файлы — storage
  `\\storage\dd$\SharedData\studio`, на debian `/mnt/storage/SharedData/studio`. Токены подключений в
  базе зашифрованы `STUDIO_DATA_KEY` (Fernet), пароли — scrypt.
- Путь файла без строки `media` не теряется — очередь удаления `purge` (без FK на `users`, переживает
  удаление учётки): туда идут файлы, которые шара не дала удалить, и файлы, которые пишутся сейчас
  (`save_async`/`save_stream` ставят путь до записи, строка `media` снимает его той же транзакцией, —
  отмена и остановка службы посреди записи не оставят файл без записи). Проходит её `media.purge()`:
  при старте и раз в `PURGE_SEC` (`start_purger`), в каждом `delete_async` и в начале стирания
  истории; пишущиеся (`media.SAVING`) пропускает. `DELETE /api/history` и `/api/admin/users/<id>`
  отвечают `{"pending_files": N}` — сколько файлов ещё ждут, страница это говорит.

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
  пробует настоящую запись от `studio` (`test -w` при `noperm` верен всегда).
- Звук — `ffmpeg` debian (ставит `install.sh`, если нет): временные файлы — `/var/lib/studio/tmp`
  (`config.tmp_dir`), чистится при старте службы. Запись для расшифровки — до 200 МиБ телом запроса
  потоком на шару: `LimitRequestBody` 210 МиБ только на `^/api/transcripts$` (`LocationMatch` vhost),
  общий предел — прежний.
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
  Восстановление —
  остановить `studio-web`, положить копию на место `/var/lib/studio/studio.db` (владелец
  `studio:www-data`, удалить `-wal`/`-shm`), запустить. Файлов, удалённых после даты копии, на шаре
  уже нет: их строки `media` отвечают 404.
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
  ролика, ответ на создание которого потерялся (`videos._adopt`, по seed студии), — в ленту чужое
  не попадает.
- Освобождения слотов `asr` и `tts-qwen` у шлюза нет: перед расшифровкой и озвучкой студия
  освобождает модели картинок и gemma-abl (`speech.free_card`), а на 503 ждёт и повторяет
  (`RETRY_SEC`, до `WAIT_MAX_SEC`). Отмена обрывает только свой запрос — WhisperX дослушает запись.
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
  `test_open_calls_the_passkey_without_waiting_for_the_network`.
- Шторка и экран блокировки — `<dialog>` через `showModal()`: иначе открытое окно просмотра
  (`#viewer`, тоже top layer) осталось бы над ними. Escape их не закрывает (cancel отменён, закрытое
  силой окно открывается снова).
- id ключа в API — хеш credential id, а не он сам: `DELETE /api/passkeys/<id>` пишет журнал Apache.
- `seen` сессии — только по отметке `POST /api/lock/alive`, а не по любому запросу: опросы разделов
  идут на Android и при погасшем экране.
