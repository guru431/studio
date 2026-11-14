"""Расширения ACP: дети не меняют ответ/контекст родителя, настройки и ветки независимы."""

import asyncio
from types import SimpleNamespace

import pytest

from studio_agent import opencode, opencode_events, opencode_native
from studio_agent.errors import Refused
from studio_agent.turns import _Turn
from test_opencode import CHAT, SESSION, FakeRpc, runner
from turn_events import asking
from test_agent import agent, root, req


def tagged(kind, sid='ses_child', parent=SESSION, depth=1, **extra):
    return {'sessionUpdate': kind, '_meta': {'opencode/child-session': {
        'id': sid, 'parentID': parent, 'depth': depth, 'title': 'Проверка'}}, **extra}


def test_child_text_usage_and_nested_events_cannot_contaminate_parent(runner):
    t = _Turn(runner.project, runner.pid, CHAT, 't')
    t.client = SimpleNamespace(session=SESSION)
    t.context = {'used': 20, 'limit': 100, 'percent': 20}
    updates = [tagged('agent_message_chunk', content={'type': 'text', 'text': 'child secret'}),
               tagged('usage_update', used=999, size=1000),
               tagged('agent_thought_chunk', sid='ses_grandchild', parent='ses_child', depth=2,
                      content={'type': 'text', 'text': 'child thought'})]
    for update in updates:
        runner._notification(t, update)
    assert not t.feed.text and not t.feed.steps
    assert t.context['used'] == 20
    assert set(t.opencode_children) == {'ses_child', 'ses_grandchild'}
    assert not any(frame['e']['k'] in ('text', 'context', 'result') for frame in runner.sent)
    rpc = SimpleNamespace(inbox=asyncio.Queue())
    for update in updates + [{'sessionUpdate': 'agent_message_chunk', 'content': {'type': 'text', 'text': 'parent'}}]:
        rpc.inbox.put_nowait({'method': 'session/update', 'params': {'sessionId': SESSION, 'update': update}})
    assert [row['text'] for row in opencode.normalize(opencode.OpenCodeRunner._updates(rpc, SESSION))] == ['parent']


@pytest.mark.parametrize('change', [{'parentID': 'ses_foreign'}, {'parentID': []}, {'depth': True},
                                   {'depth': 2}, {'id': SESSION}, {'id': '../secret'}, {'depth': 100}])
def test_unproven_child_metadata_fails_closed(change):
    update = tagged('agent_message_chunk')
    update['_meta']['opencode/child-session'].update(change)
    assert opencode_events.child(update, SESSION, {}) is False


async def test_child_permission_uses_new_marker_and_foreign_marker_is_rejected(runner, monkeypatch):
    original = FakeRpc.request

    async def request(self, method, params, **kwargs):
        if method == 'session/prompt':
            runner.fake.ask['params']['sessionId'] = 'ses_child'
            runner.fake.ask['params']['toolCall']['_meta'] = tagged('tool_call')['_meta']
        return await original(self, method, params, **kwargs)

    monkeypatch.setattr(FakeRpc, 'request', request)
    runner.start(runner.project, runner.pid, CHAT, 'Проверка', None, None)
    task = runner._turns[CHAT].task
    try:
        rid = await asking(runner)
        assert runner._turns[CHAT].asks[rid].event['subtask'] == 'Проверка'
        runner.answer(runner.pid, CHAT, rid, 'allow', None, None)
        await task
    finally:
        if not task.done():
            await runner.stop(runner.pid, CHAT)
            await task
    assert runner.fake.replies[0]['id'] == 91 and runner.fake.replies[0]['result']['outcome']['optionId'] == 'once'


async def test_mode_directory_commands_usage_and_cost_survive_completion(runner, monkeypatch):
    original = FakeRpc.request

    async def request(self, method, params, **kwargs):
        if method in ('session/new', 'session/set_config_option'):
            result = await original(self, method, params, **kwargs)
            mode = params.get('value') if params.get('configId') == 'mode' else 'build'
            result['configOptions'].append({'id': 'mode', 'currentValue': mode,
                'options': [{'value': v, 'name': v} for v in ('build', 'plan')]})
            self.update({'sessionUpdate': 'available_commands_update',
                         'availableCommands': [{'name': 'compact', 'description': 'Сжать'}]})
            return result
        if method == 'session/prompt':
            self.update({'sessionUpdate': 'usage_update', 'used': 10, 'size': 100,
                         'cost': {'amount': 0.0123, 'currency': 'USD'}})
            result = await original(self, method, params, **kwargs)
            return {**result, 'usage': {'inputTokens': 15, 'outputTokens': 5, 'totalTokens': 20, 'thoughtTokens': 2}}
        return await original(self, method, params, **kwargs)

    monkeypatch.setattr(FakeRpc, 'request', request)
    directories = [str(runner.project)]
    runner.start(runner.project, runner.pid, CHAT, 'Проверка', None, None, access='auto',
                 agent_mode='plan', additional_directories=directories)
    await runner._turns[CHAT].task
    saved = runner.choices.details(CHAT)
    assert saved['agent_mode'] == 'plan' and saved['additional_directories'] == directories
    assert saved['context']['usage'] == {'input': 15, 'output': 5, 'total': 20, 'reasoning': 2}
    assert saved['context']['cost'] == {'amount': 0.0123, 'currency': 'USD'}
    assert any(method == 'session/new' and p.get('additionalDirectories') == directories for method, p in runner.fake.calls)
    assert saved['commands'][0]['name'] == 'compact'


def test_retry_compaction_and_thoughts_are_bounded_and_do_not_expose_provider_body(runner):
    t = _Turn(runner.project, runner.pid, CHAT, 't')
    t.client = SimpleNamespace(session=SESSION)
    runner._notification(t, {'sessionUpdate': 'session_info_update', '_meta': {'opencode/retry': {
        'attempt': 2, 'nextRetryAt': '2026-11-14T12:00:00Z', 'error': {'status': 429, 'response': {'body': 'secret'}}}}})
    runner._notification(t, {'sessionUpdate': 'compaction_update', 'status': 'started'})
    runner._notification(t, {'sessionUpdate': 'agent_thought_chunk', 'content': {'type': 'text', 'text': 'reason'}})
    assert t.progress['opencode']['retry']['attempt'] == 2
    assert t.progress['opencode']['compaction'] == 'started'
    assert t.feed.steps[0]['text'] == 'reason' and t.feed.text == ''
    assert 'secret' not in str(runner.sent)
    runner._notification(t, {'sessionUpdate': 'session_info_update', '_meta': {'opencode/retry': None}})
    assert t.progress['opencode']['retry'] is None


async def test_fork_uses_owned_alias_and_keeps_source_mapping(runner, monkeypatch):
    runner.aliases.put(runner.project, CHAT, SESSION)
    original = FakeRpc.request

    async def request(self, method, params, **kwargs):
        if method == 'session/fork':
            assert params['sessionId'] == SESSION
            return {'sessionId': 'ses_branch'}
        return await original(self, method, params, **kwargs)

    monkeypatch.setattr(FakeRpc, 'request', request)
    child = await runner.fork_chat(runner.project, CHAT)
    assert child['chat'] != CHAT and runner.aliases.target(runner.project, CHAT) == SESSION
    assert runner.aliases.target(runner.project, child['chat']) == 'ses_branch'
    with pytest.raises(Refused, match='not_found'):
        await runner.fork_chat(runner.project / 'other', CHAT)


async def test_native_fork_checks_project_and_message_before_mutation(runner, monkeypatch):
    calls = []
    class Native:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def request(self, method, path, body=None):
            calls.append((method, path, body))
            if method == 'POST': return {'data': {'id': 'ses_new'}}
            if '/message/' in path: return {'data': {'id': 'msg_test', 'type': 'user'}}
            return {'data': {'location': {'directory': str(runner.project)}}}
    monkeypatch.setattr(opencode_native, 'Client', lambda *args: Native())
    assert await opencode_native.fork_at(None, runner.project, SESSION, 'msg_test') == 'ses_new'
    assert calls[-1] == ('POST', '/session/' + SESSION + '/fork', {'before': 'msg_test'})
    calls.clear()
    with pytest.raises(Refused, match='not_found'):
        await opencode_native.fork_at(None, runner.project / 'foreign', SESSION, 'msg_test')
    assert all(method == 'GET' for method, _, _ in calls)


async def test_agent_validates_opencode_directories_and_mode_before_rpc(agent, root, runner):
    from unittest.mock import AsyncMock
    from studio_agent import projects

    agent.opencode = runner
    runner.models, runner.efforts = [], []
    runner.state = AsyncMock(return_value={'settings': {}, 'context': None})
    await agent.handle(req('open', pin=None))
    pid = projects.pid('demo')
    extra = {'engine': 'opencode', 'project': pid, 'chat': CHAT}
    response = (await agent.handle(req('state', **extra, agent_mode='plan', additional_directories=[str(root)])))[0]
    assert response['ok']
    assert runner.state.call_args.kwargs == {'agent_mode': 'plan', 'additional_directories': [str(root.resolve())]}
    runner.state.reset_mock()
    for bad in ({'agent_mode': 'bad\nmode'}, {'additional_directories': ['../escape']},
                {'additional_directories': ['C:\\*']}, {'additional_directories': [str(root)] * 9},
                {'additional_directories': [str(root)], 'shared_projects': [pid]}):
        assert (await agent.handle(req('state', **extra, **bad)))[0]['code'] == 'invalid'
    assert not runner.state.called
