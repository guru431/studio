"""Оболочка приложения в настоящем браузере (app/static): вход, темы, раскладка, PWA.

Студия — живая (`live_app.py`), шлюз подменён. Браузер — свой на каждый тест: cookie у
браузера общие для всех портов localhost, и вход одного теста достался бы следующему.
"""
import asyncio
import base64
import io
import json
import re
import socket
import time

import httpx
import pytest
from PIL import Image

from browser import launch
from fakes import PNG_REAL, TOKEN as GW_TOKEN, VIDEO_MODEL, video_object
from live_app import start_live_app

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

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


def problems(r):
    """Ошибки журнала браузера: исключения, console.error, нарушения CSP, отказы загрузки."""
    return r.get("log", [])


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
    login = f"nogw{width}"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("eval", "go", "location.hash = '#/photo'"),
        ("wait", "document.querySelector('#view .hint')"),
        ("eval", "text", "document.querySelector('#view .hint').textContent"),
        ("eval", "link", "document.querySelector('#view .hint a').getAttribute('href')"),
        ("eval", "current", "document.querySelector('#rail a[aria-current=page]').dataset.section"),
        ("click", "#view .hint a"),
        ("wait", "location.hash === '#/settings/photo' && document.querySelector('.section-panel[data-section=photo]')")],
        width=width, mobile=width == 390)
    assert not r.get("failed"), r
    assert "Чтобы генерировать фото, выберите модель в настройках раздела «Фото»" in r["values"]["text"]
    assert r["values"]["link"] == "#/settings/photo" and r["values"]["current"] == "photo"
    assert not problems(r), problems(r)


@pytest.mark.parametrize("width", [390, 1280])
def test_avatar_menu_and_logout(browser, live, width):
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY),
        ("click", "#avatar"), ("wait", "!document.querySelector('#menu').hidden"),
        ("eval", "items", "[...document.querySelectorAll('#menu .menu-item')].filter(e => !e.hidden).map(e => e.textContent.trim())"),
        ("eval", "inside", "(() => { const b = document.querySelector('#menu').getBoundingClientRect();"
                           " return b.left >= 0 && b.top >= 0 && b.right <= innerWidth && b.bottom <= innerHeight; })()"),
        ("press", "Escape"), ("wait", "document.querySelector('#menu').hidden"),
        ("click", "#avatar"), ("click", "#menu-logout"),
        ("wait", "location.pathname === '/login' && document.querySelector('#login')")],
        width=width, mobile=width == 390)
    assert not r.get("failed"), r
    assert r["values"]["items"] == ["Настройки", "Панель", "Выход"] and r["values"]["inside"]
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
    assert live.cloud.chat_requests[-1]["messages"] == [{"role": "user", "content": "привет"}]


def test_non_admin_menu_has_no_panel_and_admin_route_falls_back(browser, live):
    live.add_user("plain")
    r = browser.drive(live.url + "/", [*signin("plain"), ("wait", READY),
        ("click", "#avatar"), ("wait", "!document.querySelector('#menu').hidden"),
        ("eval", "items", "[...document.querySelectorAll('#menu .menu-item')].filter(e => !e.hidden).map(e => e.textContent.trim())"),
        ("eval", "go", "location.hash = '#/admin'"), ("wait", "location.hash === '#/chat'")])
    assert not r.get("failed"), r
    assert r["values"]["items"] == ["Настройки", "Выход"]


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


def test_settings_menu_and_old_profile_route(browser, live):
    live.add_user("menuer")
    r = browser.drive(live.url + "/", [*signin("menuer"), ("wait", READY),
        ("click", "#avatar"), ("eval", "item", "document.querySelector('#menu a[href=\"#/settings\"]').textContent"),
        ("press", "Escape"),
        ("eval", "go", "location.hash = '#/profile'"), ("wait", "location.hash.startsWith('#/settings')"),
        ("wait", "document.querySelector('.settings-nav a')"),
        ("eval", "nav", "[...document.querySelectorAll('.settings-nav a')].map(a => a.dataset.section)")])
    assert not r.get("failed"), r
    assert r["values"]["item"] == "Настройки"
    assert r["values"]["nav"] == ["chat", "photo", "video", "transcribe", "speech", "dictation", "other"]
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
    assert v["conn"] == ["через сервер", ["local-gemma-abl"]]
    assert v["toggle"] == "Включить" and v["selfDelete"] is True and v["deleteLocked"] is True
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
        ("click", "#wipe-next"), ("wait", "!document.querySelector('#wipe-confirm').hidden"),
        ("click", "#wipe-confirm"),
        ("wait", "document.querySelector('#wipe-status').textContent === 'История удалена'"),
        ("eval", "closed", "!" + is_open("#wipe-dialog")),
        history_of("after"),
        ("eval", "conns", f"(async () => (await {API}.api('GET', '/api/connections')).items.length)()")],
        width=390, mobile=True)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["made"] is True and v["before"] == [1, 1]
    assert v["confirmHidden"] is True and v["closed"] is True
    assert v["after"] == [0, 0] and v["conns"] == 1
    assert not problems(r), problems(r)


# ---- чат -----------------------------------------------------------------------

CHAT_ID = "/^#\\/chat\\/[0-9a-f]{32}$/.test(location.hash)"
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
    assert live.gw.chat_requests[-1]["messages"] == [{"role": "user", "content": "Привет, студия"}]
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
    assert live.cloud.chat_requests[-1]["messages"] == [{"role": "user", "content": "Ты здесь?"}]
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

MODEL_IDS = {"lustify": "lustify-v10-krea2-turbo", "flux": "flux.2-klein-4b"}
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
        token: {GW_TOKEN!r}, sections: ['chat', 'photo', 'video'] }});
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
            ("eval", "forms", "window.sentForms")], width=width, height=900, mobile=width == 390)
    finally:
        live.gw.description_reply = saved
    assert not r.get("failed"), r
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
        ("wait", "location.hash.startsWith('#/video?source=')"),
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
  const feed = createFeed(box, { key: (item) => item.id, render: card, load: async (before) => {
    calls.push(before ?? null);
    return all.filter(x => before === undefined || x.created < before).slice(0, 10); } });
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
    assert f["calls"][0] is None and len(f["calls"]) == 4    # три страницы и пустая
    assert f["after"] == {"first": "new", "updated": "i0*", "titles": ["Сегодня", f["titles"][2]], "count": 21}
    assert f["reloaded"]["count"] == 10 and f["reloaded"]["calls"] == 5
    assert not problems(r), problems(r)


# ---- видео ---------------------------------------------------------------------

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
                             " document.querySelector('#chip-smooth').textContent,"
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
    assert "Обычно 576p" in quality and "32 к/с" in smooth_text
    assert "пусто — движение придумает gemma-abl" in placeholder
    assert not v["overflow"] and v["cols"] == 2 and v["aboveTabs"] and v["inside"] and v["menu"]
    assert v["qualities"] == ["Быстро 480p", "Обычно 576p", "Высоко 720p"]
    assert v["error"].startswith("Длительность") and v["cleared"]
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
