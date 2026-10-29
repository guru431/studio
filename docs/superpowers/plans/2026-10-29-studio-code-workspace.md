# Рабочее место «Claude Code» — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Раздел «Код» (`#/code`) — одно окно на все проекты: слева панель компьютеров и проектов, справа вкладки сессий проекта, путь плашкой; рабочее место — в учётке.

**Architecture:** Сервер — новый модуль `app/workspace.py` (две таблицы шага схемы 20, `GET/PATCH /api/code/workspace` действиями, без моста). Клиент — `code.js` остаётся роутером раздела и держит раскладку; панель (`code-panel.js`), список сессий (`code-sessions.js`), вкладки (`code-tabs.js`) и кэш рабочего места (`code-workspace.js`) — новые модули; `code-chat.js` и `code-files.js` почти не меняются. Прокручивается по-прежнему `#view` (логика прилипания ленты в `code-chat.js` не трогается); панель прилипает к нему.

**Tech Stack:** FastAPI, SQLite, pydantic; модули ES без сборки (`app/static/js`), `app/static/css/app.css`; pytest, браузерные сценарии `tests/test_ui_browser.py` (CDP), подставной мост `tests/fake_bridge.py`.

**Spec:** [docs/superpowers/specs/2026-10-29-studio-code-workspace-design.md](../specs/2026-10-29-studio-code-workspace-design.md) (дальше — «спека», § — её разделы). Прежняя спека раздела — `2026-10-28-studio-claude-code-design.md`.

## Global Constraints

- Протокол моста и проект `studio-agent` не меняются; `app/claude_code.py` — только если задача это прямо говорит.
- В рабочем месте на сервере — только id: `aid` (int), `pid` (16 hex), UUID сессии, вид `chat|files|git`. Ни имён компьютеров и проектов, ни названий сессий, ни путей.
- В журнал службы рабочее место не пишет ничего. Текст сервера и машины — только `textContent` / `markdown.js`.
- PIN — только в поле и переменной обработчика: ни в адрес, ни в хранилище браузера, ни в рабочее место.
- Новое в `localStorage` не кладётся (рабочее место — в учётке).
- Пределы: `TABS_MAX = 20` вкладок на проект, `PROJECTS_MAX = 100` проектов с вкладками; опрос меток — `IDLE_POLL_SEC = 20`, не дольше `IDLE_POLL_FOR_SEC = 1800` после действия владельца в разделе; телефон — `max-width: 760px`.
- Слова (дословно): «Компьютеров пока нет.», «Компьютер не на связи — включите его и запустите мост», «Выберите проект слева», «Новый чат», «Сессия», «Чат», «Файлы», «Git», «Закрыть чат».
- Схема — только добавление: `db.VERSION = 20`, шаг `db.STEPS[20]`; откат на код шага 19 работает с новой базой.
- Сохранить id, на которые опираются тесты и `hold()`: `#code-agents li[data-id] button.code-agent` (`.code-agent-name`, `.code-agent-state`, `.code-agent-pin`), `#code-projects li[data-pid] a`, `#code-pin`, `#code-pin-input`, `#code-pin-open`, `#code-pin-error`, `#code-empty`, `#code-chats li[data-chat]`, `#code-new-chat`, `#code-tab-files`, `#code-tab-git`, `#code-chat`, `#code-status`. Плашка пути — `#code-place` (не `#code-path`: он занят папкой во «Файлах»).
- Среда: `.venv/Scripts/python` (в worktree её нет — брать из основного дерева `C:/work/projects/studio/.venv/Scripts/python`); worktree от `origin/main` — сначала `git merge --ff-only main`.
- Быстрый набор: `.venv/Scripts/python -m pytest -q`. Браузер раздела: `.venv/Scripts/python -m pytest -q -m integration tests/test_ui_browser.py -k code -n0`. Перед правкой UI — `grep -n "code-" tests/test_ui_browser.py`: сценарии, что опираются на меняемые элементы, правятся в той же задаче.
- CLAUDE.md (абзац «Claude Code» в «Устройстве», откат) и README («Claude Code») — правятся в задаче, что меняет описанное.

## Review Focus

1. Длинные имена компьютеров и проектов, длинные названия сессий на 320 px — многоточие, страница не шире экрана (`NO_OVERFLOW`); тест — Task 2 (панель, плашка) и Task 3 (вкладки).
2. Много проектов (60) — прокручивается только список проектов, блок компьютеров на месте; тест — Task 2.
3. Восстановление, когда запомненный компьютер не на связи или отозван, — раздел открывается, панель говорит «не на связи», ошибок в консоли нет; тест — Task 2.
4. Быстрые нажатия по вкладкам — на экране сессия последнего нажатия, ответ прежней не перерисовывает; тест — Task 3.
5. Запись рабочего места не прошла (сеть, 5xx) — вкладка всё равно открывается, необработанного отказа нет; тест — Task 3.

---

### Task 1: Рабочее место на сервере (шаг схемы 20)

**Files:**
- Create: `app/workspace.py`, `tests/test_workspace.py`
- Modify: `app/db.py` (`VERSION`, `CODE_20`, `SCHEMA`, `STEPS`), `app/main.py` (роутер), `app/history.py:60` (стирание), `app/takeout.py` (README, `code_chats/workspace.json`), `tests/test_code.py:88` (фильтр маршрутов), `CLAUDE.md`, `README.md`

**Interfaces:**
- Produces:
  - `GET /api/code/workspace` → `Workspace`:
    `{"panel": int|null, "place": {"aid": int, "pid": str, "view": "chat"|"files"|"git"}|null, "projects": [{"aid": int, "pid": str, "tabs": [str, ...], "active": str|null}, ...]}` — `projects` по убыванию `touched`.
  - `PATCH /api/code/workspace` тело `{"op": "open"|"close"|"place"|"panel", "aid": int, "pid": str?, "chat": str?, "view": str?}` → `Workspace`. `open` и `close` — `aid`, `pid`, `chat`; `place` — `aid`, `pid`, `view`; `panel` — `aid`.
  - `workspace.doc(user_id: int) -> dict` (тот же `Workspace`), `workspace.TABS_MAX = 20`, `workspace.PROJECTS_MAX = 100`.

- [ ] **Step 1: Тесты `tests/test_workspace.py`** (фикстура `studio`, `make_user`, `signin` из `conftest`, `enroll`, `connect`, `FakeMachine` из `fake_bridge`; UUID — как `A`/`B` в `test_code.py`)
  - `test_empty_workspace` — GET → `{"panel": None, "place": None, "projects": []}`.
  - `test_open_appends_activates_and_places` — `open` X, затем Y того же проекта → `tabs == [X, Y]`, `active == Y`, `place == {"aid": aid, "pid": ALPHA, "view": "chat"}`.
  - `test_open_again_activates_without_duplicate` — `open` X, Y, снова X → `tabs == [X, Y]`, `active == X`.
  - `test_twenty_first_tab_pushes_out_the_leftmost` — 20 `open`, затем 21-я → длина 20, первой нет, последняя — новая и активная.
  - `test_close_moves_active_right_then_left` — `[X, Y, Z]`, активна Y: `close` Y → активна Z; `close` Z → активна X; `close` X → проекта нет в `projects`, `place` прежний.
  - `test_close_of_unknown_tab_is_a_no_op` — 200 и документ не изменился.
  - `test_place_and_panel` — `place` (view `files`) и `panel` → в документе; `place` с `view: "x"` → 422.
  - `test_hundred_and_first_project_drops_the_oldest` — 101 проект с `open` (часы `clock.NOW` подменой) → 100, нет самого раннего.
  - `test_someone_elses_or_missing_computer_is_404` — компьютер другой учётки и несуществующий id → 404 `not_found`, в таблицах пусто.
  - `test_bad_pid_and_chat_are_422` — `pid` не 16 hex, `chat` не UUID → 422.
  - `test_workspace_never_asks_the_bridge` — компьютер на связи (`connect`), все четыре действия и GET → `m.requests == []`.
  - `test_revoking_a_computer_takes_its_part` — `DELETE /api/agents/<id>` → его проектов нет, `place` и `panel` — `None`.
  - `test_erase_history_clears_it_and_a_stale_action_does_not_resurrect` — `open` X, Y; `DELETE /api/history` → пусто; `open` Z → `tabs == [Z]`.
  - `test_takeout_carries_the_workspace` — выгрузка раздела `code` (как в `tests/test_takeout.py`) несёт `code_chats/workspace.json`, равный GET.
  - `test_workspace_log_holds_no_ids` — после действий в `caplog` нет ни `pid`, ни UUID.

- [ ] **Step 2: Запустить — падают** (`.venv/Scripts/python -m pytest -q tests/test_workspace.py -n0`; 404 на маршрутах).

- [ ] **Step 3: Схема — `db.CODE_20`, `VERSION = 20`, `STEPS[20] = CODE_20`, `SCHEMA += CODE_20`**

```sql
CREATE TABLE code_tabs(user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       agent_id INTEGER NOT NULL REFERENCES agents ON DELETE CASCADE, project TEXT NOT NULL,
       tabs TEXT NOT NULL DEFAULT '[]', active TEXT, touched REAL NOT NULL, PRIMARY KEY (agent_id, project));
CREATE INDEX code_tabs_user ON code_tabs(user_id, touched);
CREATE TABLE code_place(user_id INTEGER PRIMARY KEY REFERENCES users ON DELETE CASCADE,
       panel_id INTEGER REFERENCES agents ON DELETE SET NULL, agent_id INTEGER REFERENCES agents ON DELETE SET NULL,
       project TEXT, view TEXT NOT NULL DEFAULT 'chat' CHECK (view IN ('chat','files','git')));
```
Комментарий над `CODE_20` — как у `CODE_19`: что это, только id, каскад отзыва.

- [ ] **Step 4: `app/workspace.py`** — `router = APIRouter(prefix="/api/code")`, модель `Action` (pydantic: `op: Literal[...]`, `aid: StrictInt`, `pid`, `chat`, `view: Literal["chat","files","git"] | None`; обязательные поля по `op` — `model_validator`, иначе 422). Проверки — `claude_code.own_agent`, `check_pid`, `check_chat`. Действие — одной транзакцией (`BEGIN IMMEDIATE` … `COMMIT`, откат при исключении), время — `clock.NOW()`; `place`, у которого `agent_id IS NULL`, отдаётся как `null`. Логики сверх §6.4 и §6.8 спеки нет; журнала нет. Подключить в `main.py` до `claude_code.router`.

- [ ] **Step 5: Стирание и выгрузка** — в `history.py` добавить `code_tabs`, `code_place` в перечень таблиц (комментарий: рабочее место — номера сессий, история); в `takeout.py` при `"code" in sections` записать `code_chats/workspace.json` = `workspace.doc(user_id)` после страниц `TABLES`, строка в `README`.

- [ ] **Step 6: `tests/test_code.py`** — в `test_someone_elses_agent_is_404_before_the_bridge` брать маршруты с `/api/code/agents/` (у рабочего места нет компьютера в адресе — его чужой компьютер проверяет `test_workspace.py`).

- [ ] **Step 7: Запустить** — `.venv/Scripts/python -m pytest -q tests/test_workspace.py tests/test_code.py tests/test_db.py tests/test_takeout.py tests/test_history.py -n0` → PASS (имена файлов сверить `Glob tests/test_*.py`).

- [ ] **Step 8: Документы** — CLAUDE.md: в абзаце «Claude Code» — `workspace` (таблицы, действия, без моста, стирание, выгрузка); раздел «Откат на код до шага схемы 20»: прежний код таблиц не читает, его «Удалить всю историю» их не стирает — вернувшись вперёд, стереть снова или `DELETE FROM code_tabs WHERE user_id IN (…); DELETE FROM code_place WHERE user_id IN (…);`. README «Где хранится история»: что стирается и что в выгрузке.

- [ ] **Step 9: Быстрый набор** `.venv/Scripts/python -m pytest -q` → зелёный, затем коммит:

```bash
git add app/workspace.py app/db.py app/main.py app/history.py app/takeout.py tests/test_workspace.py tests/test_code.py CLAUDE.md README.md
git commit -F <файл сообщения>    # «Claude Code: рабочее место учётки — вкладки и место, шаг схемы 20»
```

---

### Task 2: Раскладка раздела и панель компьютеров и проектов

**Files:**
- Create: `app/static/js/code-workspace.js`, `app/static/js/code-panel.js`, `app/static/js/code-sessions.js`
- Modify: `app/static/js/code.js` (раскладка, маршруты, PIN, путь), `app/static/css/app.css` (блок «Claude Code»), `tests/test_ui_browser.py` (сценарии раздела), `CLAUDE.md`, `README.md`

**Interfaces:**
- Consumes: `GET/PATCH /api/code/workspace` (Task 1); прежние `/api/agents`, `…/open`, `…/projects`, `…/chats`, `…/chats/<chat>` (PATCH `state`).
- Produces:
  - `code-workspace.js`: `load(): Promise<Workspace>`; `get(): Workspace` (до загрузки — пустой); `act(op: string, fields: object): void` — PATCH в фоне, ответ заменяет кэш и зовёт подписчиков, отказ проглатывается (`.catch`, без журнала); `tabs(aid, pid): { tabs: string[], active: string|null }`; `count(aid, pid): number`; `subscribe(fn): () => void`; `reset(): void` (уход из раздела).
  - `code-panel.js`: `panelView(host, { selected: number|null, opened(aid): void, chose(): void }) → { select(aid): void, pin(aid): void, here(aid, pid): void, refresh(): void, agents(): Promise<object[]>, unmount(): void }`. `select` — как нажатие на имя (§6.3 спеки; пишет `act('panel')`); `pin` — выбрать и сразу поле PIN (отказ `closed`, `?pin=`); `here` — подсветить открытый проект; `refresh` — числа вкладок из `code-workspace.js`; `agents` — последний список `GET /api/agents` (кэш на время показа раздела); `opened` — компьютер открыт (204 или верный PIN); `chose` — нажат проект (телефон закрывает панель).
  - `code-sessions.js`: `sessionList(host, { aid, pid, items: object[], exclude: Set<string>, picked(chat): void, created(chat): void, failed(err): void }) → { unmount() }` — `#code-new-chat` первой строкой, затем `#code-chats li[data-chat]` (кроме `exclude`) в порядке сервера: `.code-chat-title` (иначе «Новый чат»), `.code-chat-time`, `.tag` — «студия» / «VS Code или терминал», «закрыт», «идёт ход», «ждёт ответа». Нажатие закрытой — `PATCH {state: "open"}`, затем `picked`; «Новый чат» — `POST …/chats`, затем `created(chat)` (`freshChat` — в `code.js`). Кнопок «Закрыть» / «Открыть снова» нет.

- [ ] **Step 1: Переписать сценарии раздела в `tests/test_ui_browser.py` под панель** — помощники `code_open`, `code_chat`, `code_tab` оставить (их селекторы сохранены), в `code_tab` ждать `#code-place .code-place-project` вместо `.code-crumb-here`; `CODE_CHATS` — без кнопки. Новые и изменённые:
  - `test_code_section_lists_computers_and_says_when_there_are_none[320|1280]` — без компьютеров `#code-empty` справа, панели нет; с ними — `CODE_AGENTS` как прежде, у 1280 панель и правая часть рядом, `#view .head` нет; `NO_OVERFLOW`.
  - `test_code_panel_keeps_computers_while_projects_scroll` — 60 проектов, 2 компьютера: прокрутка `#code-projects` до конца — `getBoundingClientRect().top` первой `.code-agent` не изменился, `#view.scrollTop` не изменился.
  - `test_code_computer_click_changes_only_the_panel` — открыт проект A компьютера 1; нажатие на компьютер 2 → его проекты в панели, `location.hash` и `#code-place` прежние.
  - `test_code_pin_pad_counts_down_and_opens` — как прежде, но поле PIN — в `#code-panel`, адрес остаётся `#/code`.
  - `test_code_closed_shows_the_pin_in_the_panel_and_returns` (вместо `…double_closed…` и `…lock_and_unlock…` по смыслу): отказ `closed` на проекте → `#code-pin` в панели (телефон — панель открыта), адрес не менялся, после PIN — тот же проект на экране; запросов `open` — один с PIN.
  - `test_code_restores_the_last_project_after_reload` — открыли проект `beta`, перезагрузка `#/code` → `location.hash == #/code/<aid>/<pid beta>`, `#code-place` = «Рабочий ПК / beta».
  - `test_code_restore_with_an_offline_computer` — место на компьютере, который сейчас не на связи → раздел открыт, в панели «Компьютер не на связи — включите его и запустите мост», `problems(r)` пусто.
  - `test_code_phone_panel_slides_and_hides_after_a_project` — 320 px: нет места — панель открыта сама; выбор проекта — закрыта; `#code-panel-toggle` — открыта; нажатие на фон и Escape — закрыта.
  - `test_code_place_is_one_line` — имя компьютера и проекта по 80 знаков: высота `#code-place` — одна строка, `NO_OVERFLOW` на 320.
  - `test_code_project_lists_sessions_and_reopens_a_closed_one` (вместо `…closes_one`): у проекта без вкладок — `#code-chats` во всю ширину с пометками; нажатие на закрытую — PATCH `open`, затем её чат.
  - Остальные сценарии `test_code_*` — прогнать; падающие из-за строки пути (`.code-crumbs`) переводить на ссылки панели и `#code-place`, не ослабляя того, что они сторожат.

- [ ] **Step 2: Запустить — падают** (`-m integration tests/test_ui_browser.py -k code -n0`).

- [ ] **Step 3: `code-workspace.js`** — по Interfaces; кэш — переменная модуля, подписчики — `Set`.

- [ ] **Step 4: `code-panel.js`** — разметка `aside#code-panel.code-panel` > `div.code-panel-agents` (`#code-agents`, без прокрутки) + `div.code-panel-body` (прокрутка: `#code-projects` | `#code-pin` | `p#code-offline`) + `p#code-panel-status`. Поле PIN, слова отказов (`pin`, `pin_locked`, `offline`) и `attemptsLeft` — перенести из `code.js computers()` как есть. Строка проекта — `a.code-project[href="#/code/<aid>/<pid>"]` с `span.code-project-count` (число из `count()`, ноль — пусто), открытый — `aria-current="page"`; выбранный компьютер — `aria-current="true"` на `.code-agent`; версия Claude Code — `title`.

- [ ] **Step 5: `code-sessions.js`** — по Interfaces; разметка строк и пометок — из нынешнего `chatRow` без кнопки.

- [ ] **Step 6: `code.js`** — `mount` строит раз и держит до `unmount`: `div.code-ws` > панель + `div.code-main` (`div.code-top` с `div.code-head`: `button#code-panel-toggle.icon-btn` (телефон), `span#code-place` — `span.code-place-agent` / `b.code-place-project`, переключатели `#code-tab-chat` «Чат», `#code-tab-files`, `#code-tab-git`; затем `p#code-status` и тело) + `div.code-backdrop`. `update(ctx)` перерисовывает только правую часть. Маршруты — §6.7 спеки: `#/code` → `location.replace` на `place` (вид `files|git` — `?tab=`), нет места — `p#code-pick` «Выберите проект слева» (телефон — панель открыта); `#/code/<aid>` — `panel.select(aid)`, дальше как `#/code`; `?pin=<aid>` — `panel.pin(aid)` (или слова «не на связи»), `?pin` из адреса — `replaceState(history.state, …)`; проект — `act('place')`, `panel.here`, справа список сессий (`sessionList`) или вид `?tab=`; чат — `chatView` как сейчас. Плашка — имя компьютера из `panel.agents()`, имя проекта — из `GET …/projects` компьютера проекта (как нынешний `project()`; панель может показывать другой компьютер). Переключатели — смена адреса заменой (`location.replace`); экземпляр `filesView` живёт, пока открыт тот же проект (адрес меняется, проект — нет: панели не пересоздавать), `gitView` — заново при каждом показе. `failed(err, …)`: `closed` → `returnTo = { aid, hash: location.hash, file }`, `panel.pin(aid)`; `opened(aid)` с `returnTo` того же компьютера → `wantFile = file`, перерисовать правую часть по текущему адресу. Удалить `crumbsNav`, `computers`, `projects` и `redirect` на `?pin=`. `hold()` — без изменений по смыслу. Комментарий в начале файла переписать под новое устройство.

- [ ] **Step 7: CSS** — блок «Claude Code» в `app.css`: `.code-ws` (flex-строка, минимум высоты `#view`); `.code-panel` — ширина ≈ 220 px, `position: sticky; top: 0; height: 100dvh; align-self: flex-start`, колонка: `.code-panel-agents` без прокрутки, `.code-panel-body` — `flex: 1; overflow-y: auto; min-height: 0`; `.code-top` — `position: sticky; top: 0; z-index` выше ленты; `#code-place` — одна строка (`white-space: nowrap`, у частей `overflow: hidden; text-overflow: ellipsis`, имя компьютера сжимается первым — `flex-shrink` больше), моноширинный 12 px на `var(--panel-2)`; ≤ 760 px — панель выезжает, как `.chats` (`.code-ws.panel-open`, `.code-backdrop`, `--tabs-h`, safe-area), `#code-panel-toggle` виден, `.code-head` оставляет справа место под три значка (как `.head`: 142 px, ≤ 360 px — 138 px), переключатели переносятся второй строкой. Удалить стили `.code-crumbs`, `.code-bar`, `.code-tabs`, старых строк компьютеров и проектов-карточек, ставшие ненужными.

- [ ] **Step 8: Запустить сценарии раздела** — `-m integration tests/test_ui_browser.py -k code -n0` → PASS.

- [ ] **Step 9: Документы и коммит** — CLAUDE.md (абзац «Claude Code»: модули `code-panel.js`, `code-sessions.js`, `code-workspace.js`, раскладка, PIN в панели, адреса; «Ловушки»: `#code-path` занят), README «Claude Code» (панель, путь, телефон). Быстрый набор → зелёный; коммит «Claude Code: панель компьютеров и проектов, путь плашкой».

---

### Task 3: Вкладки сессий

**Files:**
- Create: `app/static/js/code-tabs.js`
- Modify: `app/static/js/code.js`, `app/static/js/code-chat.js` («Закрыть чат», 404 истории), `app/static/css/app.css`, `tests/fake_bridge.py` (сессия вне списка), `tests/test_ui_browser.py`, `CLAUDE.md`, `README.md`

**Interfaces:**
- Consumes: `code-workspace.js` (`tabs`, `act`, `subscribe`), `code-sessions.js` (`sessionList`), `chatView`.
- Produces:
  - `code-tabs.js`: `tabBar(host, { aid, pid, pick(chat): void, close(chat): void, created(chat): void, failed(err): void }) → { show({ tabs: string[], active: string|null, info: Map<string, {title, running, asking}>, items: object[] }): void, unmount(): void }`. Разметка: `div#code-tabbar[role=tablist]` > `div.code-tab[data-chat][role=tab][aria-selected]` (`span.code-tab-title` — `title` сессии, нет в `info` — «Сессия», `title` пуст — «Новый чат»; полное — `title`; `span.code-tab-mark.running|.asking`; `button.code-tab-close[aria-label="Закрыть вкладку"]` «×») + `button#code-tab-add[aria-label="Сессии проекта"]` «+», меню `div#code-tab-menu` с `sessionList` (`exclude` — открытые вкладки).
  - `code-chat.js` `chatView(…, { …, closedChat(): void, gone(): void })` — «Закрыть чат» после PATCH зовёт `closedChat()` вместо `navigate(code/<aid>/<pid>)`; отказ 404 `not_found` на чтение истории — `gone()` вместо слов.
  - `tests/fake_bridge.py`: у сессии `FakeMachine` признак `"listed": False` — её нет в ответе `chats` (как у сессии старше 200 новейших на настоящем мосте), а история и ход работают.

- [ ] **Step 1: Сценарии в `tests/test_ui_browser.py`**
  - `test_code_tabs_open_from_the_menu_and_switch` — проект с тремя сессиями: «+» → строка A → вкладка A, адрес чата A; «+» → B → две вкладки, активна B; нажатие A — `#code-chat[data-chat=A]`, история A; «Назад» браузера уходит к прежнему проекту, а не к B (`history.length` не растёт от смены вкладки).
  - `test_code_new_chat_is_a_tab` (вместо `…opens_its_page`) — «+» → «Новый чат» → вкладка «Новый чат» активна, строка `code_chats`.
  - `test_code_tab_close_picks_the_right_neighbour` — `[A, B, C]`, активна B: «×» B → активна C; «×» C → A; «×» A → `#code-chats` во всю ширину; сессии на машине — все три (`close` мосту не уходит).
  - `test_code_close_chat_removes_its_tab` — «Закрыть чат» в B → вкладки B нет, активна соседняя, PATCH `closed` ушёл.
  - `test_code_tabs_are_restored_per_project` — вкладки в alpha [A, B] и в beta [C]; переход alpha → beta → alpha — ряд свой у каждого; перезагрузка — тот же ряд и активная.
  - `test_code_twenty_first_tab_pushes_out_the_leftmost` — 21 сессия, открыть все → 20 вкладок, первой нет.
  - `test_code_missing_session_tab_goes_away` — вкладка сессии, которой нет на машине (история 404 `not_found`) → убрана, активна соседняя; сессия с `"listed": False` → вкладка «Сессия» осталась, её история показана.
  - `test_code_push_link_adds_a_tab` — `test_code_push_click_opens_the_chat_in_an_open_studio` дополнить: адрес из push на сессию без вкладки → вкладка добавлена и активна.
  - `test_code_fast_tab_clicks_end_on_the_last` (Review Focus 4) — история A отвечает медленно (заглушка `fetch`), нажать A, затем B → на экране B, `#code-feed` — B.
  - `test_code_tab_opens_when_the_workspace_write_fails` (Review Focus 5) — PATCH `/api/code/workspace` → 500 (заглушка) → вкладка открылась, `problems(r)` без необработанных отказов.
  - `test_code_unsaved_edit_asks_before_a_tab_switch` — правка во «Файлах», нажатие вкладки → окно «Изменения не сохранены»; «Остаться» — правка цела; «Уйти» — чат вкладки.
  - `test_code_long_tab_titles_fit[320|1280]` (Review Focus 1) — 5 вкладок с названиями по 120 знаков → `NO_OVERFLOW`, ряд — одна строка, на 320 листается вбок, активная видна (`scrollIntoView`).

- [ ] **Step 2: Запустить — падают.**

- [ ] **Step 3: `code-tabs.js`** — по Interfaces; меню «+» закрывается выбором, нажатием вне и Escape.

- [ ] **Step 4: `code.js`** — ряд вкладок в `.code-top` над `.code-head` на широком экране, под ним на телефоне (CSS `order`). Проект: вкладки из `tabs(aid, pid)`; `#/code/<aid>/<pid>` с вкладками → `location.replace` на активную (или на `?tab=` вида из `place` этого проекта); без вкладок — `sessionList` во всю ширину. Чат: адрес с сессией без вкладки — `act('open')` (вкладка добавлена). Выбор вкладки, «Новый чат» (`created`) и пункт меню — `act('open', …)` и `location.replace` на адрес чата; «×» — `act('close')`, активная — соседняя справа, иначе слева (как на сервере — сразу, не дожидаясь ответа), последней нет — адрес проекта. Отказ истории 404 `not_found` (`chatView` сообщает через новый `gone(): void` в опциях) — как «×» этой вкладки. `info` вкладок — из `GET …/chats` проекта (одним запросом при открытии проекта). Смена проекта — по ссылке (новая запись), смена вкладки и вида — заменой. При «Файлах» и «Git» ряд вкладок на месте, все `aria-selected="false"`; нажатие на вкладку — снова её чат.

- [ ] **Step 5: `code-chat.js`** — опции `closedChat` и `gone` (по Interfaces); 404 `not_found` истории → `gone()` вместо слов. Прочее не трогать.

- [ ] **Step 6: Запустить сценарии раздела** → PASS (все `-k code`).

- [ ] **Step 7: Документы и коммит** — CLAUDE.md (вкладки, предел, «Назад», `gone`), README; быстрый набор → зелёный; коммит «Claude Code: вкладки сессий проекта».

---

### Task 4: Метки вкладок, числа в панели и возвращение на экран

**Files:**
- Modify: `app/static/js/code.js`, `app/static/js/code-panel.js`, `app/static/css/app.css`, `tests/test_ui_browser.py`, `CLAUDE.md`

**Interfaces:**
- Consumes: `tabBar.show` (Task 3), `panelView.refresh` (Task 2), `code-workspace.js` `load`/`subscribe`.
- Produces: в `code.js` — `IDLE_POLL_SEC = 20`, `IDLE_POLL_FOR_SEC = 1800` (те же, что в `code-chat.js`; тесты подменяют `setTimeout`, как `CODE_FAST_IDLE`).

- [ ] **Step 1: Сценарии**
  - `test_code_tab_marks_follow_the_machine` — вкладки A, B; на машине в B начался ход (`code_start_elsewhere`) → у вкладки B `.code-tab-mark.running` в пределах одного опроса; вопрос агента → `.asking`; ход кончился → меток нет.
  - `test_code_tab_marks_stop_polling_after_half_an_hour` — `Date.now` и `setTimeout` подменой: после `IDLE_POLL_FOR_SEC` без действий запросов `GET …/chats` нет; нажатие в разделе — опрос снова.
  - `test_code_hidden_page_does_not_poll_marks` — `visibilitychange: hidden` → запросов `…/chats` нет.
  - `test_code_panel_counts_open_tabs` — у alpha 2 вкладки, у beta 0 → `.code-project-count` «2» и пусто; открыли третью — «3».
  - `test_code_return_to_the_screen_refreshes_without_jumping` — две страницы одной учётки (вторая — `fetch` к `/api/code/workspace` после действий первой): на второй открыт alpha/A; первая открыла beta и вкладку C в alpha; вторая — `visibilitychange: visible` → ряд alpha — с C, на экране по-прежнему alpha/A, адрес прежний.
  - `test_code_background_tab_keeps_no_stream` — вкладки A и B, активна B, в A на машине ход: у сервера нет наблюдателя A (`bridge.watching` ложно — через `live` приложения), push A ушёл бы.

- [ ] **Step 2: Запустить — падают.**

- [ ] **Step 3: `code.js`** — опрос `GET …/chats` открытого проекта раз в `IDLE_POLL_SEC`, только при `visibilityState === 'visible'` и `Date.now() - acted < IDLE_POLL_FOR_SEC * 1000`; `acted` — `pointerdown`/`keydown` в разделе; ответ → `tabBar.show` (`info`), смена проекта или уход — таймер снят. `visibilitychange: visible` → `load()`, затем `panel.refresh()` и ряд вкладок открытого проекта = вкладки сервера плюс показанная сейчас (если её там уже нет); проект, вкладку и адрес не менять.

- [ ] **Step 4: `code-panel.js`** — `refresh()` по `subscribe` рабочего места (числа меняются после каждого `act`).

- [ ] **Step 5: CSS** — `.code-tab-mark.running` (точка), `.code-tab-mark.asking` (акцент `var(--accent)`, заметнее).

- [ ] **Step 6: Полный прогон раздела и быстрого набора** — `-m integration tests/test_ui_browser.py -n0` (весь файл: сценарии других разделов задевает `app.css`) и `.venv/Scripts/python -m pytest -q` → PASS.

- [ ] **Step 7: Документы и коммит** — CLAUDE.md (опрос меток и его предел, возвращение на экран); коммит «Claude Code: метки вкладок и возвращение на экран».
