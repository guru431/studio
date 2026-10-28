"""Живая студия для сквозной проверки: `python live_studio_server.py <каталог>` средой студии.

Поднимает `start_live_app` студии (`tests/live_app.py`: база и медиа в каталоге, администратор
`owner`/`password-123`), печатает `READY <адрес>` и ждёт закрытия stdin, затем гасит службу.
Репозиторий студии — `STUDIO_REPO`, по умолчанию `C:/work/projects/studio`."""

import os
import sys
from pathlib import Path

REPO = Path(os.environ.get("STUDIO_REPO") or "C:/work/projects/studio")
sys.path[:0] = [str(REPO / "app"), str(REPO / "tests")]

from live_app import start_live_app  # noqa: E402


def main() -> None:
    live = start_live_app(Path(sys.argv[1]))
    try:
        print(f"READY {live.url}", flush=True)
        sys.stdin.read()
    finally:
        live.stop()


if __name__ == "__main__":
    main()
