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
  CSRF, пауза после неудач), `profile`, `connections` (подключения, список разрешённых адресов,
  прокси к шлюзу), `admin`, `chat` (поток SSE), `documents`, `images`, `videos`, `media` (файлы на
  шаре, `Range`), `history`; база — `db.py`, окружение — `config.py` (`REQUIRED`), часы —
  `clock.py`, команды хоста — `manage.py` (`create-admin`, `backup`). Фронтенд без сборки —
  `app/static/` (модули ES, сторонние библиотеки копией в `static/vendor/`, без CDN).
- Подключения: к адресам из списка разрешённых (панель администратора, по умолчанию
  `10.77.1.15:4000`) ходит сервер, токен в браузер не уходит; к остальным — браузер. По адресам
  вне списка сервер не ходит (с учётом перенаправлений и разрешения имён).
- История: база SQLite — `/var/lib/studio/studio.db` (локальный диск); файлы — storage
  `\\storage\dd$\SharedData\studio`, на debian `/mnt/storage/SharedData/studio`. Токены подключений в
  базе зашифрованы `STUDIO_DATA_KEY` (Fernet), пароли — scrypt.

## Правила

- **Приватность — контракт, а не пожелание. История — в базе и на шаре, в журнал — ничего.**
  Промпты, ключи, пароли, тексты сообщений и документов, картинки, ролики и имена файлов в журнал
  не пишутся (uvicorn с `--no-access-log`, журнал — только события с id). Новый код это не ломает
  (сторожат тесты журналов разделов).
- **Анонимны только** `/health`, `/login` (страница и вход), `/logout` и оболочка PWA:
  `/static/*`, `/manifest.webmanifest`, `/sw.js`, `/offline.html`; `/` без сессии уводит на вход,
  всё `/api/*` — через `auth.guard` (сессия + CSRF у изменяющих запросов).
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
- Секрет — `/etc/studio/studio.env` (0600 root, одна строка `STUDIO_DATA_KEY=`, vault
  `STUDIO_DATA_KEY`): его читает systemd, а не служба. Смена ключа делает прежние токены
  подключений нерасшифровываемыми — их вводят заново.
- Копия базы — `studio-backup.timer` (04:30, `Persistent=true`) → `studio-backup.service` →
  `manage.py backup`: `sqlite3` backup во временный файл рядом с базой, затем на шару
  `backup/studio-YYYYMMDD.db` (временное имя + `os.replace`), 7 последних; юнит ограничен
  `TimeoutStartSec=30min`. В копиях удалённое («Удалить всю историю», удалённые учётки) живёт ещё
  до 7 дней. Восстановление —
  остановить `studio-web`, положить копию на место `/var/lib/studio/studio.db` (владелец
  `studio:www-data`, удалить `-wal`/`-shm`), запустить.
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
`install.sh` ставит зависимости до подмены кода, держит прежний код в `.previous` до ответа
`/health` от www-data через сокет, ставит юниты копии базы и включает таймер. Перезапуск ждёт
открытые потоки чата не дольше 10 с (`--timeout-graceful-shutdown`, `TimeoutStopSec=30`);
недописанное помечают `chat.recover()` / `images.recover()` при старте. Смоук —
`https://studio.example.com/health`; живая приёмка — `tests/test_studio_upload_live.py` (ниже).

## Тесты

    .venv/Scripts/python -m pytest -q                                   # быстрый набор (~25 с)
    .venv/Scripts/python -m pytest -q -m integration                    # браузер (Edge/Chrome + node 22), PowerShell
    v() { grep "^$1=" C:/work/projects/local-config/secrets/vault.env | cut -d= -f2-; }    # живая приёмка: значения не печатать
    STUDIO_ADMIN_LOGIN=$(v STUDIO_ADMIN_LOGIN) STUDIO_ADMIN_PASSWORD=$(v STUDIO_ADMIN_PASSWORD) \
      GATEWAY_KEY_OWNER_IMAGES=$(v GATEWAY_KEY_OWNER_IMAGES) .venv/Scripts/python -m pytest -q -m manual

Живая приёмка (`tests/test_studio_upload_live.py`, генерирует на карте, до ~1,5 ч) — после
выкладки и `--create-admin`: входит администратором в `STUDIO_LIVE_URL` (по умолчанию
`https://studio.example.com`), делает Lustify, правку кистью (Flux, вне маски — пиксели исходника),
описание gemma-abl, ролик 1 с и ответ `local-gemma-abl` по PDF; удаляет только своё и освобождает
карту. Без `STUDIO_ADMIN_*` эта часть пропускается; с `GATEWAY_KEY_OWNER_IMAGES` — ещё шлюз
напрямую (`STUDIO_LIVE_GATEWAY`) и `client/studio-batch.local.ps1`.

Среда: `"/c/Program Files/Python314/python" -m venv .venv`, затем
`.venv/Scripts/python -m pip install -r requirements-dev.txt`. Приложение в быстрых тестах —
фикстура `studio` (`tests/conftest.py`): шлюз подменён `httpx.MockTransport` (`tests/fakes.py`),
часы — `clock.NOW`, паузы — `clock.SLEEP`. Браузерные сценарии — `tests/test_ui_browser.py`
(живая служба `tests/live_app.py`, драйвер `tests/browser.py` + `cdp_driver.mjs`). Договорённость
юнитов, vhost и скриптов выкладки сверяет `tests/test_deploy.py`.

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
- Из LAN запрос на публичный адрес приходит с роутера 10.77.1.1 (петля): для него пауза входа —
  только на пару «логин + адрес» (`config.hairpin_ip`), иначе запирался бы весь дом.
