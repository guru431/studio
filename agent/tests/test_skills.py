"""Навыки проекта (протокол §26, мост 0.48.0): список из `.agents/skills` и `.claude/skills` без текста, текст одного
навыка; пределы, неверные поля, ссылки за проект, доступ участника и наблюдателя, журнал без имён и текстов."""
import logging

import pytest

from platform_fs import create_directory_link
from studio_agent import limits, projects, skills
from studio_agent.errors import Refused
from test_agent import agent, opened, req, root  # noqa: F401 — фикстуры

HEADED = '---\nname: x\ndescription: "Приёмка — сверить"\ndisable-model-invocation: true\n---\n\nТекст навыка\n'


def put(project, folder, name, text):
    path = project / folder / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
    return path


def refused(call):
    with pytest.raises(Refused) as e:
        call()
    return e.value.code


def test_list_reads_both_folders_without_text(tmp_path):
    p = tmp_path / "demo"
    put(p, ".agents/skills", "acceptance", HEADED)
    put(p, ".claude/skills", "review", "# Ревью\n\nСмотри diff")
    put(p, ".agents/skills", "плохое имя", "x")
    (p / ".agents/skills/empty").mkdir()        # оба — мимо списка
    assert skills.list_skills(p) == {"truncated": False, "items": [
        {"name": "acceptance", "description": "Приёмка — сверить", "folder": ".agents/skills"},
        {"name": "review", "description": "Ревью", "folder": ".claude/skills"}]}
    assert skills.read_skill(p, "acceptance", ".agents/skills") == {"text": "Текст навыка"}
    assert skills.read_skill(p, "review", ".claude/skills") == {"text": "# Ревью\n\nСмотри diff"}


def test_list_without_folders_is_empty(tmp_path):
    assert skills.list_skills(tmp_path) == {"items": [], "truncated": False}


def test_head_reads_folded_and_plain_descriptions():
    assert skills.head("﻿---\r\ndescription: >\r\n  Первая\r\n  вторая\r\nname: x\r\n---\r\nТело") == (
        "Первая вторая", "Тело")
    assert skills.head("---\ndescription: просто текст\n---\n") == ("просто текст", "")
    assert skills.head("\n\n## Заголовок\nтекст") == ("Заголовок", "\n\n## Заголовок\nтекст")
    assert skills.head("---\nбез конца\n") == ("без конца", "---\nбез конца\n")


def test_limits_bad_fields_and_links(tmp_path):
    p = tmp_path / "demo"
    for i in range(limits.SKILLS_MAX):
        put(p, ".agents/skills", f"s{i:03}", f"Навык {i}")
    listed = skills.list_skills(p)
    assert len(listed["items"]) == limits.SKILLS_MAX == 100 and listed["truncated"] is False
    assert listed["items"][-1]["name"] == "s099"
    put(p, ".claude/skills", "overflow", "Последний навык")
    assert skills.list_skills(p) == {**listed, "truncated": True}

    q = tmp_path / "other"
    put(q, ".claude/skills", "big", b"x" * (limits.SKILL_MAX + 1))
    put(q, ".claude/skills", "zero", b"text\0more")
    put(q, ".claude/skills", "long", "---\ndescription: " + "д" * 500 + "\n---\nТекст")
    put(q, ".claude/skills", "blank", "---\ndescription: пусто\n---\n\n  \n")
    listed = {i["name"]: i for i in skills.list_skills(q)["items"]}
    assert "big" not in listed and "zero" not in listed
    assert len(listed["long"]["description"]) == skills.DESCRIPTION_MAX == 200
    assert listed["long"]["description"].endswith("…")
    assert refused(lambda: skills.read_skill(q, "big", ".claude/skills")) == "too_large"
    assert refused(lambda: skills.read_skill(q, "zero", ".claude/skills")) == "binary"
    assert refused(lambda: skills.read_skill(q, "blank", ".claude/skills")) == "invalid"
    for name, folder in (("../x", ".agents/skills"), ("acceptance", "docs"), ("", ".claude/skills")):
        assert refused(lambda: skills.read_skill(q, name, folder)) == "invalid"
    with pytest.raises(Refused) as e:
        skills.read_skill(q, "none", ".claude/skills")
    assert (e.value.code, e.value.message) == ("not_found", "нет такого навыка")

    source = q / "docs" / "linked"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("Внутри проекта", encoding="utf-8")
    create_directory_link(source, q / ".claude/skills/linked")
    away = tmp_path / "outside" / "away"
    away.mkdir(parents=True)
    (away / "SKILL.md").write_text("Снаружи", encoding="utf-8")
    create_directory_link(away, q / ".claude/skills/away")
    listed = {i["name"]: i for i in skills.list_skills(q)["items"]}
    assert listed["linked"]["description"] == "Внутри проекта" and "away" not in listed
    assert skills.read_skill(q, "linked", ".claude/skills") == {"text": "Внутри проекта"}
    assert refused(lambda: skills.read_skill(q, "away", ".claude/skills")) == "outside"


def test_list_distinguishes_invalid_utf8_from_a_character_split_at_head(tmp_path):
    title = b"# Review\n"
    prefix = title + b"x" * (skills.HEAD - len(title) - 2)
    put(tmp_path, ".agents/skills", "invalid-byte", prefix + b"\xffrest")
    put(tmp_path, ".agents/skills", "invalid-continuation", prefix + b"\xe2xafter")
    put(tmp_path, ".agents/skills", "ends-incomplete", prefix + b"\xe2\x82")
    put(tmp_path, ".agents/skills", "split", prefix + "€".encode() + b"rest")
    assert skills.list_skills(tmp_path) == {"truncated": False, "items": [
        {"name": "split", "description": "Review", "folder": ".agents/skills"}]}


async def test_skills_through_agent(agent, root, caplog):
    caplog.set_level(logging.DEBUG)
    assert agent.hello()["skills"] is True and agent.hello()["limits"] == {"images": 12, "attachments": 12}
    put(root / "demo", ".agents/skills", "acceptance", HEADED)
    pid = projects.pid("demo")
    assert (await agent.handle(req("skills", project=pid)))[0]["code"] == "closed"
    await opened(agent)
    listed = (await agent.handle(req("skills", project=pid)))[0]
    assert listed["ok"] and listed["data"]["items"][0]["name"] == "acceptance"
    one = (await agent.handle(req("skill", project=pid, name="acceptance", folder=".agents/skills")))[0]
    assert one["ok"] and one["data"] == {"text": "Текст навыка"}
    for shared in ({"shared_projects": [pid]}, {"shared_projects": [pid], "shared_readonly": True}):
        assert (await agent.handle(req("skills", session="5" * 32, project=pid, **shared)))[0]["ok"]
        res = (await agent.handle(req("skill", session="5" * 32, project=pid, name="acceptance",
                                      folder=".agents/skills", **shared)))[0]
        assert res["ok"] and res["data"]["text"] == "Текст навыка"
    for bad in ({"name": "../x", "folder": ".agents/skills"}, {"name": "acceptance", "folder": "docs"},
                {"name": "acceptance"}):
        assert (await agent.handle(req("skill", project=pid, **bad)))[0]["code"] == "invalid"
    missing = (await agent.handle(req("skill", project=pid, name="nope", folder=".claude/skills")))[0]
    assert missing["code"] == "not_found"
    assert "acceptance" not in caplog.text and "Текст навыка" not in caplog.text and "nope" not in caplog.text
