"""«Агенты» (D2): «Входящие» по значку-счётчику, плашка вопроса над полем, признаки и порядок вкладок, клавиши,
поиск проектов, «Отметить прочитанными» и «был на связи» — в настоящем браузере."""
import pytest

from fake_bridge import T0, pid
from test_ui_browser import (CODE_CHAT_A, CODE_CHAT_B, READY, browser, code_chat, code_free_machine, code_loaded,
                             code_online, code_requests, code_session, go, live, problems, set_value, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]
C = 'c3333333-3333-4333-8333-333333333333'
D = 'd4444444-4444-4444-8444-444444444444'
ASK = {'request_id': 'r1', 'kind': 'permission', 'tool': 'Bash', 'input': {'command': 'ls'}}
BADGE = "document.querySelector('#code-running-count')"
DOT = "document.querySelector('#code-inbox-dot')"
INBOX_OPEN = "document.querySelector('#code-inbox')?.open"


def theme(name):
    return ('eval', 'theme', f"document.documentElement.dataset.theme = '{name}'; true")


@pytest.mark.parametrize('width,shade', [(320, 'light'), (393, 'dark'), (1280, 'dark'), (1280, 'light')])
def test_badge_opens_inbox_and_pinned_question_answers_from_any_width(browser, live, width, shade, tmp_path):
    login = f'inbox-{width}-{shade}'
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session('Сессия в работе'),
                                 CODE_CHAT_B: code_session('Ждёт разрешения'),
                                 C: code_session('Готовый ответ', messages=[{'id': 'm1', 'role': 'assistant',
                                     'text': 'Готово', 'steps': [], 'time': T0}])})
    machine.project_activity = True
    machine.running = {CODE_CHAT_A: {'project': pid('alpha'), 'asks': {}, 'text': '', 'steps': [], 'rest': []},
                       CODE_CHAT_B: {'project': pid('alpha'), 'asks': {'r1': dict(ASK)}, 'text': '', 'steps': [], 'rest': []}}
    aid, server = code_online(live, login, machine)
    server.send_events(pid('alpha'), C, {'k': 'done', 'status': 'done'})
    sheet = """(() => {
      const box = document.querySelector('#code-inbox').getBoundingClientRect();
      const rows = [...document.querySelectorAll('#code-inbox .code-inbox-row')];
      return { inside: box.left >= 0 && box.right <= innerWidth + 0.5 && box.bottom <= innerHeight + 0.5 && box.top >= 0,
        noScroll: document.documentElement.scrollWidth <= innerWidth,
        touch: rows.every(r => r.getBoundingClientRect().height >= 44),
        bottom: innerWidth > 760 || Math.abs(box.bottom - innerHeight) < 1 };
    })()"""
    pin = """(() => { const p = document.querySelector('#code-ask-pin'), r = p.getBoundingClientRect();
      const form = document.querySelector('#code-composer').getBoundingClientRect();
      return { text: p.querySelector('.code-ask-pin-text').textContent, top: r.top >= 0, above: r.bottom <= form.top + 1,
        allow: !document.querySelector('#code-ask-pin-allow').hidden, deny: !document.querySelector('#code-ask-pin-deny').hidden,
        noScroll: document.documentElement.scrollWidth <= innerWidth }; })()"""
    try:
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), theme(shade),
            ('wait', f"!{BADGE}.hidden && {BADGE}.textContent === '2' && !{DOT}.hidden"),
            ('eval', 'label', "document.querySelector('#rail [data-section=code]').getAttribute('aria-label')"),
            ('click', '#code-running-count'), ('wait', INBOX_OPEN),
            ('wait', "document.querySelectorAll('#code-inbox .code-inbox-row').length === 3"
                     " && document.querySelector('#code-inbox .code-inbox-where').textContent.includes('alpha')"),
            ('eval', 'stayed', "!location.hash.includes('/code')"),
            ('eval', 'groups', "[...document.querySelectorAll('#code-inbox .code-inbox-group:not([hidden])')]"
                               ".map(s => [s.dataset.kind, [...s.querySelectorAll('.code-inbox-title')].map(n => n.textContent)])"),
            ('eval', 'where', "document.querySelector('#code-inbox .code-inbox-where').textContent"),
            ('eval', 'sheet', sheet), ('screenshot', str(tmp_path / f'inbox-{width}-{shade}.png')),
            ('press', 'Escape'), ('wait', f"!{INBOX_OPEN}"),
            # обычное нажатие на вкладку — по-прежнему переход в раздел
            ('click', '#rail [data-section=code] > span:last-child'),
            ('wait', "location.hash.startsWith('#/code') && !!document.querySelector('#code-panel')"),
            ('eval', 'noSheet', f"!{INBOX_OPEN}"),
            ('click', '#code-inbox-dot'), ('wait', INBOX_OPEN),
            ('wait', f"!!document.querySelector(\"#code-inbox a[data-chat='{CODE_CHAT_B}']\")"),
            ('click', f"#code-inbox a[data-chat='{CODE_CHAT_B}']"), code_loaded(CODE_CHAT_B),
            ('wait', "!document.querySelector('#code-ask-pin').hidden"),
            ('eval', 'pin', pin), ('screenshot', str(tmp_path / f'ask-pin-{width}-{shade}.png')),
            ('click', '#code-ask-pin-allow'), ('wait', "document.querySelector('#code-ask-pin').hidden")],
            width=width, mobile=width <= 393, touch=width <= 393)
    finally:
        server.stop()
    assert not result.get('failed'), result
    values = result['values']
    assert values['label'] == 'Агенты · Запущенных сессий: 2 · ждут ответа: 1 · не прочитано: 1'
    assert values['stayed'] and values['noSheet']
    assert values['groups'] == [['asking', ['Ждёт разрешения']], ['running', ['Сессия в работе']],
                                ['unread', ['Готовый ответ']]]
    assert 'Рабочий ПК' in values['where'] and 'alpha' in values['where']
    assert all(values['sheet'].values()), values['sheet']
    assert values['pin']['text'] == 'Claude Code просит разрешения: Bash'
    assert values['pin']['allow'] and values['pin']['deny'] and values['pin']['top'] and values['pin']['above']
    assert values['pin']['noScroll']
    assert [(r['request_id'], r['decision']) for r in code_requests(machine, 'answer')] == [('r1', 'allow')]
    assert not problems(result), problems(result)


def press_keys(code, key, target='document', **mods):
    flags = ', '.join(f'{name}: true' for name in mods)
    return ('eval', 'keys', f"{target}.dispatchEvent(new KeyboardEvent('keydown', {{code: '{code}', key: '{key}',"
                            f" bubbles: true, cancelable: true, {flags}}})); true")


TABS = "[...document.querySelectorAll('#code-tabbar .code-tab')].map(n => n.dataset.chat)"


def workspace_tabs(aid):
    return ('eval', 'stored', "import('/static/js/api.js').then(({api}) => api('GET', '/api/code/workspace'))"
                              f".then(d => d.projects.find(p => p.aid === {aid} && p.pid === '{pid('alpha')}').tabs)")


DRAG = """((from, to) => {
  const tab = (chat) => document.querySelector(`#code-tabbar .code-tab[data-chat="${chat}"]`);
  const a = tab(from).getBoundingClientRect(), b = tab(to).getBoundingClientRect();
  const fire = (node, type, x) => node.dispatchEvent(new PointerEvent(type, { bubbles: true, cancelable: true,
    pointerId: 7, pointerType: 'mouse', button: 0, buttons: 1, isPrimary: true, clientX: x, clientY: a.top + a.height / 2 }));
  fire(tab(from), 'pointerdown', a.left + 5);
  fire(tab(from), 'pointermove', a.left + 20);
  fire(tab(from), 'pointermove', b.right - 2);
  fire(tab(from), 'pointerup', b.right - 2);
  tab(from).click();     // отпускание после перетаскивания — не выбор вкладки
  return true;
})"""


@pytest.mark.parametrize('width', [320, 1280])
def test_tab_marks_order_shortcuts_project_search_and_read_marks(browser, live, width, tmp_path):
    login = f'tab-marks-{width}'
    live.add_user(login)
    done = [{'id': 'm1', 'role': 'assistant', 'text': 'Готово', 'steps': [], 'time': T0}]
    machine = code_free_machine({CODE_CHAT_A: code_session('Первый'), CODE_CHAT_B: code_session('Второй'),
                                 C: code_session('Третий'), D: code_session('Непрочитанный', messages=done)})
    for name in ('gamma', 'delta'):
        machine.projects[name] = {'chats': {}}
    machine.project_activity = True
    aid, server = code_online(live, login, machine)
    server.send_events(pid('alpha'), D, {'k': 'done', 'status': 'done'})
    chat = lambda c: [*go(f'code/{aid}/{pid("alpha")}/{c}'), code_loaded(c)]
    panel = [] if width > 760 else [('click', '#code-panel-toggle'), ('wait', "document.querySelector('.code-ws').classList.contains('panel-open')")]
    shown = "document.querySelector('#code-chat')?.dataset.chat"
    visible_projects = "[...document.querySelectorAll('#code-projects > li[data-pid]:not([hidden]) .code-project-name')].map(n => n.textContent)"
    try:
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), *code_chat(aid, CODE_CHAT_A),
            *chat(CODE_CHAT_B), *chat(C),
            set_value('#code-text', 'Неотправленное'),
            ('wait', f"!!document.querySelector(\"#code-tabbar .code-tab[data-chat='{C}'] .code-tab-draft\")"),
            ('eval', 'order0', TABS),
            # Shift+← у вкладки в фокусе — на место левее; порядок уходит в рабочее место
            ('eval', 'focusTab', f"document.querySelector(\"#code-tabbar .code-tab[data-chat='{C}']\").focus(); true"),
            press_keys('ArrowLeft', 'ArrowLeft', 'document.activeElement', shiftKey=True),
            ('wait', f"{TABS}.join() === '{CODE_CHAT_A},{C},{CODE_CHAT_B}'"),
            ('eval', 'focusKept', f"document.activeElement?.dataset.chat === '{C}'"),
            ('idle',), workspace_tabs(aid),
            # перетаскивание мышью: A — за B
            ('eval', 'drag', f"{DRAG}('{CODE_CHAT_A}', '{CODE_CHAT_B}')"),
            ('wait', f"{TABS}.join() === '{C},{CODE_CHAT_B},{CODE_CHAT_A}'"),
            ('eval', 'stillShown', shown), ('idle',),
            ('eval', 'stored2', "import('/static/js/api.js').then(({api}) => api('GET', '/api/code/workspace'))"
                                f".then(d => d.projects.find(p => p.aid === {aid}).tabs)"),
            # Ctrl+Alt+→ — соседняя вкладка (в русской раскладке key другой, code тот же), из поля задания тоже
            press_keys('ArrowRight', 'ArrowRight', "document.querySelector('#code-text')", ctrlKey=True, altKey=True),
            code_loaded(CODE_CHAT_B),
            ('eval', 'draftAway', f"!!document.querySelector(\"#code-tabbar .code-tab[data-chat='{C}'] .code-tab-draft\")"
                                  f" && !document.querySelector(\"#code-tabbar .code-tab[data-chat='{CODE_CHAT_B}'] .code-tab-draft\")"),
            press_keys('KeyK', 'л', ctrlKey=True),
            ('wait', "document.activeElement?.id === 'code-chat-search'"),
            ('screenshot', str(tmp_path / f'tabs-{width}.png')),
            *panel,
            ('wait', "!!document.querySelector('#code-project-search')?.offsetParent"),
            set_value('#code-project-search', 'GAM'), ('eval', 'found', visible_projects),
            set_value('#code-project-search', 'нет такого'), ('eval', 'none', "!!document.querySelector('#code-project-none')"),
            set_value('#code-project-search', ''), ('eval', 'all', f"{visible_projects}.length"),
            ('wait', f"!!document.querySelector(\"#code-projects li[data-pid='{pid('alpha')}'] .code-project-read\")"),
            ('eval', 'readLabel', f"document.querySelector(\"#code-projects li[data-pid='{pid('alpha')}'] .code-project-read\").textContent"),
            ('screenshot', str(tmp_path / f'panel-{width}.png')),
            ('click', f"#code-projects li[data-pid='{pid('alpha')}'] .code-project-read"),
            ('wait', f"!document.querySelector(\"#code-projects li[data-pid='{pid('alpha')}'] .code-project-read\")"
                     f" && {DOT}.hidden"),
            ('eval', 'fits', "document.documentElement.scrollWidth <= innerWidth")],
            width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get('failed'), result
    values = result['values']
    assert values['order0'] == [CODE_CHAT_A, CODE_CHAT_B, C]
    assert values['focusKept'] and values['stored'] == [CODE_CHAT_A, C, CODE_CHAT_B]
    assert values['stillShown'] == C and values['stored2'] == [C, CODE_CHAT_B, CODE_CHAT_A]
    assert values['draftAway'] and values['fits']
    assert values['found'] == ['gamma'] and values['none'] and values['all'] == 3
    assert values['readLabel'] == 'Отметить прочитанными · 1'
    assert not problems(result), problems(result)


@pytest.mark.parametrize('size', [(320, 740), (393, 852)], ids=['320', '393'])
def test_webkit_taps_open_inbox_answer_pinned_question_and_long_press_reorders_tabs(live, size, tmp_path):
    """Движок Safari: касание числа «Агентов», строки «Входящих» и «Разрешить» плашки; удержание вкладки пальцем
    переставляет её (синтетические PointerEvent touch: настоящего перетаскивания пальцем Playwright WebKit не умеет)."""
    import httpx
    pw = pytest.importorskip('playwright.sync_api')
    width, height = size
    login = f'inbox-webkit-{width}'
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session('Сессия в работе'), CODE_CHAT_B: code_session('Ждёт разрешения'),
                                 C: code_session('Готовый ответ')})
    machine.project_activity = True
    machine.running = {CODE_CHAT_A: {'project': pid('alpha'), 'asks': {}, 'text': '', 'steps': [], 'rest': []},
                       CODE_CHAT_B: {'project': pid('alpha'), 'asks': {'r1': dict(ASK)}, 'text': '', 'steps': [], 'rest': []}}
    aid, server = code_online(live, login, machine)
    server.send_events(pid('alpha'), C, {'k': 'done', 'status': 'done'})
    long_press = """async ([from, to]) => {
      const tab = (chat) => document.querySelector(`#code-tabbar .code-tab[data-chat="${chat}"]`);
      const a = tab(from).getBoundingClientRect(), b = tab(to).getBoundingClientRect();
      const fire = (type, x) => tab(from).dispatchEvent(new PointerEvent(type, { bubbles: true, cancelable: true,
        pointerId: 9, pointerType: 'touch', isPrimary: true, button: 0, buttons: 1, clientX: x, clientY: a.top + a.height / 2 }));
      fire('pointerdown', a.left + 10);
      await new Promise(resolve => setTimeout(resolve, 600));
      fire('pointermove', b.right - 2);
      fire('pointerup', b.right - 2);
      return [...document.querySelectorAll('#code-tabbar .code-tab')].map(n => n.dataset.chat);
    }"""
    try:
        with pw.sync_playwright() as driver:
            from pathlib import Path
            if not Path(driver.webkit.executable_path).is_file():
                pytest.skip('нет WebKit: .venv/Scripts/python -m playwright install webkit')
            webkit = driver.webkit.launch()
            signed = httpx.post(live.url + '/login', data={'login': login, 'password': 'password-123'})
            cookie = '; '.join(f'{key}={value}' for key, value in signed.cookies.items())
            context = webkit.new_context(viewport={'width': width, 'height': height}, is_mobile=True, has_touch=True,
                                         device_scale_factor=3, extra_http_headers={'Cookie': cookie})
            page = context.new_page()
            page.set_default_timeout(15000)
            page.goto(live.url + '/#/chat')
            badge = page.locator('#code-running-count')
            pw.expect(badge).to_have_text('2')
            for shade in ('light', 'dark'):
                page.evaluate('shade => { document.documentElement.dataset.theme = shade; }', shade)
                badge.tap()
                pw.expect(page.locator('#code-inbox .code-inbox-row')).to_have_count(3)
                geometry = page.evaluate("""() => { const r = document.querySelector('#code-inbox').getBoundingClientRect();
                  return r.left >= 0 && r.right <= innerWidth + 0.5 && Math.abs(r.bottom - innerHeight) < 1
                    && document.documentElement.scrollWidth <= innerWidth; }""")
                assert geometry, shade
                assert '/code' not in page.evaluate('location.hash')
                page.screenshot(path=str(tmp_path / f'webkit-inbox-{width}-{shade}.png'))
                page.locator('#code-inbox-close').tap()
                pw.expect(page.locator('#code-inbox')).not_to_be_visible()
            page.locator('#code-inbox-dot').tap()
            page.locator(f"#code-inbox a[data-chat='{CODE_CHAT_B}']").tap()
            pw.expect(page.locator('#code-chat')).to_have_attribute('data-loaded', '1')
            pw.expect(page.locator('#code-ask-pin')).to_be_visible()
            page.screenshot(path=str(tmp_path / f'webkit-pin-{width}.png'))
            page.locator('#code-ask-pin-allow').tap()
            pw.expect(page.locator('#code-ask-pin')).to_be_hidden()
            assert [r['decision'] for r in code_requests(machine, 'answer')] == ['allow']
            page.evaluate(f"location.hash = '#/code/{aid}/{pid('alpha')}/{CODE_CHAT_A}'")
            pw.expect(page.locator('#code-tabbar .code-tab')).to_have_count(2)
            order = page.evaluate(long_press, [CODE_CHAT_B, CODE_CHAT_A])
            assert order == [CODE_CHAT_A, CODE_CHAT_B]
            pw.expect(page.locator('#code-chat')).to_have_attribute('data-chat', CODE_CHAT_A)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            context.close()
            webkit.close()
    finally:
        server.stop()


@pytest.mark.parametrize('width', [320, 1280])
def test_offline_computer_keeps_tabs_with_a_mark_and_says_when_it_was_seen(browser, live, width, tmp_path):
    login = f'tab-offline-{width}'
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session('Первый'), CODE_CHAT_B: code_session('Второй')})
    aid, server = code_online(live, login, machine)
    server.stop()
    opened = ('eval', 'open', "import('/static/js/api.js').then(async ({api}) => {"
              + ''.join(f"await api('PATCH', '/api/code/workspace', {{op: 'open', aid: {aid}, pid: '{pid('alpha')}', chat: '{c}'}});"
                        for c in (CODE_CHAT_A, CODE_CHAT_B)) + 'return true; })')
    panel = [] if width > 760 else [('click', '#code-panel-toggle'),
                                    ('wait', "document.querySelector('.code-ws').classList.contains('panel-open')")]
    result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), opened,
        *go(f'code/{aid}/{pid("alpha")}/{CODE_CHAT_A}'),
        ('wait', "document.querySelectorAll('#code-tabbar .code-tab-offline').length === 2"),
        ('eval', 'status', "document.querySelector('#code-status').textContent"),
        ('eval', 'hidden', "document.querySelector('.code-tabs-host').hidden"),
        *panel,
        ('wait', "!!document.querySelector('.code-agent-seen')"),
        ('eval', 'seen', "document.querySelector('.code-agent-seen').textContent"),
        ('screenshot', str(tmp_path / f'offline-{width}.png')),
        ('eval', 'fits', 'document.documentElement.scrollWidth <= innerWidth')], width=width, mobile=width == 320)
    assert not result.get('failed'), result
    values = result['values']
    assert values['status'] == 'Компьютер не на связи — включите его и запустите мост' and not values['hidden']
    assert values['seen'].startswith('был на связи ') and values['fits']
    assert not problems(result), problems(result)
