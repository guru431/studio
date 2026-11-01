"""Оболочка приложения в настоящем браузере (app/static): вход, темы, раскладка, PWA.

Студия — живая (`live_app.py`), шлюз подменён. Браузер — свой на каждый тест: cookie у
браузера общие для всех портов localhost, и вход одного теста достался бы следующему.
"""
import asyncio
import base64
import io
import json
import math
import os
import re
import socket
import threading
import time

import httpx
import pytest
from PIL import Image

from browser import launch
from fakes import PNG_REAL, TOKEN as GW_TOKEN, VIDEO_MODEL, video_object
from live_app import start_live_app

#: срок теста — больше срока одного сценария драйвера (180 с, browser.py), иначе он оборвал бы сценарий раньше
pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

READY = "document.documentElement.dataset.ready === '1'"


@pytest.fixture
def browser(tmp_path_factory):
    yield from launch(tmp_path_factory.mktemp("studio-ui-browser"))


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    app = start_live_app(tmp_path_factory.mktemp("studio-live"))
    yield app
    app.stop()


def signin(login="owner", password="password-123"):
    return [("idle",), ("type", "#login", login), ("type", "#password", password), ("click", "#submit")]


#: сбои среды, а не страницы: WSAENOBUFS Windows (кончились буферы сокетов или эфемерные порты под нагрузкой
#: полного набора) — Chrome пишет его отказом загрузки любого файла
ENV_FAULTS = ("net::ERR_NO_BUFFER_SPACE",)


def problems(r):
    """Ошибки журнала браузера: исключения, console.error, нарушения CSP, отказы загрузки — без сбоев среды."""
    # Панель сначала читает проекты: closed у свежей сессии — штатный переход к POST open, а не сбой страницы.
    def opening(line):
        match = re.search(r"status of 409 .*\((?:https?://[^/]+)?/api/code/agents/(\d+)/projects\)", str(line))
        return match is not None and f"POST /api/code/agents/{match[1]}/open" in r.get("sent", [])

    return [line for line in r.get("log", []) if not any(fault in str(line) for fault in ENV_FAULTS) and not opening(line)]


def test_problems_skip_environment_faults():
    buffer = "network: Failed to load resource: net::ERR_NO_BUFFER_SPACE (http://127.0.0.1/static/icons/icon.svg)"
    real = "network: Failed to load resource: the server responded with a status of 404 (/static/x.js)"
    assert problems({"log": [buffer, real]}) == [real]
    assert problems({}) == []


@pytest.mark.parametrize("width", [390, 1280])
def test_login_shell_and_theme(browser, live, width):
    r = browser.drive(live.url + "/", [*signin(), ("wait", "document.querySelector('#rail')"), ("wait", READY),
        ("eval", "tabsAtBottom", "getComputedStyle(document.querySelector('#rail')).position === 'fixed'"),
        ("eval", "overflow", "document.documentElement.scrollWidth > innerWidth"),
        ("eval", "sw", "navigator.serviceWorker.getRegistration().then(r => !!r)"),
        ("eval", "hash", "location.hash"),
        ("eval", "current", "document.querySelector('#rail a[aria-current=page]').dataset.section"),
        ("eval", "theme", "document.documentElement.hasAttribute('data-theme')")],
        width=width, mobile=width == 390)
    assert not r.get("failed"), r
    assert not r["values"]["overflow"] and r["values"]["sw"]
    assert r["values"]["tabsAtBottom"] == (width == 390)
    assert r["values"]["hash"] == "#/chat" and r["values"]["current"] == "chat"
    assert r["values"]["theme"] is False                       # тема «auto» — по настройке устройства
    assert not problems(r), problems(r)


@pytest.mark.parametrize("theme,bg", [("dark", "rgb(27, 27, 27)"), ("light", "rgb(251, 250, 247)")])
def test_theme_from_profile_is_applied(browser, live, theme, bg):
    login = f"theme-{theme}"
    live.add_user(login, theme=theme)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("eval", "attr", "document.documentElement.dataset.theme"),
        ("eval", "bg", "getComputedStyle(document.body).backgroundColor"),
        ("eval", "stored", "localStorage.getItem('studio-theme')")])
    assert not r.get("failed"), r
    assert r["values"] == {"attr": theme, "bg": bg, "stored": theme}
    assert not problems(r), problems(r)


MENU_OPEN = "!document.querySelector('#menu').hidden"
MENU_INSIDE = ("(() => { const b = document.querySelector('#menu').getBoundingClientRect();"
               " return b.left >= 0 && b.top >= 0 && b.right <= innerWidth && b.bottom <= innerHeight; })()")
MENU_ITEMS = "[...document.querySelectorAll('#menu .menu-item')].filter(e => !e.hidden).map(e => e.textContent.trim())"
THEME_WORDS = ("Тёмная тема", "Светлая тема")


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_theme_toggle_in_the_profile_menu(browser, live, width):
    """Тема — пункт меню профиля сразу под именем (в шапке кнопки нет): подпись — что включит нажатие;
    нажатие меняет тему, пишет её в учётку и оставляет меню открытым; после перезагрузки тема та же."""
    login = f"toggle{width}"
    live.add_user(login)
    expected = "(matchMedia('(prefers-color-scheme: dark)').matches ? 'light' : 'dark')"
    label = "document.querySelector('#theme-toggle').textContent.trim()"
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("eval", "want", expected),
        ("eval", "shownClosed", "document.querySelector('#theme-toggle').getBoundingClientRect().width > 0"),
        ("click", "#avatar"), ("wait", MENU_OPEN),
        ("eval", "place", "(() => { const t = document.querySelector('#theme-toggle');"
                          " return [t.parentElement.id, t.previousElementSibling.className, t.className,"
                          " t.getAttribute('role')]; })()"),
        ("eval", "before", label),
        ("click", "#theme-toggle"), ("wait", f"document.documentElement.dataset.theme === {expected}"),
        ("eval", "stillOpen", MENU_OPEN), ("eval", "after", label),
        ("idle",), ("eval", "saved", "fetch('/api/me').then(r => r.json()).then(m => m.theme)"),
        ("eval", "reload", "location.reload()"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"),
        ("eval", "kept", "document.documentElement.dataset.theme"),
        ("click", "#avatar"), ("wait", MENU_OPEN),
        ("click", "#theme-toggle"), ("wait", f"document.documentElement.dataset.theme !== {expected}"),
        ("eval", "inside", MENU_INSIDE),
        ("eval", "go", "location.hash = '#/profile'"), ("wait", "document.querySelector('#view .page')"),
        ("eval", "profileTheme", "!!document.querySelector('#theme-auto, #theme-dark, #theme-light')")],
        width=width, mobile=width < 760)
    assert not r.get("failed"), r
    v = r["values"]
    words = dict(zip(("dark", "light"), THEME_WORDS))
    other = "light" if v["want"] == "dark" else "dark"
    assert v["shownClosed"] is False
    assert v["place"] == ["menu", "menu-who", "menu-item menu-theme", "menuitem"]
    assert v["before"] == words[v["want"]] and v["after"] == words[other] and v["stillOpen"] is True
    assert v["saved"] == v["want"] and v["kept"] == v["want"] and v["inside"] and v["profileTheme"] is False
    assert not problems(r), problems(r)


def test_wrong_password_shows_words(browser, live):
    r = browser.drive(live.url + "/login", [*signin(password="wrong-password"),
        ("wait", "document.querySelector('.error') && document.querySelector('.error').textContent.length > 0"),
        ("eval", "error", "document.querySelector('.error').textContent"),
        ("eval", "visible", "document.querySelector('.error').getBoundingClientRect().height > 0"),
        ("eval", "path", "location.pathname")], width=390, mobile=True)
    assert not r.get("failed"), r
    assert r["values"]["error"] == "Неверный логин или пароль" and r["values"]["visible"]
    assert r["values"]["path"] == "/login"


@pytest.mark.parametrize("width", [390, 1280])
def test_photo_without_gateway_shows_the_hint(browser, live, width):
    """Раздел без модели открывается с лентой; подсказка со ссылкой на настройки — на месте поля промпта."""
    login = f"nogw{width}"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("eval", "go", "location.hash = '#/photo'"),
        ("wait", "document.querySelector('#view .hint-bar') && document.querySelector('#photo-feed')?.dataset.feed === 'end'"),
        ("eval", "text", "document.querySelector('#view .hint-bar').textContent"),
        ("eval", "link", "document.querySelector('#view .hint-bar a').getAttribute('href')"),
        ("eval", "form", "!!document.querySelector('#photo-composer')"),
        ("eval", "empty", "document.querySelector('#photo-feed .feed-empty').textContent"),
        ("eval", "current", "document.querySelector('#rail a[aria-current=page]').dataset.section"),
        ("eval", "overflow", f"!({NO_OVERFLOW})"),
        ("click", "#view .hint-bar a"),
        ("wait", "location.hash === '#/settings/photo' && document.querySelector('.section-panel[data-section=photo]')")],
        width=width, mobile=width == 390)
    assert not r.get("failed"), r
    v = r["values"]
    assert "Чтобы генерировать фото, выберите модель в настройках раздела «Фото»" in v["text"]
    assert v["link"] == "#/settings/photo" and v["current"] == "photo"
    assert v["form"] is False and v["empty"] == "Здесь появятся ваши картинки." and not v["overflow"]
    assert not problems(r), problems(r)


@pytest.mark.parametrize("width", [390, 1280])
def test_avatar_menu_and_logout(browser, live, width):
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY),
        ("click", "#avatar"), ("wait", MENU_OPEN),
        ("eval", "items", MENU_ITEMS),
        ("eval", "inside", MENU_INSIDE),
        ("press", "Escape"), ("wait", "document.querySelector('#menu').hidden"),
        ("click", "#avatar"), ("click", "#menu-logout"),
        ("wait", "location.pathname === '/login' && document.querySelector('#login')")],
        width=width, mobile=width == 390)
    assert not r.get("failed"), r
    items = r["values"]["items"]
    assert items[0] in THEME_WORDS and items[1:] == ["Настройки", "Панель", "Выход"] and r["values"]["inside"]
    assert not problems(r), problems(r)


MARKDOWN = """(async () => {
  const { renderMarkdown } = await import('/static/js/markdown.js');
  const box = document.createElement('div');
  box.className = 'md';
  box.append(renderMarkdown('# Заголовок\\n\\n[ссылка](https://example.org) <img src=x onerror="window.pwned=1">'
    + '<script>window.pwned=2</script>\\n\\n```python\\ndef f():\\n    return 1\\n```'));
  document.body.append(box);
  await new Promise(r => setTimeout(r, 50));
  const a = box.querySelector('a');
  return { h1: box.querySelector('h1').textContent, target: a.target, rel: a.rel,
           script: box.querySelectorAll('script').length, onerror: !!box.querySelector('[onerror]'),
           pwned: window.pwned || 0, highlighted: !!box.querySelector('pre code.hljs .hljs-keyword'),
           copy: box.querySelector('pre button.copy').textContent };
})()"""

#: HTML из ответа модели (или из подсказки в приложенном PDF): стиль на всю страницу, форма входа
#: на чужую учётку, кнопка на весь экран, чужие id (B1-C1). Всё это — текст, а не разметка.
HOSTILE = """(async () => {
  const { renderMarkdown } = await import('/static/js/markdown.js');
  const box = document.createElement('div');
  box.className = 'md';
  box.append(renderMarkdown('Готово.\\n\\n<style>body{display:none}</style>\\n\\n'
    + '<div style="position:fixed;inset:0" id="chat-input" class="send">накрыло</div>\\n\\n'
    + 'Вход: <form action="/login" method="post"><input type="hidden" name="login" value="mallory">'
    + '<input name="password" value="x"><textarea name="t"></textarea><select name="s"></select>'
    + '<button style="position:fixed;inset:0">войти</button></form> и <img src=x onerror="window.pwned=3">\\n\\n'
    + '- [x] сделано\\n- [ ] нет\\n\\n'
    + '```js\\nconst a = 1;\\n```'));
  document.body.append(box);
  await new Promise(r => setTimeout(r, 50));
  const active = 'style, form, input, button:not(.copy), textarea, select, [style], [id], [name], [onerror]';
  const classes = [...box.querySelectorAll('[class]')].map(e => e.className)
    .filter(c => !/^(language-js hljs language-javascript|hljs-\\w+|copy)$/.test(c));
  const out = { active: [...box.querySelectorAll(active)].map(e => e.outerHTML), classes,
    pwned: window.pwned || 0,
    text: box.textContent, highlighted: !!box.querySelector('pre code.hljs .hljs-keyword'),
    copy: [...box.querySelectorAll('pre button.copy')].map(b => b.textContent) };
  box.remove();
  return out;
})()"""

#: N2: <br> в ячейке таблицы (так модели переносят строку) — перенос, а не текст; <br> с атрибутами
#: и прочие теги рядом остаются текстом.
BR = """(async () => {
  const { renderMarkdown } = await import('/static/js/markdown.js');
  const box = document.createElement('div');
  box.className = 'md';
  box.append(renderMarkdown('| a | b |\\n|---|---|\\n| раз<br>два | три<br/>четыре<BR />пять |\\n'
    + '| <br onclick="window.pwned=4">x | <b>жирно</b><script>window.pwned=5</script> |\\n'));
  document.body.append(box);
  await new Promise(r => setTimeout(r, 50));
  const cells = [...box.querySelectorAll('td')];
  const out = { brs: cells.map(td => td.querySelectorAll('br').length),
    text: cells.map(td => td.textContent),
    active: [...box.querySelectorAll('[onclick], script, b')].map(e => e.outerHTML),
    pwned: window.pwned || 0 };
  box.remove();
  return out;
})()"""

SSE = """(async (base, token) => {
  const { readSSE } = await import('/static/js/sse.js');
  const r = await fetch(base + '/chat/completions', { method: 'POST',
    headers: { 'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json' },
    body: JSON.stringify({ model: 'cloud-chat', stream: true, messages: [{ role: 'user', content: 'привет' }] }) });
  const out = [];
  for await (const item of readSSE(r)) out.push(item.data === '[DONE]' ? '[DONE]' : (item.data.choices[0].delta.content || ''));
  const synthetic = new Response('event: delta\\r\\ndata: {"a":1}\\r\\n\\r\\n: ping\\n\\ndata: tail');
  const parsed = [];
  for await (const item of readSSE(synthetic)) parsed.push(item);
  return { out, parsed };
})"""


def test_markdown_is_sanitized_highlighted_and_sse_reads_a_cloud_stream(browser, live):
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY),
        ("eval", "md", MARKDOWN),
        ("eval", "hostile", HOSTILE),
        ("eval", "br", BR),
        ("eval", "sse", f"{SSE}({live.cloud.base_url!r}, {live.cloud.TOKEN!r})")])
    assert not r.get("failed"), r
    hostile = r["values"]["hostile"]
    assert hostile["active"] == [] and hostile["classes"] == [], hostile
    assert hostile["pwned"] == 0
    assert "<style>body{display:none}</style>" in hostile["text"]          # видно словами
    assert '<form action="/login" method="post">' in hostile["text"]
    assert "☑ сделано" in hostile["text"] and "☐ нет" in hostile["text"]
    assert hostile["highlighted"] and hostile["copy"] == ["Копировать"]
    br = r["values"]["br"]
    assert br["brs"] == [1, 2, 0, 0] and br["active"] == [] and br["pwned"] == 0, br
    assert br["text"][:2] == ["раздва", "тричетырепять"], br
    assert br["text"][2] == '<br onclick="window.pwned=4">x', br
    assert br["text"][3] == "<b>жирно</b><script>window.pwned=5</script>", br
    md = r["values"]["md"]
    assert md == {"h1": "Заголовок", "target": "_blank", "rel": "noopener noreferrer", "script": 0, "onerror": False,
                  "pwned": 0, "highlighted": True, "copy": "Копировать"}
    assert r["values"]["sse"]["out"] == ["Облако ", "на связи.", "", "[DONE]"]
    assert r["values"]["sse"]["parsed"] == [{"event": "delta", "data": {"a": 1}}, {"event": "message", "data": "tail"}]
    # запрос шлёт сам тест (fetch мимо chat.js) — даты первым системным сообщением в нём нет
    assert live.cloud.chat_requests[-1]["messages"] == [{"role": "user", "content": "привет"}]


def test_non_admin_menu_has_no_panel_and_admin_route_falls_back(browser, live):
    live.add_user("plain")
    r = browser.drive(live.url + "/", [*signin("plain"), ("wait", READY),
        ("click", "#avatar"), ("wait", MENU_OPEN),
        ("eval", "items", MENU_ITEMS),
        ("eval", "go", "location.hash = '#/admin'"), ("wait", "location.hash === '#/chat'")])
    assert not r.get("failed"), r
    assert r["values"]["items"][0] in THEME_WORDS and r["values"]["items"][1:] == ["Настройки", "Выход"]


def test_api_passes_the_deadline_through(browser, live):
    """Срок (AbortSignal.timeout отклоняет fetch TimeoutError) и обрыв — как есть, а не «Нет связи с сервером»."""
    call = ("(async () => {{ try {{ await {api}.api('GET', '/api/me', undefined, {{ signal: AbortSignal.abort({why}) }});"
            " return 'ok'; }} catch (e) {{ return [e.name, e.code || null]; }} }})()")
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY),
        ("eval", "timeout", call.format(api=API, why="new DOMException('срок', 'TimeoutError')")),
        ("eval", "abort", call.format(api=API, why=""))])
    assert not r.get("failed"), r
    assert r["values"]["timeout"] == ["TimeoutError", 23]          # DOMException.TIMEOUT_ERR, не ApiError 'network'
    assert r["values"]["abort"] == ["AbortError", 20]


# ---- профиль и панель ----------------------------------------------------------

#: api() страницы — тот же экземпляр, что у оболочки (me с токеном CSRF уже загружен).
API = "(await import('/static/js/api.js'))"
NO_OVERFLOW = "document.documentElement.scrollWidth <= innerWidth"
GATEWAY_URL = "http://10.77.1.15:4000"


def go(section):
    return [("eval", "go", f"location.hash = '#/{section}'")]


def set_value(selector, value):
    """Поле или список: значение целиком (драйвер умеет только дописывать) и событие, как от человека."""
    return ("eval", "set", f"(() => {{ const e = document.querySelector({selector!r}); e.value = {value!r};"
                           " e.dispatchEvent(new Event('input', {bubbles: true}));"
                           " e.dispatchEvent(new Event('change', {bubbles: true})); return e.value; })()")


def choose_first(selector):
    """Список: первый пункт со значением, событие change."""
    return ("eval", "chosen", f"(() => {{ const e = document.querySelector({selector!r});"
                              " e.value = [...e.options].find(o => o.value).value;"
                              " e.dispatchEvent(new Event('change', {bubbles: true})); return e.value; })()")


def is_open(selector):
    """Окно открыто; закрытое окно разделы убирают со страницы, его нет — тоже «закрыто»."""
    return f"!!document.querySelector({selector!r})?.open"


def history_of(name):
    """Число диалогов и картинок вошедшего — под именем `name`."""
    return ("eval", name, f"(async () => {{ const m = {API};"
                          " return [(await m.api('GET', '/api/chats')).items.length,"
                          " (await m.api('GET', '/api/images')).items.length]; })()")


def make_history():
    """Диалог с ответом шлюза и готовая картинка — через API страницы (разделы — задачи 11–12)."""
    return ("eval", "made", f"""(async () => {{
      const m = {API};
      const chat = await m.api('POST', '/api/chats', {{}});
      const stream = await m.api('POST', '/api/chats/' + chat.id + '/turns', {{ text: 'привет' }});
      await stream.text();
      const job = await m.api('POST', '/api/images', {{ prompt: 'кот на окне', size: 'square' }});
      for (let i = 0; i < 200; i++) {{
        const item = (await m.api('GET', '/api/images')).items.find(x => x.id === job.id);
        if (item.state === 'done') return true;
        await new Promise(r => setTimeout(r, 50));
      }}
      return false;
    }})()""")


def known_refusals(log, *codes):
    """Журнал без отказов, которых сценарий добивается сам (браузер пишет их ошибкой загрузки)."""
    return [e for e in log if not any(f"status of {c}" in e for c in codes)]


def test_settings_add_a_cloud_address_to_chat(browser, live):
    live.add_user("cloudy")
    dialog, panel = "#link-dialog", ".section-panel[data-section=chat]"
    r = browser.drive(live.url + "/", [*signin("cloudy"), ("wait", READY), *go("settings/chat"),
        ("wait", "document.querySelector('#link-add')"),
        ("click", "#link-add"), ("wait", is_open(dialog)),
        ("eval", "kinds", f"[...document.querySelectorAll('{dialog} [name=kind] option')].map(o => o.textContent)"),
        ("eval", "placeholder", f"document.querySelector('{dialog} [name=base_url]').placeholder"),
        ("type", f"{dialog} [name=name]", "Облако"),
        ("type", f"{dialog} [name=base_url]", live.cloud.base_url),
        ("type", f"{dialog} [name=token]", live.cloud.TOKEN),
        ("click", "#link-save"),
        ("wait", f"!{is_open(dialog)} && document.querySelector('{panel} .link')"),
        ("eval", "link", f"(() => {{ const c = document.querySelector('{panel} .link');"
                         " return [c.querySelector('.link-name').textContent, c.querySelector('.link-url').textContent,"
                         " c.querySelector('.link-hint .tag').textContent]; })()"),
        ("eval", "chatBefore", f"(async () => {API}.me.capabilities.chat)()"),
        # модели прямого подключения браузер берёт у сервиса сам; отмеченная — сразу по умолчанию
        ("click", f"{panel} .link-refresh"),
        ("wait", f"document.querySelector('{panel} input.model-check[value=cloud-chat]')"),
        ("click", f"{panel} input.model-check[value=cloud-chat]"),
        ("wait", f"document.querySelector('{panel} input.model-default[value=cloud-chat]')?.checked"),
        ("eval", "default", f"(async () => {{ const me = await {API}.loadMe();"
                            " return [me.default_connection_id !== null, me.default_model, me.capabilities.chat]; })()"),
        ("eval", "overflow", f"!({NO_OVERFLOW})"),
        # новый адрес без нового токена — отказ словами под токеном
        ("click", f"{panel} .link-menu"), ("click", f"{panel} .link-edit"), ("wait", is_open("#conn-dialog")),
        ("eval", "tokenEmpty", "document.querySelector('#conn-dialog [name=token]').value === ''"
                               " && document.querySelector('#conn-dialog [name=token]').type === 'password'"),
        set_value("#conn-dialog [name=base_url]", live.cloud.url + "/v2"),
        ("click", "#conn-save"),
        ("wait", "document.querySelector('#conn-dialog .error[data-for=token]').textContent.length > 0"),
        ("eval", "tokenError", "document.querySelector('#conn-dialog .error[data-for=token]').textContent"),
        ("press", "Escape"), ("wait", f"!{is_open('#conn-dialog')}")],
        width=390, mobile=True)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["kinds"] == ["OpenAI-совместимый", "Шлюз srvllm"]
    assert "/v1" in v["placeholder"]
    assert v["link"] == ["Облако", live.cloud.base_url, "из браузера"] and v["chatBefore"] is False
    assert v["default"] == [True, "cloud-chat", True] and not v["overflow"]
    assert v["tokenEmpty"] and v["tokenError"] == "Новый адрес — введите токен заново"
    assert not known_refusals(problems(r), 422), problems(r)


def test_settings_lists_gateway_models_and_changes_the_password(browser, live):
    live.add_user("pwuser")
    with socket.socket() as s:                 # порт, который никто не слушает
        s.bind(("127.0.0.1", 0))
        deaf = f"http://127.0.0.1:{s.getsockname()[1]}/v1"
    panel = ".section-panel[data-section=chat]"
    r = browser.drive(live.url + "/", [*signin("pwuser"), ("wait", READY),
        ("eval", "added", f"""(async () => {{ const m = {API};
          await m.api('POST', '/api/connections', {{ name: 'Шлюз', kind: 'srvllm', base_url: {GATEWAY_URL!r},
            token: {live.gw.TOKEN!r}, sections: ['chat', 'photo'] }});
          await m.api('POST', '/api/connections', {{ name: 'Глухой', kind: 'openai',
            base_url: {deaf!r}, token: 'deaf-key-123', sections: ['chat'] }});
          await m.loadMe(); return true; }})()"""),
        *go("settings/chat"), ("wait", f"document.querySelectorAll('{panel} .link').length === 2"),
        ("eval", "tag", f"document.querySelector('{panel} .link .link-hint .tag').textContent"),
        ("click", f"{panel} .link:first-child .link-refresh"),
        ("wait", f"document.querySelector('{panel} .link:first-child input.model-check[value=gemma4-abl]')"),
        ("eval", "models", f"[...document.querySelectorAll('{panel} .link:first-child input.model-check')]"
                           ".map(c => c.value + ':' + c.checked)"),
        # прямой сервис, до которого браузер не достучался (fetch бросает TypeError, как при CORS)
        ("click", f"{panel} .link:last-child .link-refresh"),
        ("wait", "document.querySelector('.section-status').textContent.includes('CORS')"),
        ("eval", "cors", "document.querySelector('.section-status').textContent"),
        *go("settings/other"), ("wait", "document.querySelector('#pw-current')"),
        ("type", "#pw-current", "wrong-password"), ("type", "#pw-new", "new-password-1"),
        ("type", "#pw-repeat", "new-password-2"), ("click", "#password-save"),
        ("wait", "document.querySelector('#password-status').textContent.length > 0"),
        ("eval", "mismatch", "document.querySelector('#password-status').textContent"),
        set_value("#pw-repeat", "new-password-1"), ("click", "#password-save"),
        ("wait", "document.querySelector('#password-status').textContent === 'Текущий пароль неверен'"),
        set_value("#pw-current", "password-123"), ("click", "#password-save"),
        ("wait", "document.querySelector('#password-status').textContent.startsWith('Пароль сменён')"),
        ("eval", "cleared", "document.querySelector('#pw-current').value + document.querySelector('#pw-new').value")],
        width=1280)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["tag"] == "через сервер"
    assert v["models"] == ["local-gemma-abl:true", "gemma4-abl:false"]
    assert v["cors"] == "Сервис не разрешает запросы из браузера (CORS) — подключите его через сервер"
    assert v["mismatch"] == "Пароли не совпадают" and v["cleared"] == ""
    refused = [e for e in known_refusals(problems(r), 403) if "ERR_CONNECTION_REFUSED" not in e]
    assert not refused, problems(r)


def test_settings_exa_key_is_saved_and_never_shown(browser, live):
    """«Прочее» → «Интернет»: ключ Exa сохраняется, поле после этого пустое, показан только признак «Ключ
    задан» (и после перезагрузки); ключа нет ни в DOM, ни в хранилищах; «Убрать» — «Ключа нет»."""
    live.add_user("exauser")
    secret = "exa-secret-key-0123456789"
    status = "document.querySelector('#exa-status').textContent"
    r = browser.drive(live.url + "/", [*signin("exauser"), ("wait", READY),
        *go("settings/other"), ("wait", f"document.querySelector('#exa-key') && {status}"),
        ("eval", "empty", f"[{status}, document.querySelector('#exa-remove').disabled,"
                          " document.querySelector('#exa-key').type, document.querySelector('#exa-key').autocomplete,"
                          " [...document.querySelectorAll('.exa-block a')].map(a => [a.href, a.target, a.rel])]"),
        ("type", "#exa-key", secret), ("click", "#exa-save"),
        ("wait", f"{status} === 'Ключ задан'"),
        ("eval", "field", "document.querySelector('#exa-key').value"),
        ("eval", "traces", f"({TOKEN_TRACES})([{secret!r}])"),
        ("eval", "reload", "location.reload()"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"),
        ("wait", f"document.querySelector('#exa-status')?.textContent === 'Ключ задан'"),
        ("eval", "me", f"(async () => (await {API}.api('GET', '/api/me')).exa_key)()"),
        ("click", "#exa-remove"), ("wait", f"{status} === 'Ключа нет'"),
        ("eval", "gone", f"(async () => (await {API}.api('GET', '/api/me')).exa_key)()")], width=1280)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["empty"] == ["Ключа нет", True, "password", "off",
                          [["https://dashboard.exa.ai/api-keys", "_blank", "noopener noreferrer"]]]
    assert v["field"] == "" and v["traces"] == [] and v["me"] is True and v["gone"] is False
    assert not problems(r), problems(r)


def spent(gw):
    """Сколько шлюз нарисовал, освободил, озвучил, ответил и сколько раз его спросили о роликах."""
    return (len(gw.generations), len(gw.releases), len(gw.video_calls), len(gw.descriptions), len(gw.chat_requests),
            len(gw.asr_requests), len(gw.tts_requests))


PASSPORT_STAMP = r"Проверено \d\d\.\d\d \d\d:\d\d"


def test_settings_check_shows_the_gateway_passport(browser, live):
    """Критерий 1 спеки проверки: «Проверить» у srvllm — паспорт под карточкой; ничего не потрачено."""
    live.add_user("checker")
    photo, chat, speech = (f".section-panel[data-section={s}]" for s in ("photo", "chat", "speech"))
    before = spent(live.gw)
    summary = "document.querySelector('{} .passport-summary')?.textContent || ''"
    r = browser.drive(live.url + "/", [*signin("checker"), ("wait", READY), add_gateway(),
        *go("settings/photo"), ("wait", f"document.querySelector('{photo} .link-check')"),
        ("eval", "unchecked", f"!document.querySelector('{photo} .passport')"),
        ("click", f"{photo} .link-check"),
        ("wait", f"document.querySelector('{photo} .passport')?.open && ({summary.format(photo)}).startsWith('Проверено')"),
        ("eval", "photo", summary.format(photo)),
        ("eval", "lines", f"[...document.querySelectorAll('{photo} .passport-lines li')].map(li => li.textContent)"),
        ("eval", "overflow", f"!({NO_OVERFLOW})"),
        *go("settings/chat"), ("wait", f"document.querySelector('{chat} .passport')"),
        ("eval", "chat", summary.format(chat)),
        ("eval", "probes", f"document.querySelectorAll('{chat} .model-probe').length"),
        *go("settings/speech"), ("wait", f"document.querySelector('{speech} .passport')"),
        ("eval", "speech", summary.format(speech))],
        width=390, mobile=True)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["unchecked"] and not v["overflow"]
    assert re.fullmatch(PASSPORT_STAMP + " · адрес отвечает · ключ принят", v["photo"]), v["photo"]
    lines = v["lines"]
    assert re.fullmatch(r"Адрес — отвечает, \d+ мс", lines[0]) and lines[1:4] == [
        "Ключ — принят", "Модели — 3", "Профиль — шлюз srvllm"]
    assert "Lustify: генерация, правка, кисть, сила, seed — встроенный перечень srvllm" in lines
    assert "FLUX: генерация, правка, кисть, seed — встроенный перечень srvllm" in lines
    assert "gemma4-abl: «Описать», «EN» — встроенный перечень srvllm" in lines
    assert re.fullmatch(PASSPORT_STAMP + " · адрес отвечает · ключ принят", v["chat"]), v["chat"]
    assert v["probes"] == 0                                  # у srvllm пробного ответа нет
    assert re.fullmatch(PASSPORT_STAMP + " · не проверяется: проба занимала бы карту", v["speech"]), v["speech"]
    assert spent(live.gw) == before
    assert not problems(r), problems(r)


def test_settings_check_of_a_cloud_runs_in_the_browser(browser, live):
    """Прямое подключение проверяет браузер; «Пробный ответ» — с предупреждением; закрытый порт — слова про CORS."""
    live.add_user("cloudcheck")
    with socket.socket() as s:                 # порт, который никто не слушает
        s.bind(("127.0.0.1", 0))
        deaf = f"http://127.0.0.1:{s.getsockname()[1]}/v1"
    panel = ".section-panel[data-section=chat]"
    cloud, dead = f"{panel} .link:first-child", f"{panel} .link:last-child"
    summary = "document.querySelector('{} .passport-summary')?.textContent || ''"
    asked = len(live.cloud.chat_requests)
    r = browser.drive(live.url + "/", [*signin("cloudcheck"), ("wait", READY),
        ("eval", "added", f"""(async () => {{ const m = {API};
          const c = await m.api('POST', '/api/connections', {{ name: 'Облако', kind: 'openai',
            base_url: {live.cloud.base_url!r}, token: {live.cloud.TOKEN!r}, sections: ['chat'] }});
          await m.api('PUT', `/api/sections/chat/links/${{c.id}}`, {{ models: ['cloud-chat'] }});
          await m.api('POST', '/api/connections', {{ name: 'Глухой', kind: 'openai', base_url: {deaf!r},
            token: 'deaf-key-123', sections: ['chat'] }});
          await m.loadMe(); return true; }})()"""),
        *go("settings/chat"), ("wait", f"document.querySelectorAll('{panel} .link').length === 2"),
        ("click", f"{cloud} .link-check"),
        ("wait", f"({summary.format(cloud)}).startsWith('Проверено')"),
        ("eval", "cloud", summary.format(cloud)),
        ("eval", "lines", f"[...document.querySelectorAll('{cloud} .passport-lines li')].map(li => li.textContent)"),
        ("click", f"{cloud} .model-probe"), ("wait", is_open("#probe-dialog")),
        ("eval", "warning", "document.querySelector('#probe-dialog').textContent"),
        ("click", "#probe-send"),
        ("wait", f"!{is_open('#probe-dialog')} && document.querySelector('{cloud} .passport-probe')"),
        ("eval", "probe", f"document.querySelector('{cloud} .passport-probe').textContent"),
        ("eval", "probeCaps", f"[...document.querySelectorAll('{cloud} .passport-model')].map(li => li.textContent)"),
        ("click", f"{dead} .link-check"),
        ("wait", f"({summary.format(dead)}).includes('CORS')"),
        ("eval", "dead", summary.format(dead)),
        ("eval", "overflow", f"!({NO_OVERFLOW})")],
        width=1280)
    assert not r.get("failed"), r
    v = r["values"]
    assert re.fullmatch(PASSPORT_STAMP + " · адрес отвечает · ключ принят · по словам браузера", v["cloud"]), v["cloud"]
    assert "Модели — 1" in v["lines"] and any(l.startswith("Профиль — OpenAI-совместимый: нет кисти") for l in v["lines"])
    assert "Отправит одно короткое сообщение; у платного сервиса это расход." in v["warning"]
    assert re.fullmatch(r"cloud-chat: пробный ответ — ответила за \d+ мс \(\d\d\.\d\d \d\d:\d\d, по словам браузера\)",
                        v["probe"]), v["probe"]
    assert v["probeCaps"] == ["cloud-chat: отвечает — пробный ответ, по словам браузера"]
    body, = live.cloud.chat_requests[asked:]
    assert body == {"model": "cloud-chat", "messages": [{"role": "user", "content": "Ответь одним словом: ok"}],
                    "max_tokens": 16, "stream": False}
    assert v["dead"].endswith("адрес: нет ответа: сервис недоступен или не разрешает запросы из браузера (CORS)"
                              " · по словам браузера"), v["dead"]
    assert not v["overflow"]
    refused = [e for e in known_refusals(problems(r), 401) if "ERR_CONNECTION_REFUSED" not in e]
    assert not refused, problems(r)


@pytest.mark.parametrize("width", [390, 1280])
def test_settings_photo_unchecks_flux_and_the_chip_keeps_lustify(browser, live, width):
    """Критерий успеха 1 спеки: «Обновить модели» → снять FLUX — в чипе «модель» раздела только Lustify."""
    login = f"sect{width}"
    live.add_user(login)
    panel = ".section-panel[data-section=photo]"
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(),
        *go("settings/photo"), ("wait", f"document.querySelector('{panel} .link')"),
        ("click", f"{panel} .link-refresh"),
        ("wait", f"document.querySelectorAll('{panel} input.model-check').length === 2"
                 " && document.querySelector('.section-status').textContent.startsWith('Найдено')"),
        ("click", f"{panel} input.model-check[value=flux]"),
        ("wait", f"!document.querySelector('{panel} input.model-check[value=flux]').checked"
                 f" && document.querySelector('{panel} input.model-check[value=flux]')"), ("idle",),
        ("eval", "saved", f"(async () => (await {API}.api('GET', '/api/sections')).sections[1].links[0].models)()"),
        ("eval", "overflow", f"!({NO_OVERFLOW})"),
        # в чипе «модель» раздела «Фото» — только Lustify
        *go("photo"), ("wait", FEED_READY), ("click", "#chip-model"),
        ("eval", "options", "[...document.querySelectorAll('#chip-model-menu .chip-option')].map(o => o.textContent)")],
        width=width, mobile=width == 390)
    assert not r.get("failed"), r
    assert r["values"]["saved"] == ["lustify"] and not r["values"]["overflow"]
    assert r["values"]["options"] == ["Lustify"]
    assert not problems(r), problems(r)


@pytest.mark.parametrize("width", [390, 1280])
def test_settings_menu_and_old_profile_route(browser, live, width):
    login = f"menuer{width}"
    live.add_user(login)
    dictation = '.section-panel[data-section=dictation]'
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(),
        ("click", "#avatar"), ("eval", "item", "document.querySelector('#menu a[href=\"#/settings\"]').textContent"),
        ("press", "Escape"),
        ("eval", "go", "location.hash = '#/profile'"), ("wait", "location.hash.startsWith('#/settings')"),
        ("wait", "document.querySelector('.settings-nav a')"),
        ("eval", "nav", "[...document.querySelectorAll('.settings-nav a')].map(a => [a.dataset.section, a.textContent])"),
        *go("settings/transcribe"), ("wait", "document.querySelector('.section-panel[data-section=transcribe] h2')"),
        ("eval", "records", "document.querySelector('.section-panel h2').textContent"),
        *go("settings/ocr"), ("wait", "document.querySelector('.section-panel[data-section=ocr] h2')"),
        ("eval", "scans", "document.querySelector('.section-panel h2').textContent"),
        *go("settings/code"), ("wait", "document.querySelector('#code-defaults-save') && document.querySelector('#agents li')"),
        ("eval", "code", "!!document.querySelector('[data-section=code] #code-defaults-save') && "
                          "!!document.querySelector('[data-section=code] #agents') && !document.querySelector('[data-section=dictation]')"),
        ("eval", "codeFits", NO_OVERFLOW),
        *go("settings/other"), ("wait", f"document.querySelector('{dictation} input.model-default[value=gigaam]')"),
        ("eval", "other", "!document.querySelector('#code-defaults-save, #agents') && !!document.querySelector('#pw-current')"),
        set_value("#pw-current", "unsaved-password"),
        ("eval", "mark", f"document.querySelector('{dictation}').dataset.before = '1'"),
        ("click", f"{dictation} input.model-default[value=whisperx]"),
        ("wait", f"document.querySelector('{dictation}').dataset.before !== '1' && "
                 f"document.querySelector('{dictation} input.model-default[value=whisperx]').checked"),
        ("eval", "draft", "document.querySelector('#pw-current').value"),
        ("eval", "otherFits", NO_OVERFLOW),
        *go("settings/dictation"), ("wait", "document.querySelector('.settings-nav [data-section=other][aria-current=page]')"),
        ("eval", "reload", "location.reload()"), ("wait", READY),
        ("wait", f"document.querySelector('{dictation} input.model-default[value=whisperx]')?.checked"),
        ("eval", "saved", f"document.querySelector('{dictation} input.model-default:checked').value")],
        width=width, mobile=width == 390)
    assert not r.get("failed"), r
    assert r["values"]["item"] == "Настройки"
    v = r["values"]
    assert v["nav"] == [["chat", "Чат"], ["photo", "Фото"], ["video", "Видео"], ["speech", "Речь"],
                        ["transcribe", "Разговоры"], ["ocr", "Сканы"], ["translate", "Перевод"], ["code", "Агенты"], ["other", "Прочее"]]
    assert v["records"] == "Разговоры" and v["scans"] == "Сканы"
    assert v["code"] and v["other"] and v["codeFits"] and v["otherFits"]
    assert v["draft"] == "unsaved-password" and v["saved"] == "whisperx"
    assert not problems(r), problems(r)


def test_admin_creates_a_friend_who_sees_no_owner_history(browser, live):
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY), make_history(), history_of("owner"),
        *go("admin"), ("wait", "document.querySelector('#user-add') && document.querySelector('#users tbody tr')"),
        ("eval", "head", "[...document.querySelectorAll('#users thead th')].map(t => t.textContent)"),
        ("click", "#user-add"), ("wait", is_open("#user-dialog")),
        ("type", "#user-dialog [name=login]", "friend"), ("type", "#user-dialog [name=name]", "Друг"),
        ("type", "#user-dialog [name=password]", "friend-pass-1"),
        ("click", "#user-save"),
        ("wait", "document.querySelector('#users tr[data-login=friend]')"),
        ("eval", "row", "[...document.querySelectorAll('#users tr[data-login=friend] td')].slice(0, 5).map(t => t.textContent)"),
        ("eval", "overflow", f"!({NO_OVERFLOW})"),
        ("click", "#avatar"), ("click", "#menu-logout"),
        ("wait", "location.pathname === '/login' && document.querySelector('#login')"),
        *signin("friend", "friend-pass-1"), ("wait", READY), history_of("friend"),
        ("eval", "menuAdmin", "document.querySelector('#menu-admin').hidden"),
        *go("admin"), ("wait", "location.hash === '#/chat'")])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["made"] is True and v["owner"][0] >= 1 and v["owner"][1] >= 1
    assert v["head"][:5] == ["Логин", "Имя", "Админ", "Отключён", "Подключений"]
    assert v["row"] == ["friend", "Друг", "нет", "нет", "0"]
    assert v["friend"] == [0, 0] and v["menuAdmin"] is True
    assert not problems(r), problems(r)


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


@pytest.mark.parametrize("width", [390, 1280])
def test_admin_manages_users_connections_and_the_allowlist(browser, live, width):
    login = f"mate{width}"
    entry = f"10.77.1.{width % 250}:4000"
    rid = f"#users tr[data-login={login}]"
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY), *go("admin"),
        ("wait", "document.querySelector('#user-add') && document.querySelector('#allow-list li')"),
        ("click", "#user-add"), ("wait", is_open("#user-dialog")),
        ("type", "#user-dialog [name=login]", login), ("type", "#user-dialog [name=name]", "Приятель"),
        ("type", "#user-dialog [name=password]", "short"), ("click", "#user-save"),
        ("wait", "document.querySelector('#user-dialog .error[data-for=password]').textContent.length > 0"),
        ("eval", "short", "document.querySelector('#user-dialog .error[data-for=password]').textContent"),
        ("type", "#user-dialog [name=password]", "-and-long"), ("click", "#user-save"),
        ("wait", f"document.querySelector('{rid}')"),
        # подключения пользователя — те же разделы, что в его настройках
        ("click", f"{rid} .user-conns"), ("wait", is_open("#user-conns-dialog")),
        ("wait", "document.querySelector('#user-conns-dialog #link-add')"),
        ("click", "#user-conns-dialog #link-add"), ("wait", is_open("#link-dialog")),
        ("type", "#link-dialog [name=name]", "Шлюз"),
        set_value("#link-dialog [name=kind]", "srvllm"),
        ("type", "#link-dialog [name=base_url]", GATEWAY_URL + "/v1"),
        ("type", "#link-dialog [name=token]", live.gw.TOKEN),
        ("eval", "tokenType", "document.querySelector('#link-dialog [name=token]').type"),
        ("click", "#link-save"),
        ("wait", "document.querySelector('#link-dialog .error[data-for=base_url]').textContent.length > 0"),
        ("eval", "pathError", "document.querySelector('#link-dialog .error[data-for=base_url]').textContent"),
        set_value("#link-dialog [name=base_url]", GATEWAY_URL), ("click", "#link-save"),
        ("wait", "document.querySelector('#user-conns-dialog .link')"),
        ("eval", "conn", "[document.querySelector('#user-conns-dialog .link-hint .tag').textContent,"
                         " [...document.querySelectorAll('#user-conns-dialog input.model-check')].map(c => c.value)]"),
        # паспорт и проверка чужих подключений панели не даны
        ("eval", "noCheck", "!document.querySelector('#user-conns-dialog .link-check, #user-conns-dialog .passport,"
                            " #user-conns-dialog .model-probe')"),
        ("click", "#user-conns-dialog .dialog-close"), ("wait", "!" + is_open("#user-conns-dialog")),
        ("wait", f"document.querySelector('{rid} td[data-col=connections]').textContent === '1'"),
        # сброс пароля, отключение
        ("click", f"{rid} .user-reset"), ("wait", is_open("#reset-dialog")),
        ("type", "#reset-dialog [name=password]", "brand-new-pass"), ("click", "#reset-save"),
        ("wait", "!" + is_open("#reset-dialog")),
        ("click", f"{rid} .user-toggle"),
        ("wait", f"document.querySelector('{rid} td[data-col=disabled]').textContent === 'да'"),
        ("eval", "toggle", f"document.querySelector('{rid} .user-toggle').textContent"),
        ("eval", "selfDelete", "document.querySelector('#users tr[data-login=owner] .user-delete').disabled"),
        ("eval", "selfReset", "document.querySelector('#users tr[data-login=owner] .user-reset').disabled"),
        # удаление — только с логином
        ("click", f"{rid} .user-delete"), ("wait", is_open("#delete-dialog")),
        ("eval", "deleteLocked", "document.querySelector('#delete-confirm').disabled"),
        ("type", "#delete-dialog [name=confirm]", login),
        ("wait", "!document.querySelector('#delete-confirm').disabled"),
        ("click", "#delete-confirm"), ("wait", f"!document.querySelector('{rid}')"),
        # список разрешённых адресов
        ("type", "#allow-entry", "gw:4000"), ("click", "#allow-add"),
        ("click", "#allowlist-save"),
        ("wait", "document.querySelector('#allow-status').textContent.length > 0"),
        ("eval", "nameRefused", "document.querySelector('#allow-status').textContent"),
        ("click", "#allow-list li[data-entry='gw:4000'] button"),
        ("type", "#allow-entry", entry), ("click", "#allow-add"),
        ("click", "#allowlist-save"),
        ("wait", "document.querySelector('#allow-status').textContent === 'Сохранено'"),
        ("eval", "saved", f"(async () => (await {API}.api('GET', '/api/admin/allowlist')).entries)()"),
        ("click", f"#allow-list li[data-entry='{entry}'] button"), ("click", "#allowlist-save"),
        ("wait", "document.querySelector('#allow-status').textContent === 'Сохранено'"),
        ("eval", "overflow", f"!({NO_OVERFLOW})")],
        width=width, mobile=width == 390)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["short"].startswith("Пароль короче")
    assert v["tokenType"] == "password"
    assert v["pathError"] == "Адрес через сервер — без пути"
    assert v["conn"] == ["через сервер", ["local-gemma-abl"]] and v["noCheck"]
    assert v["toggle"] == "Включить" and v["selfDelete"] is True and v["deleteLocked"] is True
    assert v["selfReset"] is True                  # свой пароль — в «Прочем», с текущим
    assert "gw:4000" in v["nameRefused"] and "не IP:порт и не имя:порт" in v["nameRefused"]
    assert v["saved"] == ["10.77.1.15:4000", entry]
    assert not v["overflow"]
    assert not known_refusals(problems(r), 422), problems(r)


#: шапка раздела на месте и под аватаром, фон непрозрачный: прокрученное под аватар не уходит
HEAD_UNDER_AVATAR = """(() => { const view = document.querySelector('#view'); view.scrollTop = 400;
  const head = document.querySelector('#view > .head').getBoundingClientRect();
  const a = document.querySelector('#avatar').getBoundingClientRect();
  const bg = getComputedStyle(document.querySelector('#view > .head')).backgroundColor;
  return { scrolled: view.scrollTop > 0, top: head.top, covers: a.top >= head.top && a.bottom <= head.bottom,
           opaque: bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent' }; })()"""


def test_phone_avatar_never_covers_scrolled_profile_and_admin(browser, live):
    """B1-M8: на телефоне аватар закреплён, а шапка раздела уезжала с прокруткой — блоки
    профиля и панели проходили под аватаром. Шапка — на месте, с непрозрачным фоном."""
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY),
        *go("settings/other"), ("wait", "document.querySelector('#history-wipe')"),
        ("eval", "profile", HEAD_UNDER_AVATAR),
        *go("admin"), ("wait", "document.querySelector('#users tbody tr') && document.querySelector('#allow-list li')"),
        ("eval", "admin", HEAD_UNDER_AVATAR)], width=390, height=400, mobile=True)
    assert not r.get("failed"), r
    for name in ("profile", "admin"):
        assert r["values"][name] == {"scrolled": True, "top": 0, "covers": True, "opaque": True}, (name, r["values"])
    assert not problems(r), problems(r)


def test_history_wipe_clears_chats_and_feed(browser, live):
    live.add_user("wiper")
    r = browser.drive(live.url + "/", [*signin("wiper"), ("wait", READY),
        ("eval", "added", f"""(async () => {{ const m = {API};
          await m.api('POST', '/api/connections', {{ name: 'Шлюз', kind: 'srvllm', base_url: {GATEWAY_URL!r},
            token: {live.gw.TOKEN!r}, sections: ['chat', 'photo', 'video'] }});
          await m.loadMe(); return true; }})()"""),
        make_history(), history_of("before"),
        *go("settings/other"), ("wait", "document.querySelector('#history-wipe')"),
        ("click", "#history-wipe"), ("wait", is_open("#wipe-dialog")),
        ("eval", "confirmHidden", "document.querySelector('#wipe-confirm').hidden"),
        ("eval", "warning", "document.querySelector('#wipe-dialog p').textContent"),
        ("click", "#wipe-next"), ("wait", "!document.querySelector('#wipe-confirm').hidden"),
        ("click", "#wipe-confirm"),
        # начало: Windows иногда на миг держит свежий файл (антивирус) — он уходит в очередь удаления,
        # и статус добавляет «но файлов на хранилище ещё 1»; на debian открытый файл удаляется сразу
        ("wait", "document.querySelector('#wipe-status').textContent.startsWith('История удалена')"),
        ("eval", "closed", "!" + is_open("#wipe-dialog")),
        history_of("after"),
        ("eval", "conns", f"(async () => (await {API}.api('GET', '/api/connections')).items.length)()")],
        width=390, mobile=True)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["made"] is True and v["before"] == [1, 1]
    assert v["confirmHidden"] is True and v["closed"] is True
    assert v["warning"].endswith(" Чаты Claude Code, Codex и OpenCode хранятся на компьютерах — студия забудет только свои отметки,"
                                 " сами сессии останутся.")
    assert v["after"] == [0, 0] and v["conns"] == 1
    assert not problems(r), problems(r)


def live_sql(live, sql, args=()):
    """Строка в базу живой студии своим соединением (WAL), как LiveApp.add_user."""
    import db
    conn = db.connect(live.db_path)
    try:
        conn.execute(sql, args)
    finally:
        conn.close()


STORAGE_TEXTS = """[...document.querySelectorAll('#storage dd')].map((d) => [d.dataset.kind, d.textContent])"""


def test_storage_block_shows_own_files_and_the_queue_until_the_wipe(browser, live):
    """«Прочее» → «Хранилище»: свои файлы по видам и итог, очередь удаления с датой; после «Удалить всю
    историю» — пусто и «очередь пуста»."""
    uid = live.add_user("keeper")
    for media_id, mime, size in (("k-img", "image/png", 2048), ("k-vid", "video/mp4", 3 << 20)):
        live_sql(live, "INSERT INTO media(id, user_id, rel_path, mime, size, has_thumb, download_name, created)"
                       " VALUES (?, ?, ?, ?, ?, 0, 'x', ?)", (media_id, uid, f"u{uid}/{media_id}", mime, size, time.time()))
    live_sql(live, "INSERT INTO purge(media_id, user_id, rel_path, queued) VALUES ('k-gone', ?, ?, ?)",
             (uid, f"u{uid}/k-gone.png", time.time() - 3600))
    # разделы настроек отвечают позже «Хранилища» (так бывало под нагрузкой): их ответ «Прочее» не перерисовывает —
    # иначе блок заменялся новым, пустым до своего ответа, и строки читались пустыми
    slow = """(() => { const real = window.fetch; window.fetch = (u, o) => String(u) === '/api/sections'
      ? new Promise((r) => setTimeout(r, 600)).then(() => real(u, o)) : real(u, o); return true; })()"""
    r = browser.drive(live.url + "/", [*signin("keeper"), ("wait", READY), ("eval", "slow", slow),
        *go("settings/other"), ("wait", "document.querySelector('#storage dd[data-kind=\"total\"]')"),
        ("eval", "mark", "document.querySelector('#storage').dataset.mark = '1'"),
        ("eval", "same", settled("document.querySelector('#storage').dataset.mark === '1'", 1000)),
        ("eval", "before", STORAGE_TEXTS), ("eval", "queue", "document.querySelector('#storage-queue').textContent"),
        ("eval", "fits", NO_OVERFLOW),
        ("click", "#history-wipe"), ("wait", is_open("#wipe-dialog")), ("click", "#wipe-next"),
        ("wait", "!document.querySelector('#wipe-confirm').hidden"), ("click", "#wipe-confirm"),
        ("wait", "document.querySelector('#storage-queue').textContent.startsWith('Очередь удаления пуста')"),
        ("eval", "after", STORAGE_TEXTS)], width=390, mobile=True)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["same"] is True
    assert v["before"] == [["image", "1 файл · 2 КиБ"], ["video", "1 файл · 3 МиБ"], ["total", "2 файла · 3 МиБ"]]
    assert v["queue"].startswith("Ждут удаления на хранилище: 1 файл (с ") and v["fits"] is True
    assert v["after"] == [["total", "0 файлов · 0 Б"]]
    assert not problems(r), problems(r)


def test_export_dialog_prepares_a_download_link(browser, live):
    """«Хранилище» → «Выгрузить мою историю»: галочки разделов (все отмечены), без них — отказ словами;
    «Подготовить» — ссылка «Скачать: N файлов, X» на ZIP по токену."""
    uid = live.add_user("exporter")
    live_sql(live, "INSERT INTO chats(id, user_id, title, connection_id, model, created, updated)"
                   " VALUES ('exp-chat', ?, 'title', NULL, 'm', ?, ?)", (uid, time.time(), time.time()))
    boxes = "[...document.querySelectorAll('#export-dialog input[name=export-section]')]"
    r = browser.drive(live.url + "/", [*signin("exporter"), ("wait", READY),
        *go("settings/other"), ("wait", "document.querySelector('#export-open')"),
        ("click", "#export-open"), ("wait", is_open("#export-dialog")),
        ("eval", "boxes", f"{boxes}.map((b) => [b.value, b.checked])"),
        ("eval", "labels", f"{boxes}.slice(-2).map((b) => b.closest('label').textContent)"),
        ("eval", "none", f"{boxes}.forEach((b) => {{ b.checked = false; }})"),
        ("click", "#export-prepare"), ("wait", "document.querySelector('#export-error').textContent"),
        ("eval", "empty", "document.querySelector('#export-error').textContent"),
        ("eval", "all", f"{boxes}.forEach((b) => {{ b.checked = true; }})"),
        ("click", "#export-prepare"), ("wait", "document.querySelector('#export-download')"),
        ("eval", "link", "document.querySelector('#export-download').textContent"),
        ("eval", "zip", """(async () => { const r = await fetch(document.querySelector('#export-download').href);
          const head = new Uint8Array(await r.arrayBuffer()).slice(0, 2);
          return [r.status, r.headers.get('content-type'), String.fromCharCode(...head)]; })()"""),
        ("eval", "fits", NO_OVERFLOW)], width=390, mobile=True)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["boxes"] == [[k, True] for k in ("chat", "photo", "video", "speech", "transcribe", "voices", "ocr",
                                              "translate", "code", "agents")]
    assert v["labels"] == ["Агенты: чаты", "Агенты: подключения"]
    assert v["empty"] == "Отметьте хотя бы один раздел"
    assert v["link"] == "Скачать: 0 файлов, 0 Б" and v["zip"] == [200, "application/zip", "PK"]
    assert v["fits"] is True
    assert not problems(r), problems(r)


def test_admin_storage_block_shows_the_share_and_the_backup_age(browser, live):
    folder = live.db_path.parent / "media" / "backup"
    folder.mkdir(exist_ok=True)
    copy = folder / "studio-20270114.db"
    copy.write_bytes(b"db")
    made = time.time() - 3 * 3600 - 60
    os.utime(copy, (made, made))
    try:
        r = browser.drive(live.url + "/", [*signin(), ("wait", READY),
            *go("admin"), ("wait", "document.querySelector('#admin-storage dd[data-key=\"queue\"]')"),
            ("eval", "lines", "[...document.querySelectorAll('#admin-storage dd')].map((d) => [d.dataset.key,"
                              " d.textContent, d.classList.contains('error')])")])
    finally:
        copy.unlink()
    assert not r.get("failed"), r
    share, backup, queue = r["values"]["lines"]
    assert share[0] == "share" and share[1].startswith("доступна, свободно ") and share[2] is False
    assert backup[0] == "backup" and backup[1].startswith("3 ч назад (") and backup[1].endswith("копий — 1")
    assert backup[2] is False
    assert queue[0] == "queue" and queue[1].startswith(("Очередь удаления пуста", "Ждут удаления"))
    assert not problems(r), problems(r)


def test_admin_checks_the_backup(browser, live):
    """«Хранилище» → «Проверить копию»: итог словами, зелёным, — копия живой базы поднимается."""
    import sqlite3
    folder = live.db_path.parent / "media" / "backup"
    folder.mkdir(exist_ok=True)
    copy = folder / "studio-20990101.db"
    src, dst = sqlite3.connect(live.db_path), sqlite3.connect(copy)
    try:
        src.backup(dst)
        dst.execute("PRAGMA journal_mode=DELETE")
    finally:
        dst.close()
        src.close()
    result = "document.querySelector('#admin-backup-result')"
    try:
        r = browser.drive(live.url + "/", [*signin(), ("wait", READY),
            *go("admin"), ("wait", f"{result} && {result}.textContent.startsWith('Ещё не проверялась')"),
            ("click", "#admin-backup-check"),
            ("wait", f"{result}.textContent.startsWith('Копия ')"),
            ("eval", "text", f"{result}.textContent"), ("eval", "ok", f"{result}.classList.contains('ok')"),
            ("eval", "enabled", "!document.querySelector('#admin-backup-check').disabled")])
    finally:
        copy.unlink()
    assert not r.get("failed"), r
    v = r["values"]
    assert re.match(r"Копия \d\d\.\d\d \d\d:\d\d — поднимается: схема ", v["text"]), v["text"]
    assert "целостность в порядке" in v["text"] and "файлов нет на шаре" in v["text"]
    assert v["ok"] is True and v["enabled"] is True
    assert not problems(r), problems(r)


def test_admin_backup_check_start_failure_stays_said(browser, live):
    """Запуск проверки отклонён (не 409): ошибка остаётся словами, кнопка снова доступна, опроса нет —
    иначе он затёр бы её «Ещё не проверялась…» или прошлым итогом зелёным."""
    result = "document.querySelector('#admin-backup-result')"
    stub = """(() => { const real = window.fetch; window.checkGets = 0;
      window.fetch = (u, o = {}) => {
        if (String(u) === '/api/admin/backup-check') {
          if ((o.method || 'GET') === 'GET') window.checkGets += 1;
          else return Promise.resolve(new Response(JSON.stringify({ detail: { code: 'storage',
            message: 'хранилище недоступно' } }), { status: 503, headers: { 'content-type': 'application/json' } }));
        }
        return real(u, o); };
      return true; })()"""
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY),
        # «Ещё не проверялась…» или итог прошлой проверки (служба общая с test_admin_checks_the_backup)
        *go("admin"), ("wait", f"{result} && {result}.textContent"
                               " && !document.querySelector('#admin-backup-check').disabled"),
        ("eval", "stub", stub), ("click", "#admin-backup-check"),
        ("wait", f"{result}.classList.contains('error')"),
        ("eval", "later", f"""new Promise((done) => setTimeout(() => done([{result}.textContent,
          {result}.classList.contains('error'), !document.querySelector('#admin-backup-check').disabled,
          window.checkGets]), 600))""")])
    assert not r.get("failed"), r
    text, error, enabled, gets = r["values"]["later"]
    assert text == "Хранилище недоступно" and error is True and enabled is True and gets == 0, r["values"]


# ---- чат -----------------------------------------------------------------------

CHAT_ID ="/^#\\/chat\\/[0-9a-f]{32}$/.test(location.hash)"
#: ответ кончился: снова «Отправить», у ответа — кнопки
ANSWERED = ("!document.querySelector('#send').hidden && document.querySelector('#stop').hidden"
            " && document.querySelector('#messages .msg.assistant:last-child .msg-actions button')")
LAST_ANSWER = "document.querySelector('#messages .msg.assistant:last-child')"
SAVED = f"""(async () => {{ const m = {API};
  const id = location.hash.split('/')[2];
  const last = (await m.api('GET', '/api/chats/' + id)).messages.at(-1);
  return [last.role, last.status, last.content]; }})()"""


def open_new_chat(mobile=False):
    """Раздел чата и «Новый чат»; на телефоне кнопка — в выдвижной панели."""
    return [*go("chat"), ("wait", "document.querySelector('#chat-input') && document.querySelector('#model-chip')"),
            *([("click", "#chats-toggle")] if mobile else []),
            ("click", "#chat-new"), ("wait", "location.hash === '#/chat' && !document.querySelector('#messages .msg')")]


def add_cloud(cloud, name="Облако"):
    """Облако в разделе «Чат» с отмеченной моделью cloud-chat (у openai при подключении не отмечено ничего)."""
    return ("eval", "cloud", f"""(async () => {{ const m = {API};
      const c = await m.api('POST', '/api/connections', {{ name: {name!r}, kind: 'openai',
        base_url: {cloud.base_url!r}, token: {cloud.TOKEN!r}, sections: ['chat'] }});
      await m.api('PUT', '/api/sections/chat/links/' + c.id, {{ models: ['cloud-chat'] }});
      await m.loadMe(); return c.id; }})()""")


def attach(js_files):
    """Файлы в #attach, как из окна выбора: `js_files` — выражение JS со списком File."""
    return ("eval", "attached", f"""(() => {{ const dt = new DataTransfer();
      for (const f of {js_files}) dt.items.add(f);
      const input = document.querySelector('#attach'); input.files = dt.files; const n = input.files.length;
      input.dispatchEvent(new Event('change', {{ bubbles: true }})); return n; }})()""")


def test_chat_streams_through_the_server_and_survives_reload(browser, live):
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY), *open_new_chat(),
        ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
        ("eval", "conn", "document.querySelector('.chat-conn').textContent"),
        ("type", "#chat-input", "Привет, студия"), ("press", "Enter"),
        ("wait", ANSWERED),
        ("eval", "answer", f"{LAST_ANSWER}.querySelector('.md').textContent.trim()"),
        ("eval", "id", CHAT_ID),
        ("eval", "input", "document.querySelector('#chat-input').value"),
        ("eval", "bubble", "document.querySelector('#messages .msg.user .bubble').textContent"),
        ("wait", "document.querySelector('.chat-item [aria-current=page]')?.textContent === 'Привет, студия'"),
        ("eval", "group", "document.querySelector('.chat-group').textContent"),
        ("eval", "retry", f"!!{LAST_ANSWER}.querySelector('.msg-retry')"),
        ("idle",),
        ("eval", "reload", "location.reload()"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"),
        ("wait", "document.querySelectorAll('#messages .msg').length === 2"),
        ("eval", "after", "[...document.querySelectorAll('#messages .msg')]"
                          ".map(m => m.querySelector('.bubble, .md').textContent.trim())")])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["conn"] == "srvllm" and v["answer"] == "Здравствуйте!" and v["id"] is True
    assert v["input"] == "" and v["bubble"] == "Привет, студия" and v["group"] == "Сегодня" and v["retry"]
    assert v["after"] == ["Привет, студия", "Здравствуйте!"]
    assert live.gw.chat_requests[-1]["messages"][1:] == [{"role": "user", "content": "Привет, студия"}]
    assert not problems(r), problems(r)


def test_chat_with_a_pdf_attachment(browser, live):
    from test_documents import pdf
    data = base64.b64encode(pdf("Payment is due within five days")).decode()
    file = f"[new File([Uint8Array.from(atob({data!r}), c => c.charCodeAt(0))], 'договор.pdf', {{ type: 'application/pdf' }})]"
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY), *open_new_chat(),
        attach(file),
        ("wait", "document.querySelector('#composer .att-chip')"),
        ("eval", "chip", "document.querySelector('#composer .att-chip .att-name').textContent"),
        ("type", "#chat-input", "Какой срок оплаты?"), ("click", "#send"),
        ("wait", ANSWERED),
        ("eval", "sent", "[document.querySelector('#messages .msg.user .att').textContent,"
                         " document.querySelectorAll('#composer .att-chip').length]")])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["attached"] == 1 and v["chip"] == "договор.pdf"
    assert "договор.pdf" in v["sent"][0] and v["sent"][1] == 0
    content = live.gw.chat_requests[-1]["messages"][-1]["content"]
    assert content.startswith('<document name="договор.pdf">') and "Payment is due" in content
    assert content.endswith("Какой срок оплаты?")
    assert not problems(r), problems(r)


WEB_TOGGLE = "document.querySelector('#web-toggle')"
#: ключ Exa учётки через API страницы (сам ключ страница больше нигде не видит) и профиль заново
EXA_KEY_SET = f"""(async () => {{ const m = {API};
  await m.api('PUT', '/api/me/exa-key', {{ key: 'exa-test-key' }}); await m.loadMe(); return true; }})()"""


def test_chat_internet_toggle_search_and_sources(browser, live):
    """«Интернет»: кнопка выключена → включить → ход: плашка «🌐 Ищу: …», ответ, над ним зелёная плашка, под ним
    «Источники» со ссылками в новую вкладку без адреса студии; перезагрузка — кнопка включена (хранится у диалога);
    диалог на прямом подключении — кнопки нет. Результат с адресом `javascript:` или `data:` — текстом, не
    ссылкой, и в число «Источников» (ссылки, а не шаги) не идёт."""
    from fakes import EXA_SEARCH
    live.add_user("webber")
    live.gw.chat_script = [{"tool_calls": [("web_search", json.dumps({"query": "python 3.14"}))]},
                           ["Python 3.14 вышел ", "7 октября."]]
    live.gw.exa_replies["/search"] = (200, {**EXA_SEARCH, "results": [
        *EXA_SEARCH["results"], {"title": "Скрипт", "url": "javascript:alert(document.cookie)"},
        {"title": "Данные", "url": "data:text/html,<script>alert(1)</script>"}]})
    live.gw.chat_delay = 1.5                 # строку хода видно, пока модель молчит после поиска
    searches = len(live.gw.exa_requests)
    try:
        r = browser.drive(live.url + "/", [*signin("webber"), ("wait", READY), add_gateway(),
            ("eval", "exa", EXA_KEY_SET), add_cloud(live.cloud),
            *go("settings"), ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
            ("wait", f"{WEB_TOGGLE} && !{WEB_TOGGLE}.hidden"),
            ("eval", "before", f"[{WEB_TOGGLE}.getAttribute('aria-pressed'), {WEB_TOGGLE}.textContent.trim()]"),
            ("click", "#web-toggle"), ("wait", f"{WEB_TOGGLE}.getAttribute('aria-pressed') === 'true'"),
            ("eval", "hint", "document.querySelector('#web-note').textContent"),
            ("type", "#chat-input", "Что нового в Python?"), ("press", "Enter"),
            ("wait", f"{LAST_ANSWER}?.querySelector('.web-badge.busy')?.textContent.startsWith('🌐 Ищу:')"),
            ("eval", "progress", f"[{LAST_ANSWER}.querySelector('.web-badge').textContent,"
                                 f" {LAST_ANSWER}.querySelector('.msg-meta').textContent]"),
            ("wait", f"{ANSWERED} && {LAST_ANSWER}.querySelector('details.web-steps')"),
            ("eval", "answer", f"{LAST_ANSWER}.querySelector('.md').textContent.trim()"),
            ("eval", "badge", f"[{LAST_ANSWER}.querySelector('.web-badge').textContent,"
                              f" {LAST_ANSWER}.querySelector('.web-badge').className]"),
            ("eval", "summary", f"{LAST_ANSWER}.querySelector('details.web-steps summary').textContent"),
            ("eval", "steps", f"{LAST_ANSWER}.querySelector('details.web-steps li').textContent"),
            ("eval", "links", f"[...{LAST_ANSWER}.querySelectorAll('details.web-steps a')]"
                              ".map(a => [a.href, a.textContent, a.target, a.rel])"),
            ("eval", "found", f"[...{LAST_ANSWER}.querySelectorAll('details.web-steps li li')]"
                              ".map(li => [li.firstChild.tagName, li.firstChild.textContent])"),
            ("eval", "reload", "location.reload()"),
            ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"),
            ("wait", f"document.querySelectorAll('#messages .msg').length === 2 && {WEB_TOGGLE}"),
            ("eval", "kept", f"[{WEB_TOGGLE}.hidden, {WEB_TOGGLE}.getAttribute('aria-pressed'),"
                             f" !!{LAST_ANSWER}.querySelector('details.web-steps')]"),
            # тот же диалог на прямом подключении: инструменты ему не даются — кнопки нет
            ("click", "#model-chip"),
            ("wait", "document.querySelector('.model-option[data-model=\"cloud-chat\"]')"),
            ("click", ".model-option[data-model=\"cloud-chat\"]"),
            ("wait", "document.querySelector('#model-chip').textContent.includes('cloud-chat')"),
            ("wait", f"{WEB_TOGGLE}.hidden"),
            ("eval", "direct", "document.querySelector('#web-note').textContent")])
    finally:
        live.gw.chat_delay = 0
        live.gw.chat_script = []
        live.gw.exa_replies["/search"] = (200, EXA_SEARCH)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["before"] == ["false", "Интернет"] and v["hint"] == ""
    assert v["progress"] == ["🌐 Ищу: python 3.14", ""]
    assert v["answer"] == "Python 3.14 вышел 7 октября."
    assert v["badge"] == ["🌐 Интернет (Exa): 1 поиск", "web-badge ok"]
    assert v["summary"] == "Источники (2)" and "python 3.14" in v["steps"]
    assert v["links"] == [
        ["https://www.python.org/downloads/", "Download Python", "_blank", "noopener noreferrer"],
        ["https://docs.python.org/3/whatsnew/", "What's New in Python", "_blank", "noopener noreferrer"]]
    assert v["found"] == [["A", "Download Python"], ["A", "What's New in Python"], ["SPAN", "Скрипт"],
                          ["SPAN", "Данные"]]
    assert v["kept"] == [False, "true", True] and v["direct"] == ""
    assert [(p, b["query"], k) for p, b, k in live.gw.exa_requests[searches:]] == [("/search", "python 3.14", "exa-test-key")]
    assert not problems(r), problems(r)


BADGE = "document.querySelector('#messages .msg.assistant .web-badge:not([hidden])')"


def test_chat_internet_badge_counts_and_errors(browser, live):
    """Плашка над ответом с шагами: поиск, который Exa отклонил, — красная с числом ошибок; нажатие раскрывает
    «Источники» этого ответа; ответ без шагов — без плашки; после перезагрузки плашка та же (из базы)."""
    live.add_user("badger")
    live.gw.chat_script = [{"tool_calls": [("web_search", json.dumps({"query": "курс доллара"}))]},
                           ["Не получилось найти."], ["Привет!"]]
    live.gw.exa_html = True                                  # 403 HTML Cloudflare, как Exa отвечает debian
    badge = f"[{BADGE}.textContent, {BADGE}.className, document.querySelectorAll('#messages .web-badge:not([hidden])').length]"
    try:
        r = browser.drive(live.url + "/", [*signin("badger"), ("wait", READY), add_gateway(),
            ("eval", "exa", EXA_KEY_SET), *go("settings"), ("wait", "document.querySelector('.settings-nav')"),
            *open_new_chat(),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
            ("wait", f"{WEB_TOGGLE} && !{WEB_TOGGLE}.hidden && !{WEB_TOGGLE}.disabled"),
            ("click", "#web-toggle"), ("wait", f"{WEB_TOGGLE}.getAttribute('aria-pressed') === 'true'"),
            ("type", "#chat-input", "Какой курс доллара?"), ("press", "Enter"),
            ("wait", f"{ANSWERED} && {LAST_ANSWER}.querySelector('details.web-steps') && {BADGE}?.classList.contains('bad')"),
            ("eval", "failed", badge),
            ("eval", "closed", f"{LAST_ANSWER}.querySelector('details.web-steps').open"),
            ("click", "#messages .msg.assistant .web-badge"),
            ("wait", f"{LAST_ANSWER}.querySelector('details.web-steps').open"),
            ("type", "#chat-input", "Привет"), ("press", "Enter"),
            ("wait", f"document.querySelectorAll('#messages .msg.assistant').length === 2 && {ANSWERED}"),
            ("eval", "plain", f"[{LAST_ANSWER}.querySelector('.md').textContent.trim(),"
                              f" !!{LAST_ANSWER}.querySelector('.web-badge:not([hidden])'), ...{badge}]"),
            ("eval", "reload", "location.reload()"),
            ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"),
            ("wait", f"document.querySelectorAll('#messages .msg').length === 4 && {BADGE}"),
            ("eval", "kept", badge)])
    finally:
        live.gw.exa_html = False
        live.gw.chat_script = []
    assert not r.get("failed"), r
    v = r["values"]
    assert v["failed"] == ["🌐 Интернет (Exa): 1 поиск — 1 ошибка", "web-badge bad", 1] and v["closed"] is False
    assert v["plain"] == ["Привет!", False, "🌐 Интернет (Exa): 1 поиск — 1 ошибка", "web-badge bad", 1]
    assert v["kept"] == v["failed"]
    assert not problems(r), problems(r)


THINK_TOGGLE = "document.querySelector('#think-toggle')"
#: метка страницы «увидела, что модель думает»: название диалога — шлюз держит рассуждение, пока его нет
SEEN = f"""(async () => {{ await {API}.api('PATCH', '/api/chats/' + location.hash.split('/')[2],
  {{ title: 'увидела' }}); return true; }})()"""
#: то же со своим названием: база живой службы общая на модуль, «увидела» уже стоит у диалога прежнего теста
MARK = f"""(async () => {{ await {API}.api('PATCH', '/api/chats/' + location.hash.split('/')[2],
  {{ title: '%s' }}); return true; }})()"""


def test_chat_reasoning_switch_and_thinking_line(browser, live):
    """Кнопка «Рассуждение» у local-gemma-abl: нажата до первого сообщения — уходит PATCH после создания диалога;
    с «Интернетом» — подсказка, что модель реже ищет; пока модель думает — «Думает… N с» и текст рассуждения,
    после — свёрнутое «💭 Рассуждала N с»; рассуждение — только текстом (HTML модели не исполняется); перезагрузка —
    то же из базы, кнопка нажата; облачная роль шлюза — кнопки нет."""
    import state
    live.add_user("thinker")
    live.gw.chat_script = [{"reasoning": ["Считаю ", "<img src=x onerror=alert(1)>"], "text": ["410"]}]
    live.gw.think_hold = lambda: state.DB.execute("SELECT 1 FROM chats WHERE title = 'увидела'").fetchone() is None
    two_models = f"""(async () => {{ const m = {API};
      const chat = (await m.api('GET', '/api/sections')).sections.find(x => x.id === 'chat');
      const link = chat.links.find(l => l.connection.kind === 'srvllm');
      await m.api('PUT', '/api/sections/chat/links/' + link.connection.id, {{ models: ['local-gemma-abl', 'gemma4-abl'] }});
      return true; }})()"""
    toggle = f"[{THINK_TOGGLE}.hidden, {THINK_TOGGLE}.getAttribute('aria-pressed'), {THINK_TOGGLE}.textContent.trim()]"
    done = (f"[{LAST_ANSWER}.querySelector('.md').textContent.trim(), {LAST_ANSWER}.querySelector('details.think').open,"
            f" {LAST_ANSWER}.querySelector('details.think summary').textContent,"
            f" {LAST_ANSWER}.querySelector('details.think .think-text').textContent,"
            f" document.querySelectorAll('#messages img').length]")
    try:
        r = browser.drive(live.url + "/", [*signin("thinker"), ("wait", READY), add_gateway(),
            ("eval", "exa", EXA_KEY_SET), ("eval", "models", two_models),
            *go("settings"), ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
            ("wait", f"{THINK_TOGGLE} && !{THINK_TOGGLE}.hidden"),
            ("eval", "before", toggle),
            ("click", "#think-toggle"), ("wait", f"{THINK_TOGGLE}.getAttribute('aria-pressed') === 'true'"),
            ("click", "#web-toggle"), ("wait", f"{WEB_TOGGLE}.getAttribute('aria-pressed') === 'true'"),
            ("eval", "note", "document.querySelector('#web-note').textContent"),
            ("type", "#chat-input", "Сколько будет 17×23?"), ("press", "Enter"),
            ("wait", f"{LAST_ANSWER}?.querySelector('.think.busy .think-text')?.textContent === 'Считаю '"),
            ("eval", "busy", f"{LAST_ANSWER}.querySelector('.think.busy').textContent"),
            ("eval", "seen", SEEN),
            ("wait", f"{ANSWERED} && {LAST_ANSWER}.querySelector('details.think')"),
            ("eval", "done", done),
            ("eval", "saved", f"""(async () => {{ const m = {API};
              const c = await m.api('GET', '/api/chats/' + location.hash.split('/')[2]);
              return [c.chat.thinking, c.messages.at(-1).reasoning, c.messages.at(-1).reasoning_sec]; }})()"""),
            ("eval", "reload", "location.reload()"),
            ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"),
            ("wait", f"document.querySelectorAll('#messages .msg').length === 2 && {THINK_TOGGLE} && !{THINK_TOGGLE}.hidden"),
            ("eval", "kept", f"[{toggle}, {done}]"),
            ("click", "#model-chip"),
            ("wait", "document.querySelector('.model-option[data-model=\"gemma4-abl\"]')"),
            ("click", ".model-option[data-model=\"gemma4-abl\"]"),
            ("wait", "document.querySelector('#model-chip').textContent.includes('gemma4-abl')"),
            ("wait", f"{THINK_TOGGLE}.hidden"),
            ("eval", "cloud", "document.querySelector('#web-note').textContent")])
    finally:
        live.gw.think_hold = False
        live.gw.chat_script = []
    assert not r.get("failed"), r
    v = r["values"]
    assert v["before"] == [False, "false", "Рассуждение"]
    assert v["note"] == "С рассуждением модель реже ищет в интернете"
    assert v["busy"].startswith("Думает… ") and v["busy"].endswith(" сСчитаю "), v["busy"]
    img = "Считаю <img src=x onerror=alert(1)>"
    # секунды сразу после ответа — те, что записал сервер (событие `done`), а не часы страницы
    sec = max(1, math.floor(v["saved"][2] + 0.5))
    assert v["done"] == ["410", False, f"💭 Рассуждала {sec} с", img, 0]
    assert v["saved"][:2] == [True, img]
    assert v["kept"] == [[False, "true", "Рассуждение"], v["done"]]
    assert v["cloud"] == ""
    assert live.gw.chat_requests[-1]["chat_template_kwargs"] == {"enable_thinking": True}
    assert not problems(r), problems(r)


def test_chat_reasoning_and_internet_show_one_spinner_at_a_time(browser, live, monkeypatch):
    """Ревью партии, пункт 1: «Интернет» и «Рассуждение» вместе. Пока идут субтитры — крутится только плашка, а
    рассуждение первого круга уже «Рассуждала»; модель снова думает во втором круге — крутится только «Думает…»,
    плашка — итог вызовов без крутилки."""
    import state
    import web
    from test_web import CANON, FakeYtDlp
    fake = FakeYtDlp()

    def titled(title):
        return state.DB.execute("SELECT 1 FROM chats WHERE title = ?", (title,)).fetchone() is not None

    async def held(argv, timeout):                       # субтитры «идут», пока страница их не увидела
        while not titled("субтитры"):
            await asyncio.sleep(0.01)
        return await fake(argv, timeout)

    monkeypatch.setattr(web, "RUN", held)
    live.add_user("both")
    start = len(live.gw.chat_requests)
    live.gw.chat_script = [{"reasoning": ["Надо посмотреть ролик."],
                            "tool_calls": [("youtube_subtitles", json.dumps({"url": CANON}))]},
                           {"reasoning": ["Смотрю субтитры."], "text": ["Ролик о Python."]}]
    # второй круг думает, пока страница его не увидела
    live.gw.think_hold = lambda: len(live.gw.chat_requests) > start + 1 and not titled("думает")
    mark = f"""(async () => {{ await {API}.api('PATCH', '/api/chats/' + location.hash.split('/')[2],
      {{ title: '%s' }}); return true; }})()"""
    shown = (f"[{LAST_ANSWER}.querySelector('.web-badge').className, {LAST_ANSWER}.querySelector('.web-badge').textContent,"
             f" !!{LAST_ANSWER}.querySelector('.think.busy'),"
             f" {LAST_ANSWER}.querySelector('details.think summary')?.textContent || ''] ")
    try:
        r = browser.drive(live.url + "/", [*signin("both"), ("wait", READY), add_gateway(),
            ("eval", "exa", EXA_KEY_SET), *go("settings"), ("wait", "document.querySelector('.settings-nav')"),
            *open_new_chat(),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
            ("wait", f"{THINK_TOGGLE} && !{THINK_TOGGLE}.hidden && {WEB_TOGGLE} && !{WEB_TOGGLE}.disabled"),
            ("click", "#think-toggle"), ("wait", f"{THINK_TOGGLE}.getAttribute('aria-pressed') === 'true'"),
            ("click", "#web-toggle"), ("wait", f"{WEB_TOGGLE}.getAttribute('aria-pressed') === 'true'"),
            ("type", "#chat-input", f"Перескажи ролик {CANON}"), ("press", "Enter"),    # ролик — только из переписки
            ("wait", f"{LAST_ANSWER}?.querySelector('.web-badge.busy')?.textContent.startsWith('▶ Субтитры:')"),
            ("eval", "tool", shown),
            ("eval", "seen1", mark % "субтитры"),
            ("wait", f"{LAST_ANSWER}.querySelector('.think.busy .think-text')?.textContent.includes('Смотрю субтитры.')"),
            ("eval", "think", shown),
            ("eval", "seen2", mark % "думает"),
            ("wait", f"{ANSWERED} && {LAST_ANSWER}.querySelector('details.think')"),
            ("eval", "done", f"{LAST_ANSWER}.querySelector('.md').textContent.trim()")])
    finally:
        live.gw.think_hold = False
        live.gw.chat_script = []
    assert not r.get("failed"), r
    v = r["values"]
    assert v["tool"][0] == "web-badge busy" and v["tool"][1].startswith("▶ Субтитры: ") and v["tool"][2] is False
    assert v["tool"][3].startswith("💭 Рассуждала "), v["tool"]
    assert v["think"] == ["web-badge", "▶ YouTube: 1 ролик", True, ""], v["think"]
    assert v["done"] == "Ролик о Python."
    assert not problems(r), problems(r)


def test_chat_thinking_without_text_while_the_switch_is_off(browser, live):
    """Ревью партии, пункт 6: кнопка «Рассуждение» выключена — пока модель думает, «Думает… N с» с крутилкой и
    без текста; первое слово ответа строку убирает, «Рассуждала» нет (рассуждение не хранится)."""
    import state
    live.add_user("dreamer")
    live.gw.chat_script = [{"reasoning": ["thought"], "text": ["Ок."]}]
    live.gw.think_hold = lambda: state.DB.execute("SELECT 1 FROM chats WHERE title = 'думает молча'").fetchone() is None
    busy = (f"[{LAST_ANSWER}.querySelector('.think.busy .think-sec').textContent,"
            f" {LAST_ANSWER}.querySelector('.think.busy .think-text').textContent,"
            f" !!{LAST_ANSWER}.querySelector('.think.busy .spin')]")
    try:
        r = browser.drive(live.url + "/", [*signin("dreamer"), ("wait", READY), add_gateway(),
            *go("settings"), ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
            ("wait", f"{THINK_TOGGLE} && !{THINK_TOGGLE}.hidden && {THINK_TOGGLE}.getAttribute('aria-pressed') === 'false'"),
            ("type", "#chat-input", "Привет"), ("press", "Enter"),
            ("wait", f"{LAST_ANSWER}?.querySelector('.think.busy .think-sec')?.textContent.startsWith('Думает… ')"),
            ("eval", "busy", busy),
            ("eval", "seen", MARK % "думает молча"),
            ("wait", ANSWERED),
            ("eval", "done", f"[{LAST_ANSWER}.querySelector('.md').textContent.trim(),"
                             f" {LAST_ANSWER}.querySelector('.think-box').childElementCount]")])
    finally:
        live.gw.think_hold = False
        live.gw.chat_script = []
    assert not r.get("failed"), r
    v = r["values"]
    assert re.fullmatch(r"Думает… \d+ с", v["busy"][0]) and v["busy"][1:] == ["", True], v["busy"]
    assert v["done"] == ["Ок.", 0]
    assert live.gw.chat_requests[-1]["chat_template_kwargs"] == {"enable_thinking": False}
    assert not problems(r), problems(r)


def test_chat_thinking_text_opens_from_the_keyboard_and_stays_open(browser, live):
    """Ревью партии, пункты 4 и 5: бегущий текст рассуждения раскрывается с клавиатуры (Enter), подпись и
    aria-expanded меняются; раскрытое остаётся раскрытым, пока модель пишет текст и снова думает, и в свёрнутом
    «Рассуждала»; второй отрезок рассуждения — через пустую строку."""
    import state
    live.add_user("opener")
    live.gw.chat_script = [{"reasoning": ["Считаю."], "text": ["410"],
                            "then": {"reasoning": ["Проверяю."], "text": [" — верно."]}}]
    live.gw.think_hold = lambda: state.DB.execute("SELECT 1 FROM chats WHERE title = 'раскрыла'").fetchone() is None
    run = f"{LAST_ANSWER}.querySelector('.think.busy .think-text')"
    attrs = (f"[{run}.getAttribute('role'), {run}.tabIndex, {run}.getAttribute('aria-expanded'), {run}.title,"
             f" {run}.classList.contains('open')]")
    done = (f"[{LAST_ANSWER}.querySelector('details.think').open,"
            f" {LAST_ANSWER}.querySelector('details.think .think-text').textContent,"
            f" {LAST_ANSWER}.querySelector('.md').textContent.trim()]")
    try:
        r = browser.drive(live.url + "/", [*signin("opener"), ("wait", READY), add_gateway(),
            *go("settings"), ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
            ("wait", f"{THINK_TOGGLE} && !{THINK_TOGGLE}.hidden"),
            ("click", "#think-toggle"), ("wait", f"{THINK_TOGGLE}.getAttribute('aria-pressed') === 'true'"),
            ("type", "#chat-input", "Сколько будет 17×23?"), ("press", "Enter"),
            ("wait", f"{LAST_ANSWER}?.querySelector('.think.busy .think-text')?.textContent === 'Считаю.'"),
            ("eval", "closed", attrs),
            ("eval", "focus", f"{run}.focus(), document.activeElement === {run}"),
            ("press", "Enter"),
            ("wait", f"{run}.classList.contains('open')"),
            ("eval", "opened", attrs),
            ("eval", "seen", MARK % "раскрыла"),
            ("wait", f"{ANSWERED} && {LAST_ANSWER}.querySelector('details.think')"),
            ("eval", "done", done)])
    finally:
        live.gw.think_hold = False
        live.gw.chat_script = []
    assert not r.get("failed"), r
    v = r["values"]
    assert v["focus"] is True
    assert v["closed"] == ["button", 0, "false", "Показать целиком", False]
    assert v["opened"] == ["button", 0, "true", "Свернуть", True]
    assert v["done"] == [True, "Считаю.\n\nПроверяю.", "410 — верно."]
    assert not problems(r), problems(r)


def test_chat_internet_needs_an_exa_key(browser, live):
    """Решение владельца 2026-10-21: без ключа Exa кнопка «Интернет» видна, но выключена, не нажата, с подсказкой
    (title и строка под полем); ключ задан в другой вкладке — возвращение на экран (профиль перечитан) её
    включает, и после перезагрузки тоже. Ключ убрали в другой вкладке — нажатие: 409 словами, кнопка выключена."""
    live.add_user("nokey")
    state = (f"[{WEB_TOGGLE}.hidden, {WEB_TOGGLE}.disabled, {WEB_TOGGLE}.getAttribute('aria-pressed'),"
             f" {WEB_TOGGLE}.title, document.querySelector('#web-note').textContent]")
    enabled = f"{WEB_TOGGLE} && !{WEB_TOGGLE}.hidden && !{WEB_TOGGLE}.disabled"
    put_key = f"(async () => {{ await {API}.api('PUT', '/api/me/exa-key', {{ key: 'exa-test-key' }}); return true; }})()"
    r = browser.drive(live.url + "/", [*signin("nokey"), ("wait", READY), add_gateway(),
        *go("settings"), ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(),
        ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
        ("wait", f"{WEB_TOGGLE} && !{WEB_TOGGLE}.hidden"),
        ("eval", "nokey", state),
        ("eval", "put", put_key),                                   # профиль вкладки ещё без ключа
        ("eval", "back", "document.dispatchEvent(new Event('visibilitychange')), true"),
        ("wait", enabled), ("eval", "returned", state),
        ("eval", "reload", "location.reload()"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"),
        ("wait", "document.querySelector('#model-chip')?.textContent.includes('local-gemma-abl')"),
        ("wait", enabled), ("eval", "reloaded", state),
        ("eval", "open", f"""(async () => {{ const m = {API};
          location.hash = '#/chat/' + (await m.api('POST', '/api/chats', {{}})).id; return true; }})()"""),
        ("wait", f"/^#\\/chat\\/./.test(location.hash) && {enabled}"),
        ("eval", "drop", f"(async () => {{ await {API}.api('DELETE', '/api/me/exa-key'); return true; }})()"),
        ("click", "#web-toggle"),
        ("wait", "document.querySelector('#chat-error').textContent.length > 0"),
        ("eval", "refused", f"[document.querySelector('#chat-error').textContent, ...{state}]")], width=1280)
    assert not r.get("failed"), r
    v = r["values"]
    hint = "Задайте ключ Exa в Настройках → Прочее → Интернет"
    assert v["nokey"] == [False, True, "false", hint, hint]
    title = "Интернет: поиск, страницы и субтитры YouTube"
    assert v["returned"] == v["reloaded"] == [False, False, "false", title, ""]
    assert v["refused"] == ["Сначала задайте ключ Exa в Настройках → Прочее → Интернет", False, True, "false", hint, hint]
    assert known_refusals(problems(r), 409) == [], problems(r)


def test_chat_context_refusal_keeps_the_text_and_the_attachments(browser, live):
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY), *open_new_chat(),
        attach("[new File(['а'.repeat(400000)], 'том.txt', { type: 'text/plain' })]"),
        ("type", "#chat-input", "Перескажи"), ("click", "#send"),
        ("wait", "document.querySelector('#chat-error').textContent.length > 0"),
        ("eval", "error", "document.querySelector('#chat-error').textContent"),
        ("eval", "kept", "[document.querySelector('#chat-input').value,"
                         " document.querySelectorAll('#composer .att-chip').length,"
                         " document.querySelectorAll('#messages .msg').length]"),
        ("eval", "ready", "!document.querySelector('#send').hidden && !document.querySelector('#send').disabled"),
        # загруженное, но не отправленное вложение крестик убирает и с сервера
        ("click", "#composer .att-remove"), ("wait", "!document.querySelector('#composer .att-chip')"),
        ("idle",)])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["error"].startswith("Не помещается в контекст модели")
    assert v["kept"] == ["Перескажи", 1, 0] and v["ready"]
    assert any(re.fullmatch(r"DELETE /api/attachments/[0-9a-f]{32}", s) for s in r["sent"]), r["sent"]
    assert not known_refusals(problems(r), 413), problems(r)


def abandoned_answer(live, uid):
    """Диалог с ответом из браузера, чья вкладка закрылась: признака жизни нет дольше ALIVE_SEC."""
    import uuid
    import chat
    import db
    conn = db.connect(live.db_path)
    try:
        cid, now = uuid.uuid4().hex, time.time()
        conn.execute("INSERT INTO chats(id, user_id, title, created, updated) VALUES (?, ?, 'брошенный', ?, ?)",
                     (cid, uid, now, now))
        conn.execute("INSERT INTO messages(id, chat_id, role, content, status, via, model, created)"
                     " VALUES (?, ?, 'user', 'брошенный', 'done', 'browser', 'cloud-chat', ?)", (uuid.uuid4().hex, cid, now))
        conn.execute("INSERT INTO messages(id, chat_id, role, content, status, via, model, created, alive_at)"
                     " VALUES (?, ?, 'assistant', '', 'streaming', 'browser', 'cloud-chat', ?, ?)",
                     (uuid.uuid4().hex, cid, now + 1, now - chat.ALIVE_SEC - 1))
        return cid
    finally:
        conn.close()


def test_direct_cloud_chat_saves_the_answer(browser, live):
    abandoned = abandoned_answer(live, live.add_user("clouder"))
    # подключение появилось после входа: раздел чата открывается заново, уже не подсказкой
    steps = [*signin("clouder"), ("wait", READY), add_cloud(live.cloud), *go("settings"), ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(),
        ("click", "#model-chip"),
        ("wait", "document.querySelector('.model-option[data-model=\"cloud-chat\"]')"),
        ("click", ".model-option[data-model=\"cloud-chat\"]"),
        ("wait", "document.querySelector('#model-chip').textContent.includes('cloud-chat')"),
        ("eval", "conn", "document.querySelector('.chat-conn').textContent"),
        ("type", "#chat-input", "Ты здесь?"), ("press", "Enter"),
        ("wait", ANSWERED),
        ("eval", "answer", f"{LAST_ANSWER}.querySelector('.md').textContent.trim()"),
        ("eval", "saved", SAVED),
        ("eval", "model", f"""(async () => {{ const m = {API};
          return (await m.api('GET', '/api/chats/' + location.hash.split('/')[2])).chat.model; }})()"""),
        # ответ, который читает другая вкладка (ход — через API, признак жизни свежий), — идущий:
        # без «Ответить заново»; вкладка его сохранила — диалог перечитывается сам
        ("eval", "other", f"""(async () => {{ const m = {API};
          const c = await m.api('POST', '/api/chats', {{}});
          const t = await m.api('POST', '/api/chats/' + c.id + '/turns', {{ text: 'из другой вкладки' }});
          location.hash = '#/chat/' + c.id; return [c.id, t.assistant_message_id]; }})()"""),
        ("wait", f"{LAST_ANSWER}?.querySelector('.msg-meta').textContent === 'ответ идёт в другой вкладке…'"),
        ("eval", "otherActions", f"{LAST_ANSWER}.querySelectorAll('.msg-actions button').length"),
        ("eval", "put", f"""(async () => {{ const m = {API}; const id = location.hash.split('/')[2];
          const last = (await m.api('GET', '/api/chats/' + id)).messages.at(-1);
          await m.api('PUT', '/api/chats/' + id + '/messages/' + last.id, {{ content: 'Готово в другой вкладке', status: 'done' }});
          return true; }})()"""),
        ("wait", f"{LAST_ANSWER}?.querySelector('.md').textContent.trim() === 'Готово в другой вкладке'"
                 f" && {LAST_ANSWER}.querySelector('.msg-retry')"),
        # ответ, который никто не дочитал (вкладку закрыли, признака жизни нет), — «прервано»
        ("eval", "left", f"location.hash = '#/chat/{abandoned}'"),
        ("wait", f"{LAST_ANSWER}?.querySelector('.msg-meta').textContent === 'прервано'"),
        ("eval", "leftRetry", f"!!{LAST_ANSWER}.querySelector('.msg-retry')"),
        ("eval", "free", "!document.querySelector('#send').hidden")]
    r = browser.drive(live.url + "/", steps)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["conn"] == "Облако" and v["answer"] == "Облако на связи." and v["model"] == "cloud-chat"
    assert v["saved"] == ["assistant", "done", "Облако на связи."] and v["free"]
    assert v["otherActions"] == 0 and v["leftRetry"]
    assert live.cloud.chat_requests[-1]["model"] == "cloud-chat"
    assert live.cloud.chat_requests[-1]["messages"][1:] == [{"role": "user", "content": "Ты здесь?"}]
    assert live.cloud.chat_auth[-1] == f"Bearer {live.cloud.TOKEN}"
    assert not problems(r), problems(r)


#: где токен прямого подключения мог бы остаться на странице
TOKEN_TRACES = """(names) => { const where = { local: JSON.stringify({ ...localStorage }),
  session: JSON.stringify({ ...sessionStorage }), dom: document.documentElement.outerHTML, cookie: document.cookie };
  return Object.entries(where).flatMap(([k, v]) => names.filter(n => v.includes(n)).map(n => k + ':' + n)); }"""


def test_direct_token_stays_in_tab_memory_and_goes_only_to_its_address(browser, live):
    """B1-I1/M10: токен прямого подключения — ни в хранилищах, ни в DOM; после смены адреса
    подключения (другая вкладка, администратор) прежний токен новому адресу не уходит — ни из
    чата (токен в памяти вкладки), ни из профиля (список подключений на экране устарел)."""
    from fakes import FakeCloud
    live.add_user("tokens")
    second = FakeCloud()
    second.TOKEN = "second-key"
    second.chat_reply = ["Второе ", "облако."]
    first_chats, first_models = len(live.cloud.chat_auth), len(live.cloud.models_auth)
    names = [live.cloud.TOKEN, second.TOKEN]
    try:
        r = browser.drive(live.url + "/", [*signin("tokens"), ("wait", READY),
            ("eval", "made", f"""(async () => {{ const m = {API};
              const c = await m.api('POST', '/api/connections', {{ name: 'Облако', kind: 'openai',
                base_url: {live.cloud.base_url!r}, token: {live.cloud.TOKEN!r}, sections: ['chat'] }});
              await m.api('PUT', '/api/sections/chat/links/' + c.id, {{ models: ['cloud-chat'] }});
              await m.loadMe(); window.connId = c.id; return true; }})()"""),
            *go("settings/chat"), ("wait", "document.querySelector('.link')"), *open_new_chat(),
            ("type", "#chat-input", "первый"), ("press", "Enter"), ("wait", ANSWERED),
            ("eval", "firstAnswer", f"{LAST_ANSWER}.querySelector('.md').textContent.trim()"),
            ("eval", "traces1", f"({TOKEN_TRACES})({names!r})"),
            # настройки открыты со старым списком; подключение тем временем переехало (другая вкладка)
            *go("settings/chat"), ("wait", "document.querySelector('.link .link-refresh')"),
            ("eval", "moved", f"""(async () => {{ const m = {API};
              const c = await m.api('PATCH', '/api/connections/' + window.connId,
                {{ base_url: {second.base_url!r}, token: {second.TOKEN!r} }});
              return c.base_url; }})()"""),
            ("click", ".link .link-refresh"),
            ("wait", "/^Найдено моделей|\\(\\d{3}\\)/.test(document.querySelector('.section-status').textContent)"),
            ("eval", "models", "document.querySelector('.section-status').textContent"),
            *open_new_chat(),
            ("type", "#chat-input", "второй"), ("press", "Enter"), ("wait", ANSWERED),
            ("eval", "secondAnswer", f"[{LAST_ANSWER}.querySelector('.md').textContent.trim(),"
                                     f" {LAST_ANSWER}.querySelector('.msg-meta').textContent]"),
            ("eval", "traces2", f"({TOKEN_TRACES})({names!r})")])
    finally:
        second.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["firstAnswer"] == "Облако на связи." and v["moved"] == second.base_url
    assert v["traces1"] == [] and v["traces2"] == []
    assert v["models"] == "Найдено моделей: 1"
    assert v["secondAnswer"] == ["Второе облако.", ""]
    assert live.cloud.chat_auth[first_chats:] == [f"Bearer {live.cloud.TOKEN}"]
    assert live.cloud.models_auth[first_models:] == []
    assert second.chat_auth == [f"Bearer {second.TOKEN}"] and second.models_auth == [f"Bearer {second.TOKEN}"]
    assert not problems(r), problems(r)


def test_stop_keeps_partial_answer(browser, live):
    live.gw.chat_hold = True
    try:
        r = browser.drive(live.url + "/", [*signin(), ("wait", READY), *open_new_chat(),
            ("type", "#chat-input", "Расскажи длинно"), ("press", "Enter"),
            ("wait", f"{LAST_ANSWER}?.querySelector('.md').textContent.trim() === 'Здрав'"
                     " && !document.querySelector('#stop').hidden"),
            ("click", "#stop"),
            ("wait", ANSWERED),
            ("eval", "meta", f"{LAST_ANSWER}.querySelector('.msg-meta').textContent"),
            ("eval", "saved", SAVED)])
    finally:
        live.gw.chat_hold = False
    assert not r.get("failed"), r
    assert r["values"]["meta"] == "остановлено"
    assert r["values"]["saved"] == ["assistant", "stopped", "Здрав"]
    assert "POST /api/chats/" in " ".join(r["sent"]) and any(s.endswith("/stop") for s in r["sent"])
    assert not problems(r), problems(r)


def test_direct_stop_aborts_and_saves_the_partial_answer(browser, live):
    live.add_user("cloudstop")
    live.cloud.chunk_pause = 3                     # после первого куска сервис «думает»
    try:
        r = browser.drive(live.url + "/", [*signin("cloudstop"), ("wait", READY), add_cloud(live.cloud), *go("settings"), ("wait", "document.querySelector('.settings-nav')"),
            *open_new_chat(),
            ("type", "#chat-input", "Подумай"), ("press", "Enter"),
            ("wait", f"{LAST_ANSWER}?.querySelector('.md').textContent.trim() === 'Облако'"),
            ("click", "#stop"),
            ("wait", ANSWERED),
            ("eval", "meta", f"{LAST_ANSWER}.querySelector('.msg-meta').textContent"),
            ("eval", "saved", SAVED)])
    finally:
        live.cloud.chunk_pause = 0.05
    assert not r.get("failed"), r
    assert r["values"]["meta"] == "остановлено"
    assert r["values"]["saved"] == ["assistant", "stopped", "Облако "]
    assert not any(s.endswith("/stop") for s in r["sent"])     # прямое — обрыв в браузере, не POST /stop
    assert not problems(r), problems(r)


def test_direct_stream_without_an_ending_is_saved_as_cut(browser, live):
    """Прямое подключение: поток кончился без [DONE] и finish_reason — ответ оборван, а не готов;
    finish_reason `length` — обрезан пределом длины модели (аудит). Прочитанное сохраняется ошибкой."""
    import chat
    seen = {}
    for name, ending in (("eof", None), ("length", "length")):
        live.add_user(f"cloudcut-{name}")
        live.cloud.chat_ending = ending
        try:
            r = browser.drive(live.url + "/", [*signin(f"cloudcut-{name}"), ("wait", READY), add_cloud(live.cloud),
                *go("settings"), ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(),
                ("type", "#chat-input", "Скажи"), ("press", "Enter"), ("wait", ANSWERED),
                ("eval", "meta", f"{LAST_ANSWER}.querySelector('.msg-meta').textContent"),
                ("eval", "saved", SAVED),
                ("eval", "out", "fetch('/logout', { method: 'POST' }).then((r) => r.ok)")])   # следующий проход — вход
        finally:
            live.cloud.chat_ending = "stop"
        assert not r.get("failed"), r
        assert not problems(r), problems(r)
        seen[name] = r["values"]["meta"], r["values"]["saved"]
    assert seen["eof"] == ("Модель оборвала ответ: поток кончился без признака конца",
                           ["assistant", "error", "Облако на связи."])
    assert seen["length"] == ("Ответ обрезан пределом длины модели",
                              ["assistant", "error", "Облако на связи." + chat.CUT_LENGTH])


def test_direct_answer_longer_than_the_limit_is_saved_cut(browser, live):
    """Ответ прямого подключения длиннее ANSWER_MAX — как у сервера: обрезан с пометкой и сохранён
    ошибкой, а не потерян отказом PUT (422). Край среза — посреди эмодзи: половину пары (одиночный
    суррогат) PUT не принял бы, она отрезается целиком."""
    import chat
    live.add_user("cloudbig")
    saved, live.cloud.chat_reply = live.cloud.chat_reply, ["x" * 700_000] * 2 + ["x" * 599_999 + "😀" * 3, "лишнее"]
    measured = f"""(async () => {{ const m = {API}; const id = location.hash.split('/')[2];
      const last = (await m.api('GET', '/api/chats/' + id)).messages.at(-1);
      return [last.status, last.content.length, last.content.slice(-60)]; }})()"""
    try:
        r = browser.drive(live.url + "/", [*signin("cloudbig"), ("wait", READY), add_cloud(live.cloud),
            *go("settings"), ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(),
            ("type", "#chat-input", "Много"), ("press", "Enter"), ("wait", ANSWERED),
            ("eval", "meta", f"{LAST_ANSWER}.querySelector('.msg-meta').textContent"),
            ("eval", "saved", measured)], timeout=40000)
    finally:
        live.cloud.chat_reply = saved
    assert not r.get("failed"), r
    assert r["values"]["meta"] == "Ответ слишком длинный — обрезан"
    assert r["values"]["saved"] == ["error", chat.ANSWER_MAX - 1 + len(chat.CUT_SIZE), ("x" * 60 + chat.CUT_SIZE)[-60:]]
    assert not problems(r), problems(r)


#: PUT ответа прямого подключения сервер не принял (422)
REFUSE_PUT = """(() => { const real = window.fetch;
  window.fetch = (u, o = {}) => o.method === 'PUT' && /\\/messages\\//.test(String(u))
    ? Promise.resolve(new Response(JSON.stringify({ detail: { code: 'input', message: 'ответ не принят' } }),
        { status: 422, headers: { 'content-type': 'application/json' } }))
    : real(u, o);
  return true; })()"""


def test_direct_answer_that_was_not_saved_says_so(browser, live):
    """Прямое подключение: PUT готового ответа отказал — под ответом «Ответ не сохранился: …», а не пусто."""
    live.add_user("cloudput")
    r = browser.drive(live.url + "/", [*signin("cloudput"), ("wait", READY), add_cloud(live.cloud),
        *go("settings"), ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(),
        ("eval", "refuse", REFUSE_PUT), ("type", "#chat-input", "Скажи"), ("press", "Enter"), ("wait", ANSWERED),
        ("eval", "meta", f"[{LAST_ANSWER}.querySelector('.md').textContent.trim(),"
                         f" {LAST_ANSWER}.querySelector('.msg-meta').textContent]")])
    assert not r.get("failed"), r
    assert r["values"]["meta"] == ["Облако на связи.", "Ответ не сохранился: Ответ не принят"]
    assert not problems(r), problems(r)


def test_chat_list_renames_and_deletes(browser, live):
    item = "document.querySelector('.chat-item[data-id=\"' + window.made + '\"]')"
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY),
        ("eval", "made", f"""(async () => {{ const m = {API};
          const c = await m.api('POST', '/api/chats', {{}});
          window.made = c.id; location.hash = '#/chat/' + c.id; return true; }})()"""),
        ("wait", f"{item}?.querySelector('[aria-current=page]')"),
        ("eval", "mark", "document.querySelector('.chat-item [aria-current=page]').closest('.chat-item')"
                         ".classList.add('under-test')"),
        ("click", ".under-test .chat-more"), ("click", ".under-test .chat-rename"),
        ("wait", is_open("#rename-dialog")),
        set_value("#rename-dialog [name=title]", "Мой план"), ("click", "#rename-save"),
        ("wait", f"!{is_open('#rename-dialog')} && {item}?.textContent === 'Мой план'"),
        ("eval", "mark2", f"{item}.classList.add('under-test')"),
        ("click", ".under-test .chat-more"), ("click", ".under-test .chat-delete"),
        ("wait", is_open("#chat-delete-dialog")),
        ("click", "#chat-delete-confirm"),
        ("wait", f"!{is_open('#chat-delete-dialog')} && !{item} && location.hash === '#/chat'"),
        ("eval", "gone", f"""(async () => {{ const m = {API};
          return !(await m.api('GET', '/api/chats')).items.some(c => c.id === window.made); }})()""")])
    assert not r.get("failed"), r
    assert r["values"]["gone"] is True
    assert not problems(r), problems(r)


def test_chat_link_loads_the_list_once(browser, live):
    """B1-M5: ссылка #/chat/<id> на свежий раздел — один GET /api/chats, а не два."""
    spy = """(() => { window.listGets = 0; const real = window.fetch;
      window.fetch = (url, opts = {}) => { if ((opts.method || 'GET') === 'GET' && String(url) === '/api/chats')
        window.listGets += 1; return real(url, opts); }; return true; })()"""
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY),
        ("eval", "made", f"""(async () => {{ const m = {API};
          window.made = (await m.api('POST', '/api/chats', {{}})).id; return true; }})()"""),
        *go("settings"), ("wait", "document.querySelector('.settings-nav')"),
        ("eval", "spy", spy),
        ("eval", "open", "location.hash = '#/chat/' + window.made"),
        ("wait", "document.querySelector('.chat-item[data-id=\"' + window.made + '\"] [aria-current=page]')"),
        ("idle",),
        ("eval", "gets", "window.listGets")])
    assert not r.get("failed"), r
    assert r["values"]["gets"] == 1
    assert not problems(r), problems(r)


CODE_REPLY = ["Вот код:\n\n```python\ndef hello():\n", "    return 'привет'\n```\n"]


def test_markdown_code_has_copy_button(browser, live):
    saved = live.gw.chat_reply
    live.gw.chat_reply = CODE_REPLY
    try:
        r = browser.drive(live.url + "/", [*signin(), ("wait", READY), *open_new_chat(),
            ("type", "#chat-input", "Напиши функцию"), ("press", "Enter"),
            ("wait", ANSWERED),
            ("eval", "code", f"(() => {{ const a = {LAST_ANSWER};"
                             " return [a.querySelector('pre code').textContent, !!a.querySelector('pre code .hljs-keyword'),"
                             " a.querySelector('pre button.copy').textContent, a.querySelector('.md p').textContent]; })()")])
    finally:
        live.gw.chat_reply = saved
    assert not r.get("failed"), r
    assert r["values"]["code"] == ["def hello():\n    return 'привет'\n", True, "Копировать", "Вот код:"]
    assert not problems(r), problems(r)


LONG_CODE = ["```\n" + "x = " + "1234567890 " * 40 + "\n```\n"]


def test_chat_layout_fits_390(browser, live):
    saved = live.gw.chat_reply
    live.gw.chat_reply = LONG_CODE
    panel = "document.querySelector('#chats').getBoundingClientRect()"
    try:
        r = browser.drive(live.url + "/", [*signin(), ("wait", READY), *open_new_chat(mobile=True),
            ("type", "#chat-input", "Длинная строка"), ("press", "Enter"),
            ("wait", ANSWERED),
            ("eval", "overflow", f"!({NO_OVERFLOW}) || document.querySelector('#view').scrollWidth"
                                 " > document.querySelector('#view').clientWidth"),
            # длинная строка кода прокручивается внутри блока (pre или его code), а не страница
            ("eval", "codeScrolls", "[...document.querySelectorAll('#messages pre, #messages pre code')]"
                                    ".some(e => e.scrollWidth > e.clientWidth)"),
            ("eval", "hidden", f"{panel}.right <= 0"),
            ("eval", "aboveTabs", "document.querySelector('#composer').getBoundingClientRect().bottom"
                                  " <= document.querySelector('#rail').getBoundingClientRect().top"),
            ("click", "#chats-toggle"),
            ("wait", f"{panel}.left === 0 && {panel}.right <= innerWidth"),
            ("click", ".chat-item a"),
            ("wait", f"{panel}.right <= 0"),
            ("eval", "overflow2", f"!({NO_OVERFLOW})")], width=390, mobile=True)
    finally:
        live.gw.chat_reply = saved
    assert not r.get("failed"), r
    v = r["values"]
    assert not v["overflow"] and v["codeScrolls"] and v["hidden"] and v["aboveTabs"] and not v["overflow2"]
    assert not problems(r), problems(r)


# ---- фото ----------------------------------------------------------------------

MODEL_IDS = {"lustify": "lustify-v10-krea2-turbo", "flux": "flux-3-image-klein"}
FEED_READY = ("document.querySelector('#photo-feed')?.dataset.feed"
              " && document.querySelector('#photo-feed').dataset.feed !== 'loading'")
NEWEST_DONE = "#photo-feed .photo-card[data-state=done] .card-open"
#: запросы POST /api/images… страницы: поля формы (файл — 'file:<размер>') или тело JSON
SPY = """(() => { window.sentForms = []; const real = window.fetch;
  window.fetch = (url, opts = {}) => {
    if (opts.method === 'POST' && String(url).startsWith('/api/images')) {
      const b = opts.body;
      window.sentForms.push({ path: String(url), form: b instanceof FormData
        ? Object.fromEntries([...b.entries()].map(([k, v]) => [k, typeof v === 'string' ? v : 'file:' + v.size]))
        : JSON.parse(b) });
    }
    return real(url, opts);
  }; return true; })()"""


def done_cards(n):
    return f"document.querySelectorAll('#photo-feed .photo-card[data-state=done]').length === {n}"


#: ссылки, которые страница «нажала» для загрузки: [href, есть ли download]; сама загрузка не идёт
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


def png_of(w, h, color):
    out = io.BytesIO()
    Image.new("RGB", (w, h), color).save(out, "PNG")
    return out.getvalue()


def picture(data):
    with Image.open(io.BytesIO(data)) as im:
        return im.size, im.convert("RGB").getpixel((0, 0))


def described_picture(body):
    url = body["messages"][0]["content"][1]["image_url"]["url"]
    return picture(base64.b64decode(url.split(",", 1)[1]))


def put_file(selector, data, name="photo.png"):
    """Файл в поле, как из окна выбора, и событие change."""
    b64 = base64.b64encode(data).decode()
    return ("eval", "file", f"""(() => {{ const bytes = Uint8Array.from(atob('{b64}'), c => c.charCodeAt(0));
      const dt = new DataTransfer(); dt.items.add(new File([bytes], '{name}', {{ type: 'image/png' }}));
      const input = document.querySelector('{selector}'); input.files = dt.files;
      input.dispatchEvent(new Event('change', {{ bubbles: true }})); return input.files.length; }})()""")


def add_gateway():
    return ("eval", "gateway", f"""(async () => {{ const m = {API};
      await m.api('POST', '/api/connections', {{ name: 'Шлюз', kind: 'srvllm', base_url: {GATEWAY_URL!r},
        token: {GW_TOKEN!r}, sections: ['chat', 'photo', 'video', 'transcribe', 'ocr', 'speech', 'dictation'] }});
      await m.loadMe(); return true; }})()""")


def make_images(n):
    """n готовых картинок через API: промпт «кот i», размеры по кругу, seed 100+i."""
    return ("eval", "images", f"""(async () => {{
      const m = {API}; const sizes = ['portrait', 'square', 'landscape']; const out = [];
      for (let i = 0; i < {n}; i++) {{
        const job = await m.api('POST', '/api/images', {{ prompt: 'кот ' + i, size: sizes[i % 3], seed: 100 + i }});
        for (let t = 0; t < 200; t++) {{
          const item = await m.api('GET', '/api/images/' + job.id);
          if (item.state === 'done') {{ out.push(item.media_id); break; }}
          await new Promise(r => setTimeout(r, 30));
        }}
      }}
      return out; }})()""")


def photo_screen(login, cards=0):
    """Вход, подключение к шлюзу, `cards` картинок в истории, раздел «Фото» с загруженной лентой."""
    return [*signin(login), ("wait", READY), add_gateway(), *([make_images(cards)] if cards else []),
            *go("photo"), ("wait", FEED_READY)]


@pytest.mark.parametrize("width", [390, 1280])
@pytest.mark.parametrize("model", ["lustify", "flux"])
def test_photo_source_strength_mask_and_model_switch(browser, live, width, model):
    nxt = "flux" if model == "lustify" else "lustify"
    login = f"ph-{model}-{width}"
    live.add_user(login)
    start = len(live.gw.edits)
    r = browser.drive(live.url + "/", [*photo_screen(login), ("eval", "spy", SPY),
        put_file("#photo-file", png_of(40, 30, "blue")),
        ("wait", "document.querySelector('#photo-source img')?.naturalWidth === 40"),
        ("eval", "strengthShown", "!document.querySelector('#photo-strength-label').hidden"),
        set_value("#photo-strength", "0.4"),
        ("type", "#photo-prompt", "watercolor landscape"),
        ("click", "#photo-go"), ("wait", done_cards(1)),
        ("eval", "overflow", f"!({NO_OVERFLOW})"),
        ("click", "#photo-source-clear"),
        ("eval", "cleared", "document.querySelector('#photo-source').hidden"
                            " && document.querySelector('#photo-file').files.length === 0"),
        ("click", "#chip-model"), ("click", f"#chip-model-menu [data-value$=':{model}']"),
        ("click", NEWEST_DONE), ("wait", is_open("#viewer")),
        ("click", "#viewer .act-edit"),
        ("wait", f"{is_open('#editor')} && !document.querySelector('#viewer')"),
        ("eval", "defaultModel", "document.querySelector('#edit-model-label').textContent"),
        ("type", "#edit-prompt", "change only this patch to green"),
        ("click", "#edit-local"), ("click", "#edit-submit"),
        ("eval", "emptyMask", f"{is_open('#editor')} && document.querySelector('#edit-error').textContent.length > 0"),
        ("click", "#edit-mask"), ("click", "#edit-submit"),
        ("wait", f"!{is_open('#editor')} && {done_cards(2)}"),
        ("click", "#chip-model"), ("click", f"#chip-model-menu [data-value$=':{nxt}']"),
        ("click", NEWEST_DONE), ("wait", is_open("#viewer")),
        ("click", "#viewer .act-edit"), ("wait", is_open("#editor")),
        ("eval", "reset", "!document.querySelector('#edit-local').checked && document.querySelector('#edit-prompt').value === ''"),
        ("eval", "nextLabel", "document.querySelector('#edit-model-label').textContent"),
        ("eval", "strengthVisible", "!document.querySelector('#edit-strength-label').hidden"),
        ("eval", "editorOverflow", f"!({NO_OVERFLOW}) || document.querySelector('#editor').scrollWidth"
                                   " > document.querySelector('#editor').clientWidth"),
        ("type", "#edit-prompt", "make the landscape a watercolor"),
        ("click", "#edit-submit"),
        ("wait", f"!{is_open('#editor')} && {done_cards(3)}"),
        ("eval", "forms", "window.sentForms"),
        ("eval", "media", f"(async () => (await {API}.api('GET', '/api/images')).items.map(i => i.media_id))()")],
        width=width, height=900, mobile=width == 390)
    assert not r.get("failed"), r
    v = r["values"]
    names = {"lustify": "Lustify", "flux": "FLUX"}
    assert v["strengthShown"] and not v["overflow"] and v["cleared"]
    assert names[model] in v["defaultModel"] and v["reset"] and names[nxt] in v["nextLabel"]
    assert v["strengthVisible"] == (nxt == "lustify") and not v["editorOverflow"] and v["emptyMask"]
    # страница: исходник файлом, затем фото истории с маской и без
    _newest, middle, oldest = v["media"]
    first, second, third = (f["form"] for f in v["forms"])
    assert [f["path"] for f in v["forms"]] == ["/api/images"] * 3
    assert first["model"] == "lustify" and first["strength"] == "0.4" and first["image"].startswith("file:")
    assert "source_media_id" not in first and "mask" not in first
    assert second["size"] == "original" and second["model"] == model and second["source_media_id"] == oldest
    assert second["mask"].startswith("file:") and "image" not in second
    assert third["size"] == "original" and third["model"] == nxt and third["source_media_id"] == middle
    assert "mask" not in third
    # шлюз: модель, сила только у Lustify, фото исходника
    edits = live.gw.edits[start:]
    assert len(edits) == 3
    assert edits[0][0]["model"] == MODEL_IDS["lustify"] and edits[0][0]["strength"] == "0.4"
    assert picture(edits[0][1]) == ((40, 30), (0, 0, 255))
    assert edits[1][0]["model"] == MODEL_IDS[model] and edits[1][0]["size"] == "64x48"
    assert edits[2][0]["model"] == MODEL_IDS[nxt]
    assert edits[1 if model == "lustify" else 2][0]["strength"] == "0.25"
    assert "strength" not in edits[2 if model == "lustify" else 1][0]
    assert not problems(r), problems(r)


@pytest.mark.parametrize("width,model", [(390, "lustify"), (1280, "flux")])
def test_photo_description_is_edited_and_creates_without_a_source(browser, live, width, model):
    login = f"desc-{width}"
    live.add_user(login)
    text = "An exceptionally detailed landscape with precise lighting and textures. " * 100
    saved = live.gw.description_reply
    live.gw.description_reply = {"choices": [{"message": {"content": text}, "finish_reason": "stop"}]}
    d0, g0, e0 = len(live.gw.descriptions), len(live.gw.generations), len(live.gw.edits)
    described = f"{is_open('#describe-dialog')} && document.querySelector('#describe-text').value.length > 0"
    try:
        r = browser.drive(live.url + "/", [*photo_screen(login), ("eval", "spy", SPY),
            ("click", "#chip-model"), ("click", f"#chip-model-menu [data-value$=':{model}']"),
            ("click", "#photo-menu-toggle"), ("wait", "!document.querySelector('#photo-menu').hidden"),
            ("eval", "menu", "[...document.querySelectorAll('#photo-menu .menu-item')].map(e => e.textContent.trim())"),
            ("press", "Escape"), ("wait", "document.querySelector('#photo-menu').hidden"),
            put_file("#describe-file", png_of(64, 48, "blue"), "source.png"),
            ("wait", described),
            ("eval", "descriptionLength", "document.querySelector('#describe-text').value.length"),
            ("eval", "append", "(() => { const t = document.querySelector('#describe-text'); t.value += ' Visible extra detail.';"
                               " t.dispatchEvent(new Event('input')); return true; })()"),
            ("type", "#describe-extra", "Добавь маленькую оранжевую лодку"),
            ("eval", "counter", "document.querySelector('#describe-counter').textContent"),
            ("click", "#describe-create"),
            ("wait", f"!{is_open('#describe-dialog')} && {done_cards(1)}"),
            ("eval", "prompt", "document.querySelector('#photo-prompt').value"),
            ("click", NEWEST_DONE), ("wait", is_open("#viewer")),
            ("click", "#viewer .act-describe"), ("wait", described),
            ("eval", "fits", f"{NO_OVERFLOW} && document.querySelector('#describe-dialog').scrollWidth"
                             " <= document.querySelector('#describe-dialog').clientWidth"),
            ("eval", "forms", "window.sentForms"),
            ("eval", "tick", "new Promise(r => setTimeout(r, 300)).then(() => true)")],
            width=width, height=900, mobile=width == 390)
    finally:
        live.gw.description_reply = saved
    assert not r.get("failed"), r
    # текст в поле — получение подтверждено, сервер его стирает (повтор GET до того отдал бы его снова)
    assert len([s for s in r["sent"] if s.startswith("DELETE /api/images/descriptions/")]) == 2
    v = r["values"]
    assert v["menu"] == ["Описать фото с устройства…"]                 # «Освободить карту» — только админу
    assert v["descriptionLength"] == len(text.strip()) and v["fits"]
    assert v["counter"].startswith(f"{len(v['prompt'])} / 24000")
    assert v["prompt"].startswith(text.strip()) and "Visible extra detail." in v["prompt"]
    assert "Добавь маленькую оранжевую лодку" in v["prompt"]
    paths = [f["path"] for f in v["forms"]]
    assert paths == ["/api/images/descriptions", "/api/images", "/api/images/descriptions"]
    created = v["forms"][1]["form"]
    assert created["model"] == model and created["prompt"] == v["prompt"]
    assert not {"image", "mask", "source_media_id"} & created.keys()
    assert v["forms"][0]["form"]["image"].startswith("file:") and v["forms"][2]["form"]["source_media_id"]
    bodies = live.gw.descriptions[d0:]
    assert [described_picture(b) for b in bodies] == [((64, 48), (0, 0, 255)), picture(PNG_REAL)]
    gens = live.gw.generations[g0:]
    assert len(gens) == 1 and len(live.gw.edits) == e0                  # по тексту, без исходного фото
    assert gens[0][0]["model"] == MODEL_IDS[model] and gens[0][0]["prompt"] == v["prompt"]
    assert not problems(r), problems(r)


@pytest.mark.parametrize("created", [True, False])
def test_photo_description_is_cancelled_when_the_section_goes_away(browser, live, created):
    """Ушли из раздела («Назад» браузера) при открытом «Описать»: удалённое окно события close не шлёт,
    а задание gemma-abl всё равно отменено — и созданное (created), и то, чьё создание ещё шло; часы окна
    остановлены."""
    import images
    login = f"descgone{int(created)}"
    live.add_user(login)

    async def held(request):
        """Описание «идёт», пока задание не отменят (предел — 20 с)."""
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not any(j.state == "cancelling" for j in images.JOBS.values()):
            await asyncio.sleep(0.01)
        return await live.gw.builtin(request)

    #: отмены и опросы окна; ответ на создание — через 500 мс, если уйти надо раньше него
    spy = f"""(() => {{ const real = window.fetch; window.cancels = []; window.polls = 0;
      window.fetch = (url, opts = {{}}) => {{ const u = String(url);
        if (opts.method === 'POST' && u.endsWith('/cancel')) window.cancels.push(u);
        if (opts.method === 'GET' && u.startsWith('/api/images/descriptions/')) window.polls += 1;
        const p = real(url, opts);
        return {'false' if created else 'true'} && opts.method === 'POST' && u === '/api/images/descriptions'
          ? p.then(r => new Promise(res => setTimeout(() => res(r), 500))) : p; }};
      return true; }})()"""
    leave = [("wait", "window.polls >= 1")] if created else []
    live.gw.on["/slot/images/descriptions"] = held
    try:
        r = browser.drive(live.url + "/", [*photo_screen(login), ("eval", "spy", spy),
            put_file("#describe-file", png_of(64, 48, "blue"), "source.png"), ("wait", is_open("#describe-dialog")),
            ("eval", "st", "!!(window.st = document.querySelector('#describe-status'))"), *leave,
            *go("chat"), ("wait", "document.querySelector('#composer')"),
            ("wait", "window.cancels.length === 1"), ("wait", "window.st.hidden"),
            ("eval", "polls", "window.polls"),
            ("eval", "after", "new Promise(r => setTimeout(() => r(window.polls), 2000))")], timeout=20000)
    finally:
        live.gw.on.pop("/slot/images/descriptions", None)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["after"] == v["polls"] and (v["polls"] >= 1) == created     # после ухода окно не опрашивает
    assert not problems(r), problems(r)


def test_photo_feed_survives_reload_with_a_running_job(browser, live):
    import images
    login = "runner"
    live.add_user(login)

    async def held(request):
        """Генерация «долгого заката» «идёт», пока задание не отменят (предел — 30 с)."""
        deadline = time.monotonic() + 30
        while (json.loads(request.content)["prompt"] == "долгий закат" and time.monotonic() < deadline
               and not any(j.state == "cancelling" for j in images.JOBS.values())):
            await asyncio.sleep(0.01)
        return await live.gw.builtin(request)

    running = "#photo-feed .photo-card[data-state=working]"
    #: кнопки просмотра, начинающие задание, и «Описать фото с устройства…»: закрыты ли
    starts = ("[...document.querySelectorAll('#viewer .act-edit, #viewer .act-repeat, #viewer .act-describe,"
              " #describe-file')].map(b => b.disabled).concat(document.querySelector('#viewer .act-animate').disabled)")
    live.gw.on["/slot/images/generations"] = held
    try:
        r = browser.drive(live.url + "/", [*photo_screen(login, cards=1), ("wait", done_cards(1)),
            ("type", "#photo-prompt", "долгий закат"), ("click", "#photo-go"),
            ("wait", f"document.querySelector('{running} .card-cancel')"),
            ("eval", "goBusy", "document.querySelector('#photo-go').disabled"),
            ("click", NEWEST_DONE), ("wait", is_open("#viewer")),
            ("eval", "startsBusy", starts),
            ("press", "Escape"), ("wait", "!document.querySelector('#viewer')"),
            ("idle",),
            ("eval", "reload", "location.reload()"),
            ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"),
            ("wait", f"document.querySelector('{running} .card-cancel')"),
            ("wait", f"/Генерирую · \\d+:\\d\\d/.test(document.querySelector('{running}').textContent)"),
            ("eval", "goBusyAfter", "document.querySelector('#photo-go').disabled"),
            ("click", f"{running} .card-cancel"),
            ("wait", "document.querySelector('#photo-feed .photo-card[data-state=cancelled]')"),
            ("eval", "final", "document.querySelector('#photo-feed .photo-card').textContent"),
            ("eval", "goFree", "!document.querySelector('#photo-go').disabled"),
            ("click", NEWEST_DONE), ("wait", is_open("#viewer")),
            ("eval", "startsFree", starts)], timeout=20000)
    finally:
        live.gw.on.pop("/slot/images/generations", None)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["goBusy"] and v["goBusyAfter"] and v["goFree"]
    # B1-M2: пока идёт своё задание, просмотр не начинает второе; «Оживить» — раздел видео, открыт
    assert v["startsBusy"] == [True, True, True, True, False] and v["startsFree"] == [False] * 5
    assert "Отменено" in v["final"]
    assert any(s.endswith("/cancel") for s in r["sent"])
    assert not problems(r), problems(r)


def test_editor_strength_out_of_range_is_refused_in_words(browser, live):
    """B1-M9: сила перерисовки вне 0.05–1 — отказ словами в окне, запрос не уходит."""
    login = "strength"
    live.add_user(login)
    edit_error = "document.querySelector('#edit-error').textContent"
    r = browser.drive(live.url + "/", [*photo_screen(login, cards=1), ("wait", done_cards(1)),
        ("click", NEWEST_DONE), ("wait", is_open("#viewer")),
        ("click", "#viewer .act-edit"), ("wait", is_open("#editor")),
        ("type", "#edit-prompt", "make it blue"),
        set_value("#edit-strength", "5"), ("click", "#edit-submit"),
        ("wait", f"{edit_error}.length > 0"), ("eval", "high", edit_error),
        set_value("#edit-strength", "0"), ("click", "#edit-submit"),
        ("eval", "low", f"{is_open('#editor')} && {edit_error}"),
        set_value("#edit-strength", "0.5"), ("click", "#edit-submit"),
        ("wait", f"!{is_open('#editor')} && {done_cards(2)}")], width=390, height=900, mobile=True)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["high"] == v["low"] == "Сила перерисовки — число от 0.05 до 1"
    posts = [s for s in r["sent"] if s == "POST /api/images"]
    assert len(posts) == 2                       # картинка ленты и последняя, верная попытка правки
    assert not problems(r), problems(r)


#: опрос задания идёт медленно (1 с в пути); window.polls — сколько опросов в пути сейчас и максимум
SLOW_POLLS = """(() => { window.polls = { now: 0, max: 0 }; const real = window.fetch;
  window.fetch = async (url, opts = {}) => {
    if ((opts.method || 'GET') !== 'GET' || !/^\\/api\\/images\\/[0-9a-f]+$/.test(String(url))) return real(url, opts);
    window.polls.now += 1; window.polls.max = Math.max(window.polls.max, window.polls.now);
    try { await new Promise(r => setTimeout(r, 1000)); return await real(url, opts); } finally { window.polls.now -= 1; }
  }; return true; })()"""


def test_photo_cancel_during_a_poll_keeps_one_poll_chain(browser, live):
    """B1-M3: «Отменить», пока опрос задания в пути, не заводит второй цепочки опросов."""
    import images
    login = "onechain"
    live.add_user(login)

    async def held(request):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not any(j.state == "cancelling" for j in images.JOBS.values()):
            await asyncio.sleep(0.01)
        await asyncio.sleep(3)                       # «Отменяю» держится: несколько опросов подряд
        return await live.gw.builtin(request)

    running = "#photo-feed .photo-card[data-state=working]"
    live.gw.on["/slot/images/generations"] = held
    try:
        r = browser.drive(live.url + "/", [*photo_screen(login), ("eval", "spy", SLOW_POLLS),
            ("type", "#photo-prompt", "долгий рассвет"), ("click", "#photo-go"),
            ("wait", f"document.querySelector('{running} .card-cancel') && window.polls.now === 1"),
            ("click", f"{running} .card-cancel"),
            ("wait", "document.querySelector('#photo-feed .photo-card[data-state=cancelled]')"),
            ("eval", "polls", "window.polls.max")], timeout=20000)
    finally:
        live.gw.on.pop("/slot/images/generations", None)
    assert not r.get("failed"), r
    assert r["values"]["polls"] == 1
    assert not problems(r), problems(r)


def test_photo_layout_fits_390(browser, live):
    login = "narrow"
    live.add_user(login)
    grid = "document.querySelector('#photo-feed .feed-grid')"
    inside = """(sel) => [...document.querySelectorAll(sel)].every(e => { const b = e.getBoundingClientRect();
      return b.width === 0 || (b.left >= 0 && b.right <= innerWidth); })"""
    r = browser.drive(live.url + "/", [*photo_screen(login, cards=3), ("wait", done_cards(3)),
        ("eval", "overflow", f"!({NO_OVERFLOW}) || document.querySelector('#view').scrollWidth"
                             " > document.querySelector('#view').clientWidth"),
        ("eval", "cols", f"getComputedStyle({grid}).gridTemplateColumns.split(' ').length"),
        ("eval", "day", "document.querySelector('#photo-feed .feed-day-title').textContent"),
        ("eval", "aboveTabs", "document.querySelector('#photo-composer').getBoundingClientRect().bottom"
                              " <= document.querySelector('#rail').getBoundingClientRect().top"),
        ("eval", "chips", f"({inside})('#photo-composer .chip, #photo-go')"),
        ("click", "#chip-size"), ("wait", "!document.querySelector('#chip-size-menu').hidden"),
        ("eval", "sizeMenu", f"({inside})('#chip-size-menu .chip-option')"),
        ("eval", "sizes", "[...document.querySelectorAll('#chip-size-menu .chip-option')].map(e => e.textContent)"),
        ("click", "#chip-size-menu [data-value=landscape]"),
        ("eval", "sizeChip", "document.querySelector('#chip-size').textContent"),
        ("click", NEWEST_DONE), ("wait", is_open("#viewer")),
        ("eval", "viewer", f"({inside})('#viewer button, #viewer a') && {NO_OVERFLOW}"),
        ("eval", "actions", "[...document.querySelectorAll('#viewer [class*=act-]')].map(e => e.textContent)"),
        ("press", "Escape"), ("wait", "!document.querySelector('#viewer')")], width=390, mobile=True)
    assert not r.get("failed"), r
    v = r["values"]
    assert not v["overflow"] and v["cols"] == 2 and v["day"] == "Сегодня" and v["aboveTabs"]
    assert v["chips"] and v["sizeMenu"] and v["viewer"]
    assert v["sizes"] == ["Портрет 832×1216", "Квадрат 1024×1024", "Альбом 1216×832"]
    assert "Альбом" in v["sizeChip"]
    assert v["actions"] == ["Изменить", "Оживить", "Повторить с тем же seed", "Описать", "Скачать", "Удалить"]
    assert not problems(r), problems(r)


def test_photo_viewer_actions_and_release_for_the_admin(browser, live):
    login = "boss"
    live.add_user(login, is_admin=True)
    g0, rel0 = len(live.gw.generations), len(live.gw.releases)
    grid = "document.querySelector('#photo-feed .feed-grid')"
    cards = "document.querySelectorAll('#photo-feed .photo-card').length"
    r = browser.drive(live.url + "/", [*photo_screen(login, cards=2), ("wait", done_cards(2)),
        ("eval", "cols", f"getComputedStyle({grid}).gridTemplateColumns.split(' ').length"),
        ("eval", "caption", "document.querySelector('#photo-feed .photo-card .card-cap').textContent"),
        ("click", "#photo-menu-toggle"), ("click", "#photo-release"),
        ("wait", "document.querySelector('#photo-status').textContent.length > 0"),
        ("eval", "released", "document.querySelector('#photo-status').textContent"),
        # «Скачать», «Повторить с тем же seed»
        ("click", NEWEST_DONE), ("wait", is_open("#viewer")), ("eval", "spy", CLICKS),
        ("click", "#viewer .act-download"), ("wait", "window.clicked.length === 1"),
        ("eval", "download", "window.clicked[0]"),
        ("wait", "document.querySelector('#viewer .chain-step')"),          # цепочку просмотр догружает
        ("eval", "meta", "document.querySelector('#viewer .chain-step:last-child .chain-meta').textContent"),
        ("click", "#viewer .act-repeat"),
        ("wait", f"!document.querySelector('#viewer') && {done_cards(3)}"),
        # «Удалить» — через окно подтверждения
        ("click", NEWEST_DONE), ("wait", is_open("#viewer")),
        ("click", "#viewer .act-delete"), ("wait", is_open("#photo-delete-dialog")),
        ("click", "#photo-delete-confirm"),
        ("wait", f"!document.querySelector('#viewer') && {cards} === 2"),
        # «Оживить» — в раздел видео с картинкой
        ("click", NEWEST_DONE), ("wait", is_open("#viewer")),
        ("click", "#viewer .act-animate"),
        # адрес меняется сразу, а рельс — в render по hashchange, позже: ждать обоих
        ("wait", "location.hash.startsWith('#/video?source=')"
                 " && document.querySelector('#rail a[aria-current=page]')?.dataset.section === 'video'"),
        ("eval", "hash", "location.hash"),
        ("eval", "current", "document.querySelector('#rail a[aria-current=page]').dataset.section"),
        ("eval", "items", f"(async () => (await {API}.api('GET', '/api/images')).items"
                          ".map(i => [i.prompt, i.seed, i.size, i.media_id]))()")])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["cols"] == 6 and v["caption"] == "Lustify"
    assert v["released"] == "Lustify — освобождена; FLUX — освобождена; gemma-abl — освобождена"
    # две картинки ленты и повтор (Lustify) — release FLUX до запроса; кнопка — три модели
    lustify, flux = MODEL_IDS["lustify"], MODEL_IDS["flux"]
    assert [m for m, _ in live.gw.releases[rel0:]] == [flux, flux, lustify, flux, "gemma4-abl", flux]
    newest = v["items"][0]
    assert [i[:3] for i in v["items"]] == [["кот 1", 101, "1024x1024"], ["кот 0", 100, "832x1216"]]
    assert v["download"][0].endswith("?download=1") and v["download"][1] and "seed 101" in v["meta"]
    repeat = live.gw.generations[g0 + 2:]
    assert len(repeat) == 1 and repeat[0][0]["seed"] == 101 and repeat[0][0]["prompt"] == "кот 1"
    assert repeat[0][0]["size"] == "1024x1024" and repeat[0][0]["model"] == MODEL_IDS["lustify"]
    assert any(s.startswith("DELETE /api/images/") for s in r["sent"])
    assert v["hash"] == f"#/video?source={newest[3]}" and v["current"] == "video"
    assert not problems(r), problems(r)


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


def media_of_size(mib, header):
    """Ответ /api/media/<id> подменён: header — 8 байт с Content-Length в `mib` МиБ (отказ до чтения),
    иначе — настоящие `mib` МиБ без заголовка, как chunked (размер виден только после чтения)."""
    body = (f"new Blob([new Uint8Array(8)], {{ type: 'image/png' }}), {{ headers: {{ 'Content-Type': 'image/png',"
            f" 'Content-Length': String({mib} * 1024 * 1024) }} }}" if header
            else "big, { headers: { 'Content-Type': 'image/png' } }")
    return ("eval", "big", f"""(() => {{ const real = window.fetch; const mb = new Uint8Array(1024 * 1024);
      const big = {'null' if header else f"new Blob(Array({mib}).fill(mb), {{ type: 'image/png' }})"};
      window.fetch = (url, opts) => /^\\/api\\/media\\/[0-9a-f]{{32}}$/.test(String(url))
        ? Promise.resolve(new Response({body})) : real(url, opts);
      return new Response(new Blob([mb])).headers.get('Content-Length'); }})()""")


@pytest.mark.parametrize("header", [True, False])
def test_too_big_for_the_phone_is_downloaded_not_shared(browser, live, header):
    login = f"iosbig{int(header)}"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*photo_screen(login, cards=1), ("wait", done_cards(1)), iphone(),
        media_of_size(200 if header else 101, header),
        ("eval", "spy", CLICKS), ("click", "#photo-feed .card-save"), ("wait", "window.clicked.length === 1"),
        ("eval", "shared", "window.shared.length"), ("eval", "href", "window.clicked[0][0]")], width=390, mobile=True)
    assert not r.get("failed"), r
    assert r["values"]["big"] is None                      # Response из Blob заголовка не несёт
    assert r["values"]["shared"] == 0 and r["values"]["href"].endswith("?download=1")


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
        ("click", NEWEST_DONE), ("wait", is_open("#viewer")), ("wait", f"{steps}.length === 2"),
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


#: правка кистью через API страницы по первой картинке ленты: mask_media_id готовой правки
BRUSH_EDIT = f"""(async () => {{ const m = {API}; const source = (await m.api('GET', '/api/images')).items[0];
  const canvas = document.createElement('canvas'); canvas.width = 64; canvas.height = 48;
  const ctx = canvas.getContext('2d'); ctx.fillStyle = 'black'; ctx.fillRect(0, 0, 64, 48);
  ctx.fillStyle = 'white'; ctx.fillRect(0, 0, 16, 16);
  const mask = await new Promise(r => canvas.toBlob(r, 'image/png'));
  const body = new FormData();
  for (const [k, v] of Object.entries({{ model: 'flux', size: 'original', prompt: 'patch', source_media_id: source.media_id }}))
    body.append(k, v);
  body.append('mask', mask, 'mask.png');
  const job = await m.api('POST', '/api/images', body);
  for (let t = 0; t < 200; t++) {{
    const item = await m.api('GET', '/api/images/' + job.id);
    if (item.state === 'done') return [source.media_id, item.mask_media_id];
    await new Promise(r => setTimeout(r, 30));
  }}
  return null; }})()"""


def test_photo_repeat_of_a_brush_edit_keeps_its_mask(browser, live):
    """B1-M1: «Повторить» правки кистью шлёт её маску — меняется снова только выделенное."""
    login = "remask"
    live.add_user(login)
    e0 = len(live.gw.edits)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), make_images(1),
        ("eval", "edit", BRUSH_EDIT), *go("photo"), ("wait", FEED_READY), ("wait", done_cards(2)),
        ("eval", "spy", SPY),
        ("click", NEWEST_DONE), ("wait", is_open("#viewer")), ("click", "#viewer .act-repeat"),
        ("wait", f"!document.querySelector('#viewer') && {done_cards(3)}"),
        ("eval", "forms", "window.sentForms"),
        ("eval", "masks", f"(async () => (await {API}.api('GET', '/api/images')).items.map(i => i.mask_media_id))()")])
    assert not r.get("failed"), r
    v = r["values"]
    source, mask = v["edit"]
    assert mask and v["masks"] == [mask, mask, None]
    (sent,) = v["forms"]
    assert sent["path"] == "/api/images" and sent["form"]["mask_media_id"] == mask
    assert sent["form"]["source_media_id"] == source and sent["form"]["size"] == "original"
    edits = live.gw.edits[e0:]
    assert len(edits) == 2 and edits[0][1] == edits[1][1]       # тот же исходник у шлюза
    assert not problems(r), problems(r)


FEED_MODULE = """(async () => {
  const { createFeed } = await import('/static/js/feed.js');
  const box = document.createElement('div');
  box.style.cssText = 'position:fixed;inset:0;overflow:auto;z-index:99;background:#fff';
  document.body.append(box);
  const day = new Date(); day.setHours(0, 0, 0, 0);
  const today = day.getTime() / 1000;
  const all = [];
  for (let i = 0; i < 25; i++) all.push({ id: 'i' + i,
    created: i < 5 ? today + 100 - i : i < 10 ? today - 3600 - i : today - 5 * 86400 - i });
  const calls = [];
  const card = (item) => { const n = document.createElement('div'); n.className = 'card';
    n.style.height = '1000px'; n.textContent = item.id + (item.tag || ''); return n; };
  const feed = createFeed(box, { key: (item) => item.id, render: card, load: async (last) => {
    calls.push(last?.id ?? null);
    return all.filter(x => last === undefined || x.created < last.created).slice(0, 10); } });
  const sleep = (ms) => new Promise(r => setTimeout(r, ms));
  for (let i = 0; i < 100 && box.dataset.feed !== 'end'; i++) { box.scrollTop = box.scrollHeight; await sleep(30); }
  const titles = () => [...box.querySelectorAll('.feed-day-title')].map(t => t.textContent);
  const out = { end: box.dataset.feed, titles: titles(), count: box.querySelectorAll('.card').length, calls: calls.slice() };
  feed.prepend({ id: 'new', created: today + 200 });
  feed.update({ id: 'i0', created: today + 100, tag: '*' });
  for (let i = 5; i < 10; i++) feed.remove('i' + i);
  out.after = { first: box.querySelector('.card').textContent, updated: [...box.querySelectorAll('.card')][1].textContent,
                titles: titles(), count: box.querySelectorAll('.card').length };
  await feed.reload();
  out.reloaded = { calls: calls.length, count: box.querySelectorAll('.card').length };
  box.remove();
  return out;
})()"""


def test_photo_feed_module_groups_days_and_pages(browser, live):
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY), ("eval", "feed", FEED_MODULE)])
    assert not r.get("failed"), r
    f = r["values"]["feed"]
    assert f["end"] == "end" and f["count"] == 25
    assert f["titles"][:2] == ["Сегодня", "Вчера"] and len(f["titles"]) == 3
    assert f["titles"][2][0].isdigit()                       # «15 октября»
    assert f["calls"] == [None, "i9", "i19", "i24"]          # три страницы и пустая — после последней записи
    assert f["after"] == {"first": "new", "updated": "i0*", "titles": ["Сегодня", f["titles"][2]], "count": 21}
    assert f["reloaded"]["count"] == 10 and f["reloaded"]["calls"] == 5
    assert not problems(r), problems(r)


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_rail_labels_fit_their_tabs(browser, live, width):
    """Вкладки по порядку: «Речь», затем «Разговоры»; «Перевод»
    после «Сканов» (спека 2026-10-28-studio-translator, 6.1), «Код» — после «Перевода» (спека
    2026-10-28-studio-claude-code, 6.5); все восемь подписей целиком и на 320 px, страница
    без горизонтальной прокрутки. На телефоне шапка раздела кончается у лупы — справа сверху три значка (поиск,
    «В работе», профиль), место прежней кнопки темы отдано заголовку."""
    fits = """[...document.querySelectorAll('#rail .tab span')].map(s => {
      const b = s.getBoundingClientRect(); return [s.textContent, b.left >= 0 && b.right <= innerWidth,
      s.scrollWidth <= s.clientWidth]; })"""
    spacing = """(() => {
      const boxes = [...document.querySelectorAll('#rail .tab .ico')].map(n => n.getBoundingClientRect());
      const centers = boxes.map(b => b.left + b.width / 2);
      const gaps = centers.slice(1).map((x, i) => x - centers[i]);
      const labels = [...document.querySelectorAll('#rail .tab span')].map(n => n.getBoundingClientRect());
      return [Math.max(...gaps) - Math.min(...gaps), labels.every((b, i) => !i || b.left >= labels[i - 1].right)];
    })()"""
    room = """(() => { const h = document.querySelector('#view .head'); const end = h.getBoundingClientRect().right
      - parseFloat(getComputedStyle(h).paddingRight); return document.querySelector('#search-open').getBoundingClientRect().left - end; })()"""
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY), ("wait", "document.querySelector('#view .head')"),
                                       ("eval", "fits", fits), ("eval", "spacing", spacing),
                                       ("eval", "room", room), ("eval", "page", NO_OVERFLOW)],
                      width=width, mobile=width < 760)
    assert not r.get("failed"), r
    assert r["values"]["page"] is True
    labels = {name: (inside, whole) for name, inside, whole in r["values"]["fits"]}
    assert [name for name, _, _ in r["values"]["fits"]] == ["Чат", "Фото", "Видео", "Речь", "Разговоры", "Сканы", "Перевод",
                                                            "Агенты"]
    assert all(inside and whole for inside, whole in labels.values()), labels
    if width < 760:
        assert r["values"]["spacing"][0] < 1 and r["values"]["spacing"][1], r["values"]["spacing"]
        assert 0 <= r["values"]["room"] <= 16, r["values"]["room"]


def grid_cols(feed):
    return f"getComputedStyle(document.querySelector('{feed} .feed-grid')).gridTemplateColumns.split(' ').length"


@pytest.mark.parametrize("width", [390, 1280])
def test_photo_views_preview_tiles_and_table_are_kept(browser, live, width):
    login = f"views{width}"
    live.add_user(login)
    card = "#photo-feed .photo-card"
    rows = (f"[...document.querySelectorAll('{card}')].map(r => [r.querySelector('.cell-prompt').textContent,"
            " ...[...r.querySelectorAll('.cell-meta > span')].map(s => s.textContent)])")
    # заголовок «Промпт» — над промптами строк
    aligned = (f"Math.abs(document.querySelectorAll('#photo-feed .feed-head span')[1].getBoundingClientRect().left"
               f" - document.querySelector('{card} .cell-prompt').getBoundingClientRect().left) < 1")
    newest = (f"(() => {{ const c = document.querySelector('{card}'); const o = c.querySelector('.card-open').getBoundingClientRect();"
              " return [c.querySelector('.card-prompt').textContent, c.querySelector('.card-meta').textContent,"
              " c.querySelector('img').getAttribute('srcset'), o.width / o.height]; })()")
    r = browser.drive(live.url + "/", [*photo_screen(login, cards=3), ("wait", done_cards(3)),
        ("eval", "pressed", "[...document.querySelectorAll('#view .view-menu [aria-checked=true]')].map(b => b.id)"),
        ("eval", "tiles", grid_cols("#photo-feed")),
        ("click", "#photo-view"), ("click", "#photo-view-table"),
        ("wait", "document.querySelector('#photo-feed').dataset.view === 'table' && document.querySelector('#photo-feed .cell-prompt')"),
        ("eval", "head", "getComputedStyle(document.querySelector('#photo-feed .feed-head')).display"),
        ("eval", "rows", rows),
        ("eval", "aligned", f"{width} < 760 || {aligned}"),
        ("eval", "tableFits", f"{NO_OVERFLOW} && document.querySelector('#photo-feed').scrollWidth <= document.querySelector('#photo-feed').clientWidth"),
        ("click", NEWEST_DONE), ("wait", is_open("#viewer")), ("wait", "document.querySelector('#viewer .chain-step')"),
        ("press", "Escape"), ("wait", "!document.querySelector('#viewer')"),
        ("click", "#photo-view"), ("click", "#photo-view-preview"), ("wait", "document.querySelector('#photo-feed .card-info')"),
        ("eval", "previewCols", grid_cols("#photo-feed")),
        ("eval", "newest", newest),
        ("idle",), ("eval", "reload", "location.reload()"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY} && {FEED_READY}"),
        ("eval", "kept", "fetch('/api/me').then(r => r.json()).then(m => [document.querySelector('#photo-feed').dataset.view,"
                         " m.views.photo, document.querySelector('#photo-view-preview').getAttribute('aria-checked')])")],
        width=width, mobile=width == 390)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["pressed"] == ["photo-view-tiles"] and v["tiles"] == (6 if width == 1280 else 2)
    assert v["head"] == ("grid" if width == 1280 else "none") and v["aligned"] and v["tableFits"]
    assert [row[0] for row in v["rows"]] == ["кот 2", "кот 1", "кот 0"]
    prompt, model, size, seed, when = v["rows"][0]
    assert (model, size, seed) == ("Lustify", "1216×832", "seed 102") and re.fullmatch(r"\d\d:\d\d", when)
    assert (v["previewCols"] == 1) if width == 390 else (v["previewCols"] >= 3)
    text, meta, srcset, ratio = v["newest"]
    assert text == "кот 2" and meta.startswith("Lustify · 1216×832 · seed 102 · ")
    assert re.fullmatch(r"/api/media/\w+\?thumb=1 384w, /api/media/\w+ 1216w", srcset)
    assert abs(ratio - 1216 / 832) < 0.02                       # картинка целиком, без обрезки
    assert v["kept"] == ["preview", "preview", "true"]
    assert not problems(r), problems(r)


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_views_menu_closes_by_choice_escape_and_outside(browser, live, width):
    """Вид ленты — одна кнопка со значком текущего вида: меню из трёх пунктов (внутри окна и на телефоне)
    закрывается выбором, Escape (фокус — на кнопку) и нажатием вне."""
    login = f"vmenu{width}"
    live.add_user(login)
    menu = "document.querySelector('#photo-view + .view-menu')"
    state = (f"[{menu}.hidden, document.querySelector('#photo-view').getAttribute('aria-expanded'),"
             " document.activeElement.id]")
    # значок кнопки — тот же, что у пункта вида
    shows = "document.querySelector('#photo-view svg').innerHTML === document.querySelector('#photo-view-{0} svg').innerHTML"
    r = browser.drive(live.url + "/", [*photo_screen(login),
        ("eval", "button", f"[{menu}.hidden, document.querySelector('#photo-view').getAttribute('aria-haspopup'),"
                           f" {shows.format('tiles')}]"),
        ("click", "#photo-view"), ("wait", f"!{menu}.hidden"),
        ("eval", "items", f"[...{menu}.children].map(b => [b.id, b.getAttribute('role'), b.getAttribute('aria-checked'),"
                          " b.textContent.trim(), !!b.querySelector('svg')])"),
        ("eval", "inside", f"(() => {{ const b = {menu}.getBoundingClientRect();"
                           f" return b.left >= 0 && b.right <= innerWidth && b.bottom <= innerHeight && {NO_OVERFLOW}; }})()"),
        ("click", "#photo-view-table"), ("wait", "document.querySelector('#photo-feed').dataset.view === 'table'"),
        ("eval", "chosen", state), ("eval", "shows", shows.format("table")),
        ("click", "#photo-view"), ("wait", f"!{menu}.hidden"), ("press", "Escape"),
        ("eval", "escaped", state),
        ("click", "#photo-view"), ("wait", f"!{menu}.hidden"), ("click", ".photo .head h1"),
        ("eval", "outside", state)],
        width=width, mobile=width < 760)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["button"] == [True, "menu", True]
    assert v["items"] == [["photo-view-preview", "menuitemradio", "false", "Превью", True],
                          ["photo-view-tiles", "menuitemradio", "true", "Плитка", True],
                          ["photo-view-table", "menuitemradio", "false", "Таблица", True]]
    assert v["inside"] and v["shows"]
    assert v["chosen"] == [True, "false", "photo-view"]
    assert v["escaped"] == [True, "false", "photo-view"]
    assert v["outside"][:2] == [True, "false"]
    assert not problems(r), problems(r)


# ---- выделение нескольких записей ленты (select.js) ---------------------------------

PHOTO_CARD = "#photo-feed .photo-card"


def nth(i, inner="", card=PHOTO_CARD):
    """i-я карточка ленты (все записи теста — «Сегодня», одна сетка дня) или элемент в ней."""
    return f"{card}:nth-child({i}){' ' + inner if inner else ''}"


def ids(feed="#photo-feed"):
    return f"[...document.querySelectorAll('{feed} .card[data-id]')].map(c => c.dataset.id)"


def picked(feed="#photo-feed"):
    return f"[...document.querySelectorAll('{feed} .card[data-selected=true]')].map(c => c.dataset.id)"


def selecting(root=".photo"):
    return f"document.querySelector('{root}').hasAttribute('data-selecting')"


def text_of(selector):
    return f"document.querySelector({selector!r}).textContent"


def long_press(selector):
    """Палец на карточке 650 мс без сдвига, затем отпустить; следом браузер шлёт click (хвост касания)."""
    return ("eval", "pressed", f"""(async () => {{ const e = document.querySelector({selector!r});
      e.scrollIntoView({{ block: 'center' }}); const b = e.getBoundingClientRect();
      const at = {{ bubbles: true, cancelable: true, pointerType: 'touch', isPrimary: true, pointerId: 7,
        clientX: b.left + b.width / 2, clientY: b.top + b.height / 2 }};
      e.dispatchEvent(new PointerEvent('pointerdown', at));
      await new Promise(r => setTimeout(r, 650));
      e.dispatchEvent(new PointerEvent('pointerup', at));
      e.click(); return true; }})()""")


def shift_click(selector):
    return ("eval", "shift", f"""(() => {{ document.querySelector({selector!r}).dispatchEvent(
      new MouseEvent('click', {{ bubbles: true, cancelable: true, shiftKey: true }})); return true; }})()""")


def deletes(mode):
    """DELETE /api/images/<id> страницы: второй — 503 «хранилище недоступно» (fail) или ждёт window.release() (hold)."""
    return ("eval", "deletes", f"""(() => {{ const real = window.fetch; let n = 0;
      window.fetch = (url, opts = {{}}) => {{
        if (opts.method === 'DELETE' && String(url).startsWith('/api/images/') && ++n === 2) {{
          if ('{mode}' === 'fail') return Promise.resolve(new Response(JSON.stringify(
            {{ detail: {{ code: 'storage', message: 'хранилище недоступно' }} }}),
            {{ status: 503, headers: {{ 'Content-Type': 'application/json' }} }}));
          return new Promise((done) => {{ window.release = () => done(real(url, opts)); }});
        }}
        return real(url, opts);
      }}; return true; }})()""")


SERVER_IMAGES = f"(async () => (await {API}.api('GET', '/api/images')).items.map(i => String(i.id)))()"


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_photo_selection_deletes_the_picked_and_keeps_the_rest(browser, live, width):
    """Режим выделения: меню вида внутри окна и на 320 px, где кнопка вида уходит в начало второй строки шапки."""
    login = f"sel{width}"
    live.add_user(login)
    # все три пункта меню вида — в окне и нажимаются (elementFromPoint — сам пункт)
    menu_in = """[...document.querySelectorAll('#photo-view-menu .view-option')].map(e => { const b = e.getBoundingClientRect();
      return b.left >= 0 && b.right <= innerWidth && e.contains(document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2)); })"""
    r = browser.drive(live.url + "/", [*photo_screen(login, cards=4), ("wait", done_cards(4)),
        ("eval", "all", ids()),
        ("click", "#photo-select"), ("wait", selecting()),
        ("click", nth(1)), ("click", nth(3)),
        ("eval", "viewer", "!!document.querySelector('#viewer')"),
        ("eval", "count", text_of("#photo-select-count")),
        ("eval", "picked", picked()),
        ("click", "#photo-view"), ("wait", "!document.querySelector('#photo-view-menu').hidden"),
        ("eval", "menuIn", menu_in), ("click", "#photo-view-table"),
        ("wait", "document.querySelector('#photo-feed').dataset.view === 'table'"),
        ("eval", "pickedTable", picked()),
        ("eval", "fits", NO_OVERFLOW),
        ("eval", "bar", "getComputedStyle(document.querySelector('.photo .select-bar')).display !== 'none'"
                        " && getComputedStyle(document.querySelector('#photo-composer')).display === 'none'"),
        ("eval", "inHead", "!!document.querySelector('.photo .head #photo-select-delete')"),
        ("click", "#photo-view"), ("click", "#photo-view-tiles"),
        ("wait", "document.querySelector('#photo-feed').dataset.view === 'tiles'"),
        ("eval", "pickedTiles", picked()),
        ("click", "#photo-select-delete"), ("wait", is_open("#photo-select-delete-dialog")),
        ("eval", "title", text_of("#photo-select-delete-dialog h2")),
        ("click", "#photo-select-delete-confirm"),
        ("wait", f"document.querySelectorAll('{PHOTO_CARD}').length === 2 && !{selecting()}"),
        ("eval", "left", ids()), ("eval", "server", SERVER_IMAGES)],
        width=width, mobile=width < 760)
    assert not r.get("failed"), r
    v = r["values"]
    first, second, third, fourth = v["all"]
    assert v["viewer"] is False and v["count"] == "Выбрано 2"
    assert v["menuIn"] == [True, True, True], v["menuIn"]
    assert sorted(v["picked"]) == sorted(v["pickedTable"]) == sorted(v["pickedTiles"]) == sorted([first, third])
    assert v["title"] == "Удалить 2 картинки?"
    assert v["left"] == [second, fourth] and sorted(v["server"]) == sorted([second, fourth])
    if width < 760:
        assert v["fits"] and v["bar"] and not v["inHead"]
    else:
        assert v["inHead"]
    assert not problems(r), problems(r)


@pytest.mark.parametrize("width", [390, 1280])
def test_photo_selection_by_long_press_hover_shift_and_day(browser, live, width):
    login = f"pick{width}"
    live.add_user(login)
    steps = [*photo_screen(login, cards=4), ("wait", done_cards(4)), ("eval", "all", ids())]
    if width == 390:
        steps += [long_press(nth(3, ".card-open")), ("wait", selecting()),
                  ("eval", "viewer", "!!document.querySelector('#viewer')"), ("eval", "first", picked())]
    else:
        steps += [("click", nth(1, ".card-pick")), ("wait", selecting()),
                  ("eval", "viewer", "!!document.querySelector('#viewer')"), ("eval", "first", picked()),
                  shift_click(nth(4)), ("eval", "range", picked())]
    steps += [("click", "#photo-feed .day-pick"), ("eval", "day1", picked()),
              ("click", "#photo-feed .day-pick"), ("eval", "day2", picked()),
              ("eval", "allLabel", text_of("#photo-select-all")),
              ("click", "#photo-select-all"), ("eval", "afterAll", picked()),
              ("eval", "allLabel2", text_of("#photo-select-all")),
              ("press", "Escape"), ("wait", f"!{selecting()}"), ("eval", "cleared", picked())]
    r = browser.drive(live.url + "/", steps, width=width, mobile=width == 390)
    assert not r.get("failed"), r
    v = r["values"]
    every = v["all"]
    assert v["viewer"] is False and v["cleared"] == []
    if width == 390:                      # третья → день целиком → день снят → «Все» отмечает всё
        assert v["first"] == [every[2]]
        assert v["day1"] == every and v["day2"] == []
        assert (v["allLabel"], v["afterAll"], v["allLabel2"]) == ("Все", every, "Снять")
    else:                                 # первая → Shift до четвёртой → день снят и снова отмечен → «Снять»
        assert v["first"] == [every[0]] and v["range"] == every
        assert v["day1"] == [] and v["day2"] == every
        assert (v["allLabel"], v["afterAll"], v["allLabel2"]) == ("Снять", [], "Все")
    assert not problems(r), problems(r)


def test_photo_selection_survives_a_new_card_and_counts_gone_as_deleted(browser, live):
    live.add_user("selnew")
    gone_elsewhere = ("eval", "gone", f"""(async () => {{ const id = document.querySelector('{nth(1)}').dataset.id;
      await {API}.api('DELETE', '/api/images/' + id); return id; }})()""")
    r = browser.drive(live.url + "/", [*photo_screen("selnew", cards=3), ("wait", done_cards(3)),
        ("eval", "all", ids()),
        ("click", "#photo-select"), ("click", nth(1)), ("click", nth(2)),
        gone_elsewhere,                                          # удалена в другой вкладке
        ("type", "#photo-prompt", "новый кот"), ("click", "#photo-go"), ("wait", done_cards(4)),
        ("eval", "newest", f"(() => {{ const c = document.querySelector('{nth(1)}');"
                           " return [c.dataset.id, !!c.querySelector('.card-pick'), c.dataset.selected]; })()"),
        ("eval", "count", text_of("#photo-select-count")),
        ("click", "#photo-select-delete"), ("wait", is_open("#photo-select-delete-dialog")),
        ("eval", "title", text_of("#photo-select-delete-dialog h2")),
        ("click", "#photo-select-delete-confirm"), ("wait", f"!{selecting()}"),
        ("eval", "error", text_of("#photo-error")), ("eval", "left", ids())], width=1280)
    assert not r.get("failed"), r
    v = r["values"]
    newest, has_pick, selected = v["newest"]
    assert has_pick and selected == "false" and v["count"] == "Выбрано 2"
    assert v["title"] == "Удалить 2 картинки?" and v["error"] == ""
    assert v["left"] == [newest, v["all"][2]]
    assert not known_refusals(problems(r), 404), problems(r)   # DELETE уже удалённой — 404 gone


def test_photo_selection_escape_in_the_dialog_keeps_the_mode(browser, live):
    live.add_user("selesc")
    r = browser.drive(live.url + "/", [*photo_screen("selesc", cards=1), ("wait", done_cards(1)),
        ("click", "#photo-select"), ("click", nth(1)),
        ("click", "#photo-select-delete"), ("wait", is_open("#photo-select-delete-dialog")),
        ("press", "Escape"), ("wait", f"!{is_open('#photo-select-delete-dialog')}"),
        ("eval", "mode", selecting()), ("eval", "count", text_of("#photo-select-count"))], width=1280)
    assert not r.get("failed"), r
    assert r["values"]["mode"] is True and r["values"]["count"] == "Выбрано 1"
    assert not problems(r), problems(r)


def test_photo_selection_failure_is_told_and_keeps_the_failed_picked(browser, live):
    live.add_user("selfail")
    r = browser.drive(live.url + "/", [*photo_screen("selfail", cards=3), ("wait", done_cards(3)),
        ("eval", "all", ids()), deletes("fail"),
        ("click", "#photo-select"), ("click", "#photo-select-all"),
        ("click", "#photo-select-delete"), ("click", "#photo-select-delete-confirm"),
        ("wait", "document.querySelector('#photo-error').textContent"),
        ("eval", "error", text_of("#photo-error")), ("eval", "mode", selecting()),
        ("eval", "picked", picked()), ("eval", "left", ids())], width=1280)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["error"] == "Не удалось удалить 1 из 3: Хранилище недоступно"
    assert v["mode"] is True and v["picked"] == v["left"] == [v["all"][1]]
    assert not known_refusals(problems(r), 503), problems(r)


def test_photo_selection_cancel_stops_after_the_current_one(browser, live):
    live.add_user("selstop")
    r = browser.drive(live.url + "/", [*photo_screen("selstop", cards=3), ("wait", done_cards(3)),
        ("eval", "all", ids()), deletes("hold"),
        ("click", "#photo-select"), ("click", "#photo-select-all"),
        ("click", "#photo-select-delete"), ("click", "#photo-select-delete-confirm"),
        ("wait", "typeof window.release === 'function'"),
        ("eval", "progress", "document.querySelector('#photo-select-delete-dialog').textContent.includes('Удаляю 2 из 3')"),
        ("click", "#photo-select-delete-dialog .dialog-close"), ("wait", f"!{is_open('#photo-select-delete-dialog')}"),
        ("eval", "released", "window.release() || true"),
        ("wait", "document.querySelector('#photo-error').textContent"),
        ("eval", "error", text_of("#photo-error")), ("eval", "picked", picked()),
        ("eval", "left", ids()), ("eval", "server", SERVER_IMAGES)], width=1280)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["progress"] and v["error"] == "Удалено 2 из 3, остальные не тронуты"
    assert v["picked"] == v["left"] == v["server"] == [v["all"][2]]
    assert not problems(r), problems(r)


def test_photo_two_fingers_do_not_start_the_selection(browser, live):
    """Щипок (второй палец, затем браузер отменяет касания): таймер первого пальца не остаётся жить."""
    live.add_user("pinch")
    pinch = ("eval", "pinch", f"""(async () => {{
      const at = (sel, id, primary) => {{ const e = document.querySelector(sel); const b = e.getBoundingClientRect();
        return [e, {{ bubbles: true, cancelable: true, pointerType: 'touch', isPrimary: primary, pointerId: id,
          clientX: b.left + b.width / 2, clientY: b.top + b.height / 2 }}]; }};
      const [a, pa] = at('{nth(1, ".card-open")}', 11, true);
      const [b, pb] = at('{nth(2, ".card-open")}', 12, false);
      a.dispatchEvent(new PointerEvent('pointerdown', pa));
      b.dispatchEvent(new PointerEvent('pointerdown', pb));
      a.dispatchEvent(new PointerEvent('pointercancel', pa));
      b.dispatchEvent(new PointerEvent('pointercancel', pb));
      await new Promise(r => setTimeout(r, 700)); return true; }})()""")
    r = browser.drive(live.url + "/", [*photo_screen("pinch", cards=2), ("wait", done_cards(2)), pinch,
        ("eval", "mode", selecting()), ("eval", "picked", picked())], width=390, mobile=True)
    assert not r.get("failed"), r
    assert r["values"]["mode"] is False and r["values"]["picked"] == []
    assert not problems(r), problems(r)


#: media id из тел POST /api/media/archives страницы
ARCHIVED = """(() => { const real = window.fetch; window.archived = [];
  window.fetch = (url, opts = {}) => {
    if (opts.method === 'POST' && String(url) === '/api/media/archives') window.archived.push(JSON.parse(opts.body).media_ids);
    return real(url, opts);
  }; return true; })()"""


@pytest.mark.parametrize("width", [390, 1280])
def test_photo_selection_downloads_one_zip(browser, live, width):
    """Компьютер и Android (не iPhone): отмеченные — одним архивом по ссылке на токен."""
    login = f"zip{width}"
    live.add_user(login)
    first_bytes = ("eval", "zip", """(async () => { const r = await fetch(window.clicked[0][0]);
      const b = new Uint8Array(await r.arrayBuffer()); return [r.headers.get('content-type'), b[0], b[1]]; })()""")
    r = browser.drive(live.url + "/", [*photo_screen(login, cards=3), ("wait", done_cards(3)),
        ("eval", "spy", CLICKS), ("eval", "archiving", ARCHIVED),
        ("click", "#photo-select"), ("click", nth(1)), ("click", nth(3)),
        ("eval", "label", text_of("#photo-select-download")),
        ("click", "#photo-select-download"), ("wait", "window.clicked.length === 1"), ("wait", f"!{selecting()}"),
        ("eval", "clicked", "window.clicked"), ("eval", "archived", "window.archived"), first_bytes],
        width=width, mobile=width == 390)
    assert not r.get("failed"), r
    v = r["values"]
    (href, download), = v["clicked"]
    assert re.fullmatch(r"/api/media/archives/[\w-]+", href) and download and v["label"] == "Скачать"
    newest, _, oldest = v["images"][::-1]                     # лента — новые сверху
    assert v["archived"] == [[newest, oldest]] and v["zip"] == ["application/zip", 0x50, 0x4B]
    assert not problems(r), problems(r)


def test_photo_selection_counts_only_files_to_download(browser, live):
    uid = live.add_user("zipcount")
    live_sql(live, "INSERT INTO images(id, user_id, kind, model, prompt, size, seed, state, detail, created)"
                   " VALUES (?, ?, 'generation', 'lustify', 'упавший кот', '832x1216', 1, 'error', 'шлюз не ответил', ?)",
             ("e" * 32, uid, time.time() - 60))
    r = browser.drive(live.url + "/", [*photo_screen("zipcount", cards=1), ("wait", done_cards(1)),
        ("click", "#photo-select"), ("click", "#photo-select-all"),
        ("eval", "both", f"[{text_of('#photo-select-count')}, {text_of('#photo-select-download')}]"),
        ("click", nth(1)),                                     # готовая снята — осталась неудачная
        ("eval", "failedOnly", "document.querySelector('#photo-select-download').disabled")], width=1280)
    assert not r.get("failed"), r
    assert r["values"]["both"] == ["Выбрано 2", "Скачать 1"] and r["values"]["failedOnly"] is True
    assert not problems(r), problems(r)


@pytest.mark.parametrize("fail_first", [False, True])
def test_photo_selection_on_iphone_shares_all_files(browser, live, fail_first):
    login = f"iosmany{int(fail_first)}"
    live.add_user(login)
    steps = [*photo_screen(login, cards=3), ("wait", done_cards(3)), iphone(fail_first), ("eval", "spy", CLICKS),
             ("click", "#photo-select"), ("click", "#photo-select-all"), ("click", "#photo-select-download")]
    if fail_first:
        steps += [("wait", is_open("#save-dialog")), ("eval", "title", text_of("#save-dialog h2")),
                  ("click", "#save-again"), ("wait", "!document.querySelector('#save-dialog')")]
    r = browser.drive(live.url + "/", [*steps, ("wait", "window.shared.length === 1"), ("wait", f"!{selecting()}"),
        ("eval", "shared", "window.shared"), ("eval", "clicked", "window.clicked.length")], width=390, mobile=True)
    assert not r.get("failed"), r
    assert r["values"]["shared"] == [[[".png", "image/png", True]] * 3] and r["values"]["clicked"] == 0
    if fail_first:
        assert r["values"]["title"] == "Файлы готовы"
    assert not problems(r), problems(r)


@pytest.mark.parametrize("header", [True, False])
def test_photo_selection_too_big_for_the_iphone_is_a_zip(browser, live, header):
    login = f"iosmanybig{int(header)}"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*photo_screen(login, cards=2), ("wait", done_cards(2)), iphone(),
        media_of_size(200 if header else 60, header),          # заголовок — отказ до чтения; без него — 2 × 60 > 100
        ("eval", "spy", CLICKS), ("click", "#photo-select"), ("click", "#photo-select-all"),
        ("click", "#photo-select-download"), ("wait", "window.clicked.length === 1"),
        ("eval", "shared", "window.shared.length"), ("eval", "href", "window.clicked[0][0]")], width=390, mobile=True)
    assert not r.get("failed"), r
    assert r["values"]["shared"] == 0 and r["values"]["href"].startswith("/api/media/archives/")


def test_photo_selection_archive_refusal_is_told(browser, live):
    live.add_user("ziprefused")
    refuse = ("eval", "refuse", """(() => { const real = window.fetch;
      window.fetch = (url, opts = {}) => opts.method === 'POST' && String(url) === '/api/media/archives'
        ? Promise.resolve(new Response(JSON.stringify({ detail: { code: 'storage', message: 'хранилище недоступно' } }),
            { status: 503, headers: { 'Content-Type': 'application/json' } }))
        : real(url, opts); return true; })()""")
    r = browser.drive(live.url + "/", [*photo_screen("ziprefused", cards=2), ("wait", done_cards(2)), refuse,
        ("eval", "spy", CLICKS), ("click", "#photo-select"), ("click", "#photo-select-all"),
        ("click", "#photo-select-download"), ("wait", "document.querySelector('#photo-error').textContent"),
        ("eval", "error", text_of("#photo-error")), ("eval", "mode", selecting()), ("eval", "picked", picked()),
        ("eval", "clicked", "window.clicked.length")], width=1280)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["error"] == "Хранилище недоступно" and v["mode"] is True and len(v["picked"]) == 2 and v["clicked"] == 0
    assert not problems(r), problems(r)


def test_photo_selection_leaves_an_open_chip_menu_to_itself(browser, live):
    """Escape при открытом меню чипа закрывает меню, режим остаётся; нажатие по карточке в режиме закрывает
    меню и отмечает её. Вне режима карточка — свой слой (галочка строки не выше шапки таблицы), картинка
    не перетаскивается (долгое нажатие в Chrome на Android)."""
    live.add_user("selmenu")
    menu_open = "!document.querySelector('#chip-size-menu').hidden"
    r = browser.drive(live.url + "/", [*photo_screen("selmenu", cards=2), ("wait", done_cards(2)),
        ("eval", "isolated", f"getComputedStyle(document.querySelector('{nth(1)}')).isolation"),
        ("eval", "drag", f"getComputedStyle(document.querySelector('{nth(1, 'img')}')).webkitUserDrag"),
        ("click", "#photo-select"), ("click", nth(1)),
        ("click", "#chip-size"), ("wait", menu_open),
        ("press", "Escape"), ("wait", f"!({menu_open})"), ("eval", "afterEscape", selecting()),
        ("click", "#chip-size"), ("wait", menu_open),
        ("click", nth(2)), ("eval", "menuAfterCard", menu_open), ("eval", "picked", picked()), ("eval", "all", ids())],
        width=1280)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["isolated"] == "isolate" and v["drag"] == "none"
    assert v["afterEscape"] is True and v["menuAfterCard"] is False and sorted(v["picked"]) == sorted(v["all"])
    assert not problems(r), problems(r)


def test_photo_selection_on_iphone_cancel_of_files_ready_keeps_the_picked(browser, live):
    live.add_user("ioscancel")
    r = browser.drive(live.url + "/", [*photo_screen("ioscancel", cards=2), ("wait", done_cards(2)), iphone(True),
        ("click", "#photo-select"), ("click", "#photo-select-all"), ("click", "#photo-select-download"),
        ("wait", is_open("#save-dialog")), ("click", "#save-dialog .dialog-close"),
        ("wait", "!document.querySelector('#save-dialog')"),
        ("eval", "mode", selecting()), ("eval", "picked", picked()), ("eval", "all", ids()),
        ("eval", "count", text_of("#photo-select-count")), ("eval", "shared", "window.shared.length")],
        width=390, mobile=True)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["mode"] is True and sorted(v["picked"]) == sorted(v["all"]) and v["count"] == "Выбрано 2"
    assert v["shared"] == 0
    assert not problems(r), problems(r)


def test_photo_selection_on_iphone_skips_a_file_gone_in_another_tab(browser, live):
    live.add_user("iosgone")
    gone = ("eval", "gone", f"""(async () => {{ const id = document.querySelector('{nth(1)}').dataset.id;
      await {API}.api('DELETE', '/api/images/' + id); return id; }})()""")
    r = browser.drive(live.url + "/", [*photo_screen("iosgone", cards=3), ("wait", done_cards(3)), iphone(),
        ("eval", "spy", CLICKS), ("click", "#photo-select"), ("click", "#photo-select-all"), gone,
        ("click", "#photo-select-download"), ("wait", "window.shared.length === 1"), ("wait", f"!{selecting()}"),
        ("eval", "shared", "window.shared"), ("eval", "clicked", "window.clicked.length")], width=390, mobile=True)
    assert not r.get("failed"), r
    assert r["values"]["shared"] == [[[".png", "image/png", True]] * 2] and r["values"]["clicked"] == 0
    assert not known_refusals(problems(r), 404), problems(r)


def test_theme_and_view_follow_the_account_when_the_studio_comes_back(browser, live):
    """Тему и вид сменили на другом устройстве учётки: открытая студия берёт их, вернувшись на экран,
    без перезагрузки — тему сразу, вид при следующем открытии раздела."""
    login = "comeback"
    live.add_user(login, theme="light")
    elsewhere = ("fetch('/api/me').then(r => r.json()).then(m => fetch('/api/me', { method: 'PATCH',"
                 " headers: { 'content-type': 'application/json', 'x-studio-csrf': m.csrf },"
                 " body: JSON.stringify({ theme: 'dark', views: { photo: 'table' } }) })).then(r => r.status)")
    r = browser.drive(live.url + "/", [*photo_screen(login),
        ("eval", "before", "[document.documentElement.dataset.theme, document.querySelector('#photo-feed').dataset.view]"),
        ("eval", "patched", elsewhere),
        ("eval", "back", "document.dispatchEvent(new Event('visibilitychange'))"),
        ("wait", "document.documentElement.dataset.theme === 'dark'"),
        ("eval", "stillOpen", "document.querySelector('#photo-feed').dataset.view"),
        *go("chat"), ("wait", "!document.querySelector('#photo-feed')"),
        *go("photo"), ("wait", FEED_READY),
        ("eval", "after", "document.querySelector('#photo-feed').dataset.view"),
        ("eval", "pressed", "document.querySelector('#photo-view-table').getAttribute('aria-checked')")])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["before"] == ["light", "tiles"] and v["patched"] == 200
    assert v["stillOpen"] == "tiles" and v["after"] == "table" and v["pressed"] == "true"
    assert not problems(r), problems(r)


def test_photo_translates_the_prompt_into_the_field(browser, live):
    """«EN»: перевод встаёт в поле вместо промпта, генерации нет; во время перевода «Создать» закрыт,
    «очистить» не действует (не прячет «EN» с отменой), а «EN» перевод отменяет — промпт остаётся."""
    login = "translator"
    live.add_user(login)
    gw = live.gw
    d0, g0 = len(gw.descriptions), len(gw.generations)

    async def slow(request):
        await asyncio.sleep(1.0)
        return await gw.builtin(request)

    saved = gw.description_reply
    gw.description_reply = {"choices": [{"message": {"content": "a cat on the windowsill"}, "finish_reason": "stop"}]}
    gw.on["/slot/images/descriptions"] = slow
    idle = "document.querySelector('#photo-translate').textContent === 'EN' && !document.querySelector('#photo-go').disabled"
    try:
        r = browser.drive(live.url + "/", [*photo_screen(login),
            ("eval", "title", "document.querySelector('#photo-translate').title"),
            ("eval", "empty", "document.querySelector('#photo-translate').hidden"),   # пустое поле — «EN» нет
            ("type", "#photo-prompt", "кошка на подоконнике"), ("click", "#photo-translate"),
            ("wait", "document.querySelector('#photo-translate').textContent.startsWith('EN · ')"),
            ("eval", "busy", "[document.querySelector('#photo-go').disabled, document.querySelector('#photo-prompt').readOnly,"
                             " document.querySelector('#photo-translate').title]"),
            # «очистить» во время перевода ничего не делает: «EN» с отменой и часами остаётся
            ("click", "#photo-composer .field-clear"),
            ("eval", "clearBusy", "[document.querySelector('#photo-prompt').value, document.querySelector('#photo-translate').hidden,"
                                  " document.querySelector('#photo-translate').textContent.startsWith('EN · ')]"),
            ("wait", "document.querySelector('#photo-prompt').value === 'a cat on the windowsill'"), ("wait", idle),
            # второй перевод — отменён «EN»: промпт тот же
            ("eval", "edit", "(() => { const p = document.querySelector('#photo-prompt'); p.value = 'собака'; return true; })()"),
            ("click", "#photo-translate"),
            ("wait", "document.querySelector('#photo-translate').textContent.startsWith('EN · ')"),
            ("click", "#photo-translate"), ("wait", idle),
            ("eval", "after", "[document.querySelector('#photo-prompt').value, document.querySelector('#photo-prompt').readOnly,"
                              " document.querySelector('#photo-error').textContent]")], timeout=20000)
    finally:
        gw.description_reply = saved
        gw.on.pop("/slot/images/descriptions", None)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["title"] == "Перевести промпт на английский (gemma-abl)" and v["empty"] is True
    assert v["busy"] == [True, True, "Отменить перевод"]
    assert v["clearBusy"] == ["кошка на подоконнике", False, True]
    assert v["after"] == ["собака", False, ""]
    first, second = gw.descriptions[d0:]
    assert first["messages"][0]["content"].endswith("кошка на подоконнике")
    assert second["messages"][0]["content"].endswith("собака")
    assert len(gw.generations) == g0 and any(s.endswith("/cancel") for s in r["sent"])
    assert any(s.startswith("DELETE /api/images/translations/") for s in r["sent"])   # перевод в поле — получен
    assert not problems(r), problems(r)


#: опрос перевода всегда 500; window.fails — число отказов, window.fast() — пауза опроса (POLL_MS) 5 мс
TRANSLATION_DOWN = """(() => { const real = window.fetch; const wait = window.setTimeout.bind(window);
  window.fails = 0;
  window.fast = () => { window.setTimeout = (f, ms, ...a) => wait(f, ms === 1500 ? 5 : ms, ...a); return true; };
  window.fetch = (url, opts = {}) => (opts.method || 'GET') === 'GET' && String(url).startsWith('/api/images/translations/')
    ? (window.fails += 1, Promise.resolve(new Response('{}', { status: 500, headers: { 'content-type': 'application/json' } })))
    : real(url, opts);
  return true; })()"""


def test_photo_translation_that_keeps_failing_lets_the_field_go(browser, live):
    """Опрос перевода отказывает: «EN» отменяет сразу, а без отмены после TRANSLATE_FAILS отказов подряд
    поле снова правится, под ним — ошибка, задание снято."""
    login = "trdown"
    live.add_user(login)
    idle = "document.querySelector('#photo-translate').textContent === 'EN' && !document.querySelector('#photo-prompt').readOnly"
    state = ("[document.querySelector('#photo-prompt').value, document.querySelector('#photo-prompt').readOnly,"
             " document.querySelector('#photo-error').textContent, window.fails]")
    r = browser.drive(live.url + "/", [*photo_screen(login), ("eval", "stub", TRANSLATION_DOWN),
        ("type", "#photo-prompt", "кошка на подоконнике"), ("click", "#photo-translate"),
        ("wait", "window.fails >= 2"), ("click", "#photo-translate"), ("wait", idle),
        ("eval", "cancelled", state),
        ("eval", "fast", "window.fast()"), ("eval", "zero", "window.fails = 0"),
        ("click", "#photo-translate"), ("wait", idle), ("eval", "gaveUp", state)], timeout=20000)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["cancelled"][:3] == ["кошка на подоконнике", False, ""]
    assert v["gaveUp"] == ["кошка на подоконнике", False, "Перевод не получился: Ошибка сервера (500)", 40]
    assert sum(s.endswith("/cancel") for s in r["sent"]) >= 2
    assert not problems(r), problems(r)


# ---- видео ---------------------------------------------------------------------

# ---- фото: серии, пресеты, сравнение, варианты (спека 2026-10-21-studio-photo-presets-compare-series) ----

#: элемент целиком в окне по ширине (или скрыт)
FITS = """(sel) => [...document.querySelectorAll(sel)].every(e => { const b = e.getBoundingClientRect();
  return b.width === 0 || (b.left >= 0 && b.right <= innerWidth); })"""


def slow_first(live, prompt, seconds=2.0):
    """Первая генерация с промптом `prompt` идёт `seconds`: страница успевает увидеть строки «в очереди»."""
    seen = []

    async def slow(request):
        body = request.content
        if prompt.encode() in body and not seen:
            seen.append(1)
            await asyncio.sleep(seconds)
        return await live.gw.builtin(request)

    return slow


@pytest.mark.parametrize("width", [390, 1280])
def test_photo_series_by_seed_shows_queued_then_done(browser, live, width):
    """Критерий 4: «×3 · seed» → три карточки, две «в очереди серии», готовые встают по одной; чип — снова «×1».
    Поля seed нет: запрос без seed, у шлюза — три разных случайных."""
    login = f"series{width}"
    live.add_user(login)
    g0 = len(live.gw.generations)
    queued = "#photo-feed .photo-card[data-state=queued]"
    live.gw.on["/slot/images/generations"] = slow_first(live, "серия котов")
    try:
        r = browser.drive(live.url + "/", [*photo_screen(login), ("eval", "spy", SPY),
            ("type", "#photo-prompt", "серия котов"),
            ("click", "#chip-series"), ("wait", "!document.querySelector('#chip-series-menu').hidden"),
            ("eval", "noStrength", "document.querySelector('#photo-series-panel [data-vary=strength]').hidden"),
            ("click", "#photo-series-panel [data-count='3']"),
            ("eval", "label", text_of("#chip-series")),
            ("eval", "menuFits", f"({FITS})('#chip-series-menu')"),
            ("click", "#photo-go"),
            ("wait", f"document.querySelectorAll('{queued}').length === 2"),
            ("eval", "queued", f"[...document.querySelectorAll('{queued}')].map(c => [c.textContent, !!c.querySelector('.card-cancel')])"),
            ("eval", "after", text_of("#chip-series")),
            ("eval", "goBusy", "document.querySelector('#photo-go').disabled"),
            ("wait", done_cards(3)),
            ("eval", "free", "!document.querySelector('#photo-go').disabled"),
            ("eval", "feed", ids()),
            ("eval", "forms", "window.sentForms")], width=width, height=900, mobile=width == 390, timeout=20000)
    finally:
        live.gw.on.pop("/slot/images/generations", None)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["noStrength"] and v["label"] == "×3 · seed" and v["menuFits"] and v["after"] == "×1"
    assert [q[1] for q in v["queued"]] == [True, True]
    assert sorted("2 из 3" in q[0] or "3 из 3" in q[0] for q in v["queued"]) == [True, True]
    assert all(q[0].startswith("В очереди серии") for q in v["queued"])
    assert v["goBusy"] and v["free"]
    assert v["forms"][0]["form"]["series"] == {"count": 3, "vary": "seed"} and "seed" not in v["forms"][0]["form"]
    seeds = [g[0]["seed"] for g in live.gw.generations[g0:]]
    assert len(seeds) == 3 and len(set(seeds)) == 3 and all(isinstance(s, int) for s in seeds)
    assert not problems(r), problems(r)


def test_photo_strength_series_shows_the_values_here_and_in_the_editor(browser, live):
    """Критерий 5: Lustify с фото — «сила» от 0.25 до 0.75, 3 варианта: значения видны до запуска; и в «Изменить»."""
    login = "strengths"
    live.add_user(login)
    e0 = len(live.gw.edits)
    values = "document.querySelector('#photo-series-values').textContent"
    r = browser.drive(live.url + "/", [*photo_screen(login), ("eval", "spy", SPY),
        put_file("#photo-file", png_of(40, 30, "blue")),
        ("wait", "document.querySelector('#photo-source img')?.naturalWidth === 40"),
        ("click", "#chip-series"), ("click", "#photo-series-panel [data-count='3']"),
        ("click", "#photo-series-panel [data-vary=strength]"),
        set_value("#photo-series-to", "0.75"),
        ("eval", "values", values), ("eval", "label", text_of("#chip-series")),
        # сила «от» — ползунок поля: строка значений меню идёт за ним и при закрытом меню
        ("click", "#chip-series"), set_value("#photo-strength", "0.4"), ("click", "#chip-series"),
        ("eval", "moved", values),
        set_value("#photo-strength", "0.7"), ("eval", "movedNarrow", values),
        set_value("#photo-strength", "0.25"),
        set_value("#photo-series-to", "0.3"), ("eval", "narrow", values),
        set_value("#photo-series-to", "0.75"),
        # меню серии открыто над полем (чип — в строке под ним): закрыть, как человек, прежде чем писать
        ("press", "Escape"), ("wait", "document.querySelector('#chip-series-menu').hidden"),
        ("type", "#photo-prompt", "акварель"), ("click", "#photo-go"), ("wait", done_cards(3)),
        # «Изменить»: серия по силе, с кистью — без серии
        ("click", NEWEST_DONE), ("wait", is_open("#viewer")), ("click", "#viewer .act-edit"), ("wait", is_open("#editor")),
        ("click", "#edit-series-panel [data-count='2']"), ("click", "#edit-series-panel [data-vary=strength]"),
        set_value("#edit-series-to", "0.5"),
        ("eval", "editValues", text_of("#edit-series-values")),
        ("click", "#edit-local"), ("eval", "brushHides", "document.querySelector('#edit-series').hidden"),
        ("click", "#edit-local"),
        ("type", "#edit-prompt", "ярче"), ("click", "#edit-submit"),
        ("wait", f"!{is_open('#editor')} && {done_cards(5)}"),
        ("eval", "forms", "window.sentForms")], width=1280, height=900, timeout=20000)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["values"] == "0.25 · 0.50 · 0.75" and v["label"] == "×3 · сила"
    assert v["moved"] == "0.40 · 0.60 · 0.75" and v["movedNarrow"] == "слишком узкий диапазон для 3 вариантов"
    assert v["narrow"] == "слишком узкий диапазон для 3 вариантов"
    assert v["editValues"] == "0.25 · 0.50" and v["brushHides"]
    first, edit = (f["form"] for f in v["forms"])
    assert json.loads(first["series"]) == {"count": 3, "vary": "strength", "strength_to": 0.75}
    assert json.loads(edit["series"]) == {"count": 2, "vary": "strength", "strength_to": 0.5} and "mask" not in edit
    assert [e[0]["strength"] for e in live.gw.edits[e0:]] == ["0.25", "0.5", "0.75", "0.25", "0.5"]
    assert not problems(r), problems(r)


def test_photo_preset_saves_and_applies(browser, live):
    """Критерий 1: «★» → «Сохранить как пресет…» → в меню; применение ставит модель, формат, силу и промпт;
    в непустое поле — после вопроса; то же имя — замена, «×» — удаление. Seed пресет не хранит, а seed
    старого пресета не применяется: картинка по нему идёт без seed."""
    login = "presets"
    live.add_user(login)
    prompt = "document.querySelector('#photo-prompt').value"
    chips = "[document.querySelector('#chip-model').textContent, document.querySelector('#chip-size').textContent," \
            " document.querySelector('#photo-strength').value]"
    old = f"""(async () => {{ await {API}.api('POST', '/api/images/presets', {{ name: 'Старый', params: {{ model: 'lustify',
      connection_id: null, size: 'square', seed: 42, strength: 0.25, prompt: 'старый кот' }} }}); return true; }})()"""
    r = browser.drive(live.url + "/", [*photo_screen(login),
        ("type", "#photo-prompt", "портрет у окна"),
        ("click", "#chip-size"), ("click", "#chip-size-menu [data-value=landscape]"),
        set_value("#photo-strength", "0.6"),
        ("click", "#chip-preset"), ("wait", "!document.querySelector('#chip-preset-menu').hidden"),
        ("eval", "empty", text_of("#chip-preset-menu .preset-list")),
        ("click", "#preset-save"), ("wait", is_open("#preset-dialog")),
        ("eval", "checks", "[document.querySelector('#preset-with-prompt').checked, !!document.querySelector('#preset-with-seed')]"),
        ("type", "#preset-name", "Портрет студия"), ("click", "#preset-save-confirm"),
        ("wait", f"!{is_open('#preset-dialog')}"),
        ("eval", "saved", f"(async () => (await {API}.api('GET', '/api/images/presets')).items.map(p => [p.name, p.params]))()"),
        # всё другое — и пресет возвращает
        set_value("#photo-prompt", ""), set_value("#photo-strength", "0.25"),
        ("click", "#chip-size"), ("click", "#chip-size-menu [data-value=portrait]"),
        ("click", "#chip-model"), ("click", "#chip-model-menu [data-value$=':flux']"),
        ("click", "#chip-preset"), ("wait", "document.querySelector('#chip-preset-menu .preset-apply')"),
        ("click", "#chip-preset-menu .preset-apply"),
        ("eval", "prompt", prompt), ("eval", "chips", chips),
        # своё в поле — вопрос; «Оставить свой» ставит остальное
        set_value("#photo-prompt", "свой текст"),
        ("click", "#chip-preset"), ("click", "#chip-preset-menu .preset-apply"),
        ("wait", is_open("#preset-prompt-dialog")), ("click", "#preset-prompt-keep"),
        ("wait", f"!{is_open('#preset-prompt-dialog')}"),
        ("eval", "kept", prompt),
        # то же имя — окно говорит, что заменит
        ("click", "#chip-preset"), ("click", "#preset-save"), ("wait", is_open("#preset-dialog")),
        ("type", "#preset-name", "Портрет студия"),
        ("eval", "replace", f"[{text_of('#preset-replace-note')}, {text_of('#preset-save-confirm')}]"),
        ("press", "Escape"), ("wait", f"!{is_open('#preset-dialog')}"),
        # «×» — удаление с подтверждением
        ("click", "#chip-preset"), ("click", "#chip-preset-menu .preset-delete"),
        ("wait", is_open("#preset-delete-dialog")), ("click", "#preset-delete-confirm"),
        ("wait", f"!{is_open('#preset-delete-dialog')}"), ("idle",),
        ("click", "#chip-preset"), ("wait", "!document.querySelector('#chip-preset-menu .preset-apply')"),
        ("eval", "gone", text_of("#chip-preset-menu .preset-list")), ("press", "Escape"),
        # пресет прежнего кода с seed: применяется всё, кроме seed
        ("eval", "old", old), *go("chat"), ("wait", "!document.querySelector('#photo-feed')"),
        *go("photo"), ("wait", FEED_READY), ("eval", "spy", SPY),
        ("click", "#chip-preset"), ("wait", "document.querySelector('#chip-preset-menu .preset-apply')"),
        ("click", "#chip-preset-menu .preset-apply"), ("click", "#photo-go"), ("wait", done_cards(1)),
        ("eval", "forms", "window.sentForms")], width=1280, height=900)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["empty"] == "Пресетов пока нет" and v["checks"] == [True, False]
    (name, params), = v["saved"]
    assert name == "Портрет студия" and params["model"] == "lustify" and params["size"] == "landscape"
    assert params.get("seed") is None and params["strength"] == 0.6 and params["prompt"] == "портрет у окна"
    assert v["prompt"] == "портрет у окна"
    model, size, strength = v["chips"]
    assert "Lustify" in model and "Альбом" in size and strength == "0.6"
    assert v["kept"] == "свой текст"
    assert "уже есть" in v["replace"][0] and v["replace"][1] == "Заменить"
    assert v["gone"] == "Пресетов пока нет"
    (sent,) = (f["form"] for f in v["forms"])
    assert sent["prompt"] == "старый кот" and sent["size"] == "square" and "seed" not in sent
    assert not problems(r), problems(r)


def test_photo_external_model_hides_the_seed(browser, live):
    """Критерий 7: у внешнего сервиса seed нет — в таблице seed его картинок не показан; поля seed нет ни у какой модели."""
    login = "extseed"
    uid = live.add_user(login)
    live_sql(live, "INSERT INTO images(id, user_id, kind, model, prompt, size, seed, state, detail, created)"
                   " VALUES (?, ?, 'generation', 'gpt-image-1', 'облако', '1024x1024', 123456, 'error', 'нет', ?)",
             ("5eed" * 8, uid, time.time()))
    # внешний сервис картинок ходит через сервер — адрес из списка администратора (сам он не зовётся)
    cloud = ("eval", "cloud", f"""(async () => {{ const m = {API};
      const c = await m.api('POST', '/api/connections', {{ name: 'Облако', kind: 'openai',
        base_url: {GATEWAY_URL + '/v1'!r}, token: 'cloud-key', sections: ['photo'] }});
      await m.api('PUT', '/api/sections/photo/links/' + c.id, {{ models: ['gpt-image-1'] }});
      await m.loadMe(); return c.id; }})()""")
    no_seed = "!document.querySelector('#chip-seed, #photo-seed')"
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), cloud, *go("photo"),
        ("wait", FEED_READY), ("wait", "document.querySelector('#chip-model-menu [data-value$=\":gpt-image-1\"]')"),
        ("eval", "onGateway", no_seed),
        ("click", "#chip-model"), ("click", "#chip-model-menu [data-value$=':gpt-image-1']"),
        ("eval", "external", no_seed),
        ("click", "#photo-view"), ("click", "#photo-view-table"), ("wait", "document.querySelector('#photo-feed').dataset.view === 'table'"),
        ("eval", "row", text_of(PHOTO_CARD))], width=1280)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["onGateway"] is True and v["external"] is True
    assert "gpt-image-1" in v["row"] and "123456" not in v["row"] and "seed" not in v["row"]
    assert not problems(r), problems(r)


def test_photo_external_series_offers_no_seed(browser, live):
    """Серия внешнему сервису — N независимых картинок: чип «×3» без «меняется: seed», и в «Изменить»."""
    login = "extseries"
    live.add_user(login)
    cloud = ("eval", "cloud", f"""(async () => {{ const m = {API};
      const c = await m.api('POST', '/api/connections', {{ name: 'Облако', kind: 'openai',
        base_url: {GATEWAY_URL + '/v1'!r}, token: 'cloud-key', sections: ['photo'] }});
      await m.api('PUT', '/api/sections/photo/links/' + c.id, {{ models: ['gpt-image-1'] }});
      await m.loadMe(); return c.id; }})()""")
    vary_hidden = "document.querySelector('#{}-series-vary').hidden"
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), make_images(1), cloud,
        *go("photo"), ("wait", FEED_READY), ("wait", done_cards(1)),
        ("wait", "document.querySelector('#chip-model-menu [data-value$=\":gpt-image-1\"]')"),
        ("click", "#chip-model"), ("click", "#chip-model-menu [data-value$=':gpt-image-1']"),
        ("click", "#chip-series"), ("click", "#photo-series-panel [data-count='3']"),
        ("eval", "varyHidden", vary_hidden.format("photo")), ("eval", "label", text_of("#chip-series")),
        ("click", "#chip-series"),
        ("click", NEWEST_DONE), ("wait", is_open("#viewer")), ("click", "#viewer .act-edit"), ("wait", is_open("#editor")),
        ("click", "#edit-series-panel [data-count='3']"),
        ("eval", "editVaryHidden", vary_hidden.format("edit"))], width=1280)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["varyHidden"] is True and v["label"] == "×3" and v["editVaryHidden"] is True
    assert not problems(r), problems(r)


#: генерация «кот» (seed 5) и правка FLUX «ярче» из неё через API страницы
MAKE_CHAIN = f"""(async () => {{ const m = {API};
  const wait = async (id) => {{ for (let t = 0; t < 200; t++) {{ const i = await m.api('GET', '/api/images/' + id);
    if (i.state === 'done') return i; await new Promise(r => setTimeout(r, 30)); }} }};
  const gen = await wait((await m.api('POST', '/api/images', {{ prompt: 'кот', size: 'square', seed: 5 }})).id);
  const edit = await wait((await m.api('POST', '/api/images', {{ prompt: 'ярче', size: 'original', model: 'flux',
    source_media_id: gen.media_id }})).id);
  return [gen.media_id, edit.media_id]; }})()"""
COMPARE_SIDES = ("[...document.querySelectorAll('#compare .compare-img')].map(i => i.getAttribute('src'))"
                 ".concat([...document.querySelectorAll('#compare .compare-label')].map(l => l.textContent))")
#: перетаскивание по рамке сравнения мышью: нажать на 80 %, вести до 60 %, отпустить
DRAG = """(() => { const f = document.querySelector('#compare .compare-frame'); const b = f.getBoundingClientRect();
  const at = (x) => ({ bubbles: true, cancelable: true, pointerType: 'mouse', isPrimary: true, pointerId: 1,
    clientX: b.left + b.width * x, clientY: b.top + b.height / 2 });
  f.dispatchEvent(new PointerEvent('pointerdown', at(0.8))); f.dispatchEvent(new PointerEvent('pointermove', at(0.6)));
  f.dispatchEvent(new PointerEvent('pointerup', at(0.6)));
  return [Number(document.querySelector('#compare-range').value),
    document.querySelector('#compare .compare-line').style.left]; })()"""


@pytest.mark.parametrize("width", [390, 1280])
def test_photo_compare_from_the_viewer_and_the_selection(browser, live, width):
    """Критерии 2–3: «Сравнить с исходником» в просмотре правки (исходник слева) и «Сравнить» при двух отмеченных."""
    login = f"compare{width}"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), ("eval", "made", MAKE_CHAIN),
        *go("photo"), ("wait", FEED_READY), ("wait", done_cards(2)),
        ("click", NEWEST_DONE), ("wait", is_open("#viewer")), ("wait", "document.querySelector('#viewer .act-compare')"),
        ("click", "#viewer .act-compare"), ("wait", is_open("#compare")),
        ("eval", "sides", COMPARE_SIDES),
        set_value("#compare-range", "30"),
        ("eval", "clip", "[document.querySelector('#compare .compare-right').style.clipPath,"
                         " document.querySelector('#compare .compare-line').style.left]"),
        ("eval", "drag", DRAG),
        ("eval", "fits", f"{NO_OVERFLOW} && ({FITS})('#compare button, #compare input')"),
        ("click", "#compare-swap"), ("eval", "swapped", COMPARE_SIDES),
        ("press", "Escape"), ("wait", f"!document.querySelector('#compare') && {is_open('#viewer')}"),
        ("press", "Escape"), ("wait", "!document.querySelector('#viewer')"),
        # выделение: одна — «Сравнить» нет, две — есть, режим остаётся
        ("click", "#photo-select"), ("wait", selecting()), ("click", nth(1)),
        ("eval", "one", "document.querySelector('#photo-select-compare').hidden"),
        ("click", nth(2)),
        ("eval", "two", "document.querySelector('#photo-select-compare').hidden"),
        ("click", "#photo-select-compare"), ("wait", is_open("#compare")),
        ("eval", "picked", COMPARE_SIDES),
        ("press", "Escape"), ("wait", "!document.querySelector('#compare')"),
        ("eval", "stillSelecting", selecting())], width=width, height=900, mobile=width == 390)
    assert not r.get("failed"), r
    v = r["values"]
    gen, edit = v["made"]
    assert v["sides"][:2] == [f"/api/media/{gen}", f"/api/media/{edit}"]
    assert v["sides"][2].startswith("1. Lustify · 1024×1024 · seed 5") and v["sides"][3].startswith("2. FLUX · 64×48")
    assert "сила" not in v["sides"][3]                                   # у FLUX силы нет
    assert "30%" in v["clip"][0] and v["clip"][1] == "30%"
    assert 55 <= v["drag"][0] <= 65 and v["drag"][1].endswith("%")
    assert v["fits"] and v["swapped"][:2] == v["sides"][:2][::-1]
    assert v["one"] is True and v["two"] is False and v["stillSelecting"]
    assert sorted(v["picked"][:2]) == sorted(f"/api/media/{m}" for m in (gen, edit))
    assert not problems(r), problems(r)


def test_photo_variants_open_a_variant(browser, live):
    """Критерий 6: «Варианты» в просмотре — соседи по исходнику и сделанные из этой; нажатие открывает вариант."""
    login = "variants"
    live.add_user(login)
    make = ("eval", "made", f"""(async () => {{ const m = {API};
      const wait = async (id) => {{ for (let t = 0; t < 200; t++) {{ const i = await m.api('GET', '/api/images/' + id);
        if (i.state === 'done') return i; await new Promise(r => setTimeout(r, 30)); }} }};
      const gen = await wait((await m.api('POST', '/api/images', {{ prompt: 'кот', size: 'square', seed: 5 }})).id);
      const out = [gen.id];
      for (const p of ['синий', 'красный']) out.push((await wait((await m.api('POST', '/api/images',
        {{ prompt: p, size: 'original', model: 'flux', source_media_id: gen.media_id }})).id)).id);
      out.push((await wait((await m.api('POST', '/api/images', {{ prompt: 'одинокий', size: 'square' }})).id)).id);
      window.made = out; return out; }})()""")
    open_made = ("eval", "openGen", "(() => { document.querySelector(`#photo-feed .photo-card[data-id='${window.made[0]}']"
                                    " .card-open`).click(); return true; })()")
    open_blue = ("eval", "openBlue", "(() => { document.querySelector(`#viewer .variant-open[data-id='${window.made[1]}']`)"
                                     ".click(); return true; })()")
    variants = "[...document.querySelectorAll('#viewer .variant-open')].map(b => b.dataset.id)"
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), make, *go("photo"),
        ("wait", FEED_READY), ("wait", done_cards(4)),
        ("click", NEWEST_DONE), ("wait", is_open("#viewer")), ("idle",),
        ("eval", "lonely", "document.querySelector('#viewer .viewer-variants').hidden"),
        ("press", "Escape"), ("wait", "!document.querySelector('#viewer')"),
        open_made, ("wait", is_open("#viewer")),
        ("wait", "document.querySelectorAll('#viewer .variants-children .variant-open').length === 2"),
        ("eval", "children", variants), ("eval", "noSiblings", "!document.querySelector('#viewer .variants-siblings')"),
        open_blue,
        ("wait", "document.querySelectorAll('#viewer .chain-step').length === 2"
                 " && document.querySelector('#viewer .variants-siblings')"),
        ("eval", "opened", "document.querySelector('#viewer .chain-step.now .chain-prompt').textContent"),
        ("eval", "siblings", variants)])
    assert not r.get("failed"), r
    v = r["values"]
    gen, blue, red, lonely = v["made"]
    assert v["lonely"] is True
    assert v["children"] == [red, blue] and v["noSiblings"]
    assert v["opened"] == "синий" and v["siblings"] == [red]
    assert not problems(r), problems(r)


CLIP = "#video-feed .video-card"
VIDEO_FEED_READY = ("document.querySelector('#video-feed')?.dataset.feed"
                    " && document.querySelector('#video-feed').dataset.feed !== 'loading'")


def clips(state, n):
    return f"document.querySelectorAll('{CLIP}[data-state={state}]').length === {n}"


def video_screen(login):
    """Вход, подключение к шлюзу, раздел «Видео» с загруженной лентой."""
    return [*signin(login), ("wait", READY), add_gateway(), *go("video"), ("wait", VIDEO_FEED_READY)]


@pytest.fixture
def ready_clips(live):
    """Ролики шлюза в тесте готовы с первого опроса; прежнее поведение шлюза — после теста."""
    gw = live.gw
    saved = gw.video_status, dict(gw.video_extra), dict(gw.on)
    gw.video_status = "completed"
    yield gw
    gw.video_status, gw.video_extra = saved[0], saved[1]
    gw.on.clear()
    gw.on.update(saved[2])


@pytest.mark.parametrize("width", [390, 1280])
def test_video_animates_a_photo_and_the_clip_plays(browser, live, ready_clips, width):
    """Перенос test_studio_upload_browser «Оживить» на новые разделы: «Оживить» в просмотре фото
    открывает раздел видео с этим фото, «Оживить» раздела шлёт его в /api/videos, готовый ролик
    в ленте — плеер с постером исходника, «Скачать», «Удалить»."""
    login = f"anim{width}"
    live.add_user(login)
    v0 = len(live.gw.video_forms)
    player = f"{CLIP}[data-state=done] video"
    r = browser.drive(live.url + "/", [*photo_screen(login, cards=1), ("wait", done_cards(1)),
        ("click", NEWEST_DONE), ("wait", is_open("#viewer")), ("click", "#viewer .act-animate"),
        ("wait", "document.querySelector('#video-source img')?.naturalWidth > 0"),
        ("eval", "source", "location.hash.split('source=')[1]"),
        set_value("#video-seconds", "7"),
        ("click", "#chip-quality"), ("click", "#chip-quality-menu [data-value=high]"),
        ("click", "#chip-smooth"),
        ("eval", "smooth", "document.querySelector('#chip-smooth').getAttribute('aria-pressed')"),
        ("type", "#video-prompt", "she slowly turns her head"),
        ("click", "#video-go"),
        ("wait", f"document.querySelector('{player}')"),
        ("eval", "player", f"(() => {{ const v = document.querySelector('{player}');"
                           " return [v.getAttribute('src'), v.getAttribute('poster'), v.controls, v.playsInline, v.preload]; })()"),
        ("eval", "range", f"fetch(document.querySelector('{player}').src, {{ headers: {{ Range: 'bytes=0-9' }} }})"
                          ".then(r => [r.status, r.headers.get('content-type')])"),
        ("eval", "spy", CLICKS), ("click", f"{CLIP} .card-download"), ("wait", "window.clicked.length === 1"),
        ("eval", "download", f"[window.clicked[0][0], document.querySelector('{CLIP} .card-download').textContent]"),
        ("eval", "card", f"document.querySelector('{CLIP}').textContent"),
        ("eval", "items", f"(async () => (await {API}.api('GET', '/api/videos')).items)()"),
        ("eval", "overflow", f"!({NO_OVERFLOW})"),
        # ролик из фото истории — «Промпты»: цепочка фото и промпт ролика последним
        ("click", f"{CLIP} .card-chain"), ("wait", is_open("#chain-dialog")),
        ("eval", "chain", "[...document.querySelectorAll('#chain-dialog .chain-step .chain-prompt')].map(p => p.textContent)")],
        width=width, height=900, mobile=width == 390)
    assert not r.get("failed"), r
    v = r["values"]
    forms = live.gw.video_forms[v0:]
    assert len(forms) == 1
    fields, image, _raw = forms[0]
    seed = fields.pop("seed")                                   # пусто — случайный, выбирает студия
    assert fields == {"model": VIDEO_MODEL, "seconds": "7", "quality": "high", "smooth": "false",
                      "prompt": "she slowly turns her head"}
    assert picture(image) == picture(PNG_REAL)                  # фото истории, а не файл с устройства
    item = v["items"][0]
    assert item["state"] == "done" and item["source_media_id"] == v["source"] and v["smooth"] == "false"
    assert v["player"] == [f"/api/media/{item['media_id']}", f"/api/media/{v['source']}?thumb=1", True, True, "metadata"]
    assert v["range"] == [206, "video/mp4"]
    assert v["download"] == [f"/api/media/{item['media_id']}?download=1", "Скачать"]
    for words in ("Готово", "7 с", "720p", "16 к/с", "придуман по фото", f"seed {seed}", "Удалить"):
        assert words in v["card"], words
    assert re.search(r"\d+:\d\d", v["card"]) and not v["overflow"]
    assert v["chain"] == ["кот 0", "she slowly turns her head"]
    assert not problems(r), problems(r)


def test_video_feed_shows_progress_and_survives_reload(browser, live, ready_clips):
    login = "vreload"
    live.add_user(login)
    v0 = len(live.gw.video_forms)
    running_id = f"video_{len(live.gw.video_ids) + 2:024x}"     # второй ролик теста «делается»

    async def running(request):
        if request.method != "GET":
            return await live.gw.builtin(request)
        return httpx.Response(200, json=video_object(running_id, status="in_progress", progress=37,
                                                     detail="делается: шаг 3 из 8", seed=7))

    live.gw.on[f"/slot/videos/{running_id}"] = running
    busy = f"{CLIP}[data-state=in_progress]"
    r = browser.drive(live.url + "/", [*video_screen(login),
        ("eval", "empty", "document.querySelector('#video-feed .feed-empty').textContent"),
        ("click", "#video-go"),
        ("wait", "document.querySelector('#video-error').textContent.length > 0"),
        ("eval", "noSource", "document.querySelector('#video-error').textContent"),
        put_file("#video-file", png_of(48, 64, "purple")),
        ("wait", "document.querySelector('#video-source img')?.naturalWidth === 48"),
        ("click", "#video-go"), ("wait", clips("done", 1)),
        ("type", "#video-prompt", "медленный наезд камеры"), ("click", "#video-go"),
        ("wait", f"document.querySelector('{busy}')?.textContent.includes('37%')"),
        ("eval", "bar", f"document.querySelector('{busy} .video-bar span').style.width"),
        ("eval", "busyText", f"document.querySelector('{busy}').textContent"),
        ("eval", "goFree", "!document.querySelector('#video-go').disabled"),
        ("idle",),
        ("eval", "reload", "location.reload()"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"),
        ("wait", f"document.querySelector('{busy}')?.textContent.includes('37%')"),
        ("eval", "after", f"[...document.querySelectorAll('{CLIP}')].map(c => c.dataset.state)"),
        ("click", f"{busy} .card-cancel"),
        ("wait", clips("cancelled", 1)),
        ("eval", "final", f"document.querySelector('{CLIP}[data-state=cancelled]').textContent")], timeout=20000)
    assert not r.get("failed"), r
    v = r["values"]
    assert "Здесь появятся ваши ролики" in v["empty"] and v["noSource"].startswith("Выберите фото")
    assert v["bar"] == "37%" and "Делается: шаг 3 из 8" in v["busyText"] and v["goFree"]
    assert v["after"] == ["in_progress", "done"]
    assert "Отменено" in v["final"]
    assert any(s.endswith("/cancel") for s in r["sent"]) and running_id in live.gw.video_deleted
    first, second = (f[0] for f in live.gw.video_forms[v0:])
    assert picture(live.gw.video_forms[v0][1]) == ((48, 64), (128, 0, 128))
    assert "prompt" not in first and first["seconds"] == "5" and first["quality"] == "standard"
    assert first["smooth"] == "true" and first["seed"].isdigit()     # пустой seed — случайный от студии
    assert second["prompt"] == "медленный наезд камеры"
    assert not problems(r), problems(r)


def test_video_layout_fits_390(browser, live, ready_clips):
    login = "vnarrow"
    live.add_user(login)
    grid = "document.querySelector('#video-feed .feed-grid')"
    inside = """(sel) => [...document.querySelectorAll(sel)].every(e => { const b = e.getBoundingClientRect();
      return b.width === 0 || (b.left >= 0 && b.right <= innerWidth); })"""
    r = browser.drive(live.url + "/", [*video_screen(login),
        ("eval", "defaults", "[document.querySelector('#video-seconds').value,"
                             " document.querySelector('#chip-quality').textContent,"
                             " document.querySelector('#chip-smooth').getAttribute('aria-pressed'),"
                             " document.querySelector('#chip-smooth').title,"
                             " document.querySelector('#video-prompt').placeholder,"
                             " document.querySelector('#video-prompt').maxLength,"
                             " document.querySelector('#video-go').textContent]"),
        put_file("#video-file", png_of(40, 30, "teal")),
        ("wait", "document.querySelector('#video-source img')?.naturalWidth === 40"),
        ("click", "#video-go"), ("wait", clips("done", 1)),
        ("eval", "overflow", f"!({NO_OVERFLOW}) || document.querySelector('#view').scrollWidth"
                             " > document.querySelector('#view').clientWidth"),
        ("eval", "cols", f"getComputedStyle({grid}).gridTemplateColumns.split(' ').length"),
        ("eval", "aboveTabs", "document.querySelector('#video-composer').getBoundingClientRect().bottom"
                              " <= document.querySelector('#rail').getBoundingClientRect().top"),
        ("eval", "inside", f"({inside})('#video-composer .chip, #video-go, #video-source-clear, {CLIP}, {CLIP} .btn')"),
        ("click", "#chip-quality"), ("wait", "!document.querySelector('#chip-quality-menu').hidden"),
        ("eval", "menu", f"({inside})('#chip-quality-menu .chip-option')"),
        ("eval", "qualities", "[...document.querySelectorAll('#chip-quality-menu .chip-option')].map(e => e.textContent)"),
        ("press", "Escape"), ("wait", "document.querySelector('#chip-quality-menu').hidden"),
        set_value("#video-seconds", "31"), ("click", "#video-go"),
        ("wait", "document.querySelector('#video-error').textContent.length > 0"),
        ("eval", "error", "document.querySelector('#video-error').textContent"),
        ("click", "#video-source-clear"),
        ("eval", "cleared", "!document.querySelector('#video-source img[src]')"
                            " && document.querySelector('#video-file').files.length === 0")], width=390, mobile=True)
    assert not r.get("failed"), r
    v = r["values"]
    seconds, quality, smooth, smooth_text, placeholder, maxlength, go_text = v["defaults"]
    assert (seconds, smooth, maxlength, go_text) == ("5", "true", 4000, "Оживить")
    assert quality == "576p" and "32 к/с" in smooth_text            # полные слова качества — в меню
    assert "пусто — движение придумает gemma-abl" in placeholder
    # вид по умолчанию — превью: на телефоне ролик во всю ширину
    assert not v["overflow"] and v["cols"] == 1 and v["aboveTabs"] and v["inside"] and v["menu"]
    assert v["qualities"] == ["Быстро 480p", "Обычно 576p", "Высоко 720p"]
    assert v["error"].startswith("Длительность") and v["cleared"]
    assert not problems(r), problems(r)


def make_clips(n):
    """n готовых роликов из одного фото (шлюз теста готов с первого опроса)."""
    steps = [put_file("#video-file", png_of(48, 64, "olive")),
             ("wait", "document.querySelector('#video-source img')?.naturalWidth === 48")]
    for i in range(1, n + 1):
        steps += [("click", "#video-go"), ("wait", clips("done", i))]
    return steps


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_video_selection_in_every_view(browser, live, ready_clips, width):
    login = f"vsel{width}"
    live.add_user(login)
    steps = [*video_screen(login), *make_clips(3), ("click", "#video-select"), ("wait", selecting(".video"))]
    for i, view in enumerate(("preview", "tiles", "table"), 1):   # по ролику в каждом виде, отметки копятся
        steps += [("click", "#video-view"), ("click", f"#video-view-{view}"),
                  ("wait", f"document.querySelector('#video-feed').dataset.view === '{view}'"),
                  ("click", nth(i, card=CLIP)),
                  ("eval", f"{view}Picked", picked("#video-feed")),
                  ("eval", f"{view}Viewer", "!!document.querySelector('#video-viewer')"),
                  ("eval", f"{view}Paused", f"[...document.querySelectorAll('{CLIP} video')].every(v => v.paused)"),
                  ("eval", f"{view}Fits", NO_OVERFLOW)]
    steps += [("eval", "bar", "getComputedStyle(document.querySelector('.video .select-bar')).display !== 'none'"),
              ("eval", "label", text_of("#video-select-download"))]
    r = browser.drive(live.url + "/", steps, width=width, mobile=width < 760, timeout=20000)
    assert not r.get("failed"), r
    v = r["values"]
    for n, view in enumerate(("preview", "tiles", "table"), 1):
        assert len(v[f"{view}Picked"]) == n, view
        assert v[f"{view}Viewer"] is False and v[f"{view}Paused"] and v[f"{view}Fits"], view
    assert set(v["previewPicked"]) < set(v["tilesPicked"]) < set(v["tablePicked"])
    assert v["bar"] is (width < 760) and v["label"] == "Скачать"
    assert not problems(r), problems(r)


def test_video_selection_pauses_the_clips(browser, live, ready_clips):
    """Ролик, запущенный до режима, под слоем выделения не играет дальше (его не остановить нажатием)."""
    live.add_user("vpause")
    spy = ("eval", "spyPause", "(() => { window.pausedClips = 0;"
                               " HTMLMediaElement.prototype.pause = function () { window.pausedClips += 1; }; return true; })()")
    r = browser.drive(live.url + "/", [*video_screen("vpause"), *make_clips(2), spy,
        ("click", "#video-select"), ("wait", selecting(".video")),
        ("eval", "paused", "window.pausedClips"), ("eval", "players", f"document.querySelectorAll('{CLIP} video').length")],
        width=1280, timeout=20000)
    assert not r.get("failed"), r
    assert r["values"]["players"] == 2 and r["values"]["paused"] == 2
    assert not problems(r), problems(r)


def test_video_selection_deletes_and_downloads(browser, live, ready_clips):
    live.add_user("vseldel")
    r = browser.drive(live.url + "/", [*video_screen("vseldel"), *make_clips(3), ("eval", "all", ids("#video-feed")),
        ("click", "#video-select"), ("click", nth(1, card=CLIP)), ("click", nth(2, card=CLIP)),
        ("click", "#video-select-delete"), ("wait", is_open("#video-select-delete-dialog")),
        ("eval", "title", text_of("#video-select-delete-dialog h2")),
        ("click", "#video-select-delete-confirm"),
        ("wait", f"document.querySelectorAll('{CLIP}').length === 1 && !{selecting('.video')}"),
        ("eval", "spy", CLICKS), ("eval", "archiving", ARCHIVED),
        ("click", "#video-select"), ("click", nth(1, card=CLIP)),
        ("click", "#video-select-download"), ("wait", "window.clicked.length === 1"),
        ("eval", "href", "window.clicked[0][0]"), ("eval", "archived", "window.archived"),
        ("eval", "left", ids("#video-feed"))], width=1280, timeout=20000)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["title"] == "Удалить 2 ролика?" and v["left"] == [v["all"][2]]
    assert v["href"].startswith("/api/media/archives/") and len(v["archived"]) == 1 and len(v["archived"][0]) == 1
    assert not problems(r), problems(r)


@pytest.mark.parametrize("width", [390, 1280])
def test_video_tiles_and_table_open_the_clip_in_the_viewer(browser, live, ready_clips, width):
    login = f"vviews{width}"
    live.add_user(login)
    viewer = """(() => { const v = document.querySelector('#video-viewer video');
      return [v.getAttribute('src'), v.controls, document.querySelector('#video-viewer .viewer-meta').textContent,
        [...document.querySelectorAll('#video-viewer .viewer-actions .btn')].map(b => b.textContent)]; })()"""
    r = browser.drive(live.url + "/", [*video_screen(login),
        put_file("#video-file", png_of(48, 64, "olive")),
        ("wait", "document.querySelector('#video-source img')?.naturalWidth === 48"),
        ("click", "#video-go"), ("wait", clips("done", 1)),
        ("eval", "pressed", "document.querySelector('#video-view-preview').getAttribute('aria-checked')"),
        ("click", "#video-view"), ("click", "#video-view-tiles"), ("wait", f"document.querySelector('{CLIP} .video-open')"),
        ("eval", "tile", f"[document.querySelector('{CLIP} .card-cap').textContent, !!document.querySelector('{CLIP} video'),"
                         f" {grid_cols('#video-feed')}]"),
        ("click", f"{CLIP} .video-open"), ("wait", is_open("#video-viewer")),
        ("wait", "document.querySelectorAll('#video-viewer .chain-step').length === 2"),
        ("eval", "viewer", viewer),
        ("eval", "inside", f"[...document.querySelectorAll('#video-viewer button')].every(b => {{ const r = b.getBoundingClientRect();"
                           f" return r.left >= 0 && r.right <= innerWidth; }}) && {NO_OVERFLOW}"),
        ("eval", "spy", CLICKS), ("click", "#video-viewer .act-download"), ("wait", "window.clicked.length === 1"),
        ("eval", "download", "window.clicked[0][0]"),
        ("press", "Escape"), ("wait", "!document.querySelector('#video-viewer')"),
        ("click", "#video-view"), ("click", "#video-view-table"), ("wait", f"document.querySelector('{CLIP} .cell-prompt')"),
        ("eval", "row", f"[...document.querySelectorAll('{CLIP} .cell-meta > span')].map(s => s.textContent)"),
        ("eval", "tableFits", f"{NO_OVERFLOW} && document.querySelector('#video-feed').scrollWidth"
                              " <= document.querySelector('#video-feed').clientWidth"),
        ("click", f"{CLIP} .video-open"), ("wait", is_open("#video-viewer")),
        ("click", "#video-viewer .act-delete"), ("wait", is_open("#video-delete-dialog")),
        ("click", "#video-delete-confirm"),
        ("wait", f"!document.querySelector('#video-viewer') && !document.querySelector('{CLIP}')"),
        ("eval", "kept", "fetch('/api/me').then(r => r.json()).then(m => m.views.video)")], width=width, mobile=width == 390)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["pressed"] == "true"
    assert v["tile"] == ["5 с", False, 6 if width == 1280 else 2]
    src, controls, meta, actions = v["viewer"]
    assert src.startswith("/api/media/") and controls and meta.startswith("5 с · 576p · 32 к/с")
    assert actions == ["Скачать", "Удалить"] and v["inside"] and v["download"] == f"{src}?download=1"
    params, seed, when = v["row"]
    assert params == "5 с · 576p · 32 к/с" and seed.startswith("seed ") and re.fullmatch(r"\d\d:\d\d · \d+:\d\d", when)
    assert v["tableFits"] and v["kept"] == "table"
    assert any(s.startswith("DELETE /api/videos/") for s in r["sent"])
    assert not problems(r), problems(r)


def test_chat_chip_lists_only_checked_models_grouped_by_connection(browser, live):
    login = "chipper"
    live.add_user(login)
    check = f"""(async () => {{ const m = {API}; const s = (await m.api('GET', '/api/sections')).sections[0];
      const id = s.links[0].connection.id;
      await m.api('PUT', '/api/sections/chat/links/' + id, {{ models: ['local-gemma-abl', 'gemma4-abl'] }});
      return true; }})()"""
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), ("eval", "check", check),
        *go("settings"), ("wait", "document.querySelector('.settings-nav')"),
        *open_new_chat(), ("click", "#model-chip"), ("wait", "document.querySelector('.model-option')"),
        ("eval", "models", "[...document.querySelectorAll('.model-option')].map(o => o.dataset.model)"),
        ("eval", "groups", "document.querySelectorAll('.model-group-name').length")])
    assert not r.get("failed"), r
    assert r["values"]["models"] == ["local-gemma-abl", "gemma4-abl"] and r["values"]["groups"] == 0
    assert not problems(r), problems(r)


#: микрофон браузера без железа: запись — Blob 1 КиБ audio/webm, разрешение дано
FAKE_MIC = """(() => {
  navigator.mediaDevices.getUserMedia = async () => ({ getTracks: () => [{ stop() {} }] });
  window.MediaRecorder = class {
    static isTypeSupported(t) { return t === 'audio/webm'; }
    constructor(stream, opts) { this.mimeType = (opts && opts.mimeType) || 'audio/webm'; this.state = 'inactive'; this.l = {}; }
    addEventListener(n, f) { this.l[n] = f; }
    start() { this.state = 'recording'; }
    stop() { this.state = 'inactive'; this.l.dataavailable({ data: new Blob([new Uint8Array(1024)], { type: 'audio/webm' }) });
             this.l.stop(); }
  };
  return true; })()"""


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_mic_inserts_text_at_the_cursor(browser, live, width):
    """Текст встаёт на место курсора; идущая запись (кнопка шире, с таймером) — в рамке поля и середину поля
    не закрывает и на 320 px."""
    login = f"mic{width}"
    live.add_user(login)
    saved = live.gw.asr_reply
    live.gw.asr_reply = {"text": "кот"}
    put = """(() => { const f = document.querySelector('#photo-prompt'); f.value = 'рыжий на окне';
      f.setSelectionRange(6, 6); return f.selectionStart; })()"""
    try:
        r = browser.drive(live.url + "/", [*photo_screen(login, cards=1), ("wait", done_cards(1)),
            ("eval", "spy", SPY), ("eval", "mic", FAKE_MIC), ("eval", "put", put),
            ("click", "#photo-composer .mic"), ("wait", "document.querySelector('#photo-composer .mic.on')"),
            ("eval", "recording", field_state("#photo-prompt")),
            ("click", "#photo-composer .mic"),
            ("wait", "document.querySelector('#photo-prompt').value === 'рыжий кот на окне'"),
            ("eval", "sent", "window.sentForms.length"),
            ("eval", "overflow", f"!({NO_OVERFLOW})"),
            # микрофон есть и в окне «Изменить», в чате и в видео
            ("click", NEWEST_DONE), ("wait", is_open("#viewer")), ("click", "#viewer .act-edit"),
            ("wait", "document.querySelector('#editor .mic')"), ("press", "Escape"),
            *go("chat"), ("wait", "document.querySelector('#composer .mic')"),
            *go("video"), ("wait", "document.querySelector('#video-composer .mic')")],
            width=width, mobile=width < 760)
    finally:
        live.gw.asr_reply = saved
    assert not r.get("failed"), r
    assert r["values"]["sent"] == 0 and not r["values"]["overflow"]
    # [.mic.on в рамке, «очистить» в рамке, «очистить» скрыт, середина поля — поле, EN скрыт]
    assert r["values"]["recording"] == [True, True, False, True, False], r["values"]["recording"]
    assert not problems(r), problems(r)


def test_mic_inserts_at_the_new_cursor_when_the_field_was_edited_while_recording(browser, live):
    """Поле правили, пока шла запись: курсор на момент нажатия устарел — текст встаёт по нынешнему."""
    login = "micedit"
    live.add_user(login)
    saved = live.gw.asr_reply
    live.gw.asr_reply = {"text": "кот"}
    put = """(() => { const f = document.querySelector('#photo-prompt'); f.value = 'рыжий на окне';
      f.setSelectionRange(6, 6); return f.selectionStart; })()"""
    edit = """(() => { const f = document.querySelector('#photo-prompt'); f.value = 'большой рыжий на окне';
      f.setSelectionRange(14, 14); return true; })()"""
    try:
        r = browser.drive(live.url + "/", [*photo_screen(login, cards=1), ("wait", done_cards(1)),
            ("eval", "mic", FAKE_MIC), ("eval", "put", put),
            ("click", "#photo-composer .mic"), ("wait", "document.querySelector('#photo-composer .mic.on')"),
            ("eval", "edit", edit), ("click", "#photo-composer .mic"),
            ("wait", "document.querySelector('#photo-prompt').value !== 'большой рыжий на окне'"),
            ("eval", "value", "document.querySelector('#photo-prompt').value")], width=1280)
    finally:
        live.gw.asr_reply = saved
    assert not r.get("failed"), r
    assert r["values"]["value"] == "большой рыжий кот на окне"
    assert not problems(r), problems(r)


#: FAKE_MIC со счётчиками: window.mics — вызовы getUserMedia, window.stopped — отпущенные дорожки;
#: разрешение приходит через 300 мс, как после окна «Разрешить» браузера
COUNTED_MIC = FAKE_MIC.replace(
    "navigator.mediaDevices.getUserMedia = async () => ({ getTracks: () => [{ stop() {} }] });",
    """window.mics = 0; window.stopped = 0;
  navigator.mediaDevices.getUserMedia = async () => { window.mics += 1; await new Promise(r => setTimeout(r, 300));
    return { getTracks: () => [{ stop() { window.stopped += 1; } }] }; };""")
assert COUNTED_MIC != FAKE_MIC


def test_mic_stops_when_its_section_or_form_goes_away(browser, live):
    """Запись голоса не переживает свой раздел и своё окно: микрофон отпущен, звук не уходит, а
    повторное нажатие, пока браузер спрашивает разрешение, второй записи не начинает."""
    login = "micgone"
    live.add_user(login)
    double = "(() => { const b = document.querySelector('#photo-composer .mic'); b.click(); b.click(); return true; })()"
    r = browser.drive(live.url + "/", [*photo_screen(login, cards=1), ("wait", done_cards(1)), ("eval", "mic", COUNTED_MIC),
        ("eval", "double", double), ("wait", "document.querySelector('#photo-composer .mic.on')"),
        ("eval", "asked", "window.mics"),
        *go("chat"), ("wait", "window.stopped === 1 && document.querySelector('#composer .mic')"),
        # ушли из раздела, пока браузер спрашивал разрешение: микрофон отпущен сразу после ответа
        ("eval", "early", "(() => { document.querySelector('#composer .mic').click(); location.hash = '#/photo'; return true; })()"),
        ("wait", f"window.stopped === 2 && {FEED_READY}"),
        # окно «Изменить» закрыто во время записи
        ("click", NEWEST_DONE), ("wait", is_open("#viewer")), ("click", "#viewer .act-edit"),
        ("wait", "document.querySelector('#editor .mic')"), ("click", "#editor .mic"),
        ("wait", "document.querySelector('#editor .mic.on')"), ("click", "#editor .dialog-close"),
        ("wait", "window.stopped === 3"),
        # образец голоса в настройках: окно закрыто во время записи
        *go("settings/speech"), ("wait", "document.querySelector('#voice-add')"),
        ("click", "#voice-add"), ("wait", is_open("#voice-dialog")), ("click", "#voice-record"),
        ("wait", "document.querySelector('#voice-record').textContent === 'Остановить'"),
        ("click", "#voice-dialog .dialog-close"), ("wait", "window.stopped === 4"),
        ("eval", "on", settled("document.querySelectorAll('.mic.on').length", 500)), ("idle",)])
    assert not r.get("failed"), r
    assert r["values"]["asked"] == 1 and r["values"]["on"] == 0
    assert not any(s in ("POST /api/dictation", "POST /api/voices") for s in r["sent"]), r["sent"]
    assert not problems(r), problems(r)


def test_mic_without_permission_says_so(browser, live):
    live.add_user("nomic")
    deny = """(() => { navigator.mediaDevices.getUserMedia = async () => {
      throw new DOMException('denied', 'NotAllowedError'); }; return true; })()"""
    r = browser.drive(live.url + "/", [*photo_screen("nomic"), ("eval", "deny", deny),
        ("click", "#photo-composer .mic"),
        ("wait", "document.querySelector('#photo-composer .mic-error').textContent.length > 0"),
        ("eval", "words", "document.querySelector('#photo-composer .mic-error').textContent")])
    assert not r.get("failed"), r
    assert "разрешения на микрофон" in r["values"]["words"]


# ---- поле со значками и строка под полем (спека 2026-10-27-studio-ui-tidy, 4.4–4.5) ----

def field_state(field):
    """[микрофон в рамке поля, «очистить» в рамке, «очистить» скрыт, середина поля — само поле, EN скрыт или null]."""
    return f"""(() => {{ const f = document.querySelector({field!r}); const box = f.closest('.field-box');
      const b = box.getBoundingClientRect(); const r = f.getBoundingClientRect();
      const inside = (e) => {{ if (!e || e.hidden) return !!e; const x = e.getBoundingClientRect();
        return x.left >= b.left && x.right <= b.right && x.top >= b.top && x.bottom <= b.bottom; }};
      const clear = box.querySelector('.field-tools .field-clear'); const en = box.querySelector('#photo-translate');
      return [inside(box.querySelector('.field-tools .mic')), inside(clear), clear.hidden,
        document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2) === f, en ? en.hidden : null]; }})()"""


def field_steps(name, field, scope):
    """Пустое поле, набранный текст, «очистить»: значения `<name>-empty`, `<name>-typed`, `<name>-cleared`."""
    return [("eval", f"{name}-empty", field_state(field)), ("type", field, "кот"),
            ("eval", f"{name}-typed", field_state(field)), ("click", f"{scope} .field-clear"),
            ("eval", f"{name}-cleared", f"(() => {{ const f = document.querySelector({field!r});"
                                        " return [f.value, document.activeElement === f,"
                                        " f.closest('.field-box').querySelector('.field-clear').hidden]; })()")]


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_every_field_has_the_mic_and_clear_inside(browser, live, width):
    """Чат, фото, «Изменить», видео, озвучка, поиск: микрофон и «очистить» — в рамке поля, «очистить» виден
    только при тексте и очищает поле с фокусом в нём; середина поля значками не закрыта; EN фото — при тексте."""
    login = f"fields{width}"
    live.add_user(login)
    mobile = width < 760
    r = browser.drive(live.url + "/", [*photo_screen(login, cards=1), ("wait", done_cards(1)),
        *field_steps("photo", "#photo-prompt", "#photo-composer"),
        ("click", NEWEST_DONE), ("wait", is_open("#viewer")), ("click", "#viewer .act-edit"), ("wait", is_open("#editor")),
        *field_steps("editor", "#edit-prompt", "#editor"), ("press", "Escape"), ("wait", f"!{is_open('#editor')}"),
        *open_new_chat(mobile), ("wait", "document.querySelector('#chats').getBoundingClientRect().right <= 0"
                                         " || innerWidth > 760"),     # выдвижная панель списка уехала
        *field_steps("chat", "#chat-input", "#composer"),
        *go("video"), ("wait", "document.querySelector('#video-prompt')"),
        *field_steps("video", "#video-prompt", "#video-composer"),
        *go("speech"), ("wait", "document.querySelector('#speech-text')"),
        *field_steps("speech", "#speech-text", "#speech-composer"),
        *go("search"), ("wait", "document.querySelector('#search-q')"),
        *field_steps("search", "#search-q", ".search-page"),
        ("eval", "noNote", "!document.querySelector('.mic-note')"),
        ("eval", "overflow", f"!({NO_OVERFLOW})")], width=width, mobile=mobile, height=900)
    assert not r.get("failed"), r
    v = r["values"]
    for name in ("photo", "editor", "chat", "video", "speech", "search"):
        en = name == "photo" or None
        assert v[f"{name}-empty"] == [True, True, True, True, en], name
        assert v[f"{name}-typed"] == [True, True, False, True, en and False], name
        assert v[f"{name}-cleared"] == ["", True, True], name
    assert v["noNote"] and not v["overflow"]
    assert not problems(r), problems(r)


#: [середина главной кнопки − середина последнего видимого чипа, главная кнопка правее всех чипов, её высота]
BAR = """((go) => { const g = document.querySelector(go); const bar = g.closest('.composer-bar');
  const opts = [...bar.querySelector('.composer-opts').querySelectorAll(':scope > *, :scope > .chip-wrap > .chip')]
    .filter(e => !e.classList.contains('chip-wrap') && e.getBoundingClientRect().width > 0);
  const mid = (e) => { const b = e.getBoundingClientRect(); return b.top + b.height / 2; };
  const last = opts.reduce((a, e) => (mid(e) > mid(a) ? e : a));
  const gb = g.getBoundingClientRect();
  return [Math.round(Math.abs(mid(g) - mid(last))), opts.every(e => e.getBoundingClientRect().right <= gb.left),
    g.offsetHeight]; })"""


@pytest.mark.parametrize("width", [320, 390])
def test_composer_bars_keep_the_main_button_on_the_last_row(browser, live, width):
    """320 и 390 px: у чата 📎, «Интернет», «Рассуждение» и ↑ — одной строкой (слова кнопок — только в подписях);
    у фото, видео и озвучки главная кнопка справа на строке последнего чипа; поля seed нет; у чипа качества —
    название параметра."""
    login = f"bars{width}"
    live.add_user(login)
    local = f"""(async () => {{ const m = {API};
      const chat = (await m.api('GET', '/api/sections')).sections.find(x => x.id === 'chat');
      const link = chat.links.find(l => l.connection.kind === 'srvllm');
      await m.api('PUT', '/api/sections/chat/links/' + link.connection.id, {{ models: ['local-gemma-abl'] }});
      return true; }})()"""
    line = """[...document.querySelectorAll('#composer .attach, #web-toggle, #think-toggle, #send')].map(e => {
      const b = e.getBoundingClientRect(); return Math.round(b.top + b.height / 2); })"""
    words = "[...document.querySelectorAll('#web-toggle, #think-toggle')].map(e => [e.getAttribute('aria-label'), e.offsetWidth < 50])"
    r = browser.drive(live.url + "/", [*photo_screen(login),
        ("eval", "photo", f"({BAR})('#photo-go')"),
        ("eval", "photoSeed", "!!document.querySelector('#chip-seed, #photo-seed')"),
        ("eval", "sizeChip", text_of("#chip-size")),
        *go("video"), ("wait", "document.querySelector('#video-go')"),
        ("eval", "video", f"({BAR})('#video-go')"),
        ("eval", "videoSeed", "!!document.querySelector('#chip-vseed, #video-seed')"),
        ("eval", "videoChips", "[document.querySelector('#chip-quality').textContent,"
                               " document.querySelector('#chip-smooth').textContent, document.querySelector('#chip-smooth').title]"),
        ("eval", "qualityName", "[document.querySelector('#chip-quality').getAttribute('aria-label'),"
                                " document.querySelector('#chip-quality').title]"),
        ("click", "#chip-quality"), ("click", "#chip-quality-menu [data-value=high]"),
        ("eval", "qualityHigh", "[document.querySelector('#chip-quality').textContent,"
                                " document.querySelector('#chip-quality').getAttribute('aria-label')]"),
        *go("speech"), ("wait", "document.querySelector('#speech-go')"),
        ("eval", "speech", f"({BAR})('#speech-go')"),
        ("eval", "exa", EXA_KEY_SET), ("eval", "local", local),
        *open_new_chat(mobile=True),
        ("wait", "!document.querySelector('#think-toggle').hidden && !document.querySelector('#web-toggle').hidden"),
        ("eval", "chat", line), ("eval", "words", words),
        ("eval", "goHeight", "document.querySelector('#send').offsetHeight"),
        ("eval", "overflow", f"!({NO_OVERFLOW})")], width=width, mobile=True)
    assert not r.get("failed"), r
    v = r["values"]
    for name in ("photo", "video", "speech"):
        gap, rightmost, height = v[name]
        assert gap <= 4 and rightmost and height == 34, (name, v[name])
    assert not v["photoSeed"] and not v["videoSeed"] and v["sizeChip"] == "Портрет"
    quality, smooth, smooth_title = v["videoChips"]
    assert quality == "576p" and smooth == "плавно" and "32 к/с" in smooth_title
    assert v["qualityName"] == ["Качество: Обычно 576p"] * 2
    assert v["qualityHigh"] == ["720p", "Качество: Высоко 720p"]
    assert len(v["chat"]) == 4 and max(v["chat"]) - min(v["chat"]) <= 2, v["chat"]
    assert v["words"] == [["Интернет", True], ["Рассуждение", True]]
    assert v["goHeight"] == 34 and not v["overflow"]
    assert not problems(r), problems(r)


def box_geo(field):
    """[высота рамки поля, видимых значков, все они в рамке, середина поля — само поле, высота поля]."""
    return f"""(() => {{ const f = document.querySelector({field!r}); const box = f.closest('.field-box');
      const b = box.getBoundingClientRect(); const r = f.getBoundingClientRect();
      const tools = [...box.querySelectorAll('.field-tools > *')].filter(e => !e.hidden);
      return [Math.round(b.height), tools.length, tools.every(e => {{ const x = e.getBoundingClientRect();
        return x.left >= b.left - 0.5 && x.right <= b.right + 0.5 && x.top >= b.top - 0.5 && x.bottom <= b.bottom + 0.5; }}),
        document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2) === f, Math.round(r.height)]; }})()"""


def hint_fits(field):
    """[высота подсказки пустого поля, высота поля]: подсказка — значением в обход перехвата field.js
    (значки не появляются, ширина та же), высота — scrollHeight; поле затем снова пустое."""
    return f"""(() => {{ const f = document.querySelector({field!r});
      const set = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
      set.call(f, f.placeholder); const out = [f.scrollHeight, f.clientHeight]; set.call(f, ''); return out; }})()"""


def geo_steps(name, field, scope):
    """Рамка поля пустого, с одной буквой и после «очистить»: `<name>-empty`, `<name>-typed`, `<name>-cleared`;
    подсказка пустого — `<name>-hint` (hint_fits)."""
    return [("eval", f"{name}-empty", box_geo(field)), ("eval", f"{name}-hint", hint_fits(field)), ("type", field, "к"),
            ("eval", f"{name}-typed", box_geo(field)), ("click", f"{scope} .field-clear"),
            ("eval", f"{name}-cleared", box_geo(field))]


@pytest.mark.parametrize("width", [320, 390])
def test_one_line_fields_keep_their_height_and_clear_sends_input(browser, live, width):
    """Чат, фото, видео: с первой буквой появляются «очистить» (и «EN» у фото) — в ряд с микрофоном, рамка поля
    не меняет высоту, значки в рамке, середина поля свободна. «Очистить» шлёт `input`: поле чата сжимается
    обратно, счётчик озвучки — «0 / 2000», поиск — снова в ожидании."""
    login = f"oneline{width}"
    live.add_user(login)
    tall = """(() => { const f = document.querySelector('#chat-input'); f.value = 'кот '.repeat(80);
      f.dispatchEvent(new Event('input', { bubbles: true })); return Math.round(f.getBoundingClientRect().height); })()"""
    chat_height = "Math.round(document.querySelector('#chat-input').getBoundingClientRect().height)"
    state = "document.querySelector('#search-results').dataset.state"
    r = browser.drive(live.url + "/", [*photo_screen(login),
        *geo_steps("photo", "#photo-prompt", "#photo-composer"),
        *open_new_chat(mobile=True), ("wait", "document.querySelector('#chats').getBoundingClientRect().right <= 0"),
        *geo_steps("chat", "#chat-input", "#composer"),
        ("eval", "chatTall", tall), ("click", "#composer .field-clear"), ("eval", "chatBack", chat_height),
        *go("video"), ("wait", "document.querySelector('#video-prompt')"),
        *geo_steps("video", "#video-prompt", "#video-composer"),
        *go("speech"), ("wait", "document.querySelector('#speech-text')"),
        ("type", "#speech-text", "кот"), ("eval", "speechTyped", text_of("#speech-count")),
        ("click", "#speech-composer .field-clear"), ("eval", "speechCleared", text_of("#speech-count")),
        *go("search"), ("wait", "document.querySelector('#search-q')"),
        ("type", "#search-q", "кот"), ("wait", f"{state} === 'ready'"),
        ("click", ".search-page .field-clear"), ("wait", f"{state} === 'idle'"),
        ("eval", "searchCleared", "document.querySelector('#search-q').value"),
        ("eval", "overflow", f"!({NO_OVERFLOW})")], width=width, mobile=True, height=760)
    assert not r.get("failed"), r
    v = r["values"]
    for name, tools in (("photo", 3), ("chat", 2), ("video", 2)):
        empty, typed, cleared = v[f"{name}-empty"], v[f"{name}-typed"], v[f"{name}-cleared"]
        # у фото и видео на телефоне поле — под подсказку целиком (две строки, у видео уже 360 px — три), у чата — в одну
        assert empty[0] <= {"chat": 34, "photo": 50, "video": 72 if width < 360 else 50}[name], (name, empty)
        hint, height = v[f"{name}-hint"]
        assert name == "chat" or hint <= height, (name, v[f"{name}-hint"], empty)
        assert abs(typed[0] - empty[0]) <= 1 and cleared[0] == empty[0], (name, empty, typed, cleared)
        assert empty[1:4] == [1, True, True] and typed[1:4] == [tools, True, True], (name, empty, typed)
    assert v["chatTall"] > v["chat-empty"][4] + 20 and abs(v["chatBack"] - v["chat-empty"][4]) <= 1, (v["chatTall"], v["chatBack"])
    assert v["speechTyped"] == "3 / 2000" and v["speechCleared"] == "0 / 2000"
    assert v["searchCleared"] == "" and not v["overflow"]
    assert not problems(r), problems(r)


@pytest.mark.parametrize("width", [320, 390])
def test_photo_and_video_heads_fit_the_phone(browser, live, width):
    """Шапки «Фото» и «Видео» на телефоне: заголовок целиком, «Выбрать» не сжата, всё — левее лупы."""
    login = f"heads{width}"
    live.add_user(login)
    head = """((root) => { const h = document.querySelector(root + ' .head'); const t = h.querySelector('h1');
      const end = document.querySelector('#search-open').getBoundingClientRect().left;
      const kids = [...h.children].filter(e => e.getBoundingClientRect().width > 0 && !e.classList.contains('grow'));
      return [t.textContent, t.scrollWidth <= t.clientWidth, h.querySelector('.select-btn').offsetWidth,
        kids.every(e => e.getBoundingClientRect().right <= end)]; })"""
    r = browser.drive(live.url + "/", [*photo_screen(login), ("eval", "photo", f"({head})('.photo')"),
        *go("video"), ("wait", VIDEO_FEED_READY), ("eval", "video", f"({head})('.video')")],
        width=width, mobile=True)
    assert not r.get("failed"), r
    assert r["values"]["photo"] == ["Фото", True, 34, True], r["values"]["photo"]
    assert r["values"]["video"] == ["Видео", True, 34, True], r["values"]["video"]
    assert not problems(r), problems(r)


# ---- расшифровка ----------------------------------------------------------------

def wav_of(seconds=1.0, rate=16000) -> bytes:
    import wave
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * int(rate * seconds))
    return out.getvalue()


def put_audio(selector, data, name="Звонок.wav", mime="audio/wav"):
    """Запись в поле файла, как из окна выбора, и событие change."""
    b64 = base64.b64encode(data).decode()
    return ("eval", "file", f"""(() => {{ const bytes = Uint8Array.from(atob('{b64}'), c => c.charCodeAt(0));
      const dt = new DataTransfer(); dt.items.add(new File([bytes], '{name}', {{ type: '{mime}' }}));
      const input = document.querySelector('{selector}'); input.files = dt.files;
      input.dispatchEvent(new Event('change', {{ bubbles: true }})); return input.files.length; }})()""")


COPY_SPY = "(() => { window.copied = null; navigator.clipboard.writeText = async (t) => { window.copied = t; }; return true; })()"


@pytest.mark.parametrize("width", [390, 1280])
def test_transcribe_uploads_and_reads_by_speakers(browser, live, width):
    login = f"tr{width}"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(),
        *go("settings"), ("wait", "document.querySelector('.settings-nav')"),
        *go("transcribe"), ("wait", "document.querySelector('#transcribe-add input')"),
        put_audio("#transcribe-add input", wav_of(1.0)),
        ("wait", "document.querySelectorAll('.tr-view .tr-turn').length === 2"),
        ("eval", "who", "[...document.querySelectorAll('.tr-view .tr-who')].map(e => e.textContent)"),
        ("eval", "meta", "document.querySelector('.tr-view .tr-meta').textContent"),
        ("eval", "audio", "document.querySelector('.tr-view audio.tr-audio').getAttribute('src').startsWith('/api/media/')"),
        ("eval", "item", "document.querySelector('.tr-item[data-state=done]').textContent"),
        ("eval", "group", "document.querySelector('.tr-group').textContent"),
        ("eval", "copy", COPY_SPY), ("click", "#tr-copy"), ("wait", "window.copied"),
        ("eval", "copied", "window.copied"),
        ("eval", "spy", CLICKS), ("click", "#tr-download"), ("click", ".tr-formats [data-format=docx]"),
        ("wait", "window.clicked.length === 1"), ("eval", "clicked", "window.clicked[0]"),
        ("eval", "overflow", f"!({NO_OVERFLOW})")], width=width, mobile=width == 390)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["who"] == ["Собеседник 1 · 00:00", "Собеседник 2 · 00:03"]
    assert "собеседников: 2" in v["meta"] and v["audio"] and "Звонок.wav" in v["item"]
    assert v["group"] == "Сегодня"                     # подпись дня — dayTitle ленты (с годом, если не текущий)
    assert v["copied"].startswith("Собеседник 1 · 00:00\nЗдравствуйте.")
    assert v["clicked"][0].endswith("/export?format=docx") and v["clicked"][1]
    assert not v["overflow"]
    assert not problems(r), problems(r)


TR_TEXTS = "[...document.querySelectorAll('.tr-view .tr-text')].map(e => e.textContent)"
TR_TXT = f"""(async () => {{ const id = document.querySelector('.tr-item').dataset.id;
  return (await fetch('/api/transcripts/' + id + '/export?format=txt')).text(); }})()"""


def transcribed(login, seconds=6.0):
    """Вход, шлюз, загруженная и расшифрованная запись «Звонок.wav» (две реплики шлюза: 0 и 3 с)."""
    return [*signin(login), ("wait", READY), add_gateway(),
            *go("transcribe"), ("wait", "document.querySelector('#transcribe-add input')"),
            put_audio("#transcribe-add input", wav_of(seconds)),
            ("wait", "document.querySelectorAll('.tr-view .tr-turn').length === 2")]


@pytest.mark.parametrize("width", [390, 1280])
def test_transcribe_edits_names_seeks_and_exports(browser, live, width):
    """«Править» → текст реплики → «Сохранить»; «Имена» → «Иван»; выгрузка TXT — с ними; нажатие по
    реплике ставит плеер на её начало и подсвечивает её; «Сбросить правки» — исходное распознавание."""
    login = f"tredit{width}"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*transcribed(login),
        ("click", "#tr-edit"), ("wait", "document.querySelectorAll('.tr-edit-text').length === 2"),
        set_value(".tr-turn[data-index='1'] .tr-edit-text", "Добрый вечер."),
        ("eval", "marked", "!document.querySelector(\".tr-turn[data-index='1'] .tr-mark\").hidden"),
        ("click", "#tr-save"), ("wait", "!document.querySelector('.tr-edit-text')"),
        ("click", "#tr-names"), ("wait", is_open("#tr-names-dialog")),
        set_value("#tr-names-dialog input[data-label='Собеседник 1']", "Иван"),
        ("click", "#tr-names-save"), ("wait", "!document.querySelector('#tr-names-dialog')"),
        ("eval", "who", "[...document.querySelectorAll('.tr-view .tr-who')].map(e => e.textContent)"),
        ("eval", "texts", TR_TEXTS), ("eval", "txt", TR_TXT),
        ("eval", "fits", NO_OVERFLOW),
        ("click", ".tr-turn[data-index='1'] .tr-text"),
        ("wait", "Math.abs(document.querySelector('audio.tr-audio').currentTime - 3) < 0.3"),
        ("wait", "document.querySelector('.tr-turn[aria-current=true]')?.dataset.index === '1'"),
        ("click", "#tr-reset"), ("wait", is_open("#tr-reset-dialog")), ("click", "#tr-reset-confirm"),
        ("wait", "!document.querySelector('#tr-reset')"),
        ("eval", "after", TR_TEXTS),
        ("eval", "who_after", "document.querySelector('.tr-view .tr-who').textContent")],
        width=width, mobile=width == 390)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["marked"] is True and v["fits"] is True
    assert v["who"] == ["Иван · 00:00", "Собеседник 2 · 00:03"] and v["texts"] == ["Здравствуйте.", "Добрый вечер."]
    assert v["txt"] == "Иван · 00:00\nЗдравствуйте.\n\nСобеседник 2 · 00:03\nДобрый вечер.\n"
    assert v["after"] == ["Здравствуйте.", "Добрый день."] and v["who_after"] == "Собеседник 1 · 00:00"
    assert not problems(r), problems(r)


#: PATCH расшифровки идёт 1.5 с; window.patched — его ответ пришёл (и страница успела его применить)
SLOW_PATCH = """(() => { window.patched = false; const real = window.fetch;
  window.fetch = async (url, opts = {}) => {
    if (opts.method !== 'PATCH' || !String(url).includes('/api/transcripts/')) return real(url, opts);
    await new Promise(r => setTimeout(r, 1500));
    try { return await real(url, opts); } finally { setTimeout(() => { window.patched = true; }, 500); }
  }; return true; })()"""
TR_SHOWN = "[document.querySelector('.split-main h1').textContent, document.querySelector('.tr-item[aria-current=true]')?.dataset.id]"
TR_EXITS = "['#tr-copy', '#tr-download', '#tr-chat'].map(s => !!document.querySelector(s))"


def test_transcribe_edit_hides_the_exits_seeks_by_the_time_and_a_late_save_keeps_the_pick(browser, live):
    """Время реплики — кнопка (перемотка и с клавиатуры). Во время правки нет «Копировать», «Скачать» и
    «В чат». «Сохранить», а пока PATCH идёт, выбрана другая запись: ответ вид не забирает, правка — на сервере."""
    login = "trlate"
    other = seed_list(live, "transcribe", live.add_user(login), 1)[0]
    r = browser.drive(live.url + "/", [*transcribed(login),
        ("eval", "seek", """(() => { const b = document.querySelector(".tr-turn[data-index='1'] .tr-seek");
          b.focus(); b.click(); return [b.tagName, b.type, document.activeElement === b, b.textContent]; })()"""),
        ("wait", "Math.abs(document.querySelector('audio.tr-audio').currentTime - 3) < 0.3"),
        ("eval", "who", "document.querySelector(\".tr-turn[data-index='1'] .tr-who\").textContent"),
        ("click", "#tr-edit"), ("wait", "document.querySelectorAll('.tr-edit-text').length === 2"),
        ("eval", "editing", TR_EXITS),
        set_value(".tr-turn[data-index='1'] .tr-edit-text", "Добрый вечер."),
        ("eval", "spy", SLOW_PATCH), ("click", "#tr-save"),
        ("click", f".tr-item[data-id='{other}']"),
        ("wait", "document.querySelector('.split-main h1').textContent === 'Запись 0' && window.patched"),
        ("eval", "shown", TR_SHOWN),
        ("click", f".tr-item:not([data-id='{other}'])"),
        ("wait", "document.querySelector('.split-main h1').textContent === 'Звонок.wav'"),
        ("eval", "texts", TR_TEXTS), ("eval", "after", TR_EXITS)], timeout=20000)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["seek"] == ["BUTTON", "button", True, "00:03"] and v["who"] == "Собеседник 2 · 00:03"
    assert v["editing"] == [False, False, False] and v["after"] == [True, True, True]
    assert v["shown"] == ["Запись 0", other]
    assert v["texts"] == ["Здравствуйте.", "Добрый вечер."]
    assert not known_refusals(problems(r), 404), problems(r)      # плеер записи без файла (строка — без media)


#: первый список расшифровок идёт 2 с; window.slow — он пришёл (и страница успела бы выбрать запись)
SLOW_FIRST_LIST = """(() => { window.slow = false; let first = true; const real = window.fetch;
  window.fetch = async (url, opts = {}) => {
    if (!first || !/^\\/api\\/transcripts\\?/.test(String(url))) return real(url, opts);
    first = false;
    await new Promise(r => setTimeout(r, 2000));
    try { return await real(url, opts); } finally { setTimeout(() => { window.slow = true; }, 1000); }
  }; return true; })()"""


def test_transcribe_link_does_not_take_a_record_picked_meanwhile(browser, live):
    """#/transcribe?id=<A>, а пока идёт первый список, загрузили запись: она и остаётся выбранной."""
    login = "trwanted"
    wanted = seed_list(live, "transcribe", live.add_user(login), 1)[0]
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
        ("wait", "document.querySelector('.settings-nav')"),
        ("eval", "spy", SLOW_FIRST_LIST), ("eval", "go", f"location.hash = '#/transcribe?id={wanted}'"),
        ("wait", "document.querySelector('#transcribe-add input')"),
        put_audio("#transcribe-add input", wav_of(1.0)),
        ("wait", "document.querySelector('.split-main h1').textContent === 'Звонок.wav'"),
        ("wait", "window.slow"),
        ("eval", "shown", "document.querySelector('.split-main h1').textContent")], timeout=20000)
    assert not r.get("failed"), r
    assert r["values"]["shown"] == "Звонок.wav"
    assert not known_refusals(problems(r), 404), problems(r)


TURNS_SO_FAR = "performance.getEntriesByType('resource').filter(e => e.name.includes('/turns')).length"
CHAT_CHIP = "document.querySelector('#composer .att-chip .att-open')"


@pytest.mark.parametrize("width", [390, 1280])
def test_transcript_to_chat_with_the_local_model_and_back(browser, live, width):
    """«В чат ▾» → «Составить итоги»: новый диалог с вложением «Звонок.txt» (число знаков, текст по
    нажатию) и вопросом, модель — локальная по умолчанию, ничего не отправлено; после отправки —
    ссылка «к записи» открывает эту расшифровку."""
    login = f"trchat{width}"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*transcribed(login, 1.0),
        ("eval", "tid", "document.querySelector('.tr-item').dataset.id"),
        ("click", "#tr-chat"), ("click", ".tr-chat-menu [data-ask=summary]"),
        ("wait", CHAT_CHIP), ("eval", "hash", "location.hash"),
        ("eval", "chip", "document.querySelector('#composer .att-chip').textContent"),
        ("eval", "question", "document.querySelector('#chat-input').value"),
        ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
        ("eval", "ready", "!document.querySelector('#send').disabled && !document.querySelector('#model-note').textContent"),
        ("click", "#composer .att-open"), ("wait", is_open("#att-text-dialog")),
        ("eval", "shown", "document.querySelector('#att-text-dialog .att-text').textContent"),
        ("eval", "tokens", "document.querySelector('#att-text-dialog p').textContent"),
        ("click", "#att-text-dialog .dialog-close"), ("wait", "!document.querySelector('#att-text-dialog')"),
        ("eval", "fits", NO_OVERFLOW), ("eval", "before", TURNS_SO_FAR),
        ("click", "#send"), ("wait", ANSWERED), ("wait", "document.querySelector('#messages .att-source')"),
        ("click", "#messages .att-source"),
        ("wait", "location.hash === '#/transcribe' && document.querySelectorAll('.tr-view .tr-turn').length === 2"),
        ("eval", "back", "[document.querySelector('.tr-item[aria-current=true]').dataset.id,"
                         " document.querySelector('.split-main h1').textContent]")],
        width=width, mobile=width == 390)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["hash"] == "#/chat" and "Звонок.txt" in v["chip"] and "зн." in v["chip"]
    assert v["question"] == "Составь краткие итоги этой записи: о чём говорили и к чему пришли."
    assert v["ready"] is True and v["fits"] is True and v["before"] == 0
    assert v["shown"].startswith("Расшифровка «Звонок.wav»") and "Собеседник 1 · 00:00\nЗдравствуйте." in v["shown"]
    assert "тыс. токенов" in v["tokens"]
    content = live.gw.chat_requests[-1]["messages"][-1]["content"]
    assert live.gw.chat_requests[-1]["model"] == "local-gemma-abl"
    assert content.startswith('<document name="Звонок.txt">\nРасшифровка «Звонок.wav»') and content.endswith(v["question"])
    assert v["back"] == [v["tid"], "Звонок.wav"]          # ?id выбрал запись и ушёл из адреса (как «Открыть» в «В работе»)
    assert not problems(r), problems(r)


def test_transcript_to_chat_without_a_local_model_asks_first(browser, live):
    """В чате отмечена только облачная роль шлюза: модель не выбрана и «Отправить» закрыто; после
    явного выбора — окно «уйдёт во внешний сервис», и только «Отправить» в нём шлёт ход."""
    live.add_user("trcloud")
    r = browser.drive(live.url + "/", [*transcribed("trcloud", 1.0),
        ("eval", "cloud_only", f"""(async () => {{ const m = {API}; const s = await m.api('GET', '/api/sections');
          const link = s.sections.find(x => x.id === 'chat').links[0];
          await m.api('PUT', '/api/sections/chat/links/' + link.connection.id, {{ models: ['gemma4-abl'] }});
          return true; }})()"""),
        ("click", "#tr-chat"), ("click", ".tr-chat-menu [data-ask=tasks]"),
        ("wait", CHAT_CHIP),
        ("wait", "document.querySelector('#model-chip').textContent.includes('модель не выбрана')"),
        ("eval", "locked", "[document.querySelector('#send').disabled, document.querySelector('#model-note').textContent]"),
        ("click", "#model-chip"), ("click", "#model-menu .model-option[data-model='gemma4-abl']"),
        ("eval", "open", "[document.querySelector('#send').disabled, document.querySelector('#model-note').textContent]"),
        ("click", "#send"), ("wait", is_open("#external-dialog")),
        ("eval", "asked", "document.querySelector('#external-dialog p').textContent"),
        ("eval", "before", TURNS_SO_FAR),
        ("click", "#external-confirm"), ("wait", ANSWERED)])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["locked"] == [True, "Запись уйдёт во внешний сервис — выберите модель"] and v["open"] == [False, ""]
    assert v["asked"] == "Расшифровка уйдёт во внешний сервис «Шлюз» — отправить?" and v["before"] == 0
    assert live.gw.chat_requests[-1]["model"] == "gemma4-abl"
    assert live.gw.chat_requests[-1]["messages"][-1]["content"].endswith("сроки, если названы.")
    assert not problems(r), problems(r)


ASSISTANTS = "document.querySelectorAll('#messages .msg.assistant').length"
NOT_ASKED = "!document.querySelector('#external-dialog')"
UNMARK_LOCAL = f"""(async () => {{ const m = {API}; const s = await m.api('GET', '/api/sections');
  const link = s.sections.find(x => x.id === 'chat').links[0];
  await m.api('PUT', '/api/sections/chat/links/' + link.connection.id, {{ models: ['gemma4-abl'] }});
  return true; }})()"""


def test_transcript_in_the_chat_history_asks_once_per_cloud_model(browser, live):
    """Расшифровка ушла локальной модели. Её сняли с отметки — диалог на ней ходит без вопроса (признак
    сервера `local`, а не отмеченные модели). Смена модели на облачную — окно «уйдёт во внешний сервис»
    и для хода без нового вложения; после подтверждения — без окна, и «Ответить заново» тоже."""
    live.add_user("trhist")
    sent = len(live.gw.chat_requests)
    r = browser.drive(live.url + "/", [*transcribed("trhist", 1.0),
        ("click", "#tr-chat"), ("click", ".tr-chat-menu [data-ask=summary]"), ("wait", CHAT_CHIP),
        ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
        ("click", "#send"), ("wait", f"{ASSISTANTS} === 1 && {ANSWERED}"), ("wait", CHAT_ID),
        ("eval", "unmark", UNMARK_LOCAL),
        ("eval", "reload", "location.reload()"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"),
        ("wait", f"{ASSISTANTS} === 1 && {ANSWERED}"),
        ("type", "#chat-input", "Ещё"), ("click", "#send"),
        ("wait", f"{ASSISTANTS} === 2 && {ANSWERED}"), ("eval", "local", NOT_ASKED),
        ("click", "#model-chip"), ("click", "#model-menu .model-option[data-model='gemma4-abl']"),
        ("wait", "document.querySelector('#model-chip').textContent.includes('gemma4-abl')"),
        ("type", "#chat-input", "Облаку"), ("click", "#send"), ("wait", is_open("#external-dialog")),
        ("eval", "asked", "document.querySelector('#external-dialog p').textContent"),
        ("eval", "before", TURNS_SO_FAR),
        ("click", "#external-confirm"), ("wait", f"{ASSISTANTS} === 3 && {ANSWERED}"),
        ("type", "#chat-input", "Ещё облаку"), ("click", "#send"),
        ("wait", f"{ASSISTANTS} === 4 && {ANSWERED}"), ("eval", "again", NOT_ASKED),
        ("click", "#messages .msg.assistant:last-child .msg-retry"),
        ("wait", f"{TURNS_SO_FAR} === 4 && {ANSWERED}"), ("eval", "retried", NOT_ASKED)], timeout=20000)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["local"] and v["again"] and v["retried"] and v["before"] == 1
    assert v["asked"] == "Расшифровка уйдёт во внешний сервис «Шлюз» — отправить?"
    assert [q["model"] for q in live.gw.chat_requests[sent:]] == [
        "local-gemma-abl", "local-gemma-abl", "gemma4-abl", "gemma4-abl", "gemma4-abl"]
    assert not problems(r), problems(r)


def test_chat_internet_transcript_asks_before_exa(browser, live):
    """Расшифровка в диалог локальной модели с включённым «Интернетом» и ключом Exa: окно «могут уйти в
    Exa», ход — только после «Отправить» в нём; следующий ход того же диалога — без окна."""
    live.add_user("trweb")
    r = browser.drive(live.url + "/", [*transcribed("trweb", 1.0), ("eval", "exa", EXA_KEY_SET),
        ("click", "#tr-chat"), ("click", ".tr-chat-menu [data-ask=summary]"), ("wait", CHAT_CHIP),
        ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
        ("wait", f"{WEB_TOGGLE} && !{WEB_TOGGLE}.hidden"), ("click", "#web-toggle"),
        ("click", "#send"), ("wait", is_open("#web-dialog")),
        ("eval", "asked", "document.querySelector('#web-dialog p').textContent"),
        ("eval", "before", TURNS_SO_FAR), ("eval", "external", NOT_ASKED),
        ("click", "#web-confirm"), ("wait", f"{ASSISTANTS} === 1 && {ANSWERED}"),
        ("eval", "web", f"""(async () => {{ const m = {API};
          return (await m.api('GET', '/api/chats/' + location.hash.split('/')[2])).chat.web; }})()"""),
        ("type", "#chat-input", "Ещё"), ("click", "#send"),
        ("wait", f"{ASSISTANTS} === 2 && {ANSWERED}"), ("eval", "again", "!document.querySelector('#web-dialog')")])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["asked"] == "Запросы, составленные из расшифровки, могут уйти в Exa — отправить?"
    assert v["before"] == 0 and v["external"] and v["web"] is True and v["again"]
    assert not problems(r), problems(r)


def test_chat_internet_and_a_cloud_model_ask_in_turn(browser, live):
    """Расшифровка, нелокальная модель и «Интернет» с ключом Exa: сначала окно внешнего сервиса, затем окно
    Exa, ход — одним запросом после обоих."""
    live.add_user("trboth")
    r = browser.drive(live.url + "/", [*transcribed("trboth", 1.0), ("eval", "exa", EXA_KEY_SET),
        ("eval", "unmark", UNMARK_LOCAL),
        ("click", "#tr-chat"), ("click", ".tr-chat-menu [data-ask=tasks]"), ("wait", CHAT_CHIP),
        ("click", "#model-chip"), ("click", "#model-menu .model-option[data-model='gemma4-abl']"),
        ("wait", f"{WEB_TOGGLE} && !{WEB_TOGGLE}.hidden"), ("click", "#web-toggle"),
        ("click", "#send"), ("wait", is_open("#external-dialog")), ("eval", "first", "!document.querySelector('#web-dialog')"),
        ("click", "#external-confirm"), ("wait", is_open("#web-dialog")),
        ("eval", "before", TURNS_SO_FAR),
        ("click", "#web-confirm"), ("wait", f"{ASSISTANTS} === 1 && {ANSWERED}"),
        ("eval", "turns", TURNS_SO_FAR)])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["first"] and v["before"] == 0 and v["turns"] == 1
    assert live.gw.chat_requests[-1]["model"] == "gemma4-abl" and live.gw.chat_requests[-1].get("tools")
    assert not problems(r), problems(r)


def test_transcript_to_chat_that_fails_leaves_the_sections_default(browser, live):
    """Вложение из расшифровки не создалось (запись удалили): чип — умолчание раздела, а не «модель не
    выбрана»: сообщение без вложения уйдёт в диалог с этим умолчанием."""
    live.add_user("trgone")
    r = browser.drive(live.url + "/", [*transcribed("trgone", 1.0),
        ("eval", "gone", f"""(async () => {{ const m = {API}; const id = document.querySelector('.tr-item').dataset.id;
          await m.api('DELETE', '/api/transcripts/' + id); window.gone = id; return id; }})()"""),
        ("eval", "unmark", UNMARK_LOCAL),
        ("eval", "go", "location.hash = '#/chat?transcript=' + window.gone + '&ask=summary'"),
        ("wait", "document.querySelector('#chat-error')?.textContent"),
        ("wait", "document.querySelector('#model-chip').textContent.includes('gemma4-abl')"),
        ("eval", "open", "[document.querySelector('#send').disabled, document.querySelector('#model-note').textContent]")])
    assert not r.get("failed"), r
    assert r["values"]["open"] == [False, ""]
    assert not known_refusals(problems(r), 404), problems(r)


# ---- распознавание ---------------------------------------------------------------

#: ответ HunyuanOCR: заголовок, HTML-таблица (должна стать таблицей) и то, что не должно сработать
OCR_MARKDOWN = ("# Отчёт\n\n<table><tr><td>Офис</td><td>38,5</td></tr></table>\n\n"
                "<script>window.pwned = 1</script>\n\n<a href=\"javascript:window.pwned = 2\">ссылка</a>")
OCR_PAGE = "document.querySelector('#ocr-page')?.textContent"


def put_files(selector, files, mime="image/png"):
    """Несколько файлов в поле одним выбором, как из окна выбора, и событие change."""
    parts = ", ".join(f"['{base64.b64encode(data).decode()}', '{name}']" for name, data in files)
    return ("eval", "files", f"""(() => {{ const dt = new DataTransfer();
      for (const [b64, name] of [{parts}]) dt.items.add(new File([Uint8Array.from(atob(b64), c => c.charCodeAt(0))],
        name, {{ type: '{mime}' }}));
      const input = document.querySelector('{selector}'); input.files = dt.files;
      input.dispatchEvent(new Event('change', {{ bubbles: true }})); return input.files.length; }})()""")


def recognized(login, files):
    """Вход, шлюз, «Распознавание», фото страниц одним выбором — документ готов."""
    return [*signin(login), ("wait", READY), add_gateway(),
            *go("ocr"), ("wait", "document.querySelector('#ocr-add input')"),
            put_files("#ocr-add input", files),
            ("wait", "document.querySelector('.ocr-item[data-state=done]') && document.querySelector('.ocr-text')")]


@pytest.fixture
def ocr_reply(live):
    before = live.gw.ocr_reply
    live.gw.ocr_reply = {"choices": [{"message": {"content": OCR_MARKDOWN}, "finish_reason": "stop"}]}
    try:
        yield
    finally:
        live.gw.ocr_reply = before


@pytest.mark.parametrize("width", [390, 1280])
def test_ocr_uploads_photos_and_pages_through_scan_and_text(browser, live, ocr_reply, width):
    """Два фото одним выбором — один документ из двух страниц: слева (на телефоне — сверху) картинка
    страницы, справа текст с HTML-таблицей; «›» — вторая страница; нажатие на картинку — окно просмотра."""
    from scans import photo
    login = f"ocr{width}"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*recognized(login, [("Договор.png", photo("PNG", (800, 600))),
                                                           ("Лист 2.png", photo("PNG", (600, 800)))]),
        ("eval", "item", "document.querySelector('.ocr-item[data-state=done]').textContent"),
        ("eval", "title", "document.querySelector('.split-main h1').textContent"),
        ("eval", "meta", "document.querySelector('.ocr-meta').textContent"),
        ("eval", "page1", OCR_PAGE),
        ("wait", "document.querySelector('img.ocr-scan')?.complete && document.querySelector('img.ocr-scan').naturalWidth > 0"),
        ("eval", "scan1", "document.querySelector('img.ocr-scan').naturalWidth"),
        ("eval", "cells", "[...document.querySelectorAll('.ocr-text td')].map(e => e.textContent)"),
        ("eval", "unsafe", "[document.querySelectorAll('.ocr-view script').length, window.pwned || 0,"
                           " [...document.querySelectorAll('.ocr-text a')].some(a => a.href.startsWith('javascript'))]"),
        ("eval", "placed", """(() => { const s = document.querySelector('.ocr-scan').getBoundingClientRect();
          const t = document.querySelector('.ocr-text').getBoundingClientRect();
          return [s.bottom <= t.top + 0.5, s.right <= t.left + 0.5]; })()"""),
        ("eval", "fits", NO_OVERFLOW),
        ("eval", "prev", "document.querySelector('#ocr-prev').disabled"),
        ("click", "#ocr-next"), ("wait", f"{OCR_PAGE} === 'страница 2 из 2'"),
        ("wait", "document.querySelector('img.ocr-scan')?.complete && document.querySelector('img.ocr-scan').naturalWidth === 600"),
        ("eval", "next", "document.querySelector('#ocr-next').disabled"),
        ("click", "img.ocr-scan"), ("wait", is_open("#ocr-viewer")),
        ("eval", "viewer", "document.querySelector('#ocr-viewer img').getAttribute('src').startsWith('/api/media/')"),
        ("click", "#ocr-viewer .viewer-close"), ("wait", "!document.querySelector('#ocr-viewer')")],
        width=width, mobile=width == 390)
    assert not r.get("failed"), r
    v = r["values"]
    assert "Договор.png" in v["item"] and "2 стр." in v["item"] and v["title"] == "Договор.png"
    assert v["meta"].startswith("2 страницы · ")
    assert v["page1"] == "страница 1 из 2" and v["scan1"] == 800 and v["prev"] is True and v["next"] is True
    assert v["cells"] == ["Офис", "38,5"] and v["unsafe"] == [0, 0, False]
    assert v["placed"] == ([True, False] if width == 390 else [False, True])   # телефон — над текстом, иначе — слева
    assert v["fits"] is True and v["viewer"] is True
    assert not problems(r), problems(r)


def test_ocr_download_and_to_chat(browser, live, ocr_reply):
    """«Скачать ▾» — .txt, .md, .docx (ссылка на выгрузку); «Копировать» — весь текст по страницам;
    «В чат ▾» — три просьбы, нажатие ведёт в #/chat?ocr=<id>&ask=<просьба> (вложение — Task 8 чата)."""
    from scans import photo
    live.add_user("ocrdl")
    r = browser.drive(live.url + "/", [*recognized("ocrdl", [("Счёт.png", photo("PNG"))]),
        ("eval", "sid", "document.querySelector('.ocr-item').dataset.id"),
        ("eval", "pager", "!!document.querySelector('#ocr-page')"),
        ("eval", "copy", COPY_SPY), ("click", "#ocr-copy"), ("wait", "window.copied"),
        ("eval", "copied", "window.copied"),
        ("eval", "spy", CLICKS), ("click", "#ocr-download"),
        ("eval", "formats", "[...document.querySelectorAll('.ocr-formats [data-format]')].map(b => b.dataset.format)"),
        ("click", ".ocr-formats [data-format=md]"),
        ("wait", "window.clicked.length === 1"), ("eval", "clicked", "window.clicked[0]"),
        ("click", "#ocr-chat"),
        ("eval", "asks", "[...document.querySelectorAll('.ocr-chat-menu [data-ask]')].map(b => [b.dataset.ask, b.textContent])"),
        ("eval", "hashes", "(() => { window.hashes = []; addEventListener('hashchange', (e) => window.hashes.push("
                           "new URL(e.newURL).hash)); return true; })()"),
        ("click", ".ocr-chat-menu [data-ask=translate]"), ("wait", "window.hashes.length"),
        ("eval", "went", "window.hashes[0]"),
        ("wait", f"{CHAT_CHIP}?.textContent === 'Счёт.md'"),
        ("eval", "chat", "[location.hash, document.querySelector('#chat-input').value]")])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["pager"] is False                                       # одна страница — без листалки
    assert v["copied"] == f"## Страница 1\n\n{OCR_MARKDOWN}\n"
    assert v["formats"] == ["txt", "md", "docx"]
    assert v["clicked"] == [f"/api/ocr/{v['sid']}/export?format=md", True]
    assert v["asks"] == [["discuss", "Обсудить в чате"], ["summary", "Кратко пересказать"], ["translate", "Перевести"]]
    assert v["went"] == f"#/chat?ocr={v['sid']}&ask=translate"
    assert v["chat"] == ["#/chat", "Переведи этот документ на русский"]
    assert not problems(r), problems(r)


SCAN_BADGE = f"{LAST_ANSWER}?.querySelector('.web-badge.busy')?.textContent"


def scan_file(name="скан.pdf"):
    """PDF из двух страниц-сканов (без текстового слоя) — выражение JS со списком File для `attach`."""
    from scans import scan_pdf
    data = base64.b64encode(scan_pdf("ink", "ink")).decode()
    return f"[new File([Uint8Array.from(atob({data!r}), c => c.charCodeAt(0))], {name!r}, {{ type: 'application/pdf' }})]"


def test_chat_scan_shows_progress_then_answers(browser, live):
    """Скан в чате через сервер: ответ ждёт распознавания — над ним плашка «📄 Распознаю скан · стр. N из M»
    (событие SSE `ocr`); ушли в «Фото» и вернулись — плашка на месте; перезагрузка — плашка из `ocr` сообщения
    (опрос идущего ответа); распознали — ответ модели, плашки нет, модели ушёл распознанный текст."""
    import state
    live.add_user("scanner")
    live.gw.ocr_hold = lambda request: state.DB.execute(
        "SELECT 1 FROM chats WHERE title = 'скан виден'").fetchone() is None
    try:
        r = browser.drive(live.url + "/", [*signin("scanner"), ("wait", READY), add_gateway(),
            *go("settings"), ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
            attach(scan_file()), ("wait", "document.querySelector('#composer .att-chip')"),
            ("type", "#chat-input", "О чём документ?"), ("press", "Enter"),
            ("wait", f"{SCAN_BADGE}?.includes('Распознаю скан')"),
            ("eval", "own", f"[{SCAN_BADGE}, !!{LAST_ANSWER}.querySelector('.web-badge .spin'),"
                            f" {LAST_ANSWER}.querySelector('.msg-meta').textContent]"),
            ("eval", "cid", "window.cid = location.hash"),
            *go("photo"), ("wait", "document.querySelector('#photo-feed')"),
            ("eval", "back", "location.hash = window.cid"),
            ("wait", f"{SCAN_BADGE}?.includes('Распознаю скан')"),
            ("eval", "again", SCAN_BADGE),
            ("eval", "reload", "location.reload()"),          # поток SSE открыт — страница не затихнет, ("idle",) нельзя
            ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"),
            ("wait", f"{SCAN_BADGE}?.includes('Распознаю скан')"),
            ("eval", "other", SCAN_BADGE),
            ("eval", "seen", MARK % "скан виден"),
            ("wait", f"{ANSWERED} && {LAST_ANSWER}.querySelector('.md').textContent.trim() === 'Здравствуйте!'"),
            ("eval", "after", f"[{LAST_ANSWER}.querySelector('.web-badge').hidden,"
                              " document.querySelector('#messages .msg.user .att').textContent]")])
    finally:
        live.gw.ocr_hold = False
    assert not r.get("failed"), r
    v = r["values"]
    assert v["own"] == ["📄 Распознаю скан · стр. 1 из 2", True, ""]
    assert v["again"] == "📄 Распознаю скан · стр. 1 из 2"
    assert v["other"] == "📄 Распознаю скан · стр. 1 из 2"
    assert v["after"][0] is True and "скан.pdf" in v["after"][1]
    content = live.gw.chat_requests[-1]["messages"][-1]["content"]
    assert content.startswith('<document name="скан.pdf">') and "Текст страницы." in content
    assert content.endswith("О чём документ?")
    assert not problems(r), problems(r)


def test_chat_scan_on_a_direct_connection_waits_in_the_page(browser, live):
    """Прямое подключение: сервер на ход с неготовым сканом отвечает 409 `not_ready` — под полем «скан ещё
    распознаётся — отправлю, когда будет готов», страница повторяет ход сама; распознали — ответ облака по
    распознанному тексту, ход ушёл один раз."""
    import state
    live.add_user("scandirect")
    live.gw.ocr_hold = lambda request: state.DB.execute(
        "SELECT 1 FROM chats WHERE title = 'скан прямой'").fetchone() is None
    sent = len(live.cloud.chat_requests)
    try:
        r = browser.drive(live.url + "/", [*signin("scandirect"), ("wait", READY), add_gateway(), add_cloud(live.cloud),
            *go("settings"), ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(),
            ("click", "#model-chip"), ("wait", "document.querySelector('.model-option[data-model=\"cloud-chat\"]')"),
            ("click", ".model-option[data-model=\"cloud-chat\"]"),
            ("wait", "document.querySelector('#model-chip').textContent.includes('cloud-chat')"),
            attach(scan_file()), ("wait", "document.querySelector('#composer .att-chip')"),
            ("type", "#chat-input", "О чём документ?"), ("press", "Enter"),
            ("wait", "document.querySelector('#chat-error').textContent"),
            ("eval", "waiting", "[document.querySelector('#chat-error').textContent,"
                                " document.querySelector('#chat-input').value,"
                                " document.querySelectorAll('#composer .att-chip').length]"),
            ("eval", "seen", MARK % "скан прямой"),
            ("wait", f"{ANSWERED} && {LAST_ANSWER}.querySelector('.md').textContent.trim() === 'Облако на связи.'"),
            ("eval", "after", "[document.querySelector('#chat-error').textContent,"
                              " document.querySelectorAll('#composer .att-chip').length]")])
    finally:
        live.gw.ocr_hold = False
    assert not r.get("failed"), r
    v = r["values"]
    assert v["waiting"] == ["скан ещё распознаётся — отправлю, когда будет готов", "О чём документ?", 1]
    assert v["after"] == ["", 0]
    assert len(live.cloud.chat_requests) == sent + 1
    content = live.cloud.chat_requests[-1]["messages"][-1]["content"]
    assert content.startswith('<document name="скан.pdf">') and "Текст страницы." in content
    assert not known_refusals(problems(r), 409), problems(r)


def test_chat_scan_failure_words_stay_after_a_reload(browser, live):
    """Скан не распознан: под ответом — слова причины, и после перезагрузки тоже, а не «ответ не получен»
    (ревью M1)."""
    import chat
    live.add_user("scanfail")
    live.gw.ocr_replies = [(500, {"error": {"message": "внутренняя ошибка"}})]
    live.gw.ocr_hold = lambda request: not chat.STREAMS          # отказ — когда ход уже ждёт скан
    meta = f"{LAST_ANSWER}?.querySelector('.msg-meta')?.textContent"
    try:
        r = browser.drive(live.url + "/", [*signin("scanfail"), ("wait", READY), add_gateway(),
            *go("settings"), ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
            attach(scan_file()), ("wait", "document.querySelector('#composer .att-chip')"),
            ("type", "#chat-input", "О чём документ?"), ("press", "Enter"),
            ("wait", f"{meta}?.startsWith('скан не распознан')"),
            ("eval", "live", meta),
            ("eval", "reload", "location.reload()"),
            ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"),
            ("wait", f"{meta}?.startsWith('скан не распознан')"),
            ("eval", "after", f"[{meta}, {LAST_ANSWER}.querySelector('.md').textContent]")])
    finally:
        live.gw.ocr_replies = []
        live.gw.ocr_hold = False
    assert not r.get("failed"), r
    v = r["values"]
    assert v["live"].startswith("скан не распознан: ") and v["after"] == [v["live"], ""]


def test_chat_scan_wait_is_dropped_when_the_field_changes(browser, live):
    """Прямое подключение ждёт скан, а человек дописал вопрос: ожидание снято, «скан ещё распознаётся…»
    убрано, и по готовности скана ход сам не уходит — уходит по «Отправить», с новым текстом (ревью M6)."""
    import state
    live.add_user("scanedit")
    live.gw.ocr_hold = lambda request: state.DB.execute(
        "SELECT 1 FROM chats WHERE title = 'скан правят'").fetchone() is None
    sent = len(live.cloud.chat_requests)
    quiet = (f"(async () => {{ for (;;) {{ const {{ items }} = await {API}.api('GET', '/api/ocr');"
             " if (items[0] && items[0].state === 'done') break; await new Promise(r => setTimeout(r, 50)); }"
             " await new Promise(r => setTimeout(r, 3500));"
             " return [document.querySelectorAll('#messages .msg').length,"
             " document.querySelector('#chat-error').textContent]; })()")
    try:
        r = browser.drive(live.url + "/", [*signin("scanedit"), ("wait", READY), add_gateway(), add_cloud(live.cloud),
            *go("settings"), ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(),
            ("click", "#model-chip"), ("wait", "document.querySelector('.model-option[data-model=\"cloud-chat\"]')"),
            ("click", ".model-option[data-model=\"cloud-chat\"]"),
            ("wait", "document.querySelector('#model-chip').textContent.includes('cloud-chat')"),
            attach(scan_file()), ("wait", "document.querySelector('#composer .att-chip')"),
            ("type", "#chat-input", "О чём документ?"), ("press", "Enter"),
            ("wait", "document.querySelector('#chat-error').textContent"),
            ("type", "#chat-input", " Кратко."),
            ("eval", "edited", "[document.querySelector('#chat-error').textContent,"
                               " document.querySelector('#chat-input').value]"),
            ("eval", "seen", MARK % "скан правят"),
            ("eval", "quiet", quiet),
            ("press", "Enter"),
            ("wait", f"{ANSWERED} && {LAST_ANSWER}.querySelector('.md').textContent.trim() === 'Облако на связи.'")])
    finally:
        live.gw.ocr_hold = False
    assert not r.get("failed"), r
    v = r["values"]
    assert v["edited"] == ["", "О чём документ? Кратко."]
    assert v["quiet"] == [0, ""]                                       # скан готов, а ход сам не ушёл
    assert len(live.cloud.chat_requests) == sent + 1
    content = live.cloud.chat_requests[-1]["messages"][-1]["content"]
    assert "Текст страницы." in content and content.endswith("О чём документ? Кратко.")
    assert not known_refusals(problems(r), 409), problems(r)


def test_chat_retry_waits_for_a_scan_from_the_history(browser, live):
    """Ход через сервер остановили посреди распознавания (скан ещё отменяется), выбрали облако и перечитали
    диалог: у вложения истории — не «0 зн.». «Ответить заново» — 409 `not_ready`: страница опрашивает этот
    документ (только состояние), а не шлёт ход раз в 3 с; набор следующего вопроса ожидание повтора не
    снимает; скан кончился — повтор уходит сам, один раз, поле остаётся."""
    import state
    live.add_user("scanretry")
    live.gw.ocr_hold = lambda request: state.DB.execute(
        "SELECT 1 FROM chats WHERE title = 'скан повтор'").fetchone() is None
    sent = len(live.cloud.chat_requests)
    init = ("window.calls = []; const fetch0 = window.fetch; window.fetch = function (url, opts) {"
            " window.calls.push(`${(opts && opts.method) || 'GET'} ${url}`); return fetch0.apply(this, arguments); };")
    quiet = ("(async () => { const from = window.calls.length; await new Promise(r => setTimeout(r, 3500));"
             " return window.calls.slice(from); })()")
    # страница ушла в шлюз: остановленный раньше документ отменился бы сразу, не дожидаясь шлюза
    sent_page = (f"(async () => {{ for (;;) {{ const {{ items }} = await {API}.api('GET', '/api/ocr');"
                 " if (items[0] && items[0].detail.startsWith('распознаю')) return true;"
                 " await new Promise(r => setTimeout(r, 50)); } })()")
    try:
        r = browser.drive(live.url + "/", [*signin("scanretry"), ("wait", READY), add_gateway(), add_cloud(live.cloud),
            *go("settings"), ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
            attach(scan_file()), ("wait", "document.querySelector('#composer .att-chip')"),
            ("type", "#chat-input", "О чём документ?"), ("press", "Enter"),
            ("wait", f"{SCAN_BADGE}?.includes('Распознаю скан')"), ("eval", "page", sent_page),
            ("click", "#stop"), ("wait", ANSWERED),
            ("click", "#model-chip"), ("wait", "document.querySelector('.model-option[data-model=\"cloud-chat\"]')"),
            ("click", ".model-option[data-model=\"cloud-chat\"]"),
            ("wait", "document.querySelector('#model-chip').textContent.includes('cloud-chat')"),
            ("eval", "reload", "location.reload()"),
            ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY} && {ANSWERED}"),
            ("eval", "label", "document.querySelector('#messages .msg.user .att').textContent"),
            ("click", "#messages .msg.assistant:last-child .msg-retry"),
            ("wait", "document.querySelector('#chat-error').textContent"),
            ("type", "#chat-input", "Следующий вопрос"),
            ("eval", "typed", "document.querySelector('#chat-error').textContent"),
            ("eval", "quiet", quiet),
            ("eval", "seen", MARK % "скан повтор"),
            ("wait", f"{ANSWERED} && {LAST_ANSWER}.querySelector('.md').textContent.trim() === 'Облако на связи.'"),
            ("eval", "field", "document.querySelector('#chat-input').value")], init=init)
    finally:
        live.gw.ocr_hold = False
    assert not r.get("failed"), r
    v = r["values"]
    assert "скан.pdf" in v["label"] and "зн." not in v["label"]
    assert v["typed"] == "скан ещё распознаётся — отправлю, когда будет готов"
    assert not [c for c in v["quiet"] if c.startswith("POST") and c.endswith("/turns")], v["quiet"]
    assert [c for c in v["quiet"] if c.startswith("GET /api/ocr/") and c.endswith("?brief=1")], v["quiet"]
    assert v["field"] == "Следующий вопрос"
    assert len(live.cloud.chat_requests) == sent + 1
    content = live.cloud.chat_requests[-1]["messages"][-1]["content"]
    assert content.startswith('<document name="скан.pdf">') and content.endswith("О чём документ?")
    assert not known_refusals(problems(r), 409), problems(r)


def test_voice_is_added_in_settings(browser, live):
    live.add_user("voicer")
    dialog = "#voice-dialog"
    r = browser.drive(live.url + "/", [*signin("voicer"), ("wait", READY), add_gateway(),
        *go("settings/speech"), ("wait", "document.querySelector('#voice-add')"),
        ("click", "#voice-add"), ("wait", is_open(dialog)),
        ("type", f"{dialog} [name=name]", "Мой голос"),
        ("type", f"{dialog} [name=text]", "Привет, это образец моего голоса."),
        put_audio(f"{dialog} input[type=file]", wav_of(6.0), "sample.wav"),
        ("click", "#voice-save"), ("wait", f"!{is_open(dialog)} && document.querySelector('.voice')"),
        ("eval", "voice", "document.querySelector('.voice').textContent"),
        ("click", ".voice .voice-delete"), ("wait", "!document.querySelector('.voice')")])
    assert not r.get("failed"), r
    assert "Мой голос" in r["values"]["voice"] and "0:06" in r["values"]["voice"]
    assert not problems(r), problems(r)


# ---- озвучка --------------------------------------------------------------------

def add_voice(seconds=6.0):
    b64 = base64.b64encode(wav_of(seconds)).decode()
    return ("eval", "voice", f"""(async () => {{ const m = {API};
      const bytes = Uint8Array.from(atob('{b64}'), c => c.charCodeAt(0));
      const body = new FormData(); body.append('name', 'Мой голос'); body.append('text', 'текст образца');
      body.append('audio', new File([bytes], 'sample.wav', {{ type: 'audio/wav' }}));
      return (await m.api('POST', '/api/voices', body)).id; }})()""")


@pytest.mark.parametrize("width", [390, 1280])
def test_speech_speaks_with_a_voice_and_plays(browser, live, width):
    login = f"sp{width}"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(),
        *go("settings"), ("wait", "document.querySelector('.settings-nav')"),
        *go("speech"), ("wait", "document.querySelector('.sp-voice-hint') && !document.querySelector('.sp-voice-hint').hidden"),
        ("eval", "hint", "document.querySelector('.sp-voice-hint').textContent"),
        add_voice(), *go("settings"), ("wait", "document.querySelector('.settings-nav')"),
        *go("speech"), ("wait", "document.querySelector('#chip-voice option')"),
        ("type", "#speech-text", "Привет. Как дела?"),
        ("eval", "count", "document.querySelector('#speech-count').textContent"),
        ("click", "#speech-go"),
        ("wait", "document.querySelector('.sp-item[data-state=done]') && document.querySelector('.sp-view audio.sp-audio')"),
        ("eval", "audio", "document.querySelector('.sp-view audio.sp-audio').getAttribute('src')"),
        ("eval", "text", "document.querySelector('.sp-view .sp-text').textContent"),
        ("eval", "spy", CLICKS), ("click", "#sp-download"), ("wait", "window.clicked.length === 1"),
        ("eval", "clicked", "window.clicked[0]"),
        ("eval", "overflow", f"!({NO_OVERFLOW})")], width=width, mobile=width == 390)
    assert not r.get("failed"), r
    v = r["values"]
    assert "добавьте голос" in v["hint"] and v["count"].startswith("17 / 2000")
    assert v["audio"].startswith("/api/media/") and v["text"] == "Привет. Как дела?"
    assert v["clicked"][0].endswith("?download=1") and v["clicked"][1] and not v["overflow"]
    assert not problems(r), problems(r)


# ---- списки расшифровки и озвучки: страницы и выбор --------------------------------

#: раздел → (приставка id кнопок, таблица, строка: id, user_id, имя или текст, состояние, created)
LISTS = {
    "transcribe": ("tr", "transcripts", "INSERT INTO transcripts(id, user_id, model, name, source_media_id, state,"
                                        " detail, created) VALUES (?, ?, 'whisperx', ?, 'm', ?, 'ошибка', ?)"),
    "speech": ("sp", "speeches", "INSERT INTO speeches(id, user_id, model, voice, text, state, detail, created)"
                                 " VALUES (?, ?, 'qwen3-tts', 'v', ?, ?, 'ошибка', ?)"),
}


def seed_list(live, section, uid, n, running_newest=False) -> list:
    """n записей раздела ошибкой (без файлов), новые — с большим номером; у шести старейших одно
    время — на краю первой страницы (60) при n = 62. Новейшая идущая — список опрашивается."""
    import db
    sql, base, ids = LISTS[section][2], time.time() - 3600, []
    conn = db.connect(live.db_path)
    try:
        for i in range(n):
            row_id = f"{uid:08x}{i:024x}"
            state = "working" if running_newest and i == n - 1 else "error"
            conn.execute(sql, (row_id, uid, f"Запись {i}", state, base + max(i, 5)))
            ids.append(row_id)
    finally:
        conn.close()
    return ids


#: страница после курсора идёт 3.5 с (опрос первой страницы — раз в 3 с — успевает пройти)
SLOW_PAGE = """(() => { window.lists = { first: 0, paged: 0 }; const real = window.fetch;
  window.fetch = async (url, opts = {}) => {
    const u = String(url);
    if (/^\\/api\\/(transcripts|speeches)\\?/.test(u)) {
      if (!u.includes('before_id=')) window.lists.first += 1;
      else { await new Promise(r => setTimeout(r, 3500)); window.lists.paged += 1; }
    }
    return real(url, opts); }; return true; })()"""
ITEMS = "[...document.querySelectorAll('.tr-list .tr-item')]"


@pytest.mark.parametrize("section", LISTS)
def test_list_pages_past_sixty_through_a_poll_and_a_delete(browser, live, section):
    """62 записи: первая страница — 60 и «Показать ещё», затем две записи того же времени, что край
    страницы; опрос первой страницы во время подгрузки её не теряет; удаление последней записи
    первой страницы не возвращает «Показать ещё» и не двоит записи."""
    p = LISTS[section][0]
    login = f"pages{section}"
    ids = seed_list(live, section, live.add_user(login), 62, running_newest=True)
    edge = ids[2]                                        # последняя первой страницы
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
        ("wait", "document.querySelector('.settings-nav')"), *go(section),
        ("wait", f"{ITEMS}.length === 60 && document.querySelector('#{p}-more')"),
        ("eval", "spy", SLOW_PAGE), ("eval", "first0", "window.lists.first"),
        ("click", f"#{p}-more"),
        ("wait", f"{ITEMS}.length === 62 && !document.querySelector('#{p}-more')"),
        ("eval", "lists", "window.lists"),
        ("eval", "paged", f"{ITEMS}.map(e => e.dataset.id)"),
        ("click", f".tr-item[data-id='{edge}']"), ("wait", f"document.querySelector('#{p}-delete')"),
        ("click", f"#{p}-delete"), ("wait", is_open(f"#{p}-delete-dialog")), ("click", f"#{p}-delete-confirm"),
        ("wait", f"{ITEMS}.length === 61"),
        ("eval", "left", f"{ITEMS}.map(e => e.dataset.id)"),
        ("eval", "more", f"!!document.querySelector('#{p}-more')")], timeout=20000)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["lists"]["paged"] == 1 and v["lists"]["first"] > v["first0"]
    assert v["paged"] == ids[::-1]
    assert v["left"] == [i for i in ids[::-1] if i != edge] and not v["more"]
    assert not known_refusals(problems(r), 404), problems(r)      # плеер записи без файла (строки — без media)


#: чтение записи `id` идёт 1.5 с; window.late — сколько таких ответов уже пришло
SLOW_READ = """((id) => { window.late = 0; const real = window.fetch;
  window.fetch = async (url, opts = {}) => {
    if ((opts.method || 'GET') !== 'GET' || !String(url).endsWith('/' + id)) return real(url, opts);
    await new Promise(r => setTimeout(r, 1500));
    try { return await real(url, opts); } finally { setTimeout(() => { window.late += 1; }, 300); }
  }; return true; })"""


@pytest.mark.parametrize("section", LISTS)
def test_late_answer_of_an_earlier_pick_does_not_take_the_view(browser, live, section):
    """Выбор A (ответ запаздывает), затем B: остаётся B. Выбор A во время удаления B: запоздалый A
    пустой вид после удаления не занимает."""
    p = LISTS[section][0]
    login = f"pick{section}"
    a, b = seed_list(live, section, live.add_user(login), 2)
    shown = ("document.querySelector('.split-main h1').textContent" if section == "transcribe"
             else "document.querySelector('.sp-text')?.textContent ?? ''")
    current = "document.querySelector('.tr-item[aria-current=true]')?.dataset.id ?? null"
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
        ("wait", "document.querySelector('.settings-nav')"), *go(section),
        ("wait", f"{ITEMS}.length === 2"), ("eval", "spy", f"{SLOW_READ}({a!r})"),
        ("click", f".tr-item[data-id='{a}']"), ("click", f".tr-item[data-id='{b}']"),
        ("wait", "window.late === 1"),
        ("eval", "picked", f"[{shown}, {current}]"),
        ("click", f".tr-item[data-id='{a}']"), ("click", f"#{p}-delete"),
        ("wait", is_open(f"#{p}-delete-dialog")), ("click", f"#{p}-delete-confirm"),
        ("wait", f"{ITEMS}.length === 1 && window.late === 2"),
        ("eval", "after", f"[!!document.querySelector('.tr-empty'), {current}]")], timeout=20000)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["picked"] == ["Запись 1", b]
    assert v["after"] == [True, None]
    assert not known_refusals(problems(r), 404), problems(r)      # плеер записи без файла (строки — без media)


# ---- история без модели раздела ------------------------------------------------------

def made_history_then_no_connection():
    """Через API: диалог, картинка, ролик из неё, расшифровка, озвучка; затем подключение удалено —
    ни у одного раздела нет модели. window.made — id диалога и готовность разделов."""
    b64, sample64 = base64.b64encode(wav_of(1.0)).decode(), base64.b64encode(wav_of(6.0)).decode()
    return ("eval", "made", f"""(async () => {{ const m = {API};
      const sleep = (ms) => new Promise(r => setTimeout(r, ms));
      const until = async (path, done) => {{ for (let i = 0; i < 400; i++) {{
        const x = await m.api('GET', path); if (done(x)) return x; await sleep(50); }} throw new Error(path); }};
      const conn = (await m.api('GET', '/api/connections')).items[0].id;
      const chat = await m.api('POST', '/api/chats', {{}});
      await (await m.api('POST', '/api/chats/' + chat.id + '/turns', {{ text: 'привет' }})).text();
      const image = await until('/api/images/' + (await m.api('POST', '/api/images', {{ prompt: 'кот', size: 'square' }})).id,
        x => x.state === 'done');
      const form = new FormData(); form.append('source_media_id', image.media_id); form.append('seconds', '1');
      form.append('quality', 'fast'); form.append('smooth', 'false'); form.append('prompt', 'кот моргает');
      await until('/api/videos/' + (await m.api('POST', '/api/videos', form)).id, x => x.state === 'done');
      const bytes = Uint8Array.from(atob('{b64}'), c => c.charCodeAt(0));
      const sent = await fetch('/api/transcripts', {{ method: 'POST', body: bytes, headers: {{ 'content-type': 'audio/wav',
        'x-studio-name': encodeURIComponent('Звонок.wav'), 'x-studio-csrf': m.me.csrf }} }});
      await until('/api/transcripts/' + (await sent.json()).id, x => x.state === 'done');
      const sample = new FormData(); sample.append('name', 'Мой голос'); sample.append('text', 'текст образца');
      sample.append('audio', new File([Uint8Array.from(atob('{sample64}'), c => c.charCodeAt(0))], 'sample.wav',
        {{ type: 'audio/wav' }}));
      const voice = (await m.api('POST', '/api/voices', sample)).id;
      await until('/api/speeches/' + (await m.api('POST', '/api/speeches', {{ text: 'Привет.', voice }})).id,
        x => x.state === 'done');
      await m.api('DELETE', '/api/connections/' + conn);
      await m.loadMe();
      window.made = {{ chat: chat.id, caps: m.me.capabilities }};
      return window.made; }})()""")


@pytest.mark.parametrize("width", [390, 1280])
def test_history_without_a_model_is_read_downloaded_and_deleted(browser, live, ready_clips, width):
    """Подключение удалено — генерации нет, а своя история осталась: прежний диалог читается, фото и ролик
    скачиваются, фото удаляется, расшифровка выгружается, озвучка скачивается и удаляется; вместо полей
    создания — подсказка со ссылкой на настройки раздела."""
    login = f"nomodel{width}"
    live.add_user(login)
    mobile = width == 390
    lists = [("click", "#view .split-toggle")] if mobile else []
    hint = "document.querySelector('#view .hint-bar a')?.getAttribute('href') ?? null"
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), made_history_then_no_connection(),
        ("eval", "spy", CLICKS), *go("settings"), ("wait", "document.querySelector('.settings-nav')"),
        # чат: новый диалог — подсказка без поля ввода, прежний — читается
        *go("chat"), ("wait", "document.querySelector('#messages .hint')"),
        ("eval", "chatNew", "[document.querySelector('#composer').hidden,"
                            " document.querySelector('#messages .hint a').getAttribute('href'),"
                            " document.querySelector('#model-chip').textContent.trim(),"
                            " document.querySelector('#model-chip').classList.contains('off')]"),
        ("eval", "open", "location.hash = '#/chat/' + window.made.chat"),
        ("wait", "document.querySelectorAll('#messages .msg').length === 2"),
        ("eval", "chatOld", "[document.querySelector('#composer').hidden, [...document.querySelectorAll('#messages .msg')]"
                            ".map(m => m.querySelector('.bubble, .md').textContent.trim())]"),
        # видео: ролик в ленте, «Скачать»
        *go("video"), ("wait", f"{VIDEO_FEED_READY} && {clips('done', 1)}"),
        ("eval", "video", f"[!!document.querySelector('#video-composer'), {hint}]"),
        ("click", f"{CLIP} .card-download"), ("wait", "window.clicked.length === 1"),
        # фото: лента, просмотр без «Изменить» и «Повторить», «Скачать», «Удалить»
        *go("photo"), ("wait", f"{FEED_READY} && document.querySelector('{NEWEST_DONE}')"),
        ("eval", "photo", f"[!!document.querySelector('#photo-composer'), {hint}]"),
        ("eval", "overflow", f"!({NO_OVERFLOW})"),
        ("click", NEWEST_DONE), ("wait", is_open("#viewer")),
        ("eval", "acts", "[...document.querySelectorAll('#viewer .viewer-actions button')].map(b => b.textContent)"),
        ("click", "#viewer .act-download"), ("wait", "window.clicked.length === 2"),
        ("click", "#viewer .act-delete"), ("wait", is_open("#photo-delete-dialog")), ("click", "#photo-delete-confirm"),
        ("wait", "document.querySelector('#photo-feed .feed-empty')"),
        # расшифровка: без «Загрузить запись», выгрузка прежней
        *go("transcribe"), ("wait", "document.querySelector('.tr-item[data-state=done]')"),
        ("eval", "transcribe", f"[!!document.querySelector('#transcribe-add'), {hint},"
                               " document.querySelector('.tr-empty').textContent]"),
        *lists, ("click", ".tr-item[data-state=done]"), ("wait", "document.querySelector('#tr-download')"),
        ("click", "#tr-download"), ("click", ".tr-formats [data-format=txt]"), ("wait", "window.clicked.length === 3"),
        ("eval", "export", "fetch(window.clicked[2][0]).then(async r => [r.status, (await r.text()).length > 0])"),
        # озвучка: без поля текста, «Скачать mp3», «Удалить»
        *go("speech"), ("wait", "document.querySelector('.sp-item[data-state=done]')"),
        ("eval", "speech", f"[!!document.querySelector('#speech-composer'), {hint}]"),
        *lists, ("click", ".sp-item[data-state=done]"), ("wait", "document.querySelector('#sp-download')"),
        ("click", "#sp-download"), ("wait", "window.clicked.length === 4"),
        ("click", "#sp-delete"), ("wait", is_open("#sp-delete-dialog")), ("click", "#sp-delete-confirm"),
        ("wait", "!document.querySelector('.sp-item')"), ("eval", "clicked", "window.clicked.map(c => c[0])")],
        width=width, mobile=mobile, timeout=20000)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["made"]["caps"] == {s: False for s in v["made"]["caps"]}
    assert v["chatNew"] == [True, "#/settings/chat", "модель не выбрана", True]
    assert v["chatOld"] == [False, ["привет", "Здравствуйте!"]]
    assert v["video"] == [False, "#/settings/video"] and v["photo"] == [False, "#/settings/photo"] and not v["overflow"]
    assert "Изменить" not in v["acts"] and "Повторить с тем же seed" not in v["acts"]
    assert {"Скачать", "Удалить", "Оживить"} <= set(v["acts"])
    assert v["transcribe"] == [False, "#/settings/transcribe", "Выберите запись в списке."]
    assert v["export"] == [200, True] and v["speech"] == [False, "#/settings/speech"]
    assert [re.sub(r"[0-9a-f]{32}", "<id>", c) for c in v["clicked"]] == [
        "/api/media/<id>?download=1", "/api/media/<id>?download=1", "/api/transcripts/<id>/export?format=txt",
        "/api/media/<id>?download=1"]
    assert not problems(r), problems(r)


# ---- блокировка на телефоне (спека 2026-10-21-studio-screen-lock) -----------------

PHONE = {"width": 390, "mobile": True, "touch": True, "authenticator": True}
KEY_NAMES = ("iPhone", "iPad", "Android", "Устройство")


def server_lock_is(value):
    """Ждать, пока сервер не скажет, что блокировка учётки включена (True) или выключена (False)."""
    return ("wait", f"fetch('/api/me').then(r => r.json()).then(m => m.screen_lock === {str(value).lower()})")


def lock_settings(login):
    return [*signin(login), ("wait", READY), *go("settings/other"), ("wait", "document.querySelector('#lock-enabled')")]


def test_touch_emulation_gives_a_coarse_pointer(browser, live):
    coarse = [("idle",), ("eval", "coarse", "matchMedia('(pointer: coarse)').matches")]
    phone = browser.drive(live.url + "/login", coarse, width=390, mobile=True, touch=True)
    computer = browser.drive(live.url + "/login", coarse)
    assert not phone.get("failed") and not computer.get("failed"), (phone, computer)
    assert phone["values"]["coarse"] is True and computer["values"]["coarse"] is False


def test_lock_settings_add_list_and_remove_a_key(browser, live):
    login = "lockset"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*lock_settings(login),
        ("eval", "before", "document.querySelector('#lock-enabled').checked"),
        ("click", "#lock-enabled"), server_lock_is(True),
        ("type", "#passkey-password", "password-123"), ("click", "#passkey-next"),
        ("wait", "document.querySelector('#passkey-add') && !document.querySelector('#passkey-add').hidden"),
        ("click", "#passkey-add"), ("wait", "document.querySelectorAll('#passkeys .passkey').length === 1"),
        ("eval", "row", "document.querySelector('#passkeys .passkey').textContent"),
        ("click", "#passkeys .passkey-delete"), ("wait", "document.querySelectorAll('#passkeys .passkey').length === 0"),
        ("click", "#lock-enabled"), ("wait", "document.querySelector('#lock-off').getClientRects().length > 0"),
        ("type", "#lock-off-password", "password-123"), ("click", "#lock-off"), server_lock_is(False),
        ("eval", "after", "document.querySelector('#lock-enabled').checked")], **PHONE)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["before"] is False and v["after"] is False
    assert any(name in v["row"] for name in KEY_NAMES) and "ещё не открывал" in v["row"], v["row"]
    assert not problems(r), problems(r)


def test_add_key_is_absent_on_a_computer(browser, live):
    login = "lockpc"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*lock_settings(login),
        ("eval", "found", "['#lock-enabled', '#passkeys', '#passkey-add', '#passkey-password']"
                          ".map(s => !!document.querySelector(s))")], width=1280)
    assert not r.get("failed"), r
    assert r["values"]["found"] == [True, True, False, False]
    assert not problems(r), problems(r)


def server_pin_is(value):
    """Ждать, пока сервер не скажет, что PIN учётки задан (True) или нет (False)."""
    return ("wait", f"fetch('/api/me').then(r => r.json()).then(m => m.pin === {str(value).lower()})")


PIN_BLOCK = """['#pin-state', '#pin-set', '#pin-remove', '#pin-form'].map(s => document.querySelector(s))
  .map(e => e.id === 'pin-remove' || e.id === 'pin-form' ? e.hidden : e.textContent)"""
PIN_FIELDS = "['#pin-password', '#pin-new', '#pin-repeat'].map(s => document.querySelector(s).value)"


def test_lock_settings_set_change_and_remove_the_pin(browser, live):
    """PIN задаётся с компьютера: пароль учётки, PIN и повтор; не совпал — запроса нет; убрать — без пароля."""
    login = "pinset"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*lock_settings(login), ("eval", "before", PIN_BLOCK),
        ("click", "#pin-set"), ("type", "#pin-password", "password-123"), ("type", "#pin-new", "1234"),
        ("type", "#pin-repeat", "1243"), ("click", "#pin-save"),
        ("eval", "mismatch", "document.querySelector('#screen-lock-status').textContent"),
        ("eval", "unset", "fetch('/api/me').then(r => r.json()).then(m => m.pin)"),
        set_value("#pin-repeat", "1234"), ("click", "#pin-save"), server_pin_is(True),
        ("wait", "document.querySelector('#pin-state').textContent === 'PIN задан'"),
        ("eval", "set", PIN_BLOCK), ("eval", "fields", PIN_FIELDS),
        ("click", "#pin-set"), ("eval", "again", PIN_BLOCK),
        ("click", "#pin-remove"), server_pin_is(False),
        ("wait", "document.querySelector('#pin-state').textContent === 'PIN: не задан'"),
        ("eval", "removed", PIN_BLOCK), ("eval", "overflow", f"!({NO_OVERFLOW})")], width=1280)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["before"] == ["PIN: не задан", "Задать PIN", True, True]
    assert v["mismatch"] == "PIN и повтор не совпадают" and v["unset"] is False
    assert v["set"] == ["PIN задан", "Сменить PIN", False, True] and v["fields"] == ["", "", ""]
    assert v["again"] == ["PIN задан", "Сменить PIN", False, False]
    assert v["removed"] == ["PIN: не задан", "Задать PIN", True, True] and not v["overflow"]
    assert r["sent"].count("PUT /api/me/pin") == 1, r["sent"]
    assert not problems(r), problems(r)

    phone = browser.drive(live.url + "/", [("wait", READY), *go("settings/other"),     # вход — из первой вкладки
        ("wait", "document.querySelector('#pin-set')"), ("click", "#pin-set"),
        ("eval", "hint", "document.querySelector('#pin-form').textContent"),
        ("eval", "overflow", f"!({NO_OVERFLOW})")], width=320, mobile=True, touch=True)
    assert not phone.get("failed"), phone
    assert "От 4 до 8 цифр. Не совпадающий с кодом телефона" in phone["values"]["hint"]
    assert not phone["values"]["overflow"]
    assert not problems(phone), problems(phone)


def test_settings_pin_fields_are_not_passwords(browser, live):
    """«Блокировка» → PIN: пароль учётки — прежний password с current-password, а поля PIN — не password (два поля
    password рядом с паролем менеджер паролей принял бы за его смену и предложил сохранить PIN): text с точками
    (-webkit-text-security), one-time-code, цифровая клавиатура, до 8 знаков; вне <form>. PIN задаётся."""
    login = "pin-text"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*lock_settings(login), ("click", "#pin-set"),
        ("eval", "password", "(e => [e.type, e.autocomplete])(document.querySelector('#pin-password'))"),
        ("eval", "keys", "['#pin-new', '#pin-repeat'].map(s => (e => [e.type, getComputedStyle(e).webkitTextSecurity,"
                         " e.getAttribute('autocomplete'), e.inputMode, e.maxLength, !!e.closest('form')])"
                         "(document.querySelector(s)))"),
        ("type", "#pin-password", "password-123"), ("type", "#pin-new", "2468"), ("type", "#pin-repeat", "2468"),
        ("click", "#pin-save"), server_pin_is(True)], width=320, mobile=True, touch=True)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["password"] == ["password", "current-password"]
    assert v["keys"] == [["text", "disc", "one-time-code", "numeric", 8, False]] * 2
    assert not problems(r), problems(r)


LOCK_OPEN = "!!document.querySelector('#lock')?.open"
CURTAIN_OPEN = "!!document.querySelector('#curtain')?.open"
ME_STATUS = "fetch('/api/me').then(r => r.status)"
PASSKEY_READY = "document.querySelector('#lock-open').dataset.ready === '1'"


def visibility_js(state):
    """Выражение: студия ушла с экрана (hidden) или вернулась (visible) — подмена document и событие."""
    return f"""(() => {{
      Object.defineProperty(document, 'visibilityState', {{ configurable: true, get: () => '{state}' }});
      Object.defineProperty(document, 'hidden', {{ configurable: true, get: () => {str(state == 'hidden').lower()} }});
      document.dispatchEvent(new Event('visibilitychange')); return true; }})()"""


def visibility(state):
    return ("eval", state, visibility_js(state))


def hide():
    return visibility("hidden")


def show():
    return visibility("visible")


def later(ms):
    """Часы страницы вперёд на ms (Date.now со сдвигом): минуту не ждать."""
    return ("eval", "later", f"""(() => {{
      if (!window.realNow) {{ window.realNow = Date.now.bind(Date); window.shift = 0; }}
      window.shift += {ms}; Date.now = () => window.realNow() + window.shift; return window.shift; }})()""")


def settled(expression, ms=300):
    """Значение выражения спустя ms: окно, которое закрылось бы, успевает закрыться."""
    return f"new Promise(r => setTimeout(() => r({expression}), {ms}))"


def phone_with_key(login):
    """Задача 4 до конца: блокировка включена, ключ этого устройства добавлен."""
    return [*lock_settings(login), ("click", "#lock-enabled"), server_lock_is(True),
            ("type", "#passkey-password", "password-123"), ("click", "#passkey-next"),
            ("wait", "document.querySelector('#passkey-add') && !document.querySelector('#passkey-add').hidden"),
            ("click", "#passkey-add"), ("wait", "document.querySelectorAll('#passkeys .passkey').length === 1")]


def locked_phone(login):
    """Ключ, уход с экрана дольше минуты, возвращение: экран блокировки, сервер отказывает."""
    return [*phone_with_key(login), hide(), ("wait", CURTAIN_OPEN), later(61000), show(),
            ("wait", LOCK_OPEN), ("wait", f"{ME_STATUS}.then(s => s === 423)")]


def test_phone_locks_a_minute_after_leaving_and_opens_with_the_passkey(browser, live):
    login = "lk-pass"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*phone_with_key(login), add_gateway(), *go("photo"),
        ("wait", "document.querySelector('#photo-prompt')"), ("type", "#photo-prompt", "кот на крыше"),
        hide(), ("wait", CURTAIN_OPEN), later(61000), show(), ("wait", LOCK_OPEN),
        ("wait", f"{ME_STATUS}.then(s => s === 423)"),
        ("eval", "title", "document.querySelector('#lock-title').textContent"),
        ("press", "Escape"), ("eval", "afterEscape", settled(LOCK_OPEN)),
        ("wait", PASSKEY_READY), ("click", "#lock-open"), ("wait", f"!{LOCK_OPEN} && !{CURTAIN_OPEN}"),
        ("eval", "prompt", "document.querySelector('#photo-prompt').value"),
        ("eval", "after", ME_STATUS)], **PHONE)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["title"] == "Студия заперта" and v["afterEscape"] is True
    assert v["prompt"] == "кот на крыше" and v["after"] == 200
    assert not known_refusals(problems(r), 423), problems(r)


def test_phone_back_within_a_minute_is_not_locked(browser, live):
    login = "lk-soon"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*phone_with_key(login), hide(), ("wait", CURTAIN_OPEN), later(30000), show(),
        ("wait", f"!{CURTAIN_OPEN}"), ("eval", "lock", settled(LOCK_OPEN)), ("eval", "me", ME_STATUS)], **PHONE)
    assert not r.get("failed"), r
    assert r["values"]["lock"] is False and r["values"]["me"] == 200
    assert not problems(r), problems(r)


def test_computer_locks_after_the_configured_idle_time(browser, live):
    login = "lk-pc"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("eval", "on", f"(async () => {{ const api = {API}; const answer = await api.api('POST', '/api/me/screen-lock', {{ enabled: true }});"
                        " await api.loadMe(); document.dispatchEvent(new Event('studio-me')); return answer.screen_lock; })()"),
        hide(), ("eval", "covered", settled(CURTAIN_OPEN)), later(3_600_000), show(),
        ("wait", LOCK_OPEN), ("eval", "me", ME_STATUS),
        ("type", "#lock-password", "password-123"), ("click", "#lock-password-go"), ("wait", f"!{LOCK_OPEN}"),
        ("eval", "opened", ME_STATUS)], width=1280)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["on"] is True and v["covered"] is True and v["me"] == 423 and v["opened"] == 200
    assert not known_refusals(problems(r), 423), problems(r)


@pytest.mark.parametrize("width", [320, 1280])
def test_pin_settings_save_sections_and_both_timers(browser, live, width, tmp_path):
    login = f"pin-policy-{width}"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*lock_settings(login),
        ("click", "#lock-scope-sections"), ("click", "#lock-section-photo"), ("click", "#lock-section-code"),
        set_value("#lock-timeout", "10"), ("click", "#lock-enabled"), server_lock_is(True),
        ("click", "#lock-section-chat"), ("type", "#lock-policy-password", "password-123"),
        ("eval", "refresh", f"(async () => {{ await {API}.loadMe(); document.dispatchEvent(new Event('studio-me')); return true; }})()"),
        ("eval", "passwordKept", "document.querySelector('#lock-policy-password').value === 'password-123'"),
        ("click", "#lock-save"),
        ("wait", "document.querySelector('#screen-lock-status').textContent.includes('сохранены')"),
        ("eval", "reload", "location.reload()"), ("wait", READY), ("wait", "document.querySelector('#lock-section-chat')"),
        ("eval", "policy", "fetch('/api/me').then(r => r.json()).then(m => [m.screen_lock_sections, m.screen_lock_timeout])"),
        ("eval", "checked", "['chat','photo','code'].map(s => document.querySelector('#lock-section-' + s).checked)"),
        ("eval", "timer", "document.querySelector('#lock-timeout').value"),
        ("eval", "fit", "document.documentElement.scrollWidth <= innerWidth"),
        ("eval", "scroll", "document.querySelector('.lock-block').scrollIntoView({block:'start'})"),
        ("screenshot", str(tmp_path / f"pin-settings-{width}.png")), *go("settings/code"),
        ("wait", "document.querySelector('#computer-pin-timeout')"), set_value("#computer-pin-timeout", "2"),
        ("click", "#computer-pin-save"), ("wait", "document.querySelector('#computer-pin-status').textContent.includes('сохранён')"),
        ("eval", "computer", "fetch('/api/me').then(r => r.json()).then(m => m.computer_pin_timeout)"),
        ("eval", "scroll", "document.querySelector('#computer-pin-timeout').closest('section').scrollIntoView({block:'start'})"),
        ("screenshot", str(tmp_path / f"computer-pin-{width}.png"))],
        width=width, mobile=width < 760)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["policy"] == [["chat", "photo", "code"], 600] and v["checked"] == [True, True, True]
    assert v["timer"] == "10" and v["computer"] == 7200 and v["fit"] is True and v["passwordKept"] is True
    assert not problems(r), problems(r)


def test_partial_pin_keeps_other_sections_available_and_guards_direct_return(browser, live):
    login = "pin-scope-route"
    live.add_user(login)
    configure = f"""(async () => {{ const m = {API};
      await m.api('PUT', '/api/me/pin', {{password:'password-123', pin:'2580'}});
      await m.api('POST', '/api/me/screen-lock', {{enabled:true, sections:['photo','code'], timeout:60}});
      await m.loadMe(); document.dispatchEvent(new Event('studio-me')); return true; }})()"""
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("eval", "configure", configure),
        *go("photo"), ("wait", "document.querySelector('#rail [data-section=photo][aria-current=page]')"),
        hide(), later(61000), show(), ("wait", LOCK_OPEN),
        ("eval", "hidden", "getComputedStyle(document.querySelector('#view')).visibility"),
        ("click", "#lock-elsewhere"), ("wait", "location.hash === '#/chat' && !document.documentElement.classList.contains('shrouded')"),
        ("wait", "document.querySelector('#chat-input')"),
        ("eval", "available", "fetch('/api/me').then(r => r.status)"),
        ("eval", "direct", "location.hash = '#/photo'"), ("wait", LOCK_OPEN), ("wait", PIN_SHOWN),
        ("eval", "reload", "location.reload()"), ("wait", LOCK_OPEN), ("wait", PIN_SHOWN),
        *tap("2580"), ("click", "#lock-pin-ok"), ("wait", f"!{LOCK_OPEN} && {READY}"),
        ("wait", "document.querySelector('#rail [data-section=photo][aria-current=page]')"),
        ("eval", "scope", "fetch('/api/lock?section=photo').then(r => r.json()).then(s => s.locked)")])
    assert not r.get("failed"), r
    assert r["values"]["hidden"] == "hidden" and r["values"]["available"] == 200 and r["values"]["scope"] is False
    assert not known_refusals(problems(r), 423), problems(r)


def test_partial_pin_first_navigation_after_idle_does_not_renew_access(browser, live):
    login = "pin-first-nav"
    live.add_user(login)
    configure = f"""(async () => {{ const m = {API};
      await m.api('POST', '/api/me/screen-lock', {{enabled:true, sections:['photo'], timeout:60}});
      await m.loadMe(); document.dispatchEvent(new Event('studio-me')); return true; }})()"""
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("eval", "configure", configure),
        later(61000), ("click", "#rail [data-section=photo]"), ("wait", LOCK_OPEN),
        ("eval", "locked", "fetch('/api/lock?section=photo').then(r => r.json()).then(s => s.locked)"),
        ("click", "#lock-elsewhere"), ("wait", "location.hash === '#/chat' && !document.documentElement.classList.contains('shrouded')"),
        ("click", "#rail [data-section=photo]"), ("wait", LOCK_OPEN),
        ("eval", "hidden", "getComputedStyle(document.querySelector('#view')).visibility"),
        ("type", "#lock-password", "password-123"), ("click", "#lock-password-go"), ("wait", f"!{LOCK_OPEN}")])
    assert not r.get("failed"), r
    assert r["values"]["locked"] is True and r["values"]["hidden"] == "hidden"
    assert not known_refusals(problems(r), 423), problems(r)


@pytest.mark.parametrize("timeout", [0, 1])
def test_pin_timer_locks_on_return_or_while_visible(browser, live, timeout):
    login = f"pin-timer-{timeout}"
    live.add_user(login)
    configure = f"""(async () => {{ const m = {API};
      await m.api('POST', '/api/me/screen-lock', {{enabled:true, timeout:{timeout}}});
      await m.loadMe(); document.dispatchEvent(new Event('studio-me')); return true; }})()"""
    steps = [hide(), ("wait", LOCK_OPEN), show()] if timeout == 0 else []
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("eval", "configure", configure), *steps,
        ("wait", LOCK_OPEN), ("type", "#lock-password", "password-123"), ("click", "#lock-password-go"),
        ("wait", f"!{LOCK_OPEN}"), ("eval", "opened", ME_STATUS)])
    assert not r.get("failed"), r
    assert r["values"]["opened"] == 200
    assert not known_refusals(problems(r), 423), problems(r)


def test_password_opens_and_a_reload_stays_locked(browser, live):
    login = "lk-reload"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*locked_phone(login), ("eval", "reload", "location.reload()"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {LOCK_OPEN}"),
        ("eval", "section", settled("document.querySelector('#view').children.length")),
        ("click", "#lock-use-password"), ("type", "#lock-password", "password-123"), ("click", "#lock-password-go"),
        ("wait", f"!{LOCK_OPEN} && {READY}"),
        ("eval", "after", "document.querySelector('#view').children.length > 0"), ("eval", "me", ME_STATUS)], **PHONE)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["section"] == 0 and v["after"] is True and v["me"] == 200
    assert not known_refusals(problems(r), 423), problems(r)


def test_curtain_covers_an_open_viewer(browser, live):
    login = "lk-viewer"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*phone_with_key(login), add_gateway(), make_images(1), *go("photo"),
        ("wait", FEED_READY), ("click", NEWEST_DONE), ("wait", is_open("#viewer")), hide(), ("wait", CURTAIN_OPEN),
        ("eval", "covered", "document.querySelector('#curtain').contains(document.elementFromPoint(innerWidth / 2,"
                            " innerHeight / 2))")], **PHONE)
    assert not r.get("failed"), r
    assert r["values"]["covered"] is True
    assert not problems(r), problems(r)


def test_requests_during_the_lock_wait_and_are_retried(browser, live):
    login = "lk-wait"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*locked_phone(login),
        ("eval", "asked", """(() => { window.done = false;
          import('/static/js/api.js').then(m => Promise.all([m.api('GET', '/api/sections'), m.api('GET', '/api/me')]))
            .then(([sections, me]) => { window.done = !!sections.sections && !!me.login; }); return true; })()"""),
        ("eval", "waiting", settled("window.done", 500)),
        ("eval", "screens", "document.querySelectorAll('dialog.lock[open]').length"),
        ("wait", PASSKEY_READY), ("click", "#lock-open"), ("wait", "window.done === true")], **PHONE)
    assert not r.get("failed"), r
    assert r["values"]["waiting"] is False and r["values"]["screens"] == 1
    assert not known_refusals(problems(r), 423), problems(r)


def test_open_calls_the_passkey_without_waiting_for_the_network(browser, live):
    login = "lk-gesture"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*locked_phone(login), ("wait", PASSKEY_READY),
        ("eval", "spy", """(() => { window.fetches = 0; const real = window.fetch;
          window.fetch = (...args) => { window.fetches += 1; return real(...args); };
          const get = navigator.credentials.get.bind(navigator.credentials);
          navigator.credentials.get = (options) => { window.atGet = window.fetches; return get(options); };
          return true; })()"""),
        ("click", "#lock-open"), ("wait", "window.atGet !== undefined"), ("eval", "atGet", "window.atGet"),
        ("wait", f"!{LOCK_OPEN}")], **PHONE)
    assert not r.get("failed"), r
    assert r["values"]["atGet"] == 0
    assert not known_refusals(problems(r), 423), problems(r)


def test_marks_come_from_every_visible_tab(browser, live):
    """Сессия, защёлкнутая телефоном, на устройстве, где палец перестал быть основным указателем
    (сенсорный ноутбук вне режима планшета), без отметок запиралась бы каждые 75 с посреди работы."""
    login = "lk-marks"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), hide(), show(), ("idle",)], width=1280)
    assert not r.get("failed"), r
    assert "POST /api/lock/alive" in r["sent"], r["sent"]
    assert not problems(r), problems(r)


def test_leaving_while_it_opens_keeps_the_curtain(browser, live):
    """Ушла с экрана, пока шло отпирание: шторка остаётся, уход считается — через минуту снова заперто."""
    login = "lk-leave"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*locked_phone(login), ("click", "#lock-use-password"),
        ("type", "#lock-password", "password-123"),
        ("eval", "leave", f"(() => {{ document.querySelector('#lock-password-go').click(); return {visibility_js('hidden')}; }})()"),
        ("wait", f"!{LOCK_OPEN}"), ("eval", "covered", settled(CURTAIN_OPEN)),
        later(61000), show(), ("wait", LOCK_OPEN), ("wait", f"{ME_STATUS}.then(s => s === 423)")], **PHONE)
    assert not r.get("failed"), r
    assert r["values"]["covered"] is True
    assert not known_refusals(problems(r), 423), problems(r)


def test_leaving_again_during_the_mark_keeps_the_curtain(browser, live):
    """Вернулась раньше минуты и сразу ушла: ответ отметки не снимает шторку со скрытой студии."""
    login = "lk-flip"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*phone_with_key(login), hide(), ("wait", CURTAIN_OPEN), later(30000),
        ("eval", "flip", f"(() => {{ {visibility_js('visible')}; return {visibility_js('hidden')}; }})()"),
        ("eval", "covered", settled(CURTAIN_OPEN, 800))], **PHONE)
    assert not r.get("failed"), r
    assert r["values"]["covered"] is True
    assert not problems(r), problems(r)


def test_upload_chosen_while_locked_goes_after_opening(browser, live):
    """Окно выбора файла дольше минуты — уход с экрана (спека, раздел 7): выбранная запись уходит после
    отпирания, а не падает «Студия заперта»."""
    login = "lk-upload"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*phone_with_key(login), add_gateway(),
        *go("transcribe"), ("wait", "document.querySelector('#transcribe-add input')"),
        hide(), ("wait", CURTAIN_OPEN), later(61000), show(), ("wait", LOCK_OPEN),
        ("wait", f"{ME_STATUS}.then(s => s === 423)"),
        put_audio("#transcribe-add input", wav_of(1.0)), ("eval", "early", settled("document.querySelector('.tr-view .tr-turn') === null", 800)),
        ("wait", PASSKEY_READY), ("click", "#lock-open"),
        ("wait", "document.querySelectorAll('.tr-view .tr-turn').length === 2")], **PHONE)
    assert not r.get("failed"), r
    assert r["values"]["early"] is True
    assert not known_refusals(problems(r), 423), problems(r)


def test_double_tap_open_makes_one_passkey_call(browser, live):
    """Второе «Открыть», пока устройство ещё не показало Face ID, не выдаёт новый вызов вместо того, что
    подписывает устройство: иначе сервер отказал бы (403) и понадобился бы второй Face ID."""
    login = "lk-double"
    live.add_user(login)
    taps = """(() => { const get = navigator.credentials.get.bind(navigator.credentials);
      navigator.credentials.get = (o) => new Promise(r => setTimeout(r, 500)).then(() => get(o));
      const b = document.querySelector('#lock-open'); b.click(); b.click(); setTimeout(() => b.click(), 100);
      return true; })()"""
    r = browser.drive(live.url + "/", [*locked_phone(login), ("wait", PASSKEY_READY), ("eval", "taps", taps),
        ("wait", f"!{LOCK_OPEN}"), ("eval", "me", ME_STATUS)], **PHONE)
    assert not r.get("failed"), r
    assert r["values"]["me"] == 200
    assert r["sent"].count("POST /api/lock/challenge") == 1 and r["sent"].count("POST /api/lock/open") == 1, r["sent"]
    assert not known_refusals(problems(r), 423), problems(r)


#: счётчик вызовов passkey (window.challenges); slow — ответ сервера на 600 мс позже
COUNT_CHALLENGES = """((slow) => { window.challenges = 0; const real = window.fetch;
  window.fetch = (u, o) => { if (u !== '/api/lock/challenge') return real(u, o); window.challenges += 1;
    return slow ? new Promise(r => setTimeout(r, 600)).then(() => real(u, o)) : real(u, o); };
  return true; })"""
OPEN_BUTTON = "(b => [b.disabled, b.textContent, b.dataset.ready === undefined])(document.querySelector('#lock-open'))"


def test_open_prepares_once_and_only_when_the_options_are_old(browser, live):
    """Возвращение на экран готовит вход заново, только если варианты старше FRESH_SEC; пока готовит —
    «Готовлю…», кнопка выключена, и нажатия подготовку не перезапускают."""
    login = "lk-prep"
    live.add_user(login)
    double = "(() => { const b = document.querySelector('#lock-open'); b.click(); b.click(); return true; })()"
    r = browser.drive(live.url + "/", [*locked_phone(login), ("wait", PASSKEY_READY),
        ("eval", "spy", f"{COUNT_CHALLENGES}(true)"),
        hide(), show(), ("eval", "kept", settled("window.challenges")),
        later(101_000), hide(), show(), ("eval", "button", OPEN_BUTTON), ("eval", "double", double),
        ("wait", PASSKEY_READY), ("eval", "count", "window.challenges"),
        ("click", "#lock-open"), ("wait", f"!{LOCK_OPEN}")], **PHONE)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["kept"] == 0 and v["button"] == [True, "Готовлю…", True] and v["count"] == 1
    assert not known_refusals(problems(r), 423), problems(r)


def test_open_options_are_renewed_before_the_server_forgets_them(browser, live):
    """Экран блокировки открыт дольше FRESH_SEC: вход готовится заново по таймеру отметок (не по нажатию —
    Safari), и «Открыть» проходит с одного Face ID. Ждёт таймер отметок — до 15 с."""
    login = "lk-renew"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*locked_phone(login), ("wait", PASSKEY_READY),
        ("eval", "spy", f"{COUNT_CHALLENGES}(false)"), later(101_000),
        ("wait", f"window.challenges === 1 && {PASSKEY_READY}"),
        ("click", "#lock-open"), ("wait", f"!{LOCK_OPEN}")], timeout=25000, **PHONE)
    assert not r.get("failed"), r
    assert not known_refusals(problems(r), 423), problems(r)


def test_settings_old_key_options_go_back_to_the_password(browser, live):
    """Варианты создания ключа старше FRESH_SEC: «Добавить» не зовёт устройство (сервер не принял бы ключ,
    а в «Паролях» iPhone остался бы сирота), а возвращает к паролю и «Далее»."""
    login = "lk-stale"
    live.add_user(login)
    spy = """(() => { window.created = 0; const create = navigator.credentials.create.bind(navigator.credentials);
      navigator.credentials.create = (o) => { window.created += 1; return create(o); }; return true; })()"""
    shown = "document.querySelector('#passkey-add') && !document.querySelector('#passkey-add').hidden"
    r = browser.drive(live.url + "/", [*lock_settings(login), ("eval", "spy", spy),
        ("type", "#passkey-password", "password-123"), ("click", "#passkey-next"), ("wait", shown),
        later(101_000), ("click", "#passkey-add"),
        ("wait", "document.querySelector('#passkey-add').hidden && document.querySelector('#passkey-password').getClientRects().length > 0"),
        ("eval", "note", "document.querySelector('#screen-lock-status').textContent"),
        ("eval", "created", "window.created"),
        ("type", "#passkey-password", "password-123"), ("click", "#passkey-next"), ("wait", shown),
        ("click", "#passkey-add"), ("wait", "document.querySelectorAll('#passkeys .passkey').length === 1")], **PHONE)
    assert not r.get("failed"), r
    assert r["values"]["created"] == 0 and "пароль ещё раз" in r["values"]["note"]
    assert r["sent"].count("POST /api/passkeys") == 1, r["sent"]
    assert not problems(r), problems(r)


VISIBILITY = "['#view', '#rail'].map(s => getComputedStyle(document.querySelector(s)).visibility)"


def test_lock_hides_the_studio_even_while_its_window_is_forced_shut(browser, live):
    """Окно блокировки, закрытое силой (Escape без жеста, «назад» на Android), открывается снова лишь из
    события close: до него содержимое студии не показывается (класс на <html>, closedby="none")."""
    login = "lk-forced"
    live.add_user(login)
    forced = f"(() => {{ document.querySelector('#lock').close(); return {VISIBILITY}; }})()"
    r = browser.drive(live.url + "/", [*phone_with_key(login), hide(), ("wait", CURTAIN_OPEN),
        ("eval", "covered", VISIBILITY), later(61000), show(), ("wait", LOCK_OPEN),
        ("eval", "closedby", "['#lock', '#curtain'].map(s => document.querySelector(s).getAttribute('closedby'))"),
        ("eval", "forced", forced), ("eval", "reopened", settled(LOCK_OPEN)),
        ("wait", PASSKEY_READY), ("click", "#lock-open"), ("wait", f"!{LOCK_OPEN} && !{CURTAIN_OPEN}"),
        ("eval", "after", VISIBILITY)], **PHONE)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["covered"] == ["hidden", "hidden"] and v["forced"] == ["hidden", "hidden"]
    assert v["closedby"] == ["none", "none"] and v["reopened"] is True and v["after"] == ["visible", "visible"]
    assert not known_refusals(problems(r), 423), problems(r)


def test_media_broken_by_the_lock_load_again_after_opening(browser, live):
    """Картинки и плееры /api/media/… идут мимо api.js: запрос во время блокировки получает 423. После
    отпирания битые грузятся заново; при запирании плееры встают на паузу."""
    login = "lk-media"
    live.add_user(login)
    player = f"""(async () => {{ const m = {API}; window.mid = (await m.api('GET', '/api/images')).items[0].media_id;
      window.paused = 0; window.reloaded = 0;
      const a = document.createElement('audio'); a.pause = () => {{ window.paused += 1; }};
      document.body.append(a); return true; }})()"""
    broken = """(() => { const i = new Image(); i.id = 'probe'; i.src = '/api/media/' + window.mid; document.body.append(i);
      const v = document.createElement('video'); v.setAttribute('src', '/api/media/' + window.mid);
      Object.defineProperty(v, 'error', { get: () => ({ code: 4 }) }); v.load = () => { window.reloaded += 1; };
      document.body.append(v); return true; })()"""
    probe = "document.querySelector('#probe')"
    r = browser.drive(live.url + "/", [*phone_with_key(login), add_gateway(), make_images(1), ("eval", "player", player),
        hide(), ("wait", CURTAIN_OPEN), later(61000), show(), ("wait", LOCK_OPEN),
        ("wait", f"{ME_STATUS}.then(s => s === 423)"), ("eval", "paused", "window.paused"),
        ("eval", "broken", broken), ("wait", f"{probe}.complete && {probe}.naturalWidth === 0"),
        ("wait", PASSKEY_READY), ("click", "#lock-open"), ("wait", f"!{LOCK_OPEN}"),
        ("wait", f"{probe}.naturalWidth > 0"), ("eval", "reloaded", "window.reloaded")], **PHONE)
    assert not r.get("failed"), r
    assert r["values"]["paused"] >= 1 and r["values"]["reloaded"] == 1
    assert not known_refusals(problems(r), 423), problems(r)


def test_mic_stops_under_the_curtain(browser, live):
    """Ушла с экрана — запись голоса отменяется: микрофон не слушает под шторкой, звук не уходит."""
    login = "lk-mic"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*phone_with_key(login), add_gateway(), *go("photo"),
        ("wait", "document.querySelector('#photo-composer .mic')"), ("eval", "mic", COUNTED_MIC),
        ("click", "#photo-composer .mic"), ("wait", "document.querySelector('#photo-composer .mic.on')"),
        hide(), ("wait", f"{CURTAIN_OPEN} && window.stopped === 1"), show(), ("wait", f"!{CURTAIN_OPEN}"),
        ("eval", "on", settled("document.querySelectorAll('.mic.on').length", 500)), ("idle",)], **PHONE)
    assert not r.get("failed"), r
    assert r["values"]["on"] == 0 and "POST /api/dictation" not in r["sent"], r["sent"]
    assert not problems(r), problems(r)


#: GET /api/lock падает «нет связи» столько раз, сколько записано в sessionStorage.failLock (с самого старта страницы)
FAIL_LOCK_STATE = """(() => { const real = window.fetch;
  window.fetch = function (input, init) {
    const left = Number(sessionStorage.getItem('failLock') || 0);
    if (left > 0 && String(input).split('?')[0] === '/api/lock' && init && init.method === 'GET') {
      sessionStorage.setItem('failLock', String(left - 1));
      return Promise.reject(new TypeError('Failed to fetch'));
    }
    return real.call(this, input, init);
  }; })();"""


@pytest.mark.parametrize("way", ["passkey", "password"])
def test_cold_start_without_the_lock_state_still_opens(browser, live, way):
    """Запертый холодный старт, оба GET /api/lock упали: CSRF нет ни из профиля, ни из состояния —
    «Открыть» и пароль берут его заново, а не получают 403 «обновите страницу» (в PWA обновить нечем)."""
    login = f"lk-cold-{way}"
    live.add_user(login)
    opening = ([("click", "#lock-open"), ("wait", PASSKEY_READY), ("click", "#lock-open")] if way == "passkey" else
               [("click", "#lock-use-password"), ("type", "#lock-password", "password-123"), ("click", "#lock-password-go")])
    r = browser.drive(live.url + "/", [*locked_phone(login), ("eval", "cut", "sessionStorage.setItem('failLock', '2')"),
        ("eval", "reload", "location.reload()"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {LOCK_OPEN}"
                 " && document.querySelector('#lock-status').textContent.length > 0"),
        ("eval", "left", "sessionStorage.getItem('failLock')"),
        *opening, ("wait", f"!{LOCK_OPEN} && {READY}"), ("eval", "me", ME_STATUS)], init=FAIL_LOCK_STATE, **PHONE)
    assert not r.get("failed"), r
    assert r["values"]["left"] == "0" and r["values"]["me"] == 200
    assert not known_refusals(problems(r), 423), problems(r)


#: GET путей из sessionStorage.fail ({путь: сколько раз}) падают «нет связи» с самого старта страницы;
#: коды ответов на отметки POST /api/lock/alive — в window.marks
FAIL_GETS = """(() => { const real = window.fetch; window.marks = [];
  window.fetch = async function (input, init) {
    const fail = JSON.parse(sessionStorage.getItem('fail') || '{}');
    const path = String(input).split('?')[0];
    if (fail[path] > 0 && (!init || !init.method || init.method === 'GET')) {
      fail[path] -= 1;
      sessionStorage.setItem('fail', JSON.stringify(fail));
      throw new TypeError('Failed to fetch');
    }
    const response = await real.call(this, input, init);
    if (input === '/api/lock/alive') window.marks.push(response.status);
    return response;
  }; })();"""


def cold_start_offline(login):
    """Вход, после которого первые GET /api/lock и /api/me студии падают без связи: «Студия не открылась»."""
    return [("idle",), ("eval", "cut", "sessionStorage.setItem('fail', JSON.stringify({'/api/lock': 1, '/api/me': 1}))"),
            *signin(login)[1:], ("wait", "document.querySelector('#start-retry')"),
            ("eval", "note", "document.querySelector('#view').textContent")]


@pytest.mark.parametrize("way", ["button", "online"])
def test_start_without_the_profile_opens_without_a_reload(browser, live, way):
    """Профиль при запуске не пришёл (нет связи): в PWA обновить страницу нечем — «Повторить» или
    появление сети открывают студию в той же странице."""
    login = f"start-{way}"
    live.add_user(login)
    retry = ("click", "#start-retry") if way == "button" else ("eval", "online", "window.dispatchEvent(new Event('online'))")
    r = browser.drive(live.url + "/", [*cold_start_offline(login), retry, ("wait", READY),
        ("wait", "document.querySelector('#chat-input')"),
        ("eval", "navigation", "performance.getEntriesByType('navigation')[0].type")], init=FAIL_GETS)
    assert not r.get("failed"), r
    v = r["values"]
    assert "Студия не открылась: Нет связи с сервером" in v["note"] and "Повторить" in v["note"]
    assert v["navigation"] == "navigate"
    assert not problems(r), problems(r)


def test_cold_start_mark_takes_the_csrf_first(browser, live):
    """Холодный старт без связи (GET /api/lock и профиль упали), возвращение на экран: отметка берёт CSRF
    из GET /api/lock и доходит до сервера, а не получает 403; студия открывается тем же возвращением."""
    login = "mark-cold"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*cold_start_offline(login), show(), ("wait", f"window.marks.length > 0 && {READY}"),
        ("eval", "marks", "window.marks")], init=FAIL_GETS)
    assert not r.get("failed"), r
    assert r["values"]["marks"] == [200]
    assert not problems(r), problems(r)


def test_curtain_leaves_when_the_mark_hangs(browser, live):
    """Вернулась раньше минуты, а отметка висит без ответа (у fetch нет срока, iOS ждёт минуту): через
    MARK_SEC — как без связи, шторка убирается, а не висит без кнопок."""
    login = "lk-hang"
    live.add_user(login)
    hang = """(() => { window.asked = []; const timeout = AbortSignal.timeout.bind(AbortSignal);
      AbortSignal.timeout = (ms) => { window.asked.push(ms); return timeout(100); };
      const real = window.fetch;
      window.fetch = (u, o) => u !== '/api/lock/alive' ? real(u, o) : new Promise((_, no) => {
        if (o && o.signal) o.signal.addEventListener('abort', () => no(o.signal.reason)); });
      return true; })()"""
    r = browser.drive(live.url + "/", [*phone_with_key(login), ("eval", "hang", hang), hide(), ("wait", CURTAIN_OPEN),
        later(30000), show(), ("wait", f"!{CURTAIN_OPEN}"), ("eval", "asked", "window.asked"),
        ("eval", "lock", settled(LOCK_OPEN))], **PHONE)
    assert not r.get("failed"), r
    assert r["values"]["asked"][:1] == [8000] and r["values"]["lock"] is False
    assert not problems(r), problems(r)


# ---- PIN на экране блокировки (спека 2026-10-28-studio-lock-pin) ---------------------

PIN_SHOWN = "!document.querySelector('#lock-pin').hidden"
DOTS = "document.querySelector('#lock-pin-dots').children.length"
LOCK_NOTE = "document.querySelector('#lock-status').textContent"
PIN_OK_OFF = "document.querySelector('#lock-pin-ok').disabled"
EXHAUSTED = "PIN не принимается после 10 ошибок — откройте паролем учётки"
#: что видно на экране блокировки: панель, форма пароля, «Открыть» (passkey), «Открыть паролем»
LOCK_PARTS = "['#lock-pin', '#lock-password-form', '#lock-open', '#lock-use-password'].map(s => !document.querySelector(s).hidden)"
USE_PIN_SHOWN = "!document.querySelector('#lock-use-pin').hidden"


def phone_with_pin(login, pin="2580"):
    """Блокировка включена, PIN задан блоком настроек, ключей нет."""
    return [*lock_settings(login), ("click", "#lock-enabled"), server_lock_is(True),
            ("click", "#pin-set"), ("type", "#pin-password", "password-123"), ("type", "#pin-new", pin),
            ("type", "#pin-repeat", pin), ("click", "#pin-save"), server_pin_is(True)]


def pin_locked():
    """Уход с экрана дольше минуты и возвращение: экран блокировки с панелью, сервер отказывает."""
    return [hide(), ("wait", CURTAIN_OPEN), later(61000), show(), ("wait", LOCK_OPEN),
            ("wait", f"{ME_STATUS}.then(s => s === 423)"), ("wait", PIN_SHOWN)]


def tap(digits):
    return [("click", f'[data-digit="{d}"]') for d in digits]


def wrong(note):
    """«0000», «OK» — ждать подписи."""
    return [*tap("0000"), ("click", "#lock-pin-ok"), ("wait", f"{LOCK_NOTE} === {note!r}")]


def test_phone_opens_with_the_pin_keeping_the_text(browser, live):
    login = "pin-open"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*phone_with_pin(login), add_gateway(), *go("photo"),
        ("wait", "document.querySelector('#photo-prompt')"), ("type", "#photo-prompt", "кот на крыше"),
        *pin_locked(), ("eval", "parts", LOCK_PARTS),
        *tap("1111"), ("eval", "typed", f"[{DOTS}, {PIN_OK_OFF}]"), ("click", "#lock-pin-ok"),
        ("wait", f"{LOCK_NOTE} === 'Неверный PIN'"), ("eval", "wrong", f"[{DOTS}, {LOCK_OPEN}]"),
        ("eval", "still", ME_STATUS),
        *tap("2580"), ("click", "#lock-pin-ok"), ("wait", f"!{LOCK_OPEN} && !{CURTAIN_OPEN}"),
        ("eval", "prompt", "document.querySelector('#photo-prompt').value"), ("eval", "after", ME_STATUS),
        ("eval", "stored", "JSON.stringify({...localStorage}) + JSON.stringify({...sessionStorage})")], **PHONE)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["parts"] == [True, False, False, True]
    assert v["typed"] == [4, False] and v["wrong"] == [0, True] and v["still"] == 423
    assert v["prompt"] == "кот на крыше" and v["after"] == 200
    assert "2580" not in v["stored"] and "1111" not in v["stored"]
    assert r["sent"].count("POST /api/lock/pin") == 2, r["sent"]
    assert not known_refusals(problems(r), 423, 403), problems(r)


def test_ten_wrong_pins_turn_the_pad_into_the_password(browser, live):
    login = "pin-ten"
    live.add_user(login)
    spend = f"""(async () => {{ const m = {API}; const codes = [];
      for (let i = 0; i < 5; i++) {{ try {{ await m.api('POST', '/api/lock/pin', {{ pin: '9999' }}); }}
        catch (e) {{ codes.push(e.code); }} }} return codes; }})()"""
    r = browser.drive(live.url + "/", [*phone_with_pin(login), *pin_locked(), ("eval", "spent", spend),
        *wrong("Неверный PIN — осталось 4 попытки"), ("eval", "dots", DOTS),
        *wrong("Неверный PIN — осталось 3 попытки"), *wrong("Неверный PIN — осталось 2 попытки"),
        *wrong("Неверный PIN — осталась 1 попытка"),
        *tap("0000"), ("click", "#lock-pin-ok"), ("wait", f"document.querySelector('#lock-pin').hidden"),
        ("eval", "parts", LOCK_PARTS), ("eval", "usePin", USE_PIN_SHOWN), ("eval", "note", LOCK_NOTE),
        ("type", "#lock-password", "password-123"), ("click", "#lock-password-go"), ("wait", f"!{LOCK_OPEN}"),
        ("eval", "me", ME_STATUS), *pin_locked(), ("eval", "again", LOCK_PARTS)], **PHONE)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["spent"] == ["pin"] * 5 and v["dots"] == 0
    assert v["parts"] == [False, True, False, False] and v["usePin"] is False and v["note"] == EXHAUSTED
    assert v["me"] == 200 and v["again"] == [True, False, False, True]
    assert not known_refusals(problems(r), 423, 403), problems(r)


def test_lock_goes_back_to_the_pin_after_the_password(browser, live):
    """«Открыть паролем» — форма пароля, под ней «Открыть PIN-кодом»; она — снова панель (форма и сама кнопка скрыты,
    набранный пароль и слова отказа стёрты); PIN открывает."""
    login = "pin-back"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*phone_with_pin(login), *pin_locked(), ("eval", "first", USE_PIN_SHOWN),
        ("click", "#lock-use-password"), ("wait", "!document.querySelector('#lock-password-form').hidden"),
        ("eval", "password", f"[...{LOCK_PARTS}, {USE_PIN_SHOWN}]"),
        ("type", "#lock-password", "not-the-password"), ("click", "#lock-password-go"), ("wait", f"{LOCK_NOTE} !== ''"),
        ("click", "#lock-use-pin"),
        ("eval", "back", f"[...{LOCK_PARTS}, {USE_PIN_SHOWN}, {LOCK_NOTE}, document.querySelector('#lock-password').value]"),
        *tap("2580"), ("click", "#lock-pin-ok"), ("wait", f"!{LOCK_OPEN}"), ("eval", "me", ME_STATUS)], **PHONE)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["first"] is False and v["password"] == [False, True, False, False, True]
    assert v["back"] == [True, False, False, True, False, "", ""] and v["me"] == 200
    assert not known_refusals(problems(r), 423, 403, 401), problems(r)


#: POST /api/lock/pin считаются (window.pins) и отвечают на 400 мс позже
COUNT_PINS = """(() => { window.pins = 0; const real = window.fetch;
  window.fetch = function (input, init) {
    if (input !== '/api/lock/pin') return real.call(this, input, init);
    window.pins += 1;
    return new Promise(r => setTimeout(r, 400)).then(() => real.call(this, input, init)); }; })();"""


def test_double_ok_sends_one_pin(browser, live):
    """Двойное касание «OK» и Enter, пока идёт запрос, — один запрос и одна попытка."""
    login = "pin-double"
    live.add_user(login)
    double = """(() => { const ok = document.querySelector('#lock-pin-ok'); ok.click(); ok.click();
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); return ok.disabled; })()"""
    r = browser.drive(live.url + "/", [*phone_with_pin(login), *pin_locked(), *tap("0000"),
        ("eval", "busy", double), ("click", "#lock-pin-ok"), ("press", "Enter"),
        ("wait", f"{LOCK_NOTE} === 'Неверный PIN'"), ("press", "Enter"),
        ("eval", "pins", settled("window.pins", 500)),
        ("eval", "left", "fetch('/api/lock').then(r => r.json()).then(s => s.pin_left)")], init=COUNT_PINS, **PHONE)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["busy"] is True and v["pins"] == 1 and v["left"] == 9
    assert not known_refusals(problems(r), 423, 403), problems(r)


def test_typed_pin_digits_are_gone_after_leaving(browser, live):
    """После 8 цифр новые не принимаются, «OK» — от 4 цифр; уход с экрана стирает набранное. 320 px."""
    login = "pin-gone"
    live.add_user(login)
    clear = "(() => { const b = document.querySelector('#lock-pin-back'); for (let i = 0; i < 7; i++) b.click(); return true; })()"
    fits = f"""[{NO_OVERFLOW}, (b => b.width >= 64 && b.height >= 64)(document.querySelector('[data-digit="5"]').getBoundingClientRect()),
      (b => b.left >= 0 && b.right <= innerWidth)(document.querySelector('#lock-pin').getBoundingClientRect()),
      document.querySelector('#lock-pin-back').getAttribute('aria-label')]"""
    r = browser.drive(live.url + "/", [*phone_with_pin(login), *pin_locked(), ("eval", "fits", fits),
        *tap("123456789"), ("eval", "nine", f"[{DOTS}, {PIN_OK_OFF}]"),
        ("click", "#lock-pin-back"), ("eval", "back", DOTS),
        ("eval", "clear", clear), *tap("25"), ("eval", "two", f"[{DOTS}, {PIN_OK_OFF}]"),
        hide(), show(), ("eval", "after", DOTS)], **{**PHONE, "width": 320})
    assert not r.get("failed"), r
    v = r["values"]
    assert v["fits"] == [True, True, True, "Стереть"]
    assert v["nine"] == [8, False] and v["back"] == 7 and v["two"] == [2, True] and v["after"] == 0
    assert "POST /api/lock/pin" not in r["sent"], r["sent"]
    assert not known_refusals(problems(r), 423), problems(r)


def test_physical_keys_type_the_pin(browser, live):
    login = "pin-keys"
    live.add_user(login)
    keys = [("press", k) for k in ["2", "5", "9", "Backspace", "8", "0", "Enter"]]
    r = browser.drive(live.url + "/", [*phone_with_pin(login), *pin_locked(), *keys,
        ("wait", f"!{LOCK_OPEN}"), ("eval", "me", ME_STATUS)], **PHONE)
    assert not r.get("failed"), r
    assert r["values"]["me"] == 200 and r["sent"].count("POST /api/lock/pin") == 1, r["sent"]
    assert not known_refusals(problems(r), 423), problems(r)


@pytest.mark.parametrize("way", ["return", "open"])
def test_pin_pad_comes_back_when_the_lock_state_was_lost(browser, live, way):
    """GET /api/lock при показе экрана не пришёл (нет связи): «Открыть» и «Нет связи». Возвращение на экран
    или «Открыть» перечитывают состояние — панель PIN, и PIN открывает; подготовки passkey (её no_keys
    увёл бы учётку без ключей к паролю) нет."""
    login = f"pin-stale-{way}"
    live.add_user(login)
    retry = [hide(), show()] if way == "return" else [("click", "#lock-open")]
    r = browser.drive(live.url + "/", [*phone_with_pin(login), hide(), ("wait", CURTAIN_OPEN),
        ("eval", "cut", "sessionStorage.setItem('fail', JSON.stringify({'/api/lock': 1}))"),
        later(61000), show(), ("wait", LOCK_OPEN), ("wait", f"{LOCK_NOTE} === 'Нет связи с сервером'"),
        ("eval", "lost", LOCK_PARTS), *retry, ("wait", PIN_SHOWN), ("eval", "back", LOCK_PARTS),
        ("eval", "note", LOCK_NOTE),
        *tap("2580"), ("click", "#lock-pin-ok"), ("wait", f"!{LOCK_OPEN}"), ("eval", "me", ME_STATUS)],
        init=FAIL_GETS, **PHONE)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["lost"] == [False, False, True, True] and v["back"] == [True, False, False, True]
    assert v["note"] == "" and v["me"] == 200
    assert "POST /api/lock/challenge" not in r["sent"], r["sent"]
    assert not known_refusals(problems(r), 423), problems(r)


def test_enter_on_a_lock_button_presses_it_not_the_pin(browser, live):
    """Панель на экране, фокус на «Открыть паролем»: цифра с клавиатуры набирается, а Enter не шлёт
    набранный PIN и не отменён — его действие по умолчанию нажимает кнопку в фокусе. (Нажатие драйвера —
    keyDown без text: кнопку оно само не нажимает, поэтому отмену проверяет событие страницы.)"""
    login = "pin-enter"
    live.add_user(login)
    focus = "(() => { document.querySelector('#lock-use-password').focus(); return document.activeElement.id; })()"
    prevented = "!document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }))"
    r = browser.drive(live.url + "/", [*phone_with_pin(login), *pin_locked(), *tap("2580"),
        ("eval", "focus", focus), ("press", "5"), ("press", "Enter"), ("eval", "after", settled(f"[{DOTS}, {PIN_SHOWN}]")),
        ("eval", "prevented", prevented), ("click", "#lock-use-password"),
        ("wait", "!document.querySelector('#lock-password-form').hidden")], **PHONE)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["focus"] == "lock-use-password" and v["after"] == [5, True] and v["prevented"] is False
    assert "POST /api/lock/pin" not in r["sent"], r["sent"]
    assert not known_refusals(problems(r), 423), problems(r)


def test_physical_keys_type_the_pin_with_focus_on_open(browser, live):
    """Учётка с ключом и PIN: showModal() ставит фокус на «Открыть». Цифры и Backspace с клавиатуры без
    касания панели набираются; Enter на «Открыть» PIN не шлёт (нажимает кнопку); фокус ни на чём — Enter открывает."""
    login = "pin-keys-open"
    live.add_user(login)
    pin = [("click", "#pin-set"), ("type", "#pin-password", "password-123"), ("type", "#pin-new", "2580"),
           ("type", "#pin-repeat", "2580"), ("click", "#pin-save"), server_pin_is(True)]
    focus = "(() => { document.querySelector('#lock-open').focus(); return document.activeElement.id; })()"
    keys = [("press", k) for k in ["2", "5", "9", "Backspace", "8", "0"]]
    r = browser.drive(live.url + "/", [*phone_with_key(login), *pin, *pin_locked(), ("wait", PASSKEY_READY),
        ("eval", "parts", LOCK_PARTS), ("eval", "shown", "document.activeElement.id"), ("eval", "focus", focus),
        *keys, ("eval", "dots", DOTS), ("press", "Enter"), ("eval", "after", settled(f"[{DOTS}, {LOCK_OPEN}]")),
        ("eval", "blur", "(() => { document.activeElement.blur(); return document.activeElement === document.body; })()"),
        ("press", "Enter"), ("wait", f"!{LOCK_OPEN}"), ("eval", "me", ME_STATUS)], **PHONE)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["parts"] == [True, False, True, True] and v["focus"] == "lock-open"
    assert v["dots"] == 4 and v["after"] == [4, True] and v["blur"] is True and v["me"] == 200
    assert r["sent"].count("POST /api/lock/pin") == 1, r["sent"]
    assert not known_refusals(problems(r), 423), problems(r)


def remove_pin_after_a_wrong_one(live, login):
    """«Убрать PIN» на другом устройстве: как только учётка потратила попытку PIN, сервер убирает PIN
    (своё DELETE /api/me/pin запертая страница не сделает — 423). Поток; вернуть — join."""
    import db

    def run():
        conn = db.connect(live.db_path)
        try:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if conn.execute("SELECT pin_fails FROM users WHERE login = ?", (login,)).fetchone()[0] >= 1:
                    conn.execute("UPDATE users SET pin_hash = NULL WHERE login = ?", (login,))
                    return
                time.sleep(0.05)
        finally:
            conn.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


@pytest.mark.parametrize("gone", ["no_pin", "pin_exhausted"])
def test_pin_taken_away_elsewhere_turns_the_pad_into_the_password(browser, live, gone):
    """Панель на экране, а PIN тем временем убрали (no_pin) или исчерпали (pin_exhausted) на другом
    устройстве: «OK» — панель уходит, поле пароля, при исчерпанном — подпись; пароль открывает."""
    login = f"pin-{gone}"
    live.add_user(login)
    if gone == "no_pin":
        thread = remove_pin_after_a_wrong_one(live, login)
        meanwhile = [*wrong("Неверный PIN"), ("wait", "fetch('/api/lock').then(r => r.json()).then(s => !s.pin)")]
    else:
        thread = None
        spend = f"""(async () => {{ const m = {API}; const left = [];
          for (let i = 0; i < 10; i++) {{ try {{ await m.api('POST', '/api/lock/pin', {{ pin: '9999' }}); }}
            catch (e) {{ left.push(e.detail.left); }} }} return left.at(-1); }})()"""
        meanwhile = [("eval", "spent", spend)]
    try:
        r = browser.drive(live.url + "/", [*phone_with_pin(login), *pin_locked(), *meanwhile,
            ("eval", "still", PIN_SHOWN), *tap("2580"), ("click", "#lock-pin-ok"),
            ("wait", "document.querySelector('#lock-pin').hidden"),
            ("eval", "parts", LOCK_PARTS), ("eval", "note", LOCK_NOTE),
            ("type", "#lock-password", "password-123"), ("click", "#lock-password-go"), ("wait", f"!{LOCK_OPEN}"),
            ("eval", "me", ME_STATUS)], **PHONE)
    finally:
        if thread:
            thread.join(5)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["still"] is True and v["parts"] == [False, True, False, False]
    assert v["note"] == ("" if gone == "no_pin" else EXHAUSTED) and v["me"] == 200
    if gone == "pin_exhausted":
        assert v["spent"] == 0
    assert not known_refusals(problems(r), 423, 403, 409), problems(r)


# ---- «В работе» и уведомления (спека 2026-10-21-studio-jobs-panel) ----------------------------

JOBS_POLLED = "document.querySelector('#jobs').dataset.polled === '1'"
JOBS_PLACED = """(() => { const j = document.querySelector('#jobs').getBoundingClientRect();
  const apart = (s) => { const b = document.querySelector(s).getBoundingClientRect();
    return j.right <= b.left || b.right <= j.left || j.bottom <= b.top || b.bottom <= j.top; };
  return j.left >= 0 && j.top >= 0 && j.right <= innerWidth && j.bottom <= innerHeight
    && apart('#avatar'); })()"""
JOBS_SHEET_INSIDE = """(() => { const b = document.querySelector('#jobs-panel').getBoundingClientRect();
  return b.left >= 0 && b.top >= 0 && b.right <= innerWidth + 0.5 && b.bottom <= innerHeight + 0.5; })()"""
JOBS_ROWS = """[...document.querySelectorAll('{list} .jobs-row')].map((r) => [r.dataset.section,
  r.querySelector('.jobs-title').textContent, r.classList.contains('new'), r.dataset.state])"""


def reload_page(name="reload"):
    """Перезагрузка; ждать новую страницу (у старой метка window.before ещё стоит)."""
    return [("eval", name, "window.before = 1; location.reload()"), ("wait", f"!window.before && {READY}")]


@pytest.mark.parametrize("width", [390, 1280])
def test_jobs_badge_counts_a_running_picture(browser, live, width):
    """Запустил картинку — у значка «1» сразу (jobs.kick), панель: «Идёт» — «Картинка · Lustify» с ходом,
    «Готово за час» — пусто; значок в окне и не на аватаре."""
    login = f"jobs-run{width}"
    live.add_user(login)
    live.gw.hold = True
    try:
        r = browser.drive(live.url + "/", [*photo_screen(login), ("wait", JOBS_POLLED),
            ("eval", "before", "document.querySelector('#jobs').dataset.running"),
            ("type", "#photo-prompt", "секретный кот"), ("click", "#photo-go"),
            ("wait", "document.querySelector('#jobs').dataset.running === '1'"),
            ("eval", "count", "document.querySelector('#jobs .jobs-count').textContent"),
            ("eval", "label", "document.querySelector('#jobs').getAttribute('aria-label')"),
            ("eval", "placed", JOBS_PLACED),
            ("click", "#jobs"), ("wait", is_open("#jobs-panel")),
            ("eval", "rows", JOBS_ROWS.replace("{list}", "#jobs-running")),
            ("eval", "detail", "document.querySelector('#jobs-running .jobs-detail').textContent"),
            ("eval", "finished", "document.querySelector('#jobs-finished').textContent"),
            ("eval", "inside", JOBS_SHEET_INSIDE),
            ("press", "Escape"), ("wait", "!" + is_open("#jobs-panel")),
            ("eval", "overflow", f"!({NO_OVERFLOW})")],
            width=width, mobile=width == 390)
    finally:
        live.gw.hold = False
    assert not r.get("failed"), r
    v = r["values"]
    assert v["before"] == "0" and v["count"] == "1" and v["label"] == "В работе: 1" and v["placed"]
    assert v["rows"] == [["photo", "Картинка · Lustify", False, "running"]]
    assert re.fullmatch(r"жду карту или генерирую · \d+:\d\d", v["detail"]), v["detail"]
    assert v["finished"] == "За час ничего не закончилось" and v["inside"] and not v["overflow"]
    assert not problems(r), problems(r)


def test_jobs_panel_shows_ready_and_opens_the_record(browser, live):
    """Готовое до открытия страницы — в «Готово за час» с точкой у значка; открытая панель снимает точку
    (и после перезагрузки: отметка на устройстве), «Открыть» ведёт в «Фото» к просмотру картинки."""
    login = "jobs-ready"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*photo_screen(login, cards=1), *reload_page(),
        ("wait", "document.querySelector('#jobs').dataset.new === '1'"),
        ("eval", "dot", "!document.querySelector('#jobs .jobs-dot').hidden"),
        ("click", "#jobs"), ("wait", is_open("#jobs-panel")),
        ("eval", "rows", JOBS_ROWS.replace("{list}", "#jobs-finished")),
        ("eval", "running", "document.querySelector('#jobs-running').textContent"),
        ("eval", "dotOpen", "document.querySelector('#jobs').dataset.new || ''"),
        ("click", "#jobs-finished .jobs-row .jobs-open"),
        ("wait", is_open("#viewer")),
        ("eval", "hash", "location.hash"), ("eval", "panel", is_open("#jobs-panel")),
        *reload_page("again"), ("wait", JOBS_POLLED),
        ("eval", "dotAfter", "document.querySelector('#jobs').dataset.new || ''")])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["dot"] is True and v["rows"] == [["photo", "Картинка · Lustify", True, "done"]]
    assert v["running"] == "Сейчас ничего не идёт" and v["dotOpen"] == ""
    assert v["hash"] == "#/photo" and v["panel"] is False and v["dotAfter"] == ""
    assert not problems(r), problems(r)


def test_jobs_open_selects_records_in_every_section(browser, live):
    """#/<раздел>?id=<запись>: расшифровка, распознавание и озвучка выбраны в списке, карточка ролика
    подсвечена; ?id из адреса убран. Записи готовы за час — они же в «Готово за час»."""
    login = "jobs-open"
    uid = live.add_user(login)
    now = time.time()
    live_sql(live, "INSERT INTO transcripts(id, user_id, model, name, source_media_id, state, detail, created,"
                   " finished) VALUES ('tr-jobs', ?, 'whisperx', 'Звонок.m4a', 'nope', 'error', 'не вышло', ?, ?)",
             (uid, now - 60, now - 30))
    live_sql(live, "INSERT INTO speeches(id, user_id, model, voice, text, state, detail, created, finished)"
                   " VALUES ('sp-jobs', ?, 'qwen3-tts', 'v', 'Привет', 'error', 'не вышло', ?, ?)",
             (uid, now - 60, now - 20))
    live_sql(live, "INSERT INTO scans(id, user_id, model, name, origin, pages, state, detail, created, finished)"
                   " VALUES ('oc-jobs', ?, 'hunyuan-ocr', 'Скан.pdf', 'section', 2, 'error', 'не вышло', ?, ?)",
             (uid, now - 60, now - 25))
    live_sql(live, "INSERT INTO videos(id, user_id, connection_id, source_media_id, prompt, seconds, quality, smooth,"
                   " state, progress, detail, created, finished) VALUES ('vd-jobs', ?, 1, 'nope', '', 5, 'standard',"
                   " 1, 'failed', 0, 'ролик не получился', ?, ?)", (uid, now - 60, now - 10))
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("wait", JOBS_POLLED),
        ("eval", "badge", "document.querySelector('#jobs').dataset.new"),
        *go("transcribe?id=tr-jobs"),
        ("wait", "document.querySelector('.tr-item[data-id=\"tr-jobs\"][aria-current=true]')"),
        ("eval", "trHash", "location.hash"),
        *go("speech?id=sp-jobs"),
        ("wait", "document.querySelector('.sp-item[data-id=\"sp-jobs\"][aria-current=true]')"),
        ("eval", "spHash", "location.hash"),
        *go("ocr?id=oc-jobs"),
        ("wait", "document.querySelector('.ocr-item[data-id=\"oc-jobs\"][aria-current=true]')"),
        ("eval", "ocHash", "location.hash"),
        *go("video?id=vd-jobs"),
        ("wait", "document.querySelector('.video-card[data-id=\"vd-jobs\"].jobs-focus')"),
        ("eval", "vdHash", "location.hash"),
        ("click", "#jobs"), ("wait", is_open("#jobs-panel")),
        ("eval", "rows", JOBS_ROWS.replace("{list}", "#jobs-finished"))])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["badge"] == "1"
    assert (v["trHash"], v["spHash"], v["ocHash"], v["vdHash"]) == ("#/transcribe", "#/speech", "#/ocr", "#/video")
    assert v["rows"] == [["video", "Ролик · 5 с", True, "error"], ["speech", "Озвучка · Привет", True, "error"],
                         ["ocr", "Распознавание «Скан.pdf»", True, "error"],
                         ["transcribe", "Расшифровка «Звонок.m4a»", True, "error"]]
    assert not known_refusals(problems(r), 404), problems(r)


#: уведомления без настоящего разрешения: Notification.permission и requestPermission — из
#: sessionStorage.perm (по умолчанию granted), showNotification регистрации пишет в window.shown
NOTIFY_INIT = """(() => {
  window.shown = [];
  if (!window.Notification) window.Notification = function () { throw new TypeError('Illegal constructor'); };
  Object.defineProperty(Notification, 'permission', { configurable: true,
    get: () => sessionStorage.getItem('perm') || 'granted' });
  Notification.requestPermission = async () => sessionStorage.getItem('perm') || 'granted';
  if (window.ServiceWorkerRegistration) ServiceWorkerRegistration.prototype.showNotification = function (title, options) {
    window.shown.push({ title, options: JSON.parse(JSON.stringify(options)) }); return Promise.resolve(); };
})();"""


def picture_job(name):
    """Картинка через API страницы и опрос «В работе» сразу; id — под именем `name`."""
    return ("eval", name, f"""(async () => {{ const m = {API};
      const job = await m.api('POST', '/api/images', {{ prompt: 'секретный кот', size: 'square' }});
      (await import('/static/js/jobs.js')).kick(); return job.id; }})()""")


def notify_settings(login):
    return [*signin(login), ("wait", READY), add_gateway(), *go("settings/other"),
            ("wait", "document.querySelector('#notify-enabled')")]


def test_notification_only_when_hidden_and_without_content(browser, live):
    """«Прочее» → «Уведомления» включает флаг устройства; готовое при видимой странице — без
    уведомления, при скрытой — одно: «Студия: картинка готова», tag — id, data.href — запись, без промпта."""
    login = "notify-on"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*notify_settings(login),
        ("eval", "disabled", "document.querySelector('#notify-enabled').disabled"),
        ("click", "#notify-enabled"),
        ("wait", "document.querySelector('#notify-status').textContent.includes('включены')"),
        ("eval", "stored", "localStorage.getItem('studio-notify')"),
        picture_job("first"),
        ("wait", "document.querySelector('#jobs').dataset.new === '1' && document.querySelector('#jobs').dataset.running === '0'"),
        ("eval", "whileVisible", settled("window.shown.length")),
        hide(), picture_job("second"),
        ("wait", "window.shown.length === 1"),
        ("eval", "shown", settled("window.shown", 500)),
        show()], init=NOTIFY_INIT)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["disabled"] is False and v["stored"] == "1" and v["whileVisible"] == 0
    assert v["shown"] == [{"title": "Студия: картинка готова", "options": {
        "tag": v["second"], "icon": "/static/icons/icon-192.png", "data": {"href": f"photo?id={v['second']}"}}}]
    assert "секретный" not in json.dumps(v["shown"], ensure_ascii=False)
    assert not problems(r), problems(r)


def test_notify_toggle_explains_a_refusal(browser, live):
    """Браузер не разрешил уведомления: переключатель снят, флага нет, слова — что сделать."""
    login = "notify-denied"
    live.add_user(login)
    r = browser.drive(live.url + "/", [("idle",), ("eval", "deny", "sessionStorage.setItem('perm', 'denied')"),
        *notify_settings(login), ("click", "#notify-enabled"),
        ("wait", "document.querySelector('#notify-status').textContent !== ''"),
        ("eval", "note", "document.querySelector('#notify-status').textContent"),
        ("eval", "checked", "document.querySelector('#notify-enabled').checked"),
        ("eval", "stored", "localStorage.getItem('studio-notify')")], init=NOTIFY_INIT)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["note"] == "Браузер запретил уведомления — разрешите их в настройках сайта"
    assert v["checked"] is False and v["stored"] is None
    assert not problems(r), problems(r)


#: ответы GET /api/jobs — из window.jobsOut (пока он задан); window.job(раздел, id, состояние, finished, доп.)
JOBS_STUB = """(() => { window.jobsOut = null; const real = window.fetch;
  window.job = (section, id, state, finished, extra = {}) => ({ section, id, state, title: 'Запись ' + id,
    detail: 'шаг ' + id, started: Date.now() / 1000 - 60, finished, href: section + '?id=' + id, ...extra });
  window.fetch = (u, o) => !(window.jobsOut && String(u).startsWith('/api/jobs')) ? real(u, o)
    : Promise.resolve(new Response(JSON.stringify({ now: Date.now() / 1000, ...window.jobsOut }),
      { status: 200, headers: { 'content-type': 'application/json' } }));
  return true; })()"""
KICK = "(await import('/static/js/jobs.js')).kick()"


def test_jobs_push_replaces_local_notifications_and_leaves_a_fallback(browser, live):
    """Подписанная сессия не дублирует push локальным уведомлением; без подписки локальное остаётся."""
    login = "jobs-push"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("wait", JOBS_POLLED),
        ("eval", "stub", JOBS_STUB),
        ("eval", "notify", "localStorage.setItem('studio-notify', '1')"), hide(),
        ("eval", "push", f"""(async () => {{ window.jobsOut = {{ push: true, running: [],
          finished: [job('translate', 'push-done', 'done', Date.now() / 1000)] }}; {KICK}; }})()"""),
        ("wait", "document.querySelector('#jobs').dataset.new === '1'"),
        ("eval", "withPush", "window.shown.length"),
        ("eval", "local", f"""(async () => {{ window.jobsOut = {{ push: false, running: [],
          finished: [job('speech', 'local-done', 'done', Date.now() / 1000)] }}; {KICK}; }})()"""),
        ("wait", "window.shown.length === 1"), ("eval", "shown", "window.shown"), show()], init=NOTIFY_INIT)
    assert not r.get("failed"), r
    assert r["values"]["withPush"] == 0
    assert r["values"]["shown"] == [{"title": "Студия: озвучка готова", "options": {
        "tag": "local-done", "icon": "/static/icons/icon-192.png", "data": {"href": "speech?id=local-done"}}}]
    assert not problems(r), problems(r)


def test_jobs_panel_keeps_rows_and_focus_between_polls(browser, live):
    """Открытая панель на опросе не пересобирается: фокус на «Открыть» остаётся, строка того же id — тот
    же узел с новым ходом, ставшее готовым уходит из «Идёт» и встаёт сверху «Готово за час» (новое)."""
    login = "jobs-keep"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("wait", JOBS_POLLED),
        ("eval", "stub", JOBS_STUB),
        ("eval", "first", f"""(async () => {{ window.jobsOut = {{ running: [job('photo', 'ja', 'running', null),
          job('video', 'jb', 'running', null, {{ progress: 10 }})],
          finished: [job('speech', 'jc', 'done', Date.now() / 1000 - 120)] }}; {KICK}; return true; }})()"""),
        ("wait", "document.querySelector('#jobs').dataset.running === '2'"),
        ("click", "#jobs"), ("wait", is_open("#jobs-panel")),
        ("eval", "opened", settled("document.querySelectorAll('#jobs-panel .jobs-row').length", 500)),
        ("eval", "focus", """(() => { const b = document.querySelector('#jobs-finished .jobs-row[data-id=jc] .jobs-open');
          b.focus(); window.kept = b; window.rowB = document.querySelector('#jobs-running .jobs-row[data-id=jb]');
          return document.activeElement === b; })()"""),
        ("eval", "second", f"""(async () => {{ window.jobsOut = {{
          running: [job('video', 'jb', 'running', null, {{ progress: 50, detail: 'генерирую кадры' }})],
          finished: [job('photo', 'ja', 'done', Date.now() / 1000), job('speech', 'jc', 'done', Date.now() / 1000 - 120)] }};
          {KICK}; return true; }})()"""),
        ("wait", "document.querySelectorAll('#jobs-finished .jobs-row').length === 2"),
        ("eval", "after", """(() => { const b = window.rowB;
          return { focused: document.activeElement === window.kept, sameRow: document.querySelector('#jobs-running .jobs-row[data-id=jb]') === b,
            detail: b.querySelector('.jobs-detail').textContent,
            running: [...document.querySelectorAll('#jobs-running .jobs-row')].map((n) => n.dataset.id),
            finished: [...document.querySelectorAll('#jobs-finished .jobs-row')].map((n) => [n.dataset.id, n.dataset.state, n.classList.contains('new')]) }; })()""")])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["opened"] == 3 and v["focus"] is True
    a = v["after"]
    assert a["focused"] is True and a["sameRow"] is True
    assert re.fullmatch(r"генерирую кадры · 50% · \d+:\d\d", a["detail"]), a["detail"]
    assert a["running"] == ["jb"] and [f[:2] for f in a["finished"]] == [["ja", "done"], ["jc", "done"]]
    assert a["finished"][0][2] is True
    assert not problems(r), problems(r)


def test_jobs_poll_gives_up_a_hanging_request(browser, live):
    """Запрос опроса висит без ответа (iOS после сна держит fetch минуту): у него срок, и следующий опрос
    (kick) проходит, а не ждёт его вечно."""
    login = "jobs-hang"
    live.add_user(login)
    hang = """(() => { window.asked = []; window.hangJobs = true; const timeout = AbortSignal.timeout.bind(AbortSignal);
      AbortSignal.timeout = (ms) => { window.asked.push(ms); return timeout(100); };
      const real = window.fetch;
      window.fetch = (u, o) => !(window.hangJobs && String(u).startsWith('/api/jobs')) ? real(u, o) : new Promise((_, no) => {
        if (o && o.signal) o.signal.addEventListener('abort', () => no(o.signal.reason)); });
      return true; })()"""
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("wait", JOBS_POLLED),
        ("eval", "hang", hang),
        ("eval", "kicks", f"""(async () => {{ delete document.querySelector('#jobs').dataset.polled; {KICK};
          await new Promise((r) => setTimeout(r, 30)); window.hangJobs = false; {KICK}; return true; }})()"""),
        ("wait", JOBS_POLLED), ("eval", "asked", "window.asked")], timeout=8000)
    assert not r.get("failed"), r
    assert 20000 in r["values"]["asked"]
    assert not problems(r), problems(r)


def test_jobs_open_keeps_the_open_section(browser, live):
    """«Открыть» (и адрес ?id) в раздел, который уже открыт, его не пересобирает: набранный промпт и
    текст остаются в том же поле, запись открыта или выбрана, ?id из адреса убран."""
    login = "jobs-same"
    uid = live.add_user(login)
    now = time.time()
    live_sql(live, "INSERT INTO speeches(id, user_id, model, voice, text, state, detail, created, finished)"
                   " VALUES ('sp-keep', ?, 'qwen3-tts', 'v', 'Привет', 'error', 'не вышло', ?, ?)",
             (uid, now - 60, now - 20))
    live_sql(live, "INSERT INTO videos(id, user_id, connection_id, source_media_id, prompt, seconds, quality, smooth,"
                   " state, progress, detail, created, finished) VALUES ('vd-keep', ?, 1, 'nope', '', 5, 'standard',"
                   " 1, 'failed', 0, 'ролик не получился', ?, ?)", (uid, now - 60, now - 10))
    kept = "(() => {{ const f = document.querySelector('{0}'); return [f === window.field, f.value, location.hash]; }})()"
    mark = "(() => {{ window.field = document.querySelector('{0}'); return true; }})()"
    r = browser.drive(live.url + "/", [*photo_screen(login, cards=1), ("wait", JOBS_POLLED),
        ("type", "#photo-prompt", "черновик промпта"), ("eval", "m1", mark.format("#photo-prompt")),
        ("click", "#jobs"), ("wait", is_open("#jobs-panel")),
        ("wait", "document.querySelector('#jobs-finished .jobs-row[data-section=photo] .jobs-open')"),
        ("click", "#jobs-finished .jobs-row[data-section=photo] .jobs-open"), ("wait", is_open("#viewer")),
        ("eval", "photo", kept.format("#photo-prompt")),
        *go("video"), ("wait", VIDEO_FEED_READY),
        ("type", "#video-prompt", "черновик ролика"), ("eval", "m2", mark.format("#video-prompt")),
        *go("video?id=vd-keep"), ("wait", "document.querySelector('.video-card[data-id=\"vd-keep\"].jobs-focus')"),
        ("eval", "video", kept.format("#video-prompt")),
        *go("speech"), ("wait", "document.querySelector('#speech-text')"),
        ("type", "#speech-text", "черновик озвучки"), ("eval", "m3", mark.format("#speech-text")),
        *go("speech?id=sp-keep"), ("wait", "document.querySelector('.sp-item[data-id=\"sp-keep\"][aria-current=true]')"),
        ("eval", "speech", kept.format("#speech-text"))])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["photo"] == [True, "черновик промпта", "#/photo"]
    assert v["video"] == [True, "черновик ролика", "#/video"]
    assert v["speech"] == [True, "черновик озвучки", "#/speech"]
    assert not known_refusals(problems(r), 404), problems(r)


def test_jobs_open_keeps_the_transcript_draft(browser, live):
    """Правка расшифровки не сохранена, а пришёл адрес той же записи (?id, «Открыть»): черновик цел."""
    login = "jobs-draft"
    live.add_user(login)
    area = "document.querySelector(\".tr-turn[data-index='1'] .tr-edit-text\")"
    r = browser.drive(live.url + "/", [*transcribed(login),
        ("click", "#tr-edit"), ("wait", "document.querySelectorAll('.tr-edit-text').length === 2"),
        set_value(".tr-turn[data-index='1'] .tr-edit-text", "Черновик правки"),
        ("eval", "nav", "location.hash = '#/transcribe?id=' + document.querySelector('.tr-item[aria-current=true]').dataset.id"),
        ("wait", "location.hash === '#/transcribe'"),
        ("eval", "draft", settled(f"{area}?.value ?? null", 1000))])
    assert not r.get("failed"), r
    assert r["values"]["draft"] == "Черновик правки"
    assert not problems(r), problems(r)


# ---- поиск по истории ---------------------------------------------------------------

SEARCH_ROWS = """[...document.querySelectorAll('#search-results .search-row')].map((r) => [r.dataset.section,
  r.querySelector('.search-title').textContent, [...r.querySelectorAll('mark')].map((m) => m.textContent)])"""
SEARCH_PLACED = JOBS_PLACED.replace("'#jobs'", "'#search-open'").replace(
    "apart('#avatar')", "apart('#avatar') && apart('#jobs')")


def search_ready(rows):
    return (f"document.querySelector('#search-results')?.dataset.state === 'ready'"
            f" && document.querySelectorAll('#search-results .search-row').length === {rows}")


@pytest.mark.parametrize("width", [390, 1280])
def test_search_finds_a_word_filters_by_section_and_opens_the_record(browser, live, width):
    """Лупа → «Поиск»; «кот» находит картинку (промпт «кот 0») и расшифровку («Кот в сапогах») с выделенным
    словом; чип «Разговоры» сужает выдачу; слова — не в адресе; вернувшись, поиск на месте; нажатие на
    картинку открывает её просмотр в «Фото»."""
    login = f"searcher{width}"
    uid = live.add_user(login)
    live_sql(live, "INSERT INTO transcripts(id, user_id, model, name, source_media_id, state, detail, created,"
                   " finished) VALUES (?, ?, 'whisperx', 'Кот в сапогах.m4a', 'nope', 'error', 'не вышло', ?, ?)",
             (f"tr-find{width}", uid, time.time() - 600, time.time() - 590))
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), make_images(1),
        ("eval", "placed", SEARCH_PLACED),
        ("click", "#search-open"), ("wait", "location.hash === '#/search' && document.querySelector('#search-q')"),
        ("eval", "current", "document.querySelector('#search-open').getAttribute('aria-current')"),
        ("eval", "idle", "document.querySelector('#search-status').textContent"),
        ("type", "#search-q", "кот"), ("wait", search_ready(2)),
        ("eval", "rows", SEARCH_ROWS), ("eval", "hash", "location.hash"),
        ("click", "#view .search-chips .chip[data-section=transcribe]"), ("wait", search_ready(1)),
        ("eval", "narrowed", SEARCH_ROWS),
        ("type", "#search-q", "ы"), ("wait", "document.querySelector('#search-status').textContent === 'Ничего не нашлось'"),
        *go("chat"), ("wait", "document.querySelector('#composer')"),
        *go("search"), ("wait", search_ready(0)),
        ("eval", "kept", "[document.querySelector('#search-q').value,"
                         " document.querySelector('.search-chips [aria-pressed=true]').dataset.section]"),
        ("click", "#view .search-chips .chip[data-section='']"),
        set_value("#search-q", "кот"), ("wait", search_ready(2)),
        ("eval", "overflow", f"!({NO_OVERFLOW})"),
        ("click", "#search-results .search-row[data-section=photo]"),
        ("wait", is_open("#viewer")), ("eval", "opened", "location.hash")],
        width=width, mobile=width == 390)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["placed"] and v["current"] == "page"
    assert v["idle"] == "Введите слова или выберите раздел, модель или период"
    assert v["rows"] == [["photo", "кот 0", ["кот"]], ["transcribe", "Кот в сапогах.m4a", ["Кот"]]]
    assert v["hash"] == "#/search" and v["narrowed"] == [["transcribe", "Кот в сапогах.m4a", ["Кот"]]]
    assert v["kept"] == ["коты", "transcribe"] and not v["overflow"]
    assert v["opened"] == "#/photo"
    assert not problems(r), problems(r)


def test_search_more_right_after_typing_starts_the_new_query(browser, live):
    """«Показать ещё» в паузе после ввода: новый запрос ещё не ушёл — кнопка начинает его заново, а не
    дописывает его страницу курсором прежнего к прежнему списку."""
    login = "search-more"
    uid = live.add_user(login)
    for word, at in (("кот", time.time() - 7200), ("пёс", time.time() - 3600)):
        live_sql(live, "WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i + 1 FROM n WHERE i < 30)"
                       " INSERT INTO images(id, user_id, kind, model, prompt, size, seed, state, detail, created)"
                       " SELECT ? || i, ?, 'generation', 'flux', ? || ' ' || i, '1024x1024', 1, 'done', 'готово', ? + i"
                       " FROM n", (f"{word}-{uid}-", uid, word, at))
    titles = "[...document.querySelectorAll('#search-results .search-title')].map((t) => t.textContent)"
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        *go("search"), ("wait", "document.querySelector('#search-q')"),
        set_value("#search-q", "кот"),
        ("wait", f"{search_ready(30)} && !document.querySelector('#search-more').hidden"),
        ("eval", "typed", "(() => { const q = document.querySelector('#search-q'); q.value = 'пёс';"
                          " q.dispatchEvent(new Event('input', {bubbles: true}));"
                          " document.querySelector('#search-more').click();"
                          " return document.querySelector('#search-results').dataset.state; })()"),
        ("wait", "document.querySelector('#search-results').dataset.state === 'ready'"),
        ("eval", "titles", settled(titles, 500))])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["typed"] == "loading"
    assert len(v["titles"]) == 30 and all(t.startswith("пёс ") for t in v["titles"]), v["titles"]
    assert not problems(r), problems(r)


# ---- перевод ---------------------------------------------------------------------

TL_OUTPUT = "document.querySelector('#tl-output').textContent"
TL_DONE = "document.querySelector('.tl-row[data-state=done]')"
TL_LANGS = "[document.querySelector('#tl-source').value, document.querySelector('#tl-target').value]"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def add_translator():
    """Шлюз в чате, «Сканах» и «Переводе»; в переводчике отмечены локальная и облачная модели."""
    return ("eval", "gateway", f"""(async () => {{ const m = {API};
      const c = await m.api('POST', '/api/connections', {{ name: 'Шлюз', kind: 'srvllm', base_url: {GATEWAY_URL!r},
        token: {GW_TOKEN!r}, sections: ['chat', 'ocr', 'translate'] }});
      await m.api('PUT', '/api/sections/translate/links/' + c.id, {{ models: ['local-gemma-abl', 'gemma4-abl'] }});
      await m.loadMe(); return true; }})()""")


def translating(login):
    """Вход, шлюз, раздел «Перевод» с загруженными моделями и языками."""
    return [*signin(login), ("wait", READY), add_translator(), *go("translate"),
            ("wait", "document.querySelectorAll('#tl-model option').length === 2"
                     " && document.querySelectorAll('#tl-target option').length > 10")]


@pytest.fixture
def translator(live):
    """Шлюз переводчика: определение языка (ответ без потока) — «en», переводы — `chat_script` по порядку."""
    gw = live.gw
    saved = gw.chat_reply, gw.chat_script
    gw.chat_reply, gw.chat_script = ["en"], []
    try:
        yield gw
    finally:
        gw.chat_reply, gw.chat_script = saved
        gw.__dict__.pop("_chat", None)


@pytest.mark.parametrize("width", [320, 1280])
def test_translate_text_goes_right_and_into_history(browser, live, translator, width):
    """«Hello» → «Перевести»: справа «Привет», строка истории «EN → RU», слева «Определить язык · английский».
    Модель — умолчание раздела (локальная — без пометки, облачная — « · облако»). Широко окна рядом, на 320 px —
    друг под другом и без горизонтальной прокрутки."""
    login = f"tl-text{width}"
    live.add_user(login)
    translator.chat_script = [["При", "вет"]]
    r = browser.drive(live.url + "/", [*translating(login),
        ("eval", "models", "[...document.querySelectorAll('#tl-model option')].map(o => o.textContent)"),
        ("eval", "model", "document.querySelector('#tl-model').selectedOptions[0].textContent"),
        ("eval", "langs", TL_LANGS),
        ("eval", "swap", "document.querySelector('#tl-swap').disabled"),
        ("type", "#tl-input", "Hello"),
        ("eval", "count", "document.querySelector('#tl-count').textContent"),
        ("click", "#tl-go"),
        ("wait", f"{TL_DONE} && {TL_OUTPUT} === 'Привет'"),
        ("eval", "pair", f"{TL_DONE}.querySelector('.tl-pair').textContent"),
        ("eval", "row", f"[{TL_DONE}.querySelector('.tl-from').textContent, {TL_DONE}.querySelector('.tl-to').textContent,"
                        f" {TL_DONE}.getAttribute('aria-current')]"),
        ("eval", "auto", "document.querySelector('#tl-source').selectedOptions[0].textContent"),
        ("eval", "placed", """(() => { const l = document.querySelector('.tl-left').getBoundingClientRect();
          const r = document.querySelector('.tl-right').getBoundingClientRect();
          return [l.bottom <= r.top + 0.5, l.right <= r.left + 0.5]; })()"""),
        ("eval", "fits", NO_OVERFLOW)],
        width=width, mobile=width < 760)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["models"] == ["Шлюз · local-gemma-abl", "Шлюз · gemma4-abl · облако"]
    assert v["model"] == "Шлюз · local-gemma-abl" and v["langs"] == ["auto", "ru"] and v["swap"] is True
    assert v["count"] == "5 / 300 000"
    assert v["pair"] == "EN → RU" and v["row"] == ["Hello", "Привет", "true"]
    assert v["auto"] == "Определить язык · английский"
    assert v["placed"] == ([True, False] if width == 320 else [False, True])
    assert v["fits"] is True
    sent = [b for b in translator.chat_requests if b.get("stream")][-1]
    assert sent["model"] == "local-gemma-abl" and sent["messages"][-1]["content"] == "Hello"
    assert not problems(r), problems(r)


def test_translate_swap_uses_the_detected_language(browser, live, translator):
    """Облачная модель из шапки — следующий перевод идёт через неё. После перевода с «Определить язык» ⇄ берёт
    определённый язык: «русский → английский», окна меняются текстом. Пара по умолчанию при следующем входе в
    раздел — языки последней записи, а не выбор, который не перевели."""
    live.add_user("tl-swap")
    translator.chat_script = [["Привет"]]
    pick_cloud = """(() => { const s = document.querySelector('#tl-model');
      const o = [...s.options].find(o => o.textContent.endsWith(' · облако')); s.value = o.value;
      s.dispatchEvent(new Event('change', { bubbles: true })); return o.textContent; })()"""
    r = browser.drive(live.url + "/", [*translating("tl-swap"),
        ("eval", "cloud", pick_cloud),
        ("type", "#tl-input", "Hello"), ("click", "#tl-go"),
        ("wait", f"{TL_DONE} && {TL_OUTPUT} === 'Привет'"),
        ("eval", "before", "document.querySelector('#tl-swap').disabled"),
        ("click", "#tl-swap"),
        ("eval", "after", f"[...{TL_LANGS}, document.querySelector('#tl-input').value, {TL_OUTPUT}]"),
        ("click", "#tl-swap"),                                            # второй ⇄ — тексты обратно вместе с языками
        ("eval", "back", f"[...{TL_LANGS}, document.querySelector('#tl-input').value, {TL_OUTPUT}]"),
        set_value("#tl-target", "de"),
        *go("chat"), ("wait", "document.querySelector('#chat-input')"),
        *go("translate"), ("wait", f"{TL_DONE} && document.querySelectorAll('#tl-target option').length > 10"),
        ("wait", f"{TL_LANGS}.join() === 'auto,ru'"),
        ("eval", "again", TL_LANGS)])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["cloud"] == "Шлюз · gemma4-abl · облако"
    assert v["before"] is False
    assert v["after"] == ["ru", "en", "Привет", "Hello"]
    assert v["back"] == ["en", "ru", "Hello", "Привет"]
    assert v["again"] == ["auto", "ru"]
    assert [b["model"] for b in translator.chat_requests[-2:]] == ["gemma4-abl", "gemma4-abl"]   # язык и перевод
    assert not problems(r), problems(r)


def test_translate_file_shows_source_and_translation(browser, live, translator):
    """Word в «Файл» — плашка с именем вместо поля; «Перевести» — слева текст документа и ссылка на исходник,
    справа перевод; «Скачать ▾» — .txt и .docx перевода; крестик плашки — снова пустое поле."""
    from test_documents import docx
    live.add_user("tl-file")
    translator.chat_script = [["Привет, мир."]]
    r = browser.drive(live.url + "/", [*translating("tl-file"),
        put_files("#tl-file-input", [("Записка.docx", docx(["Hello world."]))], DOCX_MIME),
        ("wait", "!document.querySelector('#tl-chip').hidden"),
        ("eval", "chosen", "[document.querySelector('#tl-chip').textContent,"
                           " document.querySelector('#tl-input').closest('.field-box').hidden]"),
        ("click", "#tl-go"),
        ("wait", f"{TL_DONE} && {TL_OUTPUT} === 'Привет, мир.'"),
        ("eval", "tid", f"{TL_DONE}.dataset.id"),
        ("eval", "left", "[document.querySelector('.tl-source-text').textContent,"
                         " document.querySelector('#tl-chip a').getAttribute('href').startsWith('/api/media/'),"
                         " document.querySelector('#tl-chip a').textContent]"),
        ("eval", "kind", f"{TL_DONE}.querySelector('.tl-meta').textContent.includes('Word')"),
        ("eval", "spy", CLICKS), ("click", "#tl-export"),
        ("eval", "formats", "[...document.querySelectorAll('.tl-formats [data-format]')].map(b => b.dataset.format)"),
        ("click", ".tl-formats [data-format=txt]"),
        ("wait", "window.clicked.length === 1"), ("eval", "clicked", "window.clicked[0]"),
        ("eval", "body", f"fetch('/api/translate/' + {TL_DONE}.dataset.id + '/export?format=txt').then(r => r.text())"),
        ("click", "#tl-chip-x"),
        ("eval", "cleared", f"[document.querySelector('#tl-chip').hidden, document.querySelector('#tl-input').value,"
                            f" {TL_OUTPUT}]")])
    assert not r.get("failed"), r
    v = r["values"]
    assert "Записка.docx" in v["chosen"][0] and v["chosen"][1] is True
    assert v["left"] == ["Hello world.", True, "Записка.docx"] and v["kind"] is True
    assert v["formats"] == ["txt", "docx"]
    assert v["clicked"] == [f"/api/translate/{v['tid']}/export?format=txt", True]
    assert v["body"] == "Привет, мир."
    assert v["cleared"] == [True, "", ""]
    assert not problems(r), problems(r)


def test_translate_continue_after_an_error(browser, live, translator):
    """Второй кусок — отказ шлюза 400: запись — ошибка «кусок 2 из 2», первый кусок виден, «Продолжить» —
    переводится только второй, готово."""
    from fakes import FakeGateway
    live.add_user("tl-cont")
    gw = translator
    gw.chat_script = [["Первый"], ["Второй"]]
    real, streams = FakeGateway._chat.__get__(gw), []

    def failing(request):
        body = json.loads(request.content)
        if body.get("stream"):
            streams.append(body)
            if len(streams) == 2:
                return httpx.Response(400, json={"error": {"message": "bad request"}})
        return real(request)

    gw._chat = failing
    paragraph = ("Hello world. " * 160).strip()
    r = browser.drive(live.url + "/", [*translating("tl-cont"),
        set_value("#tl-source", "en"),
        set_value("#tl-input", f"{paragraph}\n\n{paragraph}"),
        ("click", "#tl-go"),
        ("wait", "document.querySelector('.tl-row[data-state=error]') && !document.querySelector('#tl-continue').hidden"),
        ("eval", "failed", f"[document.querySelector('#tl-status').textContent, {TL_OUTPUT}]"),
        ("click", "#tl-continue"),
        ("wait", f"{TL_DONE} && {TL_OUTPUT} === 'Первый\\n\\nВторой'"),
        ("eval", "after", "[document.querySelector('#tl-continue').hidden, document.querySelector('#tl-status').textContent]")])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["failed"][0].startswith("кусок 2 из 2") and v["failed"][1] == "Первый"
    assert v["after"] == [True, ""]
    assert len(streams) == 3 and streams[2]["messages"][-1]["content"] == streams[1]["messages"][-1]["content"]
    assert not problems(r), problems(r)


def test_translate_continue_without_connection_says_why(browser, live, translator):
    """«Продолжить», когда подключение записи удалено, — 409 `no_connection`: слова под окном, запись на месте
    (перевод, строка истории, «Продолжить»)."""
    from fakes import FakeGateway
    live.add_user("tl-noconn")
    gw = translator
    gw.chat_script = [["Первый"]]
    real, streams = FakeGateway._chat.__get__(gw), []

    def failing(request):
        body = json.loads(request.content)
        if body.get("stream"):
            streams.append(body)
            if len(streams) == 2:
                return httpx.Response(400, json={"error": {"message": "bad request"}})
        return real(request)

    gw._chat = failing
    paragraph = ("Hello world. " * 160).strip()
    drop = f"""(async () => {{ const m = {API};
      const id = (await m.api('GET', '/api/sections')).sections.find(s => s.id === 'translate').links[0].connection.id;
      await m.api('DELETE', '/api/connections/' + id); return true; }})()"""
    r = browser.drive(live.url + "/", [*translating("tl-noconn"),
        set_value("#tl-source", "en"),
        set_value("#tl-input", f"{paragraph}\n\n{paragraph}"),
        ("click", "#tl-go"),
        ("wait", "document.querySelector('.tl-row[data-state=error]') && !document.querySelector('#tl-continue').hidden"),
        ("eval", "drop", drop),
        ("click", "#tl-continue"),
        ("wait", "document.querySelector('#tl-status').textContent.startsWith('Подключение')"),
        ("eval", "after", f"[document.querySelector('#tl-status').textContent,"
                          " document.querySelector('#tl-status').classList.contains('error'),"
                          f" {TL_OUTPUT}, document.querySelector('.tl-row[aria-current=true]')?.dataset.state,"
                          " document.querySelector('#tl-continue').hidden, document.querySelector('#tl-continue').disabled]")])
    assert not r.get("failed"), r
    assert r["values"]["after"] == ["Подключение перевода удалено — переведите заново", True, "Первый", "error",
                                    False, False]
    assert len(streams) == 2                                         # «Продолжить» модель не звал
    assert not [p for p in problems(r) if "409 (Conflict)" not in p or not p.endswith("/continue)")], problems(r)


def test_translate_running_record_grows_in_place_and_stops(browser, live, translator):
    """Идущая запись: справа — готовый кусок и `live` идущего, слова состояния; опрос раз в секунду не
    пересобирает строки истории и перевод (фокус на строке остаётся, текстовый узел тот же), aria-live — у
    состояния, не у перевода; сбой опроса — слова, следующий удачный их убирает. Ушли из раздела — опросов нет;
    вернулись и открыли — «Остановить»: «остановлено», готовый кусок на месте, «Продолжить»."""
    import state
    live.add_user("tl-stop")
    gw = translator
    gw.chat_script = [["Первый"], ["Вто", "рой"]]
    base = sum(1 for b in gw.chat_requests if b.get("stream"))
    gw.chat_hold = lambda: (sum(1 for b in gw.chat_requests if b.get("stream")) >= base + 2
                            and state.DB.execute("SELECT 1 FROM translations t JOIN users u ON u.id = t.user_id"
                                                 " WHERE u.login = 'tl-stop' AND t.state = 'cancelling'").fetchone() is None)
    spy = r"""(() => { window.polls = 0; window.lists = 0; window.failPolls = 0; const real = window.fetch;
      window.fetch = (url, opts) => {
        if (/\/api\/translate\/[^/?]+\?from=/.test(url)) {
          window.polls += 1;
          if (window.failPolls > 0) { window.failPolls -= 1; return Promise.reject(new TypeError('Failed to fetch')); }
        }
        if (/\/api\/translate\?limit=/.test(url)) window.lists += 1;
        return real(url, opts); };
      return true; })()"""
    growing = f"{TL_OUTPUT} === 'Первый\\n\\nВто' && !document.querySelector('#tl-stop').hidden"
    paragraph = ("Hello world. " * 160).strip()
    try:
        r = browser.drive(live.url + "/", [*translating("tl-stop"),
            ("eval", "spy", spy),
            set_value("#tl-source", "en"),
            set_value("#tl-input", f"{paragraph}\n\n{paragraph}"),
            ("click", "#tl-go"),
            ("wait", growing),
            ("eval", "running", "[document.querySelector('#tl-status').textContent,"
                                " document.querySelector('#tl-output').getAttribute('aria-live'),"
                                " document.querySelector('#tl-status').getAttribute('aria-live'),"
                                " document.querySelector('.tl-row[aria-current=true]').dataset.state]"),
            ("eval", "mark", """(() => { const b = document.querySelector('.tl-row[aria-current=true] .tl-open');
              window.keep = [b, document.querySelector('#tl-output').firstChild, window.polls, window.lists];
              b.focus(); window.t0 = Date.now(); return true; })()"""),
            ("wait", "Date.now() - window.t0 > 3500"),
            ("eval", "kept", """[window.keep[0].isConnected, document.activeElement === window.keep[0],
              window.keep[1] === document.querySelector('#tl-output').firstChild,
              window.polls - window.keep[2] >= 2, window.lists - window.keep[3] >= 1]"""),
            ("eval", "fail", "window.failPolls = 2"),
            ("wait", "document.querySelector('.tl-error').textContent === 'Нет связи с сервером'"),
            ("wait", "window.failPolls === 0 && document.querySelector('.tl-error').textContent === ''"),
            *go("chat"), ("wait", "document.querySelector('#chat-input')"),
            ("eval", "left", "window.left = [window.polls, window.lists, Date.now()]"),
            ("wait", "Date.now() - window.left[2] > 3500"),
            ("eval", "quiet", "[window.polls - window.left[0], window.lists - window.left[1]]"),
            *go("translate"), ("wait", "document.querySelector('.tl-row[data-state=working]')"),
            ("click", ".tl-row[data-state=working] .tl-open"),
            ("wait", growing),
            ("click", "#tl-stop"),
            ("wait", "document.querySelector('.tl-row[data-state=cancelled]')"
                     " && document.querySelector('#tl-status').textContent === 'остановлено'"),
            ("eval", "stopped", f"[{TL_OUTPUT}, document.querySelector('#tl-stop').hidden,"
                                " document.querySelector('#tl-continue').hidden]")])
    finally:
        gw.chat_hold = False
    assert not r.get("failed"), r
    v = r["values"]
    assert v["running"] == ["перевожу · 2 из 2", None, "polite", "working"]
    assert v["kept"] == [True, True, True, True, True]
    assert v["quiet"] == [0, 0]
    assert v["stopped"] == ["Первый", True, False]
    assert not problems(r), problems(r)


def test_translate_waiting_row_keeps_its_node_and_open_clears_poll_error(browser, live, translator, monkeypatch):
    """Запись ждёт карту: «жду карту · м:сс» меняется каждую секунду, а строка истории — тот же узел через два
    обновления ленты (слова — на месте, фокус на строке остаётся). Опрос открытой записи не удался — слова под
    окнами; нажатие на её строку (свежая копия той же записи) их снимает, хоть опрос и не наладился."""
    import translate
    monkeypatch.setattr(translate, "RETRY_SEC", 1)                     # «Остановить» — после паузы
    live.add_user("tl-wait")
    gw = translator
    gw.chat_error = (503, {"error": {"message": "карта занята"}})
    spy = r"""(() => { window.lists = 0; window.failPolls = 0; const real = window.fetch;
      window.fetch = (url, opts) => {
        if (/\/api\/translate\/[^/?]+\?from=/.test(url) && window.failPolls > 0) {
          return Promise.reject(new TypeError('Failed to fetch'));
        }
        if (/\/api\/translate\?limit=/.test(url)) window.lists += 1;
        return real(url, opts); };
      return true; })()"""
    meta = "document.querySelector('.tl-row[aria-current=true] .tl-meta').textContent"
    reopen = """(async () => { document.querySelector('.tl-row[aria-current=true] .tl-open').click();
      for (let i = 0; i < 100; i++) {
        if (document.querySelector('.tl-error').textContent === '') return window.failPolls > 0;
        await new Promise((ok) => setTimeout(ok, 20));
      }
      return false; })()"""
    try:
        r = browser.drive(live.url + "/", [*translating("tl-wait"),
            ("eval", "spy", spy),
            set_value("#tl-source", "en"),
            ("type", "#tl-input", "Hello"), ("click", "#tl-go"),
            ("wait", f"document.querySelector('.tl-row[aria-current=true][data-state=working]')"
                     f" && {meta}.includes('жду карту')"),
            ("eval", "mark", f"""(() => {{ const row = document.querySelector('.tl-row[aria-current=true]');
              window.keep = [row, {meta}, window.lists]; row.querySelector('.tl-open').focus(); return true; }})()"""),
            ("wait", f"window.lists - window.keep[2] >= 2 && {meta} !== window.keep[1]"),
            ("eval", "kept", f"""[window.keep[0] === document.querySelector('.tl-row[aria-current=true]'),
              window.keep[0].isConnected, document.activeElement === window.keep[0].querySelector('.tl-open'),
              {meta}.includes('жду карту')]"""),
            ("eval", "fail", "window.failPolls = 1"),
            ("wait", "document.querySelector('.tl-error').textContent === 'Нет связи с сервером'"),
            ("eval", "reopened", reopen),
            ("eval", "heal", "window.failPolls = 0"),
            ("click", "#tl-stop"),
            ("wait", "document.querySelector('.tl-row[data-state=cancelled]')"
                     " && document.querySelector('#tl-status').textContent === 'остановлено'")])
    finally:
        gw.chat_error = None
    assert not r.get("failed"), r
    v = r["values"]
    assert v["kept"] == [True, True, True, True]
    assert v["reopened"] is True
    assert not problems(r), problems(r)


def finished_record(text="Привет", target="fr"):
    """Запись «Привет» → fr готова (через API, без экрана): window.tid — её id."""
    return ("eval", "tid", f"""(async () => {{ const m = {API};
      const job = await m.api('POST', '/api/translate', {{ text: {text!r}, source_lang: 'ru', target_lang: {target!r} }});
      for (let t = 0; t < 200; t++) {{
        const item = await m.api('GET', '/api/translate/' + job.id);
        if (item.state === 'done') {{ window.tid = job.id; return job.id; }}
        await new Promise((ok) => setTimeout(ok, 50));
      }}
      return null; }})()""")


def test_translate_delete_from_history(browser, live, translator):
    """Крестик строки истории — окно «Удалить перевод?»; «Удалить» — строки нет, показанная запись убрана из окон,
    сервер записи не знает."""
    live.add_user("tl-del")
    translator.chat_script = [["Bonjour"]]
    r = browser.drive(live.url + "/", [*signin("tl-del"), ("wait", READY), add_translator(), finished_record(),
        *go("translate"), ("wait", TL_DONE),
        ("click", ".tl-row[data-state=done] .tl-open"), ("wait", f"{TL_OUTPUT} === 'Bonjour'"),
        ("click", ".tl-row[data-state=done] .tl-delete"),
        ("wait", "document.querySelector('#tl-delete-dialog')?.open"),
        ("eval", "asked", "document.querySelector('#tl-delete-dialog').textContent.includes('Удалить перевод?')"),
        ("click", "#tl-delete-confirm"),
        ("wait", "!document.querySelector('.tl-row') && !document.querySelector('#tl-delete-dialog')?.open"),
        ("eval", "after", f"[{TL_OUTPUT}, document.querySelector('#tl-history').textContent,"
                          " document.querySelector('.tl-acts').hidden]"),
        ("eval", "gone", "fetch('/api/translate/' + window.tid).then(r => r.status)")])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["asked"] is True
    assert v["after"] == ["", "Здесь появятся ваши переводы", True]
    assert v["gone"] == 404
    own = f"404 (Not Found) ({live.url}/api/translate/{v['tid']})"     # сам тест спросил удалённую запись
    assert not [p for p in problems(r) if not p.endswith(own)], problems(r)


def test_translate_delete_window_says_whether_the_scan_stays(browser, live, translator):
    """Ревью: перевод скана и его повтор держат один документ «Сканов» — окно удаления первой записи говорит, что
    скан останется; удалили её — окно оставшейся говорит, что скан удалится вместе с переводом."""
    from scans import scan_pdf
    live.add_user("tl-delscan")
    translator.chat_script = [["Страница."], ["Seite."]]
    repeat = f"""(async () => {{ const m = {API}; window.first = {TL_DONE}.dataset.id;
      const job = await m.api('POST', '/api/translate/' + window.first + '/repeat', {{ source_lang: 'en', target_lang: 'de' }});
      for (let t = 0; t < 200; t++) {{
        if ((await m.api('GET', '/api/translate/' + job.id)).state === 'done') return job.id;
        await new Promise((ok) => setTimeout(ok, 50));
      }}
      return null; }})()"""
    row = "document.querySelector(`.tl-row[data-id='${window.first}']`)"
    notes = "[...document.querySelectorAll('#tl-delete-dialog p:not(.error)')].map(p => p.textContent)"
    r = browser.drive(live.url + "/", [*translating("tl-delscan"),
        put_files("#tl-file-input", [("Скан.pdf", scan_pdf("ink"))], "application/pdf"),
        ("wait", "!document.querySelector('#tl-chip').hidden"),
        ("click", "#tl-go"),
        ("wait", TL_DONE),
        ("eval", "again", repeat),
        *go("chat"), ("wait", "document.querySelector('#chat-input')"),
        *go("translate"), ("wait", "document.querySelectorAll('.tl-row[data-state=done]').length === 2"),
        ("eval", "open", f"{row}.querySelector('.tl-delete').click()"),
        ("wait", "document.querySelector('#tl-delete-dialog')?.open"),
        ("eval", "shared", notes),
        ("click", "#tl-delete-confirm"),
        ("wait", "document.querySelectorAll('.tl-row').length === 1 && !document.querySelector('#tl-delete-dialog')"),
        ("click", ".tl-row .tl-delete"),
        ("wait", "document.querySelector('#tl-delete-dialog')?.open"),
        ("eval", "alone", notes)])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["again"] is not None
    assert v["shared"][-1] == "Распознанный документ останется в «Сканах»: его использует повтор этого перевода."
    assert v["alone"][-1] == "Распознанный документ в «Сканах» удалится вместе с переводом."
    assert not problems(r), problems(r)


def test_translate_without_model_reads_history(browser, live, translator):
    """Раздел без модели: вместо «Перевести», «Файл» и «Фото» — подсказка со ссылкой на настройки; история
    открывается в окнах и скачивается."""
    live.add_user("tl-nomodel")
    translator.chat_script = [["Bonjour"]]
    unlink = f"""(async () => {{ const m = {API};
      const id = (await m.api('GET', '/api/sections')).sections.find(s => s.id === 'translate').links[0].connection.id;
      await m.api('DELETE', '/api/sections/translate/links/' + id); await m.loadMe(); return true; }})()"""
    r = browser.drive(live.url + "/", [*signin("tl-nomodel"), ("wait", READY), add_translator(), finished_record(),
        ("eval", "unlink", unlink),
        *go("translate"), ("wait", f"document.querySelector('#view .hint-bar') && {TL_DONE}"),
        ("eval", "bare", "[!!document.querySelector('#tl-go'), !!document.querySelector('#tl-file-input'),"
                         " !!document.querySelector('#tl-photo-input'),"
                         " document.querySelector('#view .hint-bar a').getAttribute('href')]"),
        ("click", ".tl-row[data-state=done] .tl-open"), ("wait", f"{TL_OUTPUT} === 'Bonjour'"),
        ("eval", "shown", "[document.querySelector('.tl-acts').hidden, document.querySelector('#tl-export').hidden,"
                          " document.querySelector('#tl-continue').hidden]")])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["bare"] == [False, False, False, "#/settings/translate"]
    assert v["shown"] == [False, False, True]
    assert not problems(r), problems(r)


def test_translate_opens_a_record_by_id(browser, live, translator):
    """#/translate?id=<id> («В работе», уведомление, «Поиск»): запись в окнах, её языки, ?id из адреса ушёл;
    адрес записи перевода проходит HREF jobs.js."""
    live.add_user("tl-open")
    translator.chat_script = [["Bonjour"]]
    make = f"""(async () => {{ const m = {API};
      const job = await m.api('POST', '/api/translate', {{ text: 'Привет', source_lang: 'ru', target_lang: 'fr' }});
      for (let t = 0; t < 200; t++) {{
        const item = await m.api('GET', '/api/translate/' + job.id);
        if (item.state === 'done') {{ window.tid = job.id; return job.id; }}
        await new Promise((ok) => setTimeout(ok, 50));
      }}
      return null; }})()"""
    r = browser.drive(live.url + "/", [*signin("tl-open"), ("wait", READY), add_translator(),
        ("eval", "tid", make),
        ("eval", "href", "(async () => (await import('/static/js/jobs.js')).HREF.test('translate?id=a1-b2'))()"),
        ("eval", "go", "location.hash = '#/translate?id=' + window.tid"),
        ("wait", f"{TL_OUTPUT} === 'Bonjour'"),
        ("eval", "shown", f"[location.hash, document.querySelector('#tl-input').value, ...{TL_LANGS},"
                          " document.querySelector('.tl-row[aria-current=true]')?.dataset.id]")])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["tid"] and v["href"] is True
    assert v["shown"] == ["#/translate", "Привет", "ru", "fr", v["tid"]]
    assert not problems(r), problems(r)


#: ответ HunyuanOCR с координатами (доли 0…1000) — две далёкие строки, два блока
TL_SPOTTED = "Exit(93,146),(491,214)Staff only(193,640),(379,706)"
TL_SIGN = (1000, 600, (30, 90, 200))
TL_BLOCKS = "document.querySelectorAll('#tl-photo .tl-block')"
TL_FITTED = (f"{TL_BLOCKS}.length === 2 && document.querySelector('#tl-photo img').naturalWidth > 0"
             f" && parseFloat({TL_BLOCKS}[0].style.fontSize) > 0")


@pytest.fixture
def spotted(live, translator):
    """Снимок: распознавание — TL_SPOTTED, перевод блоков — один нумерованный ответ."""
    before = live.gw.ocr_reply
    live.gw.ocr_reply = {"choices": [{"message": {"content": TL_SPOTTED}, "finish_reason": "stop"}]}
    translator.chat_script = [["[1] Выход\n[2] Только персонал"]]
    try:
        yield translator
    finally:
        live.gw.ocr_reply = before


def photo_translated(login):
    """Вход, «Перевод», снимок «Фото» → «Перевести» — запись готова, слой из двух блоков подогнан."""
    return [*translating(login),
            put_files("#tl-photo-input", [("Вывеска.png", png_of(*TL_SIGN))]),
            ("wait", "!document.querySelector('#tl-chip').hidden"),
            ("click", "#tl-go"),
            ("wait", f"{TL_DONE} && {TL_FITTED}")]


@pytest.mark.parametrize("width", [320, 1280])
def test_translate_photo_overlay_and_text_modes(browser, live, spotted, width):
    """Снимок — по умолчанию «Поверх снимка»: блок перевода в рамке своего исходного блока (left = x1 / 10 %),
    шрифт подогнан под рамку, подложка белая и текст тёмный в обеих темах. «Исходник» прячет слой; «Текстом» —
    слева распознанный текст, справа перевод; переключение не распознаёт заново. На 320 px — без прокрутки вбок."""
    login = f"tl-photo{width}"
    live.add_user(login)
    asked = len(live.gw.ocr_requests)
    block = f"{TL_BLOCKS}[0]"
    r = browser.drive(live.url + "/", [*photo_translated(login),
        ("eval", "blocks", f"[...{TL_BLOCKS}].map(b => [b.textContent, b.style.left, b.style.top, b.style.width])"),
        ("eval", "placed", f"""(() => {{ const i = document.querySelector('#tl-photo img').getBoundingClientRect();
          const b = {block}.getBoundingClientRect();
          return [Math.abs(b.left - (i.left + .093 * i.width)) < 1, Math.abs(b.top - (i.top + .146 * i.height)) < 1,
                  Math.abs(b.width - .398 * i.width) < 1, Math.abs(b.height - .068 * i.height) < 1]; }})()"""),
        ("eval", "fitted", f"""(() => {{ const b = {block}; const size = parseFloat(b.style.fontSize);
          const fits = b.scrollWidth <= b.clientWidth && b.scrollHeight <= b.clientHeight;
          b.style.fontSize = (size + 1) + 'px';
          const bigger = b.scrollWidth <= b.clientWidth && b.scrollHeight <= b.clientHeight;
          b.style.fontSize = size + 'px';
          return [size >= 8, size <= b.clientHeight, fits || size === 8, bigger && size < b.clientHeight]; }})()"""),
        ("eval", "light", f"[getComputedStyle({block}).backgroundColor, getComputedStyle({block}).color]"),
        ("eval", "dark", f"""(() => {{ document.documentElement.dataset.theme = 'dark';
          const s = getComputedStyle({block}); const out = [s.backgroundColor, s.color];
          delete document.documentElement.dataset.theme; return out; }})()"""),
        ("eval", "modes", "[document.querySelector('#tl-mode-overlay').getAttribute('aria-pressed'),"
                          " document.querySelector('#tl-mode-text').getAttribute('aria-pressed'),"
                          " document.querySelector('#tl-output').hidden, document.querySelector('.tl-source-text').hidden,"
                          " document.querySelector('#tl-original').getAttribute('aria-pressed')]"),
        ("eval", "fits", NO_OVERFLOW),
        ("click", "#tl-original"),
        ("eval", "original", "[document.querySelector('#tl-original').getAttribute('aria-pressed'),"
                             " document.querySelector('#tl-photo .tl-layer').hidden,"
                             " !document.querySelector('#tl-photo').hidden]"),
        ("click", "#tl-mode-text"),
        ("eval", "text", f"[{TL_OUTPUT}, document.querySelector('#tl-output').hidden,"
                         " document.querySelector('.tl-source-text').textContent, document.querySelector('.tl-source-text').hidden,"
                         " document.querySelector('#tl-photo').hidden, document.querySelector('#tl-save').closest('[hidden]') !== null]"),
        ("click", "#tl-mode-overlay"),
        ("eval", "back", "[document.querySelector('#tl-output').hidden, document.querySelector('#tl-photo').hidden]")],
        width=width, mobile=width < 760)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["blocks"] == [["Выход", "9.3%", "14.6%", "39.8%"], ["Только персонал", "19.3%", "64%", "18.6%"]]
    assert v["placed"] == [True, True, True, True]
    assert v["fitted"] == [True, True, True, False], v["fitted"]   # наибольший помещающийся кегль
    white, ink = "rgba(255, 255, 255, 0.92)", "rgb(17, 17, 17)"
    assert v["light"] == [white, ink] and v["dark"] == [white, ink]
    assert v["modes"] == ["true", "false", True, True, "false"]
    assert v["fits"] is True
    assert v["original"] == ["true", True, True]
    assert v["text"] == ["Выход\nТолько персонал", False, "Exit\nStaff only", False, True, True]
    assert v["back"] == [True, False]
    assert len(live.gw.ocr_requests) == asked + 1                    # переключение не распознаёт заново
    assert not problems(r), problems(r)


@pytest.mark.parametrize("width", [320, 1280])
def test_translate_photo_save_gives_a_png(browser, live, spotted, width):
    """«Сохранить снимок» — PNG в натуральном размере: снимок, на месте блока — белая подложка с тёмным текстом,
    вне блоков — исходные пиксели; имя «<имя>.<язык>.png». На iPhone — меню «Поделиться» с тем же PNG.
    Кегль блока на PNG подогнан под рамку в натуральном размере, а не перенят с экрана: на 320 px рамка «Только
    персонал» на экране ниже 8 px, а на PNG текст блока нарисован весь и не выходит за рамку."""
    login = f"tl-save{width}"
    live.add_user(login)
    spy = """(() => { window.clicked = []; window.made = []; window.drawn = [];
      HTMLAnchorElement.prototype.click = function () {
        window.clicked.push([this.getAttribute('href').slice(0, 5), this.getAttribute('download')]); };
      const real = URL.createObjectURL; URL.createObjectURL = (b) => { window.made.push(b); return real.call(URL, b); };
      const fill = CanvasRenderingContext2D.prototype.fillText;
      CanvasRenderingContext2D.prototype.fillText = function (text, x, y) {
        window.drawn.push([text, x, y, parseFloat(this.font), this.measureText(text).width, this.canvas.width]);
        return fill.apply(this, arguments); };
      return true; })()"""
    png = """(async () => { const b = window.made[0]; const u = new Uint8Array(await b.arrayBuffer()); let s = '';
      for (const x of u) s += String.fromCharCode(x); return [b.type, btoa(s)]; })()"""
    r = browser.drive(live.url + "/", [*photo_translated(login),
        *go("chat"), ("wait", "document.querySelector('#chat-input')"),          # снова — из истории
        *go("translate"), ("wait", TL_DONE), ("click", ".tl-row[data-state=done] .tl-open"), ("wait", TL_FITTED),
        ("eval", "screen", f"{TL_BLOCKS}[1].clientHeight"),
        ("eval", "spy", spy),
        ("click", "#tl-save"),
        ("wait", "window.clicked.length === 1 && !document.querySelector('#tl-save').disabled"),
        ("eval", "clicked", "window.clicked[0]"),
        ("eval", "drawn", "window.drawn"),
        ("eval", "png", png),
        iphone(),
        ("click", "#tl-save"),
        ("wait", "window.shared.length === 1"),
        ("eval", "shared", "window.shared[0]")],
        width=width, mobile=width < 760)
    assert not r.get("failed"), r
    v = r["values"]
    if width == 320:
        assert v["screen"] < 16, v["screen"]                         # на экране блок мельче, чем в PNG
    # Каждая строка, что canvas нарисовал в рамке блока (по вертикали — с запасом на обрезанные строки ниже
    # рамки): текст блока целиком, кегль не меньше 8, строка не выходит за рамку ни вправо, ни вниз.
    width_px, height_px = TL_SIGN[:2]
    for text, (x1, y1, x2, y2) in [("Выход", (93, 146, 491, 214)), ("Только персонал", (193, 640, 379, 706))]:
        left, top = x1 * width_px / 1000, y1 * height_px / 1000
        right, bottom = x2 * width_px / 1000, y2 * height_px / 1000
        lines = [d for d in v["drawn"] if d[5] == width_px and left <= d[1] < right
                 and top <= d[2] < bottom + 2 * (bottom - top)]
        assert " ".join(d[0] for d in lines) == text, (text, lines)
        for line, x, y, size, measured, _ in lines:
            assert size >= 8 and x + measured <= right + 0.5 and y + size <= bottom + 0.5, (text, lines)
    assert v["clicked"] == ["blob:", "Вывеска.ru.png"]
    assert v["png"][0] == "image/png"
    with Image.open(io.BytesIO(base64.b64decode(v["png"][1]))) as im:
        rgb = im.convert("RGB")
        assert rgb.size == TL_SIGN[:2]
        assert rgb.getpixel((10, 10)) == TL_SIGN[2]                  # вне блоков — снимок как есть
        assert all(c > 225 for c in rgb.getpixel((95, 89)))           # угол рамки «Выход» — подложка
        assert all(c > 225 for c in rgb.getpixel((489, 127)))
        assert rgb.getpixel((88, 140)) == TL_SIGN[2]                  # рядом, но вне рамки
        darkest = min(sum(rgb.getpixel((x, y))) for x in range(93, 491, 3) for y in range(88, 128, 2))
        assert darkest < 200                                         # текст перевода нарисован
    assert v["shared"] == [[".png", "image/png", True]]
    assert not problems(r), problems(r)


# ---- Claude Code ----

def wait_for(cond, sec=5.0):
    deadline = time.monotonic() + sec
    while not cond():
        assert time.monotonic() < deadline, "не дождался"
        time.sleep(0.02)


def test_socket_bridge_comes_online(live):
    """Подставной мост через настоящий WebSocket uvicorn: привязка, знакомство, конец связи."""
    import bridge
    from fake_bridge import FakeMachine, SocketBridge
    r = httpx.post(f"{live.url}/api/agent/enroll", json={"login": "owner", "password": "password-123",
                                                          "name": "Сетевой ПК"}, timeout=10)
    assert r.status_code == 201, r.text
    aid, key = r.json()["agent_id"], r.json()["device_key"]
    machine = FakeMachine(pin="1234")
    sb = SocketBridge(live.url, key, machine).start()
    try:
        assert sb.welcome == {"t": "welcome", "agent_id": aid, "name": "Сетевой ПК", "login": "owner"}
        wait_for(lambda: aid in bridge.LINKS)
        assert bridge.LINKS[aid].hello["has_pin"] is True
    finally:
        sb.stop()
    wait_for(lambda: aid not in bridge.LINKS)


def enroll_live(live, login, name):
    """Привязка моста к учётке `login` живой студии: (agent_id, device_key)."""
    r = httpx.post(f"{live.url}/api/agent/enroll", json={"login": login, "password": "password-123", "name": name},
                   timeout=10)
    assert r.status_code == 201, r.text
    return r.json()["agent_id"], r.json()["device_key"]


AGENT_ROWS = """[...document.querySelectorAll('#agents li[data-id]')].map((li) => [Number(li.dataset.id),
  li.querySelector('.agent-name').textContent, li.querySelector('.agent-state').textContent,
  li.querySelector('.agent-pin') ? li.querySelector('.agent-pin').textContent : null,
  li.querySelector('.agent-revoke').textContent])"""
#: строки истории подключений без времени (оно — первым через « · »)
AGENT_EVENTS = "[...document.querySelectorAll('#agent-events li')].map((li) => li.textContent.split(' · ').slice(1))"


def test_settings_lists_computers_and_revokes_one(browser, live):
    """«Код» → «Компьютеры (Claude Code)»: два компьютера, один на связи с PIN; история подключений по 50
    и «Показать ещё»; «Отозвать» → окно → подтверждение: строка ушла, мост получил 4401, в истории «отозван»."""
    import bridge
    from fake_bridge import FakeMachine, SocketBridge
    login = "computers"
    uid = live.add_user(login)
    for i in range(55):                         # старые события: больше страницы EVENTS_PAGE
        live_sql(live, "INSERT INTO agent_events(user_id, agent_id, agent_name, created, kind, device, ip)"
                       " VALUES (?, 999, 'Старый ПК', ?, 'pin_fail', 'телефон', '10.0.0.9')",
                 (uid, time.time() - 86400 + i))
    home, _ = enroll_live(live, login, "Домашний ПК")
    work, key = enroll_live(live, login, "Рабочий ПК")
    sb = SocketBridge(live.url, key, FakeMachine(pin="1234")).start()
    try:
        wait_for(lambda: work in bridge.LINKS)
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("settings/code"),
            ("wait", "document.querySelectorAll('#agents li[data-id]').length === 2"),
            ("wait", "document.querySelectorAll('#agent-events li').length === 50"),
            ("eval", "title", "document.querySelector('.agents-block h2').textContent"),
            ("eval", "rows", AGENT_ROWS),
            ("eval", "first", AGENT_EVENTS),
            ("eval", "moreText", "document.querySelector('#agent-events-more').textContent"),
            ("eval", "fits", NO_OVERFLOW),
            ("click", "#agent-events-more"),
            ("wait", "document.querySelectorAll('#agent-events li').length === 58"),
            ("eval", "moreHidden", "document.querySelector('#agent-events-more').hidden"),
            ("click", f"#agents li[data-id='{work}'] .agent-revoke"), ("wait", is_open("#agent-revoke-dialog")),
            ("eval", "dialogTitle", "document.querySelector('#agent-revoke-dialog h2').textContent"),
            ("eval", "dialogText", "document.querySelector('#agent-revoke-dialog p').textContent"),
            ("eval", "confirmText", "document.querySelector('#agent-revoke-confirm').textContent"),
            ("eval", "dialogFits", NO_OVERFLOW),
            ("click", "#agent-revoke-confirm"),
            ("wait", "document.querySelectorAll('#agents li[data-id]').length === 1"),
            ("wait", "document.querySelector('#agent-events li')?.textContent.includes('Рабочий ПК · отозван')"
                     " || document.querySelectorAll('#agent-events li')[1]?.textContent.includes('Рабочий ПК · отозван')"),
            ("eval", "closed", "!" + is_open("#agent-revoke-dialog")),
            ("eval", "after", AGENT_ROWS),
            ("eval", "events", AGENT_EVENTS)], width=320, mobile=True)
        wait_for(lambda: sb.close_code is not None)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["title"] == "Компьютеры (Claude Code, Codex и OpenCode)"
    assert v["rows"] == [[home, "Домашний ПК", "не на связи", None, "Отозвать"],
                         [work, "Рабочий ПК", "на связи", "PIN", "Отозвать"]]
    assert v["first"][:3] == [["Рабочий ПК", "на связи", "127.0.0.1"], ["Рабочий ПК", "привязан", "127.0.0.1"],
                              ["Домашний ПК", "привязан", "127.0.0.1"]]
    assert v["first"][3] == ["Старый ПК", "неверный PIN", "телефон", "10.0.0.9"]
    assert v["moreText"] == "Показать ещё" and v["moreHidden"] is True and v["fits"] is True
    assert v["dialogTitle"] == "Отозвать компьютер?" and v["confirmText"] == "Отозвать"
    assert v["dialogText"] == ("Мост на нём отключится и не войдёт снова без нового входа. История подключений"
                               " останется.") and v["dialogFits"] is True
    assert v["closed"] is True and v["after"] == [[home, "Домашний ПК", "не на связи", None, "Отозвать"]]
    revoked = [e for e in v["events"] if e[:2] == ["Рабочий ПК", "отозван"]]
    assert len(revoked) == 1 and re.fullmatch(r"(телефон|браузер) [0-9a-f]{6}", revoked[0][2]), v["events"][:3]
    assert revoked[0][3] == "127.0.0.1"
    assert ["Рабочий ПК", "связь потеряна", "127.0.0.1"] in v["events"][:3]
    assert sb.close_code == 4401 and work not in bridge.LINKS
    assert not problems(r), problems(r)


AGENTS_DOWNLOAD = """(() => { const root = document.querySelector('.settings'), a = document.querySelector('#agent-download');
  const box = a.closest('.agent-download'), nav = root.querySelector('.settings-nav');
  return [root.firstElementChild === box && box.getBoundingClientRect().bottom <= nav.getBoundingClientRect().top,
    a.getAttribute('href'), a.getAttribute('download'), a.textContent, document.querySelectorAll('#agent-download').length]; })()"""


def test_settings_root_has_agent_installer(browser, live, tmp_path):
    """Установщик EXE и подсказка видны над разделами в корне, вложенные настройки сохраняют свои блоки."""
    login = "agents-download"
    live.add_user(login)
    agents_loaded = "document.querySelector('#agents li')"
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("settings"),
        ("eval", "link", AGENTS_DOWNLOAD),
        ("eval", "notice", "document.querySelector('.settings .agent-install-note').textContent"),
        ("eval", "fits", NO_OVERFLOW), ("screenshot", str(tmp_path / "installer-settings-320.png")),
        *go("settings/code"), ("wait", agents_loaded),
        ("eval", "empty", "document.querySelector('#agents li').textContent"),
        ("eval", "nested", "document.querySelector('.settings > .agent-download').hidden && !document.querySelector('.agents-block .agent-download')"),
        *go("settings/other"), ("wait", "document.querySelector('#password-save')"),
        ("eval", "other", "document.querySelector('.settings > .agent-download').hidden"),
        *go("settings"), ("eval", "returned", AGENTS_DOWNLOAD)], width=320, mobile=True)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["link"] == v["returned"] == [True, "/api/agent/download", "studio-agent-setup.exe", "Скачать агент для Windows (.exe)", 1]
    assert v["nested"] and v["other"]
    assert v["notice"] == "Для работы раздела «Агенты» установите и запустите агент на компьютере с Windows."
    assert v["empty"] == "Компьютеров пока нет." and v["fits"] is True
    assert not problems(r), problems(r)

    enroll_live(live, login, "Рабочий ПК")
    r = browser.drive(live.url + "/#/settings", [("wait", READY),
        ("wait", "document.querySelector('.settings > .agent-download')"),
        ("eval", "link", AGENTS_DOWNLOAD), ("eval", "fits", NO_OVERFLOW),
        ("screenshot", str(tmp_path / "installer-settings-1280.png"))], width=1280)
    assert not r.get("failed"), r
    assert r["values"]["link"] == [True, "/api/agent/download", "studio-agent-setup.exe", "Скачать агент для Windows (.exe)", 1]
    assert r["values"]["fits"]
    assert not problems(r), problems(r)


def push_keys():
    """Ключи подписки верного вида: точка P-256 и 16 байт auth — base64url без «=»."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    point = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    b64 = lambda data: base64.urlsafe_b64encode(data).rstrip(b"=").decode()     # noqa: E731
    return {"p256dh": b64(point), "auth": b64(os.urandom(16))}


PUSH_ENDPOINT = "https://fcm.googleapis.com/fcm/send/test"


def push_init(keys):
    """Подписка без служб push: subscribe и getSubscription — заглушки (подписка — отметкой в localStorage,
    чтобы её видела и следующая вкладка), вызовы — в window.pushCalls; уведомления — NOTIFY_INIT."""
    return NOTIFY_INIT + f"""(() => {{
  const KEYS = {json.dumps(keys)};
  const ENDPOINT = {PUSH_ENDPOINT!r};
  window.pushCalls = [];
  const sub = () => ({{ endpoint: ENDPOINT, expirationTime: null,
    toJSON: () => ({{ endpoint: ENDPOINT, expirationTime: null, keys: KEYS }}),
    unsubscribe: async () => {{ window.pushCalls.push(['unsubscribe']); localStorage.removeItem('test-push-sub');
      return true; }} }});
  if (window.PushManager) {{
    PushManager.prototype.subscribe = async function (options) {{
      const bytes = new Uint8Array(options.applicationServerKey);
      window.pushCalls.push(['subscribe', options.userVisibleOnly,
        btoa(String.fromCharCode(...bytes)).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '')]);
      localStorage.setItem('test-push-sub', '1');
      return sub(); }};
    PushManager.prototype.getSubscription = async function () {{
      return localStorage.getItem('test-push-sub') ? sub() : null; }};
  }}
}})();"""


def push_rows(live, uid):
    import db
    conn = db.connect(live.db_path)
    try:
        return [tuple(r) for r in conn.execute(
            "SELECT endpoint, p256dh, auth, digest IN (SELECT digest FROM sessions WHERE user_id = ?) FROM push_subs"
            " WHERE user_id = ?", (uid, uid))]
    finally:
        conn.close()


PUSH_READY = "document.querySelector('#push-enabled') && !document.querySelector('#push-enabled').disabled"


def test_settings_push_toggle_subscribes_this_device(browser, live):
    """«Уведомления» → «Push от Claude Code на этом устройстве»: включение — подписка с ключом VAPID сервера и
    строка push_subs у сессии, флаг устройства studio-push; новая вкладка видит флажок отмеченным; выключение —
    unsubscribe, строки нет, флага нет."""
    login = "pusher"
    uid = live.add_user(login)
    keys = push_keys()
    init = push_init(keys)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("settings/other"), ("wait", PUSH_READY),
        ("eval", "label", "document.querySelector('#push-enabled').closest('label').textContent"),
        ("eval", "hint", "document.querySelector('#push-hint').textContent"),
        ("eval", "before", "document.querySelector('#push-enabled').checked"),
        ("click", "#push-enabled"),
        ("wait", "localStorage.getItem('studio-push') === '1'"),
        ("eval", "status", "document.querySelector('#push-status').textContent"),
        ("eval", "calls", "window.pushCalls"),
        ("eval", "server", f"(async () => (await {API}.api('GET', '/api/me/push')))()"),
        ("eval", "fits", NO_OVERFLOW)], width=320, mobile=True, init=init)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["label"] == "Push на этом устройстве"
    assert v["hint"] == ("Приходят и при закрытой студии и погасшем экране: запрос разрешения, вопрос и конец хода"
                         " Claude Code, Codex или OpenCode, готовность и ошибки фоновых заданий."
                         " Идут через серверы Apple или Google — содержимое зашифровано, но факт и время им видны."
                         " На iPhone — только у студии, добавленной на экран «Домой».")
    assert v["before"] is False and v["status"] == "Push включён на этом устройстве" and v["fits"] is True
    assert v["calls"] == [["subscribe", True, v["server"]["key"]]] and v["server"]["subscribed"] is True
    assert push_rows(live, uid) == [(PUSH_ENDPOINT, keys["p256dh"], keys["auth"], 1)]
    assert not problems(r), problems(r)

    r = browser.drive(live.url + "/#/settings/other", [("wait", READY), ("wait", PUSH_READY),
        ("eval", "checked", "document.querySelector('#push-enabled').checked"),
        ("click", "#push-enabled"),
        ("wait", "localStorage.getItem('studio-push') === null"),
        ("wait", "document.querySelector('#push-status').textContent !== ''"),
        ("eval", "status", "document.querySelector('#push-status').textContent"),
        ("eval", "calls", "window.pushCalls"),
        ("eval", "server", f"(async () => (await {API}.api('GET', '/api/me/push')).subscribed)()")],
        width=320, mobile=True, init=init)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["checked"] is True and v["status"] == "Push выключен на этом устройстве"
    assert v["calls"] == [["unsubscribe"]] and v["server"] is False
    assert push_rows(live, uid) == []
    assert not problems(r), problems(r)


#: служебный работник не зарегистрирован (частный режим Firefox): ready не разрешается никогда; срок PUSH_READY_SEC
#: (10 с) — 200 мс
PUSH_NO_WORKER = NOTIFY_INIT + """(() => {
  Object.defineProperty(ServiceWorkerContainer.prototype, 'ready', { configurable: true, get: () => new Promise(() => {}) });
  const real = window.setTimeout.bind(window);
  window.setTimeout = (f, ms, ...a) => real(f, ms === 10000 ? 200 : ms, ...a);
})();"""


def test_push_checkbox_gives_up_without_service_worker(browser, live):
    """Служебного работника нет — включение push не висит: через PUSH_READY_SEC слова, флажок снят и снова
    нажимается, подписки нет ни у сервера, ни во флаге устройства."""
    login = "push-no-worker"
    uid = live.add_user(login)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("settings/other"), ("wait", PUSH_READY),
        ("click", "#push-enabled"),
        ("wait", "document.querySelector('#push-status').textContent !== ''"),
        ("eval", "status", "document.querySelector('#push-status').textContent"),
        ("eval", "box", "(b => [b.checked, b.disabled])(document.querySelector('#push-enabled'))"),
        ("eval", "flag", "localStorage.getItem('studio-push')")], width=320, mobile=True, init=PUSH_NO_WORKER)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["status"] == "Фоновая служба студии в этом браузере не запустилась (частный режим?) — push здесь недоступен"
    assert v["box"] == [False, False] and v["flag"] is None
    assert push_rows(live, uid) == []
    assert not problems(r), problems(r)


# раздел «Код» (code.js, code-panel.js, code-sessions.js): панель компьютеров и проектов, PIN, сессии проекта

CODE_PIN = "2580"
#: строки компьютеров: id, имя, «на связи» / «не на связи», значок «PIN» или None
CODE_AGENTS = """[...document.querySelectorAll('#code-agents li[data-id]')].map((li) => [Number(li.dataset.id),
  li.querySelector('.code-agent-name').textContent, li.querySelector('.code-agent-state').textContent,
  li.querySelector('.code-agent-pin') ? li.querySelector('.code-agent-pin').textContent : null])"""
CODE_PROJECTS = """[...document.querySelectorAll('#code-projects li[data-pid]')].map((li) => [li.dataset.pid,
  li.querySelector('.code-project-name').textContent])"""
#: строки сессий проекта: id, название, время есть, пометки (.tag по порядку)
CODE_CHATS = """[...document.querySelectorAll('#code-chats li[data-chat]')].map((li) => [li.dataset.chat,
  li.querySelector('.code-chat-title').textContent, !!li.querySelector('.code-chat-time')?.textContent,
  [...li.querySelectorAll('.tag')].map((t) => t.textContent)])"""
CODE_PIN_SHOWN = "!!document.querySelector('#code-panel #code-pin')"
CODE_PLACE = "document.querySelector('#code-place')?.textContent"
CODE_PANEL_OPEN = "document.querySelector('.code-ws')?.classList.contains('panel-open')"
#: запросы страницы к мосту (/api/code/*, кроме рабочего места — оно к мосту не ходит) — в window.codeCalls
CODE_SPY = """(() => { window.codeCalls = []; const real = window.fetch;
  window.fetch = (u, o) => { const s = String(u);
    if (s.startsWith('/api/code/') && !s.startsWith('/api/code/workspace')) window.codeCalls.push([(o && o.method) || 'GET', s]);
    return real(u, o); }; return true; })()"""


def code_machine(chats=None):
    from fake_bridge import FakeMachine
    return FakeMachine(projects={"alpha": {"chats": chats or {}}, "beta": {}}, pin=CODE_PIN)


def code_online(live, login, machine, name="Рабочий ПК"):
    """Компьютер учётки на связи через настоящий WebSocket: (agent_id, SocketBridge)."""
    import bridge
    from fake_bridge import SocketBridge
    # Обычные проекты сценариев — чистые репозитории; отсутствие Git задаётся явно через git=None.
    for project in machine.projects.values():
        project.setdefault("git", {"branch": "main", "upstream": "origin/main", "ahead": 0, "behind": 0, "files": []})
    aid, key = enroll_live(live, login, name)
    sb = SocketBridge(live.url, key, machine).start()
    wait_for(lambda: aid in bridge.LINKS)
    return aid, sb


def code_open(aid, pin=CODE_PIN):
    """«Код» (панель сама открывает компьютер: у него PIN) → PIN в панели → его проекты."""
    return [*go("code"), ("wait", f"document.querySelector(\"#code-agents li[data-id='{aid}']\")"), ("wait", CODE_PIN_SHOWN),
            ("type", "#code-pin-input", pin), ("click", "#code-pin-open"),
            ("wait", "document.querySelectorAll('#code-projects li[data-pid]').length === 2")]


def code_project(aid, project):
    """Проект из панели (на телефоне панель сама открыта, пока проекта нет): плашка пути — его имя."""
    from fake_bridge import pid
    return [("wait", f"document.querySelector(\"#code-projects li[data-pid='{pid(project)}']\")"),
            ("click", f"#code-projects li[data-pid='{pid(project)}'] a"),
            ("wait", f"document.querySelector('#code-place .code-place-project')?.textContent === '{project}'")]


@pytest.mark.parametrize("width", [320, 1280])
def test_code_section_lists_computers_and_says_when_there_are_none(browser, live, width):
    """Вкладка «Код» после «Перевода», шапки раздела нет; без компьютеров — панели нет, справа подсказка, как
    подключить; с ними — строки в панели: имя, «на связи» / «не на связи», значок «PIN» только у компьютера с PIN;
    на широком экране панель и правая часть — рядом."""
    login = f"code-list-{width}"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"),
        ("wait", "document.querySelector('#code-empty')"),
        ("eval", "tabs", "[...document.querySelectorAll('#rail .tab')].map((t) => t.dataset.section)"),
        ("eval", "tab", "document.querySelector('#rail .tab[data-section=code]').textContent"),
        ("eval", "current", "document.querySelector('#rail .tab[aria-current=page]').dataset.section"),
        ("eval", "head", "!!document.querySelector('#view .head')"),
        ("eval", "panel", "!!document.querySelector('#code-panel')"),
        ("eval", "empty", "document.querySelector('#code-empty > p').textContent"),
        ("eval", "fits", NO_OVERFLOW)], width=width, mobile=width < 760)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["tabs"] == ["chat", "photo", "video", "speech", "transcribe", "ocr", "translate", "code"]
    assert v["tab"] == "Агенты" and v["current"] == "code" and v["head"] is False and v["panel"] is False
    assert v["empty"] == "Компьютеров пока нет." and v["fits"] is True
    assert not problems(r), problems(r)

    home, _ = enroll_live(live, login, "Домашний-компьютер-с-очень-длинным-именем-без-единого-пробела")
    work, sb = code_online(live, login, code_machine())
    side = """(() => { const p = document.querySelector('#code-panel').getBoundingClientRect();
      const m = document.querySelector('.code-main').getBoundingClientRect();
      return [p.width > 0 && m.width > 0 && p.right <= m.left + 1, m.width > p.width]; })()"""
    try:
        r = browser.drive(live.url + "/#/code", [("wait", READY),
            ("wait", "document.querySelectorAll('#code-agents li[data-id]').length === 2"),
            ("eval", "rows", CODE_AGENTS), ("eval", "gone", "!document.querySelector('#code-empty')"),
            ("eval", "head", "!!document.querySelector('#view .head')"),
            ("eval", "fits", NO_OVERFLOW), *([("eval", "side", side)] if width > 760 else [])],
            width=width, mobile=width < 760)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["rows"] == [[home, "Домашний-компьютер-с-очень-длинным-именем-без-единого-пробела", "не на связи", None],
                         [work, "Рабочий ПК", "на связи", "PIN"]]
    assert v["gone"] is True and v["head"] is False and v["fits"] is True
    if width > 760:
        assert v["side"] == [True, True]
    assert not problems(r), problems(r)


def test_code_empty_offers_agent_download(browser, live):
    """Компьютеров нет: значок, «Компьютеров пока нет.», ссылка на EXE на /api/agent/download
    (download), что нужно и шаги установки (спека 2026-10-28-studio-agent-download-and-window-design, 4.2)."""
    login = "code-download"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"),
        ("wait", "document.querySelector('#code-empty #agent-download')"),
        ("eval", "link", """(() => { const a = document.querySelector('#code-empty #agent-download');
          return [a.tagName, a.getAttribute('href'), a.hasAttribute('download'), a.textContent,
            document.querySelectorAll('#agent-download').length]; })()"""),
        ("eval", "texts", "[...document.querySelectorAll('#code-empty .agent-download p')].map((p) => p.textContent)"),
        ("eval", "badge", "!!document.querySelector('#code-empty .badge svg')"),
        ("eval", "fits", NO_OVERFLOW)], width=320, mobile=True)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["link"] == ["A", "/api/agent/download", True, "Скачать агент для Windows (.exe)", 1]
    assert v["texts"] == ["Для работы раздела «Агенты» установите и запустите агент на компьютере с Windows.",
                          "Установщик EXE, нужны Python 3.11+ и Claude Code, Codex или OpenCode",
                          "Запустите studio-agent-setup.exe. В окне моста войдите логином и паролем студии —"
                          " компьютер появится в разделе «Агенты»."]
    assert v["badge"] is True and v["fits"] is True
    assert not problems(r), problems(r)


def test_code_panel_keeps_computers_while_projects_scroll(browser, live):
    """60 проектов: прокручивается только список проектов в панели — имена компьютеров на месте, #view не двигается."""
    from fake_bridge import FakeMachine
    login = "code-scroll"
    live.add_user(login)
    machine = FakeMachine(projects={f"project-{i:02d}": {} for i in range(60)})
    aid, sb = code_online(live, login, machine)
    enroll_live(live, login, "Второй ПК")
    measure = """(() => { const a = document.querySelector('.code-agent').getBoundingClientRect();
      return [a.top, document.querySelector('#view').scrollTop, document.querySelector('.code-panel-body').scrollTop]; })()"""
    last = """(() => { const li = [...document.querySelectorAll('#code-projects li')].pop().getBoundingClientRect();
      const box = document.querySelector('.code-panel-body').getBoundingClientRect();
      return li.bottom <= box.bottom + 1 && li.top >= box.top; })()"""
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"),
            ("wait", "document.querySelectorAll('#code-projects li[data-pid]').length === 60"),
            ("eval", "before", measure),
            ("eval", "scroll", "(() => { const b = document.querySelector('.code-panel-body'); b.scrollTop = b.scrollHeight;"
                               " return true; })()"),
            ("eval", "after", settled(measure, 100)), ("eval", "last", last),
            ("eval", "agents", "document.querySelectorAll('#code-agents li').length")], width=1280)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["agents"] == 2 and v["last"] is True
    assert v["after"][0] == v["before"][0] and v["after"][1] == v["before"][1] == 0
    assert v["after"][2] > v["before"][2] == 0
    assert not problems(r), problems(r)


def test_code_computer_click_changes_only_the_panel(browser, live):
    """Открыт проект alpha компьютера 1; нажатие на компьютер 2 — в панели его проекты, а адрес и плашка пути те же:
    справа по-прежнему alpha."""
    from fake_bridge import FakeMachine, pid
    login = "code-two"
    live.add_user(login)
    one, sb1 = code_online(live, login, FakeMachine(projects={"alpha": {}, "beta": {}}))
    two, sb2 = code_online(live, login, FakeMachine(projects={"gamma": {}}), name="Второй ПК")
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), *code_project(one, "alpha"),
            ("eval", "here", f"[location.hash, {CODE_PLACE}]"),
            ("click", f"#code-agents li[data-id='{two}'] .code-agent"),
            ("wait", f"document.querySelector(\"#code-projects li[data-pid='{pid('gamma')}']\")"),
            ("eval", "projects", CODE_PROJECTS),
            ("eval", "still", f"[location.hash, {CODE_PLACE}]"),
            ("eval", "picked", "document.querySelector('.code-agent[aria-current=true]').closest('li').dataset.id")],
            width=1280)
    finally:
        sb1.stop()
        sb2.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["here"] == [f"#/code/{one}/{pid('alpha')}", "Рабочий ПК / alpha"]
    assert v["still"] == v["here"] and v["projects"] == [[pid("gamma"), "gamma"]] and v["picked"] == str(two)
    assert not problems(r), problems(r)


def test_code_pin_pad_counts_down_and_opens(browser, live):
    """Компьютер с PIN: при входе в раздел — POST open без PIN, 409 pin_needed — поле PIN в панели (нажатие на имя
    его не повторяет); неверный PIN — «осталось 4 попытки», поле пусто; верный — проекты в панели, адрес — #/code.
    PIN нет ни в хранилищах, ни в адресе."""
    from fake_bridge import pid
    login = "code-pin"
    live.add_user(login)
    machine = code_machine()
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"),
            ("wait", f"document.querySelector(\"#code-agents li[data-id='{aid}']\")"),
            ("click", f"#code-agents li[data-id='{aid}'] .code-agent"), ("wait", CODE_PIN_SHOWN),
            ("eval", "pinTitle", "document.querySelector('#code-pin h2').textContent"),
            ("eval", "pinButton", "document.querySelector('#code-pin-open').textContent"),
            ("eval", "input", """(() => { const i = document.querySelector('#code-pin-input');
              return [i.getAttribute('inputmode'), i.getAttribute('autocomplete')]; })()"""),
            ("eval", "fits", NO_OVERFLOW),
            ("type", "#code-pin-input", "1111"), ("click", "#code-pin-open"),
            ("wait", "document.querySelector('#code-pin-error').textContent !== ''"),
            ("eval", "wrong", "document.querySelector('#code-pin-error').textContent"),
            ("eval", "cleared", "document.querySelector('#code-pin-input').value"),
            ("type", "#code-pin-input", CODE_PIN), ("click", "#code-pin-open"),
            ("wait", "document.querySelectorAll('#code-projects li[data-pid]').length === 2"),
            ("eval", "hash", "location.hash"),
            ("eval", "projects", CODE_PROJECTS),
            ("eval", "stored", f"JSON.stringify([{{ ...localStorage }}, {{ ...sessionStorage }}, location.href])"
                               f".includes('{CODE_PIN}')")], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["pinTitle"] == "PIN компьютера" and v["pinButton"] == "Открыть"
    assert v["input"] == ["numeric", "off"] and v["fits"] is True
    assert v["wrong"] == "Неверный PIN — осталось 4 попытки" and v["cleared"] == ""
    assert v["hash"] == "#/code"
    assert v["projects"] == [[pid("alpha"), "alpha"], [pid("beta"), "beta"]]
    assert v["stored"] is False
    assert [q.get("pin") for q in machine.requests if q["op"] == "open"] == [None, "1111", CODE_PIN]
    assert machine.pin_fails == 0
    assert not known_refusals(problems(r), 403, 409), problems(r)


def test_code_offline_computer_shows_only_its_row(browser, live):
    """Компьютер не на связи: в панели — его строка и слова вместо проектов; нажатие — те же слова, ни одного запроса
    к мосту, ни поля PIN, ни проектов."""
    login = "code-off"
    live.add_user(login)
    aid, _ = enroll_live(live, login, "Спящий ПК")
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("eval", "spy", CODE_SPY), *go("code"),
        ("wait", f"document.querySelector(\"#code-agents li[data-id='{aid}']\")"),
        ("wait", "!!document.querySelector('#code-offline')"),
        ("click", f"#code-agents li[data-id='{aid}'] .code-agent"),
        ("eval", "words", settled("document.querySelector('#code-panel #code-offline')?.textContent")),
        ("eval", "calls", settled("window.codeCalls")),
        ("eval", "rest", f"[{CODE_PIN_SHOWN}, !!document.querySelector('#code-projects'), location.hash]")],
        width=320, mobile=True)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["words"] == "Компьютер не на связи — включите его и запустите мост"
    assert v["calls"] == [] and v["rest"] == [False, False, "#/code"]
    assert not problems(r), problems(r)


def test_code_pin_query_for_offline_computer_says_why(browser, live):
    """Ссылка к PIN (#/code?pin=<aid>), а компьютер не на связи: в панели слова, без поля PIN и запросов к мосту;
    ?pin из адреса уходит."""
    login = "code-pin-off"
    live.add_user(login)
    aid, _ = enroll_live(live, login, "Пропавший ПК")
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("eval", "spy", CODE_SPY), *go(f"code?pin={aid}"),
        ("wait", "!!document.querySelector('#code-panel #code-offline')"
                 f" && !!document.querySelector(\"#code-agents li[data-id='{aid}']\")"),
        ("eval", "words", "document.querySelector('#code-offline').textContent"),
        ("eval", "rest", f"[{CODE_PIN_SHOWN}, location.hash, window.codeCalls, {CODE_PANEL_OPEN}]")], width=320, mobile=True)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["words"] == "Компьютер не на связи — включите его и запустите мост"
    assert v["rest"] == [False, "#/code", [], True]
    assert not problems(r), problems(r)


def test_code_pin_field_is_not_a_password(browser, live):
    """Поле PIN — не форма входа: без <form> и type=password (менеджер паролей не предложит сохранить PIN), поле
    text с точками (-webkit-text-security), autocomplete=off, цифровая клавиатура; Enter открывает компьютер."""
    login = "code-pin-text"
    live.add_user(login)
    machine = code_machine()
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"),
            ("wait", f"document.querySelector(\"#code-agents li[data-id='{aid}']\")"),
            ("click", f"#code-agents li[data-id='{aid}'] .code-agent"), ("wait", CODE_PIN_SHOWN),
            ("eval", "field", """(() => { const box = document.querySelector('#code-pin');
              const i = document.querySelector('#code-pin-input');
              return [box.tagName, !!box.closest('form') || !!box.querySelector('form'),
                !!box.querySelector('input[type=password]'), i.type, getComputedStyle(i).webkitTextSecurity,
                i.getAttribute('autocomplete'), i.getAttribute('inputmode')]; })()"""),
            ("type", "#code-pin-input", CODE_PIN), ("press", "Enter"),
            ("wait", "document.querySelectorAll('#code-projects li[data-pid]').length === 2"),
            ("eval", "hash", "location.hash")], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["field"] == ["DIV", False, False, "text", "disc", "off", "numeric"]
    assert v["hash"] == "#/code"
    assert [q.get("pin") for q in code_requests(machine, "open")] == [None, CODE_PIN]
    assert not known_refusals(problems(r), 409), problems(r)


def test_code_pin_leaves_before_the_projects_answer(browser, live):
    """Компьютер открыт PIN-ом, а список проектов отвечает медленно: поля PIN с доступной «Открыть» в панели уже нет,
    списка ещё нет; список — по ответу."""
    login = "code-frame"
    live.add_user(login)
    aid, sb = code_online(live, login, code_machine())
    slow = """(() => { const real = window.fetch; window.fetch = (u, o) => String(u).endsWith('/projects')
      ? real(u, o).then((r) => new Promise((ok) => setTimeout(() => ok(r), 1500))) : real(u, o); return true; })()"""
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("eval", "slow", slow), *go("code"),
            ("wait", f"document.querySelector(\"#code-agents li[data-id='{aid}']\")"), ("wait", CODE_PIN_SHOWN),
            ("type", "#code-pin-input", CODE_PIN), ("click", "#code-pin-open"),
            ("wait", f"!{CODE_PIN_SHOWN}"),
            ("eval", "early", "[!!document.querySelector('#code-projects'), !!document.querySelector('#code-agents')]"),
            ("wait", "document.querySelectorAll('#code-projects li[data-pid]').length === 2"),
            ("eval", "hash", "location.hash")], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["early"] == [False, True] and v["hash"] == "#/code"
    assert not known_refusals(problems(r), 409), problems(r)


def test_code_closed_shows_the_pin_in_the_panel_and_returns(browser, live):
    """Сессия закрыта на машине (блокировка студии, перезапуск моста), а владелец переходит в другой проект: отказ
    closed — поле PIN этого компьютера в панели (телефон — панель открыта), адрес — тот же; верный PIN — тот же
    проект на экране, панель спрятана. Записей в истории — только переход в проект; open с PIN после отказа — один."""
    from fake_bridge import pid
    login = "code-closed-pin"
    live.add_user(login)
    machine = code_machine()
    aid, sb = code_online(live, login, machine)
    beta = f"#/code/{aid}/{pid('beta')}"

    def close_sessions():
        wait_for(lambda: code_requests(machine, "chats"), 60)
        machine.open_sessions.clear()
        live_sql(live, "UPDATE agents SET name = ? WHERE id = ?", ("Закрытый ПК", aid))

    closer = threading.Thread(target=close_sessions, daemon=True)
    closer.start()
    named = "fetch('/api/agents').then((r) => r.json()).then((j) => j.items.some((a) => a.name === 'Закрытый ПК'))"
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_open(aid), *code_project(aid, "alpha"),
            ("wait", "!!document.querySelector('#code-chats')"), ("wait", named),
            ("eval", "len0", "history.length"),
            ("click", "#code-panel-toggle"), ("wait", CODE_PANEL_OPEN),
            ("click", f"#code-projects li[data-pid='{pid('beta')}'] a"),
            ("wait", CODE_PIN_SHOWN),
            ("eval", "pin", f"[location.hash, document.querySelector('#code-pin').dataset.id, {CODE_PANEL_OPEN}]"),
            ("type", "#code-pin-input", CODE_PIN), ("click", "#code-pin-open"),
            ("wait", f"location.hash === '{beta}' && !!document.querySelector('#code-chats')"
                     f" && !{CODE_PIN_SHOWN} && !{CODE_PANEL_OPEN}"),
            ("eval", "place", "document.querySelector('#code-place .code-place-project').textContent"),
            ("eval", "len1", "history.length")], width=320, mobile=True)
        closer.join(5)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["pin"] == [beta, str(aid), True] and v["place"] == "beta"
    assert v["len1"] == v["len0"] + 1                 # только переход в проект
    assert [q.get("pin") for q in code_requests(machine, "open")] == [None, CODE_PIN, CODE_PIN]
    chats = code_requests(machine, "chats")
    assert [q["project"] for q in chats] == [pid("alpha"), pid("beta"), pid("beta")]   # отказ closed, затем — после PIN
    assert not known_refusals(problems(r), 409), problems(r)


def test_code_restores_the_last_project_after_reload(browser, live):
    """Открыт проект beta; страница заново на #/code — тот же проект (адрес заменой), плашка «Рабочий ПК / beta»."""
    from fake_bridge import pid
    login = "code-restore"
    live.add_user(login)
    machine = code_free_machine()
    machine.projects["beta"] = {}
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), *code_project(aid, "beta"),
            ("wait", "!!document.querySelector('#code-chats')")], width=1280)
        assert not r.get("failed"), r
        r = browser.drive(live.url + "/#/code", [("wait", READY),
            ("wait", f"location.hash === '#/code/{aid}/{pid('beta')}'"
                     " && document.querySelector('#code-place .code-place-project')?.textContent === 'beta'"),
            ("eval", "place", CODE_PLACE), ("eval", "length", "history.length"),
            ("eval", "here", "document.querySelector('#code-projects a[aria-current=page]')?.closest('li').dataset.pid")],
            width=1280)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["place"] == "Рабочий ПК / beta" and v["here"] == pid("beta")
    assert not problems(r), problems(r)


def test_code_restore_with_an_offline_computer(browser, live):
    """Запомненный проект — на компьютере, который сейчас не на связи: раздел открывается, в панели и справа —
    «Компьютер не на связи — включите его и запустите мост», к мосту — ни одного запроса, ошибок нет."""
    import bridge
    from fake_bridge import pid
    login = "code-restore-off"
    live.add_user(login)
    aid, sb = code_online(live, login, code_free_machine())
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), *code_project(aid, "alpha"),
            ("wait", "!!document.querySelector('#code-chats')")], width=1280)
        assert not r.get("failed"), r
    finally:
        sb.stop()
    wait_for(lambda: aid not in bridge.LINKS)
    r = browser.drive(live.url + "/", [("wait", READY), ("eval", "spy", CODE_SPY), *go("code"),
        ("wait", f"location.hash === '#/code/{aid}/{pid('alpha')}'"
                 " && !!document.querySelector('#code-panel #code-offline')"
                 " && document.querySelector('#code-status')?.textContent !== ''"),
        ("eval", "words", "[document.querySelector('#code-offline').textContent,"
                          " document.querySelector('#code-status').textContent]"),
        ("eval", "calls", settled("window.codeCalls")), ("eval", "fits", NO_OVERFLOW)], width=1280)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["words"] == ["Компьютер не на связи — включите его и запустите мост"] * 2
    assert v["calls"] == [] and v["fits"] is True
    assert not problems(r), problems(r)


def test_code_phone_panel_slides_and_hides_after_a_project(browser, live):
    """Телефон: проекта нет — панель открыта сама; выбор компьютера её не закрывает, выбор проекта — закрывает; ☰ —
    открыта; нажатие на фон и Escape — закрыта."""
    login = "code-phone"
    live.add_user(login)
    aid, sb = code_online(live, login, code_free_machine())
    visible = "(() => { const p = document.querySelector('#code-panel').getBoundingClientRect(); return p.left >= 0; })()"
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"),
            ("wait", "document.querySelector('#code-projects li[data-pid]')"),
            ("eval", "first", CODE_PANEL_OPEN), ("eval", "shown", settled(visible, 400)),
            ("click", f"#code-agents li[data-id='{aid}'] .code-agent"), ("eval", "kept", settled(CODE_PANEL_OPEN)),
            ("wait", "document.querySelector('#code-computer-dialog').open"),
            ("eval", "closeInfo", "document.querySelector('#code-computer-dialog').close();true"),
            *code_project(aid, "alpha"), ("eval", "chosen", CODE_PANEL_OPEN),
            ("eval", "toggle", "getComputedStyle(document.querySelector('#code-panel-toggle')).display !== 'none'"),
            ("click", "#code-panel-toggle"), ("wait", CODE_PANEL_OPEN),
            ("eval", "backdrop", "document.querySelector('.code-backdrop').click()"), ("wait", f"!{CODE_PANEL_OPEN}"),
            ("click", "#code-panel-toggle"), ("wait", CODE_PANEL_OPEN),
            ("press", "Escape"), ("wait", f"!{CODE_PANEL_OPEN}"),
            ("eval", "fits", NO_OVERFLOW)], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["first"] is True and v["shown"] is True and v["kept"] is True and v["chosen"] is False
    assert v["toggle"] is True
    assert v["fits"] is True
    assert not problems(r), problems(r)


def test_code_place_is_one_line(browser, live):
    """Длинные имена (проект — 80 знаков, компьютер — 64, предел привязки): плашка пути — одна строка, обрезана
    многоточием (сначала имя компьютера), не заходит под три значка справа; страница не шире экрана."""
    from fake_bridge import FakeMachine
    login = "code-place-long"
    live.add_user(login)
    long_project = "проект-" + "п" * 73
    aid, sb = code_online(live, login, FakeMachine(projects={long_project: {}}), name="Компьютер-" + "к" * 54)
    shape = """(() => { const p = document.querySelector('#code-place'); const r = p.getBoundingClientRect();
      const a = document.querySelector('.code-place-agent');
      return [r.height < 2 * parseFloat(getComputedStyle(p).lineHeight), a.scrollWidth > a.clientWidth,
        r.right <= innerWidth - 130]; })()"""
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"),
            ("wait", "document.querySelector('#code-projects li[data-pid]')"),
            ("click", "#code-projects li[data-pid] a"),
            ("wait", f"document.querySelector('#code-place .code-place-project')?.textContent === '{long_project}'"),
            ("eval", "shape", shape), ("eval", "fits", NO_OVERFLOW)], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert r["values"]["shape"] == [True, True, True] and r["values"]["fits"] is True
    assert not problems(r), problems(r)


def test_codex_new_chat_uses_its_catalog_and_permission_cards(browser, live):
    """Мост с двумя помощниками: новый Codex, его модель и карточка; новая вкладка Claude сохраняет свой каталог."""
    from test_codex import CATALOG
    login = "code-codex"
    live.add_user(login)
    machine = code_machine()
    machine.codex = CATALOG
    machine.script = [{"k": "ask", "kind": "permission", "request_id": "p1", "tool": "Shell",
                       "input": {"command": "git status"}, "always": True},
                      {"k": "text", "text": "Готово Codex"}, {"k": "done", "status": "done"}]
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_open(aid), *code_project(aid, "alpha"),
            ("wait", "!!document.querySelector('#code-new-codex')"),
            ("eval", "starters", "[...document.querySelectorAll('#code-tab-menu .code-new-chat')].map((b) => b.textContent)"),
            ("click", "#code-new-codex"),
            ("wait", "document.querySelector('#code-chat')?.dataset.loaded === '1'"),
            ("eval", "engine", "document.querySelector('.code-tab[aria-selected=true] .code-tab-engine').textContent"),
            ("eval", "nullText", "[...document.querySelector('#code-chat').childNodes].some((n) => n.nodeType === Node.TEXT_NODE && n.textContent.trim() === 'null')"),
            ("eval", "models", "[...document.querySelector('#code-model').options].map((o) => o.value)"),
            ("eval", "pick", "(() => { const m = document.querySelector('#code-model'); m.value = 'codex-test';"
                              " m.dispatchEvent(new Event('change')); const e = document.querySelector('#code-effort');"
                              " e.value = 'ultra'; e.dispatchEvent(new Event('change')); return true; })()"),
            *code_send("Проверь проект"), ("wait", "document.querySelector('.code-ask-yes')"),
            ("eval", "card", "document.querySelector('.code-ask-title').textContent"),
            ("eval", "always", "document.querySelector('.code-ask-always').textContent"),
            ("click", ".code-ask-yes"),
            ("wait", "document.querySelector('#code-stop').hidden && document.querySelector('#code-feed').textContent.includes('Готово Codex')"),
            ("click", "#code-tab-add"), ("wait", "!!document.querySelector('#code-new-chat')"),
            ("click", "#code-new-chat"), ("wait", "document.querySelector('#code-chat')?.dataset.loaded === '1'"
                                              " && document.querySelector('#code-chat').dataset.engine === 'claude'"),
            ("eval", "claude", "[...document.querySelector('#code-model').options].map((o) => o.value)"),
            ("eval", "choice", "document.querySelector('#code-model').value"),
            ("eval", "engines", "[...document.querySelectorAll('#code-tabbar .code-tab-engine')].map((e) => e.textContent)"),
            ("eval", "fits", NO_OVERFLOW)], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["engine"] == "Codex" and v["models"] == ["", "codex-test"]
    assert v["nullText"] is False
    assert v["starters"] == ['Claude', 'Codex'] and v["engines"] == ['Codex', 'Claude Code']
    assert v["card"] == "Codex просит разрешения" and v["always"] == "Да, для этой сессии"
    assert v["claude"] == ["", "opus", "sonnet"] and v["choice"] == "" and v["fits"]
    [turn] = code_requests(machine, "turn")
    assert (turn["engine"], turn["model"], turn["effort"]) == ("codex", "codex-test", "ultra")
    assert code_requests(machine, "answer")[-1]["engine"] == "codex"
    assert not [p for p in problems(r) if "/open)" not in p], problems(r)


def test_code_project_lists_sessions_and_reopens_a_closed_one(browser, live):
    """Проект: «Новый чат» первой строкой, затем сессии в порядке сервера (открытые, затем закрытые) — название (без
    него «Новый чат»), время, откуда сессия, «закрыт», «идёт ход», «ждёт ответа»; кнопок «Закрыть» нет. Нажатие на
    закрытую — PATCH open, затем её чат."""
    from fake_bridge import T0, pid
    login = "code-chats"
    live.add_user(login)
    mine, other, busy, asking = ("11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222",
                                 "33333333-3333-4333-8333-333333333333", "44444444-4444-4444-8444-444444444444")
    machine = code_machine({
        mine: {"title": "Починить вход", "created": T0, "modified": T0 + 40, "tag": "studio", "messages": []},
        other: {"title": None, "created": T0, "modified": T0 + 30, "tag": None, "messages": []},
        busy: {"title": "Сборка", "created": T0, "modified": T0 + 20, "tag": "studio", "messages": []},
        asking: {"title": "Тесты", "created": T0, "modified": T0 + 10, "tag": None, "messages": []}})
    machine.running = {busy: {"asks": {}}, asking: {"asks": {"r1": {}}}}
    aid, sb = code_online(live, login, machine)
    close = (f"(async () => {{ await {API}.api('PATCH', '/api/code/agents/{aid}/projects/{pid('alpha')}/chats/{mine}',"
             " { state: 'closed' }); return true; })()")
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_open(aid), ("eval", "close", close),
            *code_project(aid, "alpha"),
            ("wait", "document.querySelectorAll('#code-chats li[data-chat]').length === 4"),
            ("eval", "hash", "location.hash"),
            ("eval", "first", "!!document.querySelector('#code-chats .code-new-chat')"),
            ("eval", "views", "['#code-tab-chat', '#code-tab-files', '#code-tab-git'].map((s) => document.querySelector(s).textContent)"),
            ("eval", "rows", CODE_CHATS), ("eval", "fits", NO_OVERFLOW),
            ("eval", "buttons", "document.querySelectorAll('#code-chats .code-chat-close, #code-chats .code-chat-reopen').length"),
            ("click", f'#code-chats li[data-chat="{mine}"] .code-chat-link'),
            ("wait", f"location.hash === '#/code/{aid}/{pid('alpha')}/{mine}'"
                     f" && document.querySelector('#code-chat')?.dataset.chat === '{mine}'"),
            ("eval", "tab", "document.querySelector('#code-tabbar .code-tab[aria-selected=true]')?.dataset.chat"),
            ("eval", "reopened", f"fetch('/api/code/agents/{aid}/projects/{pid('alpha')}/chats').then((r) => r.json())"
                                 f".then((j) => j.items.find((c) => c.chat === '{mine}').state)")], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["hash"] == f"#/code/{aid}/{pid('alpha')}" and v["fits"] is True
    assert v["first"] is True and v["views"] == ["Чат", "Файлы", "Git"] and v["buttons"] == 0
    assert v["rows"] == [[other, "Новый чат", True, ["Claude Code", "VS Code или терминал"]],
                         [busy, "Сборка", True, ["Claude Code", "студия", "идёт ход"]],
                         [asking, "Тесты", True, ["Claude Code", "VS Code или терминал", "ждёт ответа"]],
                         [mine, "Починить вход", True, ["Claude Code", "студия", "закрыт"]]]
    assert v["tab"] == mine and v["reopened"] == "open"
    assert not known_refusals(problems(r), 409), problems(r)


def test_code_push_click_opens_the_chat_in_an_open_studio(browser, live):
    """Нажатие на push при открытой студии: sw.js шлёт окну адрес code/<aid>/<pid>/<chat> — окно переходит в
    чат; адрес другого вида не открывается."""
    login = "code-push-click"
    live.add_user(login)
    aid, _ = enroll_live(live, login, "Спящий ПК")          # не на связи: чат — словами, без запросов к мосту
    chat = "55555555-5555-4555-8555-555555555555"
    href = f"code/{aid}/0123456789abcdef/{chat}"
    post = "navigator.serviceWorker.dispatchEvent(new MessageEvent('message', {{ data: {{ jobs: {!r} }} }}))"
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("eval", "bad", post.format("code/7/../x")), ("eval", "badHash", settled("location.hash")),
        ("eval", "sent", post.format(href)),
        ("wait", f"location.hash === '#/{href}' && !!document.querySelector('#code-chat')"),
        ("eval", "page", "document.querySelector('#code-chat').dataset.chat"),
        ("wait", f"document.querySelector('#code-tabbar .code-tab[aria-selected=true]')?.dataset.chat === '{chat}'"),
        ("eval", "tabs", "[...document.querySelectorAll('#code-tabbar .code-tab')].map((t) => [t.dataset.chat,"
                         " t.querySelector('.code-tab-title').textContent])")])
    assert not r.get("failed"), r
    assert r["values"]["badHash"] == "#/chat" and r["values"]["page"] == chat
    assert r["values"]["tabs"] == [[chat, ""]]  # компьютер спит: список сессий ещё не прочитан
    assert not problems(r), problems(r)


# чат Claude Code (code-chat.js): лента, ход потоком, карточки, остановка, обрыв связи

CODE_CHAT_A = "a1111111-1111-4111-8111-111111111111"
CODE_CHAT_B = "b2222222-2222-4222-8222-222222222222"
CODE_WAITING = "Компьютер пропал со связи — жду его"
#: сообщения ленты: вид и текст (у ответа — текст разметки)
CODE_MSGS = """[...document.querySelectorAll('#code-feed .code-msg')].map((m) => [m.classList.contains('user') ? 'user'
  : 'assistant', m.classList.contains('user') ? m.textContent : m.querySelector('.md').textContent.trim()])"""
#: шаги ленты: инструмент, начало ввода, итог, ошибка, ссылка «Открыть файл»
CODE_STEPS = """[...document.querySelectorAll('#code-feed details.code-step')].map((d) => [
  d.querySelector('.code-step-tool').textContent, d.querySelector('.code-step-head').textContent,
  d.querySelector('.code-step-result').textContent, d.classList.contains('error'),
  d.querySelector('.code-step-file') ? d.querySelector('.code-step-file').textContent : null])"""
CODE_IDLE_AFTER = ("document.querySelector('#code-chat').dataset.running === '0'"
                   " && document.querySelectorAll('#code-feed .code-msg.assistant').length === {n}"
                   " && !document.querySelector('.code-ask')")


def code_free_machine(chats=None):
    """Компьютер без PIN с одним проектом alpha: открывается нажатием."""
    from fake_bridge import FakeMachine
    return FakeMachine(projects={"alpha": {"chats": chats or {}}})


def code_session(title, tag="studio", messages=None):
    from fake_bridge import T0
    return {"title": title, "created": T0, "modified": T0, "tag": tag, "messages": messages or []}


def code_chat(aid, chat):
    """«Код» (панель сама открывает компьютер без PIN) → его проекты, затем адрес чата: история показана."""
    from fake_bridge import pid
    return [*go("code"), ("wait", f"document.querySelector(\"#code-agents li[data-id='{aid}'] .code-agent[aria-current]\")"
                                  " && document.querySelector('#code-projects li[data-pid]')"),
            *go(f"code/{aid}/{pid('alpha')}/{chat}"), code_loaded(chat)]


def code_loaded(chat):
    return ("wait", f"document.querySelector('#code-chat')?.dataset.chat === '{chat}'"
                    " && document.querySelector('#code-chat').dataset.loaded === '1'")


def code_send(text):
    return [("type", "#code-text", text), ("click", "#code-send")]


def code_requests(machine, op):
    return [q for q in machine.requests if q["op"] == op]


@pytest.mark.parametrize("engine,width", [("claude", 320), ("codex", 1280)])
def test_code_screenshots_paste_pick_drop_and_retry(browser, live, engine, width):
    """Скриншоты тремя способами; черновик переживает вкладки и отказ, без текста уходит обеим системам."""
    from test_codex import CATALOG
    login = "screenshots-" + engine
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Скриншоты")})
    machine.codex = CATALOG
    machine.script = [{"k": "text", "text": "Вижу скриншоты"}, {"k": "done", "status": "done"}]
    aid, sb = code_online(live, login, machine)
    setup_file = """(() => { window.shot = new File([Uint8Array.from(atob(%s), (c) => c.charCodeAt(0))],
      'screen.png', {type: 'image/png'}); window.shots = new DataTransfer(); shots.items.add(shot); return true; })()""" % json.dumps(base64.b64encode(PNG_REAL).decode())
    pick = "(() => { const p = document.querySelector('#code-image-input'); p.files = shots.files; p.dispatchEvent(new Event('change')); return true; })()"
    paste = "document.querySelector('#code-text').dispatchEvent(new ClipboardEvent('paste', {clipboardData: shots, bubbles: true, cancelable: true}))"
    drop = "(() => { const dt = new DataTransfer(); dt.items.add(shot); return document.querySelector('#code-composer').dispatchEvent(new DragEvent('drop', {dataTransfer: dt, bubbles: true, cancelable: true})); })()"
    fail_once = """(() => { const real = window.fetch; let fail = true;
      window.fetch = (u, o) => { if (fail && String(u).includes('/turn')) { fail = false;
        return Promise.resolve(new Response(JSON.stringify({detail: {code: 'invalid', message: 'Попробуйте ещё'}}),
          {status: 422, headers: {'content-type': 'application/json'}})); } return real(u, o); }; return true; })()"""
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            *([("click", "#code-tab-add"), ("wait", "!!document.querySelector('#code-new-codex')"),
                ("click", "#code-new-codex"), ("wait", "document.querySelector('#code-chat')?.dataset.engine === 'codex'"
                " && document.querySelector('#code-chat').dataset.loaded === '1'")] if engine == "codex" else []),
            ("eval", "file", setup_file), ("eval", "paste", paste),
            ("wait", "document.querySelectorAll('#code-images img').length === 1"),
            ("eval", "pick", pick), ("wait", "document.querySelectorAll('#code-images img').length === 2"),
            ("eval", "drop", drop), ("wait", "document.querySelectorAll('#code-images img').length === 3"),
            ("click", ".code-image-remove"), ("eval", "count", "document.querySelectorAll('#code-images img').length"),
            ("click", "#code-tab-files"), ("wait", "!!document.querySelector('#code-files')"),
            ("click", "#code-tab-chat"), ("wait", "document.querySelectorAll('#code-images img').length === 2"),
            ("eval", "stored", "JSON.stringify({...localStorage, ...sessionStorage}).includes('screen.png')"),
            ("eval", "fail", fail_once), ("click", "#code-send"),
            ("wait", "document.querySelector('#code-status').textContent === 'Попробуйте ещё'"),
            ("eval", "kept", "document.querySelectorAll('#code-images img').length"),
            ("click", "#code-send"),
            ("wait", "document.querySelector('#code-stop').hidden && document.querySelector('#code-feed').textContent.includes('Вижу скриншоты')"),
            ("eval", "sent", "[...document.querySelectorAll('.code-sent-image')].map((i) => i.naturalWidth > 0)"),
            ("eval", "cleared", "document.querySelectorAll('#code-images img').length"),
            ("eval", "fits", NO_OVERFLOW)], width=width, mobile=width == 320)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    values = r["values"]
    assert values["count"] == values["kept"] == 2 and values["cleared"] == 0
    assert values["sent"] == [True, True] and values["fits"] and not values["stored"]
    [turn] = code_requests(machine, "turn")
    assert turn["text"] == "" and turn.get("engine", "claude") == engine
    assert turn["images"] == [{"mime": "image/png", "data": base64.b64encode(PNG_REAL).decode()}] * 2
    assert not known_refusals(problems(r), 422), problems(r)


def test_code_turn_streams_text_and_steps(browser, live):
    """«Отправить» — ход потоком: текст по кускам в одном ответе (разметка), шаги с итогом (ошибка — классом),
    «Открыть файл» — только у шага Read/Edit/Write с путём внутри проекта; после done «Остановить» не видно.
    Модель и усилие — первый пункт «по умолчанию» (null — прежние модель и усилие чата): у чата с историей выбор
    устройства не подставляется; ссылка файла ведёт на вкладку «Файлы»."""
    from fake_bridge import T0, pid
    login = "code-turn"
    live.add_user(login)
    before = [{"id": "m-0", "role": "user", "text": "Раньше", "steps": [], "time": T0},
              {"id": "m-1", "role": "assistant", "text": "Было", "steps": [], "time": T0}]
    machine = code_free_machine({CODE_CHAT_A: code_session("Чат A", messages=before)})
    machine.script = [{"k": "text", "text": "Сейчас "}, {"k": "text", "text": "**проверю**."},
                      {"k": "step", "id": "s1", "tool": "Bash", "input": {"command": "pytest -q", "timeout": 60}},
                      {"k": "result", "id": "s1", "text": "3 passed", "error": False},
                      {"k": "step", "id": "s2", "tool": "Read", "input": {"file_path": "C:\\work\\Projects\\ALPHA\\src\\app.py"}},
                      {"k": "result", "id": "s2", "text": "нет файла", "error": True},
                      {"k": "step", "id": "s3", "tool": "Edit", "input": {"file_path": "C:\\work\\projects\\beta\\x.py"}},
                      {"k": "text", "text": " Готово."}, {"k": "done", "status": "done"}]
    machine.projects["alpha"]["files"] = {"src/app.py": b"print(1)\n"}   # ссылка шага его и откроет
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
            ("eval", "saved", f"localStorage.setItem('studio-code-choice:{aid}',"
                              " JSON.stringify({ model: 'sonnet', effort: 'high' }))"),
            *code_chat(aid, CODE_CHAT_A),
            ("eval", "labels", "[...document.querySelectorAll('.code-pick')].map((l) => l.firstChild.textContent)"),
            ("eval", "options", "['#code-model', '#code-effort'].map((s) => [...document.querySelector(s).options]"
                                ".map((o) => [o.value, o.textContent]))"),
            ("eval", "picked", "[document.querySelector('#code-model').value, document.querySelector('#code-effort').value]"),
            ("eval", "words", "['#code-send', '#code-stop'].map((s) => document.querySelector(s).textContent)"),
            ("eval", "note", "!!document.querySelector('#code-shared-note')"),
            *code_send("Запусти тесты"),
            ("wait", CODE_IDLE_AFTER.format(n=2) + " && [...document.querySelectorAll('.code-msg.assistant .md')]"
                     ".pop().textContent.includes('Готово')"),
            ("eval", "msgs", CODE_MSGS), ("eval", "steps", CODE_STEPS),
            ("eval", "stop", "document.querySelector('#code-stop').hidden"),
            ("eval", "field", "document.querySelector('#code-text').value"),
            ("eval", "stored", "JSON.stringify({ ...localStorage, ...sessionStorage }).includes('Запусти')"),
            ("eval", "fits", NO_OVERFLOW),
            ("click", ".code-step-file"),
            ("wait", "document.querySelector('#code-tab-files')?.getAttribute('aria-selected') === 'true'"),
            ("eval", "hash", "location.hash")], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["labels"] == ["Модель", "Усилие", "Права"]
    assert v["options"] == [[["", "opus"], ["opus", "Opus"], ["sonnet", "Sonnet"]],
                            [["", "high"], ["low", "low"], ["high", "high"]]]
    assert v["picked"] == ["", ""]
    assert v["words"] == ["Отправить", "Остановить"] and v["note"] is False
    assert v["msgs"] == [["user", "Раньше"], ["assistant", "Было"], ["user", "Запусти тесты"],
                         ["assistant", "Сейчас проверю. Готово."]]
    assert v["steps"] == [["Bash", "pytest -q", "3 passed", False, None],
                          ["Read", "C:\\work\\Projects\\ALPHA\\src\\app.py", "нет файла", True, "Открыть файл"],
                          ["Edit", "C:\\work\\projects\\beta\\x.py", "", False, None]]
    assert v["stop"] is True and v["field"] == "" and v["stored"] is False and v["fits"] is True
    assert v["hash"] == f"#/code/{aid}/{pid('alpha')}?tab=files"
    turn, = code_requests(machine, "turn")
    assert (turn["chat"], turn["text"], turn["model"], turn["effort"]) == (CODE_CHAT_A, "Запусти тесты", None, None)
    assert not problems(r), problems(r)


def code_permission(request_id, always, tool="Bash", input=None):
    return {"k": "ask", "request_id": request_id, "kind": "permission", "tool": tool,
            "input": input or {"command": "rm -rf build"}, "always": always}


def test_code_permission_card_yes_and_always(browser, live):
    """Карточка разрешения: слова, инструмент и ввод, «Да» / «Да, больше не спрашивать» / «Нет» и поле; на 320 px
    без прокрутки вбок. «Да» — allow, ход дошёл до done, карточка ушла; второй ход — «Да, больше не спрашивать»
    — always."""
    from fake_bridge import pid
    login = "code-allow"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Чат A")})
    machine.script = [{"k": "text", "text": "Нужно удалить сборку."}, code_permission("r1", True),
                      {"k": "text", "text": " Удалил."}, {"k": "done", "status": "done"}]
    aid, sb = code_online(live, login, machine)
    card = ".code-ask[data-request='r1']"
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            *code_send("Почисти сборку"), ("wait", f"document.querySelector(\"{card}\")"),
            ("eval", "card", f"""(() => {{ const c = document.querySelector("{card}");
              return [c.querySelector('.code-ask-title').textContent, c.querySelector('.code-ask-tool').textContent,
                JSON.parse(c.querySelector('.code-ask-input').textContent),
                [...c.querySelectorAll('.code-ask-actions button')].map((b) => b.textContent),
                c.querySelector('textarea.code-ask-instead').placeholder]; }})()"""),
            ("eval", "stop", "!document.querySelector('#code-stop').hidden"),
            ("eval", "fits", NO_OVERFLOW),
            ("click", f"{card} .code-ask-yes"),
            ("wait", CODE_IDLE_AFTER.format(n=1) + " && document.querySelector('.code-msg.assistant .md').textContent.includes('Удалил')"),
            ("eval", "msgs", CODE_MSGS)], width=320, mobile=True)
        assert not r.get("failed"), r
        v = r["values"]
        assert v["card"] == ["Claude Code просит разрешения", "Bash", {"command": "rm -rf build"},
                             ["Да", "Да, больше не спрашивать", "Нет"], "Что сделать вместо этого (необязательно)"]
        assert v["stop"] is True and v["fits"] is True
        assert v["msgs"] == [["user", "Почисти сборку"], ["assistant", "Нужно удалить сборку. Удалил."]]
        answer, = code_requests(machine, "answer")
        assert (answer["request_id"], answer["decision"], answer["message"], answer["answers"]) == ("r1", "allow", None, None)
        assert not known_refusals(problems(r), 409), problems(r)

        machine.script = [code_permission("r2", True), {"k": "done", "status": "done"}]
        r = browser.drive(live.url + f"/#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}", [("wait", READY),
            code_loaded(CODE_CHAT_A), *code_send("Ещё раз"),
            ("wait", "document.querySelector(\".code-ask[data-request='r2']\")"),
            ("click", ".code-ask[data-request='r2'] .code-ask-always"),
            ("wait", CODE_IDLE_AFTER.format(n=2))], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert [(q["request_id"], q["decision"]) for q in code_requests(machine, "answer")] == [("r1", "allow"),
                                                                                         ("r2", "always")]
    assert not known_refusals(problems(r), 409), problems(r)


def test_code_permission_no_with_words(browser, live):
    """«Нет» со словами в поле — deny и message; без предложенного правила кнопки «Да, больше не спрашивать» нет."""
    login = "code-deny"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Чат A")})
    machine.script = [code_permission("r1", False, "Write", {"file_path": "C:\\work\\projects\\alpha\\a.txt", "content": "x"}),
                      {"k": "done", "status": "done"}]
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            *code_send("Запиши файл"), ("wait", "document.querySelector('.code-ask')"),
            ("eval", "always", "!!document.querySelector('.code-ask-always')"),
            ("type", ".code-ask-instead", "Сначала покажи план"), ("click", ".code-ask-no"),
            ("wait", CODE_IDLE_AFTER.format(n=1))], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert r["values"]["always"] is False
    answer, = code_requests(machine, "answer")
    assert (answer["decision"], answer["message"], answer["answers"]) == ("deny", "Сначала покажи план", None)
    assert not known_refusals(problems(r), 409), problems(r)


def test_code_question_card_sends_answers(browser, live):
    """Вопрос агента: переключатели у вопроса с одним ответом, флажки у multi; «Ответить» — allow и answers
    «вопрос → ответ» (несколько — через запятую)."""
    login = "code-question"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Чат A")})
    machine.script = [{"k": "ask", "request_id": "q1", "kind": "question", "questions": [
        {"question": "Какую базу взять?", "header": "База", "multi": False,
         "options": [{"label": "SQLite", "description": "файл"}, {"label": "Postgres", "description": "сервер"}]},
        {"question": "Что проверить?", "header": "Проверки", "multi": True,
         "options": [{"label": "Тесты", "description": ""}, {"label": "Линтер", "description": ""},
                     {"label": "Типы", "description": ""}]}]},
        {"k": "text", "text": "Беру Postgres."}, {"k": "done", "status": "done"}]
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            *code_send("Настрой базу"), ("wait", "document.querySelector(\".code-ask[data-request='q1']\")"),
            ("eval", "kinds", """[...document.querySelectorAll('.code-ask-question')].map((f) =>
              [f.querySelector('legend').textContent, [...f.querySelectorAll('input:not(.code-ask-own)')].map((i) => i.type)])"""),
            ("eval", "button", "document.querySelector('.code-ask-answer').textContent"),
            ("eval", "fits", NO_OVERFLOW),
            ("click", ".code-ask input[value='Postgres']"), ("click", ".code-ask input[value='Тесты']"),
            ("click", ".code-ask input[value='Типы']"), ("click", ".code-ask-answer"),
            ("wait", CODE_IDLE_AFTER.format(n=1)), ("eval", "msgs", CODE_MSGS)], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["kinds"] == [["База", ["radio", "radio"]], ["Проверки", ["checkbox", "checkbox", "checkbox"]]]
    assert v["button"] == "Ответить" and v["fits"] is True
    assert v["msgs"] == [["user", "Настрой базу"], ["assistant", "Беру Postgres."]]
    answer, = code_requests(machine, "answer")
    assert (answer["request_id"], answer["decision"], answer["message"]) == ("q1", "allow", None)
    assert answer["answers"] == {"Какую базу взять?": "Postgres", "Что проверить?": "Тесты, Типы"}
    assert not known_refusals(problems(r), 409), problems(r)


def test_code_question_own_answer(browser, live):
    """«Свой ответ» у каждого вопроса (в Claude Code — «Other»): написан — уходит строкой вместо отмеченного; у
    соседнего вопроса без своего ответа — отмеченное. Ни своего, ни отмеченного — запроса нет."""
    login = "code-own"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Чат A")})
    machine.script = [{"k": "ask", "request_id": "q1", "kind": "question", "questions": [
        {"question": "Какую базу взять?", "header": "База", "multi": False,
         "options": [{"label": "SQLite", "description": ""}, {"label": "Postgres", "description": ""}]},
        {"question": "Что проверить?", "header": "Проверки", "multi": True,
         "options": [{"label": "Тесты", "description": ""}, {"label": "Линтер", "description": ""}]}]},
        {"k": "text", "text": "Беру DuckDB."}, {"k": "done", "status": "done"}]
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            *code_send("Настрой базу"), ("wait", "document.querySelector(\".code-ask[data-request='q1']\")"),
            ("eval", "own", "[...document.querySelectorAll('.code-ask-question')].map((f) =>"
                            " (i => [i.type, i.placeholder, i.maxLength])(f.querySelector('.code-ask-own')))"),
            ("eval", "fits", NO_OVERFLOW),
            ("click", ".code-ask-answer"), ("eval", "nothing", settled("true", 300)),
            ("type", ".code-ask-question:first-of-type .code-ask-own", "  DuckDB  "),
            ("click", ".code-ask input[value='Тесты']"), ("click", ".code-ask-answer"),
            ("wait", CODE_IDLE_AFTER.format(n=1))], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["own"] == [["text", "Свой ответ", 2000]] * 2 and v["fits"] is True
    answer, = code_requests(machine, "answer")
    assert (answer["decision"], answer["answers"]) == ("allow", {"Какую базу взять?": "DuckDB", "Что проверить?": "Тесты"})
    assert not known_refusals(problems(r), 409), problems(r)


def test_code_turn_error_without_message_says_error(browser, live):
    """Конец хода с ошибкой без слов — «Ошибка», без висящего двоеточия; со словами — «Ошибка: <слова>»."""
    from fake_bridge import pid
    login = "code-error"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Чат A")})
    machine.script = [{"k": "text", "text": "Начал"}, {"k": "done", "status": "error"}]
    aid, sb = code_online(live, login, machine)
    end = "[...document.querySelectorAll('.code-turn-end')].map((e) => [e.textContent, e.classList.contains('error')])"
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            *code_send("Сделай"), ("wait", "document.querySelector('.code-turn-end')"), ("eval", "end", end)],
            width=320, mobile=True)
        assert not r.get("failed"), r
        assert r["values"]["end"] == [["Ошибка", True]]
        machine.script = [{"k": "done", "status": "error", "message": "сбой модели"}]
        r = browser.drive(live.url + f"/#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}", [("wait", READY),
            code_loaded(CODE_CHAT_A), *code_send("Ещё"), ("wait", "document.querySelector('.code-turn-end')"),
            ("eval", "end", end)], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert r["values"]["end"] == [["Ошибка: сбой модели", True]]
    assert not known_refusals(problems(r), 409), problems(r)


#: опрос live на простое — IDLE_POLL_SEC (20 с) — 300 мс
CODE_FAST_IDLE = """(() => { const real = window.setTimeout.bind(window);
  window.setTimeout = (f, ms, ...a) => real(f, ms === 20000 ? 300 : ms, ...a); })();"""


def code_start_elsewhere(machine, chat, text, script):
    """Ход, начатый не этой страницей (другая вкладка, VS Code): машина ведёт его сама, событий студии не шлёт."""
    from fake_bridge import pid
    machine.script = script
    machine._op_turn({"op": "turn", "project": pid("alpha"), "chat": chat, "text": text}, [])


def test_code_idle_tab_polls_live(browser, live):
    """Чат на простое: live ответил idle — через IDLE_POLL_SEC снова live, пока страница видна; ход, начатый в другом
    месте, страница показывает снимком без перезагрузки."""
    login = "code-idle-poll"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Чат A")})
    aid, sb = code_online(live, login, machine)

    def start():
        wait_for(lambda: len(code_requests(machine, "watch")) >= 2, 60)      # страница уже опрашивает
        code_start_elsewhere(machine, CODE_CHAT_A, "С другого устройства", [{"k": "text", "text": "Из другой вкладки"}])

    mover = threading.Thread(target=start, daemon=True)
    mover.start()
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("wait", "document.querySelector('#code-chat').dataset.running === '1'"
                     " && document.querySelector('.code-msg.assistant .md')?.textContent.trim() === 'Из другой вкладки'")],
            width=320, mobile=True, init=CODE_FAST_IDLE)
        mover.join(10)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert len(code_requests(machine, "watch")) >= 3 and code_requests(machine, "turn") == []
    assert not known_refusals(problems(r), 409), problems(r)


def test_code_done_before_snapshot_reloads_history(browser, live):
    """Ход кончился между ответом на watch и снимком: поток live приносит только done — страница перечитывает
    историю, и ответ хода виден без перезагрузки."""
    from fake_bridge import FakeMachine

    class DoneFirst(FakeMachine):
        def _op_watch(self, req, events):
            self._chat(req)
            if req.get("chat") not in self.running:
                return {"running": False}
            self._finish(req["chat"], events, {"k": "done", "status": "done"})     # вместо снимка
            return {"running": True}

    login = "code-done-first"
    live.add_user(login)
    machine = DoneFirst(projects={"alpha": {"chats": {CODE_CHAT_A: code_session("Чат A")}}})
    code_start_elsewhere(machine, CODE_CHAT_A, "Задача", [{"k": "text", "text": "Итог"}])
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("wait", CODE_IDLE_AFTER.format(n=1)), ("eval", "msgs", CODE_MSGS)], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert r["values"]["msgs"] == [["user", "Задача"], ["assistant", "Итог"]]
    assert len(code_requests(machine, "history")) >= 2
    assert not known_refusals(problems(r), 409), problems(r)


#: ответ на «Отправить» — 504 timeout, а ход на машине при этом начат (запрос к службе настоящий)
CODE_TURN_TIMEOUT = """(() => { const real = window.fetch; window.fetch = (u, o) => !String(u).endsWith('/turn')
  ? real(u, o) : real(u, o).then((r) => { r.body && r.body.cancel(); return new Response(JSON.stringify({ detail: {
    code: 'timeout', message: 'компьютер не ответил' } }), { status: 504, headers: { 'content-type': 'application/json' } }); });
  return true; })()"""


def test_code_turn_running_and_timeout_open_live(browser, live):
    """«Отправить» отвергнут: 409 running (ход уже начат в другом месте) и 504 timeout (ход, может быть, начат) —
    слова и live: страница показывает идущий ход снимком; задание остаётся в поле."""
    from fake_bridge import pid
    login = "code-busy"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Чат A")})
    aid, sb = code_online(live, login, machine)
    here = f"code/{aid}/{pid('alpha')}/{CODE_CHAT_A}"
    shown = ("document.querySelector('#code-chat').dataset.running === '1'"
             " && document.querySelector('.code-msg.assistant .md')?.textContent.trim() === '{text}'")
    running = (f"fetch('/api/code/agents/{aid}/projects/{pid('alpha')}/chats').then((r) => r.json())"
               ".then((j) => j.items.some((c) => c.running))")

    def start():
        wait_for(lambda: code_requests(machine, "watch"), 60)
        code_start_elsewhere(machine, CODE_CHAT_A, "Где-то ещё", [{"k": "text", "text": "Идёт"}])

    mover = threading.Thread(target=start, daemon=True)
    mover.start()
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("wait", running), *code_send("Моё"), ("wait", shown.format(text="Идёт")),
            ("eval", "status", "document.querySelector('#code-status').textContent"),
            ("eval", "field", "document.querySelector('#code-text').value")], width=320, mobile=True)
        mover.join(10)
        assert not r.get("failed"), r
        assert r["values"]["status"] == "В чате уже идёт ход" and r["values"]["field"] == "Моё"
        assert not known_refusals(problems(r), 409), problems(r)

        machine.running.clear()
        machine.script = [{"k": "text", "text": "Начал"}]
        r = browser.drive(live.url + f"/#/{here}", [("wait", READY), code_loaded(CODE_CHAT_A),
            ("eval", "timeout", CODE_TURN_TIMEOUT), *code_send("Долгое"), ("wait", shown.format(text="Начал")),
            ("eval", "status", "document.querySelector('#code-status').textContent")], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert r["values"]["status"] == "Компьютер не ответил"
    assert [q["text"] for q in code_requests(machine, "turn")] == ["Моё", "Долгое"]     # «Где-то ещё» — мимо студии
    assert not known_refusals(problems(r), 409), problems(r)


#: вкладка всегда видна: соседняя вкладка того же браузера не снимает её поток
ALWAYS_VISIBLE = """(() => { Object.defineProperty(Document.prototype, 'visibilityState', { configurable: true,
  get: () => 'visible' }); Object.defineProperty(Document.prototype, 'hidden', { configurable: true, get: () => false });
})();"""


def test_code_answer_in_one_tab_removes_card_in_other(browser, live):
    """Две вкладки одного чата (Review Focus 2): ответ на карточку в одной — событие asked, и карточка уходит и во
    второй, хотя ход идёт дальше (следующая карточка видна в обеих)."""
    login = "code-two-tabs"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Чат A")})
    machine.script = [code_permission("r1", False), {"k": "text", "text": "Дальше"}, code_permission("r2", False)]
    aid, sb = code_online(live, login, machine)
    card = "document.querySelector(\".code-ask[data-request='{id}']\")"
    first = {}

    def tab_one():
        first["r"] = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            *code_send("Почисти"), ("wait", card.format(id="r1")),
            ("wait", f"!{card.format(id='r1')} && {card.format(id='r2')}"
                     " && document.querySelector('.code-msg.assistant .md').textContent.trim() === 'Дальше'"),
            ("eval", "msgs", CODE_MSGS)],
            width=320, mobile=True, init=ALWAYS_VISIBLE, timeout=60000)

    one = threading.Thread(target=tab_one, daemon=True)
    one.start()
    try:
        wait_for(lambda: "r1" in machine.running.get(CODE_CHAT_A, {}).get("asks", {}), 90)
        r = browser.drive(live.url + "/", [("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("wait", card.format(id="r1")), ("click", "#code-take-session"),
            ("wait", "document.querySelector('#code-session-notice').hidden"),
            ("click", ".code-ask[data-request='r1'] .code-ask-yes"),
            ("wait", f"!{card.format(id='r1')} && {card.format(id='r2')}")], width=320, mobile=True, init=ALWAYS_VISIBLE)
        one.join(90)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert not first["r"].get("failed"), first["r"]
    assert first["r"]["values"]["msgs"] == [["user", "Почисти"], ["assistant", "Дальше"]]
    assert [(q["request_id"], q["decision"]) for q in code_requests(machine, "answer")] == [("r1", "allow")]
    assert not known_refusals(problems(r), 409), problems(r)
    assert not known_refusals(problems(first["r"]), 409), problems(first["r"])


def test_code_stop_and_offline(browser, live):
    """«Остановить» — stop мосту, карточка ушла, «Ход прерван». Затем ход идёт и мост пропал — «Компьютер пропал
    со связи — жду его»; мост снова на связи — надпись ушла (новый канал закрыл сессии — к панели PIN, у
    компьютера без PIN — сразу open и снова этот чат: снимок идущего хода)."""
    import bridge
    from fake_bridge import SocketBridge, pid
    login = "code-stop"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Чат A")})
    machine.script = [{"k": "text", "text": "Думаю"}, code_permission("r1", False)]
    aid, sb = code_online(live, login, machine)
    state = {}
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            *code_send("Долгая задача"), ("wait", "document.querySelector('.code-ask')"),
            ("click", "#code-stop"),
            ("wait", "document.querySelector('.code-turn-end') && " + CODE_IDLE_AFTER.format(n=1)),
            ("eval", "end", "document.querySelector('.code-turn-end').textContent"),
            ("eval", "stop", "document.querySelector('#code-stop').hidden")], width=320, mobile=True)
        assert not r.get("failed"), r
        assert r["values"]["end"] == "Ход прерван" and r["values"]["stop"] is True
        assert len(code_requests(machine, "stop")) == 1 and not machine.running
        assert not known_refusals(problems(r), 409), problems(r)

        machine.script = [{"k": "text", "text": "Ещё думаю"}]

        def drop_and_return():
            wait_for(lambda: CODE_CHAT_A in machine.running and bridge.watching(aid, CODE_CHAT_A), 60)
            sb.stop()
            wait_for(lambda: aid not in bridge.LINKS)
            time.sleep(1.5)
            state["sb"] = SocketBridge(live.url, sb.key, machine).start()

        mover = threading.Thread(target=drop_and_return, daemon=True)
        mover.start()
        r = browser.drive(live.url + f"/#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}", [("wait", READY),
            code_loaded(CODE_CHAT_A), *code_send("Снова"),
            ("wait", f"document.querySelector('#code-status')?.textContent === '{CODE_WAITING}'"),
            ("eval", "stop", "document.querySelector('#code-stop').hidden"),
            ("wait", f"document.querySelector('#code-status')?.textContent !== '{CODE_WAITING}'"),
            ("wait", f"location.hash === '#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}'"
                     " && document.querySelector('#code-chat')?.dataset.running === '1'"
                     " && document.querySelector('.code-msg.assistant:last-child .md')?.textContent.trim() === 'Ещё думаю'")],
            width=320, mobile=True)
        mover.join(10)
    finally:
        sb.stop()
        if "sb" in state:
            state["sb"].stop()
    assert not r.get("failed"), r
    assert r["values"]["stop"] is True
    # повторный вход читает проекты без открытия; второе открытие — после нового канала
    assert [q.get("pin") for q in code_requests(machine, "open")] == [None, None]
    assert not known_refusals(problems(r), 409), problems(r)


def test_code_reopened_chat_shows_history_and_more(browser, live):
    """250 сообщений: открыт — 100 последних; «Показать раньше» — ещё 100 выше, затем остаток, и кнопка уходит.
    Поле — над вкладками телефона."""
    from fake_bridge import T0
    login = "code-history"
    live.add_user(login)
    messages = [{"id": f"m-{i}", "role": "user" if i % 2 == 0 else "assistant", "text": f"сообщение {i}",
                 "steps": [], "time": T0 + i} for i in range(250)]
    machine = code_free_machine({CODE_CHAT_A: code_session("Длинный", messages=messages)})
    aid, sb = code_online(live, login, machine)
    first = "document.querySelector('#code-feed .code-msg').textContent.trim()"
    count = "document.querySelectorAll('#code-feed .code-msg').length"
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("eval", "n1", count), ("eval", "first1", first), ("eval", "more1", "document.querySelector('#code-more').textContent"),
            ("eval", "composer", """(() => { const c = document.querySelector('#code-composer').getBoundingClientRect();
              const rail = document.querySelector('#rail').getBoundingClientRect();
              return c.bottom <= rail.top + 1 && c.top >= 0; })()"""),
            ("eval", "fits", NO_OVERFLOW),
            ("click", "#code-more"), ("wait", f"{count} === 200"), ("eval", "first2", first),
            ("eval", "shown2", "!document.querySelector('#code-more').hidden"),
            ("click", "#code-more"), ("wait", f"{count} === 250"), ("eval", "first3", first),
            ("eval", "shown3", "!document.querySelector('#code-more').hidden")], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert (v["n1"], v["first1"], v["more1"]) == (100, "сообщение 150", "Показать раньше")
    assert v["composer"] is True and v["fits"] is True
    assert (v["first2"], v["shown2"], v["first3"], v["shown3"]) == ("сообщение 50", True, "сообщение 0", False)
    assert [q["before"] for q in code_requests(machine, "history")] == [None, 150, 50]
    assert not known_refusals(problems(r), 409), problems(r)


@pytest.mark.parametrize("engine,width", [("claude", 320), ("codex", 1280)])
def test_code_defaults_are_saved_in_settings_and_used_after_reload(browser, live, engine, width):
    from test_codex import CATALOG
    login = "settings-code-" + engine
    live.add_user(login)
    machine = code_free_machine()
    machine.codex = CATALOG
    aid, sb = code_online(live, login, machine)
    chosen_model = "codex-test" if engine == "codex" else "sonnet"
    chosen_effort = "ultra" if engine == "codex" else "high"
    chosen_access = "read-only" if engine == "codex" else "plan"
    prefix = f"#code-defaults-{engine}"
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("settings/code"),
            ("wait", "!!document.querySelector('#code-defaults-save')"),
            set_value(prefix + "-model", chosen_model), set_value(prefix + "-effort", chosen_effort),
            set_value(prefix + "-access", chosen_access),
            ("click", "#code-defaults-save"),
            ("wait", "document.querySelector('#code-defaults-status').textContent.includes('Умолчания сохранены')"),
            ("eval", "fits", NO_OVERFLOW),
            ("eval", "reload", "location.reload()"), ("wait", READY),
            ("wait", f"document.querySelector('{prefix}-model')?.value === '{chosen_model}'"),
            ("eval", "effort", f"document.querySelector('{prefix}-effort').value"),
            *go("code"), ("wait", "!!document.querySelector('#code-projects li[data-pid]')"),
            *code_project(aid, "alpha"), ("wait", "!!document.querySelector('#code-new-chat')"),
            ("click", "#code-new-codex" if engine == "codex" else "#code-new-chat"),
            ("wait", "document.querySelector('#code-chat')?.dataset.loaded === '1'"),
            ("eval", "picked", "[document.querySelector('#code-model').value, document.querySelector('#code-effort').value]"),
            ("eval", "access", "document.querySelector('#code-access').value"),
            ("eval", "profile", f"(async () => (await {API}.api('GET', '/api/me')).code_defaults)()")],
            width=width, mobile=width == 320)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    values = r["values"]
    assert values["fits"] and values["effort"] == chosen_effort and values["picked"] == [chosen_model, chosen_effort]
    assert values["access"] == chosen_access
    assert values["profile"][engine] == {"model": chosen_model, "effort": chosen_effort, "access": chosen_access}
    other = "claude" if engine == "codex" else "codex"
    assert values["profile"][other] == {"model": None, "effort": None, "access": None}
    assert not known_refusals(problems(r), 409), problems(r)


@pytest.mark.parametrize("engine", ["claude", "codex"])
def test_code_concrete_settings_context_and_access_are_visible(browser, live, engine):
    from test_codex import CATALOG
    login = "code-state-" + engine
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Контекст")})
    machine.codex = CATALOG
    machine.context = {"used": 5000, "limit": 100000, "percent": 5}
    canonical = "gpt-6.1-sol" if engine == "codex" else "claude-opus-5-5"
    chosen = "read-only" if engine == "codex" else "plan"
    machine.script = [{"k": "settings", "model": canonical, "effort": "high", "access": chosen},
        {"k": "context", "used": 70000, "limit": 100000, "percent": 70},
        {"k": "text", "text": "Готово"}, {"k": "done", "status": "done"}]
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            *([("click", "#code-tab-add"), ("wait", "!!document.querySelector('#code-new-codex')"),
                ("click", "#code-new-codex"), ("wait", "document.querySelector('#code-chat')?.dataset.engine === 'codex'"
                " && document.querySelector('#code-chat').dataset.loaded === '1'")] if engine == "codex" else []),
            ("eval", "before", "['#code-model', '#code-effort', '#code-access'].map((s) => document.querySelector(s).selectedOptions[0].textContent)"),
            ("eval", "context", "document.querySelector('#code-context-text').textContent"),
            set_value("#code-access", chosen), *code_send("Проверка"),
            ("wait", "document.querySelector('#code-context-meter').value === 70 && document.querySelector('#code-stop').hidden"),
            ("eval", "after", "['#code-model', '#code-effort', '#code-access'].map((s) => document.querySelector(s).selectedOptions[0].textContent)"),
            ("eval", "contextAfter", "document.querySelector('#code-context').title"), ("eval", "fits", NO_OVERFLOW)],
            width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    values = r["values"]
    assert all("умолчанию" not in v and "Загрузка" not in v and "получено" not in v for v in values["before"])
    assert values["context"] == "5%" and values["after"][0] == canonical
    assert "70" in values["contextAfter"] and values["fits"]
    [turn] = code_requests(machine, "turn")
    assert turn["access"] == chosen and turn.get("engine", "claude") == engine
    assert not known_refusals(problems(r), 409), problems(r)


def test_code_account_defaults_apply_only_to_new_chats(browser, live):
    """Новые чаты берут умолчания учётки; выбор в чате их не меняет, начатые сессии продолжаются со своим."""
    from fake_bridge import pid
    login = "code-choice"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Чат A")})
    machine.script = [{"k": "text", "text": "ок"}, {"k": "done", "status": "done"}]
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
            ("eval", "defaults", "(async () => { await " + API + ".api('PATCH', '/api/me', {code_defaults: {claude:"
              " {model: 'sonnet', effort: 'high'}}}); await " + API + ".loadMe(); return true; })()"),
            *code_chat(aid, CODE_CHAT_A),
            set_value("#code-model", "opus"), set_value("#code-effort", "low"),
            *code_send("привет"), ("wait", CODE_IDLE_AFTER.format(n=1)),
            ("eval", "stored", f"(async () => (await {API}.api('GET', '/api/me')).code_defaults.claude)()"),
            ("click", "#code-tab-add"), ("wait", "document.querySelector('#code-tab-menu #code-new-chat')"),
            ("click", "#code-tab-menu #code-new-chat"),
            ("wait", "/^#\\/code\\/\\d+\\/[0-9a-f]{16}\\/[0-9a-f-]{36}$/.test(location.hash)"
                     f" && location.hash.split('/').pop() !== '{CODE_CHAT_A}'"
                     " && document.querySelector('#code-chat')?.dataset.loaded === '1'"),
            ("eval", "picked", "[document.querySelector('#code-model').value, document.querySelector('#code-effort').value]"),
            ("eval", "newChat", "window.newChat = location.hash"),
            *code_send("и тут"),
            ("wait", "document.querySelector('#code-chat').dataset.running === '1'"),
            *go(f"code/{aid}/{pid('alpha')}/{CODE_CHAT_A}"), code_loaded(CODE_CHAT_A),
            ("eval", "again", "[document.querySelector('#code-model').value, document.querySelector('#code-effort').value]"),
            *code_send("снова"),
            ("wait", "document.querySelector('#code-chat').dataset.running === '1'"),
            ("eval", "stored2", f"(async () => (await {API}.api('GET', '/api/me')).code_defaults.claude)()"),
            # новый выбор устройства — уже начатому «новому» чату он не подставляется
            set_value("#code-model", "opus"), set_value("#code-effort", "low"),
            ("eval", "back", "location.hash = window.newChat"),
            ("wait", "location.hash === window.newChat && document.querySelector('#code-chat')?.dataset.loaded === '1'"
                     " && document.querySelector('#code-chat').dataset.chat === location.hash.split('/').pop()"),
            ("eval", "newAgain", "[document.querySelector('#code-model').value, document.querySelector('#code-effort').value]")],
            width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["stored"] == {"model": "sonnet", "effort": "high"} and v["picked"] == ["sonnet", "high"]
    assert v["again"] == ["", ""] and v["stored2"] == v["stored"] and v["newAgain"] == ["", ""]
    turns = code_requests(machine, "turn")
    assert [(q["text"], q["model"], q["effort"]) for q in turns] == [
        ("привет", "opus", "low"), ("и тут", "sonnet", "high"), ("снова", None, None)]
    assert turns[0]["chat"] == CODE_CHAT_A and turns[1]["chat"] != CODE_CHAT_A and turns[2]["chat"] == CODE_CHAT_A
    assert not known_refusals(problems(r), 409), problems(r)


def test_code_foreign_session_warns_about_two_writers(browser, live):
    """Чат из VS Code или терминала — предупреждение над лентой; чат студии — без него."""
    from fake_bridge import pid
    login = "code-shared"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Чат A"), CODE_CHAT_B: code_session("Чат B", tag=None)})
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_B),
            ("eval", "noteB", "document.querySelector('#code-shared-note')?.textContent ?? null"),
            ("eval", "here", CODE_PLACE),
            ("eval", "fits", NO_OVERFLOW),
            *go(f"code/{aid}/{pid('alpha')}/{CODE_CHAT_A}"), code_loaded(CODE_CHAT_A),
            ("eval", "noteA", "!!document.querySelector('#code-shared-note')")], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["noteB"] == ("Эту сессию начали в VS Code или терминале. Если она открыта и там, не пишите в двух местах"
                          " сразу — ответы перемешаются.")
    assert v["here"] == "Рабочий ПК / alpha" and v["fits"] is True and v["noteA"] is False
    assert not known_refusals(problems(r), 409), problems(r)


def test_code_hidden_page_drops_its_stream_and_returns_to_live(browser, live):
    """Страница ушла с экрана — свой поток она снимает (у службы нет наблюдателей чата: push уходит), вернулась —
    снова live, снимок догоняет ход; ход на машине идёт."""
    import bridge
    login = "code-hidden"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Чат A")})
    machine.script = [{"k": "text", "text": "Работаю"}]
    aid, sb = code_online(live, login, machine)
    seen = []

    def signal(name):
        """Знак странице, что служба уже увидела нужное: имя компьютера в GET /api/agents."""
        live_sql(live, "UPDATE agents SET name = ? WHERE id = ?", (name, aid))

    def record():
        # условия — в Python, без пауз: наблюдатель есть (ход), снят (страница ушла), снова есть (вернулась)
        wait_for(lambda: CODE_CHAT_A in machine.running and bridge.watching(aid, CODE_CHAT_A), 60)
        seen.append(True)
        wait_for(lambda: not bridge.watching(aid, CODE_CHAT_A), 60)
        seen.append(False)
        signal("снят")
        wait_for(lambda: bridge.watching(aid, CODE_CHAT_A), 60)
        seen.append(True)
        signal("снова")

    named = "fetch('/api/agents').then((r) => r.json()).then((j) => j.items.some((a) => a.name === '{}'))"
    spy = threading.Thread(target=record, daemon=True)
    spy.start()
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            *code_send("Работай"), ("wait", "document.querySelector('#code-chat').dataset.running === '1'"
                                            " && document.querySelector('.code-msg.assistant .md')?.textContent.trim() === 'Работаю'"),
            hide(), ("wait", named.format("снят")),
            ("eval", "md", "document.querySelector('.code-msg.assistant .md').textContent.trim()"),
            show(), ("wait", named.format("снова")),
            ("wait", "document.querySelector('#code-chat').dataset.running === '1'"),
            ("eval", "back", "document.querySelector('#code-chat').dataset.running"),
            ("eval", "md2", "document.querySelector('.code-msg.assistant .md').textContent.trim()"),
            ("eval", "assistants", "document.querySelectorAll('#code-feed .code-msg.assistant').length")])
    finally:
        spy.join(5)
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert seen == [True, False, True], seen
    assert v["md"] == v["md2"] == "Работаю" and v["back"] == "1" and v["assistants"] == 1
    ops = [q["op"] for q in machine.requests]
    assert "watch" in ops[ops.index("turn"):], ops                  # по возвращении — снова live
    assert CODE_CHAT_A in machine.running
    assert not known_refusals(problems(r), 409), problems(r)


CODE_LOCK_NOW = f"(async () => {{ await {API}.api('POST', '/api/lock/now'); return true; }})()"


def test_code_closed_event_leads_to_the_pin_and_back_to_the_chat(browser, live):
    """Ход идёт (машина не шлёт done), студия на телефоне заперлась: поток кончается событием closed — после
    отпирания студии панель PIN этого компьютера, верный PIN — снова этот чат со снимком хода. Перенаправления к
    PIN и назад записей в историю браузера не добавляют; ход на машине идёт."""
    from fake_bridge import pid
    login = "code-closed"
    live.add_user(login)
    machine = code_machine({CODE_CHAT_A: code_session("Чат A")})
    machine.script = [{"k": "text", "text": "Работаю"}]
    aid, sb = code_online(live, login, machine)
    here = f"code/{aid}/{pid('alpha')}/{CODE_CHAT_A}"
    try:
        r = browser.drive(live.url + "/", [*phone_with_key(login), *code_open(aid),
            *go(here), code_loaded(CODE_CHAT_A), *code_send("Работай"),
            ("wait", "document.querySelector('#code-chat').dataset.running === '1'"
                     " && document.querySelector('.code-msg.assistant .md')?.textContent.trim() === 'Работаю'"),
            ("eval", "len0", "history.length"),
            ("eval", "lock", CODE_LOCK_NOW),         # поток открыт: closed приходит событием, не отказом запроса
            ("wait", LOCK_OPEN), ("wait", PASSKEY_READY), ("click", "#lock-open"), ("wait", f"!{LOCK_OPEN}"),
            ("wait", CODE_PIN_SHOWN),
            ("eval", "pin", "[location.hash, document.querySelector('#code-pin').dataset.id]"),
            ("eval", "old", "!!(window.oldChat = document.querySelector('#code-chat'))"),
            ("type", "#code-pin-input", CODE_PIN), ("click", "#code-pin-open"),
            ("wait", f"location.hash === '#/{here}' && document.querySelector('#code-chat') !== window.oldChat"
                     " && document.querySelector('#code-chat')?.dataset.loaded === '1'"
                     " && document.querySelector('#code-chat').dataset.running === '1'"
                     " && document.querySelector('.code-msg.assistant .md')?.textContent.trim() === 'Работаю'"),
            ("eval", "len1", "history.length")], **PHONE)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["pin"] == [f"#/{here}", str(aid)] and v["len1"] == v["len0"]
    assert [q.get("pin") for q in code_requests(machine, "open")] == [None, CODE_PIN, CODE_PIN]
    assert len(code_requests(machine, "turn")) == 1 and CODE_CHAT_A in machine.running
    # событие, а не запасной путь (поток без конца → live → 409 closed): watch — до хода и после PIN, не третий
    assert len(code_requests(machine, "watch")) == 2
    assert not known_refusals(problems(r), 409, 423), problems(r)


def test_code_closed_at_send_keeps_the_draft(browser, live):
    """Набранное задание не теряется: «Отправить» на запертой студии — отпирание, 409 closed, панель PIN; после
    PIN — снова этот чат, задание в поле (черновик — только в памяти страницы, не в хранилищах)."""
    from fake_bridge import pid
    login = "code-draft"
    live.add_user(login)
    machine = code_machine({CODE_CHAT_A: code_session("Чат A")})
    aid, sb = code_online(live, login, machine)
    here = f"code/{aid}/{pid('alpha')}/{CODE_CHAT_A}"
    try:
        r = browser.drive(live.url + "/", [*phone_with_key(login), *code_open(aid),
            *go(here), code_loaded(CODE_CHAT_A),
            ("wait", "document.querySelector('#code-chat').dataset.running === '0'"),
            ("type", "#code-text", "Черновик задания"),
            ("eval", "lock", CODE_LOCK_NOW),         # потока нет (простой): закрытость увидит «Отправить»
            ("click", "#code-send"),
            ("wait", LOCK_OPEN), ("wait", PASSKEY_READY), ("click", "#lock-open"), ("wait", f"!{LOCK_OPEN}"),
            ("wait", CODE_PIN_SHOWN), ("eval", "old", "!!(window.oldChat = document.querySelector('#code-chat'))"),
            ("type", "#code-pin-input", CODE_PIN), ("click", "#code-pin-open"),
            ("wait", f"location.hash === '#/{here}' && document.querySelector('#code-chat') !== window.oldChat"
                     " && document.querySelector('#code-chat')?.dataset.loaded === '1'"),
            ("eval", "field", "document.querySelector('#code-text').value"),
            ("eval", "stored", "JSON.stringify([{ ...localStorage }, { ...sessionStorage }, location.href])"
                               ".includes('Черновик')")], **PHONE)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["field"] == "Черновик задания" and v["stored"] is False
    assert len(code_requests(machine, "turn")) == 1 and not machine.running      # отказ closed — хода нет
    assert not known_refusals(problems(r), 409, 423), problems(r)


def test_code_offline_at_send_waits_for_the_computer(browser, live):
    """Компьютер пропал до «Отправить»: 409 offline — «Компьютер пропал со связи — жду его» и задание в поле;
    мост вернулся (новый канал закрыл сессии) — компьютер без PIN открывается сам, и снова этот чат с заданием."""
    import bridge
    from fake_bridge import SocketBridge, pid
    login = "code-send-off"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Чат A")})
    aid, sb = code_online(live, login, machine)
    state = {}

    def drop_and_return():
        wait_for(lambda: code_requests(machine, "watch"), 60)
        sb.stop()
        wait_for(lambda: aid not in bridge.LINKS)
        time.sleep(3)
        state["sb"] = SocketBridge(live.url, sb.key, machine).start()

    mover = threading.Thread(target=drop_and_return, daemon=True)
    mover.start()
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("wait", "fetch('/api/agents').then((r) => r.json()).then((j) => j.items.every((a) => !a.online))"),
            *code_send("Задание без связи"),
            ("wait", f"document.querySelector('#code-status')?.textContent === '{CODE_WAITING}'"),
            ("eval", "field", "document.querySelector('#code-text').value"),
            ("eval", "mine", "document.querySelectorAll('#code-feed .code-msg.user').length"),
            ("wait", f"location.hash === '#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}'"
                     " && document.querySelector('#code-chat')?.dataset.loaded === '1'"
                     f" && document.querySelector('#code-status')?.textContent !== '{CODE_WAITING}'"),
            ("eval", "back", "document.querySelector('#code-text').value")], width=320, mobile=True)
        mover.join(10)
    finally:
        sb.stop()
        if "sb" in state:
            state["sb"].stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["field"] == "Задание без связи" and v["mine"] == 0 and v["back"] == "Задание без связи"
    assert code_requests(machine, "turn") == []
    assert [q.get("pin") for q in code_requests(machine, "open")] == [None, None]
    assert not known_refusals(problems(r), 409), problems(r)


def test_code_late_closed_answer_does_not_pull_the_owner_back(browser, live):
    """Ответ 409 closed на «Остановить» пришёл, когда владелец уже ушёл из чата в другой раздел: страница его
    никуда не уводит."""
    login = "code-late"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Чат A")})
    machine.script = [{"k": "text", "text": "Работаю"}]
    aid, sb = code_online(live, login, machine)
    late = """(() => { const real = window.fetch; window.fetch = (u, o) => String(u).endsWith('/stop')
      ? new Promise((r) => setTimeout(r, 500)).then(() => new Response(JSON.stringify({ detail: { code: 'closed',
        message: 'компьютер закрыт' } }), { status: 409, headers: { 'content-type': 'application/json' } }))
      : real(u, o); return true; })()"""
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            *code_send("Работай"), ("wait", "document.querySelector('#code-chat').dataset.running === '1'"),
            ("eval", "late", late), ("click", "#code-stop"), *go("chat"),
            ("eval", "hash", settled("location.hash", 1200))], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert r["values"]["hash"] == "#/chat"
    assert not known_refusals(problems(r), 409), problems(r)


def test_code_draft_goes_once_the_turn_is_accepted_even_after_leaving(browser, live):
    """«Отправить», и сразу уход из чата, пока ответ ещё не пришёл: запрос не обрывается, ход принят — черновика
    больше нет, и вернувшись в чат, задание в поле снова не появится (второй раз его не отправить по ошибке)."""
    from fake_bridge import pid
    login = "code-sent"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Чат A")})
    machine.script = [{"k": "text", "text": "Работаю"}]
    aid, sb = code_online(live, login, machine)
    # ответ на ход приходит странице на 800 мс позже; оборванный за это время запрос — AbortError, как у fetch
    slow = """(() => { const real = window.fetch; window.fetch = (u, o) => !String(u).endsWith('/turn') ? real(u, o)
      : real(u, o).then((r) => new Promise((ok, fail) => setTimeout(() => (o && o.signal && o.signal.aborted
        ? fail(new DOMException('aborted', 'AbortError')) : ok(r)), 800))); return true; })()"""
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("eval", "slow", slow), *code_send("Один раз"), *go("chat"),
            ("wait", "location.hash === '#/chat'"), ("eval", "pause", settled("true", 1200)),
            *go(f"code/{aid}/{pid('alpha')}/{CODE_CHAT_A}"), code_loaded(CODE_CHAT_A),
            ("eval", "field", "document.querySelector('#code-text').value")], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert r["values"]["field"] == ""
    assert len(code_requests(machine, "turn")) == 1 and CODE_CHAT_A in machine.running
    assert not known_refusals(problems(r), 409), problems(r)


# файлы и git проекта (code-files.js): список, правка, просмотр .md и .pdf, статус git, ссылка из шага чата

CODE_APP = b"print(1)\r\nprint(2)\r\n"
CODE_CONFLICT = "Файл изменился на компьютере — откройте его заново, чтобы не затереть правки"
CODE_FILE_NAMES = "[...document.querySelectorAll('#code-files li[data-name]')].map((li) => li.dataset.name)"
#: части пути файлов (без разделителей «›»)
CODE_PATH = "[...document.querySelectorAll('#code-path > :not([aria-hidden])')].map((e) => e.textContent)"
CODE_FILE_STATUS = "document.querySelector('#code-file-status').textContent"
CODE_STORED = "JSON.stringify([{ ...localStorage }, { ...sessionStorage }, location.href])"


def code_files_machine(chats=None):
    """Компьютер без PIN; у alpha — папки src и src/lib, README.md, двухстраничный PDF, двоичный и большой файлы."""
    from fake_bridge import FakeMachine
    from scans import scan_pdf
    files = {"README.md": "# Заголовок\n\nТекст **жирный**.\n".encode(), "src/app.py": CODE_APP,
             "src/lib/util.py": b"x = 1\n", "a.pdf": scan_pdf("first page", "second page"), "bin.dat": b"ab\0cd",
             "big.txt": b"x" * (2 * 1024 * 1024 + 1)}
    return FakeMachine(projects={"alpha": {"files": files, "chats": chats or {},
        "git": {"branch": "main", "upstream": "origin/main", "ahead": 0, "behind": 0, "files": []}}})


def code_tab(aid, tab, project="alpha"):
    """«Код» (панель сама открывает компьютер без PIN) → проект → вид."""
    from fake_bridge import pid
    return [*go("code"), ("wait", f"document.querySelector(\"#code-agents li[data-id='{aid}']\")"),
            ("wait", f"document.querySelector(\"#code-projects li[data-pid='{pid(project)}']\")"),
            ("click", f"#code-projects li[data-pid='{pid(project)}'] a"),
            ("wait", f"document.querySelector('#code-place .code-place-project')?.textContent === '{project}'"
                     f" && !!document.querySelector('#code-tab-{tab}')"),
            ("click", f"#code-tab-{tab}")]


def code_item(name):
    return f"#code-files li[data-name='{name}'] button"


def code_root_names(n):
    return ("wait", f"document.querySelectorAll('#code-files > li[data-name]').length === {n}"
                    " && document.querySelector('#code-file').hidden")


@pytest.mark.parametrize("width", [320, 1280])
def test_code_compact_header_and_git_file_tree(browser, live, width):
    """Компактная шапка без кнопок создания; дерево раскрывается на месте и помечает изменения Git."""
    from test_codex import CATALOG
    login = f"code-tree-{width}"
    live.add_user(login, theme="dark" if width == 320 else "light")
    machine = code_files_machine()
    machine.codex = CATALOG
    machine.projects["alpha"]["git"] = {
        "branch": "stage-5g", "upstream": "origin/stage-5g", "ahead": 2, "behind": 1,
        "files": [{"path": "src/app.py", "status": " M"}, {"path": "src/lib/util.py", "status": "??"},
                  {"path": "README.md", "status": "M "}]}
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_tab(aid, "files"),
            ("wait", "!!document.querySelector('#code-files > li[data-name=\"README.md\"]')"),
            ("wait", "document.querySelector('#code-repo-status')?.textContent.includes('stage-5g')"),
            ("eval", "header", "document.querySelector('#code-repo-status').textContent"),
            ("eval", "duplicates", "document.querySelectorAll('#code-chats .code-new-chat').length"),
            ("eval", "oneLine", "!document.querySelector('.code-starts')"),
            ("eval", "gitBeforeChat", "(() => { const g = document.querySelector('#code-repo-status').getBoundingClientRect();"
                                         " const c = document.querySelector('#code-tab-chat').getBoundingClientRect();"
                                         " return g.right <= c.left && Math.abs(g.top + g.height / 2 - c.top - c.height / 2) < 2; })()"),
            ("click", code_item("src")), ("wait", "document.querySelector(\"#code-files li[data-name='app.py']\")"),
            ("eval", "rootStays", "!!document.querySelector(\"#code-files > li[data-name='README.md']\")"),
            ("eval", "expanded", "document.querySelector(\"#code-files li[data-name='src']\").getAttribute('aria-expanded')"),
            ("eval", "modified", "document.querySelector(\"#code-files li[data-path='src/app.py'] .code-file-git\").textContent"),
            ("eval", "parentMark", "document.querySelector(\"#code-files li[data-path='src'] .code-file-git\").textContent"),
            ("click", code_item("lib")), ("wait", "document.querySelector(\"#code-files li[data-name='util.py']\")"),
            ("eval", "untracked", "document.querySelector(\"#code-files li[data-path='src/lib/util.py'] .code-file-git\").textContent"),
            ("eval", "fits", NO_OVERFLOW),
            ("click", code_item("src")),
            ("eval", "collapsed", "document.querySelector(\"#code-files li[data-name='src'] > ul\").hidden"),
            ("click", code_item("src")),
            ("eval", "nestedKept", "document.querySelector(\"#code-files li[data-name='lib']\").getAttribute('aria-expanded')")],
            width=width, mobile=width == 320)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert "stage-5g*" in v["header"] and "↑2" in v["header"] and "↓1" in v["header"]
    assert v["duplicates"] == 0 and (width == 320 or v["oneLine"])
    assert v["gitBeforeChat"]
    assert v["rootStays"] and v["expanded"] == "true" and v["modified"] == "M"
    assert v["parentMark"] == "•" and v["untracked"] == "U" and v["fits"]
    assert v["collapsed"] and v["nestedKept"] == "true"
    assert [q["path"] for q in code_requests(machine, "files")] == ["", "src", "src/lib"]
    assert not problems(r), problems(r)


def test_code_git_refresh_updates_tree_without_losing_the_editor(browser, live):
    """Сохранение обновляет общий статус; внешняя правка добавляет файл в раскрытое дерево, редактор остаётся."""
    login = "code-tree-refresh"
    live.add_user(login)
    machine = code_files_machine()
    project = machine.projects["alpha"]
    save = machine._op_save
    read_git = machine._op_git

    def changed(req, events):
        result = save(req, events)
        project["git"]["files"] = [{"path": "src/app.py", "status": " M"}]
        return result

    def external_commit(req, events):
        if len(code_requests(machine, "git")) >= 3:
            project["files"]["src/new.py"] = b"new = 1\n"
            project["git"] = {"branch": "release", "upstream": None, "ahead": 0, "behind": 0,
                              "files": [{"path": "src/new.py", "status": "??"}]}
        return read_git(req, events)

    machine._op_save = changed
    machine._op_git = external_commit
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_tab(aid, "files"),
            code_root_names(5), ("click", code_item("src")),
            ("wait", "document.querySelector(\"#code-files li[data-name='app.py']\")"),
            ("click", code_item("app.py")), ("wait", "document.querySelector('#code-editor')"),
            set_value("#code-editor", "print('saved')\n"),
            ("click", code_item("src")), ("eval", "kept", "document.querySelector('#code-editor').value"),
            ("eval", "noDialog", f"!{CODE_UNSAVED}"), ("click", code_item("src")),
            ("click", "#code-save"),
            ("wait", "document.querySelector('#code-repo-status').textContent.includes('main*')"
                     " && document.querySelector(\"li[data-path='src/app.py'] > button\").dataset.git === 'modified'"),
            ("click", "#code-repo-status"),
            ("wait", "document.querySelector('#code-git .code-git-branch')?.textContent === 'release'"
                     " && document.querySelector(\"li[data-path='src/new.py']\")"),
            ("eval", "header", "document.querySelector('#code-repo-status').textContent"),
            ("click", "#code-tab-files"),
            ("wait", "document.querySelector('#code-tab-files').getAttribute('aria-selected') === 'true'"
                     " && document.querySelector('#code-files').getClientRects().length > 0"),
            ("eval", "editor", "document.querySelector('#code-editor').value"),
            ("eval", "mark", "document.querySelector(\"li[data-path='src/new.py'] .code-file-git\").textContent"),
            ("eval", "oldClean", "document.querySelector(\"li[data-path='src/app.py'] > button\").dataset.git"),
            ("eval", "focus", "document.querySelector(\"li[data-path='src'] > button\").focus()"),
            ("press", "ArrowRight"),
            ("wait", "document.activeElement.closest('li')?.dataset.path === 'src/lib'"),
            ("eval", "keyChild", "document.activeElement.closest('li').dataset.path"),
            ("press", "ArrowLeft"),
            ("eval", "keyParent", "document.activeElement.closest('li').dataset.path")], width=1280)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["kept"] == v["editor"] == "print('saved')\n" and v["noDialog"]
    assert "release*" in v["header"] and v["mark"] == "U" and v["oldClean"] == ""
    assert v["keyChild"] == "src/lib" and v["keyParent"] == "src"
    assert not problems(r), problems(r)


def test_code_tree_refresh_closed_keeps_unsaved_text_for_the_pin(browser, live):
    """Фоновое обновление дерева получило closed: PIN возвращает несохранённый текст с прежней версией."""
    login = "code-tree-pin"
    live.add_user(login)
    machine = code_files_machine()
    read_git = machine._op_git

    def close_before_refresh(req, events):
        if len(code_requests(machine, "git")) == 2:
            machine.projects["alpha"]["git"]["files"] = [{"path": "src/app.py", "status": " M"}]
            machine.pin = CODE_PIN
            machine.open_sessions.clear()
        return read_git(req, events)

    machine._op_git = close_before_refresh
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_tab(aid, "files"),
            code_root_names(5), ("click", code_item("src")),
            ("wait", "document.querySelector(\"#code-files li[data-name='app.py']\")"),
            ("click", code_item("app.py")), ("wait", "document.querySelector('#code-editor')"),
            set_value("#code-editor", "print('unsaved')\n"),
            ("eval", "refresh", "document.dispatchEvent(new Event('visibilitychange'))"),
            ("wait", CODE_PIN_SHOWN), ("type", "#code-pin-input", CODE_PIN), ("click", "#code-pin-open"),
            ("wait", "document.querySelector('#code-editor')?.value === \"print('unsaved')\\n\""),
            ("click", "#code-save"), ("wait", f"{CODE_FILE_STATUS} === 'Сохранено'")], width=1280)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert machine.projects["alpha"]["files"]["src/app.py"] == b"print('unsaved')\r\n"
    assert not known_refusals(problems(r), 409), problems(r)


def test_code_files_open_edit_and_save(browser, live):
    """«Файлы»: корень (папки сначала), папка src, app.py в моноширинном редакторе; правка — «Сохранить» →
    «Сохранено», у машины новые байты с прежними концами строк \\r\\n, второе сохранение — с новым version; путь
    — только в теле запросов: ни в адресе, ни в хранилищах; 320 px без прокрутки вбок."""
    from fake_bridge import _version, pid
    login = "code-files"
    live.add_user(login)
    machine = code_files_machine()
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_tab(aid, "files"),
            code_root_names(5), ("eval", "root", CODE_FILE_NAMES), ("eval", "rootPath", CODE_PATH),
            ("click", code_item("src")), ("wait", "document.querySelector(\"#code-files li[data-name='app.py']\")"),
            ("eval", "src", CODE_FILE_NAMES), ("eval", "srcPath", CODE_PATH),
            ("click", code_item("app.py")), ("wait", "document.querySelector('#code-editor')"),
            ("eval", "text", "document.querySelector('#code-editor').value"),
            ("eval", "filePath", CODE_PATH),
            ("eval", "save", "document.querySelector('#code-save').textContent"),
            ("eval", "mono", "getComputedStyle(document.querySelector('#code-editor')).fontFamily"),
            ("eval", "fits", NO_OVERFLOW),
            set_value("#code-editor", "print(1)\nprint(3)\n"), ("click", "#code-save"),
            ("wait", f"{CODE_FILE_STATUS} !== ''"), ("eval", "status", CODE_FILE_STATUS),
            set_value("#code-editor", "print(4)\n"), ("eval", "cleared", CODE_FILE_STATUS), ("click", "#code-save"),
            ("wait", f"{CODE_FILE_STATUS} === 'Сохранено'"),
            ("eval", "hash", "location.hash"), ("eval", "stored", CODE_STORED),
            ("click", "#code-path .code-path-link"), code_root_names(5),
            ("eval", "back", CODE_PATH)], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["root"] == ["src", "README.md", "a.pdf", "big.txt", "bin.dat"] and v["rootPath"] == ["alpha"]
    assert v["src"] == ["src", "lib", "app.py", "README.md", "a.pdf", "big.txt", "bin.dat"] and v["srcPath"] == ["alpha", "src"]
    assert v["text"] == "print(1)\nprint(2)\n" and v["filePath"] == ["alpha", "src", "app.py"]
    assert v["save"] == "Сохранить" and "monospace" in v["mono"] and v["fits"] is True
    assert v["status"] == "Сохранено" and v["cleared"] == ""
    assert v["hash"] == f"#/code/{aid}/{pid('alpha')}?tab=files"
    assert "app.py" not in v["stored"] and "print" not in v["stored"]
    assert v["back"] == ["alpha"]
    assert machine.projects["alpha"]["files"]["src/app.py"] == b"print(4)\r\n"
    saves = code_requests(machine, "save")
    assert [(q["path"], q["text"], q["version"]) for q in saves] == [
        ("src/app.py", "print(1)\r\nprint(3)\r\n", _version(CODE_APP)),
        ("src/app.py", "print(4)\r\n", _version(b"print(1)\r\nprint(3)\r\n"))]
    listed = [q["path"] for q in code_requests(machine, "files")]
    assert listed[:2] == ["", "src"] and len(listed) >= 3 and all(path == "src" for path in listed[2:])
    assert [q["path"] for q in code_requests(machine, "read")] == ["src/app.py"]
    assert not problems(r), problems(r)


def test_code_file_conflict_is_said_in_words(browser, live):
    """Файл открыт, а машина его меняет (его поправил Claude): «Сохранить» — 409 conflict, слова из «Текстов»;
    на машине — её правка, в редакторе — своя."""
    login = "code-conflict"
    live.add_user(login)
    machine = code_files_machine()
    read = machine._op_read

    def read_then_change(req, events):
        out = read(req, events)
        machine.projects["alpha"]["files"]["src/app.py"] = b"print('claude')\n"
        return out

    machine._op_read = read_then_change
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_tab(aid, "files"),
            code_root_names(5), ("click", code_item("src")),
            ("wait", "document.querySelector(\"#code-files li[data-name='app.py']\")"),
            ("click", code_item("app.py")), ("wait", "document.querySelector('#code-editor')"),
            set_value("#code-editor", "print('мой')\n"), ("click", "#code-save"),
            ("wait", f"{CODE_FILE_STATUS} !== ''"), ("eval", "status", CODE_FILE_STATUS),
            ("eval", "error", "document.querySelector('#code-file-status').classList.contains('error')"),
            ("eval", "text", "document.querySelector('#code-editor').value"),
            ("eval", "fits", NO_OVERFLOW)], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["status"] == CODE_CONFLICT and v["error"] is True and v["fits"] is True
    assert v["text"] == "print('мой')\n"
    assert machine.projects["alpha"]["files"]["src/app.py"] == b"print('claude')\n"
    assert len(code_requests(machine, "save")) == 1
    assert not known_refusals(problems(r), 409), problems(r)


CODE_UNSAVED = is_open("#code-unsaved")


def test_code_unsaved_edit_asks_before_leaving(browser, live):
    """Правка не сохранена, а уходят на папку (путь) или вкладку — окно студии «Изменения не сохранены»:
    «Остаться» — файл и правка на месте; «Уйти без сохранения» — правка отброшена. Сохранённое — без окна."""
    login = "code-unsaved"
    live.add_user(login)
    machine = code_files_machine()
    aid, sb = code_online(live, login, machine)
    editor = "document.querySelector('#code-editor').value"
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_tab(aid, "files"),
            code_root_names(5), ("click", code_item("src")),
            ("wait", "document.querySelector(\"#code-files li[data-name='app.py']\")"),
            ("click", code_item("app.py")), ("wait", "document.querySelector('#code-editor')"),
            set_value("#code-editor", "print('правка')\n"),
            ("click", "#code-path .code-path-link"), ("wait", CODE_UNSAVED),
            ("eval", "dialog", """(() => { const d = document.querySelector('#code-unsaved');
              return [d.querySelector('h2').textContent, [...d.querySelectorAll('.actions button')].map((b) => b.textContent)]; })()"""),
            ("eval", "fits", NO_OVERFLOW),
            ("click", "#code-unsaved .dialog-close"), ("wait", f"!{CODE_UNSAVED}"),
            ("eval", "stayed", f"[{editor}, !document.querySelector('#code-file').hidden]"),
            ("click", "#code-tab-git"), ("wait", CODE_UNSAVED),
            ("eval", "gitWaits", "document.querySelector('#code-tab-files').getAttribute('aria-selected')"),
            ("click", "#code-unsaved-leave"),
            ("wait", f"!{CODE_UNSAVED} && document.querySelector('#code-tab-git').getAttribute('aria-selected') === 'true'"),
            ("click", "#code-tab-files"), ("eval", "reverted", editor),
            set_value("#code-editor", "print('ещё')\n"), ("click", "#code-save"),
            ("wait", f"{CODE_FILE_STATUS} === 'Сохранено'"),
            ("click", "#code-path .code-path-link"), code_root_names(5),
            ("eval", "noDialog", f"!{CODE_UNSAVED}")], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["dialog"] == ["Изменения не сохранены", ["Остаться", "Уйти без сохранения"]] and v["fits"] is True
    assert v["stayed"] == ["print('правка')\n", True] and v["gitWaits"] == "true"
    assert v["reverted"] == "print(1)\nprint(2)\n" and v["noDialog"] is True
    assert [q["text"] for q in code_requests(machine, "save")] == ["print('ещё')\r\n"]
    assert not known_refusals(problems(r), 409), problems(r)             # git: у проекта нет репозитория


#: beforeunload страницы отменён (закрытие вкладки спросит)
CODE_UNLOAD_ASKS = ("(() => { const e = new Event('beforeunload', { cancelable: true }); window.dispatchEvent(e);"
                    " return e.defaultPrevented; })()")


def test_code_unsaved_edit_survives_a_project_link(browser, live):
    """Правка не сохранена, а уходят по ссылке проекта в панели или в другой раздел студии: адрес возвращается, окно
    «Изменения не сохранены»; «Остаться» — файл, правка и адрес на месте; «Уйти без сохранения» — туда, куда шли.
    Закрытие вкладки с правкой — beforeunload; без правки и после ухода — нет."""
    from fake_bridge import pid
    login = "code-unsaved-level"
    live.add_user(login)
    machine = code_files_machine()
    machine.projects["beta"] = {"files": {}, "chats": {}}
    aid, sb = code_online(live, login, machine)
    here = f"#/code/{aid}/{pid('alpha')}?tab=files"
    editor = "document.querySelector('#code-editor')?.value"
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_tab(aid, "files"),
            code_root_names(5), ("click", code_item("src")),
            ("wait", "document.querySelector(\"#code-files li[data-name='app.py']\")"),
            ("click", code_item("app.py")), ("wait", "document.querySelector('#code-editor')"),
            ("eval", "clean", CODE_UNLOAD_ASKS),
            set_value("#code-editor", "print('правка')\n"), ("eval", "dirty", CODE_UNLOAD_ASKS),
            ("click", "#code-panel-toggle"), ("wait", CODE_PANEL_OPEN),
            ("click", f"#code-projects li[data-pid='{pid('beta')}'] a"), ("wait", CODE_UNSAVED),
            ("eval", "asked", "location.hash"),
            ("click", "#code-unsaved .dialog-close"), ("wait", f"!{CODE_UNSAVED}"),
            ("eval", "stayed", f"[location.hash, {editor}, document.querySelector('#code-place .code-place-project').textContent]"),
            *go("chat"), ("wait", CODE_UNSAVED),
            ("eval", "kept", "[location.hash, document.querySelector('#rail .tab[aria-current=page]').dataset.section]"),
            ("click", "#code-unsaved-leave"),
            ("wait", "location.hash === '#/chat'"
                     " && document.querySelector('#rail .tab[aria-current=page]')?.dataset.section === 'chat'"
                     " && !document.querySelector('#code-editor')"),
            ("eval", "left", CODE_UNLOAD_ASKS)], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["clean"] is False and v["dirty"] is True
    assert v["asked"] == here and v["stayed"] == [here, "print('правка')\n", "alpha"]
    assert v["kept"] == [here, "code"] and v["left"] is False
    assert code_requests(machine, "save") == []
    assert not problems(r), problems(r)


def test_code_unsaved_edit_back_and_stay_keeps_the_history(browser, live):
    """Ревью: «Назад» с несохранённой правкой и «Остаться» писали адрес файла в прежнюю запись истории — запись, с
    которой пришли к файлу, пропадала. Теперь «Остаться» — файл, правка и адрес на месте, записей столько же;
    «Сохранить» и снова «Назад» — проект, откуда пришли."""
    from fake_bridge import pid
    login = "code-unsaved-back"
    live.add_user(login)
    machine = code_files_machine()
    machine.projects["beta"] = {"files": {}, "chats": {}}
    aid, sb = code_online(live, login, machine)
    here = f"#/code/{aid}/{pid('alpha')}?tab=files"
    editor = "document.querySelector('#code-editor')?.value"
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), *code_project(aid, "beta"),
            ("click", "#code-panel-toggle"), ("wait", CODE_PANEL_OPEN), *code_project(aid, "alpha"),
            ("click", "#code-tab-files"),
            code_root_names(5), ("click", code_item("src")),
            ("wait", "document.querySelector(\"#code-files li[data-name='app.py']\")"),
            ("click", code_item("app.py")), ("wait", "document.querySelector('#code-editor')"),
            set_value("#code-editor", "print('правка')\n"),
            ("eval", "length", "window.length0 = history.length"),
            ("eval", "back", "history.back()"), ("wait", CODE_UNSAVED),
            ("click", "#code-unsaved .dialog-close"), ("wait", f"!{CODE_UNSAVED}"),
            ("eval", "stayed", f"[location.hash, {editor}, history.length === window.length0]"),
            ("click", "#code-save"), ("wait", f"{CODE_FILE_STATUS} === 'Сохранено'"),
            ("eval", "again", "history.back()"),
            ("wait", f"location.hash === '#/code/{aid}/{pid('beta')}' && !!document.querySelector('#code-chats')"),
            ("eval", "landed", f"[location.hash, !{CODE_UNSAVED}]")], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["stayed"] == [here, "print('правка')\n", True]
    assert v["landed"] == [f"#/code/{aid}/{pid('beta')}", True]
    assert [q["text"] for q in code_requests(machine, "save")] == ["print('правка')\r\n"]
    assert not problems(r), problems(r)


def test_code_unsaved_edit_back_to_another_section_keeps_the_history(browser, live):
    """«Назад» с несохранённой правкой ведёт на запись другого раздела (чат): метку записи ставит роутер app.js
    всем разделам, и «Остаться» не затирает запись чата; «Сохранить» и снова «Назад» — чат."""
    from fake_bridge import pid
    login = "code-unsaved-back-chat"
    live.add_user(login)
    machine = code_files_machine()
    aid, sb = code_online(live, login, machine)
    files = f"#/code/{aid}/{pid('alpha')}?tab=files"
    editor = "document.querySelector('#code-editor')?.value"
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_tab(aid, "files"),
            code_root_names(5), *go("chat"), ("wait", "location.hash === '#/chat'"),
            ("eval", "go", f"location.hash = '{files}'"), code_root_names(5), ("click", code_item("src")),
            ("wait", "document.querySelector(\"#code-files li[data-name='app.py']\")"),
            ("click", code_item("app.py")), ("wait", "document.querySelector('#code-editor')"),
            set_value("#code-editor", "print('правка')\n"),
            ("eval", "back", "history.back()"), ("wait", CODE_UNSAVED),
            ("click", "#code-unsaved .dialog-close"), ("wait", f"!{CODE_UNSAVED}"),
            ("eval", "stayed", f"[location.hash, {editor}]"),
            ("click", "#code-save"), ("wait", f"{CODE_FILE_STATUS} === 'Сохранено'"),
            ("eval", "again", "history.back()"), ("wait", "location.hash === '#/chat'"),
            ("eval", "landed", f"[location.hash, !{CODE_UNSAVED}]")], width=1280)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["stayed"] == [files, "print('правка')\n"]
    assert v["landed"] == ["#/chat", True]
    assert not problems(r), problems(r)


@pytest.mark.parametrize("claude_edits", [False, True])
def test_code_save_on_a_closed_computer_keeps_the_edit_through_the_pin(browser, live, claude_edits):
    """Студия заперлась с правленым файлом: «Сохранить» — отпирание, 409 closed, панель PIN; верный PIN — снова
    «Файлы» с этим файлом и правкой, version — прежний; «Сохранить» — у машины новые байты. Поправил файл за это
    время Claude — 409 conflict, его правка цела. Правки нет ни в хранилищах, ни в адресе."""
    from fake_bridge import _version, pid
    login = f"code-save-pin-{int(claude_edits)}"
    live.add_user(login)
    machine = code_files_machine()
    machine.pin = CODE_PIN
    handle = machine.handle

    def claude_then(req):                    # отказ closed «Сохранить» — и Claude правит файл, пока вводят PIN
        out = handle(req)
        if claude_edits and req.get("op") == "save" and not out[0]["ok"]:
            machine.projects["alpha"]["files"]["src/app.py"] = b"print('claude')\n"
        return out

    machine.handle = claude_then
    aid, sb = code_online(live, login, machine)
    editor = "document.querySelector('#code-editor')?.value"
    try:
        r = browser.drive(live.url + "/", [*phone_with_key(login), *code_open(aid)[:-1],   # проект у машины один
            ("wait", f"document.querySelector(\"#code-projects li[data-pid='{pid('alpha')}']\")"),
            ("click", f"#code-projects li[data-pid='{pid('alpha')}'] a"),
            ("wait", "!!document.querySelector('#code-tab-files')"), ("click", "#code-tab-files"),
            code_root_names(5), ("click", code_item("src")),
            ("wait", "document.querySelector(\"#code-files li[data-name='app.py']\")"),
            ("click", code_item("app.py")), ("wait", "document.querySelector('#code-editor')"),
            set_value("#code-editor", "print('моё')\n"),
            ("eval", "lock", CODE_LOCK_NOW), ("click", "#code-save"),
            ("wait", LOCK_OPEN), ("wait", PASSKEY_READY), ("click", "#lock-open"), ("wait", f"!{LOCK_OPEN}"),
            ("wait", CODE_PIN_SHOWN),
            ("type", "#code-pin-input", CODE_PIN), ("click", "#code-pin-open"),
            ("wait", f"{editor} === \"print('моё')\\n\""),
            ("eval", "where", f"[location.hash, {CODE_PATH}]"),
            ("eval", "stored", f"{CODE_STORED}.includes('моё')"),
            ("click", "#code-save"), ("wait", f"{CODE_FILE_STATUS} !== ''"),
            ("eval", "status", CODE_FILE_STATUS)], **PHONE)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["where"] == [f"#/code/{aid}/{pid('alpha')}?tab=files", ["alpha", "src", "app.py"]]
    assert v["stored"] is False
    saves = code_requests(machine, "save")              # первый — отказ closed, второй — после PIN
    if claude_edits:
        assert v["status"] == CODE_CONFLICT
        assert machine.projects["alpha"]["files"]["src/app.py"] == b"print('claude')\n"
        assert [q["version"] for q in saves] == [_version(CODE_APP)] * 2     # не version перечитанного файла
    else:
        assert v["status"] == "Сохранено"
        assert machine.projects["alpha"]["files"]["src/app.py"] == "print('моё')\r\n".encode()
        assert [(q["text"], q["version"]) for q in saves] == [("print('моё')\r\n", _version(CODE_APP))] * 2
    assert not known_refusals(problems(r), 409, 423), problems(r)


def test_code_markdown_and_pdf_view(browser, live):
    """README.md: «Текст» / «Вид» — разметка с заголовком h1; a.pdf — страница картинкой, «›» — «Страница 2 из 2»,
    прежний blob URL освобождён (и при уходе со страницы файла); двоичный и больше 2 МиБ — слова. Широкий экран:
    список папки рядом с файлом."""
    login = "code-view"
    live.add_user(login)
    machine = code_files_machine()
    aid, sb = code_online(live, login, machine)
    spy = """(() => { window.revoked = []; const real = URL.revokeObjectURL.bind(URL);
      URL.revokeObjectURL = (u) => { window.revoked.push(u); real(u); }; return true; })()"""
    page_is = ("document.querySelector('#code-pdf-page')?.textContent === 'Страница {n} из 2'"
               " && document.querySelector('#code-pdf img').complete"
               " && document.querySelector('#code-pdf img').naturalWidth > 0")
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_tab(aid, "files"),
            code_root_names(5), ("eval", "spy", spy),
            ("click", code_item("README.md")), ("wait", "document.querySelector('#code-editor')"),
            ("eval", "modes", "[...document.querySelectorAll('#code-md-view button')].map((b) => [b.textContent,"
                              " b.getAttribute('aria-pressed')])"),
            ("eval", "listShown", "getComputedStyle(document.querySelector('#code-files')).display !== 'none'"),
            ("click", "#code-md-view [data-mode='view']"), ("wait", "document.querySelector('#code-md h1')"),
            ("eval", "md", "[document.querySelector('#code-md h1').textContent,"
                           " document.querySelector('#code-md strong').textContent,"
                           " document.querySelector('#code-editor').hidden]"),
            ("click", "#code-md-view [data-mode='text']"),
            ("eval", "textAgain", "[document.querySelector('#code-editor').hidden, document.querySelector('#code-md').hidden]"),
            ("click", code_item("a.pdf")), ("wait", page_is.format(n=1)),
            ("eval", "first", "[document.querySelector('#code-pdf img').src, document.querySelector('#code-pdf-prev').disabled,"
                              " document.querySelector('#code-pdf-next').disabled,"
                              " document.querySelector('#code-pdf-prev').textContent,"
                              " document.querySelector('#code-pdf-next').textContent]"),
            ("click", "#code-pdf-next"), ("wait", page_is.format(n=2)),
            ("eval", "second", "[document.querySelector('#code-pdf img').src, document.querySelector('#code-pdf-prev').disabled,"
                               " document.querySelector('#code-pdf-next').disabled, [...window.revoked]]"),
            ("eval", "fits", NO_OVERFLOW),
            ("click", code_item("bin.dat")), ("wait", f"{CODE_FILE_STATUS} !== ''"),
            ("eval", "binary", CODE_FILE_STATUS), ("eval", "revoked", "window.revoked"),
            ("eval", "noEditor", "!document.querySelector('#code-editor')"),
            ("click", code_item("big.txt")), ("wait", f"{CODE_FILE_STATUS} !== ''"),
            ("eval", "large", CODE_FILE_STATUS)])
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["modes"] == [["Текст", "true"], ["Вид", "false"]] and v["listShown"] is True
    assert v["md"] == ["Заголовок", "жирный", True] and v["textAgain"] == [False, True]
    first, second = v["first"], v["second"]
    assert first[0].startswith("blob:") and first[1:] == [True, False, "‹", "›"]
    assert second[0].startswith("blob:") and second[0] != first[0] and second[1:3] == [False, True]
    assert second[3] == [first[0]]                           # прежняя страница освобождена
    assert v["revoked"] == [first[0], second[0]] and v["fits"] is True
    assert v["binary"] == "Двоичный файл — не открывается" and v["noEditor"] is True
    assert v["large"] == "Файл больше 2 МиБ — не открывается"
    assert [q["path"] for q in code_requests(machine, "raw")] == ["a.pdf"]          # листание — из памяти службы
    assert not known_refusals(problems(r), 413, 415), problems(r)


def test_code_git_panel(browser, live):
    """«Git»: ветка, «впереди на N» / «позади на N» при upstream, изменённые файлы; чистый без upstream — «Изменений
    нет»; не репозиторий — «Не репозиторий git»."""
    from fake_bridge import FakeMachine, pid
    login = "code-git"
    live.add_user(login)
    machine = FakeMachine(projects={
        "alpha": {"git": {"branch": "main", "upstream": "origin/main", "ahead": 2, "behind": 1,
                          "files": [{"path": "src/app.py", "status": "M"}, {"path": "новое/очень-длинное-имя-файла-без-"
                                    "пробелов-которое-не-влезет-в-узкий-экран.txt", "status": "??"}]}},
        "beta": {"git": None},
        "gamma": {"git": {"branch": "work", "upstream": None, "ahead": 0, "behind": 0, "files": []}}})
    aid, sb = code_online(live, login, machine)
    head = """(() => { const g = document.querySelector('#code-git');
      return [g.querySelector('.code-git-head').firstChild.textContent, g.querySelector('.code-git-branch').textContent,
        g.querySelector('.code-git-ahead')?.textContent ?? null, g.querySelector('.code-git-behind')?.textContent ?? null]; })()"""
    rows = "[...document.querySelectorAll('#code-git .code-git-files li')].map((li) => [li.querySelector('.code-git-status').textContent, li.querySelector('.code-git-path').textContent])"

    def project_git(name):
        return [*go(f"code/{aid}/{pid(name)}"),
                ("wait", f"document.querySelector('#code-place .code-place-project')?.textContent === '{name}'"),
                ("click", "#code-tab-git")]

    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_tab(aid, "git"),
            ("wait", "document.querySelector('#code-git .code-git-head')"),
            ("eval", "alpha", head), ("eval", "rows", rows), ("eval", "fits", NO_OVERFLOW),
            *project_git("gamma"), ("wait", "document.querySelector('#code-git .code-git-empty')"),
            ("eval", "gamma", head), ("eval", "empty", "document.querySelector('#code-git .code-git-empty').textContent"),
            *project_git("beta"), ("wait", "document.querySelector('#code-git .code-git-none')"),
            ("eval", "beta", "document.querySelector('#code-git').textContent")], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["alpha"] == ["Ветка ", "main", "впереди на 2", "позади на 1"] and v["fits"] is True
    assert v["rows"] == [["M", "src/app.py"], ["??", "новое/очень-длинное-имя-файла-без-пробелов-которое-не-влезет-в-узкий-экран.txt"]]
    assert v["gamma"] == ["Ветка ", "work", None, None] and v["empty"] == "Изменений нет"
    assert v["beta"] == "Не репозиторий git"
    assert {q["project"] for q in code_requests(machine, "git")} == {pid("alpha"), pid("beta"), pid("gamma")}
    assert not known_refusals(problems(r), 409), problems(r)


def test_git_hides_zero_ahead_behind(browser, live):
    """«Git» при upstream: нулевые «впереди на 0» и «позади на 0» не показываются, ненулевые — да."""
    from fake_bridge import FakeMachine, pid
    login = "code-git-zero"
    live.add_user(login)
    clean = {"branch": "main", "upstream": "origin/main", "ahead": 0, "behind": 0, "files": []}
    machine = FakeMachine(projects={"alpha": {"git": {**clean, "behind": 3}}, "beta": {"git": clean}})
    aid, sb = code_online(live, login, machine)
    marks = """(() => { const g = document.querySelector('#code-git');
      return [g.querySelector('.code-git-upstream')?.textContent ?? null, g.querySelector('.code-git-ahead')?.textContent ?? null,
        g.querySelector('.code-git-behind')?.textContent ?? null, g.querySelector('.code-git-head').textContent]; })()"""
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_tab(aid, "git"),
            ("wait", "document.querySelector('#code-git .code-git-head')"), ("eval", "alpha", marks),
            *go(f"code/{aid}/{pid('beta')}"),
            ("wait", "document.querySelector('#code-place .code-place-project')?.textContent === 'beta'"),
            ("click", "#code-tab-git"), ("wait", "document.querySelector('#code-git .code-git-head')"),
            ("eval", "beta", marks)], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["alpha"] == ["origin/main", None, "позади на 3", "Ветка mainorigin/mainпозади на 3"]
    assert v["beta"] == ["origin/main", None, None, "Ветка mainorigin/main"]
    assert not problems(r), problems(r)


def test_code_step_file_link_opens_the_file(browser, live):
    """Шаг Edit с file_path внутри проекта — «Открыть файл»: вкладка «Файлы», папка файла и сам файл в редакторе;
    путь — не в адресе и не в хранилищах. Шаг с путём вне проекта — без ссылки."""
    from fake_bridge import T0, pid
    login = "code-step-file"
    live.add_user(login)
    step = {"id": "s1", "tool": "Edit", "text": "ok", "error": False,
            "input": {"file_path": "C:\\work\\projects\\alpha\\src\\app.py", "old_string": "2", "new_string": "3"}}
    outside = {"id": "s2", "tool": "Edit", "text": "ok", "error": False,
               "input": {"file_path": "C:\\work\\projects\\beta\\x.py", "old_string": "a", "new_string": "b"}}
    messages = [{"id": "m-0", "role": "user", "text": "Поправь", "steps": [], "time": T0},
                {"id": "m-1", "role": "assistant", "text": "Готово.", "steps": [step, outside], "time": T0}]
    machine = code_files_machine({CODE_CHAT_A: code_session("Чат A", messages=messages)})
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("eval", "steps", CODE_STEPS),
            ("click", ".code-step-file"), ("wait", "document.querySelector('#code-editor')"),
            ("eval", "tab", "document.querySelector('#code-tab-files').getAttribute('aria-selected')"),
            ("eval", "text", "document.querySelector('#code-editor').value"),
            ("eval", "path", CODE_PATH), ("eval", "hash", "location.hash"), ("eval", "stored", CODE_STORED),
            ("eval", "fits", NO_OVERFLOW)], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["steps"] == [["Edit", "C:\\work\\projects\\alpha\\src\\app.py", "ok", False, "Открыть файл"],
                          ["Edit", "C:\\work\\projects\\beta\\x.py", "ok", False, None]]
    assert v["tab"] == "true" and v["text"] == "print(1)\nprint(2)\n"
    assert v["path"] == ["alpha", "src", "app.py"] and v["fits"] is True
    assert v["hash"] == f"#/code/{aid}/{pid('alpha')}?tab=files"
    assert "app.py" not in v["stored"]
    assert [q["path"] for q in code_requests(machine, "files")] == ["", "src"]    # раскрывает дерево до файла
    assert [q["path"] for q in code_requests(machine, "read")] == ["src/app.py"]
    assert not problems(r), problems(r)


# вкладки сессий проекта (code-tabs.js): «+», «×», соседняя активная, предел 20, восстановление

CODE_CHAT_C = "c3333333-3333-4333-8333-333333333333"
#: вкладки ряда: id, название, выбрана
CODE_TABS = """[...document.querySelectorAll('#code-tabbar .code-tab')].map((t) => [t.dataset.chat,
  t.querySelector('.code-tab-title').textContent, t.getAttribute('aria-selected')])"""
CODE_ACTIVE_TAB = "document.querySelector('#code-tabbar .code-tab[aria-selected=true]')?.dataset.chat"


def code_tab_ready(chat):
    """Вкладка активна, её чат показан."""
    return ("wait", f"{CODE_ACTIVE_TAB} === '{chat}' && document.querySelector('#code-chat')?.dataset.chat === '{chat}'"
                    " && document.querySelector('#code-chat').dataset.loaded === '1'")


def code_from_list(chat):
    """Проект без вкладок: сессия из списка во всю ширину — первая вкладка."""
    return [("wait", f"document.querySelector(\"#code-chats li[data-chat='{chat}']\")"),
            ("click", f"#code-chats li[data-chat='{chat}'] .code-chat-link"), code_tab_ready(chat)]


def code_from_menu(chat):
    """«+» — меню сессий проекта, выбор — новой вкладкой."""
    return [("click", "#code-tab-add"), ("wait", f"document.querySelector(\"#code-tab-menu li[data-chat='{chat}']\")"),
            ("click", f"#code-tab-menu li[data-chat='{chat}'] .code-chat-link"), code_tab_ready(chat)]


def code_tab_click(chat):
    return [("click", f"#code-tabbar .code-tab[data-chat='{chat}'] .code-tab-title"), code_tab_ready(chat)]


def code_three(title="Чат"):
    """Компьютер без PIN: alpha с тремя сессиями A, B, C (у каждой — своё сообщение), beta — пустой."""
    from fake_bridge import T0

    def session(name, minute):
        return {**code_session(f"{title} {name}", messages=[{"id": "m-0", "role": "user", "text": f"про {name}",
                                                             "steps": [], "time": T0}]), "modified": T0 + minute}
    machine = code_free_machine({CODE_CHAT_A: session("A", 3), CODE_CHAT_B: session("B", 2), CODE_CHAT_C: session("C", 1)})
    machine.projects["beta"] = {"chats": {}}
    return machine


def code_workspace_rows(live, uid):
    import db
    conn = db.connect(live.db_path)
    try:
        return [(r["project"], json.loads(r["tabs"]), r["active"])
                for r in conn.execute("SELECT project, tabs, active FROM code_tabs WHERE user_id = ? ORDER BY touched",
                                      (uid,))]
    finally:
        conn.close()


def test_code_tabs_open_from_the_menu_and_switch(browser, live):
    """Сессия из списка — первая вкладка; «+» — меню остальных сессий проекта, выбранная — новой вкладкой в конце и
    активной; нажатие на вкладку — её чат с историей. Смена вкладки заменяет запись истории: «Назад» — к прежнему
    проекту, а не к прежней вкладке."""
    from fake_bridge import pid
    login = "code-tabs"
    live.add_user(login)
    machine = code_three()
    aid, sb = code_online(live, login, machine)
    alpha = pid("alpha")
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), *code_project(aid, "beta"),
            ("wait", "!!document.querySelector('#code-chats')"), *code_project(aid, "alpha"),
            ("eval", "len0", "history.length"),
            *code_from_list(CODE_CHAT_A), ("eval", "hashA", "location.hash"),
            ("click", "#code-tab-add"), ("wait", "document.querySelector('#code-tab-menu li[data-chat]')"),
            ("eval", "menu", "[document.querySelector('#code-tab-menu li #code-new-chat')?.textContent,"
                             " [...document.querySelectorAll('#code-tab-menu li[data-chat]')].map((li) => li.dataset.chat)]"),
            ("click", f"#code-tab-menu li[data-chat='{CODE_CHAT_B}'] .code-chat-link"), code_tab_ready(CODE_CHAT_B),
            ("eval", "menuGone", "!document.querySelector('#code-tab-menu li')"),
            ("eval", "tabs", CODE_TABS),
            *code_tab_click(CODE_CHAT_A), ("eval", "msgs", CODE_MSGS),
            ("eval", "hash", "location.hash"), ("eval", "len1", "history.length"),
            ("eval", "back", "history.back()"),
            ("wait", f"location.hash === '#/code/{aid}/{pid('beta')}' && !!document.querySelector('#code-chats')"),
            ("eval", "fits", NO_OVERFLOW)], width=1280)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["hashA"] == f"#/code/{aid}/{alpha}/{CODE_CHAT_A}"
    assert v["menu"] == ["Claude", [CODE_CHAT_B, CODE_CHAT_C]] and v["menuGone"] is True
    assert v["tabs"] == [[CODE_CHAT_A, "Чат A", "false"], [CODE_CHAT_B, "Чат B", "true"]]
    assert v["msgs"] == [["user", "про A"]] and v["hash"] == f"#/code/{aid}/{alpha}/{CODE_CHAT_A}"
    assert v["len1"] == v["len0"] and v["fits"] is True
    assert not problems(r), problems(r)


def test_code_new_chat_is_a_tab(browser, live):
    """«Новый чат» из списка и из «+» — новая вкладка «Новый чат», активна; строка code_chats у каждого."""
    import db
    from fake_bridge import pid
    login = "code-new-tab"
    uid = live.add_user(login)
    aid, sb = code_online(live, login, code_three())
    new_tab = ("wait", f"{CODE_ACTIVE_TAB}?.length === 36 && {CODE_ACTIVE_TAB} === location.hash.split('/').pop()"
                       " && document.querySelector('#code-chat')?.dataset.chat === location.hash.split('/').pop()")
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), *code_project(aid, "beta"),
            ("wait", "!!document.querySelector('#code-new-chat')"), ("click", "#code-new-chat"), new_tab,
            ("eval", "first", "window.firstTab = location.hash.split('/').pop()"),
            ("click", "#code-tab-add"), ("wait", "document.querySelector('#code-tab-menu #code-new-chat')"),
            ("click", "#code-tab-menu #code-new-chat"),
            ("wait", f"{CODE_ACTIVE_TAB} !== window.firstTab && document.querySelectorAll('#code-tabbar .code-tab').length === 2"),
            new_tab, ("eval", "tabs", CODE_TABS), ("eval", "fits", NO_OVERFLOW)], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    first, second = v["tabs"][0][0], v["tabs"][1][0]
    assert first == v["first"] and v["tabs"] == [[first, "Новый чат", "false"], [second, "Новый чат", "true"]]
    conn = db.connect(live.db_path)
    try:
        rows = {tuple(x) for x in conn.execute("SELECT session_id, project, state FROM code_chats WHERE user_id = ?", (uid,))}
    finally:
        conn.close()
    assert rows == {(first, pid("beta"), "open"), (second, pid("beta"), "open")} and v["fits"] is True
    assert not known_refusals(problems(r), 404), problems(r)          # история нового чата без хода — норма


def test_code_tab_close_picks_the_right_neighbour(browser, live):
    """[A, B, C], активна B: «×» B — активна C (справа); «×» C — A (слева); «×» A — сессии проекта во всю ширину.
    «×» сессию не закрывает: мосту ничего, в отметках студии «закрыт» нет, у машины все три."""
    import db
    from fake_bridge import pid
    login = "code-tab-close"
    uid = live.add_user(login)
    machine = code_three()
    aid, sb = code_online(live, login, machine)
    close = "#code-tabbar .code-tab[data-chat='{}'] .code-tab-close"
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), *code_project(aid, "alpha"),
            *code_from_list(CODE_CHAT_A), *code_from_menu(CODE_CHAT_B), *code_from_menu(CODE_CHAT_C),
            *code_tab_click(CODE_CHAT_B),
            ("click", close.format(CODE_CHAT_B)), code_tab_ready(CODE_CHAT_C), ("eval", "afterB", CODE_TABS),
            ("click", close.format(CODE_CHAT_C)), code_tab_ready(CODE_CHAT_A), ("eval", "afterC", CODE_TABS),
            ("click", close.format(CODE_CHAT_A)),
            ("wait", f"location.hash === '#/code/{aid}/{pid('alpha')}'"
                     " && document.querySelectorAll('#code-chats li[data-chat]').length === 3"),
            ("eval", "bar", "!document.querySelector('#code-tabbar .code-tab')")], width=1280)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["afterB"] == [[CODE_CHAT_A, "Чат A", "false"], [CODE_CHAT_C, "Чат C", "true"]]
    assert v["afterC"] == [[CODE_CHAT_A, "Чат A", "true"]] and v["bar"] is True
    wait_for(lambda: code_workspace_rows(live, uid) == [])
    conn = db.connect(live.db_path)
    try:
        assert conn.execute("SELECT count(*) FROM code_chats WHERE state = 'closed'").fetchone()[0] == 0
    finally:
        conn.close()
    assert set(machine.projects["alpha"]["chats"]) == {CODE_CHAT_A, CODE_CHAT_B, CODE_CHAT_C}
    assert not problems(r), problems(r)


def test_code_tabs_are_restored_per_project(browser, live):
    """alpha — [A, B], beta — [C]: у каждого проекта свой ряд, переход между проектами его не трогает; страница
    заново — тот же проект, тот же ряд и активная вкладка."""
    from fake_bridge import pid
    login = "code-tabs-restore"
    uid = live.add_user(login)
    machine = code_three()
    machine.projects["beta"]["chats"] = {"d4444444-4444-4444-8444-444444444444": code_session("Чат D")}
    chat_d = "d4444444-4444-4444-8444-444444444444"
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), *code_project(aid, "alpha"),
            *code_from_list(CODE_CHAT_A), *code_from_menu(CODE_CHAT_B),
            *code_project(aid, "beta"), *code_from_list(chat_d), ("eval", "beta", CODE_TABS),
            *code_project(aid, "alpha"), code_tab_ready(CODE_CHAT_B), ("eval", "alpha", CODE_TABS),
            *code_project(aid, "beta"), code_tab_ready(chat_d), ("eval", "beta2", CODE_TABS)], width=1280)
        assert not r.get("failed"), r
        wait_for(lambda: len(code_workspace_rows(live, uid)) == 2)
        again = browser.drive(live.url + "/#/code", [("wait", READY), code_tab_ready(chat_d),
            ("eval", "beta", CODE_TABS), ("eval", "hash", "location.hash"),
            *code_project(aid, "alpha"), code_tab_ready(CODE_CHAT_B), ("eval", "alpha", CODE_TABS)], width=1280)
    finally:
        sb.stop()
    assert not again.get("failed"), again
    v, w = r["values"], again["values"]
    assert v["beta"] == v["beta2"] == w["beta"] == [[chat_d, "Чат D", "true"]]
    assert v["alpha"] == w["alpha"] == [[CODE_CHAT_A, "Чат A", "false"], [CODE_CHAT_B, "Чат B", "true"]]
    assert w["hash"] == f"#/code/{aid}/{pid('beta')}/{chat_d}"
    assert not problems(again), problems(again)


def test_code_twenty_first_tab_pushes_out_the_leftmost(browser, live):
    """20 вкладок открыто; 21-я из «+» — в конце и активна, самая левая ушла."""
    from fake_bridge import pid
    login = "code-tabs-21"
    live.add_user(login)
    chats = [f"{i:08x}-1111-4111-8111-111111111111" for i in range(1, 22)]
    machine = code_free_machine({c: {**code_session(f"Сессия {i}"), "modified": 100 - i} for i, c in enumerate(chats)})
    aid, sb = code_online(live, login, machine)
    opens = "(async () => { for (const chat of %s) await %s.api('PATCH', '/api/code/workspace'," \
            " { op: 'open', aid: %d, pid: '%s', chat }); return true; })()" % (json.dumps(chats[:20]), API, aid, pid("alpha"))
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("eval", "opens", opens), *go("code"),
            code_tab_ready(chats[19]), ("eval", "before", "document.querySelectorAll('#code-tabbar .code-tab').length"),
            *code_from_menu(chats[20]), ("eval", "tabs", CODE_TABS)], width=1280)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["before"] == 20 and [t[0] for t in v["tabs"]] == chats[1:]
    assert v["tabs"][-1] == [chats[20], "Сессия 20", "true"]
    assert not problems(r), problems(r)


def test_code_missing_session_tab_goes_away(browser, live):
    """Вкладка сессии, которой нет на машине (история — 404 not_found), уходит сама, активна соседняя. Сессия вне
    списка машины (старше 200 новейших) — вкладка «Сессия» остаётся, её история показана."""
    from fake_bridge import T0, pid
    login = "code-tab-gone"
    live.add_user(login)
    old = "0ff00000-0000-4000-8000-000000000000"
    gone = "9a900000-0000-4000-8000-000000000000"
    machine = code_three()
    machine.projects["alpha"]["chats"][old] = {**code_session("Старая", messages=[{"id": "m-0", "role": "user",
                                                                                  "text": "давно", "steps": [], "time": T0}]),
                                              "listed": False}
    aid, sb = code_online(live, login, machine)
    opens = "(async () => { for (const chat of %s) await %s.api('PATCH', '/api/code/workspace'," \
            " { op: 'open', aid: %d, pid: '%s', chat }); return true; })()" % (json.dumps([CODE_CHAT_A, gone]), API, aid,
                                                                              pid("alpha"))
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("eval", "opens", opens), *go("code"),
            code_tab_ready(CODE_CHAT_A), ("eval", "tabs", CODE_TABS), ("eval", "hash", "location.hash"),
            *go(f"code/{aid}/{pid('alpha')}/{old}"), code_tab_ready(old), ("eval", "old", CODE_TABS),
            ("eval", "msgs", CODE_MSGS)], width=1280)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["tabs"] == [[CODE_CHAT_A, "Чат A", "true"]]
    assert v["hash"] == f"#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}"
    assert v["old"] == [[CODE_CHAT_A, "Чат A", "false"], [old, "Сессия", "true"]] and v["msgs"] == [["user", "давно"]]
    assert not known_refusals(problems(r), 404), problems(r)


def test_code_fast_tab_clicks_end_on_the_last(browser, live):
    """История A отвечает медленно: нажали A, затем сразу B — на экране B, ответ A её не перерисовывает."""
    login = "code-tab-race"
    live.add_user(login)
    aid, sb = code_online(live, login, code_three())
    slow = """(() => { const real = window.fetch; window.fetch = (u, o) => String(u).endsWith('/chats/%s')
      ? real(u, o).then((r) => new Promise((ok) => setTimeout(() => ok(r), 1500))) : real(u, o); return true; })()""" % CODE_CHAT_A
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), *code_project(aid, "alpha"),
            *code_from_list(CODE_CHAT_A), *code_from_menu(CODE_CHAT_B), ("eval", "slow", slow),
            ("click", f"#code-tabbar .code-tab[data-chat='{CODE_CHAT_A}'] .code-tab-title"),
            ("click", f"#code-tabbar .code-tab[data-chat='{CODE_CHAT_B}'] .code-tab-title"),
            code_tab_ready(CODE_CHAT_B),
            ("eval", "after", settled(f"[{CODE_ACTIVE_TAB}, document.querySelector('#code-chat').dataset.chat,"
                                      f" {CODE_MSGS}, location.hash.split('/').pop()]", 2500))], width=1280)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert r["values"]["after"] == [CODE_CHAT_B, CODE_CHAT_B, [["user", "про B"]], CODE_CHAT_B]
    assert not problems(r), problems(r)


def test_code_tab_opens_when_the_workspace_write_fails(browser, live):
    """Запись рабочего места отвечает 500: вкладка всё равно открылась и показывает чат; необработанных отказов нет."""
    login = "code-tab-ws-500"
    live.add_user(login)
    aid, sb = code_online(live, login, code_three())
    broken = """(() => { const real = window.fetch; window.fetch = (u, o) => String(u) === '/api/code/workspace'
      && o && o.method === 'PATCH' ? Promise.resolve(new Response('{}', { status: 500,
        headers: { 'content-type': 'application/json' } })) : real(u, o); return true; })()"""
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("eval", "broken", broken), *go("code"),
            *code_project(aid, "alpha"), *code_from_list(CODE_CHAT_A), *code_from_menu(CODE_CHAT_B),
            ("eval", "tabs", settled(CODE_TABS, 500))], width=1280)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert r["values"]["tabs"] == [[CODE_CHAT_A, "Чат A", "false"], [CODE_CHAT_B, "Чат B", "true"]]
    assert not [p for p in problems(r) if "status of 500" not in p], problems(r)


@pytest.mark.parametrize("target", ["chat", "new"])
def test_code_unsaved_edit_asks_before_a_tab_switch(browser, live, target):
    """Вкладка или «+» защищают несохранённую правку: «Остаться» — правка цела;
    «Уйти без сохранения» — выбранный чат или экран новой вкладки."""
    login = "code-tab-unsaved-" + target
    live.add_user(login)
    machine = code_files_machine({CODE_CHAT_A: code_session("Чат A")})
    aid, sb = code_online(live, login, machine)
    editor = "document.querySelector('#code-editor')?.value"
    tab = "#code-tab-add" if target == "new" else f"#code-tabbar .code-tab[data-chat='{CODE_CHAT_A}'] .code-tab-title"
    ready = ("wait", "!!document.querySelector('#code-tab-menu #code-new-chat')") if target == "new" else code_tab_ready(CODE_CHAT_A)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), *code_project(aid, "alpha"),
            *code_from_list(CODE_CHAT_A), ("click", "#code-tab-files"), code_root_names(5),
            ("eval", "selected", "[...document.querySelectorAll('#code-tabbar .code-tab')].map((t) => t.getAttribute('aria-selected'))"),
            ("click", code_item("src")), ("wait", "document.querySelector(\"#code-files li[data-name='app.py']\")"),
            ("click", code_item("app.py")), ("wait", "document.querySelector('#code-editor')"),
            set_value("#code-editor", "print('правка')\n"),
            ("click", tab), ("wait", CODE_UNSAVED), ("click", "#code-unsaved .dialog-close"), ("wait", f"!{CODE_UNSAVED}"),
            ("eval", "stayed", f"[{editor}, location.hash.endsWith('?tab=files')]"),
            ("click", tab), ("wait", CODE_UNSAVED), ("click", "#code-unsaved-leave"), ready,
            ("eval", "left", "location.hash.endsWith('?tab=new')" if target == "new" else "location.hash.split('/').pop()")], width=1280)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["selected"] == ["false"] and v["stayed"] == ["print('правка')\n", True]
    assert v["left"] == (True if target == "new" else CODE_CHAT_A)
    assert code_requests(machine, "save") == []
    assert not problems(r), problems(r)


@pytest.mark.parametrize("width", [320, 1280])
def test_code_long_tab_titles_fit(browser, live, width):
    """Пять вкладок с названиями по 120 знаков: ряд — одна строка, страница не шире экрана; на телефоне ряд
    листается вбок, активная — в видимой части."""
    from fake_bridge import pid
    login = f"code-tab-long-{width}"
    live.add_user(login)
    chats = [f"{i:08x}-2222-4222-8222-222222222222" for i in range(1, 6)]
    machine = code_free_machine({c: {**code_session(f"{i}-" + "длинное-название-сессии-" * 5), "modified": 100 - i}
                                 for i, c in enumerate(chats)})
    aid, sb = code_online(live, login, machine)
    opens = "(async () => { for (const chat of %s) await %s.api('PATCH', '/api/code/workspace'," \
            " { op: 'open', aid: %d, pid: '%s', chat }); return true; })()" % (json.dumps(chats), API, aid, pid("alpha"))
    shape = """(() => { const bar = document.querySelector('#code-tabbar'); const tabs = [...bar.querySelectorAll('.code-tab')];
      const tops = new Set(tabs.map((t) => Math.round(t.getBoundingClientRect().top)));
      const a = bar.querySelector('.code-tab[aria-selected=true]').getBoundingClientRect(); const b = bar.getBoundingClientRect();
      const title = tabs[0].querySelector('.code-tab-title');
      return [tops.size, bar.scrollWidth > bar.clientWidth, a.left >= b.left - 1 && a.right <= b.right + 1,
        title.scrollWidth > title.clientWidth, title.title.length > 100,
        b.top >= document.querySelector('.code-head').getBoundingClientRect().bottom]; })()"""
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("eval", "opens", opens), *go("code"),
            code_tab_ready(chats[4]), ("eval", "shape", settled(shape, 400)), ("eval", "fits", NO_OVERFLOW)],
            width=width, mobile=width < 760)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["shape"][0] == 1 and v["shape"][2:] == [True, True, True, True] and v["fits"] is True
    if width < 760:
        assert v["shape"][1] is True
    assert not problems(r), problems(r)


# метки вкладок, числа вкладок в панели, возвращение на экран

#: класс метки вкладки чата (нет метки — null)
CODE_MARK = "(document.querySelector(\"#code-tabbar .code-tab[data-chat='{}'] .code-tab-mark\")?.className ?? null)"
#: GET …/chats страницы — счётчиком window.chatsCalls (для init вместе с CODE_FAST_IDLE)
CODE_CHATS_SPY = """(() => { window.chatsCalls = 0; const real = window.fetch; window.fetch = (u, o) => {
  if (/\\/chats$/.test(String(u)) && !(o && o.method && o.method !== 'GET')) window.chatsCalls += 1; return real(u, o); };
  return true; })();"""
CODE_COUNTS = """Object.fromEntries([...document.querySelectorAll('#code-projects li[data-pid]')].map((li) => [li.dataset.pid,
  li.querySelector('.code-project-count').textContent]))"""


def test_code_tab_marks_follow_the_machine(browser, live):
    """Вкладки A и B, на экране A: на машине в B начался ход — у вкладки B метка «идёт ход» в пределах опроса
    (GET …/chats проекта раз в IDLE_POLL_SEC); агент спросил — «ждёт ответа»; ход кончился — меток нет."""
    login = "code-marks"
    live.add_user(login)
    machine = code_three()
    aid, sb = code_online(live, login, machine)
    mark = CODE_MARK.format(CODE_CHAT_B)

    def after_polls(n=2):
        start = len(code_requests(machine, "chats"))
        wait_for(lambda: len(code_requests(machine, "chats")) >= start + n, 60)

    def a_after_b():
        chats = [q.get("chat") for q in code_requests(machine, "watch")]
        return CODE_CHAT_B in chats and CODE_CHAT_A in chats[chats.index(CODE_CHAT_B):]

    def play():
        wait_for(a_after_b, 60)                 # на экране снова A, B — фоновая вкладка
        code_start_elsewhere(machine, CODE_CHAT_B, "Где-то ещё", [{"k": "text", "text": "работаю"}])
        after_polls()
        machine.running[CODE_CHAT_B]["asks"]["r1"] = code_permission("r1", False)
        after_polls()
        machine._finish(CODE_CHAT_B, [], {"k": "done", "status": "done"})

    player = threading.Thread(target=play, daemon=True)
    player.start()
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), *code_project(aid, "alpha"),
            *code_from_list(CODE_CHAT_A), *code_from_menu(CODE_CHAT_B), *code_tab_click(CODE_CHAT_A),
            ("wait", f"{mark} === 'code-tab-mark running'"), ("eval", "label", f"document.querySelector(\"#code-tabbar"
                     f" .code-tab[data-chat='{CODE_CHAT_B}'] .code-tab-mark\").textContent"),
            ("wait", f"{mark} === 'code-tab-mark asking'"),
            ("wait", f"{mark} === null"),
            ("eval", "a", CODE_MARK.format(CODE_CHAT_A))], width=1280, init=CODE_FAST_IDLE)
        player.join(10)
        assert not player.is_alive(), "Проверка завершилась до окончания фонового хода"
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert r["values"]["label"] == "идёт ход" and r["values"]["a"] is None
    assert not problems(r), problems(r)


def test_code_tab_marks_stop_polling_after_half_an_hour(browser, live):
    """Опрос меток — не дольше IDLE_POLL_FOR_SEC после действия владельца в разделе: дальше GET …/chats нет (забытая
    вкладка не держит компьютер открытым); нажатие в разделе — опрос снова."""
    login = "code-marks-idle"
    live.add_user(login)
    aid, sb = code_online(live, login, code_three())
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), *code_project(aid, "alpha"),
            *code_from_list(CODE_CHAT_A), ("wait", "window.chatsCalls >= 3"),
            later(1801000), ("eval", "c1", settled("window.chatsCalls", 700)),
            ("eval", "c2", settled("window.c2 = window.chatsCalls", 1200)),
            ("click", "#code-place"), ("wait", "window.chatsCalls > window.c2 + 1")],
            width=1280, init=CODE_FAST_IDLE + CODE_CHATS_SPY)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert r["values"]["c1"] == r["values"]["c2"]
    assert not problems(r), problems(r)


def test_code_hidden_page_does_not_poll_marks(browser, live):
    """Страница ушла с экрана — меток не опрашивает; вернулась — опрос снова."""
    login = "code-marks-hidden"
    live.add_user(login)
    aid, sb = code_online(live, login, code_three())
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), *code_project(aid, "alpha"),
            *code_from_list(CODE_CHAT_A), ("wait", "window.chatsCalls >= 3"),
            hide(), ("eval", "c1", settled("window.chatsCalls", 400)),
            ("eval", "c2", settled("window.c2 = window.chatsCalls", 1200)),
            show(), ("wait", "window.chatsCalls > window.c2")], width=1280, init=CODE_FAST_IDLE + CODE_CHATS_SPY)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert r["values"]["c1"] == r["values"]["c2"]
    assert not problems(r), problems(r)


def test_code_panel_counts_open_tabs(browser, live):
    """Число у проекта в панели — сколько у него открытых вкладок (из рабочего места): alpha — 2, beta — пусто;
    открыли третью — «3», сразу."""
    from fake_bridge import pid
    login = "code-counts"
    live.add_user(login)
    aid, sb = code_online(live, login, code_three())
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), *code_project(aid, "alpha"),
            *code_from_list(CODE_CHAT_A), *code_from_menu(CODE_CHAT_B), ("eval", "two", CODE_COUNTS),
            *code_from_menu(CODE_CHAT_C), ("eval", "three", CODE_COUNTS)], width=1280)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["two"] == {pid("alpha"): "2", pid("beta"): ""} and v["three"] == {pid("alpha"): "3", pid("beta"): ""}
    assert not problems(r), problems(r)


def test_code_return_to_the_screen_refreshes_without_jumping(browser, live):
    """Другое устройство открыло вкладку C в alpha и проект beta; эта страница (alpha, вкладка A) вернулась на экран —
    ряд alpha с C и число у alpha «2», а на экране по-прежнему alpha/A, адрес прежний."""
    from fake_bridge import pid
    login = "code-return"
    live.add_user(login)
    aid, sb = code_online(live, login, code_three())
    other = ("(async () => { const { api } = %s; await api('PATCH', '/api/code/workspace', { op: 'open', aid: %d,"
             " pid: '%s', chat: '%s' }); await api('PATCH', '/api/code/workspace', { op: 'place', aid: %d, pid: '%s',"
             " view: 'chat' }); return true; })()") % (API, aid, pid("alpha"), CODE_CHAT_C, aid, pid("beta"))
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), *code_project(aid, "alpha"),
            *code_from_list(CODE_CHAT_A), ("eval", "hash0", "location.hash"),
            hide(), ("eval", "other", other), show(),
            ("wait", f"!!document.querySelector(\"#code-tabbar .code-tab[data-chat='{CODE_CHAT_C}']\")"),
            ("eval", "tabs", CODE_TABS), ("eval", "counts", CODE_COUNTS),
            ("eval", "here", f"[location.hash, document.querySelector('#code-chat').dataset.chat, {CODE_PLACE}]")],
            width=1280)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["tabs"] == [[CODE_CHAT_A, "Чат A", "true"], [CODE_CHAT_C, "Чат C", "false"]]
    assert v["counts"][pid("alpha")] == "2"
    assert v["here"] == [v["hash0"], CODE_CHAT_A, "Рабочий ПК / alpha"]
    assert not problems(r), problems(r)


def test_code_background_tab_keeps_no_stream(browser, live):
    """Вкладки A и B, на экране B; на машине ход и в A, и в B: у службы нет наблюдателя A (push о нём уйдёт), а B
    смотрят (поток держит только показанная вкладка)."""
    import bridge
    login = "code-background"
    live.add_user(login)
    machine = code_three()
    aid, sb = code_online(live, login, machine)
    seen = []

    def look():
        wait_for(lambda: any(q.get("chat") == CODE_CHAT_B for q in code_requests(machine, "watch")), 60)
        code_start_elsewhere(machine, CODE_CHAT_A, "Где-то ещё", [{"k": "text", "text": "работаю"}])
        code_start_elsewhere(machine, CODE_CHAT_B, "И тут", [{"k": "text", "text": "тоже"}])
        wait_for(lambda: bridge.watching(aid, CODE_CHAT_B), 10)        # опрос live показанной увидел ход
        time.sleep(1)
        seen.extend([bridge.watching(aid, CODE_CHAT_A), bridge.watching(aid, CODE_CHAT_B)])

    looker = threading.Thread(target=look, daemon=True)
    looker.start()
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), *code_project(aid, "alpha"),
            *code_from_list(CODE_CHAT_A), *code_from_menu(CODE_CHAT_B),
            ("wait", "document.querySelector('#code-chat').dataset.running === '1'"), ("eval", "pause", settled("true", 1500))],
            width=1280, init=CODE_FAST_IDLE)
        looker.join(15)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert seen == [False, True], seen
    assert not problems(r), problems(r)


# находки финального ревью: компьютер вернулся на связь, ?pin при запомненном месте, меню «+» под опросом


def test_code_computer_back_online_redraws_the_right_side(browser, live):
    """Ревью: раздел открыт на проекте спящего компьютера — справа «не на связи»; мост вернулся, владелец нажал компьютер
    в панели — справа сам проект, без ухода из раздела."""
    import bridge
    from fake_bridge import SocketBridge, pid
    login = "code-back-online"
    live.add_user(login)
    machine = code_free_machine()
    aid, sb = code_online(live, login, machine)
    state = {}
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), *code_project(aid, "alpha"),
            ("wait", "!!document.querySelector('#code-chats')")], width=1280)
        assert not r.get("failed"), r
        sb.stop()
        wait_for(lambda: aid not in bridge.LINKS)

        def wake():
            time.sleep(3)                                  # страница уже показала «не на связи»
            state["sb"] = SocketBridge(live.url, sb.key, machine).start()
            wait_for(lambda: aid in bridge.LINKS)
            live_sql(live, "UPDATE agents SET name = ? WHERE id = ?", ("Проснулся", aid))

        waker = threading.Thread(target=wake, daemon=True)
        waker.start()
        offline = "Компьютер не на связи — включите его и запустите мост"
        r = browser.drive(live.url + "/#/code", [("wait", READY),
            ("wait", f"location.hash === '#/code/{aid}/{pid('alpha')}'"
                     f" && document.querySelector('#code-status')?.textContent === '{offline}'"),
            ("wait", "fetch('/api/agents').then((r) => r.json()).then((j) => j.items.some((a) => a.name === 'Проснулся'))"),
            ("click", f"#code-agents li[data-id='{aid}'] .code-agent"),
            ("wait", "!!document.querySelector('#code-chats')"
                     " && !document.querySelector('#code-status')?.textContent"),
            ("eval", "hash", "location.hash")], width=1280)
        waker.join(10)
    finally:
        sb.stop()
        if "sb" in state:
            state["sb"].stop()
    assert not r.get("failed"), r
    assert r["values"]["hash"] == f"#/code/{aid}/{pid('alpha')}"
    assert not problems(r), problems(r)


def test_code_pin_query_keeps_the_pin_with_a_remembered_place(browser, live):
    """Ревью: ссылка #/code?pin=<aid> при запомненном месте — поле PIN в панели (телефон — панель открыта), к месту
    не уводит: «Выберите проект слева»."""
    from fake_bridge import pid
    login = "code-pin-place"
    live.add_user(login)
    machine = code_machine()
    aid, sb = code_online(live, login, machine)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_open(aid), *code_project(aid, "alpha"),
            ("wait", "!!document.querySelector('#code-chats')"), *go(f"code?pin={aid}"),
            ("wait", f"{CODE_PIN_SHOWN} && location.hash === '#/code'"),
            ("eval", "open", settled(CODE_PANEL_OPEN, 400)),
            ("eval", "rest", "[location.hash, document.querySelector('#code-pick')?.textContent]")], width=320, mobile=True)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert r["values"]["open"] is True and r["values"]["rest"] == ["#/code", "Выберите проект слева"]
    assert not known_refusals(problems(r), 409), problems(r)


def test_code_open_menu_survives_a_poll(browser, live):
    """Ревью: опрос меток (раз в IDLE_POLL_SEC) не пересобирает открытое меню «+»: пункт под рукой тот же, выбор
    после опросов — вкладкой."""
    login = "code-menu-poll"
    live.add_user(login)
    aid, sb = code_online(live, login, code_three())
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), *code_project(aid, "alpha"),
            *code_from_list(CODE_CHAT_A), ("click", "#code-tab-add"),
            ("wait", f"document.querySelector(\"#code-tab-menu li[data-chat='{CODE_CHAT_B}']\")"),
            ("eval", "keep", f"!!(window.menuRow = document.querySelector(\"#code-tab-menu li[data-chat='{CODE_CHAT_B}']\"))"
                             " && (window.polls0 = window.chatsCalls) >= 0"),
            ("wait", "window.chatsCalls >= window.polls0 + 2"),
            ("eval", "same", f"document.querySelector(\"#code-tab-menu li[data-chat='{CODE_CHAT_B}']\") === window.menuRow"),
            ("click", f"#code-tab-menu li[data-chat='{CODE_CHAT_B}'] .code-chat-link"), code_tab_ready(CODE_CHAT_B)],
            width=1280, init=CODE_FAST_IDLE + CODE_CHATS_SPY)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert r["values"]["same"] is True
    assert not problems(r), problems(r)


# находки после ревью рабочего места: порядок записей, повторное нажатие на компьютер, клавиатура вкладок, названия до
# списка, ссылка на открытый проект и на отозванный компьютер, свежее меню «+», отказы closed во «Файлах», блокировка

#: PATCH рабочего места с op open уходит на 2 с позже
CODE_SLOW_OPEN = """(() => { const real = window.fetch; window.fetch = (u, o) => String(u) === '/api/code/workspace'
  && o && o.method === 'PATCH' && JSON.parse(o.body).op === 'open'
  ? new Promise((ok) => setTimeout(ok, 2000)).then(() => real(u, o)) : real(u, o); return true; })()"""
#: фокус страницы: id чата вкладки, иначе id элемента
CODE_FOCUS = "document.activeElement?.dataset?.chat ?? document.activeElement?.id"


def test_code_workspace_actions_reach_the_server_in_order(browser, live):
    """Ревью: запись «открыть» вкладку медленная. Страница вернулась на экран, пока она идёт, — ответ GET её не теряет
    (у проекта в панели «1»); «открыть» B и сразу «×» B — на сервер в том же порядке: у учётки только A."""
    from fake_bridge import pid
    login = "code-ws-order"
    uid = live.add_user(login)
    aid, sb = code_online(live, login, code_three())
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), *code_project(aid, "alpha"),
            ("wait", "!!document.querySelector('#code-chats')"), ("eval", "slow", CODE_SLOW_OPEN),
            *code_from_list(CODE_CHAT_A), hide(), show(), ("eval", "counts", settled(CODE_COUNTS, 3000)),
            *code_from_menu(CODE_CHAT_B),
            ("click", f"#code-tabbar .code-tab[data-chat='{CODE_CHAT_B}'] .code-tab-close"), code_tab_ready(CODE_CHAT_A),
            ("eval", "settle", settled("true", 3000))], width=1280)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert r["values"]["counts"][pid("alpha")] == "1"
    assert code_workspace_rows(live, uid) == [(pid("alpha"), [CODE_CHAT_A], CODE_CHAT_A)]
    assert not problems(r), problems(r)


def test_code_selected_computer_click_rereads_its_projects(browser, live):
    """Ревью: нажатие на уже выбранный компьютер — его проекты заново (папка, заведённая на машине, видна) без второго
    открытия; сессия закрылась за это время (срок, перезапуск моста) — как вход: открытие и проекты."""
    login = "code-reread"
    live.add_user(login)
    machine = code_free_machine()
    aid, sb = code_online(live, login, machine)
    agent = f"#code-agents li[data-id='{aid}'] .code-agent"

    def named(name):
        return f"fetch('/api/agents').then((r) => r.json()).then((j) => j.items.some((a) => a.name === '{name}'))"

    def play():                                 # пауза — машина успевает ответить на увиденную просьбу
        wait_for(lambda: code_requests(machine, "projects"), 60)
        time.sleep(0.3)
        machine.projects["gamma"] = {}
        live_sql(live, "UPDATE agents SET name = ? WHERE id = ?", ("С гаммой", aid))
        wait_for(lambda: len(code_requests(machine, "projects")) >= 3, 60)
        time.sleep(0.3)
        machine.open_sessions.clear()
        live_sql(live, "UPDATE agents SET name = ? WHERE id = ?", ("Закрытый", aid))

    player = threading.Thread(target=play, daemon=True)
    player.start()
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"),
            ("wait", "document.querySelectorAll('#code-projects li[data-pid]').length === 1"), ("wait", named("С гаммой")),
            ("click", agent), ("wait", "document.querySelectorAll('#code-projects li[data-pid]').length === 2"),
            ("wait", "document.querySelector('#code-computer-dialog').open"),
            ("eval", "closeInfo", "document.querySelector('#code-computer-dialog').close();true"),
            ("eval", "two", CODE_PROJECTS), ("wait", named("Закрытый")),
            ("eval", "list", "!!(window.oldList = document.querySelector('#code-projects'))"),
            ("click", agent), ("wait", "document.querySelector('#code-projects') !== window.oldList"
                                       " && document.querySelectorAll('#code-projects li[data-pid]').length === 2"),
            ("eval", "status", "document.querySelector('#code-panel-status').textContent")], width=1280)
        player.join(5)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert [name for _, name in v["two"]] == ["alpha", "gamma"] and v["status"] == ""
    assert len(code_requests(machine, "open")) == 2 and len(code_requests(machine, "projects")) == 5
    assert not known_refusals(problems(r), 409), problems(r)


def test_code_tabs_work_from_the_keyboard(browser, live):
    """Ревью: в порядке Tab из вкладок одна — выбранная (у «Файлов» — первая), «×» вне порядка; стрелки (по кругу), Home и
    End ведут фокус, Enter выбирает; опрос меток фокус не теряет; Delete закрывает вкладку в фокусе, фокус — на ту, что
    встала на её место."""
    login = "code-tab-keys"
    live.add_user(login)
    aid, sb = code_online(live, login, code_three())
    stops = "[...document.querySelectorAll('#code-tabbar .code-tab')].map((t) => t.tabIndex)"
    closes = "[...document.querySelectorAll('#code-tabbar .code-tab-close')].map((b) => b.tabIndex)"
    focus_b = f"(() => {{ document.querySelector(\"#code-tabbar .code-tab[data-chat='{CODE_CHAT_B}']\").focus(); return {CODE_FOCUS}; }})()"
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), *code_project(aid, "alpha"),
            *code_from_list(CODE_CHAT_A), *code_from_menu(CODE_CHAT_B), *code_from_menu(CODE_CHAT_C),
            ("click", "#code-tab-files"),
            ("wait", "document.querySelector('#code-tab-files').getAttribute('aria-selected') === 'true'"
                     " && !document.querySelector('#code-tabbar .code-tab[aria-selected=true]')"),
            ("eval", "files", stops),
            *code_tab_click(CODE_CHAT_B), ("eval", "stops", stops), ("eval", "closes", closes),
            ("eval", "focus", focus_b),
            ("press", "ArrowRight"), ("eval", "right", CODE_FOCUS), ("press", "ArrowRight"), ("eval", "wrap", CODE_FOCUS),
            ("press", "End"), ("eval", "end", CODE_FOCUS), ("press", "Home"), ("eval", "home", CODE_FOCUS),
            ("press", "ArrowLeft"), ("eval", "left", CODE_FOCUS),
            ("eval", "c0", "window.c0 = window.chatsCalls"), ("wait", "window.chatsCalls >= window.c0 + 2"),
            ("eval", "polled", CODE_FOCUS),
            ("press", "Enter"), code_tab_ready(CODE_CHAT_C), ("eval", "picked", CODE_FOCUS),
            ("press", "Delete"), code_tab_ready(CODE_CHAT_B), ("eval", "deleted", f"[{CODE_FOCUS}, {CODE_TABS}]")],
            width=1280, init=CODE_FAST_IDLE + CODE_CHATS_SPY)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["files"] == [0, -1, -1] and v["stops"] == [-1, 0, -1] and v["closes"] == [-1, -1, -1]
    assert [v[k] for k in ("focus", "right", "wrap", "end", "home", "left", "polled", "picked")] == [
        CODE_CHAT_B, CODE_CHAT_C, CODE_CHAT_A, CODE_CHAT_C, CODE_CHAT_A, CODE_CHAT_C, CODE_CHAT_C, CODE_CHAT_C]
    assert v["deleted"] == [CODE_CHAT_B, [[CODE_CHAT_A, "Чат A", "false"], [CODE_CHAT_B, "Чат B", "true"]]]
    assert not problems(r), problems(r)


def test_code_tab_titles_wait_for_the_session_list(browser, live):
    """Ревью: пока GET …/chats проекта не ответил, у вкладки нет названия (а не «Сессия»); ответил — «Чат A»."""
    from fake_bridge import pid
    login = "code-tab-untitled"
    live.add_user(login)
    aid, sb = code_online(live, login, code_three())
    opens = "(async () => { await %s.api('PATCH', '/api/code/workspace', { op: 'open', aid: %d, pid: '%s', chat: '%s' });" \
            " return true; })()" % (API, aid, pid("alpha"), CODE_CHAT_A)
    slow = """(() => { const real = window.fetch; window.fetch = (u, o) => /\\/chats$/.test(String(u)) && !(o && o.method
      && o.method !== 'GET') ? real(u, o).then((r) => new Promise((ok) => setTimeout(() => ok(r), 1500))) : real(u, o);
      return true; })()"""
    title = "document.querySelector('#code-tabbar .code-tab .code-tab-title')?.textContent"
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), ("eval", "opens", opens), ("eval", "slow", slow),
            *go("code"), ("wait", "!!document.querySelector('#code-tabbar .code-tab')"), ("eval", "early", title),
            ("wait", f"{title} === 'Чат A'")], width=1280)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert r["values"]["early"] == ""
    assert not problems(r), problems(r)


def test_code_link_to_the_open_project_keeps_the_page(browser, live):
    """Ревью: ссылка в панели на проект, что уже справа, — ни записи в истории, ни новой отрисовки чата."""
    from fake_bridge import pid
    login = "code-same-project"
    live.add_user(login)
    aid, sb = code_online(live, login, code_three())
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), *code_project(aid, "alpha"),
            *code_from_list(CODE_CHAT_A),
            ("eval", "mark", "!!(window.shownChat = document.querySelector('#code-chat')) && (window.len0 = history.length)"),
            ("click", f"#code-projects li[data-pid='{pid('alpha')}'] a"),
            ("eval", "after", settled("[history.length === window.len0,"
                                      " document.querySelector('#code-chat') === window.shownChat, location.hash]", 600))],
            width=1280)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert r["values"]["after"] == [True, True, f"#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}"]
    assert not problems(r), problems(r)


def test_code_link_to_a_revoked_computer_draws_no_tabs(browser, live):
    """Ревью: ссылка на чат отозванного компьютера (старый push, «В работе») — «Нет такого компьютера», без ряда вкладок
    и «+»."""
    from fake_bridge import pid
    login = "code-revoked-link"
    live.add_user(login)
    aid, _ = enroll_live(live, login, "Оставшийся ПК")
    try_aid = aid + 1000
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go(f"code/{try_aid}/{pid('alpha')}/{CODE_CHAT_A}"),
        ("wait", "document.querySelector('#code-status')?.textContent === 'Нет такого компьютера'"),
        ("eval", "row", settled("document.querySelector('.code-tabs-host').hidden", 300))], width=1280)
    assert not r.get("failed"), r
    assert r["values"]["row"] is True
    assert not problems(r), problems(r)


def test_code_reentry_does_not_record_another_open(browser, live):
    """Повторный вход, перезагрузка и нажатие на компьютер читают проекты без нового события открытия."""
    login = "code-reentry-events"
    live.add_user(login)
    machine = code_free_machine()
    aid, sb = code_online(live, login, machine)
    ready = "!!document.querySelector('#code-projects li[data-pid]')"
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), ("wait", ready),
            *go("settings/other"), ("wait", "!document.querySelector('#code-panel')"),
            *go("code"), ("wait", ready),
            ("eval", "reload", "location.reload(); true"), ("wait", READY), ("wait", ready),
            ("eval", "old", "!!(window.oldProjects = document.querySelector('#code-projects'))"),
            ("click", f"#code-agents li[data-id='{aid}'] .code-agent"),
            ("wait", "document.querySelector('#code-projects') !== window.oldProjects"),
            ("eval", "events", "fetch('/api/agents/events').then(r => r.json()).then(j => j.items.filter(e => e.kind === 'open').length)")])
    finally:
        sb.stop()
    assert not r.get("failed"), r
    assert r["values"]["events"] == 1
    assert len(code_requests(machine, "open")) == 1
    assert len(code_requests(machine, "projects")) == 5
    assert not problems(r), problems(r)


def test_code_tab_menu_reads_the_sessions_afresh(browser, live):
    """«+» сразу показывает сохранённые сессии и добавляет новую с компьютера после обновления."""
    login = "code-menu-fresh"
    live.add_user(login)
    machine = code_three()
    aid, sb = code_online(live, login, machine)
    chat_d = "d4444444-4444-4444-8444-444444444444"

    def play():
        wait_for(lambda: any(q.get("chat") == CODE_CHAT_A for q in code_requests(machine, "history")), 60)
        machine.projects["alpha"]["chats"][chat_d] = code_session("Чат D")
        live_sql(live, "UPDATE agents SET name = ? WHERE id = ?", ("С новой сессией", aid))

    player = threading.Thread(target=play, daemon=True)
    player.start()
    named = "fetch('/api/agents').then((r) => r.json()).then((j) => j.items.some((a) => a.name === 'С новой сессией'))"
    slow = """(() => { const real = window.fetch; window.fetch = (u, o) => /\\/chats$/.test(String(u)) && !(o && o.method
      && o.method !== 'GET') ? real(u, o).then((r) => new Promise((ok) => {
        window.releaseSessions = () => { window.fetch = real; ok(r); };
      })) : real(u, o);
      return true; })()"""
    rows = "[...document.querySelectorAll('#code-tab-menu li[data-chat]')].map((li) => li.dataset.chat)"
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"), *code_project(aid, "alpha"),
            *code_from_list(CODE_CHAT_A), ("wait", named), ("eval", "slow", slow), ("click", "#code-tab-add"),
            ("wait", "!!document.querySelector('#code-tab-menu #code-new-chat')"),
            ("eval", "early", "[document.querySelector('#code-tab-menu #code-new-chat')?.textContent,"
                              " document.querySelector('#code-tab-menu .code-chats-wait')?.textContent]"),
            ("eval", "earlyRows", rows), ("wait", "typeof window.releaseSessions === 'function'"),
            ("eval", "release", "window.releaseSessions(); true"),
            ("wait", f"document.querySelector(\"#code-tab-menu li[data-chat='{chat_d}']\")"), ("eval", "rows", rows),
            ("eval", "wait", "!document.querySelector('#code-tab-menu .code-chats-wait')")], width=1280)
        player.join(5)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["early"] == ["Claude", None] and v["earlyRows"] == [CODE_CHAT_B, CODE_CHAT_C]
    assert v["rows"] == [CODE_CHAT_B, CODE_CHAT_C, chat_d] and v["wait"] is True
    assert not problems(r), problems(r)


def test_code_double_closed_in_files_returns_to_the_latest_place(browser, live):
    """Ревью: «Файлы», сессию закрыли (срок, перезапуск моста): «Сохранить» правки — 409 closed, поле PIN; не вводя PIN,
    к корню проекта — «Уйти без сохранения» — второй отказ того же адреса: то же поле, без нового открытия. Верный PIN —
    «Файлы» там, где застал последний отказ: корень, список прочитан заново; отброшенная правка не вернулась."""
    from fake_bridge import pid
    login = "code-files-closed"
    live.add_user(login)
    machine = code_files_machine()
    machine.pin = CODE_PIN
    aid, sb = code_online(live, login, machine)

    def close_sessions():
        wait_for(lambda: code_requests(machine, "read"), 60)
        time.sleep(0.3)                         # чтение файла успевает ответить
        machine.open_sessions.clear()
        live_sql(live, "UPDATE agents SET name = ? WHERE id = ?", ("Закрытый ПК", aid))

    closer = threading.Thread(target=close_sessions, daemon=True)
    closer.start()
    named = "fetch('/api/agents').then((r) => r.json()).then((j) => j.items.some((a) => a.name === 'Закрытый ПК'))"
    listed = "!document.querySelector('#code-files').hasAttribute('aria-busy')"
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_open(aid)[:-1],
            ("wait", f"document.querySelector(\"#code-projects li[data-pid='{pid('alpha')}']\")"),
            ("click", f"#code-projects li[data-pid='{pid('alpha')}'] a"),
            ("wait", "!!document.querySelector('#code-tab-files')"), ("click", "#code-tab-files"),
            code_root_names(5), ("click", code_item("src")),
            ("wait", "document.querySelector(\"#code-files li[data-name='app.py']\")"),
            ("click", code_item("app.py")), ("wait", "document.querySelector('#code-editor')"),
            set_value("#code-editor", "print('моё')\n"), ("wait", named),
            ("click", "#code-save"), ("wait", CODE_PIN_SHOWN),
            ("click", "#code-path .code-path-link"), ("wait", CODE_UNSAVED), ("click", "#code-unsaved-leave"),
            ("wait", f"document.querySelector('#code-file').hidden && {listed}"),
            ("eval", "second", f"[{CODE_PIN_SHOWN}, document.querySelectorAll('#code-files li').length, location.hash]"),
            ("type", "#code-pin-input", CODE_PIN), ("click", "#code-pin-open"),
            ("wait", f"!{CODE_PIN_SHOWN}"), code_root_names(5), ("eval", "where", f"[location.hash, {CODE_PATH}]"),
            ("click", code_item("src")), ("wait", "document.querySelector(\"#code-files li[data-name='app.py']\")"),
            ("click", code_item("app.py")), ("wait", "document.querySelector('#code-editor')"),
            ("eval", "editor", "document.querySelector('#code-editor').value")], width=1280)
        closer.join(5)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    files = f"#/code/{aid}/{pid('alpha')}?tab=files"
    assert v["second"] == [True, 0, files] and v["where"] == [files, ["alpha"]]
    assert v["editor"] == "print(1)\nprint(2)\n"
    assert [q.get("pin") for q in code_requests(machine, "open")] == [None, CODE_PIN, CODE_PIN]
    assert [q["path"] for q in code_requests(machine, "files")] == ["", "src", "", "", "src"]
    assert machine.projects["alpha"]["files"]["src/app.py"] == CODE_APP
    assert not known_refusals(problems(r), 409), problems(r)


def test_code_lock_then_another_project_leads_to_the_pin(browser, live):
    """Ревью: студия заперлась, а владелец переходит в другой проект: 423 — отпирание, затем 409 closed (запирание
    закрыло сессии мостов) — поле PIN в панели, адрес — тот проект; верный PIN — его сессии. Записей в истории —
    только переход в проект."""
    from fake_bridge import pid
    login = "code-lock-project"
    live.add_user(login)
    machine = code_machine()
    aid, sb = code_online(live, login, machine)
    beta = f"#/code/{aid}/{pid('beta')}"
    try:
        r = browser.drive(live.url + "/", [*phone_with_key(login), *code_open(aid), *code_project(aid, "alpha"),
            ("wait", "!!document.querySelector('#code-chats')"), ("eval", "len0", "history.length"),
            ("eval", "lock", CODE_LOCK_NOW),
            ("click", "#code-panel-toggle"), ("wait", CODE_PANEL_OPEN),
            ("click", f"#code-projects li[data-pid='{pid('beta')}'] a"),
            ("wait", LOCK_OPEN), ("wait", PASSKEY_READY), ("click", "#lock-open"), ("wait", f"!{LOCK_OPEN}"),
            ("wait", CODE_PIN_SHOWN), ("eval", "pin", f"[location.hash, {CODE_PANEL_OPEN}]"),
            ("type", "#code-pin-input", CODE_PIN), ("click", "#code-pin-open"),
            ("wait", f"location.hash === '{beta}' && !!document.querySelector('#code-chats') && !{CODE_PIN_SHOWN}"),
            ("eval", "place", "document.querySelector('#code-place .code-place-project').textContent"),
            ("eval", "len1", "history.length")], **PHONE)
    finally:
        sb.stop()
    assert not r.get("failed"), r
    v = r["values"]
    assert v["pin"] == [beta, True] and v["place"] == "beta" and v["len1"] == v["len0"] + 1
    assert [q.get("pin") for q in code_requests(machine, "open")] == [None, CODE_PIN, CODE_PIN]
    assert [q["project"] for q in code_requests(machine, "chats")][-2:] == [pid("beta")] * 2   # отказ closed, после PIN
    assert not known_refusals(problems(r), 409, 423), problems(r)
