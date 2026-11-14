"""Настройки OpenCode, статусы и ветвление через браузер, сервер и WebSocket моста."""

import pytest

from fake_bridge import FakeMachine
from test_ui_browser import (
    CODE_PIN, NO_OVERFLOW, READY, browser, live, code_online, code_open, code_project,
    code_requests, code_send, problems, known_refusals, set_value, signin,
)
from test_ui_opencode import CATALOG

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]


@pytest.mark.parametrize('width', [320, 1280])
def test_opencode_mode_directories_commands_progress_and_fork(browser, live, width):
    class Machine(FakeMachine):
        def _op_state(self, req, events):
            data = super()._op_state(req, events)
            data['settings'].update({'agent_mode': req.get('agent_mode') or 'build',
                'modes': [{'id': 'build', 'label': 'Build'}, {'id': 'plan', 'label': 'Plan'}],
                'commands': [{'name': 'compact', 'description': 'Сжать контекст'}],
                'additional_directories': req.get('additional_directories') or []})
            return data

    login = f'ui-opencode-features-{width}'
    live.add_user(login)
    machine = Machine(projects={'alpha': {}, 'beta': {}}, pin=CODE_PIN)
    machine.opencode = CATALOG
    machine.opencode_features = machine.chat_fork = machine.subagents = True
    machine.context = {'used': 10, 'limit': 100, 'percent': 10, 'usage': {'total': 100, 'input': 80, 'output': 20},
                       'cost': {'amount': 0.0123, 'currency': 'USD'}}
    machine.script = [
        {'k': 'progress', 'opencode': {'retry': {'attempt': 2, 'next': '2026-11-14T12:00:00Z', 'status': 429},
                                     'compaction': 'started'}},
        {'k': 'ask', 'kind': 'permission', 'request_id': 'p1', 'tool': 'Shell', 'input': {}, 'always': True},
        {'k': 'progress', 'opencode': {'retry': None, 'compaction': 'completed'}},
        {'k': 'text', 'text': 'Готово'}, {'k': 'done', 'status': 'done'}]
    aid, sb = code_online(live, login, machine)
    try:
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), *code_open(aid), *code_project(aid, 'alpha'),
            ('click', '#code-new-opencode'), ('wait', "document.querySelector('#code-chat')?.dataset.loaded === '1'"),
            ('click', '#code-opencode-options summary'),
            ('wait', "document.querySelector('#code-agent-mode')?.value === 'build'"),
            set_value('#code-agent-mode', 'plan'),
            ('wait', "!document.querySelector('#code-agent-mode').disabled"),
            set_value('#code-additional-directories', 'C:\\Shared'),
            ('wait', "!document.querySelector('#code-agent-mode').disabled"),
            set_value('#code-slash-command', 'compact'),
            ('eval', 'command', "document.querySelector('#code-text').value"),
            ('eval', 'buttons', "[!document.querySelector('#code-subagents').disabled, !!document.querySelector('#code-fork-chat')]"),
            ('click', '#code-context'),
            ('eval', 'cost', "document.querySelector('#code-context-dialog').textContent"),
            ('eval', 'closeContext', "document.querySelector('#code-context-dialog').close()"),
            *code_send('Проверь проект'), ('wait', "!!document.querySelector('.code-ask-yes')"),
            ('eval', 'waiting', "document.querySelector('#code-opencode-status').textContent"),
            ('click', '.code-ask-yes'),
            ('wait', "document.querySelector('#code-stop').hidden && document.querySelector('#code-feed').textContent.includes('Готово')"),
            ('eval', 'completed', "document.querySelector('#code-opencode-status').textContent"),
            ('eval', 'fits', NO_OVERFLOW),
            ('eval', 'source', 'location.hash'),
            *([('click', '#code-status-more')] if width == 320 else []), ('click', '#code-fork-chat'),
            ('wait', "document.querySelector('#code-chat')?.dataset.loaded === '1' && !document.querySelector('#code-fork-chat').disabled"),
            ('eval', 'branch', 'location.hash')], width=width, mobile=width == 320, timeout=5000)
    finally:
        sb.stop()
    assert not result.get('failed'), result
    values = result['values']
    assert values['command'] == '/compact ' and values['buttons'] == [True, True]
    assert 'Стоимость сессии' in values['cost'] and 'Основной агент' in values['cost']
    assert 'Повторная попытка 2' in values['waiting'] and 'HTTP 429' in values['waiting'] and 'Сжимаю' in values['waiting']
    assert values['completed'] == 'Контекст сжат' and values['fits']
    assert values['source'] != values['branch']
    turn = code_requests(machine, 'turn')[-1]
    assert turn['agent_mode'] == 'plan' and turn['additional_directories'] == ['C:\\Shared']
    assert not known_refusals(problems(result), 409), problems(result)
