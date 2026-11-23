"""Перенос моста на новый компьютер: `export-chats` / `import-chats` (`transfer`, IDEAS 09:I8)."""

import json
import zipfile

import pytest

from studio_agent import __main__ as cli, store, transfer


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), "utf-8")


@pytest.fixture
def machines(tmp_path):
    old, new = tmp_path / "old-home", tmp_path / "new-home"
    old_root, new_root = tmp_path / "old-projects", tmp_path / "new-projects"
    for path in (old_root / "demo", new_root / "demo", old, new):
        path.mkdir(parents=True)
    store.save_config(old, store.Config(agent_id=3, name="Старый", projects_roots=[str(old_root)]))
    store.save_config(new, store.Config(agent_id=9, name="Новый", projects_roots=[str(new_root)]))
    return old, new, old_root.resolve(), new_root.resolve()


def test_round_trip_carries_mappings_and_choices_but_no_key_or_pin(machines, tmp_path):
    old, new, old_root, new_root = machines
    write(old / "codex-chats.json", {"c1": {"project": str(old_root / "demo"), "thread": "t1"},
                                     "c2": {"project": str(old_root / "gone"), "thread": "t2"}})
    write(old / "opencode" / "choices.json", {"c3": {"model": "m", "effort": "high"}})
    write(old / "choices.json", {"c1": {"model": "opus"}, "c9": {"model": "sonnet"}})
    for secret in ("key.bin", "pin.bin"):
        (old / secret).write_bytes(b"secret-blob")
    write(new / "choices.json", {"c9": {"model": "haiku"}})        # своё на новом компьютере сильнее
    archive = tmp_path / "transfer.zip"
    assert transfer.export(old, archive) == 3
    with zipfile.ZipFile(archive) as z:
        names = set(z.namelist())
        assert names == {"manifest.json", "codex-chats.json", "opencode/choices.json", "choices.json"}
        assert all(b"secret-blob" not in z.read(n) for n in names)
    assert transfer.import_(new, archive) == 4
    codex = json.loads((new / "codex-chats.json").read_text("utf-8"))
    assert codex["c1"] == {"project": str(new_root / "demo"), "thread": "t1"}       # путь — на корень нового
    assert codex["c2"]["project"] == str(old_root / "gone")                          # папки нет — как было
    assert json.loads((new / "choices.json").read_text("utf-8")) == {"c1": {"model": "opus"}, "c9": {"model": "haiku"}}
    assert json.loads((new / "opencode" / "choices.json").read_text("utf-8")) == {"c3": {"model": "m", "effort": "high"}}
    assert not (new / "key.bin").exists() and not (new / "pin.bin").exists()
    assert store.load_config(new).agent_id == 9                                       # config.json не трогается
    assert transfer.import_(new, archive) == 0                                         # повтор ничего не добавит


def test_windows_paths_move_to_the_new_root_case_insensitively(machines, tmp_path):
    _, new, _, new_root = machines
    archive = tmp_path / "from-windows.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("manifest.json", json.dumps({"format": 1, "platform": "win32", "roots": ["C:\\Users\\Old\\Projects"]}))
        z.writestr("opencode-chats.json", json.dumps({"c1": {"project": "c:\\users\\old\\projects\\demo",
                                                             "session": "s1", "studio": True}}))
        z.writestr("key.bin", b"x")
        z.writestr("../evil.json", b"{}")
    assert transfer.import_(new, archive) == 1
    data = json.loads((new / "opencode-chats.json").read_text("utf-8"))
    assert data == {"c1": {"project": str(new_root / "demo"), "session": "s1", "studio": True}}
    assert not (new / "key.bin").exists() and not (new.parent / "evil.json").exists()


@pytest.mark.parametrize("content", [None, {"format": 2}, "not json"])
def test_foreign_archives_are_refused_without_writing(machines, tmp_path, content):
    _, new, _, _ = machines
    archive = tmp_path / "foreign.zip"
    with zipfile.ZipFile(archive, "w") as z:
        if content is not None:
            z.writestr("manifest.json", content if isinstance(content, str) else json.dumps(content))
        z.writestr("choices.json", "{}")
    with pytest.raises(transfer.TransferError):
        transfer.import_(new, archive)
    assert not (new / "choices.json").exists()


def test_console_commands_export_and_import(machines, tmp_path, monkeypatch, capsys):
    old, new, _, _ = machines
    write(old / "choices.json", {"c1": {"model": "opus"}})
    archive = tmp_path / "t.zip"
    monkeypatch.setenv("STUDIO_AGENT_HOME", str(old))
    assert cli.main(["export-chats", str(archive)]) == 0
    monkeypatch.setenv("STUDIO_AGENT_HOME", str(new))
    assert cli.main(["import-chats", str(archive)]) == 0
    assert json.loads((new / "choices.json").read_text("utf-8")) == {"c1": {"model": "opus"}}
    (tmp_path / "junk.zip").write_bytes(b"not a zip")
    assert cli.main(["import-chats", str(tmp_path / "junk.zip")]) == 1
    out = capsys.readouterr().out
    assert "Сохранено файлов: 1" in out and "Добавлено записей: 1" in out and "не архив переноса" in out
