"""Навыки проекта «Агентов» на сервере (claude_code.project_skills, project_skill_text): список и текст через мост
0.48.0 (`hello.skills`); старому мосту просьб не шлём; участнику и наблюдателю — как чтение проекта."""
import logging

import claude_code
from conftest import make_user, signin
from test_code import ALPHA, detail, sent, setup, url
from test_project_access import share

ACCEPTANCE = {(".agents/skills", "acceptance"): {"description": "Приёмка", "text": "Сверь"}}


def skilled(studio):                                                               # мост 0.48.0
    aid, machine, link = setup(studio)
    link.hello["skills"] = machine.skills = True
    machine.projects["alpha"]["skills"] = dict(ACCEPTANCE)
    return aid, machine, link


def test_project_skills_are_listed_and_read_without_logging(studio, caplog):
    caplog.set_level(logging.DEBUG)
    aid, machine, _ = skilled(studio)
    assert studio.c.get("/api/agents").json()["items"][0]["skills"] is True
    assert studio.c.get(url(aid, "projects", ALPHA, "skills")).json() == {"truncated": False, "items": [
        {"name": "acceptance", "description": "Приёмка", "folder": ".agents/skills"}]}
    r = studio.c.post(url(aid, "projects", ALPHA, "skills", "text"), json={"name": "acceptance", "folder": ".agents/skills"})
    assert r.json() == {"text": "Сверь"} and all(w not in caplog.text for w in ("acceptance", "Приёмка", "Сверь"))
    assert sent(machine, "skill")[-1]["name"] == "acceptance" and sent(machine, "skill")[-1]["project"] == ALPHA
    missing = studio.c.post(url(aid, "projects", ALPHA, "skills", "text"), json={"name": "nope", "folder": ".claude/skills"})
    assert (missing.status_code, detail(missing)["code"]) == (404, "not_found") and "nope" not in caplog.text


def test_old_bridge_gets_no_skill_requests(studio):                              # Review Focus 1
    aid, machine, link = setup(studio)                                             # hello без skills — мост 0.47
    assert studio.c.get("/api/agents").json()["items"][0]["skills"] is False
    for r in (studio.c.get(url(aid, "projects", ALPHA, "skills")),
              studio.c.post(url(aid, "projects", ALPHA, "skills", "text"), json={"name": "a", "folder": ".agents/skills"})):
        assert (r.status_code, detail(r)["code"]) == (409, "unsupported")
    assert not sent(machine, "skills") and not sent(machine, "skill")


def test_bad_fields_and_odd_bridge_answers(studio):
    aid, machine, _ = skilled(studio)
    text_url = url(aid, "projects", ALPHA, "skills", "text")
    for body in ({"name": "../x", "folder": ".agents/skills"}, {"name": "a/b", "folder": ".agents/skills"},
                 {"name": "", "folder": ".agents/skills"}, {"name": "a", "folder": "docs"}, {"folder": ".agents/skills"}):
        assert studio.c.post(text_url, json=body).status_code == 422, body
    assert studio.c.get(url(aid, "projects", "x", "skills")).status_code == 422
    assert not sent(machine, "skill")

    rows = [{"name": "tmp-skill", "description": "x", "folder": "tmp"},
            {"name": "..", "description": "x", "folder": ".agents/skills"},
            {"name": "ok", "description": 5, "folder": ".claude/skills"},
            "строка",
            {"name": "long", "description": "д" * 500, "folder": ".agents/skills"}]
    rows += [{"name": f"s{i}", "description": "", "folder": ".agents/skills"} for i in range(150)]
    machine._op_skills = lambda req, events: {"items": rows, "truncated": False}
    got = studio.c.get(url(aid, "projects", ALPHA, "skills")).json()
    assert got["items"][0] == {"name": "ok", "description": "", "folder": ".claude/skills"}
    assert got["items"][1] == {"name": "long", "description": "д" * 200, "folder": ".agents/skills"}
    assert len(got["items"]) == 100 and got["truncated"] is True     # строк моста больше 100 — обрезано
    assert not any(i["name"] in ("tmp-skill", "..") for i in got["items"])

    machine._op_skill = lambda req, events: {"text": "x" * (claude_code.SKILL_TEXT_MAX + 1)}
    r = studio.c.post(text_url, json={"name": "acceptance", "folder": ".agents/skills"})
    assert (r.status_code, detail(r)["code"]) == (502, "bridge")
    machine._op_skill = lambda req, events: {"text": 5}
    r = studio.c.post(text_url, json={"name": "acceptance", "folder": ".agents/skills"})
    assert (r.status_code, detail(r)["code"]) == (502, "bridge")


def test_member_and_viewer_read_project_skills(studio):
    aid, machine, link, member, base = share(studio)
    link.hello["skills"] = machine.skills = True
    machine.projects["alpha"]["skills"] = dict(ACCEPTANCE)
    make_user(studio, "viewer")
    assert studio.c.post(base, json={"login": "viewer", "role": "viewer"}).status_code == 201
    for login, readonly in (("friend", False), ("viewer", True)):
        signin(studio, login)
        assert studio.c.get("/api/agents").json()["items"][0]["skills"] is True
        listed = studio.c.get(url(aid, "projects", ALPHA, "skills"))
        assert listed.status_code == 200 and listed.json()["items"][0]["name"] == "acceptance"
        request = sent(machine, "skills")[-1]
        assert request["shared_projects"] == [ALPHA] and request.get("shared_readonly", False) is readonly
        text = studio.c.post(url(aid, "projects", ALPHA, "skills", "text"),
                             json={"name": "acceptance", "folder": ".agents/skills"})
        assert text.status_code == 200 and text.json() == {"text": "Сверь"}
        request = sent(machine, "skill")[-1]
        assert request["shared_projects"] == [ALPHA] and request.get("shared_readonly", False) is readonly
