"""Сессии в «В работе» и проектах: три состояния и просмотр только показанного результата."""
import pytest

from fake_bridge import T0, pid
from test_ui_browser import (CODE_CHAT_A, CODE_CHAT_B, READY, browser, code_free_machine, code_online,
                             code_session, go, live, problems, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]
C = 'c3333333-3333-4333-8333-333333333333'


@pytest.mark.parametrize('width', [320, 1280])
def test_agent_sessions_in_jobs_and_project_panel_remain_unread_until_opened(browser, live, width, tmp_path):
    login = f'attention-{width}'
    live.add_user(login)
    title = 'Готовый ответ с длинным названием сессии для проверки переноса строк на узком экране'
    machine = code_free_machine({CODE_CHAT_A: code_session('Сессия в работе'),
                                 CODE_CHAT_B: code_session('Сессия ждёт разрешения'),
                                 C: code_session(title, messages=[{'id': 'm1', 'role': 'assistant',
                                     'text': 'Готовый результат', 'steps': [], 'time': T0}])})
    machine.project_activity = True
    machine.running = {CODE_CHAT_A: {'project': pid('alpha'), 'asks': {}, 'text': '', 'steps': []},
                       CODE_CHAT_B: {'project': pid('alpha'), 'asks': {'r': {}}, 'text': '', 'steps': []}}
    aid, server = code_online(live, login, machine)
    server.send_events(pid('alpha'), C, {'k': 'done', 'status': 'done'})
    try:
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY),
            ('wait', "document.querySelector('#jobs').dataset.running === '2' && !document.querySelector('#jobs .jobs-dot').hidden"),
            ('click', '#jobs'), ('wait', "document.querySelectorAll('#jobs-panel .jobs-row[data-section=code]').length === 3"),
            ('eval', 'titles', "[...document.querySelectorAll('#jobs-panel [data-section=code] .jobs-title')].map(n=>n.textContent)"),
            ('eval', 'states', "[...document.querySelectorAll('#jobs-panel [data-section=code]')].map(n=>n.dataset.state).sort()"),
            ('eval', 'fits', "document.documentElement.scrollWidth <= innerWidth && document.querySelector('#jobs-panel').getBoundingClientRect().right <= innerWidth"),
            ('screenshot', str(tmp_path / f'attention-jobs-{width}.png')),
            ('click', '#jobs-close'), *go(f'code/{aid}'),
            ('wait', "document.querySelectorAll('.code-project-sessions a').length === 3"),
            ('eval', 'unreadLink', f"document.querySelector('.code-project-sessions a[data-chat=\"{C}\"]').textContent"),
            ('screenshot', str(tmp_path / f'attention-projects-{width}.png')),
            ('eval', 'closeProjects', "document.dispatchEvent(new KeyboardEvent('keydown', {key:'Escape', bubbles:true})); true"),
            ('click', '#jobs'), ('wait', "!document.querySelector('#jobs-code-unread').hidden"),
            ('eval', 'stillUnread', "!document.querySelector('#jobs .jobs-dot').hidden"),
            ('click', '#jobs-code-unread .jobs-open'),
            ('wait', f"document.querySelector('#code-chat')?.dataset.chat === '{C}' && document.querySelector('#code-feed').textContent.includes('Готовый результат')"),
            ('wait', "!document.querySelector('.code-project-sessions a[data-state=unread]')"),
            ('wait', "document.querySelector('#jobs .jobs-dot').hidden"),
            ('click', '#jobs'), ('wait', "document.querySelector('#jobs-code-unread').hidden"),
            ('eval', 'remaining', "document.querySelectorAll('#jobs-panel .jobs-row[data-section=code]').length"),
            ('eval', 'noStoredText', "![...Object.values(localStorage)].some(v=>v.includes('Готовый результат'))")],
            width=width, mobile=width == 320)
    finally:
        server.stop()
    assert not result.get('failed'), result
    assert set(result['values']['titles']) == {'Сессия в работе', 'Сессия ждёт разрешения', title}
    assert result['values']['states'] == ['asking', 'running', 'unread']
    assert title in result['values']['unreadLink']
    assert result['values']['fits'] and result['values']['stillUnread'] and result['values']['noStoredText']
    assert result['values']['remaining'] == 2
    assert not problems(result), problems(result)


def test_hidden_chat_and_unseen_end_of_history_do_not_mark_result_read(browser, live):
    login = 'attention-hidden'
    live.add_user(login)
    messages = [{'id': f'm{i}', 'role': 'assistant', 'text': 'Предыдущий ответ.\n\n' * 30,
                 'steps': [], 'time': T0 + i} for i in range(20)]
    machine = code_free_machine({CODE_CHAT_A: code_session('Длинная история', messages=messages)})
    machine.project_activity = True
    machine.script = [{'k': 'text', 'text': 'Новый ответ'}]
    aid, server = code_online(live, login, machine)
    endpoint = f'/api/code/agents/{aid}/projects/{pid("alpha")}/chats/{CODE_CHAT_A}'
    hidden = """Object.defineProperty(document, 'visibilityState', {configurable:true, value:'hidden'});
      document.dispatchEvent(new Event('visibilitychange')); true"""
    visible = """Object.defineProperty(document, 'visibilityState', {configurable:true, value:'visible'});
      document.dispatchEvent(new Event('visibilitychange')); true"""
    try:
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY),
            *go(f'code/{aid}/{pid("alpha")}/{CODE_CHAT_A}'),
            ('wait', "document.querySelector('#code-chat')?.dataset.loaded === '1'"),
            ('eval', 'task', "document.querySelector('#code-text').value='Задача'; document.querySelector('#code-text').dispatchEvent(new Event('input', {bubbles:true})); true"),
            ('click', '#code-send'), ('wait', "document.querySelector('#code-feed').textContent.includes('Новый ответ')"),
            ('eval', 'scrollUp', "document.querySelector('#view').scrollTop=0; true"),
            ('eval', 'hidden', hidden),
            ('eval', 'stop', f"import('/static/js/api.js').then(m=>m.api('POST','{endpoint}/stop'))"),
            ('eval', 'unreadWhileHidden', "fetch('/api/code/activity').then(r=>r.json()).then(r=>r.items[0].items[0].unread)"),
            ('eval', 'visible', visible),
            ('wait', "!!document.querySelector('.code-project-sessions a[data-state=unread]')"),
            ('wait', "document.querySelector('#code-stop').hidden"),
            ('eval', 'keptPosition', "document.querySelector('#view').scrollTop < 10"),
            ('eval', 'bottom', "document.querySelector('#view').scrollTop=document.querySelector('#view').scrollHeight; true"),
            ('wait', "!document.querySelector('.code-project-sessions a[data-state=unread]')"),
            ('eval', 'read', "fetch('/api/code/activity').then(r=>r.json()).then(r=>r.items[0].items.length===0)")])
    finally:
        server.stop()
    assert not result.get('failed'), result
    assert all(result['values'][key] for key in ('unreadWhileHidden', 'keptPosition', 'read'))
    assert not problems(result), problems(result)
