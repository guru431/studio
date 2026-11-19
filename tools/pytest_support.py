"""Общий цикл событий и строгий режим пропусков тестов сервера и Windows-моста."""
import asyncio
import os
import sys

STRICT = "STUDIO_TESTS_STRICT"


def strict_skip(report, deliberate, item=None):
    """При STUDIO_TESTS_STRICT=1 (полный уровень контракта) пропуск — отказ: нет bash, PowerShell, git, node,
    браузера или движка Playwright. Пропуском остаются метки skip/skipif (платформа объявлена заранее), xfail
    и причины, начинающиеся с одной из `deliberate` (осознанные пропуски набора)."""
    if os.environ.get(STRICT) != "1" or not report.skipped or hasattr(report, "wasxfail"):
        return report
    reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
    if reason.startswith("Skipped: "):
        reason = reason[len("Skipped: "):]
    declared = set()
    for mark in item.iter_markers() if item is not None else ():
        if mark.name in ("skip", "skipif"):
            declared.add(mark.kwargs.get("reason", mark.args[0] if mark.name == "skip" and mark.args else ""))
    if reason in declared or reason.startswith(tuple(deliberate)):
        return report
    report.outcome = "failed"
    report.longrepr = f"{STRICT}=1: пропуск — отказ (нет инструмента?): {reason}"
    return report


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
