"""Кэши проверяются с управляемыми ожиданиями, отзывом и сменой настоящих путей."""
import asyncio
import _winapi
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from studio_agent import github_status, metadata, projects, project_summary, sessions, store
from studio_agent.codex import CodexRunner
from studio_agent.errors import Refused
from test_agent import make, opened, req, CHAT


async def test_shared_read_survives_one_cancel_and_invalidated_read_cannot_restore_snapshot():
    clock = [0]
    cache = metadata.Cache(now=lambda: clock[0])
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def load():
        calls.append(1)
        entered.set()
        await release.wait()
        return {'value': len(calls)}

    first = asyncio.create_task(cache.get(('p', 'summary'), load, ttl=20))
    await entered.wait()
    second = asyncio.create_task(cache.get(('p', 'summary'), load, ttl=20))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await second == {'value': 1} and len(calls) == 1
    assert await cache.get(('p', 'summary'), load, ttl=20) == {'value': 1}
    clock[0] = 21
    release.clear(); entered.clear()
    old = asyncio.create_task(cache.get(('p', 'summary'), load, ttl=20))
    await entered.wait()
    cache.invalidate('p')
    release.set()
    await old
    assert cache.peek(('p', 'summary')) is None
    assert await cache.get(('p', 'summary'), load, ttl=20) == {'value': 3}


def test_catalog_reuses_scan_but_rechecks_junction_and_configuration(tmp_path, monkeypatch):
    root, outside = tmp_path / 'projects', tmp_path / 'outside'
    project = root / 'demo'
    project.mkdir(parents=True); outside.mkdir()
    cfg = store.Config(projects_root=str(root))
    clock = [0]
    cache = projects.Catalog(now=lambda: clock[0])
    scan = Mock(wraps=projects.catalog)
    monkeypatch.setattr(projects, 'catalog', scan)
    for _ in range(3):
        assert cache.find(cfg, projects.pid('demo')) == project
    assert scan.call_count == 1
    project.rmdir()
    _winapi.CreateJunction(str(outside), str(project))
    with pytest.raises(Refused):
        cache.find(cfg, projects.pid('demo'))
    cfg.set_project_roots([str(outside)])
    assert cache.list(cfg) == []


async def test_catalogs_are_parallel_shared_and_activity_is_not_cached(home, tmp_path, monkeypatch):
    root = tmp_path / 'projects'; (root / 'demo').mkdir(parents=True)
    agent = make(home, root)
    pid = projects.pid('demo')
    entered = [asyncio.Event(), asyncio.Event()]
    release = asyncio.Event()
    calls = [0, 0]
    live = {}

    def backend(index):
        async def chats(project):
            calls[index] += 1
            entered[index].set()
            await release.wait()
            return [{'chat': CHAT, 'title': 'metadata'}]
        return SimpleNamespace(catalog={}, chats=chats, live=lambda: live)

    agent.codex, agent.opencode = backend(0), backend(1)
    monkeypatch.setattr(sessions, 'chats', lambda *args: [])
    await opened(agent)
    pending = asyncio.create_task(agent.handle(req('chats', project=pid)))
    await asyncio.gather(*(event.wait() for event in entered))
    release.set()
    assert (await pending)[0]['data']['complete']
    live[CHAT] = True
    result = (await agent.handle(req('chats', project=pid)))[0]['data']
    assert calls == [1, 1] and all(row['running'] and row['asking'] for row in result['items'])
    agent.emit({'t': 'ev', 'project': pid, 'e': {'k': 'done'}})
    await agent.handle(req('chats', project=pid))
    assert calls == [2, 2]


async def test_github_does_not_block_local_summary_and_remote_reads_are_shared(home, tmp_path, monkeypatch):
    root = tmp_path / 'projects'; (root / 'demo').mkdir(parents=True)
    agent = make(home, root)
    clock = [1000]
    agent._wall = lambda: clock[0]
    pid = projects.pid('demo')
    monkeypatch.setattr(project_summary, 'read', lambda *args, **kw: {
        'changed': 0, '_github': ('main', 'a' * 40, 'test/repo')})
    read = Mock(return_value={'state': 'current', 'behind': 0, 'ahead': 0})
    monkeypatch.setattr(github_status, 'read', read)
    await opened(agent)
    first = (await agent.handle(req('project_summary', project=pid)))[0]['data']
    assert first['changed'] == 0 and first['github_pending'] is True
    second = (await agent.handle(req('project_summary', project=pid, remote=True)))[0]['data']
    assert second['github']['state'] == 'current' and read.call_count == 1
    assert second['github_checked_at'] == 1000
    assert '_github' not in second
    release = threading.Event()
    def blocked(*args, **kwargs):
        assert release.wait(2)
        return {'state':'ahead','behind':0,'ahead':1}
    read.side_effect = blocked
    clock[0] = 2000
    forced = (await agent.handle(req('project_summary', project=pid, refresh=True)))[0]['data']
    assert forced['github_pending']
    waiting = asyncio.create_task(agent.handle(req('project_summary', project=pid, remote=True)))
    await asyncio.sleep(0)
    assert not waiting.done()
    release.set()
    fresh = (await waiting)[0]['data']
    assert fresh['github_checked_at'] == 2000 and fresh['github']['state'] == 'ahead'
    assert read.call_count == 2


async def test_quota_has_an_independent_metadata_channel(home):
    entered, release = asyncio.Event(), asyncio.Event()
    instances = []

    class Rpc:
        def __init__(self, *args): instances.append(self)
        async def connect(self): pass
        async def disconnect(self): pass
        async def request(self, method, params):
            if method == 'account/read':
                entered.set(); await release.wait()
            return {'data': []} if method == 'thread/list' else {}

    runner = CodexRunner(lambda frame: None, home=home, find_cli=lambda: home / 'codex.exe', factory=Rpc)
    runner.catalog = {}
    quota = asyncio.create_task(runner.subscription_usage(home))
    await entered.wait()
    assert await runner.chats(home) == []
    assert len(instances) == 2 and not quota.done()
    release.set(); await quota
    await runner.close(final=True)


def test_history_index_reuses_pages_and_rejects_concurrent_source_change(monkeypatch,tmp_path):
    from test_sessions import U
    info=SimpleNamespace(last_modified=1000,file_size=100)
    source=[U('one','First')]
    calls=[]
    changing=[False]
    def read(*args,**kwargs):
        calls.append(1)
        if changing[0]: info.file_size+=1
        return list(source)
    monkeypatch.setattr(sessions,'get_session_info',lambda *args,**kwargs:info)
    monkeypatch.setattr(sessions,'get_session_messages',read)
    index=metadata.HistoryIndex()
    assert sessions.history(tmp_path,CHAT,None,30,index=index)['total']==1
    assert sessions.history(tmp_path,CHAT,1,30,index=index)['total']==1
    assert len(calls)==1
    source.append(U('two','Second')); info.file_size+=1
    assert sessions.history(tmp_path,CHAT,None,30,index=index)['total']==2
    assert len(calls)==2
    info.file_size+=1; changing[0]=True
    sessions.history(tmp_path,CHAT,None,30,index=index)
    sessions.history(tmp_path,CHAT,None,30,index=index)
    assert len(calls)==4  # Две разные ревизии вокруг чтения не попадают в индекс.


async def test_opencode_catalog_connection_is_shared_and_closed(home):
    from studio_agent.opencode import OpenCodeRunner
    opened=[]
    class Rpc:
        def __init__(self,*args):self.closed=False;self.info={};opened.append(self)
        async def connect(self):pass
        async def disconnect(self):self.closed=True
        async def request(self,method,params):
            assert method=='session/list'
            return {'sessions':[]}
    runner=OpenCodeRunner(lambda frame:None,home=home,find_cli=lambda:home/'opencode.exe',factory=Rpc)
    runner.catalog={}
    assert await runner.chats(home)==await runner.chats(home)==[]
    assert len(opened)==1 and not opened[0].closed
    await runner.close(final=True)
    assert opened[0].closed
