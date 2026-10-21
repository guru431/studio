"""Профиль (app/profile.py), панель администратора (app/admin.py), удаление истории (app/history.py)."""
import auth
import clock
import sections
import state
from conftest import add_connection, add_gateway, login, make_user, signin


def add_history(s, env, uid, tag="тайна"):
    """Строки всех таблиц истории пользователя и файл его media: (путь файла, id чата)."""
    now = clock.NOW()
    rel = f"u{uid}/m-{uid}.png"
    path = env.media_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"png")
    db = state.DB
    db.execute("INSERT INTO media(id, user_id, rel_path, mime, size, has_thumb, download_name, created)"
               " VALUES (?, ?, ?, 'image/png', 3, 0, 'x.png', ?)", (f"m-{uid}", uid, rel, now))
    db.execute("INSERT INTO chats(id, user_id, title, created, updated) VALUES (?, ?, ?, ?, ?)",
               (f"c-{uid}", uid, f"{tag} title", now, now))
    db.execute("INSERT INTO messages(id, chat_id, role, content, status, via, created)"
               " VALUES (?, ?, 'user', ?, 'done', 'server', ?)", (f"msg-{uid}", f"c-{uid}", f"{tag} content", now))
    db.execute("INSERT INTO attachments(id, user_id, message_id, kind, name, media_id, created)"
               " VALUES (?, ?, ?, 'image', 'x.png', ?, ?)", (f"a-{uid}", uid, f"msg-{uid}", f"m-{uid}", now))
    db.execute("INSERT INTO images(id, user_id, kind, model, prompt, size, seed, media_id, state, detail, created)"
               " VALUES (?, ?, 'generation', 'lustify', ?, '1024x1024', 1, ?, 'done', '', ?)",
               (f"i-{uid}", uid, f"{tag} prompt", f"m-{uid}", now))
    db.execute("INSERT INTO videos(id, user_id, connection_id, source_media_id, prompt, seconds, quality, smooth,"
               " state, detail, created) VALUES (?, ?, 1, ?, ?, 5, 'fast', 0, 'done', '', ?)",
               (f"v-{uid}", uid, f"m-{uid}", f"{tag} video prompt", now))
    return path


HISTORY = ("chats", "messages", "attachments", "images", "videos", "media")


def history_rows(uid) -> int:
    rows = 0
    for table in HISTORY:
        if table == "messages":
            sql = "SELECT COUNT(*) FROM messages m JOIN chats c ON c.id = m.chat_id WHERE c.user_id = ?"
        else:
            sql = f"SELECT COUNT(*) FROM {table} WHERE user_id = ?"
        rows += state.DB.execute(sql, (uid,)).fetchone()[0]
    return rows


def code(r) -> str:
    return r.json()["detail"]["code"]


# ---- профиль ---------------------------------------------------------------

def test_me_carries_csrf_and_profile(studio):
    make_user(studio, "owner", is_admin=True); signin(studio, "owner")
    me = studio.c.get("/api/me").json()
    assert me["login"] == "owner" and me["is_admin"] and len(me["csrf"]) >= 32 and me["theme"] == "auto"
    assert me["capabilities"] == dict.fromkeys(sections.SECTIONS, False) and me["views"] == {}
    assert me["screen_lock"] is False
    assert set(me) == {"id", "login", "name", "is_admin", "theme", "views", "default_connection_id", "default_model",
                       "csrf", "capabilities", "screen_lock"}


def test_theme_is_validated(studio):
    make_user(studio, "owner"); signin(studio, "owner")
    r = studio.c.patch("/api/me", json={"theme": "pink"})
    assert r.status_code == 422 and code(r) == "theme"
    r = studio.c.patch("/api/me", json={"theme": "dark"})
    assert r.status_code == 200 and r.json()["theme"] == "dark"
    assert studio.c.get("/api/me").json()["theme"] == "dark"


def test_views_are_kept_per_section_and_validated(studio):
    """Вид ленты — в учётке, у каждого раздела свой: PATCH дополняет выбор, а не заменяет его."""
    make_user(studio, "owner"); signin(studio, "owner")
    assert studio.c.patch("/api/me", json={"views": {"photo": "table"}}).json()["views"] == {"photo": "table"}
    me = studio.c.patch("/api/me", json={"views": {"video": "tiles"}}).json()
    assert me["views"] == {"photo": "table", "video": "tiles"}
    for bad in ({"chat": "table"}, {"photo": "grid"}, None):
        r = studio.c.patch("/api/me", json={"views": bad})
        assert r.status_code == 422 and code(r) == "views", bad
    assert studio.c.get("/api/me").json()["views"] == {"photo": "table", "video": "tiles"}


def test_chat_default_comes_from_the_chat_section(studio):
    """Умолчание чата в /api/me — из раздела «Чат»; PATCH /api/me его больше не правит."""
    cid = add_connection(studio, chat_models=("gemma", "gemma-2"))
    me = studio.c.get("/api/me").json()
    assert me["default_connection_id"] == cid and me["default_model"] == "gemma"
    uid = me["id"]
    sections.set_default(uid, "chat", cid, "gemma-2")
    me = studio.c.patch("/api/me", json={"theme": "light", "default_connection_id": None, "default_model": "x"}).json()
    assert me["default_connection_id"] == cid and me["default_model"] == "gemma-2" and me["theme"] == "light"


def test_password_change_needs_current_and_ends_other_sessions(studio):
    uid = make_user(studio, "owner"); signin(studio, "owner")
    other, _ = auth.create_session(uid)
    r = studio.c.post("/api/me/password", json={"current": "wrong-password", "new": "brand-new-pass"})
    assert r.status_code == 403 and code(r) == "password"
    r = studio.c.post("/api/me/password", json={"current": "password-123", "new": "short"})
    assert r.status_code == 422 and code(r) == "short"
    assert auth.session_user(other) is not None                          # отказы ничего не гасят
    r = studio.c.post("/api/me/password", json={"current": "password-123", "new": "brand-new-pass"})
    assert r.status_code == 204
    assert auth.session_user(other) is None
    assert studio.c.get("/api/me").status_code == 200                    # текущая сессия жива
    assert login(studio, "owner", "password-123").status_code == 401
    assert login(studio, "owner", "brand-new-pass").status_code == 303


def test_wrong_current_password_is_limited(studio):
    """Укравший сессию не переберёт текущий пароль: 5 неудач за 15 минут — пауза и с верным (A1-M1)."""
    uid = make_user(studio, "owner"); signin(studio, "owner")
    for _ in range(5):
        r = studio.c.post("/api/me/password", json={"current": "wrong-password", "new": "brand-new-pass"})
        assert r.status_code == 403 and code(r) == "password"
    r = studio.c.post("/api/me/password", json={"current": "password-123", "new": "brand-new-pass"})
    assert r.status_code == 429 and code(r) == "locked"
    assert ("pw", uid) in auth.FAILS
    studio.clock[0] += 901
    r = studio.c.post("/api/me/password", json={"current": "password-123", "new": "brand-new-pass"})
    assert r.status_code == 204 and ("pw", uid) not in auth.FAILS


def test_validation_errors_keep_the_error_format_and_do_not_echo_input(studio):
    make_user(studio, "owner"); signin(studio, "owner")
    r = studio.c.post("/api/me/password", json={"current": "password-123", "new": ["secret-in-a-list"]})
    assert r.status_code == 422 and code(r) == "invalid" and "secret-in-a-list" not in r.text


def test_history_wipe_takes_only_ones_own(studio, env):
    uid = make_user(studio, "owner"); other = make_user(studio, "bob"); signin(studio, "owner")
    mine, theirs = add_history(studio, env, uid), add_history(studio, env, other)
    assert studio.c.delete("/api/history").status_code == 200
    assert history_rows(uid) == 0 and not mine.exists()
    assert history_rows(other) == len(HISTORY) and theirs.exists()


# ---- панель администратора -------------------------------------------------

def test_non_admin_cannot_reach_the_panel(studio):
    make_user(studio, "owner", is_admin=True); bob = make_user(studio, "bob"); signin(studio, "bob")
    calls = [("GET", "/api/admin/users", None),
             ("POST", "/api/admin/users", {"login": "eve", "name": "Eve", "password": "password-123", "is_admin": True}),
             ("PATCH", f"/api/admin/users/{bob}", {"is_admin": True}),
             ("DELETE", f"/api/admin/users/{bob}", None),
             ("GET", "/api/admin/allowlist", None),
             ("PUT", "/api/admin/allowlist", {"entries": ["evil.test:80"]})]
    for method, path, body in calls:
        r = studio.c.request(method, path, json=body)
        assert r.status_code == 403 and code(r) == "admin", (method, path)
    assert state.DB.execute("SELECT is_admin FROM users WHERE id = ?", (bob,)).fetchone()[0] == 0
    assert state.DB.execute("SELECT COUNT(*) FROM users WHERE login = 'eve'").fetchone()[0] == 0


def test_admin_creates_user_who_can_log_in(studio):
    make_user(studio, "owner", is_admin=True); signin(studio, "owner")
    r = studio.c.post("/api/admin/users", json={"login": "Ivan", "name": "Иван", "password": "ivan-password",
                                                "is_admin": False})
    assert r.status_code == 201
    user = r.json()
    assert user["login"] == "ivan" and user["name"] == "Иван" and not user["is_admin"] and not user["disabled"]
    assert user["connections"] == 0 and "password" not in r.text and "pw_hash" not in r.text
    assert "ivan-password" not in r.text
    logins = [u["login"] for u in studio.c.get("/api/admin/users").json()["items"]]
    assert logins == ["owner", "ivan"]
    assert login(studio, "ivan", "ivan-password").status_code == 303


def test_login_must_be_simple_and_unique(studio):
    make_user(studio, "owner", is_admin=True); signin(studio, "owner")
    for bad in ["Иван", "a", "x" * 33, "a b"]:
        r = studio.c.post("/api/admin/users", json={"login": bad, "name": "n", "password": "password-123",
                                                    "is_admin": False})
        assert r.status_code == 422 and code(r) == "login", bad
    r = studio.c.post("/api/admin/users", json={"login": "owner", "name": "n", "password": "password-123",
                                                "is_admin": False})
    assert r.status_code == 409 and code(r) == "taken"
    r = studio.c.post("/api/admin/users", json={"login": "ivan", "name": "n", "password": "short",
                                                "is_admin": False})
    assert r.status_code == 422 and code(r) == "short"


def test_admin_renames_a_login_and_sessions_survive(studio):
    owner = make_user(studio, "owner", is_admin=True); signin(studio, "owner")
    r = studio.c.patch(f"/api/admin/users/{owner}", json={"login": " example-user "})
    assert r.status_code == 200 and r.json()["login"] == "example-user"
    assert studio.c.get("/api/me").json()["login"] == "example-user"          # своя сессия жива
    assert login(studio, "example-user", "password-123").status_code == 303
    assert login(studio, "owner", "password-123").status_code == 401


def test_rename_refuses_bad_or_taken_logins(studio):
    make_user(studio, "owner", is_admin=True); bob = make_user(studio, "bob"); signin(studio, "owner")
    for bad in ["Иван", "a", "x" * 33, "a b"]:
        r = studio.c.patch(f"/api/admin/users/{bob}", json={"login": bad})
        assert r.status_code == 422 and code(r) == "login", bad
    r = studio.c.patch(f"/api/admin/users/{bob}", json={"login": "OWNER"})
    assert r.status_code == 409 and code(r) == "taken"
    assert studio.c.get("/api/admin/users").json()["items"][1]["login"] == "bob"


def test_reset_password_and_disable_end_sessions(studio):
    make_user(studio, "owner", is_admin=True); bob = make_user(studio, "bob"); signin(studio, "owner")
    session, _ = auth.create_session(bob)
    r = studio.c.patch(f"/api/admin/users/{bob}", json={"password": "reset-password"})
    assert r.status_code == 200 and "reset-password" not in r.text
    assert auth.session_user(session) is None
    session, _ = auth.create_session(bob)
    r = studio.c.patch(f"/api/admin/users/{bob}", json={"name": "Боб"})
    assert r.json()["name"] == "Боб" and auth.session_user(session) is not None   # имя сессий не гасит
    r = studio.c.patch(f"/api/admin/users/{bob}", json={"disabled": True})
    assert r.status_code == 200 and r.json()["disabled"]
    assert auth.session_user(session) is None
    assert studio.c.patch("/api/admin/users/9999", json={"name": "x"}).status_code == 404
    assert login(studio, "bob", "reset-password").status_code == 401        # отключён
    state.DB.execute("UPDATE users SET disabled = 0 WHERE id = ?", (bob,))
    assert login(studio, "bob", "reset-password").status_code == 303


def test_admin_cannot_reset_own_password_from_the_panel(studio):
    """Свой пароль — только «Прочее → Пароль» с текущим: иначе получивший телефон открытым на минуту
    сменил бы его из панели и потом открывал бы запертую студию сам (спека screen-lock, раздел 4)."""
    owner = make_user(studio, "owner", is_admin=True); signin(studio, "owner")
    r = studio.c.patch(f"/api/admin/users/{owner}", json={"password": "stolen-password"})
    assert r.status_code == 409 and code(r) == "self"
    assert login(studio, "owner", "stolen-password").status_code == 401
    assert login(studio, "owner", "password-123").status_code == 303


def test_disabling_a_user_stops_their_work_but_keeps_history(studio):
    """Отключение учётки останавливает её задание картинки (карта освобождается её подключением),
    поток чата и ролик у шлюза; история остаётся (ревью A1-M13)."""
    import time
    import chat
    import images
    from fakes import PNG_REAL, TOKEN
    from test_chat import events_of

    def until(cond):
        for _ in range(3000):
            if cond():
                return
            time.sleep(0.001)
        raise AssertionError("условие не наступило")

    make_user(studio, "owner", is_admin=True); bob = make_user(studio, "bob"); signin(studio, "bob")
    add_gateway(studio)
    video = studio.c.post("/api/videos", files={"image": ("p.png", PNG_REAL, "image/png")}).json()
    studio.gw.hold = studio.gw.chat_hold = True
    try:
        job = studio.c.post("/api/images", json={"prompt": "p", "size": "square"}).json()["id"]
        until(lambda: studio.gw.generations)
        cid = studio.c.post("/api/chats", json={}).json()["id"]
        with studio.c.stream("POST", f"/api/chats/{cid}/turns", json={"text": "Привет"}) as r:
            events = events_of(r.iter_lines())
            assert next(events)[0] == "turn" and next(d for e, d in events if e == "delta") == {"text": "Здрав"}
            (stream,) = chat.STREAMS.values()
        signin(studio, "owner")
        assert studio.c.patch(f"/api/admin/users/{bob}", json={"disabled": True}).status_code == 200
        assert stream.task.done() and not chat.STREAMS
        assert images.JOBS[job].state == "cancelling"
    finally:
        studio.gw.hold = studio.gw.chat_hold = False
    until(lambda: images.JOBS[job].state in images.TERMINAL)
    assert images.JOBS[job].state == "cancelled" and "карта освобождена" in images.JOBS[job].detail
    assert studio.gw.releases[-1] == ("lustify-v10-krea2-turbo", f"Bearer {TOKEN}")
    row = state.DB.execute("SELECT state, gateway_id FROM videos WHERE id = ?", (video["id"],)).fetchone()
    assert row["state"] == "cancelled" and studio.gw.video_deleted == [row["gateway_id"]]
    statuses = [r["status"] for r in state.DB.execute("SELECT status FROM messages WHERE chat_id = ?", (cid,))]
    assert statuses == ["done", "stopped"]
    assert state.DB.execute("SELECT state FROM images WHERE id = ?", (job,)).fetchone()["state"] == "cancelled"


def test_admin_cannot_lock_themselves_out(studio):
    me = make_user(studio, "owner", is_admin=True); signin(studio, "owner")
    for body in [{"disabled": True}, {"is_admin": False}]:
        r = studio.c.patch(f"/api/admin/users/{me}", json=body)
        assert r.status_code == 409 and code(r) == "self", body
    r = studio.c.delete(f"/api/admin/users/{me}")
    assert r.status_code == 409 and code(r) == "self"
    row = state.DB.execute("SELECT is_admin, disabled FROM users WHERE id = ?", (me,)).fetchone()
    assert row["is_admin"] == 1 and row["disabled"] == 0
    assert studio.c.get("/api/admin/users").status_code == 200


def test_deleting_a_user_removes_rows_and_files(studio, env):
    make_user(studio, "owner", is_admin=True); bob = make_user(studio, "bob"); signin(studio, "owner")
    path = add_history(studio, env, bob)
    session, _ = auth.create_session(bob)
    assert path.exists() and history_rows(bob) == len(HISTORY)
    assert studio.c.delete(f"/api/admin/users/{bob}").status_code == 200
    assert not path.exists() and history_rows(bob) == 0
    assert state.DB.execute("SELECT COUNT(*) FROM users WHERE id = ?", (bob,)).fetchone()[0] == 0
    assert auth.session_user(session) is None
    assert studio.c.delete(f"/api/admin/users/{bob}").status_code == 404


def test_a_missing_file_does_not_stop_the_wipe(studio, env):
    make_user(studio, "owner", is_admin=True); bob = make_user(studio, "bob"); signin(studio, "owner")
    add_history(studio, env, bob).unlink()
    assert studio.c.delete(f"/api/admin/users/{bob}").status_code == 200
    assert history_rows(bob) == 0


def test_panel_lists_no_history(studio, env):
    make_user(studio, "owner", is_admin=True); bob = make_user(studio, "bob"); signin(studio, "owner")
    add_history(studio, env, bob, tag="секрет")
    text = studio.c.get("/api/admin/users").text
    for word in ["prompt", "title", "content", "секрет", "pw_hash"]:
        assert word not in text, word
    for item in studio.c.get("/api/admin/users").json()["items"]:
        assert set(item) == {"id", "login", "name", "is_admin", "disabled", "created", "connections"}


def test_allowlist_round_trip(studio):
    make_user(studio, "owner", is_admin=True); signin(studio, "owner")
    assert studio.c.get("/api/admin/allowlist").json() == {"entries": ["10.77.1.15:4000"]}
    r = studio.c.put("/api/admin/allowlist", json={"entries": ["10.77.1.15:4000", "[FD00::5]:8080"]})
    assert r.status_code == 200 and r.json() == {"entries": ["10.77.1.15:4000", "[fd00::5]:8080"]}
    for bad in ["10.77.1.15", "a b:1", "10.0.0.1:0", "10.0.0.1:65536", "llm:8080"]:
        r = studio.c.put("/api/admin/allowlist", json={"entries": [bad]})
        assert r.status_code == 422 and code(r) == "entry", bad
    assert studio.c.get("/api/admin/allowlist").json() == {"entries": ["10.77.1.15:4000", "[fd00::5]:8080"]}
