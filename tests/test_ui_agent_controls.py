"""Размеры, вложения, статусы проектов и подписка в настоящем браузере."""
import base64

import pytest

from fake_bridge import pid
from test_ui_browser import (CODE_CHAT_A, CODE_CHAT_B, READY, browser, code_chat, code_free_machine,
                             code_online, code_project, code_requests, code_session, code_tab_click, code_tab_ready,
                             go, live, problems, set_value, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize("width", [320, 1280])
def test_agents_icon_counts_sessions_across_computers_and_sections(browser, live, width, tmp_path):
    login = f"agent-count-{width}"
    live.add_user(login)
    third = "c3333333-3333-4333-8333-333333333333"
    machine = code_free_machine({CODE_CHAT_A: code_session("Открытый чат"), CODE_CHAT_B: code_session("Работает")})
    machine.projects["beta"] = {"chats": {third: code_session("Ждёт ответа")}}
    machine.project_activity = True
    machine.running = {CODE_CHAT_B: {"project": pid("alpha"), "asks": {}},
                       third: {"project": pid("beta"), "asks": {"r": {}}}}
    machine.script = [{"k": "text", "text": "Работаю"}]
    second = code_free_machine({CODE_CHAT_B: code_session("Другой компьютер")})
    second.project_activity = True
    second.running = {CODE_CHAT_B: {"project": pid("alpha"), "asks": {}}}
    for computer in (machine, second):
        for turn in computer.running.values():
            turn.update(text="", steps=[])
    aid, server = code_online(live, login, machine)
    other_aid, other_server = code_online(live, login, second, name="Второй ПК")
    badge = "document.querySelector('#code-running-count')"
    fits = """(() => {
      const badge = document.querySelector('#code-running-count').getBoundingClientRect();
      const icon = document.querySelector('[data-section=code] svg').getBoundingClientRect();
      return badge.width >= 17 && badge.height >= 17 && badge.left < icon.right && badge.right > icon.right
        && badge.top < icon.top && badge.bottom > icon.top && badge.left >= 0 && badge.right <= innerWidth
        && document.documentElement.scrollWidth <= innerWidth;
    })()"""
    stops = [(aid, "alpha", CODE_CHAT_B), (aid, "beta", third), (other_aid, "alpha", CODE_CHAT_B)]
    stop_all = "import('/static/js/api.js').then(async ({api}) => {" + "".join(
        f"await api('POST','/api/code/agents/{computer}/projects/{pid(project)}/chats/{chat}/stop');"
        for computer, project, chat in stops) + "document.dispatchEvent(new Event('visibilitychange')); return true; })"
    delayed = """(() => {
      const original = window.fetch;
      window.fetch = async (url, options) => {
        const response = await original(url, options);
        if (String(url) === '/api/code/activity') {
          await new Promise(resolve => { window.releaseCount = resolve; });
          const json = response.json.bind(response);
          response.json = async () => { const result = await json(); window.countDelivered = true; return result; };
          window.fetch = original;
        }
        return response;
      };
      document.dispatchEvent(new Event('visibilitychange')); return true;
    })()"""
    event = f"new CustomEvent('studio-code-activity', {{detail: {{aid: {aid}, chat: '{CODE_CHAT_A}', running: true}}}})"
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
            ("wait", f"!{badge}.hidden && {badge}.textContent === '3'"),
            ("eval", "outside", "!location.hash.includes('/code')"),
            ("eval", "label", "document.querySelector('[data-section=code]').getAttribute('aria-label')"),
            ("eval", "fits", fits), ("screenshot", str(tmp_path / f"agent-count-{width}.png")),
            *code_chat(aid, CODE_CHAT_A), set_value("#code-text", "Новая задача"), ("click", "#code-send"),
            ("wait", f"document.querySelector('#code-chat').dataset.running === '1' && {badge}.textContent === '4'"),
            ("click", "#code-stop"), ("wait", f"{badge}.textContent === '3'"),
            *go("photo"), ("eval", "kept", f"!{badge}.hidden && {badge}.textContent === '3'"),
            ("eval", "stopped", stop_all), ("wait", f"{badge}.hidden"),
            ("eval", "emptyLabel", "document.querySelector('[data-section=code]').getAttribute('aria-label')"),
            ("eval", "delay", delayed), ("wait", "typeof window.releaseCount === 'function'"),
            ("eval", "start", f"document.dispatchEvent({event}); document.dispatchEvent({event}); true"),
            ("eval", "release", "window.releaseCount(); true"), ("wait", "window.countDelivered"),
            ("eval", "fresh", f"!{badge}.hidden && {badge}.textContent === '1'"),
            ("eval", "large", "import('/static/js/code-activity.js').then(m => m.setCodeActivity(" + str(aid)
                + ", Array.from({length:123}, (_, i) => ({chat:String(i),running:true}))))"),
            ("eval", "largeFits", fits)],
            width=width, mobile=width == 320)
    finally:
        server.stop()
        other_server.stop()
    assert not result.get("failed"), result
    assert all(result["values"][key] for key in ("outside", "fits", "kept", "fresh", "largeFits"))
    assert result["values"]["label"] == "Агенты · Запущенных сессий: 3"
    assert result["values"]["emptyLabel"] == "Агенты"
    assert not problems(result), problems(result)


@pytest.mark.parametrize("width", [320, 1280])
def test_new_tab_session_history_starts_at_top_after_long_chat(browser, live, width):
    from fake_bridge import T0

    login = f"picker-scroll-{width}"
    live.add_user(login)
    messages = [{"id": f"m-{i}", "role": "user" if i % 2 == 0 else "assistant",
                 "text": "Длинная история.\n\n" * 30, "steps": [], "time": T0 + i} for i in range(20)]
    chats = {f"{i:08x}-1111-4111-8111-111111111111": code_session(f"Сессия {i}") for i in range(60)}
    chats[CODE_CHAT_A] = code_session("Открытый чат", messages=messages)
    machine = code_free_machine(chats)
    aid, server = code_online(live, login, machine)
    top = """(() => {
      const view = document.querySelector('#view');
      const search = document.querySelector('#code-chat-search').getBoundingClientRect();
      return view.scrollTop === 0 && search.top >= document.querySelector('.code-top').getBoundingClientRect().bottom
        && search.bottom <= innerHeight;
    })()"""
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("eval", "longChat", "document.querySelector('#code-chat-scroll').scrollTop > 1000"),
            ("eval", "add", "document.querySelector('#code-tab-add').click(); true"),
            ("wait", "document.querySelectorAll('#code-tab-menu li[data-chat]').length === 60"),
            ("eval", "top", top),
            *code_tab_click(CODE_CHAT_A),
            ("eval", "chatBottom", """(() => { const view = document.querySelector('#code-chat-scroll');
              return view.scrollHeight - view.clientHeight - view.scrollTop < 2; })()"""),
            ("eval", "addAgain", "document.querySelector('#code-tab-add').click(); true"),
            ("wait", "document.querySelector('#code-chats')?.getAttribute('aria-busy') === 'false'"),
            ("eval", "topAgain", top)],
            width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    assert all(result["values"][key] for key in ("longChat", "top", "topAgain", "chatBottom")), result["values"]
    assert not problems(result), problems(result)


@pytest.mark.parametrize("width", [320, 1280])
def test_calculate_all_subfolders_preserves_collapsed_tree_and_editor(browser, live, width, tmp_path):
    from fake_bridge import FakeMachine
    from test_ui_browser import code_item, code_tab

    login = f"file-stats-{width}"
    live.add_user(login)
    machine = FakeMachine({"alpha": {"files": {"src/app.py": b"123", "src/lib/u.py": b"12", ".git/objects/x": b"12345"}}})
    machine.file_stats = True
    calculate_stats = machine._op_file_stats

    def partial_on_repeat(req, events):
        result = calculate_stats(req, events)
        if len(code_requests(machine, 'file_stats')) > 1:
            result['complete'] = False
            for row in result['items']:
                row['complete'] = False
        return result

    machine._op_file_stats = partial_on_repeat
    aid, server = code_online(live, login, machine)
    gate = """(() => { const original = window.fetch;
      window.fetch = async (url, options) => {
        if (String(url).endsWith('/files/stats') && window.holdStats)
          await new Promise(resolve => { window.releaseStats = resolve; });
        return original(url, options);
      }; return true; })()"""
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_tab(aid, "files"),
            ("wait", "document.querySelector('#code-files li[data-path=\"src\"]')"),
            ("eval", "gate", gate), ("eval", "hold", "window.holdStats = true"),
            ("click", "#code-files-calculate"), ("wait", "!!window.releaseStats"),
            ("eval", "busy", "document.querySelector('#code-files-calculate').disabled"),
            ("eval", "release", "window.holdStats=false; window.releaseStats(); true"),
            ("wait", "!document.querySelector('#code-files-calculate').disabled"),
            ("eval", "summary", "document.querySelector('#code-files-stats').textContent"),
            ("eval", "src", """[...document.querySelectorAll('#code-files li[data-path="src"] > button [data-column]')]
              .map(cell => [cell.dataset.column,cell.textContent])"""),
            ("eval", "collapsed", "document.querySelector('#code-files li[data-path=\"src\"]').getAttribute('aria-expanded')"),
            ("click", code_item("src")), ("wait", "document.querySelector('#code-files li[data-path=\"src/lib\"]')"),
            ("eval", "nested", "document.querySelector('#code-files li[data-path=\"src/lib\"] .code-file-size').textContent"),
            ("screenshot", str(tmp_path / f"file-stats-{width}.png")),
            ("click", code_item("app.py")), ("wait", "document.querySelector('#code-editor')"),
            set_value("#code-editor", "unsaved text"),
            ("click", "#code-files-calculate"), ("wait", "!document.querySelector('#code-files-calculate').disabled"),
            ("eval", "editor", "document.querySelector('#code-editor').value"),
            ("eval", "partial", "document.querySelector('#code-files-stats').textContent.includes('Подсчёт неполный')"
                                " && document.querySelector('#code-files li[data-path=\"src\"] .code-file-size').textContent === '≥5'"),
            ("eval", "noDialog", "!document.querySelector('#code-unsaved')"),
            ("eval", "fits", "document.documentElement.scrollWidth <= innerWidth")], width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    values = result["values"]
    assert values["busy"] and '10 Б' in values["summary"] and '3 файла' in values["summary"] and '4 папки' in values["summary"]
    assert {key: value for key, value in values["src"] if key in ('size', 'files', 'folders')} == {'size': '5', 'files': '2', 'folders': '1'}
    assert values["collapsed"] == 'false' and values["nested"] == '2'
    assert values["editor"] == 'unsaved text' and values["noDialog"] and values["fits"] and values["partial"]
    assert len(code_requests(machine, 'file_stats')) == 2
    assert [req['path'] for req in code_requests(machine, 'files')] == ['', 'src']
    assert not problems(result), problems(result)


@pytest.mark.parametrize("width", [320, 1280])
def test_file_tree_details_expand_and_refresh_after_saving(browser, live, width, tmp_path):
    from fake_bridge import FakeMachine, T0
    from test_ui_browser import code_item, code_tab

    login = f"file-details-{width}"
    live.add_user(login)
    machine = FakeMachine({"alpha": {"files": {"src/app.py": b"12345", "src/lib/util.py": b"",
        "many/one.txt": b"x", "legacy.txt": b"", ".gitignore": b"abc"},
        "git": {"branch": "main", "upstream": None, "ahead": 0, "behind": 0,
                "files": [{"path": "src/app.py", "status": " M"}]}}})
    read_files = machine._op_files
    save = machine._op_save

    def details(req, events):
        result = read_files(req, events)
        for item in result["items"]:
            item["created"] = T0
            if item["name"] == "legacy.txt":
                item.pop("created")  # Совместимость со старым мостом.
        if req["path"] == "many":
            result["truncated"] = True
        return result

    def changed(req, events):
        result = save(req, events)
        machine.now = T0 + 3600
        return result

    machine._op_files = details
    machine._op_save = changed
    aid, server = code_online(live, login, machine)
    cells = lambda path: f"""Object.fromEntries([...document.querySelectorAll('#code-files li[data-path="{path}"] > button [data-column]')]
      .map(cell => [cell.dataset.column, cell.textContent]))"""
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_tab(aid, "files"),
            ("wait", "document.querySelector('#code-files li[data-path=\"src\"]')"),
            ("eval", "unopened", cells("src")), ("eval", "legacy", cells("legacy.txt")),
            ("eval", "dotfile", cells(".gitignore")),
            ("click", code_item("src")), ("wait", "document.querySelector('#code-files li[data-path=\"src/app.py\"]')"),
            ("eval", "directory", cells("src")), ("eval", "file", cells("src/app.py")),
            ("eval", "date", f"new Date({T0} * 1000).toLocaleString('ru-RU', {{timeZone:'Europe/Moscow',day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'}})"),
            ("click", code_item("many")),
            ("wait", "document.querySelector('#code-files li[data-path=\"many/one.txt\"]')"),
            ("eval", "limited", cells("many")),
            ("eval", "table", """(() => { const table = document.querySelector('.code-file-table');
              const overflow = table.scrollWidth > table.clientWidth;
              table.scrollLeft = table.scrollWidth;
              const scrolled = table.scrollLeft > 0;
              table.scrollLeft = 0;
              return { overflow, scrolled, fits: document.documentElement.scrollWidth <= innerWidth };
            })()"""),
            ("screenshot", str(tmp_path / f"file-details-{width}.png")),
            ("click", code_item("app.py")), ("wait", "document.querySelector('#code-editor')"),
            set_value("#code-editor", "print('changed')\n"), ("click", "#code-save"),
            ("wait", "document.querySelector('#code-file-status').textContent === 'Сохранено'"),
            ("wait", "document.querySelector('#code-files li[data-path=\"src/app.py\"] .code-file-size').textContent === '17'"),
            ("eval", "saved", cells("src/app.py")),
            ("eval", "expanded", "document.querySelector('#code-files li[data-path=\"src\"]').getAttribute('aria-expanded')"),
            ("eval", "editor", "document.querySelector('#code-editor').value")], width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    values = result["values"]
    assert values["unopened"]["files"] == values["unopened"]["folders"] == '—'
    assert values["directory"]["files"] == values["directory"]["folders"] == '1'
    assert values["directory"]["size"] == 'Папка' and values["directory"]["ext"] == '—'
    assert values["file"] == {"ext": "py", "size": "5", "files": "—", "folders": "—",
                               "created": values["date"], "modified": values["date"]}
    assert values["legacy"]["created"] == '—' and values["legacy"]["size"] == '0'
    assert values["dotfile"]["ext"] == '—'
    assert values["limited"]["files"] == '≥1' and values["limited"]["folders"] == '≥0'
    assert values["table"]["fits"] and (width != 320 or values["table"]["overflow"] and values["table"]["scrolled"])
    assert values["saved"]["size"] == '17' and values["saved"]["modified"] != values["date"]
    assert values["saved"]["created"] == values["date"]
    assert values["expanded"] == 'true' and values["editor"] == "print('changed')\n"
    assert machine.projects["alpha"]["files"]["src/app.py"] == b"print('changed')\n"
    assert not problems(result), problems(result)


@pytest.mark.parametrize("width", [320, 1280])
def test_chat_arrows_follow_user_messages_and_load_earlier_history(browser, live, width, tmp_path):
    from fake_bridge import T0

    login = f"message-arrows-{width}"
    live.add_user(login)
    messages = [{"id": f"m-{i}", "role": "user" if i % 2 == 0 else "assistant",
                 "text": f"Запрос {i}" if i % 2 == 0 else ("Длинный ответ.\n\n" * 20),
                 "steps": [], "time": T0 + i} for i in range(36)]
    machine = code_free_machine({CODE_CHAT_A: code_session("История", messages=messages)})
    aid, server = code_online(live, login, machine)
    def arrow(direction):
        # Драйвер click всегда вызывает scrollIntoView(center), что сдвигает якорь sticky-кнопки.
        # Нажимаем видимую кнопку без прокрутки; настоящие касания проверяет WebKit.
        return ("eval", "arrow", f"document.querySelector('#code-message-{direction}').click(); true")

    at_top = """(() => {
      const top = document.querySelector('#code-chat-scroll').getBoundingClientRect().top;
      return [...document.querySelectorAll('#code-feed .code-msg.user')]
        .find(n => Math.abs(n.getBoundingClientRect().top - top) < 3)?.textContent;
    })()"""
    placement = """(() => {
      const nav = document.querySelector('.code-message-nav').getBoundingClientRect();
      const input = document.querySelector('#code-composer').getBoundingClientRect();
      const toolbar = document.querySelector('.code-status-bar');
      const scroll = document.querySelector('#code-chat-scroll').getBoundingClientRect();
      const button = document.querySelector('#code-message-up'), box = button.getBoundingClientRect();
      return nav.right <= innerWidth && Math.abs(nav.right - input.right) < 2 && nav.bottom < input.top
        && !toolbar.contains(button) && nav.bottom <= scroll.bottom && scroll.bottom <= toolbar.getBoundingClientRect().top
        && button.contains(document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2));
    })()"""
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("eval", "placement", placement),
            ("eval", "bottom", "document.querySelector('#code-message-down').disabled"),
            arrow("up"), ("eval", "last", at_top),
            arrow("up"), ("eval", "previous", at_top),
            arrow("down"), ("eval", "next", at_top),
            ("eval", "tailAvailable", "!document.querySelector('#code-message-down').disabled"),
            arrow("down"),
            ("eval", "end", """(() => { const view = document.querySelector('#code-chat-scroll');
              return view.scrollHeight - view.clientHeight - view.scrollTop < 2
                && document.querySelector('#code-message-down').disabled; })()"""),
            arrow("up"), ("eval", "afterEnd", at_top),
            ("eval", "manual", "document.querySelector('#code-chat-scroll').scrollTop = 0"),
            ("wait", "document.querySelector('#code-chat-scroll').scrollTop === 0"),
            arrow("up"),
            ("wait", "document.querySelectorAll('#code-feed .code-msg').length === 36 && !document.querySelector('#code-more').disabled"),
            ("eval", "older", at_top), arrow("up"),
            arrow("up"), *[arrow("up") for _ in range(9)], ("eval", "first", at_top),
            ("eval", "start", "document.querySelector('#code-message-up').disabled"),
            arrow("down"), ("eval", "afterFirst", at_top),
            ("eval", "bottomAgain", "document.querySelector('#code-chat-scroll').scrollTop = document.querySelector('#code-chat-scroll').scrollHeight"),
            ("wait", "document.querySelector('#code-message-down').disabled"),
            arrow("up"), ("eval", "afterManual", at_top),
            ("screenshot", str(tmp_path / f"message-arrows-{width}.png")),
            ("click", "#code-tab-add"),
            ("wait", "!!document.querySelector('#code-tab-menu #code-new-chat')"),
            ("eval", "away", "!document.querySelector('.code-message-nav')?.getClientRects().length"),
            *code_tab_click(CODE_CHAT_A),
            ("wait", "!document.querySelector('.code-message-nav').hidden"),
            ("eval", "returned", placement)], width=width, mobile=width == 320, touch=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), {"failed": result.get("failed"), "values": result["values"]}
    values = result["values"]
    assert values["placement"] and values["bottom"] and values["away"] and values["returned"], values
    assert values["tailAvailable"] and values["end"], values
    assert [values[key] for key in ("last", "previous", "next", "afterEnd", "older", "first", "afterFirst", "afterManual")] == [
        "Запрос 34", "Запрос 32", "Запрос 34", "Запрос 34", "Запрос 22", "Запрос 0", "Запрос 2", "Запрос 34"]
    assert values["start"]
    assert [r["before"] for r in code_requests(machine, "history")][:2] == [None, 24]
    assert not problems(result), problems(result)


@pytest.mark.parametrize("width", [320, 1280])
def test_session_picker_uses_cached_titles_and_search_while_refreshing(browser, live, width, tmp_path):
    login = f"session-cache-{width}"
    live.add_user(login)
    title = "Длинное название чата про ёлку " * 12
    machine = code_free_machine({CODE_CHAT_A: code_session("Открытый чат"), CODE_CHAT_B: code_session(title)})
    aid, server = code_online(live, login, machine)
    gate = """(() => { const real = window.fetch; window.chatReads = 0;
      window.fetch = async (url, options) => {
        if (String(url).endsWith('/chats') && (!options?.method || options.method === 'GET')) {
          window.chatReads++;
          if (window.holdChats) await new Promise(resolve => { window.releaseChats = resolve; });
        }
        return real(url, options);
      }; return true; })()"""
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
            ("eval", "gate", gate), *code_chat(aid, CODE_CHAT_A),
            ("eval", "hold", "window.holdChats = true"), ("click", "#code-tab-add"),
            ("wait", "!!window.releaseChats && !!document.querySelector('#code-chat-search')"),
            ("eval", "cached", f"!!document.querySelector('#code-chats li[data-chat=\"{CODE_CHAT_B}\"]')"),
            ("eval", "height", """(() => { const n = document.querySelector('.code-chat-title');
              return n.getBoundingClientRect().height <= 2 * parseFloat(getComputedStyle(n).lineHeight) + 1; })()"""),
            ("click", f".code-tab[data-chat='{CODE_CHAT_A}'] .code-tab-title"),
            ("wait", f"document.querySelector('#code-chat')?.dataset.chat === '{CODE_CHAT_A}'"),
            ("click", "#code-tab-add"),
            ("wait", "!!document.querySelector('#code-chat-search')"),
            ("eval", "joined", "window.chatReads"),
            set_value("#code-chat-search", "ЕЛКУ длинное"),
            ("eval", "found", "document.querySelectorAll('#code-chats li[data-chat]').length"),
            set_value("#code-chat-search", "нет такого чата"),
            ("eval", "empty", "document.querySelector('#code-chats').textContent.includes('Ничего не найдено')"),
            ("eval", "refresh", "window.holdChats = false; window.releaseChats(); true"),
            ("wait", "document.querySelector('#code-chats').getAttribute('aria-busy') === 'false'"),
            ("eval", "kept", "[document.querySelector('#code-chat-search').value, document.querySelectorAll('#code-chats li[data-chat]').length]"),
            set_value("#code-chat-search", "елку"),
            ("screenshot", str(tmp_path / f"session-search-{width}.png")),
            ("click", f"#code-chats li[data-chat='{CODE_CHAT_B}'] button"), code_tab_ready(CODE_CHAT_B),
            ("eval", "fits", "document.documentElement.scrollWidth <= innerWidth"),
            ("eval", "reads", "window.chatReads")], width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    values = result["values"]
    assert values["cached"] and values["height"] and values["fits"]
    assert values["found"] == 1 and values["empty"]
    assert values["kept"] == ["нет такого чата", 0]
    assert values["joined"] == values["reads"] == 2
    assert not problems(result), problems(result)


@pytest.mark.parametrize("embedded_state", [True, False], ids=["embedded", "fallback"])
def test_locked_reload_uses_initial_state_without_duplicate_requests(browser, live, monkeypatch, embedded_state):
    import auth
    from test_ui_browser import LOCK_OPEN, PIN_SHOWN, PHONE, known_refusals, phone_with_pin, pin_locked, tap

    if not embedded_state:
        render_page = auth.page

        def without_lock_state(*args, **kwargs):
            return render_page(*args, **kwargs).replace('data-lock-state="{{lock_state}}"', '')

        monkeypatch.setattr(auth, "page", without_lock_state)
    login = "pin-start-once-embedded" if embedded_state else "pin-start-once-fallback"
    live.add_user(login)
    expected_reads = 0 if embedded_state else 1
    gate = """(() => { const real = window.fetch; window.lockReads = 0; window.blockLockReads=true;
      window.fetch = (url, options) => {
        if (new URL(String(url),location.href).pathname === '/api/lock'
          && performance.getEntriesByType('navigation')[0]?.type === 'reload'
          && ++window.lockReads > EXPECTED_READS && window.blockLockReads) return new Promise(() => {});
        return real(url, options);
      }; return true; })()"""
    gate = gate.replace("EXPECTED_READS", str(expected_reads))
    result = browser.drive(live.url + "/", [*phone_with_pin(login), *pin_locked(),
        ("eval", "reload", "location.reload(); true"),
        ("wait", f"performance.getEntriesByType('navigation')[0].type === 'reload' && {LOCK_OPEN} && {PIN_SHOWN}"),
        ("eval", "reads", "window.lockReads"), ("eval", "allowUnlockedPolicy", "window.blockLockReads=false;true"),
        *tap("2580"), ("click", "#lock-pin-ok"),
        ("wait", f"!{LOCK_OPEN} && {READY}")], init=gate, **PHONE)
    assert not result.get("failed"), result
    assert result["values"]["reads"] == expected_reads
    assert not known_refusals(problems(result), 423), problems(result)


@pytest.mark.parametrize("width", [320, 1280])
def test_new_tab_picker_fills_workspace_and_preserves_chat_draft(browser, live, width, tmp_path):
    login = f"agent-picker-{width}"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Чат A"), CODE_CHAT_B: code_session("Чат B")})
    aid, server = code_online(live, login, machine)
    shape = """(() => {
      const picker = document.querySelector('#code-tab-menu'), rect = picker.getBoundingClientRect();
      const body = document.querySelector('.code-body'), area = body.getBoundingClientRect();
      const style = getComputedStyle(body), selects = document.querySelectorAll('.code-starts');
      return { inline: picker.parentElement.classList.contains('code-pane') && getComputedStyle(picker).position === 'static',
        fullWidth: Math.abs(rect.width - area.width + parseFloat(style.paddingLeft) + parseFloat(style.paddingRight)) < 2,
        noHeaderButtons: selects.length === 0, noChat: !document.querySelector('#code-chat'),
        fits: document.documentElement.scrollWidth <= innerWidth };
    })()"""
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *go("code"),
            ("wait", "!!document.querySelector('#code-projects li[data-pid]')"), *code_project(aid, "alpha"),
            ("wait", "document.querySelectorAll('#code-tab-menu li[data-chat]').length === 2"),
            ("eval", "initial", shape), ("screenshot", str(tmp_path / f"picker-empty-{width}.png")),
            ("click", f"#code-chats li[data-chat='{CODE_CHAT_A}'] button"), code_tab_ready(CODE_CHAT_A),
            set_value("#code-text", "Черновик задания"),
            ("eval", "fonts", "[...document.querySelectorAll('.code-pick select')].map(s => getComputedStyle(s.closest('.code-pick').querySelector('.code-pick-value') || s).fontSize).concat(getComputedStyle(document.querySelector('#code-context')).fontSize)"),
            ("click", "#code-tab-add"),
            ("wait", "document.querySelectorAll('#code-tab-menu li[data-chat]').length === 1"),
            ("eval", "opened", shape),
            ("eval", "rows", "[...document.querySelectorAll('#code-tab-menu li[data-chat]')].map(n => n.dataset.chat)"),
            ("click", "#code-place"), ("press", "Escape"),
            ("eval", "stillOpen", "!!document.querySelector('#code-tab-menu #code-new-chat')"),
            ("screenshot", str(tmp_path / f"picker-open-{width}.png")),
            *code_tab_click(CODE_CHAT_A), ("eval", "draft", "document.querySelector('#code-text').value"),
            ("click", "#code-tab-add"),
            ("wait", "document.querySelectorAll('#code-tab-menu li[data-chat]').length === 1"),
            ("click", ".code-tab-close"),
            ("wait", "document.querySelectorAll('#code-tab-menu li[data-chat]').length === 2"),
            ("eval", "lastClosed", "!!document.querySelector('#code-new-chat') && !document.querySelector('.code-tab')")],
            width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get("failed"), result
    values = result["values"]
    assert all(values["initial"].values()) and all(values["opened"].values()), values
    assert values["fonts"] == [f"{11 if width <= 550 else 12}px"] * 4 + ["10px"]
    assert values["rows"] == [CODE_CHAT_B] and values["stillOpen"] and values["lastClosed"]
    assert values["draft"] == "Черновик задания"
    assert not problems(result), problems(result)


@pytest.mark.parametrize("width", [390, 1280])
def test_font_size_and_zoom_persist_without_losing_form(browser, live, width, tmp_path):
    login = f"appearance-{width}"
    live.add_user(login)
    result = browser.drive(live.url + "/", [*signin(login), ("wait", READY),
        ("click", "#avatar"), ("wait", "!document.querySelector('#menu').hidden"),
        ("eval", "initial", "[document.querySelector('#appearance-scale').value, document.documentElement.style.zoom]"),
        ("eval", "options", "[...document.querySelector('#appearance-scale').options].map(o => Number(o.value))"),
        ("eval", "chosenSize", """(() => {
          const f = document.querySelector('#appearance-font'); f.value = '125'; f.dispatchEvent(new Event('input'));
          const s = document.querySelector('#appearance-scale'); s.value = '80'; s.dispatchEvent(new Event('change'));
          return [getComputedStyle(document.body).fontSize, document.documentElement.style.zoom]; })()"""),
        ("screenshot", str(tmp_path / f"appearance-{width}.png")),
        ("eval", "overflow", "document.documentElement.scrollWidth > innerWidth"),
        ("eval", "reload", "location.reload(); true"),
        ("wait", "performance.getEntriesByType('navigation')[0].type === 'reload' && " + READY),
        ("eval", "saved", "[document.querySelector('#appearance-font').value, document.querySelector('#appearance-scale').value]"),
        *go("settings/code"), ("wait", "!!document.querySelector('[data-settings=code]') || !!document.querySelector('.code-defaults-block')"),
        ("eval", "names", "[document.querySelector('#rail [data-section=code] > span:last-child').textContent.trim(), document.querySelector('a[href=\"#/settings/code\"]').textContent]"),
        ("click", "#avatar"),
        ("eval", "largest", """(() => { const f = document.querySelector('#appearance-font'); f.value='150';
          f.dispatchEvent(new Event('input')); const s = document.querySelector('#appearance-scale'); s.value='100';
          s.dispatchEvent(new Event('change')); const r = document.querySelector('#menu').getBoundingClientRect();
          return [document.documentElement.scrollWidth <= innerWidth, r.left >= 0, r.right <= innerWidth,
            document.body.getBoundingClientRect().height <= innerHeight + 1]; })()"""),
        ("screenshot", str(tmp_path / f"appearance-largest-{width}.png")),
        set_value('#appearance-scale', '50'),
        ("eval", "reloadSmallest", "location.reload(); true"),
        ("wait", "performance.getEntriesByType('navigation')[0].type === 'reload' && " + READY),
        ("eval", "smallest", "[document.querySelector('#appearance-scale').value, document.documentElement.style.zoom, document.documentElement.scrollWidth <= innerWidth]")],
        width=width, mobile=width == 390,
        init="if (!localStorage.getItem('studio-ui-scale')) localStorage.setItem('studio-ui-scale', '150');")
    assert not result.get("failed"), result
    assert result["values"]["initial"] == ["100", "1"]
    assert result["values"]["options"] == list(range(50, 101, 5))
    assert result["values"]["chosenSize"] == ["18.75px", "0.8"]
    assert result["values"]["saved"] == ["125", "80"]
    assert result["values"]["names"] == ["Агенты", "Агенты"]
    assert not result["values"]["overflow"]
    assert result["values"]["largest"] == [True, True, True, True]
    assert result["values"]["smallest"] == ["50", "0.5", True]
    assert not problems(result), problems(result)


@pytest.mark.parametrize("width", [390, 1280])
def test_agent_attachments_context_project_status_and_subscription(browser, live, width, tmp_path):
    login = f"agent-controls-{width}"
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session("Вложения"), CODE_CHAT_B: code_session("В работе")})
    machine.projects["beta"] = {"chats": {}}
    machine.attachments = machine.project_activity = machine.subscription_usage = True
    machine.context = {"used": 85000, "limit": 100000, "percent": 85,
                       "usage": {"total": 234567, "input": 200000, "output": 34567, "cached": 10000}}
    machine.running = {CODE_CHAT_B: {"project": pid("alpha"), "asks": {}},
                       "c3333333-3333-4333-8333-333333333333": {"project": pid("beta"), "asks": {"r": {}}}}
    machine.usage_data = {"engine": "claude", "available": True, "source": "test", "checked_at": 1800000000,
        "cache_ttl": 60, "plan": "max", "windows": [
            {"id": "five_hour", "label": "5 часов", "available": True, "used_percent": 25,
             "remaining_percent": 75, "window_minutes": 300, "resets_at": 1800010000},
            {"id": "weekly", "label": "Неделя", "available": True, "used_percent": 40,
             "remaining_percent": 60, "window_minutes": 10080, "resets_at": 1800500000},
            {"id": "monthly", "label": "Месяц", "available": False, "reason": "Не сообщается"}],
        "statistics": {"available": True, "source": "CLI", "summary": {"lifetime_tokens": 12345},
                       "daily": [{"date": "2026-10-30", "tokens": 456}]}}
    machine.script = [{"k": "text", "text": "Получил файлы"}, {"k": "done", "status": "done"}]
    aid, server = code_online(live, login, machine)
    pick = """(() => { const dt = new DataTransfer();
      dt.items.add(new File(['%PDF-sample'], 'документ.pdf', {type:'application/pdf'}));
      dt.items.add(new File([new Uint8Array([0, 255, 7])], 'archive.zip', {type:'application/zip'}));
      const input = document.querySelector('#code-image-input'); input.files = dt.files;
      input.dispatchEvent(new Event('change')); return true; })()"""
    try:
        result = browser.drive(live.url + "/", [*signin(login), ("wait", READY), *code_chat(aid, CODE_CHAT_A),
            ("wait", "[...document.querySelectorAll('.code-project-state:not([hidden])')].length === 2"),
            ("eval", "statuses", "[...document.querySelectorAll('.code-project-state:not([hidden])')].map(n=>n.getAttribute('aria-label'))"),
            ("eval", "context", "[document.querySelector('#code-context-text').textContent, document.querySelector('#code-context').dataset.level]"),
            ("click", "#code-context"), ("wait", "document.querySelector('#code-context-dialog').open"),
            ("eval", "details", "document.querySelector('#code-context-dialog').textContent.replaceAll(/\\s/g,'')"),
            ("screenshot", str(tmp_path / f"agent-context-{width}.png")),
            ("click", "#code-context-dialog .btn"),
            ("wait", "document.querySelector('#code-usage-remaining').textContent === '60%'"),
            ("eval", "week", "document.querySelector('#code-usage').getAttribute('aria-label')"),
            ("eval", "keepTab", "window.firstAgentTab = document.querySelector('.code-tab'); import('/static/js/code-workspace.js').then(w=>w.act('panel',{aid:" + str(aid) + "}))"),
            ("idle",), ("eval", "sameTab", "window.firstAgentTab === document.querySelector('.code-tab')"),
            ("eval", "files", pick), ("wait", "document.querySelectorAll('#code-images .code-file').length === 2"),
            ("eval", "accept", "document.querySelector('#code-image-input').hasAttribute('accept')"),
            ("click", "#code-usage"), ("wait", "!!document.querySelector('[data-window=five_hour]')"),
            ("eval", "usage", "document.querySelector('#code-usage-dialog').textContent"),
            ("screenshot", str(tmp_path / f"agent-usage-{width}.png")), ("click", "#code-usage-dialog .dialog-actions .btn:last-child"),
            ("screenshot", str(tmp_path / f"agent-controls-{width}.png")),
            ("click", "#code-send"), ("wait", "document.querySelector('#code-feed').textContent.includes('Получил файлы')"),
            ("eval", "overflow", "document.documentElement.scrollWidth > innerWidth")], width=width, mobile=width == 390)
    finally:
        server.stop()
    assert not result.get("failed"), result
    assert result["values"]["context"] == ["85%", "orange"]
    assert "100000" in result["values"]["details"] and "234567" in result["values"]["details"]
    assert CODE_CHAT_A in result["values"]["details"] and "60%" in result["values"]["week"]
    assert result["values"]["statuses"] == ["В работе: 1", "Требуют вашего ответа: 1"]
    assert result["values"]["sameTab"] and not result["values"]["accept"]
    assert "Осталось 75%" in result["values"]["usage"] and "Нет данных" in result["values"]["usage"]
    assert "null" not in result["values"]["usage"]
    assert "Доступные сбросы лимита" not in result["values"]["usage"]
    assert "12" in result["values"]["usage"] and "345" in result["values"]["usage"]
    turn = code_requests(machine, "turn")[-1]
    assert turn["text"] == "" and [item["name"] for item in turn["attachments"]] == ["документ.pdf", "archive.zip"]
    assert base64.b64decode(turn["attachments"][1]["data"]) == bytes([0, 255, 7])
    assert not result["values"]["overflow"]
    assert not problems(result), problems(result)
