"""Настоящий Workflow и SDK; ответы модели подставляет локальный HTTP, квота не расходуется."""

import asyncio
import json
import itertools
import os
import threading
import tempfile
import sys
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
    background_finished = threading.Event()
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
            shell = 'shell-probe-main' in words
            consumed = 'shell-consumed-probe-main' in words
            monitor = 'monitor-probe-main' in words or 'monitor-finished-probe-main' in words
            steered = 'steer-probe-main' in words
            main = shell or consumed or monitor or steered or not child and any(t.get('name') == 'Workflow' for t in data.get('tools', []))
            if child:
                if not release.wait(30):
                    self.send_error(504)
                    return
            elif main:
                main_calls.append('steer-probe-second' in json.dumps(data.get('messages', [])))
                if 'monitor-finished-probe-main' in words and len(main_calls) == 2:
                    if not background_finished.wait(10):
                        self.send_error(504)
                        return
            followup = monitor and len(main_calls) == 3
            tool = main and len(main_calls) == 1 or child and 'toolu_child_probe_' not in last or followup
            identifier = ('toolu_child_probe_' + ('one' if 'one' in words else 'two') if child else
                          'toolu_monitor_followup' if followup else 'toolu_workflow_probe')
            if child:
                name, arguments = 'Read', {'file_path': str(source)}
            elif followup:
                name, arguments = 'Bash', {'command': 'echo monitor-followup-finished', 'description': 'Inspect monitor result'}
            elif shell or consumed or monitor or steered:
                name = 'Monitor' if monitor else 'Bash'
                arguments = {'command': f'"{Path(sys.executable).as_posix()}" "{(project / "shell_probe.py").as_posix()}"',
                             'description': 'Studio shell probe'}
                if shell:
                    arguments['run_in_background'] = True
            else:
                name, arguments = 'Workflow', {'script': SCRIPT}
            block = ({'type': 'tool_use', 'id': identifier, 'name': name, 'input': {}}
                     if tool else {'type': 'text', 'text': ''})
            delta = ({'type': 'input_json_delta', 'partial_json': json.dumps(arguments)}
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
        yield f'http://127.0.0.1:{server.server_port}', release, main_calls, background_finished
    finally:
        release.set()
        background_finished.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)


@pytest.mark.parametrize('mode', ['workflow', 'shell', 'shell-consumed', 'monitor', 'monitor-finished'])
async def test_native_background_work_keeps_its_children_alive_after_parent_result(home, project, provider, monkeypatch, mode):
    address, release, main_calls, background_finished = provider
    config = home / 'claude'
    config.mkdir()
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(config))
    monkeypatch.delenv('CLAUDECODE', raising=False)
    script = ('import time\ntime.sleep(3)\nprint("shell finished")\n' if mode == 'shell-consumed' else
              'import pathlib, time\ndeadline = time.monotonic() + 30\nwhile not pathlib.Path(__file__).with_name("release").exists() and time.monotonic() < deadline:\n time.sleep(0.05)\nprint("shell finished")\n')
    (project / 'shell_probe.py').write_text(script, encoding='utf-8')
    if mode == 'monitor-finished':
        (project / 'release').touch()
    parent_result = asyncio.Event()
    children_started = asyncio.Event()
    sent = []

    class Client(ClaudeSDKClient):
        def __init__(self, *, options):
            options.setting_sources = []
            options.allowed_tools = ['Workflow', 'Read', 'Bash', 'Monitor']
            options.can_use_tool = None
            options.env.update(ANTHROPIC_BASE_URL=address, ANTHROPIC_API_KEY='local-test-key',
                               ANTHROPIC_AUTH_TOKEN='', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1',
                               CLAUDE_CODE_WORKFLOW_PREFIX_STAGGER_MS='0')
            options.extra_args.pop('chrome', None)
            super().__init__(options=options)

        async def receive_response(self):
            async for message in super().receive_response():
                if getattr(message, 'subtype', '').startswith('task_'):
                    if message.data.get('status') in ('completed', 'failed', 'stopped') or message.data.get('patch', {}).get('status') in ('completed', 'failed', 'stopped'):
                        background_finished.set()
                yield message
                if isinstance(message, ResultMessage):
                    parent_result.set()

    def emit(frame):
        sent.append(frame)
        if len([r for r in frame['e'].get('agents', []) if r['status'] == 'running']) == 2:
            children_started.set()

    cli_path = os.environ.get('STUDIO_TEST_CLAUDE_CLI')
    runner = Runner(emit, choices=store.Choices(home), cli_path=Path(cli_path) if cli_path else None, factory=Client)
    runner.start(project, PID, CHAT, f'{mode}-probe-main', 'claude-sonnet-5-5', None)
    turn = runner._turns[CHAT]
    try:
        await asyncio.wait_for(parent_result.wait(), 20)
        if mode == 'shell-consumed':
            await asyncio.wait_for(asyncio.shield(turn.task), 20)
            assert sent[-1]['e']['status'] == 'done' and len(main_calls) == 2
            assert background_finished.is_set() and not turn.background_notices
            return
        if mode == 'workflow':
            await asyncio.wait_for(children_started.wait(), 10)
        assert CHAT in runner.live() and turn.waiting and not turn.finishing
        assert not any(f['e']['k'] == 'done' for f in sent)
        release.set()
        (project / 'release').touch()
        await asyncio.wait_for(asyncio.shield(turn.task), 20)
        assert sent[-1]['e']['status'] == 'done' and len(main_calls) >= 3
        if mode.startswith('monitor'):
            assert any(f['e']['k'] == 'result' and f['e']['id'] == 'toolu_monitor_followup' and not f['e']['error']
                       and 'monitor-followup-finished' in f['e']['text'] for f in sent)
        files = list((config / 'projects').glob('*/' + CHAT + '.jsonl'))
        assert len(files) == 1
        records = [json.loads(line) for line in files[0].read_text(encoding='utf-8').splitlines()]
        assert next(row for row in records if row.get('type') == 'user')['entrypoint'] == 'local-agent'
        monkeypatch.setattr(sessions, 'exists', lambda *_: True)
        rows = subagents.list_children(project, CHAT, choices=runner.choices)['items']
        assert len(rows) == (2 if mode == 'workflow' else 0)
        assert {row['status'] for row in rows} == ({'completed'} if mode == 'workflow' else set())
        for row in rows:
            messages = subagents.history(project, CHAT, row['id'])['items']
            steps = [step for message in messages for step in message['steps']]
            assert any(step['tool'] == 'Read' and not step['error'] for step in steps), messages
    finally:
        release.set()
        (project / 'release').touch()
        if not turn.task.done() and not turn.finishing:
            turn.task.cancel()
        await asyncio.gather(turn.task, return_exceptions=True)


async def test_native_steer_absorbed_by_the_running_response_finishes_the_turn(home, project, provider, monkeypatch):
    """Уточнение во время Bash: CLI поглощает его тем же ответом (один ResultMessage на два ввода) — ход кончается
    одним `done`, а не висит «Думает…» (мост 0.47.1, 25.11)."""
    address, _release, main_calls, _finished = provider
    config = home / 'claude'
    config.mkdir()
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(config))
    monkeypatch.delenv('CLAUDECODE', raising=False)
    (project / 'shell_probe.py').write_text('import time\ntime.sleep(3)\nprint("shell finished")\n', encoding='utf-8')
    sent = []
    step = asyncio.Event()

    class Client(ClaudeSDKClient):
        def __init__(self, *, options):
            options.setting_sources = []
            options.allowed_tools = ['Bash']
            options.can_use_tool = None
            options.env.update(ANTHROPIC_BASE_URL=address, ANTHROPIC_API_KEY='local-test-key',
                               ANTHROPIC_AUTH_TOKEN='', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC='1')
            options.extra_args.pop('chrome', None)
            super().__init__(options=options)

    def emit(frame):
        sent.append(frame)
        if frame['e']['k'] == 'step':
            step.set()

    cli_path = os.environ.get('STUDIO_TEST_CLAUDE_CLI')
    runner = Runner(emit, choices=store.Choices(home), cli_path=Path(cli_path) if cli_path else None, factory=Client)
    runner.start(project, PID, CHAT, 'steer-probe-main', 'claude-sonnet-5-5', None)
    turn = runner._turns[CHAT]
    try:
        await asyncio.wait_for(step.wait(), 20)
        assert (await runner.steer(PID, CHAT, 'steer-probe-second', []))['mode'] == 'steer'
        await asyncio.wait_for(asyncio.shield(turn.task), 20)
        assert main_calls == [False, True]  # уточнение ушло модели в том же ответе, своего ответа у него нет
        assert [f['e']['status'] for f in sent if f['e']['k'] == 'done'] == ['done']
        assert CHAT not in runner.live() and not turn.unread
    finally:
        if not turn.task.done() and not turn.finishing:
            turn.task.cancel()
        await asyncio.gather(turn.task, return_exceptions=True)
