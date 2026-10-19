"""Оболочка приложения в настоящем браузере (app/static): вход, темы, раскладка, PWA.

Студия — живая (`live_app.py`), шлюз подменён. Браузер — свой на каждый тест: cookie у
браузера общие для всех портов localhost, и вход одного теста достался бы следующему.
"""
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
