"""Сводка проекта: только счётчики и даты, без содержимого журналов и путей файлов в ответе."""

import re
import subprocess
import time
from pathlib import Path

from . import files, github_status, gitinfo, remote_status
from .errors import Refused

JOURNAL_MAX = 2 * 1024 * 1024
GIT_SEC = 5


def count_entries(text: str, status: str) -> int:
    """Записи второго уровня с активным Status; примеры кода и HTML-комментарии не считаются."""
    lines = []
    fence = None
    comment = False
    for line in text.splitlines():
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence:
            if re.match(r"^ {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*$", line):
                fence = None
        elif marker and not comment:
            fence = marker[1]
        else:
            visible = ""
            while line:
                if comment:
                    end = line.find("-->")
                    if end < 0:
                        break
                    line, comment = line[end + 3:], False
                else:
                    start = line.find("<!--")
                    if start < 0:
                        visible += line
                        break
                    visible += line[:start]
                    line, comment = line[start + 4:], True
            lines.append(visible)
    return sum(bool(re.search(r"^\s*\*\*Status:\*\*\s*" + status + r"\s*$", entry, re.M | re.I))
               for entry in re.split(r"^##\s+.+$", "\n".join(lines), flags=re.M)[1:])


def journal_count(project: Path, name: str, status: str) -> int | None:
    try:
        path = files.resolve(project, name)
        with path.open("rb") as stream:
            raw = stream.read(JOURNAL_MAX + 1)
        if len(raw) > JOURNAL_MAX:
            return None
        return count_entries(raw.decode("utf-8-sig"), status)
    except FileNotFoundError:
        return 0
    except (OSError, UnicodeError, Refused):
        return None


def read(project: Path, *, now=time.time) -> dict:
    result = {"findings": journal_count(project, "FINDINGS.md", "open"),
              "ideas": journal_count(project, "IDEAS.md", "proposed"),
              "changed": None, "unpushed": None, "modified": None, "git": "error", "github": None,
              "remotes": []}
    try:
        result["modified"] = project.stat().st_mtime
        # Наличие своего .git проверяется до запуска: чужой родительский репозиторий не учитывается.
        if not (project / ".git").exists():
            result["git"] = "not_git"
            return {**result, "checked_at": now()}
        out = gitinfo.run(project, ["status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all"],
                          timeout=GIT_SEC)
        if out.returncode != 0:
            return {**result, "checked_at": now()}
        state = gitinfo.parse(out.stdout, truncate=False)
        result.update(git="ok", changed=len(state["files"]), modified=None)
        commit = gitinfo.run(project, ["log", "-1", "--format=%ct"], timeout=GIT_SEC)
        if commit.returncode == 0 and commit.stdout.strip():
            result["modified"] = int(commit.stdout.strip())
        for item in state["files"]:
            try:
                path = files.resolve(project, item["path"])
                stamp = (path if path.exists() else path.parent).stat().st_mtime
                result["modified"] = max(result["modified"] or 0, stamp)
            except (OSError, Refused):
                pass
        if state["upstream"]:
            result["unpushed"] = state["ahead"]
        elif b"# branch.oid (initial)\0" in out.stdout:
            result["unpushed"] = 0  # В новом репозитории ещё нет коммитов.
        else:
            unpublished = gitinfo.run(project, ["rev-list", "--count", "HEAD", "--not", "--remotes"],
                                      timeout=GIT_SEC)
            if unpublished.returncode == 0:
                result["unpushed"] = int(unpublished.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass  # Неизвестное значение остаётся null, а не превращается в ноль.
    if result["git"] == "ok":
        oid = re.search(rb"(?:^|\0)# branch.oid ([0-9a-f]{40}|[0-9a-f]{64})\0", out.stdout)
        head = oid[1].decode("ascii") if oid else None
        result["github"] = github_status.read(project, state["branch"], head)
        result["remotes"] = remote_status.read(project, state["branch"], head)
    return {**result, "checked_at": now()}
