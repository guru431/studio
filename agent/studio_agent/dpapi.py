"""Защита локальных секретов в контексте пользователя."""
from .host import LINUX, MACOS

if MACOS:
    from .macos_keychain import protect, unprotect
elif LINUX:
    from .linux_secrets import protect, unprotect
else:
    from .dpapi_windows import protect, unprotect
