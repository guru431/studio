# Studio 0.55.0 — навыки, строка состояния «Чата», локальные модели одним планом (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** одним выпуском сделать все 43 изменения, утверждённые владельцем: навыки и разбиение задачи на части,
перенос возможностей «Агентов» в «Чат» (строка состояния, статистика, ветки, очередь, вкладки), экономную работу
с окном локальной модели, общие удобства разделов и 12 вложений в «Агентах» — без деления на выпуски.

**Architecture:** навык — именованный текст, который человек вызывает через «/», а Studio подставляет в
сообщение: библиотека учётки на сервере (образец — роли) плюс файлы `SKILL.md` проекта через мост. Разбиение в
«Чате» — навык и один встроенный инструмент `run_part` (отдельный запрос с чистым контекстом). «Чат» получает
каркас строки состояния «Агентов»; сервер начинает читать `usage` шлюза и хранить его у ответа. История для
модели становится неизменной от хода к ходу: выжимки результатов инструментов, хранимый пересказ, постоянные
имена инструментов. Один шаг схемы 82; новые возможности моста — по capability в `hello`.

**Tech Stack:** FastAPI, SQLite (FTS5), httpx, Fernet (`STUDIO_DATA_KEY`), ES-модули без сборки, Web Push, мост
`studio_agent` (Python 3.11+, протокол v1 по WebSocket), Claude Agent SDK, Codex app-server, ACP (OpenCode, DSH).

**Spec:** [2026-11-25-studio-skills-status-local-models-design.md](../specs/2026-11-25-studio-skills-status-local-models-design.md)
(номера пунктов 1–43 ниже — оттуда); правила — CLAUDE.md и `agent/CLAUDE.md`. Решения владельца и раздел
«Не-цели» спеки не пересматривать.

## Как выполнять

- Одна сессия, все задачи. Исполнители — агенты `implementer` в worktree, не больше 4 одновременно, волнами ниже.
  Первое действие исполнителя — `git merge --ff-only main` (worktree создаётся от коммита начала сессии). На одного
  исполнителя — одна задача, коммит после каждого пункта задачи.
- Задача — единица слияния и набора файлов, а не объём одного исполнителя. На одного исполнителя — не больше
  двух пунктов спеки (двух групп коммитов задачи): длинные задачи (6, 7, 9, 10, 13, 15, 16) ведут несколько
  исполнителей подряд в одном worktree-наследнике, каждый следующий — с записки предыдущего и со следующего
  коммита, названного в шаге Commit задачи.
- Порядок волн — зависимости, а не приоритет: все задачи обязательны.
- Слияние в `main` — cherry-pick; конфликты дописывания (`chat.py`, `chat.js`, `code-chat.js`, `en.js`, `app.css`,
  `admin.py`, карта модулей CLAUDE.md) — оставлять обе стороны. После каждого слияния — быстрый набор.
- Новое поведение страницы — в новых модулях; в `chat.js` и `code-chat.js` только точки подключения. Задача,
  которой нужно больше, пишет об этом в отчёте до правки.
- Ход не завершать, пока работают исполнители (запускать их на переднем плане, по 4 в одном сообщении).
- Push, выкладка и перезапуск работающего моста — не делать. Вопросы владельцу — одним сообщением в конце.
- Находки вне задач — в `FINDINGS.md`; три записи от 2026-11-25 закрывают задачи 7, 9 и 17 — заголовок записи в
  сообщение коммита. Саму запись удаляет ведущий при слиянии: журнал вне Git, в worktree исполнителя его нет.
- До выпуска — живая проба хода s06 после задачи 9: ход начинает нести `enable_thinking: false`, и если шлюз
  параметр не применяет, длинное рассуждение оборвёт ответ. Пробу делает владелец или ведущий с его согласия.

| Волна | Задачи (параллельно) | Ждёт |
|---|---|---|
| A | 1 Схема 82 и версии · 2 Мост 0.48.0 · 3 Общие модули страницы · 8 Инструменты MCP | — |
| B | 4 `usage` и длительность ответа · 5 Библиотека навыков · 7 История · 12 «Агенты»: вложения, время, поиск, копирование | A |
| C | 6 Строка состояния «Чата» · 9 Окно, «локальная», параметры на модель · 11 «Агенты»: навыки · 13 «Чат»: ветка, стрелки, черновик, очередь, вставка | B |
| D | 10 Навыки в «Чате» и `run_part` · 14 «Чат»: список, вкладки, push на паузе · 15 «Чат»: окно HTML, диалог с итогом, источники, картинки MCP, рассуждение · 16 Все разделы | C |
| E | 17 Завершение | D |

Номер задачи — имя, а не очередь: задачи ниже стоят по волнам. Слияние внутри волны — по возрастанию номера
(в волне D: 10 → 14 → 15 → 16, все четыре правят `chat.py` и `chat.js`). Упоминания «задача N слита раньше» в
тексте задач согласованы с этой таблицей; при расхождении права таблица.

Команды: быстрый набор `.venv/Scripts/python -m pytest -q`; файл — `.venv/Scripts/python -m pytest -q -n0
--assert=rewrite <файл>`; браузерный файл — `.venv/Scripts/python -m pytest -q -n0 -m integration <файл>`; мост — из
`agent/` его средой: `.venv/Scripts/python -m pytest -q` и `… -m integration`.

## Покрытие пунктов спеки

| Пункты | Задачи |
|---|---|
| Данные шага 82, откат | 1 |
| 1, 4, 5, 6 — библиотека навыков, типовой набор, перенос быстрых действий | 5 (кнопки пустого чата «Агентов» — 11) |
| 2 — меню «/» | 3 (модуль), 10 («Чат»), 11 («Агенты») |
| 3 — навыки проекта | 2 (мост), 11 (сервер и страница) |
| 7 — `run_part` | 10 |
| 8, 9, 10, 43 — время и скорость, поиск в чате, «Копировать» и модель, вложения 12/12 | 2 (мост), 12 |
| 11 — «Проверить другим помощником» | 11 |
| 12–17, 36 — строка состояния «Чата», окно, статистика, файлы, счётчик, шапка, сведения о карте | 3 (каркас), 4 (`usage`), 6 |
| 18, 19, 21, 22, 24 — ветка, стрелки, черновик, очередь, вставка | 3 (модули), 13 |
| 20, 23, 26 — push на паузе, список диалогов, вкладки | 14 |
| 25, 27, 28, 39, 40 — окно HTML, диалог с итогом, источники знаний, картинки MCP, рассуждение | 15 |
| 29, 30, 34 — выжимки, пересказ, неизменное начало | 7 (имена инструментов для п. 34 — 8) |
| 31, 35 — счёт окна, признак «локальная», параметры на модель | 9 |
| 32, 33 — имена и пределы инструментов MCP, терпимость к вызову | 8 |
| 37, 38, 41, 42 — звук, цитата и меню, ширина и поле, «Поделиться» | 16 («Скопировать адрес» — 6) |
| Документация и выпуск, журналы | 17 |

## Global Constraints

- Версии: `app/release.py` `VERSION = "0.55.0"`; `db.VERSION = 82`, **один** шаг `db.STEPS[82]`, только добавление,
  новых столбцов `*media_id` нет; мост `agent/studio_agent/__init__.py` `VERSION = "0.48.0"`, протокол v1 без
  изменений — новое только по capability в `hello`.
- Приватность: в журнал — id, коды и числа; тексты навыков, пересказов, цитат, промптов, имена файлов, пути,
  аргументы инструментов — никогда. Тексты агентских чатов в базу не пишутся (кроме снимка ссылки шифром).
  Администратор чужие навыки, статистику и файлы диалогов не видит.
- Защищённый источник не ослаблять: ветка, «Новый диалог с итогом» и `run_part` наследуют `chats.protected_used`;
  окно HTML/SVG не ходит в сеть. Запрос подключения через Cleaner — только через `cleaner_route`.
- Анонимные входы — только перечисленные в CLAUDE.md; новых нет. Новых внешних адресов у сервера и моста нет.
- Контракт шлюзов (`llm_routers`) не меняется.
- Страница: подписи — `tr`/`data-i18n` и `static/en.js`; значки — `icons.js`; меню — `menu.js`; цвета —
  `light-dark()`; `:hover` — только в `@media (hover: hover)`; имена разделов — `sections-meta.js` + `sections.NAMES`.
- Тесты: без реального времени (`clock.NOW`, `clock.SLEEP`), шлюз — `tests/fakes.py`, мост — `tests/fake_bridge.py`;
  дольше 1 с и браузер — `integration` (320 и 1280 px); быстрый набор ≤ 60 с.
- Числа (копировать как есть): навык — имя ≤ 80, описание ≤ 200, текст ≤ 100 000 знаков, навыков учётки ≤ 100;
  навык проекта — файл ≤ 64 КиБ, в списке ≤ 100, папки `.agents/skills` и `.claude/skills`; части — ≤ 10 на ответ,
  одна за круг, итог ≤ 4000 знаков, кругов инструментов в части ≤ 8, ответ по частям — до 7200 с; вложения «Агентов» — изображений ≤ 12, всего ≤ 12, файл ≤
  10 МиБ, всего ≤ 30 МиБ; «Чат» — `chat.ATTACH_MAX = 10`, `profiles.DEFAULT_MAX_IMAGES = 4` (без изменений); выжимка
  результата инструмента — 1500 знаков; пересказ — 2048 токенов, «не длиннее 500 слов»; инструменты MCP локальной
  модели — ≤ 32, описание ≤ 1000 знаков; статистика — 31 день; цитата ≤ 4000 знаков; очередь «После ответа» ≤ 5;
  вкладок диалогов ≤ 12; блок HTML/SVG ≤ 2 МиБ; уровень рассуждения — `low`, `medium`, `high`; «последнее событие
  N с назад» — после 10 с без событий.

## Review Focus

1. **Старый мост 0.47 и новая страница** — больше 4 изображений, навыки проекта в «/», время хода: страница не
   шлёт незнакомых просьб, отказ словами с действующим числом до отправки. Тесты — задачи 2, 11, 12.
2. **Шлюз без `usage` или отвергающий параметр потока** — ход работает, поля пустые, один повтор без параметра,
   подключение запоминается до перезапуска службы. Тесты — задача 4.
3. **Защищённый диалог** — ветка, «Новый диалог с итогом», `run_part`, окно HTML: отметка наследуется, «Интернет»
   не появляется, окно без сети. Тесты — задачи 10, 13, 15.
4. **Очередь «После ответа»** — ход с другого устройства (409 `busy`), ждущая карточка, закрытие страницы:
   сообщение не теряется и не уходит дважды. Тесты — задача 13.
5. **Телефон 320 px на английском** — строка состояния «Чата», вкладки диалогов, меню «/»: без горизонтальной
   прокрутки и выхода за экран. Тесты — задачи 3, 6, 14.

---

## Волна A

### Task 1: Шаг схемы 82, версия Studio 0.55.0, строка отката

Все 43 изменения делят один шаг схемы: позже добавить столбец нельзя, поэтому здесь — весь состав раздела спеки
«Данные: шаг схемы 82». Образцы — `CAPABILITIES_57` (`app/db.py:778`: `role_defaults`, `chat_roles`) и `SHARES_69`
(`app/db.py:1009`: триггеры `chat_shares_<таблица>_gone`). Что нашли в коде: к `connections` после пересборки шага
55 новые таблицы привязаны триггером, а не внешним ключом (`connection_profiles_delete`, `app/db.py:781`; DROP при
пересборке каскадом стёр бы зависимые строки — `app/db.py:624`); `chat_shares_item` — уникальный индекс `(item_kind,
item_id)` на всю базу, не на учётку (`app/db.py:1010`); прежний `chat_shares.LIVE` (`app/chat_shares.py:611`)
пропускает только известные ему `item_kind`; пустой профиль `profiles.put` удаляет строкой (`app/profiles.py:106`);
номер шага сверяет `SCHEMA_VERSION` (`tests/test_foundation.py:18`), строку отката и имена столбцов в CLAUDE.md —
`tests/test_docs.py` (`test_rollback_table_covers_every_schema_step_from_16`, `_columns`).

**Files:**
- Modify: `app/db.py` (константа `SKILLS_82` с комментарием `#:`, `SCHEMA += SKILLS_82`, `STEPS[82]`, `VERSION = 82`)
- Modify: `app/release.py` (`VERSION = "0.55.0"`); `CLAUDE.md` (строка 82 таблицы «Откат по шагам схемы»)
- Test: `tests/test_foundation.py` (`SCHEMA_VERSION = 82`, новый `test_step_82_…`)

**Interfaces:**
- Produces (все задачи опираются на эти имена; `SKILLS_82` — ровно этот SQL):

```sql
CREATE TABLE skill_defaults(id INTEGER PRIMARY KEY AUTOINCREMENT, builtin TEXT,
       lang TEXT NOT NULL CHECK (lang IN ('ru','en')), name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
       text TEXT NOT NULL, scope TEXT NOT NULL CHECK (scope IN ('chat','agents','both')),
       quick INTEGER NOT NULL DEFAULT 0, parts INTEGER NOT NULL DEFAULT 0, position INTEGER NOT NULL);
CREATE TABLE skills(id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE,
       origin TEXT, name TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', text TEXT NOT NULL,
       scope TEXT NOT NULL CHECK (scope IN ('chat','agents','both')), quick INTEGER NOT NULL DEFAULT 0,
       parts INTEGER NOT NULL DEFAULT 0, position INTEGER NOT NULL, updated REAL NOT NULL);
CREATE INDEX skills_user ON skills(user_id, position);
ALTER TABLE users ADD COLUMN skills_seeded REAL;
ALTER TABLE messages ADD COLUMN skill TEXT;
ALTER TABLE messages ADD COLUMN usage TEXT;
ALTER TABLE messages ADD COLUMN gen_ms INTEGER;
ALTER TABLE messages ADD COLUMN total_ms INTEGER;
ALTER TABLE chats ADD COLUMN summary TEXT;
ALTER TABLE chats ADD COLUMN summary_upto TEXT;
ALTER TABLE chats ADD COLUMN summary_at REAL;
ALTER TABLE chats ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
ALTER TABLE chats ADD COLUMN seen REAL;
UPDATE chats SET seen = updated;
ALTER TABLE chats ADD COLUMN effort TEXT CHECK (effort IN ('low','medium','high'));
ALTER TABLE connection_profiles ADD COLUMN model_params TEXT NOT NULL DEFAULT '{}';
CREATE TABLE model_facts(connection_id INTEGER NOT NULL, model TEXT NOT NULL, ratio REAL, window INTEGER,
       updated REAL NOT NULL, PRIMARY KEY (connection_id, model));
CREATE TRIGGER model_facts_connection_delete AFTER DELETE ON connections BEGIN
  DELETE FROM model_facts WHERE connection_id = old.id; END;
CREATE TRIGGER chat_shares_speech_chats_gone AFTER DELETE ON speeches
  WHEN NOT EXISTS (SELECT 1 FROM speeches WHERE chat_id = old.chat_id) BEGIN
  DELETE FROM chat_shares WHERE item_kind = 'speech_chats' AND item_id = old.chat_id; END;
CREATE TRIGGER chat_shares_owui_gone AFTER DELETE ON owui_accounts BEGIN
  DELETE FROM chat_shares WHERE item_kind = 'owui' AND item_id LIKE old.id || ':%'; END;
```

  - `skill_defaults.builtin` — ключ встроенного навыка (латиница, цифры, дефис) или NULL (умолчание завёл
    администратор). `skills.origin` — ключ умолчания, из которого навык засеян: его `builtin`, а без него —
    `default-<id>`; NULL — навык создал человек.
  - `messages.skill` — JSON `{"name": str, "chars": int, "parts": bool}` (`chars` — длина текста навыка в начале
    `content`); `messages.usage` — JSON `{"in", "out", "cached", "reasoning"}` (нет ключа — шлюз не сообщил);
    `gen_ms`, `total_ms` — целые миллисекунды.
  - `chats.seen` — время последнего открытия; прежние диалоги получают `seen = updated` (после обновления они не
    «не прочитаны»), вставка прежнего кода — NULL. `chats.effort` — NULL или `low|medium|high`.
  - `connection_profiles.model_params` — JSON `{модель: параметры}`. `model_facts` — без внешнего ключа: строки
    уносит триггер. Ссылки новых видов: `item_kind = 'speech_chats'`, `item_id` = `speeches.chat_id`;
    `item_kind = 'owui'`, `item_id` = `"<owui_accounts.id>:<id чата Open WebUI>"` (уходит с учёткой Open WebUI).

- [ ] **Step 1: Write the failing test** `tests/test_foundation.py::test_step_82_adds_skills_answer_stats_summary_and_previous_writer_works`
  (образец — `test_step_57_…`, `test_step_66_…`; `SCHEMA_VERSION = 82`):

```python
install_schema(conn, 81)                                    # списки столбцов — как у прежнего кода
conn.execute("INSERT INTO users(id,login,name,pw_hash,created,code_quick_actions) VALUES(1,'owner','Owner','hash',0,'[]')")
chat = "INSERT INTO chats(id,user_id,title,created,updated) VALUES(?,1,'t',0,?)"
message = "INSERT INTO messages(id,chat_id,role,content,status,via,created) VALUES(?,?,'user','hi','done','server',0)"
connection = ("INSERT INTO connections(id,user_id,name,kind,base_url,token_enc,sections,created,server_only)"
              " VALUES(?,1,'c','openai','https://x.test/v1',x'00','[]',0,1)")
profile = "INSERT INTO connection_profiles(connection_id,system_text,params,suggestions,updated) VALUES(?,'s','{}','[]',0)"
# до шага: chat('before', 7), message('m1','before'), connection(5), profile(5); затем db.migrate дважды
assert conn.execute("PRAGMA user_version").fetchone()[0] == db.VERSION == SCHEMA_VERSION == 82
assert columns("skill_defaults") == {"id","builtin","lang","name","description","text","scope","quick","parts","position"}
assert columns("skills") == {"id","user_id","origin","name","description","text","scope","quick","parts","position","updated"}
assert columns("model_facts") == {"connection_id","model","ratio","window","updated"}
assert {"skill","usage","gen_ms","total_ms"} <= columns("messages")
assert {"summary","summary_upto","summary_at","pinned","seen","effort"} <= columns("chats")
assert conn.execute("SELECT pinned, seen, effort, summary FROM chats WHERE id='before'").fetchone()[:] == (0, 7, None, None)
assert conn.execute("SELECT model_params FROM connection_profiles").fetchone()[0] == "{}"
assert conn.execute("SELECT code_quick_actions, skills_seeded FROM users").fetchone()[:] == ("[]", None)   # не стёрты
# прежний писатель после шага: chat('previous', 9), message('m2','previous'), connection(6), profile(6) — без ошибок
assert conn.execute("SELECT pinned, seen FROM chats WHERE id='previous'").fetchone()[:] == (0, None)
# sqlite3.IntegrityError: skills.scope='all'; skill_defaults.lang='de'; UPDATE chats SET effort='max'
conn.execute("INSERT INTO model_facts(connection_id,model,ratio,window,updated) VALUES(5,'m',3.2,131072,0)")
assert conn.execute("SELECT window FROM model_facts").fetchone()[0] == 131072      # имя `window` без кавычек
conn.execute("DELETE FROM connections WHERE id=5"); assert count("model_facts") == 0
# две озвучки с chat_id='group' и ссылка ('speech_chats','group'); учётка Open WebUI id=2 и ссылки ('owui','2:abc'),
# ('owui','20:abc'): удалили первую озвучку — ссылка жива; вторую озвучку и учётку 2 — осталась только '20:abc'
assert [r[0] for r in conn.execute("SELECT item_id FROM chat_shares")] == ["20:abc"]
# DELETE FROM users WHERE id=1: skills — 0 строк, skill_defaults — на месте; PRAGMA foreign_key_check == []
```

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -n0 --assert=rewrite tests/test_foundation.py` — Expected:
  FAIL (`db.VERSION == 81`, таблицы `skill_defaults` нет).
- [ ] **Step 3: Implement** `SKILLS_82` (SQL выше дословно), `SCHEMA += SKILLS_82`, `STEPS[82] = SKILLS_82`,
  `VERSION = 82`; `release.VERSION = "0.55.0"`. Строка CLAUDE.md после строки 81, одной строкой таблицы:
  «| 82 | навыки (`skill_defaults`, `skills`, `users.skills_seeded`, `messages.skill`), токены и время ответа
  (`messages.usage`, `gen_ms`, `total_ms`), `chats.summary`, `summary_upto`, `summary_at`, `pinned`, `seen`,
  `effort`, `connection_profiles.model_params`, `model_facts`, ссылки «Поделиться» видов `speech_chats` и `owui` |
  не читает: навыков, «/» и `run_part` нет; быстрые действия «Агентов» — прежние из `users.code_quick_actions`
  (новый код его не стирает; правки навыков в них не попадают); сообщение с навыком видно и правится целиком,
  вместе с текстом навыка (отметка `messages.skill` остаётся); шаги частей под ответом — «Инструмент»; токены,
  время ответа, закрепление, «не прочитано», уровень рассуждения и параметры на модель не читаются (параметры — с
  подключения; очистка профиля прежним кодом удаляет строку вместе с `model_params`); пересказ не читает и
  считает заново; ссылки новых видов недоступны (прежний `chat_shares.LIVE` не знает их `item_kind`), отключить в
  «Доступах и ссылках» можно; удаление последней озвучки чата и учётки Open WebUI уносит их ссылки триггерами |
  Не нужен; данные сохраняются до возврата вперёд. Если прежним кодом удаляли ходы или правили сообщения, после
  возврата вперёд сбросить пересказы: `UPDATE chats SET summary = NULL, summary_upto = NULL, summary_at = NULL;` —
  построятся заново |».
- [ ] **Step 4: Run** `.venv/Scripts/python -m pytest -q -n0 tests/test_foundation.py tests/test_docs.py
  tests/test_backup_check.py tests/test_media_orphans.py` — Expected: PASS.
- [ ] **Step 5: Commit** `Add schema step 82 for skills, answer stats, summaries and model facts`.

### Task 2: Мост 0.48.0 — навыки проекта, пределы вложений, время хода

Новое мост объявляет в `hello` (`agent/studio_agent/agent.py:194`), просьбы — таблица `OPS` (`:36`), доступ участника —
литерал `SHARED_OPS` (`:114`; его читает AST в `tests/fake_bridge.py:23` — форму не менять). Числа вложений зашиты в
`limits.py:14,17` и в словах `image_input.py:23`, `attachments.py:31`. Ход всех четырёх помощников начинает
`turns.Runner.start` (`turns.py:158`) и кончает `_finish` → `_done` (`:767,814`); `state` у каждого свой, но идёт через
`Agent._op_state` (`agent.py:1022`). Модель ответа знают только `sessions.normalize` (`sessions.py:48`) и
`deepseek_history.read` (`:328`). Сервер разбирает `hello` в `app/bridge.py:225`. Строки — на коммите `13d063ef`.

**Files:**
- Create: `agent/studio_agent/skills.py`, `agent/tests/test_skills.py`, `agent/tests/test_turn_time.py`
- Modify: `agent/studio_agent/` — `__init__.py` (`VERSION = "0.48.0"`), `limits.py`, `agent.py`, `image_input.py`,
  `attachments.py`, `store.py`, `turns.py`, `sessions.py`, `deepseek_history.py`; `agent/tests/turn_events.py`;
  `app/bridge.py`, `app/static/en.js`, `tests/fake_bridge.py`; документы — `agent/CLAUDE.md`, `agent/README.md:98`,
  `docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md` (§26), `docs/architecture/agent-bridge.md`
- Test: `agent/tests/` — `test_image_input.py`, `test_attachments.py`, `test_sessions.py`, `test_deepseek_history.py`;
  `tests/test_bridge.py`

**Interfaces:**
- Produces (мост): `limits.IMAGE_COUNT = 12`, `ATTACHMENT_COUNT = 12`, `SKILL_MAX = 64 * 1024`, `SKILLS_MAX = 100`;
  `hello.skills: true`, `hello.limits: {"images": 12, "attachments": 12}`; просьбы `skills {project}` → `{"items":
  [{"name", "description", "folder"}], "truncated": bool}` и `skill {project, name, folder}` → `{"text": str}` (отказы
  `invalid`, `not_found`, `too_large`, `binary`, `outside`) — обе в `RESTART_SAFE_OPS` и `SHARED_OPS`.
- Produces (`skills.py`): `FOLDERS = (".agents/skills", ".claude/skills")`, `NAME = re.compile(r"[A-Za-z0-9]
  [A-Za-z0-9._-]{0,63}")`, `DESCRIPTION_MAX = 200`, `HEAD = 4096`; `head(text) -> tuple[str, str]` (описание, текст без
  заголовка), `list_skills(project: Path) -> dict`, `read_skill(project: Path, name: str, folder: str) -> dict`.
- Produces (время, история): в событии `done` и ответе `state` — `turn_ms: int`, `session_ms: int`;
  `store.Choices.add_turn(chat: str, turn_ms: int) -> int`; `Runner(..., monotonic=time.monotonic)`; у ответа
  помощника в `history.items[]` — `model: str` (Claude Code и DeepSeek Harness; Codex и OpenCode её не хранят).
- Produces (сервер): `bridge.LEGACY_LIMITS = {"images": 4, "attachments": 8}`, `bridge.LIMITS_MAX = {"images": 12,
  "attachments": 12}`, `bridge.turn_limits(hello: dict) -> dict`; `parse_hello` отдаёт `skills: bool` и `limits`.
- Produces (эталон): `FakeMachine.skills = False`, `.limits = None`, `.timing = False` (по умолчанию — мост 0.47);
  навыки проекта — `projects[имя]["skills"] = {(folder, name): {"description": str, "text": str}}`.

- [ ] **Step 1: Write the failing tests** `agent/tests/test_skills.py` (`agent`, `root`, `req` — из `test_agent.py`;
  ссылки — `agent/tests/platform_fs.py`) и `agent/tests/test_turn_time.py` (`home`, `project`, `settle` — `test_turns.py`):

```python
HEADED = '---\nname: x\ndescription: "Приёмка — сверить"\ndisable-model-invocation: true\n---\n\nТекст навыка\n'
def test_list_reads_both_folders_without_text(tmp_path):
    put(p, ".agents/skills", "acceptance", HEADED); put(p, ".claude/skills", "review", "# Ревью\n\nСмотри diff")
    put(p, ".agents/skills", "плохое имя", "x"); (p / ".agents/skills/empty").mkdir()        # оба — мимо списка
    assert skills.list_skills(p) == {"truncated": False, "items": [
        {"name": "acceptance", "description": "Приёмка — сверить", "folder": ".agents/skills"},
        {"name": "review", "description": "Ревью", "folder": ".claude/skills"}]}
    assert skills.read_skill(p, "acceptance", ".agents/skills") == {"text": "Текст навыка"}
def test_limits_bad_fields_and_links(tmp_path):
    # 101 навык: len(items) == limits.SKILLS_MAX == 100 and truncated is True
    # SKILL.md в limits.SKILL_MAX + 1 байт: в списке нет, read_skill — too_large; NUL — binary; описание 500 → 200
    # ("../x", ".agents/skills"), ("acceptance", "docs") — invalid; ("none", ".claude/skills") — not_found
    # ссылка на папку внутри проекта читается (как files.resolve); на папку вне проекта — в списке нет, read — outside
async def test_skills_through_agent(agent, root, caplog):
    assert agent.hello()["skills"] is True and agent.hello()["limits"] == {"images": 12, "attachments": 12}
    # просьбы skills и skill — ok, и с shared_projects=[pid], и с shared_readonly=True; без открытой сессии — closed
    assert "acceptance" not in caplog.text and "Текст навыка" not in caplog.text
async def test_done_carries_turn_and_session_time(home, project):                   # test_turn_time.py
    ticks = iter([100.0, 101.5, 200.0, 200.25])                                      # начало и конец двух ходов
    r = Runner(sent.append, choices=store.Choices(home), cli_path=None, factory=FakeFactory([result_ok()] * 2),
               monotonic=lambda: next(ticks))
    r.start(project, PID, CHAT, "Раз", None, None); await settle(r)
    assert sent[-1]["e"] == {"k": "done", "status": "done", "turn_ms": 1500, "session_ms": 1500}
    r.start(project, PID, CHAT, "Два", None, None); await settle(r)
    assert (sent[-1]["e"]["turn_ms"], sent[-1]["e"]["session_ms"]) == (250, 1750)
# test_turn_that_never_reached_the_cli_has_no_time: stop до connect — в done нет turn_ms, в choices нет session_ms
# test_state_adds_the_saved_time: choices.add_turn(CHAT, 700) → в ответе state turn_ms == session_ms == 700; без ходов — нет
```

  Правки существующих: `test_image_input.py` — `[PNG] * 5` → `[PNG] * 13`, 12 проходят, слова `"можно прикрепить до 12
  изображений"`; `test_attachments.py` — `[FILE] * 9` → `[FILE] * 13`, `validate([FILE] * 12, [PNG])` — «…до 12 файлов»;
  `test_sessions.py::test_normalize_merges_one_answer…` — у ответа `"model": "claude-opus-5-5"`, у `<synthetic>` ключа
  нет; `test_deepseek_history.py` — ответ с `message.source.model = "deepseek-chat"` несёт `"model"`. `tests/test_bridge.py`:

```python
def test_hello_limits_and_skills_and_the_old_bridge_defaults():                    # Review Focus 1
    old = bridge.parse_hello(FakeMachine().hello)
    assert "skills" not in old and bridge.turn_limits(old) == bridge.LEGACY_LIMITS == {"images": 4, "attachments": 8}
    m = FakeMachine(); m.skills, m.limits = True, {"images": 12, "attachments": 12}
    assert bridge.parse_hello(m.hello)["skills"] is True and bridge.turn_limits(bridge.parse_hello(m.hello)) == m.limits
    assert bridge.turn_limits({"limits": {"images": 50, "attachments": 90}}) == bridge.LIMITS_MAX
    for bad in ({"images": 0, "attachments": 8}, {"images": True, "attachments": 8}, {"images": 4}, [4, 8],
                {"images": 9, "attachments": 8}):
        with pytest.raises(bridge.ProtocolError): bridge.parse_hello({**m.hello, "limits": bad})
# test_fake_machine_follows_the_protocol — дописать: по умолчанию skills — invalid, 5 изображений — invalid «можно
#   прикрепить до 4 изображений», хода нет; с m.skills и m.limits: skills и skill — ok, 12 изображений — ход, 13 —
#   «до 12 изображений»; с m.timing done и state несут turn_ms == 1000 и session_ms
```

- [ ] **Step 2: Run** из `agent/`: `.venv/Scripts/python -m pytest -q tests/test_skills.py tests/test_turn_time.py` —
  Expected: FAIL (`cannot import name 'skills'`); из корня `… -n0 --assert=rewrite tests/test_bridge.py` — FAIL.
- [ ] **Step 3: Implement — навыки.** `list_skills`: папки по порядку `FOLDERS`, внутри — по имени без регистра; запись
  берётся, если имя подходит `NAME`, а `files.resolve(project, f"{folder}/{name}/SKILL.md")` — обычный файл не больше
  `limits.SKILL_MAX` (ссылки разрешены; выход из проекта — запись пропущена); читаются первые `HEAD` байт; сотый
  элемент — стоп и `truncated: True`. `head`: BOM и `\r` прочь; первая строка `---` и есть закрывающая — заголовок, из
  него `description:` (в двойных кавычках — `json.loads`; пусто, `>` или `|` — следующие строки с отступом через
  пробел); заголовка нет — первая непустая строка без ведущих `#`; описание — `events.clip(…, DESCRIPTION_MAX)`.
  `read_skill`: папка не из `FOLDERS` или имя мимо `NAME` — `invalid`; `files._read(project, путь, limits.SKILL_MAX,
  "навык больше 64 КиБ")` и `files._text`; текст — тело без заголовка, `.strip()`; пусто — `invalid` «навык пуст»; нет
  файла — `not_found` «нет такого навыка». `agent.py`: `OPS["skills"] = ("project",)`, `OPS["skill"] = ("project",
  "name", "folder")`; `_field`: `name` при `op == "skill"` — `skills.NAME.fullmatch`, `folder` — `in skills.FOLDERS`;
  `_op_skills` — через `self._metadata.get((frame["project"], "skills"), …, ttl=20)`. В журнал — только код отказа.
- [ ] **Step 4: Implement — пределы и знакомство.** Константы `limits.py`; слова отказов — f-строками от них;
  `Agent.hello` — `skills`, `limits`. `app/bridge.py`: `skills` — в кортеж булевых capability; `limits` — объект с
  целыми (не `bool`) `1 <= images <= attachments <= 100`, иначе `ProtocolError("hello")`; `turn_limits` — числа
  `hello["limits"]`, каждое не больше `LIMITS_MAX`; нет ключа — `LEGACY_LIMITS`. `en.js`: шаблоны `"можно прикрепить до
  {0} изображений"`, `"можно прикрепить до {0} файлов"` (словарь и `StudioEnglishMessages`), строки «нет такого
  навыка», «навык больше 64 КиБ», «навык пуст» — сторож `tests/test_i18n_server.py`.
- [ ] **Step 5: Implement — время и модель.** `turns.py`: `_Turn.started: float = 0.0`, `_Turn.times: dict | None =
  None`; `start` ставит `t.started = self.monotonic()`; новая `Runner._time(t)` в начале `_finish`: ход без `t.accepted`
  и без `t.start_attempted` не считается; иначе `turn_ms = max(0, int((self.monotonic() - t.started) * 1000))`,
  `session_ms = await asyncio.to_thread(self.choices.add_turn, t.chat, turn_ms)` (`OSError` — только `turn_ms`); `_done`
  дописывает `t.times`. Время — от принятия просьбы `turn` до конца ответа, вместе с ожиданием карточек и очереди.
  `Choices.add_turn` — под замком: сложить и записать `turn_ms`, `session_ms` чата (только числа). `Agent._op_state`
  дописывает их из `choices.details(chat)`, если целые и неотрицательные. `sessions.normalize`: `model` последней
  записи ответа — строка, не `<synthetic>`, подходит `limits.MODEL_ID`; `deepseek_history.read`:
  `_pair(message.source)[1][:100]`. 28 сравнений `== {"k": "done", …}` в `agent/tests` (13 — `test_turns.py`, 5 —
  `test_deepseek.py`, прочие — в шести файлах) перевести на новый помощник `turn_events.bare(event)` (без двух полей).
- [ ] **Step 6: Implement — эталон и документы.** `FakeMachine`: `hello` несёт `skills` и `limits`, когда заданы;
  `_op_skills`/`_op_skill` без `self.skills` — `Refused("invalid", "неизвестная просьба")`; `_op_turn` и `_op_steer`
  считают вложения по `self.limits or {"images": 4, "attachments": 8}` словами моста; с `self.timing` `_finish` кладёт
  в `done` `turn_ms` (секунды часов машины × 1000) и копит `session_ms` в записи чата, `_op_state` их отдаёт; ответу
  истории — `model` из `chat["settings"]`. Протокол §26 «Навыки проекта, пределы вложений и время хода (мост 0.48.0)»:
  просьбы, вид полей, отказы, `hello.limits` (нет поля — 4 и 8), `turn_ms`/`session_ms`, `history.items[].model`.
  `agent-bridge.md`: строка `| 0.48.0 | … | §26 |` с `hello.skills` и `hello.limits` (сторож —
  `test_capability_table_names_every_hello_field` в `tests/test_docs.py`), раздел с `limits.SKILL_MAX` (65536),
  `limits.SKILLS_MAX` (100), `limits.IMAGE_COUNT` (12), `limits.ATTACHMENT_COUNT` (12), модуль `skills` в списке модулей.
  `agent/CLAUDE.md`: `skills` в «Проекты и файлы», пределы 12 и 12; `agent/README.md:98` — 12 файлов, 12 картинок.
- [ ] **Step 7: Run** из `agent/`: `.venv/Scripts/python -m pytest -q` — PASS; из корня: `… -n0 --assert=rewrite
  tests/test_bridge.py tests/test_code.py tests/test_code_images.py tests/test_docs.py tests/test_i18n_server.py` — PASS.
- [ ] **Step 8: Commit** (шаги 3–6): `Add project skills to the Studio bridge`; `Raise bridge attachment limits to 12`;
  `Measure turn time in the bridge and report the answer model`; `Release bridge 0.48.0 with skills and limits`.

### Task 3: Общие модули страницы — без изменения поведения «Агентов»

Четыре вещи «Агентов» нужны «Чату» (п. 2, 12, 19, 21, 22), но зашиты под один раздел. `code-status-bar.js`
(`statusActions`, 141 строка) держит id `code-status-more` / `code-status-menu` (:8, :11) и наименьшие ширины по
классам `code-cache` / `code-usage` / `code-context` (:74–76, :82–83, :93–94, :107). `code-drafts.js` знает один ключ
`studio-agent-drafts` (импорт — `app.js:45`, `code-chat.js:67`, `code.js:57`, тест `tests/test_ui_page_guards.py:37`;
`login.js:7` стирает ключ). Стрелки живут внутри `chatView` (`code-chat.js`): узлы :238–249, `messageNeighbours` …
`jumpMessage` :701–767, обработчики :1997–2009, а их состояние читают `grow` :772, `trackScroll` :783, `tailObserver`
:792–796, `loadHistory` :1761, `loadEarlier` :1978–1984 и шаг :887. Меню «/» нет вовсе: образец списка под полем —
`code-autocomplete.js::fileHints`, Enter поля ловит `field.js::enterSends`. Классы `.code-status-*`
(`app.css:2167–2183`), `.code-message-nav` (:2257–2261) и id «Агентов» стоят в 12 браузерных тестах — **не
переименовываются**: переезжает только код, «Чат» берёт те же классы со своими id.

**Files:**
- Create: `app/static/js/status-bar.js` (`git mv` из `code-status-bar.js`), `app/static/js/message-nav.js`,
  `app/static/js/dialog-drafts.js` (`git mv` из `code-drafts.js`), `app/static/js/slash.js`
- Modify: `app/static/js/code-chat.js` (импорты :62, :67; места из абзаца выше), `app/static/js/code.js` (:57, :999),
  `app/static/js/app.js:45`, `app/static/login.js:7` (стирать оба ключа), комментарии `api.js:46`,
  `account-security.js:340`; `app/static/css/app.css` (блок `.slash-*`, `.skill-chip`), `app/static/en.js`;
  `docs/architecture/agents-workspace.md` (:225, :613 — новые имена файлов), `docs/architecture/shell.md` (раздел
  «Общие модули ленты и поля ввода»), `CLAUDE.md` (строка «Страница: общее» карты модулей)
- Test: Create `tests/test_ui_shared_modules.py`, `tests/test_ui_slash.py`; Modify `tests/test_ui_page_guards.py:32–43`

**Interfaces:**
- Produces (подключают задачи 6, 10, 11, 13, 14):
  - `statusActions(host, bar, activity, actions, fixed, { id = 'code-status' } = {}) -> { unmount() }` — id «⋯»
    `${id}-more`, панели `${id}-menu`; вид закреплённой кнопки — `button.dataset.status` (`'cache' | 'usage' |
    'ring'`), без атрибута — по прежним классам; ширины прежние (32/28/24, 28/24/20, 26/24/22/18), `'ring'` — квадрат.
  - `messageNav({ scroller, feed, prefix = 'code', own = '.code-msg.user', steps = 'details.code-step', expandLabels,
    stuck, ready = () => true, earlier = null, act = () => {}, follow = () => {}, change = (apply) => apply() }) ->
    { node, paint(), target(), drop(), pin(), expanded(), scrollBy(delta), unmount() }`. `node` —
    `nav.code-message-nav[hidden]` с кнопками `#${prefix}-message-up`, `#${prefix}-toggle-steps`,
    `#${prefix}-message-down`; `expandLabels` — по умолчанию «Развернуть / Свернуть все команды»; `earlier` — `{ has(),
    busy(), load(): Promise<bool> }` или `null` (вся история загружена); `act()` — любое нажатие, `follow(false)` —
    прыжок к сообщению, `follow(true)` — в конец, `change` — правка высоты ленты. `target()`, `drop()`, `pin()`,
    `expanded()`, `scrollBy` — бывшие `messageTarget`, его сброс, `messageScrollTop = scroller.scrollTop`,
    `expandSteps`, `scrollByViewportPixels`. Слушателей `scroll` ленты и `resize` окна ставит и снимает модуль.
  - `dialog-drafts.js`: `AGENT_DRAFTS = 'studio-agent-drafts'`, `CHAT_DRAFTS = 'studio-chat-drafts'`,
    `draftStore(key) -> { saved(), save(id, text, receipt?), has(id) }` — один объект на ключ, `sessionStorage[key] =
    {scope: me.csrf, items: {id: {text, receipt?}}}`, чужой `scope` не читается и удаляется; смена «есть / нет» —
    событие `studio-dialog-draft` `{ store, id, draft }` на `document`; `studio-locked`, `studio-signed-out` и отправка
    `form[action="/logout"]` стирают оба ключа. Событие «Агентов» `studio-code-draft` (`code-chat.js:113`) остаётся.
  - `slashMenu(field, { items, onPick, host = null }) -> { close(), unmount() }`, `items()` → `Promise<[{kind:
    'skill' | 'project' | 'command', id, name, description}]>`; `skillChip(host, { changed } = {}) -> { node,
    set(item), clear(), value() }`. Пределы данных (поправка контрактов): имя ≤ 80, описание ≤ 200 знаков.

- [ ] **Step 1: Run (эталон «до»)** `… -m integration tests/test_ui_status_layout.py tests/test_ui_agent_controls.py
  tests/test_ui_code_progress.py tests/test_ui_chat_restoration.py tests/test_ui_page_guards.py` — Expected: PASS.
- [ ] **Step 2: Write the failing test** `tests/test_ui_shared_modules.py` (`integration`). Помощник `probe(browser,
  live, script, width=1280)` — `browser.drive(live.url + "/", [*signin(), ("wait", READY), ("eval", "v", script)],
  width=width, mobile=width == 320)["values"]["v"]`; сценарий импортирует модуль (`await import('/static/js/…')`) и
  кладёт узлы пробы в `#view`, как `test_ui_design_tokens.py`.

```python
def test_status_bar_takes_ids_from_options_and_overflows(browser, live):
    # проба 300 px: 6 кнопок действий, закреплённая button[data-status=ring], statusActions(…, { id: 'probe-status' })
    assert v["more"] is True and v["agent_ids"] == 0     # #probe-status-more виден; #code-status-more на странице нет
    assert v["moved"] >= 1 and v["labels"] is True       # лишние — в #probe-status-menu, у каждой видна подпись
    assert v["square"] is True and v["inside"] is True   # кольцо квадратное; ряд кнопок не шире 60 % строки
    assert v["old_module"] == 404                        # fetch('/static/js/code-status-bar.js').status

@pytest.mark.parametrize("width", [320, 1280])
def test_message_nav_walks_own_messages_and_toggles_steps(browser, live, width):
    # лента-проба 300 px, прокручена в конец: 6 пар .msg.user «Вопрос N» + .msg.assistant 400 px с details.web-steps;
    # messageNav({ prefix: 'chat', own: '.msg.user', steps: 'details.web-steps', … })
    assert v["ids"] == ["chat-message-up", "chat-toggle-steps", "chat-message-down"]
    assert [v["up"], v["up2"], v["down"]] == ["Вопрос 6", "Вопрос 5", "Вопрос 6"]   # верх сообщения у верха ленты ±3 px
    assert v["tail"] == ["В конец чата", True] and v["end"] is True   # своих ниже нет: подпись, кнопка жива; нажали — низ
    assert v["steps"] == [6, "true", 0, "false"]         # открыто details и aria-pressed; повтор — закрыто
    assert v["calls"] == {"act": 6, "follow": [False, False, False, True]} and v["inside"] is True   # nav.right <= innerWidth

def test_dialog_drafts_are_scoped_announced_and_cleared(browser, live):
    assert v["stored"] == {"scope": v["csrf"], "items": {"c1": {"text": "Черновик"}}}   # sessionStorage['studio-chat-drafts']
    assert v["events"] == [["studio-chat-drafts", "c1", True], ["studio-chat-drafts", "c1", False]]
    assert v["same"] is True and v["foreign"] == {}      # один объект на ключ; запись с другим scope не читается
    assert v["locked"] == [None, None]                   # studio-locked: оба ключа sessionStorage стёрты
```

  `tests/test_ui_page_guards.py::test_agent_drafts_leave_the_tab_with_the_emergency_exit` — импорт
  `/static/js/dialog-drafts.js`, запись через `draftStore` в оба ключа; после аварийного выхода оба — `None`.
- [ ] **Step 3: Run** `… -m integration tests/test_ui_shared_modules.py tests/test_ui_page_guards.py` — Expected: FAIL
  (модули `status-bar.js`, `message-nav.js`, `dialog-drafts.js` — 404).
- [ ] **Step 4: Implement.** `status-bar.js`: шестой параметр и `dataset.status`, остальное без правок. `dialog-drafts.js`:
  реестр `Map` ключ → хранилище, `current()` / `persist()` прежние на каждый ключ, общий `clear()` по списку обоих
  ключей; в `code-chat.js` — `const agentDrafts = draftStore(AGENT_DRAFTS)` вместо `saveDraft` / `savedDrafts` (:118,
  :122, :169), в `code.js:999` — так же; `app.js:45` — `import './dialog-drafts.js'`. `message-nav.js`: перенос кода
  как есть, `alive` — свой флаг модуля (гаснет в `unmount`). В `chatView` остаются `followTail`, `grow`, `trackScroll`,
  `tailObserver`; вызов — `const nav = messageNav({ scroller, feed, stuck, ready: () => !historyLoading, earlier: {
  has: () => start > 0, busy: () => more.disabled, load: () => loadEarlier() }, act: () => { interacted(); acted =
  Date.now(); }, follow: (on) => { followTail = on; }, change: grow })`; `navigation` → `nav.node`,
  `paintNavigation()` → `nav.paint()`, чтения `messageTarget` → `nav.target()`, :1978 → `nav.drop()`, :1982 →
  `nav.scrollBy(…)`, :1984 → `nav.pin()`, :887 → `nav.expanded()`, :2091 и :2096 → `nav.unmount()`.
- [ ] **Step 5: Run** файлы шагов 1 и 3, ещё `tests/test_ui_webkit.py tests/test_ui_code_quick_actions.py
  tests/test_ui_active_commands.py tests/test_ui_agent_reload.py tests/test_ui_chat_exports.py` и быстрые
  `tests/test_docs.py tests/test_static.py` — PASS; `test_ui_browser.py::test_code_closed_at_send_keeps_the_draft`
  по-прежнему видит один ключ `session:studio-agent-drafts`.
- [ ] **Step 6: Commit** `Share the status bar, message arrows and dialog drafts between sections`.
- [ ] **Step 7: Write the failing test** `tests/test_ui_slash.py` (`integration`; Review Focus 5 — 320 px, английский):

```python
ITEMS = [{"kind": "skill", "id": "s1", "name": "Разобрать по частям", "description": "План и части"},
         {"kind": "skill", "id": "s2", "name": "Н" * 80, "description": "о" * 200},          # пределы навыка
         {"kind": "project", "id": ".agents/skills/spec", "name": "spec", "description": "Подготовить ТЗ"},
         {"kind": "command", "id": "init", "name": "init", "description": ""}]

@pytest.mark.parametrize("width,lang", [(320, "en"), (1280, "ru")])
def test_slash_menu_filters_picks_and_fits(browser, live, width, lang):
    # проба: form.composer в #view с fieldBox(textarea#probe-text) и enterSends(field, () => window.sent += 1);
    # slashMenu(field, { items: async () => ITEMS, onPick: (i) => window.picked = i }); skillChip(form)
    assert v["plain"] is True and v["open"] == [4, "true"]     # «привет» — закрыто; «/» первым знаком — 4 строки, aria-expanded
    assert v["kinds"] == (["skill", "skill", "project", "command"] if lang == "en" else ["навык", "навык", "проект", "команда"])
    assert v["filtered"] == ["spec"] and v["middle"] is True   # «/sp»; «текст /sp» и «/sp текст» — закрыто
    assert v["fits"] is True     # меню и каждая строка в [0, innerWidth], NO_OVERFLOW, имя из 80 знаков — с многоточием
    assert v["picked"] == ["skill", "s1"] and v["after"] == "" and v["sent"] == 0   # «/раз», Enter: выбор, не отправка
    assert v["chip"] == [("Skill: " if lang == "en" else "Навык: ") + "Разобрать по частям", "s1"]   # set(item), value().id
    assert v["cleared"] == [True, None]                        # крестик чипа: скрыт, value() — null, changed() вызван
    assert v["escaped"] == [True, "/ра", True] and v["reopened"] is True   # Esc: закрыто, текст цел, «/раз» — закрыто; заново «/» — открыто
```

- [ ] **Step 8: Run** `… -m integration tests/test_ui_slash.py` — Expected: FAIL (`slash.js` — 404).
- [ ] **Step 9: Implement** `slash.js`. Меню — блок в потоке над полем, не popover (не уходит за экран): `div
  .slash-menu#slash-menu[hidden]` → `div[role=listbox]#slash-options` («Навыки и команды») + `p.small.muted
  [role=status]`; вставляется перед `field.closest('.field-box')` (или в `host`). Строка — `button.slash-option
  [role=option]#slash-option-N[aria-selected][data-kind][data-id]`: `span.slash-name` (многоточие, `title` — полное
  имя), `span.tag.slash-kind` («навык» / «проект» / «команда»), `span.slash-desc.muted`. Открыто, пока
  `field.value.slice(0, selectionStart)` подходит под `/^\/(\S*)$/`; запрос — группа 1, отбор —
  `name.toLocaleLowerCase().includes(запрос)`, порядок — как пришёл; `items()` зовётся раз на открытие, отказ — слова в
  строке статуса. Клавиши — `keydown` в фазе захвата с `stopImmediatePropagation()` (раньше `enterSends` и
  `fileHints`): ↓ / ↑ по кругу, Enter и Tab — взять, Esc — закрыть и не открывать, пока начало поля не перестанет
  подходить; `event.isComposing` — мимо. Взять: убрать из поля `/^\/\S*[ \t]*/`, событие `input`, `onPick(item)`
  (команду хозяин вставит сам). ARIA поля — как у `fileHints` (`aria-controls`, `aria-expanded`,
  `aria-activedescendant`). `skillChip`: `div.skill-chip#skill-chip[hidden]` → `span.skill-chip-name` («Навык: {0}»,
  многоточие) + `iconButton('x', tr("Убрать навык"))`. CSS: рамка и `max-height: calc(22 * var(--dvh))` как
  `.code-file-hints` (`app.css:2155`); строка — сетка `minmax(0, auto) auto minmax(0, 1fr)`, до 420 px описание —
  второй строкой; `:hover` — в `@media (hover: hover)`; на сенсоре строка не ниже `var(--h-touch)`.
- [ ] **Step 10: Run** `tests/test_ui_slash.py` (`integration`) и быстрые `tests/test_i18n.py tests/test_css_tokens.py
  tests/test_sections_meta.py` — PASS.
- [ ] **Step 11: Commit** `Add the shared slash menu and skill chip for composers`.

### Task 8: Инструменты MCP

Пункты 32 и 33 спеки (п. 36 сюда не входит: ожидание карты, очередь и оценка на сервере уже есть —
`StreamTask._wait_card`, `app/chat.py:973–992`, ветка `BUSY` `:1616–1624`; события `card` и прогрева нет). Имя
инструмента MCP для модели — `mcp_{id сервера}_{номер}` (`app/mcp_client.py:737`), номер — сквозной по всем
инструментам хода: выключили один инструмент или не ответил сервер из начала набора — имена остальных сдвинулись и
начало запроса изменилось. Описание режется по 4000 знаков (`:742`), предел числа — `profiles.max_tools`, по
умолчанию 128 (`:707`): локальной модели это десятки тысяч токенов описаний. Вызов исполняется только при
`finish_reason == "tool_calls"` (`app/chat.py:1635`), хотя слабые шлюзы кончают структурный вызов словом `stop`, а
локальные модели без разборщика vLLM пишут вызов текстом. Ход и пауза находят инструмент по паре «сервер, имя»
(`StreamTask._seen_key`, `:1441`; `chat_pending._target`), а не по имени функции — смена имён их не задевает. В
тестах прежнее имя стоит 77 раз в 15 файлах.

**Files:**
- Modify: `app/mcp_client.py` — константы `CALL_MAX`, `SERVER_PART`, `LOCAL_MAX_TOOLS`, `DESCRIPTION_MAX`,
  `LOCAL_DESCRIPTION_MAX`, `TRANSLIT`; новая `tool_name`; `Tools.__init__` (параметр `model`), `Tools.discover`
  (`:702–748`), `web_tools` (`:816`). `app/profiles.py::max_tools` (`:143`).
- Modify: `app/chat.py` — `StreamTask._read` (`:1511`) и `_turn_specs` (`:2739`): `Tools(..., model=…)`;
  `StreamTask._read_model` — только строки `:1630–1650`; новые `TOOL_CALL_RE`, `_text_calls` рядом с `ToolCall`
  (`:856`); слова в строке документации `_seen_key`. `compose`, `_compact`, `estimate`, `create_turn` не трогать —
  их правит задача 7.
- Create: `tests/test_mcp_names.py`, `tests/test_chat_weak_calls.py`.
- Modify (имена): `tests/test_mcp_dialog.py:106` (`name_of`), `tests/test_access_groups.py:435`,
  `tests/test_chat_archive.py:30`, `tests/test_chat_pending.py`, `tests/test_chat_tools_rounds.py`,
  `tests/test_cleaner_contract.py:202`, `tests/test_cleaner_route_chat.py` (и новый тест-сторож),
  `tests/test_cleaner_web.py`, `tests/test_mcp.py`, `tests/test_mcp_chat_files.py`, `tests/test_protected_source.py`,
  `tests/test_shared_integrations.py:96`, `tests/test_ui_chat_files.py:138`, `tests/test_ui_chat_mcp.py:167,222,265`,
  `tests/test_ui_chat_pending.py:72`.

**Interfaces:**
- Consumes: `sections.is_local(conn, model)` — как есть (после задачи 9 — и шлюз s06).
- Produces:
  - `mcp_client.tool_name(server: str, tool: str, taken=()) -> str` — `mcp_<сервер>__<инструмент>`, не длиннее
    `CALL_MAX = 64`, знаки `[A-Za-z0-9_-]`; `SERVER_PART = 24`.
  - `mcp_client.LOCAL_MAX_TOOLS = 32`, `mcp_client.DESCRIPTION_MAX = 4000`, `mcp_client.LOCAL_DESCRIPTION_MAX = 1000`.
  - `mcp_client.Tools(uid, chat_id, ids, *, explicit=(), conn=None, model=None)`;
    `profiles.max_tools(conn, model=None) -> int`.
  - `chat._text_calls(text: str) -> tuple[str, list[ToolCall]]`.
  - Тестам: `name_of(sid, tool="lookup")` (`tests/test_mcp_dialog.py`) — имя по серверу из базы и инструменту.

**Решения:**
- **Имя.** Сервер: нижний регистр, кириллица — по таблице `TRANSLIT` (33 буквы: ж→zh, х→h, ц→c, ч→ch, ш→sh, щ→sch,
  ю→yu, я→ya, й и ы→y, ё и э→e, ъ и ь — пусто, прочие — по звучанию), каждая серия знаков вне `[a-z0-9]` — один `_`,
  `_` по краям прочь, первые 24 знака; пусто — `server`. Инструмент: знаки вне `[A-Za-z0-9_-]` — `_`, регистр как у
  сервера MCP. Целое — `f"mcp_{сервер}__{инструмент}"[:64]`; занято в `taken` — хвост `_2`, `_3`… (основа короче на
  длину хвоста). `discover` зовёт `tool_name(item["name"], tool["name"], self.calls)`: совпадения нумеруются по
  порядку набора. Приставка `mcp_` остаётся — по ней инструменты MCP отличают тесты.
- **Пределы локальной модели.** `profiles.max_tools(conn, model)`: `max_tools` профиля, если задан; иначе
  `LOCAL_MAX_TOOLS` при `model is not None and sections.is_local(conn, model)`; иначе `MAX_TOOLS` (128). Описание в
  `discover` — `LOCAL_DESCRIPTION_MAX` знаков локальной модели, иначе `DESCRIPTION_MAX`. `_read` и `_turn_specs`
  передают модель хода, `web_tools` — `row["model"]`. Заметка об урезанном списке — прежняя (`TRIMMED.format(cap)`).
- **Структурный вызов** исполняется при любом признаке конца, кроме его отсутствия и `length` (обрыв — вызов мог
  прийти не весь; его ведёт ветка `LENGTH_RETRIES`, сторож — `tests/test_chat.py::
  test_length_retries_without_long_reasoning_or_executing_partial_tool_calls`): условие `:1635` —
  `offer and calls and ending not in (None, "length")`; итоговая проверка `:1649` —
  `calls and ending != "length" and not text.strip()` → `NO_ANSWER`.
- **Вызов текстом** — перед условием `:1635`, только когда: `offer`, структурных вызовов нет, `ending` не `None` и не
  `length`, в тексте круга есть `<tool_call>`, `sections.is_local(self.conn, self.model)` и **не**
  `(self.conn.cleaner or self.conn.routed)` (решение 13 спеки: Cleaner заменяет только структурные вызовы).
  `_text_calls`: блоки `TOOL_CALL_RE = <tool_call>(.*?)</tool_call>` (`re.S`); тело блока — объект JSON с `name`
  (строка 1–128 знаков) → `ToolCall("", name, …)`, аргументы: словарь — `json.dumps(…, ensure_ascii=False)`, строка —
  как есть, нет ключа — `"{}"`, иное — `json.dumps`; не JSON, но имя находится по `"name"\s*:\s*"([^"]{1,128})"` —
  вызов с телом блока вместо аргументов (инструмент сам откажет словами); имени нет — блок остаётся текстом. Итог —
  текст без разобранных блоков (`strip`) и вызовы. Нашлись вызовы: хвост `self.content` длиной `len(text)`
  заменяется очищенным текстом, странице — событие `replace {text: self.content}` (его уже понимает `chat.js:307`),
  дальше круг идёт как структурный (`_tools`: id вызова `call_<n>_<i>`, битые аргументы в запрос не возвращаются).
- **Неразобранные аргументы** — уже шаг-ошибка инструмента, а не ошибка хода (`web.call`, `mcp_client.Tools.call`,
  `chat_tools`; сторож — `tests/test_chat_web.py::test_broken_or_unknown_calls_are_error_steps_and_the_model_goes_on`);
  новое — то же для вызова текстом.

- [ ] **Step 1: Write the failing tests** `tests/test_mcp_names.py` (`HOST`, `server`, `setup`, `tool` —
  `test_mcp_dialog`; `messages`, `new_chat`, `sse`, `turn` — `test_chat`; `mcp_tools(request)` — описания функций
  запроса, чьё имя начинается с `mcp_`):

```python
def test_tool_name_is_built_from_the_server_and_the_tool():
    assert mcp_client.tool_name("Shared tools", "lookup") == "mcp_shared_tools__lookup"
    assert mcp_client.tool_name("Почта отдела", "mail.send:v2") == "mcp_pochta_otdela__mail_send_v2"
    assert mcp_client.tool_name("🙂", "getItem") == "mcp_server__getItem"
    long = mcp_client.tool_name("S" * 80, "t" * 128)
    assert len(long) == mcp_client.CALL_MAX == 64 and long.startswith("mcp_" + "s" * 24 + "__t")
    assert mcp_client.tool_name("A b", "t", {"mcp_a_b__t"}) == "mcp_a_b__t_2"

def test_names_stay_when_another_tool_is_switched_off(studio):
    sid, _, _ = setup(studio, tools=[tool("a"), tool("b"), tool("c")]); cid = new_chat(studio)
    sse(turn(studio, cid))
    before = [t["name"] for t in mcp_tools(studio.gw.chat_requests[-1])]
    assert before == ["mcp_shared_tools__a", "mcp_shared_tools__b", "mcp_shared_tools__c"]
    assert studio.c.patch(f"/api/chats/{cid}", json={"mcp_tools_off": {str(sid): ["a"]}}).status_code == 200
    sse(turn(studio, cid, "Ещё"))
    assert [t["name"] for t in mcp_tools(studio.gw.chat_requests[-1])] == before[1:]     # было: mcp_<sid>_0, _1

def test_local_model_gets_32_tools_with_1000_char_descriptions_unless_the_profile_says_more(studio):
    gw = add_gateway(studio); cid = new_chat(studio)                    # local-gemma-abl — локальная
    server(studio, "/mine", tools=[tool(f"t{n}", description="д" * 3000) for n in range(40)])
    mine = studio.c.post("/api/mcp", json={"name": "Mine", "url": HOST + "/mine"}).json()["id"]
    assert studio.c.patch(f"/api/chats/{cid}", json={"mcp_ids": [mine]}).status_code == 200
    sse(turn(studio, cid))
    sent = mcp_tools(studio.gw.chat_requests[-1])
    assert len(sent) == mcp_client.LOCAL_MAX_TOOLS == 32
    assert {len(t["description"]) for t in sent} == {mcp_client.LOCAL_DESCRIPTION_MAX} == {1000}
    assert messages(studio, cid)[1]["tools"] == [{"kind": "mcp", "server": "Mine", "note": mcp_client.TRIMMED.format(32)}]
    assert studio.c.put(f"/api/connections/{gw}/profile", json={"params": {"max_tools": 64}}).status_code == 200
    sse(turn(studio, cid, "Ещё"))
    assert len(mcp_tools(studio.gw.chat_requests[-1])) == 40            # `max_tools` профиля сильнее
    # профиль убран (PUT {}), модель диалога — облачная роль того же шлюза `gemma4-abl` (связь раздела, PATCH)
    sse(turn(studio, cid, "И ещё"))
    cloud = mcp_tools(studio.gw.chat_requests[-1])                      # облачной роли — как было
    assert len(cloud) == 40 and all(3000 < len(t["description"]) <= mcp_client.DESCRIPTION_MAX for t in cloud)
```

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -n0 --assert=rewrite tests/test_mcp_names.py` — Expected:
  FAIL (`module 'mcp_client' has no attribute 'tool_name'`).
- [ ] **Step 3: Implement** `tool_name`, константы, `Tools(model=)`, `discover`, `profiles.max_tools`, три вызова
  `Tools(...)`. В тестах: `name_of(sid, tool="lookup")` = `mcp_client.tool_name(<имя сервера sid из mcp_servers>,
  tool)`; каждое `name_of(sid, n)` и `f"mcp_{sid}_{n}"` заменить именем по инструменту из набора того же теста
  (`name_of(sid, "translate")`); в браузерных тестах (база живой службы) — `mcp_client.tool_name("<имя сервера>",
  "<инструмент>")`, отбор `startswith(f"mcp_{sid}_")` в `tests/test_ui_chat_mcp.py` — по приставке
  `mcp_client.tool_name("<имя сервера>", "")`.
- [ ] **Step 4: Run** `tests/test_mcp_names.py tests/test_mcp.py tests/test_mcp_dialog.py tests/test_mcp_chat_files.py
  tests/test_chat_pending.py tests/test_chat_tools_rounds.py tests/test_protected_source.py tests/test_cleaner_web.py
  tests/test_cleaner_route_chat.py tests/test_access_groups.py tests/test_shared_integrations.py
  tests/test_chat_archive.py tests/test_connection_profiles.py` — PASS; затем
  `.venv/Scripts/python -m pytest -q -n0 -m integration tests/test_ui_chat_mcp.py tests/test_ui_chat_pending.py
  tests/test_ui_chat_files.py tests/test_cleaner_contract.py` — PASS (`test_cleaner_contract.py` — при
  `cleaner/.venv` или `CLEANER_TEST_REPO`).
- [ ] **Step 5: Commit** `Give MCP tools stable readable names and smaller sets for local models`.
- [ ] **Step 6: Write the failing tests** `tests/test_chat_weak_calls.py` (`call`, `patch`, `web_chat` —
  `test_chat_web`) и сторож в `tests/test_cleaner_route_chat.py` (его фикстура `cleaner_gw`, `routed_chat`, `web_on`):

```python
TEXT_CALL = 'Ищу.\n<tool_call>\n{"name": "web_search", "arguments": {"query": "версия Python"}}\n</tool_call>'

def test_structured_call_runs_with_any_finish_reason(studio):
    cid = web_chat(studio)
    studio.gw.chat_script = [{"tool_calls": [call("web_search", query="q")], "ending": "stop"}, ["Нашёл."]]
    assert sse(turn(studio, cid))[-1] == ("done", {"status": "done"})
    assert len(studio.gw.exa_requests) == 1 and messages(studio, cid)[1]["content"] == "Нашёл."

def test_local_model_text_call_is_executed_and_leaves_the_answer(studio):
    cid = web_chat(studio); studio.gw.chat_script = [[TEXT_CALL], ["Python 3.14."]]    # local-gemma-abl
    events = sse(turn(studio, cid))
    assert studio.gw.exa_requests[0][1]["query"] == "версия Python" and ("replace", {"text": "Ищу."}) in events
    assert messages(studio, cid)[1]["content"] == "Ищу.\n\nPython 3.14."
    assistant, result = studio.gw.chat_requests[1]["messages"][-2:]
    assert assistant["content"] == "Ищу." and result["role"] == "tool"
    assert assistant["tool_calls"][0]["function"] == {"name": "web_search", "arguments": '{"query": "версия Python"}'}

def test_text_call_with_broken_arguments_is_a_tool_refusal_not_a_turn_error(studio):
    cid = web_chat(studio)
    studio.gw.chat_script = [['<tool_call>{"name": "web_search", "arguments": {"query": </tool_call>'], ["Не вышло."]]
    assert sse(turn(studio, cid))[-1] == ("done", {"status": "done"})
    answer = messages(studio, cid)[1]
    assert (answer["status"], answer["content"]) == ("done", "Не вышло.") and answer["tools"][0]["error"]
    assert studio.gw.exa_requests == []
    assert studio.gw.chat_requests[1]["messages"][-1]["content"].startswith("Ошибка инструмента: ")

def test_text_call_stays_text_for_a_cloud_role(studio):
    cid = web_chat(studio)    # затем модель диалога — облачная роль `gemma4-abl` того же шлюза (связь раздела, patch)
    studio.gw.chat_script = [[TEXT_CALL]]
    assert sse(turn(studio, cid))[-1] == ("done", {"status": "done"})
    assert messages(studio, cid)[1]["content"] == TEXT_CALL and studio.gw.exa_requests == []
    assert len(studio.gw.chat_requests) == 1

# tests/test_cleaner_route_chat.py — сторож решения 13: через Cleaner текст не разбирается никогда
def test_text_tool_call_is_never_parsed_through_cleaner(cleaner_gw, monkeypatch):
    s, gw = cleaner_gw
    monkeypatch.setattr(sections, "is_local", lambda conn, model: True)   # даже если признак «локальная» истинен
    chat_id = routed_chat(s, gw); web_on(s, chat_id); gw.script = [[TEXT_CALL]]
    assert sse(turn(s, chat_id, "Найди"))[-1] == ("done", {"status": "done"})
    assert len(gw.turns) == 1 and s.gw.exa_requests == []
    answer = messages(s, chat_id)[-1]
    assert answer["content"] == TEXT_CALL and answer["tools"] == []
```

- [ ] **Step 7: Run** `.venv/Scripts/python -m pytest -q -n0 --assert=rewrite tests/test_chat_weak_calls.py` —
  Expected: FAIL (первый тест: Exa не вызван, ответ пуст — `NO_ANSWER`; второй: `<tool_call>` в ответе).
- [ ] **Step 8: Implement** `TOOL_CALL_RE`, `_text_calls` и правку `_read_model` по «Решениям». **Run**
  `tests/test_chat_weak_calls.py tests/test_cleaner_route_chat.py tests/test_chat.py tests/test_chat_web.py
  tests/test_chat_tools_rounds.py tests/test_chat_reasoning.py tests/test_i18n_server.py`, затем быстрый набор — PASS.
- [ ] **Step 9: Commit** `Run tool calls that weak models end with stop or write as text`.

## Волна B

### Task 4: `usage` и длительность ответа

Сервер читает из потока модели только `choices[].delta` (`app/chat.py:1189–1212`), кусок `usage` пропускает; запрос
собирают `StreamTask._read_model` (`:1601`) и `_compact` (`:1280`) без `stream_options`. Итог ответа пишет `_finish`
(`:524`), данные события `done` — `_ended_with` (`:1823`), JSON сообщения — `_message_out` (`:447`); продолжение
после паузы — новый `StreamTask` (`app/chat_pending.py:275–298`); ответ прямого подключения кладёт `put_answer`
(`:3026`, тело `AnswerIn` — `:282`). В тестах 129 утверждений в 21 файле сравнивают `("done", {"status": "done"})`
целиком — новые ключи события по умолчанию убирает помощник `events_of` (`tests/test_chat.py:25`): время хода в
тестах двигают пинги, с ним сравнения стали бы случайными. Шлюз Cleaner параметр знает и кусок `usage` пропускает
(`cleaner/core/agent/chat.py:26, 372`) — ход через `cleaner_route` получает его как все.

**Files:**
- Modify: `app/chat.py` — константы `NO_USAGE`, `USAGE_MARKS`, `USAGE_MAX`, `MS_MAX`, `RATIO_MIN_TOKENS`,
  `RATIO_RANGE`, `RATIO_BLEND`; `AnswerIn` и новый `UsageIn`; `_message_out`; `_finish`; `StreamTask` (поля,
  `metrics`); `StreamTask._round`; `StreamTask.run`; `_cancel`; `_ended_with`; `_ended`; `put_answer`; новые функции
  `_usage_of`, `request_chars`, `note_usage` — сразу после `spec_tokens` (`:2728`).
- Modify: `app/chat_pending.py:275–298`; `tests/fakes.py` (`FakeGateway.chat_usage`); `tests/conftest.py:322`
  (`chat.NO_USAGE.clear()` рядом с `chat.NO_TOOLS.clear()`); `tests/test_chat.py:25–43` (`events_of`, `sse`).
- Create: `tests/test_chat_usage.py`.

**Interfaces:**
- Consumes: задача 1 — `messages.usage TEXT`, `messages.gen_ms INTEGER`, `messages.total_ms INTEGER`,
  `model_facts(connection_id, model, ratio, window, updated)`.
- Produces:
  - JSON сообщения (`chat._message_out`) и событие SSE `done`: `usage: {in, out, cached, reasoning} | null` (ключа
    нет — шлюз его не назвал), `gen_ms: int | null`, `total_ms: int | null`; у `done` три ключа есть всегда.
  - `chat.NO_USAGE: set[int]` — id подключений, отказавших параметру; только память процесса.
  - `chat.request_chars(payload: dict) -> int | None` — знаки запроса: текст сообщений, аргументы `tool_calls` и
    `json.dumps(payload["tools"], ensure_ascii=False)`; в запросе есть картинка — `None`.
  - `chat.note_usage(conn_id: int, model: str, chars: int, usage: dict) -> None` — пишет `model_facts.ratio`.
  - `StreamTask.metrics() -> {"usage": dict | None, "gen_ms": int | None, "total_ms": int}`.
  - `PUT /api/chats/{chat_id}/messages/{message_id}` принимает `usage`, `gen_ms`, `total_ms` (шлёт задача 6).
  - Тестам: `events_of(lines, metrics=False)` и `sse(response, metrics=False)` — без `metrics` у события `done` нет
    ключей `usage`, `gen_ms`, `total_ms`; `FakeGateway.chat_usage: dict | None` и ключ `usage` шага `chat_script`.

**Решения:**
- Ключи: `in` ← `prompt_tokens`, `out` ← `completion_tokens`, `cached` ← `prompt_tokens_details.cached_tokens`,
  `reasoning` ← `completion_tokens_details.reasoning_tokens`; берётся только `int >= 0` (не `bool`). Кусок `usage`
  круга — последний непустой (vLLM шлёт его отдельным куском с `choices: []`, иные шлюзы — в каждом).
- У ответа — **сумма по кругам** с инструментами (расход, а не размер контекста). Служебные круги (`silent`:
  пересказ) в `usage` и `gen_ms` ответа не входят, но `note_usage` зовут.
- `gen_ms` — сумма по кругам «первый фрагмент (текст, рассуждение или вызов) → конец потока» по `clock.NOW()`; ни
  одного фрагмента — `null`. `total_ms` — от создания `StreamTask` до итога; продолжение после паузы прибавляет к
  сохранённому (ожидание человека не считается). Ход модели MCP и прежний Cleaner (`conn.cleaner`) — только `total_ms`.
- Параметр `stream_options: {"include_usage": true}` — в каждый потоковый запрос подключения, которого нет в
  `NO_USAGE`. Отказ — ответ 400 или 422, чей текст в нижнем регистре содержит `stream_options` или `include_usage`
  (`USAGE_MARKS`): id подключения — в `NO_USAGE`, тот же запрос **один раз** без параметра, в журнал — только id.
- `note_usage`: пропуск при `usage["in"] < RATIO_MIN_TOKENS` (500) или `chars <= 0`; новое отношение
  `chars / usage["in"]` в пределах `RATIO_RANGE = (1.5, 6.0)`; при прежнем значении `old` в базу идёт
  `old + RATIO_BLEND * (new - old)`, `RATIO_BLEND = 0.3`; `updated = clock.NOW()`; столбец `window` не трогает.
- Прямое подключение (решение 19 спеки): `UsageIn` — `extra="forbid"`, поля `in` (alias; имя поля `in_`), `out`,
  `cached`, `reasoning`: `int | None`, `strict`, `0 … USAGE_MAX` (`100_000_000`); `gen_ms`, `total_ms`:
  `int | None`, `strict`, `0 … MS_MAX` (`86_400_000`, сутки). Повтор того же итога (`:3036`) числа не переписывает.

- [ ] **Step 1: Write the failing tests** `tests/test_chat_usage.py` (`add_gateway`, `add_connection` — `conftest`;
  `messages`, `new_chat`, `sse`, `turn` — `test_chat`; `call`, `web_chat` — `test_chat_web`; фикстура `no_pings`):

```python
USAGE = {"prompt_tokens": 1200, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 1000},
         "completion_tokens_details": {"reasoning_tokens": 7}}

def test_usage_and_timings_are_stored_and_sent(studio, no_pings):
    gw = add_gateway(studio); cid = new_chat(studio); studio.gw.chat_usage = USAGE
    # обёртка studio.gw.on["/v1/chat/completions"] двигает studio.clock на 1.0 и зовёт studio.gw.builtin;
    # функция studio.gw.chat_hold двигает часы на 2.5 и возвращает False (между первым и вторым куском ответа)
    events = sse(turn(studio, cid, "я" * 3000), metrics=True)
    sent = studio.gw.chat_requests[0]
    assert sent["stream_options"] == {"include_usage": True}
    want = {"usage": {"in": 1200, "out": 50, "cached": 1000, "reasoning": 7}, "gen_ms": 2500, "total_ms": 3500}
    assert events[-1] == ("done", {"status": "done"} | want)
    user, answer = messages(studio, cid)
    assert {k: answer[k] for k in want} == want and (user["usage"], user["gen_ms"], user["total_ms"]) == (None,) * 3
    ratio = state.DB.execute("SELECT ratio FROM model_facts WHERE connection_id = ? AND model = ?",
                             (gw, "local-gemma-abl")).fetchone()[0]
    assert ratio == pytest.approx(chat.request_chars(sent) / 1200) and 1.5 <= ratio <= 6.0

def test_gateway_without_usage_leaves_the_fields_empty(studio, no_pings):          # Review Focus 2
    add_gateway(studio); cid = new_chat(studio)
    assert sse(turn(studio, cid), metrics=True)[-1] == (
        "done", {"status": "done", "usage": None, "gen_ms": 0, "total_ms": 0})
    assert messages(studio, cid)[1]["usage"] is None
    assert state.DB.execute("SELECT count(*) FROM model_facts").fetchone()[0] == 0
    assert sse(turn(studio, cid, "Ещё"))[-1] == ("done", {"status": "done"})        # без metrics — прежний вид

def test_rejected_stream_options_retries_once_and_is_remembered_until_restart(studio):   # Review Focus 2
    gw = add_gateway(studio); cid = new_chat(studio); seen = []
    # обёртка on["/v1/chat/completions"]: тело — в seen; в теле есть "stream_options" — ответ 400
    # {"error": {"message": "Extra inputs are not permitted: stream_options"}}, иначе studio.gw.builtin
    assert sse(turn(studio, cid))[-1] == ("done", {"status": "done"})
    assert ["stream_options" in b for b in seen] == [True, False] and gw in chat.NO_USAGE
    assert {k: v for k, v in seen[0].items() if k != "stream_options"} == seen[1]       # тот же запрос, с tools
    answer = messages(studio, cid)[1]
    assert (answer["content"], answer["usage"]) == ("Здравствуйте!", None)
    sse(turn(studio, cid, "Ещё"))
    assert len(seen) == 3 and "stream_options" not in seen[2]                           # подключение запомнено
    chat.NO_USAGE.clear()                                                               # перезапуск службы
    sse(turn(studio, cid, "И ещё"))
    assert ["stream_options" in b for b in seen[3:]] == [True, False]

def test_usage_sums_the_rounds_of_one_answer(studio):
    cid = web_chat(studio); studio.gw.chat_usage = {"prompt_tokens": 100, "completion_tokens": 10}
    studio.gw.chat_script = [{"tool_calls": [call("web_search", query="q")]}, ["Готово."]]
    assert sse(turn(studio, cid), metrics=True)[-1][1]["usage"] == {"in": 200, "out": 20}
    assert state.DB.execute("SELECT count(*) FROM model_facts").fetchone()[0] == 0      # 100 токенов < 500
```

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -n0 --assert=rewrite tests/test_chat_usage.py` — Expected:
  FAIL (`sse() got an unexpected keyword argument 'metrics'`, затем `KeyError: 'stream_options'`).
- [ ] **Step 3: Implement.**
  - `tests/test_chat.py`: `events_of` у события `done` без `metrics` отдаёт данные без трёх ключей; `sse` передаёт
    флаг. `tests/fakes.py::FakeGateway._chat`: `usage` шага-словаря, иначе `self.chat_usage`; не `None` и в теле
    запроса `stream_options.include_usage` — кусок `{"choices": [], "usage": usage}` между куском `finish_reason` и
    `[DONE]`.
  - `StreamTask`: поля `usage: dict` (пустой), `gen_sec: float | None = None`, `spent_ms: int = 0`,
    `began: float = field(default_factory=lambda: clock.NOW())`; `metrics()` — `usage or None`,
    `None if gen_sec is None else round(gen_sec * 1000)`, `spent_ms + round((clock.NOW() - began) * 1000)`.
  - `StreamTask._round` (имя не менять — его сторожит `tests/test_i18n_server.py::WORD_FUNCTIONS`): в шлюз уходит
    `sent` — `payload` с `stream_options`, если `self.conn.id not in NO_USAGE`. Ветка не-200, сразу после отказа
    самого Cleaner (`own is not None`, `:1147–1153`) и **до** ветки `builtin_only` (`:1155`; иначе «extra inputs are
    not permitted» приняли бы за отказ инструментам): параметр слали, код 400 или 422, в тексте есть `USAGE_MARKS` —
    `NO_USAGE.add(self.conn.id)`, `log.info` с id диалога, ответа и подключения,
    `return await self._round(payload, offer, builtin_only, silent=silent)`. В цикле кусков (после `chunk["error"]`,
    `:1185`): `usage = _usage_of(chunk["usage"])`, если это словарь; начало генерации — время первого куска с
    рассуждением, текстом или `tool_calls`. В `finally` круга без `silent`: `self.gen_sec = (self.gen_sec or 0.0) +
    clock.NOW() - начало`. Перед итоговым `return` (`:1234`): без `silent` — сложить `usage` в `self.usage` по
    ключам; при непустом `usage` и `request_chars(sent)` не `None` — `note_usage(self.conn.id, self.model, …)`.
  - `_finish(..., error=None, *, usage=None, gen_ms=None, total_ms=None)` пишет три столбца (`usage` — JSON или
    NULL). `StreamTask.run` (`:1735–1749`) и `_cancel` (`:1781–1786`) передают `**self.metrics()` в `_finish` и в
    `_ended_with(status, reasoning_sec, metrics=None)`. `_ended` (`:1898`) шлёт три ключа со значением `None`.
  - `_message_out`: `"usage": json.loads(row["usage"]) if row["usage"] else None`, `"gen_ms": row["gen_ms"]`,
    `"total_ms": row["total_ms"]` (идущий ответ — из строки, то есть `null`).
  - `app/chat_pending.py:275`: выборка ещё `usage, gen_ms, total_ms`; в `StreamTask(...)` — `usage=json.loads(
    message["usage"] or "{}")`, `gen_sec=None if message["gen_ms"] is None else message["gen_ms"] / 1000`,
    `spent_ms=message["total_ms"] or 0`.
- [ ] **Step 4: Run** `tests/test_chat_usage.py tests/test_chat.py tests/test_chat_pending.py tests/test_chat_web.py
  tests/test_cleaner_route_chat.py tests/test_i18n_server.py` — PASS.
- [ ] **Step 5: Commit** `Store token usage and answer timings of Studio chat turns`.
- [ ] **Step 6: Write the failing test** `tests/test_chat_usage.py::test_direct_answer_brings_its_own_numbers`:

```python
add_connection(studio); cid = new_chat(studio)                      # прямое подключение: модель читает браузер
aid = turn(studio, cid, "Привет").json()["assistant_message_id"]
url, body = f"/api/chats/{cid}/messages/{aid}", {"content": "Здравствуйте!", "status": "done"}
for bad in ({"usage": {"in": -1}}, {"usage": {"in": 100_000_001}}, {"usage": {"tokens": 1}}, {"usage": {"out": 1.5}},
            {"usage": {"in": True}}, {"gen_ms": -1}, {"gen_ms": "800"}, {"total_ms": 86_400_001}):
    assert studio.c.put(url, json=body | bad).status_code == 422, bad
sent = {"usage": {"in": 12, "out": 3}, "gen_ms": 800, "total_ms": 1500}
assert studio.c.put(url, json=body | sent).status_code == 200
assert {k: messages(studio, cid)[1][k] for k in sent} == sent
assert studio.c.put(url, json=body | {"gen_ms": 1}).status_code == 200 and messages(studio, cid)[1]["gen_ms"] == 800
```

- [ ] **Step 7: Run** — Expected: FAIL (422 нет: лишние поля `AnswerIn` пропускает). **Implement** `UsageIn`, поля
  `AnswerIn`, передачу в `_finish` из `put_answer` (`usage` — `model_dump(by_alias=True, exclude_none=True)`, пустой —
  NULL). **Run** `tests/test_chat_usage.py tests/test_chat.py`, затем быстрый набор
  `.venv/Scripts/python -m pytest -q` — PASS.
- [ ] **Step 8: Commit** `Accept usage and timings saved by the page for direct chat answers`.

### Task 5: Библиотека навыков — сервер, перенос быстрых действий, «Настройки → Навыки»

Навык — именованный текст учётки; модуль строится по `app/roles.py`: умолчания администратора засевает старт службы
(`seed_defaults`, `:176`; вызов — `app/main.py:126`), библиотеку — первое чтение языком страницы одной транзакцией
(`_copy`, `list_for`, `:232–254`), «Добавить умолчания» сверяет по `origin` (`:290`); маршруты администратора —
`app/admin.py:932–952`, страница — `rolesBlock`, `roleDefaultsBlock` (`roles.js:149`, `:172`; `settings.js:1889`,
`admin.js:977`). Быстрые действия: пределы 80 и 100 000 знаков (`app/account_profile.py:56–58`), список —
`users.code_quick_actions` (NULL — три `code_actions.DEFAULTS`, `[]` — панель выключена, `code_actions.for_account`
убирает прежний «Скачать ролик»), редактор — `code-quick-actions.js` (`settings.js:1852`), кнопки пустого чата
читают `me.code_quick_actions` (`code-chat.js:353–362`; на `/api/skills` их переводит задача 11). Сторожа: реестр
маршрутов блокировки (`tests/test_lock_policy.py:344`), карта модулей и «„Настройки“ → „…“» руководства
(`tests/test_docs.py:378`, `:448`), переводы (`tests/test_i18n.py`, `tests/test_i18n_server.py`).

**Files:**
- Create: `app/skills.py`, `app/static/js/skills.js`, `tests/test_skills.py`, `tests/test_ui_skills.py` (integration)
- Delete: `app/static/js/code-quick-actions.js`
- Modify: `app/main.py` (`skills.seed_defaults()` после `roles.seed_defaults()`, `include_router(skills.router)`),
  `app/admin.py`, `app/account_profile.py` (`_me`), `app/lock_policy.py` (`blocks`), `app/takeout.py` (`_settings`,
  `README`), `app/static/js/settings.js`, `app/static/js/admin.js`, `app/static/css/app.css`, `app/static/en.js`,
  `CLAUDE.md` (карта модулей: `skills`, `skills.js`), `docs/guide.ru.md` (строки 272, 1330–1333),
  `docs/architecture/agents-workspace.md` (абзац 492–498)
- Modify tests: `tests/test_lock_policy.py` (`ROUTES`), `tests/test_code_actions.py` (третий тест),
  `tests/test_ui_code_quick_actions.py`, `tests/test_ui_i18n.py:173–204`

**Interfaces:**
- Consumes: `skill_defaults`, `skills`, `users.skills_seeded` (Task 1); `code_actions.DEFAULTS`, `for_account`.
- Produces:
  - `skills.NAME_MAX = 80`, `DESCRIPTION_MAX = 200`, `TEXT_MAX = 100_000`, `MAX_SKILLS = 100`, `BODY_MAX = 1536 *
    1024`, `QUICK_KEYS = ("close-journals", "audit", "critics")`, `KEY = re.compile(r"[a-z][a-z0-9-]{0,39}")`; отказы
    `INPUT` (422 `input`), `FULL` (422 `skills`), `GONE` (404 `gone`), `ORDER` (409 `order`).
  - `skills.BUILTIN: dict[str, tuple[Builtin, ...]]` по языкам `ru`, `en`; `Builtin = namedtuple("Builtin", "key name
    description text scope quick parts")`; ключи по порядку: `spec`, `split`, `queue`, `acceptance`,
    `close-journals`, `audit`, `critics`, `parts`, `minutes` — латиница, цифры и дефис, без подчёркиваний (ключ —
    имя папки и поле `name` файла `SKILL.md`, задача 11). `origin` навыка учётки равен ключу умолчания, из которого
    он засеян: `builtin`, у умолчания администратора без него — `default-<id>`; у своего навыка — null.
  - `skills.get(user_id, skill_id) -> dict` — `{id, name, description, text, scope, quick, parts, origin}` (`quick`,
    `parts` — bool; чужой или нет — `HTTPException(404, GONE)`); `skills.list_for(user_id, lang, scope=None)` — без
    `text`, по `position`, первое чтение засевает; `skills.quick_actions(user_id) -> list[dict] | None` —
    `[{title, text}]` навыков `quick` области `agents|both`, None — библиотека не засеяна.
  - HTTP: `GET /api/skills?scope=chat|agents&lang=ru|en` → `{"items": [{id, name, description, scope, quick, parts,
    origin}]}` (`chat` — области `chat` и `both`, `agents` — `agents` и `both`; `lang` по умолчанию `ru`, нужен
    посеву); `GET /api/skills/{id}` → то же + `text`; `POST /api/skills` (201) и `PUT /api/skills/{id}` — тело
    `{name, description?, text, scope, quick?, parts?}` целиком; `PUT /api/skills/order {ids}` → `{"items"}` (не
    перестановка своих id — 409); `DELETE /api/skills/{id}` → 204; `POST /api/skills/defaults?lang=` → `{"items"}`;
    `/order` и `/defaults` объявить раньше `/{skill_id}`. Администратор: `GET`, `POST /api/admin/skills` (`?lang=`;
    тело + `lang`), `PUT`, `DELETE /api/admin/skills/{did}`; элемент — те же поля + `lang`, `builtin`, `position`.
  - `/api/me`: у засеянной учётки `code_quick_actions` = `skills.quick_actions(uid)`, `code_quick_actions_default` =
    `false`; у незасеянной — как раньше. Поля читают `code-chat.js` до задачи 11 (она переводит кнопки на
    `/api/skills`) и страница прежней выкладки; из `/api/me` и `MePatch` эта задача их не убирает. Страница —
    `#/settings/skills`.

- [ ] **Step 1: Write the failing tests** `tests/test_skills.py` (помощники `make_user`, `signin`, `erase_history` из
  `conftest`; посев «с нуля» — фикстура `schema_db`, как `tests/test_chat_roles.py:30`):

```python
KEYS = ["spec", "split", "queue", "acceptance", "close-journals", "audit", "critics", "parts", "minutes"]
ANY = {"spec": ["tasks/todo/", "tasks/index.md", "NeedInfo", "Todo"], "split": ["tasks/todo/"], "parts": ["todo_write",
       "ask_user", "run_part", "7"], "queue": ["tasks/index.md", "Todo", "NeedInfo", "Errors", "Done", "tasks/done/", "git add"]}
RU = {"spec": ["финализируй", "вариант", "рекоменд"], "queue": ["подзадач", "коммит", "новый чат"], "split": ["Цель",
      "Владение", "Зависимости", "Критерии приёмки", "Команда проверки", "Ограничения"],
      "acceptance": ["прошло", "не прошло", "неприменимо"], "minutes": ["Решения", "Поручения", "Открытые вопросы"]}
EN = {"spec": ["finalize", "option", "recommend"], "queue": ["subtask", "commit", "new chat"], "split": ["Goal",
      "Ownership", "Dependencies", "Acceptance criteria", "Check command", "Constraints"],
      "acceptance": ["passed", "failed", "not applicable"], "minutes": ["Decisions", "Action items", "Open questions"]}

def test_builtin_set_has_nine_skills_with_required_content():
    for lang, words in (("ru", RU), ("en", EN)):
        items = {b.key: b for b in skills.BUILTIN[lang]}
        assert list(items) == KEYS and all(skills.KEY.fullmatch(k) for k in KEYS)
        assert {k: b.scope for k, b in items.items()} == {**dict.fromkeys(KEYS[:7], "agents"), "parts": "chat", "minutes": "chat"}
        assert [k for k in KEYS if items[k].quick] == list(skills.QUICK_KEYS) and [k for k in KEYS if items[k].parts] == ["parts"]
        assert all(0 < len(b.name) <= 80 and len(b.description) <= 200 and 0 < len(b.text) <= 100_000 for b in items.values())
        assert [(k, w) for k in KEYS for w in ANY.get(k, []) + words.get(k, []) if w not in items[k].text] == []
    ru = {b.key: b for b in skills.BUILTIN["ru"]}                    # три быстрых — слово в слово прежние действия
    assert [(ru[k].name, ru[k].text) for k in skills.QUICK_KEYS] == [(a["title"], a["text"]) for a in code_actions.DEFAULTS]
    assert [b.name for b in skills.BUILTIN["en"][4:7]] == ["Resolve issues and ideas", "Deep project audit", "Independent critics"]
def test_first_list_seeds_once_in_page_language(env, schema_db, request):   # 9 навыков, origin == KEYS; skill_defaults — 18
    # строк и после второго seed_defaults(); второй GET — те же id; ?lang=en язык не меняет; ?lang=de, ?scope=x — 422
def test_scope_filter_and_text_only_by_id(studio):                  # ?scope=chat — 2 навыка, ?scope=agents — 7
    assert set(item) == {"id", "name", "description", "scope", "quick", "parts", "origin"} and "text" in by_id
def test_own_quick_actions_become_skills_once(studio):              # до первого чтения: PATCH /api/me code_quick_actions =
    custom = {"title": "Моё", "text": "Мой текст"}                  # [custom, code_actions.DEFAULTS[1], LEGACY_VIDEO_ACTION]
    quick = [s for s in get("/api/skills?scope=agents") if s["quick"]]
    assert [(s["name"], s["origin"]) for s in quick] == [("Моё", None), ("Глубокий аудит проекта", "audit")]
    assert names(get("/api/skills"))[:2] == ["Моё", "Глубокий аудит проекта"] and len(get("/api/skills")) == 2 + 6
    me = studio.c.get("/api/me").json()
    assert me["code_quick_actions"] == [custom, code_actions.DEFAULTS[1]] and me["code_quick_actions_default"] is False
    assert json.loads(state.DB.execute("SELECT code_quick_actions FROM users").fetchone()[0])[0] == custom     # не стёрт
    added = post("/api/skills/defaults")                            # вернулись только недостающие
    assert sorted(s["origin"] for s in added if s["quick"] and s["origin"]) == ["audit", "close-journals", "critics"]
def test_unseeded_and_empty_quick_actions(studio):    # до посева /api/me — code_actions.DEFAULTS и default True; у учётки
    # с `[]` после посева быстрых навыков нет и /api/me.code_quick_actions == []
def test_create_replace_reorder_delete_and_limits(studio):   # 422 `input`: имя 81 знак или в две строки, описание 201,
    # текст 100_001, пустые, NUL, scope "all", лишнее поле; ровно 80/200/100_000 — 201; сто первый — 422 `skills`,
    # «Добавить умолчания» не выходит за 100; PUT /order: перестановка видна в GET, неполный или чужой список — 409
def test_foreign_skill_is_404_and_admin_sees_only_defaults(studio):   # чужой id: GET, PUT, DELETE — 404 `gone`, skills.get
    # — HTTPException 404; GET /api/admin/skills?lang=ru — 9 строк с builtin == KEYS, навыков учёток нет; не админу — 403
def test_admin_defaults_reach_new_accounts_only(studio):            # как у ролей; умолчание администратора без builtin:
    assert seeded[-1]["origin"] == f"default-{created['id']}"       # библиотека прежней учётки не изменилась
def test_history_wipe_keeps_skills_and_takeout_contains_them(studio):   # erase_history → 9; settings.json["skills"] с text
def test_skill_text_is_not_logged(studio, caplog):                  # "secret-…" в имени, описании, тексте: в журнале — id[:8]
```

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -n0 --assert=rewrite tests/test_skills.py` — Expected: FAIL
  (`ModuleNotFoundError: skills`).
- [ ] **Step 3: Implement the server.** `app/skills.py` по строению `roles.py` (те же `BAD`, `_clean`, `read`): модели
  `SkillIn` (`extra="forbid"`; `name` — одна строка), `DefaultIn(SkillIn)` с `lang`, `OrderIn {ids}`.
  `seed_defaults()` — раз на базу по флагу `settings` `skill_defaults_seeded`. Посев библиотеки — одна
  `sections.transaction()`, в конце `users.skills_seeded`: (1) если `users.code_quick_actions` не NULL — каждое
  действие `code_actions.for_account(saved)` по порядку навыком `scope="agents"`, `quick=1`, `description=""`,
  `origin` — ключ из `QUICK_KEYS`, чей текст в `code_actions.DEFAULTS` совпал дословно (первое совпадение), иначе
  NULL; (2) умолчания языка по `position` с `origin = builtin or f"default-{id}"`, но при своём списке быстрых
  действий умолчания с `quick = 1` пропускаются — иначе вернулись бы удалённые кнопки; всего не больше
  `MAX_SKILLS`. `add_defaults` — недостающие по `origin`, включая быстрые. `users.code_quick_actions` не меняется.
  `_me` (`app/account_profile.py:149–150`): засеяна — `skills.quick_actions(uid)` и `false`, иначе прежние
  выражения. `lock_policy.blocks` (`app/lock_policy.py:163`): `or (head == "skills" and request.method != "GET")` —
  чтение открыто, как `GET /api/me` с быстрыми действиями, правка закрыта любой частичной защитой, как
  `PATCH /api/me`; в `ROUTES` теста оба `GET /api/skills…` — `open`, остальные пять и четыре
  `/api/admin/skills…` — `closed`. `takeout._settings`: `"skills"` — столбцы строки без `user_id`, по `position`;
  строка в `README`. Слова отказов — в `en.js`. Карта модулей CLAUDE.md — `skills`.
- [ ] **Step 4: Write `skills.BUILTIN`** — тексты на русском и английском пишет исполнитель; обязательный состав
  (спека п. 4–6; разделы, пути и имена инструментов — латиницей в обоих языках; `description` — одна фраза):
  - `spec` «Подготовить ТЗ» / “Prepare the spec”: изучить проект; файлы задач в `tasks/todo/`; спрашивать только то,
    что нельзя решить самому, каждым вопросом с вариантами и рекомендацией; «руками» проверить доступы для проверки
    результата; задачи — в разделе `NeedInfo` файла `tasks/index.md`, пока человек не скажет «финализируй»
    (“finalize”), затем — `Todo`.
  - `split` «Разбить на задачи» / “Split into tasks”: у каждой задачи (файл в `tasks/todo/`) — «Цель», «Владение»
    (каталоги и файлы без пересечений), «Зависимости», «Критерии приёмки», «Команда проверки», «Ограничения»;
    порядок и что можно параллельно; последняя задача — приёмка.
  - `queue` «Выполнить очередь» / “Run the queue”: головной чат код не пишет — по одной задаче из `Todo` отдаёт
    подзадаче с чистым контекстом; после каждой — проверка, один коммит с точечным `git add`, перенос файла в
    `tasks/done/`, строка в `Done`; неясно — в `NeedInfo` с вопросами, код откатить; не вышло — в `Errors` с
    причиной; у помощника без подзадач — по одной задаче и просьба начать новый чат. Формат очереди —
    `tasks/index.md` с разделами `Todo`, `NeedInfo`, `Errors`, `Done`, строка — ссылка на файл задачи и одна фраза.
  - `acceptance` «Приёмка» / “Acceptance”: сверить сделанное с критериями всех задач, прогнать проверки, отчёт
    «прошло / не прошло / неприменимо». Эти четыре и три быстрых — `scope="agents"`.
  - `close-journals`, `audit`, `critics` — имя и текст `code_actions.DEFAULTS` дословно (ru), `quick=True`;
    английские имена — из `en.js` («Resolve issues and ideas», «Deep project audit», «Independent critics»).
  - `parts` «Разобрать по частям» / “Work in parts” (`parts=True`, `scope="chat"`): план до 7 частей через
    `todo_write`, подтверждение через `ask_user`, каждая часть — вызовом `run_part`, затем общий ответ.
  - `minutes` «Протокол встречи» / “Meeting minutes” (`scope="chat"`): из расшифровки в диалоге — «Решения»,
    «Поручения» с ответственными и сроками, «Открытые вопросы».
- [ ] **Step 5: Run** `tests/test_skills.py tests/test_lock_policy.py tests/test_profile_admin.py
  tests/test_code_actions.py tests/test_takeout.py tests/test_docs.py tests/test_i18n_server.py` — PASS.
  **Commit** `Add the account skill library with defaults and quick-action migration`.
- [ ] **Step 6: Write the failing browser test** `tests/test_ui_skills.py` (integration; помощники `signin`, `go`,
  `READY`, `API`, `NO_OVERFLOW`, `is_open`, `set_value`, `problems` из `tests/test_ui_browser.py`; 320 и 1280 px,
  ещё прогон 320 px с `init="localStorage.setItem('studio-language', 'en')"`):

```python
NAMES = "[...document.querySelectorAll('#skills-settings .skill-item-name')].map(e => e.textContent)"
# *go("settings/skills"), wait NAMES.length === 9 → values["seeded"] == имена skills.BUILTIN[язык] по порядку
# #skills-settings-add → #skill-dialog: #skill-name, #skill-description, #skill-text, #skill-scope = "agents",
#   #skill-quick ✓, #skill-save → десятый в списке; GET /api/skills/{id}: quick true, text как введён
# .skill-edit первого: окно с его полным текстом (GET по id), новое имя видно в списке; .skill-up у последнего → он
# девятый и после location.reload(); .skill-delete с подтверждением → 9; #skill-defaults возвращает удалённую «Приёмку»
assert values["fits"] is True and values["nav"] == "Навыки"     # NO_OVERFLOW на 320 px, и на английском ("Skills")
assert values["noQuickEditor"]                                  # в «Настройки → Агенты» блока «Быстрые действия» нет
# администратор: #admin-skill-defaults, #skill-default-lang = "en" → английские имена; правка умолчания сохраняется
```

- [ ] **Step 7: Implement the page.** `skills.js`: `skillsBlock({ changed })` — блок `#skills-settings`: `ul.skill-list`
  из `li.skill-item[data-skill]` (`.skill-item-name`, описание, подпись области и отметок; кнопки `.skill-up`,
  `.skill-down` — значки `upward`, `downward` из `icons.js`, `.skill-edit`, `.skill-delete`), `#skills-settings-add`,
  `#skill-defaults`; окно `#skill-dialog` (`openDialog`): поля теста, область — «Чат», «Агенты», «Чат и Агенты»
  (имена `NAMES.chat`, `NAMES.code` из `sections-meta.js`), флажки «Кнопкой в пустом чате „Агентов“» (выключен при
  области «Чат») и «По частям» (выключен при области «Агенты»), счётчик «{0} из {1} знаков»; после каждой записи —
  `changed()`. `skillDefaultsBlock()` — блок `#admin-skill-defaults`: то же над `/api/admin/skills` с выбором
  языка `#skill-default-lang`; в `admin.js:977` — после `roleDefaultsBlock()`. `settings.js`: `ACCOUNT_IDS =
  ['appearance', 'skills', …]`, в литерал `names` — `skills: tr("Навыки")` (его разбирает
  `tests/test_docs.py::_account_settings`; руководство обязано содержать «Настройки» → «Навыки»), `PERSONAL` — с
  `skills`, ветка `shown === 'skills'` → `group('skills', skillsBlock({ changed: () => loadMe().catch(() => null)
  }))`; из ветки `code` убрать `quickActionsBlock()` и импорт. Стили `.skill-*` — по образцу `.role-*`
  (`app.css:1448–1456`), `:hover` только в `@media (hover: hover)`. Подписи — `tr` и `en.js` (строки редактора
  убрать, если их больше никто не зовёт; `tests/test_code_actions.py` — без файла редактора). Руководство и
  `agents-workspace.md` — путь «Настройки» → «Навыки». В `tests/test_ui_code_quick_actions.py` и
  `tests/test_ui_i18n.py` шаги редактора заменить действиями на странице «Навыки» (правка имени и текста быстрого
  навыка, удаление, создание с отметкой); ожидания кнопок пустого чата прежние, в `test_ui_i18n.py:203` тексты —
  `skills.BUILTIN["en"]` трёх быстрых.
- [ ] **Step 8: Run** `.venv/Scripts/python -m pytest -q -n0 -m integration tests/test_ui_skills.py
  tests/test_ui_code_quick_actions.py tests/test_ui_i18n.py` и быстрые `tests/test_i18n.py tests/test_css_tokens.py
  tests/test_docs.py tests/test_code_actions.py tests/test_static.py` — PASS.
  **Commit** `Replace quick-action settings with the Skills page`.

### Task 7: История: выжимки результатов инструментов, хранимый пересказ и «Сжать сейчас», неизменное начало запроса

Три причины, по которым запрос следующего хода каждый раз новый и длинный. (1) `chat.compose` (`app/chat.py:635–675`)
вставляет шаги прошлого ответа целиком — `web.history_block(steps, _for_model)` (`:669`): страница до 40 000 знаков
(`web.PAGE_MAX`) едет в каждый следующий запрос. (2) Пересказ `StreamTask._compact` (`:1246–1291`) живёт только в
`self.messages` и считается каждый ход заново; `SUMMARY_NOTE` (`:96`) просит «under 700 words» при
`SUMMARY_TOKENS = 1024` (`:92`), а `:1283` принимает только `ending == "stop"` — оборванный пересказ отбрасывается
(находка FINDINGS.md 2026-11-25). (3) Выдержки большого документа ищутся по вопросу хода для всех документов диалога
(`knowledge.for_turn`, `app/knowledge.py:540–559`) и встают в старое сообщение (`_document`, `:556`). Границу пересказа
`_compact` знает только номером сообщения запроса: `compose` сливает соседние сообщения пользователя, id строки по
номеру не восстановить — нужен попутный список id.

**Files:**
- Modify: `app/chat.py` — константы `:92–98`; новые `HISTORY_RESULT_CHARS`, `HISTORY_CUT`, `SUMMARY_BLOCK`,
  `SUMMARY_BUSY`, `COMPACTING`; новые функции `_for_history`, `_stored_summary`, `_last_skill`, `_summary_head`,
  `_without`, `_touch_summary` (рядом с `compose`); `compose`; `StreamTask._compact` и новый `StreamTask._summarize`;
  `get_chat` (`:2010`); `delete_turn` (`:2453`); ветки правки `create_turn` (`:2915–2918`) и `create_mcp_turn`
  (`:2779–2782`) — по одной строке `_touch_summary`; `estimate` (`:2685`) — тело в `_estimated`; `_turn_context`
  (`:2547`); `_excerpt_plan` (`:2535`); новые маршруты `compact_chat`, `chat_summary` — сразу после `estimate`.
  `_read_model`, `_round`, `_read`, `_turn_specs` не трогать — их правит задача 8.
- Modify: `app/knowledge.py` — `for_turn`, новая `heads`.
- Create: `app/static/js/chat-summary.js`; Modify: `app/static/js/chat.js:2096` (одна точка подключения),
  `app/static/css/app.css` (`.msg-summary`), `app/static/en.js`.
- Create: `tests/test_chat_summary.py`, `tests/test_chat_prefix.py`, `tests/test_ui_chat_summary.py` (integration).
- Modify: `tests/test_chat.py:207` (партий пересказа стало три: партия меньше на 1024 токена),
  `tests/test_chat_web.py:672` (шаг 1), `tests/test_knowledge.py:192–195` («4711» — в сообщении хода, в первом
  сообщении — начало документа), `tests/test_i18n_server.py` (`EXEMPT_CONSTANTS["chat.HISTORY_CUT"]`).

**Interfaces:**
- Consumes: задача 1 — `chats.summary`, `chats.summary_upto`, `chats.summary_at`, `messages.skill` (JSON
  `{name, chars, parts}`; пишет задача 10 в волне D — тесты этой задачи ставят столбец SQL-ом); задача 4 —
  `StreamTask._round(payload, offer, *, silent=True)` и `sse(..., metrics=False)`.
- Produces:
  - `chat.HISTORY_RESULT_CHARS = 1500`, `chat.SUMMARY_TOKENS = 2048`,
    `chat.SUMMARY_BLOCK = "<conversation-summary>\n{0}\n</conversation-summary>\n\n"`.
  - `compose(chat_id, upto, model=None, max_images=…, excerpts=None, replacement=None, ids: list | None = None)` —
    в `ids` на каждое сообщение результата ложится список id строк `messages`, из которых оно собрано.
  - `POST /api/chats/{chat_id}/compact` → 200 `{summary_upto: str, tokens: int, limit: int | null}`. Отказы: чужой —
    404; идёт ответ или сжатие — 409 `busy`; нет подключения — 409 `no_connection`; прямое подключение,
    `conn.cleaner` или диалог модели MCP — 409 `no_compact`; окно неизвестно — 409 `no_limit`; до последнего
    сообщения пользователя пересказывать нечего — 409 `nothing`; история длиннее четырёх партий — 413 `context`;
    шлюз ответил 503 — 503 `card`; иной отказ модели — 502 `upstream` его словами.
  - `GET /api/chats/{chat_id}/summary` → `{text: str}`; пересказа нет — 404 `gone`.
  - GET диалога (`get_chat`): верхний ключ `summary: {upto: str, at: float} | null` рядом с `chat` и `messages`.
  - `chat._estimated(uid, row, body: EstimateIn) -> {"tokens", "limit"}` — тело маршрута `estimate` без изменений.
  - `knowledge.heads(att_ids) -> dict[str, str]`; `knowledge.for_turn(..., own=())`.
  - Страница: `placeSummary(nodes, msgs, data) -> nodes` (`chat-summary.js`); узел `.msg-summary`, кнопка
    `.msg-summary-show`, окно `#chat-summary-dialog` с текстом в `.summary-text`.

**Решения:**
- **Выжимка** — `_for_history(step)`: `_for_model(step)`; длиннее 1500 знаков — первые 1500 и `HISTORY_CUT`
  («\n[результат сокращён: остальное можно получить повторным вызовом инструмента]»). Только в `compose`; в ходе
  вызова, в payload паузы и в `messages.tools` — полный текст.
- **Где действует пересказ** — в `compose`: `_stored_summary(chat_id, rows) -> (текст, номер строки summary_upto) |
  None`; действует, если строка `summary_upto` есть среди выбранных и стоит раньше `upto`. Тогда строки по неё
  включительно пропускаются, а в начало первого сообщения результата (`_prefix`; первым стоит не сообщение
  пользователя — новое сообщение `user`) встаёт `_summary_head(summary, skill)` = `skill + "\n\n"` (если есть) +
  `SUMMARY_BLOCK`. Префикс ставится после цикла — снаружи блоков `<web>`.
- **Навык** (решение 14 спеки): `_last_skill(chat_id, upto) -> (id, текст) | None` — последнее до `upto` выбранное
  сообщение с `messages.skill`, текст — `content[:chars]`. В шапку он идёт, только если стоит не позже
  `summary_upto`; в расшифровку для пересказа его текст не входит (`_without(content, текст)` — без первого
  вхождения, у строки и у текстовых частей). Более ранние навыки пересказываются вместе со своими сообщениями.
- **Автосжатие** (`_compact`): выбор границы `boundary` — прежний (`:1248–1266`); оценка навыка входит в `overhead`,
  если его сообщение — не сообщение хода. Дальше: `ids = []`, `compose(self.chat_id, self.user_message_id,
  self.model, ids=ids)` — сообщение запроса `j` ↔ `ids[j - 1]`; расшифровка — `request[1:boundary]` без прежней
  шапки и без текста последнего навыка; `seed` — прежний `chats.summary`. Запись в базу — только при
  `len(ids) == self.history_end`: `summary_upto = ids[boundary - 2][-1]`, `summary_at = clock.NOW()`; иначе
  (продолжение после паузы с иным составом) пересказ, как раньше, только в `self.messages`.
- **`_summarize(turns, seed) -> (текст | None, слова | None)`** — цикл партий из `_compact` (`:1270–1286`): первая
  партия получает `Summary so far:\n{seed}`; годен ответ с `ending` `stop` **или `length`** и непустым текстом;
  503 шлюза — `(None, SUMMARY_BUSY)`; расшифровка длиннее `batch * SUMMARY_ROUNDS` — `(None, None)`.
  `SUMMARY_NOTE` кончается «under 500 words».
- **«Сжать сейчас»**: в пересказ уходит всё до последнего сообщения пользователя (последний ход остаётся дословно:
  «Ответить заново» и правка последнего сообщения пересказ не задевают). Расшифровка — `compose(chat_id, id строки
  перед ним, model, 0, excerpts=knowledge.heads(knowledge.big_attachments(chat_id)))`; запрос ведёт
  `StreamTask(message_id="", messages=[], limit=limit, thinking=False if sections.is_local(conn, model) else None,
  listening=False)` вне `STREAMS` — тем же `_round`, значит подключение через Cleaner идёт через `cleaner_route`.
  На время запроса id диалога — в `COMPACTING`. После ответа модели: `operations.check(uid)`, `_chat_row`, запись,
  если строка `summary_upto` ещё существует (иначе 409 `busy`); итог —
  `{"summary_upto": …, **await _estimated(uid, row, EstimateIn())}`.
- **Сброс**: `_touch_summary(chat_id, created, rowid)` обнуляет три столбца, если строка `summary_upto` исчезла или
  стоит не раньше `(created, rowid)`; зовут `delete_turn` и ветки правки — до удаления строк.
- **Неизменное начало**: `for_turn(..., own=())` — вложения сообщения хода (`own`) получают выдержки по вопросу (нет
  совпадений — начало документа, как сейчас); остальные — `heads` (первые `knowledge.TOP_K` кусков), а найденное по
  вопросу в них (один `_search` по всем таким документам, `TOP_K`) идёт в блок `<knowledge>` сообщения хода вместе с
  найденным в базах диалога. `_turn_context` передаёт `own` — id вложений строки `upto`. В оценках до поиска
  (`_excerpt_plan`, `_estimated`) блок — `EXCERPT_GUESS` за базы плюс ещё один за документы истории среди `cut`.
- В ссылку «Поделиться», экспорт и выгрузку пересказ не входит: `chat_shares.regular_snapshot` (`:285`) и `takeout`
  читают строки `messages`. В журнал — только id и числа.

- [ ] **Step 1: Write the failing tests** — выжимка и начало запроса: замена
  `tests/test_chat_web.py::test_large_past_tool_output_is_summarized_for_the_next_turn` и новый
  `tests/test_chat_prefix.py` (`web_chat`, `call`, `steps_of` — `test_chat_web`; `big_text`, `small_model` —
  `test_knowledge`; `attach`, `messages`, `sse`, `turn` — `test_chat`):

```python
def test_past_tool_output_goes_to_the_next_turn_as_its_first_1500_chars(studio):
    cid = web_chat(studio); studio.gw.exa_page_text = "слово " * 6000
    studio.gw.chat_script = [{"tool_calls": [call("read_page", url="https://example.test/long")]}, ["Кратко."], ["Да."]]
    sse(turn(studio, cid, "Прочитай https://example.test/long"))
    (step,) = steps_of(messages(studio, cid)[1]["id"])
    assert studio.gw.chat_requests[1]["messages"][-1]["content"] == step["text"] and len(step["text"]) > 30_000
    events = sse(turn(studio, cid, "Ещё?"))
    assert studio.gw.chat_requests[-1]["messages"][1]["content"] == (
        f'<web kind="page">\n{step["text"][:1500]}{chat.HISTORY_CUT}\n</web>\n\nПрочитай https://example.test/long')
    assert chat.HISTORY_RESULT_CHARS == 1500 and "recovery" not in [e for e, _ in events]
    assert steps_of(messages(studio, cid)[1]["id"])[0]["text"] == step["text"]          # в базе шаг полный

def test_next_request_repeats_the_previous_one_up_to_the_penultimate_message(studio):   # приёмка п. 34
    cid = web_chat(studio)
    gw = state.DB.execute("SELECT connection_id FROM chats WHERE id = ?", (cid,)).fetchone()[0]
    small_model(studio, cid, gw, 20_000)                         # окно 20 тыс.: большой документ — выдержками
    studio.gw.exa_page_text = "слово " * 2000
    doc = attach(studio, "отчёт.txt", big_text("Пароль от сейфа — 4711.").encode())
    url = "https://example.test/long"
    studio.gw.chat_script = [{"tool_calls": [call("read_page", url=url)]}, ["Прочитал."], ["Второй."], ["Третий."]]
    sse(turn(studio, cid, f"Прочитай {url}", attachment_ids=[doc["id"]]))
    sse(turn(studio, cid, "Какой пароль от сейфа?"))
    sse(turn(studio, cid, "А ещё?"))
    second, third = studio.gw.chat_requests[-2:]
    dump = lambda value: json.dumps(value, ensure_ascii=False).encode()
    cut = len(second["messages"]) - 1                            # до конца предпоследнего сообщения
    assert cut == 3 and dump(third["messages"][:cut]) == dump(second["messages"][:cut])
    assert dump(third["tools"]) == dump(second["tools"])
    first = second["messages"][1]["content"]
    assert first.startswith('<web kind="page">\n') and chat.HISTORY_CUT in first
    assert '<document name="отчёт.txt" excerpts="true">\n[кусок 1]' in first and "4711" not in first
    asked = second["messages"][-1]["content"]                    # найденное по новому вопросу — в новом сообщении
    assert asked.startswith("<knowledge>") and "4711" in asked and asked.endswith("Какой пароль от сейфа?")
```

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -n0 --assert=rewrite tests/test_chat_prefix.py
  tests/test_chat_web.py` — Expected: FAIL (`module 'chat' has no attribute 'HISTORY_CUT'`).
- [ ] **Step 3: Implement** `_for_history` и вызов в `compose` (`:669`); `knowledge.heads`, `for_turn(own=)`,
  `_turn_context`, блок в `_excerpt_plan` и в `guess` маршрута `estimate`; поправить `tests/test_knowledge.py:192–195`.
  **Run** `tests/test_chat_prefix.py tests/test_chat_web.py tests/test_knowledge.py tests/test_mcp.py
  tests/test_chat_suggest.py tests/test_i18n_server.py` — PASS. **Commit**
  `Send past tool results as excerpts and keep earlier chat messages stable`.
- [ ] **Step 4: Write the failing tests** `tests/test_chat_summary.py`. Помощники файла: `tiny(studio)` — шлюз, ход
  `OLD = ("ABCD " * 2800).rstrip()` (≈ 4,7 тыс. токенов) на большой модели, затем `small_model(studio, cid, gw,
  8192)` и `chat.LIMITS.clear()`, итог — id диалога; `three_turns(studio)` — новый диалог и ходы «Вопрос-1 про
  бирюзу», «Вопрос-2», «Вопрос-3» с ответами «Ответ-1…3» (шлюз заводит тест); `stored(cid)` — строка `summary,
  summary_upto, summary_at`; `compact(studio, cid)` — `POST …/compact`; `code(r)` — `(код, detail.code)`;
  `with_skill(message_id, text)` — `UPDATE messages SET content = text || char(10, 10) || content, skill =
  '{"name": "Разбор", "chars": len(text), "parts": false}'` (так сообщение запишет задача 10):

```python
def test_auto_summary_is_stored_and_the_next_turn_reuses_it(studio):
    cid = tiny(studio); studio.gw.chat_reply = ["Пересказ: план в out/план.docx."]    # и партии пересказа, и ответ
    assert ("recovery", {"stage": "summary"}) in sse(turn(studio, cid, "Дополни"))
    asked = studio.gw.chat_requests[1]
    assert asked["messages"][0]["content"] == chat.SUMMARY_NOTE and "under 500 words" in chat.SUMMARY_NOTE
    assert asked["max_tokens"] == chat.SUMMARY_TOKENS == 2048 and "tools" not in asked
    first, row = messages(studio, cid), stored(cid)
    assert (row["summary"], row["summary_upto"]) == ("Пересказ: план в out/план.docx.", first[1]["id"])
    assert studio.c.get(f"/api/chats/{cid}").json()["summary"] == {"upto": first[1]["id"], "at": row["summary_at"]}
    assert studio.c.get(f"/api/chats/{cid}/summary").json() == {"text": row["summary"]}
    count = len(studio.gw.chat_requests)
    sse(turn(studio, cid, "Ещё"))
    sent = studio.gw.chat_requests[-1]["messages"]
    assert len(studio.gw.chat_requests) == count + 1                       # пересказ не считается заново
    assert sent[1]["content"].startswith(chat.SUMMARY_BLOCK.format(row["summary"])) and "ABCD" not in json.dumps(sent)
    assert len(messages(studio, cid)) == 6 and messages(studio, cid)[0]["content"] == OLD   # история на месте

def test_summary_cut_by_length_is_accepted(studio):                        # находка FINDINGS 2026-11-25
    cid = tiny(studio); studio.gw.chat_reply = ["Пересказ оборван на полусло"]
    # обёртка on["/v1/chat/completions"]: запросу с системным SUMMARY_NOTE — studio.gw.chat_ending = "length",
    # прочим — "stop"
    assert sse(turn(studio, cid, "Дополни"))[-1] == ("done", {"status": "done"})
    assert stored(cid)["summary"] == "Пересказ оборван на полусло"

def test_compact_now_keeps_the_last_turn_and_builds_on_the_previous_summary(studio, caplog):
    caplog.set_level(logging.INFO)
    add_gateway(studio); cid = three_turns(studio); ms = messages(studio, cid)
    studio.gw.chat_script = [["Итог: говорили про бирюзу."], ["Ответ-4"], ["Итог-2"]]
    r = compact(studio, cid)
    assert r.status_code == 200 and set(r.json()) == {"summary_upto", "tokens", "limit"}
    assert r.json()["summary_upto"] == ms[3]["id"] and r.json()["limit"] == 131072
    prompt = studio.gw.chat_requests[-1]["messages"][1]["content"]
    assert "Вопрос-1 про бирюзу" in prompt and "Ответ-2" in prompt and "Вопрос-3" not in prompt
    sse(turn(studio, cid, "Вопрос-4"))
    sent = studio.gw.chat_requests[-1]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert sent[1]["content"] == chat.SUMMARY_BLOCK.format("Итог: говорили про бирюзу.") + "Вопрос-3"
    assert compact(studio, cid).status_code == 200                         # новое сжатие — из прежнего и нового
    prompt = studio.gw.chat_requests[-1]["messages"][1]["content"]
    assert prompt.startswith("Summary so far:\nИтог: говорили про бирюзу.\n\nTranscript:\nuser: Вопрос-3")
    assert "Вопрос-1" not in prompt and stored(cid)["summary"] == "Итог-2" and len(messages(studio, cid)) == 8
    assert "Итог" not in caplog.text and "бирюз" not in caplog.text        # журнал — без текстов
    uid = state.DB.execute("SELECT user_id FROM chats WHERE id = ?", (cid,)).fetchone()[0]
    assert "Итог-2" not in json.dumps(chat_shares.regular_snapshot(uid, cid)[0], ensure_ascii=False)

def test_last_skill_stays_verbatim_and_earlier_ones_are_summarized(studio):    # решение 14 спеки
    add_gateway(studio); cid = three_turns(studio); ms = messages(studio, cid)
    with_skill(ms[0]["id"], "РАННИЙ НАВЫК"); with_skill(ms[2]["id"], SKILL)    # SKILL = "НАВЫК: разбери по частям."
    studio.gw.chat_script = [["Итог"], ["Ответ-4"]]
    assert compact(studio, cid).status_code == 200
    prompt = studio.gw.chat_requests[-1]["messages"][1]["content"]
    assert "РАННИЙ НАВЫК" in prompt and SKILL not in prompt and "Вопрос-2" in prompt
    sse(turn(studio, cid, "Вопрос-4"))
    assert studio.gw.chat_requests[-1]["messages"][1]["content"] == (
        SKILL + "\n\n" + chat.SUMMARY_BLOCK.format("Итог") + "Вопрос-3")

def test_changing_a_summarized_message_resets_the_summary(studio):
    add_gateway(studio); cid = three_turns(studio); ms = messages(studio, cid)
    assert compact(studio, cid).status_code == 200                         # пересказаны ms[0]…ms[3]
    assert studio.c.delete(f"/api/chats/{cid}/turns/{ms[4]['id']}").status_code == 204   # ход после пересказа
    assert stored(cid)["summary"] is not None
    assert studio.c.delete(f"/api/chats/{cid}/turns/{ms[0]['id']}").status_code == 204   # ход внутри пересказа
    assert tuple(stored(cid)) == (None, None, None) and studio.c.get(f"/api/chats/{cid}").json()["summary"] is None
    # второй диалог: compact, затем ход с edit={"id": первое сообщение, "last_id": последнее} — тоже (None,) * 3,
    # и в запросе хода нет "<conversation-summary>"

def test_compact_refusals(studio):
    add_gateway(studio); cid = new_chat(studio); sse(turn(studio, cid, "Один ход"))
    assert code(compact(studio, cid)) == (409, "nothing")
    assert studio.c.get(f"/api/chats/{cid}/summary").status_code == 404
    # идущий ответ (studio.gw.chat_hold = True, поток хода открыт до первого delta) — (409, "busy"); диалог прямого
    # подключения (add_connection) — (409, "no_compact"); другой пользователь (make_user, signin) — 404
```

- [ ] **Step 5: Run** `.venv/Scripts/python -m pytest -q -n0 --assert=rewrite tests/test_chat_summary.py` —
  Expected: FAIL (405 на `/compact`, `KeyError: 'summary'`, `max_tokens == 1024`).
- [ ] **Step 6: Implement** константы, `_stored_summary`, `_last_skill`, `_summary_head`, `_without`,
  `_touch_summary`, `compose(ids=)`, `_summarize`, `_compact`, `_estimated`, маршруты и ключ `summary` — по
  «Решениям». Слова новых отказов — в `app/static/en.js` (сторож `tests/test_i18n_server.py`). Поправить
  `tests/test_chat.py:207–227` под три партии пересказа.
- [ ] **Step 7: Run** `tests/test_chat_summary.py tests/test_chat_prefix.py tests/test_chat.py
  tests/test_chat_pending.py tests/test_chat_shares.py tests/test_cleaner_route_chat.py tests/test_i18n_server.py
  tests/test_docs.py` — PASS. **Commit** `Store the chat history summary and add compact-now` (в теле — заголовок
  закрытой находки: «Сжатие истории чата: пересказ просят «до 700 слов» при пределе ответа 1024 токена»; запись из
  `FINDINGS.md` удаляет ведущий при слиянии — файл вне Git, в worktree его нет).
- [ ] **Step 8: Write the failing test** `tests/test_ui_chat_summary.py` (`ANSWERED`, `NO_OVERFLOW`, `READY`,
  `add_gateway`, `browser`, `go`, `live`, `open_new_chat`, `problems`, `signin` — `test_ui_browser`):

```python
pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

@pytest.mark.parametrize("width", [320, 1280])
def test_summary_mark_stands_after_the_summarized_part_and_shows_the_text(browser, live, width):
    live.gw.chat_script = [["Ответ-1"], ["Ответ-2"], ["Итог первого хода."]]
    # шаги browser.drive — как в tests/test_ui_chat_pending.py: вход, add_gateway(), open_new_chat(mobile), ходы
    # «Вопрос-1» и «Вопрос-2» (ожидание ANSWERED и двух ответов); eval: api('POST', '/api/chats/<id из адреса>/compact')
    # и location.reload(); ожидание '#messages .msg-summary'; eval order — дети #messages: 'mark' у .msg-summary,
    # 'msg' у узла с data-id; eval fits; нажатие '.msg-summary-show'; ожидание
    # '#chat-summary-dialog[open] .summary-text'; eval text — его textContent
    assert not r.get("failed"), r
    values = r["values"]
    assert values["order"] == ["msg", "msg", "mark", "msg", "msg"]
    assert values["fits"] is True              # NO_OVERFLOW и правый край .msg-summary не дальше innerWidth
    assert values["text"] == "Итог первого хода." and not problems(r), r.get("log")
```

- [ ] **Step 9: Run** `.venv/Scripts/python -m pytest -q -n0 -m integration tests/test_ui_chat_summary.py` —
  Expected: FAIL (нет `.msg-summary`). **Implement** `chat-summary.js`: `placeSummary` вставляет узел отметки после
  узла сообщения `data.summary.upto` (нет `summary` или такого сообщения в ленте — список как есть); узел один на
  пару «диалог, `upto`» (перерисовка ленты его не пересоздаёт): `div.msg-summary` с текстом `tr("Ранняя часть
  свёрнута в пересказ")` и кнопкой `tr("Показать")`, по нажатию — `GET …/summary` и `openDialog` с заголовком
  `tr("Пересказ ранней части")`. В `chat.js:2096` — `keep(feed, placeSummary(msgs.map(…), msgs, chat))` и импорт.
  Стиль — существующими переменными цвета, `:hover` — в `@media (hover: hover)`; новые строки — в `en.js`.
  **Run** тот же файл, `tests/test_css_tokens.py`, `tests/test_i18n.py` (сторож словаря страницы), затем быстрый
  набор — PASS. **Commit** `Mark the summarized part of a Studio chat in the feed`.

### Task 12: «Агенты»: вложения, время, поиск, копирование

Число вложений зашито на сервере (`app/claude_code.py:197–198` — `max_length=4` и `8`, `:318` — `> 8` со словами,
`:1389`) и на странице (`code-chat.js:90` `IMAGE_COUNT = 8`, `:369` подсказка, `:638`, `:644` — 4 изображения). Ответ
`state` и событие `done` сервер передаёт как есть (`claude_code.py:1218`, `_events` `:1308`) — для времени хода он не
меняется; окно «Сессия и контекст» — `code-context.js:79` `paint`, данные приходят из `loadState` (`code-chat.js:573`),
`handle` (`:1398`) и `finish` (`:1337`). Лента — `messageNode` (`:944`), ранние страницы — `loadEarlier` (`:1961`).
Образец кнопки «Копировать» — `chat.js:2052–2060`. «Прочитать» из п. 10 в задачу не входит: текст ответа «Агентов»
лёг бы в базу записью «Озвучки», а инвариант приватности без решения владельца не ослабляется.

**Files:**
- Create: `app/static/js/code-attach-limits.js`, `code-find.js`, `code-answer-tools.js`; `tests/test_ui_code_limits.py`,
  `tests/test_ui_code_find.py`, `tests/test_ui_code_answer.py`
- Modify: `app/claude_code.py`, `app/agents.py`, `app/static/js/code-chat.js` (точки подключения), `code-context.js`,
  `app/static/en.js`, `app/static/css/app.css`, `docs/architecture/agents-workspace.md`
- Test: `tests/test_code_images.py`, `tests/test_code_attachments.py`

**Interfaces:**
- Consumes: задача 2 — `bridge.turn_limits`, `bridge.LIMITS_MAX`, `turn_ms`/`session_ms` в `done` и `state`,
  `history.items[].model`, `FakeMachine.limits`/`.timing`; задача 3 — ряд действий `status-bar.js` (лишнее уходит в «⋯»).
- Produces (сервер): в `/api/agents` — `limits: {"images": int, "attachments": int}`; отказ 422 `invalid` до моста —
  `f"можно прикрепить до {n} изображений"`, `f"можно прикрепить до {n} файлов"`.
- Produces (`code-attach-limits.js`): `attachCounts(agent) -> {images, attachments}` (нет `agent.limits` — 4 и 8);
  `countRefusal(kept, added, counts) -> string | null`.
- Produces (`code-context.js`): `timing({ turn_ms, session_ms })`, `turnStarted()`, `turnDone({ turn_ms, session_ms })`;
  строки окна `#code-turn-time`, `#code-session-time`, `#code-turn-speed`.
- Produces (`code-find.js`): `findView(host, { scroller, feed, earlier, hasEarlier })` → `{ button, open(), close(),
  unmount() }`; `FIND_MAX = 1000`; узлы `#code-find`, `#code-find-bar`, `#code-find-input`, `#code-find-count`,
  `#code-find-prev`, `#code-find-next`, `#code-find-earlier`; подсветки `code-find`, `code-find-current`.
- Produces (`code-answer-tools.js`): `answerTools(node, { text, model })`; классы `.code-msg-actions`, `.msg-copy`,
  `.msg-model`; запросов модуль не делает.

- [ ] **Step 1: Write the failing tests** — сервер (`setup`, `chat_url`, `sent`, `detail`, `DONE` — `tests/test_code.py`):

```python
def test_twelve_images_pass_with_the_new_bridge(studio):                           # tests/test_code_images.py
    aid, machine, link = setup(studio); machine.script = [DONE]
    link.hello["limits"] = machine.limits = {"images": 12, "attachments": 12}
    assert studio.c.get("/api/agents").json()["items"][0]["limits"] == {"images": 12, "attachments": 12}
    assert studio.c.post(chat_url(aid, "turn"), json={"text": "", "images": [PNG] * 12}).status_code == 200
    assert len(sent(machine, "turn")[0]["images"]) == 12
    assert studio.c.post(chat_url(aid, "turn"), json={"text": "", "images": [PNG] * 13}).status_code == 422
def test_old_bridge_is_refused_with_its_own_number_before_the_bridge(studio):     # Review Focus 1
    aid, machine, link = setup(studio)                                              # hello без limits — мост 0.47
    assert studio.c.get("/api/agents").json()["items"][0]["limits"] == {"images": 4, "attachments": 8}
    r = studio.c.post(chat_url(aid, "turn"), json={"text": "x", "images": [PNG] * 5})
    assert (r.status_code, detail(r)) == (422, {"code": "invalid", "message": "можно прикрепить до 4 изображений"})
    link.hello["attachments"] = True                                                # FILE — tests/test_code_attachments.py
    r = studio.c.post(chat_url(aid, "steer"), json={"text": "x", "attachments": [FILE] * 9})
    assert detail(r)["message"] == "можно прикрепить до 8 файлов" and not sent(machine, "turn") and not sent(machine, "steer")
```

  Браузер (помощники `tests/test_ui_browser.py`; файлы — `DataTransfer` и событие `paste`, как в
  `test_code_screenshots_paste_pick_drop_and_retry`; буфер — `COPY_SPY`):

```python
@pytest.mark.parametrize("width", [320, 1280])                                     # tests/test_ui_code_limits.py
def test_ten_screenshots_in_one_message_and_turn_time(browser, live, width):
    machine.limits = {"images": 12, "attachments": 12}; machine.large_attachments = machine.timing = True
    machine.context = {"used": 10, "limit": 100, "percent": 10, "usage": {"total": 100, "input": 80, "output": 20}}
    machine.script = [{"k": "context", "used": 20, "limit": 100, "usage": {"total": 700, "input": 180, "output": 520}},
                      {"k": "text", "text": "Вижу"}, {"k": "done", "status": "done"}]
    # одна вставка 10 файлов PNG → 10 превью в #code-images → #code-send → «Вижу» → #code-context
    assert len(code_requests(machine, "turn")[0]["images"]) == 10
    assert values["turn"] == values["session"] == "1 с" and values["speed"] == "500 токенов/с" and values["fits"]
def test_old_bridge_refuses_the_fifth_image_before_sending(browser, live):        # Review Focus 1
    # machine без limits и timing (0.47), attachments = True: вставка 5 файлов → #code-status «Можно прикрепить до 4
    # изображений к сообщению», превью нет; вставка 4 — 4 превью; подсказка #code-attach содержит «до 8 файлов»
    assert values["rows"] == ["нет данных"] * 3      # #code-turn-time, #code-session-time, #code-turn-speed
    assert not code_requests(machine, "turn")
@pytest.mark.parametrize("width", [320, 1280])                                     # tests/test_ui_code_find.py
def test_find_counts_steps_through_and_loads_earlier_pages(browser, live, width):
    # 50 сообщений: «жираф» только в m-3 (ранняя страница), «зебра» — в трёх последних ответах
    # eval: e = new KeyboardEvent('keydown', {key: 'f', code: 'KeyF', ctrlKey: true, bubbles: true, cancelable: true});
    #   document.dispatchEvent(e) → e.defaultPrevented, фокус в #code-find-input
    # «зебра» → «1 из 3»; #code-find-next → «2 из 3»; #code-find-prev → «1 из 3»; CSS.highlights.get('code-find').size === 3
    # «жираф» → «0 из 0», видна #code-find-earlier → нажатие → «1 из 1», кнопка скрыта (история с начала)
    # Escape → строка скрыта, подсветок нет; лупа #code-find открывает снова
    assert not [p for p in result["sent"] if p.endswith("/search")] and values["fits"]   # текст на сервер не уходит
@pytest.mark.parametrize("width", [320, 1280])                                     # tests/test_ui_code_answer.py
def test_copy_and_model_under_the_answer(browser, live, width):
    # история: ответ m-1 {"text": "**Готово**", "model": "claude-opus-5-5"} и ответ m-3 без model
    assert values["models"] == ["claude-opus-5-5"]                                 # .code-msg.assistant .msg-model
    # .msg-copy → window.copied === "**Готово**", надпись «Скопировано»
    # живой ход [settings {model: "sonnet"}, text «Сделано», done] → у нового ответа .msg-model «sonnet», его
    #   .msg-copy кладёт в буфер «Сделано»
    assert values["read"] == 0 and not [p for p in result["sent"] if "/api/speeches" in p]   # .msg-read в ленте нет
```

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -n0 --assert=rewrite tests/test_code_images.py
  tests/test_code_attachments.py` — Expected: FAIL (`KeyError: 'limits'`, 422 на 12 изображениях);
  `… -m integration tests/test_ui_code_limits.py tests/test_ui_code_find.py tests/test_ui_code_answer.py` — FAIL.
- [ ] **Step 3: Implement — пределы.** `claude_code.py`: у `Turn` `max_length` обоих списков — `bridge.LIMITS_MAX[…]`
  (12); `_turn_attachments` число не сверяет; новая `_turn_counts(link, images, attachments)` — в `_turn_input` сразу
  после `link = bridge.LINKS.get(aid)`: `counts = bridge.turn_limits(link.hello) if link is not None else
  bridge.LIMITS_MAX` (без канала отказ скажет `ask` — `offline`); больше `counts["images"]` изображений или больше
  `counts["attachments"]` вложений вместе — 422 `invalid` словами с числом; `:1389` — «нужны текст и до 12 вложений».
  `agents.py`: `"limits": bridge.turn_limits(hello)` (и участнику). `en.js`: прежние строки с 4 и 8 — прочь (их
  закрывают шаблоны задачи 2), подписи страницы — с `{0}`. `code-attach-limits.js` и три точки `code-chat.js`: константа
  `IMAGE_COUNT` — прочь, подсказка скрепки — «до {2} файлов», обе проверки числа в `addImages` — один `countRefusal`.
- [ ] **Step 4: Implement — время и скорость.** `code-context.js`: три строки окна; длительность — «{0} с» (до минуты;
  меньше 10 с — до одной десятой), «{0} мин {1} с», «{0} ч {1} мин»; нет числа — «нет данных». Скорость считает страница:
  `turnStarted()` (событие `turn`) запоминает `usage.output` контекста, `turnDone` — разница с текущим `usage.output`,
  делённая на `turn_ms / 1000`, целым, «{0} токенов/с»; нет любого из чисел, разница отрицательна или ход начат не при
  этой странице — «нет данных». `loadState` зовёт `context.timing(data)`, `handle('turn')` — `turnStarted()`, `finish`
  — `turnDone(data)`. К подписи окна — фраза: время хода — от принятия задания мостом до конца ответа, вместе с
  ожиданием ответов на карточки.
- [ ] **Step 5: Implement — поиск.** `code-find.js`: строка над лентой (`role="search"`); поиск без регистра по тексту
  `.code-user-text` и `.code-timeline-text` загруженной ленты (шаги не ищутся); подсветка — CSS Custom Highlight API
  (нет `CSS.highlights` — только переход и счётчик); не больше `FIND_MAX` совпадений («1000+»); Enter и «следующее» —
  вперёд, Shift+Enter и «предыдущее» — назад, переход прокручивает `scroller`; пересчёт — по вводу (150 мс) и по
  изменению ленты, пока строка открыта. «Искать в более ранней части» видна при `hasEarlier()`: зовёт `earlier()`
  (`loadEarlier`) до первого совпадения или начала истории, повторное нажатие — стоп. Ctrl+F и ⌘F (`event.code ===
  'KeyF'`) на `document`, пока чат на экране и нет открытого окна, открывают строку; фокус уже в её поле — событие не
  трогается (поиск браузера). Escape закрывает строку и снимает подсветку. Кнопка `#code-find` (значок `search`) — в
  `actions`. Запросов модуль не делает.
- [ ] **Step 6: Implement — кнопки ответа.** `code-answer-tools.js`: под ответом с текстом — ряд `.code-msg-actions`
  с «Копировать» (`.msg-copy`: весь текст ответа, Markdown, в буфер; «Скопировано» / «Не вышло»); под ответом с `model`
  — `p.msg-model.muted.small`. `messageNode` зовёт для ответов истории (`m.text`, `m.model`), `finish` — для ответа
  кончившегося хода (текст — сегменты подряд; модель — `effectiveModel`, у DeepSeek Harness — подпись каталога). Кнопки
  «Прочитать» нет, `speech-read.js` и `app/speeches.py` не трогаются: текст ответа остаётся на странице и в буфере.
  Стили — токены, `light-dark()`, `::highlight(code-find)`; сторож — `tests/test_css_tokens.py`.
- [ ] **Step 7: Run** `tests/test_code_images.py tests/test_code_attachments.py tests/test_i18n.py
  tests/test_i18n_server.py tests/test_css_tokens.py tests/test_docs.py` — PASS; `-m integration` три новых браузерных
  — PASS; затем быстрый набор `.venv/Scripts/python -m pytest -q`.
- [ ] **Step 8: Commit** `Take Agents attachment limits from the bridge hello`; `Show turn time and speed in the
  session window`; `Search the open Agents chat on the page`; `Copy the answer and name the model under Agents
  answers`; правки `agents-workspace.md` — в коммитах своих пунктов.

## Волна C

### Task 6: Строка состояния «Чата»: стадии, «Диалог и контекст», статистика, файлы, счётчик, название

Сейчас стадия хода — текст под ответом (`chat.js` `paint` :2018–2045: «жду карту · м:сс» со сведениями `withCard`,
«загружаю модель», «жду ответа»), кольцо контекста — немая `span#ctx-meter` в `.composer-opts`
(`chat-context.js:16`, `chat.js:798–801`), в шапке — слово «Чат» (:735). Сведения о карте уже приходят событием
`wait {seconds, card: {busy, minutes, ahead}}` (`chat.py:989`, `chat.js:323–327`): п. 36 по поправке контрактов
**только переносит их в строку состояния**, сервер не меняется. Числа ответа даёт задача 4; у прямого подключения их
присылает страница (`readBrowser`, `chat.js:419–497`, решение 19 спеки). `GET /api/chats/{chat_id}` объявлен в
`chat.py:1977` — `/api/chats/stats` обязан стоять раньше (образец — `presets` перед `images`, `main.py:211`). Файлы
уже отдают `/api/media/{id}` (`media.py:842`) и `…/messages/{mid}/files/{step}/{n}` (`mcp_files.py:486`).

**Files:**
- Create: `app/chat_info.py`; в `app/static/js/` — `chat-status.js`, `chat-context-window.js`, `chat-stats.js`,
  `chat-dialog-files.js`, `chat-title.js`, `chat-usage.js`
- Modify: `app/main.py` (`include_router(chat_info.router)` перед `chat.router` :214 — «раньше chat: /api/chats/stats —
  не id диалога»), `app/static/js/chat-context.js`, `chat.js`, `settings.js:1889`, `icons.js` (значок `stats`),
  `app/static/css/app.css`, `app/static/en.js`, `CLAUDE.md` (карта модулей: `chat_info` — сторож
  `tests/test_docs.py::test_module_map_lists_every_app_module`), `docs/architecture/chat.md` (раздел «Строка
  состояния и окна диалога»), `tests/fakes.py` (`FakeCloud.chat_usage = None`, `reject_usage = False`)
- Test: Create `tests/test_chat_info.py`, `tests/test_ui_chat_status.py`; Modify `tests/test_ui_chat_composer.py:91–108`,
  `tests/test_ui_network.py:118–133`
- Подключение в `chat.js`: импорты — после :8; `notify()` :244 — `job.eventAt = Date.now()`; `append()` :273 —
  `job.firstAt ??= Date.now()`; `readBrowser()` :434–443, :453–459, :493; шапка :735; кольцо :798–801; блок после
  `view.append(root);` :808; `shareContext()` :817–823; `paint()` :2023–2024 и :2044–2045; `syncBusy()` :2155, таймер
  `refresh()` :2719–2724 и `tick` :2758–2761 — `status.paint()`; `open()` :2292 и новая `openWith` рядом с
  `newChat()` :938; `destroy()` :2861 — `status.unmount()`.

**Interfaces:**
- Consumes: `statusActions(…, { id })`, `data-status="ring"` (задача 3); поля сообщения `usage: {in, out, cached,
  reasoning} | null`, `gen_ms`, `total_ms` и приём тех же полей телом `PUT /api/chats/{id}/messages/{mid}` (задача 4);
  `POST /api/chats/{id}/compact` → `{summary_upto, tokens, limit}` (задача 7), `POST /api/chats/{id}/continue` →
  `{chat_id, text}` (задача 15) — до их слияния кнопки окна показывают слова отказа сервера.
- Produces:
  - `chat_info.STATS_DAYS = 31`; `GET /api/chats/stats?days=31` (`days` 1–31, иначе 422) → `{"days": [{date, model,
    answers: int, in: int | null, out: int | null, gen_ms: int | null}]}` — дата `ГГГГ-ММ-ДД` по поясу учётки, новые
    сверху, внутри дня — по модели; `null` — ни у одного ответа группы числа нет.
  - `GET /api/chats/{chat_id}/files` → `{"items": [{kind: "attachment" | "media" | "result", name, size: int | null,
    mime, url}]}` (`mime` — сверх контракта: по нему страница решает «Открыть» или «Скачать»).
  - `chat-status.js`: `SILENCE_MS = 10000`, `stageOf(job, message, { local }) -> { key, since, eventAt, … } | null`,
    `stageText(stage, now) -> string`, `chatStatus(host, { ring, job, last, local, stepNode, stats, files }) -> { bar,
    paint(), dialog(chat | null), unmount() }`; `chat.js`: `openWith(id, text)` — открыть диалог с текстом в поле
    (берут задачи 13 и 15); `chat-usage.js`: `usageBody(connId)`, `usageRefused(connId, status, text) -> bool`,
    `usageOf(raw)`, `withSpeed(label, message)`; `statsDialog(host)`, `statsBlock(host)`; `fileCount(chat)`,
    `filesDialog(host, chatId)`; `chatTitle({ rename, remove, said }) -> { node, actions, menu, set(chat | null) }`;
    `contextWindow(host, { dialog, connection, estimate, busy, openWith }) -> { open() }`.

- [ ] **Step 1: Write the failing test** `tests/test_chat_info.py` (быстрый; `add_gateway`, `signin`, `make_user` —
  `conftest`; `new_chat`, `turn`, `sse`, `attach`, `png` — `test_chat`; ответы — прямыми `INSERT INTO messages`
  помощником `answer(...)`, чтобы не зависеть от записи `usage` задачей 4):

```python
def test_stats_group_answers_by_account_day_and_model(studio):
    today = datetime.fromtimestamp(now, ZoneInfo("Europe/Moscow")).date()                     # now = studio.clock[0]
    answer(cid, now, "m1", usage={"in": 10, "out": 4}, gen_ms=400)
    answer(cid, now, "m1", usage={"in": 20, "out": 8, "cached": 5}, gen_ms=500, unchosen=1)   # вариант тоже считается
    answer(cid, now, "m2"); answer(cid, now - 30 * 86400, "m1"); answer(cid, now - 31 * 86400, "m1")   # 31-й день назад — мимо
    answer(cid, now, "m1", status="streaming"); answer(trashed, now, "m1"); answer(foreign, now, "m1")  # мимо все три
    row = lambda day, model, n, i, o, g: {"date": day.isoformat(), "model": model, "answers": n, "in": i, "out": o, "gen_ms": g}
    assert studio.c.get("/api/chats/stats").json()["days"] == [row(today, "m1", 2, 30, 12, 900),
        row(today, "m2", 1, None, None, None), row(today - timedelta(days=30), "m1", 1, None, None, None)]
    assert len(studio.c.get("/api/chats/stats?days=1").json()["days"]) == 2
    assert studio.c.get("/api/chats/stats?days=32").status_code == 422
    far = datetime.fromtimestamp(now, ZoneInfo("Pacific/Pago_Pago")).date(); assert far != today
    state.DB.execute("UPDATE users SET timezone = 'Pacific/Pago_Pago'")
    assert studio.c.get("/api/chats/stats?days=1").json()["days"][0]["date"] == far.isoformat()

def test_files_list_attachments_media_and_mcp_results(studio):
    # ход с notes.txt и p.png; chat_media call.mp3 и ещё одна строка с deleted; у ответа и у его невыбранного варианта —
    # шаг MCP с files [{"aid": "art_1", "mime": "image/png", "name": "plot.png"}]
    assert [(i["kind"], i["name"], i["mime"], i["size"] is None) for i in items] == [
        ("attachment", "notes.txt", "text/plain", False), ("attachment", "p.png", "image/png", False),
        ("media", "call.mp3", "audio/mpeg", False), ("result", "plot.png", "image/png", True)]
    assert [i["url"] for i in items] == [f"/api/media/{txt}?download=1", f"/api/media/{pic}",
        f"/api/media/{mp3}?download=1", f"/api/chats/{cid}/messages/{mid}/files/0/0"]   # существующие маршруты и их проверки
    assert studio.c.get(f"/api/chats/{cid}/files").status_code == 404                  # после signin другой учёткой
```

- [ ] **Step 2: Run** `… tests/test_chat_info.py` — Expected: FAIL (404: `stats` принят за id диалога, `/files` нет).
- [ ] **Step 3: Implement** `app/chat_info.py` (`router = APIRouter(prefix="/api")`; в журнал не пишет). `stats`: пояс
  — `media.user_zone(uid)`, «сегодня» — дата `clock.NOW()` в нём, окно — с полуночи дня `сегодня − (days − 1)`; строки
  — `messages m JOIN chats c`: `c.user_id = ?`, `c.deleted IS NULL`, `m.role = 'assistant'`, `m.status <> 'streaming'`,
  `m.created >=` начала окна (невыбранные варианты входят); группа — (дата `m.created` в поясе, `m.model or ""`);
  `in` / `out` — суммы ключей JSON `m.usage`, `gen_ms` — сумма столбца. `files`: `chat._chat_row`; вложения выбранных
  сообщений (`JOIN media`: `size`, `mime`; у картинки `url` без `?download=1`); `chat_media` без `deleted`; файлы шагов
  выбранных сообщений — через `mcp_files.chat_result(uid, row, step, servers=…)`, как `get_chat` (`chat.py:2006–2014`).
- [ ] **Step 4: Run** `tests/test_chat_info.py tests/test_docs.py tests/test_chat.py` — PASS.
- [ ] **Step 5: Commit** `Add chat statistics and dialog file list routes`.
- [ ] **Step 6: Write the failing test** `tests/test_ui_chat_status.py` (`integration`; `signin`, `READY`,
  `open_new_chat`, `ANSWERED`, `LAST_ANSWER`, `NO_OVERFLOW`, `API`, `live_sql`, `add_cloud`, `attach`, `is_open` —
  `test_ui_browser`; английский — `init="localStorage.setItem('studio-language', 'en')"`):

```python
@pytest.mark.parametrize("width,lang", [(320, "en"), (1280, "ru")])                 # Review Focus 5
def test_status_bar_shows_stages_and_fits(browser, live, width, lang):               # ход с удержанным шлюзом (chat_hold)
    assert v["idle"] == ["idle", "idle"] and v["writing"] == ["write", "responding"]  # #chat-activity: data-stage, data-state
    assert v["place"] is True and v["done"] == "idle"   # строка — между #messages и #composer, #chat-activity ≥ 40 % строки
    assert v["fits"] is True     # NO_OVERFLOW; строка в одну линию; видимые кнопки и #chat-status-more — в [0, innerWidth]

def test_stage_words_card_queue_and_silence(browser, live):   # чистые stageOf / stageText, now — число
    assert v["keys"] == ["card", "scan", "recovery", "tool", "think", "write", "load", "wait", "remote", "pending", None]
    assert v["card"] == "жду карту · 2 · ≈9 мин · 0:42"       # job.wait = now − 42 с, job.card = {ahead: 2, minutes: 9}
    assert v["card_plain"] == "жду карту · 0:05" and v["card_silent"] == "жду карту · 0:20"   # карта — не «тишина»
    assert v["tool"] == "инструмент Сервер · lookup · 0:03"
    assert v["quiet"] == "пишет · 0:09" and v["silent"] == "пишет · 0:12 · последнее событие 12 с назад"   # порог 10 с

@pytest.mark.parametrize("width", [320, 1280])
def test_context_window_numbers_buttons_and_no_data(browser, live, width, monkeypatch):
    # chat.CONTEXT[модель] = 10000; ход; снимок окна; live_sql: ответу usage {"in": 100, "out": 50, "cached": 40},
    # gen_ms 2000, total_ms 2500; перезагрузка; fetch подменён для /compact и /continue
    assert v["ring"] == ["BUTTON", True, True]       # #ctx-meter — кнопка в .code-status-bar; у нового диалога скрыта
    assert v["empty"]["last_in"] == v["empty"]["speed"] == "нет данных"              # приёмка п. 13: шлюз без usage
    assert v["rows"] == {"model": "local-gemma-abl", "connection": "srvllm", "role": "—", "messages": "2",
        "limit": "10 000", "last_in": "100", "last_out": "50", "last_cached": "40", "total_in": "100",
        "total_out": "50", "total_cached": "40", "time": "2,5 с", "speed": "25 ток/с"}   # dd[data-row]
    assert v["under"].endswith("· 25 ток/с")         # .msg-model последнего ответа
    assert v["compact"] == [f"POST /api/chats/{cid}/compact", "1 200"]               # ответ {tokens: 1200, …} — строка «занято»
    assert v["continued"] == [f"#/chat/{other}", "Итог диалога"] and v["fits"] is True   # {chat_id, text}: адрес и #chat-input

@pytest.mark.parametrize("width", [320, 1280])
def test_title_files_tools_counter_and_stats(browser, live, width):
    assert v["new"] == ["Чат", True] and v["title"] == ["Мой план", False]           # h1#chat-title; #chat-title-actions скрыта
    assert v["menu"] == ["Переименовать", "Удалить", "Скопировать адрес"]
    assert v["renamed"] == "Новое имя" and v["copied"].endswith(f"/#/chat/{cid}")     # navigator.clipboard подменён
    assert v["files"] == ["2", "Файлы диалога (2)", ["notes.txt", "p.png"], ["Скачать", "Открыть"]]
    assert v["tools"] == [False, "1", 1] and v["tools_after"] is True   # ход с todo_write, второй круг удержан: hidden, счёт, шагов
    assert v["stats"] == ["m1", "2", "30", "12"] and v["settings"] is True and v["fits"] is True   # и из «Настройки → Чат»

def test_direct_answer_reports_usage_and_survives_a_gateway_without_it(browser, live):
    live.cloud.chat_usage = {"prompt_tokens": 12, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 3}}
    assert live.cloud.chat_requests[-1]["stream_options"] == {"include_usage": True}
    assert v["saved"]["usage"] == {"in": 12, "out": 5, "cached": 3} and 0 <= v["saved"]["gen_ms"] <= v["saved"]["total_ms"]
    live.cloud.reject_usage = True   # 400 {"error": {"message": "Unrecognized request argument supplied: stream_options"}}
    assert v["second"] == ["done", None]                                              # статус и usage второго ответа
    assert ["stream_options" in r for r in live.cloud.chat_requests[-3:]] == [True, False, False]   # повтор и третий ход
```

  `test_ui_chat_composer.py::test_passport_warning_and_context_ring`: вместо высоты кольца против чипа модели —
  `RING.closest('.code-status-bar')` и `.ctx-ring` 18 px. `test_ui_network.py` (503 шлюза): ещё
  `#chat-activity.dataset.stage === 'card'`; под ответом — по-прежнему «жду карту · » и «Не ждать».
- [ ] **Step 7: Run** `… -m integration tests/test_ui_chat_status.py` — Expected: FAIL (`chat-status.js` — 404).
- [ ] **Step 8: Implement — строка и стадии** (`chat-status.js`). `bar` — `div.code-status-bar.chat-status-bar`;
  слева `div#chat-activity.code-activity[role=status][data-stage][data-state]` (`title` — полные слова, у карты —
  `withCard`); `actions` — `#chat-files-open` (значок `folder`, счёт `span.code-changed-count`, `aria-label` «Файлы
  диалога: N», скрыта при 0) и `#chat-stats-open` (значок `stats`); `fixed` — `#chat-tools` (значок `code`, счёт
  `span.code-active-count`, панель `popupPanel` `#chat-tools-menu` с `ul.web-list` из `stepNode`) и `ring`; сборка —
  `statusActions(host, bar, activity, actions, fixed, { id: 'chat-status' })`. Стадия — первая подошедшая (`key` —
  условие — слова — `data-state`): `card` — `job.wait != null` (без `job` — `Number.isFinite(m.wait)`) — «жду карту»,
  затем `card.ahead` и «≈{minutes} мин» — `thinking`; `scan` — `job.scan` / `m.ocr` — «распознаю скан · стр. N из M» —
  `thinking`; `recovery` — `m.recovery` — «сжимаю историю» / «продолжаю ответ» — `thinking`; `tool` — `job.tool` —
  «инструмент X» (MCP — `tool.label`, иначе «поиск», «страница», «YouTube») — `tool`; `think` — `job.thinking` —
  «думает» — `thinking`; `write` — `job.first` — «пишет» — `responding`; `load` / `wait` — есть `job`, диалог
  локальный / нет — «загружаю модель» / «жду ответа» — `thinking`; `remote` — нет `job`, `m.status === 'streaming'` —
  «ответ идёт» — `responding`; `pending` — `m.pending`, ответ не идёт — «ждёт вашего ответа» — `waiting`; иначе
  `null` — «нет активного ответа» — `idle`. `since` — когда `key` сменился (`job.stageKey` / `job.stageAt` ведёт
  `paint()`), у `card` — `job.wait`. `stageText` — слова, ` · м:сс` от `since` (кроме `remote`, `pending`, `null`) и
  ` · последнее событие N с назад` при `now − eventAt >= SILENCE_MS` (только `tool`, `think`, `write`, `load`,
  `wait`). `#chat-tools`: счёт — `(m.tools?.length ? m.tools : m.calls || []).length` идущего ответа; скрыта, когда
  ответ не идёт или счёт 0. Под ответом (`chat.js:2023–2024`) остаётся «жду карту · м:сс» без `withCard`.
- [ ] **Step 9: Implement — окна, шапка, числа.** `contextRing(request, { open })`: узел — `button#ctx-meter
  .ctx-meter[data-status=ring][aria-haspopup=dialog]`; у открытого диалога виден всегда (предел не известен — класс
  `ctx-none`, «Контекст: нет данных»), у нового скрыт. `contextWindow` — `dialog#chat-context-dialog`
  (`openDialog`), `dl.code-usage-summary` со строками `dd[data-row]` из теста: «занято» и «предел» — `estimate()`
  (`estimateContext`, :1415), последний ответ и сумма по `chat.messages` — из `usage`, время — `total_ms`, скорость —
  `usage.out / (gen_ms / 1000)` целым, чего нет — «нет данных»; кнопки `#chat-context-compact` («Сжать сейчас»,
  выключена при идущем ответе) и `#chat-context-continue` («Новый диалог с итогом» → `openWith(chat_id, text)`).
  `openWith`: `prefill = { id, text }`, `loadList()`, `ctx.navigate('chat/' + id)`; `open()` после загрузки диалога
  кладёт `prefill.text` в поле и сбрасывает его. `withSpeed` (:2044–2045) — «модель · 25 ток/с». `chatTitle`:
  `h1#chat-title` (нет диалога — `NAMES.chat`; название — с многоточием и `title`), `#chat-title-actions`
  (`iconButton('more', tr("Действия с диалогом"))`) с пунктами `#chat-title-rename`, `#chat-title-delete` — те же
  `renameDialog` / `deleteDialog` (:886, :916) — и `#chat-title-copy` (`location.origin + '/#/chat/' + id` в буфер,
  «Адрес скопирован» в `#chat-status`); CSS — `.chat-head h1 { flex: 0 1 auto; min-width: 0; }`. `statsDialog` —
  `dialog#chat-stats-dialog`, `table.code-usage-days` (день, модель, ответы, ввод, вывод, время генерации; `null` —
  «—»), пусто — «За 31 день ответов нет»; `statsBlock(host)` — `section.block#chat-stats-settings` в
  `settings.js:1889` после `memoryBlock(host)`. `filesDialog` — `dialog#chat-files-dialog`, заголовок «Файлы диалога
  (N)», строка `li.chat-file[data-kind]`: имя, вид («вложение», «медиа диалога», «результат MCP»), размер (`byteSize`
  из `chat-files.js`, `null` — «—»), `image/*` — кнопка «Открыть» (окно с картинкой), иначе ссылка «Скачать».
  Прямое подключение: в тело запроса — `...usageBody(job.connId)` (`stream_options: {include_usage: true}`, пока
  подключение не отмечено в памяти вкладки); ответ 400 с признаком из `REJECTED` (:393) и словом `stream_options` или
  `include_usage` — `usageRefused` отмечает подключение, запрос повторяется один раз без параметра; кусок потока с
  `usage` → `job.usage = usageOf(…)` (`prompt_tokens` → `in`, `completion_tokens` → `out`,
  `prompt_tokens_details.cached_tokens` → `cached`, `completion_tokens_details.reasoning_tokens` → `reasoning`; чего
  нет — нет ключа); в `job.saving` — `usage` (если есть), `gen_ms = now − job.firstAt` (если был текст), `total_ms =
  now − job.started`, целые ≥ 0; тело `pagehide` (`partBody`) — без чисел. `FakeCloud`: кусок `{"choices": [],
  "usage": …}` перед `[DONE]` при `chat_usage` и `stream_options.include_usage`; `reject_usage` — 400 на запрос с
  `stream_options`.
- [ ] **Step 10: Run** `-m integration tests/test_ui_chat_status.py tests/test_ui_chat_composer.py
  tests/test_ui_network.py tests/test_ui_design_consistency.py tests/test_ui_chat_pending.py` и быстрые
  `tests/test_i18n.py tests/test_css_tokens.py tests/test_sections_meta.py tests/test_static.py` — PASS.
- [ ] **Step 11: Commit** `Add the chat status bar with stages, context window, statistics and dialog files`.

### Task 9: Окно, признак «локальная», параметры на модель

Что в коде. Токены считаются как знаки / 3 для любой модели (`chat.estimate_tokens`, `app/chat.py:739–754`;
`spec_tokens`, `:2728`). Предел окна `chat._limit` (`:757–793`) сначала смотрит таблицу `CONTEXT` (`:108`), потом
список моделей шлюза; ручного значения нет. Проверка 413 хода (`create_turn`, `:2958–2962`; `_turn_context`,
`:2557–2561`) и подгонка результата инструмента (`_fit`, `:1105`) описаний инструментов не считают — их знают только
`/estimate` (`:2724`) и `_compact` (`:1254`). Отказ шлюза по длине (400 «maximum context length…») уходит человеку
словами шлюза (`:1168`). Признаков «локальная» два: `sections.is_local` (`app/sections.py:134`) — только роли
`local-*` srvllm, `card._local` (`app/card.py:110`) — ещё и s06 (находка FINDINGS.md 2026-11-25). Профиль
(`app/profiles.py`) — один набор параметров на подключение. Задача 10 (волна D) правит те же `create_turn` и
`_read_model` уже после этой задачи; её точки в этих функциях перечислены ниже.

**Files:**
- Modify: `app/sections.py:134–137`; `app/card.py` (`_entries.add` `:69–72`, удалить `_local` `:110–113`, слова
  шапки `:6–7`); `app/translate.py:638`; `app/profiles.py`; `app/mcp_client.py:707`; `app/checks.py:313`;
  `app/chat_pending.py:238, 294`.
- Modify: `app/chat.py` — новые `DEFAULT_RATIO`, `ratio`, `CONTEXT_WORDS`, `TOO_LONG_WORDS`, `CONTEXT_REFUSED`,
  `TOO_LONG`; `estimate_tokens`, `spec_tokens`; `_limit` → `model_window`; `_can_summarize`; `_max_images`;
  `StreamTask` (поля `ratio`, `spec_size`, `forced`), `_fit`, `_short_reasoning`, `_compact` (параметр `force`),
  `_await_scans` и `_context` (передают `ratio` и `spec_size`); в `_round` — одна ветка не-200; в `_read_model` —
  только `:1596–1601` (`spec_size`, ветка `TOO_LONG`) и условие `:1617`; `_plan_excerpts`, `_excerpt_plan`,
  `_turn_context`, `_estimated`; в `create_turn` — только `:2847` (окно, отношение, описания), `:2889` (параметры),
  `:2954–2962` (проверка) и аргументы `StreamTask` `:3014–3020`.
- Modify: `app/static/js/connection-profile.js`, `app/static/en.js`, `app/static/css/app.css` (`.profile-model`).
- Modify (ссылки на символы, сторож `tests/test_docs.py`): `docs/architecture/chat.md:567, 678` (`chat._limit` →
  `chat.model_window`), `docs/architecture/notifications.md:26` (`card._local` → `sections.is_local`).
- Create: `tests/test_chat_window.py`. Modify: `tests/test_chat.py` (помощник `window`; `:383–415` — после неудачи
  списка действует запомненное окно: ход `:410` — 413), `tests/test_s06.py`, `tests/test_sections.py:250`,
  `tests/test_connection_profiles.py`, `tests/test_ui_connection_profile.py`, `tests/test_chat_web.py:302–344`,
  `tests/test_chat_ocr.py:471`, `tests/test_chat_system.py:117`, `tests/test_chat_wait.py:147`,
  `tests/test_chat_delivery.py:89`, `tests/test_chat_pending.py:139–144`.

**Interfaces:**
- Consumes: задача 1 — `connection_profiles.model_params`, `model_facts`; задача 4 — `model_facts.ratio`
  (`chat.note_usage`); задача 7 — `_compact`, `_summarize`, `_estimated`, `SUMMARY_BLOCK`; задача 8 —
  `Tools(model=)`, `profiles.max_tools(conn, model)`.
- Produces:
  - `sections.is_local(conn, model) -> bool` = `conn.proxied and (conn.s06 or (conn.kind == "srvllm" and
    model.startswith(LOCAL_PREFIX)))`; `card._local` нет.
  - `profiles.Params.context_window: int | None` (`8192 … 4_000_000`, `strict`); `ProfileIn.model_params:
    dict[str, Params]` (не больше `sections.MODELS_MAX` (200) моделей, имя — 1…`sections.MODEL_MAX` (200) знаков,
    `_plain`); ключ `model_params` в ответе GET и PUT профиля (своего и общего).
  - `profiles.model_params(conn, model: str | None) -> dict` — параметры подключения, поверх — параметры модели;
    `profiles.request_params(conn, model=None)`, `profiles.max_images(conn, model=None)`,
    `profiles.max_tools(conn, model=None)`.
  - `chat.DEFAULT_RATIO = 3.0`; `chat.ratio(conn, model) -> float`; `chat.estimate_tokens(messages,
    ratio=DEFAULT_RATIO)`; `chat.spec_tokens(specs, ratio=DEFAULT_RATIO)`.
  - `async chat.model_window(conn, model) -> int | None` — вместо `_limit` (читает список моделей — сеть).
  - `chat.CONTEXT_WORDS = "Не помещается в контекст модели"` — начало слов любого отказа по длине (413 `context`,
    `messages.error`); по нему задача 15 узнаёт отказ для кнопки «Новый диалог с итогом».
  - Тестам: `window(studio, tokens, model="local-gemma-abl")` в `tests/test_chat.py` — запись модели в
    `studio.gw.models` с `context_length = tokens` и `chat.LIMITS.clear()`.

**Решения:**
- **Признак.** Следствия для s06 — те, о которых находка: кнопка «Рассуждение» (`thinking_ok`) и
  `chat_template_kwargs.enable_thinking` в каждом ходе, расшифровка без согласия «внешней модели», перевод с
  `temperature`, `max_tokens` и выключенным рассуждением (`translate_text._body`), предел 32 инструментов MCP без
  `max_tools` профиля и вызов текстом (задача 8). Release моделей картинок остаётся делом srvllm: условия
  `chat.py:1617` и `translate.py:638` получают `conn.kind == "srvllm" and`. `_short_reasoning` — без
  `or self.conn.s06` (стало лишним). `checks.py:307` и `cleaner_route.forced_refusal` не меняются.
- **Параметры на модель.** `model_params(conn, model)` = `{**params, **model_params.get(model, {})}`; у Cleaner —
  `{}`. В запрос уходят те же ключи, что и раньше (`request_params`); `context_window`, `max_images`, `max_tools` —
  никогда. Пустой набор модели не хранится; профиль с одними `model_params` — строка есть (`is_set`). Вызовы с
  моделью: `create_turn:2889`, `chat_pending:294`, `checks.probe:313`, `mcp_client:707`, `_max_images(conn, model)`
  в четырёх вызовах `app/chat.py` (`:1694`, `:2556`, `:2701`, `:2954`).
- **Счёт.** `ratio(conn, model)` — `model_facts.ratio` пары, иначе `DEFAULT_RATIO` (без подключения — он же).
  `estimate_tokens` = `ceil(знаки / ratio) + 1000 * картинок`, `spec_tokens` = `ceil(len(JSON) / ratio)`. Ход
  держит снимок отношения (`StreamTask.ratio`): `_fit`, `_compact`, `_summarize` (размер партии — знаки × `ratio`).
- **Окно** (`model_window`, п. 31(б)): (1) `profiles.model_params(conn, model).get("context_window")`; (2) список
  моделей шлюза — прежний код `_limit` с кэшем `LIMITS` (прямое подключение сервер не опрашивает); значение из
  списка пишется в `model_facts.window`; (3) список прочитать не удалось — `model_facts.window`; (4)
  `CONTEXT.get(model)`. Неудача списка, как раньше, не запоминается.
- **Описания инструментов.** `create_turn` до `BEGIN` (рядом с `:2847`): `rate = ratio(conn, model)`,
  `spec = spec_tokens(await _turn_specs(uid, row, conn, model), rate)` у хода через сервер не Cleaner, иначе 0
  (каталоги MCP — из кэша, холодный — не дольше `mcp_sources.DEFAULT_WAIT_SEC`). Проверка 413 —
  `estimate_tokens(…, rate) + spec`; то же в `_turn_context(…, ratio=, extra=)`, `_plan_excerpts`,
  `_can_summarize(conn, messages, limit, ratio, extra)`. `_read_model` на каждом круге ставит
  `self.spec_size = spec_tokens(specs if offer else [], self.ratio)`, `_fit` прибавляет его к занятому.
- **Отказ по длине.** `CONTEXT_REFUSED` — без регистра: `context[ _]length|context window|maximum context|too many
  tokens|prompt is too long|input is too long`. В `_round` ответ 400 с таким текстом даёт `(None, TOO_LONG, [],
  text)`; проверка — сразу после отказа самого Cleaner и отказа `stream_options`, до `builtin_only` и
  `profiles.refusal` (vLLM в такой ошибке называет `max_tokens`). `_read_model`: `ending is TOO_LONG` — при
  `self.forced` или неизвестном окне сразу `TOO_LONG_WORDS`; иначе `self.forced = True`,
  `await self._compact(specs if offer else [], force=True)` и тот же круг заново. `_compact(force=True)`: без выхода
  «и так помещается», граница — `self.history_end` (в пересказ уходит всё до сообщения хода); сжимать нечего
  (`history_end < 2` или `self.compacted`) — `TOO_LONG_WORDS`. `TOO_LONG_WORDS` = `CONTEXT_WORDS` + «: модель
  отклонила запрос по длине. Уберите документ или начните новый диалог.»; `_too_big` начинается с `CONTEXT_WORDS`.
- **Редактор.** В `PARAMS` — `context_window` («Окно контекста, токенов», 8192–4000000, целое; поле
  `#profile-context-window`). Под параметрами подключения — раздел «Параметры модели»: поле `#profile-model-name`
  (до 200 знаков, `datalist` из `conn.passport.models`, если паспорт есть), кнопка `#profile-model-add`, список
  `#profile-models`. Блок `.profile-model[data-model]` — имя модели, `.profile-model-remove` и те же поля `PARAMS`
  (`input[data-param=<ключ>]`, ошибка — `.error[data-for="<n>:<ключ>"]`, пустое поле — «как у подключения»,
  `top_k` у `openai` выключен). Повтор имени открывает уже добавленный блок. `collect()` шлёт `model_params` без
  пустых блоков. Подписи — `tr` и `en.js`, значок удаления — `icons.js` (`x`).

- [ ] **Step 1: Write the failing tests** — признак и параметры. `tests/test_s06.py` (фикстура `s06`; `sse`, `turn` —
  `test_chat`; `import card, translate_text`) и `tests/test_connection_profiles.py` (`new_chat`, `sse`, `turn`):

```python
def test_s06_models_are_local_everywhere(studio, s06):
    assert sections.is_local(s06, "qwen-27b") and not hasattr(card, "_local")
    cid = studio.c.post("/api/chats", json={"connection_id": s06.id, "model": "qwen-27b"}).json()["id"]
    view = studio.c.get(f"/api/chats/{cid}").json()["chat"]
    assert view["local"] and view["thinking_ok"]
    assert studio.c.patch(f"/api/chats/{cid}", json={"thinking": True}).status_code == 200   # было 409 no_thinking
    # обёртка on["/v1/chat/completions"]: первый запрос — 503 {"error": {"message": "slot busy"}}, дальше builtin
    assert sse(turn(studio, cid))[-1] == ("done", {"status": "done"})
    assert studio.gw.chat_requests[-1]["chat_template_kwargs"] == {"enable_thinking": True}
    assert "/slot/images/release" not in [r.url.path for r in studio.gw.requests]    # release картинок — только srvllm
    body = translate_text._body(s06, "qwen-27b", "system", "text", 0.2, 100)
    assert (body["temperature"], body["max_tokens"], body["chat_template_kwargs"]) == (0.2, 100, {"enable_thinking": False})
    links = next(s for s in studio.c.get("/api/sections").json()["sections"] if s["id"] == "chat")["links"]
    assert next(l for l in links if l["connection"]["id"] == s06.id)["local_models"] == sections.S06_CATALOG["chat"]

def test_model_params_override_the_connection_params(studio):
    cid = add_gateway(studio); url = f"/api/connections/{cid}/profile"
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]
    body = {"params": {"temperature": 0.3, "max_tools": 64},
            "model_params": {"tiny": {"temperature": 0.9, "context_window": 16384, "max_images": 2}, "empty": {}}}
    r = studio.c.put(url, json=body)
    assert r.status_code == 200 and r.json()["model_params"] == {"tiny": body["model_params"]["tiny"]}
    conn = connections.get(uid, cid)
    assert profiles.model_params(conn, "tiny") == {"temperature": 0.9, "max_tools": 64, "context_window": 16384,
                                                   "max_images": 2}
    assert profiles.model_params(conn, "other") == profiles.model_params(conn, None) == body["params"]
    assert profiles.request_params(conn, "tiny") == {"temperature": 0.9}
    assert profiles.request_params(conn) == {"temperature": 0.3}
    assert (profiles.max_images(conn, "tiny"), profiles.max_images(conn), profiles.max_tools(conn, "tiny")) == (2, 4, 64)
    studio.c.put(f"/api/sections/chat/links/{cid}", json={"models": ["local-gemma-abl", "tiny"]})
    sse(turn(studio, new_chat(studio, connection_id=cid, model="tiny")))
    sent = studio.gw.chat_requests[-1]
    assert sent["temperature"] == 0.9 and not {"context_window", "max_images", "max_tools"} & set(sent)
    assert studio.c.put(url, json={"model_params": {"tiny": {"top_p": 0.5}}}).json() == {
        "system_text": "", "params": {}, "suggestions": [], "model_params": {"tiny": {"top_p": 0.5}}, "provider": "srvllm"}
    assert own(studio, cid)["profile_set"] is True
    for bad in ({"": {}}, {"m" * 201: {}}, {"m": {"context_window": 8191}}, {"m": {"context_window": 4_000_001}},
                {"m": {"context_window": "32768"}}, {"m": {"seed": 1}}, {f"m{n}": {"top_p": 0.5} for n in range(201)}):
        assert studio.c.put(url, json={"model_params": bad}).status_code == 422, bad
```

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -n0 --assert=rewrite tests/test_s06.py
  tests/test_connection_profiles.py` — Expected: FAIL (`is_local` ложь у s06; `model_params` — 422 `extra_forbidden`).
- [ ] **Step 3: Implement** `sections.is_local`, `card`, два условия release, `_short_reasoning`; `profiles`
  (`Params.context_window`, `ProfileIn.model_params`, `_view`, `put`, `model_params` и параметр `model` трёх
  функций) и вызовы с моделью. В существующих тестах: `tests/test_connection_profiles.py:30, 36` — ключ
  `"model_params": {}` в ожидаемом ответе; `tests/test_sections.py:250` — слова о s06 в описании теста.
- [ ] **Step 4: Run** `tests/test_s06.py tests/test_sections.py tests/test_card.py tests/test_connection_profiles.py
  tests/test_profile_admin.py tests/test_translate_text.py tests/test_chat.py tests/test_chat_reasoning.py
  tests/test_chat_wait.py tests/test_checks.py tests/test_mcp_names.py tests/test_cleaner_forced.py
  tests/test_docs.py` — PASS.
- [ ] **Step 5: Commit** `Use one local-model check and add per-model profile parameters` (в теле — заголовок
  закрытой находки: «Два понятия «локальной модели»: s06 на карте, но не локальный»; запись из `FINDINGS.md` удаляет
  ведущий при слиянии).
- [ ] **Step 6: Write the failing tests** `tests/test_chat_window.py` (`HOST`, `server`, `tool` — `test_mcp_dialog`;
  `messages`, `new_chat`, `sse`, `turn`, `window` — `test_chat`; `limit_of(studio, cid)` — `limit` ответа
  `POST …/estimate {"text": ""}`):

```python
def test_window_is_manual_then_listed_then_remembered_then_the_table(studio):       # п. 31(б)
    gw = add_gateway(studio); cid = new_chat(studio); url = f"/api/connections/{gw}/profile"
    window(studio, 65536)
    assert limit_of(studio, cid) == 65536                                  # список шлюза сильнее таблицы CONTEXT
    assert state.DB.execute("SELECT window FROM model_facts WHERE connection_id = ? AND model = ?",
                            (gw, "local-gemma-abl")).fetchone()[0] == 65536
    assert studio.c.put(url, json={"params": {"context_window": 32768}}).status_code == 200
    assert limit_of(studio, cid) == 32768                                  # ручное у подключения
    studio.c.put(url, json={"params": {"context_window": 32768},
                            "model_params": {"local-gemma-abl": {"context_window": 16384}}})
    assert limit_of(studio, cid) == 16384                                  # ручное у модели — сильнее
    studio.c.put(url, json={})
    # обёртка on["/v1/models"] отвечает 503; chat.LIMITS.clear()
    assert limit_of(studio, cid) == 65536                                  # список не прочитан — запомненное
    state.DB.execute("DELETE FROM model_facts")
    assert limit_of(studio, cid) == chat.CONTEXT["local-gemma-abl"] == 131072      # прежняя таблица

def test_ratio_from_model_facts_scales_the_count(studio):                          # п. 31(а)
    text = [{"role": "user", "content": "я" * 6000}]
    assert (chat.estimate_tokens(text), chat.estimate_tokens(text, 2.0)) == (2000, 3000) and chat.DEFAULT_RATIO == 3.0
    specs = [{"type": "function", "function": {"name": "t", "description": "д" * 600, "parameters": {}}}]
    assert chat.spec_tokens(specs, 2.0) == math.ceil(len(json.dumps(specs, ensure_ascii=False)) / 2)
    gw = add_gateway(studio); cid = new_chat(studio)
    before = studio.c.post(f"/api/chats/{cid}/estimate", json={"text": "я" * 6000}).json()["tokens"]
    state.DB.execute("INSERT INTO model_facts(connection_id, model, ratio, updated) VALUES (?, ?, 2.0, ?)",
                     (gw, "local-gemma-abl", studio.clock[0]))
    after = studio.c.post(f"/api/chats/{cid}/estimate", json={"text": "я" * 6000}).json()["tokens"]
    assert after == pytest.approx(before * 1.5, rel=0.01)

def test_turn_check_counts_tool_descriptions(studio):                              # п. 31(в)
    add_gateway(studio); cid = new_chat(studio)
    server(studio, "/mine", tools=[tool(f"t{n}", description="д" * 1000) for n in range(12)])
    mine = studio.c.post("/api/mcp", json={"name": "Mine", "url": HOST + "/mine"}).json()["id"]
    studio.c.patch(f"/api/chats/{cid}", json={"mcp_ids": [mine]})
    window(studio, chat.RESERVE_TOKENS + 3000)                 # сообщение помещается, с описаниями 12 инструментов — нет
    r = turn(studio, cid, "Привет")
    assert (r.status_code, r.json()["detail"]["code"]) == (413, "context") and messages(studio, cid) == []
    assert r.json()["detail"]["message"].startswith(chat.CONTEXT_WORDS)
    studio.c.patch(f"/api/chats/{cid}", json={"mcp_ids": []})
    assert sse(turn(studio, cid, "Привет"))[-1] == ("done", {"status": "done"})

def test_context_refusal_retries_once_with_compaction_then_refuses_in_words(studio):   # п. 31(г)
    add_gateway(studio); cid = new_chat(studio)
    studio.gw.chat_script = [["Ответ-1"], ["Ответ-2"]]
    sse(turn(studio, cid, "Вопрос-1")); sse(turn(studio, cid, "Вопрос-2"))
    refuse, seen = [1], []
    # обёртка on["/v1/chat/completions"]: тело — в seen; запрос не пересказа (системное сообщение не SUMMARY_NOTE)
    # при refuse[0] > 0 уменьшает счётчик и отвечает 400 {"error": {"message": "This model's maximum context
    # length is 8192 tokens."}}, иначе studio.gw.builtin
    studio.gw.chat_script = [["Пересказ двух ходов."], ["Ответ-3"]]
    events = sse(turn(studio, cid, "Вопрос-3"))
    assert events[-1] == ("done", {"status": "done"}) and ("recovery", {"stage": "summary"}) in events
    assert len(seen) == 3                                                  # отказ, пересказ, повтор
    assert seen[2]["messages"][1]["content"] == chat.SUMMARY_BLOCK.format("Пересказ двух ходов.") + "Вопрос-3"
    assert messages(studio, cid)[-1]["content"] == "Ответ-3"
    refuse[0] = 9                                                          # отказ и после сжатия
    events = sse(turn(studio, cid, "Вопрос-4"))
    assert events[-1][0] == "error" and events[-1][1]["message"].startswith(chat.CONTEXT_WORDS)
    assert len(seen) == 6                                                  # отказ, пересказ, отказ — третьего нет
```

- [ ] **Step 7: Run** `.venv/Scripts/python -m pytest -q -n0 --assert=rewrite tests/test_chat_window.py` — Expected:
  FAIL (`cannot import name 'window'`, затем окно 131072 из таблицы и 200 вместо 413).
- [ ] **Step 8: Implement** `window` в `tests/test_chat.py`; `ratio`, `estimate_tokens`, `spec_tokens`,
  `model_window`, описания в проверках, `TOO_LONG` — по «Решениям». В тестах: правки `chat.CONTEXT` для
  `local-gemma-abl` (`tests/test_chat_web.py:306, 317`, `test_chat_ocr.py:471`, `test_chat_system.py:117`,
  `test_chat_wait.py:147`, `test_chat_delivery.py:89`) — помощником `window`; `tests/test_chat_pending.py:139–144` —
  `chat.model_window`; `tests/test_chat_web.py::test_other_400_on_the_first_round_is_the_gateway_words` — слова
  начинаются с `chat.CONTEXT_WORDS` (сжимать нечего), а не «шлюз отклонил запрос»; `tests/test_chat.py:409–411` —
  413 по запомненному окну. Слова `TOO_LONG_WORDS` — в `app/static/en.js` (сторож `tests/test_i18n_server.py`).
- [ ] **Step 9: Run** `tests/test_chat_window.py tests/test_chat.py tests/test_chat_web.py tests/test_chat_ocr.py
  tests/test_chat_system.py tests/test_chat_wait.py tests/test_chat_delivery.py tests/test_chat_pending.py
  tests/test_chat_summary.py tests/test_chat_prefix.py tests/test_knowledge.py tests/test_chat_suggest.py
  tests/test_mcp_dialog.py tests/test_i18n_server.py tests/test_docs.py`, затем быстрый набор — PASS.
- [ ] **Step 10: Commit** `Count the chat context by observed token ratio and the model window rule`.
- [ ] **Step 11: Write the failing test** `tests/test_ui_connection_profile.py` (его `DIALOG`, `open_profile`,
  `reload_page`; шаг `setup` теста `test_own_profile_is_saved_and_survives_reload` вынести в помощник
  `own_connection(name, cloud)`; `READ` — `eval`, отдающий `model_params` профиля подключения `name` через `API`):

```python
@pytest.mark.parametrize("width", [320, 1280])
def test_model_parameters_are_edited_and_survive_reload(browser, live, width):
    # шаги — как в test_own_profile_is_saved_and_survives_reload: own_connection, open_profile;
    # set_value("#profile-context-window", "32768"); "#profile-model-name" = "cloud-chat", нажатие "#profile-model-add";
    # в блоке "#profile-models .profile-model:nth-child(1)": [data-param=temperature] = "0.9",
    # [data-param=context_window] = "100"; "#profile-save" → eval outOfRange (текст .error[data-for$=context_window]
    # блока и is_open(DIALOG)); context_window = "16384"; eval dialogFits; "#profile-save"; reload_page(); окно
    # снова → eval saved (значение "#profile-context-window" и [data-model, temperature, context_window] блоков),
    # eval server (READ); нажатие ".profile-model-remove", "#profile-save" → eval removed (READ)
    assert not result.get("failed"), result
    values = result["values"]
    assert values["outOfRange"] == ["Целое от 8192 до 4000000", True]      # слова под полем, профиль не ушёл
    assert values["dialogFits"] is True                                    # scrollWidth окна <= innerWidth, NO_OVERFLOW
    assert values["saved"] == ["32768", [["cloud-chat", "0.9", "16384"]]]
    assert values["server"] == {"cloud-chat": {"temperature": 0.9, "context_window": 16384}} and values["removed"] == {}
    assert not problems(result), result.get("log")
```

- [ ] **Step 12: Run** `.venv/Scripts/python -m pytest -q -n0 -m integration tests/test_ui_connection_profile.py` —
  Expected: FAIL (нет `#profile-context-window`). **Implement** редактор по «Решениям». **Run** тот же файл,
  `tests/test_css_tokens.py`, `tests/test_i18n.py` (сторож словаря страницы), затем быстрый набор — PASS.
- [ ] **Step 13: Commit** `Edit per-model parameters and the context window in the connection profile`.

### Task 11: «Агенты»: навыки

Кнопки пустого чата берутся из `me.code_quick_actions` (`app/static/js/code-chat.js:353–363`); команды помощника —
список `#code-slash-command` (`code-opencode.js:12–31`). Отправка держится на одном `value` — и тексте поля, и тексте
задания (`submit`, `code-chat.js:1829`; `pendingInputs`, `:1895`; `acknowledgeInput`, `:1070`; `restoreInput`, `:1084`):
навык их разводит. `fileHints` (`:517`) перехватывает Enter раньше `enterSends` (`:2043`). Меню «Ветки и рабочие копии»
(`:394–407`) есть, только когда есть `busyCopy` или `forkChat`; чат с готовым текстом умеет `copyStart` (`code.js:89,
689,703`). Образец маршрута — `file_search` (`app/claude_code.py:838`). Строки — на `13d063ef`.

**Files:**
- Create: `app/static/js/code-skills.js`, `tests/test_code_skills.py`, `tests/test_ui_code_skills.py`
- Modify: `app/claude_code.py`, `app/project_access.py`, `app/agents.py`; `app/static/js/` — `code-chat.js` (точки
  подключения), `code-opencode.js`, `code.js`; `app/static/en.js`, `app/static/css/app.css`;
  `docs/architecture/agents-workspace.md`
- Test: `tests/test_code.py` (`BODIES`), `tests/test_ui_code_quick_actions.py`, `tests/test_ui_opencode_features.py`

**Interfaces:**
- Consumes: задача 2 — `link.hello["skills"]`, просьбы `skills`/`skill`, `FakeMachine.skills`; задача 3 — `slashMenu(
  field, { items, onPick })`, `skillChip(host)`; задача 5 — `GET /api/skills?scope=agents` → `{id, name, description,
  scope, quick, parts, origin}`, `GET /api/skills/{id}` → `+ text` (допущение: `origin` — ключ `skills.BUILTIN`).
- Produces (сервер): `GET /api/code/agents/{aid}/projects/{pid}/skills` → `{"items": [{"name", "description",
  "folder"}], "truncated": bool}`; `POST …/skills/text {name, folder}` → `{"text": str}`; без `hello.skills` — 409
  `unsupported` до моста; `claude_code.SKILL_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"`, `SKILL_TEXT_MAX = 64 * 1024`;
  в `/api/agents` — `skills: bool`; `project_access.READ_OPS` — плюс `skills`, `skill`.
- Produces (`code-skills.js`): чистые `mergeItems(library, project, commands) -> [{kind, id, name, description}]` и
  `skillFile(key, name, description, text) -> string`; `agentSkills(host, { aid, pid, agent, engine, text, chipHost,
  failed })` → `{ commands(rows), lead() -> Promise<{text, item} | null>, preset(item), clear(), label(text) -> {name,
  rest} | null, quick() -> Promise<[{id, name}]>, libraryText(id), menuItems({ changed, verify }), unmount() }`.
- Produces (страница): `chatView` получает `verifyWith(engine, text, skill)` и `startSkill`; узлы `#code-skill-chip`,
  `.code-user-skill`, `#code-skills-install-item`, `[data-verify="<engine>"]`, окно `#code-skills-install`
  (`input[name="code-skills-folder"]`, `#code-skills-install-go`, `li[data-key][data-state="written|exists|too_large"]`).

- [ ] **Step 1: Write the failing tests** `tests/test_code_skills.py` (`setup`, `url`, `detail`, `sent`, `ALPHA` — из
  `tests/test_code.py`; выдача участнику — `share` из `tests/test_project_access.py`):

```python
def skilled(studio):                                                               # мост 0.48.0
    aid, machine, link = setup(studio); link.hello["skills"] = machine.skills = True
    machine.projects["alpha"]["skills"] = {(".agents/skills", "acceptance"): {"description": "Приёмка", "text": "Сверь"}}
def test_project_skills_are_listed_and_read_without_logging(studio, caplog):
    assert studio.c.get(url(aid, "projects", ALPHA, "skills")).json() == {"truncated": False, "items": [
        {"name": "acceptance", "description": "Приёмка", "folder": ".agents/skills"}]}
    r = studio.c.post(url(aid, "projects", ALPHA, "skills", "text"), json={"name": "acceptance", "folder": ".agents/skills"})
    assert r.json() == {"text": "Сверь"} and all(w not in caplog.text for w in ("acceptance", "Приёмка", "Сверь"))
def test_old_bridge_gets_no_skill_requests(studio):                              # Review Focus 1
    aid, machine, link = setup(studio)                                             # hello без skills — мост 0.47
    assert studio.c.get("/api/agents").json()["items"][0]["skills"] is False
    for r in (studio.c.get(url(aid, "projects", ALPHA, "skills")),
              studio.c.post(url(aid, "projects", ALPHA, "skills", "text"), json={"name": "a", "folder": ".agents/skills"})):
        assert (r.status_code, detail(r)["code"]) == (409, "unsupported")
    assert not sent(machine, "skills") and not sent(machine, "skill")
# test_bad_fields_and_odd_bridge_answers: name "../x", "a/b", "" и folder "docs" — 422 без просьбы; строки моста с
#   folder "tmp" или name ".." отброшены, описание 500 → 200 знаков, 150 строк → 100; text длиннее SKILL_TEXT_MAX — 502
# test_member_and_viewer_read_project_skills: оба маршрута 200, просьба с shared_projects == [ALPHA] (и shared_readonly)
```

  `tests/test_ui_code_skills.py` (`integration`; помощники `code_*`, `API`, `NO_OVERFLOW` — из `tests/test_ui_browser.py`;
  свой навык заводит тест: `API.api('POST', '/api/skills', {name: 'Мой навык', text: 'Текст навыка', scope: 'agents'})`):

```python
@pytest.mark.parametrize("width", [320, 1280])
def test_slash_menu_calls_project_and_library_skills(browser, live, width):
    machine.skills = True
    machine.projects["alpha"]["skills"] = {(".agents/skills", "review-local"): {"description": "Ревью", "text": "Проверь diff"}}
    # ("type", "#code-text", "/review"), ("press", "Enter") → #code-skill-chip содержит «Навык: review-local», поле пусто
    # «только тесты», отправка → в .code-msg.user — .code-user-skill «Навык: review-local» и «только тесты», без «Проверь diff»;
    # второй ход (середина диалога): "/Мой", Enter, отправка без текста:
    assert [t["text"] for t in code_requests(machine, "turn")] == ["Проверь diff\n\nтолько тесты", "Текст навыка"]
    assert len(code_requests(machine, "skill")) == 1 and values["fits"]
    # eval чистых функций: mergeItems([навык библиотеки s1 с origin 'acceptance'], [навык проекта 'acceptance' в
    #   .agents/skills], [команда 'compact']) и skillFile('acceptance', 'Приёмка', 'Сверить: всё', 'Текст')
    assert values["merged"] == [["project", ".agents/skills/acceptance"], ["command", "compact"]]   # один раз — проектный
    assert values["file"] == '---\nname: acceptance\ndescription: "Приёмка — Сверить: всё"\ndisable-model-invocation: true\n---\n\nТекст\n'
def test_old_bridge_has_library_skills_only(browser, live):                       # Review Focus 1
    # machine по умолчанию (0.47): "/Мой", Enter → чип, ход уходит с «Текст навыка»; пункта #code-skills-install-item нет
    assert not code_requests(machine, "skills") and not code_requests(machine, "skill")
@pytest.mark.parametrize("width", [320, 1280])
def test_set_goes_into_the_project_once_and_review_opens_another_helper(browser, live, width):
    machine.skills = machine.file_operations = machine.file_upload = True; machine.codex = CATALOG     # test_codex
    machine.projects["alpha"]["files"] = {".agents/skills/spec/SKILL.md": "своё".encode()}
    # история: ответ с "progress": {"changes": {"files": [{"path": "src/app.py", "added": 2, "removed": 1}]}}
    # #code-branch-actions (на 320 — сначала #code-status-more) → #code-skills-install-item → #code-skills-install-go
    files = machine.projects["alpha"]["files"]; made = files[".agents/skills/acceptance/SKILL.md"]
    assert files[".agents/skills/spec/SKILL.md"] == "своё".encode()              # data-state="exists": не перезаписан
    assert made.startswith(b"---\nname: acceptance\ndescription: ") and b"disable-model-invocation: true" in made
    assert not any("/parts/" in f or "/minutes/" in f for f in files)            # навыки «Чата» в проект не кладутся
    # меню → [data-verify="codex"] → новый чат: dataset.engine === 'codex', чип — навык с origin acceptance, в поле «src/app.py»
    assert code_requests(machine, "turn") == []                                    # отправляет человек
```

  Правки существующих: `test_ui_code_quick_actions.py::test_quick_action_keeps_the_typed_draft_and_can_be_retried` —
  кнопки — навыки библиотеки с `quick`, ожидаемый текст хода — из `GET /api/skills/{id}`; `test_ui_opencode_features.py`
  — вместо `set_value('#code-slash-command', 'compact')`: `("type", "#code-text", "/comp"), ("press", "Enter")`, поле
  `'/compact '`, дальше «Проверь проект»; `BODIES` — `GET …/projects/1/skills` (`None`) и `POST …/projects/1/skills/text`.
- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -n0 --assert=rewrite tests/test_code_skills.py tests/test_code.py`
  — Expected: FAIL (404 маршрутов, `KeyError: 'skills'`); `… -m integration tests/test_ui_code_skills.py` — FAIL.
- [ ] **Step 3: Implement — сервер.** `project_skills`: `own_agent`, `check_pid`, `require_capability(aid, "skills")`,
  `ask(request, aid, "skills", project=pid)`; из ответа — не больше 100 строк, только с именем по `SKILL_NAME` и папкой
  `.agents/skills` или `.claude/skills`, описание — строка до 200 знаков. `project_skill_text`: тело `ProjectSkill(name:
  str = Field(pattern=SKILL_NAME), folder: Literal[".agents/skills", ".claude/skills"])` — имя телом POST; текст длиннее
  `SKILL_TEXT_MAX` — 502 `bridge`; в журнал — только код отказа. `agents.py`: `"skills": hello.get("skills", False)`.
- [ ] **Step 4: Implement — меню и отправка.** `items()` меню: `GET /api/skills?scope=agents` и, только при
  `agent.skills`, `GET …/skills` (отказ списка проекта — библиотека без него; `closed` — в `failed`). `mergeItems`:
  навыки проекта (`kind: 'project'`, `id` — `<папка>/<имя>`), библиотека (`kind: 'skill'`) без тех, чей `origin` равен
  имени навыка проекта, команды помощника (`kind: 'command'`). `onPick`: команда — `'/' + name + ' '` перед остатком
  поля (как прежний список); навык — `chip.set(item)` и снять из поля набранное «/фильтр», если `slash.js` его оставил.
  `lead()` — текст выбранного навыка (`GET /api/skills/{id}` или `POST …/skills/text`). В `submit` (не `quick`): `typed
  = text.value`, `value = [lead.text, typed].filter((s) => s.trim()).join('\n\n')`; длиннее 100 000 знаков — слова
  «Навык вместе с уточнением длиннее 100 000 знаков» без запроса; `pendingInputs` хранит ещё `typed` и `lead.item`:
  `acknowledgeInput` и ветка уточнения (`steer`) сверяют поле с `typed`, `restoreInput` возвращает `typed` и чип
  (`preset`); пока `lead()` ждёт ответа, отправку запирает флаг `composing`. `label(text)` узнаёт сообщение, начатое
  текстом навыка, отправленного с этой страницы (память модуля, 20 последних): `messageNode` рисует `.code-user-skill`.
  `agentSkills` создаётся раньше `enterSends`; `paintSettings` зовёт `commands(data.commands)`, список `command` — прочь.
- [ ] **Step 5: Implement — кнопки, набор, проверка.** Кнопки пустого чата: `quick()` — навыки библиотеки с `quick`;
  нажатие — `libraryText(id)` → `submit(text, false)`; `me.code_quick_actions` страница не читает. «Положить набор в
  проект» — при `!agent.shared`, не наблюдателю, с `agent.skills`, `agent.file_operations`, `agent.file_upload`: окно
  с выбором папки (по умолчанию `.agents/skills`); набор — навыки библиотеки со `scope` не `chat` и `origin` вида
  `^[a-z][a-z0-9-]{0,63}$`; на каждый — `create_dir` трёх уровней (`POST …/files/operate`, 409 `exists` — дальше) и
  `PUT …/files/upload` без `X-Studio-Replace` (409 `exists` — состояние `exists`; файл больше 64 КиБ — `too_large`
  без запроса). `skillFile`: `name: <ключ>`, `description:` — `JSON.stringify` строки «имя — описание» до 200 знаков,
  `disable-model-invocation: true`, пустая строка, текст, `\n`. «Проверить другим помощником: {0}» — пункт на каждый
  другой помощник компьютера (`engineOrder(me?.code_defaults)`; `claude` или каталог `agent[engine]`), не наблюдателю:
  навык с `origin === 'acceptance'` (нет — слова «Навыка «Приёмка» нет в вашей библиотеке — добавьте умолчания в
  настройках»), текст — «Изменённые файлы последнего ответа:» и до 200 строк `- путь` из `progress.changes().files`;
  `verifyWith` в `code.js` — `POST …/chats?engine=…`, `copyStart = { aid, pid, chat, text, skill }`, `toChat`; `chat()`
  передаёт `startSkill`. Меню «Ветки и рабочие копии» строится при любом пункте. Подписи — `tr`, `en.js`; стили — токены.
- [ ] **Step 6: Run** `tests/test_code_skills.py tests/test_code.py tests/test_project_access.py tests/test_i18n.py
  tests/test_i18n_server.py tests/test_css_tokens.py tests/test_docs.py` — PASS; `-m integration` три браузерных — PASS.
- [ ] **Step 7: Commit** `Serve project skills to the Agents page`; `Call skills from the Agents task field`; `Put the
  skill set into a project and review with another helper` — с ним `agents-workspace.md` (навыки вместо абзаца `:490–496`).

### Task 13: «Чат»: «Продолжить иначе», стрелки, черновик на диалог, очередь «После ответа», вставка файлов

У «Чата» поле одно на раздел: черновик — `sectionDraft('chat', input)` в памяти вкладки (`chat.js:809`), «Отправить»
во время ответа молча не делает ничего (`submit` :2404 выходит на `jobs.has(chatId)`), файлы приходят только из
`input#attach` (:2785–2797), стрелок и веток нет. Ход защищён от повтора квитанцией `request_id`
(`chat_turn_requests`, `chat.py:2655`) — id элемента очереди и будет `request_id`. Новый ход снимает ждущую карточку
(`chat_pending.supersede`, `chat.py:2884`) — поэтому при карточке очередь стоит. Файл вложения держат все строки
`attachments` с его `media_id` (`media.REFERENCES`, `media.py:456`; `chat.purge` :2182 сверяет `media.referenced`) —
ветка ссылается на те же файлы новыми строками; документы знаний больших вложений заводятся на ходу
(`_excerpt_plan` :2525 → `knowledge.adopt`), копировать их не нужно.

**Files:**
- Create: `app/chat_fork.py`; `app/static/js/chat-drafts.js`, `app/static/js/chat-queue.js`, `app/static/js/chat-drop.js`
- Modify: `app/main.py` (`include_router(chat_fork.router)` после `chat_pending.router` :215), `app/static/js/chat.js`,
  `app/static/css/app.css` (`.chat-history`, `#queue-add`), `app/static/en.js` (и слова отказа сервера — сторож
  `tests/test_i18n_server.py`), `CLAUDE.md` (карта модулей: `chat_fork`), `docs/architecture/chat.md`
- Test: Create `tests/test_chat_fork.py`, `tests/test_ui_chat_carryover.py`
- Подключение в `chat.js`: импорты — после :104 (`import { sectionDraft } …`); блок после `if (input.value)
  autosize();` :810 (черновики, стрелки с обёрткой ленты, очередь, `#queue-add`, `fileDrop`); `messageNode()`
  :1849–1851 — кнопка `.msg-fork`; `renderFeed()` :2107 — `nav.paint()`; `syncBusy()` :2155–2161; `acknowledgeTurn()`
  :2174–2184; `open()` :2234–2237; `submit()`, ветка `busy` :2438–2440; `dropEmpty()` :2461 и `createChat()` :2497 —
  `dialogDrafts.rename`; `turn()` :2630–2632 и :2643–2644; обработчики `input` :2775–2779, `submit` формы
  :2781–2784, `change` файла :2785–2797 (тело — в `addFiles(files)`); `destroy()` — первой строкой :2862.

**Interfaces:**
- Consumes: `messageNav`, `draftStore(CHAT_DRAFTS)` (задача 3); `queueView(host, { endpoint, failed, request })`
  (`code-queue.js`, без правок); `openWith(id, text)` (задача 6); столбцы шага 82 (задача 1).
- Produces:
  - `POST /api/chats/{chat_id}/fork {message_id}` → 201 `{chat_id, text}`; отказы: 404 `gone` (нет диалога или
    своего выбранного сообщения), 409 `cleaner` со словами `chat_fork.CLEANER = "диалог через Cleaner не ветвится:
    замены Cleaner привязаны к диалогу"`, 409 `not_ready` (`chat.NOT_READY`).
  - `chat_fork.new_dialog(uid: int, row, *, title: str | None = None) -> str` — диалог-наследник (его же берёт
    `/continue` задачи 15, если идёт после этой). Наследует `connection_id`, `model`, `mcp_model_sid`, `role_id`,
    `thinking`, `effort`, `mcp_ids`, `mcp_off`, `mcp_tools_off`, `protected_used`, строки `chat_kbs`, название; `web`
    — исходный, при `protected_used` — 0; `created = updated = seen = clock.NOW()`. Не наследует `web_consent`,
    `mcp_file_consent`, `pinned`, `summary*`, `chat_media`, паузы.
  - `chatDrafts(input, { autosize }) -> { enter(id), leave(id), typed(id), sent(id, text), rename(from, to) }` (id
    `null` — новый диалог, ключ `'new'`); `QUEUE_MAX = 5`, `chatQueue(host, { send, failed }) -> { add(id, text):
    bool, show(id), idle(id, free), done(itemId), unmount() }`; `fileDrop(form, add)`.

- [ ] **Step 1: Write the failing test** `tests/test_chat_fork.py` (быстрый; `new_chat`, `turn`, `sse`, `messages`,
  `attach`, `png` — `test_chat`; `setup`, `tool`, `name_of`, `offered`, `view` — `test_mcp_dialog`; `protect`,
  `web_dialog`, `names`, `flags`, `code_of` — `test_protected_source`; `call` — `test_chat_web`; `base` —
  `test_knowledge`; фикстуры `cleaner_gw` — `test_cleaner_route_chat`, `protected` — `test_cleaner`):

```python
KEEP = ("connection_id", "model", "role", "mcp_ids", "thinking", "title")

def test_fork_copies_history_before_the_message_and_shares_files(studio, caplog):
    # диалог с базой знаний; ход «Первый вопрос» с notes.txt и p.png; «Ответить заново»; ход «Второй вопрос»
    first, answer, second, _ = messages(studio, cid)
    out = studio.c.post(f"/api/chats/{cid}/fork", json={"message_id": second["id"]})
    assert out.status_code == 201 and out.json()["text"] == "Второй вопрос"
    copy = messages(studio, new)
    assert [(m["role"], m["content"]) for m in copy] == [("user", "Первый вопрос"), ("assistant", answer["content"])]
    assert {m["id"] for m in copy}.isdisjoint({first["id"], answer["id"]}) and "variants" not in copy[1]
    assert [(a["name"], a["media_id"]) for a in copy[0]["attachments"]] == [("notes.txt", doc["media_id"]), ("p.png", pic["media_id"])]
    assert {a["id"] for a in copy[0]["attachments"]}.isdisjoint({doc["id"], pic["id"]})
    assert [view(studio, new)[k] for k in KEEP] == [view(studio, cid)[k] for k in KEEP]
    assert studio.c.get(f"/api/chats/{new}").json()["knowledge"] == [kb] and len(messages(studio, cid)) == 4
    studio.c.delete(f"/api/chats/{new}"); studio.c.portal.call(chat.purge, uid, new)
    assert media.alive(uid, doc["media_id"]) and media.alive(uid, pic["media_id"])    # файлы держит исходный диалог
    for secret in ("Первый вопрос", "Второй вопрос", "notes.txt"): assert secret not in caplog.text

def test_fork_point_rules(studio):
    assert fork(with_skill)["text"] == "уточнение"   # skill {"name": "Разбор", "chars": 6, …}, content «НАВЫК.\n\nуточнение»
    assert messages(studio, fork(first_message)["chat_id"]) == []                       # ветка от первого сообщения
    for bad in (answer_id, unchosen_id, "0" * 32): assert code_of(post(cid, bad), 404) == "gone"
    assert code_of(post(foreign_chat, foreign_message), 404) == "gone"                  # чужой диалог
    assert code_of(post(cid, after_running_scan)) == "not_ready" and count_chats() == before   # скан истории ещё идёт

def test_fork_of_a_protected_dialog_stays_protected(studio):                            # Review Focus 3
    sid, calls, _ = setup(studio, tools=[tool("lookup"), tool("search", annotations={"openWorldHint": True})])
    protect(studio, sid); cid = web_dialog(studio); sse(turn(studio, cid, "До источника"))   # диалог ещё не помечен
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="1")]}, ["Готово"]]
    sse(turn(studio, cid, "Use tool")); assert flags(cid) == (1, 0)
    new = fork(messages(studio, cid)[2])["chat_id"]        # точка — второе сообщение: в ветке история ДО срабатывания
    assert flags(new) == (1, 0) and view(studio, new)["protected"] is True and view(studio, new)["web"] is False
    assert code_of(studio.c.patch(f"/api/chats/{new}", json={"web": True})) == "protected"
    studio.gw.chat_script = [["Ответ"]]; sse(turn(studio, new, "Вопрос в ветке"))
    assert names(studio.gw.chat_requests[-1]) == [] and offered(studio.gw.chat_requests[-1]) == ["Shared tools: lookup"]
    assert code_of(studio.c.post(f"/api/chats/{new}/share", json={"pin": "0123"})) == "protected"

def test_fork_refuses_cleaner_dialogs_in_words(cleaner_gw):       # и второй тест с фикстурой `protected` (вид Cleaner)
    refused = s.c.post(f"/api/chats/{routed}/fork", json={"message_id": mid})
    assert refused.status_code == 409 and refused.json()["detail"] == {"code": "cleaner", "message": chat_fork.CLEANER}
    assert count_chats() == before                                 # диалога нет
```

- [ ] **Step 2: Run** `… tests/test_chat_fork.py` — Expected: FAIL (404/405: маршрута `/fork` нет).
- [ ] **Step 3: Implement** `app/chat_fork.py`. Порядок маршрута: `chat._chat_row`; цель — `chat._user_turn(chat_id,
  message_id)` с `unchosen = 0`; `conn = chat._chat_connection(…)`, при `conn.cleaner or conn.routed` —
  `cleaner_client.refused(CLEANER)` (решение 5 спеки); документ истории до цели с `text IS NULL` и `ocr.pending(uid,
  [id])` → 409 `not_ready`; `operations.check(uid)`; дальше без `await`, одной транзакцией (`BEGIN` … `COMMIT`, отказ —
  `ROLLBACK`): `new_dialog(uid, row)`; выбранные сообщения с `(created, rowid)` меньше цели — новыми id в том же
  порядке, столбцы `role, content, status, via, model, created, tools, reasoning, reasoning_sec, error, todo, skill,
  usage, gen_ms, total_ms` (без `cleaner_raw`, `cleaner_request`, `alive_at`); их вложения — новыми id с теми же
  `kind, name, media_id, text, chars, created, source_transcript_id`, `cleaner_used = 0`. `text` ответа — `content`
  цели без первых `skill.chars` знаков и ведущих переводов строки. Вложения самого сообщения-цели не переносятся.
  Журнал — `чат %s: ветка %s, сообщений %d` (id — по 8 знаков).
- [ ] **Step 4: Run** `tests/test_chat_fork.py tests/test_protected_source.py tests/test_cleaner_route_chat.py
  tests/test_media_orphans.py tests/test_docs.py tests/test_i18n_server.py` — PASS.
- [ ] **Step 5: Commit** `Add chat branching from an own message`.
- [ ] **Step 6: Write the failing test** `tests/test_ui_chat_carryover.py` (`integration`; `signin`, `READY`,
  `open_new_chat`, `ANSWERED`, `API`, `attach`, `known_refusals`, `problems` — `test_ui_browser`; `ASK` —
  `test_ui_chat_pending`; удержание ответа — `live.gw.chat_hold`; `users` — тексты `#messages .msg.user .bubble`,
  `queued` — тексты `#code-queue .code-queue-preview`):

```python
def test_queue_sends_once_after_the_answer(browser, live):                 # Review Focus 4
    # «Первый» — ответ удержан; «Второй» + Enter; ещё четыре; шестое; отпустить шлюз
    assert v["queued"] == ["Второй"] and v["field"] == "" and v["later"] is True       # #queue-add виден, пока идёт ответ
    assert v["sixth"] == ["В очереди не больше 5 сообщений", "Шестое"]                 # #chat-error; текст остался в поле
    assert v["users"] == ["Первый", "Второй", "3", "4", "5", "6"] and len(live.gw.chat_requests) == before + 6

def test_queue_survives_a_turn_from_another_device(browser, live):         # Review Focus 4: 409 busy
    # fetch подменён: перед POST …/turns с «Второй» тот же API начинает удержанный ход «с телефона»
    assert v["held"] == ["Второй"] and v["error"] == "Ответ ещё идёт — дождитесь или остановите"
    assert v["users"] == ["Первый", "с телефона", "Второй"] and v["saved"].count("Второй") == 1   # ушло один раз
    assert not known_refusals(problems(r), 409)

def test_queue_waits_for_the_pending_card(browser, live):                  # Review Focus 4: ждущая карточка
    # chat_script = [{"text": ["Уточню."], "tool_calls": [("ask_user", ASK)]}, ["Выбран красный."], ["Третий ответ."]]
    assert v["waiting"] == [["Потом"], 1, True]      # через 1,5 с после паузы: очередь, сообщений пользователя, карточка
    assert v["after"] == ["Нарисуй", "Потом"] and len(live.gw.chat_requests) == before + 3   # после ответа на карточку

def test_closed_page_returns_the_queue_to_the_draft(browser, live):        # Review Focus 4: закрытие страницы
    # ответ удержан; в очереди «Вернётся», в поле «Набранное»; window.dispatchEvent(new Event('pagehide')); reload
    assert v["stored"] == "Набранное\n\nВернётся" == v["field"]             # studio-chat-drafts[id] и поле после reload
    assert v["users"] == ["Первый"]                  # шлюз отпущен, ожидание: «Вернётся» само не ушло

def test_draft_stays_with_its_dialog(browser, live):
    assert v["switch"] == ["", "Для А"] and v["new"] == "Для нового"        # Б пуст; назад в А; у «Нового чата» — свой
    assert v["reload"] == "Для А" and v["sent"] is False and v["locked"] is None   # sessionStorage; отправка и блокировка стирают

@pytest.mark.parametrize("width", [320, 1280])
def test_arrows_expand_all_and_fork_button(browser, live, width):
    assert v["ids"] == ["chat-message-up", "chat-toggle-steps", "chat-message-down"]
    assert v["up"] == "Вопрос 3" and v["clear"] is True and v["fits"] is True   # стрелки не лежат на последнем пузыре
    assert v["forked"] == [True, ["Вопрос 1"], "Вопрос 2"]   # .msg-fork второго сообщения: новый адрес, лента, поле

def test_pasted_and_dropped_files_become_attachments(browser, live):
    assert v["chips"] == ["p.png", "notes.txt"]              # ClipboardEvent paste в поле; DragEvent drop на #composer
    assert v["unfit"] == "поддерживаются PDF, DOCX, текстовые файлы и код, фото PNG, JPEG, WebP"   # .exe — как у скрепки
    assert v["text_paste"] == "просто текст"                 # вставка текста без файлов — обычная
```

- [ ] **Step 7: Run** `… -m integration tests/test_ui_chat_carryover.py` — Expected: FAIL (нет `#queue-add`, `.msg-fork`,
  `#chat-message-up`).
- [ ] **Step 8: Implement — черновик и стрелки.** `chat-drafts.js` поверх `draftStore(CHAT_DRAFTS)`: `enter(id)`
  ставит в поле сохранённый текст диалога (или пусто), `leave(id)` и `typed(id)` пишут `input.value` (пустое —
  удаляют), `sent(id, text)` удаляет, если сохранён тот же текст, `rename` переносит запись (`'new'` ↔ id диалога).
  В `chat.js`: `open()` — после `closeEdit()` `dialogDrafts.leave(chatId)` (кроме первого открытия), после `chatId =
  id` — `dialogDrafts.enter(id)`, затем `prefill` задачи 6; `input` — `if (!editing) dialogDrafts.typed(chatId)`;
  `destroy()` — `leave(chatId)` и `inputDraft.save(false)` (`sectionDraft` остаётся ради защиты от автоперезагрузки,
  общий черновик раздела больше не хранится). Стрелки: `const history = el('div', { class: 'chat-history' });
  scroller.replaceWith(history); history.append(scroller, nav.node)`, `nav = messageNav({ scroller, feed, prefix:
  'chat', own: '.msg.user', steps: 'details.web-steps, details.think', expandLabels: [tr("Развернуть все шаги"),
  tr("Свернуть все шаги")], stuck: () => stick, follow: (on) => { stick = on; found = null; } })`; CSS —
  `.chat-history { position: relative; flex: 1; min-height: 0; display: flex; flex-direction: column; }`, у ленты
  справа — место под стрелки.
- [ ] **Step 9: Implement — ветка, очередь, вставка.** `.msg-fork` («Продолжить иначе», после «Изменить»): `POST
  …/fork`, затем `openWith(chat_id, text)`; у сообщения с вложениями — «Вложения сообщения в новый диалог не
  перенесены» в `#chat-error`; отказ — слова сервера. Очередь (`chat-queue.js`): `Map` id диалога → `{ items: [{id:
  crypto.randomUUID(), text, version, sending}], paused }` в памяти страницы; вид — `queueView(host, { endpoint: (tail)
  => tail, request, failed })`, `request` отвечает сам: `/queue/pause` → `{items}`, `/queue` — правка или удаление по
  `request_id` (элемент с `sending` — отказ с кодом `gone`). `#queue-add` (`iconButton('wait', tr("После ответа"))`
  рядом со `#stop`) виден, пока `jobs.has(chatId) || remoteStream() || otherTab()`; он и Enter в это время зовут
  `enqueue()`: при правке сообщения — нет; с вложениями в поле — «В очередь встаёт только текст — вложения отправьте
  после ответа»; шестое — «В очереди не больше 5 сообщений»; принято — поле и черновик пусты. `syncBusy()` зовёт
  `queue.idle(chatId, free)`; `free` — ответ не идёт, `!sending`, нет правки, нет карточки (`chat.messages.some((m) =>
  liveOf(m).pending)`); тогда первый элемент уходит через `queuedTurn(item)` (как `voiceTurn` :1300: текст в поле на
  время `submit(false)`, набранное возвращается). В `turn()` попытка получает `requestId: item.id` и `queued:
  item.id`; исходы: `acknowledgeTurn` → `queue.done(id)`; `busy` — элемент остаётся до следующего `idle`; исход не
  известен (:2643) — остаётся `sending` до квитанции (`reload` :2194–2206); прочий отказ — очередь на паузе, слова в
  `#chat-error`, «Продолжить очередь» повторяет. Очередь уходит, только пока её диалог на экране; `pagehide`
  (слушатель модуля) дописывает не отправленные и не `sending` тексты к черновику диалога через пустую строку и
  очищает очередь. `fileDrop(form, addFiles)` — `paste` с файлами в `clipboardData`, `dragover` и `drop` с `Files`,
  как `code-chat.js:674–688`; `addFiles` — прежнее тело обработчика `change` скрепки.
- [ ] **Step 10: Run** `-m integration tests/test_ui_chat_carryover.py tests/test_ui_send_reliability.py
  tests/test_ui_chat_pending.py tests/test_ui_chat_composer.py tests/test_ui_page_guards.py tests/test_ui_chat_focus.py`
  и `tests/test_ui_browser.py -k "chat or transcript_to_chat"`; быстрые `tests/test_i18n.py tests/test_css_tokens.py` — PASS.
- [ ] **Step 11: Commit** `Add chat branching button, per-dialog drafts, send-after-answer queue and file paste`.

## Волна D

### Task 10: Навыки в «Чате» и инструмент `run_part`

Навык в «Чате» — текст в начале сообщения пользователя; часть — отдельный запрос той же модели с чистым контекстом.
Волна D: к этому времени слиты задачи 4, 7, 8, 9 (те же функции `app/chat.py`) и 3, 6 (страница); в волне порядок
слияния — 10 → 14 → 15 → 16. Номера строк мест правки (шаги 3, 7, 11) — по коммиту `13d063ef`, к волне D они
сдвинутся: якорь — имя функции. Что нашли неочевидного: сообщение пользователя пишут два места —
`create_turn` и `create_mcp_turn`, а Cleaner с предпросмотром собирает запрос из `body.text` (`_cleaner_messages`,
`app/chat.py:2581`); незнакомый вид события `_events` превращает в `error` и закрывает поток
(`app/chat.py:1869–1876`); `web.public` срезает `text` шага (`app/web.py:435`); `chat_tools.call` любое незнакомое
имя разбирает как `ask_user` (`app/chat_tools.py:231–244`), `for_model` падает на шаге без `text` и `error` (`:100`);
пауза хранит счётчики хода в payload (`app/chat_pending.py:112`); в тестах `STREAM_TOTAL_SEC = inf`, а часы двигают
пинги (`tests/conftest.py:308–314`, фикстура `no_pings`).

**Files:**
- Create: `app/chat_skills.py`, `app/chat_parts.py`, `app/static/js/chat-skills.js`, `app/static/js/chat-parts.js`,
  `tests/test_chat_skills.py`, `tests/test_chat_parts.py`, `tests/test_ui_chat_skills.py` (integration)
- Modify: `app/chat.py` (только названное в шагах 3 и 7), `app/chat_tools.py`, `app/chat_pending.py` (`pause`),
  `app/web.py` (`public`), `app/chat_shares.py` (`regular_snapshot`), `app/chat_exports.py` (`_parts`, `_tool_line`),
  `app/takeout.py` (`_chat`), `app/static/js/chat.js` (точки подключения шага 11), `app/static/js/chat-status.js`
  (параметр `parts`), `app/static/js/share-view.js`, `app/static/css/app.css`, `app/static/en.js`, `CLAUDE.md` (карта
  модулей: `chat_skills`, `chat_parts`, оба `.js`)

**Interfaces:**
- Consumes: `messages.skill` (Task 1); `skills.get`, `GET /api/skills?scope=chat&lang=` (Task 5); `slashMenu(field,
  { items, onPick, host })` → `{ close(), unmount() }`, `skillChip(host, { changed })` → `{ node, set(item), clear(),
  value() }` (узел `#skill-chip`) из `slash.js` (Task 3); `chatStatus(host, {…})` из `chat-status.js` (Task 6: сам
  собирает ряд `actions` и зовёт `statusActions`); `StreamTask._round` с `usage` и временем, `sse(…, metrics=False)`
  (Task 4); `compose` с `_for_history` и `_last_skill` — читает `messages.skill` и `content[:chars]` (Task 7); имена
  инструментов MCP `mcp_<сервер>__<инструмент>`, `name_of(sid, tool)`, правило структурного вызова в `_read_model`
  (Task 8); конец круга `TOO_LONG`, `StreamTask.spec_size`, `profiles.request_params(conn, model)` (Task 9).
- Produces:
  - `chat.TurnIn` (ход и предпросмотр Cleaner): `skill_id: str | None` (1–64 знака), `skill_keep: bool = False`
    (правка сообщения с навыком: оставить его прежний текст); `chat.EstimateIn.skill_id`. Сообщение: `content =
    текст навыка + "\n\n" + текст человека` (без текста человека — только текст навыка), `messages.skill = {"name",
    "chars", "parts"}`; в JSON сообщения — `skill: {name, chars, parts} | null`. Отказы: чужой или несуществующий
    навык — 404 `gone`; навык области `agents` — 422 `skill`; `skill_id` вместе с `skill_keep` или `regenerate`,
    `skill_keep` без правки или у сообщения без навыка — 422 `invalid`.
  - `chat_skills.apply(uid, body, edit) -> tuple[str, str | None]` (текст сообщения и JSON для `messages.skill`),
    `view(row) -> dict | None`, `human(row) -> str` (`content[chars:]` без ведущих `"\n\n"`), `title(mark) -> str |
    None` (имя навыка — название диалога), `preview(uid, text, skill_id) -> str` (оценка контекста: незнакомый
    навык — просто `text`).
  - `chat_tools.RUN_PART` — функция `run_part`, аргументы `{title: str, task: str}` (оба обязательны);
    `chat_tools.PARTS_MAX = 10`, `PART_RESULT_MAX = 4000`, `PART_ROUNDS = 8`, `PART_TITLE_MAX = 200`,
    `PART_TASK_MAX = 20_000`, `PART_EXCLUDED = {"run_part", "todo_write", "ask_user", "memory_save"}`,
    `parts_offered(chat_id) -> bool`, `part_specs(task) -> list[dict]`; `chat.PARTS_TOTAL_SEC = 7200`.
  - Шаг ответа `{"kind": "part", "n": 1…10, "title", "state": "running"|"done"|"error", "ms": int, "text": str}`,
    у `state: "error"` — ещё `error: str`; отказ до запуска — без `n` и `ms`. Шаги инструментов внутри части —
    обычные шаги с `"part": n` и **пустым** `text: ""` (ключ обязателен: прежний `compose` падает на шаге без
    `text` и `error`, а откат на прежний код обязан работать; в историю следующих ходов новый код такие шаги не
    берёт по `part`). Событие SSE `part` — `{n, title, state}` при запуске и `{n, title,
    state, ms}` при итоге; событие `tool` — `{kind: "part", label: title}`.
  - Слова: `chat_parts.BAD_ARGS`, `ONE_PER_ROUND`, `LIMIT`, `TIME`, `EMPTY`, `STOPPED`, `NEEDS_CONFIRM`;
    `chat_tools.NOT_IN_PART`, `PART_CUT` (их зовёт сам `chat_tools`: `chat_parts` он не импортирует — цикл).
  - Узлы страницы: `#chat-skill` (чип над полем), `.msg-skill`, `.web-part`, `#chat-parts`.

- [ ] **Step 1: Write the failing tests** `tests/test_chat_skills.py` (помощники `add_gateway`, `new_chat`, `turn`,
  `sse`, `messages` из `tests/test_chat.py`):

```python
TEXT = "Ты — редактор. Сократи текст вдвое."       # навык: POST /api/skills {name: "Сократить", text: TEXT, scope: "chat"}

def test_turn_with_skill_prepends_text_and_marks_message(studio):
    sse(turn(studio, cid, "вот текст", skill_id=sid)); user, answer = messages(studio, cid)
    assert user["content"] == TEXT + "\n\nвот текст" and answer["skill"] is None
    assert user["skill"] == {"name": "Сократить", "chars": len(TEXT), "parts": False}
    assert studio.gw.chat_requests[-1]["messages"][-1] == {"role": "user", "content": user["content"]}
    # название диалога — "вот текст"; один навык без текста — не 422 `empty`: content == TEXT, название — "Сократить"
def test_unknown_foreign_and_agents_only_skill_is_refused_before_writing(studio):
    # 404 `gone` (нет, чужой), 422 `skill` (scope "agents"); сообщений в диалоге по-прежнему нет
def test_edit_keeps_or_drops_the_skill(studio):    # навык в библиотеке изменён и удалён; {edit, text: "иначе", skill_keep: True}:
    assert edited["content"] == TEXT + "\n\nиначе" and edited["skill"]["chars"] == len(TEXT)
    # правка без skill_keep: content == "без навыка", skill is None; skill_keep у сообщения без навыка,
    # skill_id + skill_keep, regenerate + skill_id — 422 `invalid`
def test_same_request_id_with_another_skill_is_a_conflict(studio):       # 409 `request_conflict`
def test_link_and_export_carry_the_name_not_the_text_and_takeout_carries_all(studio):
    snapshot, _ = chat_shares.regular_snapshot(uid, cid)
    assert snapshot["messages"][0]["text"] == "вот текст" and snapshot["messages"][0]["skill"] == "Сократить"
    assert "Навык: Сократить" in md and TEXT not in md                # POST /api/chats/{id}/export {format: "md"}
    assert took["messages"][0]["content"].startswith(TEXT) and took["messages"][0]["skill"]["name"] == "Сократить"
def test_estimate_counts_the_chosen_skill(studio):                       # tokens с skill_id больше на ≥ len(TEXT) // 3
def test_skill_never_reaches_the_log(studio, caplog):                    # ни имени, ни текста навыка в caplog.text
```

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest -q -n0 --assert=rewrite tests/test_chat_skills.py` — Expected:
  FAIL (`skill` нет в сообщении; `skill_id` не принят).
- [ ] **Step 3: Implement the skill in a turn.** `app/chat_skills.py` — функции из Produces; `apply`: `skill_id` →
  `skills.get` (область `agents` — 422 `skill`), `skill_keep` → прежние `content[:chars]` и JSON сообщения `edit`
  (навык в библиотеке могли изменить или удалить), иначе `(body.text, None)`. В `chat.py`: поля `TurnIn` (`:269`) и
  `EstimateIn` (`:2680`); `create_turn` (`:2912`, правка `:2919`) и `create_mcp_turn` (`:2788`, `:2783`) — `content,
  mark = chat_skills.apply(uid, body, edit)` внутри транзакции, в `INSERT` — столбец `skill`, при правке — `skill =
  ?` в том же `UPDATE`, что `content`; проверка пустого сообщения (`:2839`) пропускает ход с `skill_id` или
  `skill_keep`; название (`:2974`) — `_title(body.text) or chat_skills.title(mark)`; `_cleaner_messages` берёт текст
  из `chat_skills.apply` (оба места `body.text`); `_turn_fingerprint` (`:2671`) дописывает `[skill_id,
  skill_keep]`, только когда они заданы (отпечатки прежних отправок не меняются); `estimate` —
  `chat_skills.preview`; `_message_out` (`:447`) — `"skill": chat_skills.view(row)`.
  `chat_shares.regular_snapshot` (`:302`): у сообщения пользователя `text = chat_skills.human(message)` и `skill` —
  имя; `chat_exports._parts` (`:143`) — строка `word("Навык: {}", lang, …)` перед текстом; `share-view.js` — строка
  «Навык: имя» над текстом; `takeout._chat` — столбец `skill` (`_maybe_json`), `content` целиком. Слова отказов и
  «Навык: {}» — в `en.js`.
- [ ] **Step 4: Run** `tests/test_chat_skills.py tests/test_chat.py tests/test_chat_shares.py
  tests/test_chat_exports.py tests/test_takeout.py tests/test_cleaner.py tests/test_i18n_server.py` — PASS.
  **Commit** `Send a skill with a chat turn and keep it out of links and exports`.
- [ ] **Step 5: Write the failing tests** `tests/test_chat_parts.py` (ещё помощники: `call` — `tests/test_chat_web.py`;
  `setup`, `name_of`, `offered`, `tool` — `tests/test_mcp_dialog.py`; `protect`, `web_dialog`, `flags`, `names`, `ALL`
  — `tests/test_protected_source.py`; `cleaner_gw`, `routed_chat` — `tests/test_cleaner_route_chat.py`):

```python
# parts_chat(studio) → (cid, sid): шлюз, диалог и навык {"name": "По частям", "text": "Дели задачу.", "scope": "chat",
# "parts": True}; part(title="Часть 1", task="Сделай первое") = call("run_part", title=…, task=…); tool_names(request)
def test_run_part_is_offered_only_while_the_last_skill_has_parts(studio):
    # ход без навыка — нет; с навыком parts — есть, и в следующем ходе без навыка есть; после хода с навыком без parts — нет
def test_part_request_has_clean_context_and_its_result_returns_to_the_turn(studio):
    studio.gw.chat_script = [["Старый ответ"], {"tool_calls": [part()]}, ["Итог первой части"], ["Общий ответ"]]
    sse(turn(studio, cid, "Секрет прошлого хода")); events = sse(turn(studio, cid, "Большая задача", skill_id=sid))
    _, main, sub, last = studio.gw.chat_requests
    assert [m["role"] for m in sub["messages"]] == ["system", "user"] and sub["model"] == main["model"]
    body = json.dumps(sub["messages"], ensure_ascii=False)
    assert "Сделай первое" in body and not any(w in body for w in ("Секрет прошлого хода", "Старый ответ", "Дели задачу."))
    assert not chat_tools.PART_EXCLUDED & set(tool_names(sub)) and "run_part" in tool_names(main)
    assert last["messages"][-1]["role"] == "tool" and last["messages"][-1]["content"] == "Итог первой части"
    answer = messages(studio, cid)[-1]; step = answer["tools"][0]
    assert answer["content"] == "Общий ответ"                            # текст части в ответ не попал
    assert {**step, "ms": 0} == {"kind": "part", "n": 1, "title": "Часть 1", "state": "done", "ms": 0, "text": "Итог первой части"}
    assert ("part", {"n": 1, "title": "Часть 1", "state": "running"}) in events
    # с studio.gw.chat_usage = {"prompt_tokens": 100, "completion_tokens": 10} и sse(…, metrics=True): usage ответа —
    # {"in": 300, "out": 30}: запрос части входит в расход ответа
def test_second_part_request_carries_the_result_of_the_first(studio):    # "Итог первой части" есть в запросе второй части
def test_eleventh_part_is_refused_in_words_and_part_rounds_are_not_counted(studio):
    chat.set_tool_rounds(2)                                              # 11 кругов по одному run_part, затем текст
    assert len([s for s in steps if s["kind"] == "part" and s["state"] == "done"]) == 10
    assert steps[-1]["error"] == chat_parts.LIMIT and answer["status"] == "done"
    assert "tools" in studio.gw.chat_requests[-1]                        # последний главный запрос — ещё с инструментами
def test_second_part_of_one_round_is_refused(studio):                    # [part("А"), part("Б")] в одном круге:
    assert [s.get("error") for s in parts_of(answer)] == [None, chat_parts.ONE_PER_ROUND]
def test_part_result_is_cut_and_part_rounds_are_capped(studio, monkeypatch):
    assert (chat_tools.PART_RESULT_MAX, chat_tools.PART_ROUNDS, chat_tools.PARTS_MAX) == (4000, 8, 10)
    # итог 5000 знаков → len(step["text"]) == 4000 и столько же в сообщении `tool`; PART_ROUNDS = 2 (monkeypatch): третий
    # запрос части — без "tools"; ask_user, вызванный в части, — шаг с error == chat_tools.NOT_IN_PART;
    # run_part в диалоге без навыка «по частям» — шаг с error == web.NOT_OFFERED, карточки вопроса нет
def test_answer_with_parts_outlives_1800_seconds_and_a_part_has_its_own_limit(studio, monkeypatch, no_pings):
    monkeypatch.setattr(chat, "STREAM_TOTAL_SEC", 1800); assert chat.PARTS_TOTAL_SEC == 7200
    # обёртка studio.gw.on["/v1/chat/completions"] двигает studio.clock[0] на 1700 перед запросом каждой из двух частей
    assert answer["status"] == "done" and chat.CUT_TIME not in answer["content"]             # прошло 3400 с
    # другой диалог: +1801 перед запросом части → шаг state "error", error == chat_parts.TIME, ответ "done"
def test_stop_interrupts_the_part(studio):         # chat_hold на запросе части, POST /stop:
    assert (answer["status"], parts_of(answer)[0]["state"], parts_of(answer)[0]["error"]) == ("stopped", "error", chat_parts.STOPPED)
def test_run_part_in_a_protected_dialog_gets_no_web_and_protection_inside_a_part_sticks(studio):   # Review Focus 3
    sid_mcp, calls, _ = setup(studio, tools=[tool("lookup"), tool("search", annotations={"openWorldHint": True})])
    protect(studio, sid_mcp); cid = web_dialog(studio)                   # + навык parts этой учётки
    studio.gw.chat_script = [{"tool_calls": [part()]}, {"tool_calls": [call(name_of(sid_mcp), q="1")]}, ["Итог"], ["Готово"]]
    sse(turn(studio, cid, "Задача", skill_id=sid)); main, sub1, sub2, last = studio.gw.chat_requests
    assert names(sub1) == ALL and names(sub2) == [] and names(last) == []    # сработал внутри части — «Интернета» нет дальше
    assert offered(sub2) == ["Shared tools: lookup"] and flags(cid) == (1, 0)
    # следующий ход того же диалога: запрос части без «Интернета»; вызов MCP со ссылкой внутри части — шаг
    # mcp_client.URL_REFUSED без tools/call; PATCH {web: true} — 409 `protected`
def test_part_goes_through_cleaner_with_the_dialog_context(cleaner_gw):  # провайдеру мимо Cleaner — ничего
    assert len(gw.turns) == 3 and len({r.headers["x-cleaner-chat"] for r in gw.turns}) == 1
def test_next_turn_gets_the_part_result_but_not_its_inner_steps(studio):
    # часть с web_search: шаг поиска в tools ответа с "part": 1, без "text", с "results"; в запросе следующего хода есть
    # "Итог первой части", нет текста выдачи; read_page ссылки из той выдачи в следующем ходе не отвечает web.NOT_KNOWN
def test_pause_keeps_part_counters(studio):        # [part(), call("ask_user", …)] в круге, ответ на карточку, ещё 10 частей:
    # одиннадцатая — chat_parts.LIMIT
def test_parts_never_reach_the_log(studio, caplog):                      # ни названия, ни задания, ни итога части
```

- [ ] **Step 6: Run** `tests/test_chat_parts.py` — Expected: FAIL (`run_part` — «модель вызвала инструмент, которого
  у неё нет»).
- [ ] **Step 7: Implement `run_part`.** Новое поведение — в `app/chat_parts.py` (`run(task, call) -> dict`,
  `extend(task)`, слова) и `chat_tools.py`; в `chat.py` — только:
  - константы `PARTS_TOTAL_SEC`, `CUT_TIME_PARTS` (120 минут); поля `StreamTask`: `parts: int = 0`, `part_round:
    bool = False`, `part: int | None = None`, `part_deadline: float = math.inf`, `long: bool = False`;
  - `_wait_card` (`:973`) сдвигает и `part_deadline`; `_cut_time` (`:1331`) при `long` пишет `CUT_TIME_PARTS`;
    `_events` пропускает вид `part`; `compose` (отбор шагов рядом с `"note" not in s`, после задачи 7) не отдаёт
    следующему ходу шаги с ключом `part` — итог части идёт через её шаг `part` и `_for_history`;
  - `_round` (`:1113`): параметр `part: bool = False` — текст, рассуждение и `messages.cleaner_request` круга не
    пишутся, как при `silent` (условия `silent` → `silent or part`), предел текста круга — `ANSWER_MAX`, а `usage`
    и время генерации входят в ответ, как у обычного круга (задача 4 пропускает их только у `silent`; её повтор
    без `stream_options` передаёт и `part`); после проверки срока ответа (`:1171`) — `if clock.NOW() >
    self.part_deadline: return chat_parts.TIME, None, [], text`;
  - `_read_model` (`:1637`): перед `_tools` — `before, self.part_round = self.parts, False`; после — `self.rounds
    -= 1`, если в круге запущена часть (`self.parts > before`) и все его вызовы — `run_part` (круг, где `run_part`
    только отказали, считается: иначе цикл отказов не кончался бы);
  - `_calls` (`:1362`): `n` не растёт на вызове `run_part` (он не занимает место среди `CALLS_PER_ROUND` и не
    получает `TOO_MANY`); перед веткой встроенных (`:1402`) — `elif c.name == "run_part" and self.part is None and
    not self.builtin_off and chat_tools.parts_offered(self.chat_id): step = await chat_parts.run(self, c)`; в ветке
    MCP (`:1391`) при `self.part is not None` вместо паузы — шаг с `chat_parts.NEEDS_CONFIRM`;
  - `_step` (`:1487`): шаг, уже лежащий в `steps` (тот же объект), второй раз не добавляется — только запись; при
    `self.part` шаг хранится копией без `text` с `"part": self.part`;
  - `_resume` (`:1453`): `self.parts, self.part_round = r.get("parts", 0), r.get("part_round", False)`, при `parts
    > 0` — `chat_parts.extend(self)`; `chat_pending.pause` кладёт оба ключа в payload.

  `chat_parts.run`: неверные аргументы — `BAD_ARGS`, `task.part_round` — `ONE_PER_ROUND`, `task.parts >= PARTS_MAX` —
  `LIMIT` (шаг `state: "error"` без запуска). Иначе `parts += 1`, `part_round = True`, `extend(task)` (раз на ответ:
  `long = True` и, если срок конечен, `deadline += PARTS_TOTAL_SEC - STREAM_TOTAL_SEC`), шаг `running` через
  `task._step` (до подмены, пока `task.part` — None; тот же объект потом получает итог, и общий хвост `_calls`
  записывает его повторным `_step`), события `tool` и `part`. На время части подменяются `task.messages`
  (`[системное сообщение хода, {"role": "user", …}]`: записка модели, `<part title="…">задание</part>` и итоги
  прежних частей этого ответа `<done-part n="…" title="…">…</done-part>` целиком), `task.seen = set()`, `task.part =
  n`, `task.part_deadline = clock.NOW() + STREAM_TOTAL_SEC`, `task.spec_size` (описания инструментов части); в
  `finally` всё возвращается. Круги части: запрос `{**task.params, model, messages: task._request(offer), stream}`
  (и `enable_thinking`, как у хода) с инструментами `web.specs(task.tools)` + MCP без пометки подтверждения +
  `chat_tools.part_specs(task)` через `task._round(payload, False, part=True)` — тем же путём, что ход, и через
  `cleaner_route.stream`; `BUSY` — как у хода (`_free_images` раз, `_wait_card`); `TOO_LONG` — ошибка части словами
  хода, без сжатия; вызовы исполняются по правилу структурного вызова задачи 8 (`calls and ending not in (None,
  "length")`; вызов текстом в части не разбирается) — `task._tools(calls, text)` (те же проверки, что у хода:
  ссылка после защищённого источника, «Интернет», `_protect`, повторы, `TOO_MANY`), не больше `PART_ROUNDS` кругов,
  затем запрос без инструментов. Итог — `text.strip()[:PART_RESULT_MAX]` (`length` — принять как есть, пусто — `EMPTY`,
  поток без признака конца — ошибка), `ms`, `state`; отмена — `STOPPED` и `raise`. `chat_tools`: `RUN_PART`,
  `KIND_OF["run_part"] = "part"`, `specs` добавляет `RUN_PART` последним при `parts_offered` (последнее по порядку
  сообщение диалога с `skill` имеет `parts`), `call` при `task.part` отказывает именам `PART_EXCLUDED` словами
  `NOT_IN_PART`, а `run_part`, дошедшему до него (не предложен), — `web.NOT_OFFERED`; `for_model` у шага `part` без
  `text` и `error` — `PART_CUT`. `web.public` оставляет `text` шага `part`. В журнал — номер части, состояние,
  миллисекунды. Слова — в `en.js`; записка модели — в `EXEMPT_CONSTANTS` сторожа, если он её найдёт.
  `chat_exports._tool_line` (`:108`) — «Часть: название».
- [ ] **Step 8: Run** `tests/test_chat_parts.py tests/test_chat_tools_rounds.py tests/test_chat_pending.py
  tests/test_chat_web.py tests/test_protected_source.py tests/test_cleaner_route_chat.py tests/test_chat_wait.py
  tests/test_i18n_server.py tests/test_docs.py` — PASS. **Commit** `Add the run_part tool for answers built in parts`.
- [ ] **Step 9: Write the failing browser test** `tests/test_ui_chat_skills.py` (integration, 320 и 1280 px;
  помощники `tests/test_ui_browser.py`: `signin`, `add_gateway`, `open_new_chat`, `ANSWERED`, `NO_OVERFLOW`):

```python
# навыки учётки — через API страницы: «Сократить» (chat) и «По частям» (chat, parts)
# ("type", "#chat-input", "/сокр"), ("press", "Enter") → #chat-skill содержит «Навык: Сократить», поле пустое
# ("type", "#chat-input", "вот текст"), ("press", "Enter"), ("wait", ANSWERED)
assert values["bubble"] == "вот текст" and values["head"] == "Навык: Сократить"      # .msg.user .bubble, .msg-skill summary
assert live.gw.chat_requests[-1]["messages"][-1]["content"] == TEXT + "\n\nвот текст"
assert values["chipGone"] and values["fits"]                                         # чип снят после отправки; NO_OVERFLOW
# «Изменить»: в поле — только «вот текст», чип на месте; отправка «иначе» → content == TEXT + "\n\nиначе";
# крестик чипа при правке → content == "совсем без навыка", .msg-skill нет
# части — следующим ходом того же диалога (навык в середине диалога): "/по ч", Enter, текст, Enter;
# live.gw.chat_script = [{"tool_calls": [("run_part", json.dumps({"title": "Сбор", "task": "Собери"}))]},
#                        ["Итог части"], ["Общий ответ"]]
assert values["part"] == ["Сбор", "готово"] and values["partText"] == "Итог части"   # .web-part и его раскрытый текст
assert values["partsButton"] == "1 из 1"                                             # #chat-parts в строке состояния
```

- [ ] **Step 10: Run** `.venv/Scripts/python -m pytest -q -n0 -m integration tests/test_ui_chat_skills.py` —
  Expected: FAIL (`#chat-skill` нет).
- [ ] **Step 11: Implement the page.** `chat-skills.js`: `chatSkills({ input, host, changed })` → `{ chosen(), body(),
  view(), keep(skill), clear(), reload() }` читает `GET /api/skills?scope=chat&lang=`, подключает `slashMenu(input,
  { items, onPick })` (строки `kind: 'skill'`) и `skillChip(host)`; `body()` — `{ skill_id }`, при правке
  сохранённого навыка `{ skill_keep: true }`, без чипа — `{}`; `skillMessage(m)` → `{ head, text }`: `head` —
  `details.msg-skill` («Навык: имя», внутри — текст навыка `content.slice(0, chars)`), `text` — `humanText(m)`.
  `chat-parts.js`: `partNode(step)` — `li.web-step.web-part`: «Часть N: название · идёт / готово / ошибка ·
  длительность» и раскрываемый итог (шаг `running` у кончившегося ответа — «прервана»); `noteParts(message, data)`
  копит события `part` идущего ответа; `partsButton({ root })` → `{ node, paint(message) }` — кнопка `#chat-parts`
  (значок `subagents`, подпись «Части: {0} из {1}», скрыта, пока у последнего ответа нет частей), по нажатию — окно
  со списком `partNode`. `chat-status.js`: `chatStatus(host, { …, parts })` — необязательный `parts`: его `node`
  встаёт в ряд `actions` перед `#chat-files-open` до вызова `statusActions`, `paint()` строки зовёт
  `parts.paint(последний ответ)`. Точки подключения в `chat.js` (больше ничего): импорт; `build` (`:674`, форма
  `:799`) — узел `#chat-skill` над полем (в нём чип `#skill-chip`) и `chatSkills(…)`, `partsButton` — параметром
  `parts` в `chatStatus`; `submit` (`:2404`) — ход с чипом не «пустой»; `turn` (`:2556`, `:2612`) —
  `...skills.body()` в `turnInput`, `skill: skills.view()` у показанного сообщения, `skills.clear()` после принятого
  хода; `messageNode` (`:1847`) — `skillMessage(m)` вместо голого пузыря; `editTurn` (`:1745`) — `input.value =
  humanText(m)` и `skills.keep(m.skill)`, `closeEdit` — `skills.clear()`; `estimateContext` (`:1415`) — `skill_id`;
  `readServer` (`:300`) — событие `part` → `noteParts`; `stepNode` (`:588`) — `if (s.kind === 'part') return
  partNode(s)`; `badgeText` (`:642`) — счёт «часть / части / частей» и `part` среди известных видов;
  `TOOL_WORDS.part = tr("Часть")`. Стили `.msg-skill`, `.web-part`, `#chat-skill` — в `app.css` (длинное имя —
  многоточием); подписи — `tr` и `en.js`.
- [ ] **Step 12: Run** `-m integration tests/test_ui_chat_skills.py tests/test_ui_chat_composer.py
  tests/test_ui_chat_pending.py` и быстрые `tests/test_i18n.py tests/test_css_tokens.py tests/test_sections_meta.py`
  — PASS. **Commit** `Show skills and answer parts in the chat page`.

### Task 14: «Чат»: метки и поиск в списке, закрепление, вкладки диалогов, push и напоминание на паузе хода

Список диалогов — `list_chats` (`chat.py:1906–1911`) с `_chat_out` (:316–319) без состояния; рисуют его `loadList` /
`listItem` (`chat.js:833–884`). Браузерные тесты сверяют `textContent` строки `.chat-item` с названием
(`tests/test_ui_browser.py:1225`, :2103) — метки обязаны быть без текста. Пауза хода (`chat_pending.pause`, :105–126)
push не шлёт: `StreamTask.run` пропускает `_answered` при `self.paused` (`chat.py:1739`), а напоминание есть только у
карточек «Агентов» (`bridge.remind`, `bridge.py:640–671`; `push.reminders`, `push.muted`, `push.on_screen`). Вкладок у
«Чата» нет; вид ряда — классы `.code-tabs-row` / `.code-tabbar` / `.code-tab` (`app.css:1974–2003`, :2499–2501).

**Files:**
- Create: `app/chat_list.py`; `app/static/js/chat-list.js`, `app/static/js/chat-tabs.js`
- Modify: `app/chat.py` — `ChatPatch` (`pinned: bool | None = None` сразу после `title` :243), `_chat_out` :318
  (`pinned`), `list_chats` :1906–1911, `INSERT` в `create_chat` :1926 (`seen`), `patch_chat` (сразу после блока
  `title` :2080–2081); `app/chat_pending.py` (`pause` :124 и новые функции в конце файла); `app/main.py` (:147 —
  `chat_pending.start_reminders()`; `include_router(chat_list.router)` после `chat_pending.router` :215);
  `app/push.py:78–81` (комментарий `KINDS`); `app/static/js/chat.js`; `app/static/js/app.js:45` (`import
  './chat-tabs.js'` — слушатели выхода нужны и без открытого «Чата»); `app/static/login.js` (стирать
  `localStorage['studio-chat-tabs']`); `app/static/js/notify-prefs.js:9` и `docs/guide.ru.md:1971` (подпись вида `ask`
  — «Вопросы и подтверждения агентов и чата»); `app/static/css/app.css`, `app/static/en.js`; `CLAUDE.md` (карта
  модулей: `chat_list`); `docs/architecture/chat.md`, `docs/architecture/notifications.md`;
  `tests/conftest.py::clear_push` (:421 — ещё `chat_pending.SENT.clear()`)
- Test: Create `tests/test_chat_list.py`, `tests/test_ui_chat_list.py`; Modify `tests/test_chat_pending.py`
- Подключение в `chat.js`: импорты — после :110 (`import { readSSE } …`); блок перед комментарием `/* список
  диалогов */` :832 — `listing`, `tabs`, `fresh.after(listing.search)`, `modelNote.after(tabs.node)`; тело
  `loadList()` :834–856 — `listed = await listing.reload(); tabs.sync(listed.map((c) => c.id));`; `shareContext()`
  :817–823 — `listing.seen(current); tabs.show(current);`; `destroy()` — перед `alive = false;` :2867. `listItem()`
  :859–884 и разметка строки не меняются.

**Interfaces:**
- Consumes: `chats.pinned`, `chats.seen` (задача 1); `draftStore(CHAT_DRAFTS)`, событие `studio-dialog-draft`
  (задача 3; пишет черновики задача 13 волны C — тесты всё равно кладут черновик в хранилище сами); `push.notify_later`,
  `push.reminders`, `push.muted`, `push.on_screen`, `push.DECISION_NOTICE`, `push.STUDIO` (есть).
- Produces:
  - `GET /api/chats`: у строки ещё `busy`, `unread`, `pinned` (все `bool`); `_chat_view` — `pinned`; `PATCH
    /api/chats/{id}` принимает `pinned: bool`; `POST /api/chats/{chat_id}/seen` → 204 (чужой или в корзине — 404
    `gone`); `chat_list.marks(uid: int, rows) -> dict[str, dict]` (`{id: {"busy": bool, "unread": bool}}`).
  - `chat_pending.REMIND_TIMES = 3`, `REMIND_TICK_SEC = 60`, `SENT: dict[str, dict[str, tuple[int, float]]]` (id паузы
    → сессия → (повторов, когда)), `notify(user_id: int, chat_id: str) -> None`, `remind() -> None`,
    `start_reminders() -> asyncio.Task`. Push паузы: тело `push.DECISION_NOTICE`, заголовок `push.STUDIO`, адрес
    `chat/<id>`, метка `chat-ask-<id>`, вид `ask`.
  - `LIST_POLL_MS = 5000`, `chatList(list, { row, group, failed }) -> { search, reload(): Promise<items>, items(),
    seen(chat | null), unmount() }`; `TABS_MAX = 12`, `chatTabs({ open, info }) -> { node, show(chat | null),
    sync(ids), unmount() }`; `localStorage['studio-chat-tabs'] = {"user": me.id, "ids": [...]}`.

- [ ] **Step 1: Write the failing tests** (быстрые; фикстура `no_pings` из `conftest` — иначе пинги потока сами
  двигают подменённые часы). `tests/test_chat_list.py` (`events_of`, `new_chat`, `wait`, `chat_streams` — `test_chat`):

```python
def test_list_marks_busy_unread_and_pinned(studio, no_pings):
    row = lambda cid: next(i for i in studio.c.get("/api/chats").json()["items"] if i["id"] == cid)
    assert (row(a)["busy"], row(a)["unread"], row(a)["pinned"]) == (False, False, False)
    studio.gw.chat_hold = True
    with studio.c.stream("POST", f"/api/chats/{a}/turns", json={"text": "Привет"}) as r:
        events = events_of(r.iter_lines()); next(events); next(d for e, d in events if e == "delta")
        assert (row(a)["busy"], row(a)["unread"], row(b)["busy"]) == (True, False, False); stream = chat_streams()[0]
    studio.gw.chat_hold = False; wait(studio, stream)
    assert (row(a)["busy"], row(a)["unread"]) == (False, True)             # ответ кончился, диалог не открывали
    assert studio.c.post(f"/api/chats/{a}/seen").status_code == 204 and row(a)["unread"] is False
    state.DB.execute("UPDATE chats SET seen = NULL WHERE id = ?", (a,)); assert row(a)["unread"] is False   # диалог до шага 82
    assert studio.c.patch(f"/api/chats/{b}", json={"pinned": True}).json()["pinned"] is True and row(b)["pinned"] is True
    assert studio.c.patch(f"/api/chats/{b}", json={"title": "Имя"}).json()["pinned"] is True   # прочая правка не снимает
    assert (row(direct)["busy"], row(direct)["unread"]) == (True, False)   # прямое подключение: streaming с живым alive_at
    studio.clock[0] += chat.ALIVE_SEC + 1; assert (row(direct)["busy"], row(direct)["unread"]) == (False, False)   # брошен
    signin(studio, "friend")                                               # чужой диалог — 404 обоим маршрутам
    assert [studio.c.post(f"/api/chats/{a}/seen").status_code,
            studio.c.patch(f"/api/chats/{b}", json={"pinned": False}).status_code] == [404, 404]
```

  `tests/test_chat_pending.py` (свои `confirm_setup`, `pause_on`, `answer`; `Device`, `settle` — `test_push`; `tick(n)`
  — часы на `n` минут вперёд, `portal.call(chat_pending.remind)`, `settle`, число напоминаний с начала):

```python
def test_pause_pushes_ask_without_content_and_reminds_three_times(studio, caplog, no_pings):
    # подписка телефона, «О чём уведомлять»: remind 10; пауза с текстом «секретная просьба» и аргументом «секретный текст»
    [first] = [phone.decrypt(r.content) for r in studio.gw.push_requests]
    assert first == {"title": "Студия", "body": "нужно ваше решение", "href": f"chat/{cid}", "tag": f"chat-ask-{cid}"}
    assert [tick(9), tick(1), tick(10), tick(10), tick(10)] == [0, 1, 2, 3, 3]          # не больше REMIND_TIMES
    sent = "".join(str(phone.decrypt(r.content)) for r in studio.gw.push_requests)
    for secret in ("секретный текст", "секретная просьба", "translate"): assert secret not in sent + caplog.text

def test_chat_reminder_follows_the_look_the_device_choice_and_the_answer(studio, no_pings):
    studio.c.get("/api/jobs?visible=1"); shown = pause_on(…); assert len(studio.gw.push_requests) == 0   # страница на экране
    studio.clock[0] += push.SCREEN_SEC; assert tick(10) == 1                             # ушла с экрана — напоминание
    studio.c.post(f"/api/chats/{cid}/seen"); assert [tick(9), tick(1)] == [1, 2]         # интервал — от взгляда на диалог
    prefs(off=["ask"]); assert tick(10) == 2; prefs(remind=0); assert tick(10) == 2      # снятый вид; «не напоминать»
    prefs(quiet_from="00:00", quiet_to="23:59"); assert tick(10) == 2; prefs(); assert tick(1) == 3   # тихие часы откладывают
    sse(answer(studio, cid, shown["id"], "refuse")); assert tick(30) == 3 and chat_pending.SENT == {}
```

- [ ] **Step 2: Run** `… tests/test_chat_list.py tests/test_chat_pending.py` — Expected: FAIL (`KeyError: 'busy'`;
  нет `chat_pending.remind`; push паузы не ушёл).
- [ ] **Step 3: Implement — сервер.** `chat_list.marks`: последнее выбранное сообщение каждого диалога учётки одним
  запросом (`role, status, via, alive_at`); `busy` — оно `streaming` и (`via = 'server'` или `chat._alive`); `unread`
  — `seen IS NOT NULL`, последнее — ответ не `streaming`, `updated > seen` (`seen IS NULL` — диалог до шага 82: не
  «не прочитано»). `list_chats` — `_chat_out(r) | marks[r["id"]]`; `create_chat` пишет `seen = now`; `/seen` — `UPDATE
  chats SET seen = clock.NOW()`; `patch_chat` — `changes["pinned"] = int(body.pinned)` при `"pinned" in sent`.
  `chat_pending.pause` после события `pending` зовёт `notify(task.user_id, task.chat_id)`: диалог не в корзине →
  `push.notify_later(uid, push.DECISION_NOTICE, f"chat/{chat_id}", f"chat-ask-{chat_id}", title=push.STUDIO,
  quiet=True, kind="ask")`. `remind()` (цикл `start_reminders` — `clock.SLEEP(REMIND_TICK_SEC)`, сбой прохода — в
  журнал именем типа, как `bridge._reminders`): открытые паузы моложе `PENDING_TTL_SEC` у диалогов не из корзины и
  включённых учёток; сессии из `push.reminders(uid)` повтор уходит, если интервал не 0, повторов меньше
  `REMIND_TIMES`, сессия не в `push.muted(uid, "ask")` и не в `push.on_screen()`, и `now − max(прошлый повтор или
  создание паузы, chats.seen или 0) >= интервал`; отправка — тем же `notify_later` с `skip` = остальные сессии; `SENT`
  чистится от пауз, которых нет среди открытых. Счёт повторов — в памяти (перезапуск службы начинает его заново).
- [ ] **Step 4: Run** `tests/test_chat_list.py tests/test_chat_pending.py tests/test_push.py tests/test_chat.py
  tests/test_foundation.py tests/test_docs.py` — PASS.
- [ ] **Step 5: Commit** `Mark busy, unread and pinned chats and remind about waiting chat cards`.
- [ ] **Step 6: Write the failing test** `tests/test_ui_chat_list.py` (`integration`; `signin`, `READY`, `API`,
  `NO_OVERFLOW`, `go`, `set_value`, `problems` — `test_ui_browser`; удержание ответа — `live.gw.chat_hold`):

```python
@pytest.mark.parametrize("width", [320, 1280])
def test_list_marks_pin_and_title_search(browser, live, width):
    # диалоги А «Отчёт за квартал» и Б «План отпуска» через API; ход в Б с удержанным шлюзом, затем открыт А
    assert v["busy"] == ["busy", "идёт ответ"]         # .chat-mark[data-mark] строки Б и его aria-label
    assert v["unread"] == ["unread", "не прочитано"]   # шлюз отпущен, Б не открывали: опрос списка раз в LIST_POLL_MS
    assert v["text"] == "План отпуска" and v["opened"] == [[], True]   # textContent — только название; открыли Б: меток нет, POST …/seen ушёл
    assert v["pinned"] == ["Закреплённые", "Отчёт за квартал", "true"] and v["kept"] is True   # звезда А; после перезагрузки — тоже
    assert v["found"] == ["План отпуска"] and v["none"] == "Ничего не найдено" and v["asked"] == 0   # «отпус», «zzz»; без запросов
    assert v["draft"] == ["draft", "есть черновик"]    # draftStore('studio-chat-drafts').save(А, 'текст')
    assert v["fits"] is True                           # 320: звезда и «⋯» внутри панели списка, NO_OVERFLOW

@pytest.mark.parametrize("width,lang", [(320, "en"), (1280, "ru")])      # Review Focus 5
def test_dialog_tabs_open_close_limit_and_device_storage(browser, live, width, lang):
    # 13 диалогов «Диалог N» через API (ids), открываются по очереди сменой адреса
    assert v["one"] is True and v["three"] == ["Диалог 1", "Диалог 2", "Диалог 3"] and v["active"] == "Диалог 3"   # один — ряд скрыт
    assert v["limit"] == [12, "Диалог 2"]              # тринадцатый вытеснил самую левую не текущую вкладку
    assert v["stored"] == {"user": uid, "ids": ids[1:]}   # localStorage['studio-chat-tabs']: только id
    assert v["closed"] == [11, f"#/chat/{ids[11]}"]    # крестик текущей: вкладок 11, открыта соседняя
    assert v["marks"] == [True, True]                  # .code-tab-mark.running у идущего ответа, ✎ у черновика
    assert v["reload"] == 11 and v["gone"] == 10       # ряд пережил перезагрузку; удалённый диалог ушёл из ряда
    assert v["fits"] is True     # NO_OVERFLOW; #chat-tabbar в [0, innerWidth], листается внутри себя; текущая видна целиком
    assert v["signed_out"] is None                     # «Выйти»: ключа localStorage нет
```

- [ ] **Step 7: Run** `… -m integration tests/test_ui_chat_list.py` — Expected: FAIL (нет `.chat-mark`, `#chat-search`,
  `#chat-tabbar`).
- [ ] **Step 8: Implement — список** (`chat-list.js`). `reload()` — `GET /api/chats`; порядок: группа «Закреплённые»
  (`pinned`, по `updated`), затем дни через `group(updated)` (`groupOf`, `chat.js:193`); строку строит `row(item)`
  (`listItem`), модуль дописывает звезду `button.icon-btn.chat-pin[aria-pressed]` («Закрепить» / «Открепить», значок
  `star`, перед `.chat-more`; видна как `.chat-more`, у закреплённого — всегда) и метки `span.chat-mark
  [data-mark=busy|unread|draft][role=img][aria-label][title]` **без текста** («идёт ответ», «не прочитано», «есть
  черновик»); `busy` важнее `unread`. Перерисовка — только когда ответ изменился (`JSON.stringify`): опрос не сбивает
  фокус и открытое меню строки; опрос — раз в `LIST_POLL_MS`, пока есть `busy` и страница видна. Отказ и пустой
  список — прежними словами. `search` — `input#chat-search[type=search]` («Поиск по названиям»): отбор на странице
  без учёта регистра, запроса нет; пусто — «Ничего не найдено». Звезда — `PATCH {pinned}` и `reload()`.
  `seen(chat)`: страница видна и `chat.updated` этого диалога ещё не отмечали — `POST …/seen` (отказ молча), метка
  `unread` строки снимается сразу. Черновик — `draftStore(CHAT_DRAFTS).has(id)`; по `studio-dialog-draft`
  перерисовываются только метки строки.
- [ ] **Step 9: Implement — вкладки** (`chat-tabs.js`). `node` — `div.code-tabs-row#chat-tabs[hidden]` >
  `div.code-tabbar#chat-tabbar[role=tablist]` («Открытые диалоги») > `div.code-tab[role=tab][data-chat]
  [aria-selected]`: `span.code-tab-title` (из `info(id)`, без названия — «Новый диалог»), `span.code-tab-draft` (✎),
  `span.code-tab-mark.running`, `button.code-tab-close`. `show(chat)`: диалог добавляется в конец, если его нет (при
  `TABS_MAX` уходит самая левая не текущая), становится выбранным и прокручивается в видимую часть; `null` (новый
  диалог) — выбранной нет; ряд скрыт, пока вкладок меньше двух. `sync(ids)` убирает вкладки диалогов, которых нет в
  удачно загруженном списке. Нажатие — `open(id)` (`ctx.navigate('chat/' + id)`); крестик и Delete убирают вкладку,
  у текущей — `open(соседняя ?? null)`; ← / → / Home / End — фокус по ряду; перетаскивания нет. В хранилище — только
  id; чужой `user` или мусор — пусто и ключ удалён; `studio-signed-out` и отправка `form[action="/logout"]` (слушатели
  уровня модуля) удаляют ключ. `info(id)` в `chat.js` — `{ title, busy }` из `listed`, открытого `chat` и
  `jobs.has(id)`. CSS: на телефоне ряд листается внутри себя, страница — нет.
- [ ] **Step 10: Run** `-m integration tests/test_ui_chat_list.py tests/test_ui_notify_prefs.py
  tests/test_ui_design_consistency.py tests/test_ui_chat_focus.py tests/test_ui_page_guards.py` и
  `tests/test_ui_browser.py -k "chat_list or chat_link or chat_layout or chats_move"`; быстрые `tests/test_i18n.py
  tests/test_css_tokens.py tests/test_sections_meta.py` — PASS.
- [ ] **Step 11: Commit** `Add chat list marks, title search and dialog tabs`.

### Task 15: «Чат»: окно HTML и SVG, «Новый диалог с итогом», источники «Знаний», картинки MCP — модели, уровень рассуждения

Пять пунктов спеки (25, 27, 28, 39, 40), что нашли в коде. **Окно.** Блокам кода кнопку «Копировать» ставит
`markdown.js::fragmentOf` (:191–198) и зовёт `fileLinks.decorate(fragment)` (:241); «Чат» даёт `fileLinks` только
готовому ответу (`chat.js:2009`). CSP страницы — `script-src 'self' 'sha256-…'` при `default-src 'self'`
(`main.py:95`): кадр `srcdoc` или `blob:` унаследовал бы её — встроенные скрипты блока не пошли бы, а `'self'`
остался бы достижим. Готовый образец — визуализации «Агентов»: документ отдаёт сервер со своим заголовком CSP и
`sandbox allow-scripts`, текст — только в памяти (`code_visualizations.py`, `claude_code.py:1638–1651`,
`code-visualizations.js:22`). **Итог.** `compact_chat` задачи 7 пересказывает всё до последнего сообщения
пользователя и пишет `chats.summary`; итогу нужен весь диалог и без записи. Наследника создаёт
`chat_fork.new_dialog` задачи 13. **Источники.** Шаг `knowledge_search` несёт только имена документов
(`chat_tools.py:207–209`, `chat.js:603–606`), автоматический блок (`knowledge.for_turn`, `knowledge.py:540`) шага не
оставляет вовсе, `Hit` (:94) не знает id документа; `web.public` (`web.py:435`) срезает у шага только `text`.
Прежний `compose` на шаге без `text` и `error` падает (`chat._for_model` :843 → `chat_tools.for_model` :100) —
новый шаг обязан нести `text`. **Картинки MCP.** `mcp_client.Tools.call` (:783–791) берёт из результата только
блоки `text`; модели результат идёт строкой `role: tool` (`chat.py:1425`). Файл держат только столбцы
`media.REFERENCES` (`media.py:456`), новых столбцов `*media_id` спека не даёт — картинка ложится строкой
`attachments` ответа: `compose` (:653) читает вложения только у сообщений пользователя, а `mcp_files._candidates`
(:137) — у всех. **Рассуждение.** Кнопка есть только у локальной модели (`chat.py:389`, `:1604`); вид провайдера
даёт `capabilities.profile` (:55), параметры хода — снимок `create_turn:2889` и `chat_pending.py:294`.

**Files:**
- Create: `app/chat_preview.py`, `app/chat_continue.py`, `app/chat_sources.py`; в `app/static/js/` —
  `chat-preview.js`, `chat-continue.js`, `chat-knowledge-sources.js`, `chat-result-images.js`, `chat-effort.js`
- Modify: `app/main.py` (три `include_router` после `chat_pending.router` :215), `app/chat.py` (только названное в
  шагах), `app/knowledge.py` (`Hit`, `_search` :507, `_head` :536, `for_turn`, новые `hits_view`, `chunk`),
  `app/chat_tools.py::_knowledge`, `app/mcp_client.py::Tools.call`, `app/mcp_files.py::_candidates`,
  `app/chat_pending.py` (payload паузы, параметры продолжения :294), `app/profiles.py`, `app/connections.py::out`,
  `app/static/js/chat.js`, `app/static/css/app.css`, `app/static/en.js`, `tests/test_mcp_dialog.py::server`
  (параметр `results`), `tests/test_i18n_server.py` (`EXEMPT_CONSTANTS["chat.RESULT_IMAGES_NOTE"]`), `CLAUDE.md`
  (карта модулей: три модуля сервера и пять модулей страницы; абзац «Защищённый источник MCP»: окно HTML — 409
  `protected`, диалог с итогом наследует отметку), `docs/architecture/chat.md` (разделы «Окно HTML и SVG», «Новый
  диалог с итогом»; дополнить «Знания», «Файлы между чатом и MCP», «Рассуждение»)
- Test: Create `tests/test_chat_preview.py`, `tests/test_chat_continue.py`, `tests/test_chat_sources.py`,
  `tests/test_chat_result_images.py`, `tests/test_chat_effort.py`, `tests/test_ui_chat_extras.py` (integration)
- Подключение в `chat.js`: импорты — после `import { startConversation } …` (:115); `serviceRefusal()` :408;
  `stepNode()` — ветка `knowledge` :603–606; `build`: `let draftEffort = null;` после `draftRole` :691, выбор уровня
  после `kbs` :785, `effort.chip, effort.menu` в `.composer-opts` после `thinkToggle` :801, блок после
  `head.append(share);` :815 (`notice.after(offer.node)`); `shareContext()` :817–823; `paintThink()` :1265–1268;
  `messageNode()` :1864 и :1889; `paint()` :2009 и перед «Ответить заново» :2085; `submit()` :2423 и :2441; `open()`
  — `offer.hide()`; `dropEmpty()` :2466; `createChat()` после блока `think` :2529–2532; `destroy()` :2874.

**Interfaces:**
- Consumes: `chat_fork.new_dialog(uid, row, *, title=None) -> str` (задача 13); `compact_chat`, `COMPACTING`,
  `StreamTask._summarize`, `SUMMARY_NOTE`, `SUMMARY_TOKENS`, `knowledge.for_turn(..., own=())` и помощники
  `three_turns`, `stored`, `compact`, `code` из `tests/test_chat_summary.py` (задача 7); `chat.CONTEXT_WORDS`,
  `_max_images(conn, model)`, `profiles.request_params(conn, model)` (задача 9); `openWith(id, text)` и кнопка окна
  `#chat-context-continue` (задача 6 — в окно кнопок не добавлять); `name_of(sid, tool="lookup")` (задача 8).
- Produces:
  - `chat_preview.PREVIEW_MAX = 2 * 1024 * 1024` (байты UTF-8), `TTL_SEC = 300`, `CSP = "default-src 'none';
    script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; media-src data:; frame-src
    'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'; sandbox allow-scripts"`.
    `POST /api/chats/{chat_id}/preview {kind: "html" | "svg", text}` → 201 `{token}`; 404 `gone`; 409 `protected`
    (`chat.PROTECTED_PREVIEW`); 413 `preview_size`. `GET /api/chats/preview/{token}` → документ с заголовками
    `Content-Security-Policy: CSP`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`; иначе 404.
  - `chat.summarize_dialog(uid, row, upto: str) -> str` (async); `POST /api/chats/{chat_id}/continue` → 201
    `{chat_id, text}`; отказы — как у `/compact` (404; 409 `busy`, `no_connection`, `no_compact`, `no_limit`; 413
    `context`; 503 `card`; 502 `upstream`) и 409 `nothing` (`chat_continue.NOTHING`).
  - Шаг знаний: `hits: [{doc: str, n: int, id: str}]` — и у `knowledge_search`, и у нового шага автоматического
    блока `{kind: "knowledge", auto: true, found, sources, hits, text: ""}`. `GET
    /api/chats/{chat_id}/messages/{message_id}/sources/{step}/{n}` → `{doc, n, text}`; иначе 404 `gone`.
    `knowledge.Hit.doc_id`, `knowledge.hits_view(hits) -> list[dict]`, `knowledge.chunk(uid, chat_id, doc_id, n) ->
    dict | None`, `knowledge.for_turn(..., found: list | None = None)`.
  - Картинки: `mcp_client.RESULT_IMAGES = 4` (на результат), `chat.RESULT_IMAGES_MAX = 16` (на ответ),
    `chat.RESULT_IMAGES_NOTE = "[изображения из результата инструмента]"`, `chat._sees_images(conn, model) -> bool`;
    шаг `mcp` получает `pictures: int`; картинки — `attachments` ответа (`kind: "image"`, имя `<инструмент>-<n>.png`),
    в JSON ответа — в `attachments`.
  - Уровень: `profiles.EFFORTS = ("low", "medium", "high")`, `EFFORT_PROVIDERS = ("openai", "openrouter")`,
    `effort_ok(conn) -> bool`, `effort_params(conn, level) -> dict`, `EFFORT_REFUSED`; `ChatPatch.effort`; в виде
    диалога — `effort: str | null`, `effort_ok: bool`; в выдаче подключения (`connections.out`) — `effort: bool`;
    отказ `PATCH` — 409 `no_effort` (`chat.NO_EFFORT`).
  - Страница: `previewLinks(links, { host, chatId, allowed, failed })`, `dropPreviews(root)`; `lengthRefusal(err)`,
    `lengthError(m)`, `continueOffer({ chatId, openWith, busy, failed }) -> { node, show(), hide(), button() }`;
    `knowledgeStep(li, s)`, `sourceClicks(list, { chatId, message, host })`; `resultImages(m, show)`;
    `effortChooser({ current, offered, choose, failed }) -> { chip, menu, paint(), close() }`.

- [ ] **Step 1: Write the failing tests — окно** `tests/test_chat_preview.py` (`add_gateway`, `signin`, `make_user`
  — `conftest`; `new_chat`, `turn`, `sse` — `test_chat`; `setup`, `name_of` — `test_mcp_dialog`; `protect`, `flags`,
  `code_of` — `test_protected_source`; `call` — `test_chat_web`):

```python
HTML = "<h1>Привет</h1><script>document.title = 'x'</script>"
post = lambda s, cid, kind="html", text=HTML: s.c.post(f"/api/chats/{cid}/preview", json={"kind": kind, "text": text})

def test_preview_lives_in_memory_and_is_served_sandboxed_without_network(studio, caplog):
    add_gateway(studio); cid = new_chat(studio); first = post(studio, cid); assert first.status_code == 201
    page = studio.c.get(f"/api/chats/preview/{first.json()['token']}")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html") and page.text == HTML
    csp = page.headers["content-security-policy"]
    assert csp == chat_preview.CSP and csp.startswith("default-src 'none'") and "sandbox allow-scripts" in csp
    assert not any(w in csp for w in ("allow-same-origin", "connect-src", "http", "*", "blob:"))   # сети нет вовсе
    assert csp.count("'self'") == 1 and "frame-ancestors 'self'" in csp
    assert (page.headers["cache-control"], page.headers["referrer-policy"]) == ("no-store", "no-referrer")
    second = post(studio, cid, "svg", "<svg xmlns='http://www.w3.org/2000/svg'><circle r='5'/></svg>").json()["token"]
    assert "<circle r='5'/>" in studio.c.get(f"/api/chats/preview/{second}").text
    assert studio.c.get(f"/api/chats/preview/{first.json()['token']}").status_code == 404   # одно окно на учётку
    assert chat_preview.PREVIEW_MAX == 2 * 1024 * 1024
    big = post(studio, cid, text="я" * (chat_preview.PREVIEW_MAX // 2 + 1))          # 2 МиБ — в байтах UTF-8
    assert (big.status_code, big.json()["detail"]["code"]) == (413, "preview_size")
    assert post(studio, cid, text="я" * (chat_preview.PREVIEW_MAX // 2)).status_code == 201
    studio.clock[0] += chat_preview.TTL_SEC + 1
    assert studio.c.get(f"/api/chats/preview/{second}").status_code == 404
    token = post(studio, cid).json()["token"]; make_user(studio, "bob"); signin(studio, "bob")
    assert post(studio, cid).status_code == 404 and studio.c.get(f"/api/chats/preview/{token}").status_code == 404
    assert "Привет" not in caplog.text

def test_protected_dialog_has_no_preview(studio):                                    # Review Focus 3
    sid, calls, _ = setup(studio); protect(studio, sid); cid = new_chat(studio)
    token = post(studio, cid).json()["token"]                                        # выдано до срабатывания
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="1")]}, ["Готово"]]
    sse(turn(studio, cid, "Use tool")); assert flags(cid)[0] == 1
    assert code_of(post(studio, cid)) == "protected"
    assert studio.c.get(f"/api/chats/preview/{token}").status_code == 404           # и выданное закрыто
```

- [ ] **Step 2: Run** `… tests/test_chat_preview.py` — Expected: FAIL (405: маршрута `/preview` нет).
- [ ] **Step 3: Implement** `app/chat_preview.py` (`router = APIRouter(prefix="/api")`, образец —
  `code_visualizations.py`). `VIEWS: dict[int, tuple]` — одно окно на учётку: `(token, digest сессии, chat_id,
  документ, срок по clock.NOW())`; текст — только в памяти процесса. `POST`: `chat._chat_row`; `protected_used` — 409
  `protected` словами `chat.PROTECTED_PREVIEW = "в этом диалоге сработал защищённый источник — HTML и SVG из ответа
  здесь не открываются"`; `len(text.encode()) > PREVIEW_MAX` (и одиночный суррогат) — 413 `preview_size` «блок
  больше 2 МиБ — в окне не открывается»; `secrets.token_hex(16)`. Документ: `html` — текст блока как есть, `svg` —
  `<!doctype html><meta charset="utf-8"><body style="margin:0">` + текст. `GET`: запись учётки с тем же токеном и
  той же сессией (`request.state.digest`), срок не вышел, диалог по-прежнему свой, не в корзине и без
  `protected_used` — иначе запись стирается и 404 `gone`. Журнал — `чат %s: окно HTML, %d байт`. Сеть закрывают три
  слоя: CSP ответа (`default-src 'none'`), `sandbox` без `allow-same-origin` (ни cookie, ни страницы Студии) и
  `default-src 'self'` самой страницы — переход кадра на чужой адрес запрещён её `frame-src`.
- [ ] **Step 4: Run** `tests/test_chat_preview.py tests/test_protected_source.py tests/test_static.py
  tests/test_docs.py tests/test_i18n_server.py` — PASS. **Commit** `Add a sandboxed window for HTML and SVG blocks of a chat answer`.
- [ ] **Step 5: Write the failing tests — итог** `tests/test_chat_continue.py` (ещё `base` — `test_knowledge`;
  `tool`, `offered`, `view` — `test_mcp_dialog`; `web_dialog`, `names` — `test_protected_source`; `add_connection`):

```python
KEEP = ("connection_id", "model", "role", "mcp_ids", "thinking", "effort", "title")

def test_continue_summarizes_the_whole_dialog_into_an_heir(studio, caplog):
    add_gateway(studio); cid = three_turns(studio); kb = base(studio)
    studio.c.put(f"/api/chats/{cid}/knowledge", json={"kb_ids": [kb]})
    studio.gw.chat_script = [["Итог: бирюза, три вопроса."]]
    r = studio.c.post(f"/api/chats/{cid}/continue")
    assert r.status_code == 201 and r.json()["text"] == "Итог: бирюза, три вопроса." and set(r.json()) == {"chat_id", "text"}
    asked = studio.gw.chat_requests[-1]; prompt = asked["messages"][1]["content"]
    assert asked["messages"][0]["content"] == chat.SUMMARY_NOTE and asked["max_tokens"] == chat.SUMMARY_TOKENS
    assert "tools" not in asked and "Вопрос-1 про бирюзу" in prompt and "Ответ-3" in prompt      # весь диалог
    new = r.json()["chat_id"]; assert new != cid and messages(studio, new) == []
    assert [view(studio, new)[k] for k in KEEP] == [view(studio, cid)[k] for k in KEEP]
    assert studio.c.get(f"/api/chats/{new}").json()["knowledge"] == [kb]
    assert tuple(stored(cid)) == (None, None, None) and len(messages(studio, cid)) == 6          # прежний не тронут
    assert "Итог" not in caplog.text and "бирюз" not in caplog.text
    studio.gw.chat_script = [["Итог-1"], ["Итог-2"]]; assert compact(studio, cid).status_code == 200
    studio.c.post(f"/api/chats/{cid}/continue"); prompt = studio.gw.chat_requests[-1]["messages"][1]["content"]
    assert prompt.startswith("Summary so far:\nИтог-1\n\nTranscript:\nuser: Вопрос-3") and "Вопрос-1" not in prompt

def test_continue_of_a_protected_dialog_stays_protected(studio):                     # Review Focus 3
    sid, calls, _ = setup(studio, tools=[tool("lookup"), tool("search", annotations={"openWorldHint": True})])
    protect(studio, sid); cid = web_dialog(studio)
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="1")]}, ["Секрет источника: 4711"], ["Итог с 4711"], ["Ответ"]]
    sse(turn(studio, cid, "Use tool")); assert flags(cid) == (1, 0)
    out = studio.c.post(f"/api/chats/{cid}/continue").json(); new = out["chat_id"]
    assert "tools" not in studio.gw.chat_requests[-1]                                # запрос итога — без инструментов
    assert flags(new) == (1, 0) and view(studio, new)["protected"] is True and view(studio, new)["web"] is False
    assert code_of(studio.c.patch(f"/api/chats/{new}", json={"web": True})) == "protected"
    sse(turn(studio, new, out["text"] + "\n\nЧто дальше?"))
    assert names(studio.gw.chat_requests[-1]) == [] and offered(studio.gw.chat_requests[-1]) == ["Shared tools: lookup"]
    assert code_of(studio.c.post(f"/api/chats/{new}/share", json={"pin": "0123"})) == "protected"
    assert code_of(studio.c.post(f"/api/chats/{new}/preview", json={"kind": "html", "text": "<p>4711</p>"})) == "protected"

def test_continue_refusals_create_nothing(studio):
    # пустой диалог — (409, "nothing"); прямое подключение — (409, "no_compact"); идёт ответ (chat_hold) — (409, "busy");
    # шлюз ответил 503 — (503, "card"); чужая учётка — 404; после каждого отказа число строк chats прежнее
    assert f"'{chat.CONTEXT_WORDS}'" in (STATIC / "js" / "chat-continue.js").read_text("utf-8")   # слова отказа по длине
```

- [ ] **Step 6: Run** `… tests/test_chat_continue.py` — Expected: FAIL (405: маршрута `/continue` нет).
- [ ] **Step 7: Implement.** В `app/chat.py` вынести из `compact_chat` (задача 7) в `summarize_dialog(uid, row,
  upto)` всё между чтением диалога и записью столбцов: отказы, расшифровку `compose(chat_id, upto, model, 0,
  excerpts=knowledge.heads(…))` без шапки прежнего пересказа и без текста последнего навыка, `seed` — действующий
  `chats.summary` (его `summary_upto` стоит не позже `upto`), запрос `StreamTask._summarize` вне `STREAMS`
  (подключение «Через Cleaner» — через `cleaner_route`), отметку `COMPACTING`; `upto` — id последней строки, которая
  входит в пересказ. `compact_chat` — вызов и прежняя запись; `tests/test_chat_summary.py` проходит без правок.
  `app/chat_continue.py`: `chat._chat_row`; последнего выбранного ответа с текстом нет — 409 `nothing`
  (`NOTHING = "в диалоге ещё нет ответа — итог писать не из чего"`); `text = await chat.summarize_dialog(uid, row,
  id последнего выбранного сообщения)`; `operations.check(uid)`; **строка диалога читается заново** (`_chat_row`:
  удалён — 404; отметка защищённого источника могла встать, пока модель писала) и только она идёт в
  `chat_fork.new_dialog(uid, fresh)` — название наследуется как есть. В `chats.summary` прежнего диалога итог не
  пишется, в базу нового — тоже: текст уходит только ответом. Журнал — `чат %s: диалог с итогом %s, знаков %d`.
  Слова отказов — в `en.js`.
- [ ] **Step 8: Run** `tests/test_chat_continue.py tests/test_chat_summary.py tests/test_chat_fork.py
  tests/test_protected_source.py tests/test_cleaner_route_chat.py tests/test_i18n_server.py tests/test_docs.py` —
  PASS. **Commit** `Add a new chat that starts from a summary of the current one`.
- [ ] **Step 9: Write the failing tests — источники и картинки.** `tests/test_chat_sources.py` (`base`, `big_text` и
  загрузка документа базы — `test_knowledge`; `steps_of` — `test_chat_web`; свой `chunk_text(doc_id, n)` — `SELECT
  text FROM kb_chunks WHERE doc_id = ? AND n = ?`):

```python
def test_knowledge_steps_name_document_and_chunk_and_give_the_text(studio):
    # база «Справочник» с документом справка.txt = big_text("Пароль от сейфа — 4711."), диалог со связанной базой
    studio.gw.chat_script = [{"tool_calls": [call("knowledge_search", query="пароль от сейфа")]}, ["4711"]]
    sse(turn(studio, cid, "Какой пароль от сейфа?")); answer = messages(studio, cid)[1]; auto, asked = answer["tools"]
    assert (auto["kind"], auto["auto"], auto["sources"]) == ("knowledge", True, ["справка.txt"])   # автоматический блок
    assert auto["found"] == len(auto["hits"]) >= 1 and "<knowledge>" in studio.gw.chat_requests[0]["messages"][-1]["content"]
    assert asked["query"] == "пароль от сейфа" and set(asked["hits"][0]) == {"doc", "n", "id"} and "text" not in asked
    url = f"/api/chats/{cid}/messages/{answer['id']}/sources"; hit = asked["hits"][0]
    got = studio.c.get(f"{url}/1/0").json()
    assert got["doc"] == "справка.txt" and got["n"] == hit["n"] and got["text"] == chunk_text(hit["id"], hit["n"])
    assert all(isinstance(s.get("text"), str) for s in steps_of(answer["id"]))        # прежний compose читает оба шага
    sse(turn(studio, cid, "Ещё")); again = json.dumps(studio.gw.chat_requests[-1]["messages"][:-1], ensure_ascii=False)
    assert again.count('<web kind="knowledge">') == 1                                 # авто-шаг в историю не идёт
    snapshot, _ = chat_shares.regular_snapshot(uid, cid); assert "hits" in json.dumps(snapshot) and "Обычная строка" not in json.dumps(snapshot, ensure_ascii=False)
    for bad in ("9/0", "1/99", "0/-1"): assert studio.c.get(f"{url}/{bad}").status_code == 404
    studio.c.delete(f"/api/knowledge/{kb}/documents/{hit['id']}"); assert code_of(studio.c.get(f"{url}/1/0"), 404) == "gone"
    # чужая учётка — 404; прямое подключение: авто-шаг записан у ответа до ответа JSON хода
```

  `tests/test_chat_result_images.py` (`png` — `test_chat`; `server(..., results={"lookup": RESULT})`):

```python
PIC = base64.b64encode(png("red")).decode()
RESULT = {"content": [{"type": "text", "text": "график готов"}, {"type": "image", "data": PIC, "mimeType": "image/png"},
                      {"type": "image", "data": "не base64", "mimeType": "image/png"}, {"type": "image", "data": PIC, "mimeType": "image/gif"}]}

def test_result_image_reaches_a_vision_model_and_stays_under_the_answer(studio, caplog):
    sid, calls, _ = setup(studio, results={"lookup": RESULT}); cid = new_chat(studio)   # local-gemma-abl — vision по перечню
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="1")]}, ["Вижу красный график."], ["Да."]]
    sse(turn(studio, cid, "Нарисуй")); tool_message, pictures = studio.gw.chat_requests[1]["messages"][-2:]
    assert (tool_message["role"], tool_message["content"]) == ("tool", "график готов")
    assert pictures["role"] == "user" and [p["type"] for p in pictures["content"]] == ["text", "image_url"]
    assert pictures["content"][0]["text"] == chat.RESULT_IMAGES_NOTE
    assert pictures["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
    answer = messages(studio, cid)[1]; (shown,) = answer["attachments"]
    assert (shown["kind"], shown["name"]) == ("image", "lookup-1.png") and answer["tools"][-1]["pictures"] == 1
    assert studio.c.get(f"/api/media/{shown['media_id']}").status_code == 200
    assert PIC[:60] not in json.dumps(steps_of(answer["id"])) and PIC[:60] not in caplog.text    # base64 — не в шаги и не в журнал
    sse(turn(studio, cid, "Ещё")); assert "image_url" not in json.dumps(studio.gw.chat_requests[-1]["messages"])
    task = SimpleNamespace(chat_id=cid, user_id=uid, model="local-gemma-abl", user_message_id=None)
    assert mcp_files._candidates(task) == []                                          # серверам MCP файлом диалога не уходит

def test_images_follow_max_images_text_models_cleaner_and_pause(studio):
    # три картинки и max_images 1 в профиле: в запросе одна `image_url` (последняя) и две пометки chat.DROPPED.format(1),
    #   под ответом — три; 20 картинок за ответ — вложений chat.RESULT_IMAGES_MAX == 16
    # модель без `vision` в паспорте: после `tool` сообщения user нет, под ответом картинка есть
    # подключение «Через Cleaner» (фикстура cleaner_gw): в gw.turns нет `image_url`, под ответом картинка есть
    # круг [lookup, ask_user]: после ответа на карточку запрос несёт сообщение user с `image_url` после обоих `tool`
```

- [ ] **Step 10: Run** оба файла — Expected: FAIL (`KeyError: 'hits'`; `server() got an unexpected keyword argument
  'results'`).
- [ ] **Step 11: Implement — источники.** `Hit` получает поле `doc_id: str = ""` (ставят `_search` и `_head`);
  `hits_view` — `[{doc, n, id}]` без повторов пары `(id, n)`. `for_turn(..., found=None)` дописывает в `found` куски
  блока `<knowledge>` и выдержки, найденные по вопросу (начало документа, взятое за неимением совпадений, — нет).
  `chat._turn_context(..., found=None)` передаёт список дальше. `StreamTask._context` после удачи при непустом
  `found` пишет шаг `self._step({"kind": "knowledge", "auto": True, "found": …, "sources": […], "hits": …, "text":
  ""})` — события потока нет, шаг придёт с перечитанным диалогом; `text: ""` — ради прежнего `compose` при откате.
  Прямое подключение (`create_turn`, после `_turn_context` :2986) — тем же шагом в `messages.tools` идущего ответа.
  `compose` пропускает шаги с `auto` (рядом с `"note" not in s`, :667). `chat_tools._knowledge` — ключ `hits`.
  `app/chat_sources.py`: шаг — как в `mcp_files.result_file` (:492–494), `hits[n]`, затем `knowledge.chunk`: документ
  жив и либо лежит в базе, видимой учётке (`visible_ids`), либо это документ вложения этого диалога и учётки; текст —
  `kb_chunks` по `(doc_id, n)`; иначе 404 `gone` «кусок недоступен: документ изменён, удалён или закрыт».
- [ ] **Step 12: Implement — картинки.** `Tools.call`: блоки `{"type": "image", "data": str, "mimeType"}` типов
  `image/png`, `image/jpeg`, `image/webp`, не длиннее `(image_upload.MAX_UPLOAD + 2) // 3 * 4` знаков, первые
  `RESULT_IMAGES` — во временный ключ шага `images`; результат из одних картинок — текст «Инструмент MCP вернул
  изображение». `StreamTask._calls` перед `_step` снимает ключ и зовёт `await self._result_images(step, tool,
  images)`: `base64.b64decode(validate=True)` и `normalize_image` в потоке (негодная — мимо), `media.save_async(…,
  "image/png", имя, thumb=True)`, строка `attachments(id, user_id, message_id = self.message_id, kind = 'image',
  name, media_id, chars = 0, created)`; `step["pictures"]` — сколько сохранено; сверх `RESULT_IMAGES_MAX` за ответ —
  мимо. При `_sees_images(self.conn, self.model)` id файлов копятся в новом поле `round_images`. Перед `return
  None` в `_calls` — `_flush_images()`: одно сообщение `{"role": "user", "content": [{"type": "text", "text":
  RESULT_IMAGES_NOTE}, {"type": "image_url", "media_id": …}, …]}` после всех `tool` круга, затем `fill_photos` и
  `_cap_images(self.messages, _max_images(self.conn, self.model))`; `OSError` шары — картинки мимо, ответ идёт.
  `_sees_images`: не `conn.cleaner` и не `conn.routed`, и у модели есть `vision` — в `capabilities.CATALOG_CAPS`
  (srvllm) или в паспорте подключения (`connections.passport_of`); не известно — нет. Пауза: payload
  (`chat_pending.pause`) несёт `images` — id из `round_images`, `_resume` их возвращает. `mcp_files._candidates` —
  `AND g.role = 'user'`. Журнал — `чат %s: ответ %s — картинок результата %d`.
- [ ] **Step 13: Run** `tests/test_chat_sources.py tests/test_chat_result_images.py tests/test_knowledge.py
  tests/test_chat_prefix.py tests/test_mcp_chat_files.py tests/test_chat_pending.py tests/test_media_orphans.py
  tests/test_chat_exports.py tests/test_docs.py` — PASS (после слияния с задачей 10 той же волны — ещё
  `tests/test_chat_parts.py`: картинка результата внутри части идёт тем же путём). **Commit** `Show knowledge
  sources under chat answers`; `Pass images from MCP tool results to vision models`.
- [ ] **Step 14: Write the failing test — уровень** `tests/test_chat_effort.py` (`monkeypatch.setitem(
  capabilities.PROFILES, "cloud.test", …)`; каждая проверка хода — свой диалог):

```python
def test_effort_follows_the_provider_profile_and_reaches_the_request(studio, monkeypatch):
    cloud = add_connection(studio); cid = new_chat(studio, connection_id=cloud, model="local-gemma-abl")   # «compatible»
    patch = lambda body: studio.c.patch(f"/api/chats/{cid}", json=body)
    assert view(studio, cid)["effort_ok"] is False and code_of(patch({"effort": "high"})) == "no_effort"
    monkeypatch.setitem(capabilities.PROFILES, "cloud.test", "openai")
    assert view(studio, cid)["effort_ok"] is True and patch({"effort": "high"}).json()["effort"] == "high"
    assert patch({"effort": "max"}).status_code == 422 and patch({"title": "Имя"}).json()["effort"] == "high"
    assert turn(studio, cid).json()["params"] == {"reasoning_effort": "high"}        # прямое: параметр — странице
    monkeypatch.setitem(capabilities.PROFILES, "cloud.test", "openrouter")
    other = new_chat(studio, connection_id=cloud, model="local-gemma-abl")
    studio.c.patch(f"/api/chats/{other}", json={"effort": "medium"})
    assert turn(studio, other).json()["params"] == {"reasoning": {"effort": "medium"}}
    assert patch({"effort": None}).json()["effort"] is None                           # «как у модели»
    gw = add_gateway(studio); local = new_chat(studio); assert view(studio, local)["effort_ok"] is False
    state.DB.execute("UPDATE chats SET effort = 'low' WHERE id = ?", (local,)); sse(turn(studio, local))
    assert not {"reasoning_effort", "reasoning"} & set(studio.gw.chat_requests[-1])   # непринимающему не уходит
    # `openai` через сервер (как test_request_params_by_provider): chat_requests[-1]["reasoning_effort"] == "low";
    #   продолжение после паузы несёт его же; подключение «Через Cleaner» — effort_ok False
    assert profiles.refusal("Unrecognized request argument supplied: reasoning_effort",
                            {"reasoning_effort": "high"}) == profiles.EFFORT_REFUSED
```

- [ ] **Step 15: Run** — Expected: FAIL (`KeyError: 'effort_ok'`). **Implement.** `effort_ok`: не `conn.cleaner`,
  не `conn.routed`, `capabilities.profile(conn) in EFFORT_PROVIDERS`. `effort_params`: `openai` →
  `{"reasoning_effort": level}`, `openrouter` → `{"reasoning": {"effort": level}}`, без уровня или без `effort_ok`
  — `{}`. `profiles.refusal`: названный параметр — `reasoning_effort` или `reasoning` — слова `EFFORT_REFUSED =
  "модель не принимает уровень рассуждения — выберите «Как у модели»"`. `ChatPatch.effort: Literal["low", "medium",
  "high"] | None = None`; `patch_chat` (после блока `thinking` :2115–2121): поле прислано — значение при `effort_ok`
  нового подключения диалога, иначе 409 `no_effort` (`NO_EFFORT = "у этого подключения уровень рассуждения не
  выбирается"`); `null` — сброс. `_chat_view` — `effort`, `effort_ok`. `create_turn` сразу после `params = …`
  (:2889): `params |= profiles.effort_params(conn, fresh["effort"])` — по подключению диалога, а не по `profiled`
  (уровень — настройка диалога, не профиля); то же в `chat_pending.py:294`. Смена модели столбец не трогает:
  непринимающему подключению параметр не уходит. **Run** `tests/test_chat_effort.py
  tests/test_connection_profiles.py tests/test_chat.py tests/test_chat_fork.py` — PASS. **Commit** `Add a reasoning
  effort level to chats of providers that accept it`.
- [ ] **Step 16: Write the failing test** `tests/test_ui_chat_extras.py` (`integration`; `signin`, `READY`,
  `open_new_chat`, `ANSWERED`, `LAST_ANSWER`, `NO_OVERFLOW`, `API`, `live_sql`, `add_gateway`, `add_cloud`, `problems`
  — `test_ui_browser`). Блок-проба `PROBE` — HTML со встроенным скриптом: два `fetch` (`/health` и
  `https://example.test/x`), картинка `/static/icons/icon-192.png`, чтение `document.cookie`, `localStorage`,
  `parent.document`; итог — `parent.postMessage({ probe }, '*')`, страница теста кладёт его в `window.probe`:

```python
@pytest.mark.parametrize("width", [320, 1280])
def test_html_and_svg_blocks_open_in_a_sandbox_without_network(browser, live, width):      # Review Focus 3
    live.gw.chat_reply = ["Вот:\n\n```html\n" + PROBE + "\n```\n\n```svg\n<svg xmlns='http://www.w3.org/2000/svg'/>\n```\n\n```js\n1\n```\n"]
    assert v["buttons"] == ["html", "svg"]              # .preview-open[data-kind]; у блока js кнопки нет
    assert v["frame"] == ["allow-scripts", "no-referrer", True]   # sandbox, referrerpolicy, src — /api/chats/preview/<токен>
    assert v["probe"] == {"ran": True, "fetch": ["rejected", "rejected"], "img": "blocked",
                          "cookie": "denied", "storage": "denied", "parent": "denied"}
    assert v["big"] == [1, 0] and v["fits"] is True     # decorate: блок ровно 2 МиБ — кнопка, на байт больше — нет
    live_sql(live, "UPDATE chats SET protected_used = 1, web = 0 WHERE id = ?", (cid,))
    assert v["protected"] == [0, 409, "protected"]      # после перечитывания кнопок нет; прямой POST — отказ

def test_length_refusal_offers_a_new_chat_with_the_summary(browser, live, monkeypatch):
    # окно модели 9000 токенов (`window` задачи 9); ход с текстом 40 000 знаков — 413 `context`
    assert v["offer"] == ["Новый диалог с итогом", False]            # #chat-continue рядом с #chat-error
    assert v["moved"] == [True, "Итог первого диалога"] and live.gw.chat_requests[-1]["max_tokens"] == 2048   # новый адрес, поле
    assert v["answer_offer"] is True     # ответ-ошибка со словами chat.CONTEXT_WORDS: .msg-continue среди кнопок ответа

@pytest.mark.parametrize("width", [320, 1280])
def test_sources_pictures_and_effort(browser, live, width, monkeypatch):
    assert v["sources"] == ["справка.txt · кусок 2"] and "4711" in v["chunk"]       # .kb-source и #kb-source-dialog
    assert v["pictures"] == [1, True]                    # .msg-pictures .att-thumb; нажатие — #att-photo-dialog
    assert v["effort"] == [True, ["Как у модели", "Низкий", "Средний", "Высокий"], "Высокий"]   # #effort-chip, #effort-menu
    assert live.cloud.chat_requests[-1]["reasoning_effort"] == "high" and v["local"] is True    # у srvllm чип скрыт
    assert v["kept"] == "Высокий" and v["fits"] is True and not problems(r)          # после перезагрузки; NO_OVERFLOW
```

- [ ] **Step 17: Run** `… -m integration tests/test_ui_chat_extras.py` — Expected: FAIL (нет `.preview-open`).
- [ ] **Step 18: Implement — страница.** `chat-preview.js`: `decorate` — у `pre > code.language-html,
  .language-svg`, при `allowed()` и `new Blob([code.textContent]).size <= PREVIEW_MAX`, кнопка
  `button.preview-open[data-kind]` «Открыть» рядом с `.copy`; нажатие — `POST …/preview`, окно
  `dialog#chat-preview-dialog` (`openDialog`): строка «Окно изолировано: без сети и без доступа к Студии» и
  `iframe.chat-preview-frame[sandbox="allow-scripts"][referrerpolicy="no-referrer"]` с `src =
  /api/chats/preview/<токен>`; закрытие снимает `src`; отказ — `failed`. Кадр — `color-scheme: light; background:
  Canvas`. В `paint()`: `fileLinks = final ? previewLinks(resultPathLinks(chatId, m), { host: root, chatId: () =>
  chatId, allowed: () => received.has(m) && !chat?.chat.protected, failed }) : null` — кнопка появляется только у
  сообщения, перечитанного с сервера: отметка защищённого источника к этому мигу верна; `shareContext()` при
  `current?.protected` зовёт `dropPreviews(root)`. `chat-continue.js`: `CONTEXT_WORDS` — копия `chat.CONTEXT_WORDS`;
  `lengthRefusal` — `err.code === 'context'`, `lengthError` — `m.status === 'error'` и `m.error` (слова сервера до
  перевода) начинается с `CONTEXT_WORDS`; `node` — `p.chat-continue#chat-continue-offer[hidden]` с кнопкой
  `#chat-continue`; `button()` — `.btn.btn-sm.msg-continue`; нажатие — «Пишу итог…», `POST …/continue`,
  `openWith(chat_id, text)`; при `busy()` кнопки выключены. `chat-knowledge-sources.js`: `knowledgeStep` — «Знания:
  «запрос»» (у `auto` — «Знания диалога») и `ul.kb-sources` из `button.btn-link.kb-source` «{документ} · кусок
  {n}»; шаг без `hits` — прежняя строка имён; `sourceClicks` (в `messageNode` после `const steps = …`) по нажатию
  берёт номер шага в `m.tools`, читает `…/sources/<шаг>/<k>` и открывает `dialog#kb-source-dialog` с
  `pre.kb-source-text`. `resultImages` — `div.msg-pictures` миниатюр `button.att-thumb` (как `attLabel` :1643–1646)
  по `m.attachments` ответа, нажатие — `showPhoto`. `effortChooser` — по образцу `roleChooser` (`roles.js`):
  `button#effort-chip.chip.ctl-chip[aria-haspopup=menu]` (значок `bulb`, слово «Рассуждение», уровень) и
  `div#effort-menu[role=menu]` из `menuitemradio[data-effort]`; виден при `offered()` — у открытого диалога
  `chat.chat.effort_ok`, у нового — `connection.effort` ссылки раздела; `choose` — `PATCH {effort}` или
  `draftEffort`, который `createChat` применяет после создания. `serviceRefusal`: имя `reasoning_effort` или
  `reasoning` — слова `EFFORT_REFUSED`.
- [ ] **Step 19: Run** `-m integration tests/test_ui_chat_extras.py tests/test_ui_chat_status.py
  tests/test_ui_knowledge.py tests/test_ui_chat_files.py tests/test_ui_chat_composer.py tests/test_ui_design_consistency.py`
  и быстрые `tests/test_i18n.py tests/test_css_tokens.py tests/test_sections_meta.py` — PASS (плашка ответа с базой
  знаний теперь «Знания: 1 поиск» — тест, ждавший её отсутствия, править под новое). **Commit** `Add the HTML window,
  summary chat offer, knowledge sources, result images and effort chip to the chat page`.

### Task 16: Все разделы: звук, «Цитировать» и меню сообщения, ширина ленты и поле ввода, «Поделиться» чатом «Озвучки» и Open WebUI

Пункты 37, 38, 41, 42. Что нашли. Звука нет вовсе; `AudioContext` заводит только «Разговор» (`chat-voice.js:36–38`).
Готовое всех разделов сходится в `jobs.js::take` (:272–275, список `fresh`), состояния «Агентов» — в событии
`studio-code-snapshot` (`code-activity.js:45`), конец ответа «Чата» — в `finish(job)` (`chat.js:252`). Поле ввода
общее — `fieldBox` (`field.js:18`); потолок высоты — `max-height: calc(25 * var(--dvh))` (`app.css:440`) и
`innerHeight * .25` в «Агентах» (`code-chat.js:1817`). Лента — `max-width: 800px` у `.feed` (`app.css:1003`) и
`.owui-feed-list` (:2574), поле — 900 px (:435); у `.code-feed` (:2079) предела нет. Настройки устройства до первой
отрисовки ставит `theme.js:7–20`. Отправка сверяет поле с отправленным текстом (`code-chat.js:1853`, `:1864`,
`chat.js:2609`), поэтому цитата перед отправкой переносится в само поле. Ссылки записей разделов — `chat_shares.ITEMS`
с маршрутами `_item_routes` (:562–595) и снимком `item_snapshot` (:515); `LIVE` (:611–614) строится по таблицам, а у
новых видов таблицы с таким именем нет; название чата без строки в базе хранится шифром `label` (:244). Триггеры и
формат `item_id` обоих видов задала задача 1. У ответа Open WebUI нет кнопок (`owui-chat.js::build` :146–172).

**Files:**
- Create: в `app/static/js/` — `sound.js`, `quote.js`, `message-menu.js`; `app/owui_share.py`
- Modify: `app/static/js/field.js` (`fieldBox`, параметр `fold`), `appearance.js::deviceLookBlock`, `app/static/theme.js`,
  `settings.js:1876`, `jobs.js::take`, `app.js:45`, `icons.js` (значки `quote`, `collapse`), `chat.js`, `code-chat.js`,
  `owui-chat.js`, `speech.js` (:118–125, :548), `chat-share.js::KINDS`, `share-view.js` (`KINDS`, `SHOWN`,
  `itemNodes`, имена файлов сообщения), `account-security.js` (`DETAILS` :41–43 и адрес «Открыть» ссылки);
  `app/chat_shares.py` (`ITEMS`, `_ROWS`, новые `OWUI`, `SECTIONS`, `_ALIVE`; `_detail`, `_store`, `item_snapshot`,
  `LIVE`), `app/account.py::_item_share`, `app/lock_policy.py::media_sections` (:116–122), `app/owui.py` (вынести
  чтение чата в `read_chat`), `app/main.py` (`include_router(owui_share.router)` после `owui.router` :251);
  `app/static/css/app.css`, `app/static/en.js`; `CLAUDE.md` (карта модулей: `owui_share`, три модуля страницы),
  `docs/architecture/shell.md`, `sharing-export.md`, `open-webui.md`, `speech.md`, `notifications.md`
- Test: Create `tests/test_owui_share.py`, `tests/test_ui_sound.py`, `tests/test_ui_quote.py`,
  `tests/test_ui_feed_width.py`, `tests/test_ui_share_new_kinds.py`; Modify `tests/test_item_shares.py`,
  `tests/test_css_tokens.py`
- Подключение: `chat.js` — импорты после `import { NAMES } …` (:116); `finish(job)` :252; `fieldBox(input, { …,
  fold: true })` :788; `quote.node` в форме перед `inputBox.box` :799 (после чипа навыка задачи 10); блок после
  `head.append(share);` :815; `submit()` — первой строкой :2401; `enqueue()` задачи 13; `open()`; `destroy()`.
  `code-chat.js` — импорты; `fieldBox(text, { fold: true })` :364; `quote.node` перед `field.box` :434; `autosize()`
  :1817; `submit()` — перед `const value = …` :1832; `unmount()` :2071. `owui-chat.js` — импорты; `fieldBox(input, {
  attach: clip, fold: true })` :82; форма :83; шапка ленты :85–86; `build()` :166–170; `submit()` :408; события
  `done` и `pending` :555–564; `unmount()` :590.

**Interfaces:**
- Consumes: триггеры `chat_shares_speech_chats_gone`, `chat_shares_owui_gone` и формат `item_id` (задача 1:
  `speech_chats` — `speeches.chat_id`, `owui` — `"<owui_accounts.id>:<id чата>"`); `answerTools(node, { text })`
  (`code-answer-tools.js`, задача 12); `enqueue()` очереди (задача 13); `shareMenu`, `shareDialog`.
- Produces:
  - `sound.js`: `SOUND_KEY = 'studio-sound'` (`'1'` — включён, по умолчанию ключа нет), `SIGNALS = { done: [660,
    880], ask: [880, 880], error: [330, 220] }` (Гц, два тона по 120 мс, пауза 60 мс), `GAP_MS = 1500`, `REPEAT_MS =
    15000`; `soundOn()`, `setSound(on)`, `signal(kind, key = null) -> bool`, `answerSignal(message)`,
    `jobSignal(fresh)`, `agentSignals(before: Map, aid, items) -> string[]`, `soundBlock() -> HTMLElement`.
  - `quote.js`: `QUOTE_MAX = 4000`, `quoted(text) -> string`, `quoteChip(host, field, { id, changed } = {}) -> {
    node, set(text), clear(), value(), fold() }`, `selectionQuote(scroller, { texts, quote }) -> { unmount() }`.
  - `message-menu.js`: `LONG_PRESS_MS = 550`, `messageMenu(scroller, { message, text, quote, said }) -> { unmount()
    }`; узлы `#msg-menu`, `.msg-menu-anchor`.
  - `fieldBox(field, { …, fold = false })` — кнопка `.field-fold`; настройка `localStorage['studio-feed-width']`
    (`wide` | `full`; нет ключа — обычная), атрибут `html[data-feed]`, токены `--feed-max` (800 px / 1100 px /
    `none`) и `--composer-max` (900 px / 1200 px / `none`).
  - `chat_shares.ITEMS["speech_chats"] = ("speech", "/api/speeches/chats")` — маршруты `GET/POST/PUT/DELETE
    /api/speeches/chats/{cid}/share`; `chat_shares.OWUI = "owui"`, `chat_shares.SECTIONS: dict[str, str]`;
    `owui.read_chat(acc, chat) -> dict`; `owui_share.snapshot(acc, chat) -> dict`; `GET/POST/PUT/DELETE
    /api/owui/accounts/{aid}/chats/{chat}/share`, `POST /api/owui/accounts/{aid}/chats/{chat}/export`. В «Доступах и
    ссылках» — `kind: "speech" | "owui"`.

- [ ] **Step 1: Write the failing tests — ссылки.** `tests/test_item_shares.py` (помощник `speech_chat(uid)`: записи
  `s1`, `s2` чата `chat_id = 'sc1'` — готовые, с mp3 `b"mp3-1"`, `b"mp3-2"` и голосом «Анна», и `s3` — `cancelled`;
  итог — адрес и id двух файлов):

```python
def test_speech_chat_link_carries_texts_and_audio_of_ready_records(studio):
    uid = owner(studio); endpoint, (a, b) = speech_chat(uid); out, token = create(studio, endpoint)
    opened = unlock(studio, token).json()
    assert opened["messages"] == chat_shares.PLACEHOLDER["messages"] and opened["title"] == "Первый текст"
    assert [(r["text"], r["voice"], r["file"]["mime"]) for r in opened["item"]["records"]] == [
        ("Первый текст", "Анна", "audio/mpeg"), ("Второй текст", "Анна", "audio/mpeg")]
    second = opened["item"]["records"][1]["file"]["file"]
    assert studio.c.post("/share/file", json={"access": opened["access"], "file": second}).content == b"mp3-2"
    row = state.DB.execute("SELECT item_kind, item_id FROM chat_shares").fetchone()
    assert tuple(row) == ("speech_chats", "sc1")
    listed = studio.c.get("/api/me/access").json()["shares"]
    assert [(s["kind"], s["item_id"], s["title"], s["trashed"]) for s in listed] == [("speech", "sc1", "Первый текст", False)]
    assert studio.c.delete("/api/speeches/chats/sc1").status_code == 204             # чат — в корзину
    assert unlock(studio, token).status_code == 404 and studio.c.get("/api/me/access").json()["shares"][0]["trashed"] is True
    state.DB.execute("UPDATE speeches SET deleted = NULL WHERE chat_id = 'sc1'"); assert unlock(studio, token).status_code == 200
    state.DB.execute("DELETE FROM speeches WHERE id = 's1'"); assert state.DB.execute("SELECT count(*) FROM chat_shares").fetchone()[0] == 1
    assert lock_policy.media_sections(uid, a) == {"speech"}      # файл удалённой записи держит только снимок ссылки
    state.DB.execute("DELETE FROM speeches WHERE chat_id = 'sc1'"); assert state.DB.execute("SELECT count(*) FROM chat_shares").fetchone()[0] == 0
    # чат с идущей записью — POST 409 `running`; чужой чат — 404; блокировка раздела «Озвучка» — 423 `screen_locked`

def test_previous_live_rule_hides_links_of_the_new_kinds(studio):                    # откат: SQL-обход не нужен
    old = (" AND (s.chat_id IS NULL OR s.chat_id IN (SELECT id FROM chats WHERE deleted IS NULL AND protected_used = 0))"
           " AND (s.item_kind IS NULL OR " + " OR ".join(f"(s.item_kind = '{t}' AND s.item_id IN (SELECT id FROM {t}"
           " WHERE deleted IS NULL))" for t in ("transcripts", "translations", "scans", "images", "videos")) + ")")
    for kind in ("speech_chats", "owui"):                # строки ссылок обоих видов созданы новым кодом
        assert state.DB.execute("SELECT 1 FROM chat_shares s WHERE s.item_kind = ?" + old, (kind,)).fetchone() is None
        assert state.DB.execute("SELECT 1 FROM chat_shares s WHERE s.item_kind = ?" + chat_shares.LIVE, (kind,)).fetchone()
```

  `tests/test_owui_share.py` (`setup`, `talk`, `BASE` — `test_owui_read`; `create`, `unlock` — `test_chat_shares`):

```python
def test_owui_link_is_a_sealed_snapshot_of_its_own_kind(setup, caplog):
    s, fake, aid = setup; chat = talk(fake); endpoint = f"{BASE}/{aid}/chats/{chat}/share"
    out, token = create(s, endpoint); opened = unlock(s, token).json()
    assert [(m["role"], m["text"]) for m in opened["messages"]] == [("user", "hello"), ("assistant", "hi there")]
    assert opened["messages"][0]["files"] == ["doc.txt"] and opened["messages"][1]["reasoning"] is None
    assert opened["title"] == "Talk" and "item" not in opened and opened["messages"][0]["attachments"] == []
    row = state.DB.execute("SELECT * FROM chat_shares").fetchone()
    assert (row["item_kind"], row["item_id"], row["chat_id"], row["agent_id"]) == ("owui", f"{aid}:{chat}", None, None)
    assert row["snapshot"] == chat_shares.PLACEHOLDER_RAW and security.decrypt(row["label"]) == "Talk"
    assert b"hi there" not in bytes(row["sealed"])                                    # снимок — только шифром
    listed = s.c.get("/api/me/access").json()["shares"]
    assert [(i["kind"], i["item_id"], i["title"]) for i in listed] == [("owui", f"{aid}:{chat}", "Talk")]
    # fake меняет ответ; PUT — тот же адрес и PIN, unlock показывает новое
    # Open WebUI молчит — POST 503 `owui_offline`, строка ссылки прежняя; чужая учётка Студии — 404 на все четыре метода
    assert s.c.delete(f"{BASE}/{aid}").status_code == 204                             # учётка Open WebUI удалена
    assert state.DB.execute("SELECT count(*) FROM chat_shares").fetchone()[0] == 0 and unlock(s, token).status_code == 404
    assert "hello" not in caplog.text and "hi there" not in caplog.text and "Talk" not in caplog.text

def test_owui_export_gives_markdown_and_follows_the_section_lock(setup):
    r = s.c.post(f"{BASE}/{aid}/chats/{chat}/export", json={"format": "md", "title": "Talk", "lang": "ru"})
    assert r.status_code == 200 and "hi there" in r.text and r.headers["cache-control"] == "no-store"
    # раздел Open WebUI заперт (lock_policy) — share и export отвечают 423 `screen_locked`
```

- [ ] **Step 2: Run** `… tests/test_item_shares.py tests/test_owui_share.py` — Expected: FAIL (404 и 405: маршрутов нет).
- [ ] **Step 3: Implement — сервер.** `chat_shares`: `ITEMS["speech_chats"]`, `_ROWS["speech_chats"] =
  speeches._chat_rows`; `SECTIONS = {вид: раздел по ITEMS} | {OWUI: "owui"}` — им пользуются `_detail`, `_store`
  (`_owner_open`) и `account._item_share`; `_store` проверяет `_ROWS[kind]` только у видов из `_ROWS`, у `OWUI` —
  `owui.load(uid, aid)`, и пишет `label` шифром ещё и для `OWUI`. `LIVE` собирается из `_ALIVE`: пять таблиц — как
  было; `speech_chats` — `s.item_id IN (SELECT chat_id FROM speeches WHERE deleted IS NULL AND user_id =
  s.user_id)`; `owui` — `CAST(substr(s.item_id, 1, instr(s.item_id, ':') - 1) AS INTEGER) IN (SELECT id FROM
  owui_accounts WHERE user_id = s.user_id)`. `item_snapshot`, ветка `speech_chats`: запись вне `speeches.TERMINAL` —
  409 `running` «дождитесь озвучки или отмените её»; готовых с файлом нет — 409 `running`; `item = {"kind":
  "speech_chats", "records": [{"text", "voice": voice_name or voice, "duration", "file": _media(…)}]}` по готовым
  записям по порядку, название — первые 80 знаков первого текста, файлы — все mp3; `messages` — заглушка.
  `account._item_share`: `speech_chats` — название из первой записи группы, `trashed` — живых записей нет; `owui` —
  `_label(label)`. `lock_policy.media_sections`: вид `speech_chats` — `{"speech"}`. `owui.read_chat` — чтение и
  разбор чата из `chat_view` (:617–622), маршрут зовёт её. `app/owui_share.py`: `snapshot` — `{"title", "messages":
  [{"role", "text", "attachments": [], "files": [имена], "steps": [{"name", "text": результат}], "reasoning": None,
  "status": "done" | "streaming", "tools": [], "error", "todo": None}]}` — файлы остаются в Open WebUI, рассуждение
  в ссылку не идёт; источник ссылки — `item_kind = OWUI`, `item_id = f"{aid}:{chat}"`; маршруты `share` — по образцу
  `chat_share` … `revoke_chat_share` через `chat_shares._owned` и `_store`; `export` — `chat_exports._file(snapshot,
  body, request, "owui", check)`, `check` — `owui.load`. В журнал — только id учётки.
- [ ] **Step 4: Run** `tests/test_item_shares.py tests/test_owui_share.py tests/test_chat_shares.py
  tests/test_owui_read.py tests/test_speech_chats.py tests/test_chat_exports.py tests/test_foundation.py
  tests/test_i18n_server.py tests/test_docs.py` — PASS. **Commit** `Share speech chats and Open WebUI chats by link`.
- [ ] **Step 5: Write the failing tests — страница** (все `integration`; помощники `test_ui_browser`; `probe` —
  `tests/test_ui_shared_modules.py`). `tests/test_ui_sound.py` — `init` подменяет `window.AudioContext` классом,
  который пишет частоты тонов в `window.tones`:

```python
def test_sound_is_off_by_default_and_plays_three_signals_once(browser, live):
    assert v["default"] == [None, []]                    # localStorage['studio-sound']; ответ кончился — тишина
    assert v["on"] == ["1", [660, 880]]                  # «Настройки → Уведомления», #sound-on: проба «готово»
    assert v["done"] == [660, 880] and v["after_poll"] == [660, 880]   # ответ чата; через 6 с опрос «В работе» не повторил
    assert v["error"] == [330, 220] and v["ask"] == [880, 880]         # ответ-ошибка; пауза хода (ask_user)
    assert v["hidden"] == []                             # visibilityState 'hidden' — молчит
    assert v["agents"] == [[], ["ask"], ["done"]]        # agentSignals: первый снимок; running → asking; running → нет
```

  `tests/test_ui_quote.py`:

```python
@pytest.mark.parametrize("width", [320, 1280])
def test_quote_chip_folds_into_the_field_and_the_menu_mirrors_actions(browser, live, width):   # проба модулей
    assert v["quoted"] == ["> а\n> б", 4000]             # quoted('а\nб'); длина цитаты из 5000 знаков без «> » и «…»
    assert v["chip"] == [False, "Цитата: первая строка"] and v["fold"] == [True, "> цитата\n\nвопрос", True, 1]
    assert v["menu"] == ["Копировать", "Цитировать", "Изменить"] and v["clicked"] == 1   # contextmenu у .msg; пункт жмёт кнопку
    assert v["link"] is False and v["long"] == [True, False] and v["inside"] is True     # у ссылки — меню браузера; 550 мс

def test_quote_reaches_the_request_in_chat_agents_and_open_webui(browser, live):
    assert live.gw.chat_requests[-1]["messages"][-1]["content"] == "> Здравствуйте!\n\nПочему?"   # выделение, #quote-float
    assert code_requests(machine, "turn")[0]["text"].startswith("> ") and v["agents_field"] == ""  # меню .code-msg
    # Open WebUI (эталон и помощники — tests/test_ui_owui.py): меню .owui-msg, «Цитировать», отправка
    assert owui_sent_text.startswith("> hi there") and v["owui_copied"] == "hi there"   # текст, принятый эталоном; .msg-copy
```

  `tests/test_ui_feed_width.py` (320 и 1280) и `tests/test_ui_share_new_kinds.py`:

```python
def test_feed_width_and_field_fold(browser, live, width):
    assert v["widths"] == ["800px", "1100px", "none"] and v["stored"] == [None, "wide", "full"]   # .feed, сегмент #appearance-feed
    assert v["owui"] == "1100px" and v["reload"] == "wide" and v["fits"] is True                   # .owui-feed-list; theme.js
    assert v["grown"] is True          # 60 строк: высота #chat-input > 25 % и <= 50 % высоты окна
    assert v["fold"] == ["Свернуть поле", "true", True, "Развернуть поле"]   # .field-fold: подпись, aria-pressed, высота <= 6em
    assert v["sent"] == ["", True]     # после отправки поле пусто, кнопка скрыта

def test_speech_chat_and_open_webui_are_shared_from_their_sections(browser, live, width):
    assert v["speech"] == [True, False, "Поделиться чатом озвучки"]   # #sp-share: скрыта, пока идёт запись; заголовок окна
    assert v["shared"] == ["Только чтение · чат озвучки", 2, "AUDIO"]  # страница /share: две записи, «Слушать» ставит audio
    assert v["owui"] == ["Поделиться ссылкой", "Скачать PDF", "Скачать выделенное в PDF", "Скачать Markdown"]   # #owui-share-menu
    assert v["owui_link"].startswith(live.url + "/share#") and v["fits"] is True
```

  `tests/test_css_tokens.py::test_feed_width_is_one_contract_for_css_page_and_early_theme` (быстрый): значения
  `wide` и `full` — одни и те же в `theme.js`, `appearance.js` и селекторах `[data-feed="…"]` `app.css`.
- [ ] **Step 6: Run** четыре файла с `-m integration` — Expected: FAIL (`sound.js`, `quote.js`, `message-menu.js` — 404).
- [ ] **Step 7: Implement — звук.** `sound.js`: один `AudioContext` на страницу, создаётся первым нажатием или
  клавишей (слушатель модуля, `once`) и в обработчике `#sound-on`; `signal` молчит, когда звук выключен, страница
  не `visible`, контекст не `running`, тот же `key` звучал меньше `REPEAT_MS` назад или любой сигнал — меньше
  `GAP_MS` назад; тон — `OscillatorNode` `sine` через `GainNode` (пик 0,08, нарастание и спад по 10 мс).
  `answerSignal` — `ask` при `message.pending`, `error` при `status === 'error'`, `done` при `done`, ключ — id
  ответа; `jobSignal(fresh)` — `error`, если у любого итога `state !== 'done'`, иначе `done`, ключ — id первого;
  слушатель `studio-code-snapshot` ведёт `Map` «учётка:чат → состояние» и играет `agentSignals` (первый снимок
  компьютера — без звука; стало `asking` — `ask`; было `running` или `asking`, стало не `running` — `done`).
  Подключение: `chat.js::finish`; `jobs.js::take` — `if (!first && fresh.length) jobSignal(fresh);` перед `first =
  false`; `owui-chat.js` — `done` (`error` при `data.error`) и `pending` (`ask`); `app.js:45` — `import
  './sound.js'`; `settings.js:1876` — `soundBlock()` после `notifyBlock()`: `section.block#sound-settings`, флажок
  `#sound-on` «Звуковые сигналы», кнопка `#sound-test` «Проверить», слова «Только на этом устройстве и пока Студия
  на экране». Выход из учётки ключ не стирает (настройка устройства, как тема).
- [ ] **Step 8: Implement — цитата и меню.** `quoted`: текст без краевых пробелов, первые `QUOTE_MAX` знаков (при
  обрезке — «…»), каждая строка — с «> ». Чип — `div.quote-chip#<id>[hidden]`: значок `quote`, начало цитаты (80
  знаков, многоточие, `title` — до 300), `iconButton('x', tr("Убрать цитату"))`. `fold()` — чип есть: `field.value =
  quoted(цитата) + "\n\n" + field.value` (пустое поле — без хвоста), событие `input`, чип снят; итог — был ли чип.
  Разделы зовут `quote.fold()` первой строкой своей отправки («Чат» — при `!regenerate`, «Агенты» — при `!quick`,
  `enqueue()` очереди — тоже): дальше код видит обычное поле, сверки «поле равно отправленному» целы; `open()` «Чата»
  чип снимает. `selectionQuote`: выделение не пусто и целиком внутри одного узла `texts` в `scroller` — кнопка
  `button.quote-float#quote-float` («Цитировать», `position: fixed` у конца выделения, в пределах окна; `mousedown`
  выделение не сбрасывает); нажатие — `quote(выделенный текст)`; прокрутка, Escape и пустое выделение прячут.
  `messageMenu`: `contextmenu` внутри узла `message` (правая кнопка, долгое нажатие Android, клавиша меню), а где
  события нет (iOS) — `touchstart` без сдвига больше 10 px в течение `LONG_PRESS_MS`; не открывается на `a[href],
  img, video, audio, input, textarea` и с Shift — там меню браузера. Меню — одно на ленту: `popupMenu` у скрытой
  кнопки-якоря, поставленной в точку нажатия; пункты: «Копировать» и «Цитировать» (выделение внутри этого
  сообщения, иначе `text(узел)`), затем по пункту на каждую видимую включённую кнопку `.msg-actions`,
  `.code-msg-actions` и `.code-fork` узла (кроме `.msg-copy`) — пункт нажимает её. Тексты: «Чат» — `.bubble` у
  сообщения пользователя, `content` ответа по `data-id`; «Агенты» — `.code-user-text` и склейка
  `.code-timeline-text`; Open WebUI — `text` сообщения по `data-id`. В `owui-chat.js::build` у готового ответа с
  текстом — `answerTools(body, { text: m.text })`.
- [ ] **Step 9: Implement — ширина, поле, кнопки «Поделиться».** `app.css`: `:root { --feed-max: 800px;
  --composer-max: 900px; }`, `[data-feed="wide"]` — 1100 и 1200 px, `[data-feed="full"]` — `none`; `.feed` и
  `.owui-feed-list` берут `--feed-max`, `.composer` — `--composer-max`; правило сравнения вариантов (:1049) — под
  `:root:not([data-feed="full"])`; `.composer textarea` — `max-height: calc(50 * var(--dvh))`, `.field-folded
  textarea` — `6em`; `code-chat.js::autosize` — `innerHeight * .5`. `deviceLookBlock` — сегмент `appearance-feed`
  «Ширина ленты»: «Обычная», «Широкая», «Во всю ширину»; `theme.js` ставит `data-feed` рядом с плотностью (:11).
  Лента «Агентов» предела ширины не имеет — настройка её не меняет. `fieldBox(…, { fold: true })`: первой в
  `.field-tools` — `button.icon-btn.field-fold[aria-pressed]` (значки `collapse` / `expand`, «Свернуть поле» /
  «Развернуть поле»); видна, пока текст выше 6em или поле свёрнуто; нажатие переключает класс `.field-folded` рамки;
  пустое поле состояние сбрасывает. `speech.js`: `#sp-share` «Поделиться» в панели чата, видна при готовой записи и
  без идущих, зовёт `shareDialog(root, '/api/speeches/chats/<id>/share', название, api, { kind: 'speech_chats' })`.
  `owui-chat.js`: `shareMenu(root, { id: 'owui-share', content: scroller, failed })`, кнопка — в новой шапке ленты
  `div.owui-feed-head` рядом с названием, `sharing.set` — когда известны id и название чата. `chat-share.js::KINDS`
  — `speech_chats` и `owui` (заголовок и что увидит получатель); `share-view.js` — подпись «Только чтение · чат
  озвучки», записи по порядку с текстом, голосом, кнопками «Слушать» (blob с `/share/file` в `audio`) и «Скачать»;
  у сообщения Open WebUI — список имён файлов «остались в Open WebUI». `account-security.js`: имена разделов
  `speech`, `owui` и адрес «Открыть» — `#/speech?id=<item_id>`, `#/owui/<учётка>/<чат>`.
- [ ] **Step 10: Run** четыре новых файла и `-m integration tests/test_ui_owui.py tests/test_ui_speech_chats.py
  tests/test_ui_item_shares.py tests/test_ui_chat_shares.py tests/test_ui_shared_composer.py
  tests/test_ui_design_consistency.py tests/test_ui_agent_controls.py tests/test_ui_chat_carryover.py`; быстрые
  `tests/test_i18n.py tests/test_css_tokens.py tests/test_sections_meta.py tests/test_static.py` — PASS.
- [ ] **Step 11: Commit** `Add sound signals as a device setting`; `Quote selected text and add a message menu in
  chat, Agents and Open WebUI`; `Add feed width and a foldable composer field`; `Share speech chats and Open WebUI
  chats from their sections`.

## Волна E

### Task 17: Завершение: документация, архив идей, находки, полный прогон, сверка покрытия

Последняя задача: к ней слиты все шестнадцать. Сторожа документации — `tests/test_docs.py` (ссылки на символы,
карта модулей `test_module_map_lists_every_app_module`, строка отката
`test_rollback_table_covers_every_schema_step_from_16`, таблица capability `test_capability_table_names_every_hello_field`,
разделы настроек в руководстве `test_guide_shows_where_every_account_settings_section_is`) и
`tests/test_sections_meta.py::test_glossary_names_the_sections_like_the_registry`. Журналы `FINDINGS.md`,
`IDEAS.md`, `IDEAS-archive.md` лежат в корне проекта вне Git и в worktree их нет: правки журналов делает ведущий в
основном каталоге по шагу 7. В `FINDINGS.md` три записи от 2026-11-25: две закрывают задачи 7 и 9, третью — эта.
`/api/me` после задачи 5 ещё отдаёт `code_quick_actions`, `code_quick_actions_default`, `code_quick_actions_set`
(`app/account_profile.py:149–151`, приём — :75 и :213–214) ради страницы прежней выкладки; задача 11 перевела
страницу на `/api/skills`.

**Files:**
- Modify: `docs/guide.ru.md`, `docs/architecture/chat.md`, `agents-workspace.md`, `agent-bridge.md`, `shell.md`,
  `sharing-export.md`, `notifications.md`, `docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md`,
  `CLAUDE.md`, `agent/CLAUDE.md`, `docs/glossary.md`; `app/account_profile.py`; `tests/test_profile_admin.py`
  (:73–75, :163–191), `tests/test_code_actions.py`, `tests/test_ui_code_quick_actions.py:107`, `tests/test_ui_i18n.py:195`
- Delete: `app/static/js/code-quick-actions.js` — если после задач 5 и 11 его никто не импортирует
- Create: `tests/test_rollback_steps.py`
- Вне Git (ведущий): `IDEAS-archive.md`, `FINDINGS.md`

**Interfaces:**
- Consumes: всё слитое; `code_actions.for_account` и столбец `users.code_quick_actions` остаются — их читает посев
  навыков (задача 5) и прежний код при откате.
- Produces: `/api/me` без трёх полей быстрых действий; `MePatch` без `code_quick_actions`.

- [ ] **Step 1: Write the failing tests.** `tests/test_rollback_steps.py` (быстрый) — сторож анти-цели «прежний код
  не работает на новой базе»: прежний `compose` читает у шага `text` или `error` и пропускает только шаги с `note`:

```python
def test_every_stored_step_is_readable_by_the_previous_compose(studio):
    # один диалог с навыком «по частям» и связанной базой знаний: ответ с частью (внутри неё — web_search), с
    # автоматическим блоком знаний, с knowledge_search, с результатом MCP с картинкой и с заметкой TRIMMED
    for answer in assistant_rows(cid):
        for step in json.loads(answer["tools"] or "[]"):
            assert "note" in step or "error" in step or isinstance(step.get("text"), str), step["kind"]
    old = lambda s: f"Ошибка инструмента: {s['error']}" if "error" in s else s["text"]          # прежние for_model
    blocks = [old(s) for a in assistant_rows(cid) for s in json.loads(a["tools"] or "[]") if "note" not in s]
    assert all(isinstance(b, str) for b in blocks)
```

  `tests/test_profile_admin.py`: в перечне ключей `/api/me` трёх полей нет; `PATCH /api/me {"code_quick_actions":
  [{"title": "x", "text": "y"}]}` — 200, а `users.code_quick_actions` в базе не изменился; прежние проверки правки
  списка (:163–191) удалить — их поведение держат тесты навыков задачи 5. `tests/test_code_actions.py`:

```python
def test_page_no_longer_reads_quick_actions_from_the_profile():
    sources = "".join(p.read_text("utf-8") for p in (STATIC / "js").glob("*.js"))
    assert "code_quick_actions" not in sources and not (STATIC / "js" / "code-quick-actions.js").exists()
```

- [ ] **Step 2: Run** `… tests/test_rollback_steps.py tests/test_profile_admin.py tests/test_code_actions.py` —
  Expected: FAIL на полях `/api/me`; `test_rollback_steps` — PASS или отказ с видом шага: шагу без `text` дать
  `"text": ""` там, где он пишется (внутренние шаги части — `StreamTask._step` задачи 10), и новый код не должен
  отдавать такой шаг следующему ходу.
- [ ] **Step 3: Implement.** Поиск `git grep -n code_quick_actions -- app/static tests docs` — страница поля не
  читает (иначе остановиться и вернуть находку задаче 11). Из `account_profile.py` убрать три ключа ответа, поле
  `MePatch` и ветку записи; класс `CodeQuickAction`, выборку столбца и импорт — если больше не нужны. Браузерные
  тесты (`test_ui_code_quick_actions.py:107`, `test_ui_i18n.py:195`) читают `GET /api/skills?scope=agents`.
- [ ] **Step 4: Документация** (каждый файл — что дописать; источник слов — слитый код, не спека):
  - `docs/guide.ru.md`: новый раздел «Навыки» (библиотека, «/», чип «Навык: имя», «по частям», набор в папку
    проекта) и строка о странице «Настройки → Навыки»; в «Чат» — строка состояния и её кнопки, окно «Диалог и
    контекст», «Сжать сейчас», «Новый диалог с итогом», «Статистика», «Файлы диалога», «Продолжить иначе», очередь
    «После ответа», вкладки, закрепление и метки списка, окно HTML и SVG (и почему его нет в защищённом диалоге),
    источники «Знаний», уровень рассуждения, «Цитировать» и меню сообщения; в «Агенты» — навыки проекта, время и
    скорость хода, поиск по чату, «Копировать», «Проверить другим помощником», пределы 12 изображений и 12
    вложений (у моста старше 0.48.0 — 4 и 8) и **абзац о закреплении проектов**: звёздочка у проекта в списке,
    закреплённые стоят первыми, хранится у учётки; в «Озвучка» и «Open WebUI» — «Поделиться»; в «В работе и
    уведомления» — звук и напоминание о ждущей карточке чата; в «Оформление» — ширина ленты и «Свернуть поле».
  - `docs/architecture/`: свести разделы, добавленные задачами, убрать повторы и устаревшее (счёт токенов «знаки / 3»,
    «пересказ не хранится», имена `mcp_<id>_<n>`, быстрые действия в `agents-workspace.md:492–496`); в `agent-bridge.md`
    и протоколе — capability `skills`, `limits`, поля `turn_ms`, `session_ms`.
  - `CLAUDE.md`: карта модулей называет каждый новый модуль сервера и страницы; строка 82 таблицы отката сверена с
    кодом и дополнена тем, чего в ней нет: картинки результатов MCP — вложениями ответа (прежний код под ответом
    их не показывает и с согласием передаёт серверам MCP как файлы диалога), шаг автоматического блока знаний
    (прежний код отдаёт его следующему ходу пустым блоком), уровень рассуждения не уходит; версии в тексте — 0.55.0,
    82, мост 0.48.0. `agent/CLAUDE.md` — пределы `IMAGE_COUNT`, `ATTACHMENT_COUNT`, `SKILL_MAX`, `SKILLS_MAX` с
    модулем (значения сверяет `test_docs`). `docs/glossary.md` — «Навык» (и «Быстрое действие» — как навык с
    отметкой «кнопкой»).
- [ ] **Step 5: Сверка `static/en.js`.** `tests/test_i18n.py` и `tests/test_i18n_server.py` — PASS; затем глазами
  по списку новых ключей `git diff 13d063ef -- app/static/en.js`: нет русских слов в значениях, длинные подписи
  кнопок строки состояния, вкладок и меню «/» не длиннее русских больше чем вдвое (иначе — короче; сторож — тесты
  320 px на английском задач 3, 6, 14); `-m integration tests/test_ui_i18n.py` — PASS.
- [ ] **Step 6: Полный прогон.** `.venv/Scripts/python -m pytest -q` — PASS, не дольше 60 с, в списке
  `--durations` нет новых тестов дольше 1 с (такой — ускорить или пометить `integration`); в `agent/` его средой —
  `.venv/Scripts/python -m pytest -q` — PASS. Браузерные наборы затронутых разделов: `-m integration` для всех
  `tests/test_ui_*.py`, созданных или изменённых задачами 3–16 (список — `git diff --name-only 13d063ef --
  tests/test_ui_*.py`), и `tests/test_ui_webkit.py`, `tests/test_ui_design_consistency.py`, `tests/test_ui_browser.py`.
  Версии: `release.VERSION == "0.55.0"`, `db.VERSION == 82`, мост `0.48.0`; `import main` на Python 3.13.
- [ ] **Step 7: Журналы (ведущий, основной каталог).** В `IDEAS-archive.md` после строки `**Resolved:**` дописать
  строку `**Пересмотрено:** 2026-11-25 — …` у четырёх записей: «2026-11-20 · Чат и подключения: Заполнение
  контекста и «продолжить в новом диалоге с итогом»» — «решение изменено: «Новый диалог с итогом» выпущен в Studio
  0.55.0 (спека 2026-11-25, п. 27)»; «2026-11-16 · Передача задачи между Claude Code, Codex и OpenCode» — «взята
  часть: «Проверить другим помощником» (п. 11); полная передача задачи по-прежнему не делается»; «2026-11-16 ·
  Поиск по большим документам с проверяемыми источниками» — «взята вторая половина: источники «Знаний» под ответом
  (п. 28)»; «2026-10-21 · Личные коллекции, теги и избранное» — «взято закрепление диалога (п. 23); коллекции и теги
  остаются отложенными». Статусы и прежние строки не менять. В `FINDINGS.md` удалить запись «Руководство не
  описывает закрепление проектов в «Агентах»» и убедиться, что двух других записей от 2026-11-25 уже нет.
- [ ] **Step 8: Сверка покрытия.** По таблице «Пункты спеки по задачам» шапки плана пройти все 43 пункта: у
  каждого — слитый коммит и тест с именем (`git log --oneline 13d063ef..HEAD`, `git grep -n "def test_" -- tests`);
  пять тем Review Focus — тесты названных задач существуют и проходят. Пункт без теста или не сделанная часть — не
  дописывать молча: запись в `FINDINGS.md` и строка в итоговом отчёте ведущему («сделано / не сделано и почему»).
- [ ] **Step 9: Commit** `Drop quick actions from the profile API now that skills replace them`; `Document skills,
  the chat status bar and local model work for Studio 0.55.0` (в теле — заголовок закрытой находки: «Руководство не
  описывает закрепление проектов в «Агентах»»).
