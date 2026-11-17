"""Мост studio-agent без консоли: `.venv\\Scripts\\pythonw.exe studio_agent.pyw` (так его зовёт автозапуск)."""

import sys
from pathlib import Path

if sys.platform == 'darwin':
    from studio_agent.macos_launcher import ensure_native
    ensure_native(Path(__file__).resolve().parent)

from studio_agent.app import main

sys.exit(main(sys.argv[1:]))
