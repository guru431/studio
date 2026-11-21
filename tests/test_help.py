"""«Справка» (`help.py`): руководство Studio из docs/guide.ru.md и руководства MCP-серверов — ресурсы
`text/markdown` для пользователя (`audience` с `user`)."""
import re

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


def test_help_guide_strips_intro_and_relative_links():
    text = help_module.studio_text("# Заголовок\n\n[Описание](../README.md) intro\n\n## Раздел\n"
                                   "См. [правила](../docs/publication.md), ![схема](img/a.png), "
                                   "[сайт](https://example.com) и [ниже](#раздел).\n")
    assert text == ("## Раздел\nСм. правила, схема, [сайт](https://example.com) и ниже.\n")
