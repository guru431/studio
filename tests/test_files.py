import base64
import ctypes
import hashlib
import os
import _winapi

import pytest

from studio_agent import errors, files, limits, projects


def _code(fn, *args):
    with pytest.raises(errors.Refused) as e:
        fn(*args)
    return e.value.code


def _boom(*args, **kwargs):
    raise AssertionError("обращение к диску")


def test_pid_is_the_head_of_sha256_of_the_folder_name():
    assert projects.pid("studio") == hashlib.sha256("studio".encode()).hexdigest()[:16]


def test_projects_skip_hidden_and_outside(tmp_path):
    root = tmp_path / "projects"; (root / "b").mkdir(parents=True); (root / "a").mkdir(); (root / ".git").mkdir()
    outside = tmp_path / "elsewhere"; outside.mkdir()
    _winapi.CreateJunction(str(outside), str(root / "jump"))
    assert [p["name"] for p in projects.list_projects(str(root))] == ["a", "b"]
    assert projects.list_projects(str(tmp_path / "нет")) == []


def test_projects_skip_hidden_and_system_attributes_and_plain_files(tmp_path):
    root = tmp_path / "projects"; (root / "ok").mkdir(parents=True); (root / "h").mkdir(); (root / "s").mkdir()
    (root / "file.txt").write_text("x")
    assert ctypes.windll.kernel32.SetFileAttributesW(str(root / "h"), 0x2)  # FILE_ATTRIBUTE_HIDDEN
    assert ctypes.windll.kernel32.SetFileAttributesW(str(root / "s"), 0x4)  # FILE_ATTRIBUTE_SYSTEM
    assert projects.list_projects(str(root)) == [{"pid": projects.pid("ok"), "name": "ok"}]
    assert projects.list_projects("") == []


def test_find_project_gives_the_real_path_or_not_found(tmp_path):
    root = tmp_path / "projects"; (root / "студия").mkdir(parents=True)
    got = projects.find_project(str(root), projects.pid("студия"))
    assert got == type(got)(os.path.realpath(root / "студия"))
    with pytest.raises(errors.Refused) as e:
        projects.find_project(str(root), "0" * 16)
    assert (e.value.code, e.value.message) == ("not_found", "нет такого проекта")


@pytest.mark.parametrize("bad", ["CON", "sub/nul.txt", "COM1.log", "a.txt:поток", "dir./x", "x ", "..", "a//b",
                                 "a\\b", "/abs", "a\x01b", "x" * 1025])
def test_bad_paths_are_invalid_before_the_disk(tmp_path, bad, monkeypatch):
    with monkeypatch.context() as m:
        m.setattr(os.path, "realpath", _boom)
        for name in ("stat", "lstat", "scandir"):
            m.setattr(os, name, _boom)
        with pytest.raises(errors.Refused) as e:
            files.resolve(tmp_path, bad)
    assert e.value.code == "invalid"


def test_the_empty_path_is_the_project_root(tmp_path):
    assert files.resolve(tmp_path, "") == type(tmp_path)(os.path.realpath(tmp_path))


def test_a_junction_out_of_the_project_is_outside_and_not_listed(tmp_path):
    proj = tmp_path / "p"; proj.mkdir(); out = tmp_path / "secret"; out.mkdir(); (out / "k.txt").write_text("x")
    _winapi.CreateJunction(str(out), str(proj / "link"))
    assert files.list_dir(proj, "")["items"] == []
    with pytest.raises(errors.Refused) as e:
        files.read_text(proj, "link/k.txt")
    assert e.value.code == "outside"


def test_list_dir_gives_folders_then_files_by_name(tmp_path):
    (tmp_path / "z").mkdir(); (tmp_path / "a").mkdir(); (tmp_path / "b.txt").write_bytes(b"12345")
    (tmp_path / "a" / "in.txt").write_text("x")
    items = files.list_dir(tmp_path, "")["items"]
    assert [(i["name"], i["dir"]) for i in items] == [("a", True), ("z", True), ("b.txt", False)]
    assert items[2]["size"] == 5 and isinstance(items[2]["modified"], int)
    assert [i["name"] for i in files.list_dir(tmp_path, "a")["items"]] == ["in.txt"]
    assert "truncated" not in files.list_dir(tmp_path, "")


def test_list_dir_cuts_at_files_max(tmp_path, monkeypatch):
    for n in range(4):
        (tmp_path / f"{n}.txt").write_text("x")
    monkeypatch.setattr(limits, "FILES_MAX", 3)
    got = files.list_dir(tmp_path, "")
    assert [i["name"] for i in got["items"]] == ["0.txt", "1.txt", "2.txt"] and got["truncated"] is True


def test_list_dir_skips_an_entry_that_cannot_be_stat(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("x"); (tmp_path / "gone.txt").write_text("x")
    real_stat = os.stat

    def stat(p, *args, **kwargs):
        if str(p).endswith("gone.txt"):
            raise PermissionError(5, "нет прав")
        return real_stat(p, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)
    assert [i["name"] for i in files.list_dir(tmp_path, "")["items"]] == ["a.txt"]


def test_list_dir_of_a_file_or_nothing_is_no_such_folder(tmp_path):
    (tmp_path / "f.txt").write_text("x")
    for path in ("f.txt", "нет"):
        with pytest.raises(errors.Refused) as e:
            files.list_dir(tmp_path, path)
        assert (e.value.code, e.value.message) == ("not_found", "нет такой папки")


def test_read_of_a_folder_or_nothing_is_no_such_file(tmp_path):
    (tmp_path / "d").mkdir()
    for path in ("d", "нет.txt", ""):
        with pytest.raises(errors.Refused) as e:
            files.read_text(tmp_path, path)
        assert (e.value.code, e.value.message) == ("not_found", "нет такого файла")


def test_save_keeps_bom_and_crlf_and_checks_the_version(tmp_path):
    f = tmp_path / "a.txt"; f.write_bytes(b"\xef\xbb\xbfone\r\ntwo")
    got = files.read_text(tmp_path, "a.txt")
    assert got["text"] == "﻿one\r\ntwo"
    assert got["version"] == hashlib.sha256(b"\xef\xbb\xbfone\r\ntwo").hexdigest()
    new = files.save_text(tmp_path, "a.txt", got["text"] + "\r\nthree", got["version"])
    assert f.read_bytes() == b"\xef\xbb\xbfone\r\ntwo\r\nthree"
    with pytest.raises(errors.Refused) as e:
        files.save_text(tmp_path, "a.txt", "x", got["version"])
    assert e.value.code == "conflict" and e.value.extra["version"] == new["version"]


def test_save_of_a_new_file_is_not_found_and_writes_nothing(tmp_path):
    assert _code(files.save_text, tmp_path, "new.txt", "x", hashlib.sha256(b"").hexdigest()) == "not_found"
    assert not (tmp_path / "new.txt").exists()


def test_save_of_a_text_over_2_mib_is_too_large(tmp_path):
    f = tmp_path / "a.txt"; f.write_bytes(b"x")
    with pytest.raises(errors.Refused) as e:
        files.save_text(tmp_path, "a.txt", "я" * (limits.TEXT_MAX // 2 + 1), hashlib.sha256(b"x").hexdigest())
    assert (e.value.code, e.value.message) == ("too_large", "файл больше 2 МиБ")
    assert f.read_bytes() == b"x"


def test_binary_and_size_limits(tmp_path):
    (tmp_path / "n.bin").write_bytes(b"a\0b"); (tmp_path / "w.txt").write_bytes(b"\xff\xfe")
    (tmp_path / "big.txt").write_bytes(b"a" * (2 * 1024 * 1024 + 1))
    assert _code(files.read_text, tmp_path, "n.bin") == "binary"
    assert _code(files.read_text, tmp_path, "w.txt") == "binary"
    assert _code(files.read_text, tmp_path, "big.txt") == "too_large"
    assert files.read_raw(tmp_path, "big.txt")["size"] == 2 * 1024 * 1024 + 1


def test_raw_gives_base64_and_refuses_over_10_mib(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF\0\xff")
    got = files.read_raw(tmp_path, "a.pdf")
    assert got == {"data": base64.b64encode(b"%PDF\0\xff").decode("ascii"), "size": 6,
                   "version": hashlib.sha256(b"%PDF\0\xff").hexdigest()}
    (tmp_path / "huge.pdf").write_bytes(b"a" * (limits.RAW_MAX + 1))
    with pytest.raises(errors.Refused) as e:
        files.read_raw(tmp_path, "huge.pdf")
    assert (e.value.code, e.value.message) == ("too_large", "файл больше 10 МиБ")
