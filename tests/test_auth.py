"""Вход по логину и паролю, сессии на сервере, пауза после неудач, CSRF (app/auth.py, app/main.py)
и первый администратор (app/manage.py)."""
import io
import logging
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import auth
import db
import manage
import release
import security
import state
from conftest import T0, login, make_user

DAY = 86400


def cookie(s) -> str:
    return s.c.cookies[auth.COOKIE]


def csrf_in_db(s) -> str:
    row = state.DB.execute("SELECT csrf FROM sessions WHERE digest = ?", (security.digest(cookie(s)),)).fetchone()
    return row["csrf"]


def test_health_is_open_and_api_needs_login(studio):
    assert studio.c.get("/health").json() == {"status": "ok", "version": release.VERSION}
    r = studio.c.get("/api/me"); assert r.status_code == 401 and r.json()["detail"]["code"] == "login"
    assert studio.c.get("/", follow_redirects=False).headers["location"] == "/login"


def test_login_sets_a_lax_secure_httponly_cookie(studio):
    make_user(studio, "owner"); r = login(studio, "Owner", "password-123")
    assert r.status_code == 303 and r.headers["location"] == "/"
    c = r.headers["set-cookie"].lower()
    assert "studio_sid=" in c and "httponly" in c and "secure" in c and "samesite=lax" in c and "path=/" in c
    # __Host-: браузер берёт её только с Secure, Path=/ и без Domain — соседний *.example.com не подменит (A1-M8)
    assert r.headers["set-cookie"].startswith("__Host-studio_sid=") and "domain=" not in c
    assert studio.c.get("/", follow_redirects=False).status_code == 200


def test_login_page_sends_a_live_session_home(studio):
    r = studio.c.get("/login", follow_redirects=False)
    assert r.status_code == 200 and 'id="login"' in r.text and 'id="password"' in r.text and "{{error}}" not in r.text
    make_user(studio, "owner"); login(studio, "owner", "password-123")
    r = studio.c.get("/login", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_session_is_stored_as_a_digest_only(studio):
    make_user(studio, "owner"); login(studio, "owner", "password-123")
    value = cookie(studio)
    rows = [tuple(r) for r in state.DB.execute("SELECT * FROM sessions")]
    assert len(rows) == 1
    assert all(value not in str(field) for field in rows[0])
    assert rows[0][0] == security.digest(value)


def test_wrong_password_and_unknown_login_read_the_same(studio):
    make_user(studio, "owner")
    wrong, unknown = login(studio, "owner", "nope"), login(studio, "ghost", "nope", ip="10.77.48.6")
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.text == unknown.text and "Неверный логин или пароль" in wrong.text
    assert auth.COOKIE not in studio.c.cookies


def racing_verify(monkeypatch, change) -> None:
    """Проверка пароля, за время которой база меняется (сброс из панели посреди scrypt): scrypt идёт
    в потоке, цикл событий в это время обслуживает другие запросы."""
    real = security.verify_password

    def verify(password, stored):
        ok = real(password, stored)
        change()
        return ok

    monkeypatch.setattr(security, "verify_password", verify)


RACES = {
    "reset": lambda: state.DB.execute("UPDATE users SET pw_hash = ?", (security.hash_password("admin-reset-1"),)),
    "disabled": lambda: state.DB.execute("UPDATE users SET disabled = 1"),
    "deleted": lambda: state.DB.execute("DELETE FROM users"),
}


@pytest.mark.parametrize("race", RACES)
def test_login_started_with_the_old_password_opens_no_session(studio, monkeypatch, race):
    """Пароль сменили, учётку отключили или удалили, пока считался хеш: старый вход сессии не создаёт (аудит P1)."""
    make_user(studio, "owner")
    racing_verify(monkeypatch, RACES[race])
    r = login(studio, "owner", "password-123")
    assert r.status_code == 401 and "Неверный логин или пароль" in r.text
    assert state.DB.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert studio.c.get("/api/me").status_code == 401


def test_password_of_an_account_deleted_after_the_session_check_is_refused(studio):
    """Учётку удалили между проверкой сессии и подтверждением паролем: 403, а не 500 на пустой строке."""
    import asyncio
    with pytest.raises(HTTPException) as e:
        asyncio.run(auth.confirm_password(424242, "password-123", "проверка"))
    assert e.value.status_code == 403 and e.value.detail["code"] == "password"


def test_five_failures_lock_the_pair_but_not_the_login_elsewhere(studio):
    make_user(studio, "owner")
    for _ in range(5): login(studio, "owner", "nope", ip="203.0.113.9")
    assert login(studio, "owner", "password-123", ip="203.0.113.9").status_code == 429
    assert login(studio, "owner", "password-123", ip="198.51.100.7").status_code == 303


def test_five_logins_from_one_address_lock_the_address(studio):
    make_user(studio, "owner")
    for n in range(5):
        assert login(studio, f"user{n}", "nope", ip="203.0.113.9").status_code == 401
    r = login(studio, "owner", "password-123", ip="203.0.113.9")
    assert r.status_code == 429 and "Слишком много попыток — подождите 15 минут" in r.text


def test_success_does_not_reset_the_address_count(studio):
    """Свой верный вход между чужими логинами не обнуляет счёт адреса: иначе перебор шёл бы без паузы."""
    make_user(studio, "owner")
    for n in range(4):
        login(studio, f"user{n}", "nope", ip="203.0.113.9")
    assert login(studio, "owner", "password-123", ip="203.0.113.9").status_code == 303
    login(studio, "user4", "nope", ip="203.0.113.9")
    assert login(studio, "owner", "password-123", ip="203.0.113.9").status_code == 429


def test_hairpin_address_is_locked_only_per_pair(studio):
    make_user(studio, "owner")
    for _ in range(5):
        assert login(studio, "a", "nope", ip="10.77.1.1").status_code == 401
    assert login(studio, "a", "nope", ip="10.77.1.1").status_code == 429
    assert login(studio, "owner", "password-123", ip="10.77.1.1").status_code == 303
    assert ("ip", "10.77.1.1") not in auth.FAILS


def test_spoofed_forwarded_prefix_does_not_reset_the_lock(studio):
    """Начало X-Forwarded-For присылает клиент: счёт — по последнему элементу, который
    дописал Apache."""
    make_user(studio, "owner")
    for n in range(5):
        assert login(studio, "owner", "nope", spoofed=f"203.0.113.{n}").status_code == 401
    assert login(studio, "owner", "password-123", spoofed="203.0.113.99").status_code == 429


def test_lock_expires_after_fifteen_minutes(studio):
    make_user(studio, "owner")
    for _ in range(5): login(studio, "owner", "nope")
    assert login(studio, "owner", "password-123").status_code == 429
    studio.clock[0] += 901
    assert login(studio, "owner", "password-123").status_code == 303
    assert auth.FAILS == {}


def test_login_failures_add_a_growing_delay(studio):
    make_user(studio, "owner")
    for n in range(7):
        assert login(studio, "owner", "nope", ip=f"203.0.113.{n}").status_code == 401
    assert studio.sleeps == [2, 4]
    assert login(studio, "owner", "password-123", ip="198.51.100.7").status_code == 303
    assert studio.sleeps == [2, 4]                         # верный пароль не ждёт


def test_foreign_origin_cannot_post_login(studio):
    make_user(studio, "owner")
    r = studio.c.post("/login", data={"login": "owner", "password": "password-123"},
                      headers={"Origin": "https://evil.test"}, follow_redirects=False)
    assert r.status_code == 403 and auth.COOKIE not in studio.c.cookies
    r = studio.c.post("/login", data={"login": "owner", "password": "password-123"},
                      headers={"Origin": "https://testserver"}, follow_redirects=False)
    assert r.status_code == 303


def test_oversized_login_body_is_refused(studio):
    r = studio.c.post("/login", data={"login": "owner", "password": "x" * 5000}, follow_redirects=False)
    assert r.status_code == 413 and "Слишком длинный запрос" in r.text


def test_api_write_needs_the_csrf_header(studio):
    make_user(studio, "owner"); login(studio, "owner", "password-123")
    r = studio.c.post("/api/me/password", json={"current": "password-123", "new": "another-password"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "csrf"
    r = studio.c.post("/api/me/password", json={}, headers={auth.CSRF_HEADER: "forged"})
    assert r.status_code == 403
    r = studio.c.post("/api/me/password", json={}, headers={auth.CSRF_HEADER: "пароль".encode()})   # не ASCII — не 500
    assert r.status_code == 403
    r = studio.c.post("/api/me/password", json={}, headers={auth.CSRF_HEADER: csrf_in_db(studio)})
    assert r.status_code == 422                            # заслон пройден; отказ — уже за телом запроса
    assert studio.c.get("/api/me").status_code == 200      # GET без заголовка


def test_disabled_user_session_dies(studio):
    uid = make_user(studio, "owner"); login(studio, "owner", "password-123")
    assert studio.c.get("/api/me").status_code != 401
    state.DB.execute("UPDATE users SET disabled = 1 WHERE id = ?", (uid,))
    assert studio.c.get("/api/me").status_code == 401
    assert studio.c.get("/", follow_redirects=False).status_code == 303
    assert login(studio, "owner", "password-123").status_code == 401
    state.DB.execute("UPDATE users SET disabled = 0 WHERE id = ?", (uid,))
    assert studio.c.get("/api/me").status_code == 401      # включение не оживляет старую сессию


def test_session_slides_and_expires(studio):
    make_user(studio, "owner"); login(studio, "owner", "password-123")
    studio.clock[0] = T0 + 2 * DAY
    r = studio.c.get("/api/me")
    assert r.status_code == 200 and "studio_sid=" in r.headers.get("set-cookie", "")   # продлена — cookie заново
    expires = state.DB.execute("SELECT expires FROM sessions").fetchone()[0]
    assert expires == T0 + 2 * DAY + auth.SESSION_SEC
    assert "set-cookie" not in studio.c.get("/api/me").headers                         # в тот же день — нет
    studio.clock[0] = T0 + 31 * DAY                        # первый срок вышел, продлённый — нет
    assert studio.c.get("/api/me").status_code == 200
    studio.clock[0] += 31 * DAY
    assert studio.c.get("/api/me").status_code == 401
    assert state.DB.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


def test_page_visit_does_not_eat_the_cookie_renewal(studio):
    """Первый запрос дня в PWA — страница: она не продлевает сессию молча, иначе cookie браузера
    так и не догнала бы срок на сервере и умерла бы через 30 дней после входа (A1-I1)."""
    make_user(studio, "owner"); login(studio, "owner", "password-123")
    studio.clock[0] = T0 + 2 * DAY
    assert studio.c.get("/", follow_redirects=False).status_code == 200
    assert studio.c.get("/login", follow_redirects=False).status_code == 303
    r = studio.c.get("/api/me")
    assert r.status_code == 200 and "studio_sid=" in r.headers.get("set-cookie", "")


def test_login_drops_the_session_it_replaces(studio):
    make_user(studio, "owner"); make_user(studio, "bob")
    login(studio, "owner", "password-123"); old = cookie(studio)
    assert login(studio, "bob", "password-123").status_code == 303
    assert cookie(studio) != old
    assert [r[0] for r in state.DB.execute("SELECT digest FROM sessions")] == [security.digest(cookie(studio))]
    assert auth.session_user(old) is None


def test_api_answers_are_not_cached(studio):
    make_user(studio, "owner"); login(studio, "owner", "password-123")
    assert studio.c.get("/api/me").headers["cache-control"] == "no-store"
    assert studio.c.get("/api/connections").headers["cache-control"] == "no-store"


ANONYMOUS = {"/health", "/login", "/logout", "/", "/sw.js", "/manifest.webmanifest", "/offline.html", "/robots.txt",
             "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png", "/favicon.ico", "/api/agent/enroll",
             "/share", "/share/unlock", "/share/check", "/share/file"}


def test_pages_answer_head_and_robots_are_turned_away(studio):
    """Проверки адреса и превью ссылок ходят HEAD — не 405; роботам — весь сайт закрыт (страница
    входа не в поиске)."""
    for path in ("/health", "/login", "/", "/sw.js", "/manifest.webmanifest", "/offline.html", "/robots.txt"):
        r = studio.c.head(path, follow_redirects=False)
        assert r.status_code in (200, 303) and not r.content, path
    assert studio.c.head("/", follow_redirects=False).headers["location"] == "/login"
    r = studio.c.get("/robots.txt")
    assert r.status_code == 200 and r.text == "User-agent: *\nDisallow: /\n"
    assert r.headers["content-type"].startswith("text/plain")


def guarded_routes() -> list[tuple[str, str]]:
    """(метод, путь) всех маршрутов, кроме анонимных: заслон — по пути, и роутер без префикса /api стал
    бы открытым, а ни один тест не заметил бы. Обход — двумя тестами (сессия, CSRF): ~320 запросов
    одним тестом шли дольше секунды."""
    import re
    from starlette.routing import Mount, Route, WebSocketRoute
    import main

    def walk(items, prefix=""):
        for route in items:
            if hasattr(route, "original_router"):        # FastAPI 0.14x: include_router — обёртка роутера
                yield from walk(route.original_router.routes, prefix + route.include_context.prefix)
            else:
                yield prefix, route

    routes = []
    for prefix, route in walk(main.app.routes):
        if isinstance(route, Mount):
            assert prefix + route.path in ("/static", f"/static/v/{main.STATIC_VERSION}"), route.path
            assert route.app.directory == auth.STATIC  # Только тот же публичный каталог, не данные пользователей.
            continue
        if isinstance(route, WebSocketRoute):
            # канал моста: http-заслон его не видит, вход — ключом устройства (test_bridge.py)
            assert prefix + route.path == "/api/agent/link", route.path
            continue
        assert isinstance(route, Route), route
        full = prefix + route.path
        # Пакет обновления защищён отдельным ключом устройства, а не cookie браузера.
        # Удалённый/отключённый ключ, чужой Origin и попытка подменить его сессией — test_agent_update.py.
        if full in ANONYMOUS or full == '/api/agent/package':
            continue
        assert full.startswith("/api/"), full
        path = re.sub(r"\{[^}]+\}", "1", full)
        routes += [(method, path) for method in sorted(route.methods - {"HEAD"})]
    assert len(routes) > 30
    return routes


@pytest.mark.parametrize("method", sorted({method for method, _ in guarded_routes()}))
def test_every_route_but_the_anonymous_ones_needs_a_session(studio, method):
    paths = [path for route_method, path in guarded_routes() if route_method == method]
    assert paths, method
    for path in paths:
        r = studio.c.request(method, path)
        assert r.status_code == 401 and r.json()["detail"]["code"] == "login", (method, path)


def test_every_changing_route_needs_the_csrf_header(studio):
    make_user(studio, "owner", is_admin=True); login(studio, "owner", "password-123")
    for method, path in guarded_routes():
        if method != "GET":
            r = studio.c.request(method, path)
            assert r.status_code == 403 and r.json()["detail"]["code"] == "csrf", (method, path)


def test_logout_ends_the_session_server_side(studio):
    make_user(studio, "owner"); login(studio, "owner", "password-123")
    old = cookie(studio)
    r = studio.c.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert auth.COOKIE not in studio.c.cookies
    assert studio.c.get("/api/me", headers={"Cookie": f"{auth.COOKIE}={old}"}).status_code == 401
    assert state.DB.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0


def test_end_sessions_can_keep_the_current_one(studio):
    uid = make_user(studio, "owner")
    login(studio, "owner", "password-123"); first = cookie(studio)
    studio.c.cookies.clear()                     # второй вход — с другого устройства: первая сессия живёт
    login(studio, "owner", "password-123"); second = cookie(studio)
    auth.end_sessions(uid, keep_digest=security.digest(second))
    assert auth.session_user(first) is None and auth.session_user(second)[0].login == "owner"
    auth.end_sessions(uid)
    assert auth.session_user(second) is None


def test_require_admin_refuses_plain_users():
    def request(is_admin):
        user = auth.User(id=1, login="u", name="u", is_admin=is_admin, theme="auto",
                         default_connection_id=None, default_model=None)
        return SimpleNamespace(state=SimpleNamespace(user=user))
    assert auth.require_admin(request(True)).login == "u"
    with pytest.raises(HTTPException) as e:
        auth.require_admin(request(False))
    assert e.value.status_code == 403 and e.value.detail["code"] == "admin"


def test_create_admin_reads_stdin_and_is_idempotent(env, monkeypatch, capsys):
    stdin = "STUDIO_ADMIN_LOGIN=owner\nSTUDIO_ADMIN_PASSWORD=very-long-pass\n"
    for _ in range(2):
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        assert manage.main(["create-admin"]) == 0
    conn = db.connect(env.db_path)
    rows = conn.execute("SELECT login, is_admin, disabled, pw_hash FROM users").fetchall()
    assert len(rows) == 1 and rows[0]["login"] == "owner" and rows[0]["is_admin"] == 1 and rows[0]["disabled"] == 0
    assert security.verify_password("very-long-pass", rows[0]["pw_hash"])
    out = capsys.readouterr()
    assert "very-long-pass" not in out.out + out.err


def test_create_admin_new_password_ends_sessions(env, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("STUDIO_ADMIN_LOGIN=owner\nSTUDIO_ADMIN_PASSWORD=very-long-pass\n"))
    assert manage.main(["create-admin"]) == 0
    conn = db.connect(env.db_path)
    conn.execute("UPDATE users SET is_admin = 0, disabled = 1")
    conn.execute("INSERT INTO sessions(digest, user_id, csrf, expires, renewed) VALUES ('d', 1, 'c', 9e12, 0)")
    monkeypatch.setattr(sys, "stdin", io.StringIO("STUDIO_ADMIN_LOGIN=owner\r\nSTUDIO_ADMIN_PASSWORD=other-long-pass\r\n"))
    assert manage.main(["create-admin"]) == 0
    row = conn.execute("SELECT is_admin, disabled, pw_hash FROM users").fetchone()
    assert row["is_admin"] == 1 and row["disabled"] == 0 and security.verify_password("other-long-pass", row["pw_hash"])
    assert conn.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
    monkeypatch.setattr(sys, "stdin", io.StringIO("STUDIO_ADMIN_LOGIN=owner\nSTUDIO_ADMIN_PASSWORD=short\n"))
    assert manage.main(["create-admin"]) != 0
    out = capsys.readouterr()
    assert all(pw not in out.out + out.err for pw in ("very-long-pass", "other-long-pass", "short"))


def test_passwords_and_logins_never_reach_the_log(studio, caplog):
    caplog.set_level(logging.INFO)
    make_user(studio, "owner-login", "secret-password-1")
    login(studio, "owner-login", "wrong-password-2")
    login(studio, "ghost-login", "wrong-password-3")
    for _ in range(5): login(studio, "owner-login", "wrong-password-2", ip="203.0.113.5")
    login(studio, "owner-login", "secret-password-1", ip="203.0.113.5")       # пауза
    login(studio, "owner-login", "secret-password-1")
    value = cookie(studio)
    text = caplog.text
    assert "вход с 10.77.48.5: успешно" in text and "неверно" in text and "пауза" in text
    for secret in ("owner-login", "ghost-login", "secret-password-1", "wrong-password-2", "wrong-password-3", value):
        assert secret not in text
