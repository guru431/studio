"""Очередь восстанавливается после gone; проба не повторяет постороннюю ошибку max_tokens."""
import json

import pytest

from fake_bridge import FakeMachine, Refused
from test_ui_browser import (API, READY, CODE_CHAT_A, browser, live, code_chat, code_online,
                             code_send, code_session, go, is_open, set_value, signin)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(90)]


@pytest.mark.parametrize('resume_fails', [False, True])
def test_gone_queue_item_closes_editor_and_leaves_resume_available(browser, live, resume_fails):
    class Machine(FakeMachine):
        failed_resume = False
        def _op_queue_edit(self, req, events):
            self.running[req['chat']]['followups'] = []
            raise Refused('gone')
        def _op_queue_pause(self, req, events):
            if not req['paused'] and resume_fails and not self.failed_resume:
                self.failed_resume = True
                raise Refused('busy')
            return super()._op_queue_pause(req, events)

    login = f'gone-queue-{int(resume_fails)}'
    live.add_user(login)
    machine = Machine(projects={'alpha': {'chats': {CODE_CHAT_A: code_session('Очередь')}}})
    machine.queue_edit = True
    machine.script = [{'k': 'text', 'text': 'Работаю'}]
    aid, bridge = code_online(live, login, machine)
    try:
        result = browser.drive(live.url + '/', [*signin(login), ('wait', READY), *code_chat(aid, CODE_CHAT_A),
            *code_send('Задание'), ('wait', "document.querySelector('#code-chat').dataset.running === '1'"),
            set_value('#code-delivery', 'queue'), *code_send('В очереди'),
            ('wait', "!!document.querySelector('.code-queue-preview')"),
            ('click', "button[aria-label='Изменить ожидающее задание']"),
            ('wait', "!!document.querySelector('.code-queue-text')"),
            ('click', '.code-queue-actions button'),
            ('wait', "!document.querySelector('.code-queue-text')"),
            *([('wait', "!document.querySelector('#code-queue').hidden && !!document.querySelector('#code-queue > button')"),
               ('click', '#code-queue > button')] if resume_fails else []),
            ('wait', "document.querySelector('#code-queue').hidden"),
            ('eval', 'resumed', "!document.querySelector('#code-send').disabled"),
            ('click', '#code-stop')], width=320, mobile=True, timeout=5000)
    finally:
        bridge.stop()
    assert not result.get('failed'), result
    assert result['values']['resumed']
    assert not machine.running or not next(iter(machine.running.values())).get('queue_paused')


@pytest.mark.parametrize('replacement', [False, True])
def test_probe_retries_only_an_explicit_parameter_replacement(browser, live, replacement):
    login = f'probe-parameter-{int(replacement)}'
    live.add_user(login)
    error = {'message': 'max_tokens must be at least 1', 'code': 'invalid_parameter', 'param': 'max_tokens'}
    if replacement:
        error = {'message': 'Unsupported parameter', 'code': 'unsupported_parameter', 'param': 'max_tokens'}
    intercept = f"""(() => {{
      window.probeBodies = []; const original = window.fetch;
      window.fetch = (url, options) => {{
        if (String(url) === {json.dumps(live.cloud.base_url + '/chat/completions')} && options?.method === 'POST') {{
          probeBodies.push(JSON.parse(options.body));
          if (probeBodies.length === 1) return Promise.resolve(new Response(JSON.stringify({{error: {json.dumps(error)}}}), {{status: 400}}));
        }}
        return original(url, options);
      }};
    }})()"""
    result = browser.drive(live.url + '/', [*signin(login), ('wait', READY),
        ('eval', 'added', f"""(async () => {{ const m = {API};
          const c = await m.api('POST', '/api/connections', {{name:'Проба',kind:'openai',base_url:{live.cloud.base_url!r},token:{live.cloud.TOKEN!r},sections:['chat']}});
          await m.api('PUT', `/api/sections/chat/links/${{c.id}}`, {{models:['cloud-chat']}}); await m.loadMe(); }})()"""),
        *go('settings/chat'), ('wait', "!!document.querySelector('.model-probe')"),
        ('click', '.model-probe'), ('wait', is_open('#probe-dialog')), ('click', '#probe-send'),
        ('wait', f"!{is_open('#probe-dialog')} && !!document.querySelector('.passport-probe')"),
        ('eval', 'requests', 'window.probeBodies')], init=intercept, width=1280, timeout=5000)
    assert not result.get('failed'), result
    requests = result['values']['requests']
    assert len(requests) == (2 if replacement else 1)
    assert 'max_tokens' in requests[0]
    if replacement:
        assert 'max_completion_tokens' in requests[1] and 'max_tokens' not in requests[1]
