"""Оболочка приложения в настоящем браузере (app/static): вход, темы, раскладка, PWA.

Студия — живая (`live_app.py`), шлюз подменён. Браузер — свой на каждый тест: cookie у
браузера общие для всех портов localhost, и вход одного теста достался бы следующему.
"""
import asyncio
import base64
import io
import json
import os
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
    assert v["conn"] == ["через сервер", ["local-gemma-abl"]]
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
    r = browser.drive(live.url + "/", [*signin("keeper"), ("wait", READY),
        *go("settings/other"), ("wait", "document.querySelector('#storage dd[data-kind=\"total\"]')"),
        ("eval", "before", STORAGE_TEXTS), ("eval", "queue", "document.querySelector('#storage-queue').textContent"),
        ("eval", "fits", NO_OVERFLOW),
        ("click", "#history-wipe"), ("wait", is_open("#wipe-dialog")), ("click", "#wipe-next"),
        ("wait", "!document.querySelector('#wipe-confirm').hidden"), ("click", "#wipe-confirm"),
        ("wait", "document.querySelector('#storage-queue').textContent.startsWith('Очередь удаления пуста')"),
        ("eval", "after", STORAGE_TEXTS)], width=390, mobile=True)
    assert not r.get("failed"), r
    v = r["values"]
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
    assert v["boxes"] == [[k, True] for k in ("chat", "photo", "video", "transcribe", "speech", "voices")]
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
        token: {GW_TOKEN!r}, sections: ['chat', 'photo', 'video', 'transcribe', 'speech', 'dictation'] }});
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
    """«Расшифровка» вылезала за рельс шириной 64 px; на телефоне от 390 px подписи целиком."""
    fits = """[...document.querySelectorAll('#rail .tab span')].map(s => { const t = s.closest('.tab').getBoundingClientRect();
      const b = s.getBoundingClientRect(); return [s.textContent, b.left >= t.left - 0.5 && b.right <= t.right + 0.5,
      s.scrollWidth <= s.clientWidth]; })"""
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY), ("eval", "fits", fits)],
                      width=width, mobile=width < 760)
    assert not r.get("failed"), r
    labels = {name: (inside, whole) for name, inside, whole in r["values"]["fits"]}
    assert set(labels) == {"Чат", "Фото", "Видео", "Расшифровка", "Озвучка"}
    assert all(inside for inside, _ in labels.values()), labels
    if width >= 390:
        assert all(whole for _, whole in labels.values()), labels


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
        ("eval", "pressed", "[...document.querySelectorAll('#view .view-switch [aria-pressed=true]')].map(b => b.id)"),
        ("eval", "tiles", grid_cols("#photo-feed")),
        ("click", "#photo-view-table"),
        ("wait", "document.querySelector('#photo-feed').dataset.view === 'table' && document.querySelector('#photo-feed .cell-prompt')"),
        ("eval", "head", "getComputedStyle(document.querySelector('#photo-feed .feed-head')).display"),
        ("eval", "rows", rows),
        ("eval", "aligned", f"{width} < 760 || {aligned}"),
        ("eval", "tableFits", f"{NO_OVERFLOW} && document.querySelector('#photo-feed').scrollWidth <= document.querySelector('#photo-feed').clientWidth"),
        ("click", NEWEST_DONE), ("wait", is_open("#viewer")), ("wait", "document.querySelector('#viewer .chain-step')"),
        ("press", "Escape"), ("wait", "!document.querySelector('#viewer')"),
        ("click", "#photo-view-preview"), ("wait", "document.querySelector('#photo-feed .card-info')"),
        ("eval", "previewCols", grid_cols("#photo-feed")),
        ("eval", "newest", newest),
        ("idle",), ("eval", "reload", "location.reload()"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY} && {FEED_READY}"),
        ("eval", "kept", "fetch('/api/me').then(r => r.json()).then(m => [document.querySelector('#photo-feed').dataset.view,"
                         " m.views.photo, document.querySelector('#photo-view-preview').getAttribute('aria-pressed')])")],
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


@pytest.mark.parametrize("width", [390, 1280])
def test_photo_selection_deletes_the_picked_and_keeps_the_rest(browser, live, width):
    login = f"sel{width}"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*photo_screen(login, cards=4), ("wait", done_cards(4)),
        ("eval", "all", ids()),
        ("click", "#photo-select"), ("wait", selecting()),
        ("click", nth(1)), ("click", nth(3)),
        ("eval", "viewer", "!!document.querySelector('#viewer')"),
        ("eval", "count", text_of("#photo-select-count")),
        ("eval", "picked", picked()),
        ("click", "#photo-view-table"), ("wait", "document.querySelector('#photo-feed').dataset.view === 'table'"),
        ("eval", "pickedTable", picked()),
        ("eval", "fits", NO_OVERFLOW),
        ("eval", "bar", "getComputedStyle(document.querySelector('.photo .select-bar')).display !== 'none'"
                        " && getComputedStyle(document.querySelector('#photo-composer')).display === 'none'"),
        ("eval", "inHead", "!!document.querySelector('.photo .head #photo-select-delete')"),
        ("click", "#photo-view-tiles"), ("wait", "document.querySelector('#photo-feed').dataset.view === 'tiles'"),
        ("eval", "pickedTiles", picked()),
        ("click", "#photo-select-delete"), ("wait", is_open("#photo-select-delete-dialog")),
        ("eval", "title", text_of("#photo-select-delete-dialog h2")),
        ("click", "#photo-select-delete-confirm"),
        ("wait", f"document.querySelectorAll('{PHOTO_CARD}').length === 2 && !{selecting()}"),
        ("eval", "left", ids()), ("eval", "server", SERVER_IMAGES)],
        width=width, mobile=width == 390)
    assert not r.get("failed"), r
    v = r["values"]
    first, second, third, fourth = v["all"]
    assert v["viewer"] is False and v["count"] == "Выбрано 2"
    assert sorted(v["picked"]) == sorted(v["pickedTable"]) == sorted(v["pickedTiles"]) == sorted([first, third])
    assert v["title"] == "Удалить 2 картинки?"
    assert v["left"] == [second, fourth] and sorted(v["server"]) == sorted([second, fourth])
    if width == 390:
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


def test_photo_selection_too_big_for_the_iphone_is_a_zip(browser, live):
    live.add_user("iosmanybig")
    big = ("eval", "big", """(() => { const real = window.fetch;
      window.fetch = (url, opts) => /^\\/api\\/media\\/[0-9a-f]{32}$/.test(String(url))
        ? Promise.resolve(new Response(new Blob([new Uint8Array(8)], { type: 'image/png' }),
            { headers: { 'Content-Type': 'image/png', 'Content-Length': String(60 * 1024 * 1024) } }))
        : real(url, opts); return true; })()""")
    r = browser.drive(live.url + "/", [*photo_screen("iosmanybig", cards=2), ("wait", done_cards(2)), iphone(), big,
        ("eval", "spy", CLICKS), ("click", "#photo-select"), ("click", "#photo-select-all"),
        ("click", "#photo-select-download"), ("wait", "window.clicked.length === 1"),
        ("eval", "shared", "window.shared.length"), ("eval", "href", "window.clicked[0][0]")], width=390, mobile=True)
    assert not r.get("failed"), r
    assert r["values"]["shared"] == 0 and r["values"]["href"].startswith("/api/media/archives/")   # 2 × 60 МиБ > 100


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
        ("eval", "pressed", "document.querySelector('#photo-view-table').getAttribute('aria-pressed')")])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["before"] == ["light", "tiles"] and v["patched"] == 200
    assert v["stillOpen"] == "tiles" and v["after"] == "table" and v["pressed"] == "true"
    assert not problems(r), problems(r)


def test_photo_translates_the_prompt_into_the_field(browser, live):
    """«EN»: перевод встаёт в поле вместо промпта, генерации нет; во время перевода «Создать» закрыт,
    а «EN» перевод отменяет — промпт остаётся."""
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
            ("click", "#photo-translate"), ("wait", "document.querySelector('#photo-error').textContent.length > 0"),
            ("eval", "empty", "document.querySelector('#photo-error').textContent"),
            ("type", "#photo-prompt", "кошка на подоконнике"), ("click", "#photo-translate"),
            ("wait", "document.querySelector('#photo-translate').textContent.startsWith('EN · ')"),
            ("eval", "busy", "[document.querySelector('#photo-go').disabled, document.querySelector('#photo-prompt').readOnly,"
                             " document.querySelector('#photo-translate').title]"),
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
    assert v["title"] == "Перевести промпт на английский (gemma-abl)" and v["empty"] == "Напишите промпт"
    assert v["busy"] == [True, True, "Отменить перевод"]
    assert v["after"] == ["собака", False, ""]
    first, second = gw.descriptions[d0:]
    assert first["messages"][0]["content"].endswith("кошка на подоконнике")
    assert second["messages"][0]["content"].endswith("собака")
    assert len(gw.generations) == g0 and any(s.endswith("/cancel") for s in r["sent"])
    assert any(s.startswith("DELETE /api/images/translations/") for s in r["sent"])   # перевод в поле — получен
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


@pytest.mark.parametrize("width", [390, 1280])
def test_video_selection_in_every_view(browser, live, ready_clips, width):
    login = f"vsel{width}"
    live.add_user(login)
    steps = [*video_screen(login), *make_clips(3), ("click", "#video-select"), ("wait", selecting(".video"))]
    for i, view in enumerate(("preview", "tiles", "table"), 1):   # по ролику в каждом виде, отметки копятся
        steps += [("click", f"#video-view-{view}"),
                  ("wait", f"document.querySelector('#video-feed').dataset.view === '{view}'"),
                  ("click", nth(i, card=CLIP)),
                  ("eval", f"{view}Picked", picked("#video-feed")),
                  ("eval", f"{view}Viewer", "!!document.querySelector('#video-viewer')"),
                  ("eval", f"{view}Paused", f"[...document.querySelectorAll('{CLIP} video')].every(v => v.paused)"),
                  ("eval", f"{view}Fits", NO_OVERFLOW)]
    steps += [("eval", "bar", "getComputedStyle(document.querySelector('.video .select-bar')).display !== 'none'"),
              ("eval", "label", text_of("#video-select-download"))]
    r = browser.drive(live.url + "/", steps, width=width, mobile=width == 390, timeout=20000)
    assert not r.get("failed"), r
    v = r["values"]
    for n, view in enumerate(("preview", "tiles", "table"), 1):
        assert len(v[f"{view}Picked"]) == n, view
        assert v[f"{view}Viewer"] is False and v[f"{view}Paused"] and v[f"{view}Fits"], view
    assert set(v["previewPicked"]) < set(v["tilesPicked"]) < set(v["tablePicked"])
    assert v["bar"] is (width == 390) and v["label"] == "Скачать"
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
        ("eval", "pressed", "document.querySelector('#video-view-preview').getAttribute('aria-pressed')"),
        ("click", "#video-view-tiles"), ("wait", f"document.querySelector('{CLIP} .video-open')"),
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
        ("click", "#video-view-table"), ("wait", f"document.querySelector('{CLIP} .cell-prompt')"),
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


@pytest.mark.parametrize("width", [390, 1280])
def test_mic_inserts_text_at_the_cursor(browser, live, width):
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
            ("click", "#photo-composer .mic"),
            ("wait", "document.querySelector('#photo-prompt').value === 'рыжий кот на окне'"),
            ("eval", "sent", "window.sentForms.length"),
            ("eval", "overflow", f"!({NO_OVERFLOW})"),
            # микрофон есть и в окне «Изменить», в чате и в видео
            ("click", NEWEST_DONE), ("wait", is_open("#viewer")), ("click", "#viewer .act-edit"),
            ("wait", "document.querySelector('#editor .mic')"), ("press", "Escape"),
            *go("chat"), ("wait", "document.querySelector('#composer .mic')"),
            *go("video"), ("wait", "document.querySelector('#video-composer .mic')")],
            width=width, mobile=width == 390)
    finally:
        live.gw.asr_reply = saved
    assert not r.get("failed"), r
    assert r["values"]["sent"] == 0 and not r["values"]["overflow"]
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
        ("eval", "copy", COPY_SPY), ("click", "#tr-copy"), ("wait", "window.copied"),
        ("eval", "copied", "window.copied"),
        ("eval", "spy", CLICKS), ("click", "#tr-download"), ("click", ".tr-formats [data-format=docx]"),
        ("wait", "window.clicked.length === 1"), ("eval", "clicked", "window.clicked[0]"),
        ("eval", "overflow", f"!({NO_OVERFLOW})")], width=width, mobile=width == 390)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["who"] == ["Собеседник 1 · 00:00", "Собеседник 2 · 00:03"]
    assert "собеседников: 2" in v["meta"] and v["audio"] and "Звонок.wav" in v["item"]
    assert v["copied"].startswith("Собеседник 1 · 00:00\nЗдравствуйте.")
    assert v["clicked"][0].endswith("/export?format=docx") and v["clicked"][1]
    assert not v["overflow"]
    assert not problems(r), problems(r)


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


def test_computer_never_locks(browser, live):
    login = "lk-pc"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("eval", "on", f"(async () => (await {API}.api('POST', '/api/me/screen-lock', {{ enabled: true }})).screen_lock)()"),
        hide(), ("eval", "covered", settled(CURTAIN_OPEN)), later(3_600_000), show(),
        ("eval", "shown", settled(f"{CURTAIN_OPEN} || {LOCK_OPEN}")), ("eval", "me", ME_STATUS)], width=1280)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["on"] is True and v["covered"] is False and v["shown"] is False and v["me"] == 200
    assert not problems(r), problems(r)


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
    if (left > 0 && input === '/api/lock' && init && init.method === 'GET') {
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
    if (fail[input] > 0 && (!init || !init.method || init.method === 'GET')) {
      fail[input] -= 1;
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


# ---- «В работе» и уведомления (спека 2026-10-21-studio-jobs-panel) ----------------------------

JOBS_POLLED = "document.querySelector('#jobs').dataset.polled === '1'"
JOBS_PLACED = """(() => { const j = document.querySelector('#jobs').getBoundingClientRect();
  const apart = (s) => { const b = document.querySelector(s).getBoundingClientRect();
    return j.right <= b.left || b.right <= j.left || j.bottom <= b.top || b.bottom <= j.top; };
  return j.left >= 0 && j.top >= 0 && j.right <= innerWidth && j.bottom <= innerHeight
    && apart('#theme-toggle') && apart('#avatar'); })()"""
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
    «Готово за час» — пусто; значок в окне и не на теме и аватаре."""
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
    """#/<раздел>?id=<запись>: расшифровка и озвучка выбраны в списке, карточка ролика подсвечена; ?id
    из адреса убран. Записи готовы за час — они же в «Готово за час»."""
    login = "jobs-open"
    uid = live.add_user(login)
    now = time.time()
    live_sql(live, "INSERT INTO transcripts(id, user_id, model, name, source_media_id, state, detail, created,"
                   " finished) VALUES ('tr-jobs', ?, 'whisperx', 'Звонок.m4a', 'nope', 'error', 'не вышло', ?, ?)",
             (uid, now - 60, now - 30))
    live_sql(live, "INSERT INTO speeches(id, user_id, model, voice, text, state, detail, created, finished)"
                   " VALUES ('sp-jobs', ?, 'qwen3-tts', 'v', 'Привет', 'error', 'не вышло', ?, ?)",
             (uid, now - 60, now - 20))
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
        *go("video?id=vd-jobs"),
        ("wait", "document.querySelector('.video-card[data-id=\"vd-jobs\"].jobs-focus')"),
        ("eval", "vdHash", "location.hash"),
        ("click", "#jobs"), ("wait", is_open("#jobs-panel")),
        ("eval", "rows", JOBS_ROWS.replace("{list}", "#jobs-finished"))])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["badge"] == "1"
    assert (v["trHash"], v["spHash"], v["vdHash"]) == ("#/transcribe", "#/speech", "#/video")
    assert v["rows"] == [["video", "Ролик · 5 с", True, "error"], ["speech", "Озвучка · Привет", True, "error"],
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
