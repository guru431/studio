from urllib.parse import quote

import pytest

import claude_code
import state
from test_code import ALPHA, setup


def test_file_operation_capability_pin_relay_and_pdf_cache(studio):
    aid, machine, link = setup(studio, pin="2468", opened=False)
    url = f"/api/code/agents/{aid}/projects/{ALPHA}/files/operate"
    body = {"operation": "create_file", "path": "new.txt"}
    assert studio.c.post(url, json=body).json()["detail"]["code"] == "unsupported"
    link.hello["file_operations"] = True
    assert studio.c.post(url, json=body).json()["detail"]["code"] == "closed"
    assert studio.c.post(f"/api/code/agents/{aid}/open", json={"pin": "2468"}).status_code == 204
    assert studio.c.post(url, json=body).status_code == 204
    assert machine.requests[-1]["op"] == "file_operation"
    assert machine.requests[-1]["path"] == "new.txt"
    assert studio.c.post(url, json=body).json()["detail"]["code"] == "exists"
    assert studio.c.post(url, json={"operation": "move", "path": "new.txt", "destination": "renamed.txt"}).status_code == 204
    assert "renamed.txt" in machine.projects["alpha"]["files"]
    digest = next(iter(machine.open_sessions))
    uid = state.DB.execute("SELECT id FROM users WHERE login = 'owner'").fetchone()[0]
    claude_code.PDFS[uid] = ((aid, ALPHA, "renamed.txt", digest), b"pdf", 1, 0)
    assert studio.c.post(url, json={"operation": "delete", "path": "renamed.txt"}).status_code == 204
    assert uid not in claude_code.PDFS
    assert "renamed.txt" not in machine.projects["alpha"]["files"]


@pytest.mark.parametrize("body", [
    {"operation": "delete", "path": ""}, {"operation": "delete", "path": "../x"},
    {"operation": "move", "path": "x", "destination": "../y"},
    {"operation": "move", "path": "x"}, {"operation": "move", "path": "x", "destination": ""},
    {"operation": "delete", "path": "x", "destination": "y"},
    {"operation": "unknown", "path": "x"},
])
def test_bad_operations_do_not_reach_bridge(studio, body):
    aid, machine, link = setup(studio)
    link.hello["file_operations"] = True
    count = len(machine.requests)
    assert studio.c.post(f"/api/code/agents/{aid}/projects/{ALPHA}/files/operate", json=body).status_code == 422
    assert len(machine.requests) == count


def test_upload_capability_header_path_no_overwrite_and_replace(studio):
    aid, machine, link = setup(studio)
    url = f"/api/code/agents/{aid}/projects/{ALPHA}/files/upload"
    name = {"x-studio-name": quote("docs/отчёт.bin")}
    machine.projects["alpha"]["files"]["docs/old.txt"] = b"old"
    assert studio.c.put(url, content=b"\x00\x01", headers=name).json()["detail"]["code"] == "unsupported"
    link.hello["file_upload"] = True
    count = len(machine.requests)
    for bad in ({}, {"x-studio-name": ""}, {"x-studio-name": quote("../x")}, {"x-studio-name": "%FF"}):
        assert studio.c.put(url, content=b"x", headers=bad).json()["detail"]["code"] == "path"
    assert len(machine.requests) == count
    assert studio.c.put(url, content=b"\x00\x01", headers=name).status_code == 204
    assert machine.projects["alpha"]["files"]["docs/отчёт.bin"] == b"\x00\x01"
    assert machine.requests[-1]["replace"] is False
    r = studio.c.put(url, content=b"new", headers=name)
    assert r.status_code == 409 and r.json()["detail"]["code"] == "exists"
    assert machine.projects["alpha"]["files"]["docs/отчёт.bin"] == b"\x00\x01"
    assert studio.c.put(url, content=b"new", headers={**name, "x-studio-replace": "1"}).status_code == 204
    assert machine.projects["alpha"]["files"]["docs/отчёт.bin"] == b"new"
    count = len(machine.requests)
    r = studio.c.put(url, content=b"x" * (claude_code.RAW_MAX + 1), headers=name)
    assert r.status_code == 413 and len(machine.requests) == count


def test_download_sends_attachment_with_the_name_in_the_header_only(studio):
    aid, machine, _ = setup(studio)
    machine.projects["alpha"]["files"]["build/отчёт 1.zip"] = b"PK\x03\x04data"
    url = f"/api/code/agents/{aid}/projects/{ALPHA}/files/download"
    r = studio.c.post(url, json={"path": "build/отчёт 1.zip"})
    assert r.status_code == 200 and r.content == b"PK\x03\x04data"
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"] == (
        "attachment; filename=\"_____ 1.zip\"; filename*=UTF-8''%D0%BE%D1%82%D1%87%D1%91%D1%82%201.zip")
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"
    assert studio.c.post(url, json={"path": ""}).json()["detail"]["code"] == "path"
    assert studio.c.post(url, json={"path": "missing.bin"}).status_code == 404
    machine.projects["alpha"]["files"]["big.bin"] = b"x" * (claude_code.RAW_MAX + 1)
    assert studio.c.post(url, json={"path": "big.bin"}).status_code == 413


def test_content_search_relays_only_well_formed_lines_inside_the_folder(studio):
    aid, machine, link = setup(studio)
    files = machine.projects["alpha"]["files"]
    files.update({"src/a.py": "x = 1\n# Привет\n".encode(), "src/b.bin": "\x00привет".encode(), "doc.md": "привет".encode()})
    url = f"/api/code/agents/{aid}/projects/{ALPHA}/files/search"
    body = {"path": "src", "query": " привет "}
    assert studio.c.post(url, json=body).json()["detail"]["code"] == "unsupported"
    link.hello["file_search"] = True
    assert studio.c.post(url, json=body).json() == {
        "items": [{"path": "src/a.py", "line": 2, "text": "# Привет"}], "complete": True, "more": False}
    assert machine.requests[-1]["query"] == "привет" and machine.requests[-1]["path"] == "src"
    machine._op_file_search = lambda req, events: {"items": [
        {"path": "src/ok.py", "line": 3, "text": "y" * 500}, {"path": "doc.md", "line": 1, "text": "out of folder"},
        {"path": "src/../x", "line": 1, "text": "bad"}, {"path": "src/z.py", "line": 0, "text": "bad line"},
        {"path": "src/w.py", "line": True, "text": "bool line"}, "junk"], "complete": False, "more": True}
    assert studio.c.post(url, json=body).json() == {
        "items": [{"path": "src/ok.py", "line": 3, "text": "y" * 400}], "complete": False, "more": True}
    count = len(machine.requests)
    for bad in ({"path": "../x", "query": "a"}, {"path": "", "query": "a\nb"}, {"path": "", "query": "a" * 201}):
        assert studio.c.post(url, json=bad).status_code == 422
    assert len(machine.requests) == count
