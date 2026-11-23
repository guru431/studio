"""Сторож паритета платформ (IDEAS 09:I9): код моста пишут и гоняют на Windows, а macOS и Linux проверяются
подменой `host.MACOS`/`host.LINUX` и `files.WINDOWS`. Здесь — `hello` каждой ОС против таблицы «`hello` по
платформам» `docs/architecture/agent-bridge.md` и обход зеркала с именами, которые запрещает только Windows;
имена путей и лимиты Claude на macOS — `test_files.py`, `test_mirror.py`, `test_subscription.py`."""

import os
import re
from pathlib import Path

import pytest

from studio_agent import host, mirror
from test_agent import make, root  # noqa: F401 — фикстура `root`: папка проектов с `demo`

DOC = Path(__file__).resolve().parents[2] / "docs" / "architecture" / "agent-bridge.md"
PLATFORMS = ("win32", "darwin", "linux")
ABSENT = object()


def _cell(text: str):
    """Ячейка таблицы → значение поля: «—» — поля нет, `true`/`false` — bool, иначе строка в обратных кавычках."""
    token = text.strip().split(" ")[0]
    if token == "—":
        return ABSENT
    value = token.strip("`")
    return {"true": True, "false": False}.get(value, value)


def _table() -> dict[str, dict]:
    text = DOC.read_text(encoding="utf-8")
    section = text.split("### `hello` по платформам", 1)[1].split("\n#", 1)[0]
    rows = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 4 and (m := re.fullmatch(r"`([a-z_]+)`", cells[0])):
            rows[m.group(1)] = dict(zip(PLATFORMS, map(_cell, cells[1:])))
    return rows


def _hello(platform, home, root, monkeypatch):  # noqa: F811
    monkeypatch.setattr(host, "MACOS", platform == "darwin")
    monkeypatch.setattr(host, "LINUX", platform == "linux")
    return make(home, root).hello()


def test_hello_of_every_platform_matches_the_document_table(home, root, monkeypatch):  # noqa: F811
    table = _table()
    assert set(table) == {"platform", "desktop", "terminal"}, "таблица «`hello` по платформам» не прочитана"
    hellos = {p: _hello(p, home, root, monkeypatch) for p in PLATFORMS}
    for platform, hello in hellos.items():
        for field, cells in table.items():
            expected = cells[platform]
            assert (field not in hello) if expected is ABSENT else hello.get(field) == expected, (platform, field)
    rest = [{k: v for k, v in hello.items() if k not in table} for hello in hellos.values()]
    assert rest[0] == rest[1] == rest[2], "поле `hello`, зависящее от ОС, — в таблицу документа"


FORBIDDEN_ON_WINDOWS = ["aux.c", "What?.md", "2026-11-20T10:11:12.log", "dot."]


def test_posix_mirror_scans_names_only_windows_forbids(tmp_path, monkeypatch):
    """На Linux и macOS `aux.c`, `What?.md` и время с двоеточиями — обычные файлы зеркала, не «пропущен». На Windows
    таких файлов не создать, поэтому без диска: `mirror.scan` пропускает ровно имена, которые отвергает
    `files._parts`, а тот с `files.WINDOWS = False` их принимает (с `True` — отвергает)."""
    for windows in (False, True):
        monkeypatch.setattr(mirror.files, "WINDOWS", windows)
        for name in FORBIDDEN_ON_WINDOWS:
            if windows:
                with pytest.raises(mirror.Refused):
                    mirror.files._parts(name)
            else:
                assert mirror.files._parts(name) == [name]
    monkeypatch.setattr(mirror.files, "WINDOWS", os.name == "nt")
    project = tmp_path / "project"
    project.mkdir()
    for name in ("plain.txt", "refused.txt"):
        (project / name).write_bytes(b"x")
    parts = mirror.files._parts

    def this_os(path):  # «эта ОС» не принимает одно из имён — как Windows `aux.c`
        if path == "refused.txt":
            raise mirror.Refused("invalid")
        return parts(path)

    monkeypatch.setattr(mirror.files, "_parts", this_os)
    entries = mirror.scan(project.resolve())["entries"]
    assert entries["refused.txt"] == mirror.SKIPPED and entries["plain.txt"][0] == "file"
