# Выделение нескольких фото и роликов — план реализации

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** режим выделения в лентах «Фото» и «Видео» (все три вида) с групповыми «Скачать» (ZIP или
меню «Поделиться» iPhone) и «Удалить».

**Architecture:** сервер — два адреса архива в `app/media.py` (`POST` списка → токен в памяти,
`GET` по токену → ZIP на лету без сжатия). Фронтенд — новый модуль `app/static/js/select.js`
(режим, отметки, долгое нажатие, Shift-диапазон, панель действий, удаление пачкой), который
`photo.js` и `video.js` подключают к своей ленте; `feed.js` даёт ему порядок записей, заголовки
дней и сигнал смены; `save.js` — групповое сохранение.

**Tech Stack:** FastAPI, `zipfile` стандартной библиотеки, модули ES без сборки, pytest +
браузерные сценарии (`tests/test_ui_browser.py`, CDP-драйвер).

**Spec:** [docs/superpowers/specs/2026-10-21-studio-feed-selection-design.md](../specs/2026-10-21-studio-feed-selection-design.md)

## Global Constraints

- Приватность: в журнал службы — ни имён файлов, ни промптов, ни media id; только «archive: N файлов» и предупреждения без имён.
- Пользователь видит только своё: каждая выборка `media` — с `user_id` сессии.
- `ARCHIVE_MAX = 1000`, `ARCHIVE_TTL_SEC = 600`, живых токенов на пользователя — `ARCHIVE_KEEP = 5`.
- ZIP — `ZIP_STORED`, блоками `media.CHUNK`, в потоке (синхронный генератор, как `_read`), без сборки в памяти или на диске.
- Имя архива `studio-YYYY-MM-DD.zip` по `clock.NOW()` (местное время хоста); имена внутри — `download_name`, совпавшие — `-2`, `-3`… перед расширением.
- Маршруты `/media/archives…` — в `media.py` выше `@router.get("/media/{media_id}")`.
- Фронтенд — без CDN и без сборки; текст сервера — только `textContent`; порог телефона — `max-width: 760px`.
- iPhone: признак — `save.js` `isIOS()` и `navigator.canShare`; предел суммы файлов — `save.SHARE_MAX` (100 МиБ).
- Тексты: «Выбрать», «Выбрано N», «Все» / «Снять», «Скачать» / «Скачать K», «Не больше 1000 за раз», «Удалить N картинок?» / «Удалить N роликов?», «Удалятся из истории вместе с файлами.», «Удаляю i из N», «Не удалось удалить f из N: …», «Удалено k из N, остальные не тронуты», «Готовлю i из N…», «Файлы готовы».
- Неверный список в `POST` — 400 `{"code": "invalid"}` (проверка в обработчике); тело не того вида — общий 422 `invalid` приложения.

## Review Focus

1. Долгое нажатие на iPhone: за ним приходит «хвостовой» click — он не должен снять отметку или открыть просмотр; системное меню картинки не всплывает (Task 2, тест долгого нажатия).
2. Новая карточка сверху и карточка, дошедшая до готовой, во время режима — с галочкой, но неотмеченные; «Выбрано N» не меняется (Task 2, тест создания во время режима).
3. Запись, удалённая в другой вкладке, при удалении пачкой — успех, без ошибки (Task 2, тест `gone`).
4. Телефон 390 px в режиме: шапка без горизонтальной прокрутки, панель действий на месте поля промпта не закрывает последнюю строку ленты и нижнюю навигацию (Task 2 и Task 4, тесты раскладки).
5. Esc при открытом окне подтверждения закрывает только окно; режим и отметки остаются (Task 2).

---

### Task 1: Архив ZIP на сервере

**Files:**
- Modify: `app/media.py` (константы, модель тела, два маршрута — выше `get_media`)
- Modify: `tests/conftest.py` (очистка `media.ARCHIVES` рядом с `media.SAVING.clear()`)
- Test: `tests/test_media.py`

**Interfaces:**
- Produces: `POST /api/media/archives` `{"media_ids": [str]}` → `200 {"id": str, "files": int}`;
  `GET /api/media/archives/{token}` → `application/zip`. Модуль: `media.ARCHIVE_MAX`,
  `media.ARCHIVE_TTL_SEC`, `media.ARCHIVE_KEEP`, `media.ARCHIVES: dict[str, tuple[int, list[str], float]]`
  (токен → user_id, id по порядку, время создания).

- [ ] **Step 1: Write the failing tests** (в `tests/test_media.py`; `import logging, uuid, zipfile`,
  `from datetime import datetime`, `from conftest import T0`)

```python
def archive(studio, ids):
    return studio.c.post("/api/media/archives", json={"media_ids": ids})


def test_archive_streams_the_chosen_files_in_order(studio, caplog):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    a = media.save(uid, png(64, 64), "image/png", "studio-lustify-42.png", thumb=True)
    b = media.save(uid, DATA, "video/mp4", "studio-video-7.mp4")
    c = media.save(uid, png(32, 32), "image/png", "studio-lustify-42.png")
    media.save(uid, DATA, "video/mp4", "not-chosen.mp4")
    with caplog.at_level(logging.INFO, logger="studio"):
        r = archive(studio, [b, a, c, a])
        assert r.status_code == 200 and r.json()["files"] == 3
        z = studio.c.get(f"/api/media/archives/{r.json()['id']}")
    assert z.status_code == 200 and z.headers["content-type"] == "application/zip"
    day = datetime.fromtimestamp(T0).strftime("%Y-%m-%d")
    assert z.headers["content-disposition"] == f'attachment; filename="studio-{day}.zip"'
    assert z.headers["cache-control"] == "no-store" and z.headers["x-content-type-options"] == "nosniff"
    zf = zipfile.ZipFile(io.BytesIO(z.content))
    assert zf.testzip() is None
    assert zf.namelist() == ["studio-video-7.mp4", "studio-lustify-42.png", "studio-lustify-42-2.png"]
    assert {i.compress_type for i in zf.infolist()} == {zipfile.ZIP_STORED}
    assert zf.read("studio-video-7.mp4") == DATA and zf.read("studio-lustify-42-2.png") == png(32, 32)
    assert "archive: 3" in caplog.text
    for secret in ("studio-lustify", "studio-video", a, b, c):
        assert secret not in caplog.text


def test_archive_takes_only_own_known_ids(studio):
    bob = make_user(studio, "bob"); uid = make_user(studio, "owner"); signin(studio, "owner")
    mine, theirs = media.save(uid, DATA, "video/mp4", "m.mp4"), media.save(bob, DATA, "video/mp4", "t.mp4")
    assert archive(studio, [theirs, uuid.uuid4().hex, mine]).json()["files"] == 1
    r = archive(studio, [theirs])
    assert r.status_code == 404 and r.json()["detail"]["code"] == "gone"


def test_archive_refuses_bad_lists(studio):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    mine = media.save(uid, DATA, "video/mp4", "m.mp4")
    for bad in ([], ["../etc"], [mine.upper()], [uuid.uuid4().hex for _ in range(media.ARCHIVE_MAX + 1)]):
        r = archive(studio, bad)
        assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid", bad
    assert archive(studio, [mine] * (media.ARCHIVE_MAX + 1)).json()["files"] == 1   # повторы схлопнуты


def test_archive_token_is_personal_reusable_and_expires(studio):
    uid = make_user(studio, "owner"); make_user(studio, "bob"); signin(studio, "owner")
    token = archive(studio, [media.save(uid, DATA, "video/mp4", "m.mp4")]).json()["id"]
    url = f"/api/media/archives/{token}"
    assert studio.c.get(url).status_code == 200 and studio.c.get(url).status_code == 200
    studio.clock[0] += media.ARCHIVE_TTL_SEC + 1
    assert studio.c.get(url).status_code == 404
    signin(studio, "owner")
    token = archive(studio, [media.save(uid, DATA, "video/mp4", "n.mp4")]).json()["id"]
    signin(studio, "bob")
    assert studio.c.get(f"/api/media/archives/{token}").status_code == 404


def test_archive_keeps_few_tokens_per_user(studio):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    mid = media.save(uid, DATA, "video/mp4", "m.mp4")
    tokens = [archive(studio, [mid]).json()["id"] for _ in range(media.ARCHIVE_KEEP + 1)]
    assert studio.c.get(f"/api/media/archives/{tokens[0]}").status_code == 404
    assert studio.c.get(f"/api/media/archives/{tokens[-1]}").status_code == 200
    assert len(media.ARCHIVES) == media.ARCHIVE_KEEP


def test_archive_skips_rows_and_files_gone_after_the_list(studio, env):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    a, b, c = (media.save(uid, DATA, "video/mp4", f"{n}.mp4") for n in "abc")
    token = archive(studio, [a, b, c]).json()["id"]
    asyncio.run(media.delete_async(uid, a))                      # строка ушла после POST
    media.path(uid, b).unlink()                                  # файла нет, строка есть
    names = zipfile.ZipFile(io.BytesIO(studio.c.get(f"/api/media/archives/{token}").content)).namelist()
    assert names == ["c.mp4"]
    asyncio.run(media.delete_async(uid, c))
    assert studio.c.get(f"/api/media/archives/{token}").status_code == 404


def test_archive_with_the_share_down_is_503_in_words(studio, env, monkeypatch):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    mid = media.save(uid, DATA, "video/mp4", "m.mp4")
    real = media.Path.stat
    monkeypatch.setattr(media.Path, "stat", lambda self, **kw: (_ for _ in ()).throw(OSError("share"))
                        if env.media_dir in self.parents else real(self, **kw))
    r = archive(studio, [mid])
    assert r.status_code == 503 and r.json()["detail"]["code"] == "storage"


def test_archive_needs_a_session_and_csrf(studio):
    uid = make_user(studio, "owner")
    mid = media.save(uid, DATA, "video/mp4", "m.mp4")
    assert archive(studio, [mid]).status_code == 401
    signin(studio, "owner")
    del studio.c.headers[auth.CSRF_HEADER]
    assert archive(studio, [mid]).status_code == 403
```

(`import auth` — в начало файла.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest -q -n0 tests/test_media.py -k archive`
Expected: FAIL — 404/405 на `/api/media/archives`, `AttributeError: ARCHIVE_MAX`.

- [ ] **Step 3: Implement in `app/media.py`**

- `class ArchiveIn(BaseModel): media_ids: list[str]`; `HEX = re.compile(r"[0-9a-f]{32}")`.
- `@router.post("/media/archives") async def create_archive(body: ArchiveIn, request: Request) -> dict` —
  схлопнуть повторы с сохранением порядка (`dict.fromkeys`); пусто, не `HEX.fullmatch`, больше
  `ARCHIVE_MAX` — `HTTPException(400, {"code": "invalid", "message": "неверный список файлов"})`;
  вычистить просроченные токены; строки `_archive_rows(user_id, ids)`; пусто — 404 `GONE`;
  `stat` первого файла в `asyncio.to_thread`: `OSError` кроме `FileNotFoundError` — 503
  `{"code": "storage", "message": "хранилище недоступно"}`; токен `secrets.token_urlsafe(18)`;
  у пользователя больше `ARCHIVE_KEEP` — старейшие вон. Ответ `{"id", "files": len(rows)}`.
- `def _archive_rows(user_id: int, ids: list[str]) -> list` — `SELECT id, rel_path, mime,
  has_thumb, download_name, created FROM media WHERE user_id = ? AND id IN (…)`, в порядке `ids`.
- `@router.get("/media/archives/{token}") async def get_archive(token: str, request: Request)` —
  токен не свой / просрочен — 404 `GONE`; строки заново (`_archive_rows`); пусто — 404; та же
  503-проверка; `log.info("archive: %d файлов", len(rows))`; `StreamingResponse(_zip(entries),
  media_type="application/zip", headers=…)` с `disposition(f"studio-{date}.zip")`,
  `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`.
- `def _zip(entries: list[tuple[str, Path, float]])` — синхронный генератор: `zipfile.ZipFile`
  поверх несдвигаемого приёмника (объект с `write`/`flush`, копит байты, генератор отдаёт их после
  каждого блока), `ZipInfo(name, date_time=localtime(created)[:6])`, `file_size` из `stat`
  (zip64 — только если нужен), `zf.open(info, "w")`, чтение блоками `CHUNK`. `FileNotFoundError`
  на `stat`/`open` — запись пропускается; прочая `OSError` посреди — `log.warning("archive:
  хранилище оборвало чтение")` и выход из генератора (обрыв ответа).
- `def _entry_names(rows) -> list[str]` — `download_name` без `/`, `\`, управляющих и ведущих
  точек (замена на `_`, пусто — `file`), совпавшие — `stem-2.ext`, `stem-3.ext`.
- Строку модуля (docstring) дополнить пунктом про архив.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest -q -n0 tests/test_media.py`
Expected: PASS (все, включая прежние).

- [ ] **Step 5: Commit** — `git add app/media.py tests/conftest.py tests/test_media.py`, сообщение
  «Stream several history files as one ZIP» через `git commit -F <файл>`.

---

### Task 2: Режим выделения и удаление пачкой в «Фото»

**Files:**
- Create: `app/static/js/select.js`
- Modify: `app/static/js/feed.js` (экспорт `dayKey`; опции `onChange`, `dayTools`; метод `items()`)
- Modify: `app/static/js/ui.js` (`plural`)
- Modify: `app/static/js/photo.js` (кнопка в шапке, `decorate` в `card()`, подключение ленты, `destroy`)
- Modify: `app/static/css/app.css`
- Test: `tests/test_ui_browser.py`

**Interfaces:**
- Consumes: существующие `remove(item, dialogs)` раздела (DELETE, `gone` — успех, `forget`, `feed.remove`).
- Produces:
  - `ui.plural(n: number, forms: [string, string, string]) -> string` — `[1 картинку, 2 картинки, 5 картинок]`-правило (11–14 — третья форма).
  - `feed.dayKey(created: number) -> string` (экспорт существующей).
  - `createFeed(el, { …, onChange?: () => void, dayTools?: (key: string) => Node|null })`;
    ответ дополнен `items(): Item[]` (копия, порядок ленты). `onChange` — после каждой смены
    показанных записей: страница, `prepend`, `update`, `remove`, `reload`, `redraw`.
    `dayTools(key)` — узел в начало `h2.feed-day-title` нового дня.
  - `select.createSelection({ section, root, head, feedBox, error, nouns, selectable, mediaOf, remove })`:
    `section` — `'photo'|'video'` (префикс id); `nouns` — формы для `plural`
    (`['картинку','картинки','картинок']` / `['ролик','ролика','роликов']`); `selectable(item) -> bool`;
    `mediaOf(item) -> string|null`; `remove(item) -> Promise`. Ответ:
    `{ button: Node, tools: Node, actions: Node, decorate(node, item), dayTools(key), attach(feed), sync(), exit(), destroy(), get selecting() }`.
  - id и классы (их ищут тесты Task 3–4): `#<s>-select` (кнопка «Выбрать»), `#<s>-select-exit`,
    `#<s>-select-count`, `#<s>-select-all`, `#<s>-select-download`, `#<s>-select-delete`,
    `#<s>-select-delete-dialog`, `#<s>-select-delete-confirm`; у карточки — `button.card-pick`
    (`role=checkbox`, `aria-checked`), атрибут `data-selected`; у дня — `button.day-pick`;
    режим — `data-selecting` на корне раздела (`.photo` / `.video`).
  - `actions` — один узел с «Скачать» и «Удалить»: модуль сам переносит его в шапку (`> 760px`)
    или в нижнюю панель `.select-bar` на месте поля промпта (`≤ 760px`, `matchMedia` с подпиской).
    «Скачать» в этой задаче — заглушка без обработчика (её делает Task 3).

- [ ] **Step 1: Write the failing browser tests** (рядом с тестами вида ленты; помощники —
  `pick(i)` щёлкает `.card-pick` i-й карточки, `long_press(sel)` — `eval` с `PointerEvent`
  `pointerType: 'touch'` вниз, пауза 600 мс, вверх и следом `click`, `shift_click(sel)` —
  `dispatchEvent(new MouseEvent('click', { bubbles: true, shiftKey: true }))`)

  - `test_photo_selection_deletes_the_picked_and_keeps_the_rest[390|1280]`: 4 картинки; «Выбрать»;
    нажатие по двум карточкам (`.card-open` не открывает `#viewer`); `#photo-select-count` —
    «Выбрано 2»; смена вида на «Таблица» и обратно — те же два `data-selected`; «Удалить» →
    заголовок окна «Удалить 2 картинки?» → подтверждение → в ленте 2 карточки, обе не те;
    `data-selecting` снят; `GET /api/images` — 2 записи. На 390 — `NO_OVERFLOW` в режиме и
    `.select-bar` видна, `#photo-composer` скрыт; на 1280 — «Удалить» внутри `.head`.
  - `test_photo_selection_by_long_press_hover_shift_and_day[390|1280]`: на 390 — `long_press`
    третьей карточки: режим включён, отмечена ровно она, `#viewer` не открылся; на 1280 —
    щелчок `.card-pick` первой карточки включает режим, `shift_click` четвёртой отмечает 1–4;
    `.day-pick` «Сегодня» снимает все, повторно — отмечает все; «Все» → «Снять» → 0; Escape —
    режим выключен.
  - `test_photo_selection_survives_a_new_card_and_counts_gone_as_deleted`: 1280; 3 картинки;
    «Выбрать», отметить две; одну из отмеченных удалить через `api('DELETE', …)` в `eval`
    (другая вкладка); через composer создать картинку — новая карточка сверху с `.card-pick`,
    без `data-selected`, счёт «Выбрано 2»; «Удалить» → «Удалить 2 картинки?» → готово, ошибки в
    `#photo-error` нет, режим выключен, в ленте 2 записи (новая и неотмеченная).
  - `test_photo_selection_escape_in_the_dialog_keeps_the_mode`: 1280; отметить одну, «Удалить»,
    Escape — окна нет, `data-selecting` есть, «Выбрано 1».
  - `test_photo_selection_stop_and_failure_are_told`: 1280; 3 картинки отмечены; `eval` подменяет
    `window.fetch` так, что `DELETE` второй отвечает 503 `storage`; подтверждение → `#photo-error`
    «Не удалось удалить 1 из 3: …», режим включён, отмечена одна (неудалённая). Второй прогон:
    `DELETE` держится (промис ждёт `window.release`), во время хода — «Отмена» окна, затем
    `window.release()` — «Удалено 1 из 3, остальные не тронуты», две остались отмеченными.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest -q -n0 -m integration tests/test_ui_browser.py -k photo_selection`
Expected: FAIL — `#photo-select` нет на странице.

- [ ] **Step 3: Implement `plural` in `ui.js`, хуки `feed.js`, модуль `select.js`**

  Поведение модуля — разделы 6–7 спеки. Решения, которые не следуют из интерфейса:
  - Нажатия режима — один обработчик `click` на `feedBox` в фазе захвата: в режиме `.day-pick`
    пропускается, по карточке `.card[data-id]` — `preventDefault` + `stopPropagation` + отметка.
    Поверх карточки в режиме — слой `::after` (`inset: 0`, выше содержимого, ниже `.card-pick`):
    `<video>` превью не играет, кнопки карточки не нажимаются; сами кнопки скрыты CSS.
  - Долгое нажатие: `pointerdown` с `pointerType !== 'mouse'` на отмечаемой карточке → таймер
    500 мс; `pointermove` дальше 10 px, `pointerup`, `pointercancel` — сброс. Сработал — режим,
    отметка, и следующий `click` (хвост касания) глотается один раз. `contextmenu` на карточках
    ленты — `preventDefault`. CSS карточек ленты: `-webkit-touch-callout: none; user-select: none`.
  - `.card-pick` вне режима: видна только на `(hover: hover)` при наведении на карточку; щелчок —
    режим + отметка (`stopPropagation`, чтобы не открыть просмотр).
  - Shift+щелчок: диапазон между последней отмеченной (`anchor`) и этой по `feed.items()`,
    только `selectable`.
  - Escape (`keydown` на `document`): только в режиме и если в `root` нет `dialog[open]`.
  - `sync()` (его зовёт `onChange` ленты): отметки без записи в `feed.items()` или ставшие
    неотмечаемыми снимаются; счёт, «Все»/«Снять», `aria-checked` дней и кнопок — заново.
  - Удаление: окно `openDialog(root, { id: '<s>-select-delete-dialog', title: \`Удалить ${n} ${plural(n, nouns)}?\`, … })`;
    по подтверждению — по очереди `await remove(item)` в порядке ленты, ход «Удаляю i из N» в
    окне; событие `close` окна во время хода и `destroy()` ставят «стоп» (следующий не
    начинается); итог — по разделу 7 спеки в `error` раздела.
  - Шапка в режиме: `h1` и меню раздела прячутся, `tools` (✕, счёт, «Все») — видны; кнопки вида
    остаются.

- [ ] **Step 4: Wire `photo.js`**

  `createSelection({ section: 'photo', root, head, feedBox, error, nouns: ['картинку', 'картинки', 'картинок'],
  selectable: (i) => !RUNNING.has(i.state), mediaOf: (i) => (i.state === 'done' && i.media_id) || null,
  remove: (i) => remove(i) })`; `button` — в шапку перед `layout.node`, `tools` — в шапку;
  `card()` в конце зовёт `selection.decorate(node, item)` для всех трёх видов;
  `createFeed(…, { onChange: () => selection.sync(), dayTools: selection.dayTools })`, затем
  `selection.attach(feed)`; `destroy()` зовёт `selection.destroy()`.

- [ ] **Step 5: CSS в `app.css`** — галочка круглая в углу (в таблице — квадрат первым
  столбцом), рамка `var(--accent)` у `[data-selected="true"]`, `.day-pick`, `.select-bar`
  (на месте composer, с `env(safe-area-inset-bottom)`), скрытие composer и кнопок карточек в
  `[data-selecting]`; обе темы — существующие токены.

- [ ] **Step 6: Run tests to verify they pass**

Run: `.venv/Scripts/python -m pytest -q -n0 -m integration tests/test_ui_browser.py -k "photo_selection or photo_views or photo_layout or preview_save"`
Expected: PASS (новые и прежние тесты вида, раскладки и одиночного «Скачать»).

- [ ] **Step 7: Commit** — «Select several photos and delete them at once».

---

### Task 3: «Скачать» выделенное — ZIP и меню «Поделиться» iPhone

**Files:**
- Modify: `app/static/js/save.js` (`saveMany`, общее окно «готово»)
- Modify: `app/static/js/select.js` (обработчик «Скачать»)
- Test: `tests/test_ui_browser.py`

**Interfaces:**
- Consumes: Task 1 (`POST /api/media/archives`, `GET /api/media/archives/{token}`), Task 2 (`#<s>-select-download`, `actions`).
- Produces: `save.saveMany(mediaIds: string[], host: Node, onProgress?: (done: number, total: number) => void) -> Promise<void>` —
  iPhone: файлы по одному `fetch` в память, сумма `Content-Length` больше `SHARE_MAX` или
  `canShare({ files })` ложно — архив; `share({ files })`; `NotAllowedError` — окно
  `#save-dialog` «Файлы готовы» с `#save-again` (то же окно, что у одиночного, с заголовком по
  числу файлов); `AbortError` — тихо. Не iPhone и запасной путь — `POST` архива и ссылка
  `<a href="/api/media/archives/<id>" download>`.click(). Отказ `POST` — исключение с
  `code`/`message` (как `api()`). Одиночный `saveMedia` — без изменений поведения.

- [ ] **Step 1: Write the failing browser tests**

  - `test_photo_selection_downloads_one_zip[1280]` (и вариант 390 без `iphone()` — Android):
    3 картинки, отметить 2, `CLICKS`-шпион, «Скачать» → `window.clicked == [["/api/media/archives/<token>", True]]`;
    отправленный `POST /api/media/archives` (`r["sent"]`) — один; режим выключен; затем
    `fetch(href)` в `eval` → `content-type` `application/zip`.
  - `test_photo_selection_counts_only_files_to_download`: отметить готовую и неудачную
    (неудачная — строка `images` с `state='error'` без `media_id`, вставленная `live_sql`
    до открытия раздела) — кнопка «Скачать 1».
  - `test_photo_selection_on_iphone_shares_all_files[fail_first]`: 390, `iphone(fail_first)`,
    отметить 3 → «Скачать» → (при `fail_first` — `#save-dialog`, `#save-again`) →
    `window.shared == [[[".png", "image/png", True]] * 3]`, `window.clicked` пуст.
  - `test_photo_selection_too_big_for_the_iphone_is_a_zip`: подмена `fetch` из
    `test_too_big_for_the_phone_is_downloaded_not_shared` (200 МиБ) → ни одного `share`,
    одна ссылка на `/api/media/archives/…`.
  - `test_photo_selection_archive_refusal_is_told`: подмена `fetch` — `POST /api/media/archives`
    отвечает 503 `storage` → в `#photo-error` «Хранилище недоступно», режим и отметки на месте.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest -q -n0 -m integration tests/test_ui_browser.py -k "selection_downloads or selection_counts or selection_on_iphone or too_big_for_the_iphone or archive_refusal"`
Expected: FAIL — «Скачать» ничего не делает.

- [ ] **Step 3: Implement `saveMany` in `save.js`** (по интерфейсу выше; `isIOS`, ссылка
  загрузки и окно «готово» — общие с одиночным; имена файлов меню —
  `studio-<8 знаков id><EXT>`).

- [ ] **Step 4: Wire «Скачать» in `select.js`** — media id отмеченных в порядке ленты
  (`mediaOf`), подпись «Скачать» / «Скачать K» (K — с файлом, если меньше отмеченных),
  недоступна при K = 0 и при K > 1000 (`title` «Не больше 1000 за раз»); во время —
  обе кнопки недоступны, ход «Готовлю i из N…» в счёте; успех — `exit()`; отказ — `words(err)`
  в `error`, режим остаётся.

- [ ] **Step 5: Run tests to verify they pass**

Run: та же команда, что в Step 2, плюс `-k "preview_save or too_big_for_the_phone"`
Expected: PASS.

- [ ] **Step 6: Commit** — «Download the picked photos as a ZIP or share them on the iPhone».

---

### Task 4: «Видео», документы и быстрый набор

**Files:**
- Modify: `app/static/js/video.js`
- Modify: `app/static/css/app.css` (если ролику нужно своё: `<video>` превью под слоем, плитка с подписью)
- Modify: `README.md` (абзац о выделении в «Фото» и «Видео»), `CLAUDE.md` («Устройство»: `/api/media/archives` в `media`, модуль `select.js`)
- Test: `tests/test_ui_browser.py`

**Interfaces:**
- Consumes: Task 2 `createSelection`, Task 3 «Скачать».

- [ ] **Step 1: Write the failing browser tests** (с фикстурой `ready_clips`, ролики — через
  `/api/videos`, как в `test_video_tiles_and_table_open_the_clip_in_the_viewer`)

  - `test_video_selection_in_every_view[390|1280]`: 3 ролика; для каждого вида — «Выбрать»,
    нажатие по ролику: `#video-viewer` не открылся, у `<video>` превью `paused === true`;
    отмечено; на 390 — `NO_OVERFLOW`, `.select-bar` видна.
  - `test_video_selection_deletes_and_downloads`: 1280; отметить 2 → «Удалить» → «Удалить 2
    ролика?» → остался 1; отметить его → «Скачать» → ссылка `/api/media/archives/…`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python -m pytest -q -n0 -m integration tests/test_ui_browser.py -k video_selection`
Expected: FAIL — `#video-select` нет.

- [ ] **Step 3: Wire `video.js`** — как Step 4 Task 2: `section: 'video'`,
  `nouns: ['ролик', 'ролика', 'роликов']`, `selectable: (i) => !RUNNING.has(i.state)`,
  `mediaOf: (i) => (i.state === 'done' && i.media_id) || null`, `remove: (i) => remove(i)`;
  `decorate` в конце `card()` для превью, плитки и таблицы; `destroy`.

- [ ] **Step 4: Docs** — README: как включить выделение (кнопка, долгое нажатие, наведение),
  что делают «Скачать» (ZIP; iPhone — «Поделиться», больше 100 МиБ — ZIP в «Файлы») и
  «Удалить»; CLAUDE.md — две строки в «Устройство».

- [ ] **Step 5: Run the touched browser tests and the fast suite**

Run: `.venv/Scripts/python -m pytest -q -n0 -m integration tests/test_ui_browser.py -k "selection or video_tiles or video_layout or photo_views"`
Expected: PASS.
Run: `.venv/Scripts/python -m pytest -q`
Expected: PASS, быстрый набор в бюджете 60 с.

- [ ] **Step 6: Commit** — «Select several clips in the video feed; document the selection».
