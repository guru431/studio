"""«Справка» (`help.py`): руководство Studio из docs/guide.ru.md и руководства MCP-серверов — ресурсы
`text/markdown` для пользователя (`audience` с `user`)."""
import re
import time

import help as help_module
from test_mcp_contracts import GUIDE, RESOURCES, empty_cache, member_with_server  # noqa: F401 — фикстура кэша


def test_help_lists_studio_guide_and_user_resources_only(studio):
    member, sid, seen = member_with_server(studio)

    listed = studio.c.get("/api/help")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    titles = [s["title"] for s in body["studio"]["sections"]]
    assert titles[0] == "Учётки и вход" and "Чат" in titles and "MCP в чате и разделах" in titles
    assert len({s["id"] for s in body["studio"]["sections"]}) == len(titles)
    assert body["servers"] == [{"sid": sid, "name": "Shared tools",
                                "resources": [{"uri": "studio-guide://aitools", "name": "Руководство aitools"}]}]

    guide = studio.c.get("/api/help/studio")
    assert guide.status_code == 200
    text = guide.json()["markdown"]
    assert text.startswith("## Учётки и вход") and "# Руководство пользователя Studio" not in text
    assert [line[3:] for line in text.splitlines() if line.startswith("## ")] == titles
    # относительные ссылки репозитория и якоря — простым текстом (якорь сменил бы раздел), внешние остаются ссылками
    assert not re.search(r"\]\((?!https?:|mailto:)", text)
    assert "](https://" in text

    read = studio.c.post("/api/help/server", json={"sid": sid, "uri": RESOURCES[0]["uri"]})
    assert read.status_code == 200, read.text
    assert read.json() == {"markdown": GUIDE}
    for uri in (RESOURCES[1]["uri"], RESOURCES[2]["uri"], "file:///etc/passwd"):
        refused = studio.c.post("/api/help/server", json={"sid": sid, "uri": uri})
        assert refused.status_code == 404, uri
    assert not [s for s in seen if s[0] == "resources/read" and s[3]["uri"] != RESOURCES[0]["uri"]]
    assert studio.c.post("/api/help/server", json={"sid": sid + 100, "uri": RESOURCES[0]["uri"]}).status_code == 404


def test_help_without_server_resources(studio):
    member_with_server(studio, resources=None)
    assert studio.c.get("/api/help").json()["servers"] == []


def test_help_topics_have_stable_unique_routes_and_no_repo_paths(studio):
    member_with_server(studio, resources=None)
    response = studio.c.get("/api/help/topics")
    assert response.status_code == 200, response.text
    topics = response.json()["topics"]
    keys = [topic["id"] for topic in topics]
    assert keys[0] == "start" and {"chat", "owui", "agents", "security"} <= set(keys)
    assert len(set(keys)) == len(keys)
    for topic in topics:
        assert topic["group"] and topic["title"] and topic["markdown"]
        assert not re.search(r"\.md\b|docs/|/etc/|\.py\b", topic["markdown"])


def test_help_guide_strips_intro_and_relative_links():
    text = help_module.studio_text("# Заголовок\n\n[Описание](../README.md) intro\n\n## Раздел\n"
                                   "См. [правила](../docs/publication.md), ![схема](img/a.png), "
                                   "[сайт](https://example.com) и [ниже](#раздел).\n")
    assert text == ("## Раздел\nСм. правила, схема, [сайт](https://example.com) и ниже.\n")


def test_help_local_links_are_linear_on_hostile_lines():
    """B2-I3: строка из одних `[` или `[a](` без закрывающих скобок разбиралась квадратично (40 000 знаков —
    десятки секунд на цикле событий: вся Studio стояла); теперь — доли секунды."""
    started = time.perf_counter()
    for line in ("[" * 20_000, "[a](" * 5_000, "](" * 10_000 + "x"):
        assert help_module.plain_links(line) == line
    assert time.perf_counter() - started < 0.5
