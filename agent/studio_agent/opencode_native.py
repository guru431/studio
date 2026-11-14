"""Ветка перед сообщением через локальный API OpenCode 2; пароль живёт только в памяти."""

import asyncio
import base64
import contextlib
import http.client
import json
import math
import os
import re
import secrets
import subprocess
from urllib.parse import urlencode

from . import jobs, limits
from .errors import Refused


class Client:
    def __init__(self, path, project):
        self.path, self.project = path, project
        self.process = self.job = None
        self.port = None
        self.password = secrets.token_urlsafe(32)

    async def __aenter__(self):
        if self.path is None:
            raise Refused('unsupported')
        try:
            self.process = await asyncio.create_subprocess_exec(str(self.path), 'serve', '--hostname', '127.0.0.1', '--port', '0',
                cwd=str(self.project), env={**os.environ, 'OPENCODE_PASSWORD': self.password,
                                          'OPENCODE_SERVER_PASSWORD': self.password},
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW)
            self.job = jobs.attach(self.process.pid)
            async with asyncio.timeout(15):
                for _ in range(32):
                    line = await self.process.stdout.readline()
                    if not line:
                        break
                    match = re.search(rb'http://127\.0\.0\.1:(\d+)', line)
                    if match and 0 < int(match[1]) <= 65535:
                        self.port = int(match[1])
                        return self
            raise Refused('unsupported', 'Локальный API OpenCode не запустился')
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *args):
        if self.process is not None:
            if self.process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
            await self.process.wait()
        if self.job is not None:
            self.job.close()
        self.password = ''

    def _request(self, method, path, body):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=15)
        try:
            auth = base64.b64encode(('opencode:' + self.password).encode()).decode('ascii')
            connection.request(method, '/api' + path, json.dumps(body).encode() if body is not None else None,
                               {'Authorization': 'Basic ' + auth, 'Content-Type': 'application/json'})
            response = connection.getresponse()
            raw = response.read(limits.HISTORY_BUDGET + 1)
            if response.status == 404:
                raise Refused('not_found')
            if response.status == 204:
                return {}
            if response.status != 200 or len(raw) > limits.HISTORY_BUDGET:
                raise Refused('invalid', 'Локальный API OpenCode отказал в операции')
            return json.loads(raw)
        finally:
            connection.close()

    async def request(self, method, path, body=None):
        return await asyncio.to_thread(self._request, method, path, body)


async def owned(client, target, project):
    from .codex import same_project
    from .opencode import SESSION_ID

    if not isinstance(target, str) or not SESSION_ID.fullmatch(target):
        raise Refused('invalid')
    session = (await client.request('GET', '/session/' + target)).get('data', {})
    if not same_project((session.get('location') or {}).get('directory'), project):
        raise Refused('not_found')
    return session


async def fork_at(path, project, target, message_id):
    if not re.fullmatch(r'msg_[A-Za-z0-9_-]{1,200}', message_id):
        raise Refused('invalid', 'Ветвление требует исходный ID сообщения OpenCode')
    async with Client(path, project) as client:
        await owned(client, target, project)
        # GET проверяет принадлежность сообщения до создания новой сессии.
        message = (await client.request('GET', '/session/' + target + '/message/' + message_id)).get('data', {})
        if message.get('type') != 'user':
            raise Refused('invalid')
        result = await client.request('POST', '/session/' + target + '/fork', {'before': message_id})
        return result.get('data', {}).get('id')


async def children_of(path, project, target):
    from .codex import same_project
    from .opencode import SESSION_ID

    items, pending, seen, complete = [], [(target, 0)], {target}, True
    async with Client(path, project) as client:
        await owned(client, target, project)
        while pending and len(items) < 50:
            parent, depth = pending.pop(0)
            query, cursors = {'parentID': parent, 'limit': 100}, set()
            for _ in range(10):
                page = await client.request('GET', '/session?' + urlencode(query))
                for row in page.get('data', []):
                    sid = row.get('id')
                    if (not isinstance(sid, str) or not SESSION_ID.fullmatch(sid) or len(sid) > 64 or sid in seen
                            or row.get('parentID') != parent or row.get('fork')
                            or not same_project((row.get('location') or {}).get('directory'), project)):
                        continue
                    seen.add(sid)
                    times, tokens = row.get('time', {}), row.get('tokens', {})
                    counts = [tokens.get(key) for key in ('input', 'output', 'reasoning')]
                    total = sum(counts) if all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in counts) else None
                    created = times.get('created')
                    started = created / 1000 if type(created) in (int, float) and math.isfinite(created) and created > 0 else None
                    items.append({'id': sid, 'title': str(row.get('title') or 'Подзадача')[:200],
                        'status': {'succeeded': 'completed', 'failed': 'failed', 'interrupted': 'stopped'}.get(row.get('outcome'), 'unknown'),
                        'started_at': started, 'duration_ms': None, 'total_tokens': total})
                    if depth < 15:
                        pending.append((sid, depth + 1))
                    else:
                        complete = False
                    if len(items) >= 50:
                        complete = False
                        break
                cursor = (page.get('cursor') or {}).get('next')
                if not cursor or len(items) >= 50:
                    break
                if cursor in cursors:
                    complete = False
                    break
                cursors.add(cursor)
                query['cursor'] = cursor
            else:
                complete = False
    return {'items': items, 'complete': complete}
