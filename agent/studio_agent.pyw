"""Мост studio-agent без консоли: `.venv\\Scripts\\pythonw.exe studio_agent.pyw` (так его зовёт автозапуск)."""

import sys

from studio_agent.app import main

sys.exit(main(sys.argv[1:]))
