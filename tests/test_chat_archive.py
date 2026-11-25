"""Архив чата одним ZIP (спека 2026-11-21-studio-source-capabilities-design, п. 32): `chat.md` — тот же Markdown,
что у выгрузки, `files/` — исходники вложений, медиа диалога и файлы результатов MCP (через Studio), недоступные —
списком в `missing.txt`; корзина — мимо; больше предела — 413; токен — только своей учётке и в срок."""
import asyncio
from contextlib import aclosing
import io
import logging
import zipfile

import httpx

import chat_exports
import state
import takeout
from conftest import make_user, signin
from test_chat import new_chat
from test_chat_web import call
from test_mcp_chat_files import ART, CONTENT, NOTE, consent, post_media, send, setup, with_note
from test_mcp_dialog import name_of


def full_chat(studio):
    """Диалог с вложением, медиа диалога (одно — в корзине) и файлом результата MCP: (id диалога, id сервера)."""
    sid, _ = setup(studio)
    cid = new_chat(studio)
    consent(studio, cid, sid)
    with_note(studio, cid, "notes.txt")
    assert post_media(studio, cid, b"v" * 40, name="clip.mp4", mime="video/mp4").status_code == 201
    gone = post_media(studio, cid, b"t" * 20, name="trashed.mp3").json()
    assert studio.c.delete(f"/api/chats/{cid}/media/{gone['id']}").status_code == 204
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid, "make"))]}, ["Готово"]]
    send(studio, cid, "Сделай файл")
    return cid, sid


def archive(studio, cid, **body):
    response = studio.c.post(f"/api/chats/{cid}/archive", json=body)
    assert response.status_code == 200, response.text
    return response.json()["token"]


def download(studio, token):
    response = studio.c.get(f"/api/chats/archive/{token}")
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    return zipfile.ZipFile(io.BytesIO(response.content))


def test_archive_has_chat_md_attachments_media_and_result_files(studio, caplog):
    caplog.set_level(logging.INFO, "studio")
    cid, _ = full_chat(studio)
    zf = download(studio, archive(studio, cid))
    assert zf.testzip() is None
    assert sorted(zf.namelist()) == ["chat.md", "files/clip.mp4", "files/notes.txt", "files/out.png"]
    md = studio.c.post(f"/api/chats/{cid}/export", json={"format": "md"})
    assert zf.read("chat.md").decode("utf-8") == md.text
    assert zf.read("files/notes.txt") == NOTE.encode()
    assert zf.read("files/clip.mp4") == b"v" * 40
    assert zf.read("files/out.png") == CONTENT
    assert not takeout.RUNNING
    for secret in ("notes.txt", "clip.mp4", "trashed", "out.png", ART, NOTE):
        assert secret not in caplog.text


def test_unavailable_result_files_go_to_missing_txt(studio):
    cid, _ = full_chat(studio)
    async def gone(request):
        return httpx.Response(404)

    studio.gw.on[f"/artifacts/{ART}/content"] = gone
    zf = download(studio, archive(studio, cid))
    assert "files/out.png" not in zf.namelist()
    assert "files/notes.txt" in zf.namelist()
    assert zf.read("missing.txt").decode("utf-8").splitlines() == ["out.png"]


def test_trashed_chat_and_over_limit_refused(studio, monkeypatch):
    cid, _ = full_chat(studio)
    monkeypatch.setattr(chat_exports, "ARCHIVE_MAX", 50)
    big = studio.c.post(f"/api/chats/{cid}/archive", json={})
    assert big.status_code == 413 and big.json()["detail"]["code"] == "export_size"
    monkeypatch.setattr(chat_exports, "ARCHIVE_MAX", 512 << 20)
    token = archive(studio, cid)
    monkeypatch.setattr(chat_exports, "ARCHIVE_MAX", 50)
    late = studio.c.get(f"/api/chats/archive/{token}")
    assert late.status_code == 413 and late.json()["detail"]["code"] == "export_size"
    monkeypatch.setattr(chat_exports, "ARCHIVE_MAX", 512 << 20)
    assert studio.c.delete(f"/api/chats/{cid}").status_code == 204          # в корзину
    assert studio.c.post(f"/api/chats/{cid}/archive", json={}).status_code == 404
    assert studio.c.get(f"/api/chats/archive/{token}").status_code == 404


def test_foreign_or_expired_token_is_404(studio):
    cid, _ = full_chat(studio)
    token = archive(studio, cid)
    make_user(studio, "other")
    signin(studio, "other")
    assert studio.c.get(f"/api/chats/archive/{token}").status_code == 404
    assert studio.c.post(f"/api/chats/{cid}/archive", json={}).status_code == 404
    signin(studio, "member")
    studio.clock[0] += chat_exports.ARCHIVE_TTL_SEC + 1
    assert studio.c.get(f"/api/chats/archive/{token}").status_code == 404
    assert studio.c.get("/api/chats/archive/nope").status_code == 404


def test_one_archive_at_a_time(studio):
    cid, _ = full_chat(studio)
    token = archive(studio, cid)
    uid = state.DB.execute("SELECT id FROM users WHERE login='member'").fetchone()[0]
    takeout.RUNNING[uid] = object()
    try:
        busy = studio.c.get(f"/api/chats/archive/{token}")
        assert busy.status_code == 409 and busy.json()["detail"]["code"] == "busy"
    finally:
        takeout.RUNNING.clear()


def test_archive_stops_between_chunks_of_one_file_after_signout(studio, monkeypatch):
    cid, _ = full_chat(studio)
    uid = state.DB.execute("SELECT id FROM users WHERE login='member'").fetchone()[0]
    digest = state.DB.execute("SELECT digest FROM sessions WHERE user_id=?", (uid,)).fetchone()[0]
    _, text, local, _ = chat_exports._plan(uid, cid, "ru")
    mark = object()
    takeout.RUNNING[uid] = mark
    monkeypatch.setattr(takeout, "CHUNK", 1)

    async def check():
        async with aclosing(chat_exports._archive(uid, cid, text, local, [], mark, digest)) as body:
            await anext(body)  # chat.md header
            await anext(body)  # chat.md contents
            await anext(body)  # file header
            assert len(await anext(body)) == 1
            state.DB.execute("DELETE FROM sessions WHERE digest=?", (digest,))
            assert [part async for part in body] == []

    asyncio.run(check())
    assert uid not in takeout.RUNNING
