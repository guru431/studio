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
