"""Оформление в Chromium и WebKit: контраст палитры в обеих темах и в авто-ветке («как на устройстве»),
фокус с клавиатуры, цели касания, выбранное в меню, печать, масштаб и один основной цвет действия.

Проверяется сам CSS студии: элементы с её классами добавляются на страницу оболочки и меряются в браузере."""
from pathlib import Path

import httpx
import pytest

from browser import find_browser
from test_ui_browser import live
from test_ui_webkit import pw

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]


@pytest.fixture(scope="module", params=["chromium", "webkit"])
def engine(request):
    with pw.sync_playwright() as driver:
        if request.param == "chromium":
            executable = find_browser()
            if not executable:
                pytest.skip("нет Chromium/Edge/Chrome")
            browser = driver.chromium.launch(executable_path=executable)
        else:
            if not Path(driver.webkit.executable_path).is_file():
                pytest.skip("нет WebKit: .venv/Scripts/python -m playwright install webkit")
            browser = driver.webkit.launch()
        yield browser
        browser.close()


@pytest.fixture(scope="module")
def cookie(live):
    signed = httpx.post(live.url + "/login", data={"login": "owner", "password": "password-123"})
    assert signed.status_code == 303
    # WebKit не отправляет Secure cookie по HTTP localhost — сессия заголовком, как в test_ui_webkit.py
    return "; ".join(f"{key}={value}" for key, value in signed.cookies.items())


def shell(engine, live, cookie, *, width=1280, height=900, touch=False, scheme="light", init=None,
          route="#/chat", **options):
    """Страница оболочки студии в новом контексте; возвращает (context, page)."""
    context = engine.new_context(viewport={"width": width, "height": height}, is_mobile=touch, has_touch=touch,
                                 color_scheme=scheme, extra_http_headers={"Cookie": cookie}, **options)
    page = context.new_page()
    page.set_default_timeout(10000)
    if init:
        page.add_init_script(init)
    page.goto(live.url + "/" + route)
    page.locator('html[data-ready="1"]').wait_for(state="attached")
    return context, page


def cookie_for(live, login, theme="auto"):
    """Своя учётка теста: выбор акцента и темы не задевает соседние тесты модуля."""
    live.add_user(login, theme=theme)
    signed = httpx.post(live.url + "/login", data={"login": login, "password": "password-123"})
    assert signed.status_code == 303
    return "; ".join(f"{key}={value}" for key, value in signed.cookies.items())


#: Контраст WCAG: цвет переднего плана к фону — по вычисленным браузером цветам (токены и настоящие правила);
#: аргумент — тема или [тема, акцент учётки]
CONTRAST = """arg => {
  const [mode, accent] = Array.isArray(arg) ? arg : [arg, null];
  const root = document.documentElement;
  if (mode === 'auto') delete root.dataset.theme; else root.dataset.theme = mode;
  if (accent) root.dataset.accent = accent; else delete root.dataset.accent;
  const host = document.createElement('div'); host.id = 'probe'; document.body.append(host);
  // [r, g, b, a] в 0…1: rgb(), rgba(), color(srgb r g b / a) (так браузер отдаёт color-mix), transparent
  const parse = c => {
    if (c === 'transparent') return [0, 0, 0, 0];
    const n = c.match(/[\\d.]+/g).map(Number), srgb = c.startsWith('color(');
    return [...n.slice(0, 3).map(x => srgb ? x : x / 255), n.length > 3 ? n[3] : 1];
  };
  // полупрозрачный цвет поверх непрозрачного фона
  const over = (top, under) => { const t = parse(top), u = parse(under);
    return `rgb(${[0, 1, 2].map(i => Math.round((t[i] * t[3] + u[i] * (1 - t[3])) * 255)).join(', ')})`; };
  const opaque = c => parse(c)[3] === 1;
  const lum = c => { const v = parse(c).slice(0, 3)
    .map(x => x <= .04045 ? x / 12.92 : ((x + .055) / 1.055) ** 2.4); return .2126 * v[0] + .7152 * v[1] + .0722 * v[2]; };
  const ratio = (a, b) => { const x = lum(a), y = lum(b); return (Math.max(x, y) + .05) / (Math.min(x, y) + .05); };
  const token = name => { const n = document.createElement('span'); n.style.color = `var(${name})`; host.append(n);
    const c = getComputedStyle(n).color; n.remove(); return c; };
  const out = [];
  const pair = (label, fg, bg, need) => out.push({label, fg, bg, need, ratio: Math.round(ratio(fg, bg) * 100) / 100});
  const surfaces = ['--surface-0', '--surface-1', '--surface-2', '--surface-3', '--surface-side', '--surface-rail'];
  for (const fg of ['--text-1', '--text-2', '--accent', '--danger', '--success', '--warn'])
    for (const bg of surfaces) pair(`${fg} / ${bg}`, token(fg), token(bg), 4.5);
  for (const [fg, bg] of [['--on-accent', '--accent'], ['--on-primary', '--primary'], ['--on-danger', '--danger'],
                          ['--on-record', '--record'], ['--accent', '--accent-soft'], ['--text-1', '--accent-soft']])
    pair(`${fg} / ${bg}`, token(fg), token(bg), 4.5);
  for (const bg of ['--surface-0', '--surface-1', '--surface-2', '--surface-side']) {
    pair(`--field-border / ${bg}`, token('--field-border'), token(bg), 3);
    pair(`--focus / ${bg}`, token('--focus'), token(bg), 3);
  }
  for (const fg of ['--caution', '--warn', '--success', '--danger', '--accent'])
    for (const bg of ['--surface-1', '--surface-2']) pair(`состояние ${fg} / ${bg}`, token(fg), token(bg), 3);
  // настоящие правила: главная кнопка, удаление, запись, поле в блоке, подсветка кода в чате и в diff «Агентов»
  host.innerHTML = `<button class="btn btn-accent">a</button><button class="btn btn-danger solid">d</button>
    <button class="send">s</button><button class="icon-btn mic on">m</button>
    <div class="block"><input class="input" value="x"></div>
    <div class="md"><pre><code class="hljs"></code></pre></div>
    <div class="code-change-lines"><span class="code-change-code hljs"></span></div>`;
  for (const sel of ['.btn-accent', '.btn-danger.solid', '.send', '.mic.on']) {
    const s = getComputedStyle(host.querySelector(sel)); pair(sel, s.color, s.backgroundColor, 4.5);
  }
  const block = getComputedStyle(host.querySelector('.block')).backgroundColor;
  const input = getComputedStyle(host.querySelector('.input'));
  pair('рамка .input / .block', input.borderTopColor, block, 3);
  pair('рамка .input / заливка поля', input.borderTopColor, input.backgroundColor, 3);
  const classes = ['keyword', 'built_in', 'comment', 'name', 'title', 'string', 'number', 'section', 'bullet',
                   'symbol', 'attr', 'meta', 'deletion', 'addition', 'type', 'literal', 'regexp', 'quote'];
  for (const [where, codeSel, bgSel] of [['чат', '.md code', '.md pre'], ['diff', '.code-change-code', '.code-change-lines']]) {
    const code = host.querySelector(codeSel);
    code.innerHTML = 'plain ' + classes.map(c => `<span class="hljs-${c}">${c}</span>`).join(' ');
    let bg = getComputedStyle(host.querySelector(bgSel)).backgroundColor;
    if (!opaque(bg)) bg = over(bg, getComputedStyle(document.body).backgroundColor);
    pair(`${where}: текст кода`, getComputedStyle(code).color, bg, 4.5);
    for (const span of code.querySelectorAll('span')) {
      const s = getComputedStyle(span);
      pair(`${where}: ${span.className}`, s.color, over(s.backgroundColor, bg), 4.5);
    }
  }
  host.remove();
  return {pairs: out, body: getComputedStyle(document.body).backgroundColor, scheme: getComputedStyle(root).colorScheme};
}"""


#: (тема учётки, тема устройства, ожидаемый фон body): явная тема сильнее устройства, «auto» — по устройству
MODES = [("light", "dark", "rgb(251, 250, 247)"), ("dark", "light", "rgb(27, 27, 27)"),
         ("auto", "dark", "rgb(27, 27, 27)"), ("auto", "light", "rgb(251, 250, 247)")]


@pytest.mark.parametrize("mode,scheme,body", MODES)
def test_palette_and_real_controls_have_minimum_contrast(engine, live, cookie, mode, scheme, body):
    context, page = shell(engine, live, cookie, scheme=scheme)
    with context:
        result = page.evaluate(CONTRAST, mode)
    assert result["body"] == body, result["body"]
    failed = [p for p in result["pairs"] if p["ratio"] < p["need"]]
    assert not failed, failed


#: акценты учётки — как profile.ACCENTS и appearance.js ACCENTS (сверяет test_css_tokens.py)
ACCENTS = ["orange", "blue", "violet", "rose"]


@pytest.mark.parametrize("accent", ACCENTS)
def test_every_accent_keeps_the_contrast_in_both_themes_and_the_auto_branch(engine, live, cookie, accent):
    """Акцент меняет --accent/--primary (и --accent-soft) — каждая пара не ниже 4,5:1 (рамка фокуса и состояния — 3:1)
    в светлой, тёмной и обеих ветках «как на устройстве»; кнопки берут цвет акцента."""
    context, page = shell(engine, live, cookie)
    with context:
        for mode, scheme, body in MODES:
            page.emulate_media(color_scheme=scheme)
            result = page.evaluate(CONTRAST, [mode, accent])
            assert result["body"] == body, (mode, scheme, result["body"])
            failed = [p for p in result["pairs"] if p["ratio"] < p["need"]]
            assert not failed, (mode, scheme, failed)
        colors = page.evaluate("""accent => { const root = document.documentElement; root.dataset.theme = 'light';
          const probe = a => { if (a) root.dataset.accent = a; else delete root.dataset.accent;
            const b = document.createElement('button'); b.className = 'btn btn-accent'; document.body.append(b);
            const c = getComputedStyle(b).backgroundColor; b.remove(); return c; };
          return [probe(null), probe(accent)]; }""", accent)
    assert (colors[0] == colors[1]) == (accent == "orange"), colors


def test_appearance_page_saves_the_accent_and_previews_both_themes(engine, live):
    """«Настройки → Оформление»: карточки тем с образцом в своих цветах при любой теме страницы; акцент — радиокнопкой
    (стрелки), сразу на <html>, в учётку и в localStorage для раннего theme.js; «Компактно» — этого устройства."""
    cookie = cookie_for(live, f"accent-{engine.browser_type.name}", theme="dark")
    context, page = shell(engine, live, cookie, width=393, height=852, route="#/settings/appearance")
    with context:
        page.locator("#appearance-accent-orange").wait_for(state="attached")
        cards = page.evaluate("""() => Object.fromEntries([...document.querySelectorAll('.theme-card')].map(c =>
          [c.dataset.preview, getComputedStyle(c.querySelector('.theme-sample')).backgroundColor]))""")
        checked = page.evaluate("[...document.querySelectorAll('.theme-card input, .accent-choice input')]"
                                ".filter(i => i.checked).map(i => i.id)")
        page.locator("label.accent-choice:has(> #appearance-accent-blue)").click()
        page.wait_for_function("() => document.documentElement.dataset.accent === 'blue'")
        page.focus("#appearance-accent-blue")                 # Safari нажатием на подпись фокус не переносит
        page.keyboard.press("ArrowRight")                     # стрелка в группе радиокнопок — следующий акцент
        page.wait_for_function("() => document.documentElement.dataset.accent === 'violet'")
        page.wait_for_function("() => document.querySelector('#appearance-save-status').textContent.includes('сохранено')")
        saved = page.evaluate("fetch('/api/me').then(r => r.json()).then(m => [m.accent, localStorage.getItem('studio-accent')])")
        page.locator("label.segment-option:has(> #appearance-density-compact)").click()
        density = page.evaluate("""() => [document.documentElement.dataset.density, localStorage.getItem('studio-density'),
          getComputedStyle(document.documentElement).getPropertyValue('--h-md').trim()]""")
        fits = page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.reload()
        page.locator('html[data-ready="1"]').wait_for(state="attached")
        kept = page.evaluate("""() => [document.documentElement.dataset.accent, document.documentElement.dataset.density,
          document.querySelector('#appearance-accent-violet')?.checked]""")
    assert cards == {"light": "rgb(251, 250, 247)", "dark": "rgb(27, 27, 27)", "auto": "rgb(251, 250, 247)"}, cards
    assert checked == ["appearance-theme-dark", "appearance-accent-orange"], checked
    assert saved == ["violet", "violet"] and density == ["compact", "compact", "32px"] and fits
    assert kept == ["violet", "compact", True], kept


def test_dark_theme_raises_menus_with_a_lighter_border_not_a_shadow(engine, live, cookie):
    """Тёмная тема: поднятое (меню аватара) — светлее поверхность и светлая рамка второго слоя тени; светлая — тень."""
    context, page = shell(engine, live, cookie)
    with context:
        page.locator("#avatar").click()
        probe = """mode => { document.documentElement.dataset.theme = mode;
          const m = getComputedStyle(document.querySelector('#menu'));
          return [m.boxShadow, m.backgroundColor, getComputedStyle(document.body).backgroundColor]; }"""
        light, dark = page.evaluate(probe, "light"), page.evaluate(probe, "dark")
    assert light[0].startswith("rgba(31, 29, 26, 0.1)") and dark[0].startswith("rgba(0, 0, 0, 0)"), (light, dark)
    assert "rgb(87, 83, 76)" in dark[0] and "rgb(87, 83, 76)" not in light[0], (light, dark)   # --line-raised
    assert dark[1] == "rgb(38, 38, 38)" and dark[2] == "rgb(27, 27, 27)", dark


def test_keyboard_focus_ring_is_visible_on_icon_buttons_and_upload_labels(engine, live, cookie):
    context, page = shell(engine, live, cookie)
    with context:
        page.evaluate("""() => { const host = document.createElement('div'); host.id = 'probe';
          host.style.cssText = 'position:fixed;top:120px;left:120px;display:flex;gap:24px;z-index:99';
          host.innerHTML = `<button class="icon-btn" id="t-icon">i</button>
            <label class="icon-btn attach" id="t-clip">c<input type="file" class="sr" id="t-clip-file"></label>
            <label class="btn btn-accent tr-add" id="t-upload">u<input type="file" class="sr" id="t-upload-file"></label>
            <label class="chip" id="t-chip">p<input type="file" class="sr" id="t-chip-file"></label>`;
          document.body.append(host); }""")
        rings = {}
        for target, ring in [("#t-icon", "#t-icon"), ("#t-clip-file", "#t-clip"), ("#t-upload-file", "#t-upload"),
                             ("#t-chip-file", "#t-chip")]:
            page.keyboard.press("Shift")                      # клавиатурный режим: фокус — :focus-visible
            page.focus(target)
            rings[ring] = page.evaluate("""([target, ring]) => {
              const t = document.querySelector(target), s = getComputedStyle(document.querySelector(ring));
              const alpha = (s.outlineColor.match(/[\\d.]+/g) || []).length === 4 ? Number(s.outlineColor.match(/[\\d.]+/g)[3]) : 1;
              return {focused: document.activeElement === t, visible: t.matches(':focus-visible'),
                style: s.outlineStyle, width: parseFloat(s.outlineWidth), alpha};
            }""", [target, ring])
    for ring, state in rings.items():
        assert state["focused"] and state["visible"], (ring, state)
        assert state["style"] != "none" and state["width"] >= 2 and state["alpha"] > 0, (ring, state)


SELECTED = """() => {
  const host = document.createElement('div'); host.id = 'probe'; document.body.append(host);
  host.innerHTML = `<div class="chip-menu"><button class="chip-option" role="menuitemradio" aria-checked="true">a</button>
    <button class="chip-option" role="menuitemradio" aria-checked="false">b</button></div>
    <div class="model-menu"><button class="menu-option model-option" role="menuitemradio" aria-checked="true">m</button></div>
    <nav class="settings-nav"><a href="#" aria-current="page">s</a><a href="#">t</a></nav>
    <div class="code-tabbar"><div class="code-tab" role="tab" aria-selected="true"><span class="code-tab-title">x</span>
      <button class="code-tab-close">×</button></div><div class="code-tab" role="tab" aria-selected="false">y</div></div>`;
  const after = el => getComputedStyle(el, '::after').content;
  const accent = (() => { const n = document.createElement('span'); n.style.color = 'var(--accent)'; host.append(n);
    return getComputedStyle(n).color; })();
  const [on, off] = host.querySelectorAll('.chip-option');
  const [current, other] = host.querySelectorAll('.settings-nav a');
  const [tab, plain] = host.querySelectorAll('.code-tab');
  const railTab = document.querySelector('#rail .tab[aria-current="page"]');
  const close = host.querySelector('.code-tab-close').getBoundingClientRect();
  const out = {checkOn: after(on), checkOff: after(off), onColor: getComputedStyle(on).color, accent,
    model: after(host.querySelector('.model-option')),
    navMark: getComputedStyle(current).boxShadow !== getComputedStyle(other).boxShadow,
    tabMark: getComputedStyle(tab).boxShadow !== getComputedStyle(plain).boxShadow,
    railMark: railTab ? getComputedStyle(railTab).boxShadow !== 'none' || getComputedStyle(railTab).color === accent : null,
    close: [close.width, close.height]};
  host.remove();
  return out;
}"""


@pytest.mark.parametrize("width", [393, 1280])
def test_selected_items_are_marked_by_more_than_a_background(engine, live, cookie, width):
    context, page = shell(engine, live, cookie, width=width)
    with context:
        v = page.evaluate(SELECTED)
    assert "✓" in v["checkOn"] and v["checkOff"] in ("none", "normal", '""'), v
    assert v["onColor"] == v["accent"] and "✓" in v["model"], v
    assert v["navMark"] and v["tabMark"] and v["railMark"], v
    assert v["close"][0] >= 24 and v["close"][1] >= 24, v             # WCAG 2.5.8: не меньше 24×24


def test_forced_colors_contrast_more_and_print(engine, live, cookie):
    if engine.browser_type.name != "chromium":
        pytest.skip("forced-colors и contrast эмулирует только Chromium")
    probe = """() => { const host = document.createElement('div'); host.id = 'probe'; document.body.append(host);
      host.innerHTML = '<button class="chip-option" aria-checked="true">a</button><button class="chip" aria-pressed="true">b</button>'
        + '<button class="code-context" data-level="red">c</button>';
      const [option, chip, ring] = host.children; const s = e => getComputedStyle(e);
      const out = {option: s(option).outlineStyle, chip: s(chip).outlineStyle, ring: s(ring).forcedColorAdjust};
      host.remove(); return out; }"""
    context, page = shell(engine, live, cookie, forced_colors="active")
    with context:
        forced = page.evaluate(probe)
    assert forced["option"] != "none" and forced["chip"] != "none" and forced["ring"] == "none", forced
    tokens = """() => { const t = n => { const e = document.createElement('span'); e.style.color = `var(${n})`;
      document.body.append(e); const c = getComputedStyle(e).color; e.remove(); return c; };
      return [t('--line'), t('--field-border'), t('--text-2')]; }"""
    context, page = shell(engine, live, cookie, contrast="no-preference")
    with context:
        normal = page.evaluate(tokens)
    context, page = shell(engine, live, cookie, contrast="more")
    with context:
        more = page.evaluate(tokens)
        page.emulate_media(media="print")
        printed = page.evaluate("""() => ({rail: getComputedStyle(document.querySelector('#rail')).display,
          overflow: getComputedStyle(document.body).overflow, height: getComputedStyle(document.body).height,
          view: getComputedStyle(document.querySelector('#view')).overflow})""")
    assert more[0] == more[1] != normal[0] and more[2] != normal[2], (normal, more)
    assert printed["rail"] == "none" and printed["overflow"] == "visible" and printed["view"] == "visible", printed


STATES = """() => {
  const host = document.createElement('div'); host.id = 'probe'; document.body.append(host);
  host.innerHTML = `<span class="code-tab-mark asking"></span><span class="code-tab-mark running"></span>
    <span class="code-project-state asking"></span><span class="code-project-state running"></span>
    <span class="code-project-state unread"></span>
    <ul class="jobs-list"><li class="jobs-row" data-section="code" data-state="asking"></li>
      <li class="jobs-row" data-section="code" data-state="running"></li>
      <li class="jobs-row" data-section="code" data-state="unread"></li></ul>
    <div class="code-project-sessions"><a data-state="asking">a</a><a data-state="unread">u</a></div>
    <input class="input" placeholder="p">`;
  const t = n => { const e = document.createElement('span'); e.style.color = `var(${n})`; host.append(e);
    const c = getComputedStyle(e).color; e.remove(); return c; };
  const q = s => getComputedStyle(host.querySelector(s));
  const out = {warn: t('--warn'), success: t('--success'), accent: t('--accent'), muted: t('--text-2'),
    tab: [q('.code-tab-mark.asking').backgroundColor, q('.code-tab-mark.running').backgroundColor],
    panel: [q('.code-project-state.asking').color, q('.code-project-state.running').color, q('.code-project-state.unread').color],
    jobs: [...host.querySelectorAll('.jobs-row')].map(r => getComputedStyle(r).borderLeftColor),
    sessions: [...host.querySelectorAll('.code-project-sessions a')].map(a => getComputedStyle(a).borderLeftColor),
    placeholder: getComputedStyle(host.querySelector('.input'), '::placeholder').color,
    accentColor: getComputedStyle(document.documentElement).accentColor};
  host.remove(); return out;
}"""


def test_state_colors_share_one_meaning_everywhere(engine, live, cookie):
    """Работает — success, ждёт — warn, не прочитано — accent: во вкладках, панели проектов и «В работе»."""
    context, page = shell(engine, live, cookie)
    with context:
        v = page.evaluate(STATES)
    assert v["tab"] == [v["warn"], v["success"]], v
    assert v["panel"] == [v["warn"], v["success"], v["accent"]], v
    assert v["jobs"] == [v["warn"], v["success"], v["accent"]], v
    assert v["sessions"] == [v["warn"], v["accent"]], v
    assert v["accentColor"] == v["accent"], v
    if engine.browser_type.name == "chromium":                     # ::placeholder в getComputedStyle — у Chromium
        assert v["placeholder"] == v["muted"], v


PRIMARY = """() => {
  const host = document.createElement('div'); host.id = 'probe'; document.body.append(host);
  host.innerHTML = `<button class="btn btn-accent">a</button><button class="send composer-go">s</button>
    <button class="btn code-chat-link code-new-chat">n</button><div class="code-tabbar"><button class="code-tab-add">+</button></div>
    <button class="send composer-go composer-stop">x</button><button class="btn composer-go composer-stop">y</button>`;
  const t = n => { const e = document.createElement('span'); e.style.color = `var(${n})`; host.append(e);
    const c = getComputedStyle(e).color; e.remove(); return c; };
  const s = sel => getComputedStyle(host.querySelector(sel));
  const ok = getComputedStyle(document.querySelector('#lock-pin-ok'));
  const stops = [...host.querySelectorAll('.composer-stop')].map(b => getComputedStyle(b))
    .map(st => [st.backgroundColor, st.color, st.borderTopColor]);
  const out = {primary: t('--primary'), onPrimary: t('--on-primary'),
    fills: ['.btn-accent', '.send:not(.composer-stop)', '.code-new-chat', '.code-tab-add'].map(x => s(x).backgroundColor)
      .concat([ok.backgroundColor]),
    inks: ['.btn-accent', '.send:not(.composer-stop)', '.code-new-chat'].map(x => s(x).color).concat([ok.color]),
    stops};
  host.remove(); return out;
}"""


@pytest.mark.parametrize("mode", ["light", "dark"])
def test_one_primary_action_color_and_one_stop_style(engine, live, cookie, mode):
    context, page = shell(engine, live, cookie)
    with context:
        page.evaluate("mode => { document.documentElement.dataset.theme = mode; }", mode)
        v = page.evaluate(PRIMARY)
    assert set(v["fills"]) == {v["primary"]} and set(v["inks"]) == {v["onPrimary"]}, v
    assert v["stops"][0] == v["stops"][1] and v["stops"][0][0] != v["primary"], v


def test_early_theme_follows_the_saved_choice_without_profile(engine, live, cookie):
    """Профиль не пришёл: строка состояния и подсветка кода — по сохранённой теме, а не по устройству."""
    context = engine.new_context(viewport={"width": 393, "height": 852}, color_scheme="light",
                                 extra_http_headers={"Cookie": cookie})
    with context:
        page = context.new_page()
        page.add_init_script("localStorage.setItem('studio-theme', 'dark')")
        page.route("**/api/me", lambda route: route.abort())
        page.goto(live.url + "/#/chat")
        page.wait_for_load_state("load")
        v = page.evaluate("""() => ({colors: [...document.querySelectorAll('meta[name="theme-color"]')].map(m => m.content),
          paper: document.querySelector('#hl-paper').media, graphite: document.querySelector('#hl-graphite').media,
          body: getComputedStyle(document.body).backgroundColor})""")
    assert v["colors"] == ["#1b1b1b", "#1b1b1b"] and v["paper"] == "not all" and v["graphite"] == "all", v
    assert v["body"] == "rgb(27, 27, 27)", v


def test_windows_and_small_text_follow_interface_scale(engine, live, cookie):
    """Масштаб 50% на узком окне: окно на всю доступную ширину, а не на половину; подписи не мельче 11 px."""
    init = "localStorage.setItem('studio-ui-scale', '50'); localStorage.setItem('studio-font-size', '85')"
    context, page = shell(engine, live, cookie, width=360, height=740, init=init)
    with context:
        v = page.evaluate("""() => {
          const d = document.createElement('dialog'); d.className = 'dialog'; d.textContent = 'текст '.repeat(2000);
          document.body.append(d); d.showModal();
          const r = d.getBoundingClientRect(); d.close(); d.remove();
          const bar = document.createElement('div'); bar.className = 'code-status-bar';
          bar.innerHTML = '<button class="code-cache">5 мин</button>'; document.body.append(bar);
          const small = parseFloat(getComputedStyle(bar.firstChild).fontSize); bar.remove();
          return {zoom: document.documentElement.style.zoom, width: r.width, height: r.height, small,
            innerWidth, innerHeight};
        }""")
    assert v["zoom"] == "0.5", v
    assert v["width"] >= .7 * v["innerWidth"] and v["width"] <= v["innerWidth"], v
    assert v["height"] <= v["innerHeight"] + 1, v
    assert v["small"] >= 11, v
