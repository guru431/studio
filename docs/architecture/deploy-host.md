# Прежняя нативная установка, копия базы, откат

Основной запуск с 2026-11-11 — [Docker Compose](deploy-docker.md). Этот документ сохраняет устройство прежней
установки для восстановления и подробности совместимости схемы. Apache остаётся публичным HTTPS-входом;
действующий профиль Docker нельзя переключать на `systemd` без переноса актуальной базы назад.

## Хост (debian)

- Доступ: `ssh -i ~/.ssh/studio_deploy_key -p 22222 deploy@10.77.1.10`, passwordless sudo.
- Код и `.venv` (Python 3.13) — `/opt/services/studio`, владелец root; служба `studio-web.service`
  ([deploy/studio-web.service](../../deploy/studio-web.service)) от пользователя `studio` (группа
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
- **api.exa.ai с debian — только через маршрут** проекта `network` (2026-10-22): выходу debian (Финляндия)
  Cloudflare Exa отвечает 403 HTML-страницей — поиск и страницы «Exa недоступен с сервера студии». Код
  студии под это не меняется. Проверка маршрута: с debian `curl -s -X POST https://api.exa.ai/search -H
  'Content-Type: application/json' -d '{}'` — JSON 402 `X402_PAYMENT_REQUIRED`, а не HTML.
- Секрет — `/etc/studio/studio.env` (0600 root: строка `STUDIO_DATA_KEY=` (vault
  `STUDIO_DATA_KEY`) и необязательная `STUDIO_CLEANER_CA=/путь` — сертификат Cleaner, docs/cleaner-integration.md): его читает systemd, а не служба. Смена ключа делает прежние токены
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
- Apache: vhost [deploy/apache-vhost.conf](../../deploy/apache-vhost.conf) (`*:8443` за прозрачным
  `sslh`, wildcard `/etc/letsencrypt/live/example.net/`) подключён в конце `/opt/services/apache/apache2.conf`
  строкой `IncludeOptional /opt/services/studio/deploy/apache-vhost.conf`. Правка vhost — выкладка:
  `install.sh` после подмены кода гоняет `apache2ctl -t` (отказ — прежний код на место, служба не
  перезапускается) и после ответа `/health` всегда делает `reload apache2`.
- Журнал: `sudo journalctl -u studio-web`, `-u studio-backup` (промптов и имён файлов там нет).

- PDF экспорта чата — Chromium: `chromium` и `fonts-dejavu-core` ставит `install.sh`, если их нет; `deploy/md2pdf.py`
  выкладки сверяется с `deploy/md2pdf.sha256` до подмены кода (sharing-export.md).
- Путь к службе один — Apache debian (CLAUDE.md, «Инварианты»):

- **Путь к службе один — Apache debian.** Служба слушает только unix-сокет `/run/studio/web.sock`
  (каталог `0750 studio:www-data`) и потому верит последнему элементу `X-Forwarded-For`. Порт TCP
  не открывать: счёт попыток входа подделывался бы заголовком.

- Команды хоста — `manage.py` (`create-admin`, `backup`, `verify-backup`); проверку копии зовёт и панель —
  `POST/GET /api/admin/backup-check`, поток-демон `admin.CHECK`, одна за раз.

## Выкладка

    bash deploy/deploy.sh                    # HEAD → debian, install.sh, проверка через Apache
    bash deploy/deploy.sh --new-secret       # заново положить STUDIO_DATA_KEY из vault
    bash deploy/deploy.sh --create-admin     # первый администратор и его подключение «srvllm» из vault

Выкладывается закоммиченный HEAD (`git archive` частей `app deploy requirements.txt`); грязное
дерево выкладываемых частей и `agent/` — отказ. С ним едут установщик EXE и ZIP обновления моста:
`git archive --format=zip --prefix=studio-agent/ HEAD:agent` → `.incoming/agent/studio-agent.zip`, строка
`==> мост <хеш>` того же коммита студии. До первого SSH `deploy/build-agent-installer.ps1` собирает
`studio-agent-setup.exe` из этого ZIP и `deploy/agent-installer.cs` того же HEAD (штатный csc .NET Framework Windows).
`GET /api/agent/download` отдаёт EXE браузеру, `/api/agent/package` — ZIP действующему мосту.
Нет моста в HEAD или он грязный — отказ до копирования кода. `install.sh` без ZIP или EXE —
отказ до подмены; `agent` переезжает в `SWAP` с кодом, и откат `.previous` возвращает оба файла.
Ключ данных при первой установке (в `studio.env` нет `STUDIO_DATA_KEY=`) едет сам.
`--create-admin` берёт из vault `STUDIO_ADMIN_LOGIN`, `STUDIO_ADMIN_PASSWORD`,
`GATEWAY_KEY_OWNER_IMAGES` и через `install.sh --admin-stdin` отдаёт в `manage.py create-admin`
(`systemd-run --pipe` от `studio` с окружением юнита); повтор безопасен, новый пароль гасит сессии.
`install.sh` до подмены кода собирает новую среду Python рядом (`.incoming/.venv`; только если
сменились `requirements.txt` или `python3` — штамп `.venv/.studio-requirements`), переносит её на
место вместе с кодом, держит прежние код и среду в `.previous` до ответа `/health` от www-data
через сокет, ставит юниты копии базы и включает таймер. Отказ `systemctl restart` и молчание `/health`
идут одним путём `service_failed` — status, журнал и строки отката, без автоматического отката; таймер
копии базы, который не включился, — ошибка с кодом не 0 (служба уже обновлена). Перенесённая среда — с чужим shebang в
`bin/`: всё запускается `.venv/bin/python` (`-m uvicorn`, `-m pip`, `manage.py`), не `bin/<скрипт>`. Перезапуск ждёт
открытые потоки чата не дольше 10 с (`--timeout-graceful-shutdown`, `TimeoutStopSec=30`);
недописанное помечают `chat.recover()` / `images.recover()` / `ocr.recover()` / `translate.recover()` при старте. Каналы мостов Claude Code остановка закрывает 1012 (`bridge.stop`): мосты
переподключаются сами, ходы на машинах идут дальше; мост ≥ 0.7.1 сохраняет оставшийся срок PIN компьютеров. Смоук —
`https://studio.example.com/health`; живая приёмка — `tests/test_studio_upload_live.py` (ниже).

Windows-мост после выкладки сервера перезапускать не нужно. Активный мост нельзя завершать (Stop-Process/kill)
из выполняемого им задания: вместе с ним прервётся исполнитель. Мост обновляют после завершения заданий
и явного выхода в трее; установщик ≥ 0.7.2 отказывает до копирования файлов, если эта копия моста запущена.

## Откат по шагам схемы — подробности

Сводная таблица всех шагов с 16 — в [CLAUDE.md](../../CLAUDE.md#откат-по-шагам-схемы). Ниже — развёрнутые описания и
SQL для шагов, где их больше строки. SQL — служба остановлена, от `studio`:
`sudo -u studio sqlite3 /var/lib/studio/studio.db "…"`; каскады — только с `PRAGMA foreign_keys=ON;`.

**Откат на код до шага схемы 20** (`.previous`): прежний код таблиц `code_tabs` и `code_place` не читает — раздел
«Claude Code» прежний (лестница уровней), рабочее место не видно и не мешает. Его «Удалить всю историю» их не стирает:
вернувшись вперёд, тем, кто стирал историю, — стереть её снова, или `DELETE FROM code_tabs WHERE user_id IN (…);
DELETE FROM code_place WHERE user_id IN (…);`. Отзыв компьютера прежним кодом уносит его вкладки каскадом (FK в схеме).

**Откат на код до шага схемы 19** (`.previous`): прежний код таблиц `agents`, `agent_events`, `code_chats`,
`push_subs` и `settings` `vapid` не читает — вкладки «Claude Code», компьютеров в «Настройках» и push нет; SQL-обхода
не нужно (таблицы не связаны с разделами `section_links` и поиском).

- Мосты получают отказ рукопожатия, а не закрытие 4401: у прежнего vhost (он едет с кодом) нет правила
  `upgrade=websocket`, и `/api/agent/link` уходит общим `ProxyPass /` в `guard` — 401 HTTP (до `reload apache2` —
  403 Starlette: маршрута нет). Мост переподключается с паузой, как при обрыве, пока код не вернётся
  (протокол §2: отказ рукопожатия — не отзыв, «отозван» — только закрытие 4401). Привязка нового моста — тоже 401
  `login` от `guard` («нужен вход»): мост скажет «неверный логин или пароль».
- «Удалить всю историю» прежнего кода `code_chats` не стирает: вернувшись вперёд, отметки чатов снова видны. Тем,
  кто стирал историю, — стереть её снова, или `DELETE FROM code_chats WHERE user_id IN (…);`.
- Отозвать компьютер прежний код не может (вкладки нет): ключ устройства в базе остаётся и снова действует после
  возврата. Срочно — `DELETE FROM agents WHERE id = …;` (с `PRAGMA foreign_keys=ON` — и его `code_chats`).
- Каскады работают (FK в схеме): удаление учётки уносит её `agents`, `agent_events`, `code_chats`, `push_subs`;
  выход и конец сессии — её `push_subs`.

**Откат на код до шага схемы 18** (`.previous`): прежний код не знает раздела `translate` и таблиц
`translations`, `translation_parts` — вкладки «Перевод» нет, а строки раздела он не видит (`connections._from_row`
и `search.KNOWN`: правка подключения и поиск работают, сторожит `test_code_without_the_translate_section_ignores_its_rows`).
SQL — как у шага 16 ниже.

- «Удалить всю историю» прежнего кода уносит строки `media` и файлы, но не `translations`: тексты переводов
  остаются в базе и после возврата вперёд снова видны (файлы — 404, повтор — 409 `gone`). Вернувшись вперёд —
  тем, кто стирал историю, стереть её снова, или `PRAGMA foreign_keys=ON; DELETE FROM translations WHERE user_id
  IN (…);` (куски — каскадом, строки поиска — триггером `search_translations_ad`).
- Прежний `media.referenced` не знает `translations`: удаление в «Сканах» документа того же PDF (скан перевода —
  обычный документ `origin=section` с тем же файлом) уносит файл перевода — повтор записи без файла — 409 `gone`.
- Флаг `settings` `adopted:translate` остаётся: вернувшись вперёд, связи чата в переводчик заново не копируются
  (связи `translate`, что были до отката, прежний код не трогал — `set_sections` их не видит).
- Ждущие и идущие в момент отката переводы прежний код не трогает; вернувшись вперёд, `translate.recover()`
  делает их «прервано» — их можно «Продолжить».

Откат на код до шага схемы 17 (PIN) сверх этого обхода не требует: прежний код не читает `users.pin_hash` и
`pin_fails` (запертая студия открывается паролем и passkey; заданный PIN снова действует после возврата).

**Откат на код до шага схемы 16** (`.previous`): прежний код не знает раздела `ocr`. SQL ниже — служба
остановлена, от `studio`: `sudo -u studio sqlite3 /var/lib/studio/studio.db "…"`.

- `PATCH /api/connections/<id>` без `sections` (имя, токен — так шлют форма подключения и панель) у
  подключения srvllm отвечает 422, пока есть строки `ocr`. Обход: `DELETE FROM section_links WHERE
  section = 'ocr'; DELETE FROM section_defaults WHERE section = 'ocr';` (второе — для чистоты: прежний
  код читает умолчание только по своим разделам).
- Поиск, в выдачу которого попал распознанный документ, отвечает 500 (прежний `search._title` не знает
  `ocr`). Обход: `DELETE FROM search_docs WHERE section = 'ocr';` (FTS чистит триггер `search_docs_ad`);
  до возврата вперёд документов «Распознавания» в поиске нет. Вернувшись вперёд — вернуть их: `INSERT OR
  IGNORE INTO search_docs(section, item, part, user_id, model, created, body) SELECT 'ocr', id, '', user_id,
  model, created, name || ' ' || substr(COALESCE(text, ''), 1, 200000) FROM scans;` — тело как у триггера
  `search_scans_ai`, в FTS кладёт `search_docs_ai` (с заменой «ё»); `OR IGNORE` пропускает документы, что
  служба уже проиндексировала после возврата (уникальный ключ `search_docs_key`).
- «Удалить всю историю» прежнего кода уносит файлы и строки своих таблиц, но не `scans` и `scan_pages`:
  тексты распознанного остаются в базе и после возврата вперёд снова видны (картинки — 404). Вернувшись
  вперёд — тем, кто стирал историю, стереть её снова, или `PRAGMA foreign_keys=ON; DELETE FROM scans WHERE
  user_id IN (…);` (страницы и строки поиска — каскадом и триггером; без PRAGMA `sqlite3` каскад не делает).
- Вложение чата, чей скан шёл в момент отката, остаётся с `text = NULL`: прежний чат шлёт его модели
  строкой «None», пока при возврате вперёд `ocr.recover()` не поставит пометку «скан не распознан».

Новый код строки незнакомых разделов не видит (`connections._from_row` — `linked`, `search.KNOWN` — выдача
и модели поиска) — у отката со следующего нового раздела на него не будет первых двух бед (422 правки
подключения и 500 поиска); стирание истории и вложения без текста зависят от устройства того раздела.

**Шаги 21–36** (сводка — в таблице CLAUDE.md):

- **33 `chat_shares`**: прежний код маршрутов `/share*` и `…/share` не знает (ссылки не открываются). Его «Удалить всю
  историю» удаляет `chats` — ссылки обычных чатов уходят каскадом по `chat_id`, а агентские (`chat_id IS NULL`)
  переживают стирание и после возврата вперёд снова открываются по PIN. Вернувшись вперёд, тем, кто стирал историю:
  `PRAGMA foreign_keys=ON; DELETE FROM chat_shares WHERE user_id IN (…);` (читатели и список файлов — каскадом).
  Удаление компьютера и учётки прежним кодом уносит ссылки каскадом (FK в схеме).
- **34 сессии и журнал**: прежний код вставляет сессии без новых столбцов (`created`, `device`, `active` — NULL),
  журнал `security_events` не читает и не пишет; вернувшись вперёд — такие сессии без даты и вида (вид — по прежней
  отметке `mobile`). SQL не нужен.
- **36 шифрованные снимки**: получатель после верного PIN видит заглушку `chat_shares.PLACEHOLDER`; прежний код не
  проверяет срок и общий предел неверных PIN, но содержимого не выдаёт (в старом столбце заглушка, id файлов вложений —
  только в шифре); ссылки, созданные прежним кодом, по возвращении шифрует `chat_shares.seal()` (срок — бессрочный);
  журнал прежний код пишет без `count` (1); сброс пароля прежним кодом флаг `users.password_reset` не ставит. SQL не
  нужен.

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
