"""Windows ACL каталогов выпуска: наследование от каталога установки вместо защищённого DACL «по владельцу».

`tempfile.mkdtemp` (режим 0o700) с Python 3.12.4/3.13 (CVE-2024-4030) ставит каталогу защищённый DACL
`SYSTEM, Administrators, OWNER RIGHTS`. Доступ пользователя тогда зависит от владельца, а после запуска с
повышением владелец новых объектов — группа Administrators: обычный сеанс того же пользователя её права не
имеет (в его токене она deny-only) и теряет доступ к выпуску — запись автозапуска отвечает «Отказано в доступе»."""
import ctypes
from ctypes import wintypes

SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x4
UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000
SE_DACL_PROTECTED = 0x1000
INHERITED_ACE = 0x10
ACL_REVISION = 2
OWNER_RIGHTS = 'S-1-3-4'


class _AceHeader(ctypes.Structure):
    _fields_ = [('type', ctypes.c_ubyte), ('flags', ctypes.c_ubyte), ('size', wintypes.WORD)]


class _AclSize(ctypes.Structure):
    _fields_ = [('count', wintypes.DWORD), ('used', wintypes.DWORD), ('free', wintypes.DWORD)]


def _api():
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    advapi.GetNamedSecurityInfoW.argtypes = (wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD,
                                             ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                                             ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p),
                                             ctypes.POINTER(ctypes.c_void_p))
    advapi.GetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.SetNamedSecurityInfoW.argtypes = (wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p,
                                             ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    advapi.SetNamedSecurityInfoW.restype = wintypes.DWORD
    advapi.GetSecurityDescriptorControl.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.WORD),
                                                    ctypes.POINTER(wintypes.DWORD))
    advapi.GetAclInformation.argtypes = (ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int)
    advapi.GetAce.argtypes = (ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
    advapi.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
    advapi.InitializeAcl.argtypes = (ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel.LocalFree.restype = ctypes.c_void_p
    return advapi, kernel


def _check(ok):
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


def describe(path) -> tuple[bool, list[tuple[str | None, bool]]]:
    """(DACL защищён от наследования, [(SID ACE строкой или None для необычного типа ACE, ACE унаследован)])."""
    advapi, kernel = _api()
    dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    error = advapi.GetNamedSecurityInfoW(str(path), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None,
                                         ctypes.byref(dacl), None, ctypes.byref(descriptor))
    if error:
        raise ctypes.WinError(error)
    try:
        control, revision = wintypes.WORD(), wintypes.DWORD()
        _check(advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)))
        entries = []
        if dacl.value:
            size = _AclSize()
            _check(advapi.GetAclInformation(dacl, ctypes.byref(size), ctypes.sizeof(size), 2))  # AclSizeInformation
            for index in range(size.count):
                ace = ctypes.c_void_p()
                _check(advapi.GetAce(dacl, index, ctypes.byref(ace)))
                header = _AceHeader.from_address(ace.value)
                sid = None
                if header.type in (0, 1):  # ACCESS_ALLOWED / ACCESS_DENIED: заголовок, маска, затем SID
                    text = wintypes.LPWSTR()
                    _check(advapi.ConvertSidToStringSidW(ace.value + 8, ctypes.byref(text)))
                    sid = text.value
                    kernel.LocalFree(text)
                entries.append((sid, bool(header.flags & INHERITED_ACE)))
        return bool(control.value & SE_DACL_PROTECTED), entries
    finally:
        kernel.LocalFree(descriptor)


def inherit(path) -> bool:
    """Защищённый DACL → только ACE, унаследованные от родителя; вложенным ОС пересчитывает наследуемые сама.
    True — DACL менялся. Нужны READ_CONTROL и WRITE_DAC: прежний DACL `mkdtemp` даёт их SYSTEM, Administrators
    (сеанс с повышением) и владельцу; обычный сеанс при владельце-Administrators — `OSError` (отказано в доступе)."""
    if not describe(path)[0]:
        return False
    advapi, _ = _api()
    empty = ctypes.create_string_buffer(64)
    _check(advapi.InitializeAcl(empty, len(empty), ACL_REVISION))
    # Пустой ACL, а не NULL: NULL-DACL открыл бы каталог всем.
    error = advapi.SetNamedSecurityInfoW(str(path), SE_FILE_OBJECT,
                                         DACL_SECURITY_INFORMATION | UNPROTECTED_DACL_SECURITY_INFORMATION,
                                         None, None, empty, None)
    if error:
        raise ctypes.WinError(error)
    return True
