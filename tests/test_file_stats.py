import pytest

from test_code import ALPHA, setup


def test_file_stats_requires_capability_pin_and_owner(studio):
    aid, machine, link = setup(studio, pin="2468", opened=False)
    url = f"/api/code/agents/{aid}/projects/{ALPHA}/files/stats"
    assert studio.c.post(url, json={"path": ""}).json()["detail"]["code"] == "unsupported"
    link.hello["file_stats"] = True
    assert studio.c.post(url, json={"path": ""}).json()["detail"]["code"] == "closed"
    assert studio.c.post(f"/api/code/agents/{aid}/open", json={"pin": "2468"}).status_code == 204
    machine.projects["alpha"]["files"] = {"src/a.py": b"abc", "src/lib/b.py": b"12"}
    result = studio.c.post(url, json={"path": ""})
    assert result.status_code == 200
    assert result.json()["items"][0] == {"path": "", "size": 5, "files": 2, "folders": 2, "complete": True}
    assert machine.requests[-1]["op"] == "file_stats"
    assert studio.c.post(url, json={"path": "../outside"}).status_code == 422
    assert studio.c.post(url.replace(str(aid), str(aid + 1), 1), json={"path": ""}).status_code == 404
    assert studio.c.post(url.replace(ALPHA, "invalid"), json={"path": ""}).status_code == 422


@pytest.mark.parametrize("bad", [{"path": "../outside", "size": 0, "files": 0, "folders": 0},
                                  {"path": "", "size": True, "files": 0, "folders": 0}])
def test_file_stats_rejects_malformed_bridge_data(studio, bad):
    aid, machine, link = setup(studio)
    link.hello["file_stats"] = True
    machine.projects["alpha"]["file_statistics"] = {"items": [bad], "secret": "must not pass"}
    assert studio.c.post(f"/api/code/agents/{aid}/projects/{ALPHA}/files/stats", json={"path": ""}).status_code == 502
