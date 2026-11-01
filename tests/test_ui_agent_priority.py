"""Приоритет помощников, компактное создание сессий и переключатель поиска."""
import pytest

from fake_bridge import pid
from test_codex import CATALOG as CODEX
from test_ui_opencode import CATALOG as OPENCODE
from test_ui_browser import (CODE_CHAT_A, CODE_CHAT_B, READY, browser, code_free_machine, code_online, code_requests,
                             code_session, go, live, live_sql, problems, set_value, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize('width', [320, 1280])
def test_priority_saved_in_settings_orders_starters_and_selects_git_agent(browser, live, width, tmp_path):
    login = f'agent-priority-{width}'
    live.add_user(login)
    machine = code_free_machine()
    machine.codex, machine.opencode = CODEX, OPENCODE
    machine.script = [{'k': 'text', 'text': 'Готово'}, {'k': 'done', 'status': 'done'}]
    aid, server = code_online(live, login, machine)
    try:
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), *go('settings/code'),
            ('wait', "document.querySelector('#code-engine-order-save')"),
            ('click', "#code-engine-order [data-engine=codex] .code-engine-up"),
            ('click', '#code-engine-order-save'),
            ('wait', "document.querySelector('#code-engine-order-status').textContent.includes('сохранён')"),
            ('eval', 'reload', 'location.reload()'), ('wait', READY),
            ('wait', "document.querySelector('#code-engine-order li')?.dataset.engine === 'codex'"),
            *go('code'), ('wait', "document.querySelector('#code-projects li[data-pid]')"),
            *go(f"code/{aid}/{pid('alpha')}"), ('wait', "document.querySelector('#code-new-opencode')"),
            ('eval', 'order', "[...document.querySelectorAll('.code-new-chat')].map(b => b.id)"),
            ('eval', 'geometry', """(() => {
              const row = document.querySelector('.code-chat-starters'), r = row.getBoundingClientRect();
              const buttons = [...row.children].map(b => b.getBoundingClientRect());
              return buttons.every(b => Math.abs(b.top-buttons[0].top)<1 && b.left>=r.left && b.right<=r.right+1)
                && row.scrollWidth<=row.clientWidth && document.documentElement.scrollWidth<=innerWidth;
            })()"""), ('screenshot', str(tmp_path / f'new-sessions-{width}.png')),
            ('click', '#code-tab-git'), ('wait', "!document.querySelector('#code-git-commit')?.disabled"),
            ('click', '#code-git-commit'),
            ('wait', "document.querySelector('#code-chat')?.dataset.running === '0' && document.querySelector('#code-feed .code-msg.user')"),
            ('eval', 'engine', "document.querySelector('#code-chat').dataset.engine")], width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get('failed'), result
    assert result['values']['order'] == ['code-new-codex', 'code-new-chat', 'code-new-opencode']
    assert result['values']['geometry'] and result['values']['engine'] == 'codex'
    [turn] = code_requests(machine, 'turn')
    assert turn['engine'] == 'codex' and turn['project'] == pid('alpha')
    assert not problems(result), problems(result)


@pytest.mark.parametrize('available, expected', [('opencode', 'opencode'), (None, 'claude')])
def test_auto_chat_skips_agents_missing_from_the_computer(browser, live, available, expected):
    login = f'priority-fallback-{expected}'
    live.add_user(login)
    live_sql(live, 'UPDATE users SET code_defaults = ? WHERE login = ?',
             ('{"codex":{"priority":1},"opencode":{"priority":2},"claude":{"priority":3}}', login))
    machine = code_free_machine()
    if available:
        machine.opencode = OPENCODE
    machine.script = [{'k': 'text', 'text': 'Готово'}, {'k': 'done', 'status': 'done'}]
    aid, server = code_online(live, login, machine)
    try:
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), *go('code'),
            ('wait', "document.querySelector('#code-projects li[data-pid]')"),
            *go(f"code/{aid}/{pid('alpha')}?tab=git"),
            ('wait', "document.querySelector('#code-git-commit') && !document.querySelector('#code-git-commit').disabled"),
            ('click', '#code-git-commit'),
            ('wait', "document.querySelector('#code-chat')?.dataset.running === '0' && document.querySelector('#code-feed .code-msg.user')"),
            ('eval', 'engine', "document.querySelector('#code-chat').dataset.engine")])
    finally:
        server.stop()
    assert not result.get('failed'), result
    assert result['values']['engine'] == expected
    [turn] = code_requests(machine, 'turn')
    assert turn.get('engine', 'claude') == expected
    assert not problems(result), problems(result)


@pytest.mark.parametrize('width', [320, 1280])
def test_search_switch_is_right_of_input_and_defaults_to_titles(browser, live, width, tmp_path):
    login = f'session-search-mode-{width}'
    live.add_user(login)
    machine = code_free_machine({CODE_CHAT_A: code_session('Заголовок Needle'),
        CODE_CHAT_B: code_session('Другая сессия', messages=[
            {'id': 'one', 'role': 'user', 'text': 'Содержимое Needle', 'steps': [], 'time': 1}])})
    machine.chat_search = True
    aid, server = code_online(live, login, machine)
    rows = "[...document.querySelectorAll('#code-chats li[data-chat]')].map(e => e.dataset.chat)"
    geometry = """(() => {
      const input = document.querySelector('#code-chat-search').getBoundingClientRect();
      const mode = document.querySelector('#code-search-content').getBoundingClientRect();
      return mode.left>=input.right && Math.abs(mode.top+mode.height/2-input.top-input.height/2)<1
        && input.width>=60 && mode.right<=innerWidth && document.documentElement.scrollWidth<=innerWidth;
    })()"""
    try:
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), *go('code'),
            ('wait', "document.querySelector('#code-projects li[data-pid]')"), *go(f"code/{aid}/{pid('alpha')}"),
            ('wait', "document.querySelectorAll('#code-chats li[data-chat]').length === 2"),
            ('eval', 'default', "document.querySelector('#code-search-content').getAttribute('aria-checked') === 'false' && document.querySelector('#code-search-content').textContent === 'По заголовкам'"),
            set_value('#code-chat-search', 'Needle'),
            ('eval', 'titles', rows), ('eval', 'geometry', geometry),
            ('click', '#code-search-content'), ('wait', "document.querySelector('.code-search-snippet')"),
            ('eval', 'contents', rows), ('eval', 'mode', "document.querySelector('#code-search-content').textContent"),
            ('eval', 'contentGeometry', geometry), ('screenshot', str(tmp_path / f'session-search-{width}.png')),
            ('click', '#code-search-content'), ('eval', 'titlesAgain', rows),
            *go('photo'), *go(f"code/{aid}/{pid('alpha')}?tab=new"),
            ('wait', "document.querySelector('#code-search-content')"),
            ('eval', 'reset', "document.querySelector('#code-search-content').getAttribute('aria-checked') === 'false'")],
            width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get('failed'), result
    values = result['values']
    assert all(values[key] for key in ('default', 'geometry', 'contentGeometry', 'reset'))
    assert values['titles'] == values['titlesAgain'] == [CODE_CHAT_A]
    assert set(values['contents']) == {CODE_CHAT_A, CODE_CHAT_B} and values['mode'] == 'По содержимому'
    assert len(code_requests(machine, 'chat_search')) == 1
    assert not any('Needle' in url for url in result.get('sent', []))
    assert not problems(result), problems(result)
