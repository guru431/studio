"""Оболочка приложения в настоящем браузере (app/static): вход, темы, раскладка, PWA.

Студия — живая (`live_app.py`), шлюз подменён. Браузер — свой на каждый тест: cookie у
браузера общие для всех портов localhost, и вход одного теста достался бы следующему.
"""
import base64
import socket

import pytest

from browser import launch
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
        ("wait", "location.hash === '#/profile'")], width=width, mobile=width == 390)
    assert not r.get("failed"), r
    assert "Чтобы генерировать фото, добавьте в профиле подключение к шлюзу srvllm" in r["values"]["text"]
    assert r["values"]["link"] == "#/profile" and r["values"]["current"] == "photo"
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
    assert r["values"]["items"] == ["Профиль", "Панель", "Выход"] and r["values"]["inside"]
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
        ("eval", "sse", f"{SSE}({live.cloud.base_url!r}, {live.cloud.TOKEN!r})")])
    assert not r.get("failed"), r
    md = r["values"]["md"]
    assert md == {"h1": "Заголовок", "target": "_blank", "rel": "noopener", "script": 0, "onerror": False,
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
    assert r["values"]["items"] == ["Профиль", "Выход"]


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


def test_profile_adds_a_cloud_connection_and_switches_theme(browser, live):
    live.add_user("cloudy")
    dialog = "#conn-dialog"
    r = browser.drive(live.url + "/", [*signin("cloudy"), ("wait", READY), *go("profile"),
        ("wait", "document.querySelector('#conn-add')"),
        ("click", "#conn-add"), ("wait", is_open(dialog)),
        ("eval", "kinds", f"[...document.querySelectorAll('{dialog} [name=kind] option')].map(o => o.textContent)"),
        ("eval", "placeholder", f"document.querySelector('{dialog} [name=base_url]').placeholder"),
        ("type", f"{dialog} [name=name]", "Облако"),
        ("type", f"{dialog} [name=base_url]", live.cloud.base_url),
        ("type", f"{dialog} [name=token]", live.cloud.TOKEN),
        ("eval", "sections", f"[...document.querySelectorAll('{dialog} [name=section]')].map(c => c.value + ':' + c.checked)"),
        ("click", "#conn-save"),
        ("wait", f"!{is_open(dialog)}"),
        ("wait", "document.querySelector('.conn')"),
        ("eval", "conn", "(() => { const c = document.querySelector('.conn');"
                         " return [c.querySelector('.conn-name').textContent, c.querySelector('.conn-url').textContent,"
                         " c.querySelector('.conn-tag').textContent, c.querySelector('.conn-sections').textContent]; })()"),
        ("eval", "chat", f"(async () => {API}.me.capabilities.chat)()"),
        ("eval", "overflow", f"!({NO_OVERFLOW})"),
        # чат по умолчанию: модели прямого подключения браузер берёт у сервиса сам
        choose_first("#default-conn"),
        ("wait", "[...document.querySelectorAll('#default-models option')].some(o => o.value === 'cloud-chat')"),
        ("type", "#default-model", "cloud-chat"),
        ("click", "#default-save"), ("wait", "document.querySelector('#default-status').textContent === 'Сохранено'"),
        ("eval", "default", f"(async () => {{ const me = await {API}.loadMe();"
                            " return [me.default_connection_id !== null, me.default_model]; })()"),
        # новый адрес без нового токена — отказ словами под токеном
        ("click", ".conn .conn-edit"), ("wait", is_open(dialog)),
        ("eval", "tokenEmpty", f"document.querySelector('{dialog} [name=token]').value === ''"
                               f" && document.querySelector('{dialog} [name=token]').type === 'password'"),
        set_value(f"{dialog} [name=base_url]", live.cloud.url + "/v2"),
        ("click", "#conn-save"),
        ("wait", f"document.querySelector('{dialog} .error[data-for=token]').textContent.length > 0"),
        ("eval", "tokenError", f"document.querySelector('{dialog} .error[data-for=token]').textContent"),
        ("press", "Escape"), ("wait", f"!{is_open(dialog)}"),
        # тема — на сервере: после перезагрузки та же
        ("click", "#theme-dark"), ("wait", "document.documentElement.dataset.theme === 'dark'"),
        ("wait", "document.querySelector('#theme-dark').getAttribute('aria-pressed') === 'true'"),
        ("idle",),
        ("eval", "reload", "location.reload()"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY}"),
        ("wait", "document.querySelector('#theme-dark')"),
        ("eval", "theme", "[document.documentElement.dataset.theme,"
                          " document.querySelector('#theme-dark').getAttribute('aria-pressed'),"
                          " document.querySelector('#theme-auto').getAttribute('aria-pressed')]"),
        ("click", "#theme-auto"), ("wait", "!document.documentElement.hasAttribute('data-theme')")],
        width=390, mobile=True)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["kinds"] == ["OpenAI-совместимый", "Шлюз srvllm"]
    assert "/v1" in v["placeholder"]
    assert v["sections"] == ["chat:true"]
    assert v["conn"] == ["Облако", live.cloud.base_url, "из браузера", "Чат"]
    assert v["chat"] is True and not v["overflow"]
    assert v["default"] == [True, "cloud-chat"]
    assert v["tokenEmpty"] and v["tokenError"] == "Новый адрес — введите токен заново"
    assert v["theme"] == ["dark", "true", "false"]
    assert not known_refusals(problems(r), 422), problems(r)


def test_profile_lists_gateway_models_and_changes_the_password(browser, live):
    live.add_user("pwuser")
    with socket.socket() as s:                 # порт, который никто не слушает
        s.bind(("127.0.0.1", 0))
        deaf = f"http://127.0.0.1:{s.getsockname()[1]}/v1"
    r = browser.drive(live.url + "/", [*signin("pwuser"), ("wait", READY),
        ("eval", "added", f"""(async () => {{ const m = {API};
          await m.api('POST', '/api/connections', {{ name: 'Шлюз', kind: 'srvllm', base_url: {GATEWAY_URL!r},
            token: {live.gw.TOKEN!r}, sections: ['chat', 'photo'] }});
          await m.loadMe(); return true; }})()"""),
        *go("profile"), ("wait", "document.querySelector('.conn')"),
        ("eval", "conn", "[document.querySelector('.conn-tag').textContent, document.querySelector('.conn-sections').textContent]"),
        choose_first("#default-conn"),
        ("wait", "[...document.querySelectorAll('#default-models option')].some(o => o.value === 'local-gemma-abl')"),
        # прямой сервис, до которого браузер не достучался (fetch бросает TypeError, как при CORS)
        ("eval", "deaf", f"""(async () => {{ const m = {API};
          await m.api('POST', '/api/connections', {{ name: 'Глухой', kind: 'openai',
            base_url: {deaf!r}, token: 'deaf-key-123', sections: ['chat'] }});
          return true; }})()"""),
        *go("chat"), ("wait", "location.hash === '#/chat'"), *go("profile"),
        ("wait", "document.querySelectorAll('.conn').length === 2"),
        ("eval", "pick", "(() => { const e = document.querySelector('#default-conn');"
                         " e.value = [...e.options].find(o => o.textContent === 'Глухой').value;"
                         " e.dispatchEvent(new Event('change')); })()"),
        ("wait", "document.querySelector('#default-note').textContent.includes('CORS')"),
        ("eval", "cors", "document.querySelector('#default-note').textContent"),
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
    assert v["conn"] == ["через сервер", "Чат, Фото"]
    assert v["cors"] == "Сервис не разрешает запросы из браузера (CORS) — подключите его через сервер"
    assert v["mismatch"] == "Пароли не совпадают" and v["cleared"] == ""
    refused = [e for e in known_refusals(problems(r), 403) if "ERR_CONNECTION_REFUSED" not in e]
    assert not refused, problems(r)


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
        # подключения пользователя — та же форма
        ("click", f"{rid} .user-conns"), ("wait", is_open("#user-conns-dialog")),
        ("click", "#conn-add"), ("wait", is_open("#conn-dialog")),
        ("type", "#conn-dialog [name=name]", "Шлюз"),
        set_value("#conn-dialog [name=kind]", "srvllm"),
        ("eval", "srvSections", "[...document.querySelectorAll('#conn-dialog [name=section]')].map(c => c.value + ':' + c.checked)"),
        ("type", "#conn-dialog [name=base_url]", GATEWAY_URL + "/v1"),
        ("type", "#conn-dialog [name=token]", live.gw.TOKEN),
        ("eval", "tokenType", "document.querySelector('#conn-dialog [name=token]').type"),
        ("click", "#conn-save"),
        ("wait", "document.querySelector('#conn-dialog .error[data-for=base_url]').textContent.length > 0"),
        ("eval", "pathError", "document.querySelector('#conn-dialog .error[data-for=base_url]').textContent"),
        set_value("#conn-dialog [name=base_url]", GATEWAY_URL), ("click", "#conn-save"),
        ("wait", "document.querySelector('#user-conns-dialog .conn')"),
        ("eval", "conn", "[document.querySelector('#user-conns-dialog .conn-tag').textContent,"
                         " document.querySelector('#user-conns-dialog .conn-sections').textContent]"),
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
        ("type", "#allow-entry", "gw.local:4000"), ("click", "#allow-add"),
        ("click", "#allowlist-save"),
        ("wait", "document.querySelector('#allow-status').textContent.length > 0"),
        ("eval", "nameRefused", "document.querySelector('#allow-status').textContent"),
        ("click", "#allow-list li[data-entry='gw.local:4000'] button"),
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
    assert v["srvSections"] == ["chat:true", "photo:true", "video:true"] and v["tokenType"] == "password"
    assert v["pathError"] == "Адрес через сервер — без пути"
    assert v["conn"] == ["через сервер", "Чат, Фото, Видео"]
    assert v["toggle"] == "Включить" and v["selfDelete"] is True and v["deleteLocked"] is True
    assert "gw.local:4000" in v["nameRefused"] and "имена не принимаются" in v["nameRefused"]
    assert v["saved"] == ["10.77.1.15:4000", entry]
    assert not v["overflow"]
    assert not known_refusals(problems(r), 422), problems(r)


def test_history_wipe_clears_chats_and_feed(browser, live):
    live.add_user("wiper")
    r = browser.drive(live.url + "/", [*signin("wiper"), ("wait", READY),
        ("eval", "added", f"""(async () => {{ const m = {API};
          await m.api('POST', '/api/connections', {{ name: 'Шлюз', kind: 'srvllm', base_url: {GATEWAY_URL!r},
            token: {live.gw.TOKEN!r}, sections: ['chat', 'photo', 'video'] }});
          await m.loadMe(); return true; }})()"""),
        make_history(), history_of("before"),
        *go("profile"), ("wait", "document.querySelector('#history-wipe')"),
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
    return ("eval", "cloud", f"""(async () => {{ const m = {API};
      const c = await m.api('POST', '/api/connections', {{ name: {name!r}, kind: 'openai',
        base_url: {cloud.base_url!r}, token: {cloud.TOKEN!r}, sections: ['chat'] }});
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
        ("eval", "ready", "!document.querySelector('#send').hidden && !document.querySelector('#send').disabled")])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["error"].startswith("Не помещается в контекст модели")
    assert v["kept"] == ["Перескажи", 1, 0] and v["ready"]
    assert not known_refusals(problems(r), 413), problems(r)


def test_direct_cloud_chat_saves_the_answer(browser, live):
    live.add_user("clouder")
    # подключение появилось после входа: раздел чата открывается заново, уже не подсказкой
    steps = [*signin("clouder"), ("wait", READY), add_cloud(live.cloud), *go("profile"),
        ("wait", "document.querySelector('.conn')"), *open_new_chat(),
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
        # ответ прямого подключения, который никто не дочитал (вкладку закрыли), — «прервано»
        ("eval", "left", f"""(async () => {{ const m = {API};
          const c = await m.api('POST', '/api/chats', {{}});
          await m.api('POST', '/api/chats/' + c.id + '/turns', {{ text: 'брошенный' }});
          location.hash = '#/chat/' + c.id; return true; }})()"""),
        ("wait", f"{LAST_ANSWER}?.querySelector('.msg-meta').textContent === 'прервано'"),
        ("eval", "free", "!document.querySelector('#send').hidden")]
    r = browser.drive(live.url + "/", steps)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["conn"] == "Облако" and v["answer"] == "Облако на связи." and v["model"] == "cloud-chat"
    assert v["saved"] == ["assistant", "done", "Облако на связи."] and v["free"]
    assert live.cloud.chat_requests[-1]["model"] == "cloud-chat"
    assert live.cloud.chat_requests[-1]["messages"] == [{"role": "user", "content": "Ты здесь?"}]
    assert live.cloud.chat_auth[-1] == f"Bearer {live.cloud.TOKEN}"
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
        r = browser.drive(live.url + "/", [*signin("cloudstop"), ("wait", READY), add_cloud(live.cloud), *go("profile"),
            ("wait", "document.querySelector('.conn')"), *open_new_chat(),
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
