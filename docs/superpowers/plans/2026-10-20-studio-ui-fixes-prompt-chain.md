# Мелочи интерфейса и цепочка промптов — план

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** единая иконка «Облачко с искрой» (и настоящая иконка на iPhone), кнопка темы в интерфейсе, «Скачать» на превью, смена логина в панели, цепочка промптов у картинок и роликов.

**Architecture:** первый из трёх планов спеки (этапы 1–2 раздела 9). Фронтенд — модули ES без сборки в `app/static/js/`; служба — FastAPI-модули в `app/`. Цепочка — снимок шагов JSON в новых столбцах `images.chain` и `videos.chain`, сборка и разметка «удалено» — новый модуль `app/chains.py`. Иконки — один рисунок, PNG собирает скрипт на Pillow.

**Tech Stack:** Python 3.13 (хост) / 3.14 (Windows), FastAPI, SQLite, Pillow; ванильный JS (ES-модули); pytest, браузерные сценарии через `tests/browser.py` (CDP, Edge/Chrome + node 22).

**Spec:** [docs/superpowers/specs/2026-10-20-studio-sections-voice-design.md](../specs/2026-10-20-studio-sections-voice-design.md) — решения 11–15, разделы 6.6, 6.7. Основа — [2026-10-19-studio-pwa-design.md](../specs/2026-10-19-studio-pwa-design.md).

Следующие планы (пишутся отдельно): «Подключения по разделам» (этап 3), «Речь: голосовой ввод, расшифровка, озвучка» (этапы 4–6).

## Global Constraints

- Приватность журналов: промпты, имена файлов, логины в журнал не пишутся — только события с id (`log.info("… %d …", uid)`).
- Шаг миграции только добавляет (`db.STEPS`): код прежней выкладки работает с новой базой; `SCHEMA` — сразу последняя версия.
- Фронтенд без сборки, без CDN; текст сервера — только `textContent`/`el()`, `innerHTML` — только с константами.
- `/static/*` открыт всем: адресов LAN в статике нет (`test_static_has_no_lan_addresses`).
- Быстрый набор `pytest -q` ≤ 60 с; браузерные сценарии — `@pytest.mark.integration` в `tests/test_ui_browser.py`.
- Тест-команды из корня репозитория: быстрый — `.venv/Scripts/python -m pytest -q`, точечный — `.venv/Scripts/python -m pytest -q tests/<файл>.py -k <имя>`, браузер — `.venv/Scripts/python -m pytest -q -m integration tests/test_ui_browser.py -k <имя>`.
- Коммит — сообщение через файл (`git commit -F`), в конце `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`; без пуша.
- Цвета иконки (решение 14): градиент `#e2825c` → `#b24f2b`, облачко `#ffffff`, искра `#c96442`.

## Review Focus

1. **iPhone теряет «нажатие» до `navigator.share`** (картинка качается дольше, чем Safari держит жест) → `NotAllowedError`; человек ждёт, что сохранение всё равно возможно: окно «Файл готов» с кнопкой «Сохранить» — тест в задаче 3.
2. **Ролик или картинка больше разумного для памяти телефона** (`Content-Length` > 100 МиБ) → не share, а обычная загрузка — тест в задаче 3.
3. **Логин, отличающийся только регистром от занятого** (`Owner` при `owner`) → 409 «такой логин уже есть», а не вторая учётка — тест в задаче 4.
4. **Старые строки без цепочки со ссылками по кругу** (исходник A — картинка B, исходник B — картинка A) → ответ приходит, цепочка конечна — тест в задаче 5.
5. **Исходник — картинка другого пользователя** (старая строка с чужим `source_media_id`) → чужой промпт и миниатюра не утекают — тест в задаче 5.

---

### Task 1: Иконка «Облачко с искрой» и заглушка iPhone

Почему iPhone рисует белую «С» на чёрном: заглушку iOS кладёт на `theme_color` manifest (`#1b1b1b`) — значит, иконку он брал из manifest, а первой крупной там стоит SVG `sizes: any`, которую iOS как иконку не читает. Исправление: в manifest — только PNG; `apple-touch-icon` с `sizes="180x180"`; корневые `/apple-touch-icon.png`, `/apple-touch-icon-precomposed.png`, `/favicon.ico` (iOS и браузеры спрашивают их сами). Проверка на живом iPhone — в задаче 7.

**Files:**
- Create: `tools/make_icons.py`
- Modify: `app/static/icons/icon.svg`, `icon-192.png`, `icon-512.png`, `icon-maskable-512.png`, `apple-touch-icon.png`; Create: `app/static/icons/favicon.ico`
- Modify: `app/static/manifest.webmanifest`, `app/static/index.html:12-13,25`, `app/static/login.html:11-12`, `app/static/offline.html:10`, `app/static/css/app.css:47-49`, `app/main.py` (после `/manifest.webmanifest`)
- Test: `tests/test_static.py`

**Interfaces:**
- Produces: рисунок (512×512): облачко `M168 108 H344 A84 84 0 0 1 428 192 V300 A84 84 0 0 1 344 384 H236 L152 446 L168 384 A84 84 0 0 1 84 300 V192 A84 84 0 0 1 168 108Z` (`#fff`), искра `M256 158 C266 222 286 236 340 246 C286 256 266 270 256 334 C246 270 226 256 172 246 C226 236 246 222 256 158Z` (`#c96442`), фон — диагональный градиент от левого верхнего `#e2825c` к правому нижнему `#b24f2b`.
- Варианты: `icon.svg`, `icon-192.png`, `icon-512.png` — квадрат со скруглением `rx=112` (углы прозрачные); `apple-touch-icon.png` (180) и `icon-maskable-512.png` — фон на всю площадь, без альфы (RGB); у maskable облачко и искра уменьшены до 0,75 вокруг центра (безопасная зона 80 %); `favicon.ico` — 16, 32, 48 из варианта «со скруглением».

- [ ] **Step 1: Write the failing tests** (в `tests/test_static.py`)

```python
def test_manifest_icons_are_png_only():
    """SVG `sizes: any` iOS берёт первой и не читает — рисует букву на theme_color."""
    m = json.loads((STATIC / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert m["icons"] and all(i["type"] == "image/png" and i["src"].endswith(".png") for i in m["icons"])

def test_icons_have_the_sizes_they_claim():          # заменяет прежний
    from PIL import Image
    for name, size in {"icon-192.png": 192, "icon-512.png": 512, "icon-maskable-512.png": 512,
                       "apple-touch-icon.png": 180}.items():
        with Image.open(STATIC / "icons" / name) as im:
            assert im.size == (size, size), name
    for name in ("apple-touch-icon.png", "icon-maskable-512.png"):
        with Image.open(STATIC / "icons" / name) as im:
            assert im.mode == "RGB", name                   # без прозрачности: iOS залил бы её чёрным
    with Image.open(STATIC / "icons" / "favicon.ico") as im:
        assert {(16, 16), (32, 32), (48, 48)} <= set(im.info["sizes"])

def test_icon_is_the_bubble_with_a_spark():
    from PIL import Image
    near = lambda a, b: all(abs(x - y) <= 30 for x, y in zip(a, b))
    with Image.open(STATIC / "icons" / "apple-touch-icon.png") as im:
        assert near(im.getpixel((6, 6)), (0xe2, 0x82, 0x5c))       # градиент у левого верхнего угла
        assert near(im.getpixel((60, 60)), (255, 255, 255))         # облачко
        assert near(im.getpixel((90, 86)), (0xc9, 0x64, 0x42))      # центр искры
    with Image.open(STATIC / "icons" / "icon-512.png") as im:
        assert im.getpixel((2, 2))[3] == 0                          # скруглённый угол прозрачен
    svg = (STATIC / "icons" / "icon.svg").read_text(encoding="utf-8")
    assert "#e2825c" in svg and "A84 84 0 0 1 428 192" in svg and "C266 222 286 236 340 246" in svg

def test_pages_link_the_same_icon():
    for page in ("index.html", "login.html"):
        text = (STATIC / page).read_text(encoding="utf-8")
        assert '<link rel="apple-touch-icon" sizes="180x180" href="/static/icons/apple-touch-icon.png">' in text, page
    assert '<img class="logo" src="/static/icons/icon.svg" alt="">' in (STATIC / "index.html").read_text(encoding="utf-8")

def test_root_icon_paths_answer_without_login(studio):
    touch = (STATIC / "icons" / "apple-touch-icon.png").read_bytes()
    for path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
        r = studio.c.get(path)
        assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == touch, path
    r = studio.c.get("/favicon.ico")
    assert r.status_code == 200 and r.headers["content-type"] in ("image/x-icon", "image/vnd.microsoft.icon")
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python -m pytest -q tests/test_static.py`
Expected: FAIL — `test_manifest_icons_are_png_only` (есть svg), `test_root_icon_paths_answer_without_login` (404), прочие новые.

- [ ] **Step 3: Write `tools/make_icons.py`** — `python tools/make_icons.py [каталог]` (по умолчанию `app/static/icons`) пишет пять PNG и `favicon.ico` из геометрии Interfaces. Только Pillow (уже в зависимостях): рисовать в 4× (2048 на 512) и уменьшать `LANCZOS`; облачко — `rounded_rectangle((84,108,428,384), 84)` плюс многоугольник хвоста `(236,384),(152,446),(168,384)`; искра — четыре кубические кривые, разбитые на точки (≥ 32 на кривую), `polygon`; градиент — построчно/попиксельно по `t = (x + y) / (2·размер)`. Заголовок модуля — откуда геометрия (`icon.svg`) и что PNG коммитятся (на хосте скрипт не запускается).

- [ ] **Step 4: Write `app/static/icons/icon.svg`** — `viewBox="0 0 512 512"`, `<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">` с двумя стопами из Interfaces, `<rect width="512" height="512" rx="112" fill="url(#g)"/>`, затем облачко и искра путями из Interfaces. Запустить `.venv/Scripts/python tools/make_icons.py` — PNG и `favicon.ico` перезаписаны.

- [ ] **Step 5: Manifest и страницы**

- `manifest.webmanifest`: из `icons` убрать запись SVG, остальные три — как есть.
- `index.html`, `login.html`: `<link rel="apple-touch-icon" sizes="180x180" href="/static/icons/apple-touch-icon.png">`; `<link rel="icon" href="/static/icons/icon.svg" type="image/svg+xml">` остаётся, добавить `<link rel="icon" href="/favicon.ico" sizes="any">` перед ним. `offline.html` — та же пара `rel="icon"`.
- `index.html:25`: `<span class="logo">…</span>` → `<img class="logo" src="/static/icons/icon.svg" alt="">`; `app.css:47-49`: `#rail .logo { width: 32px; height: 32px; border-radius: 9px; margin-bottom: 10px; }` (фон и правило `svg` — убрать).

- [ ] **Step 6: Корневые пути в `app/main.py`** — `@app.api_route("/apple-touch-icon.png", methods=PAGE)` и `"/apple-touch-icon-precomposed.png"` → `FileResponse(auth.STATIC / "icons" / "apple-touch-icon.png", media_type="image/png", headers={"Cache-Control": "no-cache"})`; `"/favicon.ico"` → тот же вид с `favicon.ico`, `media_type="image/x-icon"`. Строку анонимных путей в докстринге модуля дополнить.

- [ ] **Step 7: Run tests**

Run: `.venv/Scripts/python -m pytest -q tests/test_static.py`
Expected: PASS (все, включая прежние `test_manifest_makes_an_installable_app`, `test_shell_files_are_public_and_api_is_not`).

- [ ] **Step 8: Commit** — `tools/make_icons.py`, `app/static/icons/*`, manifest, три страницы, `app.css`, `main.py`, `tests/test_static.py`; сообщение «Give the studio one bubble-and-spark icon and a real iPhone icon».

---

### Task 2: Кнопка темы в интерфейсе

**Files:**
- Modify: `app/static/index.html` (кнопка в `#rail` перед `#avatar`), `app/static/js/app.js` (`applyTheme`, `setupTheme`), `app/static/css/app.css` (рельс, мобильная шапка), `app/static/js/profile.js` (убрать `themeBlock`, `THEMES`)
- Test: `tests/test_ui_browser.py`, `tests/test_static.py`

**Interfaces:**
- Produces: `#theme-toggle` — `button.theme-toggle`, `aria-label` — действие («Тёмная тема» / «Светлая тема»), внутри `svg.ico`: луна при светлой видимой теме, солнце — при тёмной.
- Consumes: `PATCH /api/me {theme}` (`profile.THEMES = ("auto", "dark", "light")`, без изменений на сервере).

- [ ] **Step 1: Write the failing tests**

`tests/test_static.py`:
```python
def test_theme_toggle_lives_in_the_rail_not_the_profile():
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert index.index('id="theme-toggle"') < index.index('id="avatar"')
    assert "themeBlock" not in (STATIC / "js" / "profile.js").read_text(encoding="utf-8")
```

`tests/test_ui_browser.py` (рядом с `test_theme_from_profile_is_applied`):
```python
@pytest.mark.parametrize("width", [390, 1280])
def test_theme_toggle_switches_and_is_kept(browser, live, width):
    login = f"toggle{width}"
    live.add_user(login)
    expected = "(matchMedia('(prefers-color-scheme: dark)').matches ? 'light' : 'dark')"
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("eval", "want", expected),
        ("click", "#theme-toggle"), ("wait", f"document.documentElement.dataset.theme === {expected}"),
        ("idle",), ("eval", "reload", "location.reload()"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"),
        ("eval", "kept", "document.documentElement.dataset.theme"),
        ("click", "#theme-toggle"), ("wait", f"document.documentElement.dataset.theme !== {expected}"),
        ("eval", "apart", "(() => { const a = document.querySelector('#theme-toggle').getBoundingClientRect();"
                          " const b = document.querySelector('#avatar').getBoundingClientRect();"
                          " return a.right <= b.left || b.right <= a.left || a.bottom <= b.top || b.bottom <= a.top; })()"),
        ("eval", "inside", "(() => { const a = document.querySelector('#theme-toggle').getBoundingClientRect();"
                           " return a.left >= 0 && a.top >= 0 && a.right <= innerWidth && a.bottom <= innerHeight; })()"),
        ("eval", "go", "location.hash = '#/profile'"), ("wait", "document.querySelector('#view .page')"),
        ("eval", "profileTheme", "!!document.querySelector('#theme-auto, #theme-dark, #theme-light')")],
        width=width, mobile=width == 390)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["kept"] == v["want"] and v["apart"] and v["inside"] and v["profileTheme"] is False
    assert not problems(r), problems(r)
```

Тот же файл: из прежнего сценария профиля (шаги `#theme-dark`/`#theme-auto`, `test_ui_browser.py:331-341`, и `assert v["theme"] == …`) тему убрать — она теперь в `test_theme_toggle_switches_and_is_kept`.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python -m pytest -q tests/test_static.py -k theme_toggle` и `.venv/Scripts/python -m pytest -q -m integration tests/test_ui_browser.py -k theme_toggle`
Expected: FAIL (нет `#theme-toggle`).

- [ ] **Step 3: Implement**

- `index.html`: `<button id="theme-toggle" class="theme-toggle" type="button"></button>` сразу перед `#avatar`.
- `app.js`: `function visibleTheme()` → `'dark' | 'light'` (зафиксированная `me.theme` или `matchMedia('(prefers-color-scheme: dark)')`); `function paintToggle()` — значок и `aria-label`/`title` по `visibleTheme()` (константы значков — рядом с `ICONS`); `function setupTheme()` — клик: `next = visibleTheme() === 'dark' ? 'light' : 'dark'`, сразу `applyTheme(next)`, `me.theme = next`, `paintToggle()`, затем `api('PATCH', '/api/me', { theme: next })` с молчаливым `catch` (тема уже применена и лежит в localStorage; без сети сохранится при следующем нажатии); `matchMedia(...).addEventListener('change', paintToggle)` для темы `auto`. Вызвать из `start()` после `setupMenu()`. Импорт `api` — из `./api.js`. Шапку модуля дополнить строкой о кнопке.
- `app.css`: рабочий стол — `.theme-toggle` того же вида, что `#avatar` (34px, круг, `var(--bubble)`, рамка), над ним в рельсе; мобильная (`@media (max-width: 760px)`) — `position: fixed; z-index: 21; top: calc(10px + env(safe-area-inset-top, 0px)); right: 54px;` и `.head { padding-right: 100px; }` вместо 60px.
- `profile.js`: удалить `THEMES`, `themeBlock`, его вызов в `mount`; шапку модуля поправить («тема — кнопкой в рельсе»).

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest -q tests/test_static.py` и `.venv/Scripts/python -m pytest -q -m integration tests/test_ui_browser.py -k "theme or profile or avatar or login_shell"`
Expected: PASS.

- [ ] **Step 5: Commit** — «Move the theme switch from the profile into the rail».

---

### Task 3: «Скачать» на превью (и iPhone через «Поделиться»)

**Files:**
- Create: `app/static/js/save.js`
- Modify: `app/static/js/photo.js` (карточка, просмотр), `app/static/js/video.js:187` (кнопка ролика), `app/static/css/app.css` (`.card-save`)
- Test: `tests/test_ui_browser.py`

**Interfaces:**
- Produces: `export async function saveMedia(mediaId: string, host: HTMLElement): Promise<void>` в `save.js`; `export const SHARE_MAX = 100 * 1024 * 1024`.
- Поведение: iOS (`/iP(hone|ad|od)/` в `navigator.userAgent` или `Macintosh` при `navigator.maxTouchPoints > 1`) и есть `navigator.canShare` → `fetch('/api/media/<id>', { credentials: 'same-origin' })`; `Content-Length` > `SHARE_MAX` → обычная загрузка; иначе `File([blob], 'studio-<первые 8 знаков id><расширение по blob.type: .png .jpg .webp .mp4>', { type: blob.type })`, `navigator.canShare({ files: [file] })` → `navigator.share({ files: [file] })`. `AbortError` (человек закрыл меню) — тишина. `NotAllowedError` (жест «истёк», пока качали) → `openDialog(host, { id: 'save-dialog', title: 'Файл готов', actions: [button('Сохранить', { id: 'save-again', class: 'btn btn-accent' })] })`, «Сохранить» зовёт `navigator.share` с тем же файлом и закрывает окно. Иначе (компьютер, Android) — `<a href="/api/media/<id>?download=1" download>` и `.click()`.

- [ ] **Step 1: Write the failing tests** (`tests/test_ui_browser.py`, раздел «фото»)

```python
CLICKS = """(() => { window.clicked = []; const real = HTMLAnchorElement.prototype.click;
  HTMLAnchorElement.prototype.click = function () { window.clicked.push([this.getAttribute('href'), this.hasAttribute('download')]); };
  return true; })()"""

def iphone(fail_first=False):
    """Safari на iPhone: userAgent, canShare, share — записывает файлы; fail_first — первый вызов как «жест истёк»."""
    return ("eval", "iphone", f"""(() => {{
      Object.defineProperty(navigator, 'userAgent', {{ value: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)' }});
      window.shared = []; let first = {str(fail_first).lower()};
      navigator.canShare = () => true;
      navigator.share = async (d) => {{ if (first) {{ first = false; throw new DOMException('gesture', 'NotAllowedError'); }}
        window.shared.push(d.files.map(f => [f.name.slice(-4), f.type, f.size > 0])); }};
      return true; }})()""")

@pytest.mark.parametrize("width", [390, 1280])
def test_preview_save_downloads_without_opening_the_viewer(browser, live, width):
    login = f"save{width}"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*photo_screen(login, cards=1), ("wait", done_cards(1)), ("eval", "spy", CLICKS),
        ("eval", "visible", "getComputedStyle(document.querySelector('#photo-feed .card-save')).opacity !== '0'"
                            " || matchMedia('(hover: hover)').matches"),
        ("click", "#photo-feed .card-save"), ("wait", "window.clicked.length === 1"),
        ("eval", "clicked", "window.clicked"), ("eval", "viewer", "!!document.querySelector('#viewer')")],
        width=width, mobile=width == 390)
    assert not r.get("failed"), r
    (href, download), = r["values"]["clicked"]
    assert href.startswith("/api/media/") and href.endswith("?download=1") and download
    assert r["values"]["viewer"] is False and r["values"]["visible"]
    assert not problems(r), problems(r)

@pytest.mark.parametrize("fail_first", [False, True])
def test_preview_save_on_iphone_goes_through_the_share_sheet(browser, live, fail_first):
    login = f"ios{int(fail_first)}"
    live.add_user(login)
    steps = [*photo_screen(login, cards=1), ("wait", done_cards(1)), iphone(fail_first), ("eval", "spy", CLICKS),
             ("click", "#photo-feed .card-save")]
    if fail_first:
        steps += [("wait", is_open("#save-dialog")), ("click", "#save-again"), ("wait", "!document.querySelector('#save-dialog')")]
    r = browser.drive(live.url + "/", [*steps, ("wait", "window.shared.length === 1"),
        ("eval", "shared", "window.shared"), ("eval", "clicked", "window.clicked.length")], width=390, mobile=True)
    assert not r.get("failed"), r
    assert r["values"]["shared"] == [[[".png", "image/png", True]]] and r["values"]["clicked"] == 0
    assert not problems(r), problems(r)
```

```python
def test_too_big_for_the_phone_is_downloaded_not_shared(browser, live):
    live.add_user("iosbig")
    big = ("eval", "big", """(() => { const real = window.fetch;
      window.fetch = (url, opts) => String(url).startsWith('/api/media/') && !String(url).includes('?')
        ? Promise.resolve(new Response(new Blob([new Uint8Array(8)], { type: 'image/png' }),
            { headers: { 'Content-Type': 'image/png', 'Content-Length': String(200 * 1024 * 1024) } }))
        : real(url, opts); return true; })()""")
    r = browser.drive(live.url + "/", [*photo_screen("iosbig", cards=1), ("wait", done_cards(1)), iphone(), big,
        ("eval", "spy", CLICKS), ("click", "#photo-feed .card-save"), ("wait", "window.clicked.length === 1"),
        ("eval", "shared", "window.shared.length")], width=390, mobile=True)
    assert not r.get("failed"), r
    assert r["values"]["shared"] == 0
```

В `test_photo_viewer_actions_and_release_for_the_admin` проверку `getAttribute('href')` у `#viewer .act-download` заменить на клик с `CLICKS` и ту же проверку `?download=1`.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python -m pytest -q -m integration tests/test_ui_browser.py -k preview_save`
Expected: FAIL (нет `.card-save`).

- [ ] **Step 3: Implement `save.js`** по Interfaces; шапка модуля — почему на iPhone «Поделиться» (в «Фото» молча сохранить нельзя) и зачем окно «Файл готов».

- [ ] **Step 4: Подключить**

- `photo.js` `card()`: у готовой карточки рядом с `.card-open` (не внутри — кнопка в кнопке недопустима) `button.card-save` с `aria-label="Скачать"`, `title="Скачать"`, значок стрелки вниз в линию (новая константа `save` в `ICONS`: `<path d="M12 4v11m-5-5 5 5 5-5M5 20h14"/>`); клик — `event.stopPropagation()` и `saveMedia(item.media_id, root)`.
- `photo.js` просмотр: `a.act-download` → `button` с тем же классом и текстом, клик — `saveMedia(item.media_id, root)`.
- `video.js:187`: `a.card-download` → `button` того же класса, клик — `saveMedia(item.media_id, root)`.
- `app.css`: `.photo-card { position: relative; }` (если ещё нет), `.card-save` — 32px круг в правом верхнем углу (`top: 6px; right: 6px`), `background: rgba(0,0,0,.38)`, `color: #fff`, `backdrop-filter: blur(4px)`, без рамки; `@media (hover: hover) { .card-save { opacity: 0; } .photo-card:hover .card-save, .card-save:focus-visible { opacity: 1; } }`.

- [ ] **Step 5: Run tests**

Run: `.venv/Scripts/python -m pytest -q -m integration tests/test_ui_browser.py -k "preview_save or viewer_actions or video"`
Expected: PASS.

- [ ] **Step 6: Commit** — «Save pictures and videos right from the feed, through the share sheet on iPhone».

---

### Task 4: Смена логина в панели администратора

**Files:**
- Modify: `app/admin.py:39-43,96-128` (`UserPatch.login`, `patch_user`), `app/static/js/admin.js` (действие «Изменить»)
- Test: `tests/test_profile_admin.py`, `tests/test_ui_browser.py`

**Interfaces:**
- Produces: `PATCH /api/admin/users/{uid}` принимает `login: str | None` (≤ 64): `strip().lower()`, `auth.LOGIN_RE` → 422 `{"code": "login"}`; занят (`sqlite3.IntegrityError`) → 409 `{"code": "taken", "message": "такой логин уже есть"}`; сессии не гаснут (они по id); себя переименовать можно.

- [ ] **Step 1: Write the failing tests** (`tests/test_profile_admin.py`)

```python
def test_admin_renames_a_login_and_sessions_survive(studio):
    owner = make_user(studio, "owner", is_admin=True); signin(studio, "owner")
    r = studio.c.patch(f"/api/admin/users/{owner}", json={"login": " example-user "})
    assert r.status_code == 200 and r.json()["login"] == "example-user"
    assert studio.c.get("/api/me").json()["login"] == "example-user"          # своя сессия жива
    assert login(studio, "example-user", "password-123").status_code == 303
    assert login(studio, "owner", "password-123").status_code == 401

def test_rename_refuses_bad_or_taken_logins(studio):
    make_user(studio, "owner", is_admin=True); bob = make_user(studio, "bob"); signin(studio, "owner")
    for bad in ["Иван", "a", "x" * 33, "a b"]:
        r = studio.c.patch(f"/api/admin/users/{bob}", json={"login": bad})
        assert r.status_code == 422 and code(r) == "login", bad
    r = studio.c.patch(f"/api/admin/users/{bob}", json={"login": "OWNER"})
    assert r.status_code == 409 and code(r) == "taken"
    assert studio.c.get("/api/admin/users").json()["items"][1]["login"] == "bob"
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python -m pytest -q tests/test_profile_admin.py -k rename`
Expected: FAIL (поле `login` пропускается).

- [ ] **Step 3: Implement `admin.py`** — поле `login` в `UserPatch`; в `patch_user` проверка до `BEGIN` (сообщение 422 — как у `create_user`); `UPDATE` в той же транзакции, `IntegrityError` → `ROLLBACK` и 409. Журнал — как сейчас: только ключи изменений (`login`), не значения.

- [ ] **Step 4: Implement `admin.js`** — в строке пользователя первая кнопка `button('Изменить', { class: 'btn btn-sm user-edit' })` → окно `#user-edit-dialog` (поля «Логин» `name=login`, «Имя» `name=name`, заполнены текущими; кнопка `#user-edit-save`); `PATCH {login, name}`; отказ — `showError(form, err, USER_ERRORS, 'form')`; успех: своя строка (`user.id === me.id`) → `location.reload()` (меню аватара берёт логин из `/api/me`), чужая → `reload()` таблицы. Шапку модуля дополнить.

Браузерный сценарий в `tests/test_ui_browser.py`:
```python
def test_admin_renames_a_user_in_the_panel(browser, live):
    live.add_user("chief", is_admin=True); live.add_user("olduser")
    dialog = "#user-edit-dialog"
    r = browser.drive(live.url + "/", [*signin("chief"), ("wait", READY), *go("admin"),
        ("wait", "document.querySelector('tr[data-login=olduser] .user-edit')"),
        ("click", "tr[data-login=olduser] .user-edit"), ("wait", is_open(dialog)),
        set_value(f"{dialog} [name=login]", "newuser"), ("click", "#user-edit-save"),
        ("wait", "document.querySelector('tr[data-login=newuser]')")])
    assert not r.get("failed"), r
    assert not problems(r), problems(r)
```

- [ ] **Step 5: Run tests**

Run: `.venv/Scripts/python -m pytest -q tests/test_profile_admin.py` и `.venv/Scripts/python -m pytest -q -m integration tests/test_ui_browser.py -k "renames or admin"`
Expected: PASS.

- [ ] **Step 6: Commit** — «Let the admin change a login, their own included».

---

### Task 5: Цепочка промптов — служба

**Files:**
- Create: `app/chains.py`
- Modify: `app/db.py` (`VERSION = 3`, `SCHEMA`, `STEPS[3]`), `app/images.py:176-182,301-308` (`_out`, вставка), `app/videos.py:86-92,280-286` (`_out`, вставка)
- Test: `tests/test_chains.py` (новый), `tests/test_foundation.py:33,47`

**Interfaces:**
- Produces (`app/chains.py`):
  - `MAX_DEPTH = 50`
  - шаг — `dict`: `kind` (`"upload" | "generation" | "edit" | "brush" | "video"`), необязательные `image_id`, `video_id`, `media_id`, `model`, `prompt`, `size`, `seed`, `strength`, `created`, а у ролика ещё `seconds`, `quality`, `prompt_source`; в ответах API — ещё `gone: bool` у шагов с `image_id`/`video_id`.
  - `image_step(*, image_id: str, kind: str, model: str, prompt: str, size: str, seed: int, strength: float | None, created: float) -> dict`
  - `video_step(*, video_id: str, prompt: str, prompt_source: str | None, seconds: int, quality: str, created: float) -> dict`
  - `of_source(user_id: int, media_id: str | None) -> list[dict]` — `None` → `[]`; есть строка `images` этого пользователя с `media_id = ?` → её цепочка (`of_image`); иначе `[{"kind": "upload", "media_id": media_id}]`.
  - `of_image(user_id: int, row) -> list[dict]` — сохранённая (`json.loads(row["chain"])`) или, если `NULL`, собранная по ссылкам: `of_source(row["source_media_id"]) + [свой шаг из строки]` с глубиной ≤ `MAX_DEPTH` и защитой от круга (посещённые id); у последнего шага `media_id = row["media_id"]`, `seed = row["seed"]` (шлюз мог вернуть другой seed).
  - `of_video(user_id: int, row) -> list[dict]` — так же для `videos`.
  - `annotate(user_id: int, chain: list[dict]) -> list[dict]` — копия: `gone` по наличию строки (`images`/`videos` с этим id и `user_id`), `media_id` убирается, если строки `media` этого пользователя нет.
- Produces (API): у `GET /api/images[/{id}]` и `GET /api/videos[/{id}]` поле `chain` = `annotate(of_image|of_video(...))`.
- Вид шага картинки при создании: `brush` — есть маска (`mask_id`), `edit` — есть исходник без маски, иначе `generation`.

- [ ] **Step 1: Write the failing tests** — `tests/test_chains.py`; хелперы — импортом `from test_images import start, wait_state, photo, png`, прочее — `from conftest import T0, add_gateway, make_user`, модули `chains`, `media`, `state`, `json`, `logging`, `PIL.Image`.

```python
def kinds(item): return [s["kind"] for s in item["chain"]]

def test_generation_is_one_step_with_its_own_picture(studio):
    add_gateway(studio)
    item = wait_state(studio, start(studio, prompt="кот", seed=7).json()["id"], {"done"})
    step, = item["chain"]
    assert step["kind"] == "generation" and step["image_id"] == item["id"] and step["prompt"] == "кот"
    assert step["media_id"] == item["media_id"] and step["model"] == "lustify" and step["gone"] is False

def test_edits_add_steps_and_link_the_pictures(studio):
    add_gateway(studio)
    gen = wait_state(studio, start(studio, prompt="кот").json()["id"], {"done"})
    edit = wait_state(studio, start(studio, prompt="ярче", size="original", model="flux",
                                    source_media_id=gen["media_id"]).json()["id"], {"done"})
    mask = Image.new("L", (64, 48), 0); mask.paste(255, (0, 0, 16, 16))
    r = studio.c.post("/api/images", data={"prompt": "красное платье", "size": "original", "model": "flux",
                                           "source_media_id": edit["media_id"]},
                      files={"mask": ("mask.png", png(mask), "image/png")})
    brush = wait_state(studio, r.json()["id"], {"done"})
    assert kinds(brush) == ["generation", "edit", "brush"]
    assert [s["prompt"] for s in brush["chain"]] == ["кот", "ярче", "красное платье"]
    assert [s["media_id"] for s in brush["chain"]] == [gen["media_id"], edit["media_id"], brush["media_id"]]

def test_deleted_middle_keeps_its_prompt_and_reads_gone(studio):
    add_gateway(studio)
    gen = wait_state(studio, start(studio, prompt="кот").json()["id"], {"done"})
    edit = wait_state(studio, start(studio, prompt="ярче", size="original", model="flux",
                                    source_media_id=gen["media_id"]).json()["id"], {"done"})
    last = wait_state(studio, start(studio, prompt="ещё", size="original", model="flux",
                                    source_media_id=edit["media_id"]).json()["id"], {"done"})
    assert studio.c.delete(f"/api/images/{edit['id']}").status_code == 204
    chain = studio.c.get(f"/api/images/{last['id']}").json()["chain"]
    assert [s["prompt"] for s in chain] == ["кот", "ярче", "ещё"] and [s["gone"] for s in chain] == [False, True, False]

def test_photo_from_the_device_starts_with_an_upload_step(studio):
    add_gateway(studio)
    r = studio.c.post("/api/images", data={"prompt": "x", "size": "square", "model": "flux"},
                      files={"image": ("p.png", photo(), "image/png")})
    item = wait_state(studio, r.json()["id"], {"done"})
    assert kinds(item) == ["upload", "edit"] and item["chain"][0]["media_id"] == item["source_media_id"]
    assert "prompt" not in item["chain"][0]

def test_repeat_keeps_the_parents_chain(studio):
    add_gateway(studio)
    gen = wait_state(studio, start(studio, prompt="кот").json()["id"], {"done"})
    body = dict(prompt="ярче", size="original", model="flux", source_media_id=gen["media_id"])
    wait_state(studio, start(studio, **body).json()["id"], {"done"})
    again = wait_state(studio, start(studio, **body).json()["id"], {"done"})
    assert kinds(again) == ["generation", "edit"]

def test_video_chain_ends_with_its_prompt(studio):
    add_gateway(studio)
    gen = wait_state(studio, start(studio, prompt="кот").json()["id"], {"done"})
    video = studio.c.post("/api/videos", data={"prompt": "поворачивает голову"},
                          files={"source_media_id": (None, gen["media_id"])}).json()
    assert kinds(video) == ["generation", "video"] and video["chain"][-1]["prompt"] == "поворачивает голову"
    assert video["chain"][0]["media_id"] == gen["media_id"]

def test_old_rows_get_a_chain_from_their_links(studio):
    add_gateway(studio)
    uid = state.DB.execute("SELECT id FROM users").fetchone()[0]
    a = media.save(uid, png(Image.new("RGB", (8, 8))), "image/png", "a.png")
    b = media.save(uid, png(Image.new("RGB", (8, 8))), "image/png", "b.png")
    for iid, kind, src, mid in (("ia", "generation", None, a), ("ib", "edit", a, b)):
        state.DB.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, source_media_id, media_id,"
                         " state, detail, created) VALUES (?, ?, ?, 'lustify', ?, '8x8', 1, ?, ?, 'done', 'готово', ?)",
                         (iid, uid, kind, "p-" + iid, src, mid, T0))
    assert kinds(studio.c.get("/api/images/ib").json()) == ["generation", "edit"]

def test_old_rows_linked_in_a_circle_still_answer(studio):
    add_gateway(studio)
    uid = state.DB.execute("SELECT id FROM users").fetchone()[0]
    a = media.save(uid, png(Image.new("RGB", (8, 8))), "image/png", "a.png")
    b = media.save(uid, png(Image.new("RGB", (8, 8))), "image/png", "b.png")
    for iid, src, mid in (("ia", b, a), ("ib", a, b)):
        state.DB.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, source_media_id, media_id,"
                         " state, detail, created) VALUES (?, ?, 'edit', 'flux', 'p', '8x8', 1, ?, ?, 'done', 'готово', ?)",
                         (iid, uid, src, mid, T0))
    r = studio.c.get("/api/images/ib")
    assert r.status_code == 200 and 2 <= len(r.json()["chain"]) <= chains.MAX_DEPTH + 1

def test_a_foreign_source_leaks_nothing(studio):
    add_gateway(studio)
    gen = wait_state(studio, start(studio, prompt="чужой секрет").json()["id"], {"done"})
    make_user(studio, "bob")
    bob = state.DB.execute("SELECT id FROM users WHERE login = 'bob'").fetchone()[0]
    state.DB.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, source_media_id, media_id,"
                     " state, detail, created) VALUES ('ix', ?, 'edit', 'flux', 'своё', '8x8', 1, ?, NULL, 'done', 'готово', ?)",
                     (bob, gen["media_id"], T0))
    chain = chains.annotate(bob, chains.of_image(bob, state.DB.execute("SELECT * FROM images WHERE id = 'ix'").fetchone()))
    assert chain[0] == {"kind": "upload"} and "чужой секрет" not in json.dumps(chain, ensure_ascii=False)

def test_chain_prompts_never_reach_the_log(studio, caplog):
    caplog.set_level(logging.DEBUG)
    add_gateway(studio)
    gen = wait_state(studio, start(studio, prompt="тайный промпт цепочки").json()["id"], {"done"})
    wait_state(studio, start(studio, prompt="тайная правка", size="original", model="flux",
                             source_media_id=gen["media_id"]).json()["id"], {"done"})
    assert "тайный промпт цепочки" not in caplog.text and "тайная правка" not in caplog.text
```

`tests/test_foundation.py`: `db.VERSION == 3`; новый тест по образцу `test_migrate_brings_a_version_1_base_up_keeping_its_rows` — база версии 2 (`ALTER TABLE images DROP COLUMN chain; ALTER TABLE videos DROP COLUMN chain; PRAGMA user_version = 2;`) после `migrate` — версия 3, строка `images` на месте, `chain IS NULL`; в прежнем тесте ожидать версию 3.

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/Scripts/python -m pytest -q tests/test_chains.py tests/test_foundation.py`
Expected: FAIL (`chains` нет, `chain` в ответах нет, версия 2).

- [ ] **Step 3: `db.py`** — `VERSION = 3`; в `SCHEMA` у `images` и `videos` столбец `chain TEXT`; `STEPS[3] = "ALTER TABLE images ADD COLUMN chain TEXT;\nALTER TABLE videos ADD COLUMN chain TEXT;"`; докстринг — «версия 3».

- [ ] **Step 4: `chains.py`** по Interfaces. Шапка модуля: цепочка — снимок на момент создания (удалённая промежуточная картинка не уносит промпт), старые строки собираются по ссылкам, чужое не попадает (только строки этого `user_id`), в журнал ничего. Только синхронное чтение базы — вызывается с цикла событий без `await` между проверкой исходника и `INSERT`.

- [ ] **Step 5: Подключить** — `images.create_job`: вычислить `chain = chains.of_source(uid, source_id) + [chains.image_step(...)]` сразу перед `INSERT` (после проверок `media.alive`, без `await` между ними) и записать `json.dumps(chain, ensure_ascii=False)` в новый столбец; `images._out` → `"chain": chains.annotate(row["user_id"], chains.of_image(row["user_id"], row))`. `videos.create_video` и `videos._out` — так же (`video_step`, `of_video`).

- [ ] **Step 6: Run tests**

Run: `.venv/Scripts/python -m pytest -q tests/test_chains.py tests/test_foundation.py tests/test_images.py tests/test_videos.py tests/test_deploy.py`
Expected: PASS. В `test_videos.py` `VIDEO_OUT` получает `"chain"`, а сверка значений в `test_video_is_created_as_gateway_multipart_without_private_names` (строка 80) исключает и его: `VIDEO_OUT - {"id", "source_media_id", "chain"}`.

- [ ] **Step 7: Commit** — «Keep the chain of prompts that made each picture and video».

---

### Task 6: Цепочка промптов — просмотр картинки и карточка ролика

**Files:**
- Create: `app/static/js/chain.js`
- Modify: `app/static/js/photo.js:361-410` (просмотр), `app/static/js/video.js:177-215` (карточка), `app/static/css/app.css` (`.viewer-chain`)
- Test: `tests/test_ui_browser.py`

**Interfaces:**
- Consumes: `item.chain` (задача 5).
- Produces: `export function chainList(chain: object[], { current: string | null, onOpen: (imageId: string) => void }): HTMLOListElement` — `ol.viewer-chain`, на шаг `li.chain-step[data-kind]` (`aria-current="true"` и класс `now` у шага с `image_id === current` или у последнего), внутри: миниатюра (`img` с `/api/media/<media_id>?thumb=1`, если `media_id` есть; иначе `span.chain-thumb.gone`), `p.chain-meta` — «N. Генерация · Lustify · 832×1216 · seed 41877 · 19 окт» (вид: Генерация / Фото с устройства / Правка / Правка кистью / Ролик; ролик — «5 с · 576p»; дата `toLocaleDateString('ru-RU', { day: 'numeric', month: 'short' })`), `p.chain-prompt` (у ролика пустой промпт — «придуман по фото»), `button.chain-copy` «Копировать» (`navigator.clipboard.writeText`; нет буфера — выделить текст промпта); у `gone` — подпись «удалена» и миниатюра не нажимается; у прочих шагов с `image_id`, кроме текущего, миниатюра — кнопка `onOpen(image_id)`.

- [ ] **Step 1: Write the failing test** (`tests/test_ui_browser.py`)

```python
def test_viewer_shows_the_chain_and_opens_a_step(browser, live):
    login = "chainer"
    live.add_user(login)
    make_chain = ("eval", "chain", f"""(async () => {{ const m = {API};
      const wait = async (id) => {{ for (let t = 0; t < 200; t++) {{ const i = await m.api('GET', '/api/images/' + id);
        if (i.state === 'done') return i; await new Promise(r => setTimeout(r, 30)); }} }};
      const gen = await wait((await m.api('POST', '/api/images', {{ prompt: 'кот', size: 'square', seed: 5 }})).id);
      await wait((await m.api('POST', '/api/images', {{ prompt: 'ярче', size: 'original', model: 'flux',
        source_media_id: gen.media_id }})).id); return true; }})()""")
    steps = "[...document.querySelectorAll('#viewer .chain-step')]"
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), make_chain, *go("photo"),
        ("wait", FEED_READY), ("wait", done_cards(2)),
        ("click", NEWEST_DONE), ("wait", is_open("#viewer")),
        ("eval", "prompts", f"{steps}.map(s => s.querySelector('.chain-prompt').textContent)"),
        ("eval", "now", f"{steps}.map(s => s.getAttribute('aria-current'))"),
        ("eval", "meta", f"{steps}[0].querySelector('.chain-meta').textContent"),
        ("click", "#viewer .chain-step:first-child .chain-open"),
        ("wait", "document.querySelectorAll('#viewer .chain-step').length === 1")])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["prompts"] == ["кот", "ярче"] and v["now"] == [None, "true"]
    assert v["meta"].startswith("1. Генерация · Lustify · 1024×1024 · seed 5")
    assert not problems(r), problems(r)
```

Прежние проверки `.viewer-meta` и `.viewer-prompt` (`test_photo_viewer_actions_and_release_for_the_admin`) перевести на `.chain-meta` / `.chain-prompt` последнего шага.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/Scripts/python -m pytest -q -m integration tests/test_ui_browser.py -k chain`
Expected: FAIL.

- [ ] **Step 3: Implement**

- `chain.js` по Interfaces (кнопка миниатюры — класс `chain-open`).
- `photo.js` `openViewer`: `viewer-meta` и `viewer-prompt` заменить на `chainList(item.chain, { current: item.id, onOpen })`; `onOpen(id)` — `GET /api/images/<id>`, закрыть текущий просмотр, `openViewer(новая)`; 404 — строка ошибки под цепочкой «картинка удалена».
- `video.js` `card()`: при `item.chain.length > 1` кнопка `button.card-chain` «Промпты» → `openDialog(root, { id: 'chain-dialog', title: 'Как получен ролик', body: [chainList(item.chain, { current: null, onOpen: (id) => ctx.navigate('photo') })], cancelText: 'Закрыть' })`.
- `app.css`: `.viewer-chain` — список без маркеров, `max-height: 38vh; overflow-y: auto`; шаг — сетка `44px 1fr auto`, миниатюра 44×58 `object-fit: cover`, `.now` — фон `#303030` (просмотр всегда тёмный), `.chain-meta` — `#b5b5b5` 12px, `.chain-prompt` — 14px, `overflow-wrap: anywhere`.

- [ ] **Step 4: Run tests**

Run: `.venv/Scripts/python -m pytest -q -m integration tests/test_ui_browser.py -k "chain or viewer or video"`
Expected: PASS.

- [ ] **Step 5: Commit** — «Show the chain of prompts in the viewer and on video cards».

---

### Task 7: Документы, выкладка и живая проверка

**Files:**
- Modify: `README.md` (тема — кнопкой в рельсе; «Скачать» на превью; цепочка; смена логина; иконка и `tools/make_icons.py`), `CLAUDE.md` (устройство: `chains.py`, `tools/make_icons.py`, корневые пути иконок в списке анонимных), `IDEAS.md` (идея «перевести промпт на английский» — шапка по правилам, если файла нет)

- [ ] **Step 1: Документы** — правки по списку; анонимные пути в `CLAUDE.md` («Анонимны только …») дополнить `/apple-touch-icon.png`, `/apple-touch-icon-precomposed.png`, `/favicon.ico`. В `IDEAS.md` запись:

```
## 2026-10-20 · Кнопка «перевести промпт на английский» [P3]
**Context:** брейншторм голосового ввода (спека 2026-10-20-studio-sections-voice-design)
**What:** владелец пишет и диктует промпты по-русски, а Lustify (SDXL) понимает английские заметно лучше.
**Proposal:** кнопка у поля фото: gemma-abl переводит промпт на английский в поле, без отправки.
**Status:** proposed
```

- [ ] **Step 2: Полный быстрый набор**

Run: `.venv/Scripts/python -m pytest -q`
Expected: PASS, ≤ 60 с.

- [ ] **Step 3: Браузерный набор**

Run: `.venv/Scripts/python -m pytest -q -m integration tests/test_ui_browser.py`
Expected: PASS.

- [ ] **Step 4: Commit** — «Document the icon, theme button, previews download, login rename and prompt chain».

- [ ] **Step 5: Выкладка** — `bash deploy/deploy.sh`; смоук `https://studio.example.com/health` → `{"status":"ok"}`; `curl -sI https://studio.example.com/apple-touch-icon.png` → 200 `image/png`.

- [ ] **Step 6: Владелец** — сообщить: удалить значок студии с экрана «Домой» iPhone и добавить заново («Поделиться» → «На экран Домой»), должна быть новая иконка; переименоваться в панели (`owner` → `example-user`).

- [ ] **Step 7: Vault после переименования** — когда владелец подтвердит вход логином `example-user`: в `C:/work/projects/local-config/secrets/vault.env` строку `STUDIO_ADMIN_LOGIN=owner` заменить на `STUDIO_ADMIN_LOGIN=example-user` (правка одной строки, значение не печатать); проверка — `grep -c "^STUDIO_ADMIN_LOGIN=example-user$" …/vault.env` → `1`.
