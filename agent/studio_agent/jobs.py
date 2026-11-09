"""Job Object Windows: дерево дочернего CLI гаснет вместе с ним (`KILL_ON_JOB_CLOSE`).

`codex app-server` и `opencode acp` запускают MCP-серверы (`npx` → `node`) и шеллы. Прежний
`disconnect` гасил только сам exe, и внуки оставались сиротами. Процесс помещается в свой job
сразу после запуска; закрытие описателя (`Job.close`, выход или падение моста) завершает всех
оставшихся участников. Процессы, которые CLI успел породить до назначения, в job не входят.
"""

import ctypes
import logging
import os
from ctypes import wintypes

log = logging.getLogger(__name__)

KILL_ON_JOB_CLOSE = 0x2000
# Запуск с CREATE_BREAKAWAY_FROM_JOB без этого флага падал бы «отказано в доступе»; такой
# процесс явно просит жить отдельно — он выходит из job, как и до него.
BREAKAWAY_OK = 0x0800
EXTENDED_LIMIT_INFORMATION = 9
PROCESS_SET_QUOTA = 0x0100
PROCESS_TERMINATE = 0x0001


class _Basic(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _Io(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _Extended(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", _Io), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.argtypes, k.CreateJobObjectW.restype = [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    k.SetInformationJobObject.restype = wintypes.BOOL
    k.OpenProcess.argtypes, k.OpenProcess.restype = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.CloseHandle.argtypes, k.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
    return k


class Job:
    def __init__(self, kernel32, handle):
        self.kernel32, self.handle = kernel32, handle

    def close(self):
        """Последний описатель job: Windows завершает всех его участников, если они ещё живы."""
        if self.handle:
            self.kernel32.CloseHandle(self.handle)
            self.handle = None


def attach(pid):
    """Свой job с `KILL_ON_JOB_CLOSE` для запущенного процесса; не Windows, нет pid, отказ — None
    (поведение прежнее, в журнал — только код ошибки)."""
    if os.name != "nt" or type(pid) is not int:
        return None
    k = _kernel32()
    job = k.CreateJobObjectW(None, None)  # без SECURITY_ATTRIBUTES: описатель не наследуется детьми
    if not job:
        log.warning("Job Object: создание: код %d", ctypes.get_last_error())
        return None
    try:
        info = _Extended()
        info.BasicLimitInformation.LimitFlags = KILL_ON_JOB_CLOSE | BREAKAWAY_OK
        if not k.SetInformationJobObject(job, EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError
        process = k.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not process:
            raise OSError
        try:
            if not k.AssignProcessToJobObject(job, process):
                raise OSError
        finally:
            k.CloseHandle(process)
    except OSError:
        # Не назначен — закрытие пустого job никого не завершает.
        log.warning("Job Object: назначение: код %d", ctypes.get_last_error())
        k.CloseHandle(job)
        return None
    return Job(k, job)
