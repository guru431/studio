"""Дополнительные Git remotes: сравнение только с локальными refs, без сети и credentials."""

import subprocess
import time
from pathlib import Path

from . import gitinfo

GIT_SEC = 3
BUDGET_SEC = 5
UNKNOWN = {"state": "unknown", "behind": None, "ahead": None}


def read(project: Path, branch: str | None, head: str | None, *, now=time.monotonic) -> list[dict]:
    deadline = now() + BUDGET_SEC

    def run(args):
        remaining = deadline - now()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("git", BUDGET_SEC)
        return gitinfo.run(project, args, timeout=min(GIT_SEC, remaining))

    try:
        configured = run(["remote"])
        if configured.returncode != 0:
            return []
        names = configured.stdout.decode("utf-8").splitlines()
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return []
    items = [{"name": name, **UNKNOWN} for name in names]
    if not items or not branch or not head:
        return items
    try:
        shallow = run(["rev-parse", "--is-shallow-repository"])
        if shallow.returncode != 0 or shallow.stdout.strip() != b"false":
            return items
    except (OSError, subprocess.TimeoutExpired):
        return items
    for item in items:
        try:
            # Неизвестная/не загруженная ветка не означает, что на сервере её нет.
            ref = f"refs/remotes/{item['name']}/{branch}"
            count = run(["rev-list", "--left-right", "--count", f"{ref}...{head}", "--"])
            if count.returncode != 0:
                continue
            ahead, behind = map(int, count.stdout.split())
            if min(ahead, behind) < 0:
                continue
            state = "diverged" if ahead and behind else "behind" if behind else "ahead" if ahead else "current"
            item.update(state=state, behind=behind, ahead=ahead)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    return items
