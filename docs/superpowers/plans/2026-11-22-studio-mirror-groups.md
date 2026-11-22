# Зеркала проекта: группа равноправных копий — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** все копии проекта на компьютерах владельца — одна группа: каждая видит остальные, отправляет свои
изменения в одну копию или во все, кнопка «Зеркало» показывает число копий, открытые базы SQLite не валят сравнение.

**Architecture:** новый модуль данных `app/mirror_groups.py` (группы, копии, пары, перенос старых зеркал) и шаг
схемы 63; `app/project_mirrors.py` — маршруты и сверка поверх пар (правило 3 «источник уже держал это
содержимое»); мост — пропуск служебных файлов SQLite с причиной и `root` в `mirror_identity`
(`hello.mirror_groups`); страница — список копий, «Отправить во все копии», «+ Добавить зеркало», цифра на кнопке.

**Tech Stack:** Python 3.13 (FastAPI, SQLite) на сервере, Python 3.11+ мост, ES-модули без сборки.

**Spec:** `docs/superpowers/specs/2026-11-22-studio-mirror-groups-design.md`

## Global Constraints

- Схема — только добавление: `db.VERSION` 63 (если к началу работы шаг 63 уже занят другой работой — следующий свободный номер, везде ниже вместо 63), SQL — и в `db.STEPS`, и в `db.SCHEMA`; `project_mirrors` не удаляется и не меняется; строка шага — в таблице отката `CLAUDE.md`.
- Журнал сервера и моста — только id, коды и числа: ни имён, ни путей, ни содержимого (тесты зеркал проверяют `caplog`).
- Содержимое файлов — только через канал и память сервера; в базе — только шифром `STUDIO_DATA_KEY` (`security.encrypt`).
- Возможность моста — `hello.mirror_groups: true`; поле `reasons` сервер шлёт только мостам с ней.
- Причины пропуска — ровно `"sqlite"` (база с непустым журналом) и `"sqlite-journal"` (`-wal`, `-shm`, `-journal` базы).
- Версии: Studio `app/release.py::VERSION` = `0.45.0`; мост `agent/studio_agent/__init__.py::VERSION` = `0.45.0`.
- Подписи страницы — через `tr` с переводом в `app/static/en.js`; отказы сервера и моста — тоже в `en.js` (сторожат `tests/test_i18n.py`, `tests/test_i18n_server.py`); значки — только `icons.js`; `:hover` — только в `@media (hover: hover)`; 360 и 1280 px.
- Часы — `clock.NOW`; без реального времени и сети в быстрых тестах; тест дольше 1 с — `@pytest.mark.integration`.
- Тесты: сервер `.venv/Scripts/python -m pytest -q -n0 <файл>` (интеграционные — с `-m integration`); мост — из `agent/`: `.venv/Scripts/python -m pytest -q <файл>`; быстрые наборы обоих — один раз в конце (Task 6).
- `.py` — UTF-8 без BOM, LF. Коммит — через файл (`git commit -F`), в конце `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; в `main` параллельно работает другая сессия — `git add` только своих файлов, без `git add -A`; не пушить.

## Review Focus

1. Сменился `STUDIO_DATA_KEY`: нечитаемые `location` копии или исключения группы — список без 500, сравнение 409 `reenter`, «Сохранить исключения» перезаписывает — Task 4 (`test_group_sealed_with_a_former_key_lists_unlinks_and_rewrites_exclusions`).
2. Компьютер группы отозван (строка `agents` удалена): копии и пары уходят каскадом, у оставшейся копии 0 других, добавление снова работает в той же группе — Task 2 (`test_revoked_computer_leaves_a_single_copy_group_that_can_grow_again`).
3. Копию отвязали во время её передачи из другой вкладки (или компьютер отозван): запись пары после передачи не падает `IntegrityError` и не роняет задачу — Task 2 (`test_save_after_a_copy_is_gone_is_a_no_op`).
4. Проект переименован (новый `pid`), его прежняя копия в группе: поиск показывает папки группы «В группе копий с …», присоединение требует замены прежней копии этого компьютера — Task 4 (`test_renamed_project_rejoins_its_group_replacing_the_former_copy`).
5. Старый мост (без `mirror_groups`) на текущем компьютере при первом зеркале — 409 `unsupported` «Обновите мост…», а как назначение он по-прежнему сравнивается (без `reasons`) — Task 4 (`test_bridge_without_groups_is_refused_as_source_and_still_works_as_destination`).

---

### Task 1: Мост — пропуск SQLite с причиной, `root` в `mirror_identity`, `hello.mirror_groups`

**Files:**
- Modify: `agent/studio_agent/mirror.py` (`identity`, `destination`, `_entry`, `scan`; новая `_sqlite_skips`)
- Modify: `agent/studio_agent/agent.py:47-50` (поля), `:184-187` (hello), `:534` (`_field`), `:625-636` (`_op_mirror_identity`, `_op_mirror_scan`, `_op_mirror_destination`)
- Modify: `agent/studio_agent/__init__.py` (`VERSION = "0.45.0"`)
- Modify: `agent/tests/test_mirror.py`, `agent/tests/test_agent.py:294-296`
- Modify: `docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md` (новый §23.3), `docs/architecture/agent-bridge.md` (строка 0.45.0 в таблице возможностей)

**Interfaces:**
- Produces: `mirror.SQLITE = "sqlite"`, `mirror.SQLITE_JOURNAL = "sqlite-journal"`;
  `mirror.scan(root: Path, exclude=(), reasons: bool = False) -> dict`;
  `mirror.destination(config, root_id, name, *, metadata_only=False, exclude=(), reasons=False) -> dict`;
  `mirror.identity(config, project: Path) -> {"name", "repositories", "root"?, "folder"?}` — `root` (id папки проектов из `projects.list_roots`) и `folder` (её путь) есть, когда родитель проекта — одна из разрешённых папок;
  кадры: `mirror_scan` и `mirror_destination` принимают `reasons: bool`; `hello.mirror_groups: True`.

- [ ] **Step 1: Write the failing tests** (`agent/tests/test_mirror.py`; заменить `test_open_sqlite_is_not_copied_and_closed_database_is_included`)

```python
def test_open_sqlite_database_and_its_journals_are_skipped_with_reasons(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    conn = sqlite3.connect(project / "data.db")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE records(value)")
        conn.execute("INSERT INTO records VALUES(1)")
        conn.commit()
        plain = mirror.scan(project)["entries"]
        assert plain["data.db"] == plain["data.db-wal"] == plain["data.db-shm"] == mirror.SKIPPED
        named = mirror.scan(project, reasons=True)["entries"]
        assert named["data.db"] == ["skip", "sqlite", 0, 0]
        assert named["data.db-wal"] == named["data.db-shm"] == ["skip", "sqlite-journal", 0, 0]
    finally:
        conn.close()
    after = mirror.scan(project)["entries"]
    assert after["data.db"][0] == "file" and "data.db-wal" not in after


def test_empty_journal_does_not_hide_a_closed_database(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    with sqlite3.connect(project / "data.db") as conn:
        conn.execute("CREATE TABLE records(value)")
    conn.close()
    (project / "data.db-journal").write_bytes(b"")
    (project / "notes.log-wal").write_bytes(b"not a journal")   # рядом нет базы SQLite
    (project / "notes.log").write_bytes(b"plain text")
    entries = mirror.scan(project, reasons=True)["entries"]
    assert entries["data.db"][0] == "file"
    assert entries["data.db-journal"] == ["skip", "sqlite-journal", 0, 0]
    assert entries["notes.log-wal"][0] == "file"


def test_identity_names_the_projects_folder_of_the_project(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    (second / "app").mkdir(parents=True)
    first.mkdir()
    roots = [str(first), str(second)]
    found = mirror.identity(roots, second / "app")
    root = mirror.projects.list_roots(roots)[1]
    assert found["name"] == "app" and found["root"] == root["id"] and found["folder"] == root["path"]
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert "root" not in mirror.identity(roots, outside)
```

В `agent/tests/test_agent.py:294-296` к ожидаемому `hello` добавить `"mirror_groups": True`.

- [ ] **Step 2: Run tests to verify they fail**

Run (из `agent/`): `.venv/Scripts/python -m pytest -q tests/test_mirror.py tests/test_agent.py`
Expected: FAIL — `Refused: busy` в первом тесте, `TypeError` у `identity`, нет `mirror_groups` в hello.

- [ ] **Step 3: Implement `_sqlite_skips(folder: Path, names: set[str]) -> dict[str, str]`** и переделать `scan`

Отказ `busy` «обнаружена открытая база SQLite» из `_entry` убрать. `scan` читает список папки целиком
(`list(os.scandir(...))`), считает пропуски SQLite по именам и для имени из результата ставит пропуск
(с причиной при `reasons`, иначе `SKIPPED`) без вызова `_entry`. Исключения (`exclude`) и недопустимые имена —
как сейчас, раньше SQLite.

```python
def _sqlite_skips(folder, names):
    """Служебные файлы базы SQLite — всегда пропуск; сама база — если её `-wal` или `-journal` непуст."""
    skips = {}
    for name in names:
        for suffix in ("-wal", "-shm", "-journal"):
            base = name[:-len(suffix)]
            if name.endswith(suffix) and base and base in names and _is_sqlite(folder / base):
                skips[name] = SQLITE_JOURNAL
                if suffix != "-shm" and _size(folder / name) > 0:
                    skips[base] = SQLITE
    return skips
```

`_is_sqlite(path) -> bool` — обычный файл (не reparse) с первыми 16 байтами `b"SQLite format 3\x00"`, `OSError` → `False`;
`_size(path) -> int` — `lstat().st_size` обычного файла, иначе 0. `destination(..., reasons)` передаёт `reasons` в
`scan`. `Transfer.apply` вызывает `scan(dest, exclude)` без причин — сервер сверяет `expected` по манифесту, где
пропуски приведены к `SKIPPED` (Task 3).

- [ ] **Step 4: Implement `identity(config, project)`, поля и hello в `agent.py`**

`identity` ищет корень, чей `path` равен `os.path.realpath(project.parent)`, среди `projects.list_roots(config)`.
`_op_mirror_identity` передаёт `self.cfg`; `_op_mirror_scan`/`_op_mirror_destination` — `reasons=frame.get("reasons") is True`;
в `FIELDS` добавить `"reasons"` к `mirror_scan` и `mirror_destination`; в `_field` — `"reasons"` в списке
булевых (`value is None or type(value) is bool`); в hello — `"mirror_groups": True`. `VERSION = "0.45.0"`.

- [ ] **Step 5: Run tests to verify they pass**

Run (из `agent/`): `.venv/Scripts/python -m pytest -q tests/test_mirror.py tests/test_agent.py`
Expected: PASS.

- [ ] **Step 6: Протокол и таблица возможностей**

§23.3 «Группы копий (мост 0.45.0)»: `hello.mirror_groups`; `mirror_identity` возвращает ещё `root` и `folder`;
`mirror_scan`/`mirror_destination` с `reasons: true` пишут пропуск SQLite как `["skip", "sqlite"|"sqlite-journal", 0, 0]`,
без флага — прежний `skip`; база пропускается при непустом `-wal`/`-journal`, служебные файлы — всегда; отказа
«открытая база SQLite» больше нет. Строка `| 0.45.0 | `hello.mirror_groups` — группы копий: `root`/`folder` в `mirror_identity`, причины пропуска `reasons`; база SQLite с журналом — пропуск, а не отказ | §23.3 |` — в начало таблицы `agent-bridge.md`.

- [ ] **Step 7: Commit**

```bash
git add agent/studio_agent/mirror.py agent/studio_agent/agent.py agent/studio_agent/__init__.py agent/tests/test_mirror.py agent/tests/test_agent.py docs/superpowers/specs/2026-10-28-studio-code-bridge-protocol.md docs/architecture/agent-bridge.md
git commit -F <файл>   # "Skip SQLite journals in mirror scans and name the project root"
```

---

### Task 2: Шаг схемы 63 и модуль `mirror_groups`

**Files:**
- Modify: `app/db.py` (`MIRROR_GROUPS_63`, `SCHEMA +=`, `STEPS[63]`, `VERSION = 63`)
- Create: `app/mirror_groups.py`
- Modify: `app/main.py:110-111` (`mirror_groups.adopt()` после `db.migrate`)
- Modify: `app/manage.py:73-74` (пункт `mirrors` проверки копии)
- Modify: `CLAUDE.md` (строка 63 таблицы отката; в карте модулей «Агенты» — `mirror_groups` (группы копий зеркал))
- Modify: `app/project_mirrors.py:30` (`EXCLUDE_MAX` — из `mirror_groups`), `docs/architecture/agents-workspace.md:73` (`mirror_groups.EXCLUDE_MAX` (64) вместо `project_mirrors.EXCLUDE_MAX` — иначе `tests/test_docs.py` падает)
- Create: `tests/test_mirror_groups.py`
- Modify: `tests/test_foundation.py` (тест шага 63; `== 62` в тестах шагов 61 и 62 → `== db.VERSION`), `tests/test_backup_check.py:513-517`

**Interfaces:**
- Produces (все синхронные, `state.DB`, без собственных транзакций, кроме `adopt`; вызывающий оборачивает записи в `transaction()`):
  - `EXCLUDE_MAX = 64` (переезжает сюда; `project_mirrors` берёт отсюда)
  - `transaction()` — contextmanager: `BEGIN IMMEDIATE` … `COMMIT`, `ROLLBACK` при исключении
  - `adopt() -> int` — число перенесённых строк `project_mirrors`
  - `find(aid: int, pid: str) -> sqlite3.Row | None`; `get(cid: int) -> Row | None`
  - `members(grp: int) -> list[Row]` — копии с `computer` (`agents.name`), по `id`
  - `unlocated(aid: int) -> list[Row]` — копии компьютера с `project IS NULL` или `root IS NULL`
  - `location(row) -> dict | None` — `{name, folder}`; `None` при `root IS NULL`; `InvalidToken` пробрасывается
  - `name_key(name: str) -> str` = `security.digest(name.casefold())`
  - `create_group(user_id: int) -> int`; `add(grp, agent, project, root, name, folder) -> int`
  - `place(cid, *, project=None, root=None, name=None, folder=None) -> int` — дописать известное; та же папка уже есть другой строкой — слить, вернуть id оставшейся
  - `clashes(g1: int, g2: int) -> list[str]` — имена компьютеров, у которых копии в обеих группах
  - `merge(into: int, other: int) -> None` — копии `other` в `into`, исключения объединить, `other` удалить
  - `remove(cid: int, *, prune: bool = True) -> None` — удалить копию; при `prune` группа с < 2 копиями удаляется
  - `exclusions(grp: int) -> list[str]` (`InvalidToken` пробрасывается); `set_exclusions(grp: int, paths: list[str]) -> None`
  - `Pair = NamedTuple(state_x: dict | None, state_y: dict | None, synced: float | None, mark: str | None)`
  - `pair(x: int, y: int) -> Pair` — стороны с точки зрения `x`; нечитаемое состояние — `None`; `mark` — sha256 hex зашифрованного состояния `y`
  - `synced(x: int, y: int) -> float | None`
  - `held(x: int) -> dict[str, set[tuple]]` — по пути: `tuple(entry[:3])` всех записей, кроме `skip`, из состояний стороны `x` во всех её парах
  - `save(x, y, state_x: dict | None, state_y: dict | None, synced: float | None) -> None` — upsert; нет копии `x` или `y` — ничего
  - `seal(x, y, state_x: dict, state_y: dict) -> None` — записать, только если состояния `y` в паре ещё нет

- [ ] **Step 1: Write the failing tests** (`tests/test_mirror_groups.py`, фикстура `studio`; копии и агенты — прямыми вставками через `make_user` и `enroll` из `fake_bridge`)

```python
def test_adopt_groups_mirrors_by_source_and_carries_baseline_and_exclusions(studio): ...
    # две строки project_mirrors A:p→B и A:p→C (location с exclude [".venv"] и ["node_modules"], baseline у B)
    # adopt() == 2; повторный adopt() == 0
    # одна группа из трёх копий; копия A: project="p", root None; копии B, C: project None, root/name_key как в строках
    # exclusions(grp) == [".venv", "node_modules"]; location(B) == {"name": ..., "folder": ...} без "exclude"
    # pair(A, B).state_y == прежний baseline, pair(A, B).state_x is None, synced как в строке
    # строка, добавленная после adopt (как прежним кодом после отката), переносится следующим adopt()

def test_place_merges_two_rows_of_one_folder_and_their_groups(studio): ...
    # группа G1 {A:p (root None), B:f}, группа G2 {B:q (root None), A:g} — обратное зеркало
    # place(A:p, root=g.root, name=g.name, folder=...) == id строки A:g; у A одна строка; группа одна: {A, B:f, B:q}
    # place(B:f, project="q") → у B одна строка; members == [A, B]; пара A–B — та, у которой synced позже

def test_clashes_block_merging_groups_with_copies_on_one_computer(studio): ...
    # G1 {A, B}, G2 {B', C} (B и B' — разные папки на B) → clashes(G1, G2) == ["B"]

def test_held_returns_every_entry_the_copy_kept_in_any_pair(studio): ...
    # save(A, B, {"f": v1}, ...), save(A, C, {"f": v2, "d": SKIP}, ...) → held(A) == {"f": {tuple(v1[:3]), tuple(v2[:3])}}

def test_revoked_computer_leaves_a_single_copy_group_that_can_grow_again(studio): ...
    # DELETE FROM agents WHERE id = B → копия и пары B ушли; members(G) == [A]; add(G, C, ...) работает

def test_save_after_a_copy_is_gone_is_a_no_op(studio): ...
    # remove(B); save(A, B, {...}, {...}, 1.0) не бросает и пару не создаёт

def test_removing_the_last_other_copy_drops_the_group_and_its_exclusions(studio): ...
```

В `tests/test_foundation.py` — `test_step_63_adds_mirror_groups_and_previous_writer_works(env)`: `install_schema(conn, 62)`,
строка `project_mirrors`, `db.migrate` дважды, `user_version == db.VERSION == 63`, четыре новые таблицы есть, вставка
в `project_mirrors` прежним кодом работает, удаление учётки каскадом очищает `mirror_groups`, `PRAGMA foreign_key_check` пуст.
В `tests/test_backup_check.py` — второй случай категории `mirrors`: `INSERT INTO mirror_groups(user_id, exclude) VALUES(1, ?)`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest -q -n0 tests/test_mirror_groups.py tests/test_foundation.py tests/test_backup_check.py`
Expected: FAIL — нет модуля `mirror_groups`, `db.VERSION == 62`.

- [ ] **Step 3: Implement `MIRROR_GROUPS_63`**

```sql
CREATE TABLE mirror_groups(id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users ON DELETE CASCADE, exclude BLOB);
CREATE TABLE mirror_copies(id INTEGER PRIMARY KEY AUTOINCREMENT,
    grp INTEGER NOT NULL REFERENCES mirror_groups ON DELETE CASCADE,
    agent INTEGER NOT NULL REFERENCES agents ON DELETE CASCADE,
    project TEXT, root TEXT, name_key TEXT, location BLOB, UNIQUE(agent, root, name_key));
CREATE INDEX mirror_copies_project ON mirror_copies(agent, project);
CREATE INDEX mirror_copies_grp ON mirror_copies(grp);
CREATE TABLE mirror_pairs(a INTEGER NOT NULL REFERENCES mirror_copies ON DELETE CASCADE,
    b INTEGER NOT NULL REFERENCES mirror_copies ON DELETE CASCADE,
    state_a BLOB, state_b BLOB, synced REAL, PRIMARY KEY(a, b), CHECK(a < b));
CREATE INDEX mirror_pairs_b ON mirror_pairs(b);
CREATE TABLE mirror_legacy(id INTEGER PRIMARY KEY);
```

Комментарий над константой — по образцу соседних: что добавлено, что делает прежний код.

- [ ] **Step 4: Implement `mirror_groups.py`**

`adopt()` — одной транзакцией по строкам `project_mirrors`, которых нет в `mirror_legacy`, по `id`:
1. копия источника — `find(source, project)`, нет — новая группа (`user_id` компьютера-источника) и `add(grp, source, project, None, None, None)`;
2. `location` строки расшифровать, `exclude` вынуть в исключения группы (объединение по порядку, не больше `EXCLUDE_MAX`), без него зашифровать заново; `InvalidToken` — взять блоб как есть, исключений нет;
3. копия назначения — существующая по `(destination, root, name_key)` (в другой группе — `merge` в группу источника без проверки `clashes`) или новая с `project NULL`;
4. пара источник–назначение: состояние назначения — блоб `baseline` как есть, источника — `NULL`, `synced` строки (`INSERT OR IGNORE`, с учётом `a < b`);
5. `INSERT INTO mirror_legacy(id)`. В журнал — только число перенесённых строк.

`place` и слияние строк одной папки (внутренний `_merge_copies(keep, drop)`):
- дубликат — другая строка того же `agent` с тем же `project` (если он задан) или тем же `root`+`name_key`;
  оставляется строка с известным `root` (при обеих — дубликат), вторая удаляется;
- группы разные — `merge(keep.grp, drop.grp)`;
- каждая пара `drop` с третьей копией `X` переходит к `keep` (с переворотом сторон при смене порядка `a < b`), если у `keep` пары с `X` нет или она старее по `synced`; пара `keep`–`drop` пропадает;
- `DELETE` строки `drop`, затем недостающие поля `keep` дописываются (`project`; `root`, `name_key`, `location` — вместе).

`merge`: исключения — объединение (нечитаемое — остаётся читаемое), `UPDATE mirror_copies SET grp`, группа `other` удаляется.
Блобы состояний пар при переносе не расшифровываются.

- [ ] **Step 5: Lifespan, проверка копии, документы**

`main.lifespan`: `mirror_groups.adopt()` сразу после `access.forward()`-строк (после `db.migrate`), с комментарием
«строки `project_mirrors` (прежний код, откат) — в группы». `manage.ENCRYPTED_BACKUP_FIELDS` `mirrors`: к прежнему
SQL добавить `location` из `mirror_copies`, `exclude` из `mirror_groups`, `state_a`/`state_b` из `mirror_pairs`
(все `IS NOT NULL`). Строка в таблицу отката `CLAUDE.md`:

`| 63 | `mirror_groups`, `mirror_copies`, `mirror_pairs`, `mirror_legacy` — зеркала группой копий; строки `project_mirrors` переносит `mirror_groups.adopt` при старте | новые таблицы не читает: видит односторонние зеркала `project_mirrors` на момент обновления с прежними базами сравнения; копий, групп и синхронизаций нового кода не видит; после возврата вперёд его синхронизации не учтены (больше «локальных изменений»), а его новые зеркала `adopt` перенесёт | Не нужен |`

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest -q -n0 tests/test_mirror_groups.py tests/test_foundation.py tests/test_backup_check.py tests/test_docs.py`
Expected: PASS.

- [ ] **Step 7: Commit** — `app/db.py app/mirror_groups.py app/main.py app/manage.py CLAUDE.md tests/test_mirror_groups.py tests/test_foundation.py tests/test_backup_check.py`, сообщение «Store project mirrors as groups of copies with pair states».

---

### Task 3: Сверка — правило 3, состояние источника, причины пропуска

**Files:**
- Modify: `app/project_mirrors.py` (`_entries`, `_plan`, `_next_baseline`; новая `_source_state`; `SKIP_REASONS`)
- Modify: `tests/test_project_mirrors.py` (чистые тесты рядом с `test_skipped_subtree_preserves_files_at_destination`)

**Interfaces:**
- Consumes: формат пропуска моста из Task 1.
- Produces:
  - `SKIP_REASONS = ("sqlite", "sqlite-journal")`
  - `_entries(data, *, missing=False, reasons: dict | None = None) -> dict | None` — принимает `["skip", r, 0, 0]` с `r` из `SKIP_REASONS`, в результат кладёт `SKIP`, причину — в `reasons[path]`; иная причина — 502 `bridge`
  - `_plan(source, destination, baseline, *, windows=False, held=None)` — прежний результат; `held` — из `mirror_groups.held`
  - `_next_baseline(...) -> dict` — прежняя логика без шифрования (шифрует `mirror_groups.save`)
  - `_source_state(raw: dict, previous: dict | None, pending: set[str]) -> dict`

- [ ] **Step 1: Write the failing tests**

```python
F1, F2, F3 = (["file", c * 64, 3, 0] for c in "123")

def test_plan_accepts_a_copy_entry_the_source_held_before():
    from project_mirrors import _plan
    args = ({"f": F3}, {"f": F2}, {"f": F1})
    assert _plan(*args)[3] == 1
    assert _plan(*args, held={"f": {tuple(F2[:3])}})[3] == 0

def test_plan_flags_a_newer_copy_entry_the_source_never_held():
    from project_mirrors import _plan
    assert _plan({"f": F1}, {"f": F3}, {"f": F1}, held={"f": {tuple(F1[:3]), tuple(F2[:3])}})[3] == 1

def test_file_removed_in_the_copy_is_checked_only_against_the_pair_state():
    from project_mirrors import _plan
    assert _plan({"f": F2}, {}, {"f": F1}, held={"f": {tuple(F1[:3])}})[3] == 1

def test_source_state_keeps_the_previous_entry_for_paths_not_applied():
    from project_mirrors import _source_state
    raw = {"a": F2, "b": F2, "c": F3}
    assert _source_state(raw, {"b": F1}, {"b", "c"}) == {"a": F2, "b": F1}

def test_skip_reasons_are_recorded_and_normalized():
    from project_mirrors import SKIP, _entries
    reasons = {}
    entries = _entries({"entries": {"x.db": ["skip", "sqlite", 0, 0], "x.db-wal": ["skip", "sqlite-journal", 0, 0]}},
                       reasons=reasons)
    assert entries == {"x.db": SKIP, "x.db-wal": SKIP}
    assert reasons == {"x.db": "sqlite", "x.db-wal": "sqlite-journal"}
    with pytest.raises(HTTPException):
        _entries({"entries": {"x": ["skip", "other", 0, 0]}})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest -q -n0 tests/test_project_mirrors.py -k "held or never_held or removed_in_the_copy or source_state or skip_reasons"`
Expected: FAIL — `_plan` не принимает `held`, нет `_source_state`, `_entries` отвергает причину.

- [ ] **Step 3: Implement**

Конфликт в `_plan` (правила 2 и 3 спеки):

```python
conflict = old != previous.get(path) and not (
    old is not None and old[0] != "skip" and tuple(old[:3]) in (held or {}).get(path, ()))
```

`_source_state`: записи `raw`, кроме путей из `pending`; для путей из `pending` — запись `previous`, если она есть.

- [ ] **Step 4: Run tests to verify they pass** — та же команда, PASS; затем весь файл без `integration`: `.venv/Scripts/python -m pytest -q -n0 tests/test_project_mirrors.py` (маршрутные тесты пока на прежней модели — проходят, кроме тех, что зависят от `_next_baseline` в байтах: поправить вызов в `compare`/`_sync` на `security.encrypt(_json(...))` до Task 4).

- [ ] **Step 5: Commit** — `app/project_mirrors.py tests/test_project_mirrors.py`, «Let a mirror source overwrite what it held before and name skipped SQLite files».

---

### Task 4: Маршруты зеркал на группах

**Files:**
- Modify: `app/project_mirrors.py` (маршруты, `_compare`, `_sync`, `_candidates`, `discover`, `RESULTS`)
- Modify: `app/bridge.py:235` (`"mirror_groups"` в список запоминаемых возможностей `hello`)
- Modify: `app/static/en.js` (новые отказы сервера)
- Modify: `tests/test_project_mirrors.py` (`MirrorMachine` и прежние сценарии), `tests/test_code.py:42-50`

**Interfaces:**
- Consumes: `mirror_groups.*` (Task 2), `_plan(held=)`, `_source_state`, `_entries(reasons=)`, `SKIP_REASONS` (Task 3), `hello.mirror_groups` и `reasons` (Task 1).
- Produces (префикс `/api/code/agents/{aid}/projects/{pid}/mirrors`; `{aid, pid}` — текущая копия, источник отправки):
  - `GET ""` и `POST "/locate"` → `{"copy": int | None, "items": [Item], "exclude": [str], "pending": bool}` (+ `"exclude_reenter": true` при нечитаемых исключениях); `locate` сначала дописывает `pid`/`root` копий этого компьютера (`unlocated`) через мост
  - `Item = {"id", "destination" (id компьютера), "computer", "online", "name", "folder", "synced" (пара с текущей копией), "job", "reenter"?: true, "unknown"?: true}`
  - `POST ""` тело `Destination` + `join: bool = False` (`replace` — заменить прежнюю копию этого компьютера) → 201 `Item` добавленной копии
  - `POST "/discover"` — кандидаты без копий своей группы; копия чужой группы — с `"grouped": [имена компьютеров]` вместо прежнего `elsewhere`; компьютеры, уже имеющие копию в группе, не опрашиваются
  - `PUT "/exclude"` (без `{mid}`) → как `GET`
  - `GET "/{cid}"`, `POST "/{cid}/compare"` (+ `"reasons": {путь: причина}` в ответе), `POST "/{cid}/diff"`, `POST "/{cid}/sync"`, `DELETE "/{cid}"` — `cid` — другая копия группы
  - `RESULTS` — ключ `(id текущей копии, id другой копии)`; `TRANSFERS` — по компьютеру назначения, как сейчас

- [ ] **Step 1: `MirrorMachine` — любой проект корней как источник**

`_project_path(pid)` = `mirror.projects.find_project(self.roots, pid)`; `_op_mirror_identity` → `mirror.identity(self.roots, path)`;
`_op_mirror_scan`/`_op_read`/`_op_mirror_read` — по `_project_path(req["project"])` вместо `self.source`;
`reasons=req.get("reasons") is True` в `scan`/`destination`; в `hello` — `"mirror_groups": self.groups` (по умолчанию `True`).
Фикстура `computers` не меняется (её берёт и `tests/test_ui_project_mirrors.py`); новая `trio(computers, tmp_path)` —
`(source, first, second, third)`, `third = MirrorMachine([tmp_path / "third"])`. `pid` копии на второй машине —
`mirror.projects.project_id(second.roots, Path(root["path"]), name)`.

- [ ] **Step 2: Write the failing tests**

```python
@pytest.mark.integration  # три компьютера, несколько полных передач
def test_group_is_seen_from_every_copy_and_sends_from_any_of_them(studio, trio): ...
    # A добавляет B и C; GET на A: [B, C]; GET на B (его pid): copy задан, items — [A, C]
    # A → B и A → C («Скопировать весь проект»); правка notes.txt на B; с B: compare в A и в C — conflicts == 0
    # (правило 3: в C версия A, B держал её в паре A–B); отправка в обе — содержимое совпало
    # B снова правит notes.txt и отправляет только в C; с A compare в C — conflicts == 1 (A этой версии не держал)
    # caplog без имён и содержимого файлов

def test_joining_a_folder_of_another_group_asks_first_and_merges(studio, trio): ...
    # группа {A, B}; проект на C без группы, папка B — 409 grouped, computers == ["Source"];
    # join → C в группе, GET на A — [B, C]; две группы {A, B} и {C, D-папка на B} — join даёт 409 conflict с ["Destination"]

def test_adopted_mirror_is_located_and_listed_from_both_computers(studio, computers): ...
    # строка project_mirrors с exclude, mirror_groups.adopt(); GET на A: [B] с exclude
    # GET на B: copy None, pending True; POST locate на B: copy задан, items [A]

def test_open_sqlite_database_is_skipped_with_a_reason_in_the_comparison(studio, computers): ...
    # в источнике открытая WAL-база (wal_autocheckpoint=0); compare — 200, reasons["data.db"] == "sqlite",
    # "data.db-wal" == "sqlite-journal"; без mirror_groups у назначения — запросы ему без поля reasons

def test_renamed_project_rejoins_its_group_replacing_the_former_copy(studio, computers, monkeypatch): ...
    # бывший test_mirror_of_a_renamed_project_is_offered_and_relinked_to_the_new_project:
    # поиск с нового pid — папка B с grouped == ["Source"]; join — 409 exists с former: true;
    # join + replace — 201; прежний pid: copy None; новый pid: items == [B]

def test_bridge_without_groups_is_refused_as_source_and_still_works_as_destination(studio, computers): ...
    # first.groups = False: первое зеркало с A — 409 unsupported; first.groups = True, second.groups = False:
    # добавление и compare работают, в кадрах second нет "reasons"

def test_group_sealed_with_a_former_key_lists_unlinks_and_rewrites_exclusions(studio, computers): ...
    # бывший test_mirror_settings_sealed_with_a_former_key_can_still_be_listed_and_unlinked + исключения группы
    # другим ключом: GET 200 (reenter у копии, exclude_reenter), compare 409 reenter, PUT exclude перезаписывает
```

Прежние сценарии перевести на новые формы: адрес исключений `url + "/exclude"`; `client.get(url).json()["items"]`;
`project_mirrors.RESULTS[mid]` → `client.get(f"{url}/{mid}").json()["job"]`; нечитаемый ключ — `UPDATE mirror_copies SET location`;
в поиске — `grouped` вместо `elsewhere`. `tests/test_code.py:49` — `("PUT", ".../mirrors/exclude")`, добавить `("POST", ".../mirrors/locate")`.

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest -q -n0 -m "integration or not integration" tests/test_project_mirrors.py tests/test_code.py`
Expected: FAIL — маршруты работают с `project_mirrors`.

- [ ] **Step 4: Implement списки, `locate`, отвязку и исключения**

`_listing(request, aid, pid)`, `_item(current, row)` по `Interfaces`; `synced` — `mirror_groups.synced`. `locate`: для
каждой строки `unlocated(aid)` при компьютере на связи: `root IS NULL` — `mirror_identity(project, passive=True)` (только
при `mirror_groups`, иначе пропустить) → `place(root=, name=, folder=)`; `project IS NULL` — `mirror_destination(root, name,
metadata_only=_can_discover(aid), passive=True)` → `place(project=)`; отказ моста по одной копии не прерывает остальные.
`DELETE "/{cid}"`: занятость `TRANSFERS` как сейчас, `remove(cid)`, убрать ключи `RESULTS` с этим `cid`.
`PUT "/exclude"`: проверки как сейчас; `mirror_exclude` — у каждого компьютера группы при непустом списке; занятость — любой
компьютер группы в `TRANSFERS`; `set_exclusions(grp, …)`.

- [ ] **Step 5: Implement добавление** (сетевые вызовы — до транзакции, решения — внутри `transaction()` с повторным чтением)

Нет текущей копии или у неё `root IS NULL` — `code.require_capability(aid, "mirror_groups")` и `mirror_identity` (`root`,
`name`, `folder`; иначе 502 `bridge`). Назначение — `mirror_destination` как сейчас. Затем (H — копия выбранной папки, C — текущая):

| Условие | Действие |
|---|---|
| H в группе C | 409 `exists` «эта папка уже зеркало этого проекта» |
| H нет, у компьютера назначения уже есть копия в группе C | 409 `exists` «на этом компьютере уже есть копия проекта» |
| H нет | C нет — новая группа и C; H — новая копия в группе C |
| H в другой группе, нет `join` | 409 `grouped` «папка уже входит в группу копий», `computers` — компьютеры группы H без компьютера H |
| H в другой группе, C нет, в группе H есть копия этого компьютера | без `replace` — 409 `exists`, `former: true`, «в группе уже есть прежняя копия этого компьютера»; с `replace` — `remove(прежняя, prune=False)`, C добавляется в группу H |
| H в другой группе, C нет | C добавляется в группу H |
| H в другой группе, C есть | `clashes` непуст — 409 `conflict` «в обеих группах есть копии на одном компьютере — отвяжите лишнюю», `computers`; иначе `merge(H.grp, C.grp)` |

`pid` H обновляется ответом моста (`place(H, project=)`). Ответ — `Item` добавленной (или присоединённой) копии.

- [ ] **Step 6: Implement сверку и отправку на паре**

`_pair(request, aid, pid, cid) -> (current, other)`: текущая по `find`, иначе 404; `other` — той же группы и не текущая,
иначе 404; `project_access.owner` на компьютер `other`. `_compare` (возвращает `NamedTuple` `_Comparison(result, to, raw,
entries, current, updates, remove, pair, exclude)`): у `other` нет `root` — сначала `locate` его копии (иначе 409
`unsupported` «Обновите мост на компьютере копии»); `reasons=True` — только компьютерам с `mirror_groups`; `pair` и
`held(current.id)` читаются до `to_thread`; `entries = _portable(dict(raw), …)`; токен —
`_digest([current.id, other.id, pair.synced, marker, entries, current_entries])`, `marker` — `pair.mark`, если у пары есть
состояние назначения и есть изменения; `pid` назначения из ответа — `place(other, project=)`. `compare`: состояния назначения
нет, копия есть, изменений нет — `seal(current, other, raw, _next_baseline(entries, current_entries, None, {}, []))`.
`_sync`: как сейчас; после применения `pending` = пути `changes` вне `updates ∪ remove` (после `_selection`);
`save(current.id, other.id, _source_state(raw, pair.state_x, pending), _next_baseline(...), clock.NOW())`.

`discover`: компьютеры с копией в группе текущей — не опрашиваются; `_candidates` получает карту
`(agent, root, name_key) → grp` и имена компьютеров по группам; своя группа — пропуск, чужая — `grouped`.

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest -q -n0 -m "integration or not integration" tests/test_project_mirrors.py tests/test_mirror_groups.py tests/test_code.py tests/test_i18n_server.py`
Expected: PASS; `--durations` не показывает новых тестов дольше 1 с без `integration`.

- [ ] **Step 8: Commit** — `app/project_mirrors.py app/bridge.py app/static/en.js tests/test_project_mirrors.py tests/test_code.py`, «Serve mirrors as a group of copies sending from any of them».

---

### Task 5: Окно «Зеркало» и цифра на кнопке

**Files:**
- Modify: `app/static/js/project-mirrors.js` (перестройка окна; цифра)
- Modify: `app/static/js/code.js:456`, `:544` (кнопка с цифрой; `projectMirrors` возвращает и `refresh`)
- Modify: `app/static/css/app.css:2031` (`.code-mirror-count` в общий селектор счётчиков; `#code-project-mirror { position: relative; }`)
- Modify: `app/static/en.js`
- Modify: `tests/test_ui_project_mirrors.py`

**Interfaces:**
- Consumes: маршруты Task 4.
- Produces: `projectMirrors(host, trigger, aid, project) -> { unmount(), refresh() }`; в кнопке — `span.code-mirror-count[aria-hidden=true]` (скрыт при 0); у кнопки `aria-label` — `tr("Зеркало · копий: {0}", n)` при `n > 0`.
  Узлы окна для тестов: `#code-mirror-copies` (`li[data-copy="<id>"]`), `#code-mirror-send-all`, `#code-mirror-add`,
  `#code-mirror-add-panel` (поиск `#code-mirror-discovery` и `.project-mirror-form`), `.project-mirror-exclude`, `.project-mirror-state`.

- [ ] **Step 1: Write the failing tests** (`tests/test_ui_project_mirrors.py`, `integration`)

- `test_mirror_dialog_selects_destination_and_shows_diff` — без группы окно сразу в режиме добавления; после
  «Добавить зеркало» — строка копии в `#code-mirror-copies` и её сравнение; кнопка отправки — «Скопировать весь проект»;
  исключения — в `.project-mirror-exclude` группы; сводка пропусков — «Пропущено исключений, ссылок, специальных объектов,
  баз SQLite, недопустимых имён и связанных папок: 1»; после закрытия окна `#code-project-mirror .code-mirror-count` — «1».
- `test_mirror_group_is_seen_from_the_copy_and_sends_to_all` — группа {A, B, C} создана через API, копия B — в
  `second.roots[0]` и в списке проектов машины; страница проекта на B: цифра «2»; «Отправить во все копии» после правки на B:
  одно подтверждение (`window.confirm` подменён `eval`-шагом, возвращает `true`), обе копии получили правку; копия со своей
  правкой (C) в отправку не вошла — в её строке «есть изменения, которых нет здесь — откройте сравнение».
- Прежние сценарии поиска и перепривязки — на «Присоединить» и подписи «В группе копий с {0}».

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest -q -n0 -m integration tests/test_ui_project_mirrors.py`
Expected: FAIL (или SKIP без браузера на машине разработки — тогда проверка на машине с браузером обязательна до выкладки).

- [ ] **Step 3: Implement окно**

Состав сверху вниз: список копий (строка — `компьютер · папка\имя`, пометка, «Сравнить»), действия «Отправить во все копии»
и «+ Добавить зеркало», панель добавления (поиск копий запускается только при её открытии; без группы — открыта сразу),
исключения группы (видны, когда группа есть), вид выбранной копии (нынешние `paint`/`difference`/`exclusions` без выпадающего
списка зеркал). Пометки строки: «не на связи», «синхронизирована с этим компьютером · {0}», «ещё не синхронизировалась»,
«адрес копии уточнится, когда компьютер будет на связи» (`unknown`), «настройка недоступна» (`reenter`), «Передача файлов · {0}%».
При открытии окна — `POST …/locate` (вместо `GET`). Переименования: «Синхронизировать изменения» → «Отправить изменения»,
«Отвязать зеркало» → «Отвязать копию», после отвязки — «Копия отвязана. Файлы остались на компьютере.»; пропуск с причиной —
«Пропущен · открытая база SQLite» (`sqlite`) и «Пропущен · служебный файл SQLite» (`sqlite-journal`).
Поиск: копия с `grouped` — «В группе копий с {0}» и кнопка «Присоединить» (у проекта нет группы) или «Объединить»
(`join: true`); 409 `grouped` из формы — `confirm(tr("Папка уже в группе копий с компьютерами: {0}. Присоединить этот проект к группе?", …))`;
409 `former` — `confirm(tr("В группе уже есть прежняя копия этого компьютера — например, проект под прежним именем. Заменить её этим проектом? Файлы прежней папки останутся на компьютере."))` → `replace: true`.

«Отправить во все копии» (по одной копии, последовательно):
1. пропустить `!online`, `reenter`, `unknown`, идущую передачу — с пометкой;
2. `POST /api/code/agents/{id}/open {pin: null}`; `pin_needed` — пометка «нужен PIN компьютера», пропуск;
3. `compare`; `conflicts > 0` — пометка «есть изменения, которых нет здесь — откройте сравнение», пропуск; совпадает — «совпадает»;
4. остальные — в одно подтверждение: `tr("Отправить изменения в копии?\n{0}", строки)`, строка — `tr("{0} — изменений: {1}, удалений: {2}", компьютер, …)`;
5. `sync` каждой (`overwrite: false`, `keep: []`), прогресс — в строках (`watch` по каждой).

- [ ] **Step 4: Implement цифру** — `refresh()`: `GET` (без мостов), при `pending` — `POST …/locate` и снова; число —
`items.length`; ошибки — цифра скрыта. Вызов — при монтировании (если `project.capable`) и при закрытии окна.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest -q -n0 -m integration tests/test_ui_project_mirrors.py tests/test_ui_git_actions.py` и `.venv/Scripts/python -m pytest -q -n0 tests/test_i18n.py tests/test_css_tokens.py`
Expected: PASS.

- [ ] **Step 6: Commit** — `app/static/js/project-mirrors.js app/static/js/code.js app/static/css/app.css app/static/en.js tests/test_ui_project_mirrors.py`, «Show mirror copies with a count, send to all and add from the dialog».

---

### Task 6: Документация, версия и проверка пакета

**Files:**
- Modify: `docs/architecture/agents-workspace.md` («Зеркало проекта»), `docs/guide.ru.md` (абзац «Зеркало проекта»), `docs/glossary.md` («Части «Агентов»»), `app/release.py`

- [ ] **Step 1: Документы**

`agents-workspace.md`: группа копий, пары и правила «локального изменения» (1–4 спеки), что пишется в пару, перенос
(`mirror_groups.adopt`, дозаполнение через `locate`), присоединение и слияние групп, цифра на кнопке, «Отправить во все
копии», пропуск SQLite с причинами. `guide.ru.md` —
тот же смысл словами владельца, без внутренних имён. `glossary.md` — строки «зеркало, копия проекта» и «группа копий».
Проверить поиском по `docs/` и `README*`: «Новое зеркало», «Синхронизировать изменения», «Отвязать зеркало», `elsewhere`,
«открытая база SQLite» — устаревшее поправить.

- [ ] **Step 2: Версия** — `app/release.py`: `VERSION = "0.45.0"`.

- [ ] **Step 3: Быстрые наборы**

Run: `.venv/Scripts/python -m pytest -q` и из `agent/`: `.venv/Scripts/python -m pytest -q`
Expected: оба PASS в бюджете 60 с; `--durations` без новых тестов дольше 1 с вне `integration`.

- [ ] **Step 4: Commit** — `docs/architecture/agents-workspace.md docs/guide.ru.md docs/glossary.md app/release.py`, «Release Studio 0.45.0: project mirrors as a group of copies».
