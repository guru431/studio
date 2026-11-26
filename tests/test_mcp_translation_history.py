"""MCP text translations use the same history and sharing lifecycle as gateway translations."""
import asyncio
from urllib.parse import urlsplit

import pytest

import mcp_sections
import state
from test_mcp import add
from test_mcp_sections import JOB, service


@pytest.mark.parametrize("background", [False, True])
def test_mcp_text_translation_has_one_ordinary_record(studio, background):
    sid = add(studio)
    results = {"text_translate": {"id": JOB}, "job_result": {"result": {"text": "Translated text"}}}
    service(studio.gw, catalog_results=results if background else None)
    response = studio.c.post(f"/api/mcp-tools/translate/servers/{sid}/run", json={
        "tool": "text_translate", "record": True,
        "arguments": {"text": "Текст для перевода", "source_lang": "ru", "target_lang": "en"},
    })
    assert response.status_code == 200, response.text
    tid = response.json()["history"]["id"]
    job_path = f"/api/mcp-tools/translate/history/{tid}"
    if background:
        assert studio.c.get("/api/translate").json() == {"items": []}
        assert studio.c.get(job_path).json()["active"]
        for _ in range(2):
            row = state.DB.execute("SELECT * FROM mcp_section_jobs WHERE id = ?", (tid,)).fetchone()
            asyncio.run(mcp_sections.refresh_history(row))

    assert studio.c.get(job_path).json()["result"]["translation_id"] == tid
    assert studio.c.get("/api/mcp-tools/translate/history").json()["items"] == []
    assert [row["id"] for row in studio.c.get("/api/translate").json()["items"]] == [tid]
    path = f"/api/translate/{tid}"
    record = studio.c.get(path).json()
    assert (record["source_lang"], record["target_lang"], record["source"]) == ("ru", "en", "Текст для перевода")
    assert [(part["source"], part["text"]) for part in record["parts"]] == [("Текст для перевода", "Translated text")]
    assert studio.c.get(path + "/export?format=txt").text == "Translated text"
    assert studio.c.delete(job_path).status_code == 404
    assert studio.c.get(path).status_code == 200

    shared = studio.c.post(path + "/share", json={"pin": "0123"})
    assert shared.status_code == 201, shared.text
    unlock = {"token": urlsplit(shared.json()["url"]).fragment, "pin": "0123"}
    assert studio.c.post("/share/unlock", json=unlock).json()["item"]["text"] == "Translated text"
    state.DB.execute("UPDATE mcp_servers SET protected = 1 WHERE id = ?", (sid,))
    assert studio.c.get(path).json()["mcp_share_blocked"]
    assert studio.c.post(path + "/share", json={"pin": "0123"}).status_code == 409
    assert studio.c.post("/share/unlock", json=unlock).status_code == 404

    assert studio.c.delete(path).status_code == 204
    assert studio.c.post("/api/trash/empty", json={"password": "password-123"}).json() == {"purged": 1}
    assert studio.c.get(job_path).status_code == 404
