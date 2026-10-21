# Интернет в чате (Exa, страницы, субтитры YouTube, дата) — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** кнопка «Интернет» в диалоге: модель сама ищет в Exa, читает страницы и субтитры YouTube; дата — в
каждом запросе чата.

**Architecture:** новый модуль `app/web.py` — инструменты (Exa `/search` и `/contents` ключом пользователя,
yt-dlp на хосте), их описания для модели (OpenAI function calling) и вид шагов. `chat.StreamTask` ведёт
цикл: поток модели → вызовы инструментов → сообщения `role: tool` → снова модель, не больше `TOOL_ROUNDS`
кругов; шаги — JSON в `messages.tools`, в следующих ходах `compose` кладёт их блоками `<web>` в сообщение
пользователя. Кнопка — `chats.web`, ключ — `users.exa_key` (Fernet), подтверждение «расшифровка → Exa» —
`chats.web_consent`.

**Tech Stack:** Python 3.13, FastAPI, httpx, SQLite, yt-dlp (общий на debian), ES-модули без сборки.

**Spec:** [docs/superpowers/specs/2026-10-21-studio-chat-internet-design.md](../specs/2026-10-21-studio-chat-internet-design.md)

**Среда исполнителя:** worktree без `.venv` — тесты средой основного checkout
`C:/work/projects/studio/.venv/Scripts/python -m pytest …`; первым шагом `git merge --ff-only main`, если
worktree отстал; файлы сообщений коммитов — `internet-msg-N.txt`.

## Global Constraints

- «Интернет» выключен (`chats.web = 0`) — в запросе к модели нет `tools`, Exa и yt-dlp не вызываются.
- Только путь через сервер (`conn.proxied`); у прямого подключения `web` игнорируется, `tools` нет.
- В Exa уходит только `query` / `url` из вызова модели, ключ — заголовком `x-api-key`; адрес — константа
  `https://api.exa.ai`, мимо `upstream` (исключение из списка разрешённых — спека 6.4).
- yt-dlp получает только `https://www.youtube.com/watch?v=<id>` (id — `[A-Za-z0-9_-]{11}`), argv без
  оболочки: `--ignore-config --no-playlist --no-cache-dir`; файлы — только в `state.CFG.tmp_dir`, имена
  `<uuid>.*` (файлы, не каталоги: `audio.clean_tmp` удаляет файлы), удаляются в `finally`.
- Журнал — только id и `search|page|youtube` + `ok|ошибка`: ни запросов, ни адресов, ни названий, ни
  ключа, ни stderr yt-dlp.
- Ключ Exa не уходит в браузер, выгрузку и журнал; `GET /api/me` — только `"exa_key": true|false`.
- Шаг схемы 14 только добавляет; прежний код (откат `install.sh`) работает с новой базой.
- Дата — системное сообщение первым в каждом запросе чата (сервер и прямой), пояс UTC+3, через `clock.NOW()`;
  описания и переводы раздела «Фото» — без даты.
- Числа: `TOOL_ROUNDS = 5`, `SEARCH_RESULTS = 10`, `PAGE_MAX = 40_000`, `SUBS_MAX = 120_000` знаков,
  `EXA_TIMEOUT_SEC = 30`, `YTDLP_TIMEOUT_SEC = 60`, `PARAGRAPH_SEC = 30`.

## Review Focus

1. Модель зовёт инструмент с битым JSON аргументов или с неизвестным именем — шаг-ошибка, модель получает слова
   и отвечает дальше; не 500 и не обрыв (тест в задаче 7).
2. Несколько вызовов в одном круге — исполняются по порядку, каждому — свой `role: tool` с его
   `tool_call_id` (задача 7).
3. Адрес YouTube с хвостом (`&t=42s`, `?si=`, `youtu.be/ID?si=…`), хост `M.YouTube.com` — принимается;
   `youtube.com.evil.test`, `http://10.77.1.1/watch?v=…`, `youtube.com/watch?v=` с id не 11 знаков,
   плейлист без `v` — отказ без yt-dlp (задача 5).
4. Субтитры или страница не помещаются в остаток контекста — обрезка с пометкой, а не 400 модели
   (задача 7).
5. Диалог с «Интернет» переключили на прямое подключение — `tools` нет, кнопка скрыта; обратно — состояние
   кнопки прежнее (задача 6).

---

### Task 1: Живая проверка вызова инструментов gemma-abl через шлюз (без кода)

Загрузит gemma-abl на карту srvllm (вытеснит `llm` минуты на 4); владелец согласовал спеку с этим шагом.

**Files:** Modify `docs/superpowers/specs/2026-10-21-studio-chat-internet-design.md` (раздел 8 — итог).

- [ ] Скрипт во временной папке (не в репозиторий), ключ — `GATEWAY_KEY_OWNER_IMAGES` из vault через
  окружение, не в argv и не в вывод. `POST http://10.77.1.15:4000/v1/chat/completions`, `model:
  local-gemma-abl`, `stream: true`, сообщения `[{"role":"system","content":"Сегодня …"},{"role":"user",
  "content":"Найди в интернете последнюю версию Python и ответь одной строкой"}]`, `tools` — описание
  `web_search(query)` из задачи 4, `tool_choice: auto`. Записать: приходят ли `delta.tool_calls` кусками
  (`index`, `id`, `function.name`, `function.arguments`), `finish_reason`.
- [ ] Второй запрос: прежние сообщения + `{"role":"assistant","content":null,"tool_calls":[…]}` +
  `{"role":"tool","tool_call_id":…,"content":"1. Python 3.14.1 …"}` — модель отвечает текстом потоком?
- [ ] Не пришли `tool_calls` потоком — тот же запрос с `stream: false`; шаблон не принял `role: tool` —
  результат текстом в сообщении пользователя `<tool_result name="web_search">…</tool_result>`; не принял
  `system` — дата первой строкой сообщения пользователя. Модель не зовёт инструмент ни в одном виде —
  стоп, вернуться к владельцу.
- [ ] Отпустить карту: `POST http://10.77.1.15:4000/slot/images/descriptions/release` `{"model":
  "gemma4-abl"}` тем же ключом.
- [ ] Итог — абзацем «Проверено 2026-10-xx: …» в раздел 8 спеки (какие обходы нужны: `STREAM_TOOLS`,
  `TOOL_ROLE`, `DATE_ROLE` — да/нет); коммит.

Задачи 3 и 7 пишутся по этому итогу: по умолчанию — поток, `role: tool`, `system`.

### Task 2: Схема 14 и ключ Exa в учётке

**Files:** Modify `app/db.py`, `app/profile.py`, `app/manage.py`; Create `app/web.py` (только ключ);
Test `tests/test_foundation.py`, `tests/test_profile_admin.py`, `tests/test_backup_check.py`.

**Interfaces — Produces:**
- `db.VERSION = 14`, `db.INTERNET_14` (SQL ниже), в `SCHEMA` и `STEPS[14]`:
  `ALTER TABLE chats ADD COLUMN web INTEGER NOT NULL DEFAULT 0; ALTER TABLE chats ADD COLUMN web_consent REAL;
  ALTER TABLE users ADD COLUMN exa_key BLOB; ALTER TABLE messages ADD COLUMN tools TEXT;`
- `web.ToolError(Exception)` — `args[0]` — слова для человека.
- `web.has_exa_key(user_id: int) -> bool`; `web.exa_key(user_id: int) -> str | None` (нет — None;
  не расшифровался — `ToolError("ключ Exa не читается — введите его заново в Настройках")`).
- `PUT /api/me/exa-key` `{"key": str}` (1…200 знаков после `strip`, иначе 422 `exa_key`) → 204;
  `DELETE /api/me/exa-key` → 204; `GET /api/me` — `"exa_key": bool`.
- `verify_backup` — `out["exa_keys"] = {"ok": n, "failed": n}`; не расшифровались — в `problems`:
  `ключи Exa не расшифрованы: N из M — STUDIO_DATA_KEY не тот, что у копии`.

- [ ] Тесты: `DROP_V14 = "ALTER TABLE chats DROP COLUMN web; ALTER TABLE chats DROP COLUMN web_consent;
  ALTER TABLE users DROP COLUMN exa_key; ALTER TABLE messages DROP COLUMN tools;"` — первым во всех цепочках
  `test_migrate_*`; версия 14; `test_migrate_adds_the_internet_columns_to_a_version_13_base` (строки
  диалога и учётки целы, `web = 0`, `exa_key` NULL). `test_exa_key_is_stored_encrypted_and_never_returned`:
  PUT → в базе не открытый текст, `security.decrypt` даёт ключ; `GET /api/me` — `exa_key: True`, ключа в
  ответе нет; DELETE → `False`; пустой и 201 знак — 422; ключ не в `caplog.text`; чужая учётка своего
  `exa_key` не видит. `test_exa_key_survives_the_history_wipe` (стирание истории ключ не трогает).
  `verify-backup` с ключом, зашифрованным другим Fernet, — `exa_keys.failed == 1` и строка в `problems`.
- [ ] Реализация; прогон `tests/test_foundation.py tests/test_profile_admin.py tests/test_backup_check.py`;
  коммит.

### Task 3: Дата в каждом запросе чата

**Files:** Modify `app/chat.py`; Test `tests/test_chat.py`, `tests/test_ui_browser.py` (сверки сообщений).

**Interfaces — Produces:**
- `chat.MSK = timezone(timedelta(hours=3))`; `chat.today_line(now: float) -> str` — `«Сегодня <день недели>,
  <число> <месяц в родительном> <год> года (Москва).»` (дни — `понедельник…воскресенье`, месяцы —
  `января…декабря`);
- `chat.with_date(messages: list[dict]) -> list[dict]` — `[{"role": "system", "content":
  today_line(clock.NOW())}, *messages]` (или первой строкой первого сообщения пользователя, если задача 1
  сказала `DATE_ROLE = нет`).
- В `create_turn` — после `fill_photos`, для обоих путей: браузер получает `messages` уже с датой. `compose`
  и оценка токенов — без даты.

- [ ] Тесты: `today_line(1_800_000_000) == "Сегодня пятница, 15 января 2027 года (Москва)."`; граница суток —
  `today_line(<2026-10-21T21:00:00Z>) == "Сегодня четверг, 22 октября 2026 года (Москва)."`;
  `test_every_chat_request_starts_with_today` — через сервер `chat_requests[0]["messages"][0] ==
  {"role": "system", "content": chat.today_line(T0)}`, прямое — то же в `body["messages"][0]`; повтор —
  тоже; описание фото (`gw.descriptions`) — без даты.
- [ ] Прежние сверки сообщений сдвигаются на одно: `test_chat.py` 86, 294, 336, 340, 351, 367, 380, 402,
  478, 491, 784 — сравнивать `[1:]` или индекс +1; браузерные `test_ui_browser.py` 257, 933, 1041, 1363 —
  так же. Прогон `tests/test_chat.py -n0` и браузерных сценариев чата (`-m integration -k chat`); коммит.

### Task 4: `web.py` — Exa: поиск и страница

**Files:** Modify `app/web.py`, `tests/fakes.py`; Create `tests/test_web.py`.

**Interfaces — Produces:**
- Константы: `EXA_URL = "https://api.exa.ai"`, `EXA_TIMEOUT_SEC`, `SEARCH_RESULTS`, `PAGE_MAX` (Global
  Constraints).
- Шаг — `dict`: поиск `{"kind": "search", "query", "results": [{"title", "url", "published"}], "text"}`;
  страница `{"kind": "page", "url", "title", "text"}`; ошибка — те же поля ввода + `"error": str`,
  `"text": ""`. `text` — ровно то, что получит модель.
- `async web.search(key: str, query: str) -> dict` — `POST {EXA_URL}/search` `{"query", "type": "auto",
  "numResults": SEARCH_RESULTS, "contents": {"highlights": true}}` через `state.HTTP`, `timeout=EXA_TIMEOUT_SEC`;
  `text` — нумерованный список: заголовок, адрес, дата, выдержки.
- `async web.page(key: str, url: str) -> dict` — только `http(s)`, иначе `ToolError("адрес страницы — только
  http или https")`; `POST {EXA_URL}/contents` `{"urls": [url], "text": {"maxCharacters": PAGE_MAX}}`;
  `statuses[0].status == "error"` → `ToolError` со словами по `error.tag` (`CRAWL_NOT_FOUND` — «страница не
  найдена», прочее — «страница не прочиталась (<tag>)»); `text` — заголовок, адрес, пустая строка, текст.
- `web._exa_error(r: httpx.Response) -> str`: не JSON (HTML Cloudflare) — «Exa недоступен с сервера
  студии»; 401 — «ключ Exa не подходит — проверьте в Настройках»; 402 — «Exa: бесплатный баланс исчерпан,
  обновится 1-го числа»; 429 — «Exa: слишком часто — попробуйте позже»; прочее — «Exa ответил <код>».
  Таймаут — «Exa не ответил за 30 с», сеть — «Exa недоступен: <тип ошибки>». Коды 402/429 сверить с
  документацией Exa (Context7 `exa`), не по памяти; расхождение — поправить слова и тест.
- `fakes.FakeGateway`: запросы на хост `api.exa.ai` — в `_exa`: `exa_requests: list[tuple[str, dict, str]]`
  (путь, тело, `x-api-key`), `exa_replies: dict[str, tuple[int, object]]` (по пути; по умолчанию — образцы
  ответов Exa с `results`/`statuses`), `exa_html = False` (True — 403 `text/html` «Sorry, you have been
  blocked»). `TOKEN`-проверка шлюза к ним не применяется.

- [ ] Тесты (`tests/test_web.py`, фикстура `studio` — ради `state.HTTP`): в Exa уходит только `query` и
  ключ заголовком; `text` поиска содержит заголовки, адреса и выдержки и не больше `SEARCH_RESULTS`; страница
  `ftp://…` — отказ без запроса; `statuses` с ошибкой — слова; каждая строка `_exa_error` (401, 402, 429,
  500, HTML 403); страница длиннее `PAGE_MAX` не приходит (предел — в запросе).
- [ ] Реализация; прогон `tests/test_web.py`; коммит.

### Task 5: `web.py` — YouTube через yt-dlp; проверка в `install.sh`

**Files:** Modify `app/web.py`, `deploy/install.sh`; Test `tests/test_web.py`, `tests/test_deploy.py`.

**Interfaces — Produces:**
- `web.YTDLP = "yt-dlp"`, `YTDLP_TIMEOUT_SEC`, `SUBS_MAX`, `PARAGRAPH_SEC`.
- `web.RUN: Callable[[list[str], float], Awaitable[tuple[int, bytes, bytes]]]` — по умолчанию
  `asyncio.create_subprocess_exec` (stdin `DEVNULL`, stdout/stderr `PIPE`), срок — `wait_for`, при
  `BaseException` — `proc.kill()` и `await proc.wait()` (как `audio._run`); `OSError` запуска →
  `ToolError("чтение YouTube на сервере недоступно")`. Тесты подменяют `web.RUN`.
- `web.youtube_id(url: str) -> str | None` — хосты (без учёта регистра) `youtube.com`, `www.youtube.com`,
  `m.youtube.com`, `music.youtube.com`, `youtu.be`; схема `http(s)`; формы `/watch?v=ID`, `youtu.be/ID`,
  `/shorts/ID`, `/live/ID`, `/embed/ID`; хвост запроса игнорируется; иначе `None`.
- `web.pick_track(info: dict) -> tuple[str, bool] | None` — `(ключ дорожки, автоматическая)`:

```python
lang = info.get("language")
subs = {k: v for k, v in (info.get("subtitles") or {}).items() if k != "live_chat"}
auto = info.get("automatic_captions") or {}
if lang and lang in subs: return lang, False
if lang and f"{lang}-orig" in auto: return f"{lang}-orig", True
if lang and lang in auto: return lang, True
orig = [k for k in auto if k.endswith("-orig")]
if orig: return orig[0], True
if subs: return next(iter(subs)), False
return None
```

- `web.vtt_text(vtt: str) -> str` — без заголовка `WEBVTT`/`Kind:`/`Language:`, строк времени, тегов
  `<…>`; строка, равная последней выведенной, пропускается («бегущие» автосубтитры); новый абзац с
  отметкой `[м:сс]` (или `[ч:мм:сс]`), когда начало реплики ≥ начала абзаца + `PARAGRAPH_SEC`.
- `async web.youtube(url: str) -> dict` → `{"kind": "youtube", "id", "url" (канонический), "title",
  "channel", "duration", "lang", "text"}`. Два вызова `RUN`: `[YTDLP, "--ignore-config", "--no-playlist",
  "--no-cache-dir", "-J", canon]` → `pick_track`; затем `[… , "--skip-download", "--write-subs" |
  "--write-auto-subs", "--sub-langs", key, "--sub-format", "vtt", "-o", f"{tmp}/{uuid}.%(ext)s", canon]`;
  файл `{uuid}.{key}.vtt` → `vtt_text` → не длиннее `SUBS_MAX` (длиннее — обрезка и пометка
  `[субтитры обрезаны]`). Ошибки словами: stderr с `HTTP Error 429` — «YouTube временно ограничил запросы —
  попробуйте позже»; `Private video`, `Sign in`, `unavailable` — «ролик закрыт или недоступен»; дорожки
  нет — «у ролика нет субтитров»; срок — «YouTube не ответил за 60 с»; прочий код ≠ 0 — «субтитры не
  получены». `text` — `«<название> — <канал>, <м:сс>, язык <lang>\n<адрес>\n\n<текст>»`.
- `install.sh`: `command -v yt-dlp >/dev/null || echo "ВНИМАНИЕ: нет yt-dlp — чтение YouTube в чате
  недоступно" >&2` (выкладку не останавливает).

- [ ] Тесты: `youtube_id` — все формы и Review Focus 3; адрес не YouTube — `RUN` не вызван;
  `pick_track` — свои русские, `ru-orig`, только `en-orig` при `language: None`, только `live_chat` → None;
  `vtt_text` на образце автосубтитров (взять начало настоящего VTT из спеки/пробы: `Давайте<00:00:01.360><c>
  начнём,</c>…` с повтором строки) — без повторов и тегов, отметки абзацев; `youtube()` с поддельным `RUN`
  — argv ровно как выше (канонический адрес, `--ignore-config`), временные файлы удалены и при ошибке; 429,
  закрытый ролик, `OSError`, срок — слова; stderr не в `caplog.text`. `test_deploy`:
  `test_install_warns_without_yt_dlp` — строка `command -v yt-dlp` есть, `exit` рядом с ней нет.
- [ ] Живая сверка `pick_track` на debian (`ssh … deploy@10.77.1.10`, вывод — во `/tmp`, удалить после):
  `yt-dlp --ignore-config --no-playlist --no-cache-dir -J <адрес>` для четырёх роликов — русский с
  автосубтитрами, английский со своими субтитрами, без субтитров, закрытый/с возрастом; ключи `language`,
  `subtitles`, `automatic_captions` дают ожидаемую дорожку (иначе — поправить `pick_track` и тест). Подряд
  не больше трёх загрузок дорожек: YouTube отвечал 429.
- [ ] Реализация; прогон `tests/test_web.py tests/test_deploy.py`; коммит.

### Task 6: Кнопка диалога и описания инструментов

**Files:** Modify `app/chat.py`, `app/web.py`; Test `tests/test_chat_web.py` (новый).

**Interfaces — Consumes:** `web.has_exa_key` (задача 2). **Produces:**
- `web.KINDS = ("search", "page", "youtube")`; `web.NAMES = {"web_search": "search", "read_page": "page",
  "youtube_subtitles": "youtube"}`.
- `web.tools_for(conn: Connection | None, has_key: bool) -> list[str]` — `[]` без подключения или у прямого;
  без ключа — `["youtube"]`; с ключом — `["search", "page", "youtube"]`.
- `web.specs(kinds: list[str]) -> list[dict]` — OpenAI `tools` (`type: function`; параметры: `query`,
  `url`, `url`); описания велят звать только при нужде в свежих или внешних данных или по просьбе
  пользователя.
- `ChatPatch.web: bool | None`; `_chat_view` — ещё `"web": bool`, `"web_tools": list[str]`.

- [ ] Тесты: `PATCH {"web": true}` → `GET` диалога `web: True`, `web_tools` без ключа `["youtube"]`, с
  ключом — три; чужой диалог — 404; прямое подключение — `web_tools: []`, а `web` хранится; смена подключения
  туда и обратно не сбрасывает `web` (Review Focus 5); `specs(["youtube"])` — один инструмент
  `youtube_subtitles`.
- [ ] Реализация; прогон `tests/test_chat_web.py tests/test_chat.py`; коммит.

### Task 7: Цикл инструментов в ответе через сервер

**Files:** Modify `app/chat.py`, `app/web.py`, `tests/fakes.py`; Test `tests/test_chat_web.py`.

**Interfaces — Consumes:** задачи 3–6. **Produces:**
- `web.call(name: str, arguments: str, key: str | None) -> dict` (async): разбор JSON, неизвестное имя или
  битые аргументы — шаг `{"kind": "unknown", "error": …}`; `ToolError` → шаг с `error`; не бросает
  `ToolError`. `search`/`page` с `key is None` — ошибка «ключ Exa не читается — введите его заново в
  Настройках».
- `web.for_model(step: dict) -> str` — `step["text"]` или `«Ошибка инструмента: <error>»`;
  `web.public(step: dict) -> dict` — шаг без `text`; `web.label(kind: str, args: dict) -> str` — запрос,
  адрес или `субтитры <адрес>`, до 200 знаков.
- `chat.WEB_NOTE` — строка, которая дописывается к системному сообщению даты, пока в запросе есть
  `tools` (задача 1: без неё gemma-abl на «найди в интернете» отвечает по памяти): «У тебя есть доступ к
  интернету через инструменты: <перечень по `kinds`>. Если пользователь просит найти что-то в интернете,
  прочитать ссылку или ролик YouTube или нужны свежие данные — вызови инструмент, а не отвечай по памяти.»
  Последний круг без `tools` — без неё.
- `chat.TOOL_ROUNDS = 5`; `chat.CUT_CONTEXT = "\n[обрезано: не помещается в контекст модели]"`;
  `chat.NO_TOOLS = "эта модель не умеет пользоваться интернетом — выключите «Интернет» в этом диалоге"`.
- `chat.ToolCall` — `@dataclass`: `id: str`, `name: str`, `arguments: str` (склейка кусков).
- `StreamTask` — новые поля `tools: list[str] = field(default_factory=list)`, `exa_key: str | None = None`,
  `limit: int | None = None`, `steps: list[dict] = field(default_factory=list)`;
  `async _round(payload: dict, offer: bool) -> tuple[str | None, str | None, list[ToolCall], str]` —
  (слова ошибки, `finish_reason`, вызовы по `index`, текст круга). `create_turn`: `kinds = web.tools_for(conn, web.has_exa_key(uid)) if row["web"] else []`;
  ключ — `web.exa_key(uid)` до транзакции (`ToolError` → `None`).
- Событие SSE `tool` — `{"kind", "label"}` (в `_events` рядом с `delta`); `_message_out` — `"tools":
  [web.public(s) …]` (из `live.steps` у идущего, иначе из `messages.tools`).
- `fakes.FakeGateway.chat_script: list` — ответ на очередной потоковый запрос: `list[str]` (куски текста)
  или `{"text": [...], "tool_calls": [(name, arguments_json), …]}`; пусто — прежний `chat_reply`. Вызовы
  идут кусками: имя с `id` и `index`, затем `arguments` двумя частями; конец — `finish_reason:
  "tool_calls"`. `chat_requests` хранит все тела.

Цикл в `_read` (сообщения держатся до конца, срок `deadline` — один на весь ответ):

```python
rounds = 0
while True:
    offer = bool(self.tools) and rounds < TOOL_ROUNDS
    payload = {"model": ..., "messages": self.messages, "stream": True}
    if offer: payload |= {"tools": web.specs(self.tools), "tool_choice": "auto"}
    error, ending, calls, text = await self._round(payload, offer)   # 400 при offer → NO_TOOLS
    if error is not None: return error
    if not (offer and ending == "tool_calls" and calls): break      # дальше — прежняя проверка ending
    rounds += 1
    self.messages.append({"role": "assistant", "content": text or None, "tool_calls": [...]})
    for c in calls:                                                  # по index
        self.put("tool", {"kind": web.NAMES.get(c.name, "unknown"), "label": ...})
        step = await web.call(c.name, c.arguments, self.exa_key)
        self.steps.append(step); UPDATE messages SET tools = json(self.steps)
        log.info("чат %s: ответ %s — шаг %s: %s", ..., kind, "ok" | "ошибка")
        self.messages.append({"role": "tool", "tool_call_id": c.id, "content": self._fit(web.for_model(step))})
```

`_round` читает поток как прежний `_read` (дельты текста — в `_delta`, видимы пользователю) и копит
`delta.tool_calls` по `index`: `id`, `function.name` — из первого куска, `function.arguments` — склейкой.
`_fit(text)`: при `limit` — если `estimate_tokens(messages) + ceil(len(text)/3) > limit - RESERVE_TOKENS`,
обрезать до остатка с `CUT_CONTEXT`; остатка меньше 500 знаков — `«Ошибка инструмента: не помещается в
контекст модели»`. Обходы задачи 1 (без потока, результат текстом) — здесь же, флагами
`STREAM_TOOLS`/`TOOL_ROLE` модуля.

- [ ] Тесты (`chat_script` + Exa-подделка задачи 4 + `web.RUN`):
  `test_web_note_goes_only_with_tools` (системное сообщение с `WEB_NOTE` — в запросах с `tools`, без
  них — только дата);
  `test_web_off_sends_no_tools_and_calls_nothing` (анти-цель 1: `tools` нет ни в одном запросе, `exa_requests`
  пусто, `RUN` не вызван — даже если в тексте ссылка YouTube); `test_model_searches_and_answers` (второй
  запрос несёт `tool_calls` и `role: tool` с текстом поиска; в Exa — только `query`; ответ `done`;
  `messages.tools` — один шаг; SSE: `turn`, `tool`, `delta`…, `done`); `test_answer_without_search_when_not_needed`;
  Review Focus 1 и 2; `test_rounds_are_capped` (модель зовёт всегда → `TOOL_ROUNDS` кругов, последний запрос
  без `tools`); `test_tool_result_is_cut_to_the_context` (Review Focus 4: малый `context_length` модели);
  `test_model_without_tools_answers_in_words` (400 на запрос с `tools` → ошибка `NO_TOOLS`);
  `test_stop_during_a_tool_keeps_the_done_steps` (подделка `RUN` ждёт; «Стоп» → `stopped`, процесс убит,
  выполненные шаги в `messages.tools`); `test_web_never_reaches_the_log` (запрос, адрес, название, ключ,
  текст страницы — не в `caplog.text`); без ключа — в `tools` только `youtube_subtitles`.
- [ ] Реализация; прогон `tests/test_chat_web.py tests/test_chat.py`; коммит.

### Task 8: Найденное в следующих ходах и в выгрузке

**Files:** Modify `app/chat.py`, `app/web.py`, `app/takeout.py`; Test `tests/test_chat_web.py`, `tests/test_takeout.py`.

**Interfaces — Produces:** `web.history_block(steps: list[dict]) -> str` — для каждого шага
`<web kind="<kind>">\n<for_model(step)>\n</web>\n\n`. `compose`: у ответа ассистента (`done`/`stopped`) с
`tools` блок встаёт в начало предыдущего сообщения пользователя (строка — префиксом; части — в первую
текстовую часть). Выгрузка: сообщение `chats/<id>.json` — ещё `"tools": [...]` (шаги целиком, с `text`), у
сообщений без шагов — `[]`.

- [ ] Тесты: после хода с поиском следующий ход — в сообщении пользователя прежнего хода блок `<web
  kind="search">` с текстом поиска, ролей `tool` в истории нет, Exa не вызван снова; не помещается — 413
  `context` (прежний); ответ `error` со шагами — блоков нет; выгрузка — `tools` у ответа, ключа Exa в архиве
  нет (поиск по байтам ZIP); поиск по истории (`POST /api/search`) по слову, которое есть только в
  найденном (выдержке Exa), диалог не находит — триггеры смотрят `content`, а не `tools`.
- [ ] Реализация; прогон `tests/test_chat_web.py tests/test_takeout.py`; коммит.

### Task 9: Подтверждение «расшифровка → Exa»

**Files:** Modify `app/chat.py`; Test `tests/test_chat_web.py`.

**Interfaces — Produces:** `TurnIn.web_ok: bool = False`; `chat._needs_web_consent(chat_id: str,
consent: float | None) -> bool` — есть сообщение с вложением `source_transcript_id`, `created` которого
больше `consent` (или `consent` NULL); в `create_turn` — в транзакции после проверки `external`, только при
`"search" in kinds`: без `web_ok` → 409 `{"code": "external_web", "message": "Запросы, составленные из
расшифровки, могут уйти в Exa — подтвердите"}`; с `web_ok` — `UPDATE chats SET web_consent = now`.
`_chat_view` — ещё `"external_web": bool`.

- [ ] Тесты: диалог с расшифровкой, `web` вкл., ключ есть → 409, в базе ничего, Exa не вызван; с `web_ok` —
  ход идёт, `external_web: False`; через `clock` позже вложена новая расшифровка → снова 409; без ключа
  (только YouTube) — без 409; `web` выкл. — без 409; облачная модель + расшифровка + Exa — сначала 409
  `external`, затем `external_web`; повтор (`regenerate`) — та же проверка.
- [ ] Реализация; прогон `tests/test_chat_web.py tests/test_chat.py`; коммит.

### Task 10: Страница: кнопка, ход инструментов, источники, ключ в Настройках

**Files:** Modify `app/static/js/chat.js`, `app/static/js/settings.js`, `app/static/css/app.css`;
Test `tests/test_ui_browser.py`.

**Interfaces — Consumes:** `chat.web`, `web_tools`, `external_web` диалога; SSE `tool`; `messages[].tools`;
`me.exa_key`; `PUT/DELETE /api/me/exa-key`.

- [ ] Чат: переключатель `#web-toggle` (кнопка `aria-pressed`, подпись «Интернет») рядом со скрепкой; скрыт,
  если `web_tools` пуст; нажатие — `PATCH {web}`; без `search` в `web_tools` — подсказка «поиск и страницы —
  после ключа Exa в Настройках» (`title` и строка под полем). Событие `tool` — строка `msg-meta` «Ищу: …» /
  «Читаю: …» / «Субтитры: …»; после `done` ответа с событиями `tool` — `reload()` диалога. Под ответом с
  `tools` — `<details class="web-steps">` «Источники (N)»: запрос поиска, ссылки результатов, страница,
  ролик с названием; ссылки `target="_blank" rel="noopener noreferrer"`, текст — `textContent`. 409
  `external_web` и `chat.external_web` (или новая расшифровка при включённой кнопке с `search`) — окно
  `#web-dialog` «Запросы, составленные из расшифровки, могут уйти в Exa — отправить?» → повтор с
  `web_ok: true`; после окна `external` — по очереди.
- [ ] Настройки → «Прочее» → блок «Интернет» (`exaBlock`): `#exa-key` (`type=password`,
  `autocomplete=off`), `#exa-save`, `#exa-remove`, `#exa-status` — «Ключ задан» / «Ключа нет»; ссылка
  `https://dashboard.exa.ai/api-keys`; пояснение: бесплатно $10 в месяц, без карты перерасхода нет; поисковые
  запросы и адреса страниц видит Exa.
- [ ] Браузерный тест `test_chat_internet_toggle_search_and_sources` (`live.gw.chat_script` — поиск, затем
  ответ): кнопка выключена → включить → ход → строка «Ищу:», ответ, `details.web-steps` со ссылкой
  `rel="noopener noreferrer"`; перезагрузка — кнопка включена; прямое подключение — кнопки нет.
  `test_settings_exa_key_is_saved_and_never_shown` — после сохранения поле пустое, «Ключ задан».
- [ ] Прогон этих сценариев (`-m integration -k "internet or exa"`); коммит.

### Task 11: Документация и итоговый прогон

**Files:** Modify `README.md`, `CLAUDE.md`.

- [ ] README — раздел «Интернет в чате»: кнопка и когда модель ищет; ключ Exa в Настройках, бесплатный
  тариф ($10/мес, сброс 1-го, без карты); что уходит наружу (в Exa — запросы и адреса, в YouTube — id ролика
  с адреса сервера); подтверждение для расшифровки; прямые подключения — без кнопки.
- [ ] CLAUDE.md: «Устройство» — `web.py` (Exa, yt-dlp, `RUN`, шаги в `messages.tools`, кнопка `chats.web`,
  ключ `users.exa_key`, `web_consent`); «Правила» — исключение api.exa.ai и YouTube из правила списка
  разрешённых адресов, ключ Exa шифром и не в браузер; «Хост» — общий `/usr/local/bin/yt-dlp`, крон root
  01:00 (`pip3 install -U yt-dlp`), api.exa.ai с debian — после маршрута в `network` (FINDINGS 2026-10-21).
- [ ] Быстрый набор целиком: `.venv/Scripts/python -m pytest -q` — зелёный, не дольше прежнего бюджета;
  коммит.
- [ ] Выкладка — только по слову владельца (`bash deploy/deploy.sh`); живой поиск на проде — после маршрута
  Exa в `network`; YouTube и дату — проверить сразу после выкладки.
