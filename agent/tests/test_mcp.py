import json
import asyncio
import threading

import pytest

from studio_agent import mcp, projects
from studio_agent.errors import Refused
from test_agent import agent, root, req


CONFIG = {'mcpServers': {'local': {'command': 'python', 'args': ['-m', 'example'], 'env': {'KEY': 'secret'}},
                          'remote': {'type': 'http', 'url': 'https://mcp.example.com/mcp', 'headers': {'Authorization': 'Bearer private'}}}}


def test_encrypted_store_project_engine_isolation_and_adapters(home, tmp_path):
    mcp.save(home, tmp_path, 'claude', CONFIG)
    assert mcp.load(home, tmp_path, 'claude') == CONFIG
    assert b'secret' not in next((home / 'mcp').glob('*.bin')).read_bytes()
    assert mcp.load(home, tmp_path / 'another', 'claude') == {'mcpServers': {}}
    assert mcp.load(home, tmp_path, 'codex') == {'mcpServers': {}}
    assert mcp.codex(CONFIG)['mcp_servers']['remote']['http_headers'] == {'Authorization': 'Bearer private'}
    with pytest.raises(Refused):
        mcp.opencode(CONFIG, {})
    acp = mcp.opencode(CONFIG, {'agentCapabilities': {'mcpCapabilities': {'http': True}}})
    assert acp[0]['env'] == [{'name': 'KEY', 'value': 'secret'}]
    assert acp[1]['headers'] == [{'name': 'Authorization', 'value': 'Bearer private'}]
    temporary = mcp.ClaudeConfig(home, tmp_path)
    try:
        from pathlib import Path
        path = Path(temporary.path)
        assert json.loads(path.read_text()) == CONFIG
    finally:
        temporary.cleanup()
    assert not path.exists()


@pytest.mark.parametrize('item', [{'url': 'http://user:password@example.com/mcp'}, {'command': 'python', 'args': 'bad'},
                                  {'command': 'python', 'env': {'BAD=NAME': 'x'}}, {'url': 'https://example.com', 'headers': {'X-Key': 'x\r\ny'}}])
def test_invalid_configuration_is_refused(item):
    with pytest.raises(Refused):
        mcp.validate({'mcpServers': {'test': item}}, 'claude')


async def test_owner_only_mcp_ops_require_open_computer(agent, root):
    pid = projects.pid('demo')
    assert (await agent.handle(req('mcp_get', project=pid)))[0]['code'] == 'closed'
    await agent.handle(req('open', pin=None))
    assert (await agent.handle(req('mcp_save', project=pid, config=CONFIG)))[0]['ok']
    response = (await agent.handle(req('mcp_get', project=pid)))[0]
    assert response['data'] == CONFIG
    assert (await agent.handle(req('mcp_save', project=pid, shared_projects=[pid], config=CONFIG)))[0]['code'] == 'closed'


async def test_cancelled_config_creation_cleans_the_file_after_worker_finishes(home, tmp_path, monkeypatch):
    mcp.save(home, tmp_path, 'claude', CONFIG)
    entered, release = threading.Event(), threading.Event()
    original = mcp.ClaudeConfig

    def held(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(mcp, 'ClaudeConfig', held)
    task = asyncio.create_task(mcp.claude_config(tmp_path))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        task.cancel()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert list((home / 'mcp').glob('runtime-*')) == []
