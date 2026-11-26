"""Строка состояния «Чата» (спека 2026-11-25-studio-skills-status-local-models, п. 12–17, 36): стадия хода слева,
кнопки «Файлы диалога», «Статистика», счётчик инструментов и кольцо контекста справа; окно «Диалог и контекст»,
название диалога в шапке с «Действиями с диалогом»; числа ответа прямого подключения присылает страница."""
import base64
import json
import time
import uuid

import pytest

from fakes import PNG_REAL
from test_ui_browser import (ANSWERED, API, LAST_ANSWER, NO_OVERFLOW, READY, add_cloud, add_gateway, attach,  # noqa: F401
                             browser, go, is_open, known_refusals, live, live_sql, open_new_chat, problems, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

PHOTO = base64.b64encode(PNG_REAL).decode()

EN = "localStorage.setItem('studio-language', 'en')"
ACTIVITY = "document.querySelector('#chat-activity')"
BAR = "document.querySelector('.chat-status-bar')"
STAGE = f"[{ACTIVITY}.dataset.stage, {ACTIVITY}.dataset.state]"
CHAT = "location.hash.split('/')[2]"
# строка в одну линию, видимые кнопки строки и «⋯» — в пределах экрана, страница без горизонтальной прокрутки
FITS = f"""(() => {{ const bar = {BAR}.getBoundingClientRect();
  const act = {ACTIVITY}.getBoundingClientRect();
  const inside = [...{BAR}.querySelectorAll('button')].filter(b => b.offsetParent !== null)
    .every(b => {{ const r = b.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; }});
  return {NO_OVERFLOW} && bar.height <= 48 && act.top >= bar.top && act.bottom <= bar.bottom && inside; }})()"""
ROWS = """Object.fromEntries([...document.querySelectorAll('#chat-context-dialog dd[data-row]')]
  .map(d => [d.dataset.row, d.textContent.replace(/[\\u00a0\\u202f]/g, ' ')]))"""


def files(names) -> str:
    """Выражение JS со списком File: `*.png` — настоящая картинка, прочие — строка текста (как test_ui_chat_composer)."""
    made = [f"new File([Uint8Array.from(atob({PHOTO!r}), c => c.charCodeAt(0))], '{n}', {{ type: 'image/png' }})"
            if n.endswith(".png") else f"new File(['line of {n}\\n'], '{n}', {{ type: 'text/plain' }})" for n in names]
    return "[" + ", ".join(made) + "]"


@pytest.mark.parametrize("width,lang", [(320, "en"), (1280, "ru")])
def test_status_bar_shows_stages_and_fits(browser, live, width, lang):
    login = f"status-{width}"
    live.add_user(login)
    mobile = width == 320
    live.gw.chat_hold = True
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
            ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(mobile),
            ("wait", f"{ACTIVITY} && document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
            ("eval", "idle", STAGE),
            ("type", "#chat-input", "Hello"), ("press", "Enter"),
            ("wait", f"{ACTIVITY}.dataset.stage === 'write'"),
            ("eval", "writing", STAGE),
            ("eval", "place", f"""(() => {{ const bar = {BAR};
              const after = document.querySelector('#messages').compareDocumentPosition(bar) & Node.DOCUMENT_POSITION_FOLLOWING;
              const before = bar.compareDocumentPosition(document.querySelector('#composer')) & Node.DOCUMENT_POSITION_FOLLOWING;
              return !!after && !!before && {ACTIVITY}.getBoundingClientRect().width >= 0.4 * bar.getBoundingClientRect().width - 1; }})()"""),
            ("eval", "fits", FITS), ("eval", "cid", CHAT)],
            width=width, mobile=mobile, init=EN if lang == "en" else None)
    finally:
        live.gw.chat_hold = False
    assert not r.get("failed"), r
    r2 = browser.drive(live.url + "/", [("wait", READY),
        ("eval", "go", f"location.hash = '#/chat/{r['values']['cid']}'"),
        ("wait", f"{ANSWERED} && {ACTIVITY}.dataset.stage === 'idle'"),
        ("eval", "done", f"{ACTIVITY}.dataset.state"), ("eval", "fits", FITS)],
        width=width, mobile=mobile, init=EN if lang == "en" else None)
    assert not r2.get("failed"), r2
    v = r["values"]
    assert v["idle"] == ["idle", "idle"] and v["writing"] == ["write", "responding"]
    assert v["place"] is True and r2["values"]["done"] == "idle"
    assert v["fits"] is True and r2["values"]["fits"] is True
    assert not problems(r) and not problems(r2), (r.get("log"), r2.get("log"))


def test_full_english_status_at_320_keeps_parts_live_in_overflow(browser, live):
    login = "status-overflow-en"
    live.add_user(login)
    setup = """(async () => {
      const { chatStatus } = await import('/static/js/chat-status.js');
      const { partsButton, partNode } = await import('/static/js/chat-parts.js');
      const { contextRing } = await import('/static/js/chat-context.js');
      const host = document.querySelector('#view').appendChild(document.createElement('div'));
      const part = { kind: 'part', n: 1, title: 'Report', state: 'running' };
      const answer = { id: 'answer', status: 'streaming', via: 'server', tools: [part] };
      const ring = contextRing(async () => ({ tokens: 2000, limit: 8000 }));
      const status = chatStatus(host, { ring: ring.node, job: () => null, last: () => answer, local: () => true,
        stepNode: partNode, stats: () => {}, files: () => {}, parts: partsButton({ root: host }) });
      host.append(status.bar); status.dialog({ file_count: 2 }); status.paint(); ring.schedule(true);
      window.finishPart = () => { Object.assign(part, {state: 'done', text: 'Here is the result'}); status.paint(); };
      return true;
    })()"""
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("settings"),
        ("wait", "document.querySelector('.settings-nav')"), ("eval", "setup", setup),
        ("wait", "!document.querySelector('#chat-status-more').hidden && !document.querySelector('#ctx-meter').hidden"),
        ("eval", "fit", FITS), ("click", "#chat-status-more"),
        ("wait", "document.querySelector('#chat-status-menu:popover-open #chat-parts')"),
        ("eval", "labels", "[...document.querySelectorAll('.chat-status-bar button, #chat-status-menu button')]"
                           ".filter(b => !b.hidden).map(b => b.getAttribute('aria-label'))"),
        ("click", "#chat-status-menu #chat-parts"),
        ("wait", "document.querySelector('#chat-parts-dialog .part-state')?.textContent === 'running'"),
        ("eval", "finish", "window.finishPart(); true"),
        ("wait", "document.querySelector('#chat-parts-dialog .part-state')?.textContent === 'done'"),
        ("eval", "result", "document.querySelector('#chat-parts-dialog .part-text').textContent")],
        init=EN, width=320, mobile=True)
    assert not r.get("failed"), r
    v = r["values"]
    assert {"Parts: 0 of 1", "Conversation files: 2", "Statistics", "Tool calls: 1"} <= set(v["labels"])
    assert any(label and label.startswith("Context ~25%") for label in v["labels"])
    assert v["fit"] and v["result"] == "Here is the result"
    assert not problems(r), problems(r)


STAGES = """(async () => {
  const s = await import('/static/js/chat-status.js');
  const now = 1000000;
  const streaming = { status: 'streaming', content: '' };
  const cases = [
    [{ wait: now - 42000, card: { ahead: 2, minutes: 9 }, message: streaming }, streaming],
    [{ scan: { page: 0, pages: 3 }, message: streaming }, streaming],
    [{ message: { ...streaming, recovery: 'summary' } }, { ...streaming, recovery: 'summary' }],
    [{ tool: { kind: 'mcp', label: 'Сервер · lookup' }, message: streaming }, streaming],
    [{ thinking: true, message: streaming }, streaming],
    [{ first: true, message: streaming }, streaming],
    [{ message: streaming, local: true }, streaming],
    [{ message: streaming }, streaming],
    [null, streaming],
    [null, { status: 'done', pending: { id: 'p1' } }],
    [null, { status: 'done' }],
  ];
  const keys = cases.map(([job, m]) => s.stageOf(job, m, { local: !!job?.local, now })?.key ?? null);
  const text = (job, m) => s.stageText(s.stageOf(job, m, { local: false, now }), now);
  return {
    keys,
    card: text({ wait: now - 42000, card: { ahead: 2, minutes: 9 }, message: streaming }, streaming),
    card_plain: text({ wait: now - 5000, message: streaming }, streaming),
    card_silent: text({ wait: now - 20000, eventAt: now - 20000, message: streaming }, streaming),
    tool: text({ tool: { kind: 'mcp', label: 'Сервер · lookup' }, stageKey: 'tool', stageAt: now - 3000,
      eventAt: now - 3000, message: streaming }, streaming),
    quiet: text({ first: true, stageKey: 'write', stageAt: now - 9000, eventAt: now - 9000, message: streaming }, streaming),
    silent: text({ first: true, stageKey: 'write', stageAt: now - 12000, eventAt: now - 12000, message: streaming }, streaming),
    silence: s.SILENCE_MS,
    // ответ прямого подключения, брошенный другой вкладкой («прервано»), — не идущий; живой в другой — идёт
    cut: s.stageOf(null, { status: 'streaming', via: 'browser', live: false })?.key ?? null,
    elsewhere: s.stageOf(null, { status: 'streaming', via: 'browser', live: true })?.key ?? null,
    builtin: text({ tool: { kind: 'todo', label: 'todo_write' }, message: streaming }, streaming),
    memory: text({ tool: { kind: 'memory', label: 'memory_save' }, message: streaming }, streaming),
    part: text({ tool: { kind: 'part', label: 'Сбор' }, message: streaming }, streaming),
  };
})()"""

# время стадии `tool` — с начала текущего вызова: новое событие `tool` сбрасывает его (paint строки)
TOOL_RESTART = """(async () => {
  const s = await import('/static/js/chat-status.js');
  const host = document.body.appendChild(document.createElement('div'));
  const ring = document.createElement('button');
  const job = { message: { status: 'streaming', content: '', calls: [{ kind: 'mcp' }] }, started: Date.now() - 60000,
    tool: { kind: 'mcp', label: 'A · one' } };
  const bar = s.chatStatus(host, { ring, job: () => job, last: () => null, local: () => false,
    stepNode: () => document.createElement('li'), stats: () => {}, files: () => {} });
  host.append(bar.bar);
  job.eventAt = Date.now();
  bar.paint();
  job.stageAt = Date.now() - 30000;
  bar.paint();
  const before = host.querySelector('#chat-activity').textContent;
  job.tool = { kind: 'mcp', label: 'B · two' };
  bar.paint();
  const after = host.querySelector('#chat-activity').textContent;
  bar.unmount(); host.remove();
  return [before, after];
})()"""


def test_stage_words_card_queue_and_silence(browser, live):
    r = browser.drive(live.url + "/", [*signin(), ("wait", READY), ("eval", "v", STAGES), ("eval", "restart", TOOL_RESTART)])
    assert not r.get("failed"), r
    v = r["values"]["v"]
    assert v["cut"] is None and v["elsewhere"] == "remote"
    # встроенные — словом вида, а не именем функции; часть — своим названием
    assert v["builtin"].startswith("инструмент план · ") and v["memory"].startswith("инструмент память · ")
    assert v["part"].startswith("часть: Сбор · ")
    assert r["values"]["restart"] == ["инструмент A · one · 0:30", "инструмент B · two · 0:00"]
    assert v["keys"] == ["card", "scan", "recovery", "tool", "think", "write", "load", "wait", "remote", "pending", None]
    assert v["card"] == "жду карту · 2 · ≈9 мин · 0:42"
    assert v["card_plain"] == "жду карту · 0:05" and v["card_silent"] == "жду карту · 0:20"   # карта — не «тишина»
    assert v["tool"] == "инструмент Сервер · lookup · 0:03"
    assert v["quiet"] == "пишет · 0:09" and v["silent"] == "пишет · 0:12 · последнее событие 12 с назад"
    assert v["silence"] == 10000


# /compact и /continue подменены: окно зовёт их и показывает ответ (настоящие — задачи 7 и 15)
FAKE_FETCH = """(() => { window.calls = []; const real = window.fetch;
  window.fetch = async (url, init = {}) => {
    const path = new URL(url, location.href).pathname;
    if (/\\/(compact|continue)$/.test(path) && (init.method || 'GET') === 'POST') {
      window.calls.push('POST ' + path);
      const body = path.endsWith('/compact') ? { summary_upto: 'x', tokens: 1200, limit: 10000 }
        : { chat_id: window.other, text: 'Итог диалога' };
      return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }
    return real(url, init);
  }; return true; })()"""


@pytest.mark.parametrize("width", [320, 1280])
def test_context_window_numbers_buttons_and_no_data(browser, live, width):
    import chat
    login = f"ctxwin-{width}"
    live.add_user(login)
    mobile = width == 320
    # окно — из списка моделей шлюза (решение Р3, как test_knowledge.small_model): модель `tiny`, 10 000 токенов
    tiny = {"id": "tiny", "context_length": 10000}
    live.gw.models.append(tiny)
    chat.LIMITS.clear()
    try:
        r1 = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
            ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(mobile),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
            ("eval", "fresh", "document.querySelector('#ctx-meter').hidden"),
            ("type", "#chat-input", "Вопрос"), ("press", "Enter"),
            ("wait", f"{ANSWERED} && !document.querySelector('#ctx-meter').hidden"),
            ("eval", "ring", """(() => { const r = document.querySelector('#ctx-meter');
              return [r.tagName, !!r.closest('.code-status-bar'), r.querySelector('.ctx-ring').getBoundingClientRect().width === 18]; })()"""),
            ("click", "#ctx-meter"), ("wait", is_open("#chat-context-dialog")),
            ("wait", "document.querySelector('#chat-context-dialog dd[data-row=limit]').textContent !== '…'"),
            ("eval", "empty", ROWS), ("eval", "cid", CHAT),
            ("eval", "tiny", f"""(async () => {{ const m = {API}; const id = {CHAT};
              const conn = (await m.api('GET', '/api/chats/' + id)).chat.connection_id;
              await m.api('PUT', '/api/sections/chat/links/' + conn, {{ models: ['local-gemma-abl', 'tiny'] }});
              return (await m.api('PATCH', '/api/chats/' + id, {{ model: 'tiny' }})).model; }})()""")],
            width=width, mobile=mobile)
        assert not r1.get("failed"), r1
        cid = r1["values"]["cid"]
        live_sql(live, "UPDATE messages SET usage = ?, gen_ms = 2000, total_ms = 2500 WHERE chat_id = ? AND role = 'assistant'",
                 (json.dumps({"in": 100, "out": 50, "cached": 40}), cid))
        other = uuid.uuid4().hex
        live_sql(live, "INSERT INTO chats(id, user_id, title, connection_id, model, created, updated)"
                       " SELECT ?, user_id, 'Итог', connection_id, model, created, updated FROM chats WHERE id = ?",
                 (other, cid))
        r = browser.drive(live.url + "/", [("wait", READY), ("eval", "go", f"location.hash = '#/chat/{cid}'"),
            ("wait", f"{ANSWERED} && !document.querySelector('#ctx-meter').hidden"),
            ("eval", "fake", FAKE_FETCH), ("eval", "other", f"window.other = {other!r}"),
            ("eval", "under", f"{LAST_ANSWER}.querySelector('.msg-model').textContent"),
            ("click", "#ctx-meter"), ("wait", is_open("#chat-context-dialog")),
            ("wait", "document.querySelector('#chat-context-dialog dd[data-row=limit]').textContent !== '…'"),
            ("eval", "rows", ROWS), ("eval", "fits", NO_OVERFLOW),
            ("click", "#chat-context-compact"),
            ("wait", "window.calls.length === 1 && document.querySelector('#chat-context-dialog dd[data-row=used]')"
                     ".textContent.replace(/[\\u00a0\\u202f]/g, ' ') === '1 200'"),
            ("eval", "compact", "[window.calls[0], document.querySelector('#chat-context-dialog dd[data-row=used]')"
                                ".textContent.replace(/[\\u00a0\\u202f]/g, ' ')]"),
            ("click", "#chat-context-continue"),
            ("wait", f"location.hash === '#/chat/{other}' && document.querySelector('#chat-input').value === 'Итог диалога'"),
            ("eval", "continued", "[location.hash, document.querySelector('#chat-input').value]"),
            ("eval", "fits2", NO_OVERFLOW)],
            width=width, mobile=mobile)
    finally:
        live.gw.models.remove(tiny)
        chat.LIMITS.clear()
    assert not r.get("failed"), r
    v = {**r1["values"], **r["values"]}
    assert v["fresh"] is True
    assert v["ring"] == ["BUTTON", True, True] and v["empty"]["last_in"] == v["empty"]["speed"] == "нет данных"
    assert v["under"].endswith("· 25 ток/с")
    rows = dict(v["rows"])
    assert rows.pop("used").split()[0].isdigit()
    assert v["tiny"] == "tiny" and rows == {"model": "tiny", "connection": "Шлюз", "role": "—", "messages": "2",
                    "limit": "10 000", "last_in": "100", "last_out": "50", "last_cached": "40", "total_in": "100",
                    "total_out": "50", "total_cached": "40", "time": "2,5 с", "speed": "25 ток/с"}
    assert v["compact"] == [f"POST /api/chats/{cid}/compact", "1 200"]
    assert v["continued"] == [f"#/chat/{other}", "Итог диалога"] and v["fits"] is True and v["fits2"] is True
    assert not problems(r), r.get("log")


@pytest.mark.parametrize("width", [320, 1280])
def test_context_window_continue_goes_to_the_real_route(browser, live, width):
    """«Новый диалог с итогом» без подмены (задача 15, `chat_continue`): итог модели — в поле открытого наследника."""
    import chat
    login = f"ctxwin-continue-{width}"
    mobile = width == 320
    live.add_user(login)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
        ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(mobile),
        ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
        ("type", "#chat-input", "Вопрос"), ("press", "Enter"),
        ("wait", f"{ANSWERED} && !document.querySelector('#ctx-meter').hidden"),
        ("eval", "cid", CHAT),
        ("click", "#ctx-meter"), ("wait", is_open("#chat-context-dialog")),
        ("click", "#chat-context-continue"),
        ("wait", "document.querySelector('#chat-input').value !== ''"),
        ("eval", "moved", f"[{CHAT}, document.querySelector('#chat-input').value]"),
        ("eval", "fits", NO_OVERFLOW)], width=width, mobile=mobile)
    assert not r.get("failed"), r
    v = r["values"]
    assert v["moved"][0] != v["cid"] and v["moved"][1] == "".join(live.gw.chat_reply) and v["fits"] is True
    assert live.gw.chat_requests[-1]["messages"][0]["content"] == chat.SUMMARY_NOTE
    assert not problems(r), r.get("log")


def test_context_window_without_usage_says_no_data(browser, live):
    """Приёмка п. 13: шлюз без `usage` — ход работает, в окне «нет данных»; кольцо — кнопка в строке состояния."""
    login = "ctxwin-empty"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
        ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(),
        ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
        ("type", "#chat-input", "Вопрос"), ("press", "Enter"),
        ("wait", f"{ANSWERED} && !document.querySelector('#ctx-meter').hidden"),
        ("eval", "ring", """(() => { const r = document.querySelector('#ctx-meter');
          return [r.tagName, !!r.closest('.code-status-bar'), r.querySelector('.ctx-ring').getBoundingClientRect().width === 18]; })()"""),
        ("click", "#ctx-meter"), ("wait", is_open("#chat-context-dialog")),
        ("eval", "empty", ROWS)])
    assert not r.get("failed"), r
    v = r["values"]
    assert v["ring"] == ["BUTTON", True, True]
    assert v["empty"]["last_in"] == v["empty"]["speed"] == "нет данных"
    assert v["empty"]["messages"] == "2"
    assert not problems(r), r.get("log")


TODO = json.dumps({"items": [{"text": "Шаг", "status": "in_progress"}]}, ensure_ascii=False)


@pytest.mark.parametrize("width", [320, 1280])
def test_title_files_tools_counter_and_stats(browser, live, width):
    login = f"title-{width}"
    uid = live.add_user(login)
    mobile = width == 320
    now = time.time()
    stats_chat = uuid.uuid4().hex
    live_sql(live, "INSERT INTO chats(id, user_id, title, model, created, updated) VALUES (?, ?, 'Статистика', 'm1', ?, ?)",
             (stats_chat, uid, now - 5, now - 5))
    for usage in ({"in": 10, "out": 4}, {"in": 20, "out": 8}):
        live_sql(live, "INSERT INTO messages(id, chat_id, role, content, status, via, model, created, usage, gen_ms)"
                       " VALUES (?, ?, 'assistant', 'ответ', 'done', 'server', 'm1', ?, ?, 450)",
                 (uuid.uuid4().hex, stats_chat, now - 5, json.dumps(usage)))
    hold = {"on": True}
    live.gw.chat_script = [["Принято."], {"tool_calls": [("todo_write", TODO)]}, ["Готово", " полностью."]]
    live.gw.chat_hold = lambda: hold["on"] and "Составь план" in json.dumps(live.gw.chat_requests[-1], ensure_ascii=False)
    TITLE = "document.querySelector('#chat-title')"
    ACTIONS = "document.querySelector('#chat-title-actions')"
    manage = [
        ("eval", "reveal_actions", f"""(() => {{
          if ({ACTIONS}.closest('#chat-status-menu:not(:popover-open)')) document.querySelector('#chat-status-more').click();
          return true;
        }})()"""),
        ("click", "#chat-title-actions"), ("wait", "document.querySelector('#chat-title-menu:popover-open')"),
    ]
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
            ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(mobile),
            ("wait", "document.querySelector('#model-chip').textContent.includes('local-gemma-abl')"),
            ("eval", "new", f"[{TITLE}.textContent, {ACTIONS}.hidden]"),
            ("eval", "clip", """(() => { window.copied = null;
              navigator.clipboard.writeText = async (t) => { window.copied = t; }; return true; })()"""),
            attach(files(["notes.txt", "p.png"])),
            ("wait", "document.querySelectorAll('#composer .att-chip').length === 2"),
            ("type", "#chat-input", "Мой план"), ("press", "Enter"),
            ("wait", f"{ANSWERED} && {TITLE}.textContent === 'Мой план'"),
            ("eval", "title", f"[{TITLE}.textContent, {ACTIONS}.hidden]"),
            ("eval", "cid", CHAT),
            *manage,
            ("eval", "menu", "[...document.querySelectorAll('#chat-title-menu .menu-option')].map(i => i.textContent.trim())"),
            ("click", "#chat-title-copy"), ("wait", "window.copied"),
            ("eval", "copied", "window.copied"),
            ("wait", "!document.querySelector('#chat-title-note').hidden"),
            ("eval", "copy_note", "document.querySelector('#chat-title-note').textContent"),
            ("wait", "document.querySelector('#chat-title-note').hidden"),
            ("eval", "clip_fail", """(() => { navigator.clipboard.writeText = async () => { throw new Error('denied'); };
              return true; })()"""),
            *manage,
            ("click", "#chat-title-copy"), ("wait", "document.querySelector('#chat-error').textContent"),
            ("eval", "copy_fail", """[document.querySelector('#chat-error').textContent,
              document.querySelector('#chat-error').offsetParent !== null, document.querySelector('#chat-title-note').hidden]"""),
            *manage,
            ("click", "#chat-title-rename"), ("wait", is_open("#rename-dialog")),
            ("eval", "clear", "document.querySelector('#rename-dialog input').value = ''"),
            ("type", "#rename-dialog input", "Новое имя"), ("click", "#rename-save"),
            ("wait", f"{TITLE}.textContent === 'Новое имя'"),
            ("eval", "renamed", f"{TITLE}.textContent"),
            ("wait", "document.querySelector('#chat-files-open .code-changed-count')?.textContent === '2'"
                     " && document.querySelector('#chat-files-open').offsetParent !== null"
                     " || !document.querySelector('#chat-status-more').hidden"),
            ("eval", "fcount", "document.querySelector('#chat-files-open .code-changed-count').textContent"),
            ("eval", "open_files", "document.querySelector('#chat-files-open').click(); true"),
            ("wait", is_open("#chat-files-dialog") + " && document.querySelectorAll('#chat-files-dialog li.chat-file').length === 2"),
            ("eval", "files", """[document.querySelector('#chat-files-dialog h2').textContent,
              [...document.querySelectorAll('#chat-files-dialog li.chat-file .chat-file-name')].map(n => n.textContent),
              [...document.querySelectorAll('#chat-files-dialog li.chat-file .chat-file-open')].map(n => n.textContent.trim())]"""),
            ("eval", "close_files", "document.querySelector('#chat-files-dialog').close(); true"),
            ("type", "#chat-input", "Составь план"), ("press", "Enter"),
            ("wait", "!document.querySelector('#chat-tools').hidden"),
            ("eval", "tools_count", """[document.querySelector('#chat-tools').hidden,
              document.querySelector('#chat-tools .code-active-count').textContent]"""),
            ("click", "#chat-tools"), ("wait", "document.querySelector('#chat-tools-menu:popover-open')"),
            ("eval", "tools_steps", "document.querySelectorAll('#chat-tools-menu .web-list > li').length"),
            ("press", "Escape")],
            width=width, mobile=mobile)
    finally:
        hold["on"] = False          # второй круг удержан до этого места: счётчик виден, пока ответ идёт
    try:
        assert not r.get("failed"), r
    finally:
        live.gw.chat_hold = False
        live.gw.chat_script = []
    cid = r["values"]["cid"]
    r2 = browser.drive(live.url + "/", [("wait", READY), ("eval", "go", f"location.hash = '#/chat/{cid}'"),
        ("wait", f"{ANSWERED} && document.querySelector('#chat-tools')"),
        ("eval", "tools_after", "document.querySelector('#chat-tools').hidden"),
        ("eval", "open_stats", "document.querySelector('#chat-stats-open').click(); true"),
        ("wait", is_open("#chat-stats-dialog") + " && document.querySelector('#chat-stats-dialog tbody tr')"),
        ("eval", "stats", """[...document.querySelectorAll('#chat-stats-dialog tbody tr')]
          .map(tr => [...tr.cells].map(c => c.textContent)).find(row => row[1] === 'm1').slice(1, 5)"""),
        ("eval", "fits", NO_OVERFLOW),
        ("eval", "period", "document.querySelector('#chat-stats-dialog p.muted').textContent"),
        ("eval", "close_stats", "document.querySelector('#chat-stats-dialog').close(); true"),
        *go("settings/chat"),
        ("wait", "document.querySelector('#chat-stats-settings')"),
        ("click", "#chat-stats-settings button"),
        ("wait", is_open("#chat-stats-dialog") + " && document.querySelector('#chat-stats-dialog tbody tr')"),
        ("eval", "settings", "true"), ("eval", "fits2", NO_OVERFLOW)],
        width=width, mobile=mobile)
    assert not r2.get("failed"), r2
    v = {**r["values"], **r2["values"]}
    assert v["new"] == ["Без проекта", True] and v["title"] == ["Мой план", False]
    assert v["menu"] == ["Переименовать", "Переместить в проект", "Удалить", "Скопировать адрес"]
    assert v["renamed"] == "Новое имя" and v["copied"].endswith(f"/#/chat/{cid}")
    assert v["copy_note"] == "Адрес скопирован"                   # видимая отметка успеха, уходит сама
    assert v["copy_fail"] == ["Не удалось скопировать адрес", True, True]   # отказ — видимой строкой #chat-error
    assert v["fcount"] == "2"
    assert v["files"] == ["Файлы диалога (2)", ["notes.txt", "p.png"], ["Скачать", "Открыть"]]
    assert v["tools_count"] == [False, "1"] and v["tools_steps"] == 1 and v["tools_after"] is True
    assert v["stats"] == ["m1", "2", "30", "12"] and v["settings"] is True
    assert v["period"] == "Ответы ваших диалогов за 31 день по дням и моделям"
    assert v["fits"] is True and v["fits2"] is True
    assert not problems(r) and not problems(r2), (r.get("log"), r2.get("log"))


KB_CHAT = f"""(async () => {{ const m = {API};
  const kb = await m.api('POST', '/api/knowledge', {{ name: 'Справочник' }});
  const file = new File(['Составь план: сначала шаг один, потом шаг два.'], 'справка.txt', {{ type: 'text/plain' }});
  await m.api('POST', `/api/knowledge/${{kb.id}}/documents`, file,
    {{ headers: {{ 'content-type': 'text/plain', 'x-studio-name': encodeURIComponent(file.name) }} }});
  for (let i = 0; i < 200; i++) {{
    const docs = (await m.api('GET', '/api/knowledge')).items.find(b => b.id === kb.id);
    if (JSON.stringify(docs).includes('"ready"')) break;
    await new Promise(r => setTimeout(r, 50));
  }}
  const chat = await m.api('POST', '/api/chats', {{}});
  await m.api('PUT', `/api/chats/${{chat.id}}/knowledge`, {{ kb_ids: [kb.id] }});
  location.hash = '#/chat/' + chat.id; return chat.id; }})()"""


PICK = """(async () => { const { answerSteps } = await import('/static/js/chat-status.js');
  const auto = { kind: 'knowledge', auto: true }, todo = { kind: 'todo' }, call = { kind: 'mcp' };
  return [{ tools: [auto], calls: [call] }, { tools: [auto] }, { tools: [auto, todo], calls: [call] }, {},
    { tools: [], calls: [call] }].map((m) => answerSteps(m).map((s) => s.kind)); })()"""


@pytest.mark.parametrize("width", [320, 1280])
def test_auto_knowledge_step_is_not_counted_as_a_tool_call(browser, live, width):
    """Ревью задачи 15, п. 7: шаг автоматического блока «Знаний» (`auto`) в `tools` идущего ответа, прочитанного с
    сервера, — не вызов инструмента: счётчик и список строки состояния его не считают."""
    login = f"auto-step-{width}"
    mobile = width == 320
    live.add_user(login)
    hold = {"on": True}
    live.gw.chat_script = [{"tool_calls": [("todo_write", TODO)]}, ["Готово", " полностью."]]
    live.gw.chat_hold = lambda: hold["on"] and "Составь план" in json.dumps(live.gw.chat_requests[-1], ensure_ascii=False)
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
            ("wait", "document.querySelector('.settings-nav')"), ("eval", "cid", KB_CHAT),
            ("wait", "document.querySelector('#chat-input') && document.querySelector('#model-chip')"
                     ".textContent.includes('local-gemma-abl')"),
            ("type", "#chat-input", "Составь план"), ("press", "Enter"),
            ("wait", "!document.querySelector('#chat-tools').hidden")], width=width, mobile=mobile)
        assert not r.get("failed"), r
        cid = r["values"]["cid"]
        r2 = browser.drive(live.url + "/", [("wait", READY), ("eval", "go", f"location.hash = '#/chat/{cid}'"),
            ("wait", "!document.querySelector('#chat-tools').hidden"),
            ("eval", "stored", f"""(async () => {{ const m = {API};
              const last = (await m.api('GET', '/api/chats/' + {CHAT})).messages.at(-1);
              return [last.status, last.tools.map(s => [s.kind, !!s.auto])]; }})()"""),
            ("eval", "count", "document.querySelector('#chat-tools .code-active-count').textContent"),
            ("click", "#chat-tools"), ("wait", "document.querySelector('#chat-tools-menu:popover-open')"),
            ("eval", "steps", "document.querySelectorAll('#chat-tools-menu .web-list > li').length"),
            ("press", "Escape"), ("eval", "pick", PICK), ("eval", "fits", NO_OVERFLOW)], width=width, mobile=mobile)
    finally:
        hold["on"] = False
    try:
        assert not r2.get("failed"), r2
        v = r2["values"]
        assert v["stored"] == ["streaming", [["knowledge", True], ["todo", False]]]   # авто-шаг пришёл с сервера
        assert v["count"] == "1" and v["steps"] == 1 and v["fits"] is True
        # только авто-шаг в `tools` — живые `calls` хода; вызов в `tools` — записанные шаги (answerSteps)
        assert v["pick"] == [["mcp"], ["knowledge"], ["knowledge", "todo"], [], ["mcp"]]
        assert not problems(r) and not problems(r2), (r.get("log"), r2.get("log"))
    finally:
        live.gw.chat_hold = False
        live.gw.chat_script = []


SAVED_NUMBERS = f"""(async () => {{ const m = {API};
  const last = (await m.api('GET', '/api/chats/' + {CHAT})).messages.at(-1);
  return {{ status: last.status, usage: last.usage, gen_ms: last.gen_ms, total_ms: last.total_ms }}; }})()"""


def test_direct_answer_reports_usage_and_survives_a_gateway_without_it(browser, live):
    login = "cloudusage"
    live.add_user(login)
    live.cloud.chat_usage = {"prompt_tokens": 12, "completion_tokens": 5, "prompt_tokens_details": {"cached_tokens": 3}}
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_cloud(live.cloud), *go("settings"),
            ("wait", "document.querySelector('.settings-nav')"), *open_new_chat(),
            ("wait", "document.querySelector('#model-chip').textContent.includes('cloud-chat')"),
            ("type", "#chat-input", "Первый"), ("press", "Enter"),
            ("wait", ANSWERED), ("eval", "saved", SAVED_NUMBERS)])
        assert not r.get("failed"), r
        assert live.cloud.chat_requests[-1]["stream_options"] == {"include_usage": True}
        live.cloud.reject_usage = True
        r2 = browser.drive(live.url + "/", [("wait", READY), *go("chat"),
            ("wait", "document.querySelector('.chat-link')"), ("click", ".chat-link"),
            ("wait", f"{ANSWERED} && document.querySelectorAll('#messages .msg').length === 2"),
            ("type", "#chat-input", "Второй"), ("press", "Enter"),
            ("wait", f"{ANSWERED} && document.querySelectorAll('#messages .msg').length === 4"),
            ("eval", "second", SAVED_NUMBERS),
            ("type", "#chat-input", "Третий"), ("press", "Enter"),
            ("wait", f"{ANSWERED} && document.querySelectorAll('#messages .msg').length === 6")])
    finally:
        live.cloud.chat_usage = None
        live.cloud.reject_usage = False
    assert not r2.get("failed"), r2
    saved = r["values"]["saved"]
    assert saved["status"] == "done" and saved["usage"] == {"in": 12, "out": 5, "cached": 3}
    assert 0 <= saved["gen_ms"] <= saved["total_ms"]
    second = r2["values"]["second"]
    assert [second["status"], second["usage"]] == ["done", None]
    assert ["stream_options" in body for body in live.cloud.chat_requests[-3:]] == [True, False, False]
    assert not problems(r), r.get("log")
    assert not known_refusals(problems(r2), 400), r2.get("log")
