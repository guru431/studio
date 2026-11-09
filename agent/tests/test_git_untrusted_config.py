"""Скопированный проект с чужим `.git/config`: фоновые чтения Git моста не исполняют его команды.

Маркер-команда (`touch`) в ключах, которые git исполняет при `status`/`log`: `core.fsmonitor`, драйверы фильтров
(`clean`, `process`, в том числе через `include` и в конфиге подмодуля), `gpg.program` при `log.showSignature`."""

import os
import subprocess

import pytest

from studio_agent import gitinfo, project_summary, worktrees

pytestmark = pytest.mark.integration

ID = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false"]


def git(repo, *args, data=None):
    result = subprocess.run(["git", "-C", str(repo), *ID, *args], capture_output=True, input=data, timeout=20)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result.stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    path = tmp_path / "demo"
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "a.txt").write_text("a\n")
    (path / ".gitattributes").write_text("* filter=evil\n")
    git(path, "add", "-A")
    git(path, "commit", "-qm", "one")
    return path


def marker(tmp_path, name):
    return tmp_path / f"marker-{name}"


def poison(repo, text):
    with open(repo / ".git" / "config", "a", encoding="utf-8") as f:
        f.write(text)


def stale(repo, path="a.txt"):
    """Скопированная папка: отметки времени файлов не совпадают с индексом — git перечитывает их через фильтр.
    Каждый раз новое время: обычный `git status` запоминает прочитанное в индексе."""
    stamp = os.stat(repo / path).st_mtime - 3600
    os.utime(repo / path, (stamp, stamp))


def readers(repo, *, worktree_list=True):
    gitinfo.status(repo)
    project_summary.read(repo, local=True)   # сводка панели проектов (GitHub — отдельной просьбой)
    if worktree_list:
        worktrees.list_worktrees(repo, repo.parent)


def test_fsmonitor_of_the_repository_is_not_run(repo, tmp_path):
    hit = marker(tmp_path, "fsmonitor")
    poison(repo, f'[core]\n  fsmonitor = "touch {hit.as_posix()}"\n')
    subprocess.run(["git", "-C", str(repo), "status"], capture_output=True)
    assert hit.exists()                                       # опасность настоящая: обычный git её исполняет
    hit.unlink()
    readers(repo)
    assert not hit.exists()


@pytest.mark.parametrize("key", ["clean", "process"])
def test_filter_driver_of_the_repository_is_not_run_even_through_include(repo, tmp_path, key):
    hit = marker(tmp_path, key)
    (tmp_path / "included.cfg").write_text(f'[filter "evil"]\n  {key} = "touch {hit.as_posix()}; cat"\n'
                                           f"  required = true\n")
    poison(repo, "[include]\n  path = ../../included.cfg\n")
    stale(repo)
    subprocess.run(["git", "-C", str(repo), "status"], capture_output=True)
    assert hit.exists()
    hit.unlink()
    stale(repo)
    assert gitinfo.status(repo)["files"] == []               # статус читается, файл не помечен изменённым
    stale(repo)
    assert project_summary.read(repo, local=True)["git"] == "ok"
    assert not hit.exists()


def test_filter_driver_of_a_submodule_is_not_run(repo, tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    git(child, "init", "-q", "-b", "main")
    (child / "b.txt").write_text("b\n")
    (child / ".gitattributes").write_text("* filter=evil\n")
    git(child, "add", "-A")
    git(child, "commit", "-qm", "child")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", child.as_posix(), "sub")
    git(repo, "commit", "-qm", "sub")
    hit = marker(tmp_path, "sub")
    with open(repo / ".git" / "modules" / "sub" / "config", "a", encoding="utf-8") as f:
        f.write(f'[filter "evil"]\n  clean = "touch {hit.as_posix()}; cat"\n')
    stale(repo, "sub/b.txt")
    subprocess.run(["git", "-C", str(repo), "status"], capture_output=True)
    assert hit.exists()
    hit.unlink()
    stale(repo, "sub/b.txt")
    readers(repo, worktree_list=False)
    assert not hit.exists()


def test_signature_program_of_the_repository_is_not_run(repo, tmp_path):
    hit = marker(tmp_path, "gpg")
    program = tmp_path / "fake-gpg.sh"
    program.write_text(f"#!/bin/sh\ntouch {hit.as_posix()}\nexit 1\n")
    poison(repo, f'[log]\n  showSignature = true\n[gpg]\n  program = "{program.as_posix()}"\n')
    head = git(repo, "cat-file", "commit", "HEAD").decode()
    header, _, message = head.partition("\n\n")
    signature = "gpgsig -----BEGIN PGP SIGNATURE-----\n \n iQ==\n -----END PGP SIGNATURE-----"
    signed = git(repo, "hash-object", "-t", "commit", "-w", "--stdin",
                 data=f"{header}\n{signature}\n\n{message}".encode()).decode().strip()
    git(repo, "update-ref", "HEAD", signed)
    subprocess.run(["git", "-C", str(repo), "log", "-1", "--format=%ct"], capture_output=True)
    assert hit.exists()
    hit.unlink()
    assert project_summary.read(repo, local=True)["modified"] is not None
    assert not hit.exists()
