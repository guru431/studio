"""«Чат» на странице (спека 2026-11-25-studio-skills-status-local-models, п. 25, 27, 28, 39, 40): «Открыть» у блоков
`html`/`svg` — окно в `iframe sandbox` без сети и без доступа к Студии; «Новый диалог с итогом» у отказа по длине;
источники «Знаний» под ответом с текстом куска; картинки результата MCP под ответом; выбор уровня рассуждения."""
import base64
import json

import httpx
import pytest

import capabilities
import mcp_client
from fakes import PNG_REAL
from test_chat_web import call
from test_knowledge import big_text
from test_mcp_dialog import HOST, server
from test_mcp import TOKEN as MCP_KEY
from test_ui_browser import (ANSWERED, API, LAST_ANSWER, NO_OVERFLOW, READY, add_cloud, add_gateway, browser,  # noqa: F401
                             go, is_open, live, live_sql, open_new_chat, problems, set_value, signin)
from test_ui_chat_mcp import admin_client

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

CHAT = "location.hash.split('/')[2]"
EN = "localStorage.setItem('studio-language', 'en')"
LISTEN = """(() => { window.probe = null;
  window.addEventListener('message', (e) => { if (e.data && e.data.probe) window.probe = e.data.probe; });
  return true; })()"""
# блок-проба: два fetch, картинка Студии, cookie, localStorage, страница-родитель — итог родителю postMessage
PROBE = """<!doctype html><p>probe</p><script>
(async () => {
  const probe = { ran: true, fetch: [], img: '', cookie: '', storage: '', parent: '' };
  for (const url of ['/health', 'https://example.test/x']) {
    try { await fetch(url); probe.fetch.push('ok'); } catch (e) { probe.fetch.push('rejected'); }
  }
  probe.img = await new Promise((done) => { const i = new Image(); i.onload = () => done('loaded');
    i.onerror = () => done('blocked'); i.src = '/static/icons/icon-192.png'; });
  try { probe.cookie = typeof document.cookie === 'string' ? 'read' : 'denied'; } catch (e) { probe.cookie = 'denied'; }
  try { probe.storage = typeof localStorage.length === 'number' ? 'read' : 'denied'; } catch (e) { probe.storage = 'denied'; }
  try { probe.parent = typeof parent.document.title === 'string' ? 'read' : 'denied'; } catch (e) { probe.parent = 'denied'; }
  parent.postMessage({ probe }, '*');
})();
</script>"""
# decorate: блок ровно PREVIEW_MAX байт — кнопка, на байт больше — нет
BIG = """(async () => { const { previewLinks, PREVIEW_MAX } = await import('/static/js/chat-preview.js');
  const links = previewLinks(null, { host: document.body, chatId: () => 'x', allowed: () => true, failed: () => {} });
  const block = (n) => { const f = document.createDocumentFragment(); const pre = document.createElement('pre');
    const code = document.createElement('code'); code.className = 'language-html'; code.textContent = 'a'.repeat(n);
    pre.append(code); f.append(pre); links.decorate(f); return f.querySelectorAll('.preview-open').length; };
  return [block(PREVIEW_MAX), block(PREVIEW_MAX + 1)]; })()"""
FRAME = "document.querySelector('#chat-preview-dialog iframe.chat-preview-frame')"


@pytest.mark.parametrize("width", [320, 1280])
def test_html_and_svg_blocks_open_in_a_sandbox_without_network(browser, live, width):      # Review Focus 3
    login = f"preview-{width}"
    mobile = width == 320
    live.add_user(login)
    live.gw.chat_script = [["Вот:\n\n```html\n" + PROBE + "\n```\n\n```svg\n<svg xmlns='http://www.w3.org/2000/svg'/>\n```"
                            "\n\n```js\n1\n```\n"]]
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
            ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(mobile),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
            ("type", "#chat-input", "Нарисуй"), ("press", "Enter"),
            ("wait", f"{ANSWERED} && {LAST_ANSWER}.querySelector('.preview-open')"),
            ("eval", "buttons", f"[...{LAST_ANSWER}.querySelectorAll('.preview-open')].map(b => b.dataset.kind)"),
            ("eval", "label", f"{LAST_ANSWER}.querySelector('.preview-open').textContent"),
            ("eval", "listen", LISTEN), ("eval", "cid", CHAT),
            ("click", ".msg.assistant .preview-open[data-kind=html]"),
            ("wait", f"{is_open('#chat-preview-dialog')} && window.probe"),
            ("eval", "frame", f"[{FRAME}.getAttribute('sandbox'), {FRAME}.getAttribute('referrerpolicy'),"
                              f" {FRAME}.getAttribute('src').startsWith('/api/chats/preview/')]"),
            ("eval", "note", "document.querySelector('#chat-preview-dialog .chat-preview-note').textContent"),
            ("eval", "probe", "window.probe"), ("eval", "fits", NO_OVERFLOW),
            ("click", "#chat-preview-dialog .dialog-close"), ("wait", f"!{is_open('#chat-preview-dialog')}"),
            ("eval", "big", BIG)], width=width, mobile=mobile)
    finally:
        live.gw.chat_script = []
    assert not r.get("failed"), r
    cid = r["values"]["cid"]
    live_sql(live, "UPDATE chats SET protected_used = 1, web = 0 WHERE id = ?", (cid,))
    r2 = browser.drive(live.url + "/", [("wait", READY), ("eval", "go", f"location.hash = '#/chat/{cid}'"),
        ("wait", f"{ANSWERED} && {LAST_ANSWER}.querySelector('pre code.language-html')"),
        ("eval", "protected", f"""(async () => {{ const m = {API};
          const n = document.querySelectorAll('.preview-open').length;
          try {{ await m.api('POST', '/api/chats/' + {CHAT} + '/preview', {{ kind: 'html', text: '<p>x</p>' }});
            return [n, 201, '']; }} catch (err) {{ return [n, err.status, err.code]; }} }})()""")],
        width=width, mobile=mobile)
    assert not r2.get("failed"), r2
    v = {**r["values"], **r2["values"]}
    assert v["buttons"] == ["html", "svg"] and v["label"] == "Открыть"      # у блока js кнопки нет
    assert v["frame"] == ["allow-scripts", "no-referrer", True]
    assert v["note"] == "Окно изолировано: без сети и без доступа к Студии"
    assert v["probe"] == {"ran": True, "fetch": ["rejected", "rejected"], "img": "blocked",
                          "cookie": "denied", "storage": "denied", "parent": "denied"}
    assert v["big"] == [1, 0] and v["fits"] is True
    assert v["protected"] == [0, 409, "protected"]      # после перечитывания кнопок нет; прямой POST — отказ


def test_length_refusal_offers_a_new_chat_with_the_summary(browser, live):
    import chat
    login = "continue-offer"
    live.add_user(login)
    # окно — из списка моделей шлюза (решение Р3): модель `tiny`, 9000 токенов
    tiny = {"id": "tiny", "context_length": 9000}
    live.gw.models.append(tiny)
    chat.LIMITS.clear()
    live.gw.chat_script = [["Ответ первый"], ["Итог первого диалога"]]
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
            ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
            ("type", "#chat-input", "Вопрос"), ("press", "Enter"), ("wait", ANSWERED), ("eval", "cid", CHAT),
            ("eval", "tiny", f"""(async () => {{ const m = {API}; const id = {CHAT};
              const conn = (await m.api('GET', '/api/chats/' + id)).chat.connection_id;
              await m.api('PUT', '/api/sections/chat/links/' + conn, {{ models: ['local-gemma-abl', 'tiny'] }});
              return (await m.api('PATCH', '/api/chats/' + id, {{ model: 'tiny' }})).model; }})()"""),
            ("eval", "reload", "location.reload()"),
            ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {READY} && {ANSWERED}"),
            ("eval", "mark", f"window.before = {CHAT}"),
            ("eval", "hidden", "document.querySelector('#chat-continue-offer').hidden"),
            set_value("#chat-input", "слово " * 7000), ("press", "Enter"),
            ("wait", "!document.querySelector('#chat-continue-offer').hidden"),
            ("eval", "offer", "[document.querySelector('#chat-continue').textContent,"
                              " document.querySelector('#chat-continue-offer').hidden]"),
            ("eval", "near", "document.querySelector('#chat-error').nextElementSibling.id"),
            ("eval", "error", "document.querySelector('#chat-error').textContent"),
            ("click", "#chat-continue"),
            ("wait", f"{CHAT} && {CHAT} !== window.before && document.querySelector('#chat-input').value !== ''"),
            ("eval", "moved", "[location.hash.split('/')[2], document.querySelector('#chat-input').value,"
                              " document.querySelector('#chat-continue-offer').hidden]")])
    finally:
        live.gw.models.remove(tiny)
        chat.LIMITS.clear()
        live.gw.chat_script = []
    assert not r.get("failed"), r
    v = r["values"]
    cid = v["cid"]
    assert v["tiny"] == "tiny" and v["hidden"] is True
    assert v["offer"] == ["Новый диалог с итогом", False] and v["near"] == "chat-continue-offer"
    assert v["error"].startswith(chat.CONTEXT_WORDS)
    assert v["moved"][0] != cid and v["moved"][1] == "Итог первого диалога" and v["moved"][2] is True
    assert live.gw.chat_requests[-1]["max_tokens"] == chat.SUMMARY_TOKENS == 2048
    # ответ-ошибка со словами chat.CONTEXT_WORDS: «Новый диалог с итогом» среди кнопок ответа
    live_sql(live, "UPDATE messages SET status = 'error', error = ? WHERE chat_id = ? AND role = 'assistant'",
             (chat.TOO_LONG_WORDS, cid))
    r2 = browser.drive(live.url + "/", [("wait", READY), ("eval", "go", f"location.hash = '#/chat/{cid}'"),
        ("wait", f"{LAST_ANSWER} && {LAST_ANSWER}.querySelector('.msg-continue')"),
        ("eval", "answer_offer", f"[{LAST_ANSWER}.querySelector('.msg-actions .msg-continue').textContent,"
                                 f" !!{LAST_ANSWER}.querySelector('.msg-actions .msg-retry')]")])
    assert not r2.get("failed"), r2
    assert r2["values"]["answer_offer"] == ["Новый диалог с итогом", True]
    assert not problems(r2), r2.get("log")


PIC = base64.b64encode(PNG_REAL).decode()
RESULT = {"content": [{"type": "text", "text": "график готов"}, {"type": "image", "data": PIC, "mimeType": "image/png"}]}
KB_CHAT = f"""(async () => {{ const m = {API};
  const kb = await m.api('POST', '/api/knowledge', {{ name: 'Справочник' }});
  const file = new File([{big_text("Пароль от сейфа — 4711.")!r}], 'справка.txt', {{ type: 'text/plain' }});
  await m.api('POST', `/api/knowledge/${{kb.id}}/documents`, file,
    {{ headers: {{ 'content-type': 'text/plain', 'x-studio-name': encodeURIComponent(file.name) }} }});
  for (let i = 0; i < 400; i++) {{
    const docs = (await m.api('GET', '/api/knowledge')).items.find(b => b.id === kb.id);
    if (JSON.stringify(docs).includes('"ready"')) break;
    await new Promise(r => setTimeout(r, 50));
  }}
  const chat = await m.api('POST', '/api/chats', {{}});
  await m.api('PUT', `/api/chats/${{chat.id}}/knowledge`, {{ kb_ids: [kb.id] }});
  location.hash = '#/chat/' + chat.id; return chat.id; }})()"""
HITS = f"""(async () => {{ const m = {API};
  const last = (await m.api('GET', '/api/chats/' + {CHAT})).messages.at(-1);
  return last.tools.map(s => [s.kind, !!s.auto, (s.hits || []).map(h => h.doc + ' · ' + h.n)]); }})()"""
SOURCES = f"[...{LAST_ANSWER}.querySelectorAll('.web-steps .kb-sources .kb-source')].map(b => b.textContent)"


@pytest.mark.parametrize("width", [320, 1280])
def test_knowledge_sources_and_result_pictures_under_the_answer(browser, live, width):
    login = f"sources-{width}"
    mobile = width == 320
    live.add_user(login)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
        ("wait", "document.querySelector('.settings-nav')"), ("eval", "cid", KB_CHAT),
        ("wait", "document.querySelector('#chat-input') && document.querySelector('#model-chip')"
                 ".textContent.includes('local-gemma-abl')"),
        ("eval", "local", "document.querySelector('#effort-chip').hidden"),     # у srvllm уровня нет
        ("type", "#chat-input", "Какой пароль от сейфа?"), ("press", "Enter"),
        ("wait", f"{ANSWERED} && {LAST_ANSWER}.querySelector('.kb-source')"),
        ("eval", "hits", HITS), ("eval", "sources", SOURCES),
        ("eval", "summary", f"{LAST_ANSWER}.querySelector('.web-steps summary').textContent"),
        ("eval", "badge", f"{LAST_ANSWER}.querySelector('.web-badge').textContent"),
        ("eval", "step", f"{LAST_ANSWER}.querySelector('.web-steps .web-knowledge').firstChild.textContent"),
        ("eval", "open", f"{LAST_ANSWER}.querySelector('.web-steps').open = true"),
        ("click", ".msg.assistant:last-child .kb-source"),
        ("wait", f"{is_open('#kb-source-dialog')} && document.querySelector('#kb-source-dialog .kb-source-text')"),
        ("eval", "chunk", "[document.querySelector('#kb-source-dialog h2').textContent,"
                          " document.querySelector('#kb-source-dialog .kb-source-text').textContent]"),
        ("eval", "fits", NO_OVERFLOW),
        ("click", "#kb-source-dialog .dialog-close"), ("wait", f"!{is_open('#kb-source-dialog')}")],
        width=width, mobile=mobile)
    assert not r.get("failed"), r
    v = r["values"]
    ((kind, auto, hits),) = v["hits"]
    assert (kind, auto) == ("knowledge", True) and hits and all(h.startswith("справка.txt · ") for h in hits)
    assert v["sources"] == [h.replace(" · ", " · кусок ") for h in hits]
    assert v["step"] == "Знания диалога" and v["badge"].startswith("Знания: 1 поиск")
    assert v["summary"] == f"Источники ({len(hits)})"           # авто-шаг — не «Вызовы инструментов»
    assert v["chunk"][0] == v["sources"][0] and "4711" in v["chunk"][1]
    assert v["local"] is True and v["fits"] is True

    # картинка результата MCP — под ответом, нажатие — окно фото
    name, path = f"Графики {width}", f"/pictures-{width}"
    server(live, path, results={"lookup": RESULT})
    admin = admin_client(live)
    try:
        made = admin.post("/api/admin/shared/mcp", json={"name": name, "url": HOST + path, "token": MCP_KEY})
    finally:
        admin.close()
    assert made.status_code == 201, made.text
    live.gw.chat_script = [{"tool_calls": [call(mcp_client.tool_name(name, "lookup"), q="1")]}, ["Вижу график."]]
    try:
        r2 = browser.drive(live.url + "/", [("wait", READY), *open_new_chat(mobile),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
            ("type", "#chat-input", "Нарисуй график"), ("press", "Enter"),
            ("wait", f"{ANSWERED} && {LAST_ANSWER}.querySelector('.msg-pictures .att-thumb img')?.complete"),
            ("eval", "count", f"{LAST_ANSWER}.querySelectorAll('.msg-pictures .att-thumb').length"),
            ("eval", "loaded", f"{LAST_ANSWER}.querySelector('.msg-pictures .att-thumb img').naturalWidth > 0"),
            ("click", ".msg.assistant:last-child .msg-pictures .att-thumb"),
            ("wait", is_open("#att-photo-dialog")),
            ("eval", "photo", "document.querySelector('#att-photo-dialog h2').textContent"),
            ("eval", "fits", NO_OVERFLOW)], width=width, mobile=mobile)
    finally:
        live.gw.chat_script = []
    assert not r2.get("failed"), r2
    v2 = r2["values"]
    assert [v2["count"], v2["loaded"]] == [1, True] and v2["photo"] == "lookup-1.png" and v2["fits"] is True
    assert not problems(r) and not problems(r2), (r.get("log"), r2.get("log"))


EFFORT_ITEMS = "[...document.querySelectorAll('#effort-menu [role=menuitemradio]')].map(i => i.textContent.trim())"
LEVEL = "document.querySelector('#effort-chip .effort-level').textContent"


@pytest.mark.parametrize("width,lang", [(320, "en"), (1280, "ru")])
def test_effort_chip_reaches_the_provider_and_stays(browser, live, monkeypatch, width, lang):
    login = f"effort-{width}"
    mobile = width == 320
    live.add_user(login)
    monkeypatch.setitem(capabilities.PROFILES, httpx.URL(live.cloud.base_url).host, "openai")
    init = EN if lang == "en" else None
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_cloud(live.cloud), *go("settings"),
        ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(mobile),
        ("wait", "document.querySelector('#model-chip').textContent.includes('cloud-chat')"
                 " && !document.querySelector('#effort-chip').hidden"),
        ("eval", "fresh", LEVEL),
        ("click", "#effort-chip"), ("wait", "document.querySelector('#effort-menu:popover-open')"),
        ("eval", "items", EFFORT_ITEMS), ("eval", "menu_fits", NO_OVERFLOW),
        ("click", "#effort-menu [data-effort=high]"), ("wait", "!document.querySelector('#effort-menu:popover-open')"),
        ("eval", "chosen", LEVEL),
        ("type", "#chat-input", "Привет"), ("press", "Enter"), ("wait", ANSWERED), ("eval", "cid", CHAT),
        ("eval", "fits", NO_OVERFLOW)], width=width, mobile=mobile, init=init)
    assert not r.get("failed"), r
    cid = r["values"]["cid"]
    assert live.cloud.chat_requests[-1]["reasoning_effort"] == "high"
    r2 = browser.drive(live.url + "/", [("wait", READY), ("eval", "go", f"location.hash = '#/chat/{cid}'"),
        ("wait", f"{ANSWERED} && !document.querySelector('#effort-chip').hidden"),
        ("eval", "kept", LEVEL), ("eval", "fits", NO_OVERFLOW),
        ("eval", "chip_fits", """(() => { const r = document.querySelector('#effort-chip').getBoundingClientRect();
          return r.left >= 0 && r.right <= innerWidth; })()""")], width=width, mobile=mobile, init=init)
    assert not r2.get("failed"), r2
    v = {**r["values"], **r2["values"]}
    words = {"ru": ["Как у модели", "Низкий", "Средний", "Высокий"], "en": ["Model default", "Low", "Medium", "High"]}[lang]
    assert v["fresh"] == words[0] and v["items"] == words and v["chosen"] == v["kept"] == words[3]
    assert v["fits"] is True and v["menu_fits"] is True and v["chip_fits"] is True
    assert not problems(r) and not problems(r2), (r.get("log"), r2.get("log"))
