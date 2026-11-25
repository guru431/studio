"""Синхронизация с карточкой хода без зависимости от скорости файловых потоков."""
import asyncio


async def asking(runner):
    turn = next(iter(runner._turns.values()))
    ready, previous = asyncio.Event(), runner.emit

    def emit(frame):
        previous(frame)
        if frame["e"]["k"] == "ask":
            ready.set()

    def finished(_):
        ready.set()

    runner.emit = emit
    turn.task.add_done_callback(finished)
    try:
        if not turn.asks:
            await ready.wait()  # зависание ограничивает общий pytest-timeout
        if turn.task.done():
            await turn.task
        assert turn.asks, "Ход завершился без карточки"
        return next(iter(turn.asks))
    finally:
        turn.task.remove_done_callback(finished)
        runner.emit = previous


def bare(event):
    """Событие без времени хода (`turn_ms`, `session_ms`, протокол §26): сравнение не зависит от часов."""
    return {k: v for k, v in event.items() if k not in ("turn_ms", "session_ms")}
