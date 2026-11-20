"""Общий цикл событий и строгий режим пропусков тестов сервера и Windows-моста; привязка детей pytest."""
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


_JOB = None


def bind_children():
    """Windows: процесс pytest — в свой Job Object с `KILL_ON_JOB_CLOSE`, и дети (headless Edge, драйверы
    Playwright и node, PowerShell) наследуют его. Описатель держит только этот процесс: при любом его
    завершении, в том числе `os._exit` по сроку теста или внешнем `timeout`, Windows гасит всё дерево —
    сироты-браузеры больше не держат файлы временного корня. Вложенные job (Windows 8+) не мешают job
    вызывающего; `BREAKAWAY_OK` оставляет запуски с `CREATE_BREAKAWAY_FROM_JOB` рабочими. Отказ — как
    прежде, без привязки. Возвращает True, если процесс привязан."""
    global _JOB
    if sys.platform != "win32":
        return False
    if _JOB is not None:
        return True
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.argtypes, k.CreateJobObjectW.restype = [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.SetInformationJobObject.restype = wintypes.BOOL
    k.GetCurrentProcess.restype = wintypes.HANDLE
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.CloseHandle.argtypes, k.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
    job = k.CreateJobObjectW(None, None)        # без SECURITY_ATTRIBUTES: описатель детям не наследуется
    if not job:
        return False
    info = Extended()
    info.BasicLimitInformation.LimitFlags = 0x2000 | 0x0800    # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
    if not (k.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
            and k.AssignProcessToJobObject(job, k.GetCurrentProcess())):
        k.CloseHandle(job)
        return False
    _JOB = job                                  # не закрывать: закрытие погасило бы и сам pytest
    return True
