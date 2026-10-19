# CLAUDE.md — studio

Страница студии `https://llm.example.com/studio/` и пакетные скрипты для Windows — клиенты API
картинок и видео LLM-шлюза srvllm. Что умеет и как пользоваться — [README.md](README.md).
Репозиторий `git.example.net/xadmin/studio` — **приватный**: NSFW-назначение, хосты LAN.

## История и граница с llm_routers

- Выделен из `llm_routers` 2026-10-19: страница — бывший `srvllm/deploy/services/studio/`
  (служба `srvllm-studio-web` на srvllm, :8151), скрипты — `srvllm/client/`, тесты —
  `srvllm/tests/test_studio*.py`. История правок до выделения — `git log` llm_routers по этим
  путям; спеки там же: `docs/superpowers/specs/2026-10-16-srvllm-studio-design.md`,
  `2026-10-19-images-api-lustify-design.md`, `2026-10-19-srvllm-studio-video-design.md`. Вики —
  `local-config/wiki/projects/llm_routers/architecture-srvllm-studio.md`.
- Здесь только клиенты. ComfyUI, переводчик `srvllm-lustify-api`, слот `comfy`, маршруты шлюза
  `/slot/images/*` и `/slot/videos`, потребитель шлюза `owner-images` — в `llm_routers`. Смена
  контракта API (поля, модели, размеры, пределы) — сначала там, потом здесь: имена моделей и
  размеры сверяют тесты скриптов.
- `app/image_upload.py` — копия модуля переводчика на 2026-10-19; правка пределов здесь не должна
  выходить за пределы переводчика.

## Правила

- **Приватность — контракт, а не пожелание.** Промпты, ключ, картинки, ролики и имена файлов в
  журнал не пишутся (uvicorn с `--no-access-log`, журнал — только события с id); на диск ничего не
  ложится — только память службы и браузер. Новый код это не ломает.
- **Ключ шлюза владельца** живёт только в cookie сессии (HttpOnly, подписана
  `STUDIO_SESSION_SECRET`) и в памяти запроса. На хосте его копии нет и быть не должно.
- **Анонимны только** `/studio/health`, `/studio/` (форма входа), `POST /studio/login`,
  `POST /studio/logout`; всё `/studio/api/*` — через middleware `guard`.
- **Путь к службе один — Apache debian.** Служба слушает только unix-сокет `/run/studio/web.sock`
  (каталог `0750 studio:www-data`) и потому верит последнему элементу `X-Forwarded-For`. Порт TCP
  не открывать: счёт попыток входа подделывался бы заголовком.
- Секреты — vault `local-config/secrets/vault.env`; в репозиторий — никогда. Скрипты в репозитории
  без ключа (`$EmbeddedKey = ''`, сторожит тест), копии с ключом — `*.local.ps1`, в `.gitignore`.
- `client/*.ps1` — UTF-8 **с BOM** и CRLF (PowerShell 5.1 без BOM читает CP1251).

## Хост (debian, 10.77.1.10)

- Доступ: `ssh -i ~/.ssh/studio_deploy_key -p 22222 deploy@10.77.1.10`, passwordless sudo.
- Код и `.venv` (Python 3.13) — `/opt/services/studio`, владелец root; служба `studio-web.service`
  от пользователя `studio` (группа `www-data`), `ProtectSystem=strict`, `MemoryMax=2G`.
- Секрет — `/etc/studio/studio.env` (0600 root, одна строка `STUDIO_SESSION_SECRET=`, vault
  `STUDIO_SESSION_SECRET`): его читает systemd, а не служба. Смена гасит все сессии.
- Адрес шлюза — `Environment=STUDIO_GATEWAY_URL` в юните.
- Apache: блок [deploy/apache-location.conf](deploy/apache-location.conf) подключён в vhost
  `llm.example.com` (`/opt/services/apache/llm-0-55.conf` — копия `llm_routers/dashboard/deploy/apache-vhost.conf`)
  строкой `IncludeOptional /opt/services/studio/deploy/apache-location.conf`. Правка блока —
  выкладка, затем `sudo apache2ctl configtest && sudo systemctl reload apache2`.
- Журнал: `sudo journalctl -u studio-web` (промптов там нет).

## Выкладка

    bash deploy/deploy.sh                 # HEAD → debian, install.sh, проверка через Apache
    bash deploy/deploy.sh --new-secret    # заново положить STUDIO_SESSION_SECRET из vault

Выкладывается закоммиченный HEAD (`git archive` частей `app deploy requirements.txt`); грязное
дерево — отказ. `install.sh` ставит зависимости до подмены кода, держит прежний код в
`.previous` до ответа `/studio/health` от www-data через сокет. Смоук — `https://llm.example.com/studio/health`
из LAN; живая приёмка — `tests/test_studio_upload_live.py` (ниже).

## Тесты

    .venv/Scripts/python -m pytest -q                                   # быстрый набор (~5 с)
    .venv/Scripts/python -m pytest -q -m integration                    # браузер (Edge/Chrome + node 22), PowerShell
    GATEWAY_KEY_OWNER_IMAGES=… .venv/Scripts/python -m pytest -q -m manual   # живая приёмка: генерирует на карте

Среда: `"/c/Program Files/Python314/python" -m venv .venv`, затем
`.venv/Scripts/python -m pip install -r requirements-dev.txt`. Шлюз в быстрых тестах подменён
`httpx.MockTransport`, часы — `server.NOW`, паузы — `server.SLEEP`.

## Ловушки

- Значение cookie со знаком `=` Starlette отдаёт в кавычках, и подпись не сходится: сессия
  кодирует ключ base64url без `=`.
- `ProxyPass` — на имя `studio-backend`, не `localhost`: mod_proxy подбирает воркер по префиксу
  URL, и чужой корневой `|http://localhost/` увёл бы запросы в свой сокет.
- `LimitRequestBody` блока Apache не меньше предела службы (картинка ролика 20 МБ, фото с маской
  2×10 МиБ) — сверяет `test_apache_lets_through_the_largest_body_the_studio_accepts`.
- Отмена задания запрос к шлюзу не обрывает (LiteLLM обрыв дальше не передаёт): задание ждёт
  конца, выбрасывает картинку и само освобождает карту (`release_after_cancel`).
