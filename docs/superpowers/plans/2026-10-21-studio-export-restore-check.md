# Выгрузка своей истории и проверка копии базы — план

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** пользователь скачивает всю свою историю одним потоковым ZIP; администратор и хост проверяют, что из копии базы поднимется студия.

**Architecture:** писатель ZIP (`media.ZipWriter`) выделяется из `media._zip` и общий для архива выделенного и выгрузки; выгрузка — новый `app/takeout.py` (токен в памяти, асинхронный генератор: база — на цикле событий, файлы — `asyncio.to_thread`). Проверка копии — `manage.verify_backup` на временной копии в `tmp_dir`; панель — поток-демон в `admin.py`, как `PROBE`.

**Tech Stack:** FastAPI, SQLite, Fernet, ES-модули без сборки.

**Spec:** [docs/superpowers/specs/2026-10-21-studio-export-restore-check-design.md](../specs/2026-10-21-studio-export-restore-check-design.md)

## Global Constraints

- Схема базы не меняется (`db.VERSION` прежний).
- В журнал — только «export: N файлов», «backup-check: ok/fail» и предупреждения без путей, имён, логинов.
- В выгрузке нет `token_enc`, `pw_hash`, сессий (`digest`, `csrf`), `passkeys`.
- База — только `state.DB` с цикла событий; файлы шары — только в потоке.
- `transcripts.edits` и таблица `presets` (их добавляют параллельно) попадают в выгрузку, если есть в базе (`PRAGMA table_info`, `sqlite_master`), и без них ничего не ломается.
- `EXPORT_TTL_SEC = 3600`, `EXPORT_KEEP = 2`, `VERIFY_FILES_SEC = 600`; файл `verify-<hex>.db` в `tmp_dir` (не каталог).

## Review Focus

1. Выгрузка без файлов (только чаты, шара лежит) — должна идти: шару проверяем, только если есть файлы. Тест: `test_export_without_files_needs_no_share`.
2. Строка media ушла между POST и GET — файл в `missing`, архив цел. Тест: `test_export_missing_file_goes_to_manifest`.
3. Копия — не база SQLite (мусор) — `ok: false`, временный файл убран. Тест: `test_verify_backup_of_garbage_fails_and_cleans_up`.
4. Шара не смонтирована при проверке копии по явному пути — «шара недоступна», `ok: false`. Тест: `test_verify_backup_with_the_share_down`.
5. Вторая проверка, пока идёт первая — 409 `running`. Тест: `test_admin_backup_check_runs_once`.

---

### Task 1: Общий писатель ZIP в media.py

**Files:** Modify `app/media.py` (`_zip`); Test: `tests/test_media.py` (существующие тесты архива — без правок).

**Interfaces — Produces:**
- `media.ZipWriter()` — `.entry(name: str, created: float, crc: int, size: int) -> bytes` (локальный заголовок; учитывает смещение и строку каталога), `.end() -> bytes` (центральный каталог и конец), `.count: int`.
- `media._zip` пользуется им; поведение архива прежнее.

- [ ] Переписать `_zip` на `ZipWriter`; прогнать `pytest tests/test_media.py -k archive -n0` — все зелёные.
- [ ] Коммит.

### Task 2: Выгрузка — app/takeout.py

**Files:** Create `app/takeout.py`; Modify `app/main.py` (роутер), `tests/conftest.py` (сброс `takeout.EXPORTS`); Test: `tests/test_takeout.py`.

**Interfaces:**
- Consumes: `media.ZipWriter`, `media.EXT`, `media.CHUNK`, `media.disposition`, `chains.of_image/of_video`.
- Produces: `POST /api/me/export {"sections": [...]}` → `{id, files, bytes}`; `GET /api/me/export/{token}`; `takeout.SECTIONS = ("chat","photo","video","transcribe","speech","voices")`, `takeout.EXPORTS: dict[str, tuple[int, tuple[str, ...], float]]`.

- [ ] Тесты (`tests/test_takeout.py`): состав по разделам и порядок записей (README первым, manifest последним); sha256 манифеста = байты; две учётки — чужого нет; секретов нет (хеш пароля, `token_enc`, csrf, digest сессии, id passkey); пропавший файл — `missing`; токен чужой / просроченный (`EXPORT_TTL_SEC`) — 404, не больше 2 живых; пустой и неверный список — 400; 503 шары; журнал без имён и промптов; диалог — свой JSON; поток отдаёт куски меньше файла; `edits` и `presets`, если есть; выгрузка без файлов без шары.
- [ ] Реализация; `pytest tests/test_takeout.py -n0` зелёный; коммит.

### Task 3: Выгрузка — страница

**Files:** Modify `app/static/js/settings.js` (`storageBlock`: кнопка `#export-open` → окно `#export-dialog` с галочками `input[name=export-section]`, `#export-prepare`, ссылка `#export-download` «Скачать: N файлов, X»); Test: `tests/test_ui_browser.py::test_export_dialog_prepares_a_download_link`.

- [ ] Браузерный тест; реализация; коммит.

### Task 4: Проверка копии — manage.py

**Files:** Modify `app/manage.py`; Test: `tests/test_backup_check.py`.

**Interfaces — Produces:** `manage.verify_backup(path: str | Path | None = None) -> dict` (берёт `state.CFG`); итог `{backup, at, size, integrity, schema_from, schema_to, users, tokens: {ok, failed}, files: {state, present, missing, checked, total}, ok, took, problems: [слова]}`, `backup: None` — копии нет. `manage.main(["verify-backup"[, ПУТЬ]])` → JSON в stdout, код 0/1/2.

- [ ] Тесты: целая копия (`ok`), испорченная, мусор, старая схема (`schema_from` < `schema_to`), схема 0, без учёток, чужой ключ (`tokens.failed`), пропавший файл (числа), шара недоступна, срок `VERIFY_FILES_SEC` (проверено N из M), рабочая база не изменилась (sha256 файла), временных файлов нет, коды CLI.
- [ ] Реализация; коммит.

### Task 5: Проверка копии — панель

**Files:** Modify `app/admin.py` (`CHECK`, `POST/GET /backup-check`), `app/static/js/admin.js` (строка `backup-check` в `#admin-storage`, кнопка `#admin-backup-check`); Test: `tests/test_backup_check.py` (маршруты), `tests/test_ui_browser.py::test_admin_checks_the_backup`.

- [ ] Тесты: только администратор; 409 пока идёт; итог без логинов и путей; журнал «backup-check: ok».
- [ ] Реализация; коммит.

### Task 6: Документы

**Files:** `README.md` («Где хранится история»), `CLAUDE.md` («Устройство» — takeout, «Хост» — verify-backup и запасной ключ).

- [ ] Правки; быстрый набор `pytest -q`; браузерные `-k "storage or export or admin or wipe"`; коммит.
