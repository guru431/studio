from test_code import ALPHA, setup
import pytest


def test_project_summary_capability_pin_ownership_and_filtered_response(studio):
    aid, machine, link = setup(studio, pin="2468", opened=False)
    path = f"/api/code/agents/{aid}/projects/{ALPHA}/summary"
    assert studio.c.get(path).json()["detail"]["code"] == "unsupported"
    link.hello["project_summary"] = True
    assert studio.c.get(path).json()["detail"]["code"] == "closed"
    assert studio.c.post(f"/api/code/agents/{aid}/open", json={"pin": "2468"}).status_code == 204
    machine.projects["alpha"]["summary"] = {"findings": 4, "ideas": 2, "changed": 3, "unpushed": 1,
        "modified": 100, "checked_at": 200, "git": "ok", "secret": "must not pass"}
    assert studio.c.get(path).json() == {"findings": 4, "ideas": 2, "changed": 3, "unpushed": 1,
                                       "modified": 100, "checked_at": 200, "git": "ok"}
    assert machine.requests[-1]["op"] == "project_summary" and machine.requests[-1]["project"] == ALPHA
    assert studio.c.get(path.replace(str(aid), str(aid + 1), 1)).status_code == 404
    assert studio.c.get(path.replace(ALPHA, "invalid")).status_code == 422


def test_project_summary_unknown_values_do_not_turn_into_zero(studio):
    aid, machine, link = setup(studio)
    link.hello["project_summary"] = True
    machine.projects["alpha"]["summary"] = {"findings": True, "ideas": -1, "changed": "2", "unpushed": None,
                                            "modified": True, "checked_at": "today", "git": "garbage"}
    assert studio.c.get(f"/api/code/agents/{aid}/projects/{ALPHA}/summary").json() == {
        "findings": None, "ideas": None, "changed": None, "unpushed": None,
        "modified": None, "checked_at": None, "git": "error"}


@pytest.mark.parametrize("gh, expected", [
    (None, None),
    ({"state": "behind", "behind": 4, "ahead": 0, "url": "secret"}, {"state": "behind", "behind": 4, "ahead": 0}),
    ({"state": "current", "behind": 0, "ahead": 0}, {"state": "current", "behind": 0, "ahead": 0}),
    ({"state": "missing"}, {"state": "missing", "behind": None, "ahead": None}),
    ({"state": "current", "behind": True, "ahead": 0}, {"state": "unknown", "behind": None, "ahead": None}),
    ({"state": "behind", "behind": -2, "ahead": "0"}, {"state": "unknown", "behind": None, "ahead": None}),
    ({"state": ["current"]}, {"state": "unknown", "behind": None, "ahead": None}),
])
def test_project_summary_github_counts_are_filtered_and_validated(studio, gh, expected):
    aid, machine, link = setup(studio)
    link.hello["project_summary"] = True
    machine.projects["alpha"]["summary"] = {"github": gh}
    assert studio.c.get(f"/api/code/agents/{aid}/projects/{ALPHA}/summary").json()["github"] == expected
