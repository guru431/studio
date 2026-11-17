"""Защита локальных секретов: Windows DPAPI или ключ шифрования в macOS Keychain."""
from .host import MACOS

if MACOS:
    from .macos_keychain import protect, unprotect
else:
    from .dpapi_windows import protect, unprotect
