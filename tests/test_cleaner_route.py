"""Настройки шлюза Cleaner для внешних моделей: администратор, служебный ключ шифром, шаг схемы 64 и откат."""
import json
import logging

import httpx
import pytest
from cryptography.fernet import Fernet, InvalidToken

import cleaner_route
import security
import state
from conftest import make_user, signin

ADDRESS = "https://10.77.1.20:5464"
KEY = "ck_" + "a" * 43


def allow(studio, *entries):
    """Список разрешённых адресов администратора: к умолчанию добавляются записи теста."""
    state.DB.execute("UPDATE settings SET value = ? WHERE key = 'allowlist'",
                     (json.dumps(["10.77.1.15:4000", *entries]),))


def admin(studio):
    make_user(studio, "boss", is_admin=True)
    signin(studio, "boss")


def save(studio, **body):
    return studio.c.put("/api/admin/cleaner", json={"base_url": ADDRESS, "key": KEY, "forced": False} | body)


def test_admin_saves_cleaner_settings_and_key_never_returns(studio):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    response = save(studio)
    assert response.status_code == 200
    assert response.json() == {"base_url": ADDRESS, "key_set": True, "forced": False, "reenter": False}
    assert KEY not in response.text + studio.c.get("/api/admin/cleaner").text
    raw = state.DB.execute("SELECT value FROM settings WHERE key = ?", (cleaner_route.SETTINGS_KEY,)).fetchone()[0]
    assert KEY.encode() not in raw and KEY in security.decrypt(raw)
    assert cleaner_route.configured() and not cleaner_route.forced()


def test_empty_settings_before_first_save(studio):
    admin(studio)
    assert studio.c.get("/api/admin/cleaner").json() == {"base_url": "", "key_set": False, "forced": False,
                                                          "reenter": False}
    assert not cleaner_route.configured() and not cleaner_route.forced()


@pytest.mark.parametrize("address", ["https://10.77.1.99:5464", "https://cleaner.example.com"])
def test_unlisted_cleaner_address_is_refused(studio, address):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    response = save(studio, base_url=address)
    assert response.status_code == 422 and response.json()["detail"]["code"] == "server"
    assert "в списке" in response.json()["detail"]["message"]
    assert studio.c.get("/api/admin/cleaner").json()["base_url"] == ""


@pytest.mark.parametrize("address", [ADDRESS + "/api", "ftp://10.77.1.20:5464", "https://user:pw@10.77.1.20:5464",
                                     ADDRESS + "?a=1"])
def test_cleaner_address_is_a_bare_origin(studio, address):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    response = save(studio, base_url=address)
    assert response.status_code == 422 and response.json()["detail"]["code"] == "url"


def test_trailing_slash_is_dropped(studio):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    assert save(studio, base_url=ADDRESS + "/").json()["base_url"] == ADDRESS


def test_forced_needs_address_and_key(studio):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    for body in ({"base_url": "", "key": KEY}, {"key": None}, {"key": ""}):
        response = save(studio, forced=True, **body)
        assert response.status_code == 422 and response.json()["detail"]["code"] == "cleaner", body
    assert not cleaner_route.forced()
    assert save(studio, forced=True).json()["forced"] is True and cleaner_route.forced()


def test_bad_key_is_refused_without_echo(studio):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    response = save(studio, key="ключ с пробелом")
    assert response.status_code == 422 and response.json()["detail"]["code"] == "key"
    assert "ключ с пробелом" not in response.text


def test_empty_key_keeps_the_previous(studio):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    save(studio)
    response = save(studio, key=None, forced=True)
    assert response.json() == {"base_url": ADDRESS, "key_set": True, "forced": True, "reenter": False}
    assert cleaner_route.settings()["key"] == KEY
    assert save(studio, key="").json()["key_set"] is True and cleaner_route.settings()["key"] == KEY


def test_new_data_key_asks_to_reenter(studio):
    """Смена STUDIO_DATA_KEY: настройки не расшифровать — панель просит ввести их заново, принудительный режим выключен."""
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    save(studio, forced=True)
    state.DB.execute("UPDATE settings SET value = ? WHERE key = ?",
                     (Fernet(Fernet.generate_key()).encrypt(b'{"forced": true}'), cleaner_route.SETTINGS_KEY))
    assert studio.c.get("/api/admin/cleaner").json() == {"base_url": "", "key_set": False, "forced": False,
                                                          "reenter": True}
    assert not cleaner_route.configured() and not cleaner_route.forced()
    assert studio.c.post("/api/admin/cleaner/check").status_code == 503
    assert save(studio, key=None, forced=True).status_code == 422          # прежнего ключа нет
    response = save(studio)
    assert response.status_code == 200 and response.json()["reenter"] is False and response.json()["key_set"] is True


def test_settings_are_admin_only(studio):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    make_user(studio, "member")
    signin(studio, "member")
    for method, path, body in [("GET", "/api/admin/cleaner", None),
                               ("PUT", "/api/admin/cleaner", {"base_url": ADDRESS, "key": KEY}),
                               ("POST", "/api/admin/cleaner/check", None)]:
        assert studio.c.request(method, path, json=body).status_code == 403, path
    assert not cleaner_route.configured()


def test_settings_change_is_logged_without_values(studio, caplog):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    caplog.set_level(logging.INFO, logger="studio")
    save(studio, forced=True)
    assert "cleaner: настройки сохранены" in caplog.text
    assert KEY not in caplog.text and "10.77.1.20" not in caplog.text


def test_changing_address_or_key_queues_contexts_of_the_new_kind(studio):
    """Контексты обычных подключений (`cleaner = 0`) уходят в очередь со снимком прежнего адреса и ключа; контекст
    прежнего вида Cleaner (`cleaner = 1`, личный ключ) остаётся."""
    uid = make_user(studio, "owner")
    admin(studio)
    allow(studio, "10.77.1.20:5464", "10.77.1.21:5464")
    save(studio)
    add = ("INSERT INTO connections(user_id, name, kind, base_url, token_enc, sections, created, server_only, cleaner)"
           " VALUES (?, ?, 'openai', 'https://api.cloud.test/v1', ?, '[]', 0, 1, ?)")
    plain = state.DB.execute(add, (uid, "plain", security.encrypt("provider"), 0)).lastrowid
    legacy = state.DB.execute(add, (uid, "legacy", security.encrypt("personal"), 1)).lastrowid
    context = "INSERT INTO cleaner_contexts VALUES (?, ?, 'chat', ?, ?, ?, ?, ?, 0)"
    state.DB.execute(context, ("ctx-new", uid, "c1", plain, "remote-new", ADDRESS, security.encrypt(KEY)))
    state.DB.execute(context, ("ctx-old", uid, "c2", legacy, "remote-old", "https://x.test", security.encrypt("personal")))

    save(studio, forced=True)                                             # адрес и ключ прежние — ничего не уходит
    assert state.DB.execute("SELECT count(*) FROM cleaner_deletions").fetchone()[0] == 0
    save(studio, base_url="https://10.77.1.21:5464")
    rows = state.DB.execute("SELECT id, remote_id, base_url, token_enc FROM cleaner_deletions").fetchall()
    assert [(r["id"], r["remote_id"], r["base_url"]) for r in rows] == [("ctx-new", "remote-new", ADDRESS)]
    assert security.decrypt(rows[0]["token_enc"]) == KEY
    assert [r[0] for r in state.DB.execute("SELECT id FROM cleaner_contexts")] == ["ctx-old"]

    state.DB.execute(context, ("ctx-new2", uid, "c3", plain, "remote-new2", "https://10.77.1.21:5464",
                               security.encrypt(KEY)))
    save(studio, base_url="https://10.77.1.21:5464", key="ck_" + "b" * 43)           # только ключ
    assert {r[0] for r in state.DB.execute("SELECT id FROM cleaner_deletions")} == {"ctx-new", "ctx-new2"}
    assert [r[0] for r in state.DB.execute("SELECT id FROM cleaner_contexts")] == ["ctx-old"]


# ---- самопроверка ------------------------------------------------------------------

def check_handler(studio, status=200, body=None):
    seen = []

    async def handle(request):
        seen.append(request)
        return httpx.Response(status, json=body if body is not None else {"ok": True, "checks": [
            {"name": "key", "ok": True}, {"name": "layers", "ok": True}]})

    studio.gw.on["/agent/openai/v1/cleaner/check"] = handle
    return seen


def test_check_sends_only_the_service_key_header(studio):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    save(studio)
    seen = check_handler(studio)
    response = studio.c.post("/api/admin/cleaner/check")
    assert response.status_code == 200 and response.json()["ok"] is True and len(response.json()["checks"]) == 2
    assert seen[0].headers["x-cleaner-key"] == KEY and "authorization" not in seen[0].headers
    assert str(seen[0].url) == ADDRESS + "/agent/openai/v1/cleaner/check"
    assert KEY not in response.text


def test_check_needs_settings(studio):
    admin(studio)
    response = studio.c.post("/api/admin/cleaner/check")
    assert response.status_code == 503 and response.json()["detail"]["code"] == "cleaner_unset"


@pytest.mark.parametrize("status", [401, 403])
def test_check_reports_a_refused_key_in_words(studio, status):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    save(studio)
    check_handler(studio, status, {"error": {"message": "Cleaner: bad key " + KEY}})
    response = studio.c.post("/api/admin/cleaner/check")
    assert response.status_code == 502 and "служебный ключ" in response.json()["detail"]["message"]
    assert KEY not in response.text


def test_check_when_cleaner_is_down_or_unlisted(studio, caplog):
    admin(studio)
    allow(studio, "10.77.1.20:5464")
    save(studio)
    caplog.set_level(logging.INFO, logger="studio")
    studio.gw.down = True
    response = studio.c.post("/api/admin/cleaner/check")
    assert response.status_code == 502 and response.json()["detail"]["message"] == "Cleaner недоступен"
    allow(studio)                                                       # адрес убран из списка
    response = studio.c.post("/api/admin/cleaner/check")
    assert response.status_code == 502 and "списка" in response.json()["detail"]["message"]
    assert KEY not in caplog.text and "10.77.1.20" not in caplog.text


# ---- шаг схемы 64 и откат ------------------------------------------------------------

def test_rollback_64_spoils_routed_tokens_and_forward_restores(studio, caplog):
    uid = make_user(studio, "owner")
    add = ("INSERT INTO connections(user_id, name, kind, base_url, token_enc, sections, created, server_only, via_cleaner)"
           " VALUES (?, ?, 'openai', 'https://api.cloud.test/v1', ?, '[]', 0, 1, ?)")
    routed = state.DB.execute(add, (uid, "routed", security.encrypt("provider-key"), 1)).lastrowid
    plain = state.DB.execute(add, (uid, "plain", security.encrypt("plain-key"), 0)).lastrowid

    def token(cid):
        return state.DB.execute("SELECT token_enc FROM connections WHERE id = ?", (cid,)).fetchone()[0]

    state.DB.executescript(cleaner_route.ROLLBACK_64)
    with pytest.raises(InvalidToken):
        security.decrypt(token(routed))                                 # прежний код: «токен не расшифрован»
    assert security.decrypt(token(plain)) == "plain-key"
    state.DB.executescript(cleaner_route.ROLLBACK_64)                   # повтор не затирает сохранённый ключ
    state.DB.executescript(cleaner_route.FORWARD_64)
    assert security.decrypt(token(routed)) == "provider-key" and security.decrypt(token(plain)) == "plain-key"
    assert state.DB.execute("SELECT count(*) FROM cleaner_rollback").fetchone()[0] == 0
    state.DB.executescript(cleaner_route.FORWARD_64)                    # повтор без строк ничего не меняет
    assert security.decrypt(token(routed)) == "provider-key"

    state.DB.executescript(cleaner_route.ROLLBACK_64)
    state.DB.execute("UPDATE connections SET token_enc = ? WHERE id = ?", (security.encrypt("typed-again"), routed))
    state.DB.executescript(cleaner_route.FORWARD_64)                    # ключ, введённый заново прежним кодом, остаётся
    assert security.decrypt(token(routed)) == "typed-again"

    caplog.set_level(logging.INFO, logger="studio")
    cleaner_route.forward()                                             # строк нет — ничего не делает и не пишет
    assert "после отката" not in caplog.text
    state.DB.executescript(cleaner_route.ROLLBACK_64)
    cleaner_route.forward()
    assert security.decrypt(token(routed)) == "typed-again"
    assert "после отката возвращено ключей 1" in caplog.text
    assert state.DB.execute("SELECT count(*) FROM cleaner_rollback").fetchone()[0] == 0


def test_rollback_64_skips_a_token_already_spoiled_by_the_access_rollback(studio):
    """Ключ, уже испорченный откатом шага 56, не сохраняется как «настоящий»: вперёд вернётся только он."""
    uid = make_user(studio, "owner")
    cid = state.DB.execute(
        "INSERT INTO connections(user_id, name, kind, base_url, token_enc, sections, created, server_only, via_cleaner)"
        " VALUES (?, 'routed', 'openai', 'https://api.cloud.test/v1', x'00', '[]', 0, 1, 1)", (uid,)).lastrowid
    state.DB.executescript(cleaner_route.ROLLBACK_64)
    assert state.DB.execute("SELECT count(*) FROM cleaner_rollback WHERE connection_id = ?", (cid,)).fetchone()[0] == 0
