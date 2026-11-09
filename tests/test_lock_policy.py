"""Область PIN, два независимых таймера и защита общих входов в историю."""
import pytest
from fastapi import HTTPException

import db
import bridge
import fake_bridge
import claude_code
import lock_policy
import media
import auth
import security
import state
from conftest import make_user, signin
from test_code import setup, url
from test_foundation import DROP_V24
from test_search import CHAT, IMAGE, MESSAGE


def owner(studio):
    uid = make_user(studio, "owner")
    signin(studio, "owner")
    return uid


def enable(studio, sections, timeout=300):
    response = studio.c.post("/api/me/screen-lock", json={"enabled": True, "sections": sections, "timeout": timeout})
    assert response.status_code == 200, response.text
    return response.json()


def expire(studio):
    assert studio.c.post("/api/lock/now").status_code == 204


def test_migration_preserves_pin_and_sessions_and_adds_compatible_defaults(env):
    conn = db.connect(env.db_path)
    db.migrate(conn)
    conn.execute("INSERT INTO users(id, login, name, pw_hash, pin_hash, screen_lock, created)"
                 " VALUES (1, 'owner', 'Owner', 'pw', 'pin', 1, 0)")
    conn.execute("INSERT INTO sessions(digest,user_id,csrf,expires,renewed,seen) VALUES ('d',1,'c',99,0,12)")
    conn.executescript(DROP_V24 + "PRAGMA user_version = 23;")
    db.migrate(conn)
    db.migrate(conn)
    row = conn.execute("SELECT pin_hash, screen_lock, screen_lock_sections, screen_lock_timeout, computer_pin_timeout FROM users").fetchone()
    assert tuple(row) == ('pin', 1, '["*"]', 60, 10800)
    assert conn.execute("SELECT seen FROM sessions").fetchone()[0] == 12
    conn.close()


@pytest.mark.parametrize("selected,endpoint", [
    ("chat", "/api/chats"), ("photo", "/api/images"), ("video", "/api/videos"),
    ("transcribe", "/api/transcripts"), ("speech", "/api/speeches"),
    ("ocr", "/api/ocr"), ("translate", "/api/translate"), ("code", "/api/code/place"),
])
def test_each_of_eight_sections_can_be_protected_without_locking_the_shell(studio, selected, endpoint):
    owner(studio)
    enable(studio, [selected])
    expire(studio)
    assert studio.c.get(endpoint).status_code == 423
    assert studio.c.get("/api/me").status_code == 200
    assert studio.c.get("/api/lock").json()["locked"] is False
    assert studio.c.get("/api/lock", params={"section": selected}).json()["locked"] is True
    assert studio.c.get("/api/lock?section=security").json()["locked"] is True
    for section in set(lock_policy.SECTIONS) - {selected}:
        assert studio.c.get("/api/lock", params={"section": section}).json()["locked"] is False
    assert studio.c.get("/api/images" if selected != "photo" else "/api/chats").status_code == 200


def test_all_eight_is_distinct_from_whole_app_and_changes_need_password(studio):
    owner(studio)
    enable(studio, list(lock_policy.SECTIONS))
    for body in ({"enabled": True, "sections": ["code"]}, {"enabled": True, "timeout": 3600}):
        assert studio.c.post("/api/me/screen-lock", json=body).status_code == 403
    assert studio.c.post("/api/me/screen-lock", json={"enabled": True, "sections": ["*"], "timeout": 3600,
                                                      "password": "password-123"}).status_code == 200
    expire(studio)
    assert studio.c.get("/api/me").status_code == 423


@pytest.mark.parametrize("extra", [{"sections": []}, {"sections": ["*", "chat"]}, {"sections": ["dictation"]},
                                   {"sections": ["unknown"]}, {"timeout": -1}, {"timeout": 86401},
                                   {"timeout": True}, {"timeout": "60"}, {"timeout": None}])
def test_invalid_policy_does_not_change_the_account(studio, extra):
    owner(studio)
    assert studio.c.post("/api/me/screen-lock", json={"enabled": True, **extra}).status_code == 422
    assert studio.c.get("/api/me").json()["screen_lock"] is False


def test_disabled_policy_can_be_prepared_without_a_hidden_password_field(studio):
    owner(studio)
    response = studio.c.post("/api/me/screen-lock", json={"enabled": False, "sections": ["photo", "code"], "timeout": 900})
    assert response.status_code == 200
    assert response.json() == {"screen_lock": False, "screen_lock_sections": ["photo", "code"], "screen_lock_timeout": 900}


def test_background_polling_does_not_extend_activity_or_reopen_expired_sections(studio):
    owner(studio)
    enable(studio, ["code"], timeout=300)
    studio.clock[0] += 200
    assert studio.c.post("/api/lock/alive", json={"active": False, "section": "code"}).json() == {"locked": False}
    studio.clock[0] += 100 + lock_policy.HEARTBEAT_GRACE + 1
    assert studio.c.post("/api/lock/alive", json={"active": True, "section": "chat"}).json() == {"locked": False}
    assert studio.c.get("/api/lock?section=code").json()["locked"] is True
    assert studio.c.post("/api/lock/password", json={"password": "password-123"}).status_code == 204
    studio.clock[0] += 250
    assert studio.c.post("/api/lock/alive", json={"active": True, "section": "code"}).json() == {"locked": False}
    studio.clock[0] += 250
    assert studio.c.get("/api/lock?section=code").json()["locked"] is False


def test_zero_timer_keeps_an_active_tab_open_despite_late_marks(studio):
    """Таймер 0 («при возвращении»): видимая вкладка шлёт отметку раз в 15 с (lock.js ALIVE_SEC), запрос
    может ждать ответа до 8 с (MARK_SEC). Опоздание таймера на ±50 мс и медленный ответ не запирают работу;
    молчание дольше допуска — запирает."""
    owner(studio)
    enable(studio, ["*"], timeout=0)
    for gap in (15.05, 14.95, 15.05, 15.05, 15 + 8 + 0.05):
        studio.clock[0] += gap
        assert studio.c.post("/api/lock/alive", json={"active": True}).json() == {"locked": False}, gap
        assert studio.c.get("/api/me").status_code == 200
    studio.clock[0] += lock_policy.HEARTBEAT_GRACE + 0.05
    assert studio.c.get("/api/me").status_code == 423


@pytest.mark.parametrize("selected,accessible", [("photo", True), ("code", False), ("*", False)])
def test_code_stream_is_closed_only_when_the_code_section_is_protected(studio, monkeypatch, selected, accessible):
    owner(studio)
    enable(studio, [selected])
    forgotten = []
    monkeypatch.setattr(bridge, "forget", lambda *a, **kw: forgotten.append(a))
    expire(studio)
    digest = security.digest(studio.c.cookies[auth.COOKIE])
    assert claude_code._session_open(digest) is accessible
    assert bool(forgotten) is not accessible


def test_partial_lock_hides_search_results_models_and_jobs_and_guards_settings(studio):
    uid = owner(studio)
    state.DB.execute(CHAT, ("chat", uid, "secret", "private-model", 1, 1))
    state.DB.execute(MESSAGE, ("message", "chat", "user", "secret text", "private-model", 2))
    state.DB.execute(IMAGE, ("image", uid, "flux", "secret photo", 3))
    enable(studio, ["chat"])
    expire(studio)
    result = studio.c.post("/api/search", json={"q": "secret"}).json()
    assert [(item["section"], item["id"]) for item in result["items"]] == [("photo", "image")]
    assert studio.c.get("/api/search/models").json()["models"] == {"photo": ["flux"]}
    assert all(item["section"] != "chat" for item in studio.c.get("/api/jobs").json()["running"])
    for path, method, body in [("/api/me/screen-lock", "POST", {"enabled": False, "password": "password-123"}),
                               ("/api/history", "DELETE", None), ("/api/me/export", "POST", {"sections": ["chat"]})]:
        assert studio.c.request(method, path, json=body).status_code == 423


def test_media_associated_with_a_closed_section_cannot_be_downloaded(studio):
    uid = owner(studio)
    secret = media.save(uid, b"private", "text/plain", "private.txt")
    public = media.save(uid, b"public", "text/plain", "public.txt")
    state.DB.execute(IMAGE, ("image", uid, "flux", "secret", 1))
    state.DB.execute("UPDATE images SET media_id = ? WHERE id = 'image'", (secret,))
    enable(studio, ["photo"])
    expire(studio)
    assert studio.c.get("/api/media/" + secret).status_code == 423
    response = studio.c.get("/api/media/" + public)
    assert response.status_code == 200 and response.content == b"public"
    assert response.headers["cache-control"] == "no-store"


def test_a_protected_file_cannot_be_reused_as_input_in_an_open_section(studio):
    uid = owner(studio)
    secret = media.save(uid, b"private", "image/png", "private.png")
    state.DB.execute(IMAGE, ("image", uid, "flux", "secret", 1))
    state.DB.execute("UPDATE images SET media_id = ? WHERE id = 'image'", (secret,))
    token = lock_policy.RESTRICTED.set(frozenset({"photo"}))
    try:
        with pytest.raises(HTTPException) as raised:
            media.path(uid, secret)
        assert raised.value.status_code == 423
    finally:
        lock_policy.RESTRICTED.reset(token)


SCAN_ROW = ("INSERT INTO scans(id, user_id, model, name, source_media_id, origin, pages, done_pages, state, detail,"
            " text, created, finished) VALUES (?, ?, 'hunyuan-ocr', ?, ?, ?, 1, 1, 'done', 'готово', ?, ?, ?)")


def _scan(uid, sid, origin, word, now):
    """Готовый скан: исходный PDF и PNG страницы — только у него (не вложение чата и не исходник перевода)."""
    pdf = media.save(uid, b"%PDF-1.4 " + sid.encode(), "application/pdf", f"{sid}.pdf")
    png = media.save(uid, b"png " + sid.encode(), "image/png", f"{sid}.png")
    state.DB.execute(SCAN_ROW, (sid, uid, f"{sid}.pdf", pdf, origin, f"{word} текст", now, now))
    state.DB.execute("INSERT INTO scan_pages(scan_id, user_id, n, media_id, state, text) VALUES (?, ?, 1, ?, 'done', ?)",
                     (sid, uid, png, f"{word} текст"))
    return pdf, png


@pytest.mark.parametrize("closed", ["chat", "translate"])
def test_scan_made_by_a_protected_section_is_closed_everywhere(studio, closed):
    """Скан вложения чата и скан перевода — данные своего раздела: защита «Чата» или «Перевода» закрывает
    его ленту, документ, выгрузку, поиск, «В работе», PNG страниц и исходный PDF, хотя «Сканы» открыты."""
    uid = owner(studio)
    now = studio.clock[0]
    hidden_pdf, hidden_png = _scan(uid, "hidden", "chat" if closed == "chat" else "section", "тайное", now)
    if closed == "translate":
        state.DB.execute("INSERT INTO translations(id, user_id, kind, model, source_lang, target_lang, name, scan_id,"
                         " state, detail, created) VALUES ('tr', ?, 'file', 'm', 'auto', 'ru', 'tr.pdf', 'hidden',"
                         " 'done', 'готово', ?)", (uid, now))
    open_pdf, open_png = _scan(uid, "open", "section", "открытое", now)
    enable(studio, [closed])
    expire(studio)
    assert [i["id"] for i in studio.c.get("/api/ocr").json()["items"]] == ["open"]
    for path in ("/api/ocr/hidden", "/api/ocr/hidden?brief=1", "/api/ocr/hidden/export?format=txt",
                 "/api/media/" + hidden_png, "/api/media/" + hidden_pdf):
        assert studio.c.get(path).status_code == 423, path
    for path in ("/api/ocr/open", "/api/ocr/open/export?format=txt", "/api/media/" + open_png, "/api/media/" + open_pdf):
        assert studio.c.get(path).status_code == 200, path
    assert studio.c.post("/api/ocr/hidden/attachments").status_code == 423
    assert studio.c.post("/api/search", json={"q": "тайное"}).json()["items"] == []
    assert [i["id"] for i in studio.c.post("/api/search", json={"q": "открытое"}).json()["items"]] == ["open"]
    jobs = studio.c.get("/api/jobs").json()
    assert [i["id"] for i in jobs["finished"] if i["section"] == "ocr"] == ["open"]
    # запрещённый файл не берётся и входом открытого раздела
    token = lock_policy.RESTRICTED.set(frozenset({closed}))
    try:
        with pytest.raises(HTTPException):
            media.path(uid, hidden_png)
    finally:
        lock_policy.RESTRICTED.reset(token)
    assert studio.c.post("/api/lock/password", json={"password": "password-123"}).status_code == 204
    assert {i["id"] for i in studio.c.get("/api/ocr").json()["items"]} == {"hidden", "open"}
    assert studio.c.get("/api/ocr/hidden").status_code == 200


#: голова пути /api/<голова>/… → раздел: всё под ней закрывает защита этого раздела
SECTION_HEADS = {"chats": "chat", "attachments": "chat", "images": "photo", "presets": "photo", "videos": "video",
                 "transcripts": "transcribe", "transcribe": "transcribe", "voices": "speech", "speeches": "speech",
                 "speech": "speech", "ocr": "ocr", "translate": "translate", "code": "code", "agents": "code"}
#: прочие маршруты /api/* — явно. "closed" — закрыт любой частичной защитой (управление защитой, уведомлениями,
#: ключами, общие выгрузки), "open" — открыт (данные без истории или отфильтрованные по закрытым разделам),
#: "lock" — запросы самой блокировки, "media" — по разделам файла, раздел — только при его защите.
#: Новый маршрут без строки здесь роняет тест: решите, к чему он относится, и научите этому lock_policy.blocks.
ROUTES = {
    **{(m, p): "closed" for m, p in [
        ("GET", "/api/admin/allowlist"), ("PUT", "/api/admin/allowlist"), ("GET", "/api/admin/backup-check"),
        ("POST", "/api/admin/backup-check"), ("GET", "/api/admin/storage"), ("GET", "/api/admin/users"),
        ("POST", "/api/admin/users"), ("DELETE", "/api/admin/users/{uid}"), ("PATCH", "/api/admin/users/{uid}"),
        ("GET", "/api/admin/users/{uid}/connections"), ("POST", "/api/admin/users/{uid}/connections"),
        ("DELETE", "/api/admin/users/{uid}/connections/{conn_id}"),
        ("PATCH", "/api/admin/users/{uid}/connections/{conn_id}"), ("GET", "/api/admin/users/{uid}/sections"),
        ("PUT", "/api/admin/users/{uid}/sections/{section}/default"),
        ("POST", "/api/admin/users/{uid}/sections/{section}/links"),
        ("DELETE", "/api/admin/users/{uid}/sections/{section}/links/{conn_id}"),
        ("PUT", "/api/admin/users/{uid}/sections/{section}/links/{conn_id}"),
        ("GET", "/api/admin/users/{uid}/sections/{section}/links/{conn_id}/found"),
        ("DELETE", "/api/history"), ("PATCH", "/api/me"), ("DELETE", "/api/me/exa-key"), ("PUT", "/api/me/exa-key"),
        ("POST", "/api/me/export"), ("GET", "/api/me/export/{token}"), ("DELETE", "/api/me/ntfy"),
        ("PUT", "/api/me/ntfy"), ("POST", "/api/me/ntfy/test"), ("POST", "/api/me/password"),
        ("DELETE", "/api/me/pin"), ("PUT", "/api/me/pin"), ("DELETE", "/api/me/push"), ("POST", "/api/me/push"),
        ("POST", "/api/me/screen-lock"), ("GET", "/api/me/sessions"), ("DELETE", "/api/me/sessions/{sid}"),
        ("POST", "/api/me/emergency"), ("GET", "/api/me/security-events"),
        ("POST", "/api/media/archives"), ("GET", "/api/media/archives/{token}"),
        ("GET", "/api/passkeys"), ("POST", "/api/passkeys"), ("POST", "/api/passkeys/options"),
        ("DELETE", "/api/passkeys/{key_id}")]},
    **{(m, p): "open" for m, p in [
        ("GET", "/api/agent/download"), ("DELETE", "/api/agent/enroll"), ("POST", "/api/agent/enroll"),
        ("WS", "/api/agent/link"), ("GET", "/api/agent/package"),
        ("GET", "/api/connections"), ("POST", "/api/connections"), ("DELETE", "/api/connections/{conn_id}"),
        ("PATCH", "/api/connections/{conn_id}"), ("POST", "/api/connections/{conn_id}/check"),
        ("GET", "/api/connections/{conn_id}/models"), ("PUT", "/api/connections/{conn_id}/passport"),
        ("POST", "/api/connections/{conn_id}/probe"), ("POST", "/api/dictation"), ("GET", "/api/jobs"),
        ("GET", "/api/me"), ("GET", "/api/me/ntfy"), ("GET", "/api/me/push"), ("GET", "/api/me/storage"),
        ("POST", "/api/search"), ("GET", "/api/search/models"), ("GET", "/api/sections")]},
    **{(m, p): "lock" for m, p in [
        ("GET", "/api/lock"), ("POST", "/api/lock/alive"), ("POST", "/api/lock/challenge"), ("POST", "/api/lock/now"),
        ("POST", "/api/lock/open"), ("POST", "/api/lock/password"), ("POST", "/api/lock/pin")]},
    ("GET", "/api/media/{media_id}"): "media",
    ("GET", "/api/connections/{conn_id}/token"): "chat",            # ключ прямого подключения — данные чата
    **{(m, p): "path" for m, p in [                                 # раздел — из адреса
        ("PUT", "/api/sections/{section}/default"), ("POST", "/api/sections/{section}/links"),
        ("DELETE", "/api/sections/{section}/links/{conn_id}"), ("PUT", "/api/sections/{section}/links/{conn_id}"),
        ("GET", "/api/sections/{section}/links/{conn_id}/found")]},
}


def _api_routes():
    import main

    def walk(routes):
        for route in routes:
            if hasattr(route, "original_router"):           # FastAPI: подключённый роутер
                yield from walk(route.original_router.routes)
            else:
                yield route

    for route in walk(main.app.routes):
        path = getattr(route, "path", "")
        if path.startswith("/api"):
            for method in sorted(getattr(route, "methods", None) or ["WS"]):
                yield method, path


def test_every_api_route_is_classified_for_partial_protection(studio):
    """Реестр: каждый маршрут /api/* — раздела (по голове пути), общий закрываемый, открытый, блокировки или
    файла; и lock_policy.blocks ведёт себя так при защите любого одного раздела из восьми."""
    from types import SimpleNamespace
    uid = owner(studio)
    digest = security.digest(studio.c.cookies[auth.COOKIE])
    routes = sorted(set(_api_routes()))
    unknown = [r for r in routes if r[1].split("/")[2] not in SECTION_HEADS and r not in ROUTES]
    assert not unknown, f"маршруты без классификации: {unknown}"
    assert not set(ROUTES) - set(routes), "в реестре — маршруты, которых уже нет"
    for selected in lock_policy.SECTIONS:
        state.DB.execute("UPDATE users SET screen_lock = 1, screen_lock_sections = ? WHERE id = ?",
                         (f'["{selected}"]', uid))
        state.DB.execute("UPDATE sessions SET seen = NULL WHERE digest = ?", (digest,))
        for method, path in routes:
            kind = SECTION_HEADS.get(path.split("/")[2]) or ROUTES[(method, path)]
            if kind == "lock":
                assert path in auth.SCREEN_LOCK_PATHS
                continue
            if kind == "open" and (path in auth.OPEN_API or method == "WS"):
                continue                                   # guard их не стережёт: свой вход
            concrete = path.replace("{section}", selected).replace("{", "x").replace("}", "")
            request = SimpleNamespace(url=SimpleNamespace(path=concrete), method=method)
            expected = {"closed": True, "open": False, "media": False, "path": True}.get(kind, kind == selected)
            assert lock_policy.blocks(request, uid, digest) is expected, (selected, method, path)


def test_partial_protection_closes_notification_and_key_changes(studio):
    """Защищены только «Агенты»: перенаправить их уведомления на своё устройство (ntfy, push) или сменить ключ
    Exa нельзя; читать свои настройки — можно."""
    from test_push import Device
    owner(studio)
    enable(studio, ["code"])
    expire(studio)
    ntfy = {"server": "https://psh.example.net", "topic": "thief", "enabled": True}
    for method, path, body in [("PUT", "/api/me/ntfy", ntfy), ("DELETE", "/api/me/ntfy", None),
                               ("POST", "/api/me/ntfy/test", None), ("POST", "/api/me/push", Device().body()),
                               ("DELETE", "/api/me/push", None), ("PUT", "/api/me/exa-key", {"key": "k"}),
                               ("DELETE", "/api/me/exa-key", None)]:
        assert studio.c.request(method, path, json=body).status_code == 423, path
    assert studio.c.get("/api/me/ntfy").status_code == 200
    assert studio.c.get("/api/me/push").status_code == 200


def test_computer_timer_is_persisted_and_relayed_to_capable_bridge(studio):
    aid, machine, link = setup(studio)
    response = studio.c.patch("/api/me", json={"computer_pin_timeout": 600})
    assert response.status_code == 200 and response.json()["computer_pin_timeout"] == 600
    before = len(machine.requests)
    assert studio.c.get(url(aid, "projects")).status_code == 409  # старый агент не игнорирует настройку молча
    assert len(machine.requests) == before
    link.hello["pin_timeout"] = True
    assert studio.c.get(url(aid, "projects")).status_code == 200
    assert machine.requests[-1]["idle_sec"] == 600
    assert studio.c.get("/api/me").json()["screen_lock_timeout"] == 60
    # Мост пропал (перезапуск, сон): «не на связи», страница ждёт его возвращения, а не просит обновления.
    fake_bridge.drop(studio, link)
    response = studio.c.get(url(aid, "projects"))
    assert (response.status_code, response.json()["detail"]["code"]) == (409, "offline")


def test_background_polls_are_passive_only_for_a_capable_bridge(studio):
    aid, machine, link = setup(studio)
    git = url(aid, "projects", "0" * 16, "git")
    assert studio.c.get(git, headers={"x-studio-passive": "1"}).status_code in (200, 404, 409)
    assert "passive" not in machine.requests[-1]  # старый мост не знает поля
    link.hello["passive_requests"] = True
    studio.c.get(git, headers={"x-studio-passive": "1"})
    assert machine.requests[-1]["passive"] is True
    studio.c.get(git)
    assert "passive" not in machine.requests[-1]  # действие владельца продлевает срок, как прежде
    link.hello["project_activity"] = True
    assert studio.c.get("/api/code/activity").status_code == 200  # значок раздела — всегда фоновый
    assert machine.requests[-1]["op"] == "activity" and machine.requests[-1]["passive"] is True


@pytest.mark.parametrize("value", [0, 59, 604801, True, None, "600"])
def test_invalid_computer_timer_is_rejected(studio, value):
    owner(studio)
    assert studio.c.patch("/api/me", json={"computer_pin_timeout": value}).status_code == 422
    assert studio.c.get("/api/me").json()["computer_pin_timeout"] == 10800
