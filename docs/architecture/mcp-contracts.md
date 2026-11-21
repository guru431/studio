# Контракты Studio для MCP-серверов

Studio — клиент MCP по Streamable HTTP (`mcp_client.Session`, [chat](chat.md#mcp)). Сверх стандарта она понимает
несколько своих расширений; их может реализовать **любой** сервер — без них он работает как обычный MCP, просто без
этих возможностей. Образец сервера со всеми расширениями — обёртка aitools-studio (отдельный проект рядом с aitools
на Spark, раздел «Контракты Studio» её README). Имена — общие для Studio и серверов; меняются только вместе.

Модули: `mcp_sources` (каталог и пометки инструментов), `mcp_contracts` (возможности сервера, файлы пользователя,
состояние источника), `help` («Справка»: руководства), `mcp_sections` (разделы, адреса маршрутов контрактов).

## Где объявляются

| Что | Где сервер объявляет | Ключ |
|---|---|---|
| Пометки инструмента | `_meta` инструмента в `tools/list` | `studio/confirm`, `studio/poll`, `studio/chat-files`, `studio/limits` |
| Возможности сервера | ответ `initialize`: `experimental` в `capabilities` | `studio/files`, `studio/user-files`, `studio/status` |
| Руководство | стандартные ресурсы MCP: `resources/list`, `resources/read` | ресурс `text/markdown` с `audience` для пользователя |

Значение каждого ключа `experimental` — **объект** (так требует схема MCP) с `"version": 1`; другая версия или не
объект — как не объявлено. Пример:

```json
"capabilities": {"experimental": {
  "studio/files": {"version": 1, "max_upload": 2147483648, "chunk": 33554432},
  "studio/user-files": {"version": 1, "files": [
    {"key": "youtube-cookies", "title": "Cookies YouTube", "accept": ".txt", "max_bytes": 1048576}]},
  "studio/status": {"version": 1}}}
```

`mcp_contracts.features(catalog)` читает их из каталога сервера (`mcp_sources.catalog`, тот же кэш, что у
инструментов) в `mcp_contracts.Features`: `files` — `{max_upload, chunk}` или None, `user_files` — список
`{key, title, accept, max_bytes}` (ключ — `mcp_contracts.KEY`, повтор и неверная запись пропускаются, не больше
`mcp_contracts.USER_FILES_MAX` (16)), `status` — объявлено ли состояние.

## Маршруты контрактов

Возможности сервера работают через HTTP на хосте самого сервера MCP: адрес строит `mcp_sections.endpoint(session,
path)` — схема, хост и порт подключения, путь — **только из кода Studio** (`/artifacts/…`, `/status`,
`/user-files/…`), никогда из ответа сервера или инструмента. Запросы — через `upstream.stream` с правилами списка
разрешённых адресов, заголовки — как у JSON-RPC (`mcp_client.Session.headers`): ключ и заголовки подключения, у
общего сервера ещё `X-Studio-User` (`access.studio_user(uid, sid)` — свой у каждого сервера). Сервер различает
учётки по `X-Studio-User`; у личного сервера заголовка нет — он принадлежит одной учётке. Ошибки сервера — JSON
`{"code", "message"}` с кодом HTTP; Studio передаёт 404 и 413 тем же кодом, прочее — 409 со словами сервера.
Доступ учётки к серверу — как у остального MCP (`mcp_client.row`: своё, общее и группы доступа), выключенный
сервер не опрашивается.

## Пометки инструментов

- **`studio/confirm`** — вызов с подтверждением пользователя: `true` (всегда) или `{"message", "when"}` —
  `message` до `mcp_sources.CONFIRM_MESSAGE_MAX` знаков, `when` — условия по аргументам (`{"arg", "gt": число}`,
  `{"arg", "nonempty": true}`); спрашивать, если условий нет или выполнено хотя бы одно
  (`mcp_sources.confirm_needed`). Неверная разметка — подтверждение всегда. Политика администратора общего сервера
  сильнее пометки (`mcp_sources.effective`); как это действует в чате — [chat](chat.md#mcp-в-диалоге-спека-2026-11-21-studio-source-foundation-design-4-и-7a-шаг-схемы-57).
- **`studio/poll`** — `true` (только явное значение): инструмент опрашивает долгое задание (`job_status`,
  `job_result` обёртки aitools-studio), и его тот же вызов с теми же аргументами в одном ответе — законный повтор.
  Обычный повтор Studio не исполняет (шаг «Повтор пропущен», [chat](chat.md#круги-инструментов-и-повторы-спека-2026-11-21-studio-source-capabilities-design-п-6)),
  помеченный — исполняет снова (`mcp_sources.hints`, `chat.StreamTask._polls`). Подтверждение пометка не снимает;
  ставить её только инструментам без побочных действий — повтор вызова ничего не меняет на сервере.
- **`studio/chat-files`** — `{"max_bytes": N}` у инструмента `artifact_upload`: сервер принимает файлы диалога
  (вложения, звук и видео) — `mcp_sources.chat_files`, с согласием на пару «диалог, сервер»
  ([chat](chat.md#файлы-между-чатом-и-mcp)).
- **`studio/limits`** — пределы инструмента числами, которые Studio может проверить до вызова, например
  `{"max_seconds": 3600}` у инструмента расшифровки (длительность записи). «Расшифровка» читает `max_seconds`
  (`speech.tool_limits`, поле `limits` каталога раздела) и до отправки отказывает словами записи длиннее; запись
  Studio для MCP не режет ([speech](speech.md)).

Веб-инструмент — стандартная `annotations.openWorldHint = true` (только явное значение,
[chat](chat.md#mcp-в-диалоге-спека-2026-11-21-studio-source-foundation-design-4-и-7a-шаг-схемы-57)).

## `studio/files` — «Мои файлы»

`{"version": 1, "max_upload", "chunk"}`: личная папка учётки на сервере. Путь — от личной папки через `/`
(пустой — сама папка); `..`, выход через symlink и служебные папки сервера — 400.

| Маршрут | Запрос | Ответ |
|---|---|---|
| `POST /files/list` | `{path}` | `{path, total_bytes, entries: [{name, dir, size, mtime}]}` — папки первыми |
| `GET /files/content` | `X-Studio-Path: base64url(UTF-8 пути)`, `Range` | файл (206 на Range) |
| `PUT /files/upload` | `X-Studio-Path`, `Content-Range: bytes a-b/total`, тело — кусок не больше `chunk` | `{done, size}` |
| `POST /files/mkdir` | `{path}` | `{path}` (имя занято файлом — 409) |
| `POST /files/delete` | `{path}` | `{id}` — в корзину сервера |
| `POST /files/trash` | `{}` | `[{id, path, deleted, size}]`, новые первыми |
| `POST /files/restore` | `{id}` | `{path}` — на прежнее место (имя занято — `a (2).txt`) |
| `POST /files/zip` | `{path}` | `{id, size}` — архив на сутки |
| `GET /files/zip/{id}` | `Range` | ZIP (206 на Range) |

Загрузка кусками: `a` должно равняться принятому размеру (больше — 409 с `size`, меньше — повтор куска), последний
кусок переносит файл на место; `total` больше `max_upload` или кусок больше `chunk` — 413; пустой файл —
`Content-Range: bytes */0`. Studio читает объявление (`files` в `mcp_contracts.Features`).

**В Studio** (`my_files.py`, страница `#/myfiles` — `my-files.js`, пункт меню аватара «Мои файлы» виден при хотя бы
одном таком сервере): маршруты `/api/my-files` — прокси на маршруты выше с ключом сервера и `X-Studio-User` учётки,
поэтому каждый видит свою папку, администратор — тоже свою; доступ к серверу — как у остального MCP. `GET /servers`
→ `[{sid, name, max_upload, chunk}]` (кусок — не больше `my_files.CHUNK`); `POST /{sid}/list|mkdir|delete|trash|restore`;
`POST /{sid}/ticket {path}` и `POST /{sid}/zip {path}` → адрес `GET /{sid}/download/{ticket}` (ticket — шифр
`STUDIO_DATA_KEY` с учёткой, сервером и подписью его адреса и ключа, срок `my_files.TICKET_SEC`; Range — докачка);
`PUT /{sid}/upload` — путь в `X-Studio-Name`, `Content-Range`, кусок до `my_files.CHUNK` (Apache —
свой `LocationMatch` на 34 МиБ), 409 `gap` с `size` — продолжить с принятого. Ответ всегда вложением
(`application/octet-stream`, `nosniff`, песочница CSP). `GET /api/me/storage` → `mcp_files: [{sid, name, bytes}]` по
`total_bytes`. Пока заперт хоть один раздел, `/api/my-files` закрыт (`lock_policy.blocks`).

**Личная ссылка на файл** (`file_links`, шаг схемы 58): `POST /api/my-files/{sid}/links {path}` →
`{id, url: "https://<хост>/f#<token>", expires}`; токен `secrets.token_urlsafe(32)` только во фрагменте адреса, в
базе — sha256, путь и имя — шифром, срок `my_files.LINK_SEC` (86400); не больше `my_files.LINKS_MAX` (100)
действующих у учётки. `GET /api/my-files/links` — свои действующие, `DELETE /api/my-files/links/{id}` — отзыв;
стирание истории уносит ссылки учётки. Анонимный вход: `GET /f` — оболочка `file.html` (`file-link.js`);
`POST /f/check {token}` → `{name, size, expires}`; `POST /f/open {token}` → cookie `studio_file` (HttpOnly, Secure,
SameSite=Strict, Path=/f/file, `my_files.OPEN_SEC` (600), значение — случайный id в памяти процесса);
`GET /f/file` — поток с Range, ссылка проверяется на каждый запрос. Тело POST — до `chat_shares.PUBLIC_BODY_MAX`
(`chat_shares.public_guard`), чужой `Origin` — 403; неизвестный, истёкший, отозванный токен, сменённый адрес или
ключ сервера (`stamp`), снятый доступ владельца, выключенная учётка, исчезнувший файл — одинаковый 404 без имени
файла; неудачи — `email_accounts.throttle` по адресу (`my_files.FAILS` (30) за `my_files.FAIL_SEC` (900)), потом
429 `file_locked`; удачная проверка попытку возвращает. Все ответы — `no-store`, `no-referrer`, `noindex`.

## `studio/user-files` — файлы пользователя

Файлы, которые пользователь кладёт для инструментов сервера (например, cookies YouTube в формате Netscape):
`files` — `[{key, title, accept, max_bytes}]`. Сервер хранит их в личной папке учётки так, чтобы модель не могла
получить их аргументом инструмента.

| Сервер | Ответ |
|---|---|
| `GET /user-files` | `[{key, title, present, size, mtime}]` |
| `PUT /user-files/{key}` — тело до `max_bytes` (иначе 413) | элемент списка |
| `DELETE /user-files/{key}` | элемент списка |

Studio: `GET /api/mcp/{sid}/user-files` (объявленные файлы и есть ли они; без контракта — пусто, без запроса к
серверу), `PUT /api/mcp/{sid}/user-files/{key}` — тело запроса и есть файл, не больше `max_bytes` контракта и
`mcp_contracts.USER_FILE_MAX` (1 МиБ), пустой — 422, ключ не из объявления — 404; `DELETE` — убрать. Содержимое
Studio не хранит, модели не передаёт и в журнал не пишет: в журнал — id сервера и размер. Страница — блок «Файлы для
инструментов сервера» у включённого сервера в «Настройки → Чат → MCP» (`mcp-settings.js`, «Загрузить», «Убрать»).

## `studio/status` — состояние источника

`GET /status` → `{state, queue, message}`: `state` — `ok`, `busy` (у учётки идут задания, `queue` — сколько) или
`down`. Studio: `GET /api/mcp/status` → `{"<sid>": {state, queue, message}}` включённых серверов учётки, объявивших
контракт; ответ сервера кэшируется на `mcp_contracts.STATUS_TTL_SEC` (15 с) по паре (сервер, учётка: у общего —
`X-Studio-User`), не больше `mcp_contracts.STATUS_KEEP` записей; неверный ответ или сбой — `down` со словами
`mcp_contracts.STATUS_FAILED`. Панели серверов пользователю не открываются — видно только состояние. Страница —
точка у чипа MCP чата (худшее состояние действующих серверов, подсказка — по серверам) и у выбора «Студия/MCP» в
разделах (по выбранному серверу; занятый и недоступный подписаны в списке): атрибут `data-mcp-state`. Выбор разделов —
`picker.js::picker` поверх `select#section-mcp-tool`: атрибут и подсказка — у его кнопки, точка — перед подписью.

## Руководство

Сервер публикует руководство пользователя ресурсом MCP: `resources/list` — `{uri, name, title, mimeType:
"text/markdown", annotations: {audience: ["user"]}}`, `resources/read` — текст. Studio (`mcp_client.Session.resources`
до `mcp_client.RESOURCES_MAX` (64) ресурсов, `mcp_client.Session.read_resource` до `mcp_client.RESOURCE_MAX` (1 МиБ))
берёт только ресурсы `text/markdown` с `user` в `audience` и читает только перечисленный сервером `uri`; прочие
ресурсы не показывает и не читает.

«Справка» (`#/help`, пункт меню аватара; `help.js`): `GET /api/help` → `{studio: {sections: [{id, title}]},
servers: [{sid, name, resources: [{uri, name}]}]}`; `GET /api/help/studio` — руководство Studio
(`help.GUIDE`, docs/guide.ru.md: в выкладку — частью `PARTS`, в образ — `COPY`) без вступления до первого `## `;
`POST /api/help/server {sid, uri}` → Markdown руководства сервера. Относительные ссылки и якоря становятся простым
текстом (`help.plain_links`): на сайте адресов репозитория нет, а якорь сменил бы раздел страницы. Рендер — тот же
безопасный Markdown, что у ответов чата (`markdown.js`, HTML — текстом); разделы руководства Studio — кнопками.
