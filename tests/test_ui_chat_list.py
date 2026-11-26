"""Список диалогов «Чата» и вкладки диалогов (chat-list.js, chat-tabs.js; спека 2026-11-25-studio-skills-status-local-models,
п. 23 и 26): метки «идёт ответ», «не прочитано» и черновика без текста в строке, звезда «Закрепить» и группа
«Закреплённые», поиск по названиям на странице; ряд вкладок над лентой — до 12, на устройстве, стирается при выходе."""
import pytest
import state

from test_ui_browser import API, NO_OVERFLOW, READY, add_gateway, browser, go, live, problems, set_value, signin  # noqa: F401

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

ROW = "document.querySelector('.chat-item[data-id=\"' + window.{0} + '\"]')"
MARKS = "[...({0}?.querySelectorAll('.chat-mark') || [])].map((m) => m.dataset.mark)"
FIRST = ("[document.querySelector('.chat-list .chat-group')?.textContent,"
         " document.querySelector('.chat-list .chat-item .chat-link-title')?.textContent,"
         " document.querySelector('.chat-list .chat-item .chat-pin')?.getAttribute('aria-pressed')]")
SHOWN = "[...document.querySelectorAll('.chat-list .chat-item .chat-link-title')].map((a) => a.textContent)"
TABS = "document.querySelectorAll('#chat-tabbar .code-tab')"
ACTIVE = "document.querySelector('#chat-tabbar .code-tab[aria-selected=\"true\"]')"
TITLES = f"[...{TABS}].map((t) => t.querySelector('.code-tab-title').textContent)"
TAB = "document.querySelector('#chat-tabbar .code-tab[data-chat=\"' + window.ids[{0}] + '\"]')"


def start_turn(chat):
    """Ход через сервер: ответ идёт на сервере и без вкладки, чтение потока страница бросает сразу."""
    return f"""const ctl = new AbortController();
      await m.api('POST', '/api/chats/' + {chat} + '/turns', {{ text: 'Привет' }}, {{ signal: ctl.signal }});
      ctl.abort();"""


@pytest.mark.parametrize("width", [320, 1280])
def test_list_marks_pin_and_title_search(browser, live, width):
    login = f"list-{width}"
    uid = live.add_user(login)
    mobile = width == 320
    drawer = [("click", "#chats-toggle"), ("wait", "document.querySelector('.chat.panel-open')")] if mobile else []
    a, b = ROW.format("A"), ROW.format("B")
    # ответ Б идёт, пока человек не закрепил А: метка «идёт ответ» видна, потом опрос списка приносит «не прочитано»
    live.gw.chat_hold = lambda: state.DB.execute(
        "SELECT 1 FROM chats WHERE user_id = ? AND pinned = 1", (uid,)).fetchone() is None
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
            ("wait", "document.querySelector('.settings-nav')"),
            ("eval", "made", f"""(async () => {{ const m = {API};
              const make = async (title) => {{ const c = await m.api('POST', '/api/chats', {{}});
                await m.api('PATCH', '/api/chats/' + c.id, {{ title }}); return c.id; }};
              window.A = await make('Отчёт за квартал'); window.B = await make('План отпуска');
              {start_turn('window.B')}
              sessionStorage.setItem('test-chats', JSON.stringify([window.A, window.B]));   // переживут перезагрузку
              location.hash = '#/chat/' + window.A; return [window.A, window.B]; }})()"""),
            ("wait", f"{a}?.querySelector('[aria-current=page]') && {b}?.querySelector('.chat-mark[data-mark=busy]')"),
            *drawer,
            ("eval", "busy", f"(m => [m.dataset.mark, m.getAttribute('aria-label')])({b}.querySelector('.chat-mark'))"),
            ("eval", "pin", f"{a}.querySelector('.chat-pin').click()"),
            ("wait", f"{FIRST}[0] === 'Закреплённые' && {FIRST}[2] === 'true'"),
            ("eval", "pinned", FIRST),
            ("wait", f"{b}?.querySelector('.chat-mark[data-mark=unread]')"),      # опрос раз в LIST_POLL_MS
            ("eval", "unread", f"(m => [m.dataset.mark, m.getAttribute('aria-label')])({b}.querySelector('.chat-mark'))"),
            ("eval", "text", f"{b}.querySelector('.chat-link-title').textContent"),
            ("eval", "visible_pin", f"getComputedStyle({b}.querySelector('.chat-pin')).opacity"),
            ("eval", "updated", f"{b}.querySelector('time').dateTime"),
            ("eval", "openB", "location.hash = '#/chat/' + window.B"),
            ("wait", f"{b}?.querySelector('[aria-current=page]') && !{b}.querySelector('.chat-mark')"),
            ("eval", "opened", MARKS.format(b)),
            ("eval", "reload", "location.reload()"),
            ("wait", f"{READY} && {FIRST}[0] === 'Закреплённые'"),
            ("eval", "again", "[window.A, window.B] = JSON.parse(sessionStorage.getItem('test-chats'))"),
            *drawer,
            ("eval", "kept", f"JSON.stringify({FIRST}) === JSON.stringify(['Закреплённые', 'Отчёт за квартал', 'true'])"),
            ("eval", "spy", """(() => { window.asked = 0; const real = window.fetch;
              window.fetch = (...args) => { window.asked += 1; return real(...args); }; return true; })()"""),
            ("type", "#chat-search", "ОТПУС"),
            ("wait", f"JSON.stringify({SHOWN}) === JSON.stringify(['План отпуска'])"),
            ("eval", "found", SHOWN),
            set_value("#chat-search", "zzz"),
            ("wait", "document.querySelector('.chat-list .chat-list-note')"),
            ("eval", "none", "document.querySelector('.chat-list .chat-list-note').textContent"),
            ("eval", "asked", "window.asked"),
            set_value("#chat-search", ""),
            ("wait", f"{SHOWN}.length === 2"),
            ("eval", "drafted", """(async () => (await import('/static/js/dialog-drafts.js'))
              .draftStore('studio-chat-drafts').save(window.A, 'текст'))()"""),
            ("wait", f"{a}?.querySelector('.chat-mark[data-mark=draft]')"),
            ("eval", "draft", f"(m => [m.dataset.mark, m.getAttribute('aria-label')])({a}.querySelector('.chat-mark[data-mark=draft]'))"),
            ("eval", "fits", f"""(() => {{ const side = document.querySelector('#chats').getBoundingClientRect();
              return {NO_OVERFLOW} && [...document.querySelectorAll('.chat-list .chat-item')].every((row) =>
                ['.chat-pin', '.chat-more'].every((s) => {{ const r = row.querySelector(s).getBoundingClientRect();
                  return r.width > 0 && r.left >= side.left && r.right <= side.right; }})); }})()""")],
            width=width, mobile=mobile)
    finally:
        live.gw.chat_hold = False
    assert not r.get("failed"), r
    v = r["values"]
    chat_a, chat_b = v["made"]
    assert v["busy"] == ["busy", "идёт ответ"]              # .chat-mark[data-mark] строки Б и его aria-label
    assert v["pinned"] == ["Закреплённые", "Отчёт за квартал", "true"] and v["kept"] is True
    assert v["unread"] == ["unread", "не прочитано"]       # шлюз отпущен, Б не открывали
    assert v["text"] == "План отпуска"
    assert v["visible_pin"] == "1" and v["updated"].endswith("Z")
    assert [v["opened"], f"POST /api/chats/{chat_b}/seen" in r["sent"]] == [[], True]
    assert f"POST /api/chats/{chat_a}/seen" in r["sent"]
    assert v["found"] == ["План отпуска"] and v["none"] == "Ничего не найдено" and v["asked"] == 0
    assert v["draft"] == ["draft", "есть черновик"]
    assert v["fits"] is True                                 # звезда и «⋯» внутри панели списка, NO_OVERFLOW
    assert not problems(r), problems(r)


def open_chat(i):
    return [("eval", "go", f"location.hash = '#/chat/' + window.ids[{i}]"),
            ("wait", f"{ACTIVE}?.dataset.chat === window.ids[{i}]")]


@pytest.mark.parametrize("width", [320, 1280])
def test_section_return_restores_selected_dialog_and_its_draft(browser, live, width):
    login = f"return-chat-{width}"
    live.add_user(login)
    r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
        ("eval", "created", f"""(async () => {{
          const m = {API};
          const chat = await m.api('POST', '/api/chats', {{}});
          window.chatId = chat.id;
          await m.api('POST', '/api/chats/' + chat.id + '/turns', {{text: 'Запрос для поиска'}});
          location.hash = '#/chat/' + chat.id;
        }})()"""),
        ("wait", "document.querySelector('#chat-title-actions:not([hidden])')"),
        ("eval", "send_label", "document.querySelector('#send').textContent"),
        ("eval", "toolbar", "!!document.querySelector('#chat-find').closest('.chat-status-bar, #chat-status-menu')"),
        ("eval", "find", """document.dispatchEvent(new KeyboardEvent('keydown',
          {code: 'KeyF', ctrlKey: true, cancelable: true}))"""),
        set_value("#chat-find-input", "ДЛЯ ПОИСКА"),
        ("wait", "document.querySelector('#chat-find-count').textContent === '1 из 1'"),
        ("press", "Escape"),
        set_value("#chat-input", "Черновик текущего диалога"),
        *go("settings"), ("wait", "document.querySelector('.settings-nav')"),
        *go("chat"), ("wait", "document.querySelector('#chat-input')"),
        ("eval", "returned", "[location.hash === '#/chat/' + window.chatId, document.querySelector('#chat-input').value]"),
    ], width=width, mobile=width == 320)
    assert not r.get("failed"), r
    assert r["values"]["returned"] == [True, "Черновик текущего диалога"]
    assert r["values"]["send_label"] == "Отправить" and r["values"]["toolbar"]


@pytest.mark.parametrize("width,lang", [(320, "en"), (1280, "ru")])      # Review Focus 5
def test_dialog_tabs_open_close_limit_and_device_storage(browser, live, width, lang):
    login = f"tabs-{width}-{lang}"
    uid = live.add_user(login)
    mobile = width == 320
    drawer = [("click", "#chats-toggle"), ("wait", "document.querySelector('.chat.panel-open')")] if mobile else []
    live.gw.chat_hold = True                                 # ответ диалога 6 идёт весь сценарий
    try:
        r = browser.drive(live.url + "/", [*signin(login), ("wait", READY), add_gateway(), *go("settings"),
            ("wait", "document.querySelector('.settings-nav')"),
            ("eval", "ids", f"""(async () => {{ const m = {API}; window.ids = [];
              for (let n = 1; n <= 13; n++) {{ const c = await m.api('POST', '/api/chats', {{}});
                await m.api('PATCH', '/api/chats/' + c.id, {{ title: 'Диалог ' + n }}); window.ids.push(c.id); }}
              {start_turn('window.ids[5]')}
              (await import('/static/js/dialog-drafts.js')).draftStore('studio-chat-drafts').save(window.ids[6], 'черновик');
              return window.ids; }})()"""),
            *open_chat(0),
            ("eval", "one", "document.querySelector('#chat-tabs').hidden"),
            ("click", "#chat-tab-add"),
            ("wait", "document.querySelector('#chat-tab-menu:popover-open')"),
            set_value("#chat-tab-menu input", "Диалог 2"),
            ("wait", "document.querySelectorAll('#chat-tab-menu .code-chat-link').length === 1"),
            ("click", "#chat-tab-menu .code-chat-link"),
            ("wait", f"{ACTIVE}?.dataset.chat === window.ids[1]"),
            *open_chat(1), *open_chat(2),
            ("wait", "!document.querySelector('#chat-tabs').hidden"),
            ("eval", "three", TITLES),
            ("eval", "active", f"{ACTIVE}.querySelector('.code-tab-title').textContent"),
            *[step for i in range(3, 13) for step in open_chat(i)],
            ("eval", "limit", f"[{TABS}.length, {TITLES}[0]]"),
            ("eval", "stored", "JSON.parse(localStorage.getItem('studio-chat-tabs'))"),
            ("wait", f"{TAB.format(5)}?.querySelector('.code-tab-mark.running') && {TAB.format(6)}?.querySelector('.code-tab-draft')"),
            ("eval", "marks", f"[!!{TAB.format(5)}.querySelector('.code-tab-mark.running'), !!{TAB.format(6)}.querySelector('.code-tab-draft')]"),
            ("click", "#chat-tabbar .code-tab[aria-selected=\"true\"] .code-tab-close"),
            ("wait", f"{TABS}.length === 11 && location.hash === '#/chat/' + window.ids[11] && {ACTIVE}?.dataset.chat === window.ids[11]"),
            ("eval", "closed", f"[{TABS}.length, location.hash]"),
            ("eval", "reloading", "location.reload()"),
            ("wait", f"{READY} && {ACTIVE}?.dataset.chat === location.hash.split('/')[2] && document.querySelector('.chat-item')"),
            ("eval", "reload", f"{TABS}.length"),
            *drawer,
            ("eval", "mark", "document.querySelector('.chat-item[data-id=\"' + JSON.parse(localStorage.getItem('studio-chat-tabs')).ids[2] + '\"]').classList.add('under-test')"),
            ("click", ".under-test .chat-more"), ("click", ".under-test .chat-delete"),
            ("wait", "document.querySelector('#chat-delete-dialog')?.open"),
            ("click", "#chat-delete-confirm"),
            ("wait", f"!document.querySelector('#chat-delete-dialog') && {TABS}.length === 10"),
            ("eval", "gone", f"{TABS}.length"),
            ("eval", "fits", f"""(() => {{ const bar = document.querySelector('#chat-tabbar'); const box = bar.getBoundingClientRect();
              const tab = {ACTIVE}.getBoundingClientRect();
              return {{ page: {NO_OVERFLOW}, bar: box.left >= 0 && box.right <= innerWidth,
                scrolls: bar.scrollWidth > bar.clientWidth && getComputedStyle(bar).overflowX === 'auto',
                active: tab.left >= box.left - 0.5 && tab.right <= box.right + 0.5 }}; }})()"""),
            ("eval", "escape", "document.querySelector('.chat.panel-open') && document.querySelector('#chats-backdrop, .chats-backdrop').click()"),
            ("wait", "document.querySelector('#ctx-meter:not([hidden])')"),
            ("click", "#avatar"), ("click", "#menu-logout"),
            ("wait", "location.pathname === '/login' && document.querySelector('#login')"),
            ("eval", "signed_out", "localStorage.getItem('studio-chat-tabs')")],
            width=width, mobile=mobile, init=f"localStorage.setItem('studio-language', '{lang}')")
    finally:
        live.gw.chat_hold = False
    assert not r.get("failed"), r
    v = r["values"]
    ids = v["ids"]
    assert v["one"] is False and v["three"] == ["Диалог 1", "Диалог 2", "Диалог 3"] and v["active"] == "Диалог 3"
    assert v["limit"] == [12, "Диалог 2"]                  # тринадцатый вытеснил самую левую не текущую вкладку
    assert v["stored"] == {"user": uid, "ids": ids[1:]}    # только id
    assert v["closed"] == [11, f"#/chat/{ids[11]}"]        # крестик текущей: вкладок 11, открыта соседняя
    assert v["marks"] == [True, True]                      # идёт ответ у диалога 6, ✎ у черновика диалога 7
    assert v["reload"] == 11 and v["gone"] == 10           # ряд пережил перезагрузку; удалённый диалог ушёл из ряда
    assert v["fits"] == {"page": True, "bar": True, "scrolls": True, "active": True}
    assert v["signed_out"] is None                         # «Выйти»: ключа localStorage нет
    assert not problems(r), problems(r)
