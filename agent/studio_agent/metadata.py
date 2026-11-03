"""Ограниченные снимки в памяти моста: срок, общий запрос и инвалидация без записи на диск."""
import asyncio
import time
import json
import threading
from collections import OrderedDict


class Cache:
    def __init__(self, *, now=time.monotonic, maximum=128):
        self.now, self.maximum = now, maximum
        self.values = OrderedDict()
        self.pending = {}

    def peek(self, key):
        entry = self.values.get(key)
        if entry is None or entry[0] <= self.now():
            return None
        self.values.move_to_end(key)
        return entry[1]

    def start(self, key, load, *, ttl):
        if key in self.pending:
            return self.pending[key]

        async def read():
            try:
                value = await load()
                if self.pending.get(key) is task:
                    self.values[key] = (self.now() + ttl, value)
                    self.values.move_to_end(key)
                    while len(self.values) > self.maximum:
                        self.values.popitem(last=False)
                return value
            finally:
                if self.pending.get(key) is task:
                    self.pending.pop(key, None)

        task = asyncio.create_task(read())
        self.pending[key] = task
        # Фоновая проверка GitHub может остаться без ожидающего HTTP-запроса.
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return task

    async def get(self, key, load, *, ttl, refresh=False):
        if not refresh and (value := self.peek(key)) is not None:
            return value
        return await asyncio.shield(self.start(key, load, ttl=ttl))

    def invalidate(self, project=None):
        for key in set(self.values) | set(self.pending):
            if project is None or key[0] == project:
                self.values.pop(key, None)
                # Завершение уже начатого чтения не воскресит устаревший снимок.
                self.pending.pop(key, None)


class HistoryIndex:
    """Нормализованные истории только с проверенной ревизией источника и бюджетом памяти."""
    def __init__(self, maximum=12, budget=8 * 1024 * 1024):
        self.maximum, self.budget = maximum, budget
        self.values = OrderedDict()
        self.lock = threading.Lock()
        self.size = 0

    def get(self, key, revision):
        if revision is None:
            return None
        with self.lock:
            value = self.values.get(key)
            if value is None or value[0] != revision:
                return None
            self.values.move_to_end(key)
            return value[1]

    def put(self, key, before, after, items):
        if before is None or before != after:
            return
        size = len(json.dumps(items, ensure_ascii=True))
        if size > self.budget:
            return
        with self.lock:
            old = self.values.pop(key, None)
            self.size -= old[2] if old else 0
            self.values[key] = (after, items, size)
            self.size += size
            while len(self.values) > self.maximum or self.size > self.budget:
                _, value = self.values.popitem(last=False)
                self.size -= value[2]

    def clear(self):
        with self.lock:
            self.values.clear()
            self.size = 0
