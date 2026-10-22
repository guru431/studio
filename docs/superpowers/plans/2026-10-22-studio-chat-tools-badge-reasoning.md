# Плашка «Интернет» и кнопка «Рассуждение» — план

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** над ответом чата видно, ходила ли модель в Exa и чем кончилось; в диалоге есть кнопка «Рассуждение», ход
мысли модели виден, а петля в рассуждении не держит ответ полчаса.

**Architecture:** сервер (`app/chat.py`) читает рассуждение из потока модели (`delta.reasoning_content` /
`delta.reasoning`), шлёт его странице событием SSE `think`, хранит в `messages.reasoning` и обрывает ответ по
пределам; кнопка — `chats.thinking`, параметр `chat_template_kwargs.enable_thinking` уходит только ролям
`local-*` srvllm через сервер. Страница (`app/static/js/chat.js`) рисует плашку из шагов ответа и строку
рассуждения; путь через браузер не меняется.

**Tech Stack:** Python 3.13, FastAPI, SQLite (шаги схемы `db.STEPS`), pytest + xdist; ES-модули без сборки;
браузерные тесты — `tests/test_ui_browser.py` (CDP, маркер `integration`).

**Spec:** [docs/superpowers/specs/2026-10-22-studio-chat-tools-badge-reasoning-design.md](../specs/2026-10-22-studio-chat-tools-badge-reasoning-design.md)

## Global Constraints

- Журнал службы — только события с id: ни текста рассуждения, ни промптов (CLAUDE.md «Правила»).
- Схема — только добавление: шаг 15, откат `install.sh` на код схемы 14 работает с новой базой.
- Кнопка и параметр — только при `sections.is_local(conn, model)` (srvllm, через сервер, роль `local-*`).
- `REASONING_MAX = 60_000`, `STRAY_MAX = 2_000` знаков, на весь ответ (все круги).
- Слова (точно): «эта модель не умеет рассуждать по кнопке», «рассуждение слишком длинное — ответ остановлен»,
  «модель зациклилась — ответ остановлен», «С рассуждением модель реже ищет в интернете», «Думает… N с»,
  «💭 Рассуждала N с», «🌐 Интернет (Exa): …», «▶ YouTube: …».
- Рассуждение на странице — только текстом (`textContent`), без Markdown и HTML.
- Цвета — токены темы студии (светлая и тёмная), сторонние библиотеки — нет, CDN — нет.
- Быстрый набор — без реального времени (`clock.NOW`, `clock.SLEEP`), тест дольше 1 с — чинить.
- Коммит — сообщением из файла (`git commit -F <файл>`) с последней строкой
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; строки `-m` в задачах — первая строка сообщения.

## Review Focus

1. Старые ответы (до шага 15) с шагами, но без рассуждения: плашка есть, `reasoning` — `null`, ничего не падает →
   тест в задаче 2 (`test_old_answer_has_no_reasoning`) и задаче 3 (плашка у ответа из базы).
2. «Стоп» во время рассуждения: ответ `stopped`, рассуждение сохранено → задача 2
   (`test_stop_while_thinking_keeps_the_reasoning`).
3. Перезагрузка страницы посреди рассуждения: `GET` диалога отдаёт рассуждение идущего ответа → задача 2
   (`test_reasoning_of_a_live_answer_is_in_the_chat`).
4. Рассуждение и круги инструментов: в запрос второго круга и следующего хода рассуждение не уходит → задача 2
   (`test_reasoning_never_goes_back_to_the_model`).
5. Рассуждение с разметкой и HTML (`<img onerror>`) показывается текстом → задача 4 (браузерный тест).

---

### Task 1: Кнопка на сервере — схема 15, PATCH, параметр запроса

**Files:**
- Modify: `app/db.py` (`VERSION`, новый `THINKING_15`, `SCHEMA`, `STEPS[15]`)
- Modify: `app/chat.py` (`ChatPatch`, `_chat_view`, `patch_chat`, `create_turn`, `StreamTask`, `_read`)
- Test: `tests/test_foundation.py`, новый `tests/test_chat_reasoning.py`

**Interfaces:**
- Produces: столбцы `chats.thinking INTEGER NOT NULL DEFAULT 0`, `messages.reasoning TEXT`,
  `messages.reasoning_sec REAL`; диалог в API — `thinking: bool`, `thinking_ok: bool`;
  `StreamTask.thinking: bool | None = None` (None — модель без кнопки: параметра нет; True/False — значение
  `enable_thinking`); `chat.NO_THINKING`.

- [ ] **Step 1: тесты схемы.** В `tests/test_foundation.py`: `DROP_V15 = "ALTER TABLE chats DROP COLUMN thinking;
  ALTER TABLE messages DROP COLUMN reasoning; ALTER TABLE messages DROP COLUMN reasoning_sec;"` — первым во всех
  цепочках `DROP_V14 …`; `db.VERSION == 15` вместо `== 14`; тест шага 15 по образцу шага 14 (строки чата и
  сообщения остаются, новые столбцы — `0`, `None`, `None`) и `test_previous_code_still_works_on_a_version_15_base`
  по образцу `…_version_14_base` (вставка без новых столбцов, `SELECT *` даёт `thinking == 0`, `reasoning is None`).

- [ ] **Step 2: тесты кнопки** в `tests/test_chat_reasoning.py` (хелперы — из `test_chat`, `test_chat_web`):

```python
def test_switch_is_stored_only_for_a_local_model(studio):
    gw = add_gateway(studio); cid = new_chat(studio)                      # local-gemma-abl
    assert (chat_of(studio, cid)["thinking"], chat_of(studio, cid)["thinking_ok"]) == (False, True)
    assert patch(studio, cid, thinking=True)["thinking"] is True
    assert patch(studio, cid, thinking=None)["thinking"] is True
    studio.c.put(f"/api/sections/chat/links/{gw}", json={"models": ["local-gemma-abl", "gemma4-abl"]})
    view = patch(studio, cid, model="gemma4-abl")                          # облачная роль: хранится, не действует
    assert (view["thinking"], view["thinking_ok"]) == (True, False)
    other = new_chat(studio, connection_id=gw, model="gemma4-abl")
    r = studio.c.patch(f"/api/chats/{other}", json={"thinking": True, "title": "x"})
    assert r.status_code == 409 and r.json()["detail"] == {"code": "no_thinking",
                                                           "message": "эта модель не умеет рассуждать по кнопке"}
    assert chat_of(studio, other)["title"] is None
    assert patch(studio, other, thinking=False)["thinking"] is False      # выключить можно всегда

def test_request_carries_enable_thinking_only_for_a_local_model(studio):
    gw = add_gateway(studio); cid = new_chat(studio)
    sse(turn(studio, cid)); patch(studio, cid, thinking=True); sse(turn(studio, cid, "ещё"))
    assert [r["chat_template_kwargs"] for r in studio.gw.chat_requests] == [
        {"enable_thinking": False}, {"enable_thinking": True}]
    studio.c.put(f"/api/sections/chat/links/{gw}", json={"models": ["local-gemma-abl", "gemma4-abl"]})
    patch(studio, cid, model="gemma4-abl"); sse(turn(studio, cid, "и ещё"))
    assert "chat_template_kwargs" not in studio.gw.chat_requests[-1]
    direct = new_chat(studio, connection_id=add_connection(studio))       # прямое: тело собирает браузер
    assert "chat_template_kwargs" not in turn(studio, direct).json()
```

- [ ] **Step 3: Run** `.venv/Scripts/python -m pytest -q -n0 tests/test_chat_reasoning.py tests/test_foundation.py`
  — FAIL (нет столбцов, нет поля `thinking`).

- [ ] **Step 4: реализация.** `db.THINKING_15` (три `ALTER TABLE` выше, с комментарием по образцу `INTERNET_14`),
  `VERSION = 15`, `SCHEMA += THINKING_15`, `STEPS[15]`. `ChatPatch.thinking: bool | None = None`; в `patch_chat` —
  после смены пары, по итоговой паре строки: `thinking=True` при `not sections.is_local(...)` → 409 `no_thinking`
  (`NO_THINKING = "эта модель не умеет рассуждать по кнопке"`), ничего не меняя. `_chat_view`: `thinking =
  bool(row["thinking"])`, `thinking_ok = out["local"]`. `create_turn`: `thinking` читается тем же `fresh`
  (`SELECT web, thinking …`), в `StreamTask(thinking=bool(fresh["thinking"]) if sections.is_local(conn, model)
  else None)`. `_read`: `if self.thinking is not None: payload["chat_template_kwargs"] = {"enable_thinking":
  self.thinking}` — в каждом круге.

- [ ] **Step 5: Run** те же тесты и `tests/test_chat.py tests/test_chat_web.py` — PASS.

- [ ] **Step 6: Commit** `git commit -m "Chat: per-chat reasoning switch for local srvllm models (schema 15)"`.

### Task 2: Рассуждение в потоке — режимы, событие `think`, хранение, пределы

**Files:**
- Modify: `tests/fakes.py` (`_chat`: шаг-словарь с `"reasoning": [...]`, поле `reasoning_field`, `think_hold`)
- Modify: `app/chat.py` (`StreamTask`, `_events`, `_finish`, `_cancel`, `run`, `_message_out`), `app/takeout.py`
- Modify: `CLAUDE.md` («Устройство» → `chat`: кнопка, режимы, пределы), `FINDINGS.md` (удалить запись
  «Зациклившаяся модель держит ответ до 30 минут и не видна» — решена)
- Test: `tests/test_chat_reasoning.py`, `tests/test_takeout.py`

**Interfaces:**
- Consumes: `StreamTask.thinking` (задача 1).
- Produces: событие SSE `think` — `{"text": str}` (режим «видно») или `{}` (режим «думает»); сообщение в API —
  `reasoning: str | None`, `reasoning_sec: float | None`; `chat.REASONING_MAX`, `chat.STRAY_MAX`,
  `chat.THINK_LONG`, `chat.LOOPED`; `_finish(message_id, chat_id, content, status, reasoning=None,
  reasoning_sec=None) -> bool`.
- Fakes: `chat_script` шаг `{"reasoning": ["Шаг 1. ", "Шаг 2."], "text": [...], "tool_calls": [...]}` —
  рассуждение идёт до текста дельтами `{gw.reasoning_field: кусок}` (по умолчанию `"reasoning_content"`);
  `gw.think_hold = True` держит поток после первого куска рассуждения, пока не снимут.

- [ ] **Step 1: тесты** в `tests/test_chat_reasoning.py`:
  - `test_reasoning_is_shown_and_stored_when_the_switch_is_on`: кнопка вкл., шаг `{"reasoning": ["Считаю ",
    "17×23."], "text": ["410"]}` → события `think` с `{"text": "Считаю "}`, `{"text": "17×23."}` раньше `delta`;
    сообщение: `content == "410"`, `reasoning == "Считаю 17×23."`, `isinstance(reasoning_sec, float)`.
  - `test_reasoning_field_named_reasoning_is_read_too`: `studio.gw.reasoning_field = "reasoning"` → то же.
  - `test_switch_off_shows_only_thinking_and_keeps_nothing`: кнопка выкл., `{"reasoning": ["thought"], "text":
    ["Ок."]}` → есть событие `think` с `{}`, нет `think` с `text`; `reasoning is None`, `reasoning_sec is None`.
  - `test_model_without_switch_shows_its_reasoning`: облачная роль `gemma4-abl` → `think` с текстом, хранится.
  - `test_stray_reasoning_over_the_limit_stops_the_answer`: кнопка выкл., рассуждение `"thought" * 300`
    (2 100 знаков) → событие `error` `{"message": "модель зациклилась — ответ остановлен"}`, статус `error`.
  - `test_reasoning_over_the_limit_is_cut`: кнопка вкл., рассуждение кусками на `REASONING_MAX + 10` →
    `error` «рассуждение слишком длинное — ответ остановлен», `len(reasoning) == REASONING_MAX`.
  - `test_reasoning_never_goes_back_to_the_model`: «Интернет» и кнопка вкл., круг 1 — рассуждение и вызов
    `web_search`, круг 2 — рассуждение и текст; в `chat_requests[1]` и в запросе следующего хода нет ни строки
    рассуждения, ни ключей `reasoning*`; `reasoning` ответа — два рассуждения через `"\n\n"`.
  - `test_reasoning_of_a_live_answer_is_in_the_chat`: `think_hold`, ход в фоне (как живые тесты `chat_hold` в
    `test_chat.py`) → `GET` диалога: идущий ответ с `reasoning == "<первый кусок>"`.
  - `test_stop_while_thinking_keeps_the_reasoning`: `think_hold` → `POST …/stop` → `stopped`, `reasoning`
    сохранён.
  - `test_old_answer_has_no_reasoning`: строка `messages` без новых столбцов (NULL) → `reasoning is None`,
    `reasoning_sec is None`.
  - `test_reasoning_never_reaches_the_log` (по образцу `test_web_never_reaches_the_log`): текст рассуждения не в
    `caplog.text`.
  - В `tests/test_takeout.py` к тесту шагов «Интернета»: сообщение выгрузки несёт `reasoning` и `reasoning_sec`.

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -n0 tests/test_chat_reasoning.py tests/test_takeout.py` — FAIL.

- [ ] **Step 3: fakes.** `_chat`: из шага-словаря `step.get("reasoning", [])` — дельты `{self.reasoning_field: кусок}`
  до текста; после первого — ждать, пока `self.think_hold` (как `chat_hold`). Поля в `__init__` с комментарием.

- [ ] **Step 4: реализация в `StreamTask`.**
  - Поля: `reasoning: str = ""`, `reasoning_sec: float = 0.0`, служебные — начало текущего рассуждения
    (`float | None`), счёт «бродячих» знаков, время последнего `think {}`.
  - `_round`: кусок рассуждения дельты — строка `delta["reasoning_content"]`, иначе `delta["reasoning"]`;
    обработка — метод `_think(piece: str) -> str | None` (слова ошибки или None), ошибка — выход из `_round`, как
    у `ANSWER_MAX`. Режим «видно» (`thinking is not False`): `reasoning += piece` (первый кусок нового круга —
    через `"\n\n"`), `put("think", piece)`, сверх `REASONING_MAX` — обрезать до него, `THINK_LONG`. Режим
    «думает» (`thinking is False`): счёт знаков, `put("think", None)` не чаще раза в `THINK_EVERY_SEC = 1` по
    `clock.NOW`, сверх `STRAY_MAX` — `LOOPED`.
  - `reasoning_sec`: с первого куска рассуждения до первого куска `content` после него или конца круга, сумма
    кругов; только в режиме «видно».
  - Сохранение: периодическая запись `_delta` пишет и `reasoning`, `reasoning_sec` (`NULL`, если рассуждения нет);
    кусок рассуждения тоже двигает её (`SAVE_SEC`). `_finish(…, reasoning, reasoning_sec)` — из `run` и `_cancel`.
  - `_events`: `("think", t)` → `_sse("think", {"text": t} if t is not None else {})`.
  - `_message_out`: `reasoning`/`reasoning_sec` — у живого из `STREAMS` (пустое — `None`), иначе из строки.
  - Журнал: при `THINK_LONG`/`LOOPED` — `log.info("чат %s: ответ %s — рассуждение сверх предела", …)`.

- [ ] **Step 5: takeout.** `SELECT … tools, reasoning, reasoning_sec` и поля в словаре сообщения.

- [ ] **Step 6: Run** `.venv/Scripts/python -m pytest -q -n0 tests/test_chat_reasoning.py tests/test_takeout.py
  tests/test_chat.py tests/test_chat_web.py` — PASS.

- [ ] **Step 7: документы.** CLAUDE.md — в описание `chat` («Устройство») строка про `chats.thinking`, `is_local`,
  режимы показа, `REASONING_MAX`/`STRAY_MAX`, событие `think`; FINDINGS.md — удалить решённую запись.

- [ ] **Step 8: Commit** `git commit -m "Chat: read the model's reasoning, show it, cap it"`.

### Task 3: Плашка «Интернет» над ответом

**Files:**
- Modify: `app/static/js/chat.js` (`messageNode`, `paint`, новые `badgeText(steps)`, `badgeNode`), стили чата
  (файл со стилями `.web-steps` — там же)
- Test: `tests/test_ui_browser.py` (`test_chat_internet_toggle_search_and_sources` и новый)

**Interfaces:**
- Consumes: `m.tools` (шаги без текста), `job.tool` (`{kind, label}`), `toolLine`, `plural` из `ui.js`.
- Produces: `button.web-badge` в `.msg-body` перед `.md`; классы `ok` / `bad` / `busy`.

- [ ] **Step 1: тест** — в `test_chat_internet_toggle_search_and_sources` прогресс читать из `.web-badge`
  (`"🌐 Ищу: python 3.14"`, `.msg-meta` пуста), после ответа — `.web-badge` = `"🌐 Интернет (Exa): 1 поиск"`, класс
  `ok`. Новый `test_chat_internet_badge_counts_and_errors`: шаги поиск (ошибка Exa — `exa_html = True`) →
  `"🌐 Интернет (Exa): 1 поиск — 1 ошибка"`, класс `bad`; нажатие на плашку — `details.web-steps` открыт; ответ
  без шагов — плашки нет; перезагрузка — плашка из базы та же.

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -m integration -n0 -k "internet_toggle or badge_counts"
  tests/test_ui_browser.py` — FAIL.

- [ ] **Step 3: реализация.** `badgeText(steps) -> string`: части по видам — `search`/`page` → «🌐 Интернет (Exa):
  N поиск|поиска|поисков, N страница|страницы|страниц»; `youtube` → «▶ YouTube: N ролик|ролика|роликов»;
  `unknown` → «N неверный вызов|неверных вызова|неверных вызовов»; части через « · »; ошибки → « — N
  ошибка|ошибки|ошибок». Примеры: `[search]` → «🌐 Интернет (Exa): 1 поиск»; `[search, page, youtube✗]` →
  «🌐 Интернет (Exa): 1 поиск, 1 страница · ▶ YouTube: 1 ролик — 1 ошибка». Идёт инструмент (`job.tool`) —
  плашка `busy` с крутилкой: «🌐 » (▶ у `youtube`) + `toolLine(job.tool)`; строка `.msg-meta` в это время пуста.
  Нажатие — открыть `details.web-steps` ответа и прокрутить к нему. Цвета `ok`/`bad` — токены темы.

- [ ] **Step 4: Run** тот же `-k` — PASS.

- [ ] **Step 5: Commit** `git commit -m "Chat page: an internet badge over answers that used tools"`.

### Task 4: Кнопка «Рассуждение» и строка рассуждения на странице

**Files:**
- Modify: `app/static/js/chat.js` (`build`: чип `#think-toggle`, `draftThink`, `thinkOk`, `paintThink`,
  `switchThink`; `readServer`: событие `think`; `messageNode`/`paint`: строка рассуждения), стили чата
- Modify: `README.md` (раздел чата: кнопка «Рассуждение», плашка «Интернет»)
- Test: `tests/test_ui_browser.py`

**Interfaces:**
- Consumes: диалог `thinking`, `thinking_ok`; SSE `think`; сообщение `reasoning`, `reasoning_sec`; плашка задачи 3.
- Produces: `button#think-toggle.chip` (`aria-pressed`); узел `.think` в `.msg-body` после плашки:
  идёт — `div.think.busy` «Думает… N с» (+ `div.think-text` в режиме «видно»), готово — `details.think` с
  `summary` «💭 Рассуждала N с» и `div.think-text`.

- [ ] **Step 1: тест** `test_chat_reasoning_switch_and_thinking_line`: новый диалог `local-gemma-abl` — чип виден,
  не нажат; нажать до первого сообщения (черновик → PATCH после создания); «Интернет» тоже вкл. — `#web-note` =
  «С рассуждением модель реже ищет в интернете»; `gw.think_hold = True`, шаг `{"reasoning": ["Считаю ",
  "<img src=x onerror=alert(1)>"], "text": ["410"]}` → видно «Думает…» и текст первого куска; снять hold → ответ
  «410», `details.think` закрыт, `summary` начинается с «💭 Рассуждала», в `.think-text` — исходная строка с
  `<img…>` текстом, `img` в ответе нет; перезагрузка — то же из базы и чип нажат; модель `gemma4-abl` (облачная) —
  чипа нет.

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -m integration -n0 -k reasoning_switch tests/test_ui_browser.py`
  — FAIL.

- [ ] **Step 3: реализация.** Чип рядом с `#web-toggle` (иконка из набора `icons.js`; подходящей нет — добавить в
  набор по образцу соседних), видим при `thinkOk()`: открытый диалог — `chat.chat.thinking_ok`, новый — чип
  подключения srvllm через сервер и модель `local-*` (как `sections.is_local`). Нажатие — как `switchWeb`
  (черновик `draftThink`, PATCH `{thinking}`, 409 `no_thinking` — словами в `#chat-error`). Подсказка
  «С рассуждением модель реже ищет в интернете» — в `#web-note`, когда `webOn()` и кнопка вкл. (подсказка «нет
  ключа» важнее). `readServer`: `think` → `job.thinking = true`, `job.thinkFrom ??= Date.now()`, текст —
  в `job.message.reasoning`. Строка: пока ответ идёт и после последнего `think` не было `delta` — «Думает…
  N с» (счётчик от `job.thinkFrom`); готовый ответ с `reasoning` — свёрнутый `details` «💭 Рассуждала N с» (`N =
  Math.max(1, Math.round(reasoning_sec))`); текст — только `textContent`.

- [ ] **Step 4: Run** тот же `-k` и `-k "internet"` — PASS.

- [ ] **Step 5: README** — абзац о кнопке и плашке в разделе чата.

- [ ] **Step 6: Commit** `git commit -m "Chat page: reasoning switch and the thinking line"`.

### Task 5: Проверка партии и выкладка

- [ ] **Step 1:** `.venv/Scripts/python -m pytest -q` — быстрый набор зелёный, `--durations=5` без тестов > 1 с.
- [ ] **Step 2:** `.venv/Scripts/python -m pytest -q -m integration -k "chat"` — браузерные тесты чата зелёные.
- [ ] **Step 3:** одно ревью партии (`superpowers:requesting-code-review`); находки не по задаче — в `FINDINGS.md`.
- [ ] **Step 4:** выкладка — по согласию владельца: `bash deploy/deploy.sh`; на проде — ход с кнопкой на
  `local-gemma-abl` («Сколько будет 17×23+19?») и ход с «Интернетом» (плашка, источники).
