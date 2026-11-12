"""Настоящий Workflow и SDK; ответы модели подставляет локальный HTTP, квота не расходуется."""

import asyncio
import json
import itertools
import threading
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from claude_agent_sdk import ResultMessage

from studio_agent import sessions, store, subagents
from studio_agent.claude_sdk import ClaudeSDKClient
from studio_agent.turns import Runner
from test_turns import CHAT, PID, marked

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]

SCRIPT = """export const meta = {name: 'studio-probe', description: 'Local protocol test'};
return await pipeline(['one', 'two'], name => agent('workflow-probe-child ' + name, {label: name}));
"""


@pytest.fixture
def home(monkeypatch):
    # Двойное кодирование pytest tmp_path в каталоге CLI превышает MAX_PATH Windows.
    with tempfile.TemporaryDirectory(prefix='studio-wf-') as directory:
        path = Path(directory).resolve()
        monkeypatch.setenv('STUDIO_AGENT_HOME', str(path))
        yield path


@pytest.fixture
def project(home, marked):
    path = home / 'demo'
    path.mkdir()
    return path


@pytest.fixture
def provider(project):
    release = threading.Event()
    main_calls = []
    requests = itertools.count()
    source = project / 'probe.txt'
    source.write_text('Local workflow read succeeds.', encoding='utf-8')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if 'count_tokens' in self.path:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(b'{"input_tokens":1}')
                return
            last = json.dumps(data.get('messages', [{}])[-1].get('content', ''))
            prompts = [m.get('content', '') for m in data.get('messages', []) if m.get('role') == 'user']
            words = '\n'.join(content if isinstance(content, str) else '\n'.join(
                block.get('text', '') for block in content if block.get('type') == 'text') for content in prompts)
            child = 'workflow-probe-child' in words
            main = not child and any(t.get('name') == 'Workflow' for t in data.get('tools', []))
            if child:
                if not release.wait(30):
                    self.send_error(504)
                    return
            elif main:
                main_calls.append(1)
            tool = main and len(main_calls) == 1 or child and 'toolu_child_probe_' not in last
            identifier = 'toolu_child_probe_' + ('one' if 'one' in words else 'two') if child else 'toolu_workflow_probe'
            block = ({'type': 'tool_use', 'id': identifier, 'name': 'Read' if child else 'Workflow', 'input': {}}
                     if tool else {'type': 'text', 'text': ''})
            delta = ({'type': 'input_json_delta', 'partial_json': json.dumps(
                {'file_path': str(source)} if child else {'script': SCRIPT})}
                     if tool else {'type': 'text_delta', 'text': 'child finished' if child else 'parent finished'})
            usage = {'input_tokens': 1, 'output_tokens': 1}
            frames = [
                {'type': 'message_start', 'message': {'id': f'msg_probe_{next(requests)}', 'type': 'message', 'role': 'assistant',
                 'model': data.get('model', 'claude-sonnet-5-5'), 'content': [], 'stop_reason': None,
                 'stop_sequence': None, 'usage': usage}},
                {'type': 'content_block_start', 'index': 0, 'content_block': block},
                {'type': 'content_block_delta', 'index': 0, 'delta': delta},
                {'type': 'content_block_stop', 'index': 0},
                {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use' if tool else 'end_turn',
                 'stop_sequence': None}, 'usage': {'output_tokens': 1}},
                {'type': 'message_stop'},
            ]
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            for frame in frames:
                self.wfile.write(('event: ' + frame['type'] + '\ndata: ' + json.dumps(frame) + '\n\n').encode())
            self.wfile.flush()

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', release, main_calls
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


async def test_native_workflow_keeps_its_children_alive_after_parent_result(home, project, provider, monkeypatch):
    address, release, main_calls = provider
    config = home / 'claude'
    config.mkdir()
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(config))
    monkeypatch.delenv('CLAUDECODE', raising=False)
    parent_result = asyncio.Event()
    children_started = asyncio.Event()
    sent = []

    class Client(ClaudeSDKClient):
        def __init__(self, *, options):
            options.setting_sources = []
            options.allowed_tools = ['Workflow', 'Read']
            options.can_use_tool = None
            options.env.update(ANTHROPIC_BASE_URL=address, ANTHROPIC_API_KEY='local-test-key',
                               ANTHROPIC_AUTH_TOKEN='', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
                               CLAUDE_CODE_WORKFLOW_PREFIX_STAGGER_MS='0')
            options.extra_args = {}
            super().__init__(options=options)

        async def receive_response(self):
            async for message in super().receive_response():
                yield message
                if isinstance(message, ResultMessage):
                    parent_result.set()

    def emit(frame):
        sent.append(frame)
        if len([r for r in frame['e'].get('agents', []) if r['status'] == 'running']) == 2:
            children_started.set()

    runner = Runner(emit, choices=store.Choices(home), cli_path=None, factory=Client)
    runner.start(project, PID, CHAT, 'workflow-probe-main', 'claude-sonnet-5-5', None)
    turn = runner._turns[CHAT]
    try:
        await asyncio.wait_for(parent_result.wait(), 20)
        await asyncio.wait_for(children_started.wait(), 10)
        assert CHAT in runner.live() and turn.background_tasks
        assert not any(f['e']['k'] == 'done' for f in sent)
        release.set()
        await asyncio.wait_for(asyncio.shield(turn.task), 20)
        assert sent[-1]['e']['status'] == 'done' and len(main_calls) >= 3
        monkeypatch.setattr(sessions, 'exists', lambda *_: True)
        rows = subagents.list_children(project, CHAT, choices=runner.choices)['items']
        assert len(rows) == 2 and {row['status'] for row in rows} == {'completed'}
        for row in rows:
            messages = subagents.history(project, CHAT, row['id'])['items']
            steps = [step for message in messages for step in message['steps']]
            assert any(step['tool'] == 'Read' and not step['error'] for step in steps), messages
    finally:
        release.set()
        if not turn.task.done():
            turn.task.cancel()
        await asyncio.gather(turn.task, return_exceptions=True)
