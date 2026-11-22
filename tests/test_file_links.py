"""Личная ссылка на файл «Моих файлов» на сутки и анонимный вход `/f` (my_files.py): токен только во фрагменте
адреса, в базе — sha256, путь и имя — шифром; все отказы — одинаковый 404; паузы по адресу."""
import json
import secrets

import httpx

import access
import mcp_client
import my_files
import security
import state
from test_my_files import member_with_files, upload  # noqa: F401  (фикстура empty_cache — ниже)
from test_my_files import empty_cache  # noqa: F401

PHOTO = b"\xff\xd8 picture bytes " * 50


def make_link(studio, sid, path="Фото/кот.jpg", data=PHOTO):
    assert upload(studio, sid, path, data).json()["done"] is True
    response = studio.c.post(f"/api/my-files/{sid}/links", json={"path": path})
    assert response.status_code == 200, response.text
    return response.json()


def anonymous(studio):
    studio.c.cookies.clear()


def check(studio, token, **headers):
    return studio.c.post("/f/check", json={"token": token}, headers=headers)


def opened(studio, token):
    return studio.c.post("/f/open", json={"token": token})


def public_headers(response):
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "noindex" in response.headers["x-robots-tag"]


def test_link_flow_check_open_file_range(studio):
    member, sid, server = member_with_files(studio)
    link = make_link(studio, sid)
    assert link["url"].startswith("https://testserver/f#")
    token = link["url"].split("#", 1)[1]
    assert len(token) == 43 and link["expires"] == studio.clock[0] + my_files.LINK_SEC == my_files.LINK_SEC + studio.clock[0]

    row = state.DB.execute("SELECT * FROM file_links").fetchone()
    assert row["token_hash"] == security.digest(token) and row["user_id"] == member and row["server_id"] == sid
    assert "кот" not in row["path_enc"] and "кот" not in row["name_enc"]
    assert security.decrypt(row["path_enc"].encode()) == "Фото/кот.jpg"

    listed = studio.c.get("/api/my-files/links").json()
    assert [(item["id"], item["name"], item["size"], item["sid"]) for item in listed] == [
        (link["id"], "кот.jpg", len(PHOTO), sid)]

    anonymous(studio)
    page = studio.c.get("/f")
    assert page.status_code == 200 and "file-link.js" in page.text
    public_headers(page)
    assert studio.c.head("/f").status_code == 200

    found = check(studio, token)
    assert found.status_code == 200, found.text
    assert found.json() == {"name": "кот.jpg", "size": len(PHOTO), "expires": link["expires"]}
    public_headers(found)

    entered = opened(studio, token)
    assert entered.status_code == 204, entered.text
    cookie = entered.headers["set-cookie"]
    for part in ("studio_file=", "HttpOnly", "Path=/f/file", "SameSite=strict", "Secure", "Max-Age=600"):
        assert part.lower() in cookie.lower(), cookie
    assert token not in cookie

    whole = studio.c.get("/f/file")
    assert whole.status_code == 200 and whole.content == PHOTO
    assert whole.headers["content-type"] == "application/octet-stream"
    assert whole.headers["content-disposition"].startswith("attachment;")
    public_headers(whole)
    part = studio.c.get("/f/file", headers={"Range": "bytes=5-9"})
    assert part.status_code == 206 and part.content == PHOTO[5:10]

    # Запросы к серверу — от имени владельца ссылки.
    assert all(user == access.studio_user(member, sid) for _, _, user, _ in server.seen)

    # Cookie живёт 600 с.
    studio.clock[0] += my_files.OPEN_SEC + 1
    assert studio.c.get("/f/file").status_code == 404


def test_link_lookup_reaches_beyond_the_ui_limit_and_survives_renaming(studio, monkeypatch):
    member, sid, server = member_with_files(studio)
    monkeypatch.setattr(my_files, "ENTRIES_MAX", 2)
    folder = server.folder(access.studio_user(member, sid))
    folder.update({"Фото/a.jpg": b"a", "Фото/b.jpg": b"b"})
    token = make_link(studio, sid)["url"].split("#", 1)[1]
    ticket = studio.c.post(f"/api/my-files/{sid}/ticket", json={"path": "Фото/кот.jpg"}).json()["url"]
    item = mcp_client.row(member, sid)
    mcp_client.update_for(None, sid, mcp_client.ServerIn(name="Renamed", url=item["url"], enabled=True))
    assert studio.c.get(ticket).content == PHOTO
    anonymous(studio)
    assert opened(studio, token).status_code == 204
    assert studio.c.get("/f/file").content == PHOTO


def test_link_expired_revoked_stamp_changed_access_removed_file_gone_all_404(studio):
    member, sid, server = member_with_files(studio)
    bodies = []

    def refused(token):
        response = check(studio, token)
        assert response.status_code == 404, response.text
        assert "кот" not in response.text and "Фото" not in response.text
        public_headers(response)
        bodies.append(response.json())
        assert opened(studio, token).status_code == 404

    session = dict(studio.c.cookies)

    def again():
        studio.c.cookies.clear()
        studio.c.cookies.update(session)

    # неизвестный и неверный токен
    refused(secrets.token_urlsafe(32))
    refused("short")

    # истёкший
    token = make_link(studio, sid)["url"].split("#")[1]
    assert check(studio, token).status_code == 200
    studio.clock[0] += my_files.LINK_SEC
    refused(token)

    # отозванный — и уже открытый файл тоже закрыт
    link = make_link(studio, sid, "Фото/кот2.jpg")
    token = link["url"].split("#")[1]
    assert opened(studio, token).status_code == 204
    assert studio.c.get("/f/file").status_code == 200
    assert studio.c.delete(f"/api/my-files/links/{link['id']}").status_code == 204
    refused(token)
    assert studio.c.get("/f/file").status_code == 404
    assert studio.c.get("/api/my-files/links").json() == []

    # исчезнувший файл
    again()
    token = make_link(studio, sid, "Фото/кот3.jpg")["url"].split("#")[1]
    server.folder(access.studio_user(member, sid)).pop("Фото/кот3.jpg")
    refused(token)

    # снятый доступ владельца к серверу (группы доступа)
    again()
    token = make_link(studio, sid, "Фото/кот4.jpg")["url"].split("#")[1]
    group = access.create_group("Others")["id"]
    access.set_rule("mcp", sid, [group])
    refused(token)
    access.set_rule("mcp", sid, None)
    assert check(studio, token).status_code == 200

    # сменившийся ключ сервера (stamp)
    state.DB.execute("UPDATE mcp_servers SET secret_enc = ? WHERE id = ?",
                     (security.encrypt(json.dumps({"token": "rotated"})), sid))
    refused(token)

    assert all(body == bodies[0] for body in bodies)

    # стирание истории уносит ссылки учётки
    state.DB.execute("UPDATE mcp_servers SET secret_enc = ? WHERE id = ?",
                     (security.encrypt(json.dumps({"token": "private-mcp-token"})), sid))
    again()
    make_link(studio, sid, "Фото/кот5.jpg")
    from conftest import erase_history
    assert erase_history(studio).status_code == 200
    assert state.DB.execute("SELECT COUNT(*) FROM file_links").fetchone()[0] == 0


def test_link_follows_the_file_replaced_or_changed_file_is_gone(studio):
    """Ссылка ведёт к файлу, а не к пути: другой файл под тем же именем (другой размер) или изменённый (другой
    mtime) — одинаковый 404 на всех маршрутах `/f`, и уже открытый файл тоже."""
    member, sid, server = member_with_files(studio)
    folder = server.folder(access.studio_user(member, sid))
    token = make_link(studio, sid)["url"].split("#")[1]
    gone = check(studio, secrets.token_urlsafe(32)).json()
    assert opened(studio, token).status_code == 204
    folder["Фото/кот.jpg"] = b"another private picture"
    for response in (check(studio, token), opened(studio, token), studio.c.get("/f/file")):
        assert response.status_code == 404 and response.json() == gone, response.text
        public_headers(response)

    # тот же размер, другое время изменения
    token = make_link(studio, sid, "Фото/кот2.jpg")["url"].split("#")[1]
    assert opened(studio, token).status_code == 204
    assert studio.c.get("/f/file").status_code == 200
    server.mtimes["Фото/кот2.jpg"] = 1_700_000_999.0
    for response in (check(studio, token), opened(studio, token), studio.c.get("/f/file")):
        assert response.status_code == 404 and response.json() == gone, response.text
    server.mtimes.clear()
    assert check(studio, token).status_code == 200


def file_events(uid):
    return [(row["kind"], row["detail"]) for row in state.DB.execute(
        "SELECT kind, detail FROM security_events WHERE user_id = ? AND kind LIKE 'file_link%' ORDER BY id", (uid,))]


def test_links_are_journaled_counted_in_access_and_dropped_by_revoke_all(studio):
    """Ссылки на файлы видны средствам безопасности: создание и отзыв — в журнале, счёт — в «Доступах и ссылках»,
    «Отозвать всё» их снимает, и уже открытый файл закрывается на следующем запросе."""
    member, sid, server = member_with_files(studio)
    first = make_link(studio, sid)
    make_link(studio, sid, "Фото/кот2.jpg")
    assert studio.c.delete(f"/api/my-files/links/{first['id']}").status_code == 204
    token = make_link(studio, sid, "Фото/кот3.jpg")["url"].split("#")[1]
    assert file_events(member) == [("file_link_create", None)] * 2 + [("file_link_revoke", None),
                                                                      ("file_link_create", None)]
    assert studio.c.get("/api/me/access").json()["file_links"] == 2
    assert opened(studio, token).status_code == 204
    assert studio.c.get("/f/file").status_code == 200

    revoked = studio.c.delete("/api/me/access")
    assert revoked.status_code == 200 and revoked.json()["file_links"] == 2
    assert studio.c.get("/f/file").status_code == 404
    assert check(studio, token).status_code == 404
    assert state.DB.execute("SELECT COUNT(*) FROM file_links WHERE user_id = ?", (member,)).fetchone()[0] == 0
    assert file_events(member)[-1] == ("file_link_revoke", "all")
    assert studio.c.get("/api/me/access").json()["file_links"] == 0


def test_emergency_exit_drops_file_links_and_closes_an_open_file(studio):
    member, sid, server = member_with_files(studio)
    token = make_link(studio, sid)["url"].split("#")[1]
    make_link(studio, sid, "Фото/кот2.jpg")
    assert opened(studio, token).status_code == 204
    assert studio.c.get("/f/file").status_code == 200
    assert studio.c.post("/api/me/emergency", json={"password": "password-123"}).status_code == 204
    assert state.DB.execute("SELECT COUNT(*) FROM file_links WHERE user_id = ?", (member,)).fetchone()[0] == 0
    assert studio.c.get("/f/file").status_code == 404
    assert check(studio, token).status_code == 404


def test_link_post_foreign_origin_403_and_body_cap_413(studio):
    member, sid, server = member_with_files(studio)
    token = make_link(studio, sid)["url"].split("#")[1]
    anonymous(studio)
    for path in ("/f/check", "/f/open"):
        foreign = studio.c.post(path, json={"token": token}, headers={"Origin": "https://evil.example"})
        assert foreign.status_code == 403
        public_headers(foreign)
        cross = studio.c.post(path, json={"token": token}, headers={"Sec-Fetch-Site": "cross-site"})
        assert cross.status_code == 403
        big = studio.c.post(path, content=json.dumps({"token": token, "pad": "x" * 5000}),
                            headers={"Content-Type": "application/json"})
        assert big.status_code == 413
        public_headers(big)
    assert check(studio, token).status_code == 200


def test_link_bruteforce_locks_ip_429(studio):
    member, sid, server = member_with_files(studio)
    token = make_link(studio, sid)["url"].split("#")[1]
    anonymous(studio)
    # удачные проверки неудачами не считаются
    for _ in range(35):
        assert check(studio, token).status_code == 200
    for _ in range(30):
        assert check(studio, secrets.token_urlsafe(32)).status_code == 404
    locked = check(studio, token)
    assert locked.status_code == 429 and locked.json()["detail"]["code"] == "file_locked"
    public_headers(locked)
    assert opened(studio, token).status_code == 429
    # другой адрес — свой счёт
    assert check(studio, token, **{"X-Forwarded-For": "203.0.113.9"}).status_code == 200
    studio.clock[0] += 901
    assert check(studio, token).status_code == 200


def test_link_deeply_nested_listing_is_the_uniform_404(studio):
    """B2-M1: глубоко вложенный JSON маршрута контракта (`RecursionError`, не `ValueError`) отдавал 500 без
    заголовков `/f` — теперь тот же 404, что любой отказ; свой список папки — пустой, без 500."""
    member, sid, server = member_with_files(studio)
    token = make_link(studio, sid)["url"].split("#")[1]
    gone = check(studio, secrets.token_urlsafe(32)).json()
    async def nested(request):
        return httpx.Response(200, content=b"[" * 65536)

    studio.gw.on["/files/list"] = nested
    listed = studio.c.post(f"/api/my-files/{sid}/list", json={"path": ""})
    assert listed.status_code == 200 and listed.json()["entries"] == []
    anonymous(studio)
    for response in (check(studio, token), opened(studio, token)):
        assert response.status_code == 404, response.text
        assert response.json() == gone
        public_headers(response)


def test_link_check_and_open_with_the_server_unreachable_are_the_uniform_404(studio):
    """Сервер MCP недоступен на `/files/list` (`mcp_contracts.call` → HTTPException → `_same_file`): `/f/check` и
    `/f/open` — тот же 404 «ссылка недоступна» без слов о MCP, cookie не выдаётся; сервер вернулся — ссылка жива."""
    member, sid, server = member_with_files(studio)
    token = make_link(studio, sid)["url"].split("#")[1]
    gone = check(studio, secrets.token_urlsafe(32)).json()
    async def down(request):
        raise httpx.ConnectError("down")

    studio.gw.on["/files/list"] = down
    anonymous(studio)
    for response in (check(studio, token), opened(studio, token)):
        assert response.status_code == 404, response.text
        assert response.json() == gone and "MCP" not in response.text
        assert my_files.COOKIE not in response.headers.get("set-cookie", "")
        public_headers(response)
    studio.gw.on["/files/list"] = server.files
    assert check(studio, token).status_code == 200 and opened(studio, token).status_code == 204


def test_link_file_refusals_of_the_server_are_the_uniform_404(studio, monkeypatch):
    """B2-M2: отказ сервера (5xx, недоступен) или нерасшифрованный ключ MCP на `/f/file` отвечал 409 словами о
    настройке MCP владельца — анонимный получатель видит только одинаковый 404 «ссылка недоступна»."""
    member, sid, server = member_with_files(studio)
    token = make_link(studio, sid)["url"].split("#")[1]
    gone = check(studio, secrets.token_urlsafe(32)).json()
    anonymous(studio)
    assert opened(studio, token).status_code == 204

    async def failing(request):
        return httpx.Response(500, json={"message": "внутренний сбой MCP"})

    async def forbidden(request):
        return httpx.Response(403)

    async def broken(request):
        raise httpx.ConnectError("down")

    for reply in (failing, forbidden, broken):
        studio.gw.on["/files/content"] = reply
        refused = studio.c.get("/f/file")
        assert refused.status_code == 404, refused.text
        assert refused.json() == gone and "MCP" not in refused.text
        public_headers(refused)
    studio.gw.on["/files/content"] = server.files
    assert studio.c.get("/f/file").status_code == 200

    def undecryptable(item):
        raise mcp_client.refused("Ключ MCP не расшифрован — введите его заново")

    monkeypatch.setattr(mcp_client, "secret", undecryptable)
    refused = studio.c.get("/f/file")
    assert refused.status_code == 404 and refused.json() == gone
    public_headers(refused)
