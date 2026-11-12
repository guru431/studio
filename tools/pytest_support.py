"""Общий цикл событий тестов сервера и Windows-моста."""
import asyncio
import sys


if sys.platform == "win32":
    class RecoveringProactorLoop(asyncio.ProactorEventLoop):
        """WinError 995 внутреннего чтения не отключает пробуждения из потоков.

        В CPython 3.14.2 отмена IOCP между run_until_complete иногда завершает
        новое чтение с ERROR_OPERATION_ABORTED. Штатный обработчик больше не
        читает self-pipe: callback executor остаётся в очереди, Runner.close висит.
        Только действующее чтение с этим кодом запускаем снова; отменённые и
        устаревшие Future, закрытие и прочие ошибки обрабатывает сам asyncio.
        Удалить обход после проверки исправления CPython той же регрессией.
        """

        def _loop_self_reading(self, f=None):
            if f is not None and f is self._self_reading_future and not f.cancelled():
                error = f.exception()
                if (isinstance(error, ConnectionResetError) and getattr(error, "winerror", None) == 995
                        and not self.is_closed() and self._ssock is not None):
                    self._self_reading_future = None
                    f = None
            super()._loop_self_reading(f)


def new_event_loop():
    if sys.platform == "win32":
        return RecoveringProactorLoop()
    return asyncio.new_event_loop()
