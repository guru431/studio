"""Окно HTML и SVG ответа «Чата» (п. 25 спеки 2026-11-25): документ — в памяти процесса, отдаётся своей учётке по
короткоживущему адресу с политикой содержимого без сети; защищённый диалог окна не получает (решение 17)."""
import logging

import chat_preview
from conftest import add_gateway, make_user, signin
from test_chat import new_chat, sse, turn
from test_chat_web import call
from test_mcp_dialog import name_of, setup
from test_protected_source import code_of, flags, protect

HTML = "<h1>Привет</h1><script>document.title = 'x'</script>"


def post(s, cid, kind="html", text=HTML):
    return s.c.post(f"/api/chats/{cid}/preview", json={"kind": kind, "text": text})


def test_preview_lives_in_memory_and_is_served_sandboxed_without_network(studio, caplog):
    caplog.set_level(logging.INFO)
    add_gateway(studio)
    cid = new_chat(studio)
    first = post(studio, cid)
    assert first.status_code == 201, first.text
    page = studio.c.get(f"/api/chats/preview/{first.json()['token']}")
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html") and page.text == HTML
    csp = page.headers["content-security-policy"]
    assert csp == chat_preview.CSP and csp.startswith("default-src 'none'") and "sandbox allow-scripts" in csp
    assert not any(w in csp for w in ("allow-same-origin", "connect-src", "http", "*", "blob:"))   # сети нет вовсе
    assert csp.count("'self'") == 1 and "frame-ancestors 'self'" in csp
    assert (page.headers["cache-control"], page.headers["referrer-policy"]) == ("no-store", "no-referrer")
    second = post(studio, cid, "svg", "<svg xmlns='http://www.w3.org/2000/svg'><circle r='5'/></svg>").json()["token"]
    assert "<circle r='5'/>" in studio.c.get(f"/api/chats/preview/{second}").text
    assert studio.c.get(f"/api/chats/preview/{first.json()['token']}").status_code == 404   # одно окно на учётку
    assert chat_preview.PREVIEW_MAX == 2 * 1024 * 1024
    big = post(studio, cid, text="я" * (chat_preview.PREVIEW_MAX // 2 + 1))          # 2 МиБ — в байтах UTF-8
    assert (big.status_code, big.json()["detail"]["code"]) == (413, "preview_size")
    lone = studio.c.post(f"/api/chats/{cid}/preview", content='{"kind": "html", "text": "\\ud800"}',
                         headers={"content-type": "application/json"})               # одиночный суррогат — не UTF-8
    assert (lone.status_code, lone.json()["detail"]["code"]) == (413, "preview_size")
    assert post(studio, cid, kind="js").status_code == 422
    assert post(studio, cid, text="я" * (chat_preview.PREVIEW_MAX // 2)).status_code == 201
    studio.clock[0] += chat_preview.TTL_SEC + 1
    assert studio.c.get(f"/api/chats/preview/{second}").status_code == 404
    token = post(studio, cid).json()["token"]
    make_user(studio, "bob")
    signin(studio, "bob")
    assert post(studio, cid).status_code == 404 and studio.c.get(f"/api/chats/preview/{token}").status_code == 404
    assert "Привет" not in caplog.text and "circle" not in caplog.text
    assert "окно HTML" in caplog.text


def test_preview_closes_with_the_dialog(studio):
    add_gateway(studio)
    cid = new_chat(studio)
    token = post(studio, cid).json()["token"]
    assert studio.c.delete(f"/api/chats/{cid}").status_code in (200, 204)          # в корзину
    got = studio.c.get(f"/api/chats/preview/{token}")
    assert (got.status_code, got.json()["detail"]["code"]) == (404, "gone")
    assert code_of(post(studio, cid), 404) == "gone"


def test_protected_dialog_has_no_preview(studio):                                    # Review Focus 3
    sid, calls, _ = setup(studio)
    protect(studio, sid)
    cid = new_chat(studio)
    token = post(studio, cid).json()["token"]                                        # выдано до срабатывания
    studio.gw.chat_script = [{"tool_calls": [call(name_of(sid), q="1")]}, ["Готово"]]
    sse(turn(studio, cid, "Use tool"))
    assert flags(cid)[0] == 1
    assert code_of(post(studio, cid)) == "protected"
    assert studio.c.get(f"/api/chats/preview/{token}").status_code == 404           # и выданное закрыто
